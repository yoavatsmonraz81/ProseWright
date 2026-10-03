"""Projects stay inside their own home.

``config`` resolves everything at import, exactly as a server does at start, so
each project is probed in a fresh subprocess — the same way a real project
switch restarts the server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from story_editor import loader, structure

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lantern-quay"

# Constants that legitimately sit outside any project home: the code itself,
# the built UI, and where the bundled example lives (the default home).
CODE_CONSTANTS = {"PROJECT_ROOT", "APP_DIR", "EXAMPLE_HOME"}

_PROBE = r"""
import json, sys
from pathlib import Path
from story_editor import config as c, drive_sync
out = {}
for k in sorted(dir(c)):
    if not k.isupper() or k == "MODEL_API_KEY":
        continue
    v = getattr(c, k)
    if isinstance(v, Path):
        out[k] = str(v)
    elif isinstance(v, (str, bool)) or v is None:
        out[k] = v
    elif isinstance(v, list) and all(isinstance(x, Path) for x in v):
        out[k] = [str(x) for x in v]
out["__working_log()"] = str(c.working_log())
out["__drive_remote()"] = drive_sync.remote()
print(json.dumps(out))
"""


def _probe(home: Path | None, **env) -> dict:
    clean = {k: v for k, v in os.environ.items()
             if not (k.startswith("STORY_EDITOR_") and k not in ("STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    if home is not None:
        clean["STORY_EDITOR_HOME"] = str(home)
    clean.update(env)
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=clean,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture()
def example_home(tmp_path) -> Path:
    home = tmp_path / "lantern-quay"
    shutil.copytree(EXAMPLE, home)
    return home


def test_default_home_is_the_bundled_example():
    """With no STORY_EDITOR_HOME, the server opens the example project."""
    v = _probe(None)
    assert v["HOME_DIR"] == str(EXAMPLE)
    assert v["PROJECT_ID"] == "lantern-quay"
    assert v["__working_log()"] == str(EXAMPLE / "workspace" / "lantern_quay.jsonl")


def test_example_project_reaches_nothing_outside_its_home(example_home):
    v = _probe(example_home, STORY_EDITOR_DRIVE_REMOTE="gdrive:another-project/sync")
    assert v["PROJECT_ID"] == "lantern-quay"
    assert v["__working_log()"] == str(example_home / "workspace" / "lantern_quay.jsonl")
    # Integrations the manifest leaves off stay off — even with a remote in the environment.
    assert v["SOURCE_LOG"] is None
    assert v["ST_CHARACTERS_DIR"] is None
    assert v["SPINE_MD"] is None and v["AUTHORED_SPINE_MD"] is None
    assert v["__drive_remote()"] is None
    escaped = []
    for k, val in v.items():
        if k in CODE_CONSTANTS:
            continue
        for item in (val if isinstance(val, list) else [val]):
            if isinstance(item, str) and item.startswith("/"):
                if not Path(item).resolve().is_relative_to(example_home.resolve()):
                    escaped.append((k, item))
    assert not escaped, f"paths outside the project home: {escaped}"


def test_home_without_manifest_gets_no_integrations(tmp_path):
    home = tmp_path / "bare"
    (home / "workspace").mkdir(parents=True)
    v = _probe(home)
    assert v["PROJECT_ID"] == "bare"
    assert v["SOURCE_LOG"] is None and v["ST_CHARACTERS_DIR"] is None
    assert v["SPINE_MD"] is None and v["__drive_remote()"] is None


def test_invalid_manifest_refuses_to_start(example_home):
    (example_home / "project.json").write_text('{"schema": "nope"}', encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in ("STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    clean["STORY_EDITOR_HOME"] = str(example_home)
    proc = subprocess.run([sys.executable, "-c", "import story_editor.config"], cwd=ROOT,
                          env=clean, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0 and "ManifestError" in proc.stderr


def test_example_story_segments_into_its_three_scenes():
    log = loader.load(EXAMPLE / "workspace" / "lantern_quay.jsonl")
    scenes = [s for s in structure.segment_scenes(log) if s.kind == "scene"]
    assert [s.location for s in scenes] == ["Lantern Quay", "The Harbour Office", "The Lighthouse Gallery"]
    assert [s.msg_count for s in scenes] == [5, 5, 5]
