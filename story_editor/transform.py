"""Phase 2 - transform operators (propose, never commit).

Turn a plain-language note into a proposed rewrite of a SELECTED SPAN of existing
prose, returned as before/after diffs. Nothing is written until the director
approves (the same propose-then-approve discipline as interludes and spines).

The first operator is RESTYLE: hold the EVENTS fixed (actions, information
exchanged, outcome, who is present, the beat) and move only the emotional
register / tone / legible motivation - across a character's whole expressive
surface (dialogue, body language, expression, interiority), not dialogue alone.

Targeting is "narrative GPS": a beat (from the derived spine) or a scene locates
the region; a speaker/role filter narrows to whose lines change; an explicit
msg range is always available as the escape hatch. The header
(`[ time | date | location ]`) is structural metadata and is passed through
verbatim - only the prose body is ever rewritten.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import (
    backup as backup_mod,
    canon as canon_mod,
    canon_layer,
    config,
    layers as layers_mod,
    llm,
    loader,
    pauses,
    structure as structure_mod,
    text as text_mod,
)
from .loader import Log, Message

# How many neighbouring messages (each side) to show the model as read-only scene
# context so the rewrite stays continuous with what surrounds it.
CONTEXT_RADIUS = 4
CONTEXT_MSG_MAX_CHARS = 600

# Span-awareness: when restyling a multi-message span, feed each rewrite the
# rewrites already produced EARLIER in the same pass. This keeps the character's
# new voice consistent (and lets it escalate) while letting the model see - and
# avoid recycling - the images and phrasings it has already spent. Budgeted by
# characters, most-recent-first, so a long span doesn't blow the prompt.
PRIOR_REWRITE_BUDGET = 2600
PRIOR_REWRITE_MAX_CHARS = 700

# Restyle is a disciplined creative rewrite: warm enough to find new phrasing,
# cool enough not to wander off the events.
RESTYLE_TEMPERATURE = 0.7

# Reasoning models spend a large share of max_tokens on hidden thought before
# any prose lands in `content`. The old 4k cap routinely returned empty output.
# Floors assume a local box with headroom (e.g. 96 GB VRAM); override via
# STORY_EDITOR_REWRITE_MAX_TOKENS if needed.
_REWRITE_MAX_TOKENS_FLOOR = 8_192
_REWRITE_MAX_TOKENS_CAP = 16_384


def _rewrite_max_tokens(body: str) -> int:
    """Token budget for one restyle/retune message (reasoning + prose)."""
    override = os.environ.get("STORY_EDITOR_REWRITE_MAX_TOKENS", "").strip()
    if override.isdigit():
        return max(1024, int(override))
    # Body length is chars, not tokens — pad generously for thought + rewrite.
    return min(
        _REWRITE_MAX_TOKENS_CAP,
        max(_REWRITE_MAX_TOKENS_FLOOR, len(body) + 4_000),
    )


@dataclass
class SpanResult:
    messages: list[Message]
    locator: str  # human-readable description of how the span was resolved
    msg_from: int
    msg_to: int


def split_header(text: str) -> tuple[str, str]:
    """Split a message into (header, body). The header is the leading
    `[ ... ]` block plus the whitespace up to the prose, returned verbatim so a
    rewrite can re-prepend it byte-for-byte. Messages without a leading bracket
    (most user turns) return ("", text)."""
    m = pauses._HEADER_RE.match(text)
    if not m:
        return "", text
    end = m.end()
    # Absorb the whitespace/newlines between the header and the prose so the body
    # starts at the first non-space character.
    while end < len(text) and text[end] in " \t\r\n":
        end += 1
    return text[:end], text[end:]


def _beat_range(beat_id: int) -> tuple[int, int, str]:
    spine = structure_mod.load_derived_spine()
    if spine is None:
        raise ValueError(
            "no committed derived spine; run `structure derive` + `structure "
            "commit`, or target with --scene / --from/--to instead of --beat"
        )
    for b in spine.beats:
        if b.beat_id == beat_id:
            return b.start_msg_id, b.end_msg_id, f"beat B{beat_id} ({b.title})"
    raise ValueError(f"beat B{beat_id} not found in the committed spine")


def _scene_range(log: Log, scene_id: int) -> tuple[int, int, str]:
    scenes = structure_mod.segment_scenes(log)
    for s in scenes:
        if s.scene_id == scene_id:
            return s.start_msg_id, s.end_msg_id, f"scene S{scene_id} ({s.title()})"
    raise ValueError(f"scene S{scene_id} not found")


def resolve_span(
    log: Log,
    *,
    beat: int | None = None,
    scene: int | None = None,
    msg_from: int | None = None,
    msg_to: int | None = None,
    speaker: str | None = None,
    role: str | None = None,
) -> SpanResult:
    """Resolve targeting options to a concrete, ordered list of messages.

    A region locator (beat OR scene OR explicit from/to) sets the bounds; an
    optional speaker/role filter narrows to whose lines are in scope. Interludes
    are always excluded - they are the editor's own injected prose, not story
    turns to restyle."""
    locator_parts: list[str] = []

    if beat is not None:
        lo, hi, desc = _beat_range(beat)
        locator_parts.append(desc)
    elif scene is not None:
        lo, hi, desc = _scene_range(log, scene)
        locator_parts.append(desc)
    else:
        lo = 0 if msg_from is None else msg_from
        hi = (len(log) - 1) if msg_to is None else msg_to
        locator_parts.append(f"msgs {lo}–{hi}")

    # An explicit from/to further narrows a beat/scene region when both are given.
    if msg_from is not None:
        lo = max(lo, msg_from)
    if msg_to is not None:
        hi = min(hi, msg_to)

    selected: list[Message] = []
    for m in log.messages:
        if m.msg_id < lo or m.msg_id > hi:
            continue
        if _is_interlude(m):
            continue
        if speaker and m.speaker.lower() != speaker.lower():
            continue
        if role and m.role() != role:
            continue
        selected.append(m)

    if speaker:
        locator_parts.append(f"speaker={speaker}")
    if role:
        locator_parts.append(f"role={role}")

    return SpanResult(
        messages=selected,
        locator=", ".join(locator_parts),
        msg_from=lo,
        msg_to=hi,
    )


def _is_interlude(m: Message) -> bool:
    extra = m.raw.get("extra") or {}
    se = extra.get("story_editor") if isinstance(extra, dict) else None
    return bool(isinstance(se, dict) and se.get("interlude"))


def span_preview(span: SpanResult, width: int = 100) -> list[str]:
    """One readable line per targeted message, for the dry-run preview. Composite
    turns (those that also carry another character's dialogue) are flagged so the
    director knows a restyle there needs containment review."""
    out = []
    for m in span.messages:
        header, body = split_header(m.text)
        hint = "⟦hdr⟧ " if header else ""
        comp = "⟦mixed⟧ " if is_composite_message(m.text, m.speaker) else ""
        flat = " ".join(text_mod.clean(body).split())
        snippet = flat[:width] + ("…" if len(flat) > width else "")
        out.append(
            f"  msg {m.msg_id:>4} {m.role():<5} {m.speaker:<10} {comp}{hint}{snippet}"
        )
    return out


# --------------------------------------------------------------------------- #
# Composite-message containment: a single turn (especially a user-authored
# post) often co-narrates ANOTHER character's dialogue and reactions. Restyle
# must leave that foreign content alone. We detect foreign dialogue, protect it
# in the prompt, and verify afterwards that it survived - flagging any edit that
# disturbed it so it can't be committed unreviewed.
# --------------------------------------------------------------------------- #

# Dialogue tags that mark someone speaking, used to attribute a quote to a voice.
_SAID_VERBS = frozenset({
    "said", "says", "replied", "reply", "asked", "answered", "answers",
    "rasped", "whispered", "muttered", "murmured", "murmurs", "growled",
    "croaked", "breathed", "spoke", "called", "hissed", "cried", "snarled",
    "stated", "added", "continued", "gasped", "choked", "pleaded", "begged",
    "spat", "retorted", "demanded", "warned", "offered", "insisted",
})

# Cast gender map, used only to attribute dialogue tags ("he said" vs "she
# said"): the optional ``pronouns`` field of the project's character bibles
# ("she/her", "he/him"). Unknown names fall back to name-only attribution.
def _cast_by_pronoun() -> tuple[frozenset[str], frozenset[str]]:
    import json as _json
    female: set[str] = set()
    male: set[str] = set()
    path = getattr(config, "CHARACTERS_JSON", None)
    try:
        raw = _json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    except (OSError, ValueError):
        raw = {}
    for entry in (raw.get("characters") or {}).values():
        pron = str(entry.get("pronouns") or "").lower()
        names = {a.strip().lower() for a in entry.get("aliases") or [] if a.strip() and " " not in a.strip()}
        if pron.startswith("she"):
            female |= names
        elif pron.startswith("he"):
            male |= names
    return frozenset(female), frozenset(male)


_FEMALE_NAMES, _MALE_NAMES = _cast_by_pronoun()
_ALL_NAMES = _FEMALE_NAMES | _MALE_NAMES
_FEMALE_PRON = frozenset({"she", "her", "hers"})
_MALE_PRON = frozenset({"he", "him", "his"})

_QUOTE_RE = re.compile(r'"([^"]+)"|“([^”]+)”')


def _gender(name: str) -> str | None:
    n = name.strip().lower()
    if n in _FEMALE_NAMES:
        return "f"
    if n in _MALE_NAMES:
        return "m"
    return None


def _quotes(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in _QUOTE_RE.finditer(text):
        q = m.group(1) if m.group(1) is not None else m.group(2)
        out.append((q, m.start(), m.end()))
    return out


def _quote_is_foreign(text: str, start: int, end: int, target: str) -> bool:
    """Decide whether a quoted span belongs to someone OTHER than `target`, by
    attributing the dialogue tag adjacent to the quote. Conservative: only
    returns True when a said-verb has a clearly non-target subject, so legitimate
    target dialogue (the common case) is never flagged."""
    tg = _gender(target)
    tname = target.strip().lower()
    target_pron = _FEMALE_PRON if tg == "f" else _MALE_PRON if tg == "m" else frozenset()
    foreign_pron = _MALE_PRON if tg == "f" else _FEMALE_PRON if tg == "m" else frozenset()

    post = re.findall(r"[a-z']+", text[end : end + 70].lower())
    pre = re.findall(r"[a-z']+", text[max(0, start - 45) : start].lower())
    for window, before in ((post, True), (pre, False)):
        for i, tok in enumerate(window):
            if tok not in _SAID_VERBS:
                continue
            # The subject sits next to the verb: "he said" / "said Varga".
            neighbours = []
            if i - 1 >= 0:
                neighbours.append(window[i - 1])
            if i + 1 < len(window):
                neighbours.append(window[i + 1])
            for sub in neighbours:
                if sub == tname or sub in target_pron:
                    return False  # clearly the target speaking
                if sub in foreign_pron or (sub in _ALL_NAMES and sub != tname):
                    return True   # clearly someone else speaking
    return False


def foreign_quotes(text: str, target: str) -> list[str]:
    """The quoted lines in `text` that belong to a character other than target."""
    return [q for q, s, e in _quotes(text) if _quote_is_foreign(text, s, e, target)]


def is_composite_message(text: str, target: str) -> bool:
    """True if this turn carries another character's spoken dialogue (the signal
    that restyle could leak into content that isn't the target's)."""
    return bool(foreign_quotes(text, target))


def _norm(s: str) -> str:
    return " ".join(s.split())


def containment_flags(before: str, after: str, target: str) -> list[str]:
    """Compare a rewrite against the original for containment problems. Returns a
    list of human-readable flags (empty = clean). Two levels: a 'mixed' notice
    when the turn contains other-speaker dialogue at all, and a hard 'ALTERED'
    flag when a foreign line did not survive the rewrite verbatim."""
    fq = foreign_quotes(before, target)
    if not fq:
        return []
    after_norm = _norm(after)
    altered = [q for q in fq if _norm(q) not in after_norm]
    flags = [
        f"mixed: turn carries {len(fq)} other-speaker line(s); verify foreign "
        f"dialogue and reactions are untouched"
    ]
    for q in altered:
        snip = q[:50] + ("…" if len(q) > 50 else "")
        flags.append(f"ALTERED foreign line: \"{snip}\"")
    return flags


# --------------------------------------------------------------------------- #
# RESTYLE: rewrite the expressive surface, hold the events fixed.
# --------------------------------------------------------------------------- #

RESTYLE_CONTRACT = (
    "You are a literary line-editor performing a RESTYLE pass on one message of an "
    "existing prose roleplay. Your job is to change HOW the moment is rendered "
    "while leaving WHAT happens exactly as it was.\n\n"
    "HOLD FIXED (these must survive the rewrite unchanged):\n"
    "- Every event and action, in the same order, with the same outcome.\n"
    "- Every concrete fact: who is present, names, places, objects, what is said "
    "to whom in substance, any information revealed or decision made.\n"
    "- The beat's function in the scene - it ends where it began, pointed the same "
    "way. Do not resolve, escalate, or defuse anything the original left open.\n"
    "- Roughly the same length and density. Do not add a new paragraph of events.\n\n"
    "YOU MAY CHANGE (this is the whole point):\n"
    "- The character's emotional register, tone, and diction.\n"
    "- Their legible motivation and subtext - what the prose lets us feel is "
    "driving them - as long as the SAME thing still happens.\n"
    "- Their body language, expression, posture, and gesture.\n"
    "- Their interiority where the original already has it (do not invent a new "
    "interior monologue if there was none; recolour what is there).\n"
    "- Rhythm and phrasing.\n\n"
    "DO NOT invent new plot, new characters, new information, or new period-"
    "inaccurate detail. Do not change other characters' lines or reactions - only "
    "the targeted character's surface. Stay in period and in voice.\n\n"
    "MIRROR THE ORIGINAL'S FORMATTING EXACTLY: spoken words stay wrapped in double "
    "quotes; narration, action, and body language stay unquoted. If the original "
    "is a line of dialogue in quotes, your rewrite is also a line of dialogue in "
    "quotes. If it interleaves action and speech, keep that same interleaving.\n\n"
    "Output ONLY the rewritten prose body for the one target message - in the same "
    "form as the original. No preamble, no 'msg N:' label, no commentary, and do "
    "NOT wrap the entire message in an extra layer of quotation marks."
)


def _clean_ctx(text: str) -> str:
    body = text_mod.clean(split_header(text)[1])
    flat = " ".join(body.split())
    if len(flat) > CONTEXT_MSG_MAX_CHARS:
        flat = flat[:CONTEXT_MSG_MAX_CHARS].rstrip() + "…"
    return flat


def _context_block(log: Log, target: Message) -> str:
    """The neighbours around the target, cleaned and labelled, marking the line
    that is being rewritten so the model anchors to the surrounding scene."""
    lo = max(0, target.msg_id - CONTEXT_RADIUS)
    hi = min(len(log) - 1, target.msg_id + CONTEXT_RADIUS)
    lines = []
    for m in log.messages[lo : hi + 1]:
        mark = " <<< THE LINE YOU ARE REWRITING" if m.msg_id == target.msg_id else ""
        lines.append(f"[msg {m.msg_id} · {m.speaker}]{mark}\n{_clean_ctx(m.text)}")
    return "\n\n".join(lines)


def _prior_block(prior_rewrites: list[tuple[int, str]]) -> str:
    """Render the rewrites already produced earlier in this pass, most-recent-
    first within a character budget, then shown in chronological order. Empty
    string if there are none yet."""
    if not prior_rewrites:
        return ""
    chosen: list[tuple[int, str]] = []
    used = 0
    for mid, body in reversed(prior_rewrites):
        body = split_header(body)[1].strip()
        if len(body) > PRIOR_REWRITE_MAX_CHARS:
            body = body[:PRIOR_REWRITE_MAX_CHARS].rstrip() + "…"
        if used + len(body) > PRIOR_REWRITE_BUDGET and chosen:
            break
        chosen.append((mid, body))
        used += len(body)
    chosen.reverse()
    lines = [
        "ALREADY RESTYLED IN THIS PASS — these are YOUR OWN earlier rewrites of "
        "this same character in this same run. Two jobs: (1) keep the SAME "
        "characterisation and emotional through-line you have established here, and "
        "let it ESCALATE naturally where the events escalate; (2) do NOT recycle the "
        "distinctive images, metaphors, gestures, or turns of phrase you already "
        "used below — reach for FRESH ones so the turns don't read as samey:",
    ]
    for mid, body in chosen:
        lines.append(f"\n[your rewrite of msg {mid}]\n{body}")
    return "\n".join(lines)


def build_restyle_messages(
    log: Log,
    target: Message,
    note: str,
    prior_rewrites: list[tuple[int, str]] | None = None,
) -> list[dict]:
    system = "\n\n".join([RESTYLE_CONTRACT, canon_mod.story_primer()])
    body = split_header(target.text)[1]
    parts = [
        f"DIRECTION — the change to make: {note}",
    ]

    # Phase 5: inject character canon block if we have a bible for the speaker
    char = canon_layer.get_character(target.speaker)
    if char:
        parts.append(canon_layer.render_canon_block(char))

    # Phase 5: inject beat context (narrative GPS)
    beat_ctx = canon_layer.beat_context_block(target.msg_id)
    if beat_ctx:
        parts.append(beat_ctx)

    parts.append(
        f"SCENE CONTEXT (read-only, so you understand the moment — DO NOT rewrite "
        f"these, and do not let the other characters' lines change):\n\n"
        f"{_context_block(log, target)}"
    )
    prior_block = _prior_block(prior_rewrites or [])
    if prior_block:
        parts.append(prior_block)
    fq = foreign_quotes(body, target.speaker)
    if fq:
        quoted = "\n".join(f'  - "{q}"' for q in fq)
        parts.append(
            f"PROTECTED CONTENT — this turn also narrates OTHER characters speaking "
            f"and reacting. Restyle ONLY {target.speaker}'s own speech, body, and "
            f"interiority. Reproduce the following line(s) EXACTLY, word for word, "
            f"and likewise leave untouched any narration that describes another "
            f"character's actions, reactions, or inner state:\n{quoted}"
        )
    parts.append(
        f"THE LINE TO RESTYLE — this is {target.speaker}'s turn (msg "
        f"{target.msg_id}). Rewrite ONLY this body, applying the direction above "
        f"while holding every event and fact fixed. Output only the rewritten "
        f"prose:\n\n{body}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]


def _clean_model_body(raw: str, original: str = "") -> str:
    """Strip the wrappers a model sometimes adds: fenced blocks, a leading
    'msg N:' / speaker label, and an EXTRA layer of quotes the model wrapped the
    whole message in. The quote-strip is original-aware: a line that is itself a
    quoted dialogue line keeps its quotes (we only peel a wrapper the original
    didn't have)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1 and " " not in s[:nl]:  # drop a language tag line
            s = s[nl + 1 :]
        s = s.strip()
    # Drop a leading "msg 123:" or "Wren:" style label on the first line only.
    first_nl = s.find("\n")
    head = s if first_nl == -1 else s[:first_nl]
    if len(head) < 40 and head.rstrip().endswith(":"):
        s = "" if first_nl == -1 else s[first_nl + 1 :].lstrip()
    # Only peel surrounding quotes if the ORIGINAL wasn't a quoted line - else
    # those quotes are legitimate dialogue, not a model wrapper.
    orig = original.strip()
    orig_quoted = bool(orig) and orig[0] in "\"“" and orig[-1] in "\"”"
    if not orig_quoted and len(s) >= 2 and s[0] in "\"“" and s[-1] in "\"”":
        inner = s[1:-1]
        if '"' not in inner and "“" not in inner:
            s = inner
    return s.strip()


@dataclass
class Edit:
    msg_id: int
    speaker: str
    before: str          # replace: original text; inject: "" (empty)
    after: str           # replace: new text; inject: the new passage body
    flags: list[str] = field(default_factory=list)
    kind: str = "replace"  # "replace" | "inject" | "remove"
    attribution_voice: str | None = None  # optional sidecar voice (inject)
    attribution_mode: str | None = None   # optional sidecar mode (inject)

    @property
    def hard_flagged(self) -> bool:
        """True if a containment problem (not just a 'mixed' notice) was found."""
        return any(f.startswith("ALTERED") for f in self.flags)

    def to_json(self) -> dict:
        out = {
            "msg_id": self.msg_id,
            "speaker": self.speaker,
            "before": self.before,
            "after": self.after,
            "flags": self.flags,
            "kind": self.kind,
        }
        if self.attribution_voice:
            out["attribution_voice"] = self.attribution_voice
        if self.attribution_mode:
            out["attribution_mode"] = self.attribution_mode
        return out

    @classmethod
    def from_json(cls, d: dict) -> "Edit":
        return cls(
            msg_id=d["msg_id"], speaker=d["speaker"],
            before=d["before"], after=d["after"],
            flags=list(d.get("flags", [])),
            kind=d.get("kind", "replace"),
            attribution_voice=d.get("attribution_voice"),
            attribution_mode=d.get("attribution_mode"),
        )


@dataclass
class EditSet:
    log: str
    operator: str
    note: str
    locator: str
    edits: list[Edit] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Which document this set is a proposal against. Every operator here targets
    # the log; the field exists so `commit_edits` can refuse a set that says
    # otherwise instead of inferring the answer from the operator's name.
    layer: str = layers_mod.LOG
    # Optional operator-specific facts the review pane can show without inventing
    # a second proposal kind. Voice stores gold / retrieved ids here.
    meta: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        out = {
            "log": self.log,
            "operator": self.operator,
            "note": self.note,
            "locator": self.locator,
            "layer": self.layer,
            "edits": [e.to_json() for e in self.edits],
            "created": self.created,
        }
        if self.meta:
            out["meta"] = self.meta
        return out

    @classmethod
    def from_json(cls, d: dict) -> "EditSet":
        return cls(
            log=d["log"], operator=d["operator"], note=d["note"],
            locator=d.get("locator", ""),
            edits=[Edit.from_json(e) for e in d.get("edits", [])],
            created=d.get("created", ""),
            layer=d.get("layer", layers_mod.LOG),
            meta=dict(d.get("meta") or {}),
        )


def _llm_text(
    messages,
    *,
    task: str,
    temperature: float,
    max_tokens: int,
    on_stream=None,
) -> str:
    if on_stream is None:
        return llm.chat(
            messages,
            task=task,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return llm.stream_collect(
        messages,
        task=task,
        temperature=temperature,
        max_tokens=max_tokens,
        on_event=on_stream,
    )


def single_message_restyle(
    log_path: str | Path,
    msg_id: int,
    note: str,
    *,
    prior_rewrites: list[tuple[int, str]] | None = None,
) -> "Edit | None":
    """Restyle a single message and return an Edit without touching pending_edits.
    Used by the propagation cascade to generate per-hit patch proposals that the
    director reviews as a batch before any write occurs."""
    log = loader.load(log_path)
    target = log.get(msg_id)
    header, body = split_header(target.text)
    raw = llm.chat(
        build_restyle_messages(log, target, note, prior_rewrites=prior_rewrites),
        task="restyle",
        temperature=RESTYLE_TEMPERATURE,
        max_tokens=_rewrite_max_tokens(body),
    )
    new_body = _clean_model_body(raw, original=body)
    if not new_body or new_body.strip() == body.strip():
        return None
    after_full = header + new_body
    return Edit(
        msg_id=target.msg_id, speaker=target.speaker,
        before=target.text, after=after_full,
        flags=containment_flags(target.text, after_full, target.speaker),
    )


def restyle_span(
    log_path: str | Path,
    span: SpanResult,
    note: str,
    *,
    chain: bool = True,
    on_progress=None,
    on_stream=None,
) -> EditSet:
    """Run the restyle op over every message in the span. Each rewrite preserves
    the original header verbatim and only replaces the prose body. Messages the
    model returns unchanged (or empty) are skipped. Writes the pending edit-set.

    When `chain` is True (default) the pass is SPAN-AWARE: each rewrite is shown
    the rewrites already produced earlier in this run, so the new voice stays
    consistent and escalates while the model avoids recycling its own imagery.
    Set `chain=False` for independent per-message rewrites."""
    log = loader.load(log_path)
    edits: list[Edit] = []
    prior: list[tuple[int, str]] = []  # (msg_id, new_body) produced this pass
    total = len(span.messages)
    for i, target in enumerate(span.messages, start=1):
        if on_progress is not None:
            on_progress(i, total, target)
        if on_stream is not None:
            on_stream({
                "kind": "phase",
                "text": f"Restyling msg {target.msg_id} ({i}/{total})…",
            })
        header, body = split_header(target.text)
        # Reasoning models spend tokens THINKING before `content`. Budget must
        # cover both, or content comes back empty mid-thought.
        raw = _llm_text(
            build_restyle_messages(
                log, target, note, prior_rewrites=prior if chain else None
            ),
            task="restyle",
            temperature=RESTYLE_TEMPERATURE,
            max_tokens=_rewrite_max_tokens(body),
            on_stream=on_stream,
        )
        new_body = _clean_model_body(raw, original=body)
        if not new_body or new_body.strip() == body.strip():
            continue
        prior.append((target.msg_id, new_body))
        after_full = header + new_body
        edits.append(Edit(
            msg_id=target.msg_id, speaker=target.speaker,
            before=target.text, after=after_full,
            flags=containment_flags(target.text, after_full, target.speaker),
        ))

    edit_set = EditSet(
        log=str(Path(log_path).resolve()),
        operator="restyle", note=note, locator=span.locator, edits=edits,
    )
    write_pending_edits(edit_set)
    return edit_set


# --------------------------------------------------------------------------- #
# Pending edit-set: persistence + before/after rendering.
# --------------------------------------------------------------------------- #

def _unified(before: str, after: str) -> str:
    a = split_header(before)[1].splitlines() or [""]
    b = split_header(after)[1].splitlines() or [""]
    diff = difflib.unified_diff(a, b, lineterm="", n=2)
    # Drop the @@/+++/--- machinery; keep only the +/- context for readability.
    body = [ln for ln in diff if not ln.startswith(("+++", "---", "@@"))]
    return "\n".join(body)



# A token is a word with the whitespace that follows it, so two consecutive
# rewritten words come back as one hunk rather than being cut apart by the space
# they happen to share. The leading-whitespace alternative is what keeps the
# tokens joinable back into the exact original.
_TOKENS_RE = re.compile(r"\S+\s*|\s+")


def word_hunks(before: str, after: str) -> list[dict]:
    """Word-level diff as a flat run of ``{kind, text}``, kind being
    ``same``/``del``/``ins``. Joining every hunk's text reproduces both sides
    exactly, which is what lets the reviewer trust that the pane is showing the
    real change and not a normalised paraphrase of it."""
    a = _TOKENS_RE.findall(before)
    b = _TOKENS_RE.findall(after)
    out: list[dict] = []

    def push(kind: str, text: str) -> None:
        if not text:
            return
        if out and out[-1]["kind"] == kind:
            out[-1]["text"] += text
            return
        out.append({"kind": kind, "text": text})

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, a, b, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            push("same", "".join(a[i1:i2]))
        else:
            push("del", "".join(a[i1:i2]))
            push("ins", "".join(b[j1:j2]))
    return out


def edit_diff(edit: Edit, *, index: int | None = None) -> dict:
    """One edit as the review pane reads it: the header held out of the diff
    because it is structural metadata passed through verbatim, and the bodies
    diffed word by word."""
    before_header, before_body = split_header(edit.before)
    after_header, after_body = split_header(edit.after)
    hunks = word_hunks(before_body, after_body)
    out = {
        "msg_id": edit.msg_id,
        "speaker": edit.speaker,
        "kind": edit.kind,
        "header": after_header or before_header,
        "header_preserved": before_header == after_header,
        "before": before_body,
        "after": after_body,
        "hunks": hunks,
        "flags": list(edit.flags),
        "hard_flagged": edit.hard_flagged,
    }
    if index is not None:
        out["index"] = index
    return out


def edit_set_diff(edit_set: EditSet) -> dict:
    """The whole pending set, ready to render. Kept engine-side so the header
    rule lives in one place rather than being re-derived by every client."""
    diffs = [edit_diff(e, index=i) for i, e in enumerate(edit_set.edits)]
    out = {
        "operator": edit_set.operator,
        "note": edit_set.note,
        "locator": edit_set.locator,
        "log": edit_set.log,
        "created": edit_set.created,
        "edits": diffs,
        "flagged": sum(1 for d in diffs if d["flags"]),
        "hard_flagged": sum(1 for d in diffs if d["hard_flagged"]),
    }
    if edit_set.meta:
        out["meta"] = edit_set.meta
    return out


def operator_base(operator: str) -> str:
    """The op name behind an edit-set operator label. ``retune-tighten`` is the
    tighten mode of ``retune``, and it is ``retune`` that ``layers`` knows."""
    return (operator or "").split("-", 1)[0]


def _format_edits_md(edit_set: EditSet) -> str:
    lines = [
        f"# Pending restyle — {len(edit_set.edits)} edit(s)",
        "",
        f"- **Direction:** {edit_set.note}",
        f"- **Target:** {edit_set.locator}",
        f"- **Operator:** {edit_set.operator}",
        f"- **Log:** `{edit_set.log}`",
        "",
    ]
    hard = [e for e in edit_set.edits if e.hard_flagged]
    mixed = [e for e in edit_set.edits if e.flags and not e.hard_flagged]
    if hard:
        lines += [
            f"> ⚠️ {len(hard)} edit(s) flagged for containment review — see the "
            f"⚠️ blocks below. `commit` will refuse until these are dropped "
            f"(`edits drop <msg_id>`) or explicitly allowed (`edits commit "
            f"--allow-flagged`).",
            "",
        ]
    elif mixed:
        lines += [
            f"> {len(mixed)} turn(s) also carry another speaker's line. "
            f"Look at the diff. Accept is allowed if those lines are untouched.",
            "",
        ]
    if not edit_set.edits:
        lines.append("_No message changed — the model returned the originals. Try a "
                     "sharper --note or a different span._")
    for e in edit_set.edits:
        lines += [
            f"## msg {e.msg_id} — {e.speaker}",
            "",
        ]
        if e.flags:
            lines.append("")
            for f in e.flags:
                lines.append(f"> ⚠️ {f}")
            lines.append("")
        if e.kind == "inject":
            lines += [
                f"**New passage (insert after msg {e.msg_id})**",
                "",
                "> " + "\n> ".join(e.after.splitlines()),
                "",
            ]
        elif e.kind == "remove":
            lines += [
                "**This turn will be taken out**",
                "",
                "> " + "\n> ".join(split_header(e.before)[1].splitlines()),
                "",
            ]
        else:
            lines += [
                "**Before**",
                "",
                "> " + "\n> ".join(split_header(e.before)[1].splitlines()),
                "",
                "**After**",
                "",
                "> " + "\n> ".join(split_header(e.after)[1].splitlines()),
                "",
            ]
    lines += [
        "---",
        "_Land it:_ `python -m story_editor edits commit`",
        "_Throw it away:_ `python -m story_editor edits discard`",
        "",
    ]
    return "\n".join(lines)


def write_pending_edits(edit_set: EditSet) -> None:
    config.PENDING_EDITS.write_text(
        json.dumps(edit_set.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    config.PENDING_EDITS_MD.write_text(_format_edits_md(edit_set), encoding="utf-8")


def load_pending_edits() -> EditSet | None:
    if not config.PENDING_EDITS.exists():
        return None
    return EditSet.from_json(json.loads(config.PENDING_EDITS.read_text(encoding="utf-8")))


def patch_edit(msg_id: int, new_body: str) -> bool:
    """Replace the prose body of a pending edit. Preserves the message header
    (the `[ time | date | location ]` block) verbatim and re-runs the
    containment check on the updated text. Returns True if the edit was found."""
    edit_set = load_pending_edits()
    if edit_set is None:
        return False
    for e in edit_set.edits:
        if e.msg_id != msg_id:
            continue
        # Keep whatever header the current proposal has.
        header = split_header(e.after)[0]
        e.after = header + new_body.strip()
        e.flags = containment_flags(e.before, e.after, e.speaker)
        write_pending_edits(edit_set)
        return True
    return False


def propose_hand_edits(
    log_path: str | Path,
    patches: list[dict],
) -> EditSet:
    """Turn author-typed bodies into a pending EditSet. The header of each
    turn is taken from the log and put back on; only the body is replaced.
    Nothing is written to the log until ``commit_edits``."""
    layers_mod.check_runs("patch", layers_mod.LOG)
    log_path = Path(log_path).resolve()
    log = loader.load(log_path)
    edits: list[Edit] = []
    changed: list[int] = []
    for raw in patches:
        if not isinstance(raw, dict):
            continue
        msg_id = int(raw.get("msg_id"))
        new_body = str(raw.get("body") if raw.get("body") is not None else raw.get("text") or "")
        new_body = new_body.replace("\r\n", "\n")
        target = log.get(msg_id)
        header, _old = split_header(target.text)
        after = header + new_body
        if after == target.text:
            continue
        edits.append(Edit(
            msg_id=target.msg_id,
            speaker=target.speaker,
            before=target.text,
            after=after,
            flags=containment_flags(target.text, after, target.speaker),
        ))
        changed.append(msg_id)
    if not edits:
        raise ValueError("nothing changed")
    locator = (
        f"msg {changed[0]}"
        if len(changed) == 1
        else f"msgs {changed[0]}–{changed[-1]} · {len(changed)} changed"
    )
    edit_set = EditSet(
        log=str(log_path),
        operator="patch",
        note="hand edit",
        locator=locator,
        edits=edits,
    )
    write_pending_edits(edit_set)
    return edit_set


def propose_remove(
    log_path: str | Path,
    msg_from: int,
    msg_to: int | None = None,
    *,
    sweep: bool = False,
) -> EditSet:
    """Propose taking one or more turns out of the log. Nothing is written
    until ``commit_edits``. A hard delete shifts every later id, so gold and
    the voice bank are remapped on accept — not here."""
    layers_mod.check_runs("remove", layers_mod.LOG)
    log_path = Path(log_path).resolve()
    log = loader.load(log_path)
    start = int(msg_from)
    end = int(msg_to) if msg_to is not None else start
    if end < start:
        start, end = end, start
    if start < 0 or end >= len(log):
        raise ValueError(f"msgs {start}–{end} are out of range on a {len(log)}-turn log")
    ids = list(range(start, end + 1))
    if len(ids) >= len(log):
        raise ValueError("refusing to delete every turn in the log")
    edits = [
        Edit(
            msg_id=mid,
            speaker=log.get(mid).speaker,
            before=log.get(mid).text,
            after="",
            kind="remove",
        )
        for mid in ids
    ]
    locator = (
        f"msg {ids[0]}"
        if len(ids) == 1
        else f"msgs {ids[0]}–{ids[-1]} · {len(ids)} turns"
    )
    note = "remove, then look downstream" if sweep else "remove"
    edit_set = EditSet(
        log=str(log_path),
        operator="remove",
        note=note,
        locator=locator,
        edits=edits,
        meta={"sweep": bool(sweep), "count": len(edits)},
    )
    write_pending_edits(edit_set)
    return edit_set


def shift_id_after_delete(old_id: int, removed: set[int]) -> int | None:
    """Where ``old_id`` lives after those ordinals are gone. None if it was cut."""
    if old_id in removed:
        return None
    return old_id - sum(1 for r in removed if r < old_id)


def hand_patch_message(
    log_path: str | Path,
    msg_id: int,
    text: str,
) -> dict:
    """Write one turn's ``mes`` in place. Backs up first. Refuses if a pending
    proposal already owns that turn — accept or reject that first, or the
    before-text will no longer match."""
    layers_mod.check_runs("patch", layers_mod.LOG)
    log_path = Path(log_path).resolve()
    layers_mod.check_syncable(layers_mod.LOG)
    text = text.replace("\r\n", "\n")
    pending = load_pending_edits()
    if pending and any(e.msg_id == msg_id and e.kind == "replace" for e in pending.edits):
        raise ValueError(
            f"a proposal is waiting on msg {msg_id} — accept or reject it first"
        )
    log = loader.load(log_path)
    target = log.get(msg_id)
    if target.text == text:
        return {"ok": True, "applied": 0, "backup": "", "msg_id": msg_id}

    backup_path = backup_mod.backup(log_path, label=f"patch-{msg_id}")
    lines = _raw_lines(log_path)
    file_line = msg_id + 1
    if file_line >= len(lines):
        raise ValueError(f"msg {msg_id} is out of range on disk")
    obj = json.loads(lines[file_line])
    if obj.get("mes", "") != target.text:
        raise ValueError(
            f"msg {msg_id} on disk no longer matches the loaded turn"
        )
    obj["mes"] = text
    lines[file_line] = json.dumps(obj, ensure_ascii=False)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        from . import history as _hist
        _hist.record_commit_from_edit_set(
            log_path,
            operator="patch",
            note=f"hand edit msg {msg_id}",
            locator=f"msg {msg_id}",
            backup=backup_path,
            edits=[Edit(
                msg_id=msg_id, speaker=target.speaker,
                before=target.text, after=text,
            )],
        )
    except Exception:
        pass
    return {
        "ok": True,
        "applied": 1,
        "backup": str(backup_path),
        "msg_id": msg_id,
    }


def discard_pending_edits() -> bool:
    if config.PENDING_EDITS.exists():
        config.PENDING_EDITS.unlink(missing_ok=True)
        config.PENDING_EDITS_MD.unlink(missing_ok=True)
        return True
    return False


def drop_edit(msg_id: int, *, index: int | None = None) -> bool:
    """Remove one edit from the pending set.

    Prefer ``index`` when several injects share the same anchor ``msg_id``
    (Author Studio multi-turn scenes). Falling back to ``msg_id`` alone would
    drop every inject at that anchor.
    """
    edit_set = load_pending_edits()
    if edit_set is None:
        return False
    removed = False
    if index is not None:
        if index < 0 or index >= len(edit_set.edits):
            return False
        del edit_set.edits[index]
        removed = True
    else:
        # Prefer dropping a single matching edit; if several share msg_id, drop only
        # the first so Accept/Reject of one speaker does not erase the whole scene.
        for i, e in enumerate(edit_set.edits):
            if e.msg_id == msg_id:
                del edit_set.edits[i]
                removed = True
                break
    if not removed:
        return False
    if edit_set.operator in {"weed", "copyedit"}:
        edit_set.meta["review_total"] = int(
            edit_set.meta.get("review_total") or len(edit_set.edits) + 1
        )
        edit_set.meta["review_rejected"] = int(
            edit_set.meta.get("review_rejected") or 0
        ) + 1
    if edit_set.edits:
        write_pending_edits(edit_set)
    else:
        discard_pending_edits()
    return True


def commit_one_edit(
    log_path: str | Path,
    *,
    index: int = 0,
    allow_flagged: bool = False,
) -> tuple[Path, int, int]:
    """Commit one queued replacement and preserve every undecided sibling.

    Used by serial Weed and Copyedit review. The ordinary commit path remains the single
    implementation of backups, before-text validation, history, and sweep
    context; this function temporarily narrows the pending set to one edit and
    restores the remaining queue after that commit succeeds.
    """
    full = load_pending_edits()
    if full is None:
        raise FileNotFoundError("no pending edits to commit")
    if full.operator not in {"weed", "copyedit"}:
        raise ValueError(
            "serial commit is currently available only for weed and copyedit proposals"
        )
    if index < 0 or index >= len(full.edits):
        raise ValueError(f"edit index {index} is out of range")
    chosen = full.edits[index]
    if chosen.kind != "replace":
        raise ValueError("serial review only supports replacement edits")
    remaining = full.edits[:index] + full.edits[index + 1:]
    one = EditSet(
        log=full.log,
        operator=full.operator,
        note=full.note,
        locator=f"msg {chosen.msg_id}",
        edits=[chosen],
        created=full.created,
        layer=full.layer,
        meta=dict(full.meta),
    )
    write_pending_edits(one)
    try:
        backup, applied = commit_edits(log_path, allow_flagged=allow_flagged)
    except Exception:
        write_pending_edits(full)
        raise

    if remaining:
        full.edits = remaining
        full.meta["review_total"] = int(
            full.meta.get("review_total") or len(remaining) + 1
        )
        full.meta["review_accepted"] = int(
            full.meta.get("review_accepted") or 0
        ) + 1
        write_pending_edits(full)
    return backup, applied, len(remaining)


def _raw_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]


def commit_edits(log_path: str | Path, *, allow_flagged: bool = False) -> tuple[Path, int]:
    """Apply the pending edit-set to the log. Backs up first, then rewrites only
    the `mes` field of each targeted line, preserving every other field and every
    other line byte-for-byte. Returns (backup_path, edits_applied).

    Refuses only when a foreign line was altered (``ALTERED``). A ``mixed``
    notice means the turn also carries another speaker — look, then accept.
    ``allow_flagged`` is for the hard case, not for a composite turn that
    kept the other mouth intact."""
    edit_set = load_pending_edits()
    if edit_set is None:
        raise FileNotFoundError("no pending edits to commit (run `restyle` first)")
    if not edit_set.edits:
        raise ValueError("the pending edit-set is empty; nothing to commit")

    flagged = [e for e in edit_set.edits if e.hard_flagged]
    if flagged and not allow_flagged:
        ids = ", ".join(str(e.msg_id) for e in flagged)
        raise ValueError(
            f"{len(flagged)} edit(s) flagged for containment review (msgs {ids}). "
            f"Review them in {config.PENDING_EDITS_MD}, then either drop them "
            f"(`edits drop <msg_id>`) or commit with `--allow-flagged`."
        )

    log_path = Path(log_path).resolve()
    # Compare resolved paths — a symlink (story.jsonl → mini_story.jsonl)
    # must not fail a commit that was proposed against either spelling.
    pending_log = Path(edit_set.log).resolve() if edit_set.log else None
    if pending_log != log_path and not config.same_log(edit_set.log, log_path):
        raise ValueError(
            f"pending edits target {edit_set.log}, not {log_path}. "
            "Re-run restyle against this log."
        )

    # This write lands in the working log — the one document allowed to travel
    # back to SillyTavern. A set proposed against a derived layer must not be
    # applied here, so the layer rule is asked rather than assumed.
    layers_mod.check_syncable(edit_set.layer)

    backup_path = backup_mod.backup(log_path, label=f"{edit_set.operator}-{len(edit_set.edits)}")

    lines = _raw_lines(log_path)

    # Validate replace and remove edits before writing anything.
    for e in edit_set.edits:
        if e.kind not in ("replace", "remove"):
            continue
        file_line = e.msg_id + 1
        if file_line >= len(lines):
            raise ValueError(f"msg {e.msg_id} is out of range on disk")
        if json.loads(lines[file_line]).get("mes", "") != e.before:
            raise ValueError(
                f"msg {e.msg_id} on disk no longer matches the proposed 'before' "
                f"(the log changed since this edit was generated). Aborting "
                f"before any write; re-run the operator."
            )

    # Apply replace edits (in-place, order-independent).
    applied = 0
    for e in edit_set.edits:
        if e.kind != "replace":
            continue
        file_line = e.msg_id + 1
        obj = json.loads(lines[file_line])
        obj["mes"] = e.after
        lines[file_line] = json.dumps(obj, ensure_ascii=False)
        applied += 1

    # Apply inject edits in REVERSE msg_id order so earlier insertions don't
    # shift the file-line indices of later ones.
    injects = sorted([e for e in edit_set.edits if e.kind == "inject"],
                     key=lambda e: e.msg_id, reverse=True)
    inject_labels: list[tuple[int, Edit]] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    from . import identity as identity_mod

    for e in injects:
        new_obj = {
            "name": e.speaker,
            "is_user": False,
            "is_system": False,
            "send_date": now,
            "mes": e.after,
            "extra": {"story_editor": {"inject": True}},
        }
        identity_mod.write_uid(new_obj, identity_mod.mint())
        insert_at = e.msg_id + 2  # file layout: line 0 = metadata, msg k = line k+1
        lines.insert(insert_at, json.dumps(new_obj, ensure_ascii=False))
        applied += 1
        if e.attribution_voice:
            inject_labels.append((e.msg_id + 1, e))

    # Apply remove edits high-id-first so earlier deletions do not shift the
    # file-line indices of later ones. A hard delete is why gold and the voice
    # bank have to be remapped after the write.
    removes = sorted(
        [e for e in edit_set.edits if e.kind == "remove"],
        key=lambda e: e.msg_id,
        reverse=True,
    )
    removed_ids = {e.msg_id for e in removes}
    for e in removes:
        file_line = e.msg_id + 1
        del lines[file_line]
        applied += 1

    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


    if inject_labels:
        try:
            from . import attribution as attr_mod
            store = attr_mod.load_store()
            for new_msg_id, e in inject_labels:
                mode = e.attribution_mode or default_inject_attribution_mode(
                    e.speaker, e.attribution_voice or "",
                )
                label = attr_mod.VoiceLabel(
                    card=e.speaker,
                    voice=e.attribution_voice or "unknown",
                    mode=mode,
                    source="manual",
                    reviewed=True,
                    confidence=1.0,
                    notes=f"inject commit ({edit_set.note[:120]})",
                )
                attr_mod.set_label(store, new_msg_id, label, force=True)
            attr_mod.save_store(store)
        except Exception:
            pass  # attribution is best-effort; never block commit

    # Persist a sweep context so `sweep --last` can pick up what just changed
    # without the user having to supply msg_ids manually.
    try:
        from . import propagate as _prop
        ctx = _prop.context_from_edit_set(
            log_path, edit_set.operator, edit_set.note, edit_set.edits
        )
        _prop.save_sweep_context(ctx)
    except Exception:
        pass  # sweep context is best-effort; never block a commit

    try:
        from . import history as _hist
        _hist.record_commit_from_edit_set(
            log_path,
            operator=edit_set.operator,
            note=edit_set.note,
            locator=edit_set.locator,
            backup=backup_path,
            edits=edit_set.edits,
        )
    except Exception:
        pass  # history is best-effort; never block a commit

    discard_pending_edits()
    return backup_path, applied


def undo(log_path: str | Path, *, from_backup: str | Path | None = None) -> Path:
    """Restore a log backup and record it.

    ``from_backup`` lets review surfaces bind undo to the history entry the
    author actually saw.  CLI callers retain the traditional newest-backup
    behaviour when it is omitted.
    """
    restored = Path(backup_mod.restore(log_path, from_backup=from_backup))
    try:
        from . import history as _hist
        _hist.record_undo(log_path, backup_restored=restored)
    except Exception:
        pass
    return restored


# --------------------------------------------------------------------------- #
# RETUNE: change length/density, hold everything else fixed.
# --------------------------------------------------------------------------- #

# Lower temperature than Restyle - trimming/expanding prose is more mechanical
# than shifting register.
RETUNE_TEMPERATURE = 0.4

RETUNE_CONTRACT = (
    "You are a literary line-editor performing a RETUNE pass on one message of an "
    "existing prose roleplay. Your job is to change the LENGTH and DENSITY of the "
    "prose while leaving everything else exactly as it was.\n\n"
    "HOLD FIXED (must survive the retune unchanged):\n"
    "- Every event and action, in the same order, with the same outcome.\n"
    "- Every concrete fact: who is present, names, places, objects, what is said "
    "to whom in substance, any information revealed or decision made.\n"
    "- The emotional register, tone, and characterisation. A cold line stays cold; "
    "an intimate line stays intimate. You are not allowed to shift the character's "
    "voice, only how many words they take to occupy it.\n"
    "- The beat's function — do not add resolution or escalation that isn't there.\n\n"
    "THE ONE AXIS YOU MAY MOVE:\n"
    "- For TIGHTEN: cut wordcount. Remove redundancy, filter adjectives down to the "
    "essential ones, collapse elaborate phrases, trim wasted motion. Every word that "
    "survives must earn its place. The scene must feel faster and more immediate.\n"
    "- For EXPAND: add sensory texture, interior weight, and physical specificity. "
    "Slow the moment down. But only extend what is already there — do not invent "
    "new events, new information, or new emotional beats that aren't in the original.\n\n"
    "MIRROR THE ORIGINAL'S FORMATTING EXACTLY: spoken words stay in double quotes; "
    "narration stays unquoted; the same interleaving of action and speech is kept.\n\n"
    "Output ONLY the rewritten prose body. No preamble, no label, no commentary."
)


def build_retune_messages(
    log: Log,
    target: Message,
    mode: str,
    note: str | None = None,
    prior_rewrites: list[tuple[int, str]] | None = None,
) -> list[dict]:
    """Build the prompt for one retune call. `mode` is 'tighten' or 'expand';
    an optional `note` can sharpen the direction."""
    system = "\n\n".join([RETUNE_CONTRACT, canon_mod.story_primer()])
    body = split_header(target.text)[1]

    direction = f"MODE: {mode.upper()}."
    if note:
        direction += f" Additional note: {note}"

    parts = [direction]

    # Phase 5: inject character canon for the speaker
    char = canon_layer.get_character(target.speaker)
    if char:
        parts.append(canon_layer.render_canon_block(char))

    # Phase 5: inject beat context
    beat_ctx = canon_layer.beat_context_block(target.msg_id)
    if beat_ctx:
        parts.append(beat_ctx)

    parts.append(
        f"SCENE CONTEXT (read-only — do not rewrite these):\n\n{_context_block(log, target)}"
    )
    prior_block = _prior_block(prior_rewrites or [])
    if prior_block:
        parts.append(prior_block)
    fq = foreign_quotes(body, target.speaker)
    if fq:
        quoted = "\n".join(f'  - "{q}"' for q in fq)
        parts.append(
            f"PROTECTED CONTENT — reproduce the following line(s) EXACTLY:\n{quoted}"
        )
    parts.append(
        f"THE LINE TO RETUNE — {target.speaker}'s turn (msg {target.msg_id}). "
        f"Retune ONLY this body. Output only the retuned prose:\n\n{body}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]


def retune_span(
    log_path: str | Path,
    span: SpanResult,
    mode: str,
    note: str | None = None,
    *,
    chain: bool = True,
    on_progress=None,
    on_stream=None,
) -> EditSet:
    """Retune every message in the span. `mode` is 'tighten' or 'expand'.
    Same span-aware chaining and containment guard as restyle_span."""
    if mode not in ("tighten", "expand"):
        raise ValueError(f"mode must be 'tighten' or 'expand', got {mode!r}")
    log = loader.load(log_path)
    edits: list[Edit] = []
    prior: list[tuple[int, str]] = []
    total = len(span.messages)
    for i, target in enumerate(span.messages, start=1):
        if on_progress is not None:
            on_progress(i, total, target)
        if on_stream is not None:
            on_stream({
                "kind": "phase",
                "text": f"Retuning msg {target.msg_id} ({i}/{total}, {mode})…",
            })
        header, body = split_header(target.text)
        raw = _llm_text(
            build_retune_messages(log, target, mode, note, prior_rewrites=prior if chain else None),
            task="retune",
            temperature=RETUNE_TEMPERATURE,
            max_tokens=_rewrite_max_tokens(body),
            on_stream=on_stream,
        )
        new_body = _clean_model_body(raw, original=body)
        if not new_body or new_body.strip() == body.strip():
            continue
        prior.append((target.msg_id, new_body))
        after_full = header + new_body
        edits.append(Edit(
            msg_id=target.msg_id, speaker=target.speaker,
            before=target.text, after=after_full,
            flags=containment_flags(target.text, after_full, target.speaker),
        ))

    note_str = f" / {note}" if note else ""
    edit_set = EditSet(
        log=str(Path(log_path).resolve()),
        operator=f"retune-{mode}",
        note=f"{mode}{note_str}",
        locator=span.locator,
        edits=edits,
    )
    write_pending_edits(edit_set)
    return edit_set


# --------------------------------------------------------------------------- #
# INJECT: write NEW content at a chosen position (insert, not replace).
# --------------------------------------------------------------------------- #

# Inject is the most open-ended creative task; it needs enough headroom to
# think, then write a passage of moderate length.
INJECT_TEMPERATURE = 0.85

# Card slot → the voice that card normally carries, when the two names differ.
# Story-specific; a card named after its character needs no entry.
_CARD_DEFAULT_VOICE: dict[str, str] = {}


def default_inject_attribution_mode(card: str, voice: str) -> str:
    """Guess sidecar mode from card slot vs voiced character."""
    v = voice.strip().lower()
    if v == "ensemble":
        return "scene_narrator"
    card_key = card.strip().lower()
    if _CARD_DEFAULT_VOICE.get(card_key, card_key) == v:
        return "pov"
    return "wrong_card"


INJECT_CONTRACT = (
    "You are a literary author writing a NEW passage to be inserted into an "
    "existing prose roleplay. The passage must fit organically between the two "
    "messages that surround it — it should feel as though it was always there.\n\n"
    "CONSTRAINTS:\n"
    "- Write only what the DIRECTION asks for — no more scope, no extra events.\n"
    "- Stay consistent with the events, characterisation, and facts of the "
    "surrounding context. Do not introduce new information that contradicts "
    "anything established in the surrounding messages.\n"
    "- Keep the same period (early-Victorian), register, and prose quality as the "
    "messages around you.\n"
    "- The passage should be self-contained enough to read clearly, but must not "
    "resolve or advance the plot beyond the point where the surrounding context "
    "leaves it — this is an insertion, not an extension.\n\n"
    "FORMATTING: match the style of the surrounding messages. If the context "
    "interleaves dialogue and action narration, do the same. Spoken words in double "
    "quotes. No internal headings, no metadata.\n\n"
    "Output ONLY the new passage. No preamble, no label, no commentary."
)

INJECT_CONTEXT_RADIUS = 5  # show more context for injection (no 'before' to anchor on)

_INJECT_WORDS_RE = re.compile(r"(\d+)\s*words?", re.IGNORECASE)
_INJECT_SHORT_RE = re.compile(
    r"\b(short|brief|tight|compact|under\s+\d+\s+words?|less\s+than\s+\d+)\b",
    re.IGNORECASE,
)


def _inject_length_hint(note: str) -> tuple[int, str]:
    """Return (max_tokens budget, optional extra contract line) from the note."""
    m = _INJECT_WORDS_RE.search(note)
    if m:
        words = max(20, int(m.group(1)))
        cap_line = f"LENGTH: keep the passage under {words} words unless impossible."
        # Reasoning models need headroom beyond prose length.
        return min(6000, max(3000, words * 10 + 2200)), cap_line
    if _INJECT_SHORT_RE.search(note):
        return 3500, "LENGTH: keep the passage short (roughly 80–150 words)."
    return 5500, ""


def _inject_context_block(log: Log, after_msg_id: int) -> str:
    """The messages immediately before and after the injection point, labelled.
    The anchor message (after_msg_id) is shown IN FULL so the model knows exactly
    where it ends and writes a fresh passage — not a continuation of it."""
    lo = max(0, after_msg_id - INJECT_CONTEXT_RADIUS)
    hi = min(len(log) - 1, after_msg_id + INJECT_CONTEXT_RADIUS)
    lines = []
    for m in log.messages[lo : hi + 1]:
        if m.msg_id == after_msg_id:
            mark = " <<< INSERT AFTER THIS MESSAGE (shown in full)"
            body = text_mod.clean(split_header(m.text)[1])  # full, not truncated
        elif m.msg_id == after_msg_id + 1:
            mark = " <<< THE MESSAGE THAT CURRENTLY FOLLOWS"
            body = _clean_ctx(m.text)
        else:
            mark = ""
            body = _clean_ctx(m.text)
        lines.append(f"[msg {m.msg_id} · {m.speaker}]{mark}\n{body}")
    return "\n\n".join(lines)


def build_inject_messages(
    log: Log,
    after_msg_id: int,
    speaker: str,
    note: str,
) -> list[dict]:
    system = "\n\n".join([INJECT_CONTRACT, canon_mod.story_primer()])

    parts = [
        f"DIRECTION — what to write: {note}",
        f"SPEAKER / POV: {speaker}",
    ]
    _, length_line = _inject_length_hint(note)
    if length_line:
        parts.append(length_line)

    # Phase 5: inject character canon for the speaker
    char = canon_layer.get_character(speaker)
    if char:
        parts.append(canon_layer.render_canon_block(char))

    # Phase 5: inject beat context
    beat_ctx = canon_layer.beat_context_block(after_msg_id)
    if beat_ctx:
        parts.append(beat_ctx)

    parts += [
        f"SURROUNDING CONTEXT (read-only — do not rewrite these; just make sure "
        f"your passage fits naturally between them):\n\n"
        f"{_inject_context_block(log, after_msg_id)}",
        f"Write the new passage for {speaker}, to be inserted after msg "
        f"{after_msg_id}. Output only the prose:",
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]


def inject_at(
    log_path: str | Path,
    after_msg_id: int,
    speaker: str,
    note: str,
    locator: str = "",
    *,
    attribution_voice: str | None = None,
    attribution_mode: str | None = None,
    on_stream=None,
) -> EditSet:
    """Generate a new passage to inject after `after_msg_id`. Writes it to the
    pending edit-set as a kind='inject' edit. Nothing is written to the log until
    `commit_edits` is called."""
    log = loader.load(log_path)
    log.get(after_msg_id)  # bounds check

    voice = (attribution_voice or "").strip().lower() or None
    mode = (attribution_mode or "").strip().lower() or None
    if voice and not mode:
        mode = default_inject_attribution_mode(speaker, voice)

    max_tokens, _ = _inject_length_hint(note)
    if on_stream is not None:
        on_stream({"kind": "phase", "text": f"Injecting as {speaker} after msg {after_msg_id}…"})
    raw = _llm_text(
        build_inject_messages(log, after_msg_id, speaker, note),
        task="inject",
        temperature=INJECT_TEMPERATURE,
        max_tokens=max_tokens,
        on_stream=on_stream,
    )
    body = _clean_model_body(raw)
    if not body:
        raise ValueError("model returned an empty injection — try a sharper --note")

    edit = Edit(
        msg_id=after_msg_id,
        speaker=speaker,
        before="",
        after=body,
        flags=[],
        kind="inject",
        attribution_voice=voice,
        attribution_mode=mode,
    )
    edit_set = EditSet(
        log=str(Path(log_path).resolve()),
        operator="inject",
        note=note,
        locator=locator or f"after msg {after_msg_id}",
        edits=[edit],
    )
    write_pending_edits(edit_set)
    return edit_set
