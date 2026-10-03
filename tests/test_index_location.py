"""Scene location fill-forward into the search index + card location helpers."""

from __future__ import annotations

import json

import pytest

from story_editor import config, loader
from story_editor import structure as structure_mod
from story_editor.index import builder


def _msg(speaker: str, text: str, *, name: str | None = None) -> dict:
    return {
        "name": name or speaker,
        "is_user": speaker == "You",
        "mes": text,
        "send_date": 0,
        "extra": {},
    }


def test_fill_locations_from_scenes_inherits_onto_user_turns(tmp_path, monkeypatch) -> None:
    """Header-less user turns inherit the scene's place for index LIKE search."""
    log_path = tmp_path / "log.jsonl"
    rows_in = [
        _msg(
            "Ilse",
            "[ 🕰️ 10:00 PM | 🗓️ Thursday, November 16, 1843 AD | "
            "📍 Kettering - Ropewalk Lane, Yard | 👥 Ilse ]\n"
            "The garden is quiet.",
            name="Ilse",
        ),
        _msg("You", "I step closer to the trellis."),
        _msg("Ilse", "She watches you."),
        _msg(
            "Ilse",
            "[ 🕰️ 11:00 PM | 🗓️ Thursday, November 16, 1843 AD | "
            "📍 Kettering - The Salt Anchor | 👥 Ilse ]\n"
            "The tavern door swings.",
            name="Ilse",
        ),
        _msg("You", "I order ale."),
    ]
    log_path.write_text(
        "\n".join(json.dumps(r) for r in rows_in) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "SCENE_CARDS", tmp_path / "cards.json")
    log = loader.load(log_path)
    rows = [builder._row_for(m) for m in log.messages]

    # Before fill: user turns have no location.
    assert rows[0]["location"] and "Ropewalk" in rows[0]["location"]
    assert rows[1]["location"] is None
    assert rows[4]["location"] is None

    builder._fill_locations_from_scenes(log, rows)

    assert "Ropewalk" in (rows[1]["location"] or "")
    assert "Ropewalk" in (rows[2]["location"] or "")
    assert "Salt Anchor" in (rows[3]["location"] or "")
    assert "Salt Anchor" in (rows[4]["location"] or "")


def test_fill_prefers_card_location_and_aliases(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "log.jsonl"
    rows_in = [
        _msg(
            "Ilse",
            "[ 🕰️ 10:00 PM | 🗓️ Thursday, November 16, 1843 AD | "
            "📍 Some Alley | 👥 Ilse ]\nHi.",
            name="Ilse",
        ),
        _msg("You", "User turn."),
    ]
    log_path.write_text(
        "\n".join(json.dumps(r) for r in rows_in) + "\n", encoding="utf-8"
    )
    cards_path = tmp_path / "cards.json"
    cards_path.write_text(
        json.dumps(
            {
                "version": 2,
                "log": str(log_path.resolve()),
                "cards": {
                    "scene:0-1": {
                        "synopsis": "They meet in the alley.",
                        "location": "Kettering - Ropewalk Lane, Yard",
                        "location_aliases": ["Ropewalk", "No. 3 Ropewalk"],
                        "location_enriched": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SCENE_CARDS", cards_path)
    log = loader.load(log_path)
    rows = [builder._row_for(m) for m in log.messages]
    builder._fill_locations_from_scenes(log, rows)

    for row in rows:
        blob = row["location"] or ""
        assert "Ropewalk" in blob
        assert "No. 3 Ropewalk" in blob
        # Card location overwrites the sparse header ("Some Alley").
        assert "Some Alley" not in blob


def test_card_cache_migrates_v1_strings(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(
        json.dumps(
            _msg(
                "Ilse",
                "[ 🕰️ 10:00 PM | 🗓️ Thursday, November 16, 1843 AD | "
                "📍 The Harbour Office | 👥 Ilse ]\nWake.",
                name="Ilse",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    cards_path = tmp_path / "cards.json"
    cards_path.write_text(
        json.dumps(
            {
                "version": 1,
                "log": str(log_path.resolve()),
                "cards": {
                    "scene:0-0": "Wren wakes up.",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SCENE_CARDS", cards_path)
    log = loader.load(log_path)
    data = structure_mod._load_card_cache(log)
    assert data["version"] == 2
    card = data["cards"]["scene:0-0"]
    assert card["synopsis"] == "Wren wakes up."
    assert card["location_enriched"] is False


def test_msg_ids_from_card_locations(tmp_path, monkeypatch) -> None:
    cards_path = tmp_path / "cards.json"
    cards_path.write_text(
        json.dumps(
            {
                "version": 2,
                "log": "/tmp/x.jsonl",
                "cards": {
                    "scene:100-110": {
                        "synopsis": "Yard talk.",
                        "location": "Kettering - Ropewalk Lane, Yard",
                        "location_aliases": ["Ropewalk"],
                        "location_enriched": True,
                    },
                    "scene:200-205": {
                        "synopsis": "Tavern.",
                        "location": "Kettering - The Salt Anchor",
                        "location_aliases": ["Salt Anchor"],
                        "location_enriched": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SCENE_CARDS", cards_path)
    ids = structure_mod._msg_ids_from_card_locations(["Ropewalk"], per_scene=4)
    assert ids
    assert all(100 <= i <= 110 for i in ids)
    assert 200 not in ids


def test_failed_index_build_preserves_existing_database(tmp_path, monkeypatch) -> None:
    """Model/load failures must leave the last usable index untouched."""
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(json.dumps(_msg("Ilse", "A line.")) + "\n", encoding="utf-8")
    db_path = tmp_path / "log.index.sqlite3"
    original = b"previous usable index"
    db_path.write_bytes(original)
    monkeypatch.setattr(config, "SCENE_CARDS", tmp_path / "cards.json")

    class BrokenEmbedder:
        name = "broken-test-embedder"
        dim = 384

        def encode(self, texts):
            raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        builder.build(log_path, db_path=db_path, embedder=BrokenEmbedder())

    assert db_path.read_bytes() == original
    assert not list(tmp_path.glob(".*.building"))
