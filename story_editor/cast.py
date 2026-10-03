"""The cast: who is in the story, and where.

Feeds the Char tab. Everyone with a bible is listed — the main characters from
their cards and the ones filed automatically (``"origin": "player-mode"``) —
with where they appear in the working log:

* **voiced** — a voice label names them: the voice of the turn, or one of the
  voices in an ensemble (a turn on their own card counts when it has no label);
* **pov** — a label makes them the focal character, the one whose senses
  bound the narration, without voicing them;
* **mentioned** — one of their names appears in the text.

Nothing here writes; it reads the bibles, the voice labels, the dossiers and
the log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import attribution as attr_mod, canon_layer, config, dossier as dossier_mod, loader, structure

RANK = {"voiced": 3, "pov": 2, "mentioned": 1}


@dataclass
class _Presence:
    by_msg: dict[int, str] = field(default_factory=dict)  # msg id -> strongest relation

    def add(self, msg_id: int, how: str) -> None:
        if RANK[how] > RANK.get(self.by_msg.get(msg_id, ""), 0):
            self.by_msg[msg_id] = how


def _raw_bibles() -> dict[str, dict[str, Any]]:
    path = config.CHARACTERS_JSON
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("characters", {}) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _name_of(key: str, bible) -> str:
    label = (bible.label or "").split(" — ")[0].strip()
    return label or (bible.aliases[0] if bible.aliases else key.title())


def _alias_pattern(names: list[str]) -> re.Pattern | None:
    names = sorted({n for n in names if n and len(n) > 1}, key=len, reverse=True)
    if not names:
        return None
    # Case-sensitive: names are capitalised, and "Rook" must not match "rook".
    return re.compile(r"(?<![\w’'])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w])")


def _presence(log) -> dict[str, _Presence]:
    bibles = canon_layer.all_bibles()
    store = attr_mod.load_store(log=log)
    out = {key: _Presence() for key in bibles}
    card_owner: dict[str, str] = {}
    for key, b in bibles.items():
        for alias in [_name_of(key, b), *b.aliases]:
            card_owner.setdefault(alias.lower(), key)
    patterns = {key: _alias_pattern([_name_of(key, b), *b.aliases]) for key, b in bibles.items()}
    for msg in log.messages:
        label = attr_mod.get_label(store, msg.msg_id)
        if label is not None:
            voices = {label.voice} | {s.get("voice", "") for s in (label.segments or [])}
            for key in voices & out.keys():
                out[key].add(msg.msg_id, "voiced")
            if label.focal in out and label.focal not in voices:
                out[label.focal].add(msg.msg_id, "pov")
        else:
            owner = card_owner.get((msg.speaker or "").strip().lower())
            if owner:
                out[owner].add(msg.msg_id, "voiced")
        for key, pattern in patterns.items():
            if pattern is not None and pattern.search(msg.text or ""):
                out[key].add(msg.msg_id, "mentioned")
    return out


def _scene_names(log) -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        for s in structure.segment_scenes(log):
            title = s.location or (f"scene {s.scene_id}")
            for mid in range(s.start_msg_id, s.end_msg_id + 1):
                names[mid] = title
    except (ValueError, KeyError, AttributeError, TypeError):  # a nicety here, never a reason to fail
        pass
    return names


def _spans(by_msg: dict[int, str], scenes: dict[int, str]) -> list[dict[str, Any]]:
    """One row per scene the character is in (or per run of nearby messages when
    the log has no scene headers): its range, counts, strongest relation, and
    where to jump — the first message with that relation."""
    spans: list[dict[str, Any]] = []
    for mid in sorted(by_msg):
        how, scene = by_msg[mid], scenes.get(mid, "")
        last = spans[-1] if spans else None
        same = last is not None and (scene == last["scene"] if scene or last["scene"] else mid - last["end"] <= 3)
        if same:
            last["end"] = mid
            last["counts"][how] = last["counts"].get(how, 0) + 1
            if RANK[how] > RANK[last["how"]]:
                last["how"], last["jump"] = how, mid
        else:
            spans.append({"start": mid, "end": mid, "how": how, "jump": mid, "scene": scene, "counts": {how: 1}})
    return spans


def _counts(p: _Presence) -> dict[str, int]:
    c = {"voiced": 0, "pov": 0, "mentioned": 0}
    for how in p.by_msg.values():
        c[how] += 1
    return c


def roster(log=None) -> dict[str, Any]:
    log = log if log is not None else loader.load(config.working_log())
    bibles = canon_layer.all_bibles()
    raw = _raw_bibles()
    presence = _presence(log)
    dossiers = set(dossier_mod.list_characters())
    rows = []
    for key, b in bibles.items():
        p = presence.get(key, _Presence())
        ids = sorted(p.by_msg)
        rows.append({
            "key": key,
            "name": _name_of(key, b),
            "label": b.label,
            "role": b.role,
            "aliases": b.aliases,
            "origin": (raw.get(key) or {}).get("origin") or "cards",
            "counts": _counts(p),
            "first": ids[0] if ids else None,
            "last": ids[-1] if ids else None,
            "has_portrait": dossier_mod.has_portrait(key),
            "has_dossier": key in dossiers,
        })
    rows.sort(key=lambda r: (-(r["counts"]["voiced"] * 3 + r["counts"]["pov"] * 2 + r["counts"]["mentioned"]),
                             r["name"].lower()))
    return {"characters": rows, "messages": len(log.messages)}


def member(key: str, log=None) -> dict[str, Any] | None:
    log = log if log is not None else loader.load(config.working_log())
    bibles = canon_layer.all_bibles()
    b = bibles.get(key)
    if b is None:
        return None
    raw = _raw_bibles().get(key) or {}
    p = _presence(log).get(key, _Presence())
    d = dossier_mod.load(key)
    return {
        "key": key,
        "name": _name_of(key, b),
        "label": b.label,
        "role": b.role,
        "aliases": b.aliases,
        "origin": raw.get("origin") or "cards",
        "first_scene": raw.get("first_scene", ""),
        "status": raw.get("status", ""),
        "voice_rules": b.voice_rules,
        "forbidden_phrasings": b.forbidden_phrasings,
        "canonical_facts": b.canonical_facts,
        "relationship_notes": b.relationship_notes,
        "counts": _counts(p),
        "spans": _spans(p.by_msg, _scene_names(log)),
        "has_portrait": dossier_mod.has_portrait(key),
        "dossier": [e.to_json() for e in d.entries] if d else [],
    }
