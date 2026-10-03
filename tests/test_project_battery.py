"""A battery of real operations against a project, under a write fence.

Runs in a fresh interpreter with STORY_EDITOR_HOME on a temp copy of the
example project and a fence that allows writes only inside that home and the
interpreter's temp dir. Any write anywhere else fails the test, naming the
operation and path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lantern-quay"

_BATTERY = r"""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.environ["FENCE_DIR"])
from write_fence import Fence
home = Path(os.environ["STORY_EDITOR_HOME"]).resolve()
fence = Fence(allowed=[home, Path(tempfile.gettempdir()).resolve()]).install()
done = []

def step(name, fn):
    fence.context = name
    fn()
    done.append(name)

from story_editor import (config, loader, structure, backup, history, dossier,
                          st_sync, drive_sync)
from story_editor.author import seed as seed_mod

log_path = config.working_log()
step("load log", lambda: loader.load(log_path))
step("segment scenes", lambda: structure.segment_scenes(loader.load(log_path)))
step("backup working log", lambda: backup.backup(log_path, label="battery"))
step("restore working log", lambda: backup.restore(log_path))
step("mini spine round trip", lambda: seed_mod.save_progress(
    {"completed": ["Q1"], "attempts": {}}))
step("dossier write", lambda: dossier.save(dossier.Dossier(character="Wren")))
step("st chat status (none configured)", lambda: st_sync.chat_status())
def st_push():
    try:
        st_sync.project_chat()
    except st_sync.NoChatConfigured:
        return
    raise AssertionError("example project must have no SillyTavern chat")
step("st push refused", st_push)
step("drive status (no folder)", lambda: drive_sync.status())
def drive_push():
    try:
        drive_sync.push(force=True)
    except drive_sync.DriveError:
        return
    raise AssertionError("example project must not push to any Drive folder")
step("drive push refused", drive_push)
fence.active = False
print(json.dumps({"done": done, "violations": sorted(set(fence.violations)),
                  "by": {f"{e} {p}": sorted(c) for (e, p), c in fence.by_context.items()},
                  "writes": len(fence.writes)}))
"""


def test_battery_writes_only_inside_the_project(tmp_path):
    home = tmp_path / "lantern-quay"
    shutil.copytree(EXAMPLE, home)
    env = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in ("STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    env.update({
        "STORY_EDITOR_HOME": str(home),
        # Even with the legacy Drive variable set, the example project must not reach it.
        "STORY_EDITOR_DRIVE_REMOTE": "gdrive:another-project/sync",
        "FENCE_DIR": str(ROOT / "tests"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(tmp_path / "tmp"),
    })
    (tmp_path / "tmp").mkdir()
    proc = subprocess.run([sys.executable, "-c", _BATTERY], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["violations"] == [], json.dumps(out["by"], indent=1)
    assert out["done"] == [
        "load log", "segment scenes", "backup working log", "restore working log",
        "mini spine round trip", "dossier write", "st chat status (none configured)",
        "st push refused", "drive status (no folder)", "drive push refused",
    ]
    assert out["writes"] > 0  # the fence saw the battery's own writes
