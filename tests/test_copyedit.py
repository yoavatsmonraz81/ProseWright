"""Copyedit: unique body finds only. No model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, copyedit, layers, loader
from story_editor.transform import resolve_span


HEADER = "[ 🕰️ 8:40 PM | 🗓️ Saturday, November 11, 1843 | 📍 The vault ]\n\n"


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
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending_edits.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending_edits.md")
    return tmp_path


def test_unique_find_lands_in_the_body_not_the_header(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        ("m-0", "Ilse", HEADER + "She left there gloves on the table."),
    ])
    span = resolve_span(log, msg_from=0, msg_to=0)
    fixes = [copyedit.Fix(0, "there gloves", "their gloves", "their/there")]
    edits, skipped = copyedit.apply_fixes(span.messages, fixes)
    assert skipped == []
    assert len(edits) == 1
    assert "there gloves" not in edits[0].after
    assert "their gloves" in edits[0].after
    assert edits[0].after.startswith("[ 🕰️ 8:40 PM")


def test_clock_in_the_header_is_not_editable(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        ("m-0", "Ilse", HEADER + "The vault was cold."),
    ])
    edits, skipped = copyedit.apply_fixes(log.messages, [
        copyedit.Fix(0, "8:40 PM", "9:00 PM", "header clock"),
    ])
    assert edits == []
    assert skipped[0].reason == "not found"


def test_ambiguous_find_is_skipped() -> None:
    body = "She said there, and there again."
    new, reason = copyedit.apply_one(body, "there", "their")
    assert new is None
    assert reason == "ambiguous"


def test_missing_and_identical_finds_are_skipped() -> None:
    assert copyedit.apply_one("hello", "world", "earth") == (None, "not found")
    assert copyedit.apply_one("hello", "hello", "hello") == (None, "empty or identical")
    assert copyedit.apply_one("hello", "", "x") == (None, "empty or identical")


def test_chunking_splits_on_budget() -> None:
    msgs = [
        loader.Message(msg_id=i, speaker="Ilse", is_user=False, is_system=False,
                       text="x" * 40, uid=f"m-{i}")
        for i in range(4)
    ]
    chunks = copyedit.chunk_messages(msgs, budget=90)
    assert len(chunks) == 2
    assert [m.msg_id for m in chunks[0]] == [0, 1]
    assert [m.msg_id for m in chunks[1]] == [2, 3]


def test_parse_fixes_drops_foreign_ids() -> None:
    obj = {"fixes": [
        {"msg_id": 1, "find": "teh", "replace": "the", "why": "typo"},
        {"msg_id": 99, "find": "teh", "replace": "the"},
        {"msg_id": "nope", "find": "a", "replace": "b"},
        {"find": "a", "replace": "b"},
    ]}
    fixes = copyedit.parse_fixes(obj, {1})
    assert [f.msg_id for f in fixes] == [1]


def test_apply_span_writes_pending_without_calling_a_model(home: Path) -> None:
    log_path = home / "log.jsonl"
    log = _write_log(log_path, [
        ("m-0", "Ilse", HEADER + "She left there gloves."),
        ("m-1", "Wren", "Wren said nothing."),
    ])
    span = resolve_span(log, msg_from=0, msg_to=1)

    def fake_ask(messages, **_kw):
        return [copyedit.Fix(0, "there gloves", "their gloves", "their/there")]

    edit_set = copyedit.apply_span(log_path, span, ask=fake_ask)
    assert edit_set.operator == "copyedit"
    assert layers.check_runs("copyedit", layers.LOG).name == "copyedit"
    assert len(edit_set.edits) == 1
    assert "their gloves" in edit_set.edits[0].after
    pending = json.loads((home / "pending_edits.json").read_text(encoding="utf-8"))
    assert pending["operator"] == "copyedit"
