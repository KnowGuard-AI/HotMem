"""HotMem embedding — deterministic hash-based embedder for MVP.

Purpose:
    Convert text into a fixed-dimension float vector using deterministic hashing.
    No external model required. Provides real cosine-similarity semantics via
    character n-gram hashing.

Interface:
    embed_text(text: str) -> list[float]
    pack_embedding(vec: list[float]) -> bytes
    unpack_embedding(blob: bytes) -> list[float]
    EMBEDDING_DIM: int
    EMBEDDING_MODEL: str

Deps: none (stdlib only)
Extension: replace embed_text() with a real model call (sentence-transformers, OpenAI, etc.).
"""

from __future__ import annotations

import hashlib
import math
import struct

from hotmem.trace import Timer, get_tracer

_trace = get_tracer("embed")

EMBEDDING_DIM = 64
EMBEDDING_MODEL = "hotmem-hash-v1"


def embed_text(text: str) -> list[float]:
    """Produce a deterministic embedding vector from text.

    Uses overlapping character trigrams hashed into buckets, then L2-normalized.
    Semantically similar strings share trigrams and thus produce closer vectors.
    """
    with Timer() as t:
        vec = [0.0] * EMBEDDING_DIM
        text_lower = text.lower()

        # Hash overlapping trigrams into embedding buckets
        for i in range(max(1, len(text_lower) - 2)):
            gram = text_lower[i : i + 3]
            h = int(hashlib.md5(gram.encode(), usedforsecurity=False).hexdigest(), 16)
            bucket = h % EMBEDDING_DIM
            # Use upper bits for sign/magnitude
            sign = 1.0 if (h >> 64) % 2 == 0 else -1.0
            vec[bucket] += sign

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]

    _trace.debug("compute", "embedded text", detail={"chars": len(text), "ms": round(t.ms, 2)})
    return vec


def pack_embedding(vec: list[float]) -> bytes:
    """Pack float vector into a compact binary blob (float32 array)."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> list[float]:
    """Unpack binary blob back into float vector."""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))
