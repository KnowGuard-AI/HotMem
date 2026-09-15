"""State fingerprint tests (#73) — sync-relevant mutation detection.

Locks the fingerprint contract from docs/okf/delta-v1.md: raw DB rows and
normalized records fingerprint identically; every sync-relevant mutation
changes the fingerprint; excluded fields (embedding, updated_at, snapshot_id,
id) do not; collection fingerprints are order-independent.
"""

from __future__ import annotations

from hotmem.embed import embed_text, pack_embedding
from hotmem.interchange.fingerprint import (
    FINGERPRINT_VERSION,
    record_fingerprint,
    state_fingerprint,
)
from hotmem.interchange.record import normalize_record


def _base_row(**overrides):
    row = {
        "id": "m1",
        "identifier": "vendor_x",
        "fact_text": "acme ships quarterly",
        "embedding": pack_embedding(embed_text("acme ships quarterly")),
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
        "source": "erp",
        "importance": 0.5,
        "metadata_json": '{"region": "emea"}',
        "content_hash": "c" * 64,
        "ttl_seconds": 3600,
        "created_at": "2026-09-01T00:00:00Z",
        "namespace": "team",
        "tier": "hot",
        "tags": '["biz"]',
        "memory_type": "fact",
        "source_uri": "",
        "source_format": "",
        "source_checksum": "",
        "byte_offset": None,
        "byte_length": None,
        "provenance_json": '{"origin": "erp"}',
        "promotion_state": "HOT",
        "promotion_candidate": 0,
        "updated_at": "2026-09-01T01:00:00Z",
        "snapshot_id": "",
        "parent_memory": "",
        "related_memories": "[]",
        "schema_version": 1,
    }
    row.update(overrides)
    return row


# ── cross-form agreement ────────────────────────────────────────────────────


def test_raw_row_and_normalized_record_fingerprint_identically():
    row = _base_row()
    assert record_fingerprint(row) == record_fingerprint(normalize_record(row))


def test_fingerprint_is_deterministic():
    assert record_fingerprint(_base_row()) == record_fingerprint(_base_row())


# ── mutation sensitivity: every sync-relevant change moves the fingerprint ──


def test_importance_mutation_detected():
    assert record_fingerprint(_base_row(importance=0.9)) != record_fingerprint(_base_row())


def test_namespace_mutation_detected():
    assert record_fingerprint(_base_row(namespace="other")) != record_fingerprint(_base_row())


def test_tags_mutation_detected():
    assert record_fingerprint(_base_row(tags='["biz","ops"]')) != record_fingerprint(_base_row())


def test_metadata_mutation_detected():
    assert record_fingerprint(_base_row(metadata_json='{"region": "apac"}')) != record_fingerprint(
        _base_row()
    )


def test_ttl_mutation_detected():
    assert record_fingerprint(_base_row(ttl_seconds=None)) != record_fingerprint(_base_row())


def test_promotion_state_mutation_detected():
    assert record_fingerprint(_base_row(promotion_state="WARM")) != record_fingerprint(_base_row())


def test_provenance_mutation_detected():
    assert record_fingerprint(_base_row(provenance_json='{"origin": "manual"}')) != (
        record_fingerprint(_base_row())
    )


# ── exclusions: derived/runtime fields do NOT move the fingerprint ──────────


def test_embedding_mutation_not_detected():
    """Embeddings are derived and rebuildable (interchange §5)."""
    other = _base_row(embedding=pack_embedding(embed_text("different text")))
    assert record_fingerprint(other) == record_fingerprint(_base_row())


def test_updated_at_mutation_not_detected():
    assert record_fingerprint(_base_row(updated_at="2026-09-02T00:00:00Z")) == (
        record_fingerprint(_base_row())
    )


def test_id_mutation_not_detected():
    assert record_fingerprint(_base_row(id="zzz")) == record_fingerprint(_base_row())


def test_unknown_keys_are_detected_state():
    """Unknown keys are preserved producer state (interchange §1.2) routed
    into metadata — a mutation to them is a real state change for sync."""
    row = _base_row()
    row["future_producer_field"] = {"x": 1}
    assert record_fingerprint(row) != record_fingerprint(_base_row())


# ── collection fingerprints ────────────────────────────────────────────────


def test_state_fingerprint_order_independent():
    a = _base_row(id="m1")
    b = _base_row(id="m2", identifier="vendor_y", fact_text="other fact")
    assert state_fingerprint([a, b]) == state_fingerprint([b, a])


def test_state_fingerprint_detects_collection_mutation():
    a = _base_row(id="m1")
    b = _base_row(id="m2", identifier="vendor_y", fact_text="other fact")
    b_moved = _base_row(id="m2", identifier="vendor_y", fact_text="other fact", tier="warm")
    assert state_fingerprint([a, b]) != state_fingerprint([a, b_moved])


def test_fingerprint_version_is_versioned():
    assert FINGERPRINT_VERSION == 1
    assert record_fingerprint(_base_row()).startswith("")


def test_fingerprints_are_hex():
    assert len(record_fingerprint(_base_row())) == 64
    assert len(state_fingerprint([_base_row()])) == 64


def test_unknown_keys_cross_form_agreement():
    """A raw row and its normalized record with the same unknown key
    fingerprint identically — CAS preconditions hold across forms."""
    raw = _base_row()
    raw["future_producer_field"] = {"x": 1}
    assert record_fingerprint(raw) == record_fingerprint(normalize_record(raw))
