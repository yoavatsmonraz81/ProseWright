"""Import a Word document as manuscript front matter.

Bookends such as a prologue or an epilogue are documents filed on the novel
page — same paper as the rest of the book, but not log scenes and not
novelized from turns.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import manuscript as ms
from .identity import Anchor

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_TITLE_LINE = re.compile(r"^\d+\.\d+(?:\.\d+)?\s+\S")


def _style_names(styles_xml: bytes) -> dict[str, str]:
    if not styles_xml:
        return {}
    root = ET.fromstring(styles_xml)
    out: dict[str, str] = {}
    for style in root.findall(f".//{W}style"):
        sid = style.get(f"{W}styleId") or ""
        name_el = style.find(f"{W}name")
        name = name_el.get(f"{W}val") if name_el is not None else sid
        if sid:
            out[sid] = name or sid
    return out


def _on(el: ET.Element | None) -> bool:
    if el is None:
        return False
    return el.get(f"{W}val") not in ("0", "false", "off")


def _char_style_marks(styles_xml: bytes) -> dict[str, tuple[bool, bool]]:
    """styleId → (italic, bold) from the style's own run properties."""
    names = _style_names(styles_xml)
    if not styles_xml:
        return {}
    root = ET.fromstring(styles_xml)
    out: dict[str, tuple[bool, bool]] = {}
    for style in root.findall(f".//{W}style"):
        if style.get(f"{W}type") != "character":
            continue
        sid = style.get(f"{W}styleId") or ""
        rpr = style.find(f"{W}rPr")
        italic = _on(rpr.find(f"{W}i")) if rpr is not None else False
        bold = _on(rpr.find(f"{W}b")) if rpr is not None else False
        if sid:
            out[sid] = (italic, bold)
        name = names.get(sid, "").lower()
        if name == "emphasis":
            out[sid] = (True, bold)
        if name == "strong":
            out[sid] = (italic, True)
    return out


def _run_marks(run: ET.Element, char_styles: dict[str, tuple[bool, bool]]) -> tuple[bool, bool]:
    rpr = run.find(f"{W}rPr")
    italic = _on(rpr.find(f"{W}i")) if rpr is not None else False
    bold = _on(rpr.find(f"{W}b")) if rpr is not None else False
    if rpr is not None:
        rs = rpr.find(f"{W}rStyle")
        if rs is not None:
            sid = rs.get(f"{W}val") or ""
            si, sb = char_styles.get(sid, (False, False))
            italic = italic or si
            bold = bold or sb
    return italic, bold


def _run_text(run: ET.Element) -> str:
    parts: list[str] = []
    for child in run:
        tag = child.tag[len(W) :] if child.tag.startswith(W) else child.tag
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "br":
            parts.append("\n")
        elif tag == "tab":
            parts.append("\t")
    return "".join(parts)


def _runs_of(
    paragraph: ET.Element, char_styles: dict[str, tuple[bool, bool]]
) -> list[tuple[str, bool, bool]]:
    out: list[tuple[str, bool, bool]] = []
    for run in paragraph.findall(f"{W}r"):
        text = _run_text(run)
        if text:
            italic, bold = _run_marks(run, char_styles)
            out.append((text, italic, bold))
    for hyper in paragraph.findall(f"{W}hyperlink"):
        for run in hyper.findall(f"{W}r"):
            text = _run_text(run)
            if text:
                italic, bold = _run_marks(run, char_styles)
                out.append((text, italic, bold))
    return out


def _wrap_emphasis(runs: list[tuple[str, bool, bool]]) -> str:
    """Join Word runs, wrapping italic/bold spans for the novel page."""
    pieces: list[str] = []
    buf: list[str] = []
    marks: tuple[bool, bool] | None = None

    def flush() -> None:
        if not buf or marks is None:
            return
        text = "".join(buf)
        buf.clear()
        italic, bold = marks
        if (italic or bold) and text.strip():
            lead = len(text) - len(text.lstrip(" "))
            trail = len(text) - len(text.rstrip(" "))
            core = text[lead : len(text) - trail] if trail else text[lead:]
            if italic and bold:
                wrapped = f"***{core}***"
            elif bold:
                wrapped = f"**{core}**"
            else:
                wrapped = f"*{core}*"
            pieces.append(f"{' ' * lead}{wrapped}{' ' * trail}")
        else:
            pieces.append(text)

    for text, italic, bold in runs:
        key = (italic, bold)
        if marks is None or key != marks:
            flush()
            marks = key
        buf.append(text)
    flush()
    return "".join(pieces)


def _paragraph_style(paragraph: ET.Element, names: dict[str, str]) -> str:
    ppr = paragraph.find(f"{W}pPr")
    if ppr is None:
        return ""
    ps = ppr.find(f"{W}pStyle")
    if ps is None:
        return ""
    sid = ps.get(f"{W}val") or ""
    return names.get(sid, sid)


def blocks_from_docx(path: Path | str) -> list[ms.Block]:
    """Read a .docx into manuscript blocks. Headings, rules, and kickers kept."""
    src = Path(path)
    with zipfile.ZipFile(src) as zf:
        document = ET.fromstring(zf.read("word/document.xml"))
        styles = zf.read("word/styles.xml") if "word/styles.xml" in zf.namelist() else b""
    names = _style_names(styles)
    char_styles = _char_style_marks(styles)
    body = document.find(f"{W}body")
    if body is None:
        return []

    blocks: list[ms.Block] = []
    for paragraph in body.findall(f"{W}p"):
        style = _paragraph_style(paragraph, names)
        if style == "Horizontal Line":
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="break", text=""))
            continue
        raw_runs = _runs_of(paragraph, char_styles)
        if not raw_runs:
            continue
        heading = style.startswith("Heading")
        plain = "".join(t for t, _, _ in raw_runs).strip()
        text = plain if heading else _wrap_emphasis(raw_runs).strip()
        if not plain:
            continue
        if style == "Heading 2":
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="heading", text=plain, note="h2"))
        elif style == "Heading 3":
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="heading", text=plain, note="h3"))
        elif style == "Heading 4":
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="heading", text=plain, note="h4"))
        elif style in ("Subtitle", "Quote", "Intense Quote"):
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="para", text=plain, note="kicker"))
        elif style == "Signature":
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="para", text=text, note="sig"))
        elif _TITLE_LINE.match(plain) and len(plain) < 120 and not plain.endswith("."):
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="heading", text=plain, note="h4"))
        elif "\n" in text:
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="para", text=text, note="plain"))
        else:
            blocks.append(ms.Block(id=ms.mint_block_id(), kind="para", text=text))
    return blocks


def prologue_scene(path: Path | str, *, title: str | None = None) -> ms.Scene:
    """Approved, pinned front-matter scene from a Word document.

    The title defaults to the document's first heading, else "Prologue"."""
    src = Path(path)
    blocks = blocks_from_docx(src)
    if title is None:
        title = next((b.text for b in blocks if b.kind == "heading"), "Prologue")
    return ms.new_front_matter_scene(
        blocks=blocks,
        title=title,
        notes=f"Imported from {src.name}. Not in the log.",
        status="approved",
        model=f"imported/{src.name}",
        voice=ms.Voice(person="omniscient", tense="past"),
        pinned=True,
        start=-1,
        end=-1,
    )


def empty_source_signature() -> str:
    return ms.signature_of([])


def front_matter_anchor() -> Anchor:
    return Anchor(start=-1, end=-1)


def upsert_prologue(
    docx: Path | str, *, title: str | None = None, path: Path | None = None, log=None,
) -> ms.Scene:
    """Insert or replace the prologue bookend in the manuscript document."""
    scene = prologue_scene(docx, title=title)
    doc = ms.load(ms.MANUSCRIPT_LAYER, path=path, log=log)
    for existing in list(doc.scenes):
        if (
            ms.is_front_matter(existing)
            and existing.anchor.start == -1
            and existing.anchor.end == -1
        ):
            doc.remove(existing.id)
    doc.upsert(scene)
    ms.save(doc, path)
    return scene
