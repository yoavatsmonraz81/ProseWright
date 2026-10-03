"""Reading JSON back out of a local model, and asking again when it is broken.

A local model answers a "STRICT JSON only" prompt correctly most of the time and
then, on one roll in several, drops a single comma. The call that produced it
took minutes, so throwing the whole run away over one token is the expensive
choice. Two cheap ones come first: repair the malformations that can only mean
one thing, and, failing that, ask again.

The repairs are deliberately narrow. A comma inserted between two members that
a newline already separates cannot change what the document says — in valid JSON
nothing may sit there but a separator — and a comma removed before a closing
brace has no meaning to lose. Anything needing a guess about intent is left
alone and becomes a retry instead.

``UnparsableReply`` is a ``ModelError`` because that is what it is, and because
the routes and commands that already answer 502 / exit 2 for an unreachable
model should say the same kind of thing about an incoherent one.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config
from .llm import ModelError


class UnparsableReply(ModelError):
    """The model answered, repeatedly, with something that is not JSON."""


_FENCE_OPEN = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")

# What may end a member, and what may begin the next one. Two of these adjacent
# with only a newline between them is a missing separator and nothing else.
_MEMBER_END = set('"}]0123456789el')
_MEMBER_START = set('"{[-0123456789tfn')


def _next_significant(text: str, i: int) -> str:
    while i < len(text) and text[i].isspace():
        i += 1
    return text[i] if i < len(text) else ""


def _repair(text: str) -> str:
    """Insert separators a newline stands in for, drop commas before a close."""
    out: list[str] = []
    depth = 0
    in_str = False
    esc = False
    prev = ""
    gap_newline = False
    for i, ch in enumerate(text):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                prev = '"'
            continue
        if ch.isspace():
            gap_newline = gap_newline or ch == "\n"
            out.append(ch)
            continue
        if ch == "," and _next_significant(text, i + 1) in ("}", "]"):
            continue
        if depth >= 1 and gap_newline and prev in _MEMBER_END and ch in _MEMBER_START:
            out.append(",")
        out.append(ch)
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        prev = ch
        gap_newline = False
    return "".join(out)


def _spans(text: str):
    """Balanced ``{...}`` spans, outermost and earliest first."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break
        start = text.find("{", start + 1)


def _variants(candidate: str):
    yield candidate
    repaired = _repair(candidate)
    if repaired != candidate:
        yield repaired
    # Single-quoted output is only unambiguous when no double quote is in play;
    # otherwise an apostrophe inside prose would end a string.
    if '"' not in candidate and "'" in candidate:
        requoted = candidate.replace("'", '"')
        yield requoted
        yield _repair(requoted)


def extract_object(raw: str) -> dict[str, Any] | None:
    """The JSON object in a model reply, or None if there is not one in there."""
    if not raw:
        return None
    text = raw.strip()
    defenced = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text)).strip()
    candidates = [text]
    if defenced != text:
        candidates.append(defenced)
    candidates.extend(_spans(defenced))
    for candidate in candidates:
        for variant in _variants(candidate):
            try:
                obj = json.loads(variant)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def save_raw_reply(raw: str, slug: str) -> Path | None:
    """Park an unparsable reply on disk. The HTTP body gets a path, not a dump."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.BAD_REPLIES_DIR / f"{slug}-{stamp}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw or "", encoding="utf-8")
    except OSError:
        return None
    return path


def request_object(
    call: Callable[[], str],
    *,
    what: str,
    slug: str,
    attempts: int = 3,
) -> dict[str, Any]:
    """Call the model until it yields a JSON object, up to `attempts` times.

    Raises ``UnparsableReply`` — a ``ModelError`` — rather than letting a decoder
    offset reach the user. Transport failures are not retried: they are not a bad
    roll, and the caller's own error path already says so.
    """
    last = ""
    for _ in range(max(1, attempts)):
        last = call() or ""
        obj = extract_object(last)
        if obj is not None:
            return obj
    path = save_raw_reply(last, slug)
    where = f" The last raw reply is saved at {path}." if path else ""
    raise UnparsableReply(
        f"the model returned unparsable JSON for the {what} after "
        f"{max(1, attempts)} attempts; nothing was changed. This is usually a bad "
        f"roll on a local model — running it again often succeeds.{where}"
    )
