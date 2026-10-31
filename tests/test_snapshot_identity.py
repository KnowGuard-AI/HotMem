"""Tests for #96 -- canonical semantic Snapshot v2 identity.

Covers the acceptance contract for #96: deterministic identity over portable
record state, namespace scope, and file-reference state; independence from
packaging choices; versioned manifests; legacy v1 compatibility; and #94
cursor/clone-lineage compatibility.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.memory import FileRef, add_file_backed
from hotmem.snapshot import hydrate, snapshot, verify
from hotmem.snapshot.format import SnapshotChecksumError
from hotmem.snapshot.reader import MANIFEST_NAME, MEMORIES_NAME, verify_manifest

# ── Semantic snapshot identity (#96) ─────────────────────────────────────────


def test_semantic_snapshot_id_distinguishes_namespace_and_preserves_legacy_v1_and_lineage(
    tmp_path: Path,
):
    """#96 MVT-1: semantic snapshot_id (v2) distinguishes identical content_hash
    across namespaces and portable state while keeping legacy v1 manifests and
    #94 clone/cursor lineage compatible."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.mount import compare_mount_lineage
    from hotmem.snapshot.format import (
        LEGACY_SNAPSHOT_ID_ALGORITHM,
        LEGACY_SNAPSHOT_ID_VERSION,
        SNAPSHOT_ID_ALGORITHM,
        SNAPSHOT_ID_VERSION,
        compute_legacy_snapshot_id,
    )

    ch = compute_content_hash("shared/topic", "Identical fact across namespaces.")
    db_a = MemoryDB(tmp_path / "ns_a.sqlite")
    db_b = MemoryDB(tmp_path / "ns_b.sqlite")
    try:
        for db_inst, ns, state in ((db_a, "ns/alpha", "HOT"), (db_b, "ns/beta", "READY")):
            db_inst.insert(
                id="same-id-1",
                identifier="shared/topic",
                fact_text="Identical fact across namespaces.",
                embedding=pack_embedding(embed_text("Identical fact across namespaces.")),
                embedding_dim=EMBEDDING_DIM,
                embedding_model=EMBEDDING_MODEL,
                source="agent://test",
                importance=0.5,
                metadata_json="{}",
                content_hash=ch,
                created_at="2026-10-30T10:00:00Z",
                namespace=ns,
                tier="hot",
                promotion_state=state,
                promotion_candidate=0,
            )

        snap_a = tmp_path / "snap_a"
        snap_b = tmp_path / "snap_b"
        snapshot(db_a, snap_a)
        snapshot(db_b, snap_b)

        man_a = verify_manifest(snap_a)
        man_b = verify_manifest(snap_b)
        assert man_a.snapshot_id_version == SNAPSHOT_ID_VERSION
        assert man_a.snapshot_id_algorithm == SNAPSHOT_ID_ALGORITHM
        assert man_a.snapshot_id != man_b.snapshot_id
        assert man_a.snapshot_id != compute_legacy_snapshot_id([ch])

        # Fallback base_snapshot_id lineage check works for both v2 semantic ID
        # and legacy v1 content-hash-list ID even without base_source_replica_id.
        clone_v2 = MemoryDB(tmp_path / "clone_v2.sqlite")
        clone_v1 = MemoryDB(tmp_path / "clone_v1.sqlite")
        try:
            hydrate(clone_v2, snap_a)
            clone_v2._conn.execute("DELETE FROM store_meta WHERE key = 'base_source_replica_id'")
            clone_v2.commit()
            assert compare_mount_lineage(db_a, clone_v2)["relationship"] == "clone"

            # Convert snap_a manifest to explicit v1 legacy content-hash-list identity.
            raw_man = json.loads((snap_a / MANIFEST_NAME).read_text(encoding="utf-8"))
            raw_man["snapshot_id"] = compute_legacy_snapshot_id([ch])
            raw_man["snapshot_id_version"] = LEGACY_SNAPSHOT_ID_VERSION
            raw_man["snapshot_id_algorithm"] = LEGACY_SNAPSHOT_ID_ALGORITHM
            (snap_a / MANIFEST_NAME).write_text(
                json.dumps(raw_man, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            verified_v1 = verify_manifest(snap_a)
            assert verified_v1.snapshot_id_version == LEGACY_SNAPSHOT_ID_VERSION

            hydrate(clone_v1, snap_a)
            clone_v1._conn.execute("DELETE FROM store_meta WHERE key = 'base_source_replica_id'")
            clone_v1.commit()
            assert compare_mount_lineage(db_a, clone_v1)["relationship"] == "clone"
        finally:
            clone_v2.close()
            clone_v1.close()
    finally:
        db_a.close()
        db_b.close()


def test_semantic_snapshot_id_golden_vectors():
    """#96 WP5: lock canonical projection JSON and SHA-256 digests for empty,
    scoped-empty, and maximal inline + file-backed snapshot vectors."""
    from hotmem.interchange.canonical import (
        canonical_dumps,
        compute_content_hash,
        compute_file_reference_hash,
    )
    from hotmem.snapshot.format import (
        FileReference,
        compute_semantic_snapshot_id,
        semantic_record_digest,
        semantic_record_projection,
        semantic_snapshot_projection,
        sha256_bytes,
    )

    # 1. Empty unscoped snapshot golden vector.
    empty_proj = semantic_snapshot_projection([])
    assert canonical_dumps(empty_proj) == (
        '{"file_backed_references":[],"records":[],"scope":null,"version":2}'
    )
    expected_empty_id = sha256_bytes(
        b'hotmem-snapshot-id-v2:{"file_backed_references":[],"records":[],"scope":null,"version":2}'
    )
    assert expected_empty_id == "f8a6cf35d0d17fab750458fdd94399434cf4e5816fa10cb166d01ac22946b3dd"
    assert compute_semantic_snapshot_id([]) == expected_empty_id

    # 2. Empty namespace-scoped snapshot golden vector.
    scoped_empty_id = compute_semantic_snapshot_id(
        [],
        scope={
            "namespace": "ops/shared",
            "permitted_namespaces": ["ops/public", "ops/shared", "ops/public"],
            "session_id": "ignored-session-noise",
            "caller_id": "ignored-caller-noise",
        },
    )
    assert scoped_empty_id != expected_empty_id
    assert scoped_empty_id == sha256_bytes(
        b'hotmem-snapshot-id-v2:{"file_backed_references":[],"records":[],'
        b'"scope":{"namespace":"ops/shared","permitted_namespaces":["ops/public","ops/shared"]},'
        b'"version":2}'
    )

    # 3. Pinned inline + file-backed golden records.
    inline_rec = {
        "schema_version": 2,
        "record_schema_version": 1,
        "id": "gold-inl-01",
        "identifier": "ops/runbook",
        "memory_type": "fact",
        "fact_text": "Freeze Friday production deploys after 16:00 UTC.",
        "fact_summary": "Friday deploy freeze policy",
        "embedding": "AAAA",
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
        "source": "agent://codex",
        "importance": 0.85,
        "metadata": {"service": "payments", "tier_hint": 1},
        "content_hash": compute_content_hash(
            "ops/runbook", "Freeze Friday production deploys after 16:00 UTC."
        ),
        "ttl_seconds": 86400,
        "created_at": "2026-10-30T09:00:00Z",
        "updated_at": "2026-10-30T10:00:00Z",
        "namespace": "ops/prod",
        "tier": "warm",
        "tags": ["model:gpt-5", "env:prod"],
        "related_memories": ["gold-rel-01", "gold-rel-02"],
        "parent_memory": "gold-parent-00",
        "snapshot_id": "snap-origin-00",
        "promotion_state": "READY",
        "promotion_candidate": 1,
        "provenance": {"session_id": "sess-gold-1", "actor": "human:sre"},
    }
    file_rec = {
        "schema_version": 2,
        "id": "gold-file-01",
        "identifier": "ops/metrics",
        "memory_type": "file",
        "fact_text": "",
        "fact_summary": "Latency CSV slice",
        "source": "agent://claude",
        "importance": 0.6,
        "metadata": {},
        "content_hash": compute_file_reference_hash("ops/metrics", "data/latency.csv", 0, 128),
        "created_at": "2026-10-30T09:05:00Z",
        "namespace": "ops/prod",
        "tier": "hot",
        "source_uri": "./data/latency.csv",
        "byte_offset": 0,
        "byte_length": 128,
        "source_format": "csv",
        "source_checksum": "ab" * 32,
        "promotion_state": "HOT",
        "promotion_candidate": 0,
    }
    file_ref = FileReference(
        memory_id="gold-file-01",
        source_uri="./data/latency.csv",
        byte_offset=0,
        byte_length=128,
        source_format="csv",
        source_checksum="ab" * 32,
        attachment="ab" * 32,
    )

    inl_proj = semantic_record_projection(inline_rec)
    assert "embedding" not in inl_proj
    assert "embedding_dim" not in inl_proj
    assert "embedding_model" not in inl_proj
    assert inl_proj["schema_version"] == 1
    assert semantic_record_projection(file_rec)["schema_version"] == 2
    assert semantic_record_projection(file_rec)["source_uri"] == "data/latency.csv"

    d_inl = semantic_record_digest(inline_rec)
    d_file = semantic_record_digest(file_rec)
    assert len(d_inl) == 64 and len(d_file) == 64 and d_inl != d_file

    combined_id = compute_semantic_snapshot_id(
        [inline_rec, file_rec],
        file_backed_references=[file_ref],
        scope={"namespace": "ops/prod"},
    )
    expected_envelope = {
        "file_backed_references": [
            {
                "byte_length": 128,
                "byte_offset": 0,
                "memory_id": "gold-file-01",
                "source_checksum": "ab" * 32,
                "source_format": "csv",
                "source_uri": "data/latency.csv",
            }
        ],
        "records": sorted([d_inl, d_file]),
        "scope": {"namespace": "ops/prod", "permitted_namespaces": []},
        "version": 2,
    }
    assert combined_id == sha256_bytes(
        f"hotmem-snapshot-id-v2:{canonical_dumps(expected_envelope)}".encode()
    )


def test_semantic_snapshot_id_invariant_to_record_ref_and_json_key_ordering(tmp_path: Path):
    """#96 acceptance: reordering records, file_backed_references, or JSON object
    keys never changes semantic snapshot_id."""
    from hotmem.snapshot.format import FileReference, compute_semantic_snapshot_id

    rec_1 = {
        "id": "rec-1",
        "identifier": "alpha/one",
        "fact_text": "First portable memory",
        "namespace": "team-a",
        "metadata": {"z_key": 1, "a_key": {"inner_b": "two", "inner_a": "one"}},
        "provenance": {"z_prov": "x", "a_prov": "y"},
        "created_at": "2026-10-30T01:00:00Z",
    }
    rec_1_rekeyed = {
        "created_at": "2026-10-30T01:00:00Z",
        "provenance": {"a_prov": "y", "z_prov": "x"},
        "metadata": {"a_key": {"inner_a": "one", "inner_b": "two"}, "z_key": 1},
        "namespace": "team-a",
        "fact_text": "First portable memory",
        "identifier": "alpha/one",
        "id": "rec-1",
    }
    rec_2 = {
        "id": "rec-2",
        "identifier": "beta/two",
        "memory_type": "file",
        "fact_text": "",
        "fact_summary": "Second portable file memory",
        "source_uri": "dir/../reports/q3.csv",
        "byte_offset": 10,
        "byte_length": 200,
        "source_format": "csv",
        "source_checksum": "11" * 32,
        "namespace": "team-b",
        "created_at": "2026-10-30T02:00:00Z",
    }
    ref_a = FileReference(
        memory_id="rec-2",
        source_uri="reports/q3.csv",
        byte_offset=10,
        byte_length=200,
        source_format="csv",
        source_checksum="11" * 32,
        attachment="11" * 32,
    )
    ref_b = FileReference(
        memory_id="rec-3",
        source_uri="./notes/arch.md",
        byte_offset=0,
        byte_length=64,
        source_format="md",
        source_checksum="22" * 32,
        attachment=None,
    )

    id_forward = compute_semantic_snapshot_id(
        [rec_1, rec_2],
        file_backed_references=[ref_a, ref_b],
        scope={"namespace": "team-a", "permitted_namespaces": ["team-b", "team-a"]},
    )
    id_reversed = compute_semantic_snapshot_id(
        [rec_2, rec_1_rekeyed],
        file_backed_references=[ref_b, ref_a],
        scope={"permitted_namespaces": ["team-a", "team-b", "team-a"], "namespace": "team-a"},
    )
    assert id_forward == id_reversed


def test_semantic_snapshot_id_changes_on_every_preserved_field_and_reference_state():
    """#96 acceptance: every one of the 27 #99 preserve columns, plus file-backed
    reference state and semantic scope, changes semantic snapshot_id when mutated."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.interchange.record import MEMORY_FIELD_INVENTORY
    from hotmem.snapshot.format import (
        FileReference,
        compute_semantic_snapshot_id,
        semantic_record_projection,
    )

    # Verify 1-to-1 coverage of all 27 'preserve' columns in MEMORY_FIELD_INVENTORY.
    preserved_inventory_cols = {
        name for name, spec in MEMORY_FIELD_INVENTORY.items() if spec.disposition == "preserve"
    }
    regenerated_inventory_cols = {
        name for name, spec in MEMORY_FIELD_INVENTORY.items() if spec.disposition == "regenerate"
    }
    assert len(preserved_inventory_cols) == 27
    assert regenerated_inventory_cols == {"embedding", "embedding_dim", "embedding_model"}

    base_record = {
        "id": "mem-base-01",
        "identifier": "ops/incident",
        "fact_text": "Primary database failover completed in 14 seconds.",
        "fact_summary": "DB failover duration",
        "memory_type": "fact",
        "source": "agent://codex",
        "importance": 0.75,
        "metadata": {"ticket": "INC-42", "extra": ["a"]},
        "content_hash": compute_content_hash(
            "ops/incident", "Primary database failover completed in 14 seconds."
        ),
        "ttl_seconds": 3600,
        "created_at": "2026-10-30T12:00:00Z",
        "updated_at": "2026-10-30T12:30:00Z",
        "namespace": "ops/incident",
        "tier": "warm",
        "tags": ["sev:2", "db:primary"],
        "related_memories": ["mem-rel-1", "mem-rel-2"],
        "parent_memory": "mem-parent-1",
        "source_uri": "logs/failover.log",
        "source_format": "log",
        "source_checksum": "aa" * 32,
        "byte_offset": 0,
        "byte_length": 512,
        "snapshot_id": "snap-prev-1",
        "promotion_state": "READY",
        "promotion_candidate": 1,
        "schema_version": 2,
        "record_schema_version": 1,
        "provenance": {"session_id": "s-100", "harness_id": "codex"},
        "embedding": "AAAA",
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
    }
    proj = semantic_record_projection(base_record)
    mapped_proj_cols = {
        "metadata_json" if k == "metadata" else "provenance_json" if k == "provenance" else k
        for k in proj
    }
    assert mapped_proj_cols == preserved_inventory_cols

    base_ref = FileReference(
        memory_id="mem-base-01",
        source_uri="logs/failover.log",
        byte_offset=0,
        byte_length=512,
        source_format="log",
        source_checksum="aa" * 32,
        attachment=None,
    )
    base_id = compute_semantic_snapshot_id(
        [base_record],
        file_backed_references=[base_ref],
        scope={"namespace": "ops/incident"},
    )

    mutations: list[tuple[str, dict[str, object], str]] = [
        ("id", {"id": "mem-base-02"}, "id"),
        ("identifier", {"identifier": "ops/incident-other"}, "identifier"),
        (
            "fact_text",
            {"fact_text": "Primary database failover completed in 19 seconds."},
            "fact_text",
        ),
        ("fact_summary", {"fact_summary": "Updated summary"}, "fact_summary"),
        ("memory_type", {"memory_type": "file"}, "memory_type"),
        ("source", {"source": "agent://claude"}, "source"),
        ("source_empty", {"source": ""}, "source"),
        ("importance", {"importance": 0.25}, "importance"),
        ("metadata", {"metadata": {"ticket": "INC-99", "extra": ["a"]}}, "metadata"),
        ("content_hash", {"content_hash": "ff" * 32}, "content_hash"),
        ("ttl_seconds", {"ttl_seconds": 7200}, "ttl_seconds"),
        ("ttl_none", {"ttl_seconds": None}, "ttl_seconds"),
        ("created_at", {"created_at": "2026-10-30T13:00:00Z"}, "created_at"),
        ("updated_at", {"updated_at": "2026-10-30T14:00:00Z"}, "updated_at"),
        ("updated_at_none", {"updated_at": None}, "updated_at"),
        ("namespace", {"namespace": "ops/other-ns"}, "namespace"),
        ("namespace_default", {"namespace": ""}, "namespace"),
        ("tier", {"tier": "cold"}, "tier"),
        ("tags_value", {"tags": ["sev:1", "db:primary"]}, "tags"),
        ("tags_order", {"tags": ["db:primary", "sev:2"]}, "tags"),
        (
            "related_memories_value",
            {"related_memories": ["mem-rel-1", "mem-rel-9"]},
            "related_memories",
        ),
        (
            "related_memories_order",
            {"related_memories": ["mem-rel-2", "mem-rel-1"]},
            "related_memories",
        ),
        ("parent_memory", {"parent_memory": "mem-parent-2"}, "parent_memory"),
        ("source_uri", {"source_uri": "logs/other.log"}, "source_uri"),
        ("source_format", {"source_format": "txt"}, "source_format"),
        ("source_checksum", {"source_checksum": "bb" * 32}, "source_checksum"),
        ("byte_offset", {"byte_offset": 64}, "byte_offset"),
        ("byte_offset_none", {"byte_offset": None}, "byte_offset"),
        ("byte_length", {"byte_length": 1024}, "byte_length"),
        ("byte_length_none", {"byte_length": None}, "byte_length"),
        ("snapshot_id", {"snapshot_id": "snap-prev-2"}, "snapshot_id"),
        ("promotion_state", {"promotion_state": "PROMOTED"}, "promotion_state"),
        ("promotion_candidate", {"promotion_candidate": 0}, "promotion_candidate"),
        ("schema_version", {"record_schema_version": 2}, "schema_version"),
        (
            "provenance",
            {"provenance": {"session_id": "s-200", "harness_id": "codex"}},
            "provenance",
        ),
    ]
    base_projection = semantic_record_projection(base_record)
    for label, patch, expected_key in mutations:
        mutated = {**base_record, **patch}
        projection = semantic_record_projection(mutated)
        changed = {key for key in base_projection if base_projection[key] != projection[key]}
        assert changed == {expected_key}, f"mutation {label!r} changed {sorted(changed)}"
        mut_id = compute_semantic_snapshot_id(
            [mutated],
            file_backed_references=[base_ref],
            scope={"namespace": "ops/incident"},
        )
        assert mut_id != base_id, f"mutation {label!r} did not change semantic snapshot_id"

    # Attachment reference mutation and scope mutation also change snapshot_id.
    attached_ref = FileReference(
        memory_id="mem-base-01",
        source_uri="logs/failover.log",
        byte_offset=0,
        byte_length=512,
        source_format="log",
        source_checksum="aa" * 32,
        attachment="aa" * 32,
    )
    # Attachment bundling is packaging, not portable state: it must not change
    # semantic identity (#96 acceptance, docs/snapshot-v2.md#determinism).
    assert (
        compute_semantic_snapshot_id(
            [base_record],
            file_backed_references=[attached_ref],
            scope={"namespace": "ops/incident"},
        )
        == base_id
    )
    assert (
        compute_semantic_snapshot_id(
            [base_record],
            file_backed_references=[base_ref],
            scope={"namespace": "ops/different-scope"},
        )
        != base_id
    )


def test_semantic_snapshot_id_ignores_regenerate_embeddings_and_packaging_noise(tmp_path: Path):
    """#96 acceptance: regenerate embedding fields and packaging/transport noise
    do not change semantic snapshot_id."""
    from hotmem.snapshot.format import compute_semantic_snapshot_id

    base = {
        "id": "mem-noise-1",
        "identifier": "ops/noise",
        "fact_text": "Packaging noise invariance test.",
        "namespace": "ops",
        "created_at": "2026-10-30T00:00:00Z",
        "embedding": "AAAA",
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
    }
    different_embedding = {
        **base,
        "embedding": "ZZZZ9999==",
        "embedding_dim": 1536,
        "embedding_model": "foreign-embed-v9",
    }
    no_embedding = {
        k: v for k, v in base.items() if k not in ("embedding", "embedding_dim", "embedding_model")
    }

    id_base = compute_semantic_snapshot_id(
        [base],
        scope={
            "namespace": "ops",
            "caller_id": "caller-1",
            "session_id": "sess-1",
            "store_id": "store-1",
            "exported_count": 1,
            "omitted_count": 0,
        },
    )
    assert (
        compute_semantic_snapshot_id([different_embedding], scope={"namespace": "ops"}) == id_base
    )
    assert compute_semantic_snapshot_id([no_embedding], scope={"namespace": "ops"}) == id_base
    assert (
        compute_semantic_snapshot_id(
            [base],
            scope={
                "namespace": "ops",
                "caller_id": "caller-999",
                "session_id": "sess-999",
                "store_id": "store-999",
                "exported_count": 500,
                "omitted_count": 42,
            },
        )
        == id_base
    )


def test_semantic_snapshot_id_unicode_nfc_and_null_default_normalization():
    """#96 WP2/WP5: Unicode NFC/NFD equivalence and omitted/null default
    normalization produce identical semantic snapshot_id digests."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.snapshot.format import compute_semantic_snapshot_id

    nfc_text = "Caf\u00e9 r\u00e9sum\u00e9"
    nfd_text = "Cafe\u0301 re\u0301sume\u0301"
    assert nfc_text != nfd_text

    rec_nfc_explicit_defaults = {
        "id": "uni-1",
        "identifier": "ops/caf\u00e9",
        "fact_text": nfc_text,
        "fact_summary": nfc_text,
        "memory_type": "fact",
        "content_hash": compute_content_hash("ops/caf\u00e9", nfc_text),
        "namespace": "",
        "source": "snapshot",
        "importance": 0.0,
        "metadata": {"caf\u00e9": nfc_text},
        "provenance": None,
        "tags": ["tag:caf\u00e9"],
        "related_memories": [],
        "parent_memory": "",
        "ttl_seconds": None,
        "created_at": "2026-10-30T10:00:00Z",
        "updated_at": None,
        "tier": "hot",
        "promotion_state": "HOT",
        "promotion_candidate": 0,
        "source_uri": "./docs/../ops/caf\u00e9.md",
        "source_format": "",
        "source_checksum": "",
        "byte_offset": None,
        "byte_length": None,
        "snapshot_id": "",
        "schema_version": 2,
        "record_schema_version": 1,
    }
    rec_nfd_omitted_defaults = {
        "id": "uni-1",
        "identifier": "ops/cafe\u0301",
        "fact_text": nfd_text,
        "fact_summary": nfd_text,
        "memory_type": "inline",
        "content_hash": compute_content_hash("ops/cafe\u0301", nfd_text),
        "importance": -0.0,
        "metadata": {"cafe\u0301": nfd_text},
        "provenance": {},
        "tags": ["tag:cafe\u0301"],
        "ttl_seconds": 0,
        "created_at": "2026-10-30T10:00:00Z",
        "updated_at": "",
        "source_uri": "ops/cafe\u0301.md",
        "schema_version": 1,
    }
    assert compute_semantic_snapshot_id([rec_nfc_explicit_defaults]) == (
        compute_semantic_snapshot_id([rec_nfd_omitted_defaults])
    )


def test_semantic_snapshot_id_empty_and_duplicate_logical_records(tmp_path: Path):
    """#96 acceptance: empty snapshots and duplicate logical records have
    deterministic, order-independent snapshot_id behavior."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.snapshot import verify
    from hotmem.snapshot.format import (
        SNAPSHOT_ID_ALGORITHM,
        SNAPSHOT_ID_VERSION,
        compute_semantic_snapshot_id,
    )

    # Empty store snapshot export + verify + hydrate.
    empty_db = MemoryDB(tmp_path / "empty.sqlite")
    empty_snap = tmp_path / "empty_snap"
    try:
        snapshot(empty_db, empty_snap)
        summary = verify(empty_snap)
        assert summary["valid"] is True
        assert summary["record_count"] == 0
        assert summary["snapshot_id_version"] == SNAPSHOT_ID_VERSION
        assert summary["snapshot_id_algorithm"] == SNAPSHOT_ID_ALGORITHM
        assert summary["snapshot_id"] == compute_semantic_snapshot_id([])

        target_empty = MemoryDB(tmp_path / "target_empty.sqlite")
        try:
            res = hydrate(target_empty, empty_snap)
            assert res.loaded == 0
            assert target_empty.count() == 0
        finally:
            target_empty.close()
    finally:
        empty_db.close()

    # Duplicate logical records: same (namespace, content_hash).
    ch = compute_content_hash("dup/key", "Shared logical fact")
    dup_a = {
        "id": "dup-1",
        "identifier": "dup/key",
        "fact_text": "Shared logical fact",
        "content_hash": ch,
        "namespace": "ops",
        "tier": "hot",
        "created_at": "2026-10-30T01:00:00Z",
    }
    dup_b = {
        "id": "dup-2",
        "identifier": "dup/key",
        "fact_text": "Shared logical fact",
        "content_hash": ch,
        "namespace": "ops",
        "tier": "warm",
        "created_at": "2026-10-30T02:00:00Z",
    }

    single_id = compute_semantic_snapshot_id([dup_a])
    exact_dup_id = compute_semantic_snapshot_id([dup_a, dict(dup_a)])
    divergent_dup_ab = compute_semantic_snapshot_id([dup_a, dup_b])
    divergent_dup_ba = compute_semantic_snapshot_id([dup_b, dup_a])

    assert single_id != exact_dup_id
    assert exact_dup_id != divergent_dup_ab
    assert divergent_dup_ab == divergent_dup_ba


def test_manifest_identity_version_validation_and_cursor_compatibility(tmp_path: Path):
    """#96 WP3/WP4/WP5: malformed or unsupported snapshot_id_version /
    snapshot_id_algorithm fails closed, while #94 hmc1 v1 cursors remain
    compatible across legacy v1 and semantic v2 snapshot bases."""
    from hotmem.events import (
        _build_memory_created_payload,
        append_event,
        issue_replay_cursor,
        replay_with_cursor,
    )
    from hotmem.snapshot import verify
    from hotmem.snapshot.format import (
        LEGACY_SNAPSHOT_ID_ALGORITHM,
        LEGACY_SNAPSHOT_ID_VERSION,
        SNAPSHOT_ID_ALGORITHM,
        SNAPSHOT_ID_VERSION,
        compute_legacy_snapshot_id,
    )
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "ver_src.sqlite")
    snap_dir = tmp_path / "ver_snap"
    try:
        add_memory(src, "ver/one", "Versioned snapshot identity test")
        row = src.all_rows(include_embedding=True)[0]
        append_event(
            src,
            event_type="memory.created",
            memory_id=row["id"],
            namespace=row.get("namespace") or "",
            payload=_build_memory_created_payload(row),
        )
        snapshot(src, snap_dir)
        summary = verify(snap_dir)
        assert summary["snapshot_id_version"] == SNAPSHOT_ID_VERSION
        assert summary["snapshot_id_algorithm"] == SNAPSHOT_ID_ALGORITHM

        manifest_path = snap_dir / MANIFEST_NAME
        orig_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Malformed / unsupported version and algorithm cases fail closed.
        bad_cases = [
            {"snapshot_id_version": 0},
            {"snapshot_id_version": 3},
            {"snapshot_id_version": 99},
            {"snapshot_id_version": True},
            {"snapshot_id_version": "2"},
            {"snapshot_id_algorithm": "unknown-alg"},
            {"snapshot_id_algorithm": 123},
            {
                "snapshot_id_version": SNAPSHOT_ID_VERSION,
                "snapshot_id_algorithm": LEGACY_SNAPSHOT_ID_ALGORITHM,
            },
            {
                "snapshot_id_version": LEGACY_SNAPSHOT_ID_VERSION,
                "snapshot_id_algorithm": SNAPSHOT_ID_ALGORITHM,
            },
        ]
        for patch in bad_cases:
            bad_man = {**orig_manifest, **patch}
            manifest_path.write_text(json.dumps(bad_man), encoding="utf-8")
            with pytest.raises(SnapshotChecksumError) as exc_bad:
                verify_manifest(snap_dir)
            assert exc_bad.value.reason == "malformed"

        # Restore valid v2 manifest and hydrate clone_v2; also hydrate clone_v1
        # from a legacy v1 manifest, then replay a new event into both via hmc1 v1 cursors.
        manifest_path.write_text(
            json.dumps(orig_manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        clone_v2 = MemoryDB(tmp_path / "clone_cursor_v2.sqlite")
        clone_v1 = MemoryDB(tmp_path / "clone_cursor_v1.sqlite")
        try:
            hydrate(clone_v2, snap_dir)

            legacy_man = {
                **orig_manifest,
                "snapshot_id": compute_legacy_snapshot_id([row["content_hash"]]),
                "snapshot_id_version": LEGACY_SNAPSHOT_ID_VERSION,
                "snapshot_id_algorithm": LEGACY_SNAPSHOT_ID_ALGORITHM,
            }
            manifest_path.write_text(
                json.dumps(legacy_man, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            summary_v1 = verify(snap_dir)
            assert summary_v1["snapshot_id_version"] == LEGACY_SNAPSHOT_ID_VERSION
            assert summary_v1["snapshot_id_algorithm"] == LEGACY_SNAPSHOT_ID_ALGORITHM
            hydrate(clone_v1, snap_dir)

            # Emit a second event on src and replay into both clones, including an
            # explicit hmc1 v1 cursor carrying the legacy v1 base_snapshot_id.
            add_memory(src, "ver/two", "Event emitted after snapshot export")
            row2 = src.all_rows(include_embedding=True)[-1]
            append_event(
                src,
                event_type="memory.created",
                memory_id=row2["id"],
                namespace=row2.get("namespace") or "",
                payload=_build_memory_created_payload(row2),
            )

            res_v2 = replay_with_cursor(src, clone_v2)
            assert res_v2.applied == 1 and clone_v2.count() == 2

            legacy_cursor = issue_replay_cursor(
                src,
                position="seq",
                seq=1,
                base_snapshot_id=legacy_man["snapshot_id"],
            ).encode()
            res_v1 = replay_with_cursor(src, clone_v1, cursor=legacy_cursor)
            assert res_v1.applied == 1 and clone_v1.count() == 2
        finally:
            clone_v2.close()
            clone_v1.close()
    finally:
        src.close()


def test_semantic_projection_is_total_for_malformed_lifecycle_fields():
    """#96 review: projection must never raise on adversarial lifecycle values."""
    from hotmem.snapshot.format import semantic_record_projection

    base = {"id": "tot-1", "identifier": "ops/total", "fact_text": "Projection totality."}
    for candidate in ("abc", {"a": 1}, [1], 2.5, None, "", False):
        proj = semantic_record_projection({**base, "promotion_candidate": candidate})
        assert proj["promotion_candidate"] in (0, 1)
    assert (
        semantic_record_projection({**base, "promotion_candidate": True})["promotion_candidate"]
        == 1
    )
    assert (
        semantic_record_projection({**base, "promotion_candidate": 1})["promotion_candidate"] == 1
    )


def test_verify_manifest_maps_unexpected_projection_errors_to_malformed(
    tmp_path: Path, monkeypatch
):
    """#96 review: verifier fails closed instead of leaking ValueError/TypeError."""
    import hotmem.snapshot.reader as v2_reader

    db = MemoryDB(tmp_path / "failclosed.sqlite")
    try:
        db.insert(
            id="fc-1",
            identifier="ops/failclosed",
            fact_text="Fail closed.",
            embedding=pack_embedding(embed_text("Fail closed.")),
            embedding_dim=EMBEDDING_DIM,
            embedding_model=EMBEDDING_MODEL,
            source="agent://test",
            importance=0.5,
            metadata_json="{}",
            content_hash=hashlib.sha256(b"ops/failclosed:Fail closed.").hexdigest(),
            created_at="2026-10-30T10:00:00Z",
            namespace="ops",
            tier="hot",
            promotion_state="HOT",
            promotion_candidate=0,
        )
        snap_dir = tmp_path / "failclosed_snap"
        snapshot(db, snap_dir)
    finally:
        db.close()

    def boom(*args, **kwargs):
        raise ValueError("simulated coercion failure")

    monkeypatch.setattr(v2_reader, "semantic_record_digest", boom)
    with pytest.raises(SnapshotChecksumError) as exc:
        verify_manifest(snap_dir)
    assert exc.value.reason == "malformed"
    assert exc.value.file == MEMORIES_NAME


def test_verify_manifest_handles_uncoercible_promotion_candidate(tmp_path: Path):
    """#96 review: a re-sealed manifest with `promotion_candidate: "abc"` must
    verify deterministically and hydrate as an invalid record, never crash."""
    from hotmem.interchange.fingerprint import state_fingerprint_from_list
    from hotmem.snapshot.format import compute_overall, compute_semantic_snapshot_id, sha256_file
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "malformed_candidate.sqlite")
    snap_dir = tmp_path / "malformed_candidate_snap"
    try:
        add_memory(src, "ops/candidate", "Malformed promotion candidate.")
        snapshot(src, snap_dir)
    finally:
        src.close()

    mem_path = snap_dir / MEMORIES_NAME
    record = json.loads(mem_path.read_text(encoding="utf-8").strip())
    record["promotion_candidate"] = "abc"
    mem_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    manifest_path = snap_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_sha = sha256_file(mem_path)
    manifest["files"][MEMORIES_NAME] = {"size": mem_path.stat().st_size, "sha256": new_sha}
    manifest["overall_sha256"] = compute_overall({MEMORIES_NAME: new_sha})
    manifest["snapshot_id"] = compute_semantic_snapshot_id(
        [record],
        file_backed_references=[],
        scope=manifest.get("scope"),
    )
    # The record is invalid, so it contributes no state fingerprint; re-seal the
    # metadata the way a producer of this (invalid) payload would.
    manifest["state_fingerprint"] = state_fingerprint_from_list([])
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    assert verify_manifest(snap_dir).snapshot_id == manifest["snapshot_id"]

    dst = MemoryDB(tmp_path / "malformed_candidate_dst.sqlite")
    try:
        res = hydrate(dst, snap_dir)
        assert res.loaded == 0
        assert res.invalid == 1
        assert dst.count() == 0
    finally:
        dst.close()


def test_semantic_snapshot_id_is_independent_of_attachment_bundling(tmp_path: Path):
    """#96 review: bundling byte ranges (packaging) must not change semantic
    identity; only referenced-byte state does."""
    from hotmem.snapshot.format import compute_semantic_snapshot_id

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = workspace / "slice.bin"
    data.write_bytes(b"0123456789" * 10)

    db = MemoryDB(tmp_path / "bundle.sqlite")
    try:
        add_file_backed(
            db,
            "bundle/slice",
            FileRef(
                source_uri=str(data),
                byte_offset=0,
                byte_length=50,
                source_format="bin",
            ),
            summary="bundled slice",
        )
        plain_dir = tmp_path / "bundle_plain"
        attached_dir = tmp_path / "bundle_attached"
        snapshot(db, plain_dir, base_dir=workspace)
        snapshot(db, attached_dir, copy_attachments=True, base_dir=workspace)
    finally:
        db.close()

    plain = verify_manifest(plain_dir)
    attached = verify_manifest(attached_dir)
    assert attached.file_backed_references[0].attachment is not None
    assert plain.file_backed_references[0].attachment is None
    assert plain.snapshot_id == attached.snapshot_id
    assert plain.snapshot_id == compute_semantic_snapshot_id(
        [json.loads((plain_dir / MEMORIES_NAME).read_text(encoding="utf-8").strip())],
        file_backed_references=plain.file_backed_references,
        scope=plain.scope,
    )


def test_lineage_fallback_reproduces_scoped_semantic_base_snapshot_id(tmp_path: Path):
    """#96 review: a scoped/attachment-bundled snapshot must still classify as a
    clone when the replica-id anchor is absent, and a fork must still diverge."""
    from hotmem.mount import compare_mount_lineage
    from hotmem.swap import add_memory

    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = workspace / "slice.bin"
    data.write_bytes(b"abcdefghij" * 20)

    src = MemoryDB(tmp_path / "lineage_src.sqlite")
    snap_dir = tmp_path / "lineage_snap"
    try:
        add_memory(src, "lineage/inline", "Scoped lineage fallback.")
        add_file_backed(
            src,
            "lineage/slice",
            FileRef(source_uri=str(data), byte_offset=0, byte_length=40, source_format="bin"),
            summary="scoped slice",
        )
        scope = {"namespace": "ops", "permitted_namespaces": ["ops", "public"]}
        snapshot(src, snap_dir, copy_attachments=True, base_dir=workspace, export_scope=scope)
    finally:
        src.close()

    clone = MemoryDB(tmp_path / "lineage_clone.sqlite")
    try:
        hydrate(clone, snap_dir)
        assert json.loads(clone.get_meta("base_snapshot_scope")) == {
            "namespace": "ops",
            "permitted_namespaces": ["ops", "public"],
        }
        clone._conn.execute("DELETE FROM store_meta WHERE key = 'base_source_replica_id'")
        clone.commit()

        reopened_src = MemoryDB(tmp_path / "lineage_src.sqlite")
        try:
            result = compare_mount_lineage(reopened_src, clone)
            assert result["relationship"] == "clone"
            assert result["reason"] == "clean_clone_of_mount"

            add_memory(clone, "lineage/fork", "Local divergence after clone.")
            forked = compare_mount_lineage(reopened_src, clone)
            assert forked["relationship"] != "clone"
        finally:
            reopened_src.close()
    finally:
        clone.close()


def test_empty_snapshot_hydration_replaces_previous_semantic_base_scope(tmp_path: Path):
    """An unscoped empty base must clear scope retained from an earlier clone."""
    from hotmem.mount import compare_mount_lineage

    src = MemoryDB(tmp_path / "empty_scope_src.sqlite")
    scoped_dir = tmp_path / "empty_scoped_snapshot"
    unscoped_dir = tmp_path / "empty_unscoped_snapshot"
    try:
        snapshot(
            src,
            scoped_dir,
            export_scope={"namespace": "ops", "permitted_namespaces": ["ops"]},
        )
        snapshot(src, unscoped_dir)

        clone = MemoryDB(tmp_path / "empty_scope_clone.sqlite")
        try:
            hydrate(clone, scoped_dir)
            assert clone.get_meta("base_snapshot_scope")

            # Empty stores qualify for clone binding on repeated hydration.
            hydrate(clone, unscoped_dir)
            assert clone.get_meta("base_snapshot_scope") == ""

            # Exercise the semantic snapshot-id fallback without the replica
            # anchor, as a clone may be inspected after its source is reopened.
            clone._conn.execute("DELETE FROM store_meta WHERE key = 'base_source_replica_id'")
            clone.commit()
            result = compare_mount_lineage(src, clone)
            assert result["relationship"] == "clone"
            assert result["reason"] == "clean_clone_of_mount"
        finally:
            clone.close()
    finally:
        src.close()


def test_verify_manifest_enforces_state_fingerprint_without_cursor(tmp_path: Path):
    """#96 review: a versioned manifest always verifies its declared
    state_fingerprint, even when history was trimmed and no cursor was issued."""
    from hotmem.interchange.fingerprint import record_fingerprint, state_fingerprint_from_list
    from hotmem.snapshot.format import compute_overall, compute_semantic_snapshot_id, sha256_file
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "nofp.sqlite")
    snap_dir = tmp_path / "nofp_snap"
    try:
        add_memory(src, "ops/fingerprint", "Fingerprint without cursor.")
        snapshot(src, snap_dir)
    finally:
        src.close()

    manifest_path = snap_dir / MANIFEST_NAME
    mem_path = snap_dir / MEMORIES_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = json.loads(mem_path.read_text(encoding="utf-8").strip())
    record["promotion_state"] = "READY"
    mem_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    new_sha = sha256_file(mem_path)
    manifest["files"][MEMORIES_NAME] = {"size": mem_path.stat().st_size, "sha256": new_sha}
    manifest["overall_sha256"] = compute_overall({MEMORIES_NAME: new_sha})
    manifest["snapshot_id"] = compute_semantic_snapshot_id(
        [record], file_backed_references=[], scope=manifest.get("scope")
    )
    # Simulate a trimmed-history export: no portable replay cursor.
    manifest["replay_cursor"] = ""
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(SnapshotChecksumError) as exc:
        verify_manifest(snap_dir)
    assert exc.value.file == "state_fingerprint"

    manifest["state_fingerprint"] = state_fingerprint_from_list([record_fingerprint(record)])
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    assert verify_manifest(snap_dir).snapshot_id == manifest["snapshot_id"]


def test_semantic_projection_preserves_distinct_invalid_lifecycle_and_provenance():
    """#96 review: invalid enum-like values and malformed provenance stay
    distinct instead of collapsing into a real default/null."""
    from hotmem.snapshot.format import compute_semantic_snapshot_id, semantic_record_projection

    base = {"id": "inv-1", "identifier": "ops/invalid", "fact_text": "Invalid state."}
    default_proj = semantic_record_projection(base)
    bogus_state = semantic_record_projection({**base, "promotion_state": "BOGUS"})
    bogus_tier = semantic_record_projection({**base, "tier": "lukewarm"})

    assert default_proj["promotion_state"] == "HOT"
    assert default_proj["tier"] == "hot"
    assert bogus_state["promotion_state"] == "BOGUS"
    assert bogus_tier["tier"] == "lukewarm"
    assert bogus_state["promotion_state"] != default_proj["promotion_state"]
    assert bogus_tier["tier"] != default_proj["tier"]

    prov_list = semantic_record_projection({**base, "provenance": ["a", "b"]})
    prov_str = semantic_record_projection({**base, "provenance": "a,b"})
    prov_none = semantic_record_projection({**base, "provenance": None})
    assert prov_list["provenance"] != prov_str["provenance"]
    assert prov_list["provenance"] != prov_none["provenance"]
    assert prov_none["provenance"] is None

    assert compute_semantic_snapshot_id(
        [{**base, "promotion_state": "BOGUS"}]
    ) != compute_semantic_snapshot_id([base])


def test_compute_snapshot_id_rejects_ambiguous_inputs():
    """#96 review: the backward-compatible dispatcher must never silently drop
    or ignore caller inputs."""
    from hotmem.snapshot.format import compute_snapshot_id

    hashes = ["a" * 64, "b" * 64]
    records = [{"id": "r1", "identifier": "ops/r1", "fact_text": "x"}]

    with pytest.raises(TypeError):
        compute_snapshot_id([*hashes, *records])
    with pytest.raises(TypeError):
        compute_snapshot_id(records, version=1)
    with pytest.raises(TypeError):
        compute_snapshot_id(hashes, version=2)
    with pytest.raises(ValueError):
        compute_snapshot_id(hashes, scope={"namespace": "ops"})
    with pytest.raises(ValueError):
        compute_snapshot_id(hashes, file_backed_references=[])
    with pytest.raises(ValueError):
        compute_snapshot_id(records, version=99)
    with pytest.raises(TypeError):
        compute_snapshot_id([1, 2])

    # Supported forms remain intact.
    assert len(compute_snapshot_id(hashes)) == 64
    assert len(compute_snapshot_id(records)) == 64
    assert len(compute_snapshot_id(records, version=2)) == 64
    assert compute_snapshot_id(hashes, version=1) == compute_snapshot_id(hashes)


def test_semantic_identity_projects_each_record_once(monkeypatch):
    """#96 review: identity computation must not re-project records for
    derived references or digests (write/verify/lineage hot paths)."""
    import hotmem.snapshot.format as snapshot_format

    calls = {"n": 0}
    real = snapshot_format._normalize_record_safe

    def counting(raw, **kwargs):
        calls["n"] += 1
        return real(raw, **kwargs)

    monkeypatch.setattr(snapshot_format, "_normalize_record_safe", counting)

    records = [
        {
            "id": "perf-1",
            "identifier": "perf/one",
            "memory_type": "file",
            "fact_text": "",
            "source_uri": "data/one.bin",
            "byte_offset": 0,
            "byte_length": 10,
            "source_format": "bin",
            "source_checksum": "aa" * 32,
        },
        {
            "id": "perf-2",
            "identifier": "perf/two",
            "memory_type": "file",
            "fact_text": "",
            "source_uri": "data/two.bin",
            "byte_offset": 5,
            "byte_length": 20,
            "source_format": "bin",
            "source_checksum": "bb" * 32,
        },
        {"id": "perf-3", "identifier": "perf/three", "fact_text": "Inline record."},
    ]
    snapshot_format.compute_semantic_snapshot_id(records)
    assert calls["n"] == len(records)


def test_semantic_record_digest_reuses_precomputed_normalization():
    """#96 review: the fast path must produce byte-identical digests."""
    from hotmem.interchange.record import normalize_record
    from hotmem.snapshot.format import semantic_record_digest

    record = {
        "id": "reuse-1",
        "identifier": "reuse/one",
        "fact_text": "Reuse normalization.",
        "namespace": "ops",
        "promotion_candidate": 0,
    }
    normalized = normalize_record(record, default_source="snapshot")
    assert semantic_record_digest(record) == semantic_record_digest(record, normalized=normalized)


def test_snapshot_identity_is_stable_across_hydrate_and_reexport(tmp_path: Path):
    """#96: export -> hydrate -> export reproduces the same semantic identity."""
    workspace = tmp_path / "roundtrip_ws"
    workspace.mkdir()
    data = workspace / "roundtrip.bin"
    data.write_bytes(b"roundtrip-slice" * 4)

    src = MemoryDB(tmp_path / "roundtrip_src.sqlite")
    first = tmp_path / "roundtrip_first"
    try:
        from hotmem.swap import add_memory

        add_memory(src, "roundtrip/inline", "Round-trip identity.")
        add_file_backed(
            src,
            "roundtrip/slice",
            FileRef(
                source_uri=str(data),
                byte_offset=0,
                byte_length=15,
                source_format="bin",
                source_checksum=hashlib.sha256(data.read_bytes()[:15]).hexdigest(),
            ),
            summary="round-trip slice",
        )
        snapshot(src, first)
    finally:
        src.close()

    middle = MemoryDB(tmp_path / "roundtrip_middle.sqlite")
    second = tmp_path / "roundtrip_second"
    try:
        hydrate(middle, first)
        snapshot(middle, second)
    finally:
        middle.close()

    assert verify_manifest(second).snapshot_id == verify_manifest(first).snapshot_id


def test_duplicate_logical_records_collapse_on_hydrate_but_identity_is_multiset(
    tmp_path: Path,
):
    """#96: duplicates are identity-significant; hydration dedupes by logical
    identity, so a re-export reflects the collapsed state (documented)."""
    from hotmem.interchange.fingerprint import record_fingerprint, state_fingerprint_from_list
    from hotmem.snapshot.format import compute_overall, compute_semantic_snapshot_id, sha256_file
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "dup_src.sqlite")
    snap_dir = tmp_path / "dup_snap"
    try:
        add_memory(src, "dup/one", "Duplicate logical identity.")
        snapshot(src, snap_dir)
    finally:
        src.close()

    single_id = verify_manifest(snap_dir).snapshot_id
    mem_path = snap_dir / MEMORIES_NAME
    line = mem_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    mem_path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    manifest_path = snap_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_sha = sha256_file(mem_path)
    manifest["files"][MEMORIES_NAME] = {"size": mem_path.stat().st_size, "sha256": new_sha}
    manifest["overall_sha256"] = compute_overall({MEMORIES_NAME: new_sha})
    manifest["memory_count"] = 2
    manifest["inline_count"] = 2
    manifest["snapshot_id"] = compute_semantic_snapshot_id(
        [record, record], file_backed_references=[], scope=manifest.get("scope")
    )
    manifest["state_fingerprint"] = state_fingerprint_from_list(
        [record_fingerprint(record), record_fingerprint(record)]
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    duplicate_id = verify_manifest(snap_dir).snapshot_id
    assert duplicate_id != single_id

    dst = MemoryDB(tmp_path / "dup_dst.sqlite")
    reexport = tmp_path / "dup_reexport"
    try:
        res = hydrate(dst, snap_dir)
        assert res.loaded == 1
        assert res.skipped_dupes == 1
        snapshot(dst, reexport)
    finally:
        dst.close()

    assert verify_manifest(reexport).snapshot_id == single_id


def test_legacy_manifest_identity_variants_and_verify_reporting(tmp_path: Path):
    """#96: algorithm-only manifests, v1-tagged manifests, and unversioned
    manifests stay verifiable, and `verify()` reports declared metadata."""
    from hotmem.snapshot.format import compute_legacy_snapshot_id
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "legacy_variants.sqlite")
    snap_dir = tmp_path / "legacy_variants_snap"
    try:
        add_memory(src, "legacy/one", "Legacy variant.")
        snapshot(src, snap_dir)
    finally:
        src.close()

    manifest_path = snap_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = json.loads((snap_dir / MEMORIES_NAME).read_text(encoding="utf-8").strip())
    legacy_id = compute_legacy_snapshot_id([record["content_hash"]])

    # 1. Algorithm-only (version inferred).
    alg_only = {k: v for k, v in manifest.items() if k != "snapshot_id_version"}
    alg_only["snapshot_id"] = legacy_id
    alg_only["snapshot_id_algorithm"] = "hotmem-snapshot-id-v1"
    manifest_path.write_text(json.dumps(alg_only), encoding="utf-8")
    summary = verify(snap_dir)
    assert summary["snapshot_id_version"] == 1
    assert summary["snapshot_id_algorithm"] == "hotmem-snapshot-id-v1"

    # 2. Unversioned manifest with a legacy id remains verifiable.
    unversioned = {
        k: v
        for k, v in manifest.items()
        if k not in ("snapshot_id_version", "snapshot_id_algorithm")
    }
    unversioned["snapshot_id"] = legacy_id
    manifest_path.write_text(json.dumps(unversioned), encoding="utf-8")
    summary = verify(snap_dir)
    assert summary["snapshot_id_version"] == 0
    assert summary["snapshot_id_algorithm"] == ""

    # 3. A v1-tagged manifest with a tampered id fails closed.
    tampered = {**unversioned, "snapshot_id_version": 1, "snapshot_id": "f" * 64}
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError) as exc:
        verify_manifest(snap_dir)
    assert exc.value.file == "snapshot_id"
