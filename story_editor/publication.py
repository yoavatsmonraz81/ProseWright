"""HTML, PDF, plain-text and AO3 publication exports built from the live manuscript."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from html import escape
from pathlib import Path

from markdown_it import MarkdownIt

from . import config, fonts, loader, manuscript as ms, novelize, structure as structure_mod


def chapters(log=None) -> list[dict[str, object]]:
    """Canonical chapters available to the selective exporter."""
    working_log = log if log is not None else loader.load(config.working_log())
    scenes = [s for s in ms.load(log=working_log).scenes if not ms.is_front_matter(s) and s.text()]
    return [
        {
            "id": beat.beat_id,
            "title": beat.title,
            "start": beat.start_msg_id,
            "end": beat.end_msg_id,
            # Whether the novel layer has prose for it yet: nothing to export otherwise.
            "has_text": any(beat.start_msg_id <= s.anchor.start <= beat.end_msg_id for s in scenes),
        }
        for beat in structure_mod.chapter_beats(working_log)
    ]


def html_document(
    chapter_ids: set[int] | None = None,
    *,
    title: str = "Selected chapters",
) -> str:
    """Render selected canonical chapters to the shared publication HTML."""
    log = loader.load(config.working_log())
    document = ms.load(log=log)
    markdown = novelize.assemble_private_markdown(
        document, log, chapter_ids=chapter_ids,
    )
    body = MarkdownIt("commonmark", {"html": True}).render(markdown)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    registry = fonts.load()
    page = registry.roles.get("page") or fonts.RoleSetting()
    family = (page.family or "Courier Prime").replace("\\", "\\\\").replace("'", "\\'")
    size = page.size or 16
    leading = page.line_height or 1.75
    measure = page.measure or 68
    tracking = page.letter_spacing or 0
    type_style = (
        f"--font-novel: '{family}', 'Courier Prime', monospace; "
        f"--publication-size: {size:g}px; "
        f"--publication-leading: {leading:g}; "
        f"--publication-measure: {measure:g}ch; "
        f"--publication-tracking: {tracking:g}em"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="generator" content="ProseWright publication export">
  <meta name="date" content="{escape(generated)}">
  <link rel="stylesheet" href="../assets/publication.css">
</head>
<body style="{escape(type_style, quote=True)}">
<main>
{body}
</main>
</body>
</html>
"""


def _chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("PDF export needs Google Chrome or Chromium on PATH")


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return stem or "selected"


def export_pdf(chapter_ids: set[int]) -> Path:
    """Render a chapter selection through Chromium and return the PDF path."""
    available = chapters()
    available_ids = {int(row["id"]) for row in available}
    unknown = chapter_ids - available_ids
    if unknown:
        raise ValueError(f"unknown chapter ids: {sorted(unknown)}")
    if not chapter_ids:
        raise ValueError("select at least one chapter")

    selected = [row for row in available if int(row["id"]) in chapter_ids]
    if len(selected) == len(available):
        label = "complete"
    elif len(selected) == 1:
        label = _safe_stem(str(selected[0]["title"]))
    else:
        label = f"chapters-{int(selected[0]['id']) + 1:02d}-{int(selected[-1]['id']) + 1:02d}"

    output_dir = Path(config.HOME_DIR) / "output" / "pdf"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{config.PROJECT_ID}-{label}.pdf"

    # Keep the temporary HTML in workspace so its ../assets and ../app font
    # references resolve exactly as they do in the persistent HTML export.
    workspace = Path(config.WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", prefix="publication-", dir=workspace,
        encoding="utf-8", delete=False,
    ) as handle:
        handle.write(html_document(chapter_ids))
        html_path = Path(handle.name)
    try:
        with tempfile.TemporaryDirectory(prefix="story-editor-chrome-") as profile:
            result = subprocess.run(
                [
                    _chrome(),
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--allow-file-access-from-files",
                    f"--user-data-dir={profile}",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={target}",
                    html_path.resolve().as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        if result.returncode != 0 or not target.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Chromium PDF render failed: {detail or result.returncode}")
    finally:
        html_path.unlink(missing_ok=True)
    return target


def _export_chapter_files(chapter_ids: set[int], kind: str, suffix: str, render) -> Path:
    """Write one file per selected chapter under ``output/<kind>`` and return what to download.

    A single chapter comes back as its own file; a larger selection as a
    ``.zip`` of the same files. ``render(document, log, row)`` returns the text.
    """
    available = chapters()
    available_ids = {int(row["id"]) for row in available}
    unknown = chapter_ids - available_ids
    if unknown:
        raise ValueError(f"unknown chapter ids: {sorted(unknown)}")
    if not chapter_ids:
        raise ValueError("select at least one chapter")

    log = loader.load(config.working_log())
    document = ms.load(log=log)
    output_dir = Path(config.HOME_DIR) / "output" / kind
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = [row for row in available if int(row["id"]) in chapter_ids]
    written: list[Path] = []
    for row in selected:
        chapter_id = int(row["id"])
        text = render(document, log, row)
        if not text.strip():
            raise ValueError(f"chapter {chapter_id + 1} ({row['title']}) has no manuscript text")
        target = output_dir / f"{chapter_id + 1:02d}-{_safe_stem(str(row['title']))}{suffix}"
        target.write_text(text, encoding="utf-8")
        written.append(target)
    if len(written) == 1:
        return written[0]

    if len(selected) == len(available):
        label = "complete"
    else:
        label = f"chapters-{int(selected[0]['id']) + 1:02d}-{int(selected[-1]['id']) + 1:02d}"
    archive = output_dir / f"{config.PROJECT_ID}-{label}-{kind}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in written:
            bundle.write(path, arcname=path.name)
    return archive


def export_txt(chapter_ids: set[int]) -> Path:
    """One plain-text file per selected chapter, in ``output/txt``."""
    return _export_chapter_files(
        chapter_ids, "txt", ".txt",
        lambda document, log, row: novelize.assemble_private_text(
            document, log, chapter_ids={int(row["id"])},
        ),
    )


_AO3_EMPHASIS = re.compile(r"\*\*([^*]+)\*\*|\*([^*\n]+)\*")
_AO3_SEPARATOR = re.compile(r"\s*-{3,}\s*")


def _ao3_inline(text: str) -> str:
    """Escape one paragraph and turn *italics* / **bold** into AO3's tags."""
    out, last = [], 0
    for match in _AO3_EMPHASIS.finditer(text):
        out.append(escape(text[last:match.start()], quote=False))
        if match.group(1) is not None:
            out.append(f"<strong>{escape(match.group(1), quote=False)}</strong>")
        else:
            out.append(f"<em>{escape(match.group(2), quote=False)}</em>")
        last = match.end()
    out.append(escape(text[last:], quote=False))
    return "".join(out)


def ao3_chapter_html(document: ms.Document, row: dict[str, object]) -> str:
    """The body of one chapter for AO3's HTML editor.

    AO3 takes a small tag subset and supplies its own chapter title and page,
    so this is paragraphs only: ``<p>`` per paragraph (a line break inside a
    manuscript paragraph starts a new one), ``<em>``/``<strong>`` for emphasis,
    and ``<hr />`` between scenes and for the manuscript's ``-----`` separators.
    """
    start, end = int(row["start"]), int(row["end"])
    scenes = sorted(
        (
            scene for scene in document.scenes
            if not ms.is_front_matter(scene)
            and start <= scene.anchor.start <= end
            and scene.text()
        ),
        key=lambda scene: (scene.anchor.start, scene.id),
    )
    parts: list[str] = []
    for scene in scenes:
        if parts and parts[-1] != "<hr />":
            parts.append("<hr />")
        for block in scene.blocks:
            for line in block.text.split("\n"):
                if not line.strip():
                    continue
                if _AO3_SEPARATOR.fullmatch(line):
                    if parts and parts[-1] != "<hr />":
                        parts.append("<hr />")
                    continue
                parts.append(f"<p>{_ao3_inline(line.strip())}</p>")
    while parts and parts[-1] == "<hr />":
        parts.pop()
    return "\n".join(parts) + ("\n" if parts else "")


def export_ao3(chapter_ids: set[int]) -> Path:
    """One AO3-ready HTML file per selected chapter, in ``output/ao3``."""
    return _export_chapter_files(
        chapter_ids, "ao3", ".html",
        lambda document, log, row: ao3_chapter_html(document, row),
    )
