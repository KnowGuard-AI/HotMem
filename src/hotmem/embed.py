"""HotMem embedding — deterministic hash default plus the portable protocol.

Purpose:
    Convert text into a fixed-dimension float vector. The zero-configuration
    default is `hotmem-hash-v1`: deterministic character-trigram hashing with
    no model download. Issue #78 adds the portable boundary on top - an
    immutable `EmbeddingDescriptor` identifying an embedding space and a
    minimal `Embedder` protocol - so a compatible semantic embedder can slot
    in behind an explicit runtime-owned instance without changing the
    default, stored packages, or offline behavior.

Interface:
    EmbeddingDescriptor - frozen identity of an embedding space; `key` is the
        canonical fingerprint stored in the `embedding_model` record field
    Embedder (Protocol) - `descriptor` + `embed(text) -> list[float]`
    HashEmbedder / DEFAULT_EMBEDDER - the exact `hotmem-hash-v1` default
    embed_text / pack_embedding / unpack_embedding - unchanged compatibility
        wrappers (existing importers keep working without edits)
    EMBEDDING_DIM / EMBEDDING_MODEL - legacy constants for the hash default

Deps: none (stdlib only)
Extension: one optional local semantic adapter lives behind the [semantic]
    extra (issue #78); it must load an explicitly provisioned local artifact
    and implement this protocol. Hosted adapters follow the same seam.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any, Protocol

from hotmem.trace import Timer, get_tracer

_trace = get_tracer("embed")

EMBEDDING_DIM = 64
EMBEDDING_MODEL = "hotmem-hash-v1"


# ── portable embedding boundary (issue #78) ─────────────────────────────────

_DESCRIPTOR_SEPARATORS = ("/", "@")
_NORMALIZATIONS = ("none", "l2")


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """Immutable identity of an embedding space (issue #78).

    Stored vectors are reusable only between equal descriptors - equal
    dimension alone never establishes compatibility. ``key`` is the
    canonical fingerprint persisted as the record ``embedding_model`` value;
    the hash default's key is exactly ``hotmem-hash-v1``, so existing
    records, packages, and index markers stay valid and unchanged.

    Fields:
        implementation - provider/implementation identifier
        model - model identifier
        dimension - vector dimension
        revision - model revision, when known
        normalization - "none" or "l2" (initially)
        metric - "cosine" (initially)
        preprocessing - preprocessing/version identifier
    """

    implementation: str
    model: str
    dimension: int
    revision: str = ""
    normalization: str = "l2"
    metric: str = "cosine"
    preprocessing: str = ""

    def __post_init__(self) -> None:
        for name in ("implementation", "model"):
            value = getattr(self, name)
            if not value or any(sep in value for sep in _DESCRIPTOR_SEPARATORS):
                raise ValueError(
                    f"EmbeddingDescriptor.{name} must be a non-empty string without "
                    f"'/' or '@' separators (got {value!r})"
                )
        if self.revision and any(sep in self.revision for sep in _DESCRIPTOR_SEPARATORS):
            raise ValueError(
                f"EmbeddingDescriptor.revision must not contain '/' or '@' (got {self.revision!r})"
            )
        if self.preprocessing and any(sep in self.preprocessing for sep in _DESCRIPTOR_SEPARATORS):
            raise ValueError(
                f"EmbeddingDescriptor.preprocessing must not contain '/' or '@' "
                f"(got {self.preprocessing!r})"
            )
        if self.dimension <= 0:
            raise ValueError(
                f"EmbeddingDescriptor.dimension must be positive (got {self.dimension})"
            )
        if self.normalization not in _NORMALIZATIONS:
            raise ValueError(
                f"EmbeddingDescriptor.normalization must be one of "
                f"{_NORMALIZATIONS} (got {self.normalization!r})"
            )
        if self.metric != "cosine":
            raise ValueError(f"EmbeddingDescriptor.metric must be 'cosine' (got {self.metric!r})")

    @property
    def key(self) -> str:
        """Canonical fingerprint string persisted as ``embedding_model``."""
        if self == HASH_DESCRIPTOR:
            return EMBEDDING_MODEL
        parts = [self.implementation, self.model]
        if self.revision:
            parts.append(f"rev:{self.revision}")
        parts.append(f"norm:{self.normalization}")
        if self.preprocessing:
            parts.append(f"pp:{self.preprocessing}")
        return "/".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for package/delta manifest descriptor blocks."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EmbeddingDescriptor:
        """Parse a descriptor block, ignoring unknown keys (forward-compat)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


HASH_DESCRIPTOR = EmbeddingDescriptor(
    implementation="hotmem",
    model="hash-v1",
    dimension=EMBEDDING_DIM,
    normalization="l2",
    metric="cosine",
    preprocessing="trigram-hash-v1",
)


class Embedder(Protocol):
    """Minimal embedder boundary (issue #78).

    Runtimes own their instance and pass it explicitly - no global mutable
    singleton, no network at import/startup/hydration time. Two runtimes in
    one process may use different embedders safely.
    """

    @property
    def descriptor(self) -> EmbeddingDescriptor: ...

    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class HashEmbedder:
    """The exact ``hotmem-hash-v1`` implementation - the zero-config default.

    Delegates to ``embed_text`` so the vector output stays bit-identical by
    construction; the #90 golden-vector oracle keeps proving it.
    """

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return HASH_DESCRIPTOR

    def embed(self, text: str) -> list[float]:
        return embed_text(text)


DEFAULT_EMBEDDER: Embedder = HashEmbedder()


_EMBEDDER_CHOICES = ("hash", "local-semantic")


def resolve_embedder_from_config(
    spec: str | None = None,
    *,
    model_path: str | None = None,
) -> Embedder:
    """Resolve the runtime embedder from one explicit configuration path.

    ``spec`` selects the embedder: "hash" (the default — the
    zero-configuration ``hotmem-hash-v1``) or "local-semantic" (the optional
    ``[semantic]`` extra; requires ``model_path`` — or the
    ``HOTMEM_EMBEDDER_MODEL_PATH`` fallback — pointing at an explicitly
    provisioned local model artifact). Flags take precedence over the
    ``HOTMEM_EMBEDDER`` / ``HOTMEM_EMBEDDER_MODEL_PATH`` environment
    fallbacks; omitted spec means hash, never a download (issue #78).

    Resolution is meant to run BEFORE serving so an invalid selection fails
    fast with an actionable message — never at import, hydration, or test
    time. The optional adapter loads only when selected and never at module
    import.
    """
    name = (spec or os.environ.get("HOTMEM_EMBEDDER") or "hash").strip().lower()
    resolved_path = model_path or os.environ.get("HOTMEM_EMBEDDER_MODEL_PATH")
    if name in ("", "hash"):
        return HashEmbedder()
    if name == "local-semantic":
        try:
            from hotmem.semantic import LocalSemanticEmbedder
        except ImportError as err:
            raise ValueError(
                "embedder 'local-semantic' requires the optional [semantic] extra. "
                "Install it with: uv pip install 'hotmem[semantic]'"
            ) from err
        if not resolved_path:
            raise ValueError(
                "embedder 'local-semantic' requires --embedder-model-path (or "
                "HOTMEM_EMBEDDER_MODEL_PATH) pointing at an explicitly provisioned "
                "local model artifact; HotMem never downloads models"
            )
        return LocalSemanticEmbedder(resolved_path)
    raise ValueError(f"unknown embedder {name!r}; expected one of: {', '.join(_EMBEDDER_CHOICES)}")


# ── hash implementation (unchanged; the compatibility contract) ─────────────


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
