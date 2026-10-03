"""Ask pane — grounded Q&A over the log.

Retrieval already exists (fused / keyword). This module adds:
1. ``as_of`` — a story-time ceiling (msg id, derived beat, or authored label)
   so answers can ignore future prose.
2. A short LLM synthesis that may only cite retrieved passages.

Dossiers will reuse ``resolve_as_of`` / the same ``msg_id_to`` cutoff later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config, llm, loader, structure, views
from .index.search import Hit, search as fused_search, search_keyword

Log = loader.Log

_ASK_HITS = 8
_ASK_EXCERPT = 360
_ASK_TEMPERATURE = 0.15
_ASK_MAX_TOKENS = 900

ProgressFn = Callable[[str, str], None]  # (stage, detail)


@dataclass
class AsOf:
    """A resolved story-time ceiling: only msgs ``<= msg_id`` are visible."""

    msg_id: int
    label: str  # human: "msg 504", "D8 — Kettering (≤624)", "A6 (≤504)"

    def to_json(self) -> dict[str, Any]:
        return {"msg_id": self.msg_id, "label": self.label}


@dataclass
class Citation:
    msg_id: int
    speaker: str
    preview: str

    def to_json(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "speaker": self.speaker,
            "preview": self.preview,
        }


@dataclass
class AskResult:
    question: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    as_of: AsOf | None = None
    mode: str = "fused"
    hits_considered: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": [c.to_json() for c in self.citations],
            "as_of": self.as_of.to_json() if self.as_of else None,
            "mode": self.mode,
            "hits_considered": self.hits_considered,
        }


_AS_OF_MSG = re.compile(
    r"^(?:msg(?:_id)?\s*[:=]?\s*)?(\d{1,5})$", re.IGNORECASE
)
# Longer token first — otherwise ``b`` steals the lead of ``beat8``.
_AS_OF_DERIVED = re.compile(
    r"^(?:beat|d|b)(\d{1,3})$", re.IGNORECASE
)
_AS_OF_AUTHORED = re.compile(
    r"^(?:a\s*)?(\d{1,3}\s*[a-z]?)$", re.IGNORECASE
)


def resolve_as_of(
    spec: str | int | None,
    *,
    log: Log,
    derived: structure.SpineProposal | None = None,
) -> AsOf | None:
    """Turn a free-form tip into a ``msg_id`` ceiling.

    Accepted forms:
    - ``None`` / ``""`` / ``"now"`` / ``"tip"`` → no ceiling (full log)
    - ``504`` / ``"msg:504"`` → that message
    - ``"D8"`` / ``"beat 8"`` → end of derived beat 8
    - ``"A6"`` / ``"13a"`` → tip of that authored beat (evidence / derived link)
    """
    if spec is None:
        return None
    if isinstance(spec, int):
        if spec < 0:
            raise ValueError(f"as_of msg_id must be >= 0, got {spec}")
        tip = min(spec, max(0, len(log) - 1))
        return AsOf(msg_id=tip, label=f"msg {tip}")

    text = str(spec).strip()
    if not text or text.lower() in {"now", "tip", "end", "full", "-"}:
        return None

    m = _AS_OF_MSG.match(text)
    if m:
        tip = min(int(m.group(1)), max(0, len(log) - 1))
        return AsOf(msg_id=tip, label=f"msg {tip}")

    compact = re.sub(r"\s+", "", text)
    dm = _AS_OF_DERIVED.match(compact) or re.match(r"^[dD](\d{1,3})$", compact)
    if dm:
        beat_id = int(dm.group(1))
        spine = derived or structure.load_derived_spine(log)
        if spine is None:
            raise ValueError(
                "no committed derived spine — cannot resolve as_of beat "
                f"D{beat_id}"
            )
        beat = next((b for b in spine.beats if b.beat_id == beat_id), None)
        if beat is None:
            known = ", ".join(f"D{b.beat_id}" for b in spine.beats)
            raise ValueError(
                f"derived beat D{beat_id} not found (have {known or 'none'})"
            )
        return AsOf(
            msg_id=int(beat.end_msg_id),
            label=f"D{beat_id} — {beat.title} (≤{beat.end_msg_id})",
        )

    am = _AS_OF_AUTHORED.match(compact)
    if am:
        label = am.group(1).replace(" ", "").lower()
        tip = _authored_tip(label, log)
        if tip is None:
            raise ValueError(
                f"authored beat A{label} has no realised tip yet "
                "(validate first, or use a msg / derived beat)"
            )
        return AsOf(msg_id=tip, label=f"A{label} (≤{tip})")

    raise ValueError(
        f"unrecognised as_of {spec!r} — use a msg id, D8, or A13a"
    )


def _authored_tip(label: str, log: Log) -> int | None:
    """Max msg the audit/spine view associates with an authored beat."""
    view = views.spine_view(log)
    for row in view.get("authored") or []:
        if str(row.get("label") or "").lower() != label:
            continue
        end = row.get("end")
        if end is not None:
            return int(end)
        evidence = row.get("evidence") or []
        if evidence:
            return max(int(x) for x in evidence)
        # Fall back to linked derived beats' ends.
        ends: list[int] = []
        derived_by = {d["beat_id"]: d for d in (view.get("derived") or [])}
        for did in row.get("derived_beats") or []:
            d = derived_by.get(int(did))
            if d is not None:
                ends.append(int(d["end_msg_id"]))
        if ends:
            return max(ends)
        return None
    return None


# Question words that flood FTS when the whole sentence is MATCH'd ("color of
# the lantern-keeper's ladder" used to lose to scenes that merely said "lamp").
_ASK_STOP = frozenset({
    "what", "which", "where", "when", "who", "whom", "whose", "why", "how",
    "is", "are", "was", "were", "be", "been", "being", "the", "a", "an", "of",
    "to", "in", "on", "at", "for", "from", "with", "by", "as", "into", "about",
    "that", "this", "these", "those", "it", "its", "her", "his", "their", "she",
    "he", "they", "we", "you", "i", "me", "my", "our", "your", "and", "or",
    "but", "not", "no", "do", "does", "did", "can", "could", "would", "should",
    "will", "just", "also", "than", "then", "there", "here", "out", "up", "down",
    "over", "under", "between", "through", "during", "before", "after", "above",
    "below", "again", "further", "once", "any", "all", "each", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "so", "too", "very",
    "tell", "describe", "explain", "please", "does", "have", "has", "had",
})
# Ultra-frequent names (the cast in the project's bibles) — soft boost only,
# never the sole MATCH needle: a lead's name appears in nearly every scene.
def _ask_soft_names() -> frozenset[str]:
    from . import canon_layer
    names: set[str] = set()
    for bible in canon_layer.all_bibles().values():
        for alias in bible.aliases:
            token = alias.strip().lower()
            if token and " " not in token:
                names.update({token, f"{token}'s", f"{token}’s"})
    return frozenset(names)
_ASK_STEMS: dict[str, tuple[str, ...]] = {
    "gloves": ("glove",),
    "glove": ("gloves",),
    "gifted": ("gift", "gifts"),
    "gift": ("gifted", "gifts"),
    "gifts": ("gift", "gifted"),
    "color": ("colour", "colored", "coloured"),
    "colour": ("color", "colored", "coloured"),
    "colored": ("color", "colour", "coloured"),
    "coloured": ("color", "colour", "colored"),
}


def _ask_content_terms(question: str) -> list[str]:
    """Content nouns from the question, with light stem expansion."""
    raw_terms: list[str] = []
    for tok in re.findall(r"[A-Za-zÁÉÍÓÚÄÖÜÅÆØáéíóúäöüåæø']{3,}", question, re.UNICODE):
        t = tok.lower().strip("'")
        if not t or t in _ASK_STOP or t in raw_terms:
            continue
        raw_terms.append(t)
    out: list[str] = []
    seen: set[str] = set()
    for t in raw_terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
        for syn in _ASK_STEMS.get(t, ()):
            if syn not in seen:
                seen.add(syn)
                out.append(syn)
    return out[:16]


def _term_overlap_score(hit: Hit, terms: list[str]) -> float:
    blob = f"{hit.text_clean or ''} {hit.text or ''}".lower()
    hard = [t for t in terms if t not in _ask_soft_names()]
    soft = [t for t in terms if t in _ask_soft_names()]
    score = 0.0
    for t in hard:
        if t in blob:
            # Longer / rarer needles weigh more than "color".
            score += 1.0 + min(len(t), 12) / 24.0
    for t in soft:
        if t in blob:
            score += 0.15
    return score


def retrieve_for_ask(
    question: str,
    *,
    db_path: Path,
    msg_id_to: int | None = None,
    mode: str = "fused",
    limit: int = _ASK_HITS,
) -> list[Hit]:
    """Passages visible at ``msg_id_to`` (inclusive). ``mode``: fused|keyword.

    Never MATCH the raw question — stopwords and character names drown the
    load-bearing nouns (ladder / ledger / seal). Keyword each content term,
    rescore by overlap, optionally blend a semantic pass.
    """
    mode = (mode or "fused").lower()
    terms = _ask_content_terms(question)
    hard_terms = [t for t in terms if t not in _ask_soft_names()] or terms
    structural = dict(msg_id_to=msg_id_to)

    pooled: dict[int, Hit] = {}
    # Prefer hard nouns first so their hits claim slots before soft names.
    for term in hard_terms:
        try:
            for hit in search_keyword(db_path, term, limit=5, **structural):
                pooled.setdefault(int(hit.msg_id), hit)
        except Exception:  # noqa: BLE001 — bad FTS token; skip
            continue

    if mode == "fused":
        try:
            for hit in fused_search(
                db_path, question, limit=max(limit, 6), **structural,
            ):
                pooled.setdefault(int(hit.msg_id), hit)
        except Exception:  # noqa: BLE001 — embedder/network; keyword stands alone
            pass

    if not pooled and terms:
        # Last resort: OR the content terms as one query (still no stopwords).
        try:
            for hit in search_keyword(
                db_path, " ".join(hard_terms), limit=limit, **structural,
            ):
                pooled[int(hit.msg_id)] = hit
        except Exception:  # noqa: BLE001
            pass

    ranked = sorted(
        pooled.values(),
        key=lambda h: (
            -_term_overlap_score(h, terms),
            -float(h.score or 0.0),
            int(h.msg_id),
        ),
    )
    # Drop zero-overlap strays semantic search dragged in when we have nouns.
    if hard_terms:
        strong = [h for h in ranked if _term_overlap_score(h, terms) > 0]
        if strong:
            ranked = strong
    return ranked[:limit]


def _excerpt(hit: Hit, terms: list[str] | None = None) -> str:
    """Passage window for the prompt.

    Prefer a window centred on the question's content nouns — otherwise a long
    lead-in (a long greeting) truncates before the load-bearing clause
    ("black leather gloves") and the model honestly reports "not mentioned".
    """
    body = " ".join((hit.text_clean or hit.text or "").split())
    if len(body) <= _ASK_EXCERPT:
        return body
    anchor = -1
    blob = body.lower()
    hard = [t for t in (terms or []) if t and t not in _ask_soft_names()]
    # Longest needle first — "gloves" before "glove", "gifted" before "gift".
    for term in sorted(hard, key=len, reverse=True):
        pos = blob.find(term.lower())
        if pos >= 0:
            anchor = pos
            break
    if anchor < 0:
        return body[:_ASK_EXCERPT] + "…"
    half = _ASK_EXCERPT // 2
    start = max(0, anchor - half // 2)
    end = min(len(body), start + _ASK_EXCERPT)
    start = max(0, end - _ASK_EXCERPT)
    chunk = body[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + chunk + suffix


def build_ask_messages(
    question: str,
    hits: list[Hit],
    *,
    as_of: AsOf | None,
    terms: list[str] | None = None,
) -> list[dict[str, str]]:
    terms = terms if terms is not None else _ask_content_terms(question)
    evidence_lines = []
    for h in hits:
        evidence_lines.append(
            f"- msg {h.msg_id} [{h.speaker}]: {_excerpt(h, terms)}"
        )
    evidence_block = (
        "\n".join(evidence_lines) if evidence_lines else "(no passages retrieved)"
    )
    ceiling = (
        f"STORY TIME: as of {as_of.label}. Ignore anything that would require "
        f"knowledge after msg {as_of.msg_id}.\n"
        if as_of
        else "STORY TIME: full log (no as_of ceiling).\n"
    )
    system = (
        "You answer questions about THIS story log using only the retrieved "
        "passages. You are a research aide for the director, not a novelist.\n\n"
        "Rules:\n"
        "- Ground every factual claim in the passages. If the passages do not "
        "support an answer, say so plainly.\n"
        "- Cite msg_ids from the evidence list only — never invent ids.\n"
        "- Prefer concise answers (a short paragraph, or a few bullets).\n"
        "- Concrete attributes in the passages count (e.g. \"black leather gloves\" "
        "answers a colour question — do not require the word \"colour\").\n"
        "- Do not continue the story or invent off-page events.\n\n"
        "Output STRICT JSON only — no prose, no code fence:\n"
        '{"answer":"...","evidence":[12,45]}'
    )
    user = (
        f"{ceiling}\n"
        f"QUESTION:\n{question.strip()}\n\n"
        f"EVIDENCE:\n{evidence_block}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_ask_reply(raw: str, allowed: set[int]) -> tuple[str, list[int]]:
    from . import jsonish

    data = jsonish.extract_object(raw)
    if not isinstance(data, dict):
        raise ValueError("ask reply was not a JSON object")
    answer = str(data.get("answer") or "").strip()
    claimed = data.get("evidence") or data.get("citations") or []
    ids: list[int] = []
    if isinstance(claimed, list):
        for item in claimed:
            try:
                mid = int(item if not isinstance(item, dict) else item.get("msg_id"))
            except (TypeError, ValueError):
                continue
            if mid in allowed and mid not in ids:
                ids.append(mid)
    return answer, ids


def ask(
    question: str,
    *,
    log: Log | None = None,
    log_path: Path | None = None,
    as_of: str | int | None = None,
    mode: str = "fused",
    limit: int = _ASK_HITS,
    on_progress: ProgressFn | None = None,
) -> AskResult:
    """Retrieve → answer. ``as_of`` is resolved before search so the index
    never sees future messages."""
    q = (question or "").strip()
    if not q:
        raise ValueError("question is required")

    if log is None:
        log = loader.load(log_path or config.working_log())
    path = Path(log_path or log.path)
    db_path = config.index_db_for(path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"search index missing at {db_path}; run `story-editor index build`"
        )

    def _progress(stage: str, detail: str) -> None:
        if on_progress:
            on_progress(stage, detail)

    _progress("as_of", "resolving story-time ceiling")
    ceiling = resolve_as_of(as_of, log=log)

    _progress(
        "retrieve",
        f"searching{' ≤ msg ' + str(ceiling.msg_id) if ceiling else ''}",
    )
    hits = retrieve_for_ask(
        q,
        db_path=db_path,
        msg_id_to=ceiling.msg_id if ceiling else None,
        mode=mode,
        limit=limit,
    )
    # Hard belt: never surface a hit past the ceiling even if a caller forgot.
    if ceiling is not None:
        hits = [h for h in hits if int(h.msg_id) <= ceiling.msg_id]

    allowed = {int(h.msg_id) for h in hits}
    by_id = {int(h.msg_id): h for h in hits}

    if not hits:
        return AskResult(
            question=q,
            answer=(
                "Nothing in the indexed log matched that question"
                + (f" as of {ceiling.label}." if ceiling else ".")
            ),
            citations=[],
            as_of=ceiling,
            mode=mode,
            hits_considered=0,
        )

    terms = _ask_content_terms(q)
    _progress("model", "drafting a grounded answer")
    messages = build_ask_messages(q, hits, as_of=ceiling, terms=terms)
    raw = llm.chat(
        messages,
        task="ask",
        temperature=_ASK_TEMPERATURE,
        max_tokens=_ASK_MAX_TOKENS,
    )
    try:
        answer, cited_ids = _parse_ask_reply(raw, allowed)
    except Exception:  # noqa: BLE001 — fall back to prose + all hits
        answer = raw.strip()
        cited_ids = [int(h.msg_id) for h in hits[:5]]

    if not answer:
        answer = "The model returned an empty answer."

    if not cited_ids:
        # Still show the passages we retrieved so the director can jump.
        cited_ids = [int(h.msg_id) for h in hits[:4]]

    citations = [
        Citation(
            msg_id=mid,
            speaker=by_id[mid].speaker,
            preview=_excerpt(by_id[mid], terms),
        )
        for mid in cited_ids
        if mid in by_id
    ]

    return AskResult(
        question=q,
        answer=answer,
        citations=citations,
        as_of=ceiling,
        mode=mode,
        hits_considered=len(hits),
    )


def as_of_options(log: Log) -> dict[str, Any]:
    """Dropdown fodder for the Ask tab: derived beats + realised authored tips."""
    derived = structure.load_derived_spine(log)
    derived_opts = []
    if derived:
        for b in derived.beats:
            derived_opts.append({
                "value": f"D{b.beat_id}",
                "label": f"D{b.beat_id} — {b.title} (≤{b.end_msg_id})",
                "msg_id": int(b.end_msg_id),
            })
    authored_opts = []
    view = views.spine_view(log)
    for row in view.get("authored") or []:
        end = row.get("end")
        if end is None:
            continue
        lab = str(row.get("label") or "")
        authored_opts.append({
            "value": f"A{lab}",
            "label": f"A{lab} — {row.get('title', '')} (≤{end})",
            "msg_id": int(end),
        })
    tip = max(0, len(log) - 1) if len(log) else 0
    return {
        "log_tip": tip,
        "derived": derived_opts,
        "authored": authored_opts,
    }
