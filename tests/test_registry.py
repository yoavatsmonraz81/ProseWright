"""The project registry, and how config picks the home from it.

Config resolves the home at import, exactly as a server does at start, so the
resolution tests probe it in fresh interpreters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from story_editor import registry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lantern-quay"


@pytest.fixture()
def reg_path(tmp_path) -> Path:
    return tmp_path / "config" / "projects.json"


def _project(tmp_path: Path, name: str, pid: str | None = None) -> Path:
    home = tmp_path / name
    shutil.copytree(EXAMPLE, home)
    if pid:
        raw = json.loads((home / "project.json").read_text())
        raw["id"], raw["title"] = pid, pid.replace("-", " ").title()
        (home / "project.json").write_text(json.dumps(raw))
    return home


def test_empty_registry_until_something_is_added(reg_path):
    reg = registry.load(reg_path)
    assert reg.projects == [] and reg.active is None
    assert not reg_path.exists()


def test_add_switch_remove_round_trip(tmp_path, reg_path):
    a = _project(tmp_path, "a")
    b = _project(tmp_path, "b", "second-story")
    assert registry.add(a, registry_path=reg_path).id == "lantern-quay"
    assert registry.add(b, registry_path=reg_path).id == "second-story"
    registry.switch("second-story", registry_path=reg_path)
    reg = registry.load(reg_path)
    assert reg.active == "second-story"
    assert [e.id for e in reg.projects] == ["lantern-quay", "second-story"]
    assert registry.active_home(reg_path) == (b.resolve(), "")

    registry.remove("second-story", registry_path=reg_path)
    reg = registry.load(reg_path)
    assert reg.active is None and [e.id for e in reg.projects] == ["lantern-quay"]
    assert b.is_dir() and (b / "project.json").exists()  # forgetting never deletes


def test_adding_the_same_folder_twice_is_idempotent(tmp_path, reg_path):
    a = _project(tmp_path, "a")
    registry.add(a, registry_path=reg_path)
    registry.add(a, registry_path=reg_path)
    assert len(registry.load(reg_path).projects) == 1


def test_two_homes_cannot_share_an_id(tmp_path, reg_path):
    registry.add(_project(tmp_path, "a"), registry_path=reg_path)
    with pytest.raises(registry.RegistryError, match="already registered"):
        registry.add(_project(tmp_path, "copy-of-a"), registry_path=reg_path)


def test_a_bare_folder_registers_under_its_name(tmp_path, reg_path):
    bare = tmp_path / "My Draft"
    (bare / "workspace").mkdir(parents=True)
    assert registry.add(bare, registry_path=reg_path).id == "my-draft"


def test_switch_refuses_unknown_missing_and_broken_projects(tmp_path, reg_path):
    a = _project(tmp_path, "a")
    registry.add(a, registry_path=reg_path)
    with pytest.raises(registry.RegistryError, match="no project"):
        registry.switch("nope", registry_path=reg_path)
    (a / "project.json").write_text('{"schema": "nope"}')
    with pytest.raises(ValueError):
        registry.switch("lantern-quay", registry_path=reg_path)
    assert registry.load(reg_path).active is None
    shutil.rmtree(a)
    with pytest.raises(registry.RegistryError, match="no longer exists"):
        registry.switch("lantern-quay", registry_path=reg_path)


def test_a_broken_registry_is_never_silently_replaced(tmp_path, reg_path):
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text("{ not json")
    with pytest.raises(registry.RegistryError):
        registry.add(_project(tmp_path, "a"), registry_path=reg_path)
    assert reg_path.read_text() == "{ not json"
    home, warning = registry.active_home(reg_path)
    assert home is None and "ignored" in warning


def test_writes_are_atomic(tmp_path, reg_path, monkeypatch):
    registry.add(_project(tmp_path, "a"), registry_path=reg_path)
    before = reg_path.read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(registry.json, "dump", boom)
    with pytest.raises(OSError):
        registry.add(_project(tmp_path, "b", "second-story"), registry_path=reg_path)
    assert reg_path.read_text() == before
    assert [p.name for p in reg_path.parent.iterdir()] == ["projects.json"]


# --------------------------------------------------------------------------- #
# Home resolution, in fresh interpreters
# --------------------------------------------------------------------------- #

_PROBE = ("import json; from story_editor import config as c; "
          "print(json.dumps({'home': str(c.HOME_DIR), 'source': c.HOME_SOURCE, "
          "'id': c.PROJECT_ID, 'warning': c.REGISTRY_WARNING}))")


def _probe(reg_path: Path, **env) -> dict:
    clean = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in ("STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    clean["STORY_EDITOR_REGISTRY"] = str(reg_path)
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean.update(env)
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=clean,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_no_registry_opens_the_example(reg_path):
    v = _probe(reg_path)
    assert v["source"] == "example" and v["home"] == str(EXAMPLE)


def test_the_active_project_is_opened(tmp_path, reg_path):
    b = _project(tmp_path, "b", "second-story")
    registry.add(b, registry_path=reg_path)
    registry.switch("second-story", registry_path=reg_path)
    v = _probe(reg_path)
    assert (v["source"], v["home"], v["id"]) == ("registry", str(b.resolve()), "second-story")


def test_story_editor_home_wins_over_the_registry(tmp_path, reg_path):
    b = _project(tmp_path, "b", "second-story")
    registry.add(b, registry_path=reg_path)
    registry.switch("second-story", registry_path=reg_path)
    a = _project(tmp_path, "a")
    v = _probe(reg_path, STORY_EDITOR_HOME=str(a))
    assert (v["source"], v["id"]) == ("env", "lantern-quay")


def test_a_lost_active_folder_falls_back_with_a_warning(tmp_path, reg_path):
    b = _project(tmp_path, "b", "second-story")
    registry.add(b, registry_path=reg_path)
    registry.switch("second-story", registry_path=reg_path)
    shutil.rmtree(b)
    v = _probe(reg_path)
    assert v["source"] == "example"
    assert "no longer exists" in v["warning"]
