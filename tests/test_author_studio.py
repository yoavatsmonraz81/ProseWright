"""Author Studio — spark compiler + dossier gates (no live LLM)."""

from __future__ import annotations

import pytest

from story_editor import config, dossier as dossier_mod, loader
from story_editor.author import compile_spark, load_seed
from story_editor.author.gates import run_gates
from story_editor.author.write_scene import WriteResult
from story_editor.transform import Edit, EditSet

@pytest.fixture()
def studio_home(example_home):
    yield example_home


def test_load_mini_spine(studio_home):
    spine = load_seed()
    assert len(spine.beats) == 3
    assert spine.beats[0].label == "Q1"
    assert spine.beats[0].criteria


def test_spark_includes_criteria_and_dossier(studio_home):
    spine = load_seed()
    log = loader.load(config.WORKING_LOG)
    beat = spine.beats[0]
    note = compile_spark(beat, spine=spine, log=log)
    assert "MUST LAND" in note.text
    # Every criterion phrase should appear in the spark text.
    for c in beat.criteria:
        assert c.split()[0].lower() in note.text.lower()
    assert note.dossier_blocks, "expected dossier projection in spark"
    assert beat.location.split()[0] in note.text


def test_dossier_as_of_and_check_span(studio_home):
    d = dossier_mod.load("ilse") or dossier_mod.load("Ilse")
    assert d is not None
    entries = dossier_mod.as_of(d, 0)
    assert entries
    brief = dossier_mod.project_brief(d, 0)
    assert "DOSSIER" in brief
    assert d.character.lower() in brief.lower()
    log = loader.load(config.WORKING_LOG)
    check = dossier_mod.check_span(log, 0, 1, characters=["ilse", "wren"])
    assert check.ok  # no open items in seed


def test_gates_criteria_heuristic(studio_home):
    spine = load_seed()
    beat = spine.beats[0]
    log = loader.load(config.WORKING_LOG)
    # Draft that lands distinctive tokens from the beat criteria.
    body = (
        f"[ 📍 {beat.location} ]\n"
        + " ".join(beat.criteria)
        + " Wren hands Ilse the manifest and Ilse reads it."
    )
    edits = [
        Edit(msg_id=1, speaker="Narrator", before="", after=body, kind="inject"),
    ]
    write = WriteResult(
        after_msg_id=1,
        speakers=["Narrator"],
        edit_set=EditSet(
            log=str(log.path),
            operator="test",
            note="t",
            locator="t",
            edits=edits,
        ),
    )
    gate = run_gates(log, beat, write, skip_canon_llm=True)
    assert gate.ok, gate.failures


def test_gates_fail_missing_criteria(studio_home):
    spine = load_seed()
    beat = spine.beats[0]
    log = loader.load(config.WORKING_LOG)
    edits = [
        Edit(
            msg_id=1,
            speaker="Narrator",
            before="",
            after="The corridor was empty and nobody spoke.",
            kind="inject",
        ),
    ]
    write = WriteResult(
        after_msg_id=1,
        speakers=["Narrator"],
        edit_set=EditSet(
            log=str(log.path),
            operator="test",
            note="t",
            locator="t",
            edits=edits,
        ),
    )
    gate = run_gates(log, beat, write, skip_canon_llm=True)
    assert not gate.ok
    assert any("criteria" in f for f in gate.failures)


def test_remove_dossier_entry(studio_home):
    d = dossier_mod.load("ilse") or dossier_mod.load("Ilse")
    assert d is not None
    before = len(d.entries)
    assert before >= 1
    victim = d.entries[0]
    # Pair an open item that cites the entry so remove cleans it up.
    d.open_items.append(
        dossier_mod.OpenItem(
            id="tmp-open",
            entry_a=victim.id,
            entry_b="other",
            note="test contradiction",
        )
    )
    removed = dossier_mod.remove_entry(d, victim.id)
    assert removed.id == victim.id
    assert len(d.entries) == before - 1
    assert all(o.entry_a != victim.id and o.entry_b != victim.id for o in d.open_items)


def test_author_commit_advances_beat_cursor(studio_home, tmp_path, monkeypatch):
    from story_editor.author import on_author_edits_committed, seed as seed_mod

    monkeypatch.setattr(
        config, "WORKSPACE_DIR", tmp_path,
    )
    seed_mod.save_progress({"completed": [], "awaiting": "A1", "attempts": {"A1": 1}})
    edit_set = EditSet(
        log=str(config.WORKING_LOG),
        operator="author_write_scene",
        note="spark A1",
        locator="after msg 1 · A1",
        edits=[],
    )
    label = on_author_edits_committed(edit_set, backup="/tmp/fake-backup.jsonl")
    assert label == "A1"
    progress = seed_mod.load_progress()
    assert "A1" in progress["completed"]
    assert progress.get("awaiting") in (None, "")
    assert progress["commits"][0]["backup"] == "/tmp/fake-backup.jsonl"


def test_author_undo_redo_reopens_beats(studio_home, tmp_path, monkeypatch):
    from story_editor.author import redo_author_commit, seed as seed_mod, undo_author_commit
    from story_editor import backup as backup_mod

    log = tmp_path / "mini.jsonl"
    # Minimal SillyTavern-ish log: metadata + one stub message.
    log.write_text(
        '{"user_name":"You","character_name":"Narrator"}\n'
        '{"name":"Narrator","is_user":false,"mes":"hello","send_date":"x"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("STORY_EDITOR_LOG", str(log))
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "WORKING_LOG", log)
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending_edits.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending_edits.md")

    pre_a2 = backup_mod.backup(log, label="pre-a2")
    log.write_text(log.read_text(encoding="utf-8") +
                   '{"name":"Wren","is_user":false,"mes":"after A2","send_date":"x"}\n',
                   encoding="utf-8")
    after_a2 = log.read_text(encoding="utf-8")

    seed_mod.save_progress({
        "completed": ["A1", "A2"],
        "commits": [
            {"label": "A1", "backup": str(pre_a2)},  # coarse; enough for reopen A2
            {"label": "A2", "backup": str(pre_a2)},
        ],
        "attempts": {},
    })

    undone = undo_author_commit("A2")
    assert undone["undone"] == ["A2"]
    assert seed_mod.load_progress()["completed"] == ["A1"]
    assert "after A2" not in log.read_text(encoding="utf-8")

    redone = redo_author_commit()
    assert redone["redone"] == ["A2"]
    assert seed_mod.load_progress()["completed"] == ["A1", "A2"]
    assert log.read_text(encoding="utf-8") == after_a2
