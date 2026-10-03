"""Project home on disk: world files (authored), state ledgers (derived), the log.

Layout of a project home (e.g. examples/lantern-quay-play):

    play.json               name, POV, models
    world/contract.md       narrator contract (stable, cached)
    world/canon.md          public canon digest (stable, cached)
    world/truths.md         planner-only: secrets, the week, keep-open list
    world/rules.md          planner-only: world mechanics (how news travels, what can be sensed)
    world/secrets.json      leak-check terms per secret, and who knows them
    world/spine.json        scene cards in order
    world/cast/<id>.json    character cards
    state/*.json            ledgers: state, knowledge, cast, threads, motifs
    log.jsonl               SillyTavern-compatible play log (ProseWright reads it)
    status.json             status-line data for the GUI
    inbox.jsonl             director actions from the GUI
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILES = ("state", "knowledge", "cast", "threads", "motifs")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Project:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        if not (self.home / "play.json").exists():
            raise SystemExit(f"not a player-mode project: {self.home} (no play.json)")
        self.config = read_json(self.home / "play.json")
        from . import backend  # the play folder may choose how Claude is reached
        backend.configure(self.config.get("claude"))
        # A fresh home starts from its authored starting ledgers, and gets its own
        # turn history (one commit per turn, for /undo) unless it already sits
        # inside another git repository.
        if not (self.home / "state").exists() and (self.home / "state_init").exists():
            import shutil
            shutil.copytree(self.home / "state_init", self.home / "state")
            if not self._inside_other_repo():
                self.git_init()

    def _inside_other_repo(self) -> bool:
        if (self.home / ".git").exists():
            return False
        probe = subprocess.run(["git", "-C", str(self.home), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True)
        return probe.returncode == 0 and probe.stdout.strip() == "true"

    # --- world (authored, read-only to the engine) ---------------------------
    def world_text(self, name: str) -> str:
        path = self.home / "world" / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def spine(self) -> list[dict]:
        return read_json(self.home / "world" / "spine.json", [])

    def scene_card(self, scene_id: str) -> dict:
        for card in self.spine():
            if card["id"] == scene_id:
                return card
        raise KeyError(f"no scene card {scene_id!r}")

    def next_scene(self, scene_id: str) -> dict | None:
        cards = self.spine()
        for i, card in enumerate(cards):
            if card["id"] == scene_id:
                return cards[i + 1] if i + 1 < len(cards) else None
        return None

    def cast_card(self, char_id: str) -> dict:
        return read_json(self.home / "world" / "cast" / f"{char_id}.json", {})

    def find_cast(self, name: str) -> str | None:
        """Cast id for an id or display name ("Lara" → "lara")."""
        folder = self.home / "world" / "cast"
        key = name.strip().lower()
        for path in sorted(folder.glob("*.json")):
            card = read_json(path, {})
            if key in (path.stem.lower(), str(card.get("name", "")).lower()):
                return path.stem
        return None

    def secrets(self) -> list[dict]:
        return read_json(self.home / "world" / "secrets.json", [])

    # --- state ledgers -------------------------------------------------------
    def load(self, name: str) -> Any:
        return read_json(self.home / "state" / f"{name}.json", {} if name != "threads" else [])

    def save(self, name: str, data: Any) -> None:
        write_json(self.home / "state" / f"{name}.json", data)

    # --- log -----------------------------------------------------------------
    @property
    def log_path(self) -> Path:
        return self.home / "log.jsonl"

    def ensure_log(self) -> None:
        if not self.log_path.exists():
            header = {
                "chat_metadata": {"player_mode": True, "project": self.config.get("name", "")},
                "user_name": "unused",
                "character_name": "unused",
            }
            self.log_path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")

    def append_log(self, name: str, text: str, *, is_user: bool, meta: dict | None = None) -> None:
        self.ensure_log()
        row = {
            "name": name,
            "is_user": is_user,
            "is_system": False,
            "send_date": now_iso(),
            "mes": text,
            "extra": {"player_mode": meta or {}},
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def replace_last_row(self, row: dict) -> None:
        """Rewrite the last log row (another telling of the last reply)."""
        lines = [line for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        lines[-1] = json.dumps(row, ensure_ascii=False)
        tmp = self.log_path.with_suffix(".jsonl.partial")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.log_path)

    def turns(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return rows[1:]

    # --- git as time ---------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        # Snapshot commits carry the engine's own identity; the user's git config is untouched.
        ident = ["-c", "user.name=player-mode", "-c", "user.email=player-mode@localhost"]
        return subprocess.run(["git", *ident, "-C", str(self.home), *args], capture_output=True, text=True)

    def git_init(self) -> None:
        if not (self.home / ".git").exists():
            self._git("init", "-q")
            self.commit("init")

    def commit(self, message: str) -> None:
        if not (self.home / ".git").exists():
            return
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def undo(self) -> str:
        """Drop the last turn commit. Only called on an explicit /undo."""
        if not (self.home / ".git").exists():
            return ("undo needs a turn history: copy this folder somewhere outside any git "
                    "repository and play it from there")
        head = self._git("log", "-1", "--format=%s").stdout.strip()
        if head in ("", "init"):
            return "nothing to undo"
        self._git("reset", "-q", "--hard", "HEAD~1")
        return f"undid: {head}"
