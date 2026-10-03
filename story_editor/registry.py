"""The project registry — which projects this machine knows, and which is open.

    ~/.config/story-editor/projects.json      (STORY_EDITOR_REGISTRY overrides)

    {
      "schema": "story-editor/registry@1",
      "active": "lantern-quay",
      "projects": [
        {"id": "lantern-quay", "home": "/home/me/stories/lantern-quay", "title": "Lantern Quay"}
      ]
    }

The registry is only a list of pointers. Adding, removing or switching a
project never copies, moves or deletes a project's files. Switching changes
which home ``config`` resolves the next time the engine starts (see config.py
for the order: STORY_EDITOR_HOME, then the registry's active project, then the
defaults).

This module must not import ``config`` — config imports it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import project as project_mod

SCHEMA = "story-editor/registry@1"


class RegistryError(ValueError):
    pass


def path() -> Path:
    env = os.environ.get("STORY_EDITOR_REGISTRY")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "story-editor" / "projects.json"


def projects_dir() -> Path:
    """Where projects loaded from bundles in the GUI go (STORY_EDITOR_PROJECTS_DIR overrides)."""
    env = os.environ.get("STORY_EDITOR_PROJECTS_DIR")
    return Path(env).expanduser().resolve() if env else Path.home() / "story-editor-projects"


@dataclass
class Entry:
    id: str
    home: Path
    title: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "home": str(self.home), "title": self.title}


@dataclass
class Registry:
    active: str | None = None
    projects: list[Entry] = field(default_factory=list)

    def get(self, project_id: str) -> Entry | None:
        return next((e for e in self.projects if e.id == project_id), None)

    def by_home(self, home: Path) -> Entry | None:
        home = Path(home).expanduser().resolve()
        return next((e for e in self.projects if e.home == home), None)

    def active_entry(self) -> Entry | None:
        return self.get(self.active) if self.active else None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "active": self.active,
            "projects": [e.to_json() for e in self.projects],
        }


def load(registry_path: Path | None = None) -> Registry:
    """The registry, or an empty one when the file does not exist yet.

    A file that exists but cannot be read raises: a broken registry must be
    fixed, not silently replaced by an empty one on the next write."""
    p = registry_path or path()
    if not p.exists():
        return Registry()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{p}: not readable as JSON ({exc})") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise RegistryError(f"{p}: schema must be {SCHEMA!r}")
    projects = []
    for item in raw.get("projects") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("home"), str):
            raise RegistryError(f"{p}: every project needs an id and a home")
        projects.append(Entry(id=item["id"], home=Path(item["home"]).expanduser().resolve(),
                              title=str(item.get("title") or "")))
    active = raw.get("active")
    if active is not None and not any(e.id == active for e in projects):
        raise RegistryError(f"{p}: active project {active!r} is not in the list")
    return Registry(active=active, projects=projects)


def save(reg: Registry, registry_path: Path | None = None) -> Path:
    """Write atomically: a crash mid-write leaves the previous registry intact."""
    p = registry_path or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".projects.", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(reg.to_json(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def slug(name: str) -> str:
    """A valid project id from a folder name."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]
    return s or "project"


def describe(home: Path) -> tuple[str, str]:
    """(id, title) for a home: from its manifest, else from the folder name.

    An invalid manifest raises ``project.ManifestError``."""
    manifest = project_mod.load(home)
    if manifest is not None:
        return manifest.id, manifest.title
    return slug(home.name), home.name


def add(home: str | Path, *, registry_path: Path | None = None) -> Entry:
    home = Path(home).expanduser().resolve()
    if not home.is_dir():
        raise RegistryError(f"not a folder: {home}")
    pid, title = describe(home)
    reg = load(registry_path)
    same_home = reg.by_home(home)
    if same_home is not None:
        if same_home.id != pid:
            raise RegistryError(
                f"{home} is registered as {same_home.id!r} but its manifest now says {pid!r}; "
                f"remove it and add it again")
        same_home.title = title
        save(reg, registry_path)
        return same_home
    clash = reg.get(pid)
    if clash is not None:
        raise RegistryError(
            f"a project called {pid!r} is already registered at {clash.home}; "
            f"two homes cannot share an id")
    entry = Entry(id=pid, home=home, title=title)
    reg.projects.append(entry)
    save(reg, registry_path)
    return entry


def remove(project_id: str, *, registry_path: Path | None = None) -> Entry:
    """Forget a project. Its files are not touched."""
    reg = load(registry_path)
    entry = reg.get(project_id)
    if entry is None:
        raise RegistryError(f"no project called {project_id!r}")
    reg.projects.remove(entry)
    if reg.active == project_id:
        reg.active = None
    save(reg, registry_path)
    return entry


def switch(project_id: str, *, registry_path: Path | None = None) -> Entry:
    """Make a project the active one. Takes effect when the engine next starts."""
    reg = load(registry_path)
    entry = reg.get(project_id)
    if entry is None:
        raise RegistryError(f"no project called {project_id!r}")
    if not entry.home.is_dir():
        raise RegistryError(f"{project_id!r} points at {entry.home}, which no longer exists")
    describe(entry.home)  # an invalid manifest refuses here, not at the next start
    reg.active = project_id
    save(reg, registry_path)
    return entry


def active_home(registry_path: Path | None = None) -> tuple[Path | None, str]:
    """The active project's home for config, and a warning ("" when fine).

    Never raises: config must still start (so the problem can be fixed from
    the CLI or the GUI) when the registry is broken or points at a lost folder.
    """
    try:
        reg = load(registry_path)
    except RegistryError as exc:
        return None, f"project registry ignored: {exc}"
    entry = reg.active_entry()
    if entry is None:
        return None, ""
    if not entry.home.is_dir():
        return None, (f"active project {entry.id!r} points at {entry.home}, which no longer "
                      f"exists; opened the default home instead")
    return entry.home, ""
