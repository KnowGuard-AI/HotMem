"""Interchange contract tests — normalization, validation, canonical bytes (#67).

Locks the shared reader primitives from docs/okf/interchange-v1.md:
field preservation across dialects, unknown-key preservation, canonical JSON
bytes, logical identity, and the embedding compatibility predicate.
"""

from __future__ import annotations

import base64
import json
import struct

import pytest

from hotmem.embed import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EmbeddingDescriptor,
    embed_text,
    pack_embedding,
    unpack_embedding,
)
from hotmem.interchange.canonical import (
    canonical_dumps,
    canonical_line,
    compute_content_hash,
    logical_id,
)
from hotmem.interchange.compat import compatible_embedding_blob, resolve_embedding
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


# ── descriptor-based compatibility (issue #78) ───────────────────────────────


def _semantic_descriptor(dim: int = 8) -> EmbeddingDescriptor:
    return EmbeddingDescriptor(
        implementation="local",
        model="mini",
        dimension=dim,
        revision="r1",
        preprocessing="pp1",
    )


class _FakeEmbedder:
    """Deterministic test embedder: alternating +/- unit vector."""

    def __init__(self, descriptor: EmbeddingDescriptor) -> None:
        self._descriptor = descriptor
        self.calls = 0

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        dim = self._descriptor.dimension
        vec = [0.0] * dim
        norm = (dim) ** 0.5
        for i in range(dim):
            vec[i] = (1.0 if i % 2 == 0 else -1.0) / norm
        return vec


def test_descriptor_compatible_blob_reused():
    desc = _semantic_descriptor()
    fake = _FakeEmbedder(desc)
    blob = pack_embedding(fake.embed("semantic fact"))
    b64 = base64.b64encode(blob).decode("ascii")
    record = {"embedding": b64, "embedding_model": desc.key, "embedding_dim": desc.dimension}
    assert compatible_embedding_blob(record, descriptor=desc) == blob


def test_equal_dimensions_never_compatible():
    """#78: matching dimension is not evidence of a shared embedding space."""
    desc = _semantic_descriptor()
    fake = _FakeEmbedder(desc)
    blob = pack_embedding(fake.embed("semantic fact"))
    b64 = base64.b64encode(blob).decode("ascii")
    same_dim = {"embedding": b64, "embedding_model": "foreign/v9", "embedding_dim": desc.dimension}
    assert compatible_embedding_blob(same_dim, descriptor=desc) is None
    # And the hash default never adopts a foreign key's blob either.
    assert (
        compatible_embedding_blob(
            {"embedding": b64, "embedding_model": desc.key, "embedding_dim": desc.dimension}
        )
        is None
    )


def test_legacy_absent_fields_keep_hash_semantics():
    b64 = _blob_b64()
    # Absent model/dim = hash defaults — reusable by the hash embedder only.
    assert compatible_embedding_blob({"embedding": b64}) is not None
    assert (
        compatible_embedding_blob(
            {"embedding": b64}, descriptor=_semantic_descriptor(EMBEDDING_DIM)
        )
        is None
    )


def test_nonfinite_or_unnormalized_vector_rejected():
    desc = _semantic_descriptor()
    nan_blob = struct.pack(f"{desc.dimension}f", *([float("nan")] * desc.dimension))
    inf_blob = struct.pack(f"{desc.dimension}f", *([float("inf")] * desc.dimension))
    zeros_blob = struct.pack(f"{desc.dimension}f", *([0.0] * desc.dimension))
    for bad in (nan_blob, inf_blob, zeros_blob):
        record = {
            "embedding": base64.b64encode(bad).decode("ascii"),
            "embedding_model": desc.key,
            "embedding_dim": desc.dimension,
        }
        assert compatible_embedding_blob(record, descriptor=desc) is None
    # A "none"-normalization descriptor accepts the unnormalized vector but
    # still rejects non-finite values.
    none_desc = EmbeddingDescriptor(
        implementation="local",
        model="mini",
        dimension=desc.dimension,
        revision="r1",
        normalization="none",
        preprocessing="pp1",
    )
    raw = struct.pack(f"{desc.dimension}f", *([0.5] * desc.dimension))
    ok = {
        "embedding": base64.b64encode(raw).decode("ascii"),
        "embedding_model": none_desc.key,
        "embedding_dim": none_desc.dimension,
    }
    assert compatible_embedding_blob(ok, descriptor=none_desc) == raw


def test_malformed_dimension_is_incompatible_not_error():
    b64 = _blob_b64()
    assert compatible_embedding_blob({"embedding": b64, "embedding_dim": "sixty-four"}) is None


# ── resolve_embedding statuses (issue #78) ───────────────────────────────────


def test_resolve_reused_without_calling_embedder():
    desc = _semantic_descriptor()
    fake = _FakeEmbedder(desc)
    unit = [(1.0 if i % 2 == 0 else -1.0) / (desc.dimension**0.5) for i in range(desc.dimension)]
    blob = pack_embedding(unit)
    stored, model, dim, status = resolve_embedding(
        {
            "embedding": base64.b64encode(blob).decode("ascii"),
            "embedding_model": desc.key,
            "embedding_dim": desc.dimension,
            "fact_text": "semantic fact",
        },
        embedder=fake,
    )
    assert status == "reused"
    assert fake.calls == 0
    assert stored == blob and model == desc.key and dim == desc.dimension


def test_resolve_rebuilt_stamps_active_descriptor():
    desc = _semantic_descriptor()
    fake = _FakeEmbedder(desc)
    blob, model, dim, status = resolve_embedding(
        {
            "embedding": _blob_b64(),
            "embedding_model": "foreign-v9",
            "embedding_dim": EMBEDDING_DIM,
            "fact_text": "rebuild me",
        },
        embedder=fake,
    )
    assert status == "rebuilt"
    assert fake.calls == 1
    assert model == desc.key and dim == desc.dimension
    assert len(unpack_embedding(blob)) == desc.dimension


def test_resolve_missing_for_textless_file_backed():
    blob, model, dim, status = resolve_embedding(
        {"memory_type": "file", "fact_summary": "", "fact_text": ""},
    )
    assert status == "missing"
    assert blob == b"" and model == "" and dim == EMBEDDING_DIM


def test_resolve_failed_when_embedder_raises():
    class ExplodingEmbedder(_FakeEmbedder):
        def embed(self, text: str) -> list[float]:
            self.calls += 1
            raise RuntimeError("provider down")

    desc = _semantic_descriptor()
    exploding = ExplodingEmbedder(desc)
    blob, model, dim, status = resolve_embedding(
        {"fact_text": "still canonical", "embedding_model": "foreign-v9"},
        embedder=exploding,
    )
    assert status == "failed"
    assert exploding.calls == 1
    assert blob == b"" and model == ""
    assert dim == desc.dimension


# ── complete 30-column field disposition contract (#99 WP1) ─────────────────


def test_memory_field_inventory_covers_every_sqlite_column_in_order():
    """#99 WP1: every column in _MEMORY_COLUMNS has an explicit disposition and rationale."""
    from hotmem.db import _MEMORY_COLUMNS
    from hotmem.interchange.record import MEMORY_FIELD_INVENTORY, VALID_DISPOSITIONS

    assert tuple(MEMORY_FIELD_INVENTORY.keys()) == _MEMORY_COLUMNS
    for col in _MEMORY_COLUMNS:
        spec = MEMORY_FIELD_INVENTORY[col]
        assert spec.column == col
        assert spec.disposition in VALID_DISPOSITIONS
        assert spec.snapshot_v2_field
        assert spec.interchange_v1_field
        assert spec.legacy_jsonl_field
        assert len(spec.rationale) >= 20

    preserved = {c for c, s in MEMORY_FIELD_INVENTORY.items() if s.disposition == "preserve"}
    regenerated = {c for c, s in MEMORY_FIELD_INVENTORY.items() if s.disposition == "regenerate"}
    excluded = {c for c, s in MEMORY_FIELD_INVENTORY.items() if s.disposition == "exclude"}
    assert regenerated == {"embedding", "embedding_dim", "embedding_model"}
    assert excluded == set()
    assert len(preserved) == len(_MEMORY_COLUMNS) - 3


def test_legacy_defaults_distinguish_missing_from_explicit_empty_zero_or_false():
    """#99 WP1: omitted legacy fields get defaults while explicit '', 0, 0.0, False survive."""
    from hotmem.swap import record_to_memory_record

    omitted = normalize_record({"identifier": "k", "fact_text": "v"}, default_source="snapshot")
    assert omitted["source"] == "snapshot"
    assert omitted["importance"] == 0.5
    assert omitted["byte_offset"] is None
    assert omitted["ttl_seconds"] is None
    assert omitted["updated_at"] is None
    assert omitted["promotion_state"] is None
    assert omitted["promotion_candidate"] is None
    omitted_mem = record_to_memory_record(
        omitted, b"", embedding_model="hotmem-hash-v1", embedding_dim=64
    )
    assert omitted_mem.promotion_state == "HOT"
    assert omitted_mem.promotion_candidate == 0
    assert omitted_mem.schema_version == 1

    explicit = normalize_record(
        {
            "identifier": "k",
            "fact_text": "v",
            "source": "",
            "importance": 0.0,
            "byte_offset": 0,
            "promotion_candidate": 0,
            "promotion_state": "COLD",
            "tier": "cold",
            "schema_version": 2,
            "record_schema_version": 1,
        },
        default_source="snapshot",
    )
    assert validate_record(explicit) == []
    assert explicit["source"] == ""
    assert explicit["importance"] == 0.0
    assert explicit["byte_offset"] == 0
    assert explicit["promotion_candidate"] == 0
    assert explicit["promotion_state"] == "COLD"
    assert explicit["tier"] == "cold"
    explicit_mem = record_to_memory_record(
        explicit, b"", embedding_model="hotmem-hash-v1", embedding_dim=64
    )
    assert explicit_mem.source == ""
    assert explicit_mem.importance == 0.0
    assert explicit_mem.byte_offset == 0
    assert explicit_mem.promotion_candidate == 0
    assert explicit_mem.promotion_state == "COLD"
    assert explicit_mem.tier == "cold"
    assert explicit_mem.schema_version == 1


@pytest.mark.parametrize(
    ("bad_fields", "expected_field"),
    [
        ({"importance": 1.5}, "importance"),
        ({"importance": -0.1}, "importance"),
        ({"importance": "not-a-float"}, "importance"),
        ({"importance": True}, "importance"),
        ({"ttl_seconds": 0}, "ttl_seconds"),
        ({"ttl_seconds": -60}, "ttl_seconds"),
        ({"ttl_seconds": "forever"}, "ttl_seconds"),
        ({"ttl_seconds": True}, "ttl_seconds"),
        ({"tier": "glacial"}, "tier"),
        ({"promotion_state": "SUPER_HOT"}, "promotion_state"),
        ({"promotion_candidate": 2}, "promotion_candidate"),
        ({"promotion_candidate": "maybe"}, "promotion_candidate"),
        ({"memory_type": "executable"}, "memory_type"),
        ({"byte_offset": -1}, "byte_offset"),
        ({"byte_length": -5}, "byte_length"),
        ({"memory_type": "file", "source_uri": "a.csv", "byte_length": 0}, "byte_length"),
        ({"schema_version": 0}, "schema_version"),
        ({"record_schema_version": -1}, "record_schema_version"),
        ({"metadata": ["not", "an", "object"]}, "metadata"),
        ({"metadata_json": "{broken-json"}, "metadata"),
        ({"provenance": ["not", "an", "object"]}, "provenance"),
        ({"provenance_json": "{broken-json"}, "provenance"),
        ({"tags": {"not": "a-list"}}, "tags"),
        ({"tags": [123]}, "tags"),
        ({"related_memories": {"not": "a-list"}}, "related_memories"),
        ({"related_memories": [456]}, "related_memories"),
    ],
)
def test_validate_record_rejects_malformed_values_without_leaking_content(
    bad_fields: dict, expected_field: str
):
    """#99 WP1: malformed fields produce actionable errors without echoing sensitive content."""
    secret_canary = "TOP_SECRET_CANARY_VALUE_99"
    raw = {
        "identifier": "sec/item",
        "fact_text": f"Confidential payload {secret_canary}",
        **bad_fields,
    }
    rec = normalize_record(raw)
    issues = validate_record(rec)
    assert issues, f"expected validation error for {bad_fields}"
    assert any(expected_field in issue for issue in issues), issues
    for issue in issues:
        assert secret_canary not in issue
        assert "Confidential payload" not in issue
