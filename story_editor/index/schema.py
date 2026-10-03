"""SQLite schema for the three-layer index.

Layers:
  messages       — structural (one row per message; everything we know up front)
  messages_fts   — FTS5 virtual table for keyword search (over cleaned text)
  vec_messages   — sqlite-vec virtual table for semantic search (BGE-small, 384-d)
  meta           — key/value: model name, dim, source log, build time, etc.

Notes:
- `messages.text` is the raw message body (header + meta blocks preserved) so the
  index can be queried for the original prose if needed. `text_clean` is the
  scrubbed version that goes into FTS and into the embedder.
- FTS5 uses external content (content='messages', content_rowid='msg_id') so
  the FTS index is automatically kept in sync via triggers and doesn't duplicate
  text on disk.
- The vec table is keyed by msg_id (rowid). vec_messages.embedding is a vec[384]
  column - sqlite-vec stores it efficiently and serves k-NN via MATCH.
"""

# The 384 here is intentionally hard-coded to the BGE-small model in config.py;
# changing embedders means rebuilding the index, not patching the dim in flight.
EMBED_DIM = 384

DDL = f"""
CREATE TABLE IF NOT EXISTS messages (
    msg_id          INTEGER PRIMARY KEY,
    speaker         TEXT NOT NULL,
    role            TEXT NOT NULL,            -- 'user' | 'char' | 'system'
    is_interlude    INTEGER NOT NULL DEFAULT 0,
    stage           TEXT,                     -- if interlude: tolerance|concern|...
    send_date       TEXT,                     -- ISO timestamp from the .jsonl
    story_date_iso  TEXT,                     -- yyyy-mm-dd from the header, sortable
    story_date      TEXT,                     -- 'August 27, 1841' as written
    story_time      TEXT,                     -- '10:03 PM' as written
    location        TEXT,                     -- header 📍 field
    text            TEXT NOT NULL,            -- raw .mes (header + meta intact)
    text_clean      TEXT NOT NULL             -- scrubbed for FTS + embed
);

CREATE INDEX IF NOT EXISTS idx_messages_speaker ON messages(speaker);
CREATE INDEX IF NOT EXISTS idx_messages_role    ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_date    ON messages(story_date_iso);
CREATE INDEX IF NOT EXISTS idx_messages_intl    ON messages(is_interlude);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text_clean,
    content='messages',
    content_rowid='msg_id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
    embedding float[{EMBED_DIM}]
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Trigger set to keep FTS in sync if we ever insert/update incrementally. The
# initial build does an explicit `INSERT INTO messages_fts(rowid, text_clean)`
# so we don't actually need these for the build, but they make incremental
# updates safe.
FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text_clean) VALUES (new.msg_id, new.text_clean);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text_clean)
        VALUES('delete', old.msg_id, old.text_clean);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text_clean)
        VALUES('delete', old.msg_id, old.text_clean);
    INSERT INTO messages_fts(rowid, text_clean) VALUES (new.msg_id, new.text_clean);
END;
"""

# Convenience: every table we manage, in the order to drop them for --rebuild.
TABLES_IN_DROP_ORDER = (
    "messages_fts",
    "vec_messages",
    "messages",
    "meta",
)
