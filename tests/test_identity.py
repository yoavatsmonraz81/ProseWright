"""Tests for stable message identity and the sidecars anchored to it.

The scenario that motivated all of this: committing an interlude mid-log shifts
every ordinal after the insertion point. Anything keyed by ordinal silently
points one message to the left afterwards, with no error raised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import attribution, identity, loader, st_sync
from story_editor import structure


def _write_log(path: Path, count: int = 8) -> Path:
    lines = [json.dumps({"chat_metadata": {}})]
    for i in range(count):
        lines.append(json.dumps({
            "name": "Wren" if i % 2 else "Ilse",
            "mes": f"message number {i}",
            "is_user": bool(i % 2),
            "send_date": f"2026-07-25T10:{i:02d}:00",
            "extra": {},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _insert(path: Path, at_ordinal: int, text: str = "an interlude") -> None:
    """Mimic `interlude commit`: splice a new line into the middle of the log."""
    lines = [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]
    message = {"name": "The Chorus", "mes": text, "is_user": False, "extra": {}}
    identity.write_uid(message, identity.mint())
    lines.insert(at_ordinal + 1, json.dumps(message))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    path = _write_log(tmp_path / "log.jsonl")
    identity.backfill(path)
    return path


def test_backfill_is_idempotent(log_path: Path):
    log = loader.load(log_path)
    uids_before = [m.uid for m in log.messages]
    assert all(uids_before)
    assert len(set(uids_before)) == len(uids_before)

    report = identity.backfill(log_path)
    assert report.minted == 0
    assert [m.uid for m in loader.load(log_path).messages] == uids_before


def test_uids_survive_insertion(log_path: Path):
    before = loader.load(log_path)
    target_uid = before.get(5).uid
    assert before.ordinal_of(target_uid) == 5

    _insert(log_path, 2)

    after = loader.load(log_path)
    assert len(after) == len(before) + 1
    assert after.ordinal_of(target_uid) == 6, "ordinal shifts, identity does not"
    assert after.by_uid(target_uid).text == before.get(5).text


def test_attribution_labels_follow_the_prose(log_path: Path, tmp_path: Path):
    log = loader.load(log_path)
    store_path = tmp_path / "attr.json"
    store = attribution.empty_store()
    attribution.set_label(store, 5, attribution.VoiceLabel(card="Ilse", voice="ilse"))
    attribution.save_store(store, store_path, log=log)

    on_disk = json.loads(store_path.read_text(encoding="utf-8"))
    assert list(on_disk["labels"]) == [log.get(5).uid], "file must be uid-keyed"

    _insert(log_path, 0)

    reloaded = attribution.load_store(store_path, log=loader.load(log_path))
    assert attribution.get_label(reloaded, 5) is None
    assert attribution.get_label(reloaded, 6).voice == "ilse"


def test_beat_anchor_follows_the_prose(log_path: Path):
    log = loader.load(log_path)
    beat = structure.Beat(beat_id=0, title="t", justification="j",
                          start_msg_id=3, end_msg_id=6)
    beat.bind(log)
    revived = structure.Beat.from_json(json.loads(json.dumps(beat.to_json())))

    _insert(log_path, 1)
    revived.bind(loader.load(log_path))
    assert (revived.start_msg_id, revived.end_msg_id) == (4, 7)


def test_st_export_strips_uids(log_path: Path, tmp_path: Path):
    exported = st_sync.export_st_chat(log_path)
    assert all("se_uid" not in (m.get("extra") or {}) for m in exported["messages"])
    # the working copy keeps them
    assert all(m.uid for m in loader.load(log_path).messages)

    dest = tmp_path / "st_chat.jsonl"
    st_sync.write_st_chat(log_path, dest)
    assert "se_uid" not in dest.read_text(encoding="utf-8")


def test_st_import_preserves_identity(log_path: Path):
    """A push from ST arrives anonymous; identity must be recovered, not reminted."""
    log = loader.load(log_path)
    uids_before = [m.uid for m in log.messages]
    chat = st_sync.export_st_chat(log_path)["messages"]

    st_sync.import_st_chat(chat, log_path)

    assert [m.uid for m in loader.load(log_path).messages] == uids_before


def test_st_import_keeps_identity_through_an_edit(log_path: Path):
    log = loader.load(log_path)
    target_uid = log.get(3).uid
    chat = st_sync.export_st_chat(log_path)["messages"]
    chat[3]["mes"] = "this message was edited inside SillyTavern"

    st_sync.import_st_chat(chat, log_path)

    reloaded = loader.load(log_path)
    assert reloaded.get(3).uid == target_uid, "send_date should re-identify an edit"
    assert reloaded.get(3).text.endswith("inside SillyTavern")


def test_st_import_mints_for_genuinely_new_messages(log_path: Path):
    chat = st_sync.export_st_chat(log_path)["messages"]
    chat.append({"name": "Wren", "mes": "a brand new turn",
                 "is_user": True, "send_date": "2026-07-25T11:00:00", "extra": {}})

    st_sync.import_st_chat(chat, log_path)

    reloaded = loader.load(log_path)
    assert len(reloaded) == 9
    assert reloaded.get(8).uid
    assert len({m.uid for m in reloaded.messages}) == 9
