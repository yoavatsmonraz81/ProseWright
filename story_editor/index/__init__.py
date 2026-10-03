"""Phase 1 — the three-layer index.

Public surface:
- `build(log_path, db_path=None, rebuild=False)` -> Path: build/refresh the index.
- `search(query, ...)`: fused keyword + semantic search with optional structural
  pre-filter. Returns ranked Hits with msg metadata.
- `info(db_path)` -> dict: summary stats for the CLI.

Design principle (from the dev plan):
  meaning discovers, structure validates.

The fused search uses meaning + keyword as the primary finders; structural
filters (speaker, date range, role) are applied as a pre-filter and used to
cross-check, not as the thing that finds. This keeps the editor general (works
without a spine) while getting sharper wherever canon exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .search import Hit, search

if TYPE_CHECKING:
    from pathlib import Path


def build(log_path, db_path=None, rebuild=False):
    from .builder import build as _build

    return _build(log_path, db_path=db_path, rebuild=rebuild)


def info(db_path):
    from .builder import info as _info

    return _info(db_path)


__all__ = ["build", "info", "search", "Hit"]
