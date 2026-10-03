"""Hand-edit copy: propose bodies back as an EditSet. No model, no write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, layers, loader, transform


HEADER = "[ 🕰️ 07:20 | 📅 Day 2 | 📍 The vault ]\n\n"


def _write_log(path: Path, messages: list[tuple[str, str, str]]) -> loader.Log:
    lines = [json.dumps({"chat_metadata": {}})]
    for uid, speaker, text in messages:
        lines.append(json.dumps({
            "name": speaker,
            "mes": text,
            "is_user": speaker == "Wren",
            "extra": {"se_uid": uid},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return loader.load(path)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending.md")
    return tmp_path


def test_patch_runs_on_the_log() -> None:
    assert layers.check_runs("patch", layers.LOG).name == "patch"


def test_propose_keeps_the_header_and_skips_unchanged(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", HEADER + "The air was thick with heat."),
        ("m-b", "Wren", "I looked at her."),
    ])
    edit_set = transform.propose_hand_edits(log_path, [
        {"msg_id": 0, "body": "The air was heavy with heat."},
        {"msg_id": 1, "body": "I looked at her."},
    ])
    assert edit_set.operator == "patch"
    assert len(edit_set.edits) == 1
    assert edit_set.edits[0].msg_id == 0
    assert edit_set.edits[0].after.startswith("[")
    assert "heavy" in edit_set.edits[0].after
    assert "thick" in edit_set.edits[0].before
    assert loader.load(log_path).get(0).text.endswith("thick with heat.")


def test_propose_refuses_when_nothing_changed(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [("m-a", "Ilse", "She watched.")])
    with pytest.raises(ValueError, match="nothing changed"):
        transform.propose_hand_edits(log_path, [{"msg_id": 0, "body": "She watched."}])
