"""Append-only edit history — audit log for every landed change.

Each commit (transform or interlude) and each undo appends one JSONL record.
Manuscript changes land here too, tagged ``layer="manuscript"``: an accepted
prose-review proposal (operator ``committed``) and a hand edit saved from the
novel pane (operator ``manually edited``). Backups remain the restore
mechanism; history is the navigable changelog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

LOG_LAYER = "log"
COMMITTED = "committed"
MANUALLY_EDITED = "manually edited"


@dataclass
class HistoryEdit:
    msg_id: int
    speaker: str
    kind: str
    before: str
    after: str
    flags: list[str] = field(default_factory=list)
    block_id: str = ""  # manuscript entries: the paragraph that changed

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "msg_id": self.msg_id,
            "speaker": self.speaker,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "flags": self.flags,
        }
        if self.block_id:
            out["block_id"] = self.block_id
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "HistoryEdit":
        return cls(
            msg_id=int(d["msg_id"]),
            speaker=d.get("speaker", "?"),
            kind=d.get("kind", "replace"),
            before=d.get("before", ""),
            after=d.get("after", ""),
            flags=list(d.get("flags", [])),
            block_id=str(d.get("block_id") or ""),
        )


@dataclass
class HistoryEntry:
    id: int
    created: str
    log: str
    event: str          # "commit" | "undo"
    operator: str       # restyle | retune | inject | interlude | undo
    note: str
    locator: str
    backup: str
    changed_from: int | None
    changed_to: int | None
    edits: list[HistoryEdit] = field(default_factory=list)
    layer: str = LOG_LAYER    # "log" | "manuscript" (or an edition layer)
    scene_id: str = ""        # manuscript entries: the derived scene
    scene_title: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "scene_id": self.scene_id,
            "scene_title": self.scene_title,
            "id": self.id,
            "created": self.created,
            "log": self.log,
            "event": self.event,
            "operator": self.operator,
            "note": self.note,
            "locator": self.locator,
            "backup": self.backup,
            "changed_from": self.changed_from,
            "changed_to": self.changed_to,
            "edits": [e.to_json() for e in self.edits],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "HistoryEntry":
        return cls(
            id=int(d["id"]),
            created=d.get("created", ""),
            log=d.get("log", ""),
            event=d.get("event", "commit"),
            operator=d.get("operator", "?"),
            note=d.get("note", ""),
            locator=d.get("locator", ""),
            backup=d.get("backup", ""),
            changed_from=d.get("changed_from"),
            changed_to=d.get("changed_to"),
            edits=[HistoryEdit.from_json(e) for e in d.get("edits", [])],
            layer=str(d.get("layer") or LOG_LAYER),
            scene_id=str(d.get("scene_id") or ""),
            scene_title=str(d.get("scene_title") or ""),
        )


def _history_path() -> Path:
    return config.EDIT_HISTORY


def _next_id() -> int:
    entries = load_all()
    if not entries:
        return 1
    return max(e.id for e in entries) + 1


def _span_from_edits(edits: list[HistoryEdit]) -> tuple[int | None, int | None]:
    if not edits:
        return None, None
    ids = [e.msg_id for e in edits]
    return min(ids), max(ids)


def append_entry(entry: HistoryEntry) -> HistoryEntry:
    """Append one history record. Returns the entry (with id set)."""
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if entry.id <= 0:
        entry.id = _next_id()
    if not entry.created:
        entry.created = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
    return entry


def record_commit_from_edit_set(
    log_path: str | Path,
    *,
    operator: str,
    note: str,
    locator: str,
    backup: str | Path,
    edits: list[Any],
) -> HistoryEntry:
    """Record a transform commit (restyle / retune / inject)."""
    hist_edits = [
        HistoryEdit(
            msg_id=e.msg_id,
            speaker=e.speaker,
            kind=getattr(e, "kind", "replace"),
            before=e.before,
            after=e.after,
            flags=list(getattr(e, "flags", []) or []),
        )
        for e in edits
    ]
    lo, hi = _span_from_edits(hist_edits)
    return append_entry(HistoryEntry(
        id=0,
        created="",
        log=str(Path(log_path).resolve()),
        event="commit",
        operator=operator,
        note=note,
        locator=locator,
        backup=str(Path(backup).resolve()),
        changed_from=lo,
        changed_to=hi,
        edits=hist_edits,
    ))


def record_interlude_commit(
    log_path: str | Path,
    *,
    stage: str,
    after_msg_id: int,
    new_msg_id: int,
    speaker: str,
    body: str,
    backup: str | Path,
) -> HistoryEntry:
    """Record an interlude insert."""
    return append_entry(HistoryEntry(
        id=0,
        created="",
        log=str(Path(log_path).resolve()),
        event="commit",
        operator="interlude",
        note=f"stage={stage}",
        locator=f"after msg {after_msg_id}",
        backup=str(Path(backup).resolve()),
        changed_from=new_msg_id,
        changed_to=new_msg_id,
        edits=[
            HistoryEdit(
                msg_id=new_msg_id,
                speaker=speaker,
                kind="inject",
                before="",
                after=body,
            )
        ],
    ))


def record_undo(log_path: str | Path, *, backup_restored: str | Path) -> HistoryEntry:
    """Record that the log was restored from a backup."""
    return append_entry(HistoryEntry(
        id=0,
        created="",
        log=str(Path(log_path).resolve()),
        event="undo",
        operator="undo",
        note=f"restored from {Path(backup_restored).name}",
        locator="",
        backup=str(Path(backup_restored).resolve()),
        changed_from=None,
        changed_to=None,
        edits=[],
    ))


def block_edits(
    before: list[tuple[str, str]],
    after: list[tuple[str, str]],
) -> list[HistoryEdit]:
    """Per-paragraph diff between two ``(block_id, text)`` sequences: changed,
    added and removed blocks, in reading order."""
    old = dict(before)
    new_ids = {bid for bid, _ in after}
    edits: list[HistoryEdit] = []
    for bid, text in after:
        if bid not in old:
            edits.append(HistoryEdit(-1, "", "inject", "", text, block_id=bid))
        elif old[bid] != text:
            edits.append(HistoryEdit(-1, "", "replace", old[bid], text, block_id=bid))
    for bid, text in before:
        if bid not in new_ids:
            edits.append(HistoryEdit(-1, "", "remove", text, "", block_id=bid))
    return edits


def record_manuscript_change(
    log_path: str | Path,
    *,
    operator: str,
    layer: str,
    scene_id: str,
    scene_title: str,
    span: tuple[int | None, int | None],
    note: str,
    locator: str,
    backup: str | Path,
    edits: list[HistoryEdit],
) -> HistoryEntry | None:
    """Record a manuscript change (``committed`` or ``manually edited``).
    ``span`` is the scene's log anchor, so the drawer's scene filter finds it.
    Nothing is recorded when no paragraph changed."""
    if not edits:
        return None
    return append_entry(HistoryEntry(
        id=0,
        created="",
        log=str(Path(log_path).resolve()),
        event="commit",
        operator=operator,
        note=note,
        locator=locator,
        backup=str(Path(backup).resolve()),
        changed_from=span[0],
        changed_to=span[1],
        edits=edits,
        layer=layer,
        scene_id=scene_id,
        scene_title=scene_title,
    ))


def load_all(*, log_path: str | Path | None = None) -> list[HistoryEntry]:
    path = _history_path()
    if not path.exists():
        return []
    entries: list[HistoryEntry] = []
    wanted = config.log_key(log_path) if log_path else None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = HistoryEntry.from_json(json.loads(line))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if wanted and config.log_key(entry.log) != wanted:
            continue
        entries.append(entry)
    return entries


def list_entries(
    *,
    log_path: str | Path | None = None,
    limit: int = 20,
) -> list[HistoryEntry]:
    """Most recent first."""
    entries = load_all(log_path=log_path)
    entries.sort(key=lambda e: e.id, reverse=True)
    return entries[: max(1, limit)]


def get_entry(entry_id: int, *, log_path: str | Path | None = None) -> HistoryEntry | None:
    for e in load_all(log_path=log_path):
        if e.id == entry_id:
            return e
    return None


def get_latest(*, log_path: str | Path | None = None) -> HistoryEntry | None:
    """The latest *log* entry — what `edits undo` restores. Manuscript entries
    are skipped: their backups are manuscript files, not logs."""
    entries = [e for e in load_all(log_path=log_path) if e.layer == LOG_LAYER]
    return max(entries, key=lambda e: e.id) if entries else None
