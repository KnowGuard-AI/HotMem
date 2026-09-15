"""Interchange record normalization and validation.

Purpose:
     Accept a raw record dict from any HotMem stream dialect (legacy swap
     JSONL, Snapshot v2 memories.jsonl, hotmem-interchange-v1) and produce
     one canonical dict, so readers share field preservation and validation
     instead of drifting (issue #67). Unknown top-level keys are preserved
     under ``metadata["_interchange_unknown"]`` — nothing is silently dropped.

Dialects handled:
      - legacy swap: ``embedding_b64``, ``metadata_json``/``provenance_json``
        (JSON strings), ``tags`` (JSON string), v2 row columns.
      - Snapshot v2 / interchange: ``embedding``, ``metadata``, ``provenance``
        (objects), ``tags`` (list).

Interface:
      normalize_record(raw, *, default_source="swap") -> dict
      validate_record(record) -> list[str]   (empty list = hydratable)

Deps: stdlib + hotmem.interchange.canonical only.
Extension: hydration paths decide policy (skip-and-count vs hard fail) on
      top of validate_record; see swap.py / snapshot.reader / interchange.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from hotmem.annotations import validate_metadata
from hotmem.interchange.canonical import compute_content_hash

# Every key any writer dialect has ever emitted. Anything else is an unknown
# producer field (forward-compat) and is preserved via metadata.
KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "id",
        "identifier",
        "fact_text",
        "fact_summary",
        "memory_type",
        "embedding",
        "embedding_b64",
        "embedding_dim",
        "embedding_model",
        "source",
        "importance",
        "metadata",
        "metadata_json",
        "content_hash",
        "ttl_seconds",
        "created_at",
        "namespace",
        "tier",
        "tags",
        "related_memories",
        "parent_memory",
        "source_uri",
        "source_format",
        "source_checksum",
        "byte_offset",
        "byte_length",
        "updated_at",
        "snapshot_id",
        "promotion_state",
        "promotion_candidate",
        "provenance",
        "provenance_json",
    }
)

_UNKNOWN_KEY = "_interchange_unknown"


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_json(value: Any, fallback: Any) -> Any:
    """Parse a JSON string field; pass through objects; fallback on error."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    if value is None:
        return fallback
    return value


def normalize_record(raw: dict[str, Any], *, default_source: str = "swap") -> dict[str, Any]:
    """Normalize any record dialect into the canonical field set.

    Returns a JSON-native dict with canonical keys (see interchange-v1 §1):
    ``embedding`` carries the base64 blob regardless of input spelling,
    ``metadata``/``provenance``/``tags``/``related_memories`` are objects or
    lists, and unknown top-level keys are merged into
    ``metadata["_interchange_unknown"]``.

    The reserved ``metadata.annotations`` envelope (#79) is structurally
    validated here: malformed known structure raises
    ``AnnotationValidationError`` (callers count the record invalid); local
    evidence references are checked structurally — full resolution against
    a package/target id set happens in the hydrate paths.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"record must be a JSON object, got {type(raw).__name__}")

    identifier = _as_str(raw.get("identifier"))
    fact_text = _as_str(raw.get("fact_text"))

    content_hash = raw.get("content_hash")
    if not content_hash:
        content_hash = compute_content_hash(identifier, fact_text)

    memory_type = "file" if raw.get("memory_type") == "file" else "fact"

    embedding = raw.get("embedding") or raw.get("embedding_b64") or None

    metadata = _as_json(raw.get("metadata", raw.get("metadata_json")), {})
    if not isinstance(metadata, dict):
        metadata = {"value": metadata}

    unknown = {k: v for k, v in raw.items() if k not in KNOWN_FIELDS}
    if unknown:
        preserved = metadata.get(_UNKNOWN_KEY)
        if not isinstance(preserved, dict):
            preserved = {}
        metadata = {**metadata, _UNKNOWN_KEY: {**preserved, **unknown}}

    validate_metadata(metadata)  # structural envelope check (#79)

    provenance = raw.get("provenance", raw.get("provenance_json"))
    if provenance is not None:
        provenance = _as_json(provenance, None)
        if provenance is not None and not isinstance(provenance, dict):
            provenance = {"value": provenance}

    tags = _as_json(raw.get("tags"), [])
    if not isinstance(tags, list):
        tags = []

    related = _as_json(raw.get("related_memories"), [])
    if not isinstance(related, list):
        related = []

    return {
        "schema_version": _as_int(raw.get("schema_version")) or 1,
        "id": _as_str(raw.get("id")) or uuid.uuid4().hex,
        "identifier": identifier,
        "fact_text": fact_text,
        "fact_summary": raw.get("fact_summary"),
        "memory_type": memory_type,
        "embedding": embedding,
        "embedding_dim": _as_int(raw.get("embedding_dim")),
        "embedding_model": raw.get("embedding_model"),
        "source": _as_str(raw.get("source"), default_source),
        "importance": _as_float(raw.get("importance"), 0.5),
        "metadata": metadata,
        "content_hash": _as_str(content_hash),
        "ttl_seconds": _as_int(raw.get("ttl_seconds")),
        "created_at": raw.get("created_at"),
        "namespace": _as_str(raw.get("namespace")),
        "tier": _as_str(raw.get("tier"), "hot"),
        "tags": tags,
        "related_memories": related,
        "parent_memory": _as_str(raw.get("parent_memory")),
        "source_uri": _as_str(raw.get("source_uri")),
        "source_format": _as_str(raw.get("source_format")),
        "source_checksum": raw.get("source_checksum"),
        "byte_offset": _as_int(raw.get("byte_offset")),
        "byte_length": _as_int(raw.get("byte_length")),
        "updated_at": raw.get("updated_at"),
        "snapshot_id": _as_str(raw.get("snapshot_id")),
        "promotion_state": raw.get("promotion_state"),
        "promotion_candidate": raw.get("promotion_candidate"),
        "provenance": provenance,
    }


def validate_record(record: dict[str, Any]) -> list[str]:
    """Structural validation of a normalized record (interchange-v1 §7).

    Returns a list of problems; an empty list means the record is
    hydratable. ``invalid`` counting during hydration = non-empty list, or a
    missing compatible embedding with no text to re-embed from (the caller
    combines this with the embedding compatibility check).
    """
    issues: list[str] = []
    if not record.get("identifier") and not record.get("fact_text"):
        issues.append("record has neither identifier nor fact_text")

    if record.get("memory_type") == "file":
        if not record.get("source_uri"):
            issues.append("file-backed record is missing source_uri")
        if record.get("byte_length") is None:
            issues.append("file-backed record is missing byte_length")
    else:
        if not record.get("fact_text") and not record.get("fact_summary"):
            issues.append("inline record has no fact_text and no fact_summary")

    if not record.get("content_hash"):
        issues.append("record is missing content_hash")
    return issues
