"""Phase 3 — novelization: one log scene becomes one manuscript scene.

The log is a transcript. Two writers took turns, each turn carries a header, half
of them are second person because the director was playing a character, and the
stage directions live in asterisks. None of that is prose. Novelization is the
pass that transposes it: same events, same spoken words, same order — narrative
form instead of turn form.

**Transpose, do not rewrite.** Restyle already exists for changing how a scene
feels; this operator's whole discipline is that it changes only the form. Every
action, line of dialogue, revelation and outcome in the source has to survive,
because a manuscript scene the author cannot trust is worth less than the log it
came from. The same rule applies to heat: novelization renders explicit material
at exactly the explicitness of the source. Cooling is a later, separate, opt-in
pass with its own contract — a novelizer that quietly softened things would make
that contract a lie.

Voice
-----
Person and tense are the author's decisions, held in ``manuscript.Voice`` — on
the document as the book's default, on a scene when it departs. Both are prompt
inputs here, and the resolved voice is stamped onto the scene it produced.

Provenance
----------
Each source turn is numbered in the prompt and the model marks each paragraph it
writes with the turn(s) that paragraph draws on. Those markers are parsed off the
prose and become ``Block.src`` uids, so the editor can answer "what was this
paragraph" for any sentence on the page. Markers are advisory: when the model
omits one, the paragraph inherits its predecessor's sources, and the count of
unmarked paragraphs is reported rather than hidden.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from . import (
    attribution as attr_mod,
    config,
    layers as layers_mod,
    llm,
    loader,
    manuscript as ms,
    structure as structure_mod,
    text as text_mod,
)
from .manuscript import Voice

Log = loader.Log

# Default one-pass ceiling (cleaned source characters). Prefer
# ``resolve_max_span()`` — that reads ``STORY_EDITOR_NOVELIZE_MAX_SPAN_CHARS``
# and accepts a per-call override. Longer scenes are split under the ceiling.
MAX_SPAN_CHARS = 24_000
MIN_MAX_SPAN_CHARS = 500


def resolve_max_span(override: int | None = None) -> int:
    """Effective one-pass ceiling: explicit override, else config/env, else default."""
    if override is not None:
        n = int(override)
        if n < MIN_MAX_SPAN_CHARS:
            raise ValueError(
                f"max_span_chars must be at least {MIN_MAX_SPAN_CHARS:,} "
                f"(got {n:,})"
            )
        return n
    cfg = getattr(config, "NOVELIZE_MAX_SPAN_CHARS", None)
    if cfg is not None:
        return max(MIN_MAX_SPAN_CHARS, int(cfg))
    return MAX_SPAN_CHARS


# Transposition, not invention: warm enough for real sentences, cool enough to
# stay on the events it was given.
TEMPERATURE = 0.6
# Novelized prose runs about as long as the transcript it came from, and the
# budget has to cover the model's reasoning as well as its prose — the local
# preset thinks before it writes, and thinking is charged to the same ceiling.
# Short beats (e.g. A2 ≈ 2.5k chars) used to get ~2.6k tokens; reasoning models
# spent the whole budget thinking and returned empty content. Floors match the
# restyle path; override with STORY_EDITOR_NOVELIZE_MAX_TOKENS.
OUTPUT_CHARS_PER_TOKEN = 2.5
REASONING_HEADROOM_TOKENS = 8_192
MAX_OUTPUT_TOKENS = 24_000
MIN_OUTPUT_TOKENS = 8_192

# A scene cut off mid-sentence is worthless, so a truncated reply is continued
# rather than filed. Twice, then the honest warning.
MAX_CONTINUATIONS = 2

# What the model reads before the scene (or chunk) starts, so its first
# paragraph does not open as though the book began there.
LEAD_IN_CHARS = 700

ProgressFn = Callable[..., None]  # (stage, detail, *, step=0, total=0)

# «3» or «3,4» at the head of a paragraph: which source turns it came from.
_MARKER = re.compile(r"«\s*([0-9][0-9,\s]*)\s*»")
_LEAD_MARKER = re.compile(r"^\s*«\s*([0-9][0-9,\s]*)\s*»\s*")

# Models like to introduce themselves. Drop a single opening announcement line,
# never prose that merely happens to be short.
_PREAMBLE = re.compile(
    r"^(here('s| is)\b|below\b|certainly\b|of course\b).{0,80}:\s*$",
    re.IGNORECASE,
)

PERSON_GUIDANCE: dict[str, str] = {
    "first": (
        "FIRST PERSON, from {focal}: \"I\". The page holds only what {focal} "
        "perceives, thinks, or infers. Other characters are known from the "
        "outside — their words, faces, hands — never their thoughts."
    ),
    "second": (
        "SECOND PERSON, addressed to {focal}: \"you\". The page holds only what "
        "{focal} perceives, thinks, or infers. Other characters are known from "
        "the outside only. This is a deliberate literary choice, not the "
        "transcript's second person left in place: it must read as narration, "
        "never as instructions to a player."
    ),
    "close_third": (
        "CLOSE THIRD PERSON, limited to {focal}: name and pronoun, never \"I\". "
        "The camera does not leave {focal}. Other characters are known from the "
        "outside — their words, faces, hands — never their thoughts, and "
        "certainly not their motives except as {focal} reads them."
    ),
    "omniscient": (
        "THIRD PERSON OMNISCIENT. The narrator may move between characters and "
        "knows the room. Freedom of vantage is not licence to invent: enter a "
        "character's thoughts only where the source shows what that character "
        "thinks or feels."
    ),
}

TENSE_GUIDANCE: dict[str, str] = {
    "past": 'PAST TENSE — "she crossed the floor". Hold it; no drift into present.',
    "present": (
        'PRESENT TENSE — "she crosses the floor". Hold it; slip into past only '
        "for material that is genuinely earlier than the scene."
    ),
}


# --------------------------------------------------------------------------- #
# Reading the source
# --------------------------------------------------------------------------- #


@dataclass
class Turn:
    """One source message, as the prompt will present it."""

    index: int  # 1-based marker number
    msg_id: int
    uid: str
    speaker: str
    body: str
    is_user: bool
    voiced: str = ""  # which character the director was voicing, when known

    def label(self) -> str:
        if not self.is_user:
            return self.speaker
        who = self.voiced or self.speaker
        return f"{who} (written by the director)"


def turns_for(log: Log, start: int, end: int, *, labels: dict[str, Any] | None = None) -> list[Turn]:
    """The span's messages, cleaned of headers and meta blocks, numbered for
    provenance. Empty turns are dropped — they carry nothing to transpose."""
    store_labels = labels
    if store_labels is None:
        store = attr_mod.load_store(log=log)
        store_labels = store.get("labels") or {}

    out: list[Turn] = []
    for m in log.messages[max(0, start) : end + 1]:
        body = text_mod.clean(m.text).strip()
        if not body:
            continue
        label = store_labels.get(str(m.msg_id)) or store_labels.get(m.msg_id) or {}
        out.append(
            Turn(
                index=len(out) + 1,
                msg_id=m.msg_id,
                uid=m.uid or f"@{m.msg_id}",
                speaker=m.speaker,
                body=body,
                is_user=m.is_user,
                voiced=str(label.get("voice") or "") if isinstance(label, dict) else "",
            )
        )
    return out


def source_chars(turns: list[Turn]) -> int:
    return sum(len(t.body) for t in turns)


def focal_candidates(log: Log, start: int, end: int) -> list[dict[str, Any]]:
    """Who this scene could be followed through, commonest first — the picker's
    options. Attribution names who the director was voicing; without it the
    speaker name is the best available answer."""
    counts: dict[str, int] = {}
    for t in turns_for(log, start, end):
        who = (t.voiced or t.speaker).strip()
        if not who:
            continue
        counts[who.title() if who.islower() else who] = counts.get(
            who.title() if who.islower() else who, 0
        ) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    # The narrator writes most turns but is nobody a scene can follow; offer it
    # only when a scene has no one else.
    people = [kv for kv in ranked if kv[0].strip().lower() not in _NOT_A_CHARACTER]
    return [{"name": name, "turns": n} for name, n in (people or ranked)]


_NOT_A_CHARACTER = {"narrator", "the narrator", "system", "director"}


def resolve_voice(
    doc: ms.Document,
    *,
    scene: ms.Scene | None = None,
    override: Voice | dict[str, Any] | None = None,
    log: Log | None = None,
    span: tuple[int, int] | None = None,
) -> Voice:
    """The voice to write in: an explicit override, else the scene's own, else
    the book's. A focal character is filled from the scene's cast only when the
    chosen person needs one and none was given.

    An override given as a mapping is a partial one — it changes the fields it
    names and inherits the rest, so asking for present tense keeps the focal
    character it was already following.
    """
    base = doc.voice_for(scene)
    if isinstance(override, dict):
        voice = base.merged(override)
    elif override is not None:
        voice = override.normalized()
    else:
        voice = base.normalized()
    if voice.needs_focal() and not voice.focal and log is not None and span is not None:
        candidates = focal_candidates(log, span[0], span[1])
        if candidates:
            voice = Voice(person=voice.person, tense=voice.tense, focal=candidates[0]["name"])
    return voice


def lead_in(log: Log, start: int) -> str:
    """The tail of the turn before the scene, as read-only context."""
    for msg in reversed(log.messages[: max(0, start)]):
        body = text_mod.clean(msg.text).strip()
        if body:
            flat = body[-LEAD_IN_CHARS:]
            return f"{msg.speaker}: …{flat}" if len(body) > LEAD_IN_CHARS else f"{msg.speaker}: {body}"
    return ""


def prose_lead_in(blocks: list[ms.Block]) -> str:
    """Tail of already-novelized prose — continuity for the next chunk."""
    if not blocks:
        return ""
    text = "\n\n".join(b.text for b in blocks if b.text.strip())
    if not text:
        return ""
    if len(text) <= LEAD_IN_CHARS:
        return text
    return "…" + text[-LEAD_IN_CHARS:]


def split_chunks(
    turns: list[Turn],
    *,
    max_chars: int | None = None,
) -> list[tuple[int, int]]:
    """Pack turns into contiguous ``(start_msg_id, end_msg_id)`` spans under the
    one-pass ceiling. Never splits a turn. Raises ``TooLong`` if a single turn
    itself exceeds the ceiling (un-chunkable)."""
    ceiling = resolve_max_span(max_chars)
    if not turns:
        return []
    chunks: list[tuple[int, int]] = []
    cur: list[Turn] = []
    cur_chars = 0
    for turn in turns:
        size = len(turn.body)
        if size > ceiling:
            raise TooLong(
                f"message {turn.msg_id} is {size:,} characters by itself, over the "
                f"{ceiling:,} one-pass ceiling — cannot chunk further."
            )
        if cur and cur_chars + size > ceiling:
            chunks.append((cur[0].msg_id, cur[-1].msg_id))
            cur = []
            cur_chars = 0
        cur.append(turn)
        cur_chars += size
    if cur:
        chunks.append((cur[0].msg_id, cur[-1].msg_id))
    return chunks


def chunk_plan(
    turns: list[Turn],
    *,
    max_chars: int | None = None,
) -> list[dict[str, int]]:
    """Plan payload rows for each chunk — empty when nothing to novelize."""
    try:
        spans = split_chunks(turns, max_chars=max_chars)
    except TooLong:
        return []
    rows: list[dict[str, int]] = []
    for lo, hi in spans:
        slice_turns = [t for t in turns if lo <= t.msg_id <= hi]
        rows.append({
            "from": lo,
            "to": hi,
            "source_chars": source_chars(slice_turns),
            "turns": len(slice_turns),
        })
    return rows


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def voice_block(voice: Voice) -> str:
    person = PERSON_GUIDANCE.get(voice.person, PERSON_GUIDANCE["close_third"])
    focal = voice.focal or "the scene's central character"
    return f"{person.format(focal=focal)}\n{TENSE_GUIDANCE.get(voice.tense, TENSE_GUIDANCE['past'])}"


SYSTEM = """You are a novelist adapting a roleplay transcript into narrative prose.

You are transposing form, not rewriting content. The transcript's events are the
novel's events. Every action, every spoken line, every revelation, every gesture
that carries meaning, and the order they occur in — all of it survives the pass.

VOICE
{voice}

WHAT TO CHANGE
- Turn form becomes scene form. Merge the turns into continuous prose: no turn
  boundaries, no speaker prefixes, no headers, no out-of-character remarks.
- Stage directions in asterisks become narration.
- Second-person address in the source is the director playing a character. Render
  it in the voice above like any other material.
- Dialogue tags and beats are yours to write, place, and vary.
- Sentence rhythm and paragraphing are yours. Break where the prose wants it.

WHAT NOT TO CHANGE
- Do not invent events, gestures, objects, dialogue, decisions, or knowledge.
- Speaker identity is an event, not a stylistic choice. Never transfer a spoken
  line, action, perception, thought, or reaction from one character to another.
  In a turn containing several characters, preserve every explicitly named or
  tagged speaker exactly; do not infer ownership from the turn's card name.
- Do not add interiority the source does not show or plainly imply.
- Do not summarise. This is not a condensation; a scene should come out at
  comparable length to what went in.
- Do not soften, escalate, or omit explicit or violent material. Render it at the
  explicitness of the source, in the same register. A later pass handles that
  question and depends on this one being faithful.
- Keep the source's diction, imagery and idiom. Where a phrase in the transcript
  already reads as prose, keep the phrase.
- Keep names, titles, and terms of address exactly as the source spells them.

OUTPUT
Paragraphs separated by blank lines. Begin every paragraph with the number of the
source turn it draws on, in guillemets: «3» — or «3,4» where a paragraph joins
two. Nothing else: no heading, no scene title, no commentary, no summary, no
notes on what you did."""


def build_messages(
    turns: list[Turn],
    voice: Voice,
    *,
    context: str = "",
    where: str = "",
    direction: str = "",
) -> list[dict[str, str]]:
    """The novelization prompt. ``direction`` is the author's note on a re-roll —
    what was wrong with the last attempt."""
    parts: list[str] = []
    if where:
        parts.append(f"SCENE: {where}")
    if context:
        parts.append(
            "PRECEDING CONTEXT (for continuity only — do not render any of this):\n"
            + context
        )
    if direction:
        parts.append(
            "THE AUTHOR REJECTED YOUR PREVIOUS ATTEMPT AND ASKS FOR THIS:\n" + direction
        )

    body = "\n\n".join(f"«{t.index}» {t.label()}: {t.body}" for t in turns)
    parts.append(f"TRANSCRIPT — {len(turns)} turns to transpose:\n\n{body}")
    parts.append(
        "Write the scene as prose now, in the voice given, marking each paragraph "
        "with its source turn number."
    )
    return [
        {"role": "system", "content": SYSTEM.format(voice=voice_block(voice))},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def max_output_tokens(turns: list[Turn]) -> int:
    override = os.environ.get("STORY_EDITOR_NOVELIZE_MAX_TOKENS", "").strip()
    if override.isdigit():
        return max(1024, int(override))
    budget = (
        int(source_chars(turns) / OUTPUT_CHARS_PER_TOKEN) + REASONING_HEADROOM_TOKENS
    )
    return max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, budget))


CONTINUE_INSTRUCTION = (
    "You were cut off. Continue the scene from exactly where you stopped — finish "
    "the interrupted sentence first, then carry on to the end of the transcript. "
    "Do not restart, do not summarise what you already wrote, and keep marking "
    "paragraphs with their source turn numbers."
)


def generate(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, bool]:
    """The model call, continued when it runs out of room. Returns the prose and
    whether it is still unfinished after the last attempt."""
    reply = llm.complete(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    prose = reply.text
    attempts = 0
    while reply.truncated and prose and attempts < MAX_CONTINUATIONS:
        attempts += 1
        reply = llm.complete(
            [
                *messages,
                {"role": "assistant", "content": prose},
                {"role": "user", "content": CONTINUE_INSTRUCTION},
            ],
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not reply.text:
            break
        # The continuation resumes mid-sentence; a blank line here would invent a
        # paragraph break the model did not ask for.
        joiner = "" if prose.rstrip() != prose or reply.text[:1].isspace() else " "
        prose = f"{prose}{joiner}{reply.text}"
    return prose, reply.truncated


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #


# Provenance by lexical alignment, for the paragraphs the model left unmarked.
# Dialogue survives novelization nearly verbatim and distinctive nouns survive
# entirely, so a paragraph's source is usually recoverable from the words. Order
# is preserved by construction — prose follows the transcript — so the search is
# monotonic, which is what keeps it from matching a stray echo fifty turns away.
_WORD = re.compile(r"[A-Za-z']{5,}")
_QUOTED = re.compile(r"[\"“]([^\"”]{12,})[\"”]")

# Below this share of shared distinctive words, a match is noise; the paragraph
# inherits instead, which is at least honest about being a guess.
_ALIGN_FLOOR = 0.16
_QUOTE_BONUS = 0.45


def _distinctive(s: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(s)}


def _quotes(s: str) -> list[str]:
    return [q.strip().lower() for q in _QUOTED.findall(s)]


def _align_score(para: str, para_words: set[str], turn_words: set[str], turn_quotes: list[str]) -> float:
    if not para_words:
        return 0.0
    overlap = len(para_words & turn_words) / len(para_words)
    lowered = para.lower()
    for quote in turn_quotes:
        # Spoken words carry over; a shared long quote is near-proof of source.
        if quote in lowered or (len(quote) > 40 and quote[:40] in lowered):
            return overlap + _QUOTE_BONUS
    return overlap


def align_unmarked(blocks: list[ms.Block], turns: list[Turn]) -> int:
    """Give every unattributed paragraph its most likely source turn, moving
    forward through the transcript only. Returns how many are still unattributed
    and will inherit from the paragraph above them."""
    if not turns:
        return sum(1 for b in blocks if not b.src)
    words = {t.index: _distinctive(t.body) for t in turns}
    quotes = {t.index: _quotes(t.body) for t in turns}
    index_of = {t.uid: t.index for t in turns}

    cursor = turns[0].index
    unresolved = 0
    for block in blocks:
        if block.src:
            cursor = max(cursor, max(index_of.get(uid, cursor) for uid in block.src))
            continue
        para_words = _distinctive(block.text)
        best_index, best_score = 0, 0.0
        for turn in turns:
            if turn.index < cursor:
                continue
            score = _align_score(block.text, para_words, words[turn.index], quotes[turn.index])
            if score > best_score:
                best_index, best_score = turn.index, score
        if best_index and best_score >= _ALIGN_FLOOR:
            block.src = [next(t.uid for t in turns if t.index == best_index)]
            cursor = best_index
        else:
            unresolved += 1
    return unresolved


def inherit_unmarked(blocks: list[ms.Block], turns: list[Turn]) -> None:
    """Last resort: a paragraph with no source of its own belongs to whatever the
    paragraph above it belonged to."""
    fallback = [t.uid for t in turns]
    previous: list[str] = []
    for block in blocks:
        if block.src:
            previous = block.src
        else:
            block.src = list(previous) or list(fallback)


def parse_prose(raw: str, turns: list[Turn]) -> tuple[list[ms.Block], int]:
    """Split prose into blocks and read each paragraph's source markers.

    Returns the blocks and how many paragraphs arrived without a usable marker.
    Blocks left unmarked have an empty ``src``: filling it is the aligner's job,
    which is a better guess than this function could make in one pass.
    """
    by_index = {t.index: t.uid for t in turns}

    body = raw.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```\s*$", "", body).strip()

    blocks: list[ms.Block] = []
    unmarked = 0

    for chunk in re.split(r"\n\s*\n", body.replace("\r\n", "\n")):
        para = chunk.strip()
        if not para or _PREAMBLE.match(para):
            continue

        src: list[str] = []
        lead = _LEAD_MARKER.match(para)
        if lead:
            para = para[lead.end() :].strip()
            for piece in lead.group(1).split(","):
                piece = piece.strip()
                if piece.isdigit() and int(piece) in by_index:
                    uid = by_index[int(piece)]
                    if uid not in src:
                        src.append(uid)

        # Markers the model scattered mid-paragraph still name real sources;
        # take them, then take them out of the prose.
        for inner in _MARKER.finditer(para):
            for piece in inner.group(1).split(","):
                piece = piece.strip()
                if piece.isdigit() and int(piece) in by_index:
                    uid = by_index[int(piece)]
                    if uid not in src:
                        src.append(uid)
        para = _MARKER.sub("", para).strip()
        para = re.sub(r"[ \t]{2,}", " ", para)
        if not para:
            continue

        if not src:
            unmarked += 1
        blocks.append(ms.Block(id=ms.mint_block_id(), text=para, src=src))

    return blocks, unmarked


# --------------------------------------------------------------------------- #
# The operator
# --------------------------------------------------------------------------- #


@dataclass
class Proposal:
    """A novelized scene, not yet filed."""

    scene: ms.Scene
    voice: Voice
    turns: int
    source_chars: int
    prose_chars: int
    unmarked: int  # paragraphs the model did not mark
    unresolved: int  # ...of which the aligner could not place either
    model: str
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "scene": self.scene.to_json(),
            "text": self.scene.text(),
            "voice": self.voice.to_json(),
            "voice_label": self.voice.describe(),
            "turns": self.turns,
            "source_chars": self.source_chars,
            "paragraphs": len(self.scene.blocks),
            "prose_chars": self.prose_chars,
            "unmarked_paragraphs": self.unmarked,
            "unresolved_paragraphs": self.unresolved,
            "model": self.model,
            "warnings": list(self.warnings),
        }


class TooLong(ValueError):
    """The span exceeds what one pass can hold honestly."""


def scene_title(log: Log, start: int, end: int) -> str:
    """Name the scene after the log's own segmentation, so a manuscript scene is
    recognisable beside the scene it came from."""
    for scene in structure_mod.segment_scenes(log):
        if scene.start_msg_id <= start <= scene.end_msg_id:
            return scene.title()
    return ""


def plan(
    log: Log,
    start: int,
    end: int,
    *,
    doc: ms.Document | None = None,
    override: Voice | dict[str, Any] | None = None,
    max_span_chars: int | None = None,
) -> dict[str, Any]:
    """What a novelization pass would do, without doing it: the voice it would
    use, the cast it could follow, the size of the job, and whether this span has
    already been novelized."""
    document = doc if doc is not None else ms.load(layers_mod.MANUSCRIPT, log=log)
    existing = document.covering(start)
    voice = resolve_voice(
        document, scene=existing, override=override, log=log, span=(start, end)
    )
    turns = turns_for(log, start, end)
    chars = source_chars(turns)
    ceiling = resolve_max_span(max_span_chars)
    chunks: list[dict[str, int]] = []
    chunk_count = 0
    unchunkable = False
    if turns:
        try:
            spans = split_chunks(turns, max_chars=ceiling)
            chunk_count = len(spans)
            chunks = chunk_plan(turns, max_chars=ceiling)
        except TooLong:
            unchunkable = True
            chunk_count = 0
            chunks = []
    return {
        "from": start,
        "to": end,
        "title": scene_title(log, start, end),
        "turns": len(turns),
        "source_chars": chars,
        "max_span_chars": ceiling,
        "too_long": chars > ceiling,
        "chunk_count": chunk_count,
        "chunks": chunks,
        "unchunkable": unchunkable,
        "voice": voice.to_json(),
        "voice_label": voice.describe(),
        "book_voice": document.voice.to_json(),
        "persons": ms.PERSONS,
        "tenses": ms.TENSES,
        "focal_candidates": focal_candidates(log, start, end),
        "existing": (
            {
                "id": existing.id,
                "status": existing.status,
                "generated": existing.generated,
                "voice": (existing.voice or document.voice).to_json(),
            }
            if existing
            else None
        ),
    }


def _propose_chunk(
    log: Log,
    chunk_start: int,
    chunk_end: int,
    *,
    voice: Voice,
    context: str,
    where: str,
    direction: str,
    model: str,
    temperature: float,
) -> tuple[list[ms.Block], int, int, bool, list[str]]:
    """Novelize one contiguous chunk. Returns blocks, unmarked, unresolved,
    unfinished, and local warnings."""
    turns = turns_for(log, chunk_start, chunk_end)
    if not turns:
        return [], 0, 0, False, []
    raw, unfinished = generate(
        build_messages(
            turns,
            voice,
            context=context,
            where=where,
            direction=direction,
        ),
        model=model,
        temperature=temperature,
        max_tokens=max_output_tokens(turns),
    )
    blocks, unmarked = parse_prose(raw, turns)
    if not blocks:
        raise llm.ModelError(
            f"novelization returned no prose for messages {chunk_start}–{chunk_end}"
        )
    unresolved = align_unmarked(blocks, turns)
    inherit_unmarked(blocks, turns)
    warnings: list[str] = []
    if unfinished:
        warnings.append(
            f"chunk msgs {chunk_start}–{chunk_end}: still cut off after continuing "
            "twice — that stretch may stop early."
        )
    return blocks, unmarked, unresolved, unfinished, warnings


def propose(
    log: Log,
    start: int,
    end: int,
    *,
    doc: ms.Document | None = None,
    voice: Voice | dict[str, Any] | None = None,
    direction: str = "",
    model: str | None = None,
    temperature: float = TEMPERATURE,
    on_progress: ProgressFn | None = None,
    max_span_chars: int | None = None,
    continuity_context: str = "",
) -> Proposal:
    """Novelize one span. Proposes; writes nothing.

    Spans over the one-pass ceiling are split into contiguous message chunks,
    generated sequentially with prose continuity lead-in, then merged into one
    manuscript scene. ``TooLong`` only if a single message cannot fit.
    """
    layers_mod.check("novelize", layers_mod.MANUSCRIPT)
    if end < start:
        raise ValueError(f"empty span: {start}..{end}")

    def progress(stage: str, detail: str = "", *, step: int = 0, total: int = 0) -> None:
        if on_progress is not None:
            on_progress(stage, detail, step=step, total=total)

    document = doc if doc is not None else ms.load(layers_mod.MANUSCRIPT, log=log)
    existing = document.covering(start)
    resolved = resolve_voice(
        document, scene=existing, override=voice, log=log, span=(start, end)
    )

    turns = turns_for(log, start, end)
    if not turns:
        raise ValueError(f"nothing to novelize in {start}..{end}: every turn is empty")
    chars = source_chars(turns)
    ceiling = resolve_max_span(max_span_chars)
    chunk_spans = split_chunks(turns, max_chars=ceiling)  # raises TooLong if unchunkable
    resolved_model = model or config.model_for_task("novelize")
    where = scene_title(log, start, end)
    n_chunks = len(chunk_spans)
    multi = n_chunks > 1

    progress(
        "novelize",
        f"{'chunked · ' if multi else ''}{n_chunks} pass{'es' if multi else ''} · "
        f"msgs {start}–{end}",
        step=0,
        total=n_chunks,
    )

    all_blocks: list[ms.Block] = []
    unmarked = 0
    unresolved = 0
    warnings: list[str] = []
    if multi:
        warnings.append(
            f"chunked: {n_chunks} passes over {chars:,} source characters "
            f"(≤{ceiling:,} per pass)"
        )
    for i, (c_start, c_end) in enumerate(chunk_spans):
        progress(
            "chunk",
            f"chunk {i + 1}/{n_chunks} · msgs {c_start}–{c_end}",
            step=i + 1,
            total=n_chunks,
        )
        if i == 0:
            context = lead_in(log, c_start)
            if continuity_context.strip():
                prior = continuity_context.strip()
                context = (
                    f"{context}\n\n" if context else ""
                ) + (
                    "PRECEDING MANUSCRIPT TAIL — continuity reference only; "
                    "do not recap or repeat it:\n" + prior
                )
            chunk_direction = direction
        else:
            context = prose_lead_in(all_blocks)
            # Direction applies to the first pass only; later chunks follow the
            # emerging prose rather than re-arguing the rejected take.
            chunk_direction = ""
        blocks, u_mark, u_res, _unfinished, chunk_warns = _propose_chunk(
            log,
            c_start,
            c_end,
            voice=resolved,
            context=context,
            where=where,
            direction=chunk_direction,
            model=resolved_model,
            temperature=temperature,
        )
        all_blocks.extend(blocks)
        unmarked += u_mark
        unresolved += u_res
        warnings.extend(chunk_warns)

    if not all_blocks:
        raise llm.ModelError("novelization returned no prose")

    scene = ms.new_scene(
        log,
        start,
        end,
        blocks=all_blocks,
        title=where,
        model=resolved_model,
        voice=resolved,
    )
    if existing is not None:
        # A re-roll replaces the scene in place: annotations and the review
        # verdict hang off the scene id, and losing them to a regenerate would
        # make trying a second take expensive.
        scene.id = existing.id

    prose_chars = len(scene.text())
    if unresolved:
        warnings.append(
            f"{unresolved} of {len(all_blocks)} paragraphs could not be traced to a "
            "source turn; they inherit the provenance of the paragraph above."
        )
    if prose_chars < chars * 0.55:
        warnings.append(
            f"prose is {prose_chars:,} characters against {chars:,} of source — "
            "check for summarising rather than transposing."
        )
    covered = {uid for b in all_blocks for uid in b.src}
    missed = [t for t in turns if t.uid not in covered]
    if missed:
        warnings.append(
            f"{len(missed)} source turns are cited by no paragraph "
            f"(messages {', '.join(str(t.msg_id) for t in missed[:6])}"
            f"{'…' if len(missed) > 6 else ''}) — they may have been dropped."
        )

    progress("done", f"{len(all_blocks)} paragraphs", step=n_chunks, total=n_chunks)

    return Proposal(
        scene=scene,
        voice=resolved,
        turns=len(turns),
        source_chars=chars,
        prose_chars=prose_chars,
        unmarked=unmarked,
        unresolved=unresolved,
        model=resolved_model,
        warnings=warnings,
    )


def commit(proposal: Proposal, *, doc: ms.Document | None = None, log: Log | None = None) -> ms.Document:
    """File a proposal in the manuscript. Its status stays ``draft`` — approval
    is the author's, made on the page."""
    document = doc if doc is not None else ms.load(layers_mod.MANUSCRIPT, log=log)
    document.upsert(proposal.scene)
    ms.save(document)
    return document


# --------------------------------------------------------------------------- #
# Batch runner (Phase 3.2)
# --------------------------------------------------------------------------- #


@dataclass
class BatchItem:
    start: int
    end: int
    title: str = ""
    scene_id: str = ""
    status: str = ""  # novelized | skipped | failed
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    attempts: int = 0
    elapsed_seconds: float = 0.0
    log_scene_id: int | None = None
    episode_id: int | None = None
    episode_title: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "from": self.start,
            "to": self.end,
            "title": self.title,
            "status": self.status,
        }
        if self.scene_id:
            out["scene_id"] = self.scene_id
        if self.error:
            out["error"] = self.error
        if self.warnings:
            out["warnings"] = self.warnings
        out["attempts"] = self.attempts
        out["elapsed_seconds"] = round(self.elapsed_seconds, 3)
        if self.log_scene_id is not None:
            out["log_scene_id"] = self.log_scene_id
        if self.episode_id is not None:
            out["episode_id"] = self.episode_id
        if self.episode_title:
            out["episode_title"] = self.episode_title
        return out


@dataclass
class BatchResult:
    novelized: list[BatchItem] = field(default_factory=list)
    skipped: list[BatchItem] = field(default_factory=list)
    failed: list[BatchItem] = field(default_factory=list)
    campaign: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out = {
            "novelized": [i.to_json() for i in self.novelized],
            "skipped": [i.to_json() for i in self.skipped],
            "failed": [i.to_json() for i in self.failed],
            "counts": {
                "novelized": len(self.novelized),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
        }
        if self.campaign:
            out["campaign"] = self.campaign
        return out


@dataclass(frozen=True)
class NovelizeUnit:
    """One batch job, split at both log-scene and canonical episode seams."""

    start: int
    end: int
    log_scene_id: int
    episode_id: int | None = None
    episode_title: str = ""


def orchestration_units(log: Log) -> list[NovelizeUnit]:
    """Build deterministic jobs that never cross a locked episode boundary."""
    episodes = structure_mod.load_derived_spine(log)
    beats = episodes.beats if episodes and episodes.unit_type == "episode" else []
    out: list[NovelizeUnit] = []
    for scene in structure_mod.segment_scenes(log):
        cuts = {scene.start_msg_id, scene.end_msg_id + 1}
        for beat in beats:
            if scene.start_msg_id < beat.start_msg_id <= scene.end_msg_id:
                cuts.add(beat.start_msg_id)
            if scene.start_msg_id <= beat.end_msg_id < scene.end_msg_id:
                cuts.add(beat.end_msg_id + 1)
        points = sorted(cuts)
        for start, stop in zip(points, points[1:]):
            beat = next(
                (b for b in beats if b.start_msg_id <= start <= b.end_msg_id),
                None,
            )
            out.append(NovelizeUnit(
                start=start,
                end=stop - 1,
                log_scene_id=scene.scene_id,
                episode_id=beat.beat_id if beat else None,
                episode_title=beat.title if beat else "",
            ))
    return out


def previous_prose_tail(doc: ms.Document, start: int, *, chars: int = 3000) -> str:
    """Tail of the nearest earlier manuscript scene for cross-scene continuity."""
    prior = [
        scene for scene in doc.scenes
        if not ms.is_front_matter(scene) and scene.anchor.end < start and scene.text()
    ]
    if not prior:
        return ""
    body = max(prior, key=lambda scene: (scene.anchor.end, scene.id)).text()
    return body[-chars:]


def _log_sha256(log: Log) -> str:
    return hashlib.sha256(log.path.read_bytes()).hexdigest()


def _campaign_path() -> Path:
    return Path(config.WORKSPACE_DIR) / "novelize_campaign.json"


def _write_campaign(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def assemble_private_markdown(
    doc: ms.Document,
    log: Log,
    *,
    approved_only: bool = False,
    chapter_ids: set[int] | None = None,
) -> str:
    """Assemble the body under the locked canonical chapters.

    Negative-anchor front matter is deliberately excluded: the official
    prologue and epilogue remain separate PDF bookends and are concatenated
    only when the final book is rendered.
    """
    beats = structure_mod.chapter_beats(log)
    parts: list[str] = []
    for beat in beats:
        include_chapter = chapter_ids is None or beat.beat_id in chapter_ids
        chapter_scenes = [
            scene for scene in doc.scenes
            if not ms.is_front_matter(scene)
            and beat.start_msg_id <= scene.anchor.start <= beat.end_msg_id
            and (not approved_only or scene.status == "approved")
            and scene.text()
        ]
        if not chapter_scenes:
            continue
        if include_chapter:
            parts.append(f'<h1 align="center">{escape(str(beat.title))}</h1>')
            for scene in sorted(chapter_scenes, key=lambda s: (s.anchor.start, s.id)):
                parts.append(scene.text())
    if not beats:
        body = "\n\n".join(
            scene.text()
            for scene in sorted(doc.scenes, key=lambda s: (s.anchor.start, s.id))
            if not ms.is_front_matter(scene)
            and (not approved_only or scene.status == "approved")
            and scene.text()
        ).strip()
        if body:
            parts.append(body)
    return "\n\n".join(parts) + ("\n" if parts else "")


def assemble_private_text(
    doc: ms.Document,
    log: Log,
    *,
    chapter_ids: set[int] | None = None,
) -> str:
    """A recovery-friendly body snapshot with no layout or provenance markup.

    Scene prose is preserved verbatim—including intentional emphasis markers—
    while PDF bookends, HTML ornaments, and Markdown heading sigils stay out.
    """
    beats = structure_mod.chapter_beats(log)
    parts: list[str] = []
    for beat in beats:
        if chapter_ids is not None and beat.beat_id not in chapter_ids:
            continue
        chapter_scenes = [
            scene for scene in doc.scenes
            if not ms.is_front_matter(scene)
            and beat.start_msg_id <= scene.anchor.start <= beat.end_msg_id
            and scene.text()
        ]
        if not chapter_scenes:
            continue
        parts.append(str(beat.title))
        parts.extend(
            scene.text()
            for scene in sorted(chapter_scenes, key=lambda s: (s.anchor.start, s.id))
        )
    if not beats:
        parts.extend(
            scene.text()
            for scene in sorted(doc.scenes, key=lambda s: (s.anchor.start, s.id))
            if not ms.is_front_matter(scene) and scene.text()
        )
    return "\n\n".join(parts) + ("\n" if parts else "")


def write_private_text_autosave(
    doc: ms.Document,
    log: Log,
    path: Path | None = None,
) -> Path:
    """Atomically snapshot the whole manuscript body after a hand edit."""
    target = path or (Path(config.WORKSPACE_DIR) / "private_manuscript_autosave.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(assemble_private_text(doc, log), encoding="utf-8")
    temporary.replace(target)
    return target


def pending_scenes(
    log: Log,
    doc: ms.Document | None = None,
    *,
    regenerate: bool = False,
) -> list[NovelizeUnit]:
    """Log scenes not yet covered by the manuscript (resume = skip done)."""
    document = doc if doc is not None else ms.load(layers_mod.MANUSCRIPT, log=log)
    out: list[NovelizeUnit] = []
    for scene in orchestration_units(log):
        fully_covered = all(
            document.covering(msg_id) is not None
            for msg_id in range(scene.start, scene.end + 1)
        )
        if not fully_covered or regenerate:
            out.append(scene)
    return out


def boundary_crossing_scenes(
    log: Log,
    doc: ms.Document,
) -> list[ms.Scene]:
    """Existing prose that straddles a current orchestration boundary.

    Several manuscript scenes may safely partition one orchestration unit, but
    one old scene may not cross from one current unit into another. Generating
    either side of such a stale boundary would create overlapping prose.
    """
    units = orchestration_units(log)
    return [
        scene for scene in doc.scenes
        if not ms.is_front_matter(scene)
        and not any(
            unit.start <= scene.anchor.start and scene.anchor.end <= unit.end
            for unit in units
        )
    ]


def run_batch(
    log: Log,
    *,
    doc: ms.Document | None = None,
    voice: Voice | dict[str, Any] | None = None,
    model: str | None = None,
    episode_id: int | None = None,
    max_scenes: int | None = None,
    max_span_chars: int | None = None,
    regenerate: bool = False,
    stop_on_error: bool = False,
    max_retries: int = 2,
    continuity_chars: int = 3000,
    campaign_path: Path | None = None,
    assemble_path: Path | None = None,
    backup: bool = True,
    on_progress: ProgressFn | None = None,
) -> BatchResult:
    """Novelize every pending scene in log order. Resumable: already-filed
    scenes are skipped unless ``regenerate``. Per-scene failures are collected
    rather than aborting the batch (unless ``stop_on_error``)."""
    layers_mod.check("novelize", layers_mod.MANUSCRIPT)
    document = doc if doc is not None else ms.load(layers_mod.MANUSCRIPT, log=log)
    crossing = boundary_crossing_scenes(log, document)
    if crossing:
        spans = ", ".join(
            f"{scene.id} ({scene.anchor.start}–{scene.anchor.end})"
            for scene in crossing[:5]
        )
        more = "…" if len(crossing) > 5 else ""
        raise RuntimeError(
            "manuscript scenes cross current orchestration boundaries: "
            f"{spans}{more}; reconcile or regenerate those spans before batch"
        )
    pending = pending_scenes(log, document, regenerate=regenerate)
    if episode_id is not None:
        wanted_episode = int(episode_id)
        pending = [scene for scene in pending if scene.episode_id == wanted_episode]
    if max_scenes is not None:
        pending = pending[: max(0, int(max_scenes))]

    result = BatchResult()
    total = len(pending)
    pending_starts = {s.start for s in pending}
    source_hash = _log_sha256(log)
    campaign_file = Path(campaign_path or _campaign_path())
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    backup_path = ""
    manuscript_path = ms.path_for(ms.MANUSCRIPT_LAYER)
    if backup and manuscript_path.exists() and pending:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = Path(config.BACKUP_DIR) / f"manuscript.{stamp}-pre-batch.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manuscript_path, target)
        backup_path = str(target)
    campaign: dict[str, Any] = {
        "schema": "story-editor/novelize-campaign@1",
        "status": "running",
        "started": started,
        "log": str(log.path.resolve()),
        "log_sha256": source_hash,
        "model": model or config.model_for_task("novelize"),
        "voice": (Voice.from_json(voice) if isinstance(voice, dict) else voice or document.voice).to_json(),
        "pending_at_start": len(pending),
        "max_retries": max(0, int(max_retries)),
        "continuity_chars": max(0, int(continuity_chars)),
        "backup": backup_path,
        "items": [],
    }
    _write_campaign(campaign_file, campaign)

    def progress(stage: str, detail: str = "", *, step: int = 0, total: int = 0) -> None:
        if on_progress is not None:
            on_progress(stage, detail, step=step, total=total)

    # Also report what we are skipping so the UI can show resume honestly.
    if not regenerate:
        for scene in orchestration_units(log):
            if scene.start in pending_starts:
                continue
            existing = document.covering(scene.start)
            if existing is None:
                continue
            result.skipped.append(BatchItem(
                start=scene.start,
                end=scene.end,
                title=scene_title(log, scene.start, scene.end),
                scene_id=existing.id,
                status="skipped",
                log_scene_id=scene.log_scene_id,
                episode_id=scene.episode_id,
                episode_title=scene.episode_title,
            ))

    progress("batch", f"{total} pending · {len(result.skipped)} already done", step=0, total=total)

    for i, scene in enumerate(pending):
        start, end = scene.start, scene.end
        title = scene_title(log, start, end)
        progress(
            "batch-scene",
            f"scene {i + 1}/{total} · msgs {start}–{end} · {title}",
            step=i + 1,
            total=total,
        )
        attempts = 0
        tick = time.perf_counter()
        last_exc: Exception | None = None
        proposal: Proposal | None = None
        while attempts <= max(0, int(max_retries)):
            attempts += 1
            try:
                if _log_sha256(log) != source_hash:
                    raise RuntimeError("source log changed during novelization campaign")
                proposal = propose(
                    log, start, end,
                    doc=document,
                    voice=voice,
                    model=model,
                    max_span_chars=max_span_chars,
                    continuity_context=previous_prose_tail(
                        document, start, chars=max(0, int(continuity_chars))
                    ),
                    on_progress=on_progress,
                )
                break
            except Exception as exc:  # noqa: BLE001 — campaign records exact failure
                last_exc = exc
                progress(
                    "batch-retry",
                    f"msgs {start}–{end} · attempt {attempts} failed: {exc}",
                    step=i + 1,
                    total=total,
                )
        elapsed = time.perf_counter() - tick
        if proposal is not None:
            proposal.scene.log_scene_id = scene.log_scene_id
            proposal.scene.episode_id = scene.episode_id
            proposal.scene.episode_title = scene.episode_title
            proposal.scene.narrative_scene_id = f"ns-{start}-{end}"
            commit(proposal, doc=document)
            item = BatchItem(
                start=start, end=end, title=title, scene_id=proposal.scene.id,
                status="novelized", warnings=list(proposal.warnings),
                attempts=attempts, elapsed_seconds=elapsed,
                log_scene_id=scene.log_scene_id, episode_id=scene.episode_id,
                episode_title=scene.episode_title,
            )
            result.novelized.append(item)
            campaign["items"].append(item.to_json())
            _write_campaign(campaign_file, campaign)
        else:
            exc = last_exc or RuntimeError("novelization failed without an exception")
            item = BatchItem(
                start=start,
                end=end,
                title=title,
                status="failed",
                error=str(exc),
                attempts=attempts,
                elapsed_seconds=elapsed,
                log_scene_id=scene.log_scene_id,
                episode_id=scene.episode_id,
                episode_title=scene.episode_title,
            )
            result.failed.append(item)
            campaign["items"].append(item.to_json())
            _write_campaign(campaign_file, campaign)
            progress("batch-error", f"msgs {start}–{end}: {exc}", step=i + 1, total=total)
            if stop_on_error:
                break

    export_file = Path(assemble_path or (Path(config.WORKSPACE_DIR) / "private_manuscript.md"))
    export_file.write_text(
        assemble_private_markdown(document, log, approved_only=False), encoding="utf-8"
    )
    campaign["status"] = "complete" if not result.failed else "complete_with_failures"
    campaign["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    campaign["counts"] = result.to_json()["counts"]
    campaign["assembled"] = str(export_file)
    _write_campaign(campaign_file, campaign)
    result.campaign = {
        "path": str(campaign_file),
        "status": campaign["status"],
        "backup": backup_path,
        "assembled": str(export_file),
    }
    progress(
        "batch-done",
        f"{len(result.novelized)} novelized · {len(result.failed)} failed · "
        f"{len(result.skipped)} skipped",
        step=total,
        total=total,
    )
    return result
