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

import base64
import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

from hotmem.annotations import validate_metadata
from hotmem.interchange.canonical import compute_content_hash, compute_file_reference_hash

VALID_DISPOSITIONS: tuple[str, ...] = ("preserve", "regenerate", "exclude")
VALID_TIERS: frozenset[str] = frozenset({"hot", "warm", "cold"})
VALID_PROMOTION_STATES: frozenset[str] = frozenset(
    {"HOT", "WARM", "READY", "PROMOTED", "COLD", "ARCHIVED"}
)
VALID_MEMORY_TYPES: frozenset[str] = frozenset({"fact", "inline", "file"})


@dataclass(frozen=True)
class FieldDisposition:
    """Portability contract for one column of the SQLite ``memories`` table (#99)."""

    column: str
    disposition: str
    snapshot_v2_field: str
    interchange_v1_field: str
    legacy_jsonl_field: str
    legacy_default: Any
    rationale: str


MEMORY_FIELD_INVENTORY: dict[str, FieldDisposition] = {
    "id": FieldDisposition(
        column="id",
        disposition="preserve",
        snapshot_v2_field="id",
        interchange_v1_field="id",
        legacy_jsonl_field="id",
        legacy_default="<generated-uuid4-hex>",
        rationale=(
            "Preserves primary memory ID and parent/related memory references across "
            "clean-runtime restores; generated via uuid4().hex only when omitted in v0.1 JSONL."
        ),
    ),
    "identifier": FieldDisposition(
        column="identifier",
        disposition="preserve",
        snapshot_v2_field="identifier",
        interchange_v1_field="identifier",
        legacy_jsonl_field="identifier",
        legacy_default="",
        rationale=(
            "Canonical subject/entity key; participates in inline and file-reference "
            "content_hash calculation (#100)."
        ),
    ),
    "fact_text": FieldDisposition(
        column="fact_text",
        disposition="preserve",
        snapshot_v2_field="fact_text",
        interchange_v1_field="fact_text",
        legacy_jsonl_field="fact_text",
        legacy_default="",
        rationale=(
            "Canonical inline memory text; empty string '' for file-backed records "
            "under reference-not-duplicate (#38)."
        ),
    ),
    "embedding": FieldDisposition(
        column="embedding",
        disposition="regenerate",
        snapshot_v2_field="embedding",
        interchange_v1_field="embedding",
        legacy_jsonl_field="embedding_b64",
        legacy_default=b"",
        rationale=(
            "Runtime-derived float32 vector blob: reused without recomputation when "
            "(embedding_model, embedding_dim) matches the active descriptor and the vector "
            "is finite and unit-norm (#78); rebuilt from fact_text/fact_summary otherwise."
        ),
    ),
    "embedding_dim": FieldDisposition(
        column="embedding_dim",
        disposition="regenerate",
        snapshot_v2_field="embedding_dim",
        interchange_v1_field="embedding_dim",
        legacy_jsonl_field="embedding_dim",
        legacy_default=64,
        rationale=(
            "Runtime-derived vector dimension paired with embedding_model: preserved when "
            "a compatible stored vector is reused and restamped to the active descriptor "
            "dimension when rebuilt."
        ),
    ),
    "embedding_model": FieldDisposition(
        column="embedding_model",
        disposition="regenerate",
        snapshot_v2_field="embedding_model",
        interchange_v1_field="embedding_model",
        legacy_jsonl_field="embedding_model",
        legacy_default="hotmem-hash-v1",
        rationale=(
            "Runtime-derived embedding descriptor key (#78): preserved when a compatible "
            "stored vector is reused (normalizing legacy '' on 64-d hash vectors to "
            "'hotmem-hash-v1') and restamped when rebuilt."
        ),
    ),
    "source": FieldDisposition(
        column="source",
        disposition="preserve",
        snapshot_v2_field="source",
        interchange_v1_field="source",
        legacy_jsonl_field="source",
        legacy_default="<reader-default: swap|snapshot|interchange>",
        rationale=(
            "Producer/origin attribution string; explicit '' is preserved, whereas "
            "missing/null in legacy records defaults to the reader's format label."
        ),
    ),
    "importance": FieldDisposition(
        column="importance",
        disposition="preserve",
        snapshot_v2_field="importance",
        interchange_v1_field="importance",
        legacy_jsonl_field="importance",
        legacy_default=0.5,
        rationale=(
            "Retrieval weight in [0.0, 1.0]; explicit 0.0 is preserved and distinguished "
            "from missing/null (which defaults to 0.5)."
        ),
    ),
    "metadata_json": FieldDisposition(
        column="metadata_json",
        disposition="preserve",
        snapshot_v2_field="metadata",
        interchange_v1_field="metadata",
        legacy_jsonl_field="metadata_json",
        legacy_default="{}",
        rationale=(
            "Caller metadata and annotations envelope (#74) stored as canonical sorted-key "
            "JSON; unknown top-level record keys are preserved under metadata._interchange_unknown."
        ),
    ),
    "content_hash": FieldDisposition(
        column="content_hash",
        disposition="preserve",
        snapshot_v2_field="content_hash",
        interchange_v1_field="content_hash",
        legacy_jsonl_field="content_hash",
        legacy_default="<computed-sha256>",
        rationale=(
            "Logical digest paired with namespace for (namespace, content_hash) identity (#100); "
            "preserved when present, computed when missing/blank, and updated only when "
            "host-path redaction rewrites an exported file source_uri."
        ),
    ),
    "ttl_seconds": FieldDisposition(
        column="ttl_seconds",
        disposition="preserve",
        snapshot_v2_field="ttl_seconds",
        interchange_v1_field="ttl_seconds",
        legacy_jsonl_field="ttl_seconds",
        legacy_default=None,
        rationale=(
            "Optional positive integer TTL in seconds relative to created_at; None means "
            "non-expiring and is preserved across export and restore."
        ),
    ),
    "created_at": FieldDisposition(
        column="created_at",
        disposition="preserve",
        snapshot_v2_field="created_at",
        interchange_v1_field="created_at",
        legacy_jsonl_field="created_at",
        legacy_default="<sqlite-utc-now>",
        rationale=(
            "Creation UTC ISO-8601 timestamp; preserved verbatim for auditability and TTL "
            "evaluation, or defaulted at insert time only when absent in v0.1 records."
        ),
    ),
    "namespace": FieldDisposition(
        column="namespace",
        disposition="preserve",
        snapshot_v2_field="namespace",
        interchange_v1_field="namespace",
        legacy_jsonl_field="namespace",
        legacy_default="",
        rationale=(
            "Scope partition (#100/#110); preserved verbatim unless an unscoped record ('') "
            "is hydrated with an explicit target_namespace."
        ),
    ),
    "tier": FieldDisposition(
        column="tier",
        disposition="preserve",
        snapshot_v2_field="tier",
        interchange_v1_field="tier",
        legacy_jsonl_field="tier",
        legacy_default="hot",
        rationale=(
            "Storage/retrieval tier ('hot', 'warm', 'cold'); preserved across export and "
            "restore, defaulting to 'hot' when absent in legacy records."
        ),
    ),
    "memory_type": FieldDisposition(
        column="memory_type",
        disposition="preserve",
        snapshot_v2_field="memory_type",
        interchange_v1_field="memory_type",
        legacy_jsonl_field="memory_type",
        legacy_default="fact",
        rationale=(
            "Record kind ('fact' for inline, 'file' for file-backed; legacy alias 'inline' "
            "normalizes to 'fact')."
        ),
    ),
    "source_uri": FieldDisposition(
        column="source_uri",
        disposition="preserve",
        snapshot_v2_field="source_uri",
        interchange_v1_field="source_uri",
        legacy_jsonl_field="source_uri",
        legacy_default="",
        rationale=(
            "Backing file path or provenance URI; preserved verbatim (or sanitized to a "
            "base_dir-relative path when policy redact_host_paths is active)."
        ),
    ),
    "source_format": FieldDisposition(
        column="source_format",
        disposition="preserve",
        snapshot_v2_field="source_format",
        interchange_v1_field="source_format",
        legacy_jsonl_field="source_format",
        legacy_default="",
        rationale=(
            "Backing file format ('csv', 'parquet', 'json', 'bin', etc.) used by hydration "
            "profiles; preserved verbatim."
        ),
    ),
    "source_checksum": FieldDisposition(
        column="source_checksum",
        disposition="preserve",
        snapshot_v2_field="source_checksum",
        interchange_v1_field="source_checksum",
        legacy_jsonl_field="source_checksum",
        legacy_default="",
        rationale=(
            "SHA-256 digest of the referenced byte slice (#38/#100); preserved as byte-integrity "
            "evidence independent of logical reference identity."
        ),
    ),
    "byte_offset": FieldDisposition(
        column="byte_offset",
        disposition="preserve",
        snapshot_v2_field="byte_offset",
        interchange_v1_field="byte_offset",
        legacy_jsonl_field="byte_offset",
        legacy_default=None,
        rationale=(
            "Non-negative starting byte offset for file-backed slices; explicit 0 is preserved "
            "as 0 and distinguished from inline None."
        ),
    ),
    "byte_length": FieldDisposition(
        column="byte_length",
        disposition="preserve",
        snapshot_v2_field="byte_length",
        interchange_v1_field="byte_length",
        legacy_jsonl_field="byte_length",
        legacy_default=None,
        rationale=(
            "Byte length for file-backed slices; required non-negative integer for "
            "memory_type='file' (0 is an empty slice) and None for inline records."
        ),
    ),
    "updated_at": FieldDisposition(
        column="updated_at",
        disposition="preserve",
        snapshot_v2_field="updated_at",
        interchange_v1_field="updated_at",
        legacy_jsonl_field="updated_at",
        legacy_default=None,
        rationale=(
            "Last lifecycle or mutation UTC timestamp; preserved when present and None when "
            "never updated or absent in older packages."
        ),
    ),
    "snapshot_id": FieldDisposition(
        column="snapshot_id",
        disposition="preserve",
        snapshot_v2_field="snapshot_id",
        interchange_v1_field="snapshot_id",
        legacy_jsonl_field="snapshot_id",
        legacy_default="",
        rationale=(
            "Snapshot lineage identifier; preserved across export and restore, defaulting to "
            "'' when absent."
        ),
    ),
    "promotion_state": FieldDisposition(
        column="promotion_state",
        disposition="preserve",
        snapshot_v2_field="promotion_state",
        interchange_v1_field="promotion_state",
        legacy_jsonl_field="promotion_state",
        legacy_default="HOT",
        rationale=(
            "Lifecycle access state ('HOT', 'WARM', 'READY', 'PROMOTED', 'COLD', 'ARCHIVED'); "
            "preserved so promoted or archived records are never silently reset to 'HOT'."
        ),
    ),
    "promotion_candidate": FieldDisposition(
        column="promotion_candidate",
        disposition="preserve",
        snapshot_v2_field="promotion_candidate",
        interchange_v1_field="promotion_candidate",
        legacy_jsonl_field="promotion_candidate",
        legacy_default=0,
        rationale=(
            "Candidate review flag (0 or 1); preserved across export and restore and defaults "
            "to 0 when absent."
        ),
    ),
    "parent_memory": FieldDisposition(
        column="parent_memory",
        disposition="preserve",
        snapshot_v2_field="parent_memory",
        interchange_v1_field="parent_memory",
        legacy_jsonl_field="parent_memory",
        legacy_default="",
        rationale=(
            "Parent memory ID for derivation lineage; preserved verbatim and defaults to '' "
            "when absent."
        ),
    ),
    "related_memories": FieldDisposition(
        column="related_memories",
        disposition="preserve",
        snapshot_v2_field="related_memories",
        interchange_v1_field="related_memories",
        legacy_jsonl_field="related_memories",
        legacy_default="[]",
        rationale=(
            "Ordered list of related memory IDs; represented as a JSON array in v2/package "
            "and a JSON string in SQLite/legacy JSONL."
        ),
    ),
    "tags": FieldDisposition(
        column="tags",
        disposition="preserve",
        snapshot_v2_field="tags",
        interchange_v1_field="tags",
        legacy_jsonl_field="tags",
        legacy_default="[]",
        rationale=(
            "Ordered list of attribution/categorization tags (#111); preserved without "
            "granting scope access or becoming implicit filters."
        ),
    ),
    "schema_version": FieldDisposition(
        column="schema_version",
        disposition="preserve",
        snapshot_v2_field="record_schema_version",
        interchange_v1_field="record_schema_version",
        legacy_jsonl_field="schema_version",
        legacy_default=1,
        rationale=(
            "Row-level Memory Record schema version; carried additively via "
            "record_schema_version in envelope-versioned formats (Snapshot v2 and "
            "Interchange v1) so envelope version and row schema_version never collide."
        ),
    ),
    "fact_summary": FieldDisposition(
        column="fact_summary",
        disposition="preserve",
        snapshot_v2_field="fact_summary",
        interchange_v1_field="fact_summary",
        legacy_jsonl_field="fact_summary",
        legacy_default=None,
        rationale=(
            "Optional short summary used for file-backed embedding and search (#38); "
            "preserved as string or None."
        ),
    ),
    "provenance_json": FieldDisposition(
        column="provenance_json",
        disposition="preserve",
        snapshot_v2_field="provenance",
        interchange_v1_field="provenance",
        legacy_jsonl_field="provenance_json",
        legacy_default=None,
        rationale=(
            "Structured provenance and session/harness attribution envelope (#111) stored as "
            "canonical sorted-key JSON or None when absent."
        ),
    ),
}


class _NormalizedRecord(dict[str, Any]):
    """Dict subclass carrying non-disclosing validation issues from normalization."""

    __slots__ = ("validation_issues",)

    def __init__(
        self,
        mapping: dict[str, Any],
        *,
        validation_issues: list[str] | None = None,
    ) -> None:
        super().__init__(mapping)
        self.validation_issues: list[str] = list(validation_issues or ())


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
        "record_schema_version",
    }
)

_UNKNOWN_KEY = "_interchange_unknown"


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        val = float(value)
        return val if math.isfinite(val) else default
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


def _append_issue(issues: list[str], message: str) -> None:
    if message not in issues:
        issues.append(message)


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

    validation_issues: list[str] = []

    for str_field in (
        "id",
        "identifier",
        "fact_text",
        "fact_summary",
        "source",
        "namespace",
        "parent_memory",
        "source_uri",
        "source_format",
        "source_checksum",
        "created_at",
        "updated_at",
        "snapshot_id",
        "embedding_model",
    ):
        if str_field in raw and raw[str_field] is not None and not isinstance(raw[str_field], str):
            _append_issue(validation_issues, f"invalid {str_field}: expected string")

    identifier = _as_str(raw.get("identifier"))
    fact_text = _as_str(raw.get("fact_text"))

    raw_memory_type = raw.get("memory_type")
    if (
        raw_memory_type is not None
        and raw_memory_type != ""
        and (not isinstance(raw_memory_type, str) or raw_memory_type not in VALID_MEMORY_TYPES)
    ):
        _append_issue(
            validation_issues,
            "invalid memory_type: expected 'fact' or 'file'",
        )
    memory_type = "file" if raw_memory_type == "file" else "fact"

    raw_byte_offset = raw.get("byte_offset")
    byte_offset = _as_int(raw_byte_offset)
    if (
        raw_byte_offset is not None
        and raw_byte_offset != ""
        and (byte_offset is None or byte_offset < 0)
    ):
        _append_issue(
            validation_issues,
            "invalid byte_offset: expected non-negative integer",
        )

    raw_byte_length = raw.get("byte_length")
    byte_length = _as_int(raw_byte_length)
    if (
        raw_byte_length is not None
        and raw_byte_length != ""
        and (byte_length is None or byte_length < 0)
    ):
        _append_issue(
            validation_issues,
            "invalid byte_length: expected non-negative integer",
        )

    content_hash = _as_str(raw.get("content_hash")).strip()
    if not content_hash:
        if memory_type == "file":
            content_hash = compute_file_reference_hash(
                identifier,
                _as_str(raw.get("source_uri")),
                byte_offset if byte_offset is not None and byte_offset >= 0 else 0,
                byte_length if byte_length is not None and byte_length >= 0 else 0,
            )
        else:
            content_hash = compute_content_hash(identifier, fact_text)

    embedding = raw.get("embedding") or raw.get("embedding_b64") or None
    if isinstance(embedding, bytes | bytearray):
        # DB rows stream raw blobs (delta producer, iter_rows); the
        # interchange record form is base64 — a repr string here would be
        # silently incompatible forever (#78: same-space delta replays must
        # reuse their vectors, not rebuild on every apply).
        embedding = base64.b64encode(bytes(embedding)).decode("ascii")

    raw_meta = raw.get("metadata", raw.get("metadata_json"))
    if raw_meta is not None and raw_meta != "":
        if isinstance(raw_meta, str):
            try:
                parsed_meta = json.loads(raw_meta)
                if parsed_meta is not None and not isinstance(parsed_meta, dict):
                    _append_issue(validation_issues, "invalid metadata: expected JSON object")
            except json.JSONDecodeError:
                _append_issue(validation_issues, "invalid metadata: malformed JSON")
        elif not isinstance(raw_meta, dict):
            _append_issue(validation_issues, "invalid metadata: expected JSON object")

    metadata = _as_json(raw_meta, {})
    if not isinstance(metadata, dict):
        metadata = {"value": metadata}

    unknown = {k: v for k, v in raw.items() if k not in KNOWN_FIELDS}
    if unknown:
        preserved = metadata.get(_UNKNOWN_KEY)
        if not isinstance(preserved, dict):
            preserved = {}
        metadata = {**metadata, _UNKNOWN_KEY: {**preserved, **unknown}}

    validate_metadata(metadata)  # structural envelope check (#79)

    raw_prov = raw.get("provenance", raw.get("provenance_json"))
    provenance: Any = None
    if raw_prov is not None and raw_prov != "":
        if isinstance(raw_prov, str):
            try:
                parsed_prov = json.loads(raw_prov)
                if parsed_prov is not None and not isinstance(parsed_prov, dict):
                    _append_issue(
                        validation_issues,
                        "invalid provenance: expected JSON object or null",
                    )
                provenance = parsed_prov
            except json.JSONDecodeError:
                _append_issue(validation_issues, "invalid provenance: malformed JSON")
                provenance = None
        elif isinstance(raw_prov, dict):
            provenance = raw_prov
        else:
            _append_issue(
                validation_issues,
                "invalid provenance: expected JSON object or null",
            )
            provenance = {"value": raw_prov}

    for list_field in ("tags", "related_memories"):
        raw_list = raw.get(list_field)
        if raw_list is not None and raw_list != "":
            if isinstance(raw_list, str):
                try:
                    parsed_list = json.loads(raw_list)
                    if not isinstance(parsed_list, list) or not all(
                        isinstance(item, str) for item in parsed_list
                    ):
                        _append_issue(
                            validation_issues,
                            f"invalid {list_field}: expected list of strings",
                        )
                except json.JSONDecodeError:
                    _append_issue(
                        validation_issues,
                        f"invalid {list_field}: malformed JSON",
                    )
            elif not isinstance(raw_list, list) or not all(
                isinstance(item, str) for item in raw_list
            ):
                _append_issue(
                    validation_issues,
                    f"invalid {list_field}: expected list of strings",
                )

    tags = _as_json(raw.get("tags"), [])
    if not isinstance(tags, list):
        tags = []

    related = _as_json(raw.get("related_memories"), [])
    if not isinstance(related, list):
        related = []

    raw_importance = raw.get("importance")
    if raw_importance is not None and raw_importance != "":
        if isinstance(raw_importance, bool):
            _append_issue(
                validation_issues,
                "invalid importance: expected finite float in [0.0, 1.0]",
            )
            importance = 0.5
        else:
            try:
                imp_val = float(raw_importance)
                if not math.isfinite(imp_val) or imp_val < 0.0 or imp_val > 1.0:
                    _append_issue(
                        validation_issues,
                        "invalid importance: expected finite float in [0.0, 1.0]",
                    )
                importance = imp_val if math.isfinite(imp_val) else 0.5
            except (TypeError, ValueError):
                _append_issue(
                    validation_issues,
                    "invalid importance: expected finite float in [0.0, 1.0]",
                )
                importance = 0.5
    else:
        importance = 0.5

    raw_ttl = raw.get("ttl_seconds")
    ttl_seconds = _as_int(raw_ttl)
    if raw_ttl is not None and raw_ttl != "" and (ttl_seconds is None or ttl_seconds <= 0):
        _append_issue(
            validation_issues,
            "invalid ttl_seconds: expected positive integer or null",
        )

    raw_tier = raw.get("tier")
    if raw_tier is not None and raw_tier != "":
        if not isinstance(raw_tier, str) or raw_tier not in VALID_TIERS:
            _append_issue(
                validation_issues,
                "invalid tier: expected one of 'hot', 'warm', 'cold'",
            )
        tier = _as_str(raw_tier, "hot")
    else:
        tier = "hot"

    raw_promotion_state = raw.get("promotion_state")
    if (
        raw_promotion_state is not None
        and raw_promotion_state != ""
        and (
            not isinstance(raw_promotion_state, str)
            or raw_promotion_state not in VALID_PROMOTION_STATES
        )
    ):
        _append_issue(
            validation_issues,
            (
                "invalid promotion_state: expected one of "
                "'HOT', 'WARM', 'READY', 'PROMOTED', 'COLD', 'ARCHIVED'"
            ),
        )

    raw_promotion_candidate = raw.get("promotion_candidate")
    if raw_promotion_candidate is not None and raw_promotion_candidate != "":
        if isinstance(raw_promotion_candidate, bool):
            promotion_candidate: Any = int(raw_promotion_candidate)
        else:
            pc_val = _as_int(raw_promotion_candidate)
            if pc_val not in (0, 1):
                _append_issue(
                    validation_issues,
                    "invalid promotion_candidate: expected 0 or 1",
                )
                promotion_candidate = raw_promotion_candidate
            else:
                promotion_candidate = pc_val
    else:
        promotion_candidate = raw_promotion_candidate

    for sv_key in ("schema_version", "record_schema_version"):
        raw_sv = raw.get(sv_key)
        if raw_sv is not None and raw_sv != "":
            sv_val = _as_int(raw_sv)
            if sv_val is None or sv_val <= 0:
                _append_issue(
                    validation_issues,
                    f"invalid {sv_key}: expected positive integer",
                )

    raw_dim = raw.get("embedding_dim")
    embedding_dim = _as_int(raw_dim)
    if raw_dim is not None and raw_dim != "" and (embedding_dim is None or embedding_dim <= 0):
        _append_issue(
            validation_issues,
            "invalid embedding_dim: expected positive integer",
        )

    return _NormalizedRecord(
        {
            "schema_version": _as_int(raw.get("schema_version")) or 1,
            "id": _as_str(raw.get("id")) or uuid.uuid4().hex,
            "identifier": identifier,
            "fact_text": fact_text,
            "fact_summary": raw.get("fact_summary"),
            "memory_type": memory_type,
            "embedding": embedding,
            "embedding_dim": embedding_dim,
            "embedding_model": raw.get("embedding_model"),
            "source": _as_str(raw.get("source"), default_source),
            "importance": importance,
            "metadata": metadata,
            "content_hash": _as_str(content_hash),
            "ttl_seconds": ttl_seconds,
            "created_at": raw.get("created_at"),
            "namespace": _as_str(raw.get("namespace")),
            "tier": tier,
            "tags": tags,
            "related_memories": related,
            "parent_memory": _as_str(raw.get("parent_memory")),
            "source_uri": _as_str(raw.get("source_uri")),
            "source_format": _as_str(raw.get("source_format")),
            "source_checksum": raw.get("source_checksum"),
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "updated_at": raw.get("updated_at"),
            "snapshot_id": _as_str(raw.get("snapshot_id")),
            "promotion_state": raw_promotion_state,
            "promotion_candidate": promotion_candidate,
            "provenance": provenance,
            "record_schema_version": _as_int(raw.get("record_schema_version")),
        },
        validation_issues=validation_issues,
    )


def validate_record(record: dict[str, Any]) -> list[str]:
    """Structural and domain validation of a normalized record (interchange-v1 section 7).

    Returns a list of problems; an empty list means the record is
    hydratable. Error messages identify the field and violated contract
    without echoing raw record values or protected content (#99/#110).
    """
    issues: list[str] = list(getattr(record, "validation_issues", ()))
    if not record.get("identifier") and not record.get("fact_text"):
        _append_issue(issues, "record has neither identifier nor fact_text")

    memory_type = record.get("memory_type", "fact")
    if memory_type not in VALID_MEMORY_TYPES:
        _append_issue(issues, "invalid memory_type: expected 'fact' or 'file'")

    if memory_type == "file":
        if not record.get("source_uri"):
            _append_issue(issues, "file-backed record is missing source_uri")
        if record.get("byte_length") is None:
            _append_issue(issues, "file-backed record is missing byte_length")
        elif (
            isinstance(record.get("byte_length"), bool)
            or not isinstance(record.get("byte_length"), int)
            or record["byte_length"] < 0
        ):
            _append_issue(
                issues,
                "invalid byte_length: expected non-negative integer for file record",
            )
        if record.get("byte_offset") is not None and (
            isinstance(record.get("byte_offset"), bool)
            or not isinstance(record.get("byte_offset"), int)
            or record["byte_offset"] < 0
        ):
            _append_issue(
                issues,
                "invalid byte_offset: expected non-negative integer",
            )
    else:
        if not record.get("fact_text") and not record.get("fact_summary"):
            _append_issue(issues, "inline record has no fact_text and no fact_summary")
        if record.get("byte_offset") is not None and (
            isinstance(record.get("byte_offset"), bool)
            or not isinstance(record.get("byte_offset"), int)
            or record["byte_offset"] < 0
        ):
            _append_issue(
                issues,
                "invalid byte_offset: expected non-negative integer",
            )
        if record.get("byte_length") is not None and (
            isinstance(record.get("byte_length"), bool)
            or not isinstance(record.get("byte_length"), int)
            or record["byte_length"] < 0
        ):
            _append_issue(
                issues,
                "invalid byte_length: expected non-negative integer",
            )

    importance = record.get("importance")
    if importance is not None and (
        isinstance(importance, bool)
        or not isinstance(importance, int | float)
        or not math.isfinite(float(importance))
        or float(importance) < 0.0
        or float(importance) > 1.0
    ):
        _append_issue(
            issues,
            "invalid importance: expected finite float in [0.0, 1.0]",
        )

    ttl_seconds = record.get("ttl_seconds")
    if ttl_seconds is not None and (
        isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0
    ):
        _append_issue(
            issues,
            "invalid ttl_seconds: expected positive integer or null",
        )

    tier = record.get("tier")
    if tier is not None and tier != "" and (not isinstance(tier, str) or tier not in VALID_TIERS):
        _append_issue(
            issues,
            "invalid tier: expected one of 'hot', 'warm', 'cold'",
        )

    promotion_state = record.get("promotion_state")
    if (
        promotion_state is not None
        and promotion_state != ""
        and (not isinstance(promotion_state, str) or promotion_state not in VALID_PROMOTION_STATES)
    ):
        _append_issue(
            issues,
            (
                "invalid promotion_state: expected one of "
                "'HOT', 'WARM', 'READY', 'PROMOTED', 'COLD', 'ARCHIVED'"
            ),
        )

    promotion_candidate = record.get("promotion_candidate")
    if (
        promotion_candidate is not None
        and promotion_candidate != ""
        and promotion_candidate not in (0, 1, False, True)
    ):
        _append_issue(
            issues,
            "invalid promotion_candidate: expected 0 or 1",
        )

    for sv_key in ("schema_version", "record_schema_version"):
        sv_val = record.get(sv_key)
        if sv_val is not None and (
            isinstance(sv_val, bool) or not isinstance(sv_val, int) or sv_val <= 0
        ):
            _append_issue(
                issues,
                f"invalid {sv_key}: expected positive integer",
            )

    metadata = record.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        _append_issue(issues, "invalid metadata: expected JSON object")

    provenance = record.get("provenance")
    if provenance is not None and not isinstance(provenance, dict):
        _append_issue(issues, "invalid provenance: expected JSON object or null")

    for list_field in ("tags", "related_memories"):
        list_val = record.get(list_field)
        if list_val is not None and (
            not isinstance(list_val, list) or not all(isinstance(item, str) for item in list_val)
        ):
            _append_issue(
                issues,
                f"invalid {list_field}: expected list of strings",
            )

    if not record.get("content_hash"):
        _append_issue(issues, "record is missing content_hash")
    return issues
