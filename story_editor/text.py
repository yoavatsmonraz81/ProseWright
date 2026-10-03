"""Shared text cleaning helpers.

The .jsonl messages carry two kinds of scaffolding that we don't want to feed
to the language model, embed, or index:

- A leading status header like '[ 🕰️ Time 11:15 PM | 🗓️ Thursday, October 4, 1792 |
  📍 The Harbour Office ]' — useful as metadata, noise as prose.
- `<details>...</details>` meta blocks that the prompt and the model use for
  state-tracking (Plot Momentum, NPC_Agenda, etc.) — also noise as prose.

These helpers strip both. The structural side (pauses.py / index builder)
parses the header separately when it wants the metadata.
"""

from __future__ import annotations

import re

# Meta scaffolding the author/AI use for state tracking; not story content.
_META_BLOCK_RE = re.compile(r"<details>.*?</details>", re.DOTALL | re.IGNORECASE)


def strip_header(text: str) -> str:
    """Drop a leading `[ ... ]` status header so context reads as prose. Leaves
    the rest untouched. No-op if the message doesn't start with one."""
    stripped = text.lstrip()
    if stripped.startswith("["):
        end = stripped.find("]")
        if 0 < end < 400:
            return stripped[end + 1 :].strip()
    return text


def strip_meta_blocks(text: str) -> str:
    """Drop every `<details>...</details>` block from the text."""
    return _META_BLOCK_RE.sub("", text)


def clean(text: str) -> str:
    """The full clean: drop meta blocks AND the leading header, trim whitespace.
    The right thing to feed to the model, the FTS index, and the embedder."""
    return strip_header(strip_meta_blocks(text)).strip()
