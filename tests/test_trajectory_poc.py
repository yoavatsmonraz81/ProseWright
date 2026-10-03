"""Trajectory consistency POC — gates, residuals, export, earnedness."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from story_editor import config
from story_editor.author import evidence as ev_mod
from story_editor.author import run as run_mod
from story_editor.author import seed as seed_mod
from story_editor.author import trajectory as traj_mod
from story_editor.author.beat_loop import gate_proposal, mark_beat_committed
from story_editor.author.spark import compile_spark
from story_editor.loader import Log


FIXTURE_SPINE = {
    "schema": "story-editor/mini_spine@1",
    "title": "Tea, Courtyard, Letter",
    "characters": ["Wren", "Ilse"],
    "initial_state": ["Wren and Ilse are together in the palace"],
    "end_state": ["Ilse reads from the letter aloud"],
    "beats": [
        {
            "id": 1,
            "label": "A1",
            "title": "Tea",
            "criteria": ["Wren offers Ilse tea", "Ilse accepts the cup"],
            "speakers": ["Wren", "Ilse"],
        },
        {
            "id": 2,
            "label": "A2",
            "title": "Walk",
            "criteria": ["snow is mentioned"],
            "speakers": ["Wren", "Ilse"],
        },
        {
            "id": 3,
            "label": "A3",
            "title": "Letter",
            "criteria": ["a letter from Hobb arrives", "Ilse reads from the letter aloud"],
            "speakers": ["Wren", "Ilse"],
        },
    ],
}


class SimpleOk:
    ok = True
    notes: list = []
    open_items: list = []

    def to_json(self):
        return {"ok": True}


@pytest.fixture()
def traj_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = home / "workspace"
    ws.mkdir(parents=True)
    (ws / "author_runs").mkdir()
    spine_path = home / "mini_spine.json"
    spine_path.write_text(json.dumps(FIXTURE_SPINE), encoding="utf-8")
    (ws / "mini_story.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "HOME_DIR", home)
    monkeypatch.setattr(config, "IS_ALT_HOME", True)
    monkeypatch.setattr(config, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(config, "MINI_SPINE", spine_path)
    monkeypatch.setattr(config, "AUTHOR_PROGRESS", ws / "author_progress.json")
    monkeypatch.setattr(config, "WORKING_LOG", ws / "mini_story.jsonl")
    return home, ws


def _fake_write(body: str, after_msg_id: int = 0):
    edits = [
        SimpleNamespace(kind="inject", speaker="Ilse", after=body, msg_id=after_msg_id),
        SimpleNamespace(kind="inject", speaker="Wren", after="Yes.", msg_id=after_msg_id),
    ]
    edit_set = SimpleNamespace(edits=edits, operator="author_write_scene")
    return SimpleNamespace(edit_set=edit_set, after_msg_id=after_msg_id)


def test_spine_destination_fields(traj_home):
    spine = seed_mod.load_seed()
    assert spine.initial_state
    assert spine.end_state
    d = spine.to_json()
    assert "initial_state" in d and "end_state" in d


def test_run_n_from_beat_plan(traj_home):
    spine = seed_mod.load_seed()
    run = run_mod.new_run(spine)
    assert run["N"] == 3
    assert len(run["beat_plan"]) == 3
    assert run["residuals"] == spine.end_state


def test_evidence_method_tagged():
    hits = ev_mod.evidence_from_criteria_hits(
        "Wren offers Ilse tea. Ilse accepts the cup.",
        ["Wren offers Ilse tea", "missing thing"],
    )
    assert hits[0].satisfied and hits[0].method == "match"
    assert not hits[1].satisfied


def test_end_state_early_gate(traj_home, monkeypatch):
    from story_editor.author import beat_loop as bl

    spine = seed_mod.load_seed()
    run = run_mod.new_run(spine)
    run_mod.save_run(run)
    body = (
        "Wren offers Ilse tea. Ilse accepts the cup. "
        "Ilse reads from the letter aloud."
    )
    write = _fake_write(body)
    log = Log(path=traj_home[1] / "mini_story.jsonl", metadata={}, messages=[])
    monkeypatch.setattr(bl.dossier_mod, "check_span", lambda *a, **k: SimpleOk())
    monkeypatch.setattr(bl.canon_layer, "get_character", lambda *_: None)
    beat = spine.beat_by_label("A1")
    gate = gate_proposal(
        log, beat, write, skip_canon_llm=True, spine=spine, run=run, step_k=1,
    )
    codes = [g.code for g in gate.gate_failures]
    assert "END_STATE_EARLY" in codes
    assert not gate.ok


def test_spark_injects_destination(traj_home):
    spine = seed_mod.load_seed()
    run = run_mod.new_run(spine)
    note = compile_spark(spine.beats[0], spine=spine, run=run, step_k=1)
    assert "DESTINATION" in note.text
    assert "REMAINING" in note.text
    assert "STEP: 1 of 3" in note.text
    assert "Do NOT fully resolve" in note.text


def test_residuals_and_export(traj_home):
    spine = seed_mod.load_seed()
    run = run_mod.new_run(spine)
    run_mod.save_run(run)
    seed_mod.save_progress(
        {
            "completed": [],
            "attempts": {"A1": 1},
            "commits": [],
            "pending_gate": {
                "label": "A1",
                "step_k": 1,
                "N": 3,
                "run_id": run["run_id"],
                "evidence": [
                    {
                        "condition": "Wren offers Ilse tea",
                        "satisfied": True,
                        "method": "match",
                        "confidence": 1.0,
                        "span_refs": ["msg:1"],
                    }
                ],
                "attempts": [{"attempt": 1, "ok": True, "gate_results": []}],
                "spark_text": "spark",
            },
        }
    )
    mark_beat_committed("A1", earnedness=4, earnedness_note="tea from hospitality")
    run2 = run_mod.load_run(run["run_id"])
    assert run2 is not None
    assert len(run2["steps"]) == 1
    assert run2["steps"][0]["earnedness"] == 4
    lines = traj_mod.build_trajectory(run2)
    assert lines[0]["record"] == "run"
    assert lines[0]["N"] == 3
    assert lines[1]["record"] == "step"
    assert lines[1]["earnedness"] == 4
    assert lines[-1]["record"] == "footer"
