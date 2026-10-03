"""Drive sync never crosses projects — not even with force."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, drive_sync as ds
from test_drive_sync import FakeDrive


def _open(project_id: str, ws: Path, monkeypatch) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "PROJECT_ID", project_id)
    monkeypatch.setattr(config, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(config, "BACKUP_DIR", ws / "backups")


def test_push_stamps_the_owning_project(tmp_path, monkeypatch):
    drive = FakeDrive(tmp_path / "drive")
    _open("first-story", tmp_path / "first" / "workspace", monkeypatch)
    (config.WORKSPACE_DIR / "manuscript.json").write_text("first")
    ds.push(drive)
    assert drive.read_manifest()["project"] == "first-story"


def test_other_project_can_neither_push_nor_pull_even_forced(tmp_path, monkeypatch):
    drive = FakeDrive(tmp_path / "drive")
    _open("first-story", tmp_path / "first" / "workspace", monkeypatch)
    (config.WORKSPACE_DIR / "manuscript.json").write_text("first")
    ds.push(drive)
    before = json.dumps(drive.read_manifest(), sort_keys=True)

    second = tmp_path / "second" / "workspace"
    _open("second-story", second, monkeypatch)
    (second / "manuscript.json").write_text("second")
    st = ds.status(drive)
    assert st.kind == ds.WRONG_PROJECT and not st.can_push and not st.can_pull
    for direction in ("push", "pull"):
        for force in (False, True):
            fn = ds.push if direction == "push" else ds.pull
            with pytest.raises(ds.DriveError, match="holds project 'first-story'"):
                fn(drive, force=force)
            with pytest.raises(ds.DriveError):
                ds.start(direction, force=force, transport=drive)
    # Nothing moved on either side.
    assert json.dumps(drive.read_manifest(), sort_keys=True) == before
    assert (drive.files / "manuscript.json").read_text() == "first"
    assert (second / "manuscript.json").read_text() == "second"


def test_manifest_from_before_project_stamps_is_accepted(tmp_path, monkeypatch):
    drive = FakeDrive(tmp_path / "drive")
    _open("first-story", tmp_path / "first" / "workspace", monkeypatch)
    (config.WORKSPACE_DIR / "manuscript.json").write_text("first")
    ds.push(drive)
    legacy = drive.read_manifest()
    legacy.pop("project")
    drive.write_manifest(legacy)
    assert ds.status(drive).kind == ds.IN_SYNC


def test_project_without_a_drive_folder_reports_no_remote(monkeypatch):
    monkeypatch.setattr(config, "DRIVE_REMOTE", None)
    monkeypatch.setattr(config, "PROJECT_ID", "lantern-quay")
    st = ds.status(ds.Rclone(binary="/bin/true"))
    assert st.kind == ds.NO_REMOTE and "has no Drive folder" in st.detail
    with pytest.raises(ds.DriveError):
        ds.push(ds.Rclone(binary="/bin/true"), force=True)
