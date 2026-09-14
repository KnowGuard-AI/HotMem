"""Embedding compatibility rules shared by every reader (interchange-v1 §5).

Purpose:
     One predicate deciding whether a stored embedding may be reused as-is.
     Legacy swap already enforced this (``swap._stored_embedding``); Snapshot
     v2 did not — this module is the single rule both now call.

Rules — a stored embedding is reused iff ALL hold:
      1. ``embedding_model`` matches the reader's current model
         (``hotmem-hash-v1``; absent field treated as a match, legacy default),
      2. ``embedding_dim`` matches the current dimension (64; absent = match),
      3. the base64 blob decodes and is exactly ``embedding_dim * 4`` bytes.

Otherwise the caller re-embeds from ``fact_text``/``fact_summary``, or counts
the record invalid when no usable text exists.

Interface:
      compatible_embedding_blob(record) -> bytes | None

Deps: hotmem.embed constants only.
Extension: when a real embedding model lands, the model/dim equality rules
      here are the only place to change.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import Any

from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding


def compatible_embedding_blob(record: dict[str, Any]) -> bytes | None:
    """Return a compatible stored embedding from a record dict, or None.

    Accepts both spellings — ``embedding`` (Snapshot v2 / interchange) and
    ``embedding_b64`` (legacy swap) — normalizing the readers' historic
    field-name drift (issue #67).
    """
    if record.get("embedding_dim", EMBEDDING_DIM) != EMBEDDING_DIM:
        return None
    if record.get("embedding_model", EMBEDDING_MODEL) != EMBEDDING_MODEL:
        return None

    encoded = record.get("embedding") or record.get("embedding_b64")
    if not encoded:
        return None

    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError):
        return None

    if len(blob) != EMBEDDING_DIM * 4:
        return None
    return blob


def resolve_embedding(
    record: dict[str, Any],
    *,
    embed_fn: Callable[[str], list[float]] = embed_text,
    pack_fn: Callable[[list[float]], bytes] = pack_embedding,
) -> tuple[bytes, str, int, bool]:
    """Apply interchange-v1 §5 and return ``(blob, model, dim, reused)``.

    - Compatible stored embedding → reused as-is (recorded model/dim).
    - Incompatible → re-embed from ``fact_text`` (inline) or ``fact_summary``
      (file-backed), stamped with the CURRENT model/dim.
    - File-backed with no summary → NULL-embedding convention (#38):
      empty blob, empty model — a predictable, rebuildable state.

    ``embed_fn`` is injected so callers' monkeypatches keep working.
    """
    blob = compatible_embedding_blob(record)
    if blob is not None:
        model = record.get("embedding_model") or EMBEDDING_MODEL
        dim = record.get("embedding_dim") or EMBEDDING_DIM
        return blob, str(model), int(dim), True

    if record.get("memory_type") == "file":
        text = record.get("fact_summary") or ""
    else:
        text = record.get("fact_text") or ""

    if text:
        return pack_fn(embed_fn(text)), EMBEDDING_MODEL, EMBEDDING_DIM, False
    return b"", "", EMBEDDING_DIM, False
