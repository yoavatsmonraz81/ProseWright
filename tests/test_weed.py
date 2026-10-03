"""Weed pass: lexical scan, mechanical pulls, sentence stitch. No model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, layers, loader
from story_editor import weed as weed_mod
from story_editor.transform import resolve_span


HEADER = "[ 🕰️ 07:20 | 📅 Day 2 | 📍 The vault ]\n\n"

FIXTURE_WEEDS = {
    "phrases": [
        {
            "id": "load-bearing",
            "pattern": "load[- ]bearing(?! walls?\\b)",
            "family": "claude",
            "recipe": "drop_adj",
        },
        {
            "id": "load-bearing-way",
            "pattern": "in a load[- ]bearing way",
            "family": "claude",
            "recipe": "drop_span",
        },
        {
            "id": "worth-noting",
            "pattern": "it(?:['’]s| is) worth noting(?: that)?",
            "family": "claude",
            "recipe": "drop_lead_in",
        },
        {
            "id": "utilize",
            "pattern": "utiliz(?:e|es|ed|ing)",
            "family": "chatgpt",
            "recipe": "replace",
            "replace_map": {
                "utilize": "use",
                "utilizes": "uses",
                "utilized": "used",
                "utilizing": "using",
            },
        },
        {
            "id": "sit-with",
            "pattern": "sit(?:ting)? with (?:that|this|it)",
            "family": "claude",
            "recipe": "rewrite",
        },
    ]
}


def _write_log(path: Path, messages: list[tuple[str, str, str]]) -> loader.Log:
    lines = [json.dumps({"chat_metadata": {}})]
    for uid, speaker, text in messages:
        lines.append(json.dumps({
            "name": speaker,
            "mes": text,
            "is_user": speaker == "Wren",
            "extra": {"se_uid": uid},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return loader.load(path)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    weeds = tmp_path / "weeds.json"
    weeds.write_text(json.dumps(FIXTURE_WEEDS), encoding="utf-8")
    monkeypatch.setattr(config, "WEEDS", weeds)
    monkeypatch.setattr(config, "WEED_SCOPE", tmp_path / "weed_scope.json")
    monkeypatch.setattr(config, "PENDING_EDITS", tmp_path / "pending_edits.json")
    monkeypatch.setattr(config, "PENDING_EDITS_MD", tmp_path / "pending_edits.md")
    weed_mod._load_weeds.cache_clear()
    return tmp_path


def test_weed_runs_on_the_log(home: Path) -> None:
    assert layers.runs("weed", layers.LOG)
    assert layers.check_runs("weed", layers.LOG).name == "weed"


def test_default_scope_excludes_human_authored_turns(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", "The load-bearing assumption held."),
        ("m-b", "Wren", "The load-bearing assumption broke."),
    ])
    report = weed_mod.scan_log(log_path)
    assert [hit.msg_id for hit in report.hits] == [0]
    assert report.selected_sources == ["model:Ilse"]


def test_saved_scope_can_select_human_source(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", "The load-bearing assumption held."),
        ("m-b", "Wren", "The load-bearing assumption broke."),
    ])
    log = loader.load(log_path)
    selected = weed_mod.save_selected_sources(log, ["human:Wren"])
    assert selected == ["human:Wren"]
    report = weed_mod.scan_log(log_path)
    assert [hit.msg_id for hit in report.hits] == [1]
    catalog = {row["id"]: row for row in weed_mod.source_catalog(log)}
    assert catalog["human:Wren"]["selected"] is True
    assert catalog["model:Ilse"]["selected"] is False


def test_split_sentences_leaves_the_gap_between_them() -> None:
    text = "She watched. Mrs. Halvorsen did not. Enough."
    parts = weed_mod.split_sentences(text)
    rebuilt = ""
    pos = 0
    for start, end, sent in parts:
        rebuilt += text[pos:start] + sent
        assert sent == text[start:end]
        pos = end
    rebuilt += text[pos:]
    assert rebuilt == text
    assert len(parts) == 3


def test_apply_span_keeps_the_paragraph_break(home: Path) -> None:
    log_path = home / "log.jsonl"
    body = (
        "The load-bearing assumption sat between them.\n\n"
        '"You kept me out," she said.\n'
    )
    _write_log(log_path, [("m-a", "Ilse", HEADER + body)])
    log = loader.load(log_path)
    span = resolve_span(log, msg_from=0, msg_to=0)
    edit_set = weed_mod.apply_span(log_path, span)
    assert len(edit_set.edits) == 1
    after = edit_set.edits[0].after
    assert "load-bearing" not in after
    assert '\n\n"You kept me out,"' in after


def test_scan_finds_load_bearing_and_skips_bearing_down(home: Path) -> None:
    text = (
        "The load-bearing assumption sat between them. "
        "She was bearing down on the latch."
    )
    hits = weed_mod.scan_text(text)
    assert [h[3].id for h in hits] == ["load-bearing"]
    assert "bearing down" not in hits[0][2]


def test_scan_spares_a_literal_load_bearing_wall(home: Path) -> None:
    text = (
        "She spoke about load-bearing walls and water tables. "
        "The load-bearing fear was the one he would rely on."
    )
    hits = weed_mod.scan_text(text)
    assert [h[3].id for h in hits] == ["load-bearing"]
    assert "fear" in hits[0][2]


def test_scan_skips_details_blocks(home: Path) -> None:
    text = (
        "<details><summary>Plot</summary>This is load-bearing scaffolding.</details>\n"
        "She filed the number."
    )
    hits = weed_mod.scan_text(text)
    assert hits == []


def test_mechanical_drop_adj_keeps_the_noun(home: Path) -> None:
    weeds = weed_mod.load_weeds()
    load = next(w for w in weeds if w.id == "load-bearing")
    out = weed_mod.mechanical_pull(
        "The load-bearing assumption sat in the room.", load,
    )
    assert out == "The assumption sat in the room."


def test_mechanical_refuses_a_broken_tail(home: Path) -> None:
    weeds = weed_mod.load_weeds()
    load = next(w for w in weeds if w.id == "load-bearing")
    assert weed_mod.mechanical_pull("This buildup is load-bearing.", load) is None


def test_mechanical_drops_a_lead_in(home: Path) -> None:
    weeds = weed_mod.load_weeds()
    noting = next(w for w in weeds if w.id == "worth-noting")
    out = weed_mod.mechanical_pull(
        "It's worth noting that the door was black.", noting,
    )
    assert out == "The door was black."


def test_mechanical_swaps_utilize(home: Path) -> None:
    weeds = weed_mod.load_weeds()
    utilize = next(w for w in weeds if w.id == "utilize")
    out = weed_mod.mechanical_pull("She utilizes the key.", utilize)
    assert out == "She uses the key."


def test_apply_span_stitches_only_the_infected_sentence(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", HEADER + (
            "She watched the pulse in Wren's throat and did not look away. "
            "The load-bearing assumption sat between them. "
            "Enough."
        )),
        ("m-b", "Wren", "I looked at her."),
    ])
    log = loader.load(log_path)
    span = resolve_span(log, msg_from=0, msg_to=1)
    edit_set = weed_mod.apply_span(log_path, span)
    assert len(edit_set.edits) == 1
    after = edit_set.edits[0].after
    assert "load-bearing" not in after
    assert "watched the pulse" in after
    assert "Enough." in after
    assert edit_set.edits[0].before != after
    assert "I looked at her" not in after
    assert edit_set.meta["phrases"] == ["load-bearing"]
    assert edit_set.meta["mechanical"] == 1
    assert edit_set.operator == "weed"


def test_clean_span_proposes_nothing(home: Path) -> None:
    log_path = home / "log.jsonl"
    _write_log(log_path, [
        ("m-a", "Ilse", HEADER + "She watched and did not look away."),
    ])
    log = loader.load(log_path)
    span = resolve_span(log, msg_from=0, msg_to=0)
    edit_set = weed_mod.apply_span(log_path, span)
    assert edit_set.edits == []
    assert edit_set.meta["hit_count"] == 0
