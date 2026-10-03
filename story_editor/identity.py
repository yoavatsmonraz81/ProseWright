"""Stable message identity.

Message ids in this engine are positional ordinals, and injection — the most
used feature we have — shifts every ordinal after the insertion point. Sidecars
keyed by ordinal (voice attribution, render policy, beat ranges) therefore
degrade silently: one interlude committed at message 400 slides everything after
it one message left, with no error and no way to detect it afterward.

`structure.py` already dodges this twice (scene cards keyed by content
signature, beat diffing done by content rather than position). This module gives
the rest of the engine the same protection properly: every message carries a
`uid`, and ordinals are derived from it rather than the other way round.

The uid lives in the log line under ``extra.se_uid``. ST tolerates unknown
``extra`` keys, and `strip_uids` removes them on the way out, so the chat file
we hand back to SillyTavern stays byte-clean.

Anchors
-------
A sidecar that used to store ``[189, 219]`` stores an :class:`Anchor` instead —
the pair of uids plus the ordinals they resolved to when last written. The
ordinals are a display cache and a fallback; the uids are the truth.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import backup as backup_mod
from . import config
from .loader import UID_KEY

UID_PREFIX = "m-"


def mint() -> str:
    """A fresh message uid. Random rather than content-derived: identity must
    survive a rewrite of the message text, and duplicate lines must not collide.
    """
    return UID_PREFIX + uuid.uuid4().hex[:12]


def read_uid(raw: dict[str, Any]) -> str:
    extra = raw.get("extra")
    if isinstance(extra, dict):
        value = extra.get(UID_KEY)
        if isinstance(value, str) and value:
            return value
    return ""


def write_uid(raw: dict[str, Any], uid: str) -> dict[str, Any]:
    """Attach a uid to a raw message dict, creating ``extra`` if absent."""
    extra = raw.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        raw["extra"] = extra
    extra[UID_KEY] = uid
    return raw


def strip_uids(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies with ``extra.se_uid`` removed — the shape ST gets back.

    Deep-ish copy of ``extra`` only: the caller's dicts must not be mutated,
    but the rest of the message can be shared.
    """
    out: list[dict[str, Any]] = []
    for raw in messages:
        clean = dict(raw)
        extra = clean.get("extra")
        if isinstance(extra, dict) and UID_KEY in extra:
            trimmed = {k: v for k, v in extra.items() if k != UID_KEY}
            clean["extra"] = trimmed
        out.append(clean)
    return out


@dataclass
class BackfillReport:
    total: int = 0
    minted: int = 0
    existing: int = 0
    written: bool = False
    backup_path: Path | None = None

    def summary(self) -> str:
        head = (
            f"{self.total} messages · {self.existing} already identified · "
            f"{self.minted} minted"
        )
        if not self.written:
            return head + " (dry run — nothing written)"
        where = f" · backup {self.backup_path.name}" if self.backup_path else ""
        return head + where


def backfill(
    log_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> BackfillReport:
    """Give every message in the log a uid, preserving the ones already set.

    Rewrites the log line-by-line so untouched lines keep their exact JSON
    field order — this file is round-tripped into ST and a wholesale reserialise
    would produce a needlessly enormous diff.
    """
    path = Path(log_path or config.working_log())
    if not path.exists():
        raise FileNotFoundError(f"log not found: {path}")

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    report = BackfillReport()
    out_lines: list[str] = []
    seen: set[str] = set()

    for lineno, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        obj = json.loads(stripped)
        if lineno == 0 and "mes" not in obj:
            out_lines.append(stripped)
            continue
        report.total += 1
        uid = read_uid(obj)
        if uid and uid not in seen:
            report.existing += 1
            seen.add(uid)
            out_lines.append(stripped)
            continue
        # No uid, or a duplicate one (copy-pasted line) — mint a fresh identity.
        uid = mint()
        while uid in seen:
            uid = mint()
        seen.add(uid)
        report.minted += 1
        out_lines.append(json.dumps(write_uid(obj, uid), ensure_ascii=False))

    if dry_run or not report.minted:
        return report

    report.backup_path = backup_mod.backup(path, label="pre-uid")
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    report.written = True
    return report


# --------------------------------------------------------------------------- #
# Anchors — how sidecars point at spans of the log
# --------------------------------------------------------------------------- #


@dataclass
class Anchor:
    """A span of the log that survives insertion, deletion, and reordering.

    ``start``/``end`` are the ordinals the uids resolved to when this anchor was
    last written. They are a cache for display and a fallback for pre-uid data,
    never the source of truth.
    """

    from_uid: str = ""
    to_uid: str = ""
    start: int = 0
    end: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "from_uid": self.from_uid,
            "to_uid": self.to_uid,
            "start": self.start,
            "end": self.end,
        }

    @classmethod
    def from_json(cls, data: Any) -> "Anchor":
        """Accepts an anchor object or a legacy ``[start, end]`` pair."""
        if isinstance(data, (list, tuple)) and len(data) == 2:
            return cls(start=int(data[0]), end=int(data[1]))
        if not isinstance(data, dict):
            return cls()
        return cls(
            from_uid=str(data.get("from_uid") or ""),
            to_uid=str(data.get("to_uid") or ""),
            start=int(data.get("start") or 0),
            end=int(data.get("end") or 0),
        )

    @classmethod
    def for_span(cls, log, start: int, end: int) -> "Anchor":
        """Build an anchor from ordinals against a loaded log."""
        n = len(log)
        lo = max(0, min(start, n - 1)) if n else 0
        hi = max(0, min(end, n - 1)) if n else 0
        return cls(
            from_uid=log.get(lo).uid if n else "",
            to_uid=log.get(hi).uid if n else "",
            start=start,
            end=end,
        )

    def resolve(self, log) -> tuple[int, int]:
        """Current ordinals for this span.

        Falls back to the cached ordinals for either end that no longer
        resolves, so a partially migrated or partially deleted span still
        points somewhere sane instead of raising.
        """
        start = log.ordinal_of(self.from_uid) if self.from_uid else None
        end = log.ordinal_of(self.to_uid) if self.to_uid else None
        if start is None:
            start = self.start
        if end is None:
            end = self.end
        if end < start:
            start, end = end, start
        return start, end

    def is_stale(self, log) -> bool:
        """True when the cached ordinals disagree with where the uids now are."""
        return (self.start, self.end) != self.resolve(log)

    def refreshed(self, log) -> "Anchor":
        start, end = self.resolve(log)
        return Anchor(
            from_uid=self.from_uid or (log.get(start).uid if len(log) else ""),
            to_uid=self.to_uid or (log.get(end).uid if len(log) else ""),
            start=start,
            end=end,
        )
