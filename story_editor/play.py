"""Player mode inside the editor: play the story, and keep the log in step.

A project whose manifest names a play folder (``integrations.player_mode.home``)
can be played from the editor. Turns run on the Player-mode engine
(``plugins/player-mode/engine/kp``), exactly as ``kp`` runs them, and the play
folder stays the engine's. After every turn the new messages are appended to
the project's working log:

* each with a stable id derived from the play row (so labels survive);
* with a scene header on each scene's first message, from the scene cards;
* characters the engine introduced are filed as lightweight bibles
  (``"origin": "player-mode"``; bibles written by hand are never touched);
* every message gets a voice label from the engine's own record of it.

Messages already in the log are never rewritten, so edits made in the editor
stay. Undo takes the turn back out of the log too, unless it was edited there.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

_LOCK = threading.Lock()
_HAS_HEADER = re.compile(r"^\s*\[[^\]\n]*(?:🕰️|🗓️|📍)")


class PlayError(Exception):
    pass


class PlayBusy(PlayError):
    pass


# --------------------------------------------------------------------------- #
# The play rows as the editor sees them
# --------------------------------------------------------------------------- #


def uid_of(row: dict) -> str:
    """A stable id for a play row: the same row always gets the same id."""
    basis = f"{row.get('send_date', '')}|{row.get('name', '')}|{row.get('is_user')}"
    return "m-" + hashlib.sha256(basis.encode()).hexdigest()[:12]


def _meta(row: dict) -> dict:
    return (row.get("extra") or {}).get("player_mode") or {}


def headed(rows: list[dict], cards: dict[str, dict]) -> tuple[list[dict], int]:
    """Copies of the play rows with a stable id each, and a scene header on each
    scene's first message. Returns (rows, headers added)."""
    out, added, last = [], 0, None
    for row in rows:
        row = json.loads(json.dumps(row))
        scene = _meta(row).get("scene")
        if scene and scene != last:
            card = cards.get(scene, {})
            stamp = card.get("stamp") or {}
            place = stamp.get("place") or card.get("title") or card.get("location") or scene
            bits = []
            if stamp.get("clock"):
                bits.append(f"🕰️ Time {stamp['clock']}")
            if stamp.get("date"):
                bits.append(f"🗓️ {stamp['date']}")
            bits.append(f"📍 {place}")
            if not _HAS_HEADER.match(row.get("mes", "")):
                row["mes"] = f"[ {' | '.join(bits)} ]\n{row.get('mes', '')}"
                added += 1
            last = scene
        row.setdefault("extra", {}).setdefault("se_uid", uid_of(row))
        out.append(row)
    return out, added


def _short(name: str) -> str:
    return re.split(r"\s+(?:of|the|from)\s+", name.strip(), maxsplit=1)[0].strip()


_TITLES = re.compile(r"^(?:(?:captain|capt\.?|mistress|master|miss|mrs\.?|mr\.?|ms\.?|lady|lord|sir|dame|"
                     r"doctor|dr\.?|father|mother|sister|brother|old|young)\s+)+", re.I)


def _untitled(name: str) -> str:
    """"Captain Amos Varga" -> "Amos Varga": the name without the title in front of it."""
    return _TITLES.sub("", name.strip()).strip()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "someone"


class Roster:
    """Names → character keys: the project's bibles, then the engine's cast ledger."""

    def __init__(self, bibles: dict, ledger: dict):
        self.by_alias: dict[str, str] = {}
        for key, entry in bibles.items():
            # The label's name part counts too: "Captain Amos Varga — master of the Gull's Due".
            label = str(entry.get("label") or "").split(" — ")[0].strip()
            for alias in [key, *(entry.get("aliases") or []), *([label] if label else [])]:
                self.by_alias.setdefault(alias.lower(), key)
        for real, stand_in in (ledger.get("aliases") or {}).items():
            if isinstance(stand_in, str):
                self.by_alias.setdefault(stand_in.lower(), self.key(real))

    def key(self, name: str) -> str:
        n = (name or "").strip()
        if not n:
            return ""
        for candidate in (n, _short(n), _untitled(n), _untitled(_short(n))):
            if candidate and candidate.lower() in self.by_alias:
                return self.by_alias[candidate.lower()]
        key = _slug(_short(n))
        self.by_alias[n.lower()] = key
        self.by_alias[_short(n).lower()] = key
        return key


def file_minor_characters(bibles: dict, ledger: dict, roster: Roster) -> list[str]:
    """Lightweight bibles for the characters the engine introduced. Returns the keys filed."""
    filed = []
    for full, npc in (ledger.get("npcs") or {}).items():
        key = roster.key(full)
        existing = bibles.get(key)
        if existing is not None and existing.get("origin") != "player-mode":
            continue  # written or edited by hand: never overwritten
        short = _short(full)
        affiliation = npc.get("affiliation") or npc.get("court") or ""
        facts = [x for x in (npc.get("standing"), npc.get("seen_as")) if x]
        if npc.get("wants"):
            facts.append(f"Wants: {npc['wants']}")
        if npc.get("why_here"):
            facts.append(f"Why here: {npc['why_here']}")
        entry = {
            "aliases": list(dict.fromkeys([short, full])),
            "label": f"{short} — {affiliation}" if affiliation else short,
            "role": npc.get("standing") or npc.get("seen_as") or "",
            "canonical_facts": facts,
            "relationship_notes": {},
            "origin": "player-mode",
            "first_scene": npc.get("first_scene", ""),
            "status": npc.get("status", ""),
        }
        if existing != entry:
            bibles[key] = entry
            filed.append(key)
    return filed


def label_rows(rows: list[dict], roster: Roster, default_pov: str, log, store) -> dict[str, int]:
    """A voice label for every play row in ``log``, from the engine's record.
    Reviewed labels are kept. Uses the attribution module of the open project."""
    from . import attribution as attr

    stats = {"labelled": 0, "kept_reviewed": 0}
    pov = default_pov
    for row in rows:
        pm = _meta(row)
        if pm.get("pov"):
            pov = pm["pov"]
        elif row.get("is_user"):
            pov = roster.key(row.get("name", "")) or pov
        msg_id = log.ordinal_of(row["extra"]["se_uid"])
        if msg_id is None:
            continue
        speaker = log.messages[msg_id].speaker
        kind = pm.get("kind")
        if row.get("is_user"):
            label = attr.VoiceLabel(card=speaker, voice=pov, mode="pov", focal=pov, source="player_mode",
                                    confidence=0.9, notes="the player's move")
        elif kind == "narration":
            plan = pm.get("plan") or {}
            names = pm.get("voices")
            if names is None:
                ent = plan.get("entrance") or {}
                names = list(plan.get("reacts") or []) + ([ent["name"]] if ent.get("present") and ent.get("name") else [])
            voices = [k for k in dict.fromkeys(roster.key(n) for n in names) if k and k != pov]
            segments = [{"voice": v, "kind": "dialogue", "addressees": [], "hint": "in this reply (Player mode)"}
                        for v in voices]
            if not voices:
                label = attr.VoiceLabel(card=speaker, voice="narrator", mode="pov", focal=pov,
                                        source="player_mode", confidence=0.95,
                                        notes="narration from the POV character's side; no one else acts")
            elif len(voices) == 1:
                label = attr.VoiceLabel(card=speaker, voice=voices[0], mode="mixed", focal=pov,
                                        segments=segments, source="player_mode", confidence=0.95,
                                        notes=f"narration with {voices[0]}")
            else:
                label = attr.VoiceLabel(card=speaker, voice="ensemble", mode="scene_narrator", focal=pov,
                                        segments=segments, source="player_mode", confidence=0.95,
                                        notes="ensemble: " + ", ".join(voices))
        else:
            continue  # scene summaries: narration only
        if attr.set_label(store, msg_id, label):
            stats["labelled"] += 1
        else:
            stats["kept_reviewed"] += 1
    return stats


# --------------------------------------------------------------------------- #
# Reading a play folder (no engine needed)
# --------------------------------------------------------------------------- #


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@dataclass
class Folder:
    home: Path

    def rows(self) -> list[dict]:
        path = self.home / "log.jsonl"
        if not path.exists():
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines[1:]]

    def header(self) -> dict:
        path = self.home / "log.jsonl"
        if path.exists():
            first = path.read_text(encoding="utf-8").splitlines()[:1]
            if first:
                return json.loads(first[0])
        return {"chat_metadata": {"player_mode": True}}

    def cards(self) -> dict[str, dict]:
        return {c["id"]: c for c in _read_json(self.home / "world" / "spine.json", [])}

    def ledger(self) -> dict:
        return _read_json(self.home / "state" / "cast.json", {})

    def state(self) -> dict:
        return _read_json(self.home / "state" / "state.json", {})


# --------------------------------------------------------------------------- #
# Keeping the working log in step
# --------------------------------------------------------------------------- #


def sync_into(folder: Folder, target: Path, *, replace: bool = False) -> dict[str, Any]:
    """Append the play rows the working log doesn't have yet (or, with
    ``replace``, rebuild it from the play log), then file characters and label
    voices. Messages already in the log are never rewritten."""
    from . import attribution as attr, loader

    rows, _ = headed(folder.rows(), folder.cards())
    existing_lines: list[str] = []
    if target.exists() and not replace:
        existing_lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    have: set[str] = set()
    for line in existing_lines[1:]:
        uid = ((json.loads(line).get("extra") or {}).get("se_uid")) or ""
        if uid:
            have.add(uid)
    new = [r for r in rows if r["extra"]["se_uid"] not in have]
    if new or replace or not target.exists():
        header = json.loads(existing_lines[0]) if existing_lines else dict(folder.header())
        header.setdefault("chat_metadata", {})["imported_from"] = str(folder.home / "log.jsonl")
        lines = [json.dumps(header, ensure_ascii=False)] + existing_lines[1:] + [
            json.dumps(r, ensure_ascii=False) for r in new]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".partial")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(target)

    chars_path = config.CHARACTERS_JSON
    chars = _read_json(chars_path, None) or {"schema": "story-editor/characters@1", "characters": {}}
    bibles = chars.setdefault("characters", {})
    ledger = folder.ledger()
    roster = Roster(bibles, ledger)
    filed = file_minor_characters(bibles, ledger, roster)
    if filed:
        chars_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = chars_path.with_suffix(".json.partial")
        tmp.write_text(json.dumps(chars, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(chars_path)
        from . import canon_layer

        canon_layer._load_characters_json.cache_clear()  # the Char tab sees them at once
    log = loader.load(target)
    store = attr.load_store(log=log)
    stats = label_rows(rows, roster, roster.key(folder.state().get("pov", "")), log, store)
    attr.save_store(store, log=log)
    return {"added": len(new), "filed": filed, **stats}


def _reconcile(before: list[dict], after: list[dict], target: Path) -> dict[str, Any]:
    """Bring the working log in step after the play log changed under it
    (another telling chosen, a turn undone or replayed). Each message the log
    still has exactly as play wrote it follows the change: retold, or taken
    out. Messages edited in the log are left as they are. New messages are
    appended by sync_into afterwards."""
    from . import backup as backup_mod

    old = {r["extra"]["se_uid"]: r.get("mes", "") for r in before}
    new = {r["extra"]["se_uid"]: r.get("mes", "") for r in after}
    gone = {u for u in old if u not in new}
    retold = {u for u in old if u in new and new[u] != old[u]}
    result = {"removed": 0, "retold": 0, "kept_edited": 0}
    if not (gone or retold) or not target.exists():
        return result
    if getattr(config, "PENDING_EDITS", None) and Path(config.PENDING_EDITS).exists():
        result["kept_edited"] = len(gone | retold)
        result["note"] = "a proposal is waiting on the log, so those messages were left in it as they were"
        return result
    lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    keep, changed = [lines[0]], False
    for line in lines[1:]:
        row = json.loads(line)
        uid = (row.get("extra") or {}).get("se_uid", "")
        if uid in gone or uid in retold:
            if row.get("mes", "") != old[uid]:
                result["kept_edited"] += 1
            elif uid in gone:
                result["removed"] += 1
                changed = True
                continue
            else:
                row["mes"] = new[uid]
                result["retold"] += 1
                changed = True
                line = json.dumps(row, ensure_ascii=False)
        keep.append(line)
    if changed:
        backup_mod.backup(target, label="play-sync")
        tmp = target.with_suffix(target.suffix + ".partial")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(target)
    return result


# --------------------------------------------------------------------------- #
# Driving the engine
# --------------------------------------------------------------------------- #


def home() -> Path | None:
    return getattr(config, "PLAY_HOME", None)


def _engine():
    engine = config.PROJECT_ROOT / "plugins" / "player-mode" / "engine"
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    from kp import backend, store, turn  # noqa: E402

    return backend, store, turn


def _project():
    h = home()
    if h is None:
        raise PlayError("this project has no play folder (integrations.player_mode.home in project.json)")
    if not h.is_dir():
        raise PlayError(f"the play folder {h} does not exist")
    _, store, _ = _engine()
    return store.Project(h)


def view() -> dict[str, Any]:
    """Everything the Play view shows."""
    h = home()
    if h is None:
        return {"configured": False}
    project = _project()
    folder = Folder(project.home)
    state = project.load("state")
    cards = folder.cards()
    card = cards.get(state.get("scene", ""), {})
    rows = []
    for row in folder.rows():
        pm = _meta(row)
        swipes = row.get("swipes") or []
        rows.append({"uid": uid_of(row), "name": row.get("name", ""), "is_user": bool(row.get("is_user")),
                     "text": row.get("mes", ""), "kind": pm.get("kind", ""), "scene": pm.get("scene", ""),
                     "scene_title": cards.get(pm.get("scene", ""), {}).get("title", ""),
                     "swipes": max(1, len(swipes)), "swipe_id": int(row.get("swipe_id", max(0, len(swipes) - 1)))})
    in_log: set[str] = set()
    target = config.working_log()
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip():
                in_log.add((json.loads(line).get("extra") or {}).get("se_uid", ""))
    cast = []
    for path in sorted((project.home / "world" / "cast").glob("*.json")):
        cast.append({"id": path.stem, "name": _read_json(path, {}).get("name", path.stem)})
    pending = state.get("pending") or None
    today = datetime.now().astimezone().date()
    spent_today = 0.0
    for row in folder.rows():
        cost = _meta(row).get("cost")
        try:
            when = datetime.fromisoformat(str(row.get("send_date", "")).replace("Z", "+00:00")).astimezone().date()
        except ValueError:
            continue
        if isinstance(cost, (int, float)) and when == today:
            spent_today += cost
    commits = project._git("rev-list", "--count", "HEAD").stdout.strip() if (project.home / ".git").exists() else "0"
    return {
        "configured": True,
        "home": str(project.home),
        "name": project.config.get("name", ""),
        "status": {
            "scene": state.get("scene", ""),
            "title": card.get("title", state.get("scene", "")),
            "question": card.get("question", ""),
            "rung": state.get("rung", 1),
            "rungs": len(card.get("ladder", [])) or 1,
            "moves": state.get("moves", 0),
            "dwell": card.get("dwell_min", 0),
            "pov": state.get("pov", ""),
            "pov_name": project.cast_card(state.get("pov", "")).get("name", state.get("pov", "")),
            "stage": state.get("stage", []),
            "mystery": state.get("mystery", ""),
            "cost": state.get("session_cost", 0),  # every turn since the play folder was reset
            "cost_today": round(spent_today, 4),
            "pending": pending,
        },
        "rows": rows,
        "cast": cast,
        "not_in_log": sum(1 for r in rows if r["uid"] not in in_log),
        "can_undo": commits.isdigit() and int(commits) > 1,
        "busy": _LOCK.locked(),
    }


def _locked(fn):
    def wrapper(*args, **kwargs):
        if not _LOCK.acquire(blocking=False):
            raise PlayBusy("a turn is already being played; wait for it to finish")
        try:
            return fn(*args, **kwargs)
        finally:
            _LOCK.release()
    return wrapper


@_locked
def turn(move: str | None, *, debug: bool = False) -> dict[str, Any]:
    """Play one move (None establishes the scene; "y"/"n" answers a pending cut)."""
    backend, _, turn_mod = _engine()
    project = _project()
    try:
        out = turn_mod.run(project, move, debug=debug)
    except backend.ModelError as exc:
        raise PlayError(f"the model call failed: {exc}") from exc
    synced = sync_into(Folder(project.home), config.working_log())
    return {"prose": out.prose, "prompt": out.prompt, "debug": out.debug, "synced": synced}


def _changing(work) -> dict[str, Any]:
    """Run an engine operation that rewrites the play log, then bring the
    working log in step: retold or removed messages follow (unless edited
    there), new ones are appended."""
    project = _project()
    folder = Folder(project.home)
    cards = folder.cards()
    before, _ = headed(folder.rows(), cards)
    result = work(project)
    after, _ = headed(folder.rows(), cards)
    reconciled = _reconcile(before, after, config.working_log())
    synced = sync_into(folder, config.working_log())
    return {**result, **reconciled, "synced": synced}


@_locked
def undo() -> dict[str, Any]:
    return _changing(lambda project: {"message": project.undo()})


@_locked
def renarrate(*, debug: bool = False) -> dict[str, Any]:
    """Tell the last reply again: same events, new prose. Every telling is kept."""
    backend, _, turn_mod = _engine()

    def work(project):
        try:
            out = turn_mod.renarrate(project, debug=debug)
        except ValueError as exc:
            raise PlayError(str(exc)) from exc
        except backend.ModelError as exc:
            raise PlayError(f"the model call failed: {exc}") from exc
        return {"prose": out.prose, "prompt": out.prompt, "debug": out.debug}

    return _changing(work)


@_locked
def choose_swipe(index: int) -> dict[str, Any]:
    _, _, turn_mod = _engine()

    def work(project):
        try:
            return {"message": turn_mod.choose_swipe(project, index)}
        except ValueError as exc:
            raise PlayError(str(exc)) from exc

    return _changing(work)


@_locked
def reroll(*, debug: bool = False) -> dict[str, Any]:
    """Play the last turn again from scratch: another plan, other events."""
    backend, _, turn_mod = _engine()

    def work(project):
        try:
            out = turn_mod.reroll(project, debug=debug)
        except ValueError as exc:
            raise PlayError(str(exc)) from exc
        except backend.ModelError as exc:
            raise PlayError(f"the model call failed: {exc}") from exc
        return {"prose": out.prose, "prompt": out.prompt, "debug": out.debug}

    return _changing(work)


@_locked
def switch_pov(char_id: str) -> dict[str, Any]:
    _, _, turn_mod = _engine()
    return {"message": turn_mod.switch_pov(_project(), char_id)}


@_locked
def sync() -> dict[str, Any]:
    project = _project()
    return sync_into(Folder(project.home), config.working_log())
