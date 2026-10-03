"""The review payload: what a proposal looks like to the pane that answers it.

Two properties carry the weight. The hunks must reconstruct *both* sides exactly,
or the diff on screen is a paraphrase of the change rather than the change. And
the `[ time | date | location ]` header must sit outside the diff, because the
operators re-prepend it byte for byte and a reviewer needs to see that it was not
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, layers, transform


HEADER = "[ 🕰️ 07:20 | 📅 Day 2 | 📍 The vault ]\n\n"


def _edit(before: str, after: str, **kw) -> transform.Edit:
    return transform.Edit(msg_id=kw.pop("msg_id", 4), speaker=kw.pop("speaker", "Ilse"),
                          before=before, after=after, **kw)


def _rebuild(hunks: list[dict], side: str) -> str:
    drop = "ins" if side == "before" else "del"
    return "".join(h["text"] for h in hunks if h["kind"] != drop)


# --------------------------------------------------------------------------- #
# Word-level hunks
# --------------------------------------------------------------------------- #


def test_hunks_reconstruct_both_sides_exactly() -> None:
    before = "She said nothing at all, and the lamp guttered."
    after = "She said nothing, not one word, and the lamp guttered."
    hunks = transform.word_hunks(before, after)
    assert _rebuild(hunks, "before") == before
    assert _rebuild(hunks, "after") == after


def test_unchanged_prose_is_one_untouched_hunk() -> None:
    same = "Snow gathered on the sill."
    assert transform.word_hunks(same, same) == [{"kind": "same", "text": same}]


def test_consecutive_changes_are_merged_not_shredded() -> None:
    hunks = transform.word_hunks("a b c d", "a x y d")
    kinds = [h["kind"] for h in hunks]
    assert kinds == ["same", "del", "ins", "same"]
    assert hunks[1]["text"] == "b c "
    assert hunks[2]["text"] == "x y "


def test_paragraph_breaks_survive_the_diff() -> None:
    before = "First line.\n\nSecond line."
    after = "First line.\n\nSecond line, changed."
    hunks = transform.word_hunks(before, after)
    assert _rebuild(hunks, "before") == before
    assert _rebuild(hunks, "after") == after
    assert "\n\n" in "".join(h["text"] for h in hunks if h["kind"] == "same")


# --------------------------------------------------------------------------- #
# One edit
# --------------------------------------------------------------------------- #


def test_the_header_is_held_out_of_the_diff_and_marked_kept() -> None:
    diff = transform.edit_diff(_edit(
        HEADER + "She said nothing at all.",
        HEADER + "She said nothing, not one word.",
    ))
    assert diff["header"] == HEADER
    assert diff["header_preserved"] is True
    assert diff["before"] == "She said nothing at all."
    # The header appears nowhere in the diffed bodies, so it cannot read as a change.
    assert "🕰️" not in "".join(h["text"] for h in diff["hunks"])


def test_a_header_that_changed_is_reported_rather_than_absorbed() -> None:
    diff = transform.edit_diff(_edit(
        HEADER + "She said nothing at all.",
        "[ 🕰️ 09:00 | 📅 Day 2 | 📍 The vault ]\n\nShe said nothing at all.",
    ))
    assert diff["header_preserved"] is False


def test_a_turn_without_a_header_diffs_the_whole_text() -> None:
    diff = transform.edit_diff(_edit("Wren shrugged.", "Wren shrugged, once."))
    assert diff["header"] == ""
    assert diff["header_preserved"] is True
    assert diff["before"] == "Wren shrugged."


def test_an_injected_passage_is_all_insertion() -> None:
    diff = transform.edit_diff(_edit(
        "", "The chorus met below stairs.", kind="inject", speaker="The Chorus",
    ))
    assert diff["kind"] == "inject"
    assert diff["before"] == ""
    assert [h["kind"] for h in diff["hunks"]] == ["ins"]


def test_containment_flags_travel_with_the_edit() -> None:
    edit = _edit("x", "y", flags=[
        "mixed: turn carries 1 other-speaker line(s); verify",
        'ALTERED foreign line: "You will not speak of it"',
    ])
    diff = transform.edit_diff(edit)
    assert diff["hard_flagged"] is True
    assert len(diff["flags"]) == 2


# --------------------------------------------------------------------------- #
# The whole set
# --------------------------------------------------------------------------- #


def _set(edits: list[transform.Edit], **kw) -> transform.EditSet:
    return transform.EditSet(
        log=kw.pop("log", "/tmp/log.jsonl"),
        operator=kw.pop("operator", "restyle"),
        note=kw.pop("note", "colder, more withheld"),
        locator=kw.pop("locator", "msgs 4–5"),
        edits=edits,
        **kw,
    )


def test_the_set_counts_what_needs_a_second_look() -> None:
    payload = transform.edit_set_diff(_set([
        _edit(HEADER + "one", HEADER + "one changed", msg_id=4),
        _edit("two", "two changed", msg_id=5, flags=["mixed: verify"]),
        _edit("three", "three changed", msg_id=6, flags=['ALTERED foreign line: "no"']),
    ]))
    assert len(payload["edits"]) == 3
    assert payload["flagged"] == 2
    assert payload["hard_flagged"] == 1
    assert payload["note"] == "colder, more withheld"
    assert payload["locator"] == "msgs 4–5"


def test_an_empty_set_is_a_payload_not_an_error() -> None:
    payload = transform.edit_set_diff(_set([]))
    assert payload["edits"] == []
    assert payload["flagged"] == 0


def test_operator_base_finds_the_verb_behind_the_label() -> None:
    assert transform.operator_base("retune-tighten") == "retune"
    assert transform.operator_base("restyle") == "restyle"
    assert transform.operator_base("") == ""


# --------------------------------------------------------------------------- #
# The layer a proposal targets
# --------------------------------------------------------------------------- #


def test_an_edit_set_targets_the_log_by_default() -> None:
    assert _set([]).layer == layers.LOG
    assert transform.EditSet.from_json(_set([]).to_json()).layer == layers.LOG


def test_an_old_edit_set_without_a_layer_still_loads() -> None:
    raw = _set([]).to_json()
    del raw["layer"]
    assert transform.EditSet.from_json(raw).layer == layers.LOG


def test_mixed_notice_does_not_block_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that also carries Wren's line is a look-again, not a refuse."""
    log = tmp_path / "fixture.jsonl"
    log.write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + json.dumps({"name": "Ilse", "mes": "one", "is_user": False}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending.md")

    edit_set = _set(
        [_edit("one", "two", msg_id=0, flags=["mixed: turn carries 1 other-speaker line(s)"])],
        log=str(log.resolve()),
    )
    transform.write_pending_edits(edit_set)
    backup, applied = transform.commit_edits(log)
    assert applied == 1
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[1])["mes"] == "two"
    assert backup.exists()


def test_committing_a_derived_layer_proposal_into_the_log_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write that lands in the working log is the one that can reach ST, so a
    set claiming any other layer must not be applied — and must not get as far as
    taking a backup."""
    log = tmp_path / "fixture.jsonl"
    log.write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + json.dumps({"name": "Ilse", "mes": "one", "is_user": False}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending.md")

    edit_set = _set([_edit("one", "two", msg_id=0)], log=str(log.resolve()))
    edit_set.layer = layers.MANUSCRIPT
    transform.write_pending_edits(edit_set)

    with pytest.raises(layers.LayerViolation):
        transform.commit_edits(log)
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[1])["mes"] == "one"
    assert not (tmp_path / "backups").exists()


# --------------------------------------------------------------------------- #
# Serial Weed review
# --------------------------------------------------------------------------- #


def _serial_review_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Keep every best-effort commit sidecar inside the test directory."""
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending.md")
    monkeypatch.setattr(config, "LAST_SWEEP_CONTEXT", tmp_path / "sweep.json")
    monkeypatch.setattr(config, "EDIT_HISTORY", tmp_path / "history.jsonl")
    return tmp_path / "fixture.jsonl"


def _two_turn_log(path: Path) -> None:
    path.write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + json.dumps({"name": "Ilse", "mes": "one", "is_user": False}) + "\n"
        + json.dumps({"name": "Ilse", "mes": "two", "is_user": False}) + "\n",
        encoding="utf-8",
    )


def test_serial_weed_accept_commits_one_and_preserves_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _serial_review_home(tmp_path, monkeypatch)
    _two_turn_log(log)
    proposal = _set(
        [
            _edit("one", "one revised", msg_id=0),
            _edit("two", "two revised", msg_id=1),
        ],
        operator="weed",
        log=str(log.resolve()),
        meta={"review_total": 2, "review_accepted": 0, "review_rejected": 0},
    )
    transform.write_pending_edits(proposal)

    # Accepting after using "next" must land that item, not the first one.
    backup, applied, remaining = transform.commit_one_edit(log, index=1)

    messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[1:]]
    assert [message["mes"] for message in messages] == ["one", "two revised"]
    assert backup.exists()
    assert applied == 1
    assert remaining == 1
    pending = transform.load_pending_edits()
    assert pending is not None
    assert [edit.msg_id for edit in pending.edits] == [0]
    assert pending.meta["review_total"] == 2
    assert pending.meta["review_accepted"] == 1
    assert pending.meta["review_rejected"] == 0

    second_backup, applied, remaining = transform.commit_one_edit(log, index=0)
    messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[1:]]
    assert [message["mes"] for message in messages] == ["one revised", "two revised"]
    assert second_backup != backup
    assert second_backup.exists()
    assert applied == 1
    assert remaining == 0
    assert transform.load_pending_edits() is None


def test_serial_weed_reject_removes_only_current_and_counts_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _serial_review_home(tmp_path, monkeypatch)
    _two_turn_log(log)
    proposal = _set(
        [
            _edit("one", "one revised", msg_id=0),
            _edit("two", "two revised", msg_id=1),
        ],
        operator="weed",
        log=str(log.resolve()),
        meta={"review_total": 2, "review_accepted": 0, "review_rejected": 0},
    )
    transform.write_pending_edits(proposal)

    assert transform.drop_edit(0, index=0) is True
    pending = transform.load_pending_edits()
    assert pending is not None
    assert [edit.msg_id for edit in pending.edits] == [1]
    assert pending.meta["review_total"] == 2
    assert pending.meta["review_accepted"] == 0
    assert pending.meta["review_rejected"] == 1
    assert [json.loads(line)["mes"] for line in log.read_text(encoding="utf-8").splitlines()[1:]] == [
        "one", "two",
    ]

    assert transform.drop_edit(0, index=0) is True
    assert transform.load_pending_edits() is None


def test_serial_commit_accepts_copyedit_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _serial_review_home(tmp_path, monkeypatch)
    _two_turn_log(log)
    transform.write_pending_edits(_set(
        [_edit("one", "one corrected", msg_id=0)],
        operator="copyedit",
        log=str(log.resolve()),
    ))

    _backup, applied, remaining = transform.commit_one_edit(log)
    assert applied == 1
    assert remaining == 0
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[1])["mes"] == "one corrected"


def test_serial_commit_refuses_other_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _serial_review_home(tmp_path, monkeypatch)
    _two_turn_log(log)
    transform.write_pending_edits(_set(
        [_edit("one", "one revised", msg_id=0)],
        operator="restyle",
        log=str(log.resolve()),
    ))

    with pytest.raises(ValueError, match="only for weed and copyedit"):
        transform.commit_one_edit(log)
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[1])["mes"] == "one"
