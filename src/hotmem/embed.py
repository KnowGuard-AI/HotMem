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
from functools import lru_cache

from hotmem.trace import Timer, get_tracer

_trace = get_tracer("embed")

EMBEDDING_DIM = 64
EMBEDDING_MODEL = "hotmem-hash-v1"


# Bounded cache of trigram -> (bucket, sign). Text reuses a small vocabulary
# of character trigrams heavily (bundle corpora share word pools), so the
# cache removes nearly all md5 calls while keeping vectors bit-identical:
# same gram bytes -> same digest -> same bucket/sign (#90).
@lru_cache(maxsize=65536)
def _gram_bucket_sign(gram: str) -> tuple[int, float]:
    h = int.from_bytes(hashlib.md5(gram.encode(), usedforsecurity=False).digest(), "big")
    bucket = h % EMBEDDING_DIM
    sign = 1.0 if (h >> 64) % 2 == 0 else -1.0
    return bucket, sign


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
            bucket, sign = _gram_bucket_sign(text_lower[i : i + 3])
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
