from story_editor.structure import (
    Beat,
    Scene,
    enrich_beats_with_evidence,
    validate_spine_coverage,
)


def _scene(sid: int, start: int, end: int, *, loc: str = "Room") -> Scene:
    return Scene(
        scene_id=sid,
        start_msg_id=start,
        end_msg_id=end,
        kind="scene",
        location=loc,
        opening=f"open S{sid}",
        closing=f"close S{sid}",
    )


def test_validate_spine_full_coverage() -> None:
    scenes = [_scene(i, i * 10, i * 10 + 9) for i in range(4)]
    beats = [
        Beat(0, "A", "turn", 0, 19, 0, 1),
        Beat(1, "B", "turn", 20, 39, 2, 3),
    ]
    report = validate_spine_coverage(scenes, beats)
    assert report.ok
    assert not report.uncovered_scene_ids
    assert not report.scene_gaps


def test_validate_spine_detects_gap() -> None:
    scenes = [_scene(i, i * 10, i * 10 + 9) for i in range(4)]
    beats = [
        Beat(0, "A", "turn", 0, 9, 0, 0),
        Beat(1, "B", "turn", 20, 39, 2, 3),
    ]
    report = validate_spine_coverage(scenes, beats)
    assert not report.ok
    assert 1 in report.uncovered_scene_ids


def test_enrich_beats_with_evidence() -> None:
    scenes = [_scene(0, 0, 5), _scene(1, 6, 12, loc="Cellar")]
    beats = [Beat(0, "Trap", "climax", 0, 12, 0, 1)]
    cards = {0: "Wren springs the trap.", 1: "Varga is taken."}
    enrich_beats_with_evidence(beats, scenes, cards)
    assert len(beats[0].evidence) == 2
    assert beats[0].evidence[0].startswith("S0")
    assert "trap" in beats[0].evidence[0].lower()
