"""Lazy sqlite-vec loader — keeps Phase 0 / API-only imports off the index deps."""

from __future__ import annotations

import sqlite3


def load_into(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
