"""Sync between SillyTavern in-memory chat and the engine's .jsonl working log.

The ST extension pushes ``getContext().chat`` before an operator runs and pulls
updated message bodies back after commit. Metadata line 0 is preserved across
push when the log already exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, identity, loader


class WorkingLogAhead(Exception):
    """ST pushed fewer turns than the working log already holds.

    The usual case is a prepended interlude: proposing from an un-pulled chat
    would silently delete the new first turn.
    """

    def __init__(self, working: int, incoming: int):
        self.working = working
        self.incoming = incoming
        super().__init__(
            f"SillyTavern sent {incoming} turns but the working log has {working}. "
            "Load the engine log into the chat first — proposing now would "
            "delete the prepended interlude."
        )


def _default_metadata() -> dict[str, Any]:
    return {
        "chat_metadata": {
            "synced_from": "sillytavern",
            "story_editor": True,
        }
    }


def _uid_recovery_index(log) -> tuple[dict[tuple[str, Any], str], dict[tuple[str, str], str]]:
    """Two lookups for re-identifying messages ST pushes back at us.

    ST strips nothing on its side, but we strip our uid on the way out, so a
    push arrives anonymous. ``send_date`` survives round trips and is stable
    across a text edit, which makes it the better key; exact text is the
    fallback for messages that lack one. Ambiguous keys are dropped rather than
    guessed — a wrong match would silently transplant a message's history.
    """
    by_date: dict[tuple[str, Any], str] = {}
    by_text: dict[tuple[str, str], str] = {}
    date_seen: set[tuple[str, Any]] = set()
    text_seen: set[tuple[str, str]] = set()
    for m in log.messages:
        if not m.uid:
            continue
        date_key = (m.speaker, m.raw.get("send_date"))
        if date_key[1] is not None:
            if date_key in date_seen:
                by_date.pop(date_key, None)
            else:
                date_seen.add(date_key)
                by_date[date_key] = m.uid
        text_key = (m.speaker, m.text)
        if text_key in text_seen:
            by_text.pop(text_key, None)
        else:
            text_seen.add(text_key)
            by_text[text_key] = m.uid
    return by_date, by_text


def import_st_chat(
    chat: list[dict[str, Any]], path: Path, *, force: bool = False
) -> int:
    """Write ST chat messages to a .jsonl log. Returns message count written.

    Message identity is carried across the push: a wholesale overwrite would
    otherwise mint fresh uids for the entire log and orphan every sidecar keyed
    to it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    metadata = _default_metadata()
    by_date: dict[tuple[str, Any], str] = {}
    by_text: dict[tuple[str, str], str] = {}
    incoming = [m for m in chat if isinstance(m, dict)]
    if path.exists():
        try:
            existing = loader.load(path)
            metadata = existing.metadata or metadata
            by_date, by_text = _uid_recovery_index(existing)
            if not force and len(incoming) < len(existing):
                raise WorkingLogAhead(len(existing), len(incoming))
        except WorkingLogAhead:
            raise
        except (json.JSONDecodeError, OSError):
            pass

    lines = [json.dumps(metadata, ensure_ascii=False)]
    count = 0
    claimed: set[str] = set()
    for msg in incoming:
        if not isinstance(msg, dict):
            continue
        msg = dict(msg)
        uid = identity.read_uid(msg)
        if not uid or uid in claimed:
            speaker = msg.get("name", "?")
            uid = (
                by_date.get((speaker, msg.get("send_date")))
                or by_text.get((speaker, msg.get("mes", "")))
                or ""
            )
            if not uid or uid in claimed:
                uid = identity.mint()
        claimed.add(uid)
        identity.write_uid(msg, uid)
        lines.append(json.dumps(msg, ensure_ascii=False))
        count += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


def write_st_chat(src: Path, dest: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Write the working log out as a SillyTavern chat file, uids stripped.

    This is the only sanctioned way to update the live chat file. Copying the
    working log across by hand would carry ``extra.se_uid`` into ST — harmless
    to ST itself, but it violates the rule that the chat file is ours to read
    and never to decorate.
    """
    src, dest = Path(src), Path(dest)
    log = loader.load(src)
    metadata = log.metadata
    if dest.exists():
        try:
            dest_meta = loader.load(dest).metadata
            if dest_meta:
                metadata = dest_meta
        except (json.JSONDecodeError, OSError):
            pass
    lines = [json.dumps(metadata, ensure_ascii=False)]
    lines.extend(
        json.dumps(raw, ensure_ascii=False)
        for raw in identity.strip_uids(m.raw for m in log.messages)
    )
    body = "\n".join(lines) + "\n"

    result: dict[str, Any] = {
        "source": str(src),
        "dest": str(dest),
        "messages": len(log.messages),
        "written": False,
        "backup": None,
    }
    if dry_run:
        return result

    if dest.exists():
        from . import backup as backup_mod

        result["backup"] = str(backup_mod.backup(dest, label="pre-sync"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    result["written"] = True
    return result


class NoChatConfigured(ValueError):
    """The open project names no SillyTavern chat to push to."""


def project_chat() -> Path:
    """The open project's SillyTavern chat file; never another project's."""
    if not config.SOURCE_LOG:
        raise NoChatConfigured(
            f"project {config.PROJECT_ID!r} has no SillyTavern chat "
            "(set integrations.sillytavern.chat in its project.json)"
        )
    return Path(config.SOURCE_LOG)


def export_st_chat(path: Path) -> dict[str, Any]:
    """Read a .jsonl log and return ST-shaped ``{ metadata, messages }``.

    The engine's ``extra.se_uid`` never crosses this boundary: what SillyTavern
    receives is byte-identical to what it would have written itself.
    """
    log = loader.load(path)
    return {
        "metadata": log.metadata,
        "messages": identity.strip_uids(m.raw for m in log.messages),
        "message_count": len(log.messages),
        "path": str(log.path.resolve()),
    }


def _sync_projection(raw: dict[str, Any]) -> dict[str, Any]:
    """Message payload as owned by ST, excluding story-editor annotations."""
    out = dict(raw)
    extra = out.get("extra")
    if isinstance(extra, dict):
        extra = dict(extra)
        extra.pop("se_uid", None)
        extra.pop("story_editor", None)
        if extra:
            out["extra"] = extra
        else:
            out.pop("extra", None)
    out.pop("story_editor", None)
    return out


def chat_status(
    working: Path | None = None,
    st_path: Path | None = None,
) -> dict[str, Any]:
    """Compare the working log to the live SillyTavern chat file."""
    working = Path(working or config.working_log())
    if not st_path and not config.SOURCE_LOG:
        return {
            "kind": "not_configured",
            "detail": f"project {config.PROJECT_ID!r} has no SillyTavern chat",
            "working": str(working.resolve()),
            "st": None, "st_exists": False, "working_count": 0, "st_count": 0,
            "in_sync": False, "changed_text_count": 0, "changed_message_count": 0,
            "changed_msg_ids": [],
        }
    dest = Path(st_path or config.SOURCE_LOG)
    wc = 0
    sc = 0
    changed_text: list[int] = []
    changed_payload: list[int] = []
    wlog = None
    slog = None
    if working.exists():
        wlog = loader.load(working)
        wc = len(wlog)
    if dest.exists():
        slog = loader.load(dest)
        sc = len(slog)
    if wlog is not None and slog is not None:
        for i, (wm, sm) in enumerate(zip(wlog.messages, slog.messages)):
            if wm.text != sm.text:
                changed_text.append(i)
            if _sync_projection(wm.raw) != _sync_projection(sm.raw):
                changed_payload.append(i)

    if not dest.exists():
        kind = "st_missing"
    elif not working.exists():
        kind = "working_missing"
    elif wc == sc and not changed_payload:
        kind = "in_sync"
    elif wc > sc:
        kind = "working_ahead"
    elif sc > wc:
        kind = "st_ahead"
    else:
        kind = "diverged"

    return {
        "kind": kind,
        "working": str(working.resolve()),
        "st": str(dest),
        "st_exists": dest.exists(),
        "working_count": wc,
        "st_count": sc,
        "in_sync": kind == "in_sync",
        "changed_text_count": len(changed_text),
        "changed_message_count": len(changed_payload),
        "changed_msg_ids": changed_payload[:50],
    }
