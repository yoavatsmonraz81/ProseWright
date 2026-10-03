"""Audit evidence packing — distinctive nouns must beat title/place noise."""

from __future__ import annotations

from story_editor import structure


def _beat(label: str, title: str, text: str) -> structure.AuthoredBeat:
    number = int("".join(ch for ch in label if ch.isdigit()) or "0")
    letter = "".join(ch for ch in label if ch.isalpha())
    return structure.AuthoredBeat(
        number=number,
        letter=letter,
        title=title,
        text=text,
        section="Chronological beats",
    )


def test_keyword_terms_put_the_seal_before_harbour_noise() -> None:
    beat = _beat(
        "2",
        "The broken seal (harbour office, night)",
        "**The broken seal:** under the lamp Ilse finds that the manifest's wax "
        "seal is a survey seal, its compass rose broken at the north point.",
    )
    terms = [t.lower() for t in structure._keyword_terms_for_beat(beat)]
    assert "broken seal" in terms
    assert "survey" in terms and "compass" in terms
    # Bold terms and names must outrank place words, which would otherwise fill the pack.
    assert terms.index("broken seal") < terms.index("harbour")
    assert terms.index("ilse") < terms.index("harbour")


def test_keyword_terms_put_a_bold_name_before_the_place() -> None:
    beat = _beat(
        "3",
        "The bell",
        "**The bell:** at the lighthouse gallery **Varga** makes Wren an offer "
        "while his men load the casks on the docks.",
    )
    terms = [t.lower() for t in structure._keyword_terms_for_beat(beat)]
    assert "varga" in terms
    if "docks" in terms:
        assert terms.index("varga") < terms.index("docks")
