"""Backup isolation — a backup set belongs to exactly one log.

Written after a near-miss: a live working log was reverted to a week-old
state because a fixture log shared both a file name and a backup directory with
it, and `restore` finds its source by globbing the log's stem. These tests pin
the two properties that make that impossible.
"""

from __future__ import annotations

from pathlib import Path

from story_editor import backup, config


def _log(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_working_log_backups_stay_in_the_workspace() -> None:
    assert backup.backup_dir_for(config.working_log()) == config.BACKUP_DIR


def test_other_logs_keep_backups_beside_themselves(tmp_path: Path) -> None:
    """A fixture log must never deposit anything in the live workspace."""
    fixture = _log(tmp_path / "story.jsonl", "{}\n")
    assert backup.backup_dir_for(fixture) == tmp_path / "backups"

    made = backup.backup(fixture, label="unit")
    assert made.parent == tmp_path / "backups"
    assert config.BACKUP_DIR not in made.parents


def test_same_named_logs_do_not_share_a_backup_set(tmp_path: Path) -> None:
    """The exact shape of the near-miss: two logs called story.jsonl."""
    first = _log(tmp_path / "a" / "story.jsonl", "FIRST\n")
    second = _log(tmp_path / "b" / "story.jsonl", "SECOND\n")
    backup.backup(first)
    backup.backup(second)

    assert len(backup.list_backups(first)) == 1
    assert len(backup.list_backups(second)) == 1
    assert backup.latest_backup(first).read_text(encoding="utf-8") == "FIRST\n"
    assert backup.latest_backup(second).read_text(encoding="utf-8") == "SECOND\n"


def test_restore_returns_a_logs_own_content(tmp_path: Path) -> None:
    first = _log(tmp_path / "a" / "story.jsonl", "FIRST\n")
    _log(tmp_path / "b" / "story.jsonl", "SECOND\n")
    backup.backup(first)
    backup.backup(tmp_path / "b" / "story.jsonl")

    first.write_text("EDITED\n", encoding="utf-8")
    backup.restore(first)
    assert first.read_text(encoding="utf-8") == "FIRST\n"


def test_no_backups_for_an_untouched_log(tmp_path: Path) -> None:
    fresh = _log(tmp_path / "fresh.jsonl", "{}\n")
    assert backup.list_backups(fresh) == []
    assert backup.latest_backup(fresh) is None
