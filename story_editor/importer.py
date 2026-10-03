"""Plain-story -> SillyTavern .jsonl converter.

This is the "non-SillyTavern adaptation": it lets any prose story be brought into
the engine's accepted log format without going through SillyTavern at all. The
engine's loader (loader.py) expects:

    line 0      -> a metadata object (no "mes" field)
    line 1..N   -> one message object per line: {name, is_user, is_system, mes, ...}

The authoring format (a ``.story`` file) is deliberately minimal:

    ::title: A Scandal in Bohemia (excerpt)
    ::user: Watson
    ::char: Holmes

    @Holmes [ Baker Street | 20 March 1888 | evening ]
    "To Sherlock Holmes she is always the woman." He said it without looking up.

    @Watson
    I had seen little of Holmes of late. My marriage had drifted us apart.

Rules:
- ``::key: value`` lines at the top set metadata (until the first ``@`` turn).
- ``@Name`` begins a turn. Anything after the name on that line that looks like a
  ``[ ... ]`` header is kept as the start of the message body (the transform
  operators preserve that header verbatim).
- The body runs until the next ``@Name`` line or end of file. Leading/trailing
  blank lines are trimmed; internal blank lines (paragraph breaks) are preserved.
- ``is_user`` is True when the turn's speaker matches the ``::user`` metadata.
- A speaker named ``System`` (case-insensitive) becomes an ``is_system`` line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_META_RE = re.compile(r"^::\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_TURN_RE = re.compile(r"^@(\S+)\s*(.*)$")


@dataclass
class ParsedStory:
    metadata: dict[str, str] = field(default_factory=dict)
    turns: list[tuple[str, str]] = field(default_factory=list)  # (speaker, body)


def parse_story(text: str) -> ParsedStory:
    """Parse the authoring format into metadata + ordered (speaker, body) turns."""
    story = ParsedStory()
    lines = text.splitlines()

    cur_speaker: str | None = None
    cur_body: list[str] = []
    seen_turn = False

    def flush() -> None:
        nonlocal cur_speaker, cur_body
        if cur_speaker is not None:
            body = "\n".join(cur_body).strip("\n")
            # collapse runs of >2 blank lines to a single paragraph break
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            story.turns.append((cur_speaker, body))
        cur_speaker = None
        cur_body = []

    for line in lines:
        # Metadata only before the first turn.
        if not seen_turn:
            m = _META_RE.match(line)
            if m:
                story.metadata[m.group(1).lower()] = m.group(2).strip()
                continue

        t = _TURN_RE.match(line)
        if t:
            seen_turn = True
            flush()
            cur_speaker = t.group(1).strip()
            remainder = t.group(2).strip()
            # If the remainder starts with a [ ... ] header, keep it as the body's
            # opening line so split_header() can preserve it verbatim later.
            cur_body = [remainder] if remainder else []
            continue

        if cur_speaker is not None:
            cur_body.append(line)

    flush()
    return story


def to_jsonl_objects(story: ParsedStory) -> list[dict]:
    """Turn a ParsedStory into the list of JSONL objects the loader expects.

    The first object is the metadata line (no ``mes``); the rest are messages.
    """
    user_name = story.metadata.get("user", "User")
    char_name = story.metadata.get("char", "Character")
    title = story.metadata.get("title", "Imported Story")
    now = datetime.now(timezone.utc).isoformat()

    objects: list[dict] = [
        {
            "user_name": user_name,
            "character_name": char_name,
            "title": title,
            "create_date": now,
            "imported_by": "story_editor.importer",
        }
    ]

    for speaker, body in story.turns:
        is_system = speaker.lower() == "system"
        is_user = (not is_system) and speaker.lower() == user_name.lower()
        objects.append(
            {
                "name": speaker,
                "is_user": is_user,
                "is_system": is_system,
                "send_date": now,
                "mes": body,
            }
        )
    return objects


def convert(src: str | Path, dst: str | Path) -> int:
    """Convert a ``.story`` file at ``src`` into a ``.jsonl`` log at ``dst``.

    Returns the number of message lines written (excluding the metadata line).
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"source story not found: {src}")

    story = parse_story(src.read_text(encoding="utf-8"))
    if not story.turns:
        raise ValueError(f"no @speaker turns found in {src}")

    objects = to_jsonl_objects(story)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8") as fh:
        for obj in objects:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    return len(objects) - 1  # minus the metadata line
