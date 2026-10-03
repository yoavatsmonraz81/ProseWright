"""The write fence itself: it must see every write route, and only real ones."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from write_fence import Fence


@pytest.fixture()
def fence_on(tmp_path):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    fence = Fence(guarded=[guarded]).install()
    yield fence, guarded
    fence.active = False  # audit hooks cannot be removed; switch this one off


def test_catches_every_write_route(fence_on, tmp_path):
    fence, g = fence_on
    (g / "a.txt").write_text("x")                                  # open for write
    os.replace(tmp_path / "guarded" / "a.txt", g / "b.txt")         # rename
    shutil.copyfile(g / "b.txt", g / "c.txt")                       # copy
    os.remove(g / "c.txt")                                          # delete
    (g / "sub").mkdir()                                             # mkdir
    shutil.rmtree(g / "sub")                                        # rmtree
    sqlite3.connect(g / "db.sqlite3").close()                       # sqlite
    events = {e for e, _ in fence.violations}
    assert {"open", "os.rename", "shutil.copyfile", "os.remove", "os.mkdir",
            "shutil.rmtree", "sqlite3.connect"} <= events


def test_reads_are_not_writes(fence_on):
    fence, g = fence_on
    (g / "r.txt").write_text("x")
    sqlite3.connect(g / "db.sqlite3").close()
    fence.violations.clear()
    (g / "r.txt").read_text()
    with open(g / "r.txt", "rb") as fh:
        fh.read()
    sqlite3.connect(f"file:{g / 'db.sqlite3'}?mode=ro", uri=True).close()
    assert fence.violations == []


def test_rmtree_elsewhere_is_not_mistaken_for_the_guarded_dir(fence_on, tmp_path, monkeypatch):
    """rmtree deletes names relative to a directory fd; a bare name equal to a
    guarded folder's name in the cwd must not count."""
    fence, g = fence_on
    monkeypatch.chdir(tmp_path)          # cwd contains a folder named "guarded"
    other = tmp_path / "elsewhere" / "guarded"
    other.mkdir(parents=True)
    (other / "f.txt").write_text("x")
    fence.violations.clear()
    shutil.rmtree(tmp_path / "elsewhere")
    assert fence.violations == []


def test_allowed_mode_flags_writes_outside(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fence = Fence(allowed=[home]).install()
    try:
        (home / "ok.txt").write_text("x")
        (tmp_path / "outside.txt").write_text("x")
    finally:
        fence.active = False
    assert [p for _, p in fence.violations] == [str((tmp_path / "outside.txt").resolve())]
