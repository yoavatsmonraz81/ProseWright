"""Propose a multi-turn scene from a spark via inject pending edits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import config, dossier as dossier_mod, layers as layers_mod, loader, transform as transform_mod
from ..loader import Log
from .spark import AuthorNote, live_dossier_blocks

ProgressFn = Callable[[dict], None]


@dataclass
class WriteResult:
    after_msg_id: int
    speakers: list[str]
    edit_set: transform_mod.EditSet
    note_preview: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "after_msg_id": self.after_msg_id,
            "speakers": list(self.speakers),
            "note_preview": self.note_preview[:400],
            "edit_set": self.edit_set.to_json(),
        }


def _header_prefix(note: AuthorNote) -> str:
    """Optional ST-style header for the first turn of a scene."""
    bits = []
    if note.location or True:
        # Always emit a location line so index/scene segmentation can latch on.
        loc = note.location or "Unspecified"
        bits.append(f"📍 {loc}")
    return f"[ {' | '.join(bits)} ]\n" if bits else ""


def write_scene(
    log: Log | str | Path,
    note: AuthorNote,
    *,
    after_msg_id: int | None = None,
    on_progress: ProgressFn | None = None,
) -> WriteResult:
    """Generate one inject edit per speaker; write pending edits (propose only).

    Injects share the same ``after_msg_id``. They are stored in reverse order so
    ``commit_edits`` (which inserts reverse-stable) yields narrative order.
    """
    layers_mod.check("inject", layers_mod.LOG)
    if not isinstance(log, Log):
        log = loader.load(log)
    if not len(log):
        raise ValueError("log is empty — seed at least one message before write_scene")

    after = after_msg_id if after_msg_id is not None else len(log) - 1
    log.get(after)  # bounds check

    speakers = list(note.speakers) or ["Narrator"]
    edits: list[transform_mod.Edit] = []
    # Re-read dossiers at write time so File-pane edits apply even if the spark
    # text (or a saved draft) was prepared earlier.
    dossier_blocks = live_dossier_blocks(speakers, as_of_msg=after)
    dossier_tail = ""
    if dossier_blocks:
        dossier_tail = (
            "\n\nBINDING DOSSIER (honour these facts for every turn):\n"
            + "\n".join(dossier_blocks)
        )

    for i, speaker in enumerate(speakers):
        if on_progress:
            on_progress({
                "kind": "phase",
                "text": f"Writing turn {i + 1}/{len(speakers)} as {speaker}…",
            })
        speaker_brief = ""
        d = dossier_mod.load(speaker)
        if d is not None:
            speaker_brief = dossier_mod.project_brief(d, after)
        must = list(note.must_land or [])
        must_block = ""
        if must:
            must_block = (
                "\nHARD REQUIREMENTS for the finished scene (every item must be "
                "observable somewhere across the turns — do not skip):\n"
                + "\n".join(f"  • {m}" for m in must)
            )
        turn_note = (
            f"{note.text}"
            f"{dossier_tail}"
            f"{must_block}\n\n"
            f"---\n"
            f"THIS TURN: write ONLY {speaker}'s passage "
            f"(turn {i + 1} of {len(speakers)}). "
            f"Do not write other speakers. Keep short (80–150 words). "
            f"Stay consistent with {speaker}'s dossier facts"
            + (f":\n{speaker_brief}" if speaker_brief else ".")
            + (
                " Advance at least one HARD REQUIREMENT that is not yet obvious "
                "on the page."
                if must
                else ""
            )
        )
        if i == 0:
            turn_note += (
                " Begin with a single header line matching: "
                f"{_header_prefix(note)!r} then the prose."
            )
        max_tokens, _ = transform_mod._inject_length_hint(turn_note)
        raw = transform_mod._llm_text(
            transform_mod.build_inject_messages(log, after, speaker, turn_note),
            task="inject",
            temperature=transform_mod.INJECT_TEMPERATURE,
            max_tokens=max_tokens,
            on_stream=on_progress,
        )
        body = transform_mod._clean_model_body(raw)
        if not body:
            raise ValueError(f"empty injection for speaker {speaker}")
        if i == 0 and note.location and "📍" not in body[:80]:
            body = _header_prefix(note) + body
        edits.append(
            transform_mod.Edit(
                msg_id=after,
                speaker=speaker,
                before="",
                after=body,
                flags=[],
                kind="inject",
            )
        )

    # Commit inserts same-msg_id injects preserving list order under a stable
    # reverse sort — reverse here so the first speaker lands first on disk.
    edits_for_commit = list(reversed(edits))
    edit_set = transform_mod.EditSet(
        log=str(Path(config.working_log()).resolve())
        if hasattr(config, "working_log")
        else str(log.path.resolve()),
        operator="author_write_scene",
        note=f"spark {note.beat_label}",
        locator=f"after msg {after} · {note.beat_label}",
        edits=edits_for_commit,
        layer=layers_mod.LOG,
    )
    # Prefer the log's own path when available.
    if getattr(log, "path", None):
        edit_set.log = str(Path(log.path).resolve())
    transform_mod.write_pending_edits(edit_set)
    return WriteResult(
        after_msg_id=after,
        speakers=speakers,
        edit_set=edit_set,
        note_preview=note.text,
    )
