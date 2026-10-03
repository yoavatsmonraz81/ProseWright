"""The embedding layer.

We keep this behind a small protocol so the embedder can be swapped (a future
local /v1/embeddings endpoint, a Claude embed call, a different ST model)
without touching the index code. For Phase 1 the only implementation is the
local sentence-transformers BGE model.

Lazy loading: the model is heavy to import (torch + transformers) so we only
construct it on the first encode() call. The CLI commands that don't need
embeddings (e.g. `index info`, structural-only search) stay fast.
"""

from __future__ import annotations

import struct
from typing import Iterable, Protocol

from .. import config


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> list[bytes]:
        """Return one packed float32-blob per input text, ready for sqlite-vec."""
        ...


def pack(vector: Iterable[float]) -> bytes:
    """Pack a Python float iterable into the little-endian float32 blob that
    sqlite-vec expects for vec0 columns."""
    floats = list(vector)
    return struct.pack(f"<{len(floats)}f", *floats)


class SentenceTransformerEmbedder:
    """Local BGE-small via sentence-transformers. Normalised, 384-d."""

    def __init__(self, model_name: str | None = None) -> None:
        self.name = model_name or config.EMBED_MODEL_NAME
        self.dim = config.EMBED_DIM
        self._model = None  # lazy

    def _load(self):
        if self._model is None:
            # Heavy import; only happens when we actually need to embed.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name)
        return self._model

    def encode(self, texts: list[str]) -> list[bytes]:
        if not texts:
            return []
        model = self._load()
        embeddings = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        # Each row is a float32 numpy array of length `dim`; pack to bytes.
        return [pack(row.tolist()) for row in embeddings]

    def encode_one(self, text: str) -> bytes:
        return self.encode([text])[0]


# One embedder per process — retrieval runs dozens of semantic queries per
# interlude propose; without this, each call to default_embedder() reloads BGE.
_default_embedder: SentenceTransformerEmbedder | None = None


def default_embedder() -> Embedder:
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = SentenceTransformerEmbedder()
    return _default_embedder


def reset_default_embedder() -> None:
    """Test helper: drop the cached embedder."""
    global _default_embedder
    _default_embedder = None
