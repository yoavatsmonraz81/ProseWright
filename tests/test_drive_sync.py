"""Drive sync state machine, against a directory standing in for Drive."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from story_editor import config, drive_sync as ds


class FakeDrive:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files = root / "workspace"
        self.files.mkdir(parents=True, exist_ok=True)

    def ready(self):
        return True, "", ""

    def read_manifest(self):
        path = self.root / ds.MANIFEST
        return json.loads(path.read_text()) if path.exists() else None

    def write_manifest(self, data):
        (self.root / ds.MANIFEST).write_text(json.dumps(data))

    def _mirror(self, src: Path, dst: Path) -> None:
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for rel in ds.snapshot(src, cache=False):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst / rel)

    def upload(self, local_dir):
        self._mirror(Path(local_dir), self.files)

    def download(self, local_dir):
        self._mirror(self.files, Path(local_dir))


def _machine(tmp_path: Path, name: str, monkeypatch) -> Path:
    ws = tmp_path / name / "workspace"
    ws.mkdir(parents=True)
    return ws


def _use(ws: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(config, "BACKUP_DIR", ws / "backups")


def test_two_machines_push_pull_and_divergence(tmp_path: Path, monkeypatch) -> None:
    drive = FakeDrive(tmp_path / "drive")
    laptop = _machine(tmp_path, "laptop", monkeypatch)
    home = _machine(tmp_path, "home", monkeypatch)
    (laptop / "manuscript.json").write_text("v1")
    (laptop / "backups").mkdir()
    (laptop / "backups" / "huge.json").write_text("never synced")

    _use(laptop, monkeypatch)
    assert ds.status(drive).kind == ds.DRIVE_EMPTY
    ds.push(drive)
    assert ds.status(drive).kind == ds.IN_SYNC
    assert not (drive.files / "backups").exists()

    # The workstation has an old copy and has never synced: it must choose.
    (home / "manuscript.json").write_text("v0")
    _use(home, monkeypatch)
    st = ds.status(drive)
    assert st.kind == ds.UNLINKED and st.local_changes == ["manuscript.json"]
    with pytest.raises(ds.SyncRefused):
        ds.pull(drive)
    result = ds.pull(drive, force=True)
    assert (home / "manuscript.json").read_text() == "v1"
    assert Path(result["backup"], "manuscript.json").read_text() == "v0"

    # Work at home, push; the laptop sees Drive ahead and pulls cleanly.
    (home / "manuscript.json").write_text("v2")
    (home / "queue.json").write_text("q")
    assert ds.status(drive).kind == ds.LOCAL_AHEAD
    ds.push(drive)
    _use(laptop, monkeypatch)
    st = ds.status(drive)
    assert st.kind == ds.DRIVE_AHEAD and st.drive_changes == ["manuscript.json", "queue.json"]
    with pytest.raises(ds.SyncRefused):
        ds.push(drive)
    ds.pull(drive)
    assert (laptop / "manuscript.json").read_text() == "v2"
    assert (laptop / "backups" / "huge.json").exists()

    # Both sides edit: diverged, and neither direction goes through unforced.
    (laptop / "manuscript.json").write_text("laptop edit")
    _use(home, monkeypatch)
    (home / "queue.json").write_text("home edit")
    ds.push(drive)
    _use(laptop, monkeypatch)
    st = ds.status(drive)
    assert st.kind == ds.DIVERGED
    assert st.local_changes == ["manuscript.json"] and st.drive_changes == ["queue.json"]
    with pytest.raises(ds.SyncRefused):
        ds.pull(drive)
    with pytest.raises(ds.SyncRefused):
        ds.push(drive)


def test_pull_removes_files_deleted_on_drive_after_backing_them_up(tmp_path: Path, monkeypatch) -> None:
    drive = FakeDrive(tmp_path / "drive")
    a = _machine(tmp_path, "a", monkeypatch)
    b = _machine(tmp_path, "b", monkeypatch)
    (a / "keep.json").write_text("k")
    (a / "old.json").write_text("o")
    _use(a, monkeypatch)
    ds.push(drive)
    _use(b, monkeypatch)
    ds.pull(drive, force=True)
    assert (b / "old.json").exists()

    _use(a, monkeypatch)
    (a / "old.json").unlink()
    ds.push(drive)
    _use(b, monkeypatch)
    result = ds.pull(drive)
    assert result["removed"] == ["old.json"]
    assert not (b / "old.json").exists()
    assert Path(result["backup"], "old.json").read_text() == "o"


def test_exclusions_match_rclone_filters() -> None:
    assert ds._excluded("backups/manuscript.json")
    assert ds._excluded(".drive_sync/base.json")
    assert ds._excluded("chapters/__pycache__/x.pyc")
    assert not ds._excluded("proofread_campaign/backups/x.json")
    assert not ds._excluded("chapters/stamps.json")
    flags = ds.Rclone("rclone", "gdrive:x")._filters()
    assert "/backups/**" in flags and "/stamps.json" in flags


def test_log_key_matches_across_machines() -> None:
    here = config.WORKSPACE_DIR / "my_story.jsonl"
    there = "/home/someone/Desktop/My Story/story-editor/workspace/my_story.jsonl"
    assert config.same_log(there, here)
    assert not config.same_log("/tmp/other/fixture.jsonl", here)


def test_background_job_reports_phases_and_result(tmp_path: Path, monkeypatch) -> None:
    import time

    drive = FakeDrive(tmp_path / "drive")
    ws = _machine(tmp_path, "a", monkeypatch)
    (ws / "manuscript.json").write_text("v1")
    _use(ws, monkeypatch)
    started = ds.start("push", transport=drive)
    assert started["running"] and started["direction"] == "push"
    for _ in range(100):
        if not ds.job()["running"]:
            break
        time.sleep(0.02)
    done = ds.job()
    assert done["phase"] == "done" and not done["error"]
    assert done["result"]["status"]["kind"] == ds.IN_SYNC

    # A refused direction answers at once, without starting a job.
    (ws / "manuscript.json").write_text("v2")
    with pytest.raises(ds.SyncRefused):
        ds.start("pull", transport=drive)
    assert ds.job()["direction"] == "push"


def test_rclone_stats_feed_progress() -> None:
    ds._phase("uploading")
    ds._report_stats({"bytes": 50, "totalBytes": 200, "transfers": 1, "totalTransfers": 4,
                      "transferring": [{"name": "manuscript.json"}]})
    progress = ds.job()["progress"]
    assert progress["bytes"] == 50 and progress["totalBytes"] == 200
    assert progress["current"] == ["manuscript.json"]
