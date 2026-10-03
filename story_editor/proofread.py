"""Phase 3.5 — upstream proof-reader (the fabrication guard).

Phase 3 (propagation sweep) watches for what a committed edit *breaks* downstream.
This module is the mirror image: it watches for what a proposed edit *invents* — facts
the existing canon does not support, which the model imported from its training priors
rather than from the log.

The classic failure: an inject set in the royal apartments mentions 'the corridor
between the sisters' chambers' when the log establishes they share one room.

Pipeline (runs on pending_edits BEFORE commit):
  1. EXTRACT — LLM call: pull every concrete verifiable claim from the proposed text.
     Only setup facts qualify: architectural, biographical, geographical, temporal,
     relational, presence. Subjective descriptions and plot events do not.
  2. SEARCH  — for each claim, query the index (fused) and the lorebook. Purely
     retrieval — no LLM involved.
  3. CLASSIFY — LLM batch call: given claims + retrieved evidence, verdict each as
     supported / plausibly_extends / contradicts.
  4. REPORT  — markdown surfacing contradictions beside the contradicting prose;
     lore-entry templates for plausibly_extends claims to canonize; a do-not-invent
     constraint string for re-roll.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm, lore as lore_mod, text as text_mod
from .transform import Edit, split_header


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MAX_CLAIMS = 12
EVIDENCE_HITS_PER_CLAIM = 5
EVIDENCE_EXCERPT_CHARS = 350
CLAIM_EXTRACT_TEMPERATURE = 0.15
CLASSIFY_TEMPERATURE = 0.15


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class FactClaim:
    claim: str               # the extracted factual assertion
    claim_type: str          # "architectural" | "biographical" | "geographical" |
                             # "temporal" | "relational" | "presence"
    source_line: str         # exact phrase from the proposed text making the claim
    verdict: str = ""        # "supported" | "plausibly_extends" | "contradicts"
    reason: str = ""         # model's explanation of the verdict
    evidence_quotes: list[str] = field(default_factory=list)  # supporting/contradicting passages
    evidence_msg_ids: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "claim": self.claim, "claim_type": self.claim_type,
            "source_line": self.source_line, "verdict": self.verdict,
            "reason": self.reason, "evidence_quotes": self.evidence_quotes,
            "evidence_msg_ids": self.evidence_msg_ids,
        }


@dataclass
class ProofreadReport:
    edit_msg_id: int
    edit_speaker: str
    edit_kind: str           # "replace" | "inject"
    operator: str
    note: str
    proposed_excerpt: str    # first 300 chars of the proposed text for display
    claims: list[FactClaim] = field(default_factory=list)
    verdict: str = "clean"   # "clean" | "plausibly_extends" | "contradicts"
    reroll_note: str = ""    # augmented note with do-not-invent constraints
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def contradicts(self) -> list[FactClaim]:
        return [c for c in self.claims if c.verdict == "contradicts"]

    @property
    def plausible(self) -> list[FactClaim]:
        return [c for c in self.claims if c.verdict == "plausibly_extends"]

    @property
    def supported(self) -> list[FactClaim]:
        return [c for c in self.claims if c.verdict == "supported"]

    def to_json(self) -> dict:
        return {
            "edit_msg_id": self.edit_msg_id, "edit_speaker": self.edit_speaker,
            "edit_kind": self.edit_kind, "operator": self.operator, "note": self.note,
            "proposed_excerpt": self.proposed_excerpt,
            "claims": [c.to_json() for c in self.claims],
            "verdict": self.verdict, "reroll_note": self.reroll_note,
            "created": self.created,
        }


# --------------------------------------------------------------------------- #
# Stage 1 — claim extraction
# --------------------------------------------------------------------------- #

_EXTRACT_SYSTEM = (
    "You are a continuity fact-checker for a prose roleplay. Your task: extract "
    "every CONCRETE, VERIFIABLE factual claim "
    "from the proposed prose passage.\n\n"
    "A claim qualifies ONLY if it states or implies a fact about the story's SETUP "
    "(the world as it exists, not the events of this passage):\n"
    "- ARCHITECTURAL: room layout, furniture placement, what is adjacent to what, "
    "structural geography of the castle or town.\n"
    "- BIOGRAPHICAL: a character's past, family connections, who they served under, "
    "prior events in their history.\n"
    "- GEOGRAPHICAL: places mentioned as existing or as locations of events.\n"
    "- TEMPORAL: what date/season/year it is, how long ago something happened.\n"
    "- RELATIONAL: who knows/trusts/owes whom, alliances, enmities.\n"
    "- PRESENCE: who is alive, who is at court, who is away or confined, who is "
    "in a specific location.\n\n"
    "DO NOT extract:\n"
    "- Emotional states or subjective observations ('she felt cold').\n"
    "- Pure atmospheric description ('the room was dark').\n"
    "- Plot events that happen within the proposed text itself.\n"
    "- Dialogue (what a character says) unless it asserts a setup fact.\n\n"
    "Output a JSON array. Each entry:\n"
    '  {"claim": "<the factual assertion>", '
    '"type": "<architectural|biographical|geographical|temporal|relational|presence>", '
    '"source_line": "<exact phrase from the text>"}\n'
    f"Maximum {MAX_CLAIMS} claims. If nothing qualifies, output [].\n"
    "Output ONLY the JSON array, no preamble."
)


def extract_claims(proposed_text: str, operator: str, note: str) -> list[FactClaim]:
    """LLM call: pull concrete verifiable facts from the proposed passage."""
    body = split_header(proposed_text)[1].strip()
    if not body:
        return []
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": (
            f"OPERATOR: {operator}\nDIRECTION: {note}\n\n"
            f"PROPOSED PASSAGE:\n{body}"
        )},
    ]
    raw = llm.chat(messages, task="proofread", temperature=CLAIM_EXTRACT_TEMPERATURE,
                   max_tokens=min(2000, max(800, len(body) + 600)))
    arr = _extract_json_array(raw)
    claims: list[FactClaim] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        if not claim:
            continue
        claims.append(FactClaim(
            claim=claim,
            claim_type=str(item.get("type", "relational")).strip(),
            source_line=str(item.get("source_line", "")).strip(),
        ))
    return claims[:MAX_CLAIMS]


# --------------------------------------------------------------------------- #
# Stage 2 — evidence retrieval
# --------------------------------------------------------------------------- #

def _lore_evidence(claim: str, lore_paths: list[Path]) -> list[str]:
    """Return lorebook content strings that fire on the claim text."""
    if not lore_paths:
        return []
    entries = lore_mod.find_relevant(claim, lore_paths)
    out: list[str] = []
    for hit in entries[:3]:
        e = hit.entry
        snippet = " ".join(e.content.split())
        if len(snippet) > EVIDENCE_EXCERPT_CHARS:
            snippet = snippet[:EVIDENCE_EXCERPT_CHARS] + "…"
        out.append(f"[lore: {e.comment}] {snippet}")
    return out


def search_evidence(
    claim: str,
    log_path: str | Path,
    *,
    msg_id_to: int | None = None,
    lore_paths: list[Path] | None = None,
) -> tuple[list[str], list[int]]:
    """Return (excerpt_list, msg_id_list) for the claim. Uses the fused index
    when it exists, falling back to lore-only if the index isn't built."""
    db_path = config.index_db_for(log_path)
    excerpts: list[str] = []
    msg_ids: list[int] = []

    if Path(db_path).exists():
        from .index.embed import default_embedder
        from .index.search import search
        embedder = default_embedder()
        hits = search(
            db_path, claim,
            limit=EVIDENCE_HITS_PER_CLAIM,
            embedder=embedder,
            msg_id_to=msg_id_to,
            is_interlude=False,
        )
        for h in hits:
            snippet = " ".join(h.text_clean.split())
            if len(snippet) > EVIDENCE_EXCERPT_CHARS:
                snippet = snippet[:EVIDENCE_EXCERPT_CHARS] + "…"
            excerpts.append(f"[msg {h.msg_id} · {h.speaker}] {snippet}")
            msg_ids.append(h.msg_id)

    # Add lore evidence.
    lore_hits = _lore_evidence(claim, lore_paths or [])
    excerpts.extend(lore_hits)
    return excerpts, msg_ids


# --------------------------------------------------------------------------- #
# Stage 3 — classification
# --------------------------------------------------------------------------- #

_CLASSIFY_SYSTEM = (
    "You are a continuity fact-checker for a historical prose roleplay. You have "
    "extracted factual claims from a proposed passage and retrieved evidence from "
    "the story's existing prose and lore.\n\n"
    "Classify each claim as:\n"
    "- 'supported': the existing prose or lore CLEARLY confirms this fact. Use "
    "only when you found direct evidence.\n"
    "- 'plausibly_extends': no contradicting evidence found, but the fact is NOT "
    "confirmed either. The passage is introducing new canon. Flag for review.\n"
    "- 'contradicts': the existing prose or lore contains text that CONFLICTS with "
    "this claim. Use when the evidence and the claim cannot both be true.\n\n"
    "Be conservative: prefer 'plausibly_extends' over 'supported' when evidence is "
    "indirect; prefer 'plausibly_extends' over 'contradicts' unless the conflict is "
    "clear.\n\n"
    "Output a JSON array in the SAME ORDER as the input claims:\n"
    '  {"verdict": "supported|plausibly_extends|contradicts", '
    '"reason": "<one sentence>", "evidence_quote": "<most relevant excerpt or empty>"}\n'
    "Output ONLY the JSON array."
)


def classify_claims(claims: list[FactClaim]) -> list[FactClaim]:
    """LLM batch call: classify each claim against its retrieved evidence."""
    if not claims:
        return claims

    claim_blocks = []
    for i, c in enumerate(claims, 1):
        evidence_str = (
            "\n".join(f"  - {e}" for e in c.evidence_quotes)
            if c.evidence_quotes else "  (no relevant evidence found in index/lore)"
        )
        claim_blocks.append(
            f"[{i}] CLAIM ({c.claim_type}): {c.claim}\n"
            f"    SOURCE LINE: \"{c.source_line}\"\n"
            f"    EVIDENCE:\n{evidence_str}"
        )

    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": (
            "Claims to classify:\n\n" + "\n\n".join(claim_blocks) +
            "\n\nOutput the JSON array (one entry per claim, in order)."
        )},
    ]
    raw = llm.chat(messages, task="proofread", temperature=CLASSIFY_TEMPERATURE,
                   max_tokens=min(3000, max(1000, len(claims) * 200 + 500)))
    verdicts = _extract_json_array(raw)

    for i, c in enumerate(claims):
        v = verdicts[i] if i < len(verdicts) and isinstance(verdicts[i], dict) else {}
        verdict = str(v.get("verdict", "plausibly_extends")).strip().lower()
        if verdict not in ("supported", "plausibly_extends", "contradicts"):
            verdict = "plausibly_extends"
        c.verdict = verdict
        c.reason = str(v.get("reason", "")).strip()
        eq = str(v.get("evidence_quote", "")).strip()
        if eq and eq not in c.evidence_quotes:
            c.evidence_quotes.insert(0, eq)
    return claims


# --------------------------------------------------------------------------- #
# Main proof-read function
# --------------------------------------------------------------------------- #

def proofread_edit(
    log_path: str | Path,
    edit: Edit,
    operator: str,
    note: str,
    *,
    lore_paths: list[Path] | None = None,
    on_progress=None,
) -> ProofreadReport:
    """Run the three-stage proof-read on a single proposed edit."""
    log_path = Path(log_path).resolve()
    proposed = edit.after
    body = split_header(proposed)[1].strip()
    excerpt = " ".join(body.split())[:300] + ("…" if len(body) > 300 else "")

    report = ProofreadReport(
        edit_msg_id=edit.msg_id, edit_speaker=edit.speaker,
        edit_kind=edit.kind, operator=operator, note=note,
        proposed_excerpt=excerpt,
    )

    # Stage 1 — extract claims.
    if on_progress:
        on_progress(f"msg {edit.msg_id}: extracting claims…")
    claims = extract_claims(proposed, operator, note)
    if not claims:
        report.verdict = "clean"
        return report

    # Stage 2 — retrieve evidence.
    if on_progress:
        on_progress(f"msg {edit.msg_id}: searching evidence for {len(claims)} claim(s)…")
    msg_ceiling = edit.msg_id - 1 if edit.kind == "replace" else None
    for c in claims:
        excerpts, ids = search_evidence(
            c.claim, log_path,
            msg_id_to=msg_ceiling,
            lore_paths=lore_paths,
        )
        c.evidence_quotes = excerpts
        c.evidence_msg_ids = ids

    # Stage 3 — classify.
    if on_progress:
        on_progress(f"msg {edit.msg_id}: classifying claims…")
    claims = classify_claims(claims)
    report.claims = claims

    # Overall verdict.
    if any(c.verdict == "contradicts" for c in claims):
        report.verdict = "contradicts"
    elif any(c.verdict == "plausibly_extends" for c in claims):
        report.verdict = "plausibly_extends"
    else:
        report.verdict = "clean"

    # Build do-not-invent constraint for re-roll.
    if report.contradicts:
        items = [f'"{c.claim}"' for c in report.contradicts]
        report.reroll_note = (
            f"{note} — DO NOT INVENT: {', '.join(items)}. "
            "Write around these gaps instead of asserting them."
        )

    return report


def proofread_pending(
    log_path: str | Path,
    *,
    lore_paths: list[Path] | None = None,
    on_progress=None,
) -> list[ProofreadReport]:
    """Run proof-read on ALL edits in the current pending edit-set."""
    from .transform import load_pending_edits
    edit_set = load_pending_edits()
    if edit_set is None:
        return []
    reports = []
    for edit in edit_set.edits:
        r = proofread_edit(
            log_path, edit, edit_set.operator, edit_set.note,
            lore_paths=lore_paths, on_progress=on_progress,
        )
        reports.append(r)
    return reports


# --------------------------------------------------------------------------- #
# Lore-augment suggestion
# --------------------------------------------------------------------------- #

def suggest_lore_entry(claim: FactClaim) -> str:
    """Return a JSON snippet the director can paste into a lorebook to canonize
    a plausibly_extends claim. Uses the claim text to infer trigger keys."""
    # Derive trigger keys: nouns and capitalized terms from the claim.
    words = re.findall(r"[A-Z][a-z]{2,}|[a-z]{5,}", claim.claim)
    keys = list(dict.fromkeys(w.lower() for w in words if len(w) >= 4))[:5]
    if not keys:
        keys = [claim.claim[:20].lower()]
    entry = {
        "key": keys,
        "keysecondary": [],
        "comment": f"[auto-suggested] {claim.claim[:60]}",
        "content": claim.claim,
        "constant": False,
        "selective": False,
        "disable": False,
        "order": 100,
        "position": 0,
    }
    return json.dumps(entry, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def save_reroll_constraints(reports: list[ProofreadReport]) -> str | None:
    """Persist the do-not-invent constraints from contradicts-containing reports.
    Returns the augmented note, or None if no constraints."""
    contradicted_claims: list[str] = []
    base_note = ""
    for r in reports:
        if r.contradicts:
            base_note = r.note
            contradicted_claims.extend(c.claim for c in r.contradicts)
    if not contradicted_claims:
        config.REROLL_CONSTRAINTS.unlink(missing_ok=True)
        return None
    items = list(dict.fromkeys(contradicted_claims))  # deduplicate
    constraint = (
        f"DO NOT INVENT: {', '.join(repr(i) for i in items)}. "
        "Write around these gaps instead of asserting them."
    )
    data = {"base_note": base_note, "constraint": constraint, "items": items}
    config.REROLL_CONSTRAINTS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return constraint


def load_reroll_constraints() -> dict | None:
    if not config.REROLL_CONSTRAINTS.exists():
        return None
    return json.loads(config.REROLL_CONSTRAINTS.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #

_VERDICT_ICON = {"clean": "✓", "plausibly_extends": "⚠", "contradicts": "✗"}
_CLAIM_ICON = {"supported": "✓", "plausibly_extends": "⚠", "contradicts": "✗"}


def _format_claim(c: FactClaim, idx: int) -> list[str]:
    icon = _CLAIM_ICON.get(c.verdict, "?")
    lines = [
        f"### {icon} Claim {idx} — {c.claim_type} [{c.verdict}]",
        "",
        f"**Asserted:** {c.claim}",
        f"**Source line:** _{c.source_line}_",
        f"**Reason:** {c.reason}",
    ]
    if c.evidence_quotes:
        lines += ["", "**Evidence:**"]
        for eq in c.evidence_quotes[:3]:
            lines.append(f"> {eq}")
    lines.append("")
    return lines


def write_proofread_report(
    reports: list[ProofreadReport],
    log_path: str | Path,
) -> None:
    all_contradicts = sum(len(r.contradicts) for r in reports)
    all_plausible = sum(len(r.plausible) for r in reports)
    all_supported = sum(len(r.supported) for r in reports)
    overall = ("contradicts" if all_contradicts else
               "plausibly_extends" if all_plausible else "clean")
    icon = _VERDICT_ICON[overall]

    lines = [
        f"# Proof-read report — {icon} {overall.upper()}",
        "",
        f"- **Log:** `{log_path}`",
        f"- **Edits checked:** {len(reports)}",
        f"- **Claims:** {all_supported} supported, "
        f"{all_plausible} plausibly-extend canon, {all_contradicts} contradict",
        "",
    ]

    if overall == "clean":
        lines.append("All factual claims in the proposed text are supported by "
                     "existing prose or lore. No fabrications detected.")
    else:
        for r in reports:
            if not r.claims:
                continue
            edit_desc = (f"inject after msg {r.edit_msg_id}"
                         if r.edit_kind == "inject"
                         else f"replace msg {r.edit_msg_id}")
            lines += [
                f"## {_VERDICT_ICON.get(r.verdict, '?')} {edit_desc} "
                f"· {r.edit_speaker} [{r.verdict}]",
                "",
                f"> {r.proposed_excerpt}",
                "",
            ]
            for i, c in enumerate(r.claims, 1):
                lines.extend(_format_claim(c, i))

        # Lore-augment suggestions for plausibly_extends claims.
        plausible_all = [c for r in reports for c in r.plausible]
        if plausible_all:
            lines += [
                "---",
                "## Lore-augment suggestions",
                "",
                "These claims are NEW to this story's canon. If you want to keep them, "
                "paste the entry below into your lorebook so future generations treat it "
                "as established fact.",
                "",
            ]
            for c in plausible_all:
                lines += [
                    f"**{c.claim}**",
                    "",
                    "```json",
                    suggest_lore_entry(c),
                    "```",
                    "",
                ]

        # Re-roll constraint.
        constraint_str: list[str] = []
        for r in reports:
            if r.reroll_note:
                constraint_str.append(r.reroll_note)
        if constraint_str:
            lines += [
                "---",
                "## Re-roll with constraints",
                "",
                "The following do-not-invent note has been saved. Pass it as `--note` "
                "when re-running the operator:",
                "",
            ]
            for cs in constraint_str:
                lines += ["```", cs, "```", ""]
            lines += [
                "_Or run:_ `python -m story_editor edits discard` then re-run the "
                "operator with the note above.",
                "",
            ]

    lines += [
        "---",
        f"_To commit anyway:_ `python -m story_editor edits commit`",
        f"_To discard and re-roll:_ `python -m story_editor proofread reroll`",
        "",
    ]
    config.LAST_PROOFREAD_REPORT.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

def _extract_json_array(raw: str) -> list:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
