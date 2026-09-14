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
from typing import Any

from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL


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
