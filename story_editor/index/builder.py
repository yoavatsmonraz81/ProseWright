"""Build / refresh the index for a log.

End-to-end:
  build(log_path)  -> opens (or rebuilds) workspace/<stem>.index.sqlite3
                      populates the structural, FTS, and vec layers
                      returns the db path
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .. import config, loader, pauses, text as text_mod
from . import schema
from ._sqlite_vec import load_into as _load_sqlite_vec
from .embed import Embedder, default_embedder

_EMBED_BATCH = 32


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded. Caller closes."""
    conn = sqlite3.connect(db_path)
    _load_sqlite_vec(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _drop_existing(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for table in schema.TABLES_IN_DROP_ORDER:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(schema.DDL)
    # Triggers are nice-to-have for incremental updates later; the build itself
    # populates FTS explicitly so they're not load-bearing for the initial pass.
    cur.executescript(schema.FTS_TRIGGERS)
    conn.commit()


def _iso_from_header(date_tuple: tuple[int, int, int] | None) -> str | None:
    if not date_tuple:
        return None
    y, m, d = date_tuple
    return f"{y:04d}-{m:02d}-{d:02d}"


def _row_for(message: loader.Message) -> dict:
    raw = message.raw
    header = pauses.parse_header(message.text)
    extra = raw.get("extra") or {}
    se = extra.get("story_editor") if isinstance(extra, dict) else None
    is_interlude = bool(isinstance(se, dict) and se.get("interlude"))
    stage = se.get("stage") if is_interlude and isinstance(se, dict) else None

    clean = text_mod.clean(message.text)
    return {
        "msg_id": message.msg_id,
        "speaker": message.speaker,
        "role": message.role(),
        "is_interlude": 1 if is_interlude else 0,
        "stage": stage,
        "send_date": raw.get("send_date"),
        "story_date_iso": _iso_from_header(header.date),
        "story_date": header.date_str,
        "story_time": header.time,
        "location": header.location,
        "text": message.text,
        "text_clean": clean,
    }


def _fill_locations_from_scenes(log: loader.Log, rows: list[dict]) -> None:
    """Fill-forward scene location onto header-less turns (most user messages).

    Prefer canonical location (+ aliases) from scene cards when present; otherwise
    inherit the segmented scene's header location. Mutates ``rows`` in place.
    """
    # Lazy import: structure pulls index.search for audit; keep build light.
    from .. import structure as structure_mod

    scenes = structure_mod.segment_scenes(log)
    card_by_span = structure_mod.scene_location_by_span()

    # msg_id -> (prefer_overwrite, location_blob)
    by_msg: dict[int, tuple[bool, str]] = {}
    for s in scenes:
        if s.kind != "scene":
            continue
        card = card_by_span.get((s.start_msg_id, s.end_msg_id))
        if card and card.get("location"):
            blob = structure_mod.location_search_blob(
                card.get("location"), card.get("location_aliases") or []
            )
            prefer = True
        else:
            blob = s.location
            prefer = False
        if not blob:
            continue
        for mid in range(s.start_msg_id, s.end_msg_id + 1):
            by_msg[mid] = (prefer, blob)

    for row in rows:
        info = by_msg.get(row["msg_id"])
        if not info:
            continue
        prefer, blob = info
        if prefer or not row.get("location"):
            row["location"] = blob


def _insert_messages(
    conn: sqlite3.Connection, rows: list[dict]
) -> None:
    """Insert into messages. The AFTER INSERT trigger keeps FTS in sync."""
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO messages (
            msg_id, speaker, role, is_interlude, stage,
            send_date, story_date_iso, story_date, story_time, location,
            text, text_clean
        ) VALUES (
            :msg_id, :speaker, :role, :is_interlude, :stage,
            :send_date, :story_date_iso, :story_date, :story_time, :location,
            :text, :text_clean
        )
        """,
        rows,
    )
    conn.commit()


def _embed_all(
    conn: sqlite3.Connection, rows: list[dict], embedder: Embedder
) -> None:
    """Embed every row's clean text in batches; insert into vec_messages."""
    cur = conn.cursor()
    cur.execute("DELETE FROM vec_messages")
    conn.commit()

    # Embed in batches to keep memory bounded for long logs.
    for start in range(0, len(rows), _EMBED_BATCH):
        chunk = rows[start : start + _EMBED_BATCH]
        # Empty strings can produce degenerate vectors; pad with a space so the
        # row exists and the rowid stays aligned with msg_id.
        texts = [r["text_clean"] or " " for r in chunk]
        blobs = embedder.encode(texts)
        cur.executemany(
            "INSERT INTO vec_messages(rowid, embedding) VALUES (?, ?)",
            [(r["msg_id"], blob) for r, blob in zip(chunk, blobs)],
        )
        conn.commit()


def _write_meta(
    conn: sqlite3.Connection,
    *,
    log_path: Path,
    embedder: Embedder,
    n_msgs: int,
    elapsed_s: float,
) -> None:
    pairs = [
        ("source_log", str(log_path)),
        ("embed_model", embedder.name),
        ("embed_dim", str(embedder.dim)),
        ("built_at", datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        ("messages_indexed", str(n_msgs)),
        ("build_seconds", f"{elapsed_s:.2f}"),
    ]
    cur = conn.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", pairs
    )
    conn.commit()


def build(
    log_path: str | Path,
    db_path: str | Path | None = None,
    rebuild: bool = False,
    embedder: Embedder | None = None,
) -> Path:
    """Open or create the index db for `log_path`. If `rebuild` is True, drop
    all existing tables first. Returns the db path."""
    log_path = Path(log_path).resolve()
    db_path = Path(db_path) if db_path else config.index_db_for(log_path)
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    log = loader.load(log_path)
    embedder = embedder or default_embedder()

    # Build beside the destination and atomically promote only after every text
    # and vector row is complete.  A missing model, OOM, or interrupted process
    # must never hollow out the last usable index.
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{db_path.name}.", suffix=".building", dir=db_path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    start = time.perf_counter()
    conn = _connect(temp_path)
    try:
        _create_schema(conn)

        rows = [_row_for(m) for m in log.messages]
        _fill_locations_from_scenes(log, rows)
        _insert_messages(conn, rows)
        _embed_all(conn, rows, embedder)
        elapsed = time.perf_counter() - start
        _write_meta(
            conn,
            log_path=log_path,
            embedder=embedder,
            n_msgs=len(rows),
            elapsed_s=elapsed,
        )
    except Exception:
        conn.close()
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    os.replace(temp_path, db_path)
    return db_path


def info(db_path: str | Path) -> dict:
    """Quick summary for the CLI: row counts, model, sources, build time."""
    db_path = Path(db_path)
    if not db_path.exists():
        return {"exists": False, "path": str(db_path)}
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        n_msgs = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_fts = cur.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        n_vec = cur.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0]
        meta = dict(cur.execute("SELECT key, value FROM meta").fetchall())
        speakers = [
            r[0]
            for r in cur.execute(
                "SELECT speaker FROM messages GROUP BY speaker ORDER BY COUNT(*) DESC"
            )
        ]
        return {
            "exists": True,
            "path": str(db_path),
            "messages": n_msgs,
            "fts_rows": n_fts,
            "vec_rows": n_vec,
            "meta": meta,
            "speakers": speakers,
        }
    finally:
        conn.close()
