"""Mini-spine seed schema for Author Studio fixtures."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config

MINI_SPINE_SCHEMA = "story-editor/mini_spine@1"

FIELD_GLOSSARY: list[dict[str, str]] = [
    {
        "field": "schema",
        "meaning": "Must be story-editor/mini_spine@1 so Author Studio accepts the file.",
    },
    {
        "field": "title",
        "meaning": "Human name for this generative plan (shown in Story).",
    },
    {
        "field": "characters",
        "meaning": "Cast names available for sparks / first-turn picker.",
    },
    {
        "field": "lore_paths",
        "meaning": "Paths relative to the fixture home for lore JSON (optional).",
    },
    {
        "field": "beats[].label",
        "meaning": "Stable id used by progress and TOC (A1, A2, …). Keep unique.",
    },
    {
        "field": "beats[].title",
        "meaning": "Short beat title shown in Author / Story.",
    },
    {
        "field": "beats[].criteria",
        "meaning": (
            "Observable must-lands Advance checks on the generated prose. "
            "Each string should be something that can appear on the page "
            "(not vibes). At least one required per beat."
        ),
    },
    {
        "field": "beats[].location",
        "meaning": "Where the beat is set; used in sparks and Story span hints.",
    },
    {
        "field": "beats[].speakers",
        "meaning": "Who may speak in the beat (order can bias first turn).",
    },
    {
        "field": "beats[].story_date / story_time",
        "meaning": "In-world clock for the spark header (optional but useful).",
    },
    {
        "field": "beats[].notes",
        "meaning": "Director notes fed into the spark — not gate-checked.",
    },
    {
        "field": "initial_state",
        "meaning": (
            "Observable facts true at run start (soft check on step 1). "
            "Destination spine field — not the walk length N."
        ),
    },
    {
        "field": "end_state",
        "meaning": (
            "Destination conditions the full run must evidence. "
            "N and beat_plan live on the run record, not here."
        ),
    },
]


@dataclass
class MiniBeat:
    id: int
    label: str
    title: str
    criteria: list[str]
    location: str = ""
    speakers: list[str] = field(default_factory=list)
    story_date: str = ""
    story_time: str = ""
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "title": self.title,
            "criteria": list(self.criteria),
            "location": self.location,
            "speakers": list(self.speakers),
            "story_date": self.story_date,
            "story_time": self.story_time,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MiniBeat:
        crit = d.get("criteria") or []
        if isinstance(crit, str):
            crit = [crit]
        return cls(
            id=int(d.get("id") or 0),
            label=str(d.get("label") or f"A{d.get('id') or '?'}"),
            title=str(d.get("title") or ""),
            criteria=[str(c).strip() for c in crit if str(c).strip()],
            location=str(d.get("location") or ""),
            speakers=[str(s) for s in (d.get("speakers") or [])],
            story_date=str(d.get("story_date") or ""),
            story_time=str(d.get("story_time") or ""),
            notes=str(d.get("notes") or ""),
        )


def _str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n")]
        return [ln for ln in lines if ln]
    return [str(x).strip() for x in raw if str(x).strip()]


@dataclass
class MiniSpine:
    title: str
    beats: list[MiniBeat]
    characters: list[str] = field(default_factory=list)
    lore_paths: list[str] = field(default_factory=list)
    initial_state: list[str] = field(default_factory=list)
    end_state: list[str] = field(default_factory=list)
    path: Path | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": MINI_SPINE_SCHEMA,
            "title": self.title,
            "characters": list(self.characters),
            "lore_paths": list(self.lore_paths),
            "initial_state": list(self.initial_state),
            "end_state": list(self.end_state),
            "beats": [b.to_json() for b in self.beats],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any], *, path: Path | None = None) -> MiniSpine:
        beats = [MiniBeat.from_json(b) for b in (d.get("beats") or [])]
        return cls(
            title=str(d.get("title") or "mini-spine"),
            beats=beats,
            characters=[str(c) for c in (d.get("characters") or [])],
            lore_paths=[str(p) for p in (d.get("lore_paths") or [])],
            initial_state=_str_list(d.get("initial_state")),
            end_state=_str_list(d.get("end_state")),
            path=path,
        )

    def beat_by_label(self, label: str) -> MiniBeat | None:
        for b in self.beats:
            if b.label == label or str(b.id) == label:
                return b
        return None

    def next_open(self, completed_labels: set[str]) -> MiniBeat | None:
        for b in self.beats:
            if b.label not in completed_labels:
                return b
        return None

    def labels(self) -> set[str]:
        return {b.label for b in self.beats}


def default_seed_path() -> Path:
    return Path(getattr(config, "MINI_SPINE", None) or Path(config.HOME_DIR) / "mini_spine.json")


def validate_seed(spine: MiniSpine) -> None:
    """Raise ValueError if the spine cannot drive Author Advance."""
    if len(spine.beats) < 1:
        raise ValueError("mini-spine has no beats")
    seen: set[str] = set()
    for b in spine.beats:
        label = (b.label or "").strip()
        if not label:
            raise ValueError("every beat needs a non-empty label")
        if label in seen:
            raise ValueError(f"duplicate beat label {label}")
        seen.add(label)
        if not b.criteria:
            raise ValueError(f"beat {label} needs observable criteria")
        if not (b.title or "").strip():
            raise ValueError(f"beat {label} needs a title")


def parse_seed(data: dict[str, Any], *, path: Path | None = None) -> MiniSpine:
    if not isinstance(data, dict):
        raise ValueError("mini-spine must be a JSON object")
    schema = str(data.get("schema") or "").strip()
    if schema and schema != MINI_SPINE_SCHEMA:
        raise ValueError(
            f"unsupported schema {schema!r} — expected {MINI_SPINE_SCHEMA}"
        )
    spine = MiniSpine.from_json(data, path=path)
    # Renumber missing ids in order for editor-created beats.
    for i, beat in enumerate(spine.beats, start=1):
        if beat.id <= 0:
            beat.id = i
    validate_seed(spine)
    return spine


def load_seed(path: str | Path | None = None) -> MiniSpine:
    p = Path(path) if path else default_seed_path()
    if not p.exists():
        raise FileNotFoundError(f"mini-spine seed missing: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return parse_seed(data, path=p)


def save_seed(
    spine: MiniSpine | dict[str, Any],
    path: str | Path | None = None,
) -> MiniSpine:
    """Validate and atomically write the mini-spine seed."""
    p = Path(path) if path else default_seed_path()
    if isinstance(spine, dict):
        parsed = parse_seed(spine, path=p)
    else:
        validate_seed(spine)
        parsed = spine
        parsed.path = p
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(parsed.to_json(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".mini_spine.", suffix=".json", dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        Path(tmp_name).replace(p)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    parsed.path = p
    return parsed


def load_progress(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else Path(config.WORKSPACE_DIR) / "author_progress.json"
    if not p.exists():
        return {"completed": [], "attempts": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {
            "completed": [],
            "attempts": {},
        }
    except json.JSONDecodeError:
        return {"completed": [], "attempts": {}}


def save_progress(data: dict[str, Any], path: str | Path | None = None) -> Path:
    p = Path(path) if path else Path(config.WORKSPACE_DIR) / "author_progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def prune_progress_to_labels(
    progress: dict[str, Any],
    labels: set[str],
) -> dict[str, Any]:
    """Drop completed/awaiting/commits/attempts that no longer exist on the spine."""
    out = dict(progress)
    done = [str(x) for x in (out.get("completed") or []) if str(x) in labels]
    out["completed"] = done
    awaiting = str(out.get("awaiting") or "") or None
    if awaiting and awaiting not in labels:
        out.pop("awaiting", None)
    elif awaiting:
        out["awaiting"] = awaiting
    attempts = out.get("attempts") or {}
    if isinstance(attempts, dict):
        out["attempts"] = {k: v for k, v in attempts.items() if str(k) in labels}
    commits = out.get("commits") or []
    if isinstance(commits, list):
        out["commits"] = [
            c for c in commits
            if isinstance(c, dict) and str(c.get("label") or "") in labels
        ]
    # Forward redo stack is unsafe across plan edits.
    out.pop("redo", None)
    return out


def empty_progress() -> dict[str, Any]:
    return {"completed": [], "attempts": {}, "commits": [], "redo": []}


def save_seed_and_sync_progress(
    data: dict[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Write seed and prune progress labels that disappeared."""
    spine = save_seed(data, path=path)
    progress = prune_progress_to_labels(load_progress(), spine.labels())
    save_progress(progress)
    return seed_view(spine, progress)


def load_external_seed(
    *,
    json_body: dict[str, Any] | None = None,
    path: str | Path | None = None,
    keep_progress: bool = False,
) -> dict[str, Any]:
    """Replace the fixture mini-spine from uploaded JSON or a disk path."""
    if json_body is not None:
        data = json_body
        src = "upload"
    elif path is not None:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"spine file not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        src = str(p)
    else:
        raise ValueError("provide json or path")

    spine = parse_seed(data, path=default_seed_path())
    save_seed(spine)

    prev = load_progress()
    labels = spine.labels()
    if keep_progress:
        completed = {str(x) for x in (prev.get("completed") or [])}
        if completed and not completed.issubset(labels):
            progress = empty_progress()
        else:
            progress = prune_progress_to_labels(prev, labels)
    else:
        progress = empty_progress()
    save_progress(progress)
    view = seed_view(spine, progress)
    view["loaded_from"] = src
    return view


def seed_view(
    spine: MiniSpine | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if spine is None:
        spine = load_seed()
    if progress is None:
        progress = load_progress()
    return {
        "schema": MINI_SPINE_SCHEMA,
        "path": str(spine.path or default_seed_path()),
        "spine": spine.to_json(),
        "progress": progress,
    }


def _template_json() -> dict[str, Any]:
    """Prefer the project's own ``mini_spine.template.json``; fall back to an in-module sample."""
    candidates = [Path(config.HOME_DIR) / "mini_spine.template.json"]
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
    return {
        "schema": MINI_SPINE_SCHEMA,
        "title": "Example three-beat spine",
        "characters": ["Wren", "Ilse"],
        "lore_paths": ["worlds/mini_lore.json"],
        "initial_state": [
            "Wren and Ilse are together on the quay",
        ],
        "end_state": [
            "Ilse has read the manifest aloud",
        ],
        "beats": [
            {
                "id": 1,
                "label": "A1",
                "title": "Opening beat title",
                "criteria": [
                    "Observable must-land #1 (what Advance checks on the page)",
                    "Observable must-land #2",
                ],
                "location": "Scene location name",
                "speakers": ["Wren", "Ilse"],
                "story_date": "Thursday, October 4, 1792",
                "story_time": "8:05 AM",
                "notes": "Director notes for the spark — not gate-checked.",
            }
        ],
    }


def seed_template() -> dict[str, Any]:
    sample = _template_json()
    # Ensure the sample itself validates so Help never ships a broken example.
    parse_seed(sample)
    return {
        "schema": MINI_SPINE_SCHEMA,
        "glossary": FIELD_GLOSSARY,
        "template": sample,
        "notes": [
            "Advance walks beats in order (A1 → A2 → …) and gates on criteria.",
            "Labels in author_progress must match beats[].label.",
            "initial_state / end_state are destination fields; N lives on the run.",
            "Load replaces mini_spine.json under the current Author Studio home.",
        ],
    }
