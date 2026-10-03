"""project.json: validation, parsing, path resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import project

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "lantern-quay"


def _write(home: Path, raw: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "project.json").write_text(json.dumps(raw), encoding="utf-8")


def _minimal(**over) -> dict:
    raw = {"schema": project.SCHEMA, "id": "demo", "title": "Demo", "log": "workspace/demo.jsonl"}
    raw.update(over)
    return raw


def test_example_manifest_parses_with_every_integration_off():
    m = project.load(EXAMPLE)
    assert m is not None and m.id == "lantern-quay"
    assert m.log == EXAMPLE.resolve() / "workspace" / "lantern_quay.jsonl"
    assert m.st_chat is None and m.st_characters_dir is None
    assert m.drive_remote is None and m.authored_spine_md is None
    assert m.lorebooks == [EXAMPLE.resolve() / "worlds" / "lantern_quay_lorebook.json"]


def test_home_without_manifest_returns_none(tmp_path):
    assert project.load(tmp_path) is None


def test_relative_paths_resolve_against_home_absolute_kept(tmp_path):
    _write(tmp_path, _minimal(integrations={
        "sillytavern": {"chat": "/abs/chat.jsonl", "characters_dir": "st/characters"},
        "drive": {"remote": "gdrive:Demo/sync/"},
        "lorebooks": [],
    }))
    m = project.load(tmp_path)
    assert m.st_chat == Path("/abs/chat.jsonl")
    assert m.st_characters_dir == tmp_path.resolve() / "st" / "characters"
    assert m.drive_remote == "gdrive:Demo/sync"
    assert m.lorebooks == []


@pytest.mark.parametrize("bad, fragment", [
    ({"schema": "story-editor/project@2"}, "schema"),
    ({"id": "Has Spaces"}, "id must be"),
    ({"log": ""}, "log must be"),
    ({"integrations": {"lorebooks": "worlds/x.json"}}, "lorebooks"),
    ({"integrations": {"drive": {"remote": 7}}}, "drive.remote"),
])
def test_invalid_manifests_raise_instead_of_falling_back(tmp_path, bad, fragment):
    _write(tmp_path, _minimal(**bad))
    with pytest.raises(project.ManifestError, match=fragment):
        project.load(tmp_path)


def test_broken_json_raises(tmp_path):
    (tmp_path / "project.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(project.ManifestError, match="not valid JSON"):
        project.load(tmp_path)
