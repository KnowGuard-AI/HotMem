"""Canonical serialization and digest primitives for the interchange contract.

Purpose:
     Single source of truth for the byte-level contract (interchange-v1 §2–§3):
     canonical JSON serialization, per-record content hashes, the logical
     identity aggregate, and streaming file digests. Every writer/reader that
     needs byte-stable output or identity derives from here.

Interface:
      canonical_dumps(record) -> str
      canonical_line(record) -> str
      compute_content_hash(identifier, fact_text) -> str
      logical_id(content_hashes) -> str
      sha256_bytes(data) -> str
      sha256_file(path) -> str

Deps: stdlib only. No hotmem imports — snapshot/swap import FROM this module,
      never the reverse (import-cycle safety).
Extension: package manifest digests reuse these helpers (interchange.package).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_dumps(record: dict[str, Any]) -> str:
    """Serialize one record to its canonical JSON form (interchange-v1 §2).

    Deterministic: sorted keys, compact separators, UTF-8 text (ensure_ascii
    off), NaN rejected, non-JSON scalars coerced via str. New artifacts use
    this form; readers accept historical spacings because digests always
    cover the bytes actually written.
    """
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def canonical_line(record: dict[str, Any]) -> str:
    """One canonical JSONL line (canonical form + trailing newline)."""
    return canonical_dumps(record) + "\n"


def compute_content_hash(identifier: str, fact_text: str) -> str:
    """SHA-256 of identifier + fact_text — the per-record logical identity.

    Canonical, portable, and stable across HotMem versions. Two records with
    the same hash are the same memory; hydration deduplicates on it.
    """
    return hashlib.sha256(f"{identifier}:{fact_text}".encode()).hexdigest()


def logical_id(content_hashes: list[str]) -> str:
    """Deterministic logical identity of a record set (interchange-v1 §3).

    SHA-256 over the sorted content_hash concatenation: equal for equivalent
    contents regardless of record order, compression, export time, or
    embeddings. Same algorithm as Snapshot v2's ``snapshot_id``.
    """
    return sha256_bytes("".join(sorted(content_hashes)).encode())


def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file's full contents (streaming, 64 KiB)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
