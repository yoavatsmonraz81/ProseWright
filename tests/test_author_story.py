"""GET /author/story — linear beat reader over the current log."""

from __future__ import annotations

from story_editor import author as author_mod, config, loader
from story_editor.author import seed as seed_mod


def test_build_author_story_maps_committed_beats(example_home):
    log = loader.load(config.working_log())
    n = len(log)
    assert n >= 6

    # Pretend Q2/Q3 were written and accepted onto the tail of the log: Accept
    # records each beat's message range.
    seed_mod.save_progress({
        "completed": ["Q1", "Q2", "Q3"],
        "attempts": {},
        "commits": [
            {"label": "Q2", "msg_range": [n - 4, n - 3]},
            {"label": "Q3", "msg_range": [n - 2, n - 1]},
        ],
    })

    story = author_mod.build_author_story()
    by_label = {b["label"]: b for b in story["beats"]}
    assert by_label["Q2"]["status"] == "on_log"
    assert by_label["Q2"]["messages"], "Q2 should carry current-log turns"
    assert by_label["Q3"]["messages"], "Q3 should carry current-log turns"
    assert by_label["Q2"]["msg_range"] == [n - 4, n - 3]
    assert by_label["Q3"]["msg_range"] == [n - 2, n - 1]
    # Q1 has no recorded range: its turns are inferred from the log.
    assert by_label["Q1"]["status"] == "on_log"
    assert by_label["Q1"]["messages"], "Q1 should infer the quay turns from the log"
    q1_ids = [m["msg_id"] for m in by_label["Q1"]["messages"]]
    assert min(q1_ids) < by_label["Q2"]["msg_range"][0]
