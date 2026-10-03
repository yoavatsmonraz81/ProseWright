"""Typography that persists: per-role choices, imported files, named families.

The bundled faces are a front-end asset — a folder of woff2 and a stylesheet, no
server logic required. What has to live here is everything the browser cannot
keep: which face is chosen for which role, and the faces you supplied yourself.

Two ways to supply one, and the first is better:

*Name an installed family.* Nothing is copied anywhere; CSS resolves the name
against the OS. This matters legally rather than technically — the typewriter
faces worth wanting (Maquina de Escribir, Bohemian Typewriter, Traveling
Typewriter) are free for personal use and forbid redistribution. Naming one is
not redistribution, so the awkward case simply stops being a case.

*Import a file.* For a face you want to travel with the project. It lands in
`workspace/fonts/`, which is gitignored, and is served read-only to the app.

Either way the licence you state is shown beside the family name in the picker,
so the distinction is visible where the choice is made rather than buried in a
document. Exports carry no fonts at all, so nothing here can leak into a
published edition.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

SCHEMA = "story-editor/fonts@1"

# What a face can be chosen for. `page` is the manuscript itself; `log` exists so
# the source layer can look different from the derived one, which tells the eye
# which layer it is in before it reads a word.
ROLES = ("page", "log", "ui", "diff")

ALLOWED_SUFFIXES = (".ttf", ".otf", ".woff2", ".woff")

# Kinds of provenance, which is also the licence story.
KIND_BUNDLED = "bundled"  # ships with the app, open licence
KIND_SYSTEM = "system"    # installed on this machine, named by the user
KIND_IMPORTED = "imported"  # a file the user placed in workspace/fonts/

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


@dataclass
class RoleSetting:
    """A role's face and the metrics a typewriter page actually needs."""

    family: str = ""
    size: float = 0.0          # px; 0 = inherit the app default
    line_height: float = 0.0   # unitless multiplier
    measure: int = 0           # line length in `ch`
    letter_spacing: float = 0.0  # em; typewriter faces often want a hair more

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "size": self.size,
            "line_height": self.line_height,
            "measure": self.measure,
            "letter_spacing": self.letter_spacing,
        }

    @classmethod
    def from_json(cls, d: Any) -> "RoleSetting":
        if not isinstance(d, dict):
            return cls()
        return cls(
            family=str(d.get("family") or ""),
            size=float(d.get("size") or 0),
            line_height=float(d.get("line_height") or 0),
            measure=int(d.get("measure") or 0),
            letter_spacing=float(d.get("letter_spacing") or 0),
        )


@dataclass
class UserFace:
    """A face the user brought: named-on-this-machine, or imported as a file."""

    family: str
    kind: str = KIND_SYSTEM
    licence: str = ""
    file: str = ""      # basename inside workspace/fonts (imported only)
    style: str = "normal"
    weight: str = "400"
    added: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "kind": self.kind,
            "licence": self.licence,
            "file": self.file,
            "style": self.style,
            "weight": self.weight,
            "added": self.added,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "UserFace":
        return cls(
            family=str(d.get("family") or ""),
            kind=str(d.get("kind") or KIND_SYSTEM),
            licence=str(d.get("licence") or ""),
            file=str(d.get("file") or ""),
            style=str(d.get("style") or "normal"),
            weight=str(d.get("weight") or "400"),
            added=str(d.get("added") or ""),
        )


@dataclass
class FontConfig:
    roles: dict[str, RoleSetting] = field(default_factory=dict)
    faces: list[UserFace] = field(default_factory=list)
    updated: str = ""

    def face(self, family: str) -> UserFace | None:
        lowered = family.strip().lower()
        return next((f for f in self.faces if f.family.lower() == lowered), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "updated": self.updated,
            "roles": {k: v.to_json() for k, v in self.roles.items()},
            "faces": [f.to_json() for f in self.faces],
        }


def path() -> Path:
    return Path(config.FONTS_JSON)


def fonts_dir() -> Path:
    return Path(config.FONTS_DIR)


def load() -> FontConfig:
    p = path()
    if not p.exists():
        return FontConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return FontConfig()
    return FontConfig(
        roles={
            k: RoleSetting.from_json(v)
            for k, v in (data.get("roles") or {}).items()
            if k in ROLES
        },
        faces=[UserFace.from_json(f) for f in (data.get("faces") or [])],
        updated=str(data.get("updated") or ""),
    )


def save(cfg: FontConfig) -> Path:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p.write_text(
        json.dumps(cfg.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return p


def set_role(role: str, patch: dict[str, Any]) -> FontConfig:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r} — expected one of {', '.join(ROLES)}")
    cfg = load()
    current = cfg.roles.get(role) or RoleSetting()
    merged = RoleSetting(
        family=str(patch.get("family", current.family) or ""),
        size=float(patch.get("size", current.size) or 0),
        line_height=float(patch.get("line_height", current.line_height) or 0),
        measure=int(patch.get("measure", current.measure) or 0),
        letter_spacing=float(patch.get("letter_spacing", current.letter_spacing) or 0),
    )
    cfg.roles[role] = merged
    save(cfg)
    return cfg


def add_system_face(family: str, *, licence: str = "") -> UserFace:
    """Register a family name to resolve against the OS.

    No file, no copy, no redistribution question — which is exactly why this is
    the recommended path for anything not open-licensed.
    """
    family = family.strip()
    if not family:
        raise ValueError("family name is required")
    if not _SAFE_NAME.match(family):
        raise ValueError(f"implausible family name: {family!r}")
    cfg = load()
    existing = cfg.face(family)
    if existing is not None:
        existing.kind = KIND_SYSTEM
        if licence:
            existing.licence = licence
        save(cfg)
        return existing
    face = UserFace(
        family=family,
        kind=KIND_SYSTEM,
        licence=licence or "unstated — installed locally, display only",
        added=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    cfg.faces.append(face)
    save(cfg)
    return face


def import_file(
    filename: str,
    data: bytes,
    *,
    family: str = "",
    licence: str = "",
    style: str = "normal",
    weight: str = "400",
) -> UserFace:
    """Store a font file in the workspace and register it.

    No conversion step: browsers load TTF and OTF directly, and WOFF2 only buys
    transfer size, which a localhost tool does not spend.
    """
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"unsupported font file {name!r} — expected one of "
            f"{', '.join(ALLOWED_SUFFIXES)}"
        )
    if not _SAFE_NAME.match(Path(name).stem):
        raise ValueError(f"implausible file name: {name!r}")
    if not data:
        raise ValueError("empty font file")

    target_dir = fonts_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / name).write_bytes(data)

    cfg = load()
    family = (family or Path(name).stem.replace("-", " ")).strip()
    face = cfg.face(family)
    if face is None:
        face = UserFace(family=family)
        cfg.faces.append(face)
    face.kind = KIND_IMPORTED
    face.file = name
    face.style = style
    face.weight = weight
    face.licence = licence or face.licence or "unstated — local file, display only"
    face.added = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(cfg)
    return face


def remove_face(family: str) -> bool:
    """Forget a face. The file, if any, stays on disk — deleting a font because
    a dropdown changed would be a surprise, and the workspace is the user's."""
    cfg = load()
    face = cfg.face(family)
    if face is None:
        return False
    cfg.faces.remove(face)
    for role, setting in cfg.roles.items():
        if setting.family.lower() == family.strip().lower():
            setting.family = ""
    save(cfg)
    return True


def font_path(name: str) -> Path | None:
    """Resolve a served font file, refusing anything outside the fonts dir."""
    root = fonts_dir().resolve()
    if not root.exists():
        return None
    candidate = (root / Path(name).name).resolve()
    if not str(candidate).startswith(str(root)) or not candidate.is_file():
        return None
    if candidate.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    return candidate


def registry() -> dict[str, Any]:
    """Everything the picker needs, with availability resolved."""
    cfg = load()
    faces: list[dict[str, Any]] = []
    for face in cfg.faces:
        row = face.to_json()
        if face.kind == KIND_IMPORTED:
            resolved = font_path(face.file) if face.file else None
            row["available"] = resolved is not None
            row["url"] = f"/fonts/file/{face.file}" if resolved else ""
        else:
            # A named system family cannot be verified from the server; the
            # client measures whether it resolved and says so in the preview.
            row["available"] = None
            row["url"] = ""
        faces.append(row)
    return {
        "roles": {
            role: (cfg.roles.get(role) or RoleSetting()).to_json() for role in ROLES
        },
        "faces": faces,
        "dir": str(fonts_dir()),
        "updated": cfg.updated,
        "kinds": {
            KIND_BUNDLED: "ships with the editor under an open licence",
            KIND_SYSTEM: "installed on this machine, named here",
            KIND_IMPORTED: "file stored in the workspace",
        },
    }
