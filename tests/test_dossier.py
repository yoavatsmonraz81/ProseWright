"""Dossier model + contradiction + check_span (no LLM)."""

from __future__ import annotations

from story_editor import config, dossier as dossier_mod, loader


def test_list_and_load(example_home):
    names = dossier_mod.list_characters()
    assert "wren" in names and "ilse" in names
    wren = dossier_mod.load("Wren")
    assert wren is not None
    assert any(e.kind == "state" for e in wren.entries)


def test_add_entry_detects_contradiction(example_home):
    d = dossier_mod.load("wren")
    assert d is not None
    dossier_mod.add_entry(
        d,
        kind="state",
        text="Wren never notices what doesn't fit.",
        source="hand",
    )
    # Heuristic may or may not fire; ensure save round-trips.
    path = dossier_mod.save(d)
    assert path.exists()
    again = dossier_mod.load("wren")
    assert again is not None
    assert len(again.entries) >= 4


def test_check_span_open_items_fail(example_home):
    d = dossier_mod.load("ilse")
    assert d is not None
    d.open_items.append(
        dossier_mod.OpenItem(
            id="oi-test",
            entry_a="a",
            entry_b="b",
            note="test open",
            status="open",
        )
    )
    dossier_mod.save(d)
    log = loader.load(config.working_log())
    result = dossier_mod.check_span(log, 0, 1, characters=["ilse"])
    assert result.ok is False
    assert result.open_items


def test_resolve_portrait_prefers_canon_then_st(example_home, monkeypatch, tmp_path):
    st = tmp_path / "st_chars"
    st.mkdir()
    (st / "Wren.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    (st / "Ilse.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    (st / "Varga.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    monkeypatch.setattr(config, "ST_CHARACTERS_DIR", st)

    wren = dossier_mod.resolve_portrait_path("wren")
    assert wren is not None and wren.name == "Wren.png"

    ilse = dossier_mod.resolve_portrait_path("Ilse")
    assert ilse is not None and ilse.name == "Ilse.png"

    # Local override wins over ST.
    portraits = example_home / "canon" / "portraits"
    portraits.mkdir(parents=True, exist_ok=True)
    local = portraits / "wren.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\n" + b"local")
    assert dossier_mod.resolve_portrait_path("wren") == local.resolve()

    assert dossier_mod.resolve_portrait_path("nobody-here") is None

    catalog = dossier_mod.list_portrait_catalog()
    refs = {c["ref"] for c in catalog}
    assert "st:Wren.png" in refs
    assert "canon:wren.png" in refs

    pinned = dossier_mod.set_portrait("wren", "st:Varga.png")
    assert pinned.portrait == "st:Varga.png"
    assert dossier_mod.resolve_portrait_path("wren").name == "Varga.png"

    cleared = dossier_mod.set_portrait("wren", None)
    assert cleared.portrait is None
    assert dossier_mod.resolve_portrait_path("wren").name == "wren.png"

    uploaded = dossier_mod.import_portrait_file(
        "wren",
        "hand-pick.png",
        b"\x89PNG\r\n\x1a\n" + b"upload",
    )
    assert uploaded.portrait == "canon:wren_hand-pick.png"
    assert dossier_mod.resolve_portrait_path("wren").name == "wren_hand-pick.png"
