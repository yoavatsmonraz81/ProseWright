"""Backup / restore. Every write to a log must be preceded by a timestamped
backup so any change is reversible (the prime directive).

Backups live in workspace/backups/ as `<logstem>.<timestamp>.jsonl`.

A backup set belongs to ONE log. `restore` finds its source by globbing the
log's stem, so two different logs that happen to share a file name must not
share a directory — otherwise a restore can hand one log a copy of the other,
which is a data-loss bug wearing the costume of a safety feature. Only the
configured working log uses `workspace/backups/`; any other log (a test fixture,
a second project pointed at by `STORY_EDITOR_LOG`) keeps its backups beside
itself.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from . import config


def _stamp() -> str:
    # Serial review can land several individually reversible edits in one
    # second. Microseconds keep those backups distinct instead of letting a
    # later acceptance overwrite an earlier file with the same label.
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def backup_dir_for(log_path: str | Path) -> Path:
    """Where this log's backups live."""
    path = Path(log_path)
    try:
        is_working = path.resolve() == Path(config.working_log()).resolve()
    except OSError:
        is_working = False
    return config.BACKUP_DIR if is_working else path.parent / "backups"


def backup(log_path: str | Path, *, label: str | None = None) -> Path:
    log_path = Path(log_path)
    if not log_path.exists():
        raise FileNotFoundError(f"cannot back up missing log: {log_path}")
    dest_dir = backup_dir_for(log_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{_stamp()}" + (f"-{label}" if label else "")
    dest = dest_dir / f"{log_path.stem}.{suffix}.jsonl"
    shutil.copy2(log_path, dest)
    return dest


def list_backups(log_path: str | Path) -> list[Path]:
    log_path = Path(log_path)
    src_dir = backup_dir_for(log_path)
    if not src_dir.exists():
        return []
    found = sorted(src_dir.glob(f"{log_path.stem}.*.jsonl"))
    return found


def latest_backup(log_path: str | Path) -> Path | None:
    found = list_backups(log_path)
    return found[-1] if found else None


def restore(log_path: str | Path, *, from_backup: str | Path | None = None) -> Path:
    log_path = Path(log_path)
    src = Path(from_backup) if from_backup else latest_backup(log_path)
    if src is None:
        raise FileNotFoundError(f"no backups found for {log_path}")
    if not src.exists():
        raise FileNotFoundError(f"backup not found: {src}")
    shutil.copy2(src, log_path)
    return src
