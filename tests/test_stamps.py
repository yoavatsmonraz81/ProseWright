"""Stamp carry-forward and header proposals. No model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, loader, stamps
from story_editor.transform import resolve_span


FULL = (
    "[ 🕰️ 8:40 PM | 🗓️ Saturday, November 11, 1843 | 📍 The abandoned quarters ]\n\n"
    "The rooms had not been opened for years."
)


def _write_log(path: Path, rows: list[dict]) -> loader.Log:
    lines = [json.dumps({"chat_metadata": {}})]
    for i, row in enumerate(rows):
        extra = dict(row.get("extra") or {})
        extra.setdefault("se_uid", f"m-{i:04d}")
        lines.append(json.dumps({
            "name": row["name"],
            "mes": row["mes"],
            "is_user": row.get("is_user", row["name"] == "Wren"),
            "is_system": row.get("is_system", False),
            "extra": extra,
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return loader.load(path)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "STAMPS", tmp_path / "stamps.json")
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending_edits.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending_edits.md")
    return tmp_path


def test_weekday_for_opening_night() -> None:
    assert stamps.weekday_name((1843, 11, 11)) == "Saturday"
    assert stamps.format_date((1843, 11, 11), "November 11, 1843") == (
        "Saturday, November 11, 1843"
    )


def test_carry_forward_fills_headerless_turns(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {"name": "Wren", "mes": "She closed the door."},
        {"name": "Ilse", "mes": "Ilse did not look up.", "is_user": False},
    ])
    inferred = stamps.infer_log(log)
    assert inferred[0].source == stamps.HEADER
    assert inferred[0].complete
    assert inferred[1].source == stamps.INHERITED
    assert inferred[1].location == "The abandoned quarters"
    assert inferred[1].time == "8:40 PM"
    assert inferred[1].date == (1843, 11, 11)
    assert inferred[1].will_propose is False  # Wren, no existing header
    assert inferred[2].will_propose is True   # CHAR, missing header
    assert inferred[2].location == "The abandoned quarters"


def test_body_location_is_inferred_not_inherited(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {
            "name": "Ilse",
            "mes": "They had moved on. 📍 The west stair\nShe kept walking.",
            "is_user": False,
        },
    ])
    inferred = stamps.infer_log(log)
    assert inferred[1].location == "The west stair"
    assert inferred[1].sources["location"] == stamps.INFERRED
    assert inferred[1].sources["date"] == stamps.INHERITED


def test_dialogue_date_does_not_replace_story_clock(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {"name": "Wren", "mes": '"On June 21, 1844, I named you a goddess."'},
        {"name": "Ilse", "mes": "Ilse listened.", "is_user": False},
    ])
    inferred = stamps.infer_log(log)
    assert [row.date for row in inferred] == [(1843, 11, 11)] * 3


def test_chronicle_dates_headerless_opening_and_director_memory(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {"name": "Ilse", "mes": "A new morning began.", "is_user": False,
         "extra": {"story_editor": {"chronicle": {"date": "1843-11-14", "location_mode": "physical"}}}},
        {"name": "Ilse", "mes": "[ 🕰️ 9:00 AM | 🗓️ Saturday, November 11, 1843 | 📍 Study ]\n\nA memory.",
         "is_user": False, "extra": {"story_editor": {"chronicle": {
             "date": "1831-11-11", "location_mode": "memory", "confidence": "director_confirmed",
         }}}},
    ])
    inferred = stamps.infer_log(log)
    assert inferred[1].date == (1843, 11, 14)
    assert inferred[2].date == (1831, 11, 11)


def test_user_turns_do_not_get_a_header_unless_already_partial(home: Path) -> None:
    log = _write_log(home / "log.jsonl", [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {"name": "Wren", "mes": "No header here."},
        {
            "name": "Wren",
            "mes": "[ 🕰️ 9:00 PM ]\n\nA partial one.",
        },
    ])
    inferred = stamps.infer_log(log)
    assert inferred[1].will_propose is False
    assert inferred[2].will_propose is True
    assert inferred[2].has_lead


def test_propose_prepends_only_on_char_turns(home: Path) -> None:
    path = home / "log.jsonl"
    log = _write_log(path, [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {"name": "Wren", "mes": "She closed the door."},
        {"name": "Ilse", "mes": "Ilse did not look up.", "is_user": False},
    ])
    span = resolve_span(log)
    edit_set = stamps.propose_span(path, span)
    assert edit_set.operator == "stamps"
    assert [e.msg_id for e in edit_set.edits] == [2]
    after = edit_set.edits[0].after
    assert after.startswith("[ 🕰️ 8:40 PM | 🗓️ Saturday, November 11, 1843 | 📍 The abandoned quarters ]")
    assert "Ilse did not look up." in after
    sidecar = json.loads((home / "stamps.json").read_text(encoding="utf-8"))
    assert sidecar["count"] == 3
    assert sidecar["stamps"]["m-0002"]["will_propose"] is True


def test_existing_full_header_is_not_proposed_again(home: Path) -> None:
    path = home / "log.jsonl"
    log = _write_log(path, [
        {"name": "Ilse", "mes": FULL, "is_user": False},
    ])
    edit_set = stamps.propose_span(path, resolve_span(log))
    assert edit_set.edits == []


def test_partial_header_is_replaced_not_doubled(home: Path) -> None:
    path = home / "log.jsonl"
    log = _write_log(path, [
        {"name": "The Chorus", "mes": FULL, "is_user": False},
        {
            "name": "Ilse",
            "mes": "[ 🕰️ 9:15 PM ]\n\nShe stood.",
            "is_user": False,
        },
    ])
    edit_set = stamps.propose_span(path, resolve_span(log))
    assert len(edit_set.edits) == 1
    after = edit_set.edits[0].after
    assert after.count("[ ") == 1
    assert "9:15 PM" in after
    assert "The abandoned quarters" in after
    assert after.endswith("She stood.") or "She stood." in after
