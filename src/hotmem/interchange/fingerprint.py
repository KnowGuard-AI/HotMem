"""Canonical state fingerprints for sync divergence detection (#73).

Purpose:
     The verified clone (#69) proves content identity via content_hash /
     logical_id — but those cover only identifier+fact_text. Incremental
     sync must detect EVERY sync-relevant mutation: namespace retags,
     importance, TTL edits, promotion transitions, provenance updates,
     metadata changes. This module defines the canonical per-record and
     collection fingerprints used as delta preconditions (compare-and-swap)
     and divergence evidence.

     Fingerprint = SHA-256 over the canonical sorted JSON of sync-relevant
     fields, computed on the NORMALIZED record (interchange.record) so a raw
     DB row (metadata_json string, tags JSON string) and its normalized
     interchange record fingerprint identically — the producer fingerprints
     normalized records, the applier fingerprints raw rows, and the CAS
     preconditions must agree.

     Exclusions are part of the versioned contract (FINGERPRINT_VERSION) —
     see docs/okf/delta-v1.md:
       - embedding blob/dim/model: derived and rebuildable (interchange §5);
         model/dim changes are caught by the manifest compatibility block.
       - updated_at: runtime bookkeeping, not source-originating state.
       - snapshot_id: export-time provenance, not memory state.
       - id: identity, carried alongside the fingerprint, never inside it.
     parent_memory/related_memories are excluded pending contract review
     (ADR-003); adding them is a FINGERPRINT_VERSION bump.

Interface:
      record_fingerprint(row) -> str
      state_fingerprint(rows) -> str
      FINGERPRINT_VERSION

Deps: hotmem.interchange.canonical, hotmem.interchange.record. No db
      imports — usable from producers, appliers, and tests without cycles.
Extension: delta preconditions and checkpoints (interchange.delta, #73).
"""

from __future__ import annotations

import json
from typing import Any

from hotmem.interchange.canonical import sha256_bytes
from hotmem.interchange.record import normalize_record

# Bump when the field set or semantics change; stored in delta manifests so
# a receiver can reject fingerprints it cannot interpret.
FINGERPRINT_VERSION = 1

# Sync-relevant canonical fields (JSON-native after normalization).
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "identifier",
    "fact_text",
    "fact_summary",
    "memory_type",
    "source",
    "importance",
    "metadata",
    "content_hash",
    "ttl_seconds",
    "created_at",
    "namespace",
    "tier",
    "tags",
    "source_uri",
    "source_format",
    "source_checksum",
    "byte_offset",
    "byte_length",
    "provenance",
    "promotion_state",
    "promotion_candidate",
)


def record_fingerprint(row: dict[str, Any]) -> str:
    """SHA-256 over the canonical sorted JSON of sync-relevant fields.

    Accepts a raw DB row dict (from all_rows/iter_rows) or a normalized
    interchange record — both normalize to the same canonical state, so
    fingerprints agree across the producer/applier boundary. Unknown keys
    are ignored; the record id is never part of the fingerprint.
    """
    normalized = normalize_record(row)
    state = {field: normalized.get(field) for field in _FINGERPRINT_FIELDS}
    canonical = json.dumps(
        state,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )
    return sha256_bytes(f"v{FINGERPRINT_VERSION}:{canonical}".encode())


def state_fingerprint(rows: list[dict[str, Any]] | tuple) -> str:
    """SHA-256 over the sorted per-record fingerprint concatenation.

    Order-independent collection identity of the sync-relevant state —
    same shape as the clone's logical_id, but covering every mutable
    field. Equal for equivalent instances regardless of row order.
    """
    fingerprints = sorted(record_fingerprint(row) for row in rows)
    return sha256_bytes("".join(fingerprints).encode())
