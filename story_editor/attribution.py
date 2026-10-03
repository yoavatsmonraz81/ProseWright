"""Voice attribution sidecar — labels who was really voiced per ``msg_id``.

SillyTavern logs the card slot (``Wren`` / ``Narrator``), not the character being
played.  This module maintains a parallel JSON map keyed by message id so bibles,
sweep, and canon tools can reason about true voice without rewriting the log.

Label pipeline (cheapest first):
  1. ``auto``     — card defaults + content heuristics (no LLM)
  2. ``propose``  — LLM batch labels for ambiguous / unlabeled turns
  3. ``reconcile`` — auto → propose → auto-accept high-confidence LLM labels
  4. manual ``set`` — director overrides; ``reviewed: true`` is never clobbered
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config, identity, llm
from .canon_layer import _extract_json_object, is_scene_narrator_turn, scene_narrator_confidence
from .transform import foreign_quotes, is_composite_message

SCHEMA = "story-editor/voice-attribution@2"

# On disk the labels are keyed by stable message uid, so committing an interlude
# mid-log cannot slide 649 labels one message to the left. In memory they stay
# keyed by ordinal, which is what every caller in the engine already speaks —
# `load_store` translates in, `save_store` translates out. Legacy ordinal-keyed
# files load unchanged and are migrated the first time they are written.

# Card slot → default voice, for cards named differently from the character they
# carry. Story-specific; by default a card's voice is its own lowercased name.
_CARD_DEFAULT_VOICE: dict[str, str] = {}

# Bible draft target for a card-based speaker name (same idea, for `canon draft`).
_CARD_BIBLE_VOICE: dict[str, str] = {}

VALID_MODES = frozenset({
    "pov", "scene_narrator", "wrong_card", "mixed", "unknown",
})
VALID_SOURCES = frozenset({
    "default", "heuristic", "llm_proposed", "manual",
    "player_mode",  # written from the Player-mode engine's own record of each reply
})

_PROPOSE_BATCH_SIZE = 8
_CONTEXT_NEIGHBORS = 4

# Multi-character scene blocks — no single POV character.
ENSEMBLE_VOICE = "ensemble"


def named_cast_voices() -> frozenset[str]:
    """Voices eligible for reconcile auto-accept: the project's bible keys and
    one-word aliases, plus the narrator and the ensemble."""
    from . import canon_layer
    keys = {ENSEMBLE_VOICE, "narrator"}
    for key, bible in canon_layer.all_bibles().items():
        keys.add(key.lower())
        keys.update(a.strip().lower() for a in bible.aliases if a.strip() and " " not in a.strip())
    return frozenset(keys)

# Modes considered unambiguous enough for auto-accept after LLM propose.
AUTO_ACCEPT_MODES = frozenset({"wrong_card", "scene_narrator"})


@dataclass
class VoiceLabel:
    card: str
    voice: str
    mode: str = "pov"
    # ``voice`` identifies who the post performs (and remains the bible-filter
    # key). ``focal`` identifies whose perception bounds the narration. They
    # differ in scene-master posts, where one character speaks while another is
    # the observing consciousness.
    focal: str = ""
    addressees: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    source: str = "default"
    reviewed: bool = False
    confidence: float = 1.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "card": self.card,
            "voice": self.voice,
            "mode": self.mode,
            "source": self.source,
            "reviewed": self.reviewed,
            "confidence": round(float(self.confidence), 3),
        }
        if self.focal:
            out["focal"] = self.focal
        if self.addressees:
            out["addressees"] = self.addressees
        if self.segments:
            out["segments"] = self.segments
        if self.notes:
            out["notes"] = self.notes
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VoiceLabel":
        mode = str(d.get("mode") or "pov")
        if mode not in VALID_MODES:
            mode = "unknown"
        source = str(d.get("source") or "default")
        if source not in VALID_SOURCES:
            source = "default"
        segs = d.get("segments") or []
        if not isinstance(segs, list):
            segs = []
        addressees = d.get("addressees") or []
        if isinstance(addressees, str):
            addressees = [addressees]
        if not isinstance(addressees, list):
            addressees = []
        return cls(
            card=str(d.get("card") or ""),
            voice=str(d.get("voice") or "unknown"),
            mode=mode,
            focal=str(d.get("focal") or "").strip().lower(),
            addressees=[
                str(name).strip().lower() for name in addressees
                if str(name).strip()
            ],
            segments=[s for s in segs if isinstance(s, dict)],
            source=source,
            reviewed=bool(d.get("reviewed")),
            confidence=float(d.get("confidence", 1.0)),
            notes=str(d.get("notes") or ""),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def attribution_path() -> Path:
    return getattr(config, "VOICE_ATTRIBUTION", config.CANON_DIR / "voice_attribution.json")


def empty_store(*, log_name: str = "") -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "log": log_name,
        "note": (
            "Per-msg_id voice labels. ST card name ≠ character voiced. "
            "Run `canon attribute auto` then `canon attribute propose`; "
            "confirm edge cases with `canon attribute set`."
        ),
        "labels": {},
    }


def _resolve_log(log: Any | None):
    """The log a translation should be done against. Cheap enough to reload."""
    if log is not None:
        return log
    from . import loader

    try:
        return loader.load(config.working_log())
    except (FileNotFoundError, OSError):
        return None


def _is_uid_key(key: str) -> bool:
    return key.startswith(identity.UID_PREFIX)


def _labels_to_ordinals(labels: dict[str, Any], log) -> dict[str, Any]:
    """uid keys → ordinal keys. Ordinal keys (legacy files) pass through."""
    if log is None or not any(_is_uid_key(k) for k in labels):
        return labels
    out: dict[str, Any] = {}
    for key, value in labels.items():
        if not _is_uid_key(key):
            out[key] = value
            continue
        ordinal = log.ordinal_of(key)
        # A label whose message is gone keeps its uid key so the record survives
        # until the director decides what to do with it.
        out[str(ordinal) if ordinal is not None else key] = value
    return out


def _labels_to_uids(labels: dict[str, Any], log) -> dict[str, Any]:
    """ordinal keys → uid keys, for writing."""
    if log is None:
        return labels
    out: dict[str, Any] = {}
    for key, value in labels.items():
        if _is_uid_key(key):
            out[key] = value
            continue
        try:
            ordinal = int(key)
        except ValueError:
            out[key] = value
            continue
        uid = log.get(ordinal).uid if 0 <= ordinal < len(log) else ""
        out[uid or key] = value
    return out


def load_store(path: Path | None = None, *, log: Any | None = None) -> dict[str, Any]:
    p = path or attribution_path()
    if not p.exists():
        return empty_store()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_store()
    if not isinstance(data.get("labels"), dict):
        data["labels"] = {}
    # v2 only adds optional fields, so upgrading the marker is lossless.
    data["schema"] = SCHEMA
    data["labels"] = _labels_to_ordinals(data["labels"], _resolve_log(log))
    return data


def save_store(
    data: dict[str, Any],
    path: Path | None = None,
    *,
    log: Any | None = None,
) -> Path:
    p = path or attribution_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["labels"] = _labels_to_uids(payload.get("labels") or {}, _resolve_log(log))
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def prune_orphan_labels(data: dict[str, Any], log: Any) -> list[str]:
    """Remove labels that cannot resolve to a message in ``log``.

    ``load_store(..., log=log)`` translates live UID keys to ordinals and leaves
    missing UIDs untouched.  Consequently any non-live key at this point is an
    orphan from an earlier log identity, not a label for current prose.
    Returns the removed keys so callers can report/audit the cleanup.
    """
    labels = data.setdefault("labels", {})
    live = {str(i) for i in range(len(log))}
    orphaned = sorted(key for key in labels if key not in live)
    for key in orphaned:
        del labels[key]
    return orphaned


def get_label(store: dict[str, Any], msg_id: int) -> VoiceLabel | None:
    raw = (store.get("labels") or {}).get(str(msg_id))
    if not isinstance(raw, dict):
        return None
    return VoiceLabel.from_dict(raw)


def set_label(
    store: dict[str, Any],
    msg_id: int,
    label: VoiceLabel,
    *,
    force: bool = False,
) -> bool:
    """Write a label. Returns False if a reviewed label was preserved."""
    key = str(msg_id)
    labels = store.setdefault("labels", {})
    existing = labels.get(key)
    if (
        not force
        and isinstance(existing, dict)
        and existing.get("reviewed")
        and label.source != "manual"
    ):
        return False
    labels[key] = label.to_dict()
    return True


# ---------------------------------------------------------------------------
# Auto labeling (no LLM)
# ---------------------------------------------------------------------------

def _default_voice_for_card(card: str) -> str:
    return _CARD_DEFAULT_VOICE.get(card.strip().lower(), card.strip().lower() or "unknown")


def bible_voice_for_card(card: str) -> str:
    """Target ``voice`` value for a card-based ``canon draft SPEAKER``."""
    c = card.strip().lower()
    return _CARD_BIBLE_VOICE.get(c, c.replace("_v2", ""))


def auto_label_turn(turn: dict[str, Any]) -> VoiceLabel:
    """Derive a label from card metadata + content heuristics."""
    card = str(turn.get("name") or "")
    text = (turn.get("mes") or "").strip()
    voice = _default_voice_for_card(card)
    mode = "pov"
    source = "default"
    confidence = 0.9
    notes = ""

    sn_conf = scene_narrator_confidence(text, exclude=voice if turn.get("is_user") else None)
    if turn.get("is_user"):
        # The player's own card. Quoted lines from other characters, or a crowd
        # of them, mean the player is co-narrating; the true voice needs review.
        fq = foreign_quotes(text, voice)
        if fq:
            mode = "mixed"
            source = "heuristic"
            confidence = min(0.88, 0.55 + 0.07 * len(fq))
            notes = f"player POV with {len(fq)} foreign quoted line(s)"
        elif sn_conf > 0:
            mode = "mixed"
            source = "heuristic"
            confidence = sn_conf
            notes = "player card with heavy third-party presence — verify voice"
    else:
        # A character or narrator card: whole scenes with several voices, or one
        # voice with embedded dialogue from others.
        if sn_conf > 0:
            mode = "scene_narrator"
            voice = ENSEMBLE_VOICE
            source = "heuristic"
            confidence = sn_conf
            notes = "Multi-voice scene block — no single POV"
        elif is_composite_message(text, voice):
            fq = foreign_quotes(text, voice)
            mode = "mixed"
            source = "heuristic"
            confidence = min(0.88, 0.55 + 0.07 * max(1, len(fq)))
            notes = "character card with embedded dialogue from others"

    return VoiceLabel(
        card=card, voice=voice, mode=mode,
        focal=voice if mode == "pov" and voice != ENSEMBLE_VOICE else "",
        source=source, confidence=confidence, notes=notes,
    )


def needs_propose(label: VoiceLabel | None) -> bool:
    """True when auto/heuristic confidence is low or mode is ambiguous."""
    if label is None:
        return True
    if label.reviewed:
        return False
    if label.source == "manual":
        return False
    if label.mode in ("unknown", "wrong_card", "mixed"):
        return True
    if label.confidence < 0.85:
        return True
    return False


def auto_label_turns(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    only_unlabeled: bool = False,
    msg_ids: set[int] | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Apply auto labels. Returns stats dict."""
    stats = {"considered": 0, "written": 0, "skipped_reviewed": 0, "skipped_existing": 0}
    for turn in turns:
        mid = int(turn.get("msg_id", -1))
        if mid < 0:
            continue
        if msg_ids is not None and mid not in msg_ids:
            continue
        stats["considered"] += 1
        existing = get_label(store, mid)
        if only_unlabeled and existing is not None:
            stats["skipped_existing"] += 1
            continue
        if existing and existing.reviewed and not force:
            stats["skipped_reviewed"] += 1
            continue
        label = auto_label_turn(turn)
        if set_label(store, mid, label, force=force):
            stats["written"] += 1
    return stats


# ---------------------------------------------------------------------------
# LLM propose
# ---------------------------------------------------------------------------

_PROPOSE_SYSTEM = """\
You label roleplay log posts with who was REALLY being voiced.

SillyTavern records the CARD slot, not the character played.
The player sometimes uses their own card to voice other characters (wrong_card).
A narrator or character card often writes whole scenes (scene_narrator) with multiple voices.

Output ONLY a JSON object — no prose, no markdown fences.

Schema for each label:
{
  "labels": [
    {
      "msg_id": <int>,
      "card": "<ST card name>",
      "voice": "<character key OR ensemble>",
      "focal": "<whose perception bounds the narration, or empty>",
      "addressees": ["<character directly addressed by the primary speaker>"],
      "mode": "pov" | "scene_narrator" | "wrong_card" | "mixed" | "unknown",
      "segments": [{"voice": "...", "kind": "narration|dialogue", "addressees": ["..."], "hint": "..."}],
      "confidence": <0.0-1.0>,
      "notes": "<optional short note>"
    }
  ]
}

Voice keys are this story's cast: {voice_keys}, and **ensemble**.

Rules:
- ``voice: ensemble`` when the post voices multiple characters with no single POV owner.
  Use with ``mode: scene_narrator`` for multi-character scene blocks.
- ``voice`` is the PRIMARY character for single-voice posts (for bibles). Never use a mode name as voice.
- ``focal`` is whose perceptions bound the narration, even when another character does most of the speaking.
- ``addressees`` names who the primary speaker directly addresses; leave empty for pure narration.
- ``mode: wrong_card`` when a card voices someone other than its own character (voice = that character).
- ``mode: scene_narrator`` when one post stages a full scene (multiple speakers / POVs).
- ``mode: mixed`` when one card keeps a primary POV but embeds foreign dialogue.
- ``segments`` optional — per-block voices inside scene_narrator / mixed when separable.
- Use recent labeled neighbors for consistency.
- Be conservative on confidence when uncertain.\
"""


def _propose_system() -> str:
    cast = sorted(named_cast_voices() - {ENSEMBLE_VOICE})
    return _PROPOSE_SYSTEM.replace("{voice_keys}", ", ".join(cast) or "(no bibles yet)")


def _neighbor_context(
    turns_by_id: dict[int, dict[str, Any]],
    store: dict[str, Any],
    msg_id: int,
    *,
    window: int = _CONTEXT_NEIGHBORS,
) -> str:
    lines: list[str] = []
    for mid in range(max(0, msg_id - window), msg_id):
        turn = turns_by_id.get(mid)
        if not turn:
            continue
        lab = get_label(store, mid)
        preview = " ".join((turn.get("mes") or "").split())[:80]
        if lab:
            lines.append(
                f"  {mid}: card={lab.card} voice={lab.voice} mode={lab.mode} | {preview}"
            )
    return "\n".join(lines) if lines else "  (none)"


def _format_turn_block(turn: dict[str, Any], *, max_chars: int = 1200) -> str:
    mid = turn.get("msg_id", "?")
    card = turn.get("name", "?")
    role = "user" if turn.get("is_user") else "char"
    body = (turn.get("mes") or "").strip()
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return f"--- msg {mid} | card={card} | {role} ---\n{body}"


def propose_batch(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    msg_ids: list[int],
    *,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """LLM-label a batch of msg ids. Returns stats."""
    turns_by_id = {int(t["msg_id"]): t for t in turns if "msg_id" in t}
    batch_turns = [turns_by_id[mid] for mid in msg_ids if mid in turns_by_id]
    if not batch_turns:
        return {"requested": len(msg_ids), "parsed": 0, "written": 0}

    first_id = int(batch_turns[0]["msg_id"])
    context = _neighbor_context(turns_by_id, store, first_id)
    blocks = "\n\n".join(_format_turn_block(t) for t in batch_turns)

    user_prompt = f"""Recent labeled context (msg ids before this batch):
{context}

Label these posts (in order):
{blocks}

Return JSON: {{"labels": [...]}} with one entry per msg_id listed above."""

    if on_progress:
        on_progress(f"proposing labels for msgs {msg_ids[0]}–{msg_ids[-1]}…")

    raw = llm.chat(
        [{"role": "system", "content": _propose_system()},
         {"role": "user", "content": user_prompt}],
        task="attribution_propose",
        temperature=0.2,
        max_tokens=min(4000, 400 * len(batch_turns) + 800),
    )
    parsed = _extract_json_object(raw)
    if not parsed or not isinstance(parsed.get("labels"), list):
        return {"requested": len(msg_ids), "parsed": 0, "written": 0, "error": "no_json"}

    stats = {"requested": len(msg_ids), "parsed": 0, "written": 0, "skipped_reviewed": 0}
    for entry in parsed["labels"]:
        if not isinstance(entry, dict):
            continue
        try:
            mid = int(entry["msg_id"])
        except (KeyError, TypeError, ValueError):
            continue
        stats["parsed"] += 1
        turn = turns_by_id.get(mid)
        card = str(entry.get("card") or (turn or {}).get("name") or "")
        mode = str(entry.get("mode") or "unknown")
        if mode not in VALID_MODES:
            mode = "unknown"
        segs = entry.get("segments") or []
        if not isinstance(segs, list):
            segs = []
        addressees = entry.get("addressees") or []
        if isinstance(addressees, str):
            addressees = [addressees]
        if not isinstance(addressees, list):
            addressees = []
        label = VoiceLabel(
            card=card,
            voice=str(entry.get("voice") or "unknown").lower(),
            mode=mode,
            focal=str(entry.get("focal") or "").strip().lower(),
            addressees=[
                str(name).strip().lower() for name in addressees
                if str(name).strip()
            ],
            segments=[s for s in segs if isinstance(s, dict)],
            source="llm_proposed",
            reviewed=False,
            confidence=float(entry.get("confidence", 0.7)),
            notes=str(entry.get("notes") or ""),
        )
        if not set_label(store, mid, label):
            stats["skipped_reviewed"] += 1
        else:
            stats["written"] += 1
    return stats


def propose_turns(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    only_flagged: bool = True,
    only_unlabeled: bool = False,
    msg_ids: set[int] | None = None,
    batch_size: int = _PROPOSE_BATCH_SIZE,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Run LLM propose over eligible turns in log order."""
    targets: list[int] = []
    for turn in turns:
        mid = int(turn.get("msg_id", -1))
        if mid < 0:
            continue
        if msg_ids is not None and mid not in msg_ids:
            continue
        existing = get_label(store, mid)
        if only_unlabeled and existing is not None:
            continue
        if only_flagged and existing is None:
            # Heuristic first so propose focuses on hard cases.
            existing = auto_label_turn(turn)
            set_label(store, mid, existing)
            if not needs_propose(existing):
                continue
        elif only_flagged and existing is not None and not needs_propose(existing):
            continue
        elif existing and existing.reviewed:
            continue
        targets.append(mid)

    totals = {"batches": 0, "requested": len(targets), "parsed": 0, "written": 0}
    for i in range(0, len(targets), batch_size):
        batch = targets[i : i + batch_size]
        stats = propose_batch(turns, store, batch, on_progress=on_progress)
        totals["batches"] += 1
        totals["parsed"] += stats.get("parsed", 0)
        totals["written"] += stats.get("written", 0)
    return totals


# ---------------------------------------------------------------------------
# Reconcile (auto → propose → auto-accept → human review list)
# ---------------------------------------------------------------------------

def auto_accept_eligible(
    label: VoiceLabel | None,
    *,
    min_confidence: float,
) -> bool:
    """True if an LLM label can be auto-accepted without human review."""
    if label is None or label.reviewed:
        return False
    if label.source != "llm_proposed":
        return False
    if label.confidence < min_confidence:
        return False
    if label.mode not in AUTO_ACCEPT_MODES:
        return False
    if label.voice.lower() not in named_cast_voices():
        return False
    return True


def needs_human_review(
    label: VoiceLabel | None,
    *,
    min_confidence: float,
) -> bool:
    """True if a label should appear in the post-reconcile human review file."""
    if label is None:
        return True
    if label.reviewed:
        return False
    if label.mode in ("mixed", "unknown"):
        return True
    if label.mode == "scene_narrator":
        if label.voice.lower() == ENSEMBLE_VOICE:
            if label.source == "heuristic" and label.confidence >= 0.85:
                return False
            if (
                label.source == "llm_proposed"
                and label.confidence >= min_confidence
            ):
                return False
        if label.source == "heuristic":
            return True
    if label.source == "llm_proposed" and label.confidence < min_confidence:
        return True
    if needs_propose(label):
        return True
    return False


# ---------------------------------------------------------------------------
# Review queue filters (GUI / API)
# ---------------------------------------------------------------------------

REVIEW_PRESETS: dict[str, dict[str, Any]] = {
    "all": {},
    "focus": {"exclude_modes": ("scene_narrator",)},
    "player_turns": {"modes": ("wrong_card", "mixed", "unknown")},
    "scene_blocks": {"modes": ("scene_narrator",)},
}


@dataclass
class ReviewFilter:
    """Optional filters applied after ``needs_human_review``."""

    preset: str | None = None
    card: str | None = None
    modes: frozenset[str] | None = None
    exclude_modes: frozenset[str] | None = None
    voice: str | None = None
    label_confidence_min: float | None = None
    label_confidence_max: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.preset:
            out["preset"] = self.preset
        if self.card:
            out["card"] = self.card
        if self.modes:
            out["modes"] = sorted(self.modes)
        if self.exclude_modes:
            out["exclude_modes"] = sorted(self.exclude_modes)
        if self.voice:
            out["voice"] = self.voice
        if self.label_confidence_min is not None:
            out["label_confidence_min"] = self.label_confidence_min
        if self.label_confidence_max is not None:
            out["label_confidence_max"] = self.label_confidence_max
        return out


def _parse_modes_csv(raw: str | None) -> frozenset[str] | None:
    if not raw or not str(raw).strip():
        return None
    parts = {m.strip().lower() for m in str(raw).split(",") if m.strip()}
    valid = {m for m in parts if m in VALID_MODES}
    return frozenset(valid) if valid else None


def review_filter_from_mapping(data: dict[str, Any] | None) -> ReviewFilter:
    """Build a :class:`ReviewFilter` from API query dict or JSON body fields."""
    data = data or {}
    preset = str(data.get("preset") or "").strip().lower() or None
    if preset and preset not in REVIEW_PRESETS:
        preset = None

    card = str(data.get("card") or "").strip() or None
    modes = _parse_modes_csv(data.get("modes") or data.get("mode"))
    exclude_modes = _parse_modes_csv(
        data.get("exclude_modes") or data.get("exclude_mode"),
    )
    voice = str(data.get("voice") or "").strip().lower() or None

    def _opt_float(key: str) -> float | None:
        if key not in data or data.get(key) in (None, ""):
            return None
        return float(data[key])

    label_confidence_min = _opt_float("label_confidence_min")
    if label_confidence_min is None:
        label_confidence_min = _opt_float("confidence_min")
    label_confidence_max = _opt_float("label_confidence_max")
    if label_confidence_max is None:
        label_confidence_max = _opt_float("confidence_max")

    # Preset defaults, then explicit params override.
    if preset:
        p = REVIEW_PRESETS[preset]
        if not card and p.get("card"):
            card = str(p["card"])
        if modes is None and p.get("modes"):
            modes = frozenset(str(m) for m in p["modes"])
        if exclude_modes is None and p.get("exclude_modes"):
            exclude_modes = frozenset(str(m) for m in p["exclude_modes"])

    return ReviewFilter(
        preset=preset,
        card=card,
        modes=modes,
        exclude_modes=exclude_modes,
        voice=voice,
        label_confidence_min=label_confidence_min,
        label_confidence_max=label_confidence_max,
    )


def review_filter_from_query(query: dict[str, list[str]]) -> ReviewFilter:
    """Parse ``parse_qs`` output into a :class:`ReviewFilter`."""

    def first(key: str) -> str | None:
        vals = query.get(key)
        if not vals:
            return None
        return vals[0] if vals[0] != "" else None

    return review_filter_from_mapping({
        "preset": first("preset"),
        "card": first("card"),
        "modes": first("modes") or first("mode"),
        "exclude_modes": first("exclude_modes") or first("exclude_mode"),
        "voice": first("voice"),
        "label_confidence_min": first("label_confidence_min") or first("confidence_min"),
        "label_confidence_max": first("label_confidence_max") or first("confidence_max"),
    })


def label_matches_review_filter(
    lab: VoiceLabel | None,
    card: str,
    review_filter: ReviewFilter,
) -> bool:
    if lab is None:
        return False
    if review_filter.card:
        if card.strip().lower() != review_filter.card.strip().lower():
            return False
    if review_filter.modes and lab.mode not in review_filter.modes:
        return False
    if review_filter.exclude_modes and lab.mode in review_filter.exclude_modes:
        return False
    if review_filter.voice and lab.voice.lower() != review_filter.voice:
        return False
    conf = float(lab.confidence)
    if (
        review_filter.label_confidence_min is not None
        and conf < review_filter.label_confidence_min
    ):
        return False
    if (
        review_filter.label_confidence_max is not None
        and conf > review_filter.label_confidence_max
    ):
        return False
    return True


def build_review_payload(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    min_confidence: float = 0.95,
    review_filter: ReviewFilter | None = None,
) -> dict[str, Any]:
    """Items needing human review, optionally filtered for the GUI."""
    review_filter = review_filter or ReviewFilter()
    items: list[dict[str, Any]] = []
    total_needs_human = 0
    for turn in turns:
        mid = int(turn["msg_id"])
        lab = get_label(store, mid)
        if not needs_human_review(lab, min_confidence=min_confidence):
            continue
        total_needs_human += 1
        card = str(turn.get("name") or "")
        if not label_matches_review_filter(lab, card, review_filter):
            continue
        items.append({
            "msg_id": mid,
            "card": turn.get("name"),
            "message": turn.get("mes"),
            "label": lab.to_dict() if lab else None,
        })
    return {
        "ok": True,
        "count": len(items),
        "total_needs_human": total_needs_human,
        "min_confidence": min_confidence,
        "filters": review_filter.to_dict(),
        "store_path": str(attribution_path().resolve()),
        "items": items,
    }


def bulk_set_labels(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    msg_ids: list[int],
    *,
    voice: str,
    mode: str,
    reviewed: bool = True,
    notes: str = "",
) -> dict[str, int]:
    """Apply the same voice/mode to many msg ids. Returns stats."""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode}")
    by_id = {int(t["msg_id"]): t for t in turns if "msg_id" in t}
    applied = 0
    skipped = 0
    for mid in msg_ids:
        turn = by_id.get(int(mid))
        if turn is None:
            skipped += 1
            continue
        existing = get_label(store, int(mid))
        label = VoiceLabel(
            card=str(turn.get("name") or ""),
            voice=voice.strip().lower(),
            mode=mode,
            source="manual",
            reviewed=reviewed,
            confidence=1.0,
            notes=notes or (existing.notes if existing else ""),
            focal=existing.focal if existing else "",
            addressees=list(existing.addressees) if existing else [],
            segments=existing.segments if existing else [],
        )
        if set_label(store, int(mid), label, force=True):
            applied += 1
        else:
            skipped += 1
    return {"applied": applied, "skipped": skipped, "requested": len(msg_ids)}


def _card_matches_filter(card: str, card_filter: str | None) -> bool:
    if not card_filter:
        return True
    return card.lower() == card_filter.strip().lower()


def reconcile_turns(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    min_confidence: float = 0.95,
    skip_propose: bool = False,
    card_filter: str | None = None,
    msg_ids: set[int] | None = None,
    batch_size: int = _PROPOSE_BATCH_SIZE,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], list[int]]:
    """Run auto → propose → auto-accept; return stats and msg ids needing human review."""
    scoped = [
        t for t in turns
        if _card_matches_filter(str(t.get("name") or ""), card_filter)
        and (msg_ids is None or int(t.get("msg_id", -1)) in msg_ids)
    ]

    auto_stats = auto_label_turns(scoped, store, only_unlabeled=True)
    propose_stats: dict[str, int] = {
        "batches": 0, "requested": 0, "parsed": 0, "written": 0,
    }
    if not skip_propose:
        if on_progress:
            on_progress("proposing labels for remaining flagged turns…")
        propose_stats = propose_turns(
            scoped,
            store,
            only_flagged=True,
            batch_size=batch_size,
            on_progress=on_progress,
        )

    auto_accepted = 0
    for turn in scoped:
        mid = int(turn.get("msg_id", -1))
        if mid < 0:
            continue
        lab = get_label(store, mid)
        if not auto_accept_eligible(lab, min_confidence=min_confidence):
            continue
        accepted = VoiceLabel(
            card=lab.card,
            voice=lab.voice,
            mode=lab.mode,
            focal=lab.focal,
            addressees=list(lab.addressees),
            segments=list(lab.segments),
            source=lab.source,
            reviewed=True,
            confidence=lab.confidence,
            notes=lab.notes or "auto-accepted by reconcile",
        )
        if set_label(store, mid, accepted, force=True):
            auto_accepted += 1

    human_ids: list[int] = []
    for turn in scoped:
        mid = int(turn.get("msg_id", -1))
        if mid < 0:
            continue
        lab = get_label(store, mid)
        if needs_human_review(lab, min_confidence=min_confidence):
            human_ids.append(mid)

    stats = {
        "scoped": len(scoped),
        "auto_considered": auto_stats.get("considered", 0),
        "auto_written": auto_stats.get("written", 0),
        "propose_batches": propose_stats.get("batches", 0),
        "propose_requested": propose_stats.get("requested", 0),
        "propose_written": propose_stats.get("written", 0),
        "auto_accepted": auto_accepted,
        "needs_human": len(human_ids),
    }
    return stats, human_ids


def format_reconcile_review(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    msg_ids: list[int],
    *,
    preview_chars: int = 0,
) -> str:
    """Markdown list of turns that still need human attribution review."""
    by_id = {int(t.get("msg_id", -1)): t for t in turns}
    lines = [
        "# Attribution — needs human review",
        "",
        f"**{len(msg_ids)}** messages after reconcile auto-accept.",
        "",
        "Fix with:",
        "```bash",
        "python3 -m story_editor canon attribute set --msg-id N --voice NAME --mode MODE",
        "```",
        "",
        "---",
        "",
    ]
    for mid in sorted(msg_ids):
        turn = by_id.get(mid)
        lab = get_label(store, mid)
        if turn is None:
            continue
        card = str(turn.get("name") or "?")
        body = (turn.get("mes") or "").strip()
        lines.append(f"## msg {mid}")
        lines.append("")
        if lab is None:
            lines.append("- **status:** UNLABELED")
        else:
            lines.append(f"- **card:** {lab.card or card}")
            lines.append(f"- **voice:** {lab.voice}")
            lines.append(f"- **mode:** {lab.mode}")
            if lab.focal:
                lines.append(f"- **focal:** {lab.focal}")
            if lab.addressees:
                lines.append(f"- **addressees:** {', '.join(lab.addressees)}")
            lines.append(f"- **confidence:** {lab.confidence:.3f}")
            lines.append(f"- **source:** {lab.source}")
            lines.append(f"- **reviewed:** {lab.reviewed}")
            if lab.notes:
                lines.append(f"- **notes:** {lab.notes}")
        lines.append("")
        lines.append("**message:**")
        lines.append("")
        if preview_chars and preview_chars > 0:
            preview = _preview_text(body, preview_chars)
            lines.append(preview)
        else:
            lines.append("```")
            lines.append(body)
            lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Bible draft integration
# ---------------------------------------------------------------------------

def turn_counts_for_bible(
    turn: dict[str, Any],
    label: VoiceLabel | None,
    *,
    card_speaker: str,
    bible_voice: str,
) -> bool:
    """True if this turn should feed a bible draft for ``card_speaker``."""
    if label is None:
        return True  # unlabeled — caller may apply other filters
    if label.mode == "scene_narrator":
        return False
    if label.voice.lower() != bible_voice.lower():
        return False
    if label.mode == "wrong_card":
        return False
    return True


def filter_turns_for_bible(
    turns: list[dict[str, Any]],
    card_speaker: str,
    store: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop turns whose attribution disagrees with the card's bible voice."""
    bible_voice = bible_voice_for_card(card_speaker)
    kept: list[dict[str, Any]] = []
    skipped_no_label = 0
    skipped_voice = 0
    skipped_mode = 0
    for t in turns:
        mid = int(t.get("msg_id", -1))
        lab = get_label(store, mid)
        if lab is None:
            kept.append(t)
            continue
        if lab.mode == "scene_narrator":
            skipped_mode += 1
            continue
        if lab.mode == "wrong_card":
            skipped_mode += 1
            continue
        if lab.voice.lower() != bible_voice.lower():
            skipped_voice += 1
            continue
        kept.append(t)
    stats = {
        "input": len(turns),
        "kept": len(kept),
        "skipped_attribution_voice": skipped_voice,
        "skipped_attribution_mode": skipped_mode,
        "skipped_unlabeled_kept": skipped_no_label,
        "bible_voice": bible_voice,
    }
    return kept, stats


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _turn_is_flagged(lab: VoiceLabel | None) -> bool:
    if lab is None:
        return True
    return needs_propose(lab) or lab.mode in ("unknown", "wrong_card", "mixed", "scene_narrator")


def _iter_report_turns(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    only_flagged: bool = False,
    card_filter: str | None = None,
) -> list[tuple[dict[str, Any], VoiceLabel | None]]:
    card_l = (card_filter or "").strip().lower()
    out: list[tuple[dict[str, Any], VoiceLabel | None]] = []
    for turn in turns:
        card = str(turn.get("name") or "?")
        if card_l and card.lower() != card_l:
            continue
        mid = int(turn.get("msg_id", -1))
        lab = get_label(store, mid)
        if only_flagged and not _turn_is_flagged(lab):
            continue
        out.append((turn, lab))
    return out


def _preview_text(text: str, preview_chars: int | None) -> str:
    """Collapse whitespace for one-line preview; ``None`` or ``0`` = no limit."""
    collapsed = " ".join(text.split())
    if preview_chars is None or preview_chars <= 0:
        return collapsed
    if len(collapsed) > preview_chars:
        return collapsed[: preview_chars - 1] + "…"
    return collapsed


def format_report(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    only_flagged: bool = False,
    card_filter: str | None = None,
    preview_chars: int = 90,
) -> str:
    card_l = (card_filter or "").strip().lower()
    rows = _iter_report_turns(
        turns, store, only_flagged=only_flagged, card_filter=card_filter,
    )
    lines = [
        "# voice attribution report",
        f"# labeled: {len(store.get('labels', {}))} / {len(turns)} messages",
        f"# entries: {len(rows)}",
        "# [mode/voice] card @ confidence (source) — preview",
    ]
    if card_l:
        lines.append(f"# filter: card={card_filter}")
    if preview_chars <= 0:
        lines.append("# preview: full (single line, whitespace collapsed)")
    else:
        lines.append(f"# preview: {preview_chars} chars max")
    lines.append("")
    for turn, lab in rows:
        mid = int(turn.get("msg_id", -1))
        card = str(turn.get("name") or "?")
        body = (turn.get("mes") or "").strip()
        preview = _preview_text(body, preview_chars)
        if lab is None:
            lines.append(f"  {mid:4d}  [{'UNLABELED':18s}]  {card}  —  {preview}")
            continue
        tag = f"{lab.mode}/{lab.voice}"
        rev = " ✓" if lab.reviewed else ""
        lines.append(
            f"  {mid:4d}  [{tag:18s}]  {card} @ {lab.confidence:.2f} ({lab.source}){rev}  {preview}"
        )
    return "\n".join(lines) + "\n"


def format_review_report(
    turns: list[dict[str, Any]],
    store: dict[str, Any],
    *,
    only_flagged: bool = False,
    card_filter: str | None = None,
) -> str:
    """Full-message review document — one markdown section per post."""
    card_l = (card_filter or "").strip().lower()
    rows = _iter_report_turns(
        turns, store, only_flagged=only_flagged, card_filter=card_filter,
    )
    lines = [
        "# voice attribution — review (full messages)",
        "",
        f"Labeled **{len(store.get('labels', {}))}** / **{len(turns)}** messages in log.",
        f"**{len(rows)}** entries in this report.",
    ]
    if card_l:
        lines.append(f"Card filter: **{card_filter}**.")
    if only_flagged:
        lines.append("Showing flagged entries only (`scene_narrator`, `wrong_card`, `mixed`, low confidence).")
    lines.extend([
        "",
        "Fix with:",
        "```bash",
        "python3 -m story_editor canon attribute set --msg-id N --voice NAME --mode MODE",
        "```",
        "",
        "---",
        "",
    ])
    for turn, lab in rows:
        mid = int(turn.get("msg_id", -1))
        card = str(turn.get("name") or "?")
        body = (turn.get("mes") or "").strip()
        lines.append(f"## msg {mid}")
        lines.append("")
        if lab is None:
            lines.append("- **status:** UNLABELED")
        else:
            lines.append(f"- **card:** {lab.card or card}")
            lines.append(f"- **voice:** {lab.voice}")
            lines.append(f"- **mode:** {lab.mode}")
            lines.append(f"- **confidence:** {lab.confidence:.3f}")
            lines.append(f"- **source:** {lab.source}")
            lines.append(f"- **reviewed:** {lab.reviewed}")
            if lab.notes:
                lines.append(f"- **notes:** {lab.notes}")
            if lab.segments:
                lines.append(f"- **segments:** {json.dumps(lab.segments, ensure_ascii=False)}")
        lines.append("")
        lines.append("**message:**")
        lines.append("")
        lines.append("```")
        lines.append(body if body else "(empty)")
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def format_stats(store: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    labels = store.get("labels") or {}
    total = len(turns)
    live_ids = {str(turn.get("msg_id", "")) for turn in turns}
    labeled = sum(1 for mid in live_ids if isinstance(labels.get(mid), dict))
    orphaned = sum(1 for key in labels if key not in live_ids)
    by_mode: dict[str, int] = {}
    by_voice: dict[str, int] = {}
    by_source: dict[str, int] = {}
    needs_review = 0
    reviewed = 0
    for turn in turns:
        mid = str(turn.get("msg_id", ""))
        raw = labels.get(mid)
        if not isinstance(raw, dict):
            continue
        lab = VoiceLabel.from_dict(raw)
        by_mode[lab.mode] = by_mode.get(lab.mode, 0) + 1
        by_voice[lab.voice] = by_voice.get(lab.voice, 0) + 1
        by_source[lab.source] = by_source.get(lab.source, 0) + 1
        if lab.reviewed:
            reviewed += 1
        if needs_propose(lab):
            needs_review += 1
    lines = [
        f"messages: {total}  labeled: {labeled}  unlabeled: {total - labeled}",
        f"reviewed: {reviewed}  needs_review: {needs_review}",
        "",
        "by mode:",
    ]
    if orphaned:
        lines.insert(2, f"orphaned labels (not in current log): {orphaned}")
    for k, v in sorted(by_mode.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("by voice:")
    for k, v in sorted(by_voice.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("by source:")
    for k, v in sorted(by_source.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Who speaks where — the page filter's catalogue
# ---------------------------------------------------------------------------

def mouth_of(card: str, label: VoiceLabel | None, *, is_interlude: bool = False) -> str | None:
    """Whose single voice a turn is, or None for mixed / multi-voice / unknown turns.

    A reviewed or proposed label wins; otherwise the card's default voice.
    """
    if is_interlude:
        return None
    if label is not None:
        if label.mode in ("scene_narrator", "mixed"):
            return None
        voice = label.voice.strip().lower()
        if not voice or voice in ("unknown", ENSEMBLE_VOICE):
            return None
        return voice
    voice = _default_voice_for_card(card)
    return None if voice == "unknown" else voice


def voice_catalog(log) -> list[dict[str, Any]]:
    """Every single-voice turn in the log, grouped by voice, most frequent first."""
    store = load_store(log=log)
    index: dict[str, list[int]] = {}
    for msg in log.messages:
        extra = msg.raw.get("extra") or {}
        se = extra.get("story_editor") if isinstance(extra, dict) else None
        interlude = msg.is_system or bool(isinstance(se, dict) and se.get("interlude"))
        voice = mouth_of(msg.speaker, get_label(store, msg.msg_id), is_interlude=interlude)
        if voice:
            index.setdefault(voice, []).append(msg.msg_id)
    keys = sorted(index, key=lambda k: (-len(index[k]), k))
    return [{"voice": key, "count": len(index[key]), "ids": index[key]} for key in keys]
