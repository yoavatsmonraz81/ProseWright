"""Mini-spine save / load / template for Author Studio."""

from __future__ import annotations

import json

import pytest

from story_editor import author as author_mod
from story_editor.author import seed as seed_mod


def test_save_seed_round_trip(example_home):
    spine = seed_mod.load_seed()
    spine.title = "Renamed plan"
    spine.beats[0].title = "The late ship, revised"
    saved = seed_mod.save_seed(spine)
    assert saved.title == "Renamed plan"
    again = seed_mod.load_seed()
    assert again.title == "Renamed plan"
    assert again.beats[0].title == "The late ship, revised"
    raw = json.loads((example_home / "mini_spine.json").read_text(encoding="utf-8"))
    assert raw["schema"] == seed_mod.MINI_SPINE_SCHEMA


def test_save_rejects_empty_criteria(example_home):
    data = seed_mod.load_seed().to_json()
    data["beats"][0]["criteria"] = []
    with pytest.raises(ValueError, match="Q1"):
        seed_mod.save_seed(data)


def test_save_prunes_stale_progress_labels(example_home):
    seed_mod.save_progress({
        "completed": ["Q1", "Q2", "Q9"],
        "awaiting": "Q9",
        "attempts": {"Q1": 1, "Q9": 2},
        "commits": [
            {"label": "Q1", "backup": "x"},
            {"label": "Q9", "backup": "y"},
        ],
        "redo": [{"labels": ["Q1"]}],
    })
    data = seed_mod.load_seed().to_json()
    # Drop Q2 from the plan.
    data["beats"] = [b for b in data["beats"] if b["label"] != "Q2"]
    view = seed_mod.save_seed_and_sync_progress(data)
    done = view["progress"]["completed"]
    assert "Q1" in done
    assert "Q2" not in done
    assert "Q9" not in done
    assert view["progress"].get("awaiting") in (None, "")
    assert "redo" not in view["progress"] or not view["progress"].get("redo")
    labels = {b["label"] for b in view["spine"]["beats"]}
    assert labels == {"Q1", "Q3"}


def test_load_external_resets_progress(example_home):
    seed_mod.save_progress({
        "completed": ["Q1", "Q2", "Q3"],
        "attempts": {"Q1": 1},
        "commits": [{"label": "Q1"}],
    })
    template = seed_mod.seed_template()["template"]
    view = seed_mod.load_external_seed(json_body=template, keep_progress=False)
    assert view["progress"]["completed"] == []
    assert view["spine"]["beats"][0]["label"] == "A1"
    assert (example_home / "mini_spine.json").is_file()


def test_seed_template_validates(example_home):
    payload = author_mod.seed_template()
    assert payload["schema"] == seed_mod.MINI_SPINE_SCHEMA
    assert payload["glossary"]
    seed_mod.parse_seed(payload["template"])
