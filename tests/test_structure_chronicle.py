from pathlib import Path

from story_editor.loader import Log, Message
from story_editor.structure import group_episodes, segment_scenes


def message(msg_id: int, chronicle: dict, *, speaker: str = "Ilse") -> Message:
    return Message(
        msg_id=msg_id,
        speaker=speaker,
        is_user=speaker == "Wren",
        is_system=False,
        text=f"message {msg_id}",
        raw={"extra": {"story_editor": {"chronicle": chronicle}}},
        uid=f"se:{msg_id}",
    )


def test_chronicle_drives_scene_boundaries_and_episode_anchor() -> None:
    log = Log(
        path=Path("test.jsonl"),
        metadata={},
        messages=[
            message(0, {
                "date": "1843-12-03", "time": "10:38 PM", "location": "Ilse's chamber",
                "location_mode": "physical", "timeline_lane": "palace",
                "focal_character": "ilse", "narrative_actors": ["Ilse", "Wren"],
            }),
            message(1, {
                "date": None, "physical_anchor_date": "1843-12-03", "time": None,
                "location": "Inside. The Pinewood", "location_mode": "internal",
                "timeline_lane": "vision", "focal_character": "ilse",
                "narrative_actors": ["Ilse", "Wren"],
            }),
            message(2, {
                "date": "1843-12-03", "time": "10:41 PM", "location": "Ilse's chamber",
                "location_mode": "physical", "timeline_lane": "palace",
                "focal_character": "ilse", "narrative_actors": ["Ilse"],
            }),
        ],
    )
    scenes = segment_scenes(log)
    assert [(s.start_msg_id, s.end_msg_id) for s in scenes] == [(0, 0), (1, 1), (2, 2)]
    assert scenes[1].location_mode == "internal"
    assert scenes[1].focal_characters == ["ilse"]
    assert scenes[1].date_str is None
    assert scenes[1].episode_date_str == "1843-12-03"
    episodes = group_episodes(scenes)
    assert len(episodes) == 1
    assert episodes[0].date_str == "1843-12-03"


def test_compound_context_is_retained_on_scene() -> None:
    subsegment = {"start_offset": 10, "end_offset": 20, "location": "Guild Hall"}
    log = Log(
        path=Path("test.jsonl"), metadata={},
        messages=[message(0, {
            "date": "1843-11-24", "time": "6:12 AM", "location": "Ilse's quarters",
            "location_mode": "physical", "timeline_lane": "morning",
            "focal_character": "varga", "narrative_actors": ["Varga", "Ilse"],
            "subsegments": [subsegment],
        })],
    )
    scene = segment_scenes(log)[0]
    assert scene.context_segments == [{"msg_id": 0, **subsegment}]
