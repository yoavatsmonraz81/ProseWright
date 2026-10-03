"""Ask pane — as_of resolution and grounded answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import ask, config, loader, structure
from story_editor.index import schema
from story_editor.index._sqlite_vec import load_into as load_sqlite_vec
from story_editor.index.search import search_keyword
import sqlite3


def _write_log(path: Path, texts: list[str]) -> loader.Log:
    rows = [json.dumps({"chat_metadata": {}})]
    for i, text in enumerate(texts):
        rows.append(
            json.dumps({
                "name": "Ilse" if i % 2 == 0 else "Wren",
                "is_user": False,
                "mes": text,
                "send_date": 0,
            })
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return loader.load(path)


def _fts_index(tmp_path: Path, log: loader.Log) -> Path:
    """Minimal messages + FTS index (no embeddings) for keyword / as_of tests."""
    db = tmp_path / "test.index.sqlite3"
    conn = sqlite3.connect(db)
    load_sqlite_vec(conn)
    conn.executescript(schema.DDL)
    conn.executescript(schema.FTS_TRIGGERS)
    for m in log.messages:
        conn.execute(
            "INSERT INTO messages "
            "(msg_id, speaker, role, is_interlude, stage, send_date, "
            " story_date_iso, story_date, story_time, location, text, text_clean) "
            "VALUES (?, ?, 'char', 0, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (m.msg_id, m.speaker, m.text, m.text),
        )
    conn.commit()
    conn.close()
    return db


def test_resolve_as_of_msg_and_empty(tmp_path) -> None:
    log = _write_log(tmp_path / "log.jsonl", ["early compass", "mid Varga", "late Anchor"])
    assert ask.resolve_as_of(None, log=log) is None
    assert ask.resolve_as_of("now", log=log) is None
    tip = ask.resolve_as_of(1, log=log)
    assert tip is not None and tip.msg_id == 1 and tip.label == "msg 1"
    tip2 = ask.resolve_as_of("msg:1", log=log)
    assert tip2 is not None and tip2.msg_id == 1


def test_resolve_as_of_derived_beat(tmp_path, monkeypatch) -> None:
    log = _write_log(
        tmp_path / "log.jsonl",
        ["a", "b", "c", "d", "e", "f"],
    )
    beat = structure.Beat(
        beat_id=2,
        title="Trap",
        justification="Varga",
        first_scene_id=0,
        last_scene_id=0,
        start_msg_id=1,
        end_msg_id=3,
    )
    proposal = structure.SpineProposal(
        log=str(log.path), beats=[beat], n_scenes=1, n_episodes=1,
    )
    derived_path = tmp_path / "derived.json"
    derived_path.write_text(json.dumps(proposal.to_json()), encoding="utf-8")
    monkeypatch.setattr(config, "DERIVED_SPINE", derived_path)

    tip = ask.resolve_as_of("D2", log=log)
    assert tip is not None
    assert tip.msg_id == 3
    assert "Trap" in tip.label

    tip2 = ask.resolve_as_of("beat 2", log=log)
    assert tip2 is not None and tip2.msg_id == 3


def test_keyword_search_respects_msg_id_to(tmp_path) -> None:
    log = _write_log(
        tmp_path / "log.jsonl",
        [
            "Wren finds a brass compass on the floor.",
            "They talk about weather.",
            "Varga is strapped to a chair in the dungeon.",
            "Later Varga names the ketch at the harbor.",
        ],
    )
    db = _fts_index(tmp_path, log)

    all_hits = search_keyword(db, "Varga", limit=10)
    assert {h.msg_id for h in all_hits} >= {2, 3}

    early = search_keyword(db, "Varga", limit=10, msg_id_to=1)
    assert all(h.msg_id <= 1 for h in early)
    assert 2 not in {h.msg_id for h in early}
    assert 3 not in {h.msg_id for h in early}

    mid = search_keyword(db, "Varga", limit=10, msg_id_to=2)
    assert {h.msg_id for h in mid} == {2}


def test_ask_grounds_answer_and_drops_future_citations(tmp_path, monkeypatch) -> None:
    log = _write_log(
        tmp_path / "log.jsonl",
        [
            "Wren finds a brass compass on the floor.",
            "Varga is strapped to a chair in the dungeon.",
            "The clerk sits like a heron at the Salt Anchor.",
        ],
    )
    db = _fts_index(tmp_path, log)
    monkeypatch.setattr(config, "index_db_for", lambda _p: db)

    def fake_chat(messages, **_kwargs):
        # Model tries to cite a future msg — must be stripped.
        return json.dumps({
            "answer": "Varga is captured in the dungeon.",
            "evidence": [1, 2],
        })

    monkeypatch.setattr(ask.llm, "chat", fake_chat)

    result = ask.ask(
        "Where is Varga?",
        log=log,
        as_of=1,
        mode="keyword",
    )
    assert result.as_of is not None and result.as_of.msg_id == 1
    assert "Varga" in result.answer
    assert all(c.msg_id <= 1 for c in result.citations)
    assert 2 not in {c.msg_id for c in result.citations}


def test_ask_rejects_unknown_as_of(tmp_path) -> None:
    log = _write_log(tmp_path / "log.jsonl", ["hi"])
    with pytest.raises(ValueError, match="unrecognised"):
        ask.resolve_as_of("tomorrow", log=log)


def test_excerpt_centres_on_content_noun_not_lead_in() -> None:
    from story_editor.index.search import Hit

    lead = "Varga rolls the cask up the gangway and nods to Wren. " * 8
    core = (
        "Wren reveals a pair of black leather gloves with rabbit lining "
        "from the parcel Ilse gifted her."
    )
    hit = Hit(
        msg_id=33,
        speaker="Wren",
        role="char",
        is_interlude=False,
        stage=None,
        story_date=None,
        story_time=None,
        location=None,
        text=lead + core,
        text_clean=lead + core,
    )
    excerpt = ask._excerpt(hit, ["gloves", "gifted", "color"])
    assert "black leather gloves" in excerpt
    assert excerpt.startswith("…") or "Varga" not in excerpt[:40]


def test_content_terms_strip_question_stopwords() -> None:
    terms = ask._ask_content_terms(
        "What is the color of Wren's gloves that Ilse gifted her?"
    )
    assert "gloves" in terms or "glove" in terms
    assert "gifted" in terms or "gift" in terms
    assert "color" in terms or "colour" in terms or "colored" in terms
    assert "what" not in terms and "the" not in terms and "her" not in terms


def test_glove_question_retrieves_gift_reveal(tmp_path) -> None:
    """Full-sentence FTS used to miss the reveal; content-term search must not."""
    log = _write_log(
        tmp_path / "log.jsonl",
        [
            "They talk about the color of the morning light on the fjord.",
            "Wren unties the parcel and reveals a pair of black leather gloves "
            "with rabbit lining that Ilse kept among the few gifts she packed "
            "with personal attention, mauve and teal colored ribbon.",
            "Later Wren peels the gloves off during an unrelated argument.",
            "Ilse feels jealousy about the color of Wren's cheeks.",
        ],
    )
    db = _fts_index(tmp_path, log)
    q = "What is the color of Wren's gloves that Ilse gifted her?"
    hits = ask.retrieve_for_ask(q, db_path=db, mode="keyword", limit=4)
    ids = [h.msg_id for h in hits]
    assert 1 in ids, ids
    # The gift reveal should outrank the cheek-color false friend.
    assert ids.index(1) < ids.index(3) if 3 in ids else True
