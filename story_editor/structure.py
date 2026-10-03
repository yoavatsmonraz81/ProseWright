"""Phase 1.5 - structure inference.

The plan's first grade of structure is the *auto-derived* one: episode
boundaries, scene breaks, speaker turns, time-skips, who is present - observed
from the text for free, no model required. This module derives that backbone
from the same headers the pause-finder reads:

    [ 🕰️ 11:15 PM | 🗓️ Thursday, October 4, 1792 | 📍 The Harbour Office | ... ]

A SCENE is a contiguous run of messages sharing a location and a story-day; a
new scene begins when the header introduces a new location or the calendar
advances. Each editor interlude is its own cutaway-scene. An EPISODE is a run
of scenes sharing one story-day (this story tells roughly one day per episode).

This is the deterministic foundation. The LLM beat-derivation (Generate / Audit
/ Drift-detect) builds on top of these scenes - it reasons over scene synopses,
not raw messages, so the model sees structure instead of a wall of prose.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import canon as canon_mod
from . import config, jsonish, llm, pauses, text as text_mod
from .loader import Log, Message

# Bump when the scene-card prompt/representation changes, so cached cards from an
# older prompt are recomputed rather than silently reused. v2 adds canonical
# location + aliases alongside the synopsis (v1 string cards are migrated).
CARD_SCHEMA_VERSION = 2


def _interlude_meta(m: Message) -> dict | None:
    """Return the story_editor meta dict if `m` is an editor interlude, else None.
    Inlined (rather than importing interlude.py) to keep this module light and
    free of the interlude/canon/index import chain."""
    extra = m.raw.get("extra") or {}
    se = extra.get("story_editor") if isinstance(extra, dict) else None
    if isinstance(se, dict) and se.get("interlude"):
        return se
    return None


@dataclass
class Scene:
    scene_id: int
    start_msg_id: int
    end_msg_id: int
    kind: str  # "scene" | "interlude"
    date_str: str | None = None
    episode_date_str: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    location: str | None = None
    stage: str | None = None  # interludes only
    speakers: list[str] = field(default_factory=list)
    msg_count: int = 0
    opening: str = ""  # cleaned preview of the first message
    closing: str = ""  # cleaned preview of the last message
    timeline_lane: str | None = None
    location_mode: str | None = None
    focal_characters: list[str] = field(default_factory=list)
    narrative_actors: list[str] = field(default_factory=list)
    context_segments: list[dict] = field(default_factory=list)

    def title(self) -> str:
        if self.kind == "interlude":
            return f"Interlude — {self.stage or '?'}"
        return self.location or "(no location)"

    def span_label(self) -> str:
        rng = (
            f"msg {self.start_msg_id}"
            if self.start_msg_id == self.end_msg_id
            else f"msg {self.start_msg_id}–{self.end_msg_id}"
        )
        when = self.date_str or "?"
        clock = self.time_start or ""
        if self.time_end and self.time_end != self.time_start:
            clock = f"{self.time_start or '?'}→{self.time_end}"
        return f"{rng}  [{when} {clock}]".rstrip()


@dataclass
class Episode:
    episode_id: int
    date_str: str | None
    start_msg_id: int
    end_msg_id: int
    scene_ids: list[int] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    scene_count: int = 0
    msg_count: int = 0
    interlude_count: int = 0


def _preview(m: Message, width: int = 140) -> str:
    body = " ".join(text_mod.clean(m.text).split())
    if not body:
        body = " ".join(m.text.split())
    return body[:width] + ("…" if len(body) > width else "")


def _chronicle_meta(m: Message) -> dict | None:
    """Narrative chronology embedded by the Phase 1 semantic pass."""
    extra = m.raw.get("extra") or {}
    se = extra.get("story_editor") if isinstance(extra, dict) else None
    chronicle = se.get("chronicle") if isinstance(se, dict) else None
    return chronicle if isinstance(chronicle, dict) else None


def _same_context(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    clean = lambda value: " ".join(value.casefold().replace("’", "'").split())
    return clean(left) == clean(right)


def _append_unique(target: list[str], values) -> None:
    for value in values or []:
        text = str(value).strip()
        if text and text not in target:
            target.append(text)


def segment_scenes(log: Log) -> list[Scene]:
    """Derive the scene backbone from header transitions. A new scene starts on
    a location change, a date change, or an interlude (its own cutaway-scene).
    Header-less turns (most user messages) inherit the running scene's context."""
    scenes: list[Scene] = []
    cur: Scene | None = None
    cur_date: tuple[int, int, int] | str | None = None  # for comparison
    cur_date_str: str | None = None
    cur_loc: str | None = None
    cur_location_mode: str | None = None

    def close(scene: Scene | None) -> None:
        if scene is not None:
            scenes.append(scene)

    for m in log.messages:
        meta = _interlude_meta(m)
        chronicle = _chronicle_meta(m)
        if meta is not None:
            # Interlude: a cutaway. Close the running scene, emit the interlude as
            # its own scene, and leave the main thread's running date/location
            # untouched (the cutaway happens "between" the surrounding day).
            close(cur)
            cur = None
            scenes.append(
                Scene(
                    scene_id=len(scenes),
                    start_msg_id=m.msg_id,
                    end_msg_id=m.msg_id,
                    kind="interlude",
                    date_str=(chronicle or {}).get("date")
                    or (chronicle or {}).get("physical_anchor_date")
                    or cur_date_str,
                    episode_date_str=(chronicle or {}).get("physical_anchor_date")
                    or (chronicle or {}).get("date")
                    or cur_date_str,
                    time_start=(chronicle or {}).get("time"),
                    time_end=(chronicle or {}).get("time"),
                    location=(chronicle or {}).get("location") or meta.get("location"),
                    stage=meta.get("stage"),
                    speakers=[m.speaker],
                    msg_count=1,
                    opening=_preview(m),
                    closing=_preview(m),
                    timeline_lane=(chronicle or {}).get("timeline_lane"),
                    location_mode=(chronicle or {}).get("location_mode"),
                    focal_characters=[(chronicle or {}).get("focal_character")]
                    if (chronicle or {}).get("focal_character") else [],
                    narrative_actors=list((chronicle or {}).get("narrative_actors") or []),
                    context_segments=list((chronicle or {}).get("subsegments") or []),
                )
            )
            continue

        if chronicle is not None:
            hdr = None
            new_date_str = chronicle.get("date")
            episode_date_str = chronicle.get("physical_anchor_date") or new_date_str
            new_date = new_date_str
            new_loc = chronicle.get("location")
            new_time = chronicle.get("time")
            new_location_mode = chronicle.get("location_mode") or "physical"
        else:
            hdr = pauses.parse_header(m.text)
            new_date = hdr.date or cur_date
            new_date_str = hdr.date_str or cur_date_str
            episode_date_str = new_date_str
            new_loc = hdr.location or cur_loc
            new_time = hdr.time
            new_location_mode = cur_location_mode or "physical"

        boundary = (
            cur is None
            or (new_date is not None and new_date != cur_date)
            or (new_date is None and cur_date is not None)
            or not _same_context(new_loc, cur_loc)
            or new_location_mode != cur_location_mode
        )
        if boundary:
            close(cur)
            cur = Scene(
                scene_id=len(scenes),
                start_msg_id=m.msg_id,
                end_msg_id=m.msg_id,
                kind="scene",
                date_str=new_date_str,
                episode_date_str=episode_date_str,
                time_start=new_time,
                time_end=new_time,
                location=new_loc,
                speakers=[],
                msg_count=0,
                opening=_preview(m),
                timeline_lane=(chronicle or {}).get("timeline_lane"),
                location_mode=new_location_mode,
            )

        assert cur is not None
        cur.end_msg_id = m.msg_id
        cur.msg_count += 1
        if m.speaker not in cur.speakers:
            cur.speakers.append(m.speaker)
        if new_time:
            cur.time_end = new_time
            if cur.time_start is None:
                cur.time_start = new_time
        if chronicle is not None:
            _append_unique(cur.focal_characters, [chronicle.get("focal_character")])
            _append_unique(cur.narrative_actors, chronicle.get("narrative_actors"))
            for segment in chronicle.get("subsegments") or []:
                cur.context_segments.append({"msg_id": m.msg_id, **segment})
        cur.closing = _preview(m)

        cur_date, cur_date_str, cur_loc = new_date, new_date_str, new_loc
        cur_location_mode = new_location_mode

    close(cur)
    return scenes


def group_episodes(scenes: list[Scene]) -> list[Episode]:
    """Group consecutive scenes that share a story-day into episodes. Interlude
    cutaways belong to the day they're embedded in (they carry that date)."""
    episodes: list[Episode] = []
    cur: Episode | None = None

    for s in scenes:
        episode_date = s.episode_date_str or s.date_str
        if cur is None or episode_date != cur.date_str:
            cur = Episode(
                episode_id=len(episodes),
                date_str=episode_date,
                start_msg_id=s.start_msg_id,
                end_msg_id=s.end_msg_id,
            )
            episodes.append(cur)
        cur.end_msg_id = s.end_msg_id
        cur.scene_ids.append(s.scene_id)
        cur.scene_count += 1
        cur.msg_count += s.msg_count
        if s.kind == "interlude":
            cur.interlude_count += 1
        elif s.location and s.location not in cur.locations:
            cur.locations.append(s.location)

    return episodes


def scene_for_msg(scenes: list[Scene], msg_id: int) -> Scene | None:
    """The scene that contains `msg_id`, or None."""
    for s in scenes:
        if s.start_msg_id <= msg_id <= s.end_msg_id:
            return s
    return None


def summarize(log: Log) -> dict:
    """Bundle the derived structure for the CLI / downstream beat-derivation."""
    scenes = segment_scenes(log)
    episodes = group_episodes(scenes)
    return {
        "scenes": scenes,
        "episodes": episodes,
        "n_scenes": len(scenes),
        "n_episodes": len(episodes),
        "n_interludes": sum(1 for s in scenes if s.kind == "interlude"),
    }


# --------------------------------------------------------------------------- #
# Scene cards (the map step): a faithful 1-2 sentence synopsis per scene, so beat
# derivation reasons over real summaries instead of head/tail fragments. Cached
# to disk keyed by a signature of the scene boundaries; one LLM map pass, paid
# once. This is the single biggest comprehension lever - a 48-message scene is no
# longer collapsed to its first and last 140 characters.
# --------------------------------------------------------------------------- #

SCENE_TEXT_PER_MSG = 240    # cap per message when assembling a scene's text
SCENE_TEXT_BUDGET = 3200    # cap per scene fed to the summariser
BATCH_CHAR_BUDGET = 6000    # pack scenes into a summarise call up to this size
MAX_SCENES_PER_BATCH = 4    # ...and never more than this many (keeps output JSON
                            # small enough to not hit the token cap and truncate)


def _scene_messages(log: Log, scene: Scene) -> list[Message]:
    return [
        m
        for m in log.messages
        if scene.start_msg_id <= m.msg_id <= scene.end_msg_id
    ]


def scene_text_for_summary(log: Log, scene: Scene) -> str:
    """A turn-by-turn, budgeted rendering of a scene for the summariser - the
    SHAPE of the scene (who says/does what, in order), not just its ends. Long
    scenes keep their head and tail with the middle elided."""
    lines: list[str] = []
    for m in _scene_messages(log, scene):
        body = " ".join(text_mod.clean(m.text).split())
        if not body:
            continue
        if len(body) > SCENE_TEXT_PER_MSG:
            body = body[:SCENE_TEXT_PER_MSG] + "…"
        lines.append(f"{m.speaker}: {body}")

    text = "\n".join(lines)
    if len(text) <= SCENE_TEXT_BUDGET:
        return text

    # Over budget: keep head and tail lines, elide the middle.
    head: list[str] = []
    tail: list[str] = []
    h = t = 0
    i, j = 0, len(lines) - 1
    half = SCENE_TEXT_BUDGET // 2
    while i <= j:
        if h <= t and h + len(lines[i]) < half:
            head.append(lines[i]); h += len(lines[i]) + 1; i += 1
        elif t + len(lines[j]) < half:
            tail.append(lines[j]); t += len(lines[j]) + 1; j -= 1
        else:
            break
    omitted = j - i + 1
    mid = f"… [{omitted} message(s) omitted] …" if omitted > 0 else ""
    return "\n".join(head + ([mid] if mid else []) + list(reversed(tail)))


def _scene_key(s: Scene) -> str:
    """A stable per-scene key (span + kind) so cards survive renumbering and the
    cache can be filled incrementally - only changed/new scenes are recomputed."""
    return f"{s.kind}:{s.start_msg_id}-{s.end_msg_id}"


def _normalize_card_value(v) -> dict | None:
    """Coerce a cache entry to ``{synopsis, location, location_aliases, …}``."""
    if isinstance(v, str) and v.strip():
        return {
            "synopsis": v.strip(),
            "location": None,
            "location_aliases": [],
            "location_enriched": False,
        }
    if not isinstance(v, dict):
        return None
    syn = v.get("synopsis")
    if not isinstance(syn, str) or not syn.strip():
        return None
    aliases_raw = v.get("location_aliases")
    if aliases_raw is None:
        aliases_raw = v.get("aliases") or []
    if not isinstance(aliases_raw, list):
        aliases_raw = []
    loc = v.get("location")
    if loc is not None and not isinstance(loc, str):
        loc = str(loc)
    loc = loc.strip() if isinstance(loc, str) and loc.strip() else None
    return {
        "synopsis": syn.strip(),
        "location": loc,
        "location_aliases": [str(a).strip() for a in aliases_raw if str(a).strip()],
        "location_enriched": bool(v.get("location_enriched")),
    }


def _card_synopsis(v) -> str:
    n = _normalize_card_value(v)
    return n["synopsis"] if n else ""


def location_search_blob(location: str | None, aliases: list[str] | None = None) -> str | None:
    """Canonical location plus aliases for index LIKE / audit matching."""
    parts: list[str] = []
    if location and location.strip():
        parts.append(location.strip())
    for a in aliases or []:
        a = (a or "").strip()
        if not a:
            continue
        if any(a.lower() in p.lower() for p in parts):
            continue
        parts.append(a)
    return " | ".join(parts) if parts else None


def scene_location_by_span() -> dict[tuple[int, int], dict]:
    """Read cached card locations without calling the LLM.

    Returns ``{(start, end): {location, location_aliases, synopsis}}`` for
    ``scene:*`` keys that have a usable location.
    """
    if not config.SCENE_CARDS.exists():
        return {}
    try:
        d = json.loads(config.SCENE_CARDS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {}
    out: dict[tuple[int, int], dict] = {}
    for key, raw in (d.get("cards") or {}).items():
        m = re.match(r"scene:(\d+)-(\d+)$", str(key))
        if not m:
            continue
        card = _normalize_card_value(raw)
        if not card or not card.get("location"):
            continue
        out[(int(m.group(1)), int(m.group(2)))] = card
    return out


def _load_card_cache(log: Log) -> dict:
    """Return the on-disk card cache for this log keyed by scene-key, or a fresh
    empty cache. A schema-version or log-path mismatch starts fresh. v1 string
    cards are migrated in place to the v2 rich shape (synopses kept)."""
    fresh = {
        "version": CARD_SCHEMA_VERSION,
        "log": str(log.path.resolve()),
        "cards": {},
    }
    if not config.SCENE_CARDS.exists():
        return fresh
    try:
        d = json.loads(config.SCENE_CARDS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return fresh
    ver = d.get("version")
    # Accept v1 (string synopses) and migrate; anything else is stale.
    if ver not in (1, CARD_SCHEMA_VERSION) and "version" in d:
        return fresh
    if not config.same_log(d.get("log"), log.path):
        return fresh
    cards: dict[str, dict] = {}
    for k, v in (d.get("cards") or {}).items():
        n = _normalize_card_value(v)
        if n:
            cards[str(k)] = n
    d["version"] = CARD_SCHEMA_VERSION
    d["cards"] = cards
    return d


def _save_card_cache(data: dict) -> None:
    data["created"] = datetime.now(timezone.utc).isoformat()
    config.SCENE_CARDS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_summary_batches(
    log: Log, scenes: list[Scene]
) -> list[list[tuple[Scene, str]]]:
    """Pack main-thread scenes into summarise calls up to BATCH_CHAR_BUDGET."""
    batches: list[list[tuple[Scene, str]]] = []
    cur: list[tuple[Scene, str]] = []
    cur_len = 0
    for s in scenes:
        if s.kind != "scene":
            continue
        body = scene_text_for_summary(log, s)
        block = len(body) + 80
        if cur and (
            cur_len + block > BATCH_CHAR_BUDGET
            or len(cur) >= MAX_SCENES_PER_BATCH
        ):
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append((s, body))
        cur_len += block
    if cur:
        batches.append(cur)
    return batches


def _build_summary_messages(batch: list[tuple[Scene, str]]) -> list[dict]:
    system = (
        "You summarise scenes of a story into faithful, concrete synopses and "
        "canonical locations. For EACH scene given, return an object with:\n"
        '- "synopsis": 1-2 sentences capturing what happens, who acts, and WHAT '
        "CHANGES by the end (the turn or shift, if any).\n"
        '- "location": the best place-name for this scene (prefer the header loc= '
        "value; lightly canonicalize if helpful; keep city/district when present).\n"
        '- "location_aliases": short searchable names readers would type '
        '(e.g. "Ropewalk", "Salt Anchor", "Hobb\'s Wharf").\n'
        "Rules:\n"
        "- Ground every claim in the provided text. Do NOT add events, motives, or "
        "outcomes that are not present.\n"
        "- Be specific: name who does what and the concrete change (a decision "
        "made, a secret surfaced, a power/mood shift, an arrival or departure). "
        "Avoid vague filler like 'they talk and bond'.\n"
        "- Present tense. No outside knowledge, no spoilers, no quality commentary.\n"
        "- If the place is unclear, copy the header loc= value and use an empty "
        "aliases list.\n"
        "Output STRICT JSON only - no prose, no code fence - an object mapping "
        'each scene id (e.g. "S12") to '
        '{"synopsis":"...","location":"...","location_aliases":["..."]}.'
    )
    parts = []
    for s, body in batch:
        parts.append(
            f"SCENE S{s.scene_id} ({s.date_str or '?'} "
            f"{s.time_start or ''}, loc={s.title()}):\n{body}"
        )
    user = (
        "Summarise each of the following scenes. Return only the JSON object.\n\n"
        + "\n\n".join(parts)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_locate_messages(batch: list[tuple[Scene, str, str]]) -> list[dict]:
    """Locate-only prompt: synopsis already known; ask for location + aliases."""
    system = (
        "You assign canonical story locations to scenes. For EACH scene, return:\n"
        '- "location": best place-name (prefer the given header location; lightly '
        "canonicalize; keep city/district when present).\n"
        '- "location_aliases": short searchable names (e.g. "Ropewalk", '
        '"Salt Anchor", "Hobb\'s Wharf").\n'
        "Rules: ground in the provided text and header. Do not invent places. "
        "If unclear, copy the header location and use [].\n"
        "Output STRICT JSON only - an object mapping scene ids (e.g. \"S12\") to "
        '{"location":"...","location_aliases":["..."]}.'
    )
    parts = []
    for s, body, synopsis in batch:
        parts.append(
            f"SCENE S{s.scene_id} (header_loc={s.location or s.title()}):\n"
            f"SYNOPSIS: {synopsis}\n"
            f"EXCERPT:\n{body[:1200]}"
        )
    user = (
        "Locate each scene. Return only the JSON object.\n\n" + "\n\n".join(parts)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_SID_RE = re.compile(r"(\d+)")


def _parse_card_response(v, *, header_loc: str | None = None) -> dict | None:
    """Parse one LLM card value (string or object) into a rich cache entry."""
    if isinstance(v, str) and v.strip():
        return {
            "synopsis": v.strip(),
            "location": header_loc,
            "location_aliases": [],
            "location_enriched": False,
        }
    if not isinstance(v, dict):
        return None
    syn = v.get("synopsis")
    if not isinstance(syn, str) or not syn.strip():
        return None
    loc = v.get("location")
    if isinstance(loc, str) and loc.strip():
        loc = loc.strip()
    else:
        loc = header_loc
    aliases_raw = v.get("location_aliases")
    if aliases_raw is None:
        aliases_raw = v.get("aliases") or []
    if not isinstance(aliases_raw, list):
        aliases_raw = []
    aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
    enriched = bool(v.get("location") or aliases)
    return {
        "synopsis": syn.strip(),
        "location": loc,
        "location_aliases": aliases,
        "location_enriched": enriched,
    }


def _summarize_batch(
    batch: list[tuple[Scene, str]], temperature: float
) -> dict[int, dict]:
    """One summarise call; returns {scene_id: rich card} for ids it parsed.
    Raises on transport/JSON failure so the caller can retry/fall back."""
    raw = llm.chat(
        _build_summary_messages(batch),
        temperature=temperature,
        max_tokens=2200,
    )
    obj = _extract_json_object(raw)
    by_scene = {s.scene_id: s for s, _ in batch}
    out: dict[int, dict] = {}
    for k, v in obj.items():
        m = _SID_RE.search(str(k))
        if not m:
            continue
        sid = int(m.group(1))
        s = by_scene.get(sid)
        if s is None:
            continue
        card = _parse_card_response(v, header_loc=s.location)
        if card:
            out[sid] = card
    return out


def _summarize_with_retry(
    batch: list[tuple[Scene, str]], temperature: float
) -> dict[int, dict]:
    """Summarise a batch; retry one-at-a-time any scene the batch dropped, so a
    single truncated/garbled batch response never loses scenes."""
    try:
        got = _summarize_batch(batch, temperature)
    except (ValueError, llm.ModelError):
        got = {}
    missing = [pair for pair in batch if pair[0].scene_id not in got]
    if missing and len(batch) > 1:
        for pair in missing:
            try:
                got.update(_summarize_batch([pair], temperature))
            except (ValueError, llm.ModelError):
                pass
    return got


def _locate_batch(
    batch: list[tuple[Scene, str, str]], temperature: float
) -> dict[int, dict]:
    """Locate-only call; returns {scene_id: {location, location_aliases}}."""
    raw = llm.chat(
        _build_locate_messages(batch),
        temperature=temperature,
        max_tokens=1200,
    )
    obj = _extract_json_object(raw)
    by_scene = {s.scene_id: s for s, _, _ in batch}
    out: dict[int, dict] = {}
    for k, v in obj.items():
        m = _SID_RE.search(str(k))
        if not m:
            continue
        sid = int(m.group(1))
        s = by_scene.get(sid)
        if s is None or not isinstance(v, dict):
            continue
        loc = v.get("location")
        if isinstance(loc, str) and loc.strip():
            loc = loc.strip()
        else:
            loc = s.location
        aliases_raw = v.get("location_aliases")
        if aliases_raw is None:
            aliases_raw = v.get("aliases") or []
        if not isinstance(aliases_raw, list):
            aliases_raw = []
        out[sid] = {
            "location": loc,
            "location_aliases": [str(a).strip() for a in aliases_raw if str(a).strip()],
            "location_enriched": True,
        }
    return out


def _locate_with_retry(
    batch: list[tuple[Scene, str, str]], temperature: float
) -> dict[int, dict]:
    try:
        got = _locate_batch(batch, temperature)
    except (ValueError, llm.ModelError):
        got = {}
    missing = [pair for pair in batch if pair[0].scene_id not in got]
    if missing and len(batch) > 1:
        for pair in missing:
            try:
                got.update(_locate_batch([pair], temperature))
            except (ValueError, llm.ModelError):
                pass
    return got


def scene_cards(
    log: Log, *, force: bool = False, temperature: float = 0.2, on_progress=None
) -> dict[int, str]:
    """Return {scene_id: synopsis}, building only what's missing.

    The cache is keyed per-scene (span + kind), so unchanged scenes are reused
    and only new/changed ones are summarised. It is written after EVERY batch, so
    an interrupted run is resumable - rerun and it picks up where it left off.
    After synopses exist, a locate pass fills canonical location + aliases for
    cards that have not been location-enriched yet."""
    scenes = segment_scenes(log)
    data = (
        {"version": CARD_SCHEMA_VERSION, "log": str(log.path.resolve()), "cards": {}}
        if force
        else _load_card_cache(log)
    )
    by_key: dict[str, dict] = data["cards"]

    # One-time migration: an older cache keyed cards by scene-id (no "version").
    # Salvage those good cards into the new span-keyed format so we don't redo a
    # slow full map pass just to change the key scheme.
    if not force and not by_key and config.SCENE_CARDS.exists():
        try:
            old = json.loads(config.SCENE_CARDS.read_text(encoding="utf-8"))
            old_cards = old.get("cards", {})
            if old_cards and "version" not in old:
                id_to_scene = {s.scene_id: s for s in scenes}
                for k, v in old_cards.items():
                    mm = _SID_RE.search(str(k))
                    if not mm:
                        continue
                    s = id_to_scene.get(int(mm.group(1)))
                    n = _normalize_card_value(v)
                    if s and s.kind == "scene" and n:
                        by_key[_scene_key(s)] = n
        except (json.JSONDecodeError, ValueError):
            pass

    result: dict[int, str] = {}
    missing: list[Scene] = []
    dirty = False
    for s in scenes:
        if s.kind == "interlude":
            result[s.scene_id] = f"Interlude ({s.stage or '?'}): {s.opening}"
            continue
        key = _scene_key(s)
        card = _normalize_card_value(by_key.get(key))
        if card:
            # Seed header location when the card has none yet.
            if not card.get("location") and s.location:
                card["location"] = s.location
                by_key[key] = card
                dirty = True
            result[s.scene_id] = card["synopsis"]
        else:
            missing.append(s)

    if missing:
        batches = _build_summary_batches(log, missing)
        for bi, batch in enumerate(batches):
            if on_progress:
                on_progress(bi + 1, len(batches), len(batch))
            got = _summarize_with_retry(batch, temperature)
            for s, _ in batch:
                if s.scene_id in got:
                    card = got[s.scene_id]
                    if not card.get("location") and s.location:
                        card["location"] = s.location
                    by_key[_scene_key(s)] = card
                    result[s.scene_id] = card["synopsis"]
                    dirty = True
            # Persist after each batch so progress survives an interrupt.
            data["cards"] = by_key
            _save_card_cache(data)
            dirty = False

    # Locate pass: enrich cards that still need LLM location/aliases.
    need_locate: list[Scene] = []
    for s in scenes:
        if s.kind != "scene":
            continue
        key = _scene_key(s)
        card = _normalize_card_value(by_key.get(key))
        if card and not card.get("location_enriched"):
            need_locate.append(s)

    if need_locate:
        # Pack like summaries but attach existing synopsis.
        locate_batches: list[list[tuple[Scene, str, str]]] = []
        cur: list[tuple[Scene, str, str]] = []
        cur_len = 0
        for s in need_locate:
            body = scene_text_for_summary(log, s)
            syn = _card_synopsis(by_key.get(_scene_key(s)))
            block = min(len(body), 1200) + len(syn) + 80
            if cur and (
                cur_len + block > BATCH_CHAR_BUDGET or len(cur) >= MAX_SCENES_PER_BATCH
            ):
                locate_batches.append(cur)
                cur, cur_len = [], 0
            cur.append((s, body, syn))
            cur_len += block
        if cur:
            locate_batches.append(cur)

        for bi, batch in enumerate(locate_batches):
            if on_progress:
                on_progress(bi + 1, len(locate_batches), len(batch))
            got = _locate_with_retry(batch, temperature)
            for s, _, _ in batch:
                key = _scene_key(s)
                card = _normalize_card_value(by_key.get(key)) or {
                    "synopsis": result.get(s.scene_id, ""),
                    "location": s.location,
                    "location_aliases": [],
                    "location_enriched": False,
                }
                if s.scene_id in got:
                    card["location"] = got[s.scene_id].get("location") or card.get(
                        "location"
                    ) or s.location
                    card["location_aliases"] = got[s.scene_id].get(
                        "location_aliases"
                    ) or []
                    card["location_enriched"] = True
                else:
                    # Seed header location for the index; leave unenriched so the
                    # next run retries the LLM when the model is back.
                    if not card.get("location") and s.location:
                        card["location"] = s.location
                by_key[key] = card
            data["cards"] = by_key
            _save_card_cache(data)
            dirty = False
    # Drop orphan keys from older segmentations so the cache tracks the live span set.
    live_keys = {_scene_key(s) for s in scenes if s.kind == "scene"}
    orphan_keys = [k for k in by_key if k not in live_keys]
    if orphan_keys:
        for k in orphan_keys:
            del by_key[k]
        dirty = True

    if dirty:
        # Persist header-seeded locations / orphan cleanup.
        data["cards"] = by_key
        _save_card_cache(data)

    return result


# --------------------------------------------------------------------------- #
# Beat derivation (Generate mode): ask the model where the turning points are.
# Reasons over the scene DIGEST, not raw prose - the model sees structure.
# Propose-then-approve: a derived spine is a proposal until the director signs
# off, exactly like an interlude.
# --------------------------------------------------------------------------- #

@dataclass
class Beat:
    beat_id: int
    title: str
    justification: str
    start_msg_id: int
    end_msg_id: int
    first_scene_id: int | None = None
    last_scene_id: int | None = None
    kind: str = ""  # optional model tag: turn|escalation|reversal|setup|...
    evidence: list[str] = field(default_factory=list)  # scene-card citations
    # Stable span identity; the msg_ids above are the ordinal cache these
    # resolved to when the spine was written. See `bind`.
    start_uid: str = ""
    end_uid: str = ""

    def bind(self, log) -> None:
        """Mint missing uids, then recompute msg_ids from them, so a beat range
        written before an interlude was injected still covers the same prose.
        """
        n = len(log)
        if not n:
            return
        if not self.start_uid and 0 <= self.start_msg_id < n:
            self.start_uid = log.get(self.start_msg_id).uid
        if not self.end_uid and 0 <= self.end_msg_id < n:
            self.end_uid = log.get(self.end_msg_id).uid
        start = log.ordinal_of(self.start_uid) if self.start_uid else None
        end = log.ordinal_of(self.end_uid) if self.end_uid else None
        if start is not None:
            self.start_msg_id = start
        if end is not None:
            self.end_msg_id = end
        if self.end_msg_id < self.start_msg_id:
            self.start_msg_id, self.end_msg_id = self.end_msg_id, self.start_msg_id

    def span_label(self) -> str:
        return (
            f"msg {self.start_msg_id}"
            if self.start_msg_id == self.end_msg_id
            else f"msg {self.start_msg_id}–{self.end_msg_id}"
        )

    def to_json(self) -> dict:
        out = {
            "beat_id": self.beat_id,
            "title": self.title,
            "justification": self.justification,
            "start_msg_id": self.start_msg_id,
            "end_msg_id": self.end_msg_id,
            "first_scene_id": self.first_scene_id,
            "last_scene_id": self.last_scene_id,
            "kind": self.kind,
        }
        if self.start_uid or self.end_uid:
            out["anchor"] = {"start_uid": self.start_uid, "end_uid": self.end_uid}
        if self.evidence:
            out["evidence"] = self.evidence
        return out

    @classmethod
    def from_json(cls, d: dict) -> "Beat":
        ev = d.get("evidence")
        anchor = d.get("anchor") or {}
        return cls(
            beat_id=d["beat_id"],
            title=d.get("title", ""),
            justification=d.get("justification", ""),
            start_msg_id=d["start_msg_id"],
            end_msg_id=d["end_msg_id"],
            first_scene_id=d.get("first_scene_id"),
            last_scene_id=d.get("last_scene_id"),
            kind=d.get("kind", ""),
            evidence=list(ev) if isinstance(ev, list) else [],
            start_uid=str(anchor.get("start_uid") or ""),
            end_uid=str(anchor.get("end_uid") or ""),
        )


def _is_interlude_message(m: Message) -> bool:
    extra = m.raw.get("extra") or {}
    se = extra.get("story_editor") if isinstance(extra, dict) else None
    return bool(isinstance(se, dict) and se.get("interlude"))


@dataclass
class MessageLocation:
    """Where a message sits in narrative GPS (beat spine + index within beat)."""

    msg_id: int
    speaker: str
    role: str
    preview: str
    beat_id: int | None = None
    beat_title: str = ""
    beat_start_msg_id: int | None = None
    beat_end_msg_id: int | None = None
    position_in_beat: int | None = None
    total_in_beat: int | None = None
    position_filtered: int | None = None
    total_filtered: int | None = None
    speaker_filter: str | None = None

    def to_json(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "speaker": self.speaker,
            "role": self.role,
            "preview": self.preview,
            "beat_id": self.beat_id,
            "beat_title": self.beat_title,
            "beat_start_msg_id": self.beat_start_msg_id,
            "beat_end_msg_id": self.beat_end_msg_id,
            "position_in_beat": self.position_in_beat,
            "total_in_beat": self.total_in_beat,
            "position_filtered": self.position_filtered,
            "total_filtered": self.total_filtered,
            "speaker_filter": self.speaker_filter,
        }


def beat_for_msg_id(msg_id: int, spine: SpineProposal | None) -> Beat | None:
    if spine is None:
        return None
    for b in spine.beats:
        if b.start_msg_id <= msg_id <= b.end_msg_id:
            return b
    return None


def beat_message_list(
    log: Log,
    beat: Beat,
    *,
    speaker: str | None = None,
) -> list[Message]:
    """Ordered non-interlude messages in a beat, optionally filtered by card name."""
    out: list[Message] = []
    for m in log.messages:
        if m.msg_id < beat.start_msg_id or m.msg_id > beat.end_msg_id:
            continue
        if _is_interlude_message(m):
            continue
        if speaker and m.speaker.lower() != speaker.lower():
            continue
        out.append(m)
    return out


def locate_message(
    log: Log,
    msg_id: int,
    *,
    speaker_filter: str | None = None,
) -> MessageLocation:
    """Resolve msg_id to beat context and 1-based position within the beat."""
    m = log.get(msg_id)
    spine = load_derived_spine()
    beat = beat_for_msg_id(msg_id, spine)
    loc = MessageLocation(
        msg_id=m.msg_id,
        speaker=m.speaker,
        role=m.role(),
        preview=m.preview(120),
        speaker_filter=speaker_filter or None,
    )
    if beat is None:
        return loc

    all_in_beat = beat_message_list(log, beat)
    loc.beat_id = beat.beat_id
    loc.beat_title = beat.title
    loc.beat_start_msg_id = beat.start_msg_id
    loc.beat_end_msg_id = beat.end_msg_id
    loc.total_in_beat = len(all_in_beat)
    for i, x in enumerate(all_in_beat, 1):
        if x.msg_id == msg_id:
            loc.position_in_beat = i
            break

    if speaker_filter:
        filtered = beat_message_list(log, beat, speaker=speaker_filter)
        loc.total_filtered = len(filtered)
        for i, x in enumerate(filtered, 1):
            if x.msg_id == msg_id:
                loc.position_filtered = i
                break

    return loc


def resolve_beat_position(
    log: Log,
    beat_id: int,
    position: int,
    *,
    speaker: str | None = None,
) -> MessageLocation:
    """Map beat + 1-based position (within beat) to a concrete msg_id."""
    if position < 1:
        raise ValueError("position must be >= 1 (1-based index within the beat)")
    spine = load_derived_spine()
    if spine is None:
        raise ValueError(
            "no committed derived spine; run structure derive + commit, "
            "or navigate by msg # instead"
        )
    beat: Beat | None = None
    for b in spine.beats:
        if b.beat_id == beat_id:
            beat = b
            break
    if beat is None:
        raise ValueError(f"beat B{beat_id} not found in derived spine")

    msgs = beat_message_list(log, beat, speaker=speaker)
    if not msgs:
        hint = f" for speaker {speaker}" if speaker else ""
        raise ValueError(f"no messages in beat B{beat_id}{hint}")
    if position > len(msgs):
        hint = f" for speaker {speaker}" if speaker else ""
        raise ValueError(
            f"position {position} out of range; beat B{beat_id} has "
            f"{len(msgs)} message(s){hint}"
        )
    target = msgs[position - 1]
    return locate_message(log, target.msg_id, speaker_filter=speaker)


def spine_navigation_payload(log: Log) -> dict:
    """Beat list with per-beat message counts for the navigation UI."""
    spine = load_derived_spine()
    if spine is None:
        return {"ok": True, "has_spine": False, "beats": []}
    beats: list[dict] = []
    for b in spine.beats:
        row = b.to_json()
        row["message_count"] = len(beat_message_list(log, b))
        beats.append(row)
    return {"ok": True, "has_spine": True, "beats": beats}


@dataclass
class SpineCoverageReport:
    """Deterministic coverage check for a proposed beat spine."""

    n_scenes: int
    n_beats: int
    uncovered_scene_ids: list[int]
    scene_gaps: list[tuple[int, int]]  # inclusive (from, to) scene ids missing between beats
    ok: bool

    def summary_lines(self) -> list[str]:
        lines = [
            f"scenes: {self.n_scenes}  beats: {self.n_beats}  "
            f"verdict: {'OK' if self.ok else 'ISSUES'}",
        ]
        if self.uncovered_scene_ids:
            ids = self.uncovered_scene_ids
            preview = ids[:12]
            tail = f" (+{len(ids) - 12} more)" if len(ids) > 12 else ""
            lines.append(f"uncovered scene ids: {preview}{tail}")
        for lo, hi in self.scene_gaps:
            if lo == hi:
                lines.append(f"gap: scene {lo} not assigned to any beat")
            else:
                lines.append(f"gap: scenes {lo}–{hi} not assigned to any beat")
        return lines


def validate_spine_coverage(
    scenes: list[Scene], beats: list[Beat]
) -> SpineCoverageReport:
    """Check that beat scene ranges are contiguous and cover every scene."""
    if not scenes:
        return SpineCoverageReport(0, len(beats), [], [], ok=len(beats) == 0)
    max_sid = max(s.scene_id for s in scenes)
    all_ids = set(range(0, max_sid + 1))
    covered: set[int] = set()
    gaps: list[tuple[int, int]] = []
    ordered = sorted(
        beats,
        key=lambda b: (
            b.first_scene_id if b.first_scene_id is not None else 10**9,
            b.beat_id,
        ),
    )
    prev_last: int | None = None
    for b in ordered:
        if b.first_scene_id is None or b.last_scene_id is None:
            continue
        first, last = b.first_scene_id, b.last_scene_id
        if prev_last is not None and first > prev_last + 1:
            gaps.append((prev_last + 1, first - 1))
        for sid in range(first, last + 1):
            covered.add(sid)
        prev_last = last
    if ordered and ordered[0].first_scene_id is not None and ordered[0].first_scene_id > 0:
        gaps.insert(0, (0, ordered[0].first_scene_id - 1))
    if (
        ordered
        and ordered[-1].last_scene_id is not None
        and ordered[-1].last_scene_id < max_sid
    ):
        gaps.append((ordered[-1].last_scene_id + 1, max_sid))
    uncovered = sorted(all_ids - covered)
    ok = not uncovered and not gaps
    return SpineCoverageReport(
        n_scenes=len(scenes),
        n_beats=len(beats),
        uncovered_scene_ids=uncovered,
        scene_gaps=gaps,
        ok=ok,
    )


def enrich_beats_with_evidence(
    beats: list[Beat],
    scenes: list[Scene],
    cards: dict[int, str] | None,
    *,
    max_chars: int = 220,
) -> None:
    """Attach scene-card citations to each beat (programmatic, no LLM)."""
    by_id = {s.scene_id: s for s in scenes}
    for b in beats:
        b.evidence = []
        if b.first_scene_id is None or b.last_scene_id is None:
            continue
        cite_ids = [b.first_scene_id]
        if b.last_scene_id != b.first_scene_id:
            span = b.last_scene_id - b.first_scene_id
            if span > 3:
                cite_ids.append(b.first_scene_id + span // 2)
            cite_ids.append(b.last_scene_id)
        seen: set[int] = set()
        for sid in cite_ids:
            if sid in seen:
                continue
            seen.add(sid)
            sc = by_id.get(sid)
            if sc is None:
                continue
            body = (
                cards.get(sid, "").strip()
                if cards and sid in cards
                else sc.opening.strip()
            )
            if len(body) > max_chars:
                body = body[: max_chars - 1] + "…"
            b.evidence.append(f"S{sid} ({sc.title()}): {body}")


def build_scene_digest(
    scenes: list[Scene], cards: dict[int, str] | None = None
) -> str:
    """A compact, numbered scene-by-scene digest for the model to reason over.
    When `cards` is supplied, each scene shows its faithful synopsis instead of
    the lossy head/tail previews."""
    lines = []
    for s in scenes:
        tag = f"[interlude:{s.stage}]" if s.kind == "interlude" else ""
        who = ", ".join(s.speakers[:5])
        head = f"S{s.scene_id} ({s.span_label()}) {tag} loc={s.title()} | who={who}"
        if cards and s.scene_id in cards:
            lines.append(f"{head}\n    {cards[s.scene_id]}")
        else:
            lines.append(f"{head}\n    open: {s.opening}\n    close: {s.closing}")
    return "\n".join(lines)


def build_beat_messages(
    scene_digest: str, max_beats: int, *, premise: str | None = None
) -> list[dict]:
    primer = f"{premise}\n\n" if premise else ""
    system = (
        f"{primer}"
        "You are a story-structure analyst. You are given a scene-by-scene digest "
        "of a roleplay/story log (each scene already segmented by location and "
        "time). Your job is to identify the major STRUCTURAL BEATS - the turning "
        "points, escalations, reversals, and act-turns - that organise this "
        "stretch of story. You are mapping shape, not summarising plot.\n\n"
        "Rules:\n"
        "- A beat spans one or more consecutive scenes. Beats must be contiguous "
        "and non-overlapping, and together cover the whole log in order.\n"
        "- Prefer a SMALL number of meaningful beats over many trivial ones. Each "
        "beat should mark a genuine change in the story's state or direction.\n"
        "- Anchor each beat to a scene RANGE (first and last scene id).\n"
        "- For each beat give: a short title (<= 8 words), a one-line "
        "justification naming the turn ('X shifts to Y because...'), and an "
        "optional kind (setup|turn|escalation|reversal|climax|aftermath).\n"
        "- Interlude scenes (tagged [interlude:...]) are structural punctuation "
        "and strong boundary evidence. Except for an opening interlude, normally "
        "treat an interlude as the coda to the movement before it and begin the "
        "next movement with the following main-thread scene when the narrative "
        "turn supports that reading. Do not manufacture a major beat boundary at "
        "every interlude: an interlude may instead mark a finer chapter seam "
        "inside a larger beat.\n\n"
        "Output STRICT JSON only - no prose, no code fence - an array of objects "
        "with keys: first_scene, last_scene, title, why, kind. Example:\n"
        '[{"first_scene":0,"last_scene":3,"title":"Morning reconciliation",'
        '"why":"the sisters move from estrangement to fragile trust","kind":"setup"}]'
    )
    user = (
        f"Identify at most {max_beats} structural beats in the following scene "
        f"digest. Return only the JSON array.\n\nSCENE DIGEST:\n{scene_digest}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _extract_json_array(raw: str) -> list[dict]:
    """Pull the first JSON array out of the model's reply, tolerating code fences
    and stray prose around it."""
    text = raw.strip()
    # Strip a ```json ... ``` or ``` ... ``` fence if present.
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Find the outermost [...] span.
    start = text.find("[")
    end = text.rfind("]")
    if start == -1:
        raise ValueError("model did not return a JSON array of beats")
    if end == -1 or end <= start:
        raise ValueError(
            "model did not return a JSON array of beats "
            "(response looks truncated — no closing ']'"
            f"; got {len(raw)} chars)"
        )
    blob = text[start : end + 1]
    return json.loads(blob)


def _beats_from_items(items: list[dict], scenes: list[Scene]) -> list[Beat]:
    """Map beat objects (scene ranges) onto msg_id ranges via the scenes."""
    by_id = {s.scene_id: s for s in scenes}
    max_scene = max(by_id) if by_id else 0
    beats: list[Beat] = []
    for i, it in enumerate(items):
        try:
            first = int(it["first_scene"])
            last = int(it["last_scene"])
        except (KeyError, TypeError, ValueError):
            continue
        first = max(0, min(first, max_scene))
        last = max(first, min(last, max_scene))
        s0 = by_id.get(first)
        s1 = by_id.get(last)
        if s0 is None or s1 is None:
            continue
        beats.append(
            Beat(
                beat_id=i,
                title=str(it.get("title", "")).strip(),
                justification=str(it.get("why", it.get("justification", ""))).strip(),
                start_msg_id=s0.start_msg_id,
                end_msg_id=s1.end_msg_id,
                first_scene_id=first,
                last_scene_id=last,
                kind=str(it.get("kind", "")).strip(),
            )
        )
    if not beats:
        raise ValueError("no valid beats parsed from the model output")
    return beats


def _beats_from_model(raw: str, scenes: list[Scene]) -> list[Beat]:
    """Map the model's scene-range beats onto msg_id ranges via the scenes."""
    return _beats_from_items(_extract_json_array(raw), scenes)


@dataclass
class SpineProposal:
    log: str
    beats: list[Beat]
    n_scenes: int
    n_episodes: int
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    coverage: dict | None = None
    critique_notes: list[str] | None = None
    # ``beat`` is the legacy/model-derived unit.  A director-approved map uses
    # ``episode``; keeping the field on this compatibility object lets existing
    # navigation consume canon without confusing the two concepts in the API.
    unit_type: str = "beat"
    authority: str = "derived"

    def to_json(self) -> dict:
        out = {
            "log": self.log,
            "beats": [b.to_json() for b in self.beats],
            "n_scenes": self.n_scenes,
            "n_episodes": self.n_episodes,
            "created": self.created,
        }
        if self.coverage is not None:
            out["coverage"] = self.coverage
        if self.critique_notes:
            out["critique_notes"] = self.critique_notes
        out["unit_type"] = self.unit_type
        out["authority"] = self.authority
        return out

    @classmethod
    def from_json(cls, d: dict) -> "SpineProposal":
        return cls(
            log=d["log"],
            beats=[Beat.from_json(b) for b in d.get("beats", [])],
            n_scenes=d.get("n_scenes", 0),
            n_episodes=d.get("n_episodes", 0),
            created=d.get("created", ""),
            coverage=d.get("coverage"),
            critique_notes=d.get("critique_notes"),
            unit_type=str(d.get("unit_type") or "beat"),
            authority=str(d.get("authority") or "derived"),
        )


def build_spine_critique_messages(
    beats: list[Beat],
    report: SpineCoverageReport,
    digest: str,
    max_beats: int,
) -> list[dict]:
    beat_block = json.dumps(
        [
            {
                "first_scene": b.first_scene_id,
                "last_scene": b.last_scene_id,
                "title": b.title,
                "why": b.justification,
                "kind": b.kind,
            }
            for b in beats
        ],
        ensure_ascii=False,
        indent=2,
    )
    coverage_block = "\n".join(f"- {ln}" for ln in report.summary_lines())
    system = (
        "You are reviewing a proposed structural beat spine for a roleplay log. "
        "The beats must be CONTIGUOUS in scene-id order, NON-OVERLAPPING, and "
        f"together cover every scene S0..S{report.n_scenes - 1}.\n\n"
        "Check for:\n"
        "- uncovered scenes or gaps between beats\n"
        "- beats that merge unrelated turning points (too coarse)\n"
        "- beats that split one turn across two (too fine)\n"
        "- interludes whose structural punctuation was ignored; except for an "
        "opening interlude, prefer attaching a cutaway to the preceding movement "
        "and beginning the next movement after it when the surrounding turn "
        "supports that boundary\n"
        "- weak justifications that summarise plot instead of naming the turn\n\n"
        "Return STRICT JSON only — a single object with keys:\n"
        '- "issues": string[] — concrete problems (empty if acceptable)\n'
        '- "beats": optional revised beat array (same schema as derive: '
        "first_scene, last_scene, title, why, kind). Include ONLY when revision "
        "is needed; must fix all coverage gaps and stay at or under the beat cap.\n"
    )
    user = (
        f"Beat cap: {max_beats}\n\n"
        f"COVERAGE REPORT:\n{coverage_block}\n\n"
        f"PROPOSED BEATS:\n{beat_block}\n\n"
        f"SCENE DIGEST (reference):\n{digest}\n\n"
        "Return only the JSON object."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def critique_spine_beats(
    scenes: list[Scene],
    beats: list[Beat],
    report: SpineCoverageReport,
    digest: str,
    *,
    max_beats: int,
    temperature: float = 0.2,
) -> tuple[list[str], list[Beat] | None]:
    """LLM self-critique; returns issue notes and optional revised beats."""
    raw = llm.chat(
        build_spine_critique_messages(beats, report, digest, max_beats),
        temperature=temperature,
        max_tokens=max(4096, max_beats * 280),
    )
    obj = _extract_json_object(raw)
    issues = [str(x).strip() for x in obj.get("issues", []) if str(x).strip()]
    revised: list[Beat] | None = None
    if obj.get("beats"):
        try:
            revised = _beats_from_items(obj["beats"], scenes)
            revised_report = validate_spine_coverage(scenes, revised)
            if not revised_report.ok:
                issues.append(
                    "critique revision still has coverage gaps — keeping prior beats"
                )
                revised = None
            elif len(revised) > max_beats:
                issues.append(
                    f"critique revision exceeded beat cap ({len(revised)} > {max_beats})"
                )
                revised = None
        except ValueError as exc:
            issues.append(f"critique revision unparsable: {exc}")
    return issues, revised


def derive_beats(
    log: Log,
    *,
    max_beats: int = 14,
    temperature: float = 0.3,
    use_cards: bool = True,
    use_premise: bool = True,
    force_cards: bool = False,
    critique: bool = True,
    on_card_progress=None,
    on_progress=None,
) -> SpineProposal:
    """Run the model over the scene digest and return a proposed beat spine.
    With `use_cards`, runs (or loads) the scene-card map pass first so the model
    reasons over faithful synopses; with `use_premise`, primes it with the
    non-spoiler story context. Writes nothing canon; the caller persists the
    proposal.

    ``on_progress(stage, detail, *, step=0, total=0)`` is optional UI/CLI
    feedback (cards → model → critique). ``on_card_progress`` stays for the
    CLI's batch printer and is also forwarded into card progress events.
    """
    def _progress(stage: str, detail: str, *, step: int = 0, total: int = 0) -> None:
        if not on_progress:
            return
        try:
            on_progress(stage, detail, step=step, total=total)
        except TypeError:
            on_progress(stage, detail)

    def _cards_progress(done: int, total: int, n: int) -> None:
        _progress(
            "cards",
            f"scene cards batch {done}/{total} ({n} scenes)",
            step=done,
            total=total,
        )
        if on_card_progress:
            on_card_progress(done, total, n)

    _progress("derive", "segmenting scenes", step=0, total=0)
    scenes = segment_scenes(log)
    episodes = group_episodes(scenes)
    cards = (
        scene_cards(log, force=force_cards, on_progress=_cards_progress)
        if use_cards
        else None
    )
    digest = build_scene_digest(scenes, cards)
    premise = canon_mod.story_primer() if use_premise else None
    messages = build_beat_messages(digest, max_beats, premise=premise)
    # ~200 tokens per beat object; 50-scene logs need headroom beyond the old 2k cap.
    max_tokens = max(4096, max_beats * 256)
    _progress("model", f"proposing up to {max_beats} beats", step=0, total=0)
    raw = llm.chat(messages, temperature=temperature, max_tokens=max_tokens)
    try:
        beats = _beats_from_model(raw, scenes)
    except ValueError:
        if max_tokens < 8192:
            raw = llm.chat(messages, temperature=temperature, max_tokens=8192)
            beats = _beats_from_model(raw, scenes)
        else:
            config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
            debug = config.WORKSPACE_DIR / "last_derive_raw.txt"
            debug.write_text(raw, encoding="utf-8")
            raise ValueError(
                "model did not return a JSON array of beats "
                f"(raw reply saved to {debug})"
            ) from None

    coverage_report = validate_spine_coverage(scenes, beats)
    critique_notes: list[str] = []
    if critique:
        _progress("critique", "checking coverage / revising beats", step=0, total=0)
        try:
            issues, revised = critique_spine_beats(
                scenes,
                beats,
                coverage_report,
                digest,
                max_beats=max_beats,
            )
            critique_notes.extend(issues)
            if revised is not None:
                beats = revised
                critique_notes.insert(0, "applied critique revision")
                coverage_report = validate_spine_coverage(scenes, beats)
        except (ValueError, llm.ModelError) as exc:
            critique_notes.append(f"critique pass skipped: {exc}")

    _progress("enrich", "attaching scene-card evidence", step=0, total=0)
    enrich_beats_with_evidence(beats, scenes, cards)

    return SpineProposal(
        log=str(log.path.resolve()),
        beats=beats,
        n_scenes=len(scenes),
        n_episodes=len(episodes),
        coverage={
            "ok": coverage_report.ok,
            "uncovered_scene_ids": coverage_report.uncovered_scene_ids,
            "scene_gaps": [
                {"from": lo, "to": hi} for lo, hi in coverage_report.scene_gaps
            ],
        },
        critique_notes=critique_notes or None,
    )


def _format_spine_md(proposal: SpineProposal) -> str:
    lines = [
        f"# Proposed beat spine — {len(proposal.beats)} beats",
        "",
        f"- **Log:** `{proposal.log}`",
        f"- **Scenes:** {proposal.n_scenes}  **Episodes:** {proposal.n_episodes}",
    ]
    if proposal.coverage is not None:
        ok = proposal.coverage.get("ok", False)
        lines.append(f"- **Coverage:** {'OK' if ok else 'issues — see critique'}")
    lines += [
        "",
        "| # | beats span | kind | title | justification |",
        "|---|---|---|---|---|",
    ]
    for b in proposal.beats:
        lines.append(
            f"| {b.beat_id} | {b.span_label()} | {b.kind or '—'} | "
            f"{b.title} | {b.justification} |"
        )
    if any(b.evidence for b in proposal.beats):
        lines += ["", "## Evidence (scene cards)", ""]
        for b in proposal.beats:
            if not b.evidence:
                continue
            lines.append(f"### B{b.beat_id} — {b.title}")
            for ev in b.evidence:
                lines.append(f"- {ev}")
            lines.append("")
    lines += [
        "---",
        "_Commit it:_ `python -m story_editor structure commit`",
        "_Throw it away:_ `python -m story_editor structure discard`",
    ]
    return "\n".join(lines)


def _format_spine_critique_md(proposal: SpineProposal) -> str:
    lines = ["# Spine critique", ""]
    if proposal.coverage:
        cov = SpineCoverageReport(
            n_scenes=proposal.n_scenes,
            n_beats=len(proposal.beats),
            uncovered_scene_ids=proposal.coverage.get("uncovered_scene_ids", []),
            scene_gaps=[
                (g["from"], g["to"])
                for g in proposal.coverage.get("scene_gaps", [])
            ],
            ok=bool(proposal.coverage.get("ok")),
        )
        lines.extend(cov.summary_lines())
        lines.append("")
    if proposal.critique_notes:
        lines.append("## Critique notes")
        for note in proposal.critique_notes:
            lines.append(f"- {note}")
    else:
        lines.append("_No critique notes._")
    return "\n".join(lines)


def write_pending_spine(proposal: SpineProposal) -> None:
    config.PENDING_SPINE.write_text(
        json.dumps(proposal.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    config.PENDING_SPINE_MD.write_text(_format_spine_md(proposal), encoding="utf-8")
    config.PENDING_SPINE_CRITIQUE.write_text(
        _format_spine_critique_md(proposal), encoding="utf-8"
    )


def load_pending_spine() -> SpineProposal | None:
    if not config.PENDING_SPINE.exists():
        return None
    return SpineProposal.from_json(
        json.loads(config.PENDING_SPINE.read_text(encoding="utf-8"))
    )


def discard_pending_spine() -> bool:
    existed = config.PENDING_SPINE.exists()
    config.PENDING_SPINE.unlink(missing_ok=True)
    config.PENDING_SPINE_MD.unlink(missing_ok=True)
    config.PENDING_SPINE_CRITIQUE.unlink(missing_ok=True)
    return existed


def validate_proposal_coverage(proposal: SpineProposal, log: Log) -> SpineCoverageReport:
    """Re-run deterministic coverage against the log's current scene segmentation."""
    scenes = segment_scenes(log)
    return validate_spine_coverage(scenes, proposal.beats)


def commit_spine() -> SpineProposal:
    """Promote the pending proposal to the working derived spine."""
    proposal = load_pending_spine()
    if proposal is None:
        raise FileNotFoundError("no pending spine to commit (run `structure derive`)")
    config.DERIVED_SPINE.write_text(
        json.dumps(proposal.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    discard_pending_spine()
    return proposal


def _load_canon_episode_map(log: Log) -> SpineProposal | None:
    """Expose the locked episode map through the legacy spine interface.

    Beats here are compatibility records only: ``beat_id`` carries the
    canonical ``episode_id``.  The API also declares ``unit_type=episode`` so
    new consumers never need to infer the semantics from the field name.
    """
    path = getattr(config, "CANON_EPISODE_MAP", None)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("status") != "canon_locked" or data.get("unit_type") != "episode":
        return None

    scenes = segment_scenes(log)

    def scene_at(msg_id: int) -> int | None:
        for scene in scenes:
            if scene.start_msg_id <= msg_id <= scene.end_msg_id:
                return scene.scene_id
        return None

    beats: list[Beat] = []
    for row in data.get("episodes") or []:
        try:
            episode_id = int(row["episode_id"])
            start, end = (int(v) for v in row["msg_range"])
        except (KeyError, TypeError, ValueError):
            continue
        anchor = row.get("anchor") or {}
        beat = Beat(
            beat_id=episode_id,
            title=str(row.get("title") or "").strip(),
            justification="Director-approved canonical episode",
            start_msg_id=start,
            end_msg_id=end,
            first_scene_id=scene_at(start),
            last_scene_id=scene_at(end),
            kind="episode",
            start_uid=str(anchor.get("from_uid") or ""),
            end_uid=str(anchor.get("to_uid") or ""),
        )
        beat.bind(log)
        beats.append(beat)
    if not beats:
        return None
    report = validate_spine_coverage(scenes, beats)
    coverage = {
        "ok": report.ok,
        "n_scenes": report.n_scenes,
        "n_beats": report.n_beats,
        "uncovered_scene_ids": report.uncovered_scene_ids,
        "scene_gaps": report.scene_gaps,
    }
    return SpineProposal(
        log=log.path.name,
        beats=beats,
        n_scenes=len(scenes),
        n_episodes=len(beats),
        created=str(data.get("locked_at") or data.get("updated_at") or ""),
        coverage=coverage,
        critique_notes=["Director-approved canon episode map"],
        unit_type="episode",
        authority="canon_locked",
    )


def chapter_beats(log: Log) -> list[Beat]:
    """The book's chapters: the locked episode map when there is one, otherwise
    one chapter per scene of the log, so a story can be exported before (or
    without) ever locking a chapter map."""
    spine = load_derived_spine(log)
    if spine is not None and spine.unit_type == "episode":
        return list(spine.beats)
    return [
        Beat(
            beat_id=n,  # from 0, like a locked map's episode ids
            title=f"Chapter {n + 1}" + (f": {scene.location}" if scene.location else ""),
            justification="one chapter per scene",
            start_msg_id=scene.start_msg_id,
            end_msg_id=scene.end_msg_id,
            first_scene_id=scene.scene_id,
            last_scene_id=scene.scene_id,
            kind="scene",
        )
        for n, scene in enumerate(segment_scenes(log))
    ]


def load_derived_spine(log: Log | None = None) -> SpineProposal | None:
    supplied_log = log is not None
    if log is None:
        from . import loader as loader_mod

        try:
            log = loader_mod.load(config.working_log())
        except (FileNotFoundError, OSError):
            log = None

    # Only apply workspace canon to the workspace log.  Tests and callers that
    # supply an unrelated log must continue to see their own derived fixture.
    if log is not None:
        try:
            is_working_log = log.path.resolve() == config.working_log().resolve()
        except (AttributeError, OSError):
            is_working_log = not supplied_log
        canon_path = getattr(config, "CANON_EPISODE_MAP", None)
        canon_belongs_to_workspace = (
            canon_path is not None
            and canon_path.parent.resolve() == config.WORKSPACE_DIR.resolve()
        )
        if is_working_log and canon_belongs_to_workspace:
            canon = _load_canon_episode_map(log)
            if canon is not None:
                return canon

    if not config.DERIVED_SPINE.exists():
        return None
    proposal = SpineProposal.from_json(
        json.loads(config.DERIVED_SPINE.read_text(encoding="utf-8"))
    )
    if log is not None:
        for beat in proposal.beats:
            beat.bind(log)
    return proposal


# --------------------------------------------------------------------------- #
# Audit mode: compare the authored spine against where the prose's turns land.
# The authored spine is narrative/thematic and untied to msg_ids; the derived
# spine is the model's read of THIS log. We align the two and flag drift -
# including, crucially, that this log is only an early SLICE of the whole story
# (it realises perhaps the first handful of the 14 authored beats).
# --------------------------------------------------------------------------- #

@dataclass
class AuthoredBeat:
    number: int
    title: str
    text: str
    letter: str = ""   # sub-beat suffix: "13a" → number 13, letter "a"
    section: str = ""  # the '## ' heading it was found under

    @property
    def label(self) -> str:
        return f"{self.number}{self.letter}"

    @property
    def sort_key(self) -> tuple[int, str]:
        return (self.number, self.letter)

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "number": self.number,
            "letter": self.letter,
            "title": self.title,
            "text": self.text,
            "section": self.section,
        }


_AUTHORED_HEADING = "## Chronological beats"
_NUM_ITEM_RE = re.compile(r"^(\d+)\.\s+(.*)$")
# A lettered sub-beat is written as a bold run-in head: `**13a. Reunion** …`
# The title is inside the bold run, so capture it rather than letting the
# generic bold heuristic pick up whatever emphasis follows the head.
_SUB_ITEM_RE = re.compile(r"^\*\*(\d+)([a-z])\.\s*(.*?)\*\*\s*(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _clean_title(raw: str) -> str:
    """A beat's run-in head ends in whatever punctuation joined it to its
    sentence; a list of titles should not inherit that."""
    return raw.strip().rstrip(":.,;\u2014- ").strip()


def parse_authored_spine(md_path=None) -> list[AuthoredBeat]:
    """The authored beats, wherever in the document they live.

    They are not one tidy list. Beats 1–13 sit under '## Chronological beats',
    the lettered sub-beats (13a, 13b) are bold run-in heads inside it, and 14–19
    continue much further down inside the Act sections. Reading only the first
    section — which is what this used to do — silently drops the entire unwritten
    future, and the future is the point: a beat with no message range is how the
    Spine tab says "planned, not yet written", which is what makes a checkmark
    beside beat 13 mean anything.

    The document also holds numbered lists that are NOT beats (the interlude
    schedule, the through-lines, sub-sequences inside a beat). What separates
    them is that the beats form one strictly ascending run, so a numbered item
    counts only when it continues that run — a list restarting at 1. is ignored,
    and indented sub-sequences never match at all.
    """
    path = md_path or config.AUTHORED_SPINE_MD
    if not path:
        raise FileNotFoundError(f"project {config.PROJECT_ID!r} has no authored spine file")
    from pathlib import Path as _P

    lines = _P(path).read_text(encoding="utf-8").splitlines()

    beats: list[AuthoredBeat] = []
    section = ""
    highest = 0            # last accepted main beat number
    cur: AuthoredBeat | None = None
    cur_lines: list[str] = []
    collecting = False     # body text stops at a heading, scanning does not

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        raw = " ".join(s.strip() for s in cur_lines if s.strip())
        if not cur.title:
            bold = _BOLD_RE.search(raw)
            cur.title = _clean_title(bold.group(1) if bold else raw[:60])
        cur.text = raw.strip()
        beats.append(cur)
        cur = None

    for ln in lines:
        if ln.startswith("## "):
            flush()
            section = ln[3:].strip()
            collecting = False
            continue

        sub = _SUB_ITEM_RE.match(ln)
        if sub and int(sub.group(1)) <= highest:
            # Belongs to a beat already read (13a follows 13), so it is a real
            # sub-beat rather than some other bolded number.
            flush()
            cur = AuthoredBeat(
                number=int(sub.group(1)),
                title=_clean_title(sub.group(3)),
                text="",
                letter=sub.group(2),
                section=section,
            )
            cur_lines = [sub.group(3), sub.group(4)]
            collecting = True
            continue

        item = _NUM_ITEM_RE.match(ln)
        if item and int(item.group(1)) == highest + 1:
            flush()
            highest = int(item.group(1))
            cur = AuthoredBeat(number=highest, title="", text="", section=section)
            cur_lines = [item.group(2)]
            collecting = True
            continue

        if collecting and cur is not None:
            cur_lines.append(ln)

    flush()
    beats.sort(key=lambda b: b.sort_key)
    return beats


# --------------------------------------------------------------------------- #
# Drift-detect (post-revision): re-derive the spine and diff it against the
# committed baseline, so big edits' effect on the story's SHAPE is visible.
# Beats are matched by CONTENT similarity (title + justification), not raw
# msg-range, because edits shift msg_ids - a positional diff would report
# everything as "moved" after a single insertion. difflib keeps it dependency
# free and deterministic.
# --------------------------------------------------------------------------- #

@dataclass
class BeatDelta:
    status: str  # stable | moved | added | dropped
    base: Beat | None
    cur: Beat | None
    similarity: float = 0.0
    note: str = ""


@dataclass
class DriftReport:
    baseline_created: str
    n_base: int
    n_cur: int
    deltas: list[BeatDelta] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {"stable": 0, "moved": 0, "added": 0, "dropped": 0}
        for d in self.deltas:
            c[d.status] += 1
        return c

    def shifted(self) -> bool:
        c = self.counts()
        return bool(c["added"] or c["dropped"] or c["moved"])


def _beat_text(b: Beat) -> str:
    return f"{b.title}. {b.justification}".lower()


def _beat_text_sim(a: Beat, b: Beat) -> float:
    return difflib.SequenceMatcher(None, _beat_text(a), _beat_text(b)).ratio()


def _interval_iou(a: Beat, b: Beat) -> float:
    """Intersection-over-union of two msg-id spans. Stable on an unchanged log;
    degrades gracefully when edits shift ids (overlapping beats still score high)."""
    lo = max(a.start_msg_id, b.start_msg_id)
    hi = min(a.end_msg_id, b.end_msg_id)
    inter = max(0, hi - lo + 1)
    union = (
        (a.end_msg_id - a.start_msg_id + 1)
        + (b.end_msg_id - b.start_msg_id + 1)
        - inter
    )
    return inter / union if union > 0 else 0.0


def _beat_similarity(a: Beat, b: Beat) -> float:
    """Match score blending content (robust to renumbering) and span overlap
    (robust to the model's wording variance). Either signal alone can confirm a
    match: same prose OR same place."""
    return 0.5 * _beat_text_sim(a, b) + 0.5 * _interval_iou(a, b)


def diff_spines(
    baseline: SpineProposal, current: SpineProposal, *, threshold: float = 0.4
) -> DriftReport:
    """Greedy hybrid (content + span-overlap) matching between two spines;
    classify each beat as stable / moved / added / dropped."""
    candidates: list[tuple[float, int, int]] = []
    for i, cb in enumerate(current.beats):
        for j, bb in enumerate(baseline.beats):
            candidates.append((_beat_similarity(cb, bb), i, j))
    candidates.sort(reverse=True)

    used_cur: set[int] = set()
    used_base: set[int] = set()
    matched: list[tuple[int, int, float]] = []
    for sim, i, j in candidates:
        if sim < threshold:
            break
        if i in used_cur or j in used_base:
            continue
        used_cur.add(i)
        used_base.add(j)
        matched.append((i, j, sim))

    deltas: list[BeatDelta] = []
    for i, j, sim in sorted(matched, key=lambda t: t[0]):
        cb = current.beats[i]
        bb = baseline.beats[j]
        same_span = (cb.start_msg_id, cb.end_msg_id) == (bb.start_msg_id, bb.end_msg_id)
        # Treat near-identical spans as stable - a one- or two-message boundary
        # nudge is re-derivation jitter, not a real shape change. Only a
        # meaningful span change counts as "moved".
        near = _interval_iou(cb, bb) >= 0.9
        base_len = bb.end_msg_id - bb.start_msg_id + 1
        cur_len = cb.end_msg_id - cb.start_msg_id + 1
        note = ""
        if not (same_span or near):
            note = (
                f"span {bb.start_msg_id}–{bb.end_msg_id} ({base_len} msg) → "
                f"{cb.start_msg_id}–{cb.end_msg_id} ({cur_len} msg)"
            )
        deltas.append(
            BeatDelta(
                status="stable" if (same_span or near) else "moved",
                base=bb, cur=cb, similarity=sim, note=note,
            )
        )
    for i, cb in enumerate(current.beats):
        if i not in used_cur:
            deltas.append(BeatDelta(status="added", base=None, cur=cb))
    for j, bb in enumerate(baseline.beats):
        if j not in used_base:
            deltas.append(BeatDelta(status="dropped", base=bb, cur=None))

    order = {"added": 0, "moved": 1, "stable": 2, "dropped": 3}
    deltas.sort(key=lambda d: (order[d.status],
                               (d.cur.beat_id if d.cur else d.base.beat_id)))
    return DriftReport(
        baseline_created=baseline.created,
        n_base=len(baseline.beats),
        n_cur=len(current.beats),
        deltas=deltas,
    )


def detect_drift(
    log: Log,
    *,
    baseline: SpineProposal | None = None,
    max_beats: int = 12,
    on_card_progress=None,
) -> DriftReport:
    """Re-derive the current spine and diff it against the committed baseline."""
    baseline = baseline or load_derived_spine()
    if baseline is None:
        raise FileNotFoundError(
            "no committed baseline spine to compare against; run "
            "`structure derive` then `structure commit` first"
        )
    current = derive_beats(log, max_beats=max_beats, on_card_progress=on_card_progress)
    return diff_spines(baseline, current)



# How many retrieved passages each authored beat carries into the audit prompt.
# Titles alone lied; these are the grounding. Keep them short — the call has to
# fit a local context window with every beat present.
_AUDIT_HITS = 4
_AUDIT_EXCERPT = 180
_AUDIT_AUTHORED_CHARS = 350


@dataclass
class AuditExcerpt:
    msg_id: int
    speaker: str
    preview: str
    score: float = 0.0

    def to_prompt(self) -> str:
        who = self.speaker or "?"
        return f"[msg {self.msg_id} | {who}] {self.preview}"


_STALE_ASIDE = re.compile(
    r"\*\([^)]*not yet in jsonl[^)]*\)\*|"
    r"\([^)]*not yet in jsonl[^)]*\)|"
    r"not yet in jsonl\.?",
    re.IGNORECASE,
)
_MSG_RANGE_RE = re.compile(
    r"msgs?\s*~?\s*(\d+)\s*[\u2013\u2014\-–—]+\s*(\d+)",
    re.IGNORECASE,
)
_MSG_ONE_RE = re.compile(r"msgs?\s*~?\s*(\d+)\b", re.IGNORECASE)
# Bare citations in spine prose: (~507), (~697–702), ~507
_MSG_PAREN_RANGE_RE = re.compile(
    r"\(\s*~?\s*(\d+)\s*[\u2013\u2014\-–—]+\s*(\d+)\s*\)",
)
_MSG_PAREN_ONE_RE = re.compile(r"\(\s*~?\s*(\d{2,4})\s*\)")
_MSG_TILDE_ONE_RE = re.compile(r"(?<![A-Za-z0-9])~\s*(\d{2,4})\b")
# Lowercase place nouns the proper-noun pass skips (harbor, tavern, …).
_PLACE_LEXICON = (
    "harbor", "harbour", "dock", "docks", "guild", "pool", "tavern", "throne",
    "bedchamber", "castle",
)
# US/UK spellings and near-synonyms so "harbor" still hits a "harbour" scene.
_PLACE_SYNONYMS = {
    "harbor": ("harbour", "docks"),
    "harbour": ("harbor", "docks"),
    "dock": ("docks", "harbor", "harbour"),
    "docks": ("dock", "harbor", "harbour"),
}
# Too broad for location LIKE / card-span fill — "harbor" matches every street
# named after the harbour and crowds out the scene itself. Keyword only.
_GENERIC_PLACE = frozenset({
    "harbor", "harbour", "dock", "docks", "pool", "castle",
    "guild", "tavern", "throne", "bedchamber", "street",
})
_BOLD_TERM = re.compile(r"\*\*([^*]{2,40})\*\*")
# Unicode-aware: Søren, Hobb's, etc.
_PROPER = re.compile(
    r"\b([A-ZÁÉÍÓÚÄÖÜÅÆØ][\w'’]{2,}(?:\s+[A-ZÁÉÍÓÚÄÖÜÅÆØ][\w'’]{2,}){0,3})\b",
    re.UNICODE,
)
_STOP_PROPER = frozenset({
    "The", "This", "That", "Then", "When", "After", "Before",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "November", "December", "January", "Act", "Beat", "Sequence", "Deeper",
    "Not", "She", "Her", "His", "They", "What", "With", "From", "Present",
    "Past", "Still", "Intercut", "Pincer",
})
_STOP_ANCHOR = frozenset({
    "not", "deeper", "fascinated", "unspeakable", "detonator", "transformation",
    "accident", "logged", "variant", "keystone", "just", "hour", "longer",
    # Title fluff — capitalised in headings, floods FTS before real nouns.
    "inciting", "trauma", "open", "circle", "inner", "load", "pays", "early",
    "restructuring", "activates", "capture", "turns", "staff", "pressure",
    "institutional", "flash", "chorus", "intimacy", "conversion",
})
_STOP_TOKEN = frozenset({
    "that", "this", "with", "from", "into", "have", "been", "were", "their",
    "about", "after", "before", "where", "which", "while", "would", "could",
    "should", "there", "these", "those", "through", "under", "over", "between",
    "inciting", "trauma", "after", "before", "early", "court", "turns",
    "staff", "pressure", "activates", "restructuring", "intimacy", "chorus",
})
# Load-bearing story nouns the proper-noun pass misses (lowercase in prose).
# Story-specific; none are built in.
_EVENT_LEXICON: tuple[str, ...] = ()
# FTS is picky about inflection; a project can expand audit-critical stems here.
_KEYWORD_STEMS: dict[str, tuple[str, ...]] = {}


def _audit_intent(beat: AuthoredBeat) -> str:
    """Beat text for the prompt — drop stale 'not yet in jsonl' asides that
    would talk the model into calling written material absent."""
    text = _STALE_ASIDE.sub(" ", beat.text or "")
    return re.sub(r"\s+", " ", text).strip()


def _audit_query(beat: AuthoredBeat) -> str:
    """What we ask the index for. Title first; then the body with markdown noise
    stripped so proper nouns (Tobias, Western Shoals) survive into the query."""
    body = re.sub(r"[*_`]+", " ", _audit_intent(beat))
    body = re.sub(r"\s+", " ", body).strip()
    return f"{beat.title}. {body[:280]}".strip()


def _audit_anchors(beat: AuthoredBeat) -> list[str]:
    """Load-bearing names the fused query often buries — bolded terms and other
    proper nouns from the authored beat, asked as literal keyword searches."""
    text = _audit_intent(beat)
    found: list[str] = []

    def add(term: str) -> None:
        term = re.sub(r"\s+", " ", term).strip().strip(":.,;—- ")
        if not term or term in found:
            return
        if term.lower() in _STOP_ANCHOR or term in _STOP_PROPER:
            return
        if not any(ch.isupper() for ch in term):
            return
        if len(term) < 4 and " " not in term:
            return
        if term.lower() in {"did", "does", "has", "have", "was", "were"}:
            return
        found.append(term)
        # "The Salt Anchor" → also "Salt Anchor" for FTS/location matching.
        if term.lower().startswith("the ") and len(term) > 6:
            bare = term[4:].strip()
            if bare and bare not in found:
                found.append(bare)

    for match in _BOLD_TERM.finditer(text):
        add(match.group(1))
    for match in _PROPER.finditer(text):
        add(match.group(1))
    for match in _PROPER.finditer(beat.title or ""):
        add(match.group(1))
    return found[:8]


def _msg_ids_cited_in_beat(beat: AuthoredBeat) -> list[int]:
    """Explicit msg refs in the authored prose.

    Accepts ``msgs ~627–641``, bare ``(~507)``, ``(~697–702)``, and ``~507``.
    """
    text = f"{beat.title or ''} {beat.text or ''}"
    ids: list[int] = []

    def _add_range(lo: int, hi: int) -> None:
        if hi < lo:
            lo, hi = hi, lo
        hi = min(hi, lo + 40)
        ids.extend(range(lo, hi + 1))

    for match in _MSG_RANGE_RE.finditer(text):
        _add_range(int(match.group(1)), int(match.group(2)))
    for match in _MSG_PAREN_RANGE_RE.finditer(text):
        _add_range(int(match.group(1)), int(match.group(2)))
    for match in _MSG_ONE_RE.finditer(text):
        ids.append(int(match.group(1)))
    for match in _MSG_PAREN_ONE_RE.finditer(text):
        ids.append(int(match.group(1)))
    for match in _MSG_TILDE_ONE_RE.finditer(text):
        ids.append(int(match.group(1)))
    # Stable unique, keep authored order.
    seen: set[int] = set()
    out: list[int] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _place_lexicon_terms(beat: AuthoredBeat) -> list[str]:
    """Harbor/tavern/pool etc. — lowercase place words proper-noun anchors miss."""
    blob = f"{beat.title or ''} {_audit_intent(beat)}".lower()
    out: list[str] = []
    for term in _PLACE_LEXICON:
        if re.search(rf"\b{re.escape(term)}\b", blob) and term not in out:
            out.append(term)
    # Expand harbor↔harbour so spelling drift doesn't miss the scene.
    expanded: list[str] = []
    for term in out:
        if term not in expanded:
            expanded.append(term)
        for syn in _PLACE_SYNONYMS.get(term, ()):
            if syn not in expanded:
                expanded.append(syn)
    return expanded


def _distinctive_tokens(query: str) -> list[str]:
    """Body tokens worth a keyword pass (compass, tar, lamplight…)."""
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-zÁÉÍÓÚÄÖÜÅÆØáéíóúäöüåæø'’]{5,}", query, re.UNICODE):
        tok = raw.strip("'’")
        if tok.lower() in _STOP_TOKEN or tok in tokens:
            continue
        tokens.append(tok)
        if len(tokens) >= 8:
            break
    return tokens


def _event_lexicon_terms(beat: AuthoredBeat) -> list[str]:
    """Lowercase story nouns the title fluff buries (see _EVENT_LEXICON)."""
    blob = f"{beat.title or ''} {_audit_intent(beat)}".lower()
    out: list[str] = []
    for term in _EVENT_LEXICON:
        if re.search(rf"\b{re.escape(term)}\b", blob) and term not in out:
            out.append(term)
    return out


def _expand_keyword_stems(terms: list[str]) -> list[str]:
    """Add stem variants (see _KEYWORD_STEMS) without displacing the primary terms."""
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
        for stem in _KEYWORD_STEMS.get(key, ()):
            if stem not in seen:
                seen.add(stem)
                out.append(stem)
    return out


def _keyword_terms_for_beat(beat: AuthoredBeat) -> list[str]:
    """Ordered keyword needles: event nouns first, place lexicon last.

    Place words first would fill the 8-slot evidence pack with setting noise
    before the beat's own nouns ran.
    """
    anchors = [
        a for a in _audit_anchors(beat)
        if a.lower() not in _STOP_ANCHOR and a.lower() not in _STOP_TOKEN
    ]
    # Bold terms even when lowercase / short of the proper-noun pass.
    bold: list[str] = []
    for match in _BOLD_TERM.finditer(_audit_intent(beat)):
        term = match.group(1).strip()
        if term and term.lower() not in _STOP_ANCHOR and term not in bold:
            bold.append(term)
    # Proper-noun anchors before bag-of-words distinctive tokens, otherwise
    # generic title words fill the pack first.
    primary = list(dict.fromkeys(
        _event_lexicon_terms(beat)
        + bold
        + anchors
        + _distinctive_tokens(_audit_query(beat))
    ))
    place = _place_lexicon_terms(beat)
    return _expand_keyword_stems(list(dict.fromkeys(primary + place)))


def _derived_covering(derived: SpineProposal, msg_id: int) -> list[int]:
    return [
        b.beat_id
        for b in derived.beats
        if b.start_msg_id <= msg_id <= b.end_msg_id
    ]


def _excerpt_from_hit(hit) -> AuditExcerpt:
    body = " ".join((hit.text_clean or hit.text or "").split())
    return AuditExcerpt(
        msg_id=int(hit.msg_id),
        speaker=hit.speaker or "",
        preview=body[:_AUDIT_EXCERPT] + ("…" if len(body) > _AUDIT_EXCERPT else ""),
        score=float(getattr(hit, "score", 0.0) or 0.0),
    )


def _excerpt_from_row(row) -> AuditExcerpt:
    body = " ".join(((row["text_clean"] or row["text"] or "")).split())
    return AuditExcerpt(
        msg_id=int(row["msg_id"]),
        speaker=row["speaker"] or "",
        preview=body[:_AUDIT_EXCERPT] + ("…" if len(body) > _AUDIT_EXCERPT else ""),
        score=1.0,
    )


def _index_rows_by_ids(db_path: Path, msg_ids: list[int]) -> list[AuditExcerpt]:
    if not msg_ids:
        return []
    from .index.search import _connect

    conn = _connect(db_path)
    try:
        out: list[AuditExcerpt] = []
        for mid in msg_ids:
            row = conn.execute(
                "SELECT msg_id, speaker, text, text_clean FROM messages WHERE msg_id = ?",
                (mid,),
            ).fetchone()
            if row is not None:
                out.append(_excerpt_from_row(row))
        return out
    finally:
        conn.close()


def _index_rows_by_location(db_path: Path, terms: list[str], *, limit: int = 8) -> list[AuditExcerpt]:
    """Place names often live only in the header `location` column — Ropewalk,
    Salt Anchor, Hobb's Wharf — so body search alone will miss whole beats."""
    if not terms:
        return []
    from .index.search import _connect

    conn = _connect(db_path)
    try:
        out: list[AuditExcerpt] = []
        seen: set[int] = set()
        for term in terms:
            needle = term.strip()
            if len(needle) < 3:
                continue
            rows = conn.execute(
                "SELECT msg_id, speaker, text, text_clean FROM messages "
                "WHERE location LIKE ? ORDER BY msg_id LIMIT ?",
                (f"%{needle}%", limit),
            ).fetchall()
            for row in rows:
                mid = int(row["msg_id"])
                if mid in seen:
                    continue
                seen.add(mid)
                out.append(_excerpt_from_row(row))
        return out
    finally:
        conn.close()




def _msg_ids_from_card_locations(terms: list[str], *, per_scene: int = 4) -> list[int]:
    """Map place-name terms to message ids via scene-card locations/aliases.

    Prefer this when the index has not been rebuilt yet, or when the card
    carries a canonical name / alias the raw header never used.
    """
    if not terms:
        return []
    needles = [t.strip().lower() for t in terms if len(t.strip()) >= 3]
    if not needles:
        return []
    by_span = scene_location_by_span()
    ids: list[int] = []
    for (start, end), card in sorted(by_span.items()):
        blob = location_search_blob(
            card.get("location"), card.get("location_aliases") or []
        ) or ""
        blob_l = blob.lower()
        if not any(n in blob_l for n in needles):
            continue
        # Sample edges + middle of the scene span.
        span = list(range(start, end + 1))
        if len(span) <= per_scene:
            ids.extend(span)
        else:
            mid = len(span) // 2
            sample = [span[0], span[1], span[mid], span[-2], span[-1]]
            ids.extend(sample[:per_scene] if per_scene < 5 else sample)
    seen: set[int] = set()
    out: list[int] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def retrieve_audit_evidence(
    authored: list[AuthoredBeat],
    *,
    db_path=None,
    embedder=None,
    hits_per_beat: int = _AUDIT_HITS,
    on_progress=None,
) -> dict[str, list[AuditExcerpt]]:
    """Top passages from the log for each authored beat.

    Pulls four sources, in priority order:
    1. msg ranges cited in the authored beat itself (`msgs ~627–641`)
    2. index rows whose ``location`` matches a place-name anchor
    3. keyword hits on anchors / distinctive tokens
    4. fused semantic+keyword search on the beat query
    """
    from .index import search as index_search
    from .index.search import search_keyword

    path = Path(db_path) if db_path else config.index_db_for(config.working_log())
    if not path.exists():
        raise FileNotFoundError(
            f"search index missing at {path}; run `story-editor index build --rebuild`"
        )
    out: dict[str, list[AuditExcerpt]] = {}
    n = len(authored)
    for bi, beat in enumerate(authored, start=1):
        if on_progress:
            on_progress(
                "retrieve",
                f"evidence A{beat.label} ({bi}/{n})",
                step=bi,
                total=n,
            )
        query = _audit_query(beat)
        anchors = _audit_anchors(beat)
        try:
            forced = _index_rows_by_ids(path, _msg_ids_cited_in_beat(beat))
            # Sample cited ranges — keep edges + mid so a 40-msg span fits.
            if len(forced) > 6:
                forced = (
                    forced[:2]
                    + forced[len(forced) // 2 : len(forced) // 2 + 2]
                    + forced[-2:]
                )
            # Location search only — never character names (a name is not a place).
            from . import canon_layer
            _not_place = {n.lower() for n in _STOP_PROPER} | {
                alias.lower()
                for bible in canon_layer.all_bibles().values()
                for alias in bible.aliases
            }

            def _ok_place(term: str) -> bool:
                cleaned = term.replace("'s", "").replace("’s", "").strip()
                head = (cleaned.split() or [""])[0]
                return (
                    bool(cleaned)
                    and cleaned.lower() not in _not_place
                    and head.lower() not in _not_place
                )

            place_terms: list[str] = []
            for term in anchors:
                if _ok_place(term):
                    place_terms.append(term)
            for tok in re.findall(r"[A-Za-zÁÉÍÓÚÄÖÜÅÆØ'']{4,}", beat.title or "", re.UNICODE):
                if (
                    tok.lower() not in _STOP_TOKEN
                    and tok.lower() not in _STOP_ANCHOR
                    and _ok_place(tok)
                    and tok not in place_terms
                ):
                    place_terms.append(tok)
            for term in _place_lexicon_terms(beat):
                if term not in place_terms:
                    place_terms.append(term)
            # Location/card span search: specific names only (The Broken Oar).
            loc_terms = [
                t for t in place_terms
                if " " in t or t.lower() not in _GENERIC_PLACE
            ]
            located = _index_rows_by_location(path, loc_terms, limit=6)
            # Card locations/aliases — prefer when index headers are sparse.
            from_cards = _index_rows_by_ids(
                path, _msg_ids_from_card_locations(loc_terms, per_scene=4)
            )
            fused = list(
                index_search(
                    path,
                    query,
                    limit=hits_per_beat,
                    embedder=embedder,
                )
            )
            anchored = []
            # Event nouns / bold / distinctive first — never let place lexicon
            # or title fluff consume the evidence budget.
            kw_terms = _keyword_terms_for_beat(beat)
            for anchor in kw_terms:
                try:
                    kw_hits = search_keyword(path, anchor, limit=3)
                except Exception:  # noqa: BLE001 — bad FTS query for one term
                    continue
                for hit in kw_hits:
                    blob = (
                        f"{hit.text_clean or hit.text or ''} "
                        f"{hit.location or ''}"
                    ).lower()
                    if anchor.lower() in blob:
                        anchored.append(hit)
        except Exception as exc:  # noqa: BLE001 — surface as a clean audit error
            raise RuntimeError(
                f"search failed while gathering evidence for A{beat.label}: {exc}"
            ) from exc
        excerpts: list[AuditExcerpt] = []
        seen: set[int] = set()
        # Keyword hits before location LIKE — broad place matches would
        # otherwise crowd out the body hits that actually carry the beat.
        for item in list(forced) + [
            _excerpt_from_hit(h) for h in anchored
        ] + list(from_cards) + list(located) + [_excerpt_from_hit(h) for h in fused]:
            mid = int(item.msg_id)
            if mid in seen:
                continue
            seen.add(mid)
            excerpts.append(item)
            if len(excerpts) >= hits_per_beat + 4:
                break
        out[beat.label] = excerpts
    return out


def build_audit_messages(
    authored: list[AuthoredBeat],
    derived: SpineProposal,
    evidence: dict[str, list[AuditExcerpt]] | None = None,
    *,
    log_tip: int | None = None,
) -> list[dict]:
    """Prompt for the retrieval-grounded audit.

    Each authored beat arrives with the passages the index retrieved for it.
    The model may only call a beat present when it cites those msg_ids. Derived
    beat titles are listed as a secondary map, not as the sole evidence — that
    was the failure mode that marked A8 (Tobias) absent.
    """
    evidence = evidence or {}
    packs: list[str] = []
    for b in authored:
        body = _audit_intent(b)
        lines = [
            f"A{b.label}. {b.title}",
            f"  intent: {body[:_AUDIT_AUTHORED_CHARS]}"
            + ("…" if len(body) > _AUDIT_AUTHORED_CHARS else ""),
        ]
        hits = evidence.get(b.label) or []
        if hits:
            lines.append("  evidence from the log:")
            lines.extend(f"    - {h.to_prompt()}" for h in hits)
        else:
            lines.append("  evidence from the log: (none retrieved)")
        packs.append("\n".join(lines))
    authored_block = "\n\n".join(packs)
    derived_block = "\n".join(
        f"D{b.beat_id} (msg {b.start_msg_id}–{b.end_msg_id}): "
        f"{b.title} — {b.justification}"
        for b in derived.beats
    )
    tip = log_tip if log_tip is not None else max(
        (b.end_msg_id for b in derived.beats), default=0
    )
    system = (
        "You are a story-structure auditor. You judge whether each AUTHORED beat "
        "is realised in THIS log, using only the retrieved evidence passages "
        "attached to that beat. Derived beat titles are a secondary map — they "
        "are not proof of presence or absence.\n\n"
        "Rules:\n"
        "- verdict \"present\": the log realises the beat's load-bearing event or "
        "thread. You MUST cite msg_ids from that beat's evidence list.\n"
        "- verdict \"partial\": something related is on the page, but the "
        "authored turn lands differently (wrong outcome, missing key fact, "
        "wrong order). Cite msg_ids; explain the mismatch in note.\n"
        "- verdict \"absent\": the evidence does not support the beat. Prefer "
        "absent over guessing. Truly unwritten future beats belong here.\n"
        "- Beats that happen before the story opens (backstory, an off-page "
        "inciting event) are often realised as aftermath or recollection in the "
        "opening scenes. Treat those as present/partial when the evidence shows "
        "that aftermath — do not require the off-page moment itself.\n"
        "- Do NOT invent events that are not in the cited passages. If a derived "
        "title contradicts the passages, trust the passages.\n"
        "- Thematic beats (e.g. a design that runs through many scenes) are "
        "present when the evidence shows that thread, even without one set-piece.\n"
        "- Chronology: the log tip is near the current story position. An early "
        "thematic echo of a late beat (a motif, a line of foreshadowing) is not "
        "that beat. If the only evidence for a beat in the last third of the "
        "spine is early in the log, verdict is absent.\n"
        "- Authored labels are strings like \"8\" or \"13a\" — use them exactly.\n\n"
        "Output STRICT JSON only — no prose, no code fence — an object:\n"
        '{"beats":[{"authored":"8","verdict":"present","evidence":[620,621],'
        '"note":"..."}],'
        '"drift":[{"authored":"8","note":"...","evidence":[620]}],'
        '"coverage":"one-sentence summary"}'
    )
    user = (
        f"LOG TIP: msg {tip} (story position is near the end of the derived "
        f"spine — do not mark future beats present from early echoes).\n\n"
        f"AUTHORED BEATS WITH RETRIEVED EVIDENCE:\n{authored_block}\n\n"
        f"DERIVED SPINE (secondary map only):\n{derived_block}\n\n"
        "Return only the JSON object."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _extract_json_object(raw: str) -> dict:
    obj = jsonish.extract_object(raw)
    if obj is None:
        raise ValueError("model did not return a JSON object")
    return obj


def _norm_audit_label(value) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(?:a|beat\s*)?(\d+\s*[a-z]?)\.?$", text, re.IGNORECASE)
    if not match:
        return text
    return match.group(1).replace(" ", "").lower()


def _as_msg_ids(value) -> list[int]:
    out: list[int] = []
    if not isinstance(value, list):
        return out
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _beat_number(label: str) -> int:
    match = re.match(r"(\d+)", str(label or ""))
    return int(match.group(1)) if match else 0


def _apply_chronology_guard(
    beat_rows: list[dict], *, log_tip: int
) -> None:
    """Downgrade late beats whose only evidence is early thematic echo.

    A beat in the last third of the spine that cites only an early motif (a
    late-act turn "found" in a first-act line that merely foreshadows it) is the
    failure mode. Late beats must cite evidence at or past the frontier of the
    written earlier beats, not merely past some fraction of the log tip (that
    still lets a mid-log echo look "late enough").
    """
    numbers = sorted({_beat_number(row["authored"]) for row in beat_rows})
    if len(numbers) < 2:
        return
    split = numbers[max(1, len(numbers) * 2 // 3) - 1]  # last "early" beat number
    frontier = 0
    for row in beat_rows:
        if row["verdict"] not in ("present", "partial"):
            continue
        if not row["evidence"]:
            continue
        if _beat_number(row["authored"]) <= split:
            frontier = max(frontier, max(row["evidence"]))
    if frontier <= 0:
        frontier = max(0, log_tip)
    for row in beat_rows:
        if _beat_number(row["authored"]) <= split:
            continue
        if row["verdict"] not in ("present", "partial"):
            continue
        ev = row["evidence"]
        if not ev:
            continue
        # Must reach the frontier of the earlier beats — early echoes fail.
        if frontier > 0 and max(ev) >= frontier:
            continue
        extra = (
            f"chronology guard: evidence (msg {max(ev)}) predates the current "
            f"story frontier (~msg {frontier or log_tip}); treating as absent"
        )
        note = row["note"]
        row["verdict"] = "absent"
        row["note"] = f"{note} ({extra})".strip() if note else extra
        row["evidence"] = []
        row["derived"] = []


def normalize_audit_result(
    raw: dict,
    *,
    authored: list[AuthoredBeat],
    derived: SpineProposal,
    evidence: dict[str, list[AuditExcerpt]],
    log_tip: int | None = None,
) -> dict:
    """Ground the model's reply: drop uncitable claims, synthesise the legacy
    alignment/absent shape the Spine tab already reads."""
    allowed = {
        label: {h.msg_id for h in hits} for label, hits in evidence.items()
    }
    by_label = {b.label: b for b in authored}
    beat_rows: list[dict] = []
    tip = log_tip if log_tip is not None else max(
        (b.end_msg_id for b in derived.beats), default=0
    )
    for hits in evidence.values():
        for h in hits:
            tip = max(tip, int(h.msg_id))

    raw_beats = raw.get("beats")
    if isinstance(raw_beats, list) and raw_beats:
        for row in raw_beats:
            if not isinstance(row, dict):
                continue
            label = _norm_audit_label(row.get("authored"))
            if label not in by_label:
                continue
            pack = allowed.get(label, set())
            claimed = _as_msg_ids(row.get("evidence"))
            cited = [m for m in claimed if m in pack] if pack else claimed
            verdict = str(row.get("verdict") or "").strip().lower()
            if verdict not in ("present", "partial", "absent"):
                verdict = "present" if cited else "absent"
            if verdict in ("present", "partial") and pack and not cited:
                # A present claim that cites nothing from its evidence pack is
                # the old failure mode. Empty packs (tests / missing index hits)
                # do not trigger this — there was nothing to cite.
                verdict = "absent"
            derived_ids: list[int] = []
            for mid in cited:
                for did in _derived_covering(derived, mid):
                    if did not in derived_ids:
                        derived_ids.append(did)
            for did in _as_msg_ids(row.get("derived")):
                if did not in derived_ids:
                    derived_ids.append(did)
            beat_rows.append({
                "authored": label,
                "verdict": verdict,
                "evidence": cited,
                "derived": derived_ids,
                "note": str(row.get("note") or "").strip(),
            })
    else:
        # Legacy one-shot shape (derived→authored). Keep the parse path alive for
        # old caches/tests; still require nothing we cannot re-derive.
        for row in raw.get("alignment") or []:
            if not isinstance(row, dict):
                continue
            label = _norm_audit_label(row.get("authored"))
            if label not in by_label:
                continue
            try:
                did = int(row.get("derived"))
            except (TypeError, ValueError):
                continue
            beat_rows.append({
                "authored": label,
                "verdict": "present",
                "evidence": [],
                "derived": [did],
                "note": str(row.get("note") or "").strip(),
            })
        for label in (_norm_audit_label(x) for x in (raw.get("absent") or [])):
            if label in by_label and not any(r["authored"] == label for r in beat_rows):
                beat_rows.append({
                    "authored": label,
                    "verdict": "absent",
                    "evidence": [],
                    "derived": [],
                    "note": "",
                })

    seen = {r["authored"] for r in beat_rows}
    for b in authored:
        if b.label not in seen:
            beat_rows.append({
                "authored": b.label,
                "verdict": "absent",
                "evidence": [],
                "derived": [],
                "note": "no verdict returned",
            })

    order = {b.label: i for i, b in enumerate(authored)}
    beat_rows.sort(key=lambda r: order.get(r["authored"], 10_000))
    _apply_chronology_guard(beat_rows, log_tip=tip)

    alignment: list[dict] = []
    for row in beat_rows:
        if row["verdict"] not in ("present", "partial"):
            continue
        note = row["note"]
        if row["evidence"]:
            cite = ",".join(str(m) for m in row["evidence"][:6])
            note = f"{note} (msg {cite})".strip() if note else f"msg {cite}"
        if row["derived"]:
            for did in row["derived"]:
                alignment.append({
                    "derived": did,
                    "authored": row["authored"],
                    "note": note,
                    "evidence": list(row["evidence"]),
                })
        else:
            alignment.append({
                "derived": None,
                "authored": row["authored"],
                "note": note,
                "evidence": list(row["evidence"]),
            })

    absent = [r["authored"] for r in beat_rows if r["verdict"] == "absent"]

    drift_out: list[str] = []
    raw_drift = raw.get("drift") or []
    if isinstance(raw_drift, list):
        for item in raw_drift:
            if isinstance(item, str) and item.strip():
                drift_out.append(item.strip())
            elif isinstance(item, dict):
                label = _norm_audit_label(item.get("authored"))
                note = str(item.get("note") or "").strip()
                cited = [
                    m for m in _as_msg_ids(item.get("evidence"))
                    if m in allowed.get(label, set()) or not allowed.get(label)
                ]
                if not note:
                    continue
                if label and label in by_label:
                    cite = (
                        f" (msg {','.join(str(m) for m in cited[:4])})" if cited else ""
                    )
                    drift_out.append(f"A{label}: {note}{cite}")
                else:
                    drift_out.append(note)
    for row in beat_rows:
        if row["verdict"] == "partial" and row["note"]:
            needle = f"a{row['authored']}".lower()
            if not any(needle in d.lower() for d in drift_out):
                cite = (
                    f" (msg {','.join(str(m) for m in row['evidence'][:4])})"
                    if row["evidence"] else ""
                )
                drift_out.append(f"A{row['authored']}: {row['note']}{cite}")

    coverage = raw.get("coverage")
    if not isinstance(coverage, str):
        coverage = ""

    return {
        "beats": beat_rows,
        "alignment": [a for a in alignment if a.get("derived") is not None],
        "absent": absent,
        "drift": drift_out,
        "coverage": coverage.strip() or (
            "Retrieval-grounded audit of authored beats against this log."
        ),
    }


# One giant prompt with every beat's evidence overflows a local model into an
# empty/useless JSON object. Small batches keep each call grounded and parseable.
_AUDIT_BATCH = 3


def _merge_raw_batches(parts: list[dict]) -> dict:
    beats: list = []
    alignment: list = []
    absent: list = []
    drift: list = []
    coverages: list[str] = []
    for part in parts:
        beats.extend(part.get("beats") or [])
        alignment.extend(part.get("alignment") or [])
        absent.extend(part.get("absent") or [])
        drift.extend(part.get("drift") or [])
        cov = part.get("coverage")
        if isinstance(cov, str) and cov.strip():
            coverages.append(cov.strip())
    merged = {
        "drift": drift,
        "coverage": " ".join(coverages),
    }
    # Prefer the new beats[] shape; keep legacy keys when that is all a batch had.
    if beats:
        merged["beats"] = beats
    else:
        merged["alignment"] = alignment
        merged["absent"] = absent
    return merged


def _coerce_audit_raw(raw: dict) -> dict:
    """Local models often emit one beat object, or ``beats`` as a bare dict."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    beats = out.get("beats")
    if isinstance(beats, dict):
        out["beats"] = [beats]
    elif (
        "beats" not in out
        and "alignment" not in out
        and "absent" not in out
        and out.get("authored") is not None
        and out.get("verdict") is not None
    ):
        out = {
            "beats": [raw],
            "drift": list(raw.get("drift") or []) if isinstance(raw.get("drift"), list) else [],
            "coverage": str(raw.get("coverage") or ""),
        }
    return out


def _audit_batch_usable(raw: dict, labels: set[str]) -> bool:
    """Whether this batch reply actually judged the beats we asked about."""
    raw = _coerce_audit_raw(raw)
    rows = raw.get("beats")
    if isinstance(rows, list) and rows:
        hit = {
            _norm_audit_label(r.get("authored"))
            for r in rows
            if isinstance(r, dict)
        }
        return bool(hit & labels)
    # Legacy shape still counts if it named any of our labels.
    for row in raw.get("alignment") or []:
        if isinstance(row, dict) and _norm_audit_label(row.get("authored")) in labels:
            return True
    for item in raw.get("absent") or []:
        if _norm_audit_label(item) in labels:
            return True
    return False


def audit_spine(
    derived: SpineProposal,
    *,
    md_path=None,
    db_path=None,
    evidence: dict[str, list[AuditExcerpt]] | None = None,
    temperature: float = 0.2,
    attempts: int = 3,
    batch_size: int = _AUDIT_BATCH,
    on_progress=None,
) -> dict:
    """Align the authored spine against the log, grounded in retrieved passages.

    Returns ``{'authored': [...], 'result': {...}, 'evidence': {...}}``. The
    result keeps the legacy alignment/absent/drift keys the Spine tab reads,
    plus a ``beats`` list with per-authored verdicts and msg_id evidence.

    Raises before anything is written, so a run the model fluffed leaves the
    cached alignment from the last good one in place."""
    authored = parse_authored_spine(md_path)
    if not authored:
        raise ValueError(
            f"no authored beats found under '{_AUTHORED_HEADING}' in the spine md"
        )

    def _progress(stage: str, detail: str, *, step: int = 0, total: int = 0) -> None:
        if on_progress:
            try:
                on_progress(stage, detail, step=step, total=total)
            except TypeError:
                # Older callers (CLI lambda stage, detail) — positional only.
                on_progress(stage, detail)

    size = max(1, int(batch_size))
    batches = [authored[i : i + size] for i in range(0, len(authored), size)]
    # Retrieve + model batches share one progress scale for the UI bar.
    n_retrieve = len(authored)
    n_model = len(batches)
    total_steps = n_retrieve + n_model

    if evidence is None:
        _progress(
            "retrieve",
            f"gathering evidence for {n_retrieve} authored beats",
            step=0,
            total=total_steps,
        )

        def _retrieve_progress(stage, detail, *, step=0, total=0):
            _progress(stage, detail, step=step, total=total_steps)

        evidence = retrieve_audit_evidence(
            authored,
            db_path=db_path,
            on_progress=_retrieve_progress,
        )

    raw_parts: list[dict] = []
    for i, batch in enumerate(batches, start=1):
        labels = {b.label for b in batch}
        batch_evidence = {k: evidence.get(k, []) for k in labels}
        names = ", ".join(f"A{b.label}" for b in batch)
        _progress(
            "model",
            f"batch {i}/{n_model} — {names}",
            step=n_retrieve + i,
            total=total_steps,
        )
        tip = max((b.end_msg_id for b in derived.beats), default=0)
        for hits in evidence.values():
            for h in hits:
                tip = max(tip, int(h.msg_id))
        messages = build_audit_messages(
            batch, derived, batch_evidence, log_tip=tip
        )
        need = ", ".join(f"A{b.label}" for b in batch)
        raw: dict | None = None
        last_dump = ""
        for attempt in range(max(1, attempts)):
            call_messages = messages
            if attempt > 0:
                call_messages = list(messages) + [{
                    "role": "user",
                    "content": (
                        f"Your previous reply omitted beats. Return one JSON object "
                        f"with a beats array that includes EVERY label in this batch "
                        f"exactly once: {need}."
                    ),
                }]

            def _call(call_messages=call_messages):
                return llm.chat(
                    call_messages, temperature=temperature, max_tokens=1800,
                )

            try:
                candidate = _coerce_audit_raw(
                    jsonish.request_object(
                        _call,
                        what=f"spine audit batch {i}/{len(batches)}",
                        slug=f"spine-audit-b{i}",
                        attempts=1,
                    )
                )
            except jsonish.UnparsableReply as exc:
                last_dump = str(exc)
                continue
            last_dump = json.dumps(candidate, ensure_ascii=False)
            judged_labels = {
                _norm_audit_label(r.get("authored"))
                for r in (candidate.get("beats") or [])
                if isinstance(r, dict)
            }
            # Legacy batches name labels via alignment/absent instead of beats[].
            if not judged_labels:
                for row in candidate.get("alignment") or []:
                    if isinstance(row, dict):
                        judged_labels.add(_norm_audit_label(row.get("authored")))
                for item in candidate.get("absent") or []:
                    judged_labels.add(_norm_audit_label(item))
            if labels <= judged_labels:
                raw = candidate
                break
        if raw is None:
            path = jsonish.save_raw_reply(last_dump, f"spine-audit-empty-b{i}")
            where = f" Raw reply saved at {path}." if path else ""
            raise jsonish.UnparsableReply(
                f"spine audit batch {i}/{len(batches)} did not return a verdict "
                f"for every beat ({need}); nothing was changed.{where}"
            )
        raw_parts.append(raw)

    tip = max((b.end_msg_id for b in derived.beats), default=0)
    for hits in evidence.values():
        for h in hits:
            tip = max(tip, int(h.msg_id))
    result = normalize_audit_result(
        _merge_raw_batches(raw_parts),
        authored=authored,
        derived=derived,
        evidence=evidence,
        log_tip=tip,
    )
    judged = [
        r for r in result["beats"]
        if r.get("note") != "no verdict returned"
    ]
    if not judged:
        raise jsonish.UnparsableReply(
            "spine audit produced no usable per-beat verdicts; nothing was changed"
        )
    return {
        "authored": authored,
        "result": result,
        "evidence": {
            label: [
                {
                    "msg_id": h.msg_id,
                    "speaker": h.speaker,
                    "preview": h.preview,
                    "score": h.score,
                }
                for h in hits
            ]
            for label, hits in evidence.items()
        },
    }
