"""Golden spark compiler tests (no LLM)."""

from __future__ import annotations

import pytest

from story_editor.author.seed import MiniBeat, MiniSpine, load_seed
from story_editor.author.spark import (
    clear_spark_draft,
    compile_spark,
    load_spark_draft,
    manual_spark_template,
    order_speakers,
    save_spark_draft,
    spark_from_text,
)
from story_editor import config, dossier as dossier_mod

@pytest.fixture()
def mini_home(example_home):
    return example_home


def test_load_seed_three_beats(mini_home):
    spine = load_seed(mini_home / "mini_spine.json")
    assert len(spine.beats) == 3
    assert spine.beats[0].label == "Q1"
    assert spine.beats[0].criteria


def test_spark_includes_criteria_and_dossier(mini_home):
    spine = load_seed(mini_home / "mini_spine.json")
    beat = spine.beats[0]
    note = compile_spark(beat, spine=spine, as_of_msg=1)
    assert "The Gull's Due arrives late" in note.text
    assert "MUST LAND" in note.text
    assert "DOSSIER" in note.text or note.dossier_blocks
    # Forbidden from bible should appear when characters.json loads
    assert note.must_land


def test_spark_failure_notes_steer_retry(mini_home):
    spine = load_seed(mini_home / "mini_spine.json")
    note = compile_spark(
        spine.beats[0],
        spine=spine,
        failure_notes=["criteria missing: a cask sounds like paper, not wine"],
    )
    assert "PRIOR ATTEMPT FAILED" in note.text
    assert "a cask sounds like paper" in note.text


def test_dossier_as_of_baseline(mini_home):
    d = dossier_mod.load("wren")
    assert d is not None
    brief = dossier_mod.project_brief(d, 0)
    assert "DOSSIER" in brief
    assert "chartmaker" in brief


def test_criteria_gate_helper():
    from story_editor.author.beat_loop import _criteria_hits

    ok, missing = _criteria_hits(
        "Wren offers Ilse tea and Ilse accepts the cup gratefully.",
        ["Wren offers Ilse tea", "Ilse accepts the cup", "a dragon appears"],
    )
    assert "Wren offers Ilse tea" in ok
    assert "Ilse accepts the cup" in ok
    assert "a dragon appears" in missing


def test_criteria_accepts_a3_paraphrase():
    from story_editor.author.criteria import criteria_hits

    criteria = [
        "a letter from Hobb arrives",
        "Ilse reads from the letter aloud",
    ]
    # Past tense + "out loud" — old exact-token matcher failed these.
    prose = (
        "Wren brought a sealed letter from Hobb to the desk. "
        "Ilse broke the seal and read the letter out loud."
    )
    ok, missing = criteria_hits(prose, criteria)
    assert missing == []
    assert set(ok) == set(criteria)
    # Still require Hobb — letter alone is not enough.
    _, missing2 = criteria_hits(
        "Wren brought a sealed letter. Ilse read it out loud.",
        criteria,
    )
    assert "a letter from Hobb arrives" in missing2


def test_manual_spark_template_has_placeholders(mini_home):
    spine = load_seed(mini_home / "mini_spine.json")
    note = manual_spark_template(spine.beats[0])
    assert note.beat_label == "Q1"
    assert "SCENE SPARK — Q1" in note.text
    assert "MUST LAND" in note.text
    assert "DIRECTION (your hand" in note.text
    assert "<opening image / staging>" in note.text
    assert "The Gull's Due arrives late" in note.text
    # Live dossier brief must be in the hand template (File-pane edits).
    assert "CHARACTER DOSSIERS" in note.text
    assert "DOSSIER wren" in note.text


def test_spark_from_text_injects_live_dossiers(mini_home):
    spine = load_seed(mini_home / "mini_spine.json")
    beat = spine.beats[0]
    bare = "SCENE SPARK — hand\nSPEAKERS (in order): Wren, Ilse\nMUST LAND:\n  • tea\n"
    note = spark_from_text(beat, bare, as_of_msg=1)
    assert "CHARACTER DOSSIERS" in note.text
    assert "DOSSIER wren" in note.text
    # Stale dossier in the draft is replaced with the live brief.
    stale = bare + "\nCHARACTER DOSSIERS (old):\nDOSSIER wren (as of msg 0):\n- [state] STALE FACT\n"
    refreshed = spark_from_text(beat, stale, as_of_msg=1)
    assert "STALE FACT" not in refreshed.text
    assert "chartmaker" in refreshed.text


def test_first_speaker_reorders_turns(mini_home):
    assert order_speakers(["Wren", "Ilse"], "Ilse") == ["Ilse", "Wren"]
    spine = load_seed(mini_home / "mini_spine.json")
    beat = spine.beats[0]
    note = compile_spark(beat, spine=spine, as_of_msg=1, first_speaker="Narrator")
    assert note.speakers[0] == "Narrator"
    assert "SPEAKERS (in order): Narrator, Wren" in note.text
    hand = spark_from_text(
        beat,
        "SCENE SPARK\nSPEAKERS (in order): Wren, Ilse\nMUST LAND:\n  • tea\n",
        first_speaker="Ilse",
    )
    assert hand.speakers == ["Ilse", "Wren", "Narrator"]
    assert "SPEAKERS (in order): Ilse, Wren" in hand.text


def test_spark_from_text_and_draft_roundtrip(mini_home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    spine = load_seed(mini_home / "mini_spine.json")
    beat = spine.beats[0]
    hand = "SCENE SPARK — custom\nMUST LAND:\n  • Wren offers Ilse tea\n"
    note = spark_from_text(beat, hand, failure_notes=["missed cup"])
    assert "custom" in note.text
    assert "PRIOR ATTEMPT FAILED" in note.text
    assert "missed cup" in note.text

    save_spark_draft(beat_label=beat.label, text=hand)
    draft = load_spark_draft()
    assert draft is not None
    assert draft["beat_label"] == "Q1"
    assert "custom" in draft["text"]
    assert clear_spark_draft() is True
    assert load_spark_draft() is None
