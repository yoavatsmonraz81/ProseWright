"""Novelization: voice resolution, provenance, and the guards around a pass.

No model is called here. The prompt's judgement is the author's to assess; what
these tests hold is the machinery around it — that a voice decision is honoured
and recorded, that a paragraph can be traced back to the turn it came from,
that a span too long for one pass is chunked at message boundaries, and that a
single un-chunkable turn is refused rather than truncated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import (
    layers as layers_mod,
    loader,
    manuscript as ms,
    novelize,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def write_log(path: Path, turns: list[tuple[str, bool, str]]) -> loader.Log:
    """A minimal ST chat file. Each turn is (speaker, is_user, text)."""
    lines = [json.dumps({"user_name": "You", "character_name": "Ilse"})]
    for n, (speaker, is_user, text) in enumerate(turns):
        lines.append(json.dumps({
            "name": speaker,
            "is_user": is_user,
            "is_system": False,
            "mes": text,
            "send_date": f"2026-01-01 00:{n:02d}:00",
            "extra": {"se_uid": f"uid-{n}"},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return loader.load(path)


@pytest.fixture
def log(tmp_path: Path) -> loader.Log:
    return write_log(tmp_path / "chat.jsonl", [
        ("Ilse", False, "[ 20:00 | 3 Nov | Study ]\nIlse set the ledger down. "
                        "\"The harbour master lied about the manifest.\""),
        ("Wren", True, "*you cross to the window* \"Then we sail at dawn,\" you say."),
        ("Ilse", False, "\"Dawn is too late,\" Ilse answered. The candle guttered."),
    ])


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #


def test_voice_describes_itself_in_words_an_author_uses():
    assert ms.Voice("close_third", "past", "Ilse").describe() == (
        "close third person, past tense, following Ilse"
    )
    assert ms.Voice("omniscient", "present").describe() == (
        "third person omniscient, present tense"
    )


def test_omniscient_drops_the_focal_character():
    # Nobody's head in particular, so a focal name would be a lie the prompt
    # would then act on.
    assert ms.Voice("omniscient", "past", "Ilse").normalized().focal == ""


def test_unknown_person_or_tense_falls_back_rather_than_raising():
    v = ms.Voice("epistolary", "future", "Ilse").normalized()
    assert (v.person, v.tense) == ("close_third", "past")


def test_partial_override_keeps_what_it_did_not_mention():
    book = ms.Voice("close_third", "past", "Ilse")
    assert book.merged({"tense": "present"}) == ms.Voice("close_third", "present", "Ilse")
    assert book.merged({"person": "first"}).focal == "Ilse"
    assert book.merged({"focal": "Wren"}).person == "close_third"
    assert book.merged(None) == book


def test_scene_voice_overrides_the_book(log: loader.Log):
    doc = ms.Document(voice=ms.Voice("close_third", "past", "Ilse"))
    scene = ms.new_scene(log, 0, 1, voice=ms.Voice("first", "present", "Wren"))
    doc.upsert(scene)
    assert doc.voice_for(scene).describe() == "first person, present tense, following Wren"
    assert doc.voice_for(None).describe() == "close third person, past tense, following Ilse"


def test_book_voice_survives_a_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ms.config, "MANUSCRIPT", tmp_path / "manuscript.json")
    doc = ms.Document(voice=ms.Voice("first", "present", "Wren"))
    ms.save(doc)
    assert ms.load(bind=False).voice == ms.Voice("first", "present", "Wren")


def test_generated_scene_records_the_voice_it_was_written_in(log: loader.Log):
    # Changing the book's default later must not relabel prose already written.
    scene = ms.new_scene(log, 0, 1, voice=ms.Voice("first", "present", "Wren"))
    doc = ms.Document(voice=ms.Voice("close_third", "past", "Ilse"))
    doc.upsert(scene)
    assert doc.voice_for(scene).person == "first"


def test_focal_is_filled_from_the_scenes_cast_when_unset(log: loader.Log):
    doc = ms.Document(voice=ms.Voice("close_third", "past", ""))
    voice = novelize.resolve_voice(doc, log=log, span=(0, 2))
    assert voice.focal == "Ilse"  # two of three turns


def test_omniscient_needs_no_focal_and_gets_none(log: loader.Log):
    doc = ms.Document(voice=ms.Voice("omniscient", "past", ""))
    assert novelize.resolve_voice(doc, log=log, span=(0, 2)).focal == ""


# --------------------------------------------------------------------------- #
# Reading the source
# --------------------------------------------------------------------------- #


def test_turns_are_cleaned_and_numbered(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    assert [t.index for t in turns] == [1, 2, 3]
    assert [t.uid for t in turns] == ["uid-0", "uid-1", "uid-2"]
    assert "[ 20:00" not in turns[0].body  # the header is structure, not prose


def test_director_turns_are_labelled_as_written_not_spoken(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2, labels={"1": {"voice": "wren"}})
    assert turns[1].label() == "wren (written by the director)"
    assert turns[0].label() == "Ilse"


def test_empty_turns_are_dropped(tmp_path: Path):
    log = write_log(tmp_path / "c.jsonl", [
        ("Ilse", False, "She spoke."),
        ("Wren", True, "   "),
        ("Ilse", False, "She spoke again."),
    ])
    assert [t.msg_id for t in novelize.turns_for(log, 0, 2)] == [0, 2]


def test_lead_in_is_the_turn_before_the_span(log: loader.Log):
    assert "harbour master" in novelize.lead_in(log, 1)
    assert novelize.lead_in(log, 0) == ""  # nothing precedes the first turn


def test_prompt_carries_the_voice_and_the_numbered_turns(log: loader.Log):
    messages = novelize.build_messages(
        novelize.turns_for(log, 0, 2), ms.Voice("first", "present", "Wren"),
        direction="less brooding",
    )
    system, user = messages[0]["content"], messages[1]["content"]
    assert "FIRST PERSON" in system and "Wren" in system
    assert "PRESENT TENSE" in system
    assert "«1»" in user and "«3»" in user
    assert "less brooding" in user


def test_budget_covers_reasoning_as_well_as_prose(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    # The local preset thinks before it writes, and thinking spends the same
    # ceiling; a budget sized to the prose alone gets cut off mid-scene.
    assert novelize.max_output_tokens(turns) >= novelize.MIN_OUTPUT_TOKENS
    assert novelize.max_output_tokens(turns) <= novelize.MAX_OUTPUT_TOKENS


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def test_markers_become_source_uids_and_leave_the_prose(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks, unmarked = novelize.parse_prose(
        "«1» She set the ledger down.\n\n«2,3» They argued about the tide.", turns
    )
    assert unmarked == 0
    assert blocks[0].src == ["uid-0"]
    assert blocks[1].src == ["uid-1", "uid-2"]
    assert "«" not in blocks[0].text + blocks[1].text


def test_unmarked_paragraphs_are_left_for_the_aligner(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks, unmarked = novelize.parse_prose("Plain prose, no marker at all.", turns)
    assert unmarked == 1
    assert blocks[0].src == []


def test_a_models_preamble_is_dropped(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks, _ = novelize.parse_prose(
        "Here is the novelized scene:\n\n«1» She set the ledger down.", turns
    )
    assert len(blocks) == 1
    assert blocks[0].text.startswith("She set")


def test_code_fences_are_stripped(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks, _ = novelize.parse_prose("```\n«1» The candle guttered.\n```", turns)
    assert blocks[0].text == "The candle guttered."


def test_aligner_traces_a_paragraph_by_its_dialogue(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks = [
        ms.Block(id="b1", text='Ilse closed the ledger. "The harbour master lied '
                               'about the manifest," she said.'),
        ms.Block(id="b2", text='"Dawn is too late," Ilse answered, and the candle '
                               'guttered in its dish.'),
    ]
    unresolved = novelize.align_unmarked(blocks, turns)
    assert unresolved == 0
    assert blocks[0].src == ["uid-0"]
    assert blocks[1].src == ["uid-2"]


def test_aligner_never_walks_backwards(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks = [
        ms.Block(id="b1", text="Dawn is too late.", src=["uid-2"]),
        # Echoes turn 1's words, but prose follows the transcript's order, so a
        # later paragraph cannot belong to an earlier turn.
        ms.Block(id="b2", text='The harbour master lied about the manifest, she thought.'),
    ]
    novelize.align_unmarked(blocks, turns)
    assert blocks[1].src in ([], ["uid-2"])


def test_untraceable_paragraphs_inherit_from_the_one_above(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks = [
        ms.Block(id="b1", text="Anything.", src=["uid-1"]),
        ms.Block(id="b2", text="Mmm."),
    ]
    novelize.inherit_unmarked(blocks, turns)
    assert blocks[1].src == ["uid-1"]


def test_a_scene_with_no_marked_blocks_still_gets_provenance(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    blocks = [ms.Block(id="b1", text="Nothing recognisable here.")]
    novelize.align_unmarked(blocks, turns)
    novelize.inherit_unmarked(blocks, turns)
    assert blocks[0].src == ["uid-0", "uid-1", "uid-2"]


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def test_novelize_is_illegal_on_the_log(log: loader.Log):
    with pytest.raises(layers_mod.LayerViolation):
        layers_mod.check("novelize", layers_mod.LOG)


def test_an_oversized_span_is_refused_not_truncated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ms.config, "MANUSCRIPT", tmp_path / "manuscript.json")
    long = "She crossed the floor. " * 1200
    log = write_log(tmp_path / "big.jsonl", [("Ilse", False, long)])
    with pytest.raises(novelize.TooLong) as exc:
        novelize.propose(log, 0, 0, doc=ms.Document())
    assert "one-pass ceiling" in str(exc.value)
    assert "cannot chunk" in str(exc.value).lower() or "by itself" in str(exc.value)


def test_an_empty_span_is_refused(tmp_path: Path):
    log = write_log(tmp_path / "e.jsonl", [("Ilse", False, "   ")])
    with pytest.raises(ValueError):
        novelize.propose(log, 0, 0, doc=ms.Document())


def test_plan_reports_the_job_without_calling_a_model(log: loader.Log):
    p = novelize.plan(log, 0, 2, doc=ms.Document(voice=ms.Voice("first", "present", "Wren")))
    assert p["turns"] == 3
    assert p["voice_label"] == "first person, present tense, following Wren"
    assert p["too_long"] is False
    assert p["chunk_count"] == 1
    assert len(p["chunks"]) == 1
    assert p["unchunkable"] is False
    assert [c["name"] for c in p["focal_candidates"]][0] == "Ilse"
    assert p["existing"] is None


def test_plan_reports_an_existing_scene_and_its_voice(log: loader.Log):
    doc = ms.Document(voice=ms.Voice("close_third", "past", "Ilse"))
    doc.upsert(ms.new_scene(log, 0, 2, voice=ms.Voice("omniscient", "present"),
                            status="approved"))
    p = novelize.plan(log, 0, 2, doc=doc)
    assert p["existing"]["status"] == "approved"
    assert p["existing"]["voice"]["person"] == "omniscient"
    # A re-roll of an existing scene inherits that scene's voice, not the book's.
    assert p["voice"]["person"] == "omniscient"


# --------------------------------------------------------------------------- #
# Chunked novelize
# --------------------------------------------------------------------------- #


def test_split_chunks_packs_under_the_ceiling(log: loader.Log):
    turns = novelize.turns_for(log, 0, 2)
    assert novelize.split_chunks(turns) == [(0, 2)]


def test_split_chunks_never_splits_a_turn(tmp_path: Path):
    piece = "x" * 10_000
    log = write_log(tmp_path / "pack.jsonl", [
        ("Ilse", False, piece),
        ("Wren", True, piece),
        ("Ilse", False, piece),
        ("Wren", True, piece),
    ])
    turns = novelize.turns_for(log, 0, 3)
    assert novelize.source_chars(turns) > novelize.MAX_SPAN_CHARS
    spans = novelize.split_chunks(turns)
    assert len(spans) > 1
    # Every turn appears in exactly one chunk; no mid-turn cuts.
    covered = []
    for lo, hi in spans:
        chunk = [t for t in turns if lo <= t.msg_id <= hi]
        assert chunk[0].msg_id == lo and chunk[-1].msg_id == hi
        assert novelize.source_chars(chunk) <= novelize.MAX_SPAN_CHARS
        covered.extend(t.msg_id for t in chunk)
    assert covered == [t.msg_id for t in turns]


def test_split_chunks_raises_on_a_single_giant_turn(tmp_path: Path):
    giant = "y" * (novelize.MAX_SPAN_CHARS + 100)
    log = write_log(tmp_path / "giant.jsonl", [("Ilse", False, giant)])
    turns = novelize.turns_for(log, 0, 0)
    with pytest.raises(novelize.TooLong):
        novelize.split_chunks(turns)


def test_prose_lead_in_takes_the_tail(tmp_path: Path):
    assert novelize.prose_lead_in([]) == ""
    short = [ms.Block(id="a", text="Hello there.")]
    assert novelize.prose_lead_in(short) == "Hello there."
    long = [ms.Block(id="b", text="A" * 400), ms.Block(id="c", text="B" * 400)]
    lead = novelize.prose_lead_in(long)
    assert lead.startswith("…")
    assert lead.endswith("B" * 400)
    assert len(lead) == novelize.LEAD_IN_CHARS + 1  # ellipsis + tail


def test_plan_reports_chunks_when_over_ceiling(tmp_path: Path):
    piece = "z" * 10_000
    log = write_log(tmp_path / "long.jsonl", [
        ("Ilse", False, piece),
        ("Wren", True, piece),
        ("Ilse", False, piece),
    ])
    p = novelize.plan(log, 0, 2, doc=ms.Document())
    assert p["too_long"] is True
    assert p["unchunkable"] is False
    assert p["chunk_count"] > 1
    assert len(p["chunks"]) == p["chunk_count"]
    assert p["chunks"][0]["from"] == 0
    assert p["chunks"][-1]["to"] == 2


def test_max_span_chars_override_changes_chunk_count(tmp_path: Path):
    piece = "q" * 8_000
    log = write_log(tmp_path / "override.jsonl", [
        ("Ilse", False, piece),
        ("Wren", True, piece),
        ("Ilse", False, piece),
    ])
    tight = novelize.plan(log, 0, 2, doc=ms.Document(), max_span_chars=9_000)
    loose = novelize.plan(log, 0, 2, doc=ms.Document(), max_span_chars=30_000)
    assert tight["chunk_count"] > loose["chunk_count"]
    assert loose["chunk_count"] == 1
    assert tight["max_span_chars"] == 9_000
    assert loose["too_long"] is False


def test_resolve_max_span_rejects_tiny_ceilings():
    with pytest.raises(ValueError):
        novelize.resolve_max_span(100)


def test_chunked_propose_calls_each_pass_and_merges(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ms.config, "MANUSCRIPT", tmp_path / "manuscript.json")
    piece = "w" * 10_000
    log = write_log(tmp_path / "chunk.jsonl", [
        ("Ilse", False, piece),
        ("Wren", True, piece),
        ("Ilse", False, piece),
        ("Wren", True, piece),
    ])
    calls: list[tuple[int, int, str]] = []

    def fake_chunk(
        log_, chunk_start, chunk_end, *, voice, context, where, direction, model, temperature,
    ):
        calls.append((chunk_start, chunk_end, context))
        turns = novelize.turns_for(log_, chunk_start, chunk_end)
        blocks = [
            ms.Block(
                id=f"b-{t.msg_id}",
                text=f"Prose drawn from message {t.msg_id}.",
                src=[t.uid],
            )
            for t in turns
        ]
        return blocks, 0, 0, False, []

    monkeypatch.setattr(novelize, "_propose_chunk", fake_chunk)
    prop = novelize.propose(log, 0, 3, doc=ms.Document(), model="test-model")
    assert len(calls) > 1
    assert prop.scene.anchor.start == 0
    assert prop.scene.anchor.end == 3
    assert len(prop.scene.blocks) == 4
    assert [b.text for b in prop.scene.blocks] == [
        "Prose drawn from message 0.",
        "Prose drawn from message 1.",
        "Prose drawn from message 2.",
        "Prose drawn from message 3.",
    ]
    # First chunk uses log lead-in (may be empty); later chunks use prose tail.
    assert calls[0][2] == "" or "Ilse" in calls[0][2] or calls[0][2].startswith("…")
    for _, _, ctx in calls[1:]:
        assert "Prose drawn from message" in ctx
    assert any("chunked:" in w for w in prop.warnings)


def test_orchestration_units_split_log_scenes_at_locked_episode_seams(
    log: loader.Log, monkeypatch,
):
    from types import SimpleNamespace

    source_scene = SimpleNamespace(start_msg_id=0, end_msg_id=2, scene_id=7)
    episodes = SimpleNamespace(
        unit_type="episode",
        beats=[
            SimpleNamespace(beat_id=0, title="One", start_msg_id=0, end_msg_id=0),
            SimpleNamespace(beat_id=1, title="Two", start_msg_id=1, end_msg_id=2),
        ],
    )
    monkeypatch.setattr(novelize.structure_mod, "segment_scenes", lambda _log: [source_scene])
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: episodes)

    units = novelize.orchestration_units(log)
    assert [(u.start, u.end, u.log_scene_id, u.episode_id) for u in units] == [
        (0, 0, 7, 0),
        (1, 2, 7, 1),
    ]


def test_batch_retries_checkpoints_uses_prior_prose_and_assembles(
    log: loader.Log, tmp_path: Path, monkeypatch,
):
    manuscript_path = tmp_path / "manuscript.json"
    monkeypatch.setattr(ms.config, "MANUSCRIPT", manuscript_path)
    monkeypatch.setattr(novelize.config, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(novelize.config, "BACKUP_DIR", tmp_path / "backups")
    units = [
        novelize.NovelizeUnit(0, 0, 10, 0, "Opening"),
        novelize.NovelizeUnit(1, 1, 11, 0, "Opening"),
    ]
    monkeypatch.setattr(novelize, "pending_scenes", lambda *_a, **_k: units)
    monkeypatch.setattr(novelize, "orchestration_units", lambda _log: units)
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: None)
    calls: list[tuple[int, str]] = []

    def fake_propose(log_, start, end, **kwargs):
        calls.append((start, kwargs.get("continuity_context", "")))
        if start == 0 and sum(1 for value, _ in calls if value == 0) == 1:
            raise RuntimeError("transient")
        voice = ms.Voice("close_third", "past", "Ilse")
        scene = ms.new_scene(
            log_, start, end,
            blocks=[ms.Block(id=f"b-{start}", text=f"Novel prose {start}.", src=[log_.get(start).uid])],
            title=f"Scene {start}", model="fake", voice=voice,
        )
        return novelize.Proposal(
            scene=scene, voice=voice, turns=1, source_chars=10,
            prose_chars=14, unmarked=0, unresolved=0, model="fake",
        )

    monkeypatch.setattr(novelize, "propose", fake_propose)
    doc = ms.Document(voice=ms.Voice("close_third", "past", "Ilse"))
    result = novelize.run_batch(
        log, doc=doc, model="fake", max_retries=1, backup=False,
        campaign_path=tmp_path / "campaign.json",
        assemble_path=tmp_path / "book.md",
    )

    assert result.to_json()["counts"] == {"novelized": 2, "skipped": 0, "failed": 0}
    assert [item.attempts for item in result.novelized] == [2, 1]
    assert calls[-1][0] == 1 and "Novel prose 0" in calls[-1][1]
    assert json.loads((tmp_path / "campaign.json").read_text())["status"] == "complete"
    assert "Novel prose 0" in (tmp_path / "book.md").read_text()
    assert "Novel prose 1" in (tmp_path / "book.md").read_text()


def test_batch_can_be_restricted_to_one_episode(
    log: loader.Log, tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(ms.config, "MANUSCRIPT", tmp_path / "manuscript.json")
    monkeypatch.setattr(novelize.config, "WORKSPACE_DIR", tmp_path)
    units = [
        novelize.NovelizeUnit(0, 0, 10, 3, "Three"),
        novelize.NovelizeUnit(1, 1, 11, 4, "Four"),
        novelize.NovelizeUnit(2, 2, 12, 3, "Three"),
    ]
    monkeypatch.setattr(novelize, "pending_scenes", lambda *_a, **_k: units)
    monkeypatch.setattr(novelize, "orchestration_units", lambda _log: units)
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: None)

    def fake_propose(log_, start, end, **kwargs):
        voice = ms.Voice("close_third", "past", "Ilse")
        scene = ms.new_scene(
            log_, start, end,
            blocks=[ms.Block(id=f"b-{start}", text=f"Novel prose {start}.", src=[log_.get(start).uid])],
            title=f"Scene {start}", model="fake", voice=voice,
        )
        return novelize.Proposal(
            scene=scene, voice=voice, turns=1, source_chars=10,
            prose_chars=14, unmarked=0, unresolved=0, model="fake",
        )

    monkeypatch.setattr(novelize, "propose", fake_propose)
    result = novelize.run_batch(
        log, doc=ms.Document(), model="fake", episode_id=3, backup=False,
        campaign_path=tmp_path / "campaign.json",
        assemble_path=tmp_path / "book.md",
    )

    assert [(item.start, item.episode_id) for item in result.novelized] == [(0, 3), (2, 3)]






def test_assembly_excludes_separate_pdf_bookends(tmp_path: Path, monkeypatch):
    log = write_log(tmp_path / "body.jsonl", [
        ("Ilse", False, "Ilse opened the door."),
    ])
    episodes = type("Episodes", (), {
        "unit_type": "episode",
        "beats": [type("Beat", (), {
            "title": "The Late Ship",
            "start_msg_id": 0,
            "end_msg_id": 0,
        })()],
    })()
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: episodes)
    doc = ms.Document()
    bookend = ms.Scene(
        id="sc-prologue",
        anchor=ms.Anchor(start=-1, end=-1),
        title="Official Prologue",
        blocks=[ms.Block(id="prologue", text="Separate PDF text.")],
    )
    doc.upsert(bookend)
    doc.upsert(ms.new_scene(
        log, 0, 0,
        blocks=[ms.Block(id="body", text="The door opened.", src=["uid-0"])],
    ))

    assembled = novelize.assemble_private_markdown(doc, log)

    assert assembled.startswith('<h1 align="center">The Late Ship</h1>\n')
    assert "The door opened." in assembled
    assert "Official Prologue" not in assembled
    assert "Separate PDF text." not in assembled


def test_assembly_can_select_canonical_chapters(tmp_path: Path, monkeypatch):
    log = write_log(tmp_path / "chapters.jsonl", [
        ("Ilse", False, "First source."),
        ("Wren", True, "Second source."),
    ])
    episodes = type("Episodes", (), {
        "unit_type": "episode",
        "beats": [
            type("Beat", (), {
                "beat_id": 0, "title": "First", "start_msg_id": 0, "end_msg_id": 0,
            })(),
            type("Beat", (), {
                "beat_id": 1, "title": "Second", "start_msg_id": 1, "end_msg_id": 1,
            })(),
        ],
    })()
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: episodes)
    doc = ms.Document()
    doc.upsert(ms.new_scene(
        log, 0, 0, blocks=[ms.Block(id="first", text="First chapter.", src=["uid-0"])],
    ))
    doc.upsert(ms.new_scene(
        log, 1, 1, blocks=[ms.Block(id="second", text="Second chapter.", src=["uid-1"])],
    ))

    assembled = novelize.assemble_private_markdown(doc, log, chapter_ids={1})

    assert "Second chapter." in assembled
    assert ">Second</h1>" in assembled
    assert "First chapter." not in assembled
    assert ">First</h1>" not in assembled


def test_plain_text_autosave_is_body_only_and_atomic(tmp_path: Path, monkeypatch):
    log = write_log(tmp_path / "autosave.jsonl", [
        ("Ilse", False, "Ilse opened the door."),
    ])
    episodes = type("Episodes", (), {
        "unit_type": "episode",
        "beats": [type("Beat", (), {
            "title": "The Late Ship",
            "start_msg_id": 0,
            "end_msg_id": 0,
        })()],
    })()
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: episodes)
    doc = ms.Document()
    doc.upsert(ms.Scene(
        id="sc-prologue",
        anchor=ms.Anchor(start=-1, end=-1),
        title="Official Prologue",
        blocks=[ms.Block(id="prologue", text="Separate PDF text.")],
    ))
    doc.upsert(ms.new_scene(
        log, 0, 0,
        blocks=[ms.Block(id="body", text="The *violet* scarf.", src=["uid-0"])],
    ))
    target = tmp_path / "private_manuscript_autosave.txt"

    written = novelize.write_private_text_autosave(doc, log, target)

    assert written == target
    assert target.read_text(encoding="utf-8") == "The Late Ship\n\nThe *violet* scarf.\n"
    assert not (tmp_path / ".private_manuscript_autosave.txt.tmp").exists()


def test_pending_requires_the_whole_orchestration_unit_to_be_covered(
    log: loader.Log, monkeypatch,
):
    unit = novelize.NovelizeUnit(0, 2, 7, 0, "Opening")
    monkeypatch.setattr(novelize, "orchestration_units", lambda _log: [unit])
    doc = ms.Document()
    doc.upsert(ms.new_scene(log, 0, 1, blocks=[ms.Block(id="partial", text="Partial.")]))
    assert novelize.pending_scenes(log, doc) == [unit]
    doc.upsert(ms.new_scene(log, 2, 2, blocks=[ms.Block(id="tail", text="Tail.")]))
    assert novelize.pending_scenes(log, doc) == []


def test_batch_refuses_existing_scene_that_crosses_current_boundaries(
    log: loader.Log, monkeypatch,
):
    units = [
        novelize.NovelizeUnit(0, 0, 7, 0, "Opening"),
        novelize.NovelizeUnit(1, 2, 8, 0, "Opening"),
    ]
    monkeypatch.setattr(novelize, "orchestration_units", lambda _log: units)
    doc = ms.Document()
    crossing = ms.new_scene(log, 0, 1, blocks=[ms.Block(id="cross", text="Crossing.")])
    doc.upsert(crossing)

    with pytest.raises(RuntimeError, match="cross current orchestration boundaries"):
        novelize.run_batch(log, doc=doc, backup=False)


def test_plain_text_can_select_one_chapter(tmp_path: Path, monkeypatch):
    log = write_log(tmp_path / "select.jsonl", [
        ("Ilse", False, "Ilse opened the door."),
        ("Wren", True, "Wren stepped through."),
    ])
    beat = lambda beat_id, title, start: type("Beat", (), {  # noqa: E731
        "beat_id": beat_id, "title": title, "start_msg_id": start, "end_msg_id": start,
    })()
    episodes = type("Episodes", (), {
        "unit_type": "episode",
        "beats": [beat(0, "The Late Ship", 0), beat(1, "The Bell at Dawn", 1)],
    })()
    monkeypatch.setattr(novelize.structure_mod, "load_derived_spine", lambda _log: episodes)
    doc = ms.Document()
    doc.upsert(ms.new_scene(log, 0, 0, blocks=[ms.Block(id="one", text="First.", src=["uid-0"])]))
    doc.upsert(ms.new_scene(log, 1, 1, blocks=[ms.Block(id="two", text="Second.", src=["uid-1"])]))

    assert novelize.assemble_private_text(doc, log, chapter_ids={1}) == (
        "The Bell at Dawn\n\nSecond.\n"
    )
    assert novelize.assemble_private_text(doc, log).startswith("The Late Ship\n\nFirst.")


def test_the_narrator_is_not_offered_as_a_focal_character(tmp_path: Path):
    log = write_log(tmp_path / "chat.jsonl", [
        ("Narrator", False, "[ 20:00 | 3 Nov | Quay ]\nThe brig comes in without a pilot."),
        ("Narrator", False, "Lamps along the mole."),
        ("Wren", True, "Wren watches the brig."),
    ])
    assert [c["name"] for c in novelize.focal_candidates(log, 0, 2)] == ["Wren"]
    doc = ms.Document(voice=ms.Voice("close_third", "past", ""))
    assert novelize.resolve_voice(doc, log=log, span=(0, 2)).focal == "Wren"
    only = write_log(tmp_path / "only.jsonl", [("Narrator", False, "[ 20:00 | 3 Nov | Quay ]\nRain.")])
    assert [c["name"] for c in novelize.focal_candidates(only, 0, 0)] == ["Narrator"]
