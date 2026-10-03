"""Phase 5 — canon layer.

Loads per-character bibles from canon/characters.json and provides query
functions used by the transform operators (restyle, retune, inject) and by the in-character audit (``canon check``).

Two extra capabilities:
- ``beat_context_block(msg_id)`` — reads the derived spine and returns a concise
  narrative-GPS block for the beat containing ``msg_id``.  Injected into operator
  prompts so the model knows the beat's function and doesn't accidentally resolve
  or defuse it.
- ``check_in_character(edit, char)`` — a focused LLM call that audits one pending
  edit for character consistency.  Returns a dict with ``verdict`` (in_character /
  concerns / out_of_character), ``reason``, and ``specific_issues``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config, llm


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CharacterBible:
    key: str
    label: str
    role: str
    aliases: list[str] = field(default_factory=list)
    channel: str = ""
    bias_lens: str = ""
    voice_rules: list[str] = field(default_factory=list)
    forbidden_phrasings: list[str] = field(default_factory=list)
    aesthetic_rules: list[str] = field(default_factory=list)
    canonical_facts: list[str] = field(default_factory=list)
    relationship_notes: dict[str, str] = field(default_factory=dict)
    seed_queries: list[str] = field(default_factory=list)

    def all_names(self) -> list[str]:
        return [self.key, self.label] + self.aliases


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_characters_json() -> dict[str, CharacterBible]:
    """Load canon/characters.json."""
    path = config.CHARACTERS_JSON
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, CharacterBible] = {}
    for key, entry in raw.get("characters", {}).items():
        result[key] = CharacterBible(
            key=key,
            label=entry.get("label", key),
            role=entry.get("role", ""),
            aliases=entry.get("aliases", []),
            channel=entry.get("channel", ""),
            bias_lens=entry.get("bias_lens", ""),
            voice_rules=entry.get("voice_rules", []),
            forbidden_phrasings=entry.get("forbidden_phrasings", []),
            aesthetic_rules=entry.get("aesthetic_rules", []),
            canonical_facts=entry.get("canonical_facts", []),
            relationship_notes=entry.get("relationship_notes", {}),
            seed_queries=entry.get("seed_queries", []),
        )
    return result


def all_bibles() -> dict[str, CharacterBible]:
    """Every character bible in the project's canon/characters.json."""
    return dict(_load_characters_json())


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def get_character(name: str) -> CharacterBible | None:
    """Return the CharacterBible whose name or alias matches ``name``.

    Matching is case-insensitive, normalised (strip, lower).  Tries exact key /
    alias match first, then substring containment.
    """
    needle = name.strip().lower()
    bibles = all_bibles()

    # 1. Exact match on key
    if needle in bibles:
        return bibles[needle]

    # 2. Match on any alias (exact, case-insensitive)
    for char in bibles.values():
        if any(needle == a.lower() for a in char.all_names()):
            return char

    # 3. Substring: the needle appears in any alias, or any alias appears in needle
    for char in bibles.values():
        for a in char.all_names():
            al = a.lower()
            if needle in al or al in needle:
                return char

    return None


# ---------------------------------------------------------------------------
# Prompt-ready rendering
# ---------------------------------------------------------------------------

def render_canon_block(char: CharacterBible) -> str:
    """Return a compact, directive CHARACTER CANON block for prompt injection.

    Designed to be dropped into the operator's user-message without ballooning
    the prompt — keeps each section to a handful of lines.
    """
    lines = [f"CHARACTER CANON — {char.label}"]
    lines.append(f"Role: {char.role}")

    if char.channel:
        # Take only the first sentence of the (sometimes very long) channel desc
        first_sentence = char.channel.split("\n")[0].split(". ")[0].rstrip(".") + "."
        lines.append(f"Perceptual lens: {first_sentence}")

    if char.voice_rules:
        lines.append("Voice:")
        for r in char.voice_rules:
            lines.append(f"  • {r}")

    if char.forbidden_phrasings:
        lines.append("Must not:")
        for p in char.forbidden_phrasings:
            lines.append(f"  • {p}")

    if char.aesthetic_rules:
        lines.append("Aesthetic notes:")
        for a in char.aesthetic_rules:
            lines.append(f"  • {a}")

    if char.canonical_facts:
        # Only surface the first three to keep it concise
        lines.append("Always true:")
        for f_ in char.canonical_facts[:3]:
            lines.append(f"  • {f_}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Beat context
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_spine() -> list[dict]:
    """Load workspace/derived_spine.json as a list of beat dicts."""
    path = config.DERIVED_SPINE
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Support both {"beats": [...]} and a bare list
    if isinstance(raw, list):
        return raw
    return raw.get("beats", [])


def beat_context_block(msg_id: int) -> str:
    """Return a BEAT CONTEXT block for the beat containing ``msg_id``.

    Returns an empty string if the spine is not available or ``msg_id`` falls
    outside all known beats.
    """
    beats = _load_spine()
    if not beats:
        return ""

    target_beat = None
    for beat in beats:
        start = beat.get("start_msg_id", -1)
        end = beat.get("end_msg_id", -1)
        if start <= msg_id <= end:
            target_beat = beat
            break

    if target_beat is None:
        return ""

    beat_id = target_beat.get("beat_id", "?")
    title = target_beat.get("title", "")
    justification = target_beat.get("justification", "")
    kind = target_beat.get("kind", "")
    evidence = target_beat.get("evidence") or []

    block = (
        f"BEAT CONTEXT (read-only — narrative GPS)\n"
        f"This message falls within beat {beat_id}: \"{title}\" ({kind}).\n"
        f"Beat function: {justification}.\n"
    )
    if evidence:
        block += "Grounding (scene cards):\n"
        for line in evidence[:3]:
            block += f"- {line}\n"
    block += (
        "Do not resolve, advance past, or defuse this beat's tension. Write within it."
    )
    return block


# ---------------------------------------------------------------------------
# In-character audit
# ---------------------------------------------------------------------------

_CHECK_SYSTEM = """\
You are a character-consistency editor for a gothic prose roleplay.
You will be given a CHARACTER CANON (voice, constraints, what this character \
must never do) and a PROPOSED REWRITE of one of their turns.
Your task is to decide whether the proposed rewrite is consistent with the \
character canon.

Respond with a JSON object — no prose, no code-fence, just the object:
{
  "verdict": "in_character" | "concerns" | "out_of_character",
  "reason": "<one or two sentences>",
  "specific_issues": ["<issue 1>", ...]   // empty list if no issues
}

Verdicts:
- "in_character"      : the rewrite honours voice, motivation, and constraints
- "concerns"          : minor slippage — worth human review but not a clear violation
- "out_of_character"  : one or more hard constraints violated
""".strip()


_GENERATE_SYSTEM = """\
You are a prose writer for a gothic roleplay. You will be given a CHARACTER CANON \
and a CONSTRAINT to test.
Write a SHORT passage (3-5 sentences) as a turn for that character.
Output only the prose — no preamble, no quotation marks around the whole passage.
""".strip()


def generate_test_passage(
    char: CharacterBible,
    constraint: str,
    *,
    violate: bool,
) -> str:
    """Generate a short passage that either respects or deliberately violates
    one specific constraint from the character's bible.  Used by ``canon test``
    to produce the compliant/violating pair.

    ``constraint`` is the exact text of one rule from ``forbidden_phrasings`` or
    ``voice_rules``.
    """
    canon_block = render_canon_block(char)
    direction = (
        f"DELIBERATELY VIOLATE the following constraint — make it obvious:\n{constraint}"
        if violate
        else
        f"Respect ALL constraints, and in particular this one:\n{constraint}"
    )
    user_msg = (
        f"{canon_block}\n\n"
        f"---\n\n"
        f"{direction}\n\n"
        f"Write a short passage (3-5 sentences) as {char.label}. "
        f"Output only the prose:"
    )
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    temperature = 0.75 if violate else 0.45
    return llm.chat(messages, task="canon_test", temperature=temperature, max_tokens=2000)


def check_in_character(
    before: str,
    after: str,
    speaker: str,
    char: CharacterBible,
) -> dict[str, Any]:
    """Audit one proposed rewrite against the character bible.

    Returns::

        {
            "verdict": "in_character" | "concerns" | "out_of_character",
            "reason": str,
            "specific_issues": list[str],
        }

    Falls back to ``{"verdict": "unknown", "reason": "LLM error", ...}`` on
    failure so callers don't need to guard the return value.
    """
    canon_block = render_canon_block(char)
    user_msg = (
        f"{canon_block}\n\n"
        f"---\n\n"
        f"SPEAKER: {speaker}\n\n"
        f"ORIGINAL (before):\n{before}\n\n"
        f"PROPOSED REWRITE (after):\n{after}"
    )
    messages = [
        {"role": "system", "content": _CHECK_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    try:
        # Reasoning models (Gemma thinking) spend tokens on a plan before the
        # answer, so give generous headroom — too small a ceiling truncates the
        # JSON and yields an "unknown" verdict.
        raw = llm.chat(messages, task="canon_check", temperature=0.15, max_tokens=2500)
        parsed = _extract_json_object(raw)
        if parsed is None:
            return {
                "verdict": "unknown",
                "reason": "could not parse a JSON verdict from the model output",
                "specific_issues": [],
            }
        # Normalise the shape so callers can rely on the keys.
        return {
            "verdict": str(parsed.get("verdict", "unknown")),
            "reason": str(parsed.get("reason", "")),
            "specific_issues": list(parsed.get("specific_issues", []) or []),
        }
    except Exception as exc:
        return {
            "verdict": "unknown",
            "reason": f"audit failed: {exc}",
            "specific_issues": [],
        }


# ---------------------------------------------------------------------------
# Bible source filtering (ST card name ≠ character voiced)
# ---------------------------------------------------------------------------

def _bible_names(key: str) -> set[str]:
    """A character's name and aliases, lowercased; just ``key`` when it has no bible."""
    bible = all_bibles().get((key or "").lower())
    names = {key.lower()}
    if bible is not None:
        names |= {a.lower() for a in [bible.label.split(" — ")[0], *bible.aliases] if a}
    return names


def _npc_name_re() -> re.Pattern | None:
    """Every name and alias in the project's character bibles, as one pattern."""
    names = sorted({a for b in all_bibles().values() for a in [b.label.split(" — ")[0], *b.aliases]
                    if a and len(a) > 1}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.I)
_HE_SHE_SAID_RE = re.compile(
    r"\b(he|she)\s+(said|asked|replied|whispered|murmured)\b",
    re.I,
)


def parse_msg_id_spec(spec: str | None) -> set[int]:
    """Parse ``1,5,10-20`` into a set of message ids."""
    if not spec or not str(spec).strip():
        return set()
    out: set[int] = set()
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start
            out.update(range(start, end + 1))
        else:
            out.add(int(part))
    return out


def is_scene_narrator_turn(text: str) -> bool:
    """Heuristic: a character-card turn carrying heavy third-party dialogue."""
    pattern = _npc_name_re()
    names = len(pattern.findall(text)) if pattern else 0
    he_said = len(_HE_SHE_SAID_RE.findall(text))
    return names >= 2 or (he_said >= 2 and names >= 1)


def scene_narrator_confidence(text: str, exclude: str | None = None) -> float:
    """How strongly ``is_scene_narrator_turn`` fires (0.0 if not, else ~0.55–0.95).

    ``exclude`` is the card's own voice: a player writing about themselves in
    the third person isn't a crowd."""
    pattern = _npc_name_re()
    own = _bible_names(exclude) if exclude else set()
    names = len([m for m in pattern.findall(text) if m.lower() not in own]) if pattern else 0
    he_said = len(_HE_SHE_SAID_RE.findall(text))
    if names < 2 and not (he_said >= 2 and names >= 1):
        return 0.0
    raw = 0.45 + 0.08 * names + 0.06 * he_said
    return min(0.95, max(0.55, raw))


def load_bible_source_rules(speaker: str) -> dict[str, Any]:
    """Load per-speaker rules from ``canon/bible_sources.json`` if present."""
    path = getattr(config, "BIBLE_SOURCES", config.CANON_DIR / "bible_sources.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    speakers = data.get("speakers") or {}
    key = speaker.lower()
    for k, rules in speakers.items():
        if k.lower() == key:
            return dict(rules) if isinstance(rules, dict) else {}
    return {}


def filter_draft_turns(
    turns: list[dict[str, Any]],
    speaker: str,
    *,
    exclude_ids: set[int] | None = None,
    include_only_ids: set[int] | None = None,
    skip_scene_narrator: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply bible-source filters. Returns (filtered turns, stats dict)."""
    exclude = set(exclude_ids or ())
    include_only = set(include_only_ids or ()) if include_only_ids else None
    kept: list[dict[str, Any]] = []
    skipped_exclude = 0
    skipped_include = 0
    skipped_narrator = 0
    for t in turns:
        mid = int(t.get("msg_id", -1))
        if mid in exclude:
            skipped_exclude += 1
            continue
        if include_only is not None and mid not in include_only:
            skipped_include += 1
            continue
        if skip_scene_narrator and is_scene_narrator_turn((t.get("mes") or "")):
            skipped_narrator += 1
            continue
        kept.append(t)
    stats = {
        "input": len(turns),
        "kept": len(kept),
        "skipped_exclude": skipped_exclude,
        "skipped_include_only": skipped_include,
        "skipped_scene_narrator": skipped_narrator,
        "speaker": speaker,
    }
    return kept, stats


def audit_speaker_turns(
    turns: list[dict[str, Any]],
    speaker: str,
) -> list[dict[str, Any]]:
    """Flag turns that may pollute a card-based bible draft."""
    rows: list[dict[str, Any]] = []
    for t in turns:
        mid = int(t.get("msg_id", -1))
        text = (t.get("mes") or "").strip()
        preview = " ".join(text.split())
        if len(preview) > 100:
            preview = preview[:99] + "…"
        flags: list[str] = []
        if is_scene_narrator_turn(text):
            flags.append("scene-narrator")
        rows.append({"msg_id": mid, "flags": flags, "preview": preview})
    return rows


# ---------------------------------------------------------------------------
# Bible drafting — batch observation + compression + synthesis
# ---------------------------------------------------------------------------

# Characters of turn text per observation batch.  At ~400 chars/turn this is
# ≈10 turns/batch — small enough to fit alongside the system prompt in a
# 4 k-context window, but large enough to capture a self-contained exchange.
_DRAFT_BATCH_CHARS: int = 4000

# After this many raw observations accumulate, compress them into one summary
# before adding more — prevents the synthesis prompt from growing without bound.
_DRAFT_COMPRESS_EVERY: int = 5


_DRAFT_OBS_SYSTEM = """\
You are a story editor's character analyst.
Given a set of turns by a single character, identify observable voice patterns.
Be specific and evidence-based. Output ONLY a JSON object — no prose.\
"""

_DRAFT_OBS_PROMPT = """\
Analyze {n} turns by {name}.

{primer_block}

Return a JSON object with these exact keys:
{{
  "voice_observations":        ["<observed speech pattern>", ...],
  "forbidden_observations":    ["<register/phrase this character never uses — cite evidence>", ...],
  "fact_observations":         ["<stated or implied fact about the character>", ...],
  "lens_observations":         ["<perceptual or emotional filter note>", ...],
  "relationship_observations": {{"<other_character>": "<relationship note>", ...}}
}}

Turns:
{turns_text}\
"""

_DRAFT_COMPRESS_SYSTEM = """\
You are merging multiple character analysis batches into a single compact summary.
Merge and deduplicate — keep all unique observations, drop exact duplicates.
Output ONLY a JSON object — no prose.\
"""

_DRAFT_COMPRESS_PROMPT = """\
Merge {n} observation batches about {name}.

{batches_text}

Return one JSON object with the same five keys:
voice_observations, forbidden_observations, fact_observations,
lens_observations, relationship_observations.\
"""

_DRAFT_SYNTH_SYSTEM = """\
You are producing a structured character bible entry for a story editor.
Output ONLY a JSON object — no prose, no code fences, no markdown.

Rules for forbidden_phrasings (CRITICAL):
- Every entry MUST include a concrete example in parentheses.
- Use one of: a short quote in single quotes, a parenthetical, or 'e.g.'.
- Bad:  "Court register or formal address"
- Good: "Court register or formal address (e.g. 'Your Grace', 'it would behoove us')"
- Bad:  "Verbose self-justification"
- Good: "Verbose self-justification (e.g. 'I didn't mean to, it was because...')"
Make forbidden_phrasings Tier 1/2: specific enough that an LLM can
definitively flag a violation from a brief passage.\
"""

_DRAFT_SYNTH_PROMPT = """\
Synthesize observations about {name} into a complete character bible.

{primer_block}

Observations ({n_obs} items from {n_batches} analysis pass(es)):
{obs_text}

Known speaker name(s) in log: {aliases_hint}
Channel hint (user = human player, char = AI/NPC): {channel_hint}

Produce a JSON object with EXACTLY these fields:
{{
  "aliases":              ["<name or title as it appears in the log>", ...],
  "label":                "<short label, e.g. 'Wren — apprentice chartmaker'>",
  "role":                 "<1-2 sentence role description>",
  "channel":              "<'user' or 'char'>",
  "bias_lens":            "<perceptual/emotional filter in 1 sentence>",
  "voice_rules":          ["<specific, checkable rule>", ...],
  "forbidden_phrasings":  ["<pattern with concrete example, e.g. 'Verbose apology (e.g. \\'I didn\\'t mean to\\')'>", ...],
  "aesthetic_rules":      ["<structural rule about how their turns are built>", ...],
  "canonical_facts":      ["<fact established in the story>", ...],
  "relationship_notes":   {{"<character>": "<relationship description>", ...}},
  "seed_queries":         ["<useful semantic search question about this character>", ...]
}}

Aim for 6-8 voice_rules, 6-8 forbidden_phrasings (each with a concrete example in
parentheses), 2-3 aesthetic_rules, 5-8 canonical_facts, 3 seed_queries.\
"""


def _load_primer_block() -> str:
    """Return the story primer as a formatted block, or empty string."""
    p = config.STORY_PRIMER_FILE
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    return f"Story context (primer):\n{text}"


def _batch_turns(turn_texts: list[str], batch_chars: int) -> list[list[str]]:
    """Split turn_texts into groups of at most batch_chars total characters."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for t in turn_texts:
        tlen = len(t)
        if current and current_len + tlen > batch_chars:
            batches.append(current)
            current = []
            current_len = 0
        current.append(t)
        current_len += tlen
    if current:
        batches.append(current)
    return batches


def _observe_batch(
    name: str,
    batch: list[str],
    primer_block: str,
) -> dict[str, Any]:
    """Run one observation pass over a batch of turn texts.

    Returns a raw observation dict (may be incomplete JSON on model failure —
    caller should be tolerant).
    """
    turns_text = "\n\n".join(batch)
    prompt = _DRAFT_OBS_PROMPT.format(
        n=len(batch),
        name=name,
        primer_block=primer_block or "(no primer available)",
        turns_text=turns_text,
    )
    msgs = [
        {"role": "system", "content": _DRAFT_OBS_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    raw = llm.chat(msgs, task="canon_draft", temperature=0.2, max_tokens=2000)
    parsed = _extract_json_object(raw)
    return parsed or {}


def _compress_observations(
    name: str,
    obs_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge multiple observation dicts into one via a compression LLM call."""
    batches_text = ""
    for i, obs in enumerate(obs_list, 1):
        batches_text += f"\n--- Batch {i} ---\n{json.dumps(obs, indent=2)}\n"

    prompt = _DRAFT_COMPRESS_PROMPT.format(
        n=len(obs_list),
        name=name,
        batches_text=batches_text.strip(),
    )
    msgs = [
        {"role": "system", "content": _DRAFT_COMPRESS_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    raw = llm.chat(msgs, task="canon_draft", temperature=0.15, max_tokens=2500)
    parsed = _extract_json_object(raw)
    if parsed is None:
        # Fallback: manually merge lists
        merged: dict[str, Any] = {
            "voice_observations": [],
            "forbidden_observations": [],
            "fact_observations": [],
            "lens_observations": [],
            "relationship_observations": {},
        }
        for obs in obs_list:
            for k in ("voice_observations", "forbidden_observations",
                      "fact_observations", "lens_observations"):
                merged[k].extend(obs.get(k, []))
            merged["relationship_observations"].update(
                obs.get("relationship_observations", {})
            )
        return merged
    return parsed


def _synthesize_bible(
    name: str,
    obs: dict[str, Any],
    n_batches: int,
    primer_block: str,
    channel_hint: str,
    aliases_hint: str,
) -> dict[str, Any]:
    """Run the final synthesis pass and return the bible entry dict."""
    # Flatten all observations into a readable block
    obs_lines: list[str] = []
    for k, label in [
        ("voice_observations", "Voice"),
        ("forbidden_observations", "Never does"),
        ("fact_observations", "Facts"),
        ("lens_observations", "Lens/filter"),
    ]:
        items = obs.get(k, [])
        if items:
            obs_lines.append(f"{label}:")
            for item in items:
                obs_lines.append(f"  - {item}")
    rels = obs.get("relationship_observations", {})
    if rels:
        obs_lines.append("Relationships:")
        for char, note in rels.items():
            obs_lines.append(f"  {char}: {note}")

    obs_text = "\n".join(obs_lines) if obs_lines else "(no observations)"
    n_obs = sum(
        len(obs.get(k, []))
        for k in ("voice_observations", "forbidden_observations",
                  "fact_observations", "lens_observations")
    )

    prompt = _DRAFT_SYNTH_PROMPT.format(
        name=name,
        primer_block=primer_block or "(no primer available)",
        n_obs=n_obs,
        n_batches=n_batches,
        obs_text=obs_text,
        aliases_hint=aliases_hint,
        channel_hint=channel_hint,
    )
    msgs = [
        {"role": "system", "content": _DRAFT_SYNTH_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    raw = llm.chat(msgs, task="canon_draft", temperature=0.3, max_tokens=3000)
    parsed = _extract_json_object(raw)
    return parsed or {}


def draft_bible(
    speaker_name: str,
    turns: list[dict[str, Any]],
    *,
    batch_chars: int = _DRAFT_BATCH_CHARS,
    compress_every: int = _DRAFT_COMPRESS_EVERY,
    channel_hint: str = "char",
    print_progress: bool = False,
) -> dict[str, Any]:
    """Draft a character bible by analysing all turns by ``speaker_name``.

    Parameters
    ----------
    speaker_name:
        The display name as it appears in the log (e.g. ``"Arya"``).
    turns:
        List of message dicts from the JSONL log, already filtered to this
        speaker.  Each dict should have at least ``"mes"`` and ``"msg_id"``.
    batch_chars:
        Maximum characters of turn text per observation batch.  Increase for
        larger context-window models; decrease to reduce per-call latency.
    compress_every:
        How many raw observation batches to accumulate before running a
        compression pass.  Keeps the synthesis prompt manageable for long logs.
    channel_hint:
        ``"user"`` if this is the human player character, ``"char"`` otherwise.
        Auto-detected from the log if possible.
    print_progress:
        Print a terse progress line per batch to stdout.

    Returns
    -------
    dict
        A ``characters.json``-compatible entry dict (no outer ``"characters"``
        wrapper, no key — caller adds those when merging).  Empty dict on
        failure.
    """
    # Build turn text representations
    turn_texts: list[str] = []
    for t in turns:
        idx = t.get("msg_id", "?")
        body = (t.get("mes") or "").strip()
        if body:
            turn_texts.append(f"[msg {idx}] {body}")

    if not turn_texts:
        return {}

    primer_block = _load_primer_block()
    batches = _batch_turns(turn_texts, batch_chars)
    n_batches = len(batches)
    aliases_hint = speaker_name  # log name; synthesis will normalise

    if print_progress:
        print(f"  {speaker_name}: {len(turn_texts)} turns → "
              f"{n_batches} batch(es) of ≤{batch_chars} chars")

    # ── Step 1: per-batch observation ────────────────────────────────────────
    pending: list[dict[str, Any]] = []
    for i, batch in enumerate(batches):
        if print_progress:
            print(f"  observing batch {i + 1}/{n_batches} …")
        obs = _observe_batch(speaker_name, batch, primer_block)
        pending.append(obs)

        # ── Step 2: rolling compression ──────────────────────────────────────
        if len(pending) >= compress_every:
            if print_progress:
                print(f"  compressing {len(pending)} observations …")
            pending = [_compress_observations(speaker_name, pending)]

    # Final compression if multiple observations remain
    if len(pending) > 1:
        if print_progress:
            print(f"  final compression of {len(pending)} observations …")
        pending = [_compress_observations(speaker_name, pending)]

    final_obs = pending[0] if pending else {}

    # ── Step 3: synthesis ────────────────────────────────────────────────────
    if print_progress:
        print(f"  synthesizing bible …")

    return _synthesize_bible(
        name=speaker_name,
        obs=final_obs,
        n_batches=n_batches,
        primer_block=primer_block,
        channel_hint=channel_hint,
        aliases_hint=aliases_hint,
    )


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the first balanced ``{...}`` JSON object out of an LLM response.

    Tolerant of code-fences, leading/trailing prose, and reasoning-model
    preambles. Returns the parsed dict, or ``None`` if nothing parseable is
    found.
    """
    if not raw:
        return None
    text = raw.strip()

    # Fast path: strip a single code-fence wrapper and try the whole thing.
    fenced = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    fenced = re.sub(r"\s*```$", "", fenced).strip()
    for candidate in (text, fenced):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # Slow path: scan for the first balanced brace span and parse it.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[start : i + 1]
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict):
                                return obj
                        except Exception:
                            break  # try the next '{'
        start = text.find("{", start + 1)
    return None
