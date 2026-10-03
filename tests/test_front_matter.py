"""Front-matter import: a Word document filed as a bookend, not a log scene."""

from __future__ import annotations

import zipfile
from pathlib import Path

from story_editor import front_matter, manuscript as ms

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_STYLES = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles {_W}>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
  <w:style w:type="paragraph" w:styleId="H2"><w:name w:val="Heading 2"/></w:style>
  <w:style w:type="paragraph" w:styleId="HR"><w:name w:val="Horizontal Line"/></w:style>
  <w:style w:type="paragraph" w:styleId="Sub"><w:name w:val="Subtitle"/></w:style>
  <w:style w:type="paragraph" w:styleId="Sig"><w:name w:val="Signature"/></w:style>
</w:styles>"""


def _para(text: str, style: str | None = None, italic: bool = False) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    rpr = "<w:rPr><w:i/></w:rPr>" if italic else ""
    return f"<w:p>{ppr}<w:r>{rpr}<w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def _docx(path: Path) -> Path:
    body = "".join([
        _para("A Letter from the Survey Office", "H2"),
        _para("Kettering Point, the first of October, 1792", "Sub"),
        _para("HR-placeholder", "HR"),
        _para("The charts of the reef passages are sealed with a broken north point.")
        + _para("They are not to leave a pilot's hands.", italic=True),
        _para("— The Hydrographer", "Sig"),
    ])
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _STYLES)
    return path


def test_docx_import_is_a_filed_document(tmp_path) -> None:
    docx = _docx(tmp_path / "prologue.docx")
    blocks = front_matter.blocks_from_docx(docx)
    kinds = [b.kind for b in blocks]
    notes = [b.note for b in blocks]

    assert blocks[0].kind == "heading" and blocks[0].note == "h2"
    assert "Survey Office" in blocks[0].text
    assert "kicker" in notes
    assert "sig" in notes
    assert "break" in kinds
    assert any("*They are not to leave a pilot's hands.*" in b.text for b in blocks)
    assert all(b.src == [] for b in blocks)

    scene = front_matter.prologue_scene(docx)
    assert scene.title == "A Letter from the Survey Office"
    assert ms.is_front_matter(scene)
    assert scene.status == "approved"
    assert scene.pinned
    assert scene.source_sig == ms.signature_of([])
    assert scene.voice is not None and scene.voice.person == "omniscient"
