"""Parse a SillyTavern chat .jsonl into an addressable form, touching nothing.

File shape:
  line 0      -> chat metadata object (no "mes" field)
  line 1..N   -> one message per line

Each message keeps its full raw dict so a later write preserves every field
(swipes, send_date, extra, ...). We only ever read/modify the active `mes` text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Where a message's stable identity lives inside the ST line. Defined here (the
# lowest layer) so `identity` and the loader cannot drift apart; stripped before
# anything is handed back to SillyTavern.
UID_KEY = "se_uid"


@dataclass
class Message:
    msg_id: int  # ordinal position among message lines (0-based) — shifts on insert
    speaker: str
    is_user: bool
    is_system: bool
    text: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    uid: str = ""  # stable identity (extra.se_uid); survives insertion/reordering

    def preview(self, width: int = 90) -> str:
        flat = " ".join(self.text.split())
        return flat if len(flat) <= width else flat[: width - 1] + "\u2026"

    def role(self) -> str:
        if self.is_system:
            return "system"
        return "user" if self.is_user else "char"


@dataclass
class Log:
    path: Path
    metadata: dict[str, Any]
    messages: list[Message]
    _by_uid: dict[str, int] = field(repr=False, default_factory=dict)

    def __len__(self) -> int:
        return len(self.messages)

    def get(self, msg_id: int) -> Message:
        if msg_id < 0 or msg_id >= len(self.messages):
            raise IndexError(
                f"message {msg_id} out of range (have 0..{len(self.messages) - 1})"
            )
        return self.messages[msg_id]

    def by_uid(self, uid: str) -> Message | None:
        idx = self._by_uid.get(uid)
        return self.messages[idx] if idx is not None else None

    def ordinal_of(self, uid: str) -> int | None:
        """Where this uid currently sits, or None if the log doesn't hold it."""
        return self._by_uid.get(uid)

    def has_uids(self) -> bool:
        return len(self._by_uid) == len(self.messages) and bool(self.messages)

    def speakers(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.messages:
            counts[m.speaker] = counts.get(m.speaker, 0) + 1
        return counts


def load(path: str | Path) -> Log:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"log not found: {path}")

    metadata: dict[str, Any] = {}
    messages: list[Message] = []

    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # The metadata line is the first object and has no "mes".
            if lineno == 0 and "mes" not in obj:
                metadata = obj
                continue
            extra = obj.get("extra")
            uid = ""
            if isinstance(extra, dict):
                candidate = extra.get(UID_KEY)
                if isinstance(candidate, str):
                    uid = candidate
            messages.append(
                Message(
                    msg_id=len(messages),
                    speaker=obj.get("name", "?"),
                    is_user=bool(obj.get("is_user", False)),
                    is_system=bool(obj.get("is_system", False)),
                    text=obj.get("mes", ""),
                    raw=obj,
                    uid=uid,
                )
            )

    by_uid = {m.uid: m.msg_id for m in messages if m.uid}
    return Log(path=path, metadata=metadata, messages=messages, _by_uid=by_uid)
