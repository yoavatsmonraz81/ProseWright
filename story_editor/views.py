"""Read models for the editor GUI.

The engine keeps its knowledge in separate sidecars, each with a good reason to
be separate: the derived spine, the scene segmentation, voice attribution, the
manuscript. A pane in the GUI almost always needs several of
them joined — the page wants "this scene's turns, plus who was really
speaking, plus whether it has been novelized" — and
doing that join in the browser would mean shipping four sidecar formats to the
client and keeping them in step there.

So the join happens here, once, and the client receives one shape per pane. It
also means the payloads are plain functions over a `Log`, testable without an
HTTP server.

Nothing in this module writes. Panes that write call the engine's own modules
(`manuscript.save` and friends) through their routes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from . import (
    config,
    layers as layers_mod,
    loader,
    manuscript as manuscript_mod,
    structure as structure_mod,
    text as text_mod,
)

Log = loader.Log


# --------------------------------------------------------------------------- #
# Spine
# --------------------------------------------------------------------------- #

# What a beat's checkmark can mean. Derived from real coverage where possible,
# from the cached alignment where not — never guessed.
WRITTEN = "written"        # authored beat the log demonstrably realises
PARTIAL = "partial"        # aligned, but the auditor flagged drift
PLANNED = "planned"        # authored, absent from the log — expected, not a fault
UNKNOWN = "unknown"        # no alignment has been run yet
STALE = "stale"            # aligned to derived beats the committed spine lacks


def load_alignment() -> dict[str, Any] | None:
    """The cached authored-vs-derived audit, if one has been run."""
    path = config.SPINE_ALIGNMENT
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")) or None
    except json.JSONDecodeError:
        return None


def save_alignment(result: dict[str, Any], *, authored_count: int) -> dict[str, Any]:
    payload = {
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "log": str(config.working_log()),
        "authored_count": authored_count,
        "result": result,
    }
    config.SPINE_ALIGNMENT.parent.mkdir(parents=True, exist_ok=True)
    config.SPINE_ALIGNMENT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return payload


_LABEL = re.compile(r"^(?:beat\s*)?a?\s*(\d+\s*[a-z]?)\.?$", re.IGNORECASE)


def _norm_label(value: Any) -> str:
    """`A13a`, `beat 13a`, `13a.` → `13a`.

    The audit prompt prints the authored beats as `A13a` and asks for the bare
    label back, so a model that echoes the form it was shown used to miss every
    lookup and turn the entire pane "planned" — a total failure that looked like
    an ordinary empty plan. Reading both forms is cheaper than trusting one.
    """
    text = str(value or "").strip()
    match = _LABEL.match(text)
    if not match:
        return text
    return match.group(1).replace(" ", "").lower()


def _alignment_index(alignment: dict[str, Any] | None) -> tuple[dict, set, list, dict]:
    """(authored→derived ids, absent labels, drift notes, authored→beat verdict)."""
    if not alignment:
        return {}, set(), [], {}
    result = alignment.get("result") or {}
    by_label: dict[str, list[int]] = {}
    for row in result.get("alignment") or []:
        label = _norm_label(row.get("authored"))
        if not label or label.lower() == "none":
            continue
        try:
            derived_id = int(row.get("derived"))
        except (TypeError, ValueError):
            continue
        by_label.setdefault(label, []).append(derived_id)
    absent = {_norm_label(x) for x in (result.get("absent") or [])}
    verdicts: dict[str, dict[str, Any]] = {}
    for row in result.get("beats") or []:
        if not isinstance(row, dict):
            continue
        label = _norm_label(row.get("authored"))
        if not label:
            continue
        verdicts[label] = row
        # Only present/partial rows contribute derived links. Absent rows may
        # still list near-miss evidence; that must not mark the beat written.
        if str(row.get("verdict") or "").lower() not in ("present", "partial"):
            continue
        for did in row.get("derived") or []:
            try:
                derived_id = int(did)
            except (TypeError, ValueError):
                continue
            bucket = by_label.setdefault(label, [])
            if derived_id not in bucket:
                bucket.append(derived_id)
    return by_label, absent, list(result.get("drift") or []), verdicts


def spine_view(log: Log) -> dict[str, Any]:
    """The Spine tab: the authored plan first, coverage second.

    The authored spine is the writer's intent for the WHOLE story, so beats with
    no message range are the normal case, not missing data. Presenting the
    derived beats as the spine would quietly redefine "the plan" as "whatever
    got written", which is the one thing this pane must not do.
    """
    try:
        authored = structure_mod.parse_authored_spine()
    except (FileNotFoundError, OSError):
        authored = []
    derived = structure_mod.load_derived_spine(log)
    alignment = load_alignment()
    by_label, absent, drift_notes, verdicts = _alignment_index(alignment)

    derived_rows: list[dict[str, Any]] = []
    for beat in (derived.beats if derived else []):
        row = beat.to_json()
        row["span_label"] = beat.span_label()
        row["message_count"] = len(structure_mod.beat_message_list(log, beat))
        derived_rows.append(row)
    derived_by_id = {int(r["beat_id"]): r for r in derived_rows}

    authored_rows: list[dict[str, Any]] = []
    for beat in authored:
        realised = by_label.get(beat.label, [])
        spans = [derived_by_id[d] for d in realised if d in derived_by_id]
        # The audit can name derived beats a later spine no longer has — an audit
        # outliving the spine it judged. Reporting that as "planned" would call
        # written prose unwritten, so it gets its own status and keeps the ids.
        missing = [d for d in realised if d not in derived_by_id]
        verdict = verdicts.get(beat.label) or {}
        v_name = str(verdict.get("verdict") or "").lower()
        evidence = [
            int(x) for x in (verdict.get("evidence") or [])
            if str(x).lstrip("-").isdigit()
        ]
        # Trust the per-beat verdict first. Related-but-insufficient passages
        # still carry msg_ids on an "absent" row; those must not light the circle.
        if not alignment:
            status = UNKNOWN
        elif v_name == "absent" or (
            not v_name and beat.label in absent and not spans
        ):
            status = PLANNED
        elif v_name == "partial" or (
            spans and _mentions(drift_notes, beat.label)
        ):
            status = PARTIAL
        elif v_name == "present" or spans or (
            evidence and v_name != "absent"
        ):
            status = WRITTEN
        elif missing:
            status = STALE
        else:
            status = PLANNED
        # Prefer cited evidence msg range over the derived beat's full span.
        # Otherwise A8 (guild) and A9 (Ropewalk) both light as 557–624 because
        # they share one coarse derived Kettering beat — looks like a dual assignment
        # even when the audit cited different passages.
        if evidence and status in (WRITTEN, PARTIAL):
            start = min(evidence)
            end = max(evidence)
            message_count = end - start + 1
        else:
            start = min((s["start_msg_id"] for s in spans), default=None)
            end = max((s["end_msg_id"] for s in spans), default=None)
            message_count = sum(s["message_count"] for s in spans)
        row = beat.to_json()
        row.update({
            "status": status,
            "derived_beats": realised,
            "missing_derived": missing,
            "start": start,
            "end": end,
            "message_count": message_count,
            "evidence": evidence,
            "verdict_note": str(verdict.get("note") or ""),
        })
        authored_rows.append(row)

    # How far the committed spine reaches. Prose written after it was derived
    # belongs to no beat, which is why its authored beats look unrealised — the
    # pane says so rather than leaving the writer to infer it from a dash.
    last_mapped = max((r["end_msg_id"] for r in derived_rows), default=None)
    log_end = len(log) - 1 if len(log) else None
    tail = {
        "last_mapped": last_mapped,
        "log_end": log_end,
        "unmapped": (
            log_end - last_mapped
            if last_mapped is not None and log_end is not None and log_end > last_mapped
            else 0
        ),
        "derived_at": getattr(derived, "created", "") if derived else "",
    }

    # Propose-then-commit: a fresh derive lands here until the director promotes it.
    pending = structure_mod.load_pending_spine()
    pending_row: dict[str, Any] | None = None
    if pending is not None:
        cov = pending.coverage or {}
        pending_row = {
            "n_beats": len(pending.beats),
            "n_scenes": pending.n_scenes,
            "coverage_ok": bool(cov.get("ok")) if cov else None,
            "beats": [
                {
                    "beat_id": b.beat_id,
                    "title": b.title,
                    "span_label": b.span_label(),
                    "kind": b.kind or "",
                }
                for b in pending.beats
            ],
            "critique_notes": list(pending.critique_notes or []),
        }

    return {
        "authored": authored_rows,
        "derived": derived_rows,
        "alignment": alignment,
        "drift": drift_notes,
        "has_derived_spine": derived is not None,
        "unit_type": getattr(derived, "unit_type", "beat") if derived else "beat",
        "authority": getattr(derived, "authority", "derived") if derived else "derived",
        "coverage": (derived.coverage if derived else None),
        "pending": pending_row,
        "tail": tail,
        "counts": {
            "authored": len(authored_rows),
            "derived": len(derived_rows),
            "written": sum(1 for r in authored_rows if r["status"] == WRITTEN),
            "planned": sum(1 for r in authored_rows if r["status"] == PLANNED),
            "partial": sum(1 for r in authored_rows if r["status"] == PARTIAL),
            "stale": sum(1 for r in authored_rows if r["status"] == STALE),
        },
    }


def _mentions(notes: list[str], label: str) -> bool:
    """Whether a drift note names this authored beat (`A13a`, `beat 13a`)."""
    needles = (f"a{label}", f"beat {label}", f"authored {label}")
    return any(
        any(n in note.lower() for n in needles) for note in notes if isinstance(note, str)
    )


# --------------------------------------------------------------------------- #
# Scenes — the page's index
# --------------------------------------------------------------------------- #


def scenes_view(log: Log) -> dict[str, Any]:
    """Every scene in the log with the marks that decide how it is displayed:
    which beat it belongs to and whether it has been novelized."""
    scenes = structure_mod.segment_scenes(log)
    derived = structure_mod.load_derived_spine(log)
    beats = derived.beats if derived else []
    cards = _scene_card_cache()
    novel = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)

    rows: list[dict[str, Any]] = []
    front = [s for s in novel.scenes if manuscript_mod.is_front_matter(s)]
    for i, derived_scene in enumerate(front):
        rows.append({
            "scene_id": -1 - i,
            "kind": "frontmatter",
            "start": derived_scene.anchor.start,
            "end": derived_scene.anchor.end,
            "message_count": 0,
            "date": None,
            "time_start": None,
            "location": derived_scene.title or "front matter",
            "stage": None,
            "speakers": [],
            "synopsis": derived_scene.notes or "",
            "beat_id": None,
            "beat_title": "",
            "manuscript": {
                "id": derived_scene.id,
                "status": derived_scene.status,
            },
            "manuscript_parts": [{
                "id": derived_scene.id,
                "status": derived_scene.status,
                "start": derived_scene.anchor.start,
                "end": derived_scene.anchor.end,
            }],
        })
    for scene in scenes:
        beat = next(
            (
                b
                for b in beats
                if b.start_msg_id <= scene.start_msg_id <= b.end_msg_id
            ),
            None,
        )
        # A log scene can legitimately be partitioned into several manuscript
        # records (for example at a locked episode seam).  Older one-off
        # novelization tests can leave the same shape behind too.  Returning
        # only ``covering(start)`` made the browser silently hide every later
        # part, so S1 appeared to jump straight to S2 despite the assembled
        # manuscript containing all of its prose.
        manuscript_parts = []
        for candidate in novel.scenes:
            if manuscript_mod.is_front_matter(candidate):
                continue
            part_start, part_end = candidate.anchor.resolve(log)
            if part_end < scene.start_msg_id or part_start > scene.end_msg_id:
                continue
            manuscript_parts.append((part_start, part_end, candidate))
        manuscript_parts.sort(key=lambda item: (item[0], item[1], item[2].id))
        derived_scene = manuscript_parts[0][2] if manuscript_parts else None
        rows.append({
            "scene_id": scene.scene_id,
            "kind": scene.kind,
            "start": scene.start_msg_id,
            "end": scene.end_msg_id,
            "message_count": scene.msg_count,
            "date": scene.date_str,
            "time_start": scene.time_start,
            "location": scene.title(),
            "stage": getattr(scene, "stage", None),
            "speakers": scene.speakers[:6],
            "synopsis": cards.get(f"scene:{scene.start_msg_id}-{scene.end_msg_id}", ""),
            "beat_id": beat.beat_id if beat else None,
            "beat_title": beat.title if beat else "",
            "episode_id": (
                beat.beat_id
                if beat and getattr(derived, "unit_type", "beat") == "episode"
                else None
            ),
            "episode_title": (
                beat.title
                if beat and getattr(derived, "unit_type", "beat") == "episode"
                else ""
            ),
            "manuscript": (
                {"id": derived_scene.id, "status": derived_scene.status}
                if derived_scene
                else None
            ),
            "manuscript_parts": [
                {
                    "id": part.id,
                    "status": part.status,
                    "start": part_start,
                    "end": part_end,
                }
                for part_start, part_end, part in manuscript_parts
            ],
        })
    return {"scenes": rows, "count": len(rows)}


def _scene_card_cache() -> dict[str, str]:
    path = config.SCENE_CARDS
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return {}
    cards = data.get("cards") or {}
    out: dict[str, str] = {}
    for k, v in cards.items():
        if isinstance(v, dict):
            syn = v.get("synopsis")
            if isinstance(syn, str) and syn.strip():
                out[str(k)] = syn.strip()
        elif isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
    return out


# --------------------------------------------------------------------------- #
# The page itself
# --------------------------------------------------------------------------- #

MAX_SPAN = 400  # a page request is a scene or a beat, never the whole log


def log_span_view(log: Log, start: int, end: int) -> dict[str, Any]:
    """The turns to render, each carrying what the gutter needs to mark it.

    Annotation is per-message rather than per-character on purpose: a message
    has a uid, so a mark on it survives restyling and injection, while a
    character offset into prose the LLM may rewrite does not. The selection in
    the editor is therefore snapped to whole turns before anything is stored.
    """
    n = len(log)
    if not n:
        return {"messages": [], "start": 0, "end": 0, "truncated": False}
    start = max(0, min(start, n - 1))
    end = max(start, min(end, n - 1))
    truncated = end - start + 1 > MAX_SPAN
    if truncated:
        end = start + MAX_SPAN - 1

    novel = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)
    labels = _voice_labels(log)

    messages: list[dict[str, Any]] = []
    for m in log.messages[start : end + 1]:
        derived_scene = novel.covering(m.msg_id)
        label = labels.get(m.msg_id) or {}
        messages.append({
            "msg_id": m.msg_id,
            "uid": m.uid,
            "speaker": m.speaker,
            "role": m.role(),
            "text": m.text,
            "prose": text_mod.clean(m.text),
            "header": text_mod.strip_meta_blocks(m.text) != m.text,
            "interlude": bool(_interlude_flag(m)),
            "voice": label.get("voice") or "",
            "mode": label.get("mode") or "",
            "manuscript_scene": derived_scene.id if derived_scene else None,
        })
    return {
        "messages": messages,
        "start": start,
        "end": end,
        "total": n,
        "truncated": truncated,
    }


def _interlude_flag(message: loader.Message) -> bool:
    extra = message.raw.get("extra")
    if not isinstance(extra, dict):
        return False
    se = extra.get("story_editor")
    return bool(isinstance(se, dict) and se.get("interlude"))


def _voice_labels(log: Log) -> dict[int, dict[str, Any]]:
    """Voice attribution keyed by ordinal, or empty when unavailable.

    Imported lazily: `attribution` is the one sidecar module heavy enough that
    the server defers it, and a page render must not pay for it if the store is
    absent. The log is passed in because the store is uid-keyed on disk and
    needs a log to translate against — resolving it a second time here would
    re-read a 2.6 MB file per page.
    """
    try:
        from . import attribution as attr
    except ImportError:
        return {}
    try:
        store = attr.load_store(log=log)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    labels = (store or {}).get("labels") or {}
    out: dict[int, dict[str, Any]] = {}
    for key, value in labels.items():
        try:
            msg_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out[msg_id] = value
    return out
