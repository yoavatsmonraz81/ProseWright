"""SillyTavern world-info ("lorebook") reader.

ST stores its world info as JSON files with the schema:

    { "entries": {
        "<uid>": {
            "key": ["trigger1", "trigger2", ...],          # primary triggers
            "keysecondary": [...],                          # AND/NOT companions
            "selective": bool,                              # use keysecondary?
            "selectiveLogic": 0|1|2|3,                      # AND_ANY|NOT_ALL|NOT_ANY|AND_ALL
            "content": "the lore text the model sees",
            "comment": "human-readable label",
            "disable": bool,
            "constant": bool,                               # always-on
            "matchWholeWords": bool,                        # word-boundary?
            "caseSensitive": bool,
            ...
        }
    } }

This module loads one or more lorebooks and exposes `find_relevant(text, paths)`
which returns the entries whose triggers fire on `text` (the context window).
We deliberately implement only the subset ST's matcher needs for our use:
disable, constant, key, keysecondary + selectiveLogic, caseSensitive,
matchWholeWords. Probability, depth, position, recursion, etc. are ST
*injection-time* concerns and don't apply here - we're just deciding which
entries are *relevant*.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# ST's selectiveLogic constants (mirrors the ST source).
LOGIC_AND_ANY = 0  # any primary matches AND any secondary matches
LOGIC_NOT_ALL = 1  # primary matches AND NOT all secondaries match
LOGIC_NOT_ANY = 2  # primary matches AND no secondary matches
LOGIC_AND_ALL = 3  # any primary matches AND all secondaries match


@dataclass
class LoreEntry:
    uid: str
    source: str  # filename of the lorebook this entry came from
    comment: str
    keys: list[str]
    keys_secondary: list[str]
    content: str
    constant: bool
    selective: bool
    selective_logic: int
    case_sensitive: bool
    match_whole_words: bool
    disabled: bool

    def label(self) -> str:
        return self.comment or self.keys[0] if self.keys else self.uid


@dataclass
class LoreHit:
    entry: LoreEntry
    matched_keys: list[str]
    constant: bool = False  # constant-on, not key-triggered

    @property
    def reason(self) -> str:
        if self.constant:
            return "(constant)"
        if self.matched_keys:
            return "triggers: " + ", ".join(f"'{k}'" for k in self.matched_keys)
        return "(unknown)"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=8)
def load_lorebook(path: str) -> list[LoreEntry]:
    """Read one ST world-info JSON file. Cached so multiple lookups are cheap."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("entries") or {}
    out: list[LoreEntry] = []
    src = Path(path).name
    for uid, e in entries.items():
        out.append(
            LoreEntry(
                uid=str(uid),
                source=src,
                comment=str(e.get("comment") or ""),
                keys=list(e.get("key") or []),
                keys_secondary=list(e.get("keysecondary") or []),
                content=str(e.get("content") or ""),
                constant=bool(e.get("constant")),
                selective=bool(e.get("selective")),
                selective_logic=int(e.get("selectiveLogic") or 0),
                case_sensitive=bool(e.get("caseSensitive")),
                match_whole_words=bool(
                    e.get("matchWholeWords")
                    if e.get("matchWholeWords") is not None
                    else False
                ),
                disabled=bool(e.get("disable")),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def _compile_pattern(needle: str, whole_words: bool, case_sensitive: bool) -> re.Pattern:
    """Build a regex for one key. We escape the needle and optionally pad with
    word boundaries. Caching kept simple via lru_cache below."""
    flags = 0 if case_sensitive else re.IGNORECASE
    escaped = re.escape(needle)
    if whole_words:
        # \b doesn't work for multi-word or non-letter terms reliably, so do
        # a manual lookaround: ensure the surrounding chars are not letters/digits.
        pattern = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    else:
        pattern = escaped
    return re.compile(pattern, flags)


@lru_cache(maxsize=2048)
def _cached_pattern(needle: str, whole_words: bool, case_sensitive: bool) -> re.Pattern:
    return _compile_pattern(needle, whole_words, case_sensitive)


def _matches_any(text: str, keys: list[str], entry: LoreEntry) -> list[str]:
    """Return the subset of `keys` that fire on `text`."""
    matched = []
    for k in keys:
        if not k:
            continue
        try:
            pat = _cached_pattern(k, entry.match_whole_words, entry.case_sensitive)
        except re.error:
            continue
        if pat.search(text):
            matched.append(k)
    return matched


def _entry_fires(entry: LoreEntry, text: str) -> tuple[bool, list[str]]:
    """Decide whether `entry` should fire given the context `text`. Returns
    (fires, matched_primary_keys). Mirrors ST's selectiveLogic semantics."""
    if entry.disabled:
        return False, []
    if not entry.keys:
        # An entry with no primary keys can only fire as constant.
        return False, []
    primary_hits = _matches_any(text, entry.keys, entry)
    if not primary_hits:
        return False, []
    if not entry.selective or not entry.keys_secondary:
        return True, primary_hits

    secondary_hits = _matches_any(text, entry.keys_secondary, entry)
    logic = entry.selective_logic
    if logic == LOGIC_AND_ANY:
        fires = bool(secondary_hits)
    elif logic == LOGIC_NOT_ALL:
        fires = len(secondary_hits) < len(entry.keys_secondary)
    elif logic == LOGIC_NOT_ANY:
        fires = not secondary_hits
    elif logic == LOGIC_AND_ALL:
        fires = len(secondary_hits) == len(entry.keys_secondary)
    else:
        fires = True
    return fires, primary_hits if fires else []


def find_relevant(
    text: str,
    lorebook_paths: list[str | Path],
    *,
    include_constant: bool = True,
) -> list[LoreHit]:
    """Find lore entries whose triggers fire on `text` (typically the recent
    context window). Returns LoreHits in deterministic order: constants first,
    then key-triggered entries grouped by source file. Disabled entries are
    skipped."""
    hits: list[LoreHit] = []
    for path in lorebook_paths:
        try:
            entries = load_lorebook(str(path))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        for e in entries:
            if e.disabled:
                continue
            if e.constant and include_constant:
                hits.append(LoreHit(entry=e, matched_keys=[], constant=True))
                continue
            fires, matched = _entry_fires(e, text)
            if fires:
                hits.append(LoreHit(entry=e, matched_keys=matched))
    return hits


def format_for_prompt(hits: list[LoreHit], max_chars: int = 4000) -> str:
    """Render the matched lore entries as a block of prompt-ready text. Caps
    total length so we don't blow the context budget; truncates politely at
    entry boundaries rather than mid-sentence."""
    if not hits:
        return ""
    parts: list[str] = []
    total = 0
    for h in hits:
        header = f"[{h.entry.label()}] ({h.entry.source}; {h.reason})"
        block = f"{header}\n{h.entry.content.strip()}"
        if total + len(block) > max_chars and parts:
            parts.append("... (additional lore truncated for budget)")
            break
        parts.append(block)
        total += len(block) + 2  # account for the join separator below
    return "\n\n".join(parts)
