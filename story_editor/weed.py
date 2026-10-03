"""Pull editorial watchlist phrases out of selected authorship sources.

The phrase list contains patterns commonly associated with generated prose, but
a match is not evidence of AI authorship. The project explicitly selects which
authorship sources are in scope. This operator then rewrites only the matched
sentence and proposes the result as an EditSet. Nothing is written until the
author accepts it in the fork pane.

The list lives in ``canon/weeds.json`` so a new reflux-word can be added
without a code change. Scan is cheap and lexical. A rewrite is only asked for
when a mechanical pull would break the sentence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import config, layers as layers_mod, loader, text as text_mod
from .transform import (
    Edit,
    EditSet,
    SpanResult,
    _clean_model_body,
    _llm_text,
    containment_flags,
    resolve_span,
    split_header,
    write_pending_edits,
)


WEED_TEMPERATURE = 0.25
RECIPES = ("drop_adj", "drop_lead_in", "drop_span", "replace", "rewrite")

_ABBREV = frozenset({
    "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "vol", "no",
    "prof", "rev", "hon", "gen", "col", "lt", "sgt",
})
_SENT_END = re.compile(r"([.!?…]+)(?:[\"'”’])?(?=\s|\Z)")
_DETAILS = text_mod._META_BLOCK_RE
_BROKEN_TAIL = re.compile(
    r"\b(?:is|was|were|are|be|been|being)\s*[.!?…\"'”’]*\s*$",
    re.IGNORECASE,
)

_REWRITE_SYSTEM = (
    "You are pulling an editorial watchlist phrase out of one sentence of a "
    "roleplay. The project has selected this authorship source "
    "for generated-prose cleanup; preserve the speaker's actual voice.\n\n"
    "Return ONLY the replacement sentence.\n"
    "Keep the same mouth, the same facts, and roughly the same length.\n"
    "Do not add a different machine phrase "
    "(no load-bearing, delve, unpack, tapestry, at its core, crucial, "
    "worth noting, simply put, in other words).\n"
    "Do not explain. Do not wrap the answer in quotes unless the original "
    "sentence was itself a quoted line.\n"
    "If the sentence contains HTML tags, keep them around the same words."
)


@dataclass(frozen=True)
class Weed:
    id: str
    pattern: str
    family: str
    recipe: str
    replace: str = ""
    replace_map: dict[str, str] = field(default_factory=dict)

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass
class Hit:
    msg_id: int
    speaker: str
    sentence: str
    phrase_id: str
    snippet: str
    start: int
    end: int
    source_id: str = ""

    def to_json(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "speaker": self.speaker,
            "sentence": self.sentence,
            "phrase_id": self.phrase_id,
            "snippet": self.snippet,
            "start": self.start,
            "end": self.end,
            "source_id": self.source_id,
        }


@dataclass
class WeedReport:
    locator: str
    msg_from: int
    msg_to: int
    hits: list[Hit] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    selected_sources: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "locator": self.locator,
            "from": self.msg_from,
            "to": self.msg_to,
            "hits": [h.to_json() for h in self.hits],
            "phrases": list(self.phrases),
            "hit_count": len(self.hits),
            "message_count": len({h.msg_id for h in self.hits}),
            "selected_sources": list(self.selected_sources),
        }


def source_key(message: loader.Message) -> str:
    """Authorship/source identity, deliberately independent of story voice."""
    kind = "human" if message.is_user else "system" if message.is_system else "model"
    return f"{kind}:{message.speaker}"


def source_catalog(log: loader.Log) -> list[dict]:
    counts: dict[str, int] = {}
    for message in log.messages:
        key = source_key(message)
        counts[key] = counts.get(key, 0) + 1
    selected = set(load_selected_sources(log))
    rows: list[dict] = []
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        kind, speaker = key.split(":", 1)
        rows.append({
            "id": key,
            "kind": kind,
            "speaker": speaker,
            "label": f"{speaker} ({kind})",
            "message_count": count,
            "selected": key in selected,
        })
    return rows


def _scope_path() -> Path:
    return Path(getattr(config, "WEED_SCOPE", config.CANON_DIR / "weed_scope.json"))


def load_selected_sources(log: loader.Log) -> list[str]:
    available = {source_key(message) for message in log.messages}
    path = _scope_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    # A copied project must not inherit another log's authorship decisions.
    if config.same_log(data.get("log"), log.path):
        chosen = [str(key) for key in data.get("selected_sources") or []]
        return [key for key in chosen if key in available]
    # Safe first-run default: generated/system sources, never presumed-human text.
    return sorted(key for key in available if not key.startswith("human:"))


def save_selected_sources(log: loader.Log, selected: list[str]) -> list[str]:
    available = {source_key(message) for message in log.messages}
    chosen = sorted({str(key) for key in selected if str(key) in available})
    path = _scope_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "story-editor/weed-scope@1",
        "log": str(log.path.resolve()),
        "selected_sources": chosen,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return chosen


def _weeds_path() -> Path:
    return Path(getattr(config, "WEEDS", config.CANON_DIR / "weeds.json"))


@lru_cache(maxsize=4)
def _load_weeds(path_str: str, mtime: float) -> tuple[Weed, ...]:
    path = Path(path_str)
    if not path.exists():
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("phrases") if isinstance(data, dict) else data
    out: list[Weed] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        recipe = str(raw.get("recipe") or "rewrite")
        if recipe not in RECIPES:
            recipe = "rewrite"
        mapping = raw.get("replace_map") or {}
        out.append(Weed(
            id=str(raw.get("id") or raw.get("pattern") or "").strip(),
            pattern=str(raw.get("pattern") or "").strip(),
            family=str(raw.get("family") or "machine"),
            recipe=recipe,
            replace=str(raw.get("replace") or ""),
            replace_map={str(k).lower(): str(v) for k, v in mapping.items()}
            if isinstance(mapping, dict) else {},
        ))
    return tuple(w for w in out if w.id and w.pattern)


def load_weeds() -> list[Weed]:
    path = _weeds_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return list(_load_weeds(str(path), mtime))


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """Cover ``text`` with (start, end, sentence) spans. ``end`` stops at the
    closer, so a following blank line is not part of the sentence and a pull
    cannot swallow the paragraph break after it."""
    if not text:
        return []
    parts: list[tuple[int, int, str]] = []
    start = 0
    for match in _SENT_END.finditer(text):
        word = _last_word(text[start:match.start()])
        if word.lower() in _ABBREV:
            continue
        if len(word) == 1 and word.isalpha():
            continue
        close = match.end()
        end = close
        while end < len(text) and text[end] in " \t\r\n":
            end += 1
        parts.append((start, close, text[start:close]))
        start = end
    if start < len(text):
        parts.append((start, len(text), text[start:]))
    return parts


def _core_and_trail(text: str) -> tuple[str, str]:
    """Split trailing whitespace off so a rewrite can be stitched back without
    eating the blank line that followed the sentence."""
    stripped = text.rstrip(" \t\r\n")
    return stripped, text[len(stripped):]


def _last_word(chunk: str) -> str:
    found = re.search(r"([A-Za-z]+)\s*$", chunk)
    return found.group(1) if found else ""


def _details_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _DETAILS.finditer(text)]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(not (end <= lo or start >= hi) for lo, hi in spans)


def _snippet(match: re.Match[str]) -> str:
    return match.group(0)


def scan_text(text: str, weeds: list[Weed] | None = None) -> list[tuple[int, int, str, Weed, str]]:
    """Hits inside ``text`` as (sent_start, sent_end, sentence, weed, snippet).
    Sentences that live inside a ``<details>`` block are skipped — those are
    scaffolding, not mouth."""
    weeds = weeds if weeds is not None else load_weeds()
    hidden = _details_spans(text)
    found: list[tuple[int, int, str, Weed, str]] = []
    for start, end, sentence in split_sentences(text):
        if _overlaps(start, end, hidden):
            continue
        for weed in weeds:
            for match in weed.compiled().finditer(sentence):
                found.append((start, end, sentence, weed, _snippet(match)))
    return found


def scan_span(
    span: SpanResult,
    *,
    source_keys: list[str] | set[str] | None = None,
) -> WeedReport:
    weeds = load_weeds()
    hits: list[Hit] = []
    phrases: list[str] = []
    seen_phrase: set[str] = set()
    selected = set(source_keys) if source_keys is not None else None
    for message in span.messages:
        if selected is not None and source_key(message) not in selected:
            continue
        _header, body = split_header(message.text)
        for start, end, sentence, weed, snippet in scan_text(body, weeds):
            hits.append(Hit(
                msg_id=message.msg_id,
                speaker=message.speaker,
                sentence=sentence.strip(),
                phrase_id=weed.id,
                snippet=snippet,
                start=start,
                end=end,
                source_id=source_key(message),
            ))
            if weed.id not in seen_phrase:
                seen_phrase.add(weed.id)
                phrases.append(weed.id)
    return WeedReport(
        locator=span.locator,
        msg_from=span.msg_from,
        msg_to=span.msg_to,
        hits=hits,
        phrases=phrases,
        selected_sources=sorted(selected or []),
    )


def scan_log(
    log_path: str | Path,
    *,
    msg_from: int | None = None,
    msg_to: int | None = None,
    speaker: str | None = None,
    source_keys: list[str] | None = None,
) -> WeedReport:
    log = loader.load(log_path)
    span = resolve_span(
        log, msg_from=msg_from, msg_to=msg_to, speaker=speaker,
    )
    selected = source_keys if source_keys is not None else load_selected_sources(log)
    return scan_span(span, source_keys=selected)


def _tidy_spaces(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.;:!?…])", r"\1", text)
    text = re.sub(r"\( ", "(", text)
    text = re.sub(r" \)", ")", text)
    return text


def _fix_cap(out: str, original: str) -> str:
    stripped = out.lstrip()
    lead_ws = out[: len(out) - len(stripped)] if stripped else out
    lead = original.lstrip()
    if stripped and lead[:1].isupper() and stripped[:1].islower():
        return lead_ws + stripped[0].upper() + stripped[1:]
    return out


def _too_broken(out: str, original: str) -> bool:
    s = out.strip()
    if len(s) < 3:
        return True
    words = re.findall(r"[A-Za-z']+", s)
    if len(words) < 3 and len(re.findall(r"[A-Za-z']+", original)) >= 6:
        return True
    if _BROKEN_TAIL.search(s) and len(words) < 7:
        return True
    return False


def _case_replace(match: re.Match[str], replacement: str) -> str:
    word = match.group(0)
    if word.isupper():
        return replacement.upper()
    if word[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def still_has(sentence: str, weed: Weed) -> bool:
    return bool(weed.compiled().search(sentence))


def mechanical_pull(sentence: str, weed: Weed) -> str | None:
    """Try a cheap pull. None means the sentence needs a rewrite, not that
    the phrase is gone."""
    if weed.recipe == "rewrite":
        return None
    flags = re.IGNORECASE
    if weed.recipe == "drop_adj":
        out = re.sub(rf"\b(?:{weed.pattern})\s+", "", sentence, flags=flags)
    elif weed.recipe == "drop_lead_in":
        out = re.sub(rf"^\s*(?:{weed.pattern})\s*,?\s*", "", sentence, flags=flags)
        if out == sentence:
            out = re.sub(rf"\b(?:{weed.pattern})\s*,?\s*", "", sentence, flags=flags)
        out = _fix_cap(out, sentence)
    elif weed.recipe == "drop_span":
        out = re.sub(rf"\s*,?\s*\b(?:{weed.pattern})\b\s*,?", " ", sentence, flags=flags)
        out = _fix_cap(out, sentence)
    elif weed.recipe == "replace":
        if weed.replace_map:
            keys = sorted(weed.replace_map, key=len, reverse=True)
            pattern = r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b"

            def repl(match: re.Match[str]) -> str:
                mapped = weed.replace_map.get(match.group(0).lower(), match.group(0))
                return _case_replace(match, mapped)

            out = re.sub(pattern, repl, sentence, flags=flags)
        elif weed.replace:
            def repl(match: re.Match[str]) -> str:
                return _case_replace(match, weed.replace)

            out = re.sub(rf"\b(?:{weed.pattern})\b", repl, sentence, flags=flags)
        else:
            return None
    else:
        return None
    out = _tidy_spaces(out)
    if still_has(out, weed) or _too_broken(out, sentence):
        return None
    return out


def _rewrite_sentence(
    sentence: str,
    weeds: list[Weed],
    *,
    speaker: str,
    on_stream=None,
) -> str:
    names = ", ".join(sorted({w.id for w in weeds}))
    messages = [
        {"role": "system", "content": _REWRITE_SYSTEM},
        {"role": "user", "content": (
            f"SPEAKER: {speaker}\n"
            f"PHRASES TO REMOVE: {names}\n\n"
            f"SENTENCE:\n{sentence}"
        )},
    ]
    raw = _llm_text(
        messages,
        task="weed",
        temperature=WEED_TEMPERATURE,
        max_tokens=max(2048, len(sentence) + 1024),
        on_stream=on_stream,
    )
    return _clean_model_body(raw, original=sentence)


def pull_sentence(
    sentence: str,
    weeds: list[Weed],
    *,
    speaker: str,
    on_stream=None,
) -> tuple[str, str]:
    """Return ``(new_sentence, method)``. Method is mechanical, rewritten,
    mixed, or untouched."""
    remaining = list(weeds)
    current = sentence
    used_mechanical = False
    for weed in list(remaining):
        pulled = mechanical_pull(current, weed)
        if pulled is None:
            continue
        current = pulled
        remaining = [w for w in remaining if still_has(current, w)]
        used_mechanical = True
    if not remaining:
        if current.strip() == sentence.strip():
            return sentence, "untouched"
        return current, "mechanical"
    if on_stream is not None:
        on_stream({"kind": "phase", "text": f"rewriting a sentence ({remaining[0].id})"})
    rewritten = _rewrite_sentence(current, remaining, speaker=speaker, on_stream=on_stream)
    if not rewritten:
        return current if used_mechanical else sentence, "mechanical" if used_mechanical else "untouched"
    leftover = [w for w in remaining if still_has(rewritten, w)]
    if leftover:
        # Last mechanical pass on whatever the model left standing.
        for weed in leftover:
            pulled = mechanical_pull(rewritten, weed)
            if pulled is not None:
                rewritten = pulled
    method = "mixed" if used_mechanical else "rewritten"
    return rewritten, method


def _hits_by_message(report: WeedReport) -> dict[int, list[Hit]]:
    grouped: dict[int, list[Hit]] = {}
    for hit in report.hits:
        grouped.setdefault(hit.msg_id, []).append(hit)
    return grouped


def apply_span(
    log_path: str | Path,
    span: SpanResult,
    *,
    on_stream=None,
    report: WeedReport | None = None,
    source_keys: list[str] | None = None,
) -> EditSet:
    """Rewrite infected sentences in ``span`` and write a pending EditSet."""
    layers_mod.check_runs("weed", layers_mod.LOG)
    weeds = {w.id: w for w in load_weeds()}
    if report is None:
        log = loader.load(log_path)
        selected = source_keys if source_keys is not None else load_selected_sources(log)
        report = scan_span(span, source_keys=selected)
    by_id = {m.msg_id: m for m in span.messages}
    edits: list[Edit] = []
    mechanical = 0
    rewritten = 0
    total = len(_hits_by_message(report))
    done = 0
    for msg_id, hits in _hits_by_message(report).items():
        target = by_id.get(msg_id)
        if target is None:
            continue
        done += 1
        if on_stream is not None:
            on_stream({
                "kind": "phase",
                "text": f"weeding msg {msg_id} ({done}/{total})",
            })
        header, body = split_header(target.text)
        # Unique sentence spans, last-to-first so offsets stay valid.
        spans = sorted({(h.start, h.end, h.sentence) for h in hits}, reverse=True)
        new_body = body
        methods: list[str] = []
        for start, end, _display in spans:
            original = body[start:end]
            core, trail = _core_and_trail(original)
            present = [weeds[h.phrase_id] for h in hits if h.start == start and h.phrase_id in weeds]
            if not present:
                continue
            pulled, method = pull_sentence(
                core, present, speaker=target.speaker, on_stream=on_stream,
            )
            methods.append(method)
            if pulled.strip() == core.strip():
                continue
            new_body = new_body[:start] + pulled.rstrip(" \t\r\n") + trail + new_body[end:]
        if new_body == body:
            continue
        if any(m in ("rewritten", "mixed") for m in methods):
            rewritten += 1
        elif methods:
            mechanical += 1
        after = header + new_body
        edits.append(Edit(
            msg_id=target.msg_id,
            speaker=target.speaker,
            before=target.text,
            after=after,
            flags=containment_flags(target.text, after, target.speaker),
        ))

    note = (
        f"pulled {', '.join(report.phrases) or 'nothing'} "
        f"from {len(report.hits)} sentence(s)"
    )
    edit_set = EditSet(
        log=str(Path(log_path).resolve()),
        operator="weed",
        note=note,
        locator=span.locator,
        edits=edits,
        meta={
            "phrases": report.phrases,
            "hit_count": len(report.hits),
            "mechanical": mechanical,
            "rewritten": rewritten,
            "selected_sources": report.selected_sources,
            "review_total": len(edits),
            "review_accepted": 0,
            "review_rejected": 0,
        },
    )
    write_pending_edits(edit_set)
    return edit_set


def apply_log(
    log_path: str | Path,
    *,
    msg_from: int | None = None,
    msg_to: int | None = None,
    speaker: str | None = None,
    on_stream=None,
) -> EditSet:
    log = loader.load(log_path)
    span = resolve_span(
        log, msg_from=msg_from, msg_to=msg_to, speaker=speaker,
    )
    return apply_span(log_path, span, on_stream=on_stream)
