"""Chapters for export: the locked episode map when there is one, otherwise
one chapter per scene, so a new story can be exported straight away."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CHAPTERS = """
import json
from story_editor import config, loader, manuscript as ms, novelize, publication
log = loader.load(config.working_log())
print(json.dumps({"chapters": publication.chapters(),
                  "text": novelize.assemble_private_text(ms.load(log=log), log)}))
"""


def _run(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in (
        "STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    env["STORY_EDITOR_HOME"] = str(home)
    proc = subprocess.run([sys.executable, "-c", _CHAPTERS], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_without_a_chapter_map_each_scene_is_a_chapter(tmp_path):
    home = tmp_path / "lantern-quay"
    shutil.copytree(ROOT / "examples" / "lantern-quay", home)
    r = _run(home)
    assert [(c["id"], c["title"], c["start"], c["end"], c["has_text"]) for c in r["chapters"]] == [
        (0, "Chapter 1: Lantern Quay", 0, 4, False),
        (1, "Chapter 2: The Harbour Office", 5, 9, False),
        (2, "Chapter 3: The Lighthouse Gallery", 10, 14, False),
    ]


def test_a_locked_episode_map_still_decides_the_chapters(tmp_path):
    home = tmp_path / "lantern-quay"
    shutil.copytree(ROOT / "examples" / "lantern-quay", home)
    (home / "workspace" / "director_chapter_map.json").write_text(json.dumps({
        "status": "canon_locked", "unit_type": "episode",
        "episodes": [{"episode_id": 1, "title": "The Late Ship", "msg_range": [0, 14]}],
    }))
    r = _run(home)
    assert [(c["id"], c["title"]) for c in r["chapters"]] == [(1, "The Late Ship")]
