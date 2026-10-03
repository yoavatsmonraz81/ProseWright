"""Typography persistence.

The interesting cases are all about what the server refuses: a font route that
serves any path it is handed is a file-read primitive, and a role that accepts
any string is a broken page later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from story_editor import config, fonts


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FONTS_DIR", tmp_path / "fonts")
    monkeypatch.setattr(config, "FONTS_JSON", tmp_path / "fonts" / "fonts.json")


def test_role_settings_round_trip() -> None:
    fonts.set_role("page", {"family": "Courier Prime", "size": 17, "measure": 72})
    cfg = fonts.load()
    assert cfg.roles["page"].family == "Courier Prime"
    assert cfg.roles["page"].size == 17
    assert cfg.roles["page"].measure == 72


def test_role_patch_merges_rather_than_replaces() -> None:
    fonts.set_role("page", {"family": "TT2020 Base", "size": 18})
    fonts.set_role("page", {"line_height": 1.9})
    page = fonts.load().roles["page"]
    assert page.family == "TT2020 Base"
    assert page.size == 18
    assert page.line_height == 1.9


def test_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        fonts.set_role("footnotes", {"family": "Courier Prime"})


def test_named_system_face_copies_no_file() -> None:
    """The whole point of naming a family: nothing is redistributed, so a
    personal-use face is fine to read with."""
    face = fonts.add_system_face("Traveling Typewriter", licence="personal use")
    assert face.kind == fonts.KIND_SYSTEM
    assert face.file == ""
    assert not (Path(config.FONTS_DIR) / "Traveling Typewriter").exists()
    assert fonts.load().face("traveling typewriter") is not None  # case-insensitive


def test_implausible_family_name_is_refused() -> None:
    with pytest.raises(ValueError):
        fonts.add_system_face("../../etc/passwd")
    with pytest.raises(ValueError):
        fonts.add_system_face("")


def test_import_stores_and_registers_a_file() -> None:
    face = fonts.import_file("Bohemian.ttf", b"\x00\x01ttf-bytes", licence="personal use")
    assert face.kind == fonts.KIND_IMPORTED
    assert (Path(config.FONTS_DIR) / "Bohemian.ttf").read_bytes().endswith(b"ttf-bytes")
    entry = next(f for f in fonts.registry()["faces"] if f["family"] == "Bohemian")
    assert entry["available"] is True
    assert entry["url"] == "/fonts/file/Bohemian.ttf"


def test_import_refuses_wrong_suffix_and_empty_payload() -> None:
    with pytest.raises(ValueError, match="unsupported font file"):
        fonts.import_file("payload.svg", b"data")
    with pytest.raises(ValueError, match="empty"):
        fonts.import_file("Face.ttf", b"")


def test_font_path_refuses_traversal() -> None:
    fonts.import_file("Face.ttf", b"bytes")
    assert fonts.font_path("Face.ttf") is not None
    assert fonts.font_path("../../../etc/passwd") is None
    assert fonts.font_path("nope.ttf") is None


def test_forgetting_a_face_clears_it_from_roles_but_keeps_the_file() -> None:
    fonts.import_file("Face.ttf", b"bytes")
    fonts.set_role("page", {"family": "Face"})
    assert fonts.remove_face("Face") is True
    assert fonts.load().roles["page"].family == ""
    # The workspace is the user's; a dropdown change does not delete their font.
    assert (Path(config.FONTS_DIR) / "Face.ttf").exists()
    assert fonts.remove_face("Face") is False


def test_registry_reports_a_missing_imported_file() -> None:
    fonts.import_file("Gone.ttf", b"bytes")
    (Path(config.FONTS_DIR) / "Gone.ttf").unlink()
    entry = next(f for f in fonts.registry()["faces"] if f["family"] == "Gone")
    assert entry["available"] is False
    assert entry["url"] == ""
