"""Interchange contract tests — normalization, validation, canonical bytes (#67).

Locks the shared reader primitives from docs/okf/interchange-v1.md:
field preservation across dialects, unknown-key preservation, canonical JSON
bytes, logical identity, and the embedding compatibility predicate.
"""

from __future__ import annotations

import base64
import json

import pytest

from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.interchange.canonical import (
    canonical_dumps,
    canonical_line,
    compute_content_hash,
    logical_id,
)
from hotmem.interchange.compat import compatible_embedding_blob
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.snapshot.format import compute_snapshot_id
from hotmem.swap import compute_content_hash as swap_compute_content_hash


def _blob_b64(text: str = "hello world") -> str:
    return base64.b64encode(pack_embedding(embed_text(text))).decode("ascii")


# ── compute_content_hash: single canonical definition ───────────────────────


def test_content_hash_matches_legacy_swap_definition():
    expected = swap_compute_content_hash("agent", "likes tea")
    assert compute_content_hash("agent", "likes tea") == expected


def test_logical_id_matches_snapshot_id_algorithm():
    hashes = [f"hash{i}" for i in range(5)]
    assert logical_id(hashes) == compute_snapshot_id(hashes)
    # Order-independent: equivalent contents, same logical identity.
    assert logical_id(list(reversed(hashes))) == logical_id(hashes)


# ── normalize_record: legacy swap dialect ───────────────────────────────────


def test_normalize_legacy_record_with_json_string_fields():
    raw = {
        "id": "abc",
        "identifier": "agent",
        "fact_text": "likes tea",
        "embedding_b64": _blob_b64(),
        "embedding_dim": EMBEDDING_DIM,
        "embedding_model": EMBEDDING_MODEL,
        "metadata_json": json.dumps({"k": 1}),
        "tags": '["tea", "prefs"]',
        "provenance_json": json.dumps({"origin": "test"}),
        "namespace": "team",
        "tier": "hot",
        "source_uri": "notes/a.md",
        "source_checksum": "deadbeef",
        "ttl_seconds": "300",
        "created_at": "2026-01-01T00:00:00Z",
        "byte_offset": "0",
        "byte_length": "9",
    }
    rec = normalize_record(raw)
    assert rec["id"] == "abc"
    assert rec["embedding"] == raw["embedding_b64"]
    assert rec["metadata"] == {"k": 1}
    assert rec["tags"] == ["tea", "prefs"]
    assert rec["provenance"] == {"origin": "test"}
    assert rec["ttl_seconds"] == 300
    assert rec["byte_offset"] == 0
    assert rec["byte_length"] == 9
    assert rec["content_hash"] == compute_content_hash("agent", "likes tea")
    assert rec["memory_type"] == "fact"


def test_normalize_v2_record_with_object_fields():
    raw = {
        "schema_version": 2,
        "id": "abc",
        "identifier": "agent",
        "fact_text": "likes tea",
        "embedding": _blob_b64(),
        "metadata": {"k": 1},
        "provenance": {"origin": "test"},
        "memory_type": "file",
        "source_uri": "notes/a.md",
        "byte_offset": 0,
        "byte_length": 9,
    }
    rec = normalize_record(raw)
    assert rec["embedding"] == raw["embedding"]
    assert rec["metadata"] == {"k": 1}
    assert rec["provenance"] == {"origin": "test"}
    assert rec["memory_type"] == "file"
    assert rec["schema_version"] == 2


def test_normalize_preserves_unknown_keys_in_metadata():
    raw = {"identifier": "a", "fact_text": "b", "future_field": {"x": 1}, "another": 7}
    rec = normalize_record(raw)
    assert rec["metadata"]["_interchange_unknown"] == {
        "future_field": {"x": 1},
        "another": 7,
    }
    # Round-trip stability: re-normalizing a normalized record keeps them.
    again = normalize_record({**raw, "metadata": rec["metadata"]})
    assert again["metadata"]["_interchange_unknown"] == rec["metadata"]["_interchange_unknown"]


def test_normalize_fills_id_and_content_hash():
    rec = normalize_record({"identifier": "agent", "fact_text": "likes tea"})
    assert rec["id"]
    assert rec["content_hash"] == compute_content_hash("agent", "likes tea")
    assert rec["source"] == "swap"
    assert rec["importance"] == 0.5


def test_normalize_coerces_bad_scalars_leniently():
    raw = {"identifier": "a", "fact_text": "b", "importance": "high", "ttl_seconds": "forever"}
    rec = normalize_record(raw)
    assert rec["importance"] == 0.5
    assert rec["ttl_seconds"] is None


def test_normalize_rejects_non_object():
    with pytest.raises(TypeError):
        normalize_record(["not", "a", "record"])  # type: ignore[arg-type]


# ── validate_record ──────────────────────────────────────────────────────────


def test_validate_ok_inline():
    rec = normalize_record({"identifier": "a", "fact_text": "b"})
    assert validate_record(rec) == []


def test_validate_ok_file_backed():
    rec = normalize_record(
        {"identifier": "a", "memory_type": "file", "source_uri": "f.csv", "byte_length": 5}
    )
    assert validate_record(rec) == []


def test_validate_file_backed_missing_source_uri():
    rec = normalize_record({"identifier": "a", "memory_type": "file"})
    issues = validate_record(rec)
    assert any("source_uri" in i for i in issues)
    assert any("byte_length" in i for i in issues)


def test_validate_empty_record():
    issues = validate_record(normalize_record({}))
    assert len(issues) >= 2  # neither identifier nor fact_text; no fact_text/summary


# ── canonical serialization bytes ───────────────────────────────────────────


def test_canonical_dumps_is_sorted_compact_utf8():
    line = canonical_dumps({"b": 1, "a": "é", "nested": {"z": 1, "a": 2}})
    assert line == '{"a":"é","b":1,"nested":{"a":2,"z":1}}'
    assert canonical_line({"a": 1}) == '{"a":1}\n'


def test_canonical_dumps_rejects_nan():
    with pytest.raises(ValueError):
        canonical_dumps({"a": float("nan")})


# ── embedding compatibility predicate (interchange-v1 §5) ────────────────────


def test_compatible_embedding_reused():
    b64 = _blob_b64()
    assert compatible_embedding_blob({"embedding": b64}) is not None
    assert compatible_embedding_blob({"embedding_b64": b64}) is not None


def test_incompatible_embedding_rejected():
    b64 = _blob_b64()
    assert compatible_embedding_blob({"embedding": b64, "embedding_dim": 128}) is None
    assert compatible_embedding_blob({"embedding": b64, "embedding_model": "other-v9"}) is None
    assert compatible_embedding_blob({"embedding": "!!!not-base64!!!"}) is None
    assert compatible_embedding_blob({"embedding": base64.b64encode(b"short").decode()}) is None
    assert compatible_embedding_blob({}) is None


def test_absent_dim_and_model_default_to_current():
    # Legacy records without dim/model fields are compatible by default.
    b64 = _blob_b64()
    assert compatible_embedding_blob({"embedding_b64": b64}) is not None


def test_hydrate_result_invalid_field_is_additive():
    """HydrateResult.invalid defaults to 0 — existing callers are unaffected."""
    from hotmem.snapshot.reader import HydrateResult as V2Result
    from hotmem.swap import HydrateResult as SwapResult

    assert SwapResult(loaded=1, skipped_dupes=0).invalid == 0
    assert V2Result(loaded=1, skipped_dupes=0).invalid == 0
