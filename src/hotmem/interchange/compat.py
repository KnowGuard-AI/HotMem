"""Embedding compatibility rules shared by every reader (interchange-v1 §5, #78).

Purpose:
    One predicate deciding whether a stored embedding may be reused as-is,
    extended to full embedding descriptors (issue #78): a stored vector is
    reusable only when the active embedder's descriptor matches the recorded
    identity. Absent legacy fields keep the documented hash semantics; equal
    dimension alone never establishes compatibility.

Rules — a stored embedding is reused iff ALL hold:
      1. the recorded descriptor key matches the active descriptor's key
         (absent/empty ``embedding_model`` maps to the legacy default
         ``hotmem-hash-v1``),
      2. the recorded dimension matches the active dimension (absent = 64),
      3. the base64 blob decodes and is exactly ``dimension * 4`` bytes,
      4. every unpacked value is finite and the vector satisfies the
         descriptor's normalization policy (``l2`` -> unit norm within
         float32 tolerance; ``none`` -> no norm constraint).

    Otherwise the caller re-embeds from ``fact_text``/``fact_summary`` under
    the ACTIVE embedder (stamped with its current key — never relabeled), or
    reports the vector missing when no usable text exists. An embedder that
    raises reports ``failed``: the canonical record still loads without a
    vector; an explicitly requested reindex surfaces the provider error.

Interface:
    compatible_embedding_blob(record, *, descriptor=None) -> bytes | None
    resolve_embedding(record, *, embedder=None) -> (blob, model, dim, status)
        where status is "reused" | "rebuilt" | "missing" | "failed"

Deps: hotmem.embed protocol + hash implementation.
Extension: optional/hosted adapters implement the same Embedder protocol;
    the compatibility rules here remain the only place to change.
"""

from __future__ import annotations

import base64
import binascii
import math
import struct
from typing import Literal

from hotmem.embed import (
    DEFAULT_EMBEDDER,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    Embedder,
    EmbeddingDescriptor,
    pack_embedding,
)
from hotmem.trace import get_tracer

_trace = get_tracer("interchange.compat")

EmbeddingStatus = Literal["reused", "rebuilt", "missing", "failed"]

_L2_NORM_TOLERANCE = 1e-3


def _stored_identity(record: dict) -> tuple[str, int] | None:
    """Recorded (descriptor key, dimension) with the legacy default mapping.

    Absent/empty fields keep interchange-v1 §5's documented hash semantics:
    missing model = ``hotmem-hash-v1``; missing dimension = 64. Malformed
    values (non-integer dimension) are incompatible, not an error.
    """
    model = str(record.get("embedding_model") or "") or EMBEDDING_MODEL
    try:
        dim = int(record.get("embedding_dim") or EMBEDDING_DIM)
    except (TypeError, ValueError):
        return None
    return model, dim


def _vector_is_valid(blob: bytes, descriptor: EmbeddingDescriptor) -> bool:
    """Finite values plus the descriptor's normalization policy (issue #78)."""
    if len(blob) % 4 or not blob:
        return False
    vec = struct.unpack(f"{len(blob) // 4}f", blob)
    if not all(math.isfinite(x) for x in vec):
        return False
    if descriptor.normalization == "l2":
        norm = math.sqrt(sum(x * x for x in vec))
        if abs(norm - 1.0) > _L2_NORM_TOLERANCE:
            return False
    return True


def compatible_embedding_blob(
    record: dict, *, descriptor: EmbeddingDescriptor | None = None
) -> bytes | None:
    """Return a compatible stored embedding from a record dict, or None.

    Accepts both spellings — ``embedding`` (Snapshot v2 / interchange) and
    ``embedding_b64`` (legacy swap) — normalizing the readers' historic
    field-name drift (issue #67). ``descriptor`` defaults to the hash
    embedder's; pass the active runtime's descriptor for semantic spaces.
    """
    active = descriptor if descriptor is not None else DEFAULT_EMBEDDER.descriptor

    identity = _stored_identity(record)
    if identity is None:
        return None
    stored_model, stored_dim = identity
    if stored_model != active.key:
        return None
    if stored_dim != active.dimension:
        return None

    encoded = record.get("embedding") or record.get("embedding_b64")
    if not encoded:
        return None

    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError):
        return None

    if len(blob) != active.dimension * 4:
        return None
    if not _vector_is_valid(blob, active):
        return None
    return blob


def resolve_embedding(
    record: dict,
    *,
    embedder: Embedder | None = None,
) -> tuple[bytes, str, int, EmbeddingStatus]:
    """Apply interchange-v1 §5 and return ``(blob, model, dim, status)``.

    - Compatible stored embedding -> reused as-is (recorded key/dim).
    - Incompatible with usable canonical text -> rebuilt under the ACTIVE
      embedder and stamped with its current key/dimension (#78: re-embedded
      rows carry the current model, never the stale recorded one).
    - File-backed/no usable text -> NULL-embedding convention (#38): empty
      blob and key — a predictable, rebuildable state (``missing``).
    - Embedder raises -> ``failed`` with the NULL-embedding shape: the
      canonical record still loads; an explicitly requested reindex must
      surface the provider error instead of swallowing it.

    ``embedder`` is call-time resolved (``None`` -> ``DEFAULT_EMBEDDER``) so
    every resolution site — swap hydration, package restore, snapshot v2
    reader, delta apply — observes the runtime-owned embedder without
    def-time binding; tests and benches may observe it by swapping the
    module default or passing an instance explicitly.
    """
    active = embedder if embedder is not None else DEFAULT_EMBEDDER
    descriptor = active.descriptor

    blob = compatible_embedding_blob(record, descriptor=descriptor)
    if blob is not None:
        model, dim = _stored_identity(record) or (EMBEDDING_MODEL, EMBEDDING_DIM)
        return blob, model, dim, "reused"

    if record.get("memory_type") == "file":
        text = record.get("fact_summary") or ""
    else:
        # Inline records re-embed from fact_text; a summary is the only
        # usable text when fact_text is absent (#69 predictable reporting).
        text = record.get("fact_text") or record.get("fact_summary") or ""

    if not text:
        return b"", "", descriptor.dimension, "missing"

    try:
        vec = active.embed(text)
    except Exception as err:
        _trace.warn(
            "resolve",
            "embedder failed; loading record without a vector",
            detail={"error": type(err).__name__},
        )
        return b"", "", descriptor.dimension, "failed"
    return pack_embedding(vec), descriptor.key, descriptor.dimension, "rebuilt"
