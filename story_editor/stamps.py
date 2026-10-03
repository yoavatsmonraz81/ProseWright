"""Chronicle / location stamps: infer the grind, propose the missing headers.

Most CHAR turns already open with
``[ 🕰️ time | 🗓️ date | 📍 location ]``. The player's turns usually do not. The ones
in between inherit the last clock the log stated, which is easy to see and
tedious to type. This module walks the log once, carries date / time /
location forward, and writes a sidecar keyed by uid.

A second step may propose prepending a completed header on CHAR (and
interlude) turns that lack one. User turns stay header-less unless they
already carry a partial bracket. Nothing is written to the log until the
author accepts the EditSet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, layers as layers_mod, loader, pauses
from .transform import (
    Edit,
    EditSet,
    SpanResult,
    containment_flags,
    resolve_span,
    split_header,
    write_pending_edits,
)


HEADER, INHERITED, INFERRED, PROPOSED = (
    "header", "inherited", "inferred", "proposed",
)

_LEAD = pauses._HEADER_RE
_DATE_RE = pauses._DATE_RE
_TIME_RE = pauses._TIME_RE
_LOC_RE = pauses._LOC_RE
_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°\s*[CF]")


@dataclass
class Stamp:
    uid: str
    msg_id: int
    speaker: str
    role: str
    date: tuple[int, int, int] | None = None
    date_str: str | None = None
    time: str | None = None
    location: str | None = None
    temperature: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
    has_lead: bool = False
    will_propose: bool = False

    @property
    def complete(self) -> bool:
        return bool(self.date and self.time and self.location)

    @property
    def source(self) -> str:
        vals = [self.sources.get(k) for k in ("date", "time", "location") if self.sources.get(k)]
        if not vals:
            return ""
        if all(v == HEADER for v in vals):
            return HEADER
        if INFERRED in vals:
            return INFERRED
        if INHERITED in vals:
            return INHERITED
        return vals[0]

    def to_json(self) -> dict:
        return {
            "uid": self.uid,
            "msg_id": self.msg_id,
            "speaker": self.speaker,
            "role": self.role,
            "date": self.date_str,
            "date_tuple": list(self.date) if self.date else None,
            "time": self.time,
            "location": self.location,
            "temperature": self.temperature,
            "source": self.source,
            "fields": dict(self.sources),
            "has_lead": self.has_lead,
            "complete": self.complete,
            "will_propose": self.will_propose,
        }


@dataclass
class StampReport:
    locator: str
    msg_from: int
    msg_to: int
    stamps: list[Stamp] = field(default_factory=list)

    def in_span(self) -> list[Stamp]:
        return [s for s in self.stamps if self.msg_from <= s.msg_id <= self.msg_to]

    def to_json(self) -> dict:
        rows = self.in_span()
        propose = [s for s in rows if s.will_propose]
        return {
            "locator": self.locator,
            "from": self.msg_from,
            "to": self.msg_to,
            "count": len(rows),
            "complete": sum(1 for s in rows if s.complete),
            "header": sum(1 for s in rows if s.source == HEADER),
            "inherited": sum(1 for s in rows if s.source == INHERITED),
            "inferred": sum(1 for s in rows if s.source == INFERRED),
            "missing": sum(1 for s in rows if not s.complete),
            "propose_count": len(propose),
            "propose": [s.to_json() for s in propose],
        }


def weekday_name(parts: tuple[int, int, int]) -> str:
    return date(parts[0], parts[1], parts[2]).strftime("%A")


def format_date(parts: tuple[int, int, int], date_str: str | None = None) -> str:
    """``Sunday, August 22, 1841`` — weekday computed, month from the tuple."""
    calendar = date_str or f"{date(parts[0], parts[1], parts[2]).strftime('%B')} {parts[2]}, {parts[0]}"
    if calendar.split(",")[0].strip() in {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    }:
        return calendar
    return f"{weekday_name(parts)}, {calendar}"


def format_header(stamp: Stamp) -> str:
    """Opening-style bracket. Temperature only if we already have it."""
    if not (stamp.time and stamp.date and stamp.location):
        raise ValueError("cannot format an incomplete stamp")
    calendar = format_date(stamp.date, stamp.date_str)
    bits = [f"🕰️ {stamp.time}", f"🗓️ {calendar}", f"📍 {stamp.location}"]
    if stamp.temperature:
        bits.append(stamp.temperature)
    return "[ " + " | ".join(bits) + " ]"


def _one_line(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().splitlines()[0].strip() or None


def _has_lead(text: str) -> bool:
    return bool(_LEAD.match(text))


def _is_interlude(msg: loader.Message) -> bool:
    if msg.is_system:
        return True
    extra = msg.raw.get("extra") or {}
    se = extra.get("story_editor") if isinstance(extra, dict) else None
    if isinstance(se, dict) and se.get("interlude"):
        return True
    return False


def _should_propose(msg: loader.Message, stamp: Stamp) -> bool:
    """CHAR / interlude turns missing a full header, once we have all three."""
    if not stamp.complete:
        return False
    hdr = pauses.parse_header(msg.text)
    if hdr.date and hdr.time and hdr.location:
        return False
    if msg.is_user and not _is_interlude(msg) and not stamp.has_lead:
        return False
    return True


def _read_body_hints(body: str) -> tuple[
    tuple[int, int, int] | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    """Date / time / 📍 / temperature found in the prose, not the lead header."""
    # Narrative dialogue often mentions a different date (a memory, future
    # vision, or document). Only an explicit calendar marker is a clock hint.
    dated_line = next((line for line in body.splitlines()
                       if "🗓️" in line and _DATE_RE.search(line)), "")
    dm = _DATE_RE.search(dated_line)
    date_t = None
    date_s = None
    if dm:
        month = pauses.MONTHS[dm.group(1).lower()]
        day = int(dm.group(2))
        year = int(dm.group(3))
        date_t = (year, month, day)
        date_s = f"{dm.group(1).title()} {day}, {year}"
    timed_line = next((line for line in body.splitlines()
                       if "🕰️" in line and _TIME_RE.search(line)), "")
    tm = _TIME_RE.search(timed_line)
    time = f"{tm.group(1)} {tm.group(2).upper()}" if tm else None
    lm = _LOC_RE.search(body)
    location = _one_line(lm.group(1)) if lm else None
    temp_m = _TEMP_RE.search(body)
    temperature = f"{temp_m.group(1)}°{('C' if 'c' in temp_m.group(0).lower() else 'F')}" if temp_m else None
    return date_t, date_s, time, location, temperature


def infer_log(log: loader.Log) -> list[Stamp]:
    """Walk the whole log. Carry the last known clock forward."""
    last_date: tuple[int, int, int] | None = None
    last_date_str: str | None = None
    last_time: str | None = None
    last_location: str | None = None
    last_temp: str | None = None
    out: list[Stamp] = []

    for msg in log.messages:
        has_lead = _has_lead(msg.text)
        hdr = pauses.parse_header(msg.text) if has_lead else pauses.Header(
            date=None, date_str=None, time=None, location=None,
        )
        _, body = split_header(msg.text)
        inf_date, inf_date_str, inf_time, inf_loc, inf_temp = _read_body_hints(body)
        chronicle = (msg.raw.get("extra") or {}).get("story_editor") or {}
        chronicle = chronicle.get("chronicle") if isinstance(chronicle, dict) else None
        chron_date = None
        if isinstance(chronicle, dict) and chronicle.get("date"):
            try:
                parsed = date.fromisoformat(str(chronicle["date"]))
                chron_date = (parsed.year, parsed.month, parsed.day)
            except ValueError:
                pass
        memory_override = (
            chron_date is not None
            and isinstance(chronicle, dict)
            and chronicle.get("location_mode") == "memory"
            and chronicle.get("confidence") == "director_confirmed"
        )

        sources: dict[str, str] = {}

        if hdr.date and not memory_override:
            last_date, last_date_str = hdr.date, hdr.date_str
            sources["date"] = HEADER
            date_t, date_s = hdr.date, hdr.date_str
        elif chron_date and (memory_override or chron_date != last_date):
            last_date = chron_date
            last_date_str = f"{date(*chron_date).strftime('%B')} {chron_date[2]}, {chron_date[0]}"
            sources["date"] = INFERRED
            date_t, date_s = chron_date, last_date_str
        elif inf_date:
            last_date, last_date_str = inf_date, inf_date_str
            sources["date"] = INFERRED
            date_t, date_s = inf_date, inf_date_str
        elif last_date:
            sources["date"] = INHERITED
            date_t, date_s = last_date, last_date_str
        else:
            date_t, date_s = None, None

        if hdr.time:
            last_time = hdr.time
            sources["time"] = HEADER
            time = hdr.time
        elif inf_time:
            last_time = inf_time
            sources["time"] = INFERRED
            time = inf_time
        elif last_time:
            sources["time"] = INHERITED
            time = last_time
        else:
            time = None

        if hdr.location:
            location = _one_line(hdr.location)
            last_location = location
            sources["location"] = HEADER
        elif inf_loc:
            last_location = inf_loc
            sources["location"] = INFERRED
            location = inf_loc
        elif last_location:
            sources["location"] = INHERITED
            location = last_location
        else:
            location = None

        lead_block = _LEAD.match(msg.text)
        temp = None
        if lead_block:
            tm = _TEMP_RE.search(lead_block.group(1))
            if tm:
                unit = "C" if "c" in tm.group(0).lower() else "F"
                temp = f"{tm.group(1)}°{unit}"
                last_temp = temp
        if temp is None and inf_temp:
            temp = inf_temp
            last_temp = inf_temp
            sources["temperature"] = INFERRED
        elif temp is not None:
            sources["temperature"] = HEADER
        elif last_temp:
            temp = last_temp
            sources["temperature"] = INHERITED

        stamp = Stamp(
            uid=msg.uid or f"ord:{msg.msg_id}",
            msg_id=msg.msg_id,
            speaker=msg.speaker,
            role=msg.role(),
            date=date_t,
            date_str=date_s,
            time=time,
            location=location,
            temperature=temp,
            sources=sources,
            has_lead=has_lead,
        )
        stamp.will_propose = _should_propose(msg, stamp)
        out.append(stamp)
    return out


def scan_log(
    log_path: str | Path,
    *,
    msg_from: int | None = None,
    msg_to: int | None = None,
    persist: bool = True,
) -> StampReport:
    log = loader.load(log_path)
    span = resolve_span(log, msg_from=msg_from, msg_to=msg_to)
    stamps = infer_log(log)
    report = StampReport(
        locator=span.locator,
        msg_from=span.msg_from,
        msg_to=span.msg_to,
        stamps=stamps,
    )
    if persist:
        write_sidecar(log, stamps)
    return report


def write_sidecar(log: loader.Log, stamps: list[Stamp]) -> Path:
    path = Path(config.STAMPS)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "log": str(log.path.resolve()),
        "updated": datetime.now(timezone.utc).isoformat(),
        "count": len(stamps),
        "stamps": {s.uid: s.to_json() for s in stamps},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_sidecar() -> dict | None:
    path = Path(config.STAMPS)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _apply_header(text: str, header_line: str) -> str:
    """Prepend, or replace a leading bracket, leaving the body untouched."""
    m = _LEAD.match(text)
    if not m:
        rest = text.lstrip("\n")
        return header_line + "\n\n" + rest if rest else header_line + "\n"
    end = m.end()
    while end < len(text) and text[end] in " \t\r\n":
        end += 1
    body = text[end:]
    return header_line + "\n\n" + body if body else header_line + "\n"


def propose_span(
    log_path: str | Path,
    span: SpanResult,
    *,
    on_stream=None,
    persist: bool = True,
) -> EditSet:
    """Propose header inserts for CHAR turns in ``span`` that lack a full stamp."""
    layers_mod.check_runs("stamps", layers_mod.LOG)
    log = loader.load(log_path)
    stamps = infer_log(log)
    by_id = {s.msg_id: s for s in stamps}
    if persist:
        write_sidecar(log, stamps)

    edits: list[Edit] = []
    scoped = [m for m in span.messages if span.msg_from <= m.msg_id <= span.msg_to]
    total = sum(1 for m in scoped if by_id.get(m.msg_id) and by_id[m.msg_id].will_propose)
    done = 0
    for msg in scoped:
        stamp = by_id.get(msg.msg_id)
        if stamp is None or not stamp.will_propose:
            continue
        done += 1
        if on_stream is not None:
            on_stream({
                "kind": "phase",
                "text": f"stamp msg {msg.msg_id} ({done}/{total})",
            })
        try:
            line = format_header(stamp)
        except ValueError:
            continue
        after = _apply_header(msg.text, line)
        if after == msg.text:
            continue
        stamp.sources = {**stamp.sources, "header": PROPOSED}
        edits.append(Edit(
            msg_id=msg.msg_id,
            speaker=msg.speaker,
            before=msg.text,
            after=after,
            flags=containment_flags(msg.text, after, msg.speaker),
        ))

    edit_set = EditSet(
        log=str(Path(log_path).resolve()),
        operator="stamps",
        note=f"stamps: {len(edits)} header(s) proposed",
        locator=span.locator,
        edits=edits,
        meta={
            "propose_count": len(edits),
            "complete": sum(1 for s in stamps if s.complete),
            "sidecar": str(Path(config.STAMPS)),
        },
    )
    write_pending_edits(edit_set)
    return edit_set


def propose_log(
    log_path: str | Path,
    *,
    msg_from: int | None = None,
    msg_to: int | None = None,
    on_stream=None,
) -> EditSet:
    log = loader.load(log_path)
    span = resolve_span(log, msg_from=msg_from, msg_to=msg_to)
    return propose_span(log_path, span, on_stream=on_stream)
