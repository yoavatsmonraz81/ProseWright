"""Working-log ↔ SillyTavern chat sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import loader, st_sync


def _write_log(path: Path, texts: list[str]) -> None:
    lines = [json.dumps({"chat_metadata": {}})]
    for i, text in enumerate(texts):
        lines.append(json.dumps({
            "name": "Ilse",
            "mes": text,
            "is_user": False,
            "send_date": f"2026-09-04T00:00:{i:02d}",
            "extra": {"se_uid": f"m-{i:04d}"},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_import_refuses_a_shorter_chat(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    _write_log(path, ["opening", "chair", "wake"])
    chat = st_sync.export_st_chat(path)["messages"][1:]  # drop the prepend

    with pytest.raises(st_sync.WorkingLogAhead) as exc:
        st_sync.import_st_chat(chat, path)
    assert exc.value.working == 3
    assert exc.value.incoming == 2
    assert loader.load(path).get(0).text == "opening"


def test_import_force_allows_a_shorter_chat(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    _write_log(path, ["opening", "chair", "wake"])
    chat = st_sync.export_st_chat(path)["messages"][1:]

    n = st_sync.import_st_chat(chat, path, force=True)
    assert n == 2
    assert loader.load(path).get(0).text == "chair"


def test_chat_status_detects_a_prepend(tmp_path: Path) -> None:
    working = tmp_path / "working.jsonl"
    st = tmp_path / "st.jsonl"
    _write_log(working, ["opening", "chair"])
    _write_log(st, ["chair"])

    info = st_sync.chat_status(working, st)
    assert info["kind"] == "working_ahead"
    assert info["working_count"] == 2
    assert info["st_count"] == 1
    assert info["in_sync"] is False


def test_chat_status_detects_later_text_divergence(tmp_path: Path) -> None:
    working = tmp_path / "working.jsonl"
    st = tmp_path / "st.jsonl"
    _write_log(working, ["same opening", "working edit"])
    _write_log(st, ["same opening", "old text"])

    info = st_sync.chat_status(working, st)
    assert info["kind"] == "diverged"
    assert info["changed_text_count"] == 1
    assert info["changed_msg_ids"] == [1]


def test_chat_status_ignores_story_editor_owned_metadata(tmp_path: Path) -> None:
    working = tmp_path / "working.jsonl"
    st = tmp_path / "st.jsonl"
    _write_log(working, ["same"])
    _write_log(st, ["same"])
    lines = working.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["extra"]["story_editor"] = {"chronicle": {"date": "1843-11-11"}}
    lines[1] = json.dumps(row)
    working.write_text("\n".join(lines) + "\n", encoding="utf-8")

    info = st_sync.chat_status(working, st)
    assert info["kind"] == "in_sync"
    assert info["changed_message_count"] == 0
