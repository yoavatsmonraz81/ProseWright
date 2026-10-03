"""Search the index.

Four entry points, all returning ranked `Hit`s with the message metadata
attached:

- `search_structural` — filters only (speaker, role, date range, is_interlude).
  No relevance ranking; returns rows in msg_id order.
- `search_keyword`    — FTS5 MATCH over text_clean.
- `search_semantic`   — k-NN over BGE embeddings via sqlite-vec.
- `search`            — the fused one. Runs keyword + semantic separately, fuses
                        their rankings with reciprocal rank fusion, optionally
                        narrows with a structural pre-filter.

Design principle (from the dev plan):
  meaning discovers, structure validates.

Hence: meaning + keyword are the *finders*; structural fields are used as
pre-filters (cross-checks), not as the primary discovery mechanism.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ._sqlite_vec import load_into as _load_sqlite_vec
from .embed import Embedder, default_embedder

# Reciprocal Rank Fusion constant. The classic value from Cormack et al. is 60;
# higher values flatten the contribution of top-ranked items, lower values give
# them more weight. 60 is a good general default for prose retrieval.
RRF_K = 60


@dataclass
class Hit:
    msg_id: int
    speaker: str
    role: str
    is_interlude: bool
    stage: str | None
    story_date: str | None
    story_time: str | None
    location: str | None
    text: str
    text_clean: str
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)  # 'keyword_rank', 'semantic_rank'

    def preview(self, n: int = 100) -> str:
        body = " ".join(self.text_clean.split())
        return body[:n] + ("..." if len(body) > n else "")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    _load_sqlite_vec(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _hit_from_row(row: sqlite3.Row, **extras) -> Hit:
    return Hit(
        msg_id=row["msg_id"],
        speaker=row["speaker"],
        role=row["role"],
        is_interlude=bool(row["is_interlude"]),
        stage=row["stage"],
        story_date=row["story_date"],
        story_time=row["story_time"],
        location=row["location"],
        text=row["text"],
        text_clean=row["text_clean"],
        **extras,
    )


def _structural_clause(
    *,
    speaker: str | None = None,
    role: str | None = None,
    is_interlude: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    msg_id_from: int | None = None,
    msg_id_to: int | None = None,
) -> tuple[str, list]:
    """Build a parameterised WHERE clause that applies the structural filters."""
    where: list[str] = []
    params: list = []
    if speaker:
        where.append("LOWER(speaker) = LOWER(?)")
        params.append(speaker)
    if role:
        where.append("role = ?")
        params.append(role)
    if is_interlude is not None:
        where.append("is_interlude = ?")
        params.append(1 if is_interlude else 0)
    if date_from:
        where.append("story_date_iso >= ?")
        params.append(date_from)
    if date_to:
        where.append("story_date_iso <= ?")
        params.append(date_to)
    if msg_id_from is not None:
        where.append("msg_id >= ?")
        params.append(msg_id_from)
    if msg_id_to is not None:
        where.append("msg_id <= ?")
        params.append(msg_id_to)
    return (" AND ".join(where) if where else ""), params


# --------------------------------------------------------------------------- #
# Structural-only: no ranking, just filter and return.
# --------------------------------------------------------------------------- #

def search_structural(
    db_path: str | Path,
    *,
    speaker: str | None = None,
    role: str | None = None,
    is_interlude: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    msg_id_from: int | None = None,
    msg_id_to: int | None = None,
    limit: int | None = None,
) -> list[Hit]:
    conn = _connect(Path(db_path))
    try:
        where, params = _structural_clause(
            speaker=speaker,
            role=role,
            is_interlude=is_interlude,
            date_from=date_from,
            date_to=date_to,
            msg_id_from=msg_id_from,
            msg_id_to=msg_id_to,
        )
        sql = "SELECT * FROM messages"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY msg_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [_hit_from_row(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Keyword via FTS5.
# --------------------------------------------------------------------------- #

# FTS5 'AND'/'OR'/'NOT' are operators; punctuation also has meaning. For free
# text we quote each token to avoid surprises and let phrase queries pass through.
_FTS_SAFE_RE = re.compile(r'"[^"]*"|\S+')


def _fts_query_for(text: str) -> str:
    """Turn a natural-language query into a safe FTS5 MATCH expression.
    Each bare token is quoted; quoted phrases are passed through verbatim."""
    parts: list[str] = []
    for piece in _FTS_SAFE_RE.findall(text):
        if piece.startswith('"') and piece.endswith('"'):
            parts.append(piece)
        else:
            cleaned = piece.replace('"', "")
            if cleaned:
                parts.append(f'"{cleaned}"')
    if not parts:
        return '""'
    return " OR ".join(parts)


def search_keyword(
    db_path: str | Path,
    query: str,
    *,
    limit: int = 25,
    speaker: str | None = None,
    role: str | None = None,
    is_interlude: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    msg_id_from: int | None = None,
    msg_id_to: int | None = None,
) -> list[Hit]:
    conn = _connect(Path(db_path))
    try:
        match = _fts_query_for(query)
        where, params = _structural_clause(
            speaker=speaker,
            role=role,
            is_interlude=is_interlude,
            date_from=date_from,
            date_to=date_to,
            msg_id_from=msg_id_from,
            msg_id_to=msg_id_to,
        )
        # FTS rank is a relevance score; lower is better. We use it for ranking
        # but report it as a positive 'score' on the Hit for legibility.
        sql = (
            "SELECT m.*, bm25(messages_fts) AS rank "
            "FROM messages m JOIN messages_fts f ON f.rowid = m.msg_id "
            "WHERE messages_fts MATCH ? "
        )
        sql_params: list = [match]
        if where:
            sql += f"AND {where} "
            sql_params.extend(params)
        sql += "ORDER BY rank LIMIT ?"
        sql_params.append(limit)
        rows = conn.execute(sql, sql_params).fetchall()
        return [
            _hit_from_row(r, score=-float(r["rank"]), breakdown={"keyword_rank": i})
            for i, r in enumerate(rows)
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Semantic via sqlite-vec k-NN.
# --------------------------------------------------------------------------- #

def search_semantic(
    db_path: str | Path,
    query: str,
    *,
    limit: int = 25,
    embedder: Embedder | None = None,
    speaker: str | None = None,
    role: str | None = None,
    is_interlude: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    msg_id_from: int | None = None,
    msg_id_to: int | None = None,
) -> list[Hit]:
    embedder = embedder or default_embedder()
    qblob = embedder.encode_one(query)
    conn = _connect(Path(db_path))
    try:
        where, params = _structural_clause(
            speaker=speaker,
            role=role,
            is_interlude=is_interlude,
            date_from=date_from,
            date_to=date_to,
            msg_id_from=msg_id_from,
            msg_id_to=msg_id_to,
        )
        # When the user supplies a structural pre-filter, ask for more candidates
        # than `limit` so the post-filter still has enough to fill the bucket.
        ask_k = limit * 5 if where else limit
        sql = (
            "SELECT m.*, v.distance "
            "FROM vec_messages v JOIN messages m ON m.msg_id = v.rowid "
            "WHERE v.embedding MATCH ? AND v.k = ? "
        )
        sql_params: list = [qblob, ask_k]
        if where:
            sql += f"AND {where} "
            sql_params.extend(params)
        sql += "ORDER BY v.distance LIMIT ?"
        sql_params.append(limit)
        rows = conn.execute(sql, sql_params).fetchall()
        # vec0 returns distance (smaller is better); turn into a similarity-ish
        # score for display (higher is better, capped at 1.0 since BGE is
        # normalised cosine).
        return [
            _hit_from_row(
                r,
                score=max(0.0, 1.0 - float(r["distance"])),
                breakdown={"semantic_rank": i, "distance": float(r["distance"])},
            )
            for i, r in enumerate(rows)
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Fused: RRF over keyword + semantic, with optional structural pre-filter.
# --------------------------------------------------------------------------- #

def _rrf_merge(
    keyword_hits: list[Hit], semantic_hits: list[Hit], k: int = RRF_K
) -> list[Hit]:
    """Reciprocal Rank Fusion: combine two ranked lists by summing 1/(k+rank)
    contributions for each appearance. Items that appear in both lists - and
    especially that rank highly in both - bubble to the top."""
    fused: dict[int, Hit] = {}
    for rank, hit in enumerate(keyword_hits):
        h = fused.setdefault(hit.msg_id, _clone_hit(hit))
        h.score += 1.0 / (k + rank + 1)
        h.breakdown.setdefault("keyword_rank", rank)
    for rank, hit in enumerate(semantic_hits):
        h = fused.setdefault(hit.msg_id, _clone_hit(hit))
        h.score += 1.0 / (k + rank + 1)
        h.breakdown.setdefault("semantic_rank", rank)
    ordered = sorted(fused.values(), key=lambda h: -h.score)
    return ordered


def _clone_hit(hit: Hit) -> Hit:
    """A fresh Hit with score reset to 0 and an empty breakdown. We accumulate
    fused scores onto the clone so the per-mode hits remain pure."""
    return Hit(
        msg_id=hit.msg_id,
        speaker=hit.speaker,
        role=hit.role,
        is_interlude=hit.is_interlude,
        stage=hit.stage,
        story_date=hit.story_date,
        story_time=hit.story_time,
        location=hit.location,
        text=hit.text,
        text_clean=hit.text_clean,
        score=0.0,
        breakdown={},
    )


def search(
    db_path: str | Path,
    query: str,
    *,
    limit: int = 10,
    candidates_per_layer: int = 25,
    embedder: Embedder | None = None,
    speaker: str | None = None,
    role: str | None = None,
    is_interlude: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    msg_id_from: int | None = None,
    msg_id_to: int | None = None,
) -> list[Hit]:
    """The single fused search call. Runs FTS and vector retrieval separately,
    fuses their ranks with RRF, and returns the top `limit` Hits.

    Structural filters are applied to BOTH underlying queries (they're a
    pre-filter, not a post-filter, so they shrink the candidate pool early).
    """
    structural = dict(
        speaker=speaker,
        role=role,
        is_interlude=is_interlude,
        date_from=date_from,
        date_to=date_to,
        msg_id_from=msg_id_from,
        msg_id_to=msg_id_to,
    )
    kw = search_keyword(db_path, query, limit=candidates_per_layer, **structural)
    sm = search_semantic(
        db_path,
        query,
        limit=candidates_per_layer,
        embedder=embedder,
        **structural,
    )
    fused = _rrf_merge(kw, sm)
    return fused[:limit]
