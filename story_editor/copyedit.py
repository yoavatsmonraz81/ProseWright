"""Surgical typo pass. Chunked LLM find/replace. Propose only.

``proofread`` is a canon/fabrication guard on pending edits. This operator is
the other job that word invited: their/there, a doubled word, a misspelling.
The model may only name a short exact substring of a message body and the
correction. The header is never sent as editable text, and a find that is
ambiguous or missing is dropped, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config, jsonish, layers as layers_mod, llm, loader
from .transform import (
    Edit,
    EditSet,
    SpanResult,
    containment_flags,
    resolve_span,
    split_header,
    write_pending_edits,
)


COPYEDIT_TEMPERATURE = 0.1
CHUNK_CHARS = 5_500
MAX_FIND = 80
MAX_REPLACE = 120

_SYSTEM = (
    "You are a copy editor for roleplay prose.\n"
    "Find only mechanical errors: typos, misspellings, their/there/they're, "
    "its/it's, misused homophones, doubled words, missing spaces, obvious "
    "letter-level garbles.\n\n"
    "Do NOT change style, diction, period voice, rhythm, character names, "
    "facts, or wording that is merely awkward. Do not touch chronicle headers.\n\n"
    "Return STRICT JSON:\n"
    '{"fixes": [{"msg_id": 12, "find": "exact substring", '
    '"replace": "correction", "why": "their/there"}]}\n\n'
    "Rules for find:\n"
    "- Must be a short exact substring of that message BODY (not the header).\n"
    "- Prefer the smallest unique span (a word or two).\n"
    "- Skip anything you are not sure about.\n"
    "- Empty fixes array if the chunk is clean.\n"
    "No commentary. JSON only."
)


@dataclass(frozen=True)
class Fix:
    msg_id: int
    find: str
    replace: str
    why: str = ""

    def to_json(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "find": self.find,
            "replace": self.replace,
            "why": self.why,
        }


@dataclass
class Skip:
    msg_id: int
    find: str
    reason: str

    def to_json(self) -> dict:
        return {"msg_id": self.msg_id, "find": self.find, "reason": self.reason}


def apply_one(body: str, find: str, replace: str) -> tuple[str | None, str | None]:
    """Replace ``find`` in ``body`` when it is unique. Return (new, skip-reason)."""
    if not find or find == replace:
        return None, "empty or identical"
    if len(find) > MAX_FIND or len(replace) > MAX_REPLACE:
        return None, "span too long"
    n = body.count(find)
    if n == 0:
        return None, "not found"
    if n > 1:
        return None, "ambiguous"
    return body.replace(find, replace, 1), None


def parse_fixes(obj: dict, allowed: set[int]) -> list[Fix]:
    raw = obj.get("fixes") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[Fix] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            msg_id = int(item["msg_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if msg_id not in allowed:
            continue
        find = str(item.get("find") or "")
        replace = str(item.get("replace") or "")
        why = str(item.get("why") or "").strip()
        if not find or find == replace:
            continue
        out.append(Fix(msg_id=msg_id, find=find, replace=replace, why=why))
    return out


def chunk_messages(messages: list[loader.Message], budget: int = CHUNK_CHARS) -> list[list[loader.Message]]:
    """Pack consecutive turns until the body budget fills."""
    chunks: list[list[loader.Message]] = []
    current: list[loader.Message] = []
    size = 0
    for msg in messages:
        _, body = split_header(msg.text)
        n = len(body)
        if current and size + n > budget:
            chunks.append(current)
            current = []
            size = 0
        current.append(msg)
        size += n
    if current:
        chunks.append(current)
    return chunks


def _chunk_prompt(messages: list[loader.Message]) -> str:
    parts = [
        "Mark mechanical typos in these message bodies. "
        "msg_id is the integer after 'msg'. The [header] line is omitted on purpose.",
        "",
    ]
    for msg in messages:
        _, body = split_header(msg.text)
        parts.append(f"--- msg {msg.msg_id} · {msg.speaker} ---")
        parts.append(body if body.strip() else "(empty)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def ask_chunk(
    messages: list[loader.Message],
    *,
    on_stream=None,
    base_url: str | None = None,
) -> list[Fix]:
    """One LLM call over a packed chunk. Isolated so tests can stub it."""
    allowed = {m.msg_id for m in messages}
    prompt = _chunk_prompt(messages)
    if on_stream is not None:
        on_stream({"kind": "phase", "text": f"copyedit msgs {min(allowed)}–{max(allowed)}"})

    def _call() -> str:
        return llm.chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            task="copyedit",
            base_url=base_url,
            temperature=COPYEDIT_TEMPERATURE,
            max_tokens=2_048,
        )

    obj = jsonish.request_object(_call, what="copyedit", slug="copyedit")
    return parse_fixes(obj, allowed)


def apply_fixes(
    messages: list[loader.Message],
    fixes: list[Fix],
) -> tuple[list[Edit], list[Skip]]:
    """Apply unique body finds. Header is re-prepended byte-for-byte."""
    by_id = {m.msg_id: m for m in messages}
    grouped: dict[int, list[Fix]] = {}
    for fix in fixes:
        grouped.setdefault(fix.msg_id, []).append(fix)

    edits: list[Edit] = []
    skipped: list[Skip] = []
    for msg_id, group in grouped.items():
        target = by_id.get(msg_id)
        if target is None:
            for fix in group:
                skipped.append(Skip(msg_id, fix.find, "unknown message"))
            continue
        header, body = split_header(target.text)
        new_body = body
        used = 0
        for fix in group:
            updated, reason = apply_one(new_body, fix.find, fix.replace)
            if updated is None:
                skipped.append(Skip(msg_id, fix.find, reason or "skipped"))
                continue
            new_body = updated
            used += 1
        if used == 0 or new_body == body:
            continue
        after = header + new_body
        edits.append(Edit(
            msg_id=target.msg_id,
            speaker=target.speaker,
            before=target.text,
            after=after,
            flags=containment_flags(target.text, after, target.speaker),
        ))
    return edits, skipped


def apply_span(
    log_path: str | Path,
    span: SpanResult,
    *,
    on_stream=None,
    ask=None,
    base_url: str | None = None,
) -> EditSet:
    """Copyedit ``span`` and write a pending EditSet."""
    layers_mod.check_runs("copyedit", layers_mod.LOG)
    asker = ask or (lambda msgs, **kw: ask_chunk(msgs, on_stream=on_stream, base_url=base_url))
    chunks = chunk_messages(span.messages)
    all_fixes: list[Fix] = []
    for i, chunk in enumerate(chunks, start=1):
        if on_stream is not None:
            on_stream({
                "kind": "phase",
                "text": f"copyedit chunk {i}/{len(chunks)} "
                        f"({len(chunk)} turn(s))",
            })
        all_fixes.extend(asker(chunk))
    edits, skipped = apply_fixes(span.messages, all_fixes)
    note = (
        f"copyedit: {len(edits)} turn(s) touched, "
        f"{len(all_fixes)} fix(es) named, {len(skipped)} skipped"
    )
    edit_set = EditSet(
        log=str(Path(log_path).resolve()),
        operator="copyedit",
        note=note,
        locator=span.locator,
        edits=edits,
        meta={
            "fix_count": len(all_fixes),
            "fixes": [fix.to_json() for fix in all_fixes],
            "applied": len(edits),
            "skipped": [s.to_json() for s in skipped],
            "chunks": len(chunks),
            "review_total": len(edits),
            "review_accepted": 0,
            "review_rejected": 0,
        },
    )
    write_pending_edits(edit_set)
    return edit_set


def apply_log(
    log_path: str | Path,
    *,
    msg_from: int | None = None,
    msg_to: int | None = None,
    speaker: str | None = None,
    scene: int | None = None,
    on_stream=None,
    base_url: str | None = None,
) -> EditSet:
    log = loader.load(log_path)
    span = resolve_span(
        log, msg_from=msg_from, msg_to=msg_to, speaker=speaker, scene=scene,
    )
    return apply_span(log_path, span, on_stream=on_stream, base_url=base_url)
