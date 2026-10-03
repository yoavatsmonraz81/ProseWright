"""Phase 3 — propagation sweep (the forward consistency check).

After a transform commits changes to the log, the sweep scans everything
DOWNSTREAM of the changed span and asks: does any of it still make sense?

Three things can break:
  - A CALLBACK: downstream text quotes or refers to a line that changed.
  - A FACT: downstream text asserts a fact that the change contradicted.
  - A CONTINUITY SLIP: a character's knowledge/state was altered, and later
    text still assumes the old state.

The sweep is two stages:
  1. RETRIEVE: use the index (fused semantic + keyword) to pull downstream
     messages that discuss the same entities as the changed content.  Falls
     back to a lightweight keyword scan if the index isn't built.
  2. JUDGE: feed candidates + a change summary to the LLM in one batch call
     and ask for a CLEAN / REVIEW / BREAK verdict per passage.

The result is a SweepReport.  Only REVIEW and BREAK hits are displayed; CLEAN
passages are suppressed (counted but not shown).  Nothing is rewritten —
propose-then-approve still applies at this stage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm, loader, text as text_mod
from .transform import Edit, EditSet, write_pending_edits


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# How many downstream candidates to retrieve per changed message.
CANDIDATES_PER_CHANGE = 6
# Hard cap on total candidates fed to the LLM (keeps the prompt manageable).
MAX_CANDIDATES = 20
# Excerpt shown to the LLM per candidate (chars).
CANDIDATE_EXCERPT_CHARS = 400
# Summary of each change shown to the LLM (chars).
CHANGE_SUMMARY_CHARS = 300

# Analytical task — we want sober, consistent verdicts.
SWEEP_TEMPERATURE = 0.2


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class SweepHit:
    msg_id: int
    speaker: str
    excerpt: str
    verdict: str   # "CLEAN" | "REVIEW" | "BREAK"
    reason: str

    def to_json(self) -> dict:
        return {
            "msg_id": self.msg_id, "speaker": self.speaker,
            "excerpt": self.excerpt, "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass
class SweepReport:
    changed_from: int
    changed_to: int
    sweep_from: int
    operator: str
    note: str
    hits: list[SweepHit] = field(default_factory=list)
    clean_count: int = 0
    verdict: str = "CLEAN"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict:
        return {
            "changed_from": self.changed_from, "changed_to": self.changed_to,
            "sweep_from": self.sweep_from, "operator": self.operator,
            "note": self.note, "hits": [h.to_json() for h in self.hits],
            "clean_count": self.clean_count, "verdict": self.verdict,
            "created": self.created,
        }


# --------------------------------------------------------------------------- #
# Label collection — director-labelled consistency judgments.
# --------------------------------------------------------------------------- #

@dataclass
class ConsistencyLabel:
    session_id: str
    operator: str
    note: str
    cycle: int
    changed_from: int
    changed_to: int
    candidate_msg_id: int
    candidate_speaker: str
    candidate_excerpt: str
    llm_verdict: str
    director_action: str   # "patched" | "dismissed" | "skipped"
    source: str = "director"   # "director" | "synthetic"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def append_label(label: ConsistencyLabel) -> None:
    """Append one label entry to the JSONL label file (never overwrites)."""
    with config.CONSISTENCY_LABELS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(label.to_json(), ensure_ascii=False) + "\n")


def label_stats() -> dict:
    """Return a summary dict over the label file, or an empty dict if missing."""
    if not config.CONSISTENCY_LABELS.exists():
        return {}
    counts: dict[str, int] = {}
    total = 0
    for line in config.CONSISTENCY_LABELS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            action = d.get("director_action", "unknown")
            counts[action] = counts.get(action, 0) + 1
            total += 1
        except json.JSONDecodeError:
            pass
    return {"total": total, "by_action": counts}


# --------------------------------------------------------------------------- #
# Cascade result — convergence tracking across cycles.
# --------------------------------------------------------------------------- #

@dataclass
class CascadeResult:
    cycles_run: int
    converged: bool
    reason: str          # "no_breaks" | "plateau" | "max_cycles"
    break_history: list[int]    # BREAK count per cycle
    review_counts: list[int]    # REVIEW count per cycle (informational)
    labels_written: int
    total_patched: int


# --------------------------------------------------------------------------- #
# Patch proposal — single-message restyle for BREAK/REVIEW hits.
# --------------------------------------------------------------------------- #

def propose_patch(
    log_path: str | Path,
    hit: SweepHit,
    change_summary: str,
    *,
    on_progress=None,
) -> "Edit | None":
    """Generate a restyle Edit for a flagged passage so it can be reconciled with
    upstream changes. Returns None if the model returns the original unchanged."""
    from . import transform as transform_mod
    note = (
        f"Reconcile with upstream change — {change_summary[:220]}. "
        f"Specific concern: {hit.reason}"
    )
    if on_progress:
        on_progress(f"proposing patch for msg {hit.msg_id} ({hit.speaker})…")
    return transform_mod.single_message_restyle(log_path, hit.msg_id, note)


# --------------------------------------------------------------------------- #
# Sweep context — persisted after commit so `sweep --last` works.
# --------------------------------------------------------------------------- #

@dataclass
class SweepContext:
    log: str
    operator: str
    note: str
    changed_from: int
    changed_to: int
    edits: list[dict]   # serialised Edit.to_json() for each changed message

    def to_json(self) -> dict:
        return {
            "log": self.log, "operator": self.operator, "note": self.note,
            "changed_from": self.changed_from, "changed_to": self.changed_to,
            "edits": self.edits,
        }

    @classmethod
    def from_json(cls, d: dict) -> "SweepContext":
        return cls(
            log=d["log"], operator=d["operator"], note=d["note"],
            changed_from=d["changed_from"], changed_to=d["changed_to"],
            edits=d.get("edits", []),
        )


def save_sweep_context(ctx: SweepContext) -> None:
    config.LAST_SWEEP_CONTEXT.write_text(
        json.dumps(ctx.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_sweep_context() -> SweepContext | None:
    if not config.LAST_SWEEP_CONTEXT.exists():
        return None
    return SweepContext.from_json(
        json.loads(config.LAST_SWEEP_CONTEXT.read_text(encoding="utf-8"))
    )


def context_from_edit_set(
    log_path: str | Path,
    operator: str,
    note: str,
    edits: list[Edit],
) -> SweepContext:
    msg_ids = [e.msg_id for e in edits]
    changed_from = min(msg_ids) if msg_ids else 0
    changed_to = max(msg_ids) if msg_ids else 0
    # After a hard delete later turns slide down. The first surviving turn at
    # or after the cut now lives at min(removed). Sweep starts there — callers
    # use changed_to + 1, so store the seam minus one.
    removes = [e for e in edits if e.kind == "remove"]
    if removes:
        seam = min(e.msg_id for e in removes)
        changed_from = seam
        changed_to = seam - 1
    return SweepContext(
        log=str(Path(log_path).resolve()),
        operator=operator, note=note,
        changed_from=changed_from,
        changed_to=changed_to,
        edits=[e.to_json() for e in edits],
    )


# --------------------------------------------------------------------------- #
# Candidate retrieval
# --------------------------------------------------------------------------- #

def _clean_excerpt(text: str, max_chars: int = CANDIDATE_EXCERPT_CHARS) -> str:
    body = " ".join(text_mod.clean(text).split())
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return body


def _keyword_fallback(
    log: loader.Log,
    queries: list[str],
    sweep_from: int,
    limit: int,
) -> list[loader.Message]:
    """Simple word-overlap fallback when the index is not built. Scores each
    downstream message by how many query words it contains."""
    words: set[str] = set()
    for q in queries:
        words.update(w.lower() for w in re.findall(r"[a-zA-Z']{4,}", q))
    words -= {"that", "this", "with", "from", "have", "been", "were", "they",
              "their", "what", "when", "will", "would", "could", "should",
              "said", "asked", "just", "very", "also", "then", "than"}
    scored: list[tuple[int, loader.Message]] = []
    for m in log.messages:
        if m.msg_id < sweep_from:
            continue
        body = text_mod.clean(m.text).lower()
        score = sum(1 for w in words if w in body)
        if score > 0:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:limit]]


def retrieve_candidates(
    log_path: str | Path,
    log: loader.Log,
    edits: list[Edit],
    sweep_from: int,
) -> list[loader.Message]:
    """Return up to MAX_CANDIDATES downstream messages most likely to be
    affected by the changes.  Uses the fused index if available, keyword
    fallback otherwise."""
    db_path = config.index_db_for(log_path)
    use_index = Path(db_path).exists()

    seen: set[int] = set()
    candidates: list[loader.Message] = []

    # Build queries from the before-text (what was there, now changed) plus
    # any new key entity from the after-text (for injects).
    queries: list[str] = []
    for e in edits:
        if e.before:
            queries.append(text_mod.clean(e.before)[:400])
        if e.kind == "inject" and e.after:
            queries.append(text_mod.clean(e.after)[:400])

    if not queries:
        return []

    if use_index:
        from .index.embed import default_embedder
        from .index.search import search
        embedder = default_embedder()
        for q in queries:
            hits = search(
                db_path, q,
                limit=CANDIDATES_PER_CHANGE,
                embedder=embedder,
                msg_id_from=sweep_from,
                is_interlude=False,
            )
            for h in hits:
                if h.msg_id not in seen:
                    seen.add(h.msg_id)
                    try:
                        candidates.append(log.get(h.msg_id))
                    except IndexError:
                        pass
            if len(candidates) >= MAX_CANDIDATES:
                break
    else:
        msgs = _keyword_fallback(log, queries, sweep_from, MAX_CANDIDATES)
        for m in msgs:
            if m.msg_id not in seen:
                seen.add(m.msg_id)
                candidates.append(m)

    # Sort by position and cap.
    candidates.sort(key=lambda m: m.msg_id)
    return candidates[:MAX_CANDIDATES]


# --------------------------------------------------------------------------- #
# LLM consistency judgment
# --------------------------------------------------------------------------- #

SWEEP_SYSTEM = (
    "You are a story continuity editor. You are given a summary of recent changes "
    "made to an existing prose roleplay and a set of DOWNSTREAM passages that may "
    "have been affected.\n\n"
    "For each downstream passage, classify it as:\n"
    "- CLEAN: reads naturally and is unaffected by the changes.\n"
    "- REVIEW: might be subtly affected — worth a human look, but not certainly "
    "broken. Flag this if the passage refers to a character's state, tone, or "
    "knowledge that the changes touched, even if no hard contradiction exists.\n"
    "- BREAK: clearly inconsistent — quotes a line that was removed, contradicts "
    "a new fact, or assumes a character's old state when it changed.\n\n"
    "Be conservative: prefer REVIEW over CLEAN when uncertain. Prefer REVIEW over "
    "BREAK unless the inconsistency is clear. Give a concise one-sentence reason.\n\n"
    "Output a JSON array — one object per passage — with exactly these fields:\n"
    '  {"msg_id": <int>, "verdict": "CLEAN"|"REVIEW"|"BREAK", "reason": "<str>"}\n'
    "Output ONLY the JSON array, no preamble or commentary."
)


def _change_summary(edits: list[Edit]) -> str:
    lines: list[str] = []
    for e in edits:
        if e.kind == "inject":
            snippet = " ".join(text_mod.clean(e.after).split())[:CHANGE_SUMMARY_CHARS]
            lines.append(f"  INJECT after msg {e.msg_id} (speaker {e.speaker}): {snippet}")
        elif e.kind == "remove":
            snippet = " ".join(text_mod.clean(e.before).split())[:CHANGE_SUMMARY_CHARS]
            lines.append(f"  REMOVED msg {e.msg_id} (speaker {e.speaker}): {snippet}")
        else:
            before = " ".join(text_mod.clean(e.before).split())[:CHANGE_SUMMARY_CHARS // 2]
            after = " ".join(text_mod.clean(e.after).split())[:CHANGE_SUMMARY_CHARS // 2]
            lines.append(
                f"  CHANGED msg {e.msg_id} ({e.speaker}):\n"
                f"    BEFORE: {before}\n"
                f"    AFTER:  {after}"
            )
    return "\n".join(lines)


def build_sweep_messages(
    edits: list[Edit],
    candidates: list[loader.Message],
    operator: str,
    note: str,
) -> list[dict]:
    user_parts = [
        f"OPERATOR: {operator}\nDIRECTION: {note}",
        f"CHANGES MADE:\n{_change_summary(edits)}",
        "DOWNSTREAM PASSAGES TO CHECK:\n" + "\n\n".join(
            f"[msg {m.msg_id} · {m.speaker}]\n{_clean_excerpt(m.text)}"
            for m in candidates
        ),
        "Classify each passage. Output only the JSON array.",
    ]
    return [
        {"role": "system", "content": SWEEP_SYSTEM},
        {"role": "user", "content": "\n\n---\n\n".join(user_parts)},
    ]


def _extract_json_array(raw: str) -> list[dict]:
    """Pull the first JSON array out of the model's response."""
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------- #
# Main sweep function
# --------------------------------------------------------------------------- #

def sweep(
    log_path: str | Path,
    edits: list[Edit],
    operator: str,
    note: str,
    *,
    sweep_from: int | None = None,
    on_progress=None,
) -> SweepReport:
    """Run the propagation sweep. `sweep_from` defaults to max(changed_ids) + 1.
    Returns a SweepReport; also writes the markdown summary to LAST_SWEEP_REPORT."""
    log = loader.load(log_path)

    replace_edits = [e for e in edits if e.kind == "replace"]
    inject_edits = [e for e in edits if e.kind == "inject"]
    remove_edits = [e for e in edits if e.kind == "remove"]
    all_msg_ids = [e.msg_id for e in edits]

    changed_from = min(all_msg_ids) if all_msg_ids else 0
    changed_to = max(all_msg_ids) if all_msg_ids else 0
    if sweep_from is not None:
        effective_sweep_from = sweep_from
    elif remove_edits:
        effective_sweep_from = min(e.msg_id for e in remove_edits)
    else:
        effective_sweep_from = changed_to + 1

    if on_progress:
        on_progress("retrieving candidates…")
    candidates = retrieve_candidates(log_path, log, edits, effective_sweep_from)

    if not candidates:
        report = SweepReport(
            changed_from=changed_from, changed_to=changed_to,
            sweep_from=effective_sweep_from,
            operator=operator, note=note,
            hits=[], clean_count=0, verdict="CLEAN",
        )
        _write_report(report, log_path)
        return report

    if on_progress:
        on_progress(f"judging {len(candidates)} candidate(s)…")

    raw = llm.chat(
        build_sweep_messages(edits, candidates, operator, note),
        task="sweep",
        temperature=SWEEP_TEMPERATURE,
        max_tokens=min(4000, max(2500, len(candidates) * 150 + 1500)),
    )
    verdicts = _extract_json_array(raw)
    verdict_map = {v.get("msg_id"): v for v in verdicts if isinstance(v, dict)}

    hits: list[SweepHit] = []
    clean_count = 0
    for m in candidates:
        v = verdict_map.get(m.msg_id, {})
        verd = v.get("verdict", "REVIEW").upper()
        if verd not in ("CLEAN", "REVIEW", "BREAK"):
            verd = "REVIEW"
        reason = str(v.get("reason", "(no reason returned)"))
        if verd == "CLEAN":
            clean_count += 1
        else:
            hits.append(SweepHit(
                msg_id=m.msg_id, speaker=m.speaker,
                excerpt=_clean_excerpt(m.text, 200),
                verdict=verd, reason=reason,
            ))

    overall = "CONCERNS" if any(h.verdict == "BREAK" for h in hits) else (
        "REVIEW" if hits else "CLEAN"
    )
    report = SweepReport(
        changed_from=changed_from, changed_to=changed_to,
        sweep_from=effective_sweep_from,
        operator=operator, note=note,
        hits=hits, clean_count=clean_count, verdict=overall,
    )
    _write_report(report, log_path)
    return report


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #

_VERDICT_ICON = {"CLEAN": "✓", "REVIEW": "⚠", "BREAK": "✗"}


def _write_report(report: SweepReport, log_path: str | Path) -> None:
    lines = [
        f"# Propagation sweep — {report.verdict}",
        "",
        f"- **Operator:** {report.operator}",
        f"- **Direction:** {report.note}",
        f"- **Changed span:** msgs {report.changed_from}–{report.changed_to}",
        f"- **Downstream scanned from:** msg {report.sweep_from}",
        f"- **Log:** `{log_path}`",
        "",
    ]
    if report.verdict == "CLEAN":
        lines.append("All downstream candidates read cleanly. No action needed.")
    else:
        n_break = sum(1 for h in report.hits if h.verdict == "BREAK")
        n_review = sum(1 for h in report.hits if h.verdict == "REVIEW")
        lines.append(
            f"{report.clean_count} clean, {n_review} for review, "
            f"{n_break} likely break(s)."
        )
        lines.append("")
        for h in sorted(report.hits, key=lambda h: (h.verdict != "BREAK", h.msg_id)):
            icon = _VERDICT_ICON.get(h.verdict, "?")
            lines += [
                f"## {icon} msg {h.msg_id} — {h.speaker} [{h.verdict}]",
                "",
                f"> {h.excerpt}",
                "",
                f"**Reason:** {h.reason}",
                "",
            ]
    lines += [
        "---",
        f"_To patch: `python -m story_editor restyle --from N --to N` or `inject --after N`_",
        "",
    ]
    config.LAST_SWEEP_REPORT.write_text("\n".join(lines), encoding="utf-8")
