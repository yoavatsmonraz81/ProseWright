"""The GUI's read models.

These payloads are the only thing the browser sees, so the properties worth
pinning are the ones a pane would silently get wrong: that the authored plan
keeps its unwritten beats, that a page request cannot be talked into returning
the whole log, and that marks land on the turns they belong to.
"""

from __future__ import annotations

import json
from pathlib import Path

from story_editor import config, loader, views


AUTHORED_MD = """# Spine

## Premise / backbone
Not a list.

## Chronological beats

1. **Inciting event:** the compass, the harbour office.
2. **The design:** unspeakable, and a detonator.
3. **Recovery → confession:** the harbour wall.

**3a. Wren's pool** *(parallel).* The satin-to-iron montage.

## Act III — Relationship unspool

4. **War brinkmanship.** Wren presses war.
    1. Wall-demonstration — an indented sub-sequence, not a beat.
    2. Varga almost talks them back.
5. **The end.** The lighthouse goes dark.

## Interlude schedule

1. **Tolerance** · after beat 1. Not a beat either.
2. **Concern** · after beat 4.

## Through-lines to protect

1. **The wrong sister.** Also not a beat.
"""


def _write_log(path: Path, count: int = 6) -> loader.Log:
    lines = [json.dumps({"chat_metadata": {}})]
    for i in range(count):
        speaker = "Wren" if i % 2 else "Ilse"
        lines.append(json.dumps({
            "name": speaker,
            "mes": f"Turn {i}: *she said* something.",
            "is_user": speaker == "Wren",
            "extra": {"se_uid": f"m-{i:04d}"},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return loader.load(path)


# --------------------------------------------------------------------------- #
# The authored plan
# --------------------------------------------------------------------------- #


def test_authored_spine_spans_sections_and_ignores_other_lists(tmp_path: Path) -> None:
    """Beats continue past the first heading; the document's other numbered
    lists are not beats, and neither are sub-sequences inside one."""
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    beats = structure.parse_authored_spine(md)

    assert [b.label for b in beats] == ["1", "2", "3", "3a", "4", "5"]
    assert beats[-1].title == "The end"
    assert beats[4].section.startswith("Act III")
    # The interlude schedule and the through-lines restart at 1., so they lose.
    assert not any("Tolerance" in b.title or "wrong sister" in b.title for b in beats)
    # An indented sub-sequence inside beat 4 is part of its text, not a beat.
    assert "Wall-demonstration" in beats[4].text


def test_sub_beat_takes_its_title_from_the_bold_run(tmp_path: Path) -> None:
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    sub = next(b for b in structure.parse_authored_spine(md) if b.letter)
    assert sub.title == "Wren's pool"
    assert sub.number == 3 and sub.letter == "a"


def test_spine_view_keeps_planned_beats_without_a_range(monkeypatch, tmp_path) -> None:
    """A beat nothing realises still appears — that absence is the plan."""
    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    monkeypatch.setattr(config, "DERIVED_SPINE", tmp_path / "derived.json")

    log = _write_log(tmp_path / "log.jsonl")
    view = views.spine_view(log)

    assert [b["label"] for b in view["authored"]] == ["1", "2", "3", "3a", "4", "5"]
    assert view["counts"]["authored"] == 6
    assert all(b["start"] is None for b in view["authored"])
    # Without an alignment nothing may claim to be written.
    assert {b["status"] for b in view["authored"]} == {views.UNKNOWN}
    assert view["has_derived_spine"] is False


def test_alignment_round_trip_marks_written_and_planned(monkeypatch, tmp_path) -> None:
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    derived_path = tmp_path / "derived.json"
    monkeypatch.setattr(config, "DERIVED_SPINE", derived_path)

    log = _write_log(tmp_path / "log.jsonl")
    beat = structure.Beat(
        beat_id=0,
        title="Recovery",
        justification="the pond",
        first_scene_id=0,
        last_scene_id=0,
        start_msg_id=0,
        end_msg_id=3,
    )
    proposal = structure.SpineProposal(log=str(log.path), beats=[beat], n_scenes=1, n_episodes=1)
    derived_path.write_text(json.dumps(proposal.to_json()), encoding="utf-8")

    views.save_alignment(
        {
            "alignment": [{"derived": 0, "authored": "3", "note": "the pond"}],
            "absent": ["4", "5"],
            "drift": [],
            "coverage": "early slice",
        },
        authored_count=6,
    )

    view = views.spine_view(log)
    by_label = {b["label"]: b for b in view["authored"]}
    assert by_label["3"]["status"] == views.WRITTEN
    assert by_label["3"]["start"] == 0 and by_label["3"]["end"] == 3
    assert by_label["4"]["status"] == views.PLANNED
    assert by_label["4"]["start"] is None
    assert view["counts"]["written"] == 1


def test_alignment_reads_the_label_form_the_prompt_displays() -> None:
    """The audit prompt prints beats as `A13a` and asks for `13a` back. A model
    that echoes what it was shown must still align, or one stray prefix reports
    the entire story as unwritten."""
    assert views._norm_label("A13a") == "13a"
    assert views._norm_label("13a") == "13a"
    assert views._norm_label("beat 5") == "5"
    assert views._norm_label("A5.") == "5"
    assert views._norm_label("a13A") == "13a"
    # Not a label: leave it alone rather than inventing one.
    assert views._norm_label("Kettering") == "Kettering"
    assert views._norm_label(None) == ""

    by_label, absent, _, verdicts = views._alignment_index({
        "result": {
            "alignment": [
                {"derived": 3, "authored": "A13a"},
                {"derived": 4, "authored": "13a"},
            ],
            "absent": ["A12", "14"],
            "beats": [
                {
                    "authored": "8",
                    "verdict": "present",
                    "evidence": [620],
                    "derived": [8],
                    "note": "Tobias",
                },
            ],
        }
    })
    assert by_label == {"13a": [3, 4], "8": [8]}
    assert absent == {"12", "14"}
    assert verdicts["8"]["verdict"] == "present"


def test_audit_outliving_its_spine_is_stale_not_planned(monkeypatch, tmp_path) -> None:
    """An audit can name derived beats a later spine no longer contains. Calling
    that "planned" would report written prose as unwritten, and the beat would go
    quietly unreachable in the pane — the state gets its own name instead."""
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    derived_path = tmp_path / "derived.json"
    monkeypatch.setattr(config, "DERIVED_SPINE", derived_path)

    log = _write_log(tmp_path / "log.jsonl")
    beat = structure.Beat(
        beat_id=0,
        title="Recovery",
        justification="the pond",
        first_scene_id=0,
        last_scene_id=0,
        start_msg_id=0,
        end_msg_id=3,
    )
    proposal = structure.SpineProposal(log=str(log.path), beats=[beat], n_scenes=1, n_episodes=1)
    derived_path.write_text(json.dumps(proposal.to_json()), encoding="utf-8")

    views.save_alignment(
        {
            # Beat 7 does not exist: the spine holds only beat 0.
            "alignment": [
                {"derived": 0, "authored": "3"},
                {"derived": 7, "authored": "3a"},
            ],
            "absent": ["4"],
            "drift": [],
        },
        authored_count=6,
    )

    by_label = {b["label"]: b for b in views.spine_view(log)["authored"]}
    assert by_label["3a"]["status"] == views.STALE
    assert by_label["3a"]["missing_derived"] == [7]
    # The ids the audit named survive, so the pane can say which are missing.
    assert by_label["3a"]["derived_beats"] == [7]
    # A beat the audit simply never placed is still plainly planned.
    assert by_label["4"]["status"] == views.PLANNED
    assert by_label["4"]["missing_derived"] == []
    assert by_label["3"]["status"] == views.WRITTEN


def test_tail_reports_log_written_past_the_spine(monkeypatch, tmp_path) -> None:
    """The spine is derived at a moment in time; prose written afterwards belongs
    to no beat. The pane needs that number to explain an unreachable beat."""
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    derived_path = tmp_path / "derived.json"
    monkeypatch.setattr(config, "DERIVED_SPINE", derived_path)

    log = _write_log(tmp_path / "log.jsonl", count=10)
    beat = structure.Beat(
        beat_id=0,
        title="Recovery",
        justification="the pond",
        first_scene_id=0,
        last_scene_id=0,
        start_msg_id=0,
        end_msg_id=5,
    )
    proposal = structure.SpineProposal(log=str(log.path), beats=[beat], n_scenes=1, n_episodes=1)
    derived_path.write_text(json.dumps(proposal.to_json()), encoding="utf-8")

    tail = views.spine_view(log)["tail"]
    assert tail["last_mapped"] == 5
    assert tail["log_end"] == 9
    assert tail["unmapped"] == 4


def test_tail_is_zero_when_the_spine_reaches_the_end(monkeypatch, tmp_path) -> None:
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    derived_path = tmp_path / "derived.json"
    monkeypatch.setattr(config, "DERIVED_SPINE", derived_path)

    log = _write_log(tmp_path / "log.jsonl", count=6)
    beat = structure.Beat(
        beat_id=0,
        title="All of it",
        justification="everything",
        first_scene_id=0,
        last_scene_id=0,
        start_msg_id=0,
        end_msg_id=5,
    )
    proposal = structure.SpineProposal(log=str(log.path), beats=[beat], n_scenes=1, n_episodes=1)
    derived_path.write_text(json.dumps(proposal.to_json()), encoding="utf-8")

    assert views.spine_view(log)["tail"]["unmapped"] == 0


def test_pending_derive_surfaces_until_commit(monkeypatch, tmp_path) -> None:
    """Derive writes a proposal; the Spine tab must show it before commit."""
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    monkeypatch.setattr(config, "DERIVED_SPINE", tmp_path / "derived.json")
    monkeypatch.setattr(config, "PENDING_SPINE", tmp_path / "pending.json")
    monkeypatch.setattr(config, "PENDING_SPINE_MD", tmp_path / "pending.md")
    monkeypatch.setattr(config, "PENDING_SPINE_CRITIQUE", tmp_path / "pending_critique.md")

    log = _write_log(tmp_path / "log.jsonl")
    assert views.spine_view(log)["pending"] is None

    proposal = structure.SpineProposal(
        log=str(log.path),
        beats=[
            structure.Beat(
                beat_id=0,
                title="Fresh map",
                justification="re-derived",
                first_scene_id=0,
                last_scene_id=0,
                start_msg_id=0,
                end_msg_id=3,
            )
        ],
        n_scenes=1,
        n_episodes=1,
        coverage={"ok": True, "uncovered_scene_ids": [], "scene_gaps": []},
    )
    structure.write_pending_spine(proposal)
    pending = views.spine_view(log)["pending"]
    assert pending is not None
    assert pending["n_beats"] == 1
    assert pending["beats"][0]["title"] == "Fresh map"
    assert pending["coverage_ok"] is True

    structure.commit_spine()
    assert views.spine_view(log)["pending"] is None
    assert views.spine_view(log)["has_derived_spine"] is True


def test_drift_note_naming_a_beat_downgrades_it_to_partial(monkeypatch, tmp_path) -> None:
    from story_editor import structure

    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    monkeypatch.setattr(config, "AUTHORED_SPINE_MD", md)
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", tmp_path / "alignment.json")
    derived_path = tmp_path / "derived.json"
    monkeypatch.setattr(config, "DERIVED_SPINE", derived_path)

    log = _write_log(tmp_path / "log.jsonl")
    beat = structure.Beat(
        beat_id=0, title="Recovery", justification="", first_scene_id=0,
        last_scene_id=0, start_msg_id=0, end_msg_id=3,
    )
    derived_path.write_text(
        json.dumps(
            structure.SpineProposal(
                log=str(log.path), beats=[beat], n_scenes=1, n_episodes=1
            ).to_json()
        ),
        encoding="utf-8",
    )
    views.save_alignment(
        {
            "alignment": [{"derived": 0, "authored": "3"}],
            "absent": [],
            "drift": ["A3 lands much earlier than designed"],
        },
        authored_count=6,
    )
    by_label = {b["label"]: b for b in views.spine_view(log)["authored"]}
    assert by_label["3"]["status"] == views.PARTIAL


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


def test_log_span_clamps_and_caps(monkeypatch, tmp_path) -> None:
    """A page request is a scene, never the whole log — a client asking for
    everything gets a bounded answer and is told it was cut."""
    monkeypatch.setattr(config, "MANUSCRIPT", tmp_path / "manuscript.json")
    log = _write_log(tmp_path / "log.jsonl", count=12)

    whole = views.log_span_view(log, -50, 9_999)
    assert whole["start"] == 0 and whole["end"] == 11
    assert whole["truncated"] is False

    monkeypatch.setattr(views, "MAX_SPAN", 4)
    capped = views.log_span_view(log, 0, 11)
    assert capped["truncated"] is True
    assert len(capped["messages"]) == 4


def test_page_message_carries_its_marks(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "MANUSCRIPT", tmp_path / "manuscript.json")
    log = _write_log(tmp_path / "log.jsonl", count=6)

    page = views.log_span_view(log, 0, 5)
    by_id = {m["msg_id"]: m for m in page["messages"]}
    assert by_id[0]["uid"] == "m-0000"
    assert by_id[0]["manuscript_scene"] is None
    assert by_id[1]["role"] == "user"


def test_scenes_view_reports_beat_and_manuscript(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "MANUSCRIPT", tmp_path / "manuscript.json")
    monkeypatch.setattr(config, "DERIVED_SPINE", tmp_path / "derived.json")
    monkeypatch.setattr(config, "SCENE_CARDS", tmp_path / "cards.json")
    log = _write_log(tmp_path / "log.jsonl", count=6)

    view = views.scenes_view(log)
    assert view["count"] >= 1
    first = view["scenes"][0]
    assert first["beat_id"] is None  # no committed spine
    assert first["manuscript"] is None


def test_scenes_view_reports_every_manuscript_part(monkeypatch, tmp_path) -> None:
    """A scene split into derived records must not lose its later prose in UI."""
    from story_editor import manuscript as ms

    manuscript_path = tmp_path / "manuscript.json"
    monkeypatch.setattr(config, "MANUSCRIPT", manuscript_path)
    monkeypatch.setattr(config, "DERIVED_SPINE", tmp_path / "derived.json")
    monkeypatch.setattr(config, "SCENE_CARDS", tmp_path / "cards.json")
    log = _write_log(tmp_path / "log.jsonl", count=6)

    first = ms.new_scene(log, 0, 1, title="part one")
    first.blocks = ms.blocks_from_prose("First half.", src=["m-0000", "m-0001"])
    second = ms.new_scene(log, 2, 5, title="part two")
    second.blocks = ms.blocks_from_prose(
        "Second half.", src=[f"m-{i:04d}" for i in range(2, 6)]
    )
    doc = ms.Document(layer="manuscript", log=str(log.path))
    doc.upsert(first)
    doc.upsert(second)
    ms.save(doc, manuscript_path)

    row = views.scenes_view(log)["scenes"][0]
    assert row["start"] == 0 and row["end"] == 5
    assert row["manuscript"]["id"] == first.id  # legacy single-part handle
    assert row["manuscript_parts"] == [
        {"id": first.id, "status": "draft", "start": 0, "end": 1},
        {"id": second.id, "status": "draft", "start": 2, "end": 5},
    ]


def test_scenes_view_prepends_front_matter(monkeypatch, tmp_path) -> None:
    from story_editor import manuscript as ms

    manuscript_path = tmp_path / "manuscript.json"
    monkeypatch.setattr(config, "MANUSCRIPT", manuscript_path)
    monkeypatch.setattr(config, "DERIVED_SPINE", tmp_path / "derived.json")
    monkeypatch.setattr(config, "SCENE_CARDS", tmp_path / "cards.json")
    log = _write_log(tmp_path / "log.jsonl", count=6)

    front = ms.new_front_matter_scene(
        blocks=[ms.Block(id="b-front", kind="heading", text="LANTERN QUAY", note="h2")],
        title="Prologue — A Letter from the Harbour Office",
    )
    doc = ms.Document(layer="manuscript", log=str(log.path))
    doc.upsert(front)
    ms.save(doc, manuscript_path)

    view = views.scenes_view(log)
    first = view["scenes"][0]
    assert first["kind"] == "frontmatter"
    assert first["start"] == -1 and first["end"] == -1
    assert first["message_count"] == 0
    assert first["location"].startswith("Prologue")
    assert first["manuscript"]["id"] == front.id
    # The log's first scene still starts at 0 and is not stolen by the bookend.
    log_rows = [s for s in view["scenes"] if s["kind"] != "frontmatter"]
    assert log_rows[0]["start"] == 0
    assert log_rows[0]["manuscript"] is None
