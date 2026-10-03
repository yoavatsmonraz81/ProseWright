"""Remove: propose a hard delete, accept, later ids shift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, layers, loader, transform


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
    monkeypatch.setattr(config, "LAST_SWEEP_CONTEXT", tmp_path / "sweep.json")
    monkeypatch.setattr(config, "VOICE_ATTRIBUTION", tmp_path / "attr.json")
    monkeypatch.setattr(config, "EDIT_HISTORY", tmp_path / "history.jsonl")
    return tmp_path


def test_remove_runs_on_the_log() -> None:
    assert layers.check_runs("remove", layers.LOG).name == "remove"


def test_propose_writes_nothing(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", "She watched the door."),
        ("m-b", "Wren", "I looked at her."),
        ("m-c", "Ilse", "The lock held."),
    ])
    edit_set = transform.propose_remove(log_path, 1)
    assert edit_set.operator == "remove"
    assert edit_set.meta["sweep"] is False
    assert len(edit_set.edits) == 1
    assert edit_set.edits[0].kind == "remove"
    assert edit_set.edits[0].msg_id == 1
    assert edit_set.edits[0].after == ""
    assert "looked" in edit_set.edits[0].before
    assert len(loader.load(log_path)) == 3


def test_commit_deletes_and_shifts(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", "one"),
        ("m-b", "Wren", "two"),
        ("m-c", "Ilse", "three"),
    ])
    transform.propose_remove(log_path, 1)
    backup, applied = transform.commit_edits(log_path)
    assert applied == 1
    assert backup.exists()
    log = loader.load(log_path)
    assert [m.text for m in log.messages] == ["one", "three"]
    assert log.get(1).uid == "m-c"
    assert transform.load_pending_edits() is None






def test_sweep_context_starts_at_the_seam(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", "before"),
        ("m-b", "Wren", "gone"),
        ("m-c", "Ilse", "downstream"),
    ])
    transform.propose_remove(log_path, 1, sweep=True)
    transform.commit_edits(log_path)
    from story_editor import propagate
    ctx = propagate.load_sweep_context()
    assert ctx is not None
    assert ctx.operator == "remove"
    assert ctx.changed_to + 1 == 1
    assert ctx.edits[0]["kind"] == "remove"
    assert "gone" in ctx.edits[0]["before"]


def test_refuse_emptying_the_log(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", "only"),
        ("m-b", "Wren", "two"),
    ])
    with pytest.raises(ValueError, match="every turn"):
        transform.propose_remove(log_path, 0, 1)


def test_remove_diff_is_all_deletion() -> None:
    edit = transform.Edit(
        msg_id=4, speaker="Ilse",
        before="She said nothing.", after="",
        kind="remove",
    )
    diff = transform.edit_diff(edit)
    assert diff["kind"] == "remove"
    assert diff["after"] == ""
    assert all(h["kind"] == "del" for h in diff["hunks"])
