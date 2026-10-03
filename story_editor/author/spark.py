"""Spark / gradient compiler — machine-built author's notes for write_scene."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import canon_layer, config, dossier as dossier_mod, lore as lore_mod
from ..loader import Log
from .seed import MiniBeat, MiniSpine

DRAFT_NAME = "author_spark_draft.json"


@dataclass
class AuthorNote:
    beat_label: str
    text: str
    pressures: list[str] = field(default_factory=list)
    forbids: list[str] = field(default_factory=list)
    must_land: list[str] = field(default_factory=list)
    location: str = ""
    speakers: list[str] = field(default_factory=list)
    dossier_blocks: list[str] = field(default_factory=list)
    lore_titles: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "beat_label": self.beat_label,
            "text": self.text,
            "pressures": list(self.pressures),
            "forbids": list(self.forbids),
            "must_land": list(self.must_land),
            "location": self.location,
            "speakers": list(self.speakers),
            "dossier_blocks": list(self.dossier_blocks),
            "lore_titles": list(self.lore_titles),
        }


def order_speakers(
    speakers: list[str] | None,
    first_speaker: str | None = None,
    *,
    fallback: list[str] | None = None,
) -> list[str]:
    """Return speaker turn order with ``first_speaker`` leading when set."""
    out = [str(s).strip() for s in (speakers or []) if str(s).strip()]
    if not out:
        out = [str(s).strip() for s in (fallback or []) if str(s).strip()]
    if not out:
        out = ["Narrator"]
    first = (first_speaker or "").strip()
    if not first:
        return out
    matched = next((s for s in out if s.lower() == first.lower()), first)
    rest = [s for s in out if s.lower() != matched.lower()]
    return [matched] + rest


def _rewrite_speakers_line(text: str, speakers: list[str]) -> str:
    """Keep a hand spark's SPEAKERS line aligned with turn order."""
    line = f"SPEAKERS (in order): {', '.join(speakers)}"
    updated, n = re.subn(
        r"(?m)^SPEAKERS \(in order\):.*$",
        line,
        text,
        count=1,
    )
    if n:
        return updated
    # Insert after LOCATION / STORY TIME block when missing.
    parts = text.split("\n", 1)
    if len(parts) == 1:
        return text + "\n" + line
    return parts[0] + "\n" + line + "\n" + parts[1]


def _bible_forbids(speakers: list[str], *, max_each: int = 4) -> list[str]:
    out: list[str] = []
    for name in speakers:
        char = canon_layer.get_character(name)
        if not char:
            continue
        for phrase in (char.forbidden_phrasings or [])[:max_each]:
            out.append(f"{name}: {phrase}")
    return out


def live_dossier_blocks(
    speakers: list[str],
    *,
    as_of_msg: int = 0,
) -> list[str]:
    """Fresh dossier briefs for the cast (re-read from disk every call)."""
    blocks: list[str] = []
    for name in speakers:
        d = dossier_mod.load(name)
        if d is None:
            continue
        block = dossier_mod.project_brief(d, as_of_msg)
        if block:
            blocks.append(block)
    return blocks


def ensure_dossier_section(
    text: str,
    speakers: list[str],
    *,
    as_of_msg: int = 0,
) -> tuple[str, list[str]]:
    """Ensure the spark body carries a *live* dossier section from disk.

    Hand drafts and Prepare-new-spark templates often omit dossiers, or keep a
    stale copy from before File-pane edits. Strip any existing dossier block and
    append a fresh one so Advance cannot silently ignore the case file.
    """
    blocks = live_dossier_blocks(speakers, as_of_msg=as_of_msg)
    body = (text or "").rstrip()
    if re.search(r"(?im)^CHARACTER DOSSIERS\b", body):
        body = re.sub(
            r"(?ims)^CHARACTER DOSSIERS[^\n]*\n.*?(?=^(?:LORE|PRIOR ATTEMPT FAILED|MUST LAND|PRESSURES|FORBIDS|DIRECTION|NOTES|Write a short)|\Z)",
            "",
            body,
            count=1,
        ).rstrip()
    # Orphan DOSSIER name: blocks left without the CHARACTER DOSSIERS header.
    body = re.sub(
        r"(?ims)(?:^|\n)DOSSIER\s+\S+[^\n]*\n(?:- \[[^\]]+\][^\n]*\n*)+",
        "\n",
        body,
    ).rstrip()
    if not blocks:
        return body, []
    section = (
        "CHARACTER DOSSIERS (binding case-file — stay consistent; "
        "these are established facts for the speakers):\n"
        + "\n".join(blocks)
    )
    return body + "\n\n" + section, blocks


def _lore_hits(beat: MiniBeat, spine: MiniSpine) -> list[lore_mod.LoreHit]:
    query = " ".join(
        [beat.title, beat.location, beat.notes, " ".join(beat.criteria)]
    )
    paths: list[Path] = []
    for raw in spine.lore_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = Path(config.HOME_DIR) / p
        if p.exists():
            paths.append(p)
    # Also scan worlds/ under HOME
    worlds = Path(config.HOME_DIR) / "worlds"
    if worlds.is_dir():
        paths.extend(sorted(worlds.glob("*.json")))
    if not paths:
        return []
    try:
        return lore_mod.find_relevant(query, paths)[:6]
    except Exception:  # noqa: BLE001
        return []


def compile_spark(
    beat: MiniBeat,
    *,
    spine: MiniSpine | None = None,
    log: Log | None = None,
    as_of_msg: int | None = None,
    failure_notes: list[str] | None = None,
    first_speaker: str | None = None,
    run: dict[str, Any] | None = None,
    step_k: int | None = None,
) -> AuthorNote:
    """Compile an author's-note spark from beat + dossiers + lore + bibles.

    Deterministic (no LLM). Failure notes from a prior gate are appended so
    retries steer away from the same mistakes. When ``run`` is set, injects
    DESTINATION / REMAINING / PRIOR LANDINGS trajectory context.
    """
    speakers = order_speakers(
        beat.speakers,
        first_speaker,
        fallback=list((spine.characters if spine else [])[:2]),
    )

    msg_ceiling = as_of_msg
    if msg_ceiling is None and log is not None and len(log):
        msg_ceiling = len(log) - 1
    if msg_ceiling is None:
        msg_ceiling = 0

    dossier_blocks = live_dossier_blocks(speakers, as_of_msg=msg_ceiling)

    lore_list = _lore_hits(beat, spine) if spine else []
    lore_titles = [h.entry.label() for h in lore_list]
    lore_lines = []
    for h in lore_list:
        body = " ".join((h.entry.content or "").split())
        if len(body) > 220:
            body = body[:220] + "…"
        title = h.entry.label()
        lore_lines.append(f"- {title}: {body}" if body else f"- {title}")

    forbids = _bible_forbids(speakers)
    must_land = list(beat.criteria)
    pressures = [
        f"Advance beat {beat.label}: {beat.title}",
        *([beat.notes] if beat.notes else []),
    ]

    N = int((run or {}).get("N") or 0) if run else 0
    k = int(step_k) if step_k else 0
    initial_state = list((run or {}).get("initial_state") or [])
    if not initial_state and spine is not None:
        initial_state = list(spine.initial_state or [])
    end_state = list((run or {}).get("end_state") or [])
    if not end_state and spine is not None:
        end_state = list(spine.end_state or [])
    residuals = list((run or {}).get("residuals") or end_state)
    prior_landings: list[str] = []
    if run:
        for ev in run.get("evidenced_end_state") or []:
            if ev.get("satisfied"):
                prior_landings.append(str(ev.get("condition") or ""))
        prior_landings.extend(str(x) for x in (run.get("carry_forward") or []) if x)
    is_final = bool(k and N and k >= N)

    parts: list[str] = [
        f"SCENE SPARK — {beat.label}: {beat.title}",
        f"LOCATION: {beat.location or '(unspecified)'}",
    ]
    if beat.story_date or beat.story_time:
        parts.append(
            f"STORY TIME: {beat.story_date or '?'} {beat.story_time or ''}".rstrip()
        )
    parts.append(f"SPEAKERS (in order): {', '.join(speakers)}")
    if k and N:
        parts.append(f"STEP: {k} of {N}" + (" (FINAL)" if is_final else ""))
    if initial_state:
        parts.append("INITIAL STATE (true at run start — do not contradict):")
        for c in initial_state:
            parts.append(f"  • {c}")
    if end_state:
        parts.append("DESTINATION (end_state for the full run):")
        for c in end_state:
            parts.append(f"  • {c}")
    if residuals:
        parts.append("REMAINING (not yet evidenced):")
        for c in residuals:
            parts.append(f"  • {c}")
    if prior_landings:
        parts.append("PRIOR LANDINGS (already evidenced — keep consistent):")
        for c in prior_landings:
            parts.append(f"  • {c}")
    parts.append("MUST LAND (observable — the scene fails without these):")
    for c in must_land:
        parts.append(f"  • {c}")
    if pressures:
        parts.append("PRESSURES:")
        for p in pressures:
            parts.append(f"  • {p}")
    if forbids:
        parts.append("FORBIDS (bible Tier-1 — do not write these):")
        for f in forbids[:10]:
            parts.append(f"  • {f}")
    if dossier_blocks:
        parts.append(
            "CHARACTER DOSSIERS (binding case-file — stay consistent; "
            "these are established facts for the speakers):"
        )
        parts.extend(dossier_blocks)
    if lore_lines:
        parts.append("LORE (relevant):")
        parts.extend(lore_lines)
    if failure_notes:
        parts.append("PRIOR ATTEMPT FAILED — fix these:")
        for n in failure_notes[:8]:
            parts.append(f"  • {n}")
    if is_final and residuals:
        parts.append(
            "Write a short multi-turn scene that lands every MUST LAND fact "
            "and satisfies every REMAINING destination condition. "
            "Match early-Victorian register. Keep it under ~400 words total across turns."
        )
    elif end_state and not is_final:
        parts.append(
            "Write a short multi-turn scene that lands every MUST LAND fact. "
            "Do NOT fully resolve the DESTINATION yet — leave REMAINING for later steps. "
            "Match early-Victorian register. Keep it under ~400 words total across turns."
        )
    else:
        parts.append(
            "Write a short multi-turn scene that lands every MUST LAND fact. "
            "Match early-Victorian register. Keep it under ~400 words total across turns. "
            "Do not resolve later beats."
        )

    return AuthorNote(
        beat_label=beat.label,
        text="\n".join(parts),
        pressures=pressures,
        forbids=forbids,
        must_land=must_land,
        location=beat.location,
        speakers=speakers,
        dossier_blocks=dossier_blocks,
        lore_titles=lore_titles,
    )


def manual_spark_template(
    beat: MiniBeat,
    *,
    first_speaker: str | None = None,
    as_of_msg: int = 0,
) -> AuthorNote:
    """Blank-ish hand-editable spark with the beat's known fields prefilled.

    Placeholders in angle brackets are for the director to replace. Criteria and
    staging from the mini-spine are filled so the template is already useful.
    Live dossier briefs are included so File-pane edits are visible to edit.
    """
    speakers = order_speakers(beat.speakers, first_speaker, fallback=["Narrator"])
    dossier_blocks = live_dossier_blocks(speakers, as_of_msg=as_of_msg)
    lines = [
        f"SCENE SPARK — {beat.label}: {beat.title}",
        f"LOCATION: {beat.location or '<where does this scene take place?>'}",
    ]
    if beat.story_date or beat.story_time:
        lines.append(
            f"STORY TIME: {beat.story_date or '?'} {beat.story_time or ''}".rstrip()
        )
    else:
        lines.append("STORY TIME: <date / time, or leave blank>")
    lines.append(f"SPEAKERS (in order): {', '.join(speakers)}")
    lines.append("")
    lines.append("MUST LAND (observable — the scene fails without these):")
    if beat.criteria:
        for c in beat.criteria:
            lines.append(f"  • {c}")
    else:
        lines.append("  • <concrete event that must appear on the page>")
        lines.append("  • <second observable fact>")
    lines.append("")
    lines.append("PRESSURES:")
    lines.append(f"  • Advance beat {beat.label}: {beat.title}")
    if beat.notes:
        lines.append(f"  • {beat.notes}")
    lines.append("  • <emotional or plot pressure driving this scene>")
    lines.append("")
    lines.append("DIRECTION (your hand — what should happen beat-by-beat):")
    lines.append("  • <opening image / staging>")
    lines.append("  • <turn 1 — who speaks or acts>")
    lines.append("  • <turn 2 — reply / complication>")
    lines.append("  • <closing beat — what has changed>")
    lines.append("")
    lines.append("FORBIDS:")
    lines.append("  • <facts or tones this scene must not invent>")
    lines.append("  • <optional: names / reveals reserved for later beats>")
    if dossier_blocks:
        lines.append("")
        lines.append(
            "CHARACTER DOSSIERS (binding case-file — stay consistent; "
            "these are established facts for the speakers):"
        )
        lines.extend(dossier_blocks)
    lines.append("")
    lines.append("NOTES:")
    lines.append("  • <optional director notes>")
    lines.append("")
    lines.append(
        "Write a short multi-turn scene that lands every MUST LAND fact. "
        "Honour every CHARACTER DOSSIERS fact for the speakers on stage. "
        "Match early-Victorian register. Keep it under ~400 words total across turns. "
        "Do not resolve later beats."
    )
    return AuthorNote(
        beat_label=beat.label,
        text="\n".join(lines),
        pressures=[f"Advance beat {beat.label}: {beat.title}"],
        forbids=[],
        must_land=list(beat.criteria),
        location=beat.location,
        speakers=speakers,
        dossier_blocks=dossier_blocks,
    )


def spark_from_text(
    beat: MiniBeat,
    text: str,
    *,
    failure_notes: list[str] | None = None,
    first_speaker: str | None = None,
    as_of_msg: int = 0,
) -> AuthorNote:
    """Wrap a hand-edited spark body as an AuthorNote for write_scene."""
    body = (text or "").strip()
    if not body:
        raise ValueError("spark text is empty")
    speakers = order_speakers(beat.speakers, first_speaker)
    body = _rewrite_speakers_line(body, speakers)
    body, dossier_blocks = ensure_dossier_section(
        body, speakers, as_of_msg=as_of_msg,
    )
    if failure_notes:
        body = (
            body
            + "\n\nPRIOR ATTEMPT FAILED — fix these:\n"
            + "\n".join(f"  • {n}" for n in failure_notes[:8])
        )
    return AuthorNote(
        beat_label=beat.label,
        text=body,
        pressures=[],
        forbids=[],
        must_land=list(beat.criteria),
        location=beat.location,
        speakers=speakers,
        dossier_blocks=dossier_blocks,
    )


def draft_path() -> Path:
    return Path(config.WORKSPACE_DIR) / DRAFT_NAME


def load_spark_draft() -> dict[str, Any] | None:
    p = draft_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not str(data.get("text") or "").strip():
        return None
    return data


def save_spark_draft(*, beat_label: str, text: str) -> Path:
    text = (text or "").strip()
    if not text:
        raise ValueError("spark text is empty")
    if not beat_label.strip():
        raise ValueError("beat_label required")
    p = draft_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "story-editor/author_spark_draft@1",
        "beat_label": beat_label.strip(),
        "text": text,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def clear_spark_draft() -> bool:
    p = draft_path()
    if p.exists():
        p.unlink(missing_ok=True)
        return True
    return False
