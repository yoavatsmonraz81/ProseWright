"""Gate bundle for Author Studio proposals (canon + criteria + dossier + lore)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import canon_layer, dossier as dossier_mod
from ..loader import Log
from .seed import MiniBeat
from .write_scene import WriteResult


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": list(self.failures),
            "notes": list(self.notes),
        }


def _proposed_blob(result: WriteResult) -> str:
    return "\n".join(e.after for e in result.edit_set.edits if e.after)


def _criteria_hits(blob: str, criteria: list[str]) -> list[str]:
    """Return criteria that look missing (shared heuristic — no LLM)."""
    from .criteria import criteria_hits

    _ok, missing = criteria_hits(blob, criteria)
    return missing


def run_gates(
    log: Log,
    beat: MiniBeat,
    write: WriteResult,
    *,
    characters: list[str] | None = None,
    skip_canon_llm: bool = False,
) -> GateResult:
    """Validate a proposed write_scene against the constraint stack.

    - Criteria token coverage on proposed text
    - Dossier open-item / never-clause notes (``dossier.check_span`` on
      proposed text treated as a virtual extension at ``after_msg_id``)
    - Optional per-speaker ``canon check`` on each inject (LLM)
    """
    failures: list[str] = []
    notes: list[str] = []
    blob = _proposed_blob(write)

    missing = _criteria_hits(blob, beat.criteria)
    for m in missing:
        failures.append(f"criteria not evidenced in draft: {m}")

    # Dossier gate: check open items at current frontier; scan proposed blob
    # by temporarily evaluating never-clauses against draft text.
    names = characters or list(beat.speakers)
    span = dossier_mod.check_span(
        log,
        max(0, write.after_msg_id - 2),
        write.after_msg_id,
        characters=names,
    )
    if not span.ok:
        for o in span.open_items:
            failures.append(f"dossier open item {o.id}: {o.note}")
    notes.extend(span.notes)

    # Soft never-clause against the draft itself
    for name in names:
        d = dossier_mod.load(name)
        if d is None:
            continue
        for e in dossier_mod.as_of(d, write.after_msg_id):
            m = re.search(r"\bnever\s+([^.!?]+)", e.text, re.I)
            if not m:
                continue
            frag = m.group(1).lower()
            toks = [t for t in re.findall(r"[a-z]{5,}", frag)][:2]
            low = blob.lower()
            if toks and all(t in low for t in toks) and f"not {toks[0]}" not in low:
                notes.append(
                    f"{name}: draft may violate dossier never-clause ({e.id})"
                )

    if not skip_canon_llm:
        for e in write.edit_set.edits:
            char = canon_layer.get_character(e.speaker)
            if not char:
                continue
            verdict = canon_layer.check_in_character("", e.after, e.speaker, char)
            v = str(verdict.get("verdict") or "unknown")
            if v == "out_of_character":
                failures.append(
                    f"canon[{e.speaker}]: {verdict.get('reason') or 'out of character'}"
                )
            elif v == "concerns":
                notes.append(
                    f"canon[{e.speaker}] concerns: {verdict.get('reason') or ''}"
                )

    # Lore pin sanity: if beat location is set, draft should mention a token
    if beat.location:
        loc_toks = [
            t for t in re.findall(r"[A-Za-z]{4,}", beat.location)
            if t.lower() not in {"the", "and", "room", "hall"}
        ]
        if loc_toks and not any(t.lower() in blob.lower() for t in loc_toks):
            notes.append(
                f"location '{beat.location}' not obviously present in draft"
            )

    return GateResult(ok=not failures, failures=failures, notes=notes)
