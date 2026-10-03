"""Input validation - the prep run.

A NON-DESTRUCTIVE check over a chat .jsonl, run before the structural pipeline
(index build, scene segmentation, beat derivation) leans on its headers. It
changes nothing; it reports. The whole structural backbone keys off the
`[ time | date | location ]` header, which is an authored convention of the
CHAR turns - the user's turns usually carry none - so this asymmetry, plus the odd
malformed/buried/typo'd header, can silently degrade scenes and pauses. The
doctor surfaces those before they bite.

Findings are graded:
  RISK  - will silently corrupt structural inference (a header we can't read, a
          backward date jump). Fails the run (nonzero exit) so it can gate a
          pipeline.
  WARN  - probably fine, worth a human glance (location spelling drift, a partial
          or buried header).
  INFO  - expected/observational (the user carries no headers; N posts have swipes).

If normalisation is ever wanted, these findings are the proposal for a separate,
opt-in, copy-based pass - never an in-place rewrite.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import loader, pauses

RISK, WARN, INFO = "RISK", "WARN", "INFO"
_SEVERITY_RANK = {RISK: 0, WARN: 1, INFO: 2}

# A "header" is a leading bracket carrying a calendar date, a clock time, and a
# 📍 location. We reuse the pause-finder's parser for the lead bracket and these
# loose patterns to detect headers that are malformed or buried mid-post.
_LEAD_BRACKET = re.compile(r"^\s*\[(.*?)\]", re.DOTALL)
_ANY_DATE = pauses._DATE_RE
_ANY_LOC = pauses._LOC_RE


@dataclass
class Finding:
    severity: str
    code: str
    title: str
    detail: str = ""
    msg_ids: list[int] = field(default_factory=list)


@dataclass
class Report:
    log_path: str
    n_messages: int
    findings: list[Finding] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def counts(self) -> dict[str, int]:
        c = {RISK: 0, WARN: 0, INFO: 0}
        for f in self.findings:
            c[f.severity] += 1
        return c

    def worst(self) -> str | None:
        if not self.findings:
            return None
        return min((f.severity for f in self.findings), key=lambda s: _SEVERITY_RANK[s])

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: _SEVERITY_RANK[f.severity])


def _is_full_header(h: pauses.Header) -> bool:
    return bool(h.date and h.time and h.location)


def _chronicle(m: loader.Message) -> dict:
    extra = m.raw.get("extra") or {}
    story_editor = extra.get("story_editor") or {}
    chronicle = story_editor.get("chronicle") or {}
    return chronicle if isinstance(chronicle, dict) else {}


def _chronicle_complete(m: loader.Message) -> bool:
    c = _chronicle(m)
    # ``unknown`` is an explicit temporal finding, not missing analysis.  The
    # internal/outside-time passages may also omit a narrative date while still
    # carrying the physical date from which the passage is entered.
    has_date = bool(c.get("date") or c.get("physical_anchor_date"))
    has_time = bool(
        c.get("time") or c.get("time_precision") in {"unknown", "not_applicable"}
    )
    return bool(has_date and has_time and c.get("location"))


def _iso_date(value: object) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


# --------------------------------------------------------------------------- #
# Individual checks. Each appends zero or more Findings to the report.
# --------------------------------------------------------------------------- #

def _check_line_integrity(path: Path, report: Report) -> loader.Log | None:
    """Raw-line pass: catch JSON errors and a missing metadata line before the
    lenient loader papers over them. Returns the parsed Log, or None if the file
    can't be parsed at all."""
    bad: list[int] = []
    first_payload_has_mes = None
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                bad.append(i)
                continue
            if first_payload_has_mes is None:
                first_payload_has_mes = "mes" in obj
    if bad:
        report.add(Finding(
            RISK, "json-invalid", f"{len(bad)} line(s) are not valid JSON",
            detail=f"file lines (1-based): {[b + 1 for b in bad][:20]}",
        ))
    if first_payload_has_mes:
        report.add(Finding(
            WARN, "no-metadata-line",
            "first line looks like a message, not a chat-metadata object",
            detail="SillyTavern logs open with a metadata line; its absence may "
                   "shift how the file is read.",
        ))
    try:
        return loader.load(path)
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _check_required_fields(log: loader.Log, report: Report) -> None:
    no_name = [m.msg_id for m in log.messages if m.speaker in ("?", "")]
    empty = [m.msg_id for m in log.messages if not m.text.strip()]
    both = [m.msg_id for m in log.messages if m.is_user and m.is_system]
    if no_name:
        report.add(Finding(
            RISK, "missing-name", f"{len(no_name)} message(s) have no speaker name",
            msg_ids=no_name,
        ))
    if empty:
        report.add(Finding(
            WARN, "empty-text", f"{len(empty)} message(s) have empty text",
            msg_ids=empty,
        ))
    if both:
        report.add(Finding(
            WARN, "user-and-system", f"{len(both)} message(s) are both user and system",
            msg_ids=both,
        ))


def _check_header_coverage(log: loader.Log, report: Report) -> None:
    by_role = defaultdict(lambda: [0, 0])  # [full, missing]
    for m in log.messages:
        h = pauses.parse_header(m.text)
        by_role[m.role()][0 if _is_full_header(h) else 1] += 1
    detail = "; ".join(
        f"{role}: {full}/{full + miss} headed" for role, (full, miss) in by_role.items()
    )
    report.add(Finding(
        INFO, "header-coverage", "header coverage by role", detail=detail,
    ))
    semantic = [m.msg_id for m in log.messages if _chronicle_complete(m)]
    report.add(Finding(
        INFO,
        "chronicle-coverage",
        f"semantic chronicle covers {len(semantic)}/{len(log)} messages",
        detail=(
            "chronicle metadata is authoritative for chronology-aware segmentation"
            if len(semantic) == len(log)
            else "messages without complete chronicle metadata still depend on legacy headers"
        ),
        msg_ids=[m.msg_id for m in log.messages if not _chronicle_complete(m)],
    ))
    # A user turn that DOES carry a full header is unusual and WILL be used for
    # segmentation - worth a glance.
    user_headed = [
        m.msg_id for m in log.messages
        if m.role() == "user" and _is_full_header(pauses.parse_header(m.text))
    ]
    if user_headed:
        report.add(Finding(
            INFO, "user-headers",
            f"{len(user_headed)} user turn(s) carry a full header "
            "(these will drive segmentation like char headers)",
            msg_ids=user_headed,
        ))


def _check_malformed_headers(log: loader.Log, report: Report) -> None:
    """A leading bracket that *looks* like a header but doesn't fully parse - the
    silent day/scene miss the prep run exists to catch."""
    unparsed: list[int] = []
    partial: list[int] = []
    for m in log.messages:
        if not _LEAD_BRACKET.match(m.text):
            continue
        h = pauses.parse_header(m.text)
        if _is_full_header(h):
            continue
        if h.date or h.time or h.location:
            partial.append(m.msg_id)
        else:
            unparsed.append(m.msg_id)
    if unparsed:
        report.add(Finding(
            RISK, "header-unreadable",
            f"{len(unparsed)} message(s) lead with a bracket but no readable "
            "date/time/location",
            detail="if these were meant to be headers, the day/scene they mark "
                   "is being missed.",
            msg_ids=unparsed,
        ))
    if partial:
        resolved = [mid for mid in partial if _chronicle_complete(log.get(mid))]
        unresolved = [mid for mid in partial if mid not in set(resolved)]
        if resolved:
            report.add(Finding(
                INFO, "legacy-header-partial-resolved",
                f"{len(resolved)} partial legacy header(s) are covered by complete chronicle metadata",
                detail="no structural action required; the legacy header is retained as source prose.",
                msg_ids=resolved,
            ))
        if not unresolved:
            return
        report.add(Finding(
            WARN, "header-partial",
            f"{len(unresolved)} header(s) parsed only partially (missing date, time, "
            "or location)",
            msg_ids=unresolved,
        ))


def _check_buried_headers(log: loader.Log, report: Report) -> None:
    """A header sitting mid-post instead of at the start - `parse_header` only
    reads the lead bracket, so this is missed."""
    buried: list[int] = []
    for m in log.messages:
        h = pauses.parse_header(m.text)
        if _is_full_header(h):
            continue
        # No lead header, but the body contains both a date and a 📍 location.
        if _ANY_DATE.search(m.text) and _ANY_LOC.search(m.text):
            buried.append(m.msg_id)
    if buried:
        resolved = [
            mid for mid in buried
            if _chronicle_complete(log.get(mid))
            and bool(_chronicle(log.get(mid)).get("subsegments"))
        ]
        unresolved = [mid for mid in buried if mid not in set(resolved)]
        if resolved:
            report.add(Finding(
                INFO, "buried-header-resolved",
                f"{len(resolved)} buried header(s) are represented as chronicle subsegments",
                detail="compound-message contexts remain available for later seam reconciliation.",
                msg_ids=resolved,
            ))
        if not unresolved:
            return
        report.add(Finding(
            WARN, "header-buried",
            f"{len(unresolved)} message(s) contain date+location markers but not as a "
            "leading header",
            detail="the parser reads only the lead bracket; a buried header is "
                   "not used for segmentation.",
            msg_ids=unresolved,
        ))


def _check_date_monotonic(log: loader.Log, report: Report) -> None:
    last: tuple[int, int, int] | None = None
    seen: set[tuple[int, int, int]] = set()
    backward: list[int] = []
    revisited: list[int] = []
    for m in log.messages:
        c = _chronicle(m)
        # Memories and embedded material carry their narrative date, while the
        # physical anchor keeps the outer chronology monotonic.  Prefer this
        # semantic record over an ST settings/header misfire.
        date = _iso_date(c.get("physical_anchor_date") or c.get("date"))
        if date is None:
            date = pauses.parse_header(m.text).date
        if not date:
            continue
        if last is not None and date < last:
            backward.append(m.msg_id)
        elif last is not None and date != last and date in seen:
            revisited.append(m.msg_id)
        seen.add(date)
        last = date
    if backward:
        report.add(Finding(
            RISK, "date-backward",
            f"{len(backward)} message(s) carry a date earlier than a prior one",
            detail="dates should advance; a backward jump signals a typo or a "
                   "mis-parsed header (day boundaries will be wrong).",
            msg_ids=backward,
        ))
    if revisited:
        report.add(Finding(
            WARN, "date-revisited",
            f"{len(revisited)} message(s) return to an earlier day after moving on",
            detail="legitimate for a flashback; otherwise a header error.",
            msg_ids=revisited,
        ))


def _check_location_variants(log: loader.Log, report: Report) -> None:
    norm: dict[str, set[str]] = defaultdict(set)
    for m in log.messages:
        h = pauses.parse_header(m.text)
        if h.location:
            key = re.sub(r"[^a-z0-9]+", " ", h.location.lower()).strip()
            norm[key].add(h.location)
    variants = {k: v for k, v in norm.items() if len(v) > 1}
    if variants:
        sample = "; ".join(" / ".join(sorted(v)) for v in list(variants.values())[:5])
        semantic_complete = all(_chronicle_complete(m) for m in log.messages)
        report.add(Finding(
            INFO if semantic_complete else WARN,
            "legacy-location-variants-resolved" if semantic_complete else "location-variants",
            f"{len(variants)} location(s) appear under more than one spelling",
            detail=(
                f"legacy spelling only; chronicle locations drive segmentation: {sample}"
                if semantic_complete
                else f"these will over-segment scenes: {sample}"
            ),
        ))


def _check_ambiguous_transitions(log: loader.Log, report: Report) -> None:
    prev_loc: str | None = None
    run_headerless = 0
    ambiguous: list[int] = []
    for m in log.messages:
        h = pauses.parse_header(m.text)
        if m.role() == "char" and h.location:
            if prev_loc is not None and h.location != prev_loc and run_headerless >= 2:
                ambiguous.append(m.msg_id)
            prev_loc = h.location
            run_headerless = 0
        else:
            run_headerless += 1
    if ambiguous:
        report.add(Finding(
            INFO, "transition-ambiguous",
            f"{len(ambiguous)} scene change(s) follow a header-less stretch "
            "(boundary pinned approximately)",
            detail="the new location is first seen a turn or two after it likely "
                   "began; segmentation snaps to the headed turn.",
            msg_ids=ambiguous,
        ))


def _check_swipes(log: loader.Log, report: Report) -> None:
    swiped = [
        m.msg_id for m in log.messages
        if isinstance(m.raw.get("swipes"), list) and len(m.raw["swipes"]) > 1
    ]
    if swiped:
        report.add(Finding(
            INFO, "swipes",
            f"{len(swiped)} message(s) have multiple swipes "
            "(the active swipe is what's parsed)",
            msg_ids=swiped,
        ))


def run(path: str | Path) -> Report:
    """Run all checks and return the Report. Non-destructive."""
    path = Path(path)
    report = Report(log_path=str(path), n_messages=0)
    log = _check_line_integrity(path, report)
    if log is None:
        report.add(Finding(RISK, "unloadable", "the log could not be parsed at all"))
        return report
    report.n_messages = len(log)

    _check_required_fields(log, report)
    _check_header_coverage(log, report)
    _check_malformed_headers(log, report)
    _check_buried_headers(log, report)
    _check_date_monotonic(log, report)
    _check_location_variants(log, report)
    _check_ambiguous_transitions(log, report)
    _check_swipes(log, report)
    return report
