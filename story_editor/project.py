"""Project manifests — what makes a STORY_EDITOR_HOME a project.

A project home is a folder with ``workspace/``, ``canon/``, ``cards/``, ``worlds/``
and a ``project.json`` naming the project and every external integration it
touches. Integrations are explicit: a project reaches a SillyTavern chat, a
Drive folder or an authored spine file only if its manifest names one. That is
what keeps two projects on one machine from writing into each other.

A home without a manifest gets no external integrations at all.

This module must not import ``config`` — config imports it.

    {
      "schema": "story-editor/project@1",
      "id": "lantern-quay",
      "title": "Lantern Quay",
      "log": "workspace/lantern_quay.jsonl",
      "integrations": {
        "sillytavern": {"chat": null, "characters_dir": null},
        "lorebooks": ["worlds/lantern_quay_lorebook.json"],
        "drive": {"remote": null},
        "authored_spine_md": null
      }
    }

Relative paths resolve against the project home; absolute paths are kept.
A missing ``lorebooks`` key means "every ``worlds/*.json``"; ``[]`` means none.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "story-editor/project@1"
MANIFEST_NAME = "project.json"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ManifestError(ValueError):
    pass


@dataclass
class Manifest:
    id: str
    title: str
    home: Path
    log: Path
    st_chat: Path | None = None
    st_characters_dir: Path | None = None
    lorebooks: list[Path] | None = None  # None = every worlds/*.json
    drive_remote: str | None = None
    authored_spine_md: Path | None = None
    play_home: Path | None = None  # a Player-mode play folder this project plays from
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_public(self) -> dict[str, Any]:
        """A JSON-safe description for the API and the CLI."""
        def s(p: Path | None) -> str | None:
            return str(p) if p is not None else None

        return {
            "id": self.id,
            "title": self.title,
            "home": str(self.home),
            "log": str(self.log),
            "integrations": {
                "sillytavern": {"chat": s(self.st_chat), "characters_dir": s(self.st_characters_dir)},
                "lorebooks": [str(p) for p in self.lorebooks] if self.lorebooks is not None else None,
                "drive": {"remote": self.drive_remote},
                "authored_spine_md": s(self.authored_spine_md),
                "player_mode": {"home": s(self.play_home)},
            },
        }


def _path(home: Path, value: Any, key: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{key} must be a path string or null")
    p = Path(value).expanduser()
    return p if p.is_absolute() else (home / p)


def validate(raw: Any) -> list[str]:
    """Every problem with a manifest, or [] when it is valid."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["manifest must be a JSON object"]
    if raw.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}")
    pid = raw.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        errors.append("id must be lowercase letters, digits and dashes (1-63 chars)")
    if not isinstance(raw.get("title", ""), str):
        errors.append("title must be a string")
    if not isinstance(raw.get("log"), str) or not raw["log"].strip():
        errors.append("log must be a path to the working log")
    integ = raw.get("integrations", {})
    if not isinstance(integ, dict):
        errors.append("integrations must be an object")
        return errors
    st = integ.get("sillytavern", {}) or {}
    if not isinstance(st, dict):
        errors.append("integrations.sillytavern must be an object")
    lore = integ.get("lorebooks")
    if lore is not None and not (isinstance(lore, list) and all(isinstance(x, str) for x in lore)):
        errors.append("integrations.lorebooks must be a list of paths")
    play = integ.get("player_mode", {}) or {}
    if not isinstance(play, dict):
        errors.append("integrations.player_mode must be an object")
    elif play.get("home") is not None and not isinstance(play.get("home"), str):
        errors.append("integrations.player_mode.home must be a path or null")
    drive = integ.get("drive", {}) or {}
    if not isinstance(drive, dict):
        errors.append("integrations.drive must be an object")
    elif drive.get("remote") is not None and not isinstance(drive.get("remote"), str):
        errors.append("integrations.drive.remote must be a string or null")
    return errors


def parse(raw: dict[str, Any], home: Path) -> Manifest:
    errors = validate(raw)
    if errors:
        raise ManifestError("; ".join(errors))
    integ = raw.get("integrations", {}) or {}
    st = integ.get("sillytavern", {}) or {}
    lore = integ.get("lorebooks")
    remote = (integ.get("drive", {}) or {}).get("remote")
    return Manifest(
        id=raw["id"],
        title=raw.get("title") or raw["id"],
        home=home,
        log=_path(home, raw["log"], "log"),
        st_chat=_path(home, st.get("chat"), "integrations.sillytavern.chat"),
        st_characters_dir=_path(home, st.get("characters_dir"), "integrations.sillytavern.characters_dir"),
        lorebooks=[_path(home, x, "integrations.lorebooks[]") for x in lore] if lore is not None else None,
        drive_remote=remote.rstrip("/") if isinstance(remote, str) and remote.strip() else None,
        authored_spine_md=_path(home, integ.get("authored_spine_md"), "integrations.authored_spine_md"),
        play_home=_path(home, (integ.get("player_mode") or {}).get("home"), "integrations.player_mode.home"),
        raw=raw,
    )


def load(home: str | Path) -> Manifest | None:
    """The manifest in ``home``, or None when the home has none.

    A manifest that exists but is invalid raises: a half-configured project
    must never silently fall back to someone else's integrations.
    """
    home = Path(home).expanduser().resolve()
    path = home / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON ({exc})") from exc
    try:
        return parse(raw, home)
    except ManifestError as exc:
        raise ManifestError(f"{path}: {exc}") from exc
