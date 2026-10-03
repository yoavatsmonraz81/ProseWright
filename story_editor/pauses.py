"""Find the natural pauses in the story - the moments the characters retire and a
day closes - the gaps a scene break or a time skip naturally falls into.

Structure validates: most narrator turns open with a header like
    [ 🕰️ Time 6:42 AM | 🗓️ Thursday, October 4, 1792 | 📍 ... ]
We read the calendar date out of that header; when it advances, the previous day
has ended and we have an insertion point. Retire/sleep cues in the closing prose
sharpen the signal but are not required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .loader import Log, Message

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}

_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*([AaPp][Mm])")
_LOC_RE = re.compile(r"\U0001F4CD\s*([^|\]]+)")
_HEADER_RE = re.compile(r"^\s*\[(.*?)\]", re.DOTALL)

_RETIRE_CUES = (
    "sleep", "slept", "asleep", "fell asleep", "drifted off", "retire", "retired",
    "to bed", "into bed", "goodnight", "good night", "closed her eyes",
    "closed their eyes", "candle out", "blew out", "dark of the room",
)

# Header-less bridge lines that narrate the next morning belong to the new day;
# the night pause sits *before* them.
_MORNING_CUES = (
    "daybreak", "dawn", "morning", "first light", "sunrise", "awaken", "awakes",
    "awoke", "wakes", "woke", "the smell of",
)


@dataclass
class Header:
    date: tuple[int, int, int] | None  # (year, month, day), sortable
    date_str: str | None
    time: str | None
    location: str | None


def parse_header(text: str) -> Header:
    """Read the time/date/location header from the start of a message, if present."""
    block = text[:400]
    m = _HEADER_RE.match(text)
    if m:
        block = m.group(1)

    date = None
    date_str = None
    dm = _DATE_RE.search(block)
    if dm:
        month = MONTHS[dm.group(1).lower()]
        day = int(dm.group(2))
        year = int(dm.group(3))
        date = (year, month, day)
        date_str = f"{dm.group(1).title()} {day}, {year}"

    tm = _TIME_RE.search(block)
    time = f"{tm.group(1)} {tm.group(2).upper()}" if tm else None

    lm = _LOC_RE.search(block)
    location = lm.group(1).strip() if lm else None

    return Header(date=date, date_str=date_str, time=time, location=location)


def _has_cue(msg: Message, cues: tuple[str, ...]) -> bool:
    low = msg.text.lower()
    return any(cue in low for cue in cues)


def _has_retire_cue(*messages: Message) -> bool:
    return any(_has_cue(m, _RETIRE_CUES) for m in messages)


@dataclass
class Pause:
    after_msg_id: int  # insert the interlude AFTER this message
    day_closed: str | None
    day_opens: str | None
    close_preview: str
    open_preview: str
    retire_cue: bool

    def label(self) -> str:
        span = f"{self.day_closed or '?'} -> {self.day_opens or '?'}"
        cue = " [retire cue]" if self.retire_cue else ""
        return f"after {self.after_msg_id:>4}  {span}{cue}"


def find_pauses(log: Log) -> list[Pause]:
    headers = [parse_header(m.text) for m in log.messages]

    pauses: list[Pause] = []
    last_date: tuple[int, int, int] | None = None
    last_date_str: str | None = None

    for i, hdr in enumerate(headers):
        if hdr.date is None:
            continue
        if last_date is not None and hdr.date > last_date:
            # Pull the slot back past any header-less "next morning" bridge lines so
            # the night pause sits before the new day's awakening, not after it.
            after = i - 1
            while (
                after > 0
                and headers[after].date is None
                and _has_cue(log.messages[after], _MORNING_CUES)
            ):
                after -= 1
            if after < 0:
                last_date, last_date_str = hdr.date, hdr.date_str
                continue
            close_msgs = log.messages[max(0, after - 1) : after + 1]
            pauses.append(
                Pause(
                    after_msg_id=after,
                    day_closed=last_date_str,
                    day_opens=hdr.date_str,
                    close_preview=log.messages[after].preview(),
                    open_preview=log.messages[i].preview(),
                    retire_cue=_has_retire_cue(*close_msgs),
                )
            )
        last_date, last_date_str = hdr.date, hdr.date_str

    return pauses
