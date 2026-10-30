"""Combined compatibility matrix — issues #78/#79/#80 integration proof.

Every transfer format (legacy JSONL, JSONL.GZ, Snapshot v2, interchange
package, repeat hydrate, one-way delta replay) crossed with the embedding
scenarios that matter (hash-only, compatible semantic, foreign revision or
dimension, provider failure, textless file-backed), plus the
canonical-vs-derived independence rule: the same active descriptor yields
identical logical retrieval before and after any clone; a different
descriptor preserves the canonical record and reports the rebuild.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from hotmem.annotations import validate_metadata
from hotmem.db import MemoryDB
from hotmem.embed import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EmbeddingDescriptor,
    embed_text,
    pack_embedding,
)
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.delta import apply_delta, produce_delta
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package
from hotmem.search import search_memories
from hotmem.snapshot import hydrate as snapshot_hydrate
from hotmem.snapshot import snapshot as snapshot_write
from hotmem.snapshot.writer import write_snapshot_v2
from hotmem.swap import add_memory


def _fake_embedder(dim: int, model: str, revision: str = "r1"):
    class Fake:
        _descriptor = EmbeddingDescriptor(
            implementation="local",
            model=model,
            dimension=dim,
            revision=revision,
            preprocessing="pp1",
        )

        @property
        def descriptor(self):
            return self._descriptor

        def embed(self, text: str):
            import hashlib

            seed = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
            rng = __import__("random").Random(seed)
            vec = [rng.uniform(-1, 1) for _ in range(dim)]
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            return [x / norm for x in vec]

    return Fake()


SEMANTIC_A = _fake_embedder(128, "sem", "r1")
SEMANTIC_B = _fake_embedder(128, "sem", "r2")  # same model family, new revision
FOREIGN_DIM = _fake_embedder(64, "other", "r1")  # hash-like dim, foreign key

FACTS = [
    ("vendor-x", "vendor x invoices are paid net 30"),
    ("vendor-y", "vendor y refund policy is 30 days"),
    ("infra", "cache stale data requires a manual purge"),
]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _export_variants(db: MemoryDB, tmp: Path) -> dict[str, Path | None]:
    """Every transfer format from the same source database."""
    jsonl = tmp / "legacy.jsonl"
    snapshot_write(db, jsonl)
    gz = tmp / "legacy.jsonl.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as handle:
        handle.write(jsonl.read_text())
    v2 = tmp / "snap-v2"
    write_snapshot_v2(db, v2)
    pkg = tmp / "pkg"
    write_package(db, pkg, gz=True)
    return {"jsonl": jsonl, "jsonl.gz": gz, "v2": v2, "package": pkg, "sqlite": None}


# ── matrix: format x target descriptor ───────────────────────────────────────


def test_matrix_hash_source_restores_under_every_format_and_embedder(tmp_path: Path):
    """Hash-produced data restores exactly under hash (reuse) everywhere."""
    src = MemoryDB(tmp_path / "src.sqlite")
    for identifier, fact in FACTS:
        add_memory(src, identifier, fact)

    variants = _export_variants(src, tmp_path)
    for name, path in variants.items():
        if path is None:
            continue
        target = MemoryDB(tmp_path / f"t-{name}.sqlite")
        result = snapshot_hydrate(target, path)
        assert result.loaded == len(FACTS), name
        assert result.embedding_rebuilt == len(FACTS) or result.embedding_reused == len(FACTS)
        rows = target.all_rows(include_embedding=True)
        assert {r["embedding_model"] for r in rows} == {EMBEDDING_MODEL}
        hits = search_memories(target, "cache stale data", top_k=1)
        assert hits[0]["content"].startswith("cache stale")
        target.close()
    src.close()


def test_matrix_semantic_source_same_space_reuses_every_vector(tmp_path: Path):
    """Semantic-produced data restores under its own descriptor: reuse."""
    src = MemoryDB(tmp_path / "sem-src.sqlite")
    for identifier, fact in FACTS:
        add_memory(src, identifier, fact, embedder=SEMANTIC_A)

    for name, path in _export_variants(src, tmp_path).items():
        if path is None:
            continue
        target = MemoryDB(tmp_path / f"sem-t-{name}.sqlite")
        result = snapshot_hydrate(target, path, embedder=SEMANTIC_A)
        assert result.loaded == len(FACTS), name
        assert result.embedding_reused == len(FACTS), f"{name}: vectors must be reused"
        assert result.embedding_rebuilt == 0, name
        hits = search_memories(target, "cache stale data", top_k=1, embedder=SEMANTIC_A)
        assert hits[0]["content"].startswith("cache stale"), name
        target.close()
    src.close()


def test_matrix_semantic_source_foreign_targets_rebuild_and_restamp(tmp_path: Path):
    """Different revision, different dimension, and hash targets all
    rebuild deterministically under the ACTIVE descriptor and stay
    retrievable — canonical records are never hostage to a provider."""
    src = MemoryDB(tmp_path / "sem2-src.sqlite")
    for identifier, fact in FACTS:
        add_memory(src, identifier, fact, embedder=SEMANTIC_A)

    variants = _export_variants(src, tmp_path)
    scenarios = {
        "hash": None,  # the default runtime
        "revision-b": SEMANTIC_B,
        "foreign-dim-64": FOREIGN_DIM,
    }
    for scenario, embedder in scenarios.items():
        for name, path in variants.items():
            if path is None:
                continue
            target = MemoryDB(tmp_path / f"x-{scenario}-{name}.sqlite")
            result = snapshot_hydrate(target, path, embedder=embedder)
            assert result.loaded == len(FACTS), (scenario, name)
            assert result.embedding_rebuilt == len(FACTS), (scenario, name)
            active = embedder if embedder is not None else None
            rows = target.all_rows(include_embedding=True)
            if scenario == "hash":
                assert {r["embedding_model"] for r in rows} == {EMBEDDING_MODEL}
                hits = search_memories(target, "cache stale data", top_k=1)
            else:
                assert {r["embedding_model"] for r in rows} == {active.descriptor.key}
                hits = search_memories(target, "cache stale data", top_k=1, embedder=active)
            assert hits[0]["content"].startswith("cache stale"), (scenario, name)
            target.close()
    src.close()


def test_matrix_missing_optional_runtime_keeps_canonical_data(tmp_path: Path):
    """A package produced under a semantic space restores under the hash
    runtime with zero optional dependencies — embeddings rebuild from text,
    canonical content survives, the record stays searchable lexically."""
    src = MemoryDB(tmp_path / "opt-src.sqlite")
    for identifier, fact in FACTS:
        add_memory(src, identifier, fact, embedder=SEMANTIC_A)
    pkg = tmp_path / "opt.pkg"
    write_package(src, pkg, gz=True)

    target = MemoryDB(tmp_path / "opt-target.sqlite")
    result = hydrate_package(target, pkg)  # default hash runtime, no extras
    assert result.loaded == len(FACTS)
    assert result.embedding_rebuilt == len(FACTS)
    hits = search_memories(target, "refund policy", top_k=1)
    assert hits[0]["content"].startswith("vendor y")
    src.close()
    target.close()


def test_matrix_textless_file_backed_is_missing_not_invalid(tmp_path: Path):
    """File-backed rows without summary flow as embedding_missing across
    formats — canonical record present, vector NULL, never garbage."""
    src = MemoryDB(tmp_path / "file-src.sqlite")
    from hotmem.memory import FileRef, add_file_backed

    doc = tmp_path / "contract.pdf"
    doc.write_bytes(b"%PDF- fake contract bytes")
    add_file_backed(
        src,
        "contract",
        FileRef(
            source_uri=str(doc),
            byte_offset=0,
            byte_length=doc.stat().st_size,
            source_format="pdf",
        ),
        summary=None,
    )

    pkg = tmp_path / "file.pkg"
    write_package(src, pkg, gz=True)
    target = MemoryDB(tmp_path / "file-target.sqlite")
    result = hydrate_package(target, pkg)
    assert result.loaded == 1
    assert result.embedding_missing == 1
    row = target.all_rows(include_embedding=True)[0]
    assert row["embedding"] == b""  # NULL-embedding convention
    assert row["fact_text"] == ""  # reference, not a copy (#38)
    src.close()
    target.close()


def test_matrix_repeat_hydrate_and_delta_replay_are_idempotent(tmp_path: Path):
    """Repeat restore loads zero; delta replay applies zero; annotation
    replay stays a no-op — under both runtimes."""
    for label, embedder in (("hash", None), ("semantic", SEMANTIC_A)):
        src = MemoryDB(tmp_path / f"idem-{label}-src.sqlite")
        for identifier, fact in FACTS:
            add_memory(src, identifier, fact, embedder=embedder or SEMANTIC_A)
        pkg = tmp_path / f"idem-{label}.pkg"
        write_package(src, pkg, gz=True)

        target = MemoryDB(tmp_path / f"idem-{label}-target.sqlite")
        first = hydrate_package(target, pkg, embedder=embedder)
        assert first.loaded == len(FACTS)
        second = hydrate_package(target, pkg, embedder=embedder)
        assert second.loaded == 0 and second.skipped_dupes == len(FACTS)
        assert second.embedding_rebuilt == 0  # zero embed work on replay

        producer = MemoryDB(tmp_path / f"idem-{label}-producer.sqlite")
        hydrate_package(producer, pkg, embedder=embedder)
        add_memory(producer, "new", "a fresh fact arrives", embedder=embedder or SEMANTIC_A)
        delta_dir = tmp_path / f"idem-{label}-delta"
        produce_delta(producer, pkg, delta_dir)
        applied = apply_delta(target, delta_dir, embedder=embedder)
        assert applied.applied == 1
        replay = apply_delta(target, delta_dir, embedder=embedder)
        assert replay.applied == 0 and replay.embedding_rebuilt == 0
        for db in (src, target, producer):
            db.close()


def test_matrix_canonical_vs_derived_independence(tmp_path: Path):
    """Same active descriptor -> identical logical retrieval before and
    after clone; the clone is a transport, not a re-ranking."""
    src = MemoryDB(tmp_path / "indep-src.sqlite")
    for identifier, fact in FACTS:
        add_memory(src, identifier, fact, embedder=SEMANTIC_A)
    before = search_memories(src, "vendor refund policy", top_k=3, embedder=SEMANTIC_A)

    pkg = tmp_path / "indep.pkg"
    write_package(src, pkg, gz=True)
    clone = MemoryDB(tmp_path / "indep-clone.sqlite")
    hydrate_package(clone, pkg, embedder=SEMANTIC_A)
    after = search_memories(clone, "vendor refund policy", top_k=3, embedder=SEMANTIC_A)

    assert [m["memory_id"] for m in before] == [m["memory_id"] for m in after]
    assert [m["score"] for m in before] == [m["score"] for m in after]
    src.close()
    clone.close()


def test_matrix_annotation_only_change_flows_through_delta_with_cas(tmp_path: Path):
    """Annotations + embeddings travel together: a semantic package with an
    enriched envelope replays through CAS under the same runtime."""
    envelope = {
        "schema_version": 1,
        "namespaces": {
            "org.example.entities": [{"id": "vendor-x", "type": "Organization", "confidence": 0.9}]
        },
    }
    validate_metadata({"annotations": envelope})

    src = MemoryDB(tmp_path / "ann-src.sqlite")
    add_memory(
        src, "vendor-x", FACTS[0][1], embedder=SEMANTIC_A, metadata={"annotations": envelope}
    )
    base_pkg = tmp_path / "ann-base.pkg"
    write_package(src, base_pkg, gz=True)

    producer = MemoryDB(tmp_path / "ann-producer.sqlite")
    hydrate_package(producer, base_pkg, embedder=SEMANTIC_A)
    row = producer.all_rows()[0]
    enriched = {
        "schema_version": 1,
        "namespaces": {
            "org.example.entities": [
                {"id": "vendor-x", "type": "Organization", "confidence": 0.9},
                {"id": "vendor-x-alias", "type": "Alias", "value": "VX"},
            ]
        },
    }
    producer.update_metadata_json(row["id"], json.dumps({"annotations": enriched}, sort_keys=True))
    delta_dir = tmp_path / "ann-delta"
    produce_delta(producer, base_pkg, delta_dir)

    receiver = MemoryDB(tmp_path / "ann-receiver.sqlite")
    hydrate_package(receiver, base_pkg, embedder=SEMANTIC_A)
    result = apply_delta(receiver, delta_dir, embedder=SEMANTIC_A)
    assert result.applied == 1
    assert result.embedding_reused == 1  # canonical text unchanged: vector reused
    stored = json.loads(receiver.all_rows()[0]["metadata_json"])
    assert len(stored["annotations"]["namespaces"]["org.example.entities"]) == 2
    for db in (src, producer, receiver):
        db.close()


def test_matrix_legacy_jsonl_with_foreign_model_rebuilds(tmp_path: Path):
    """A legacy JSONL row stamped with a foreign model and a hash-dimension
    blob: never scored as hash (equal dims are not compatibility), rebuilt
    under the active embedder."""
    import base64
    import random

    rng = random.Random("deterministic")
    vec = [rng.uniform(-1, 1) for _ in range(EMBEDDING_DIM)]
    blob = pack_embedding(vec)
    path = tmp_path / "foreign.jsonl"
    _write_jsonl(
        path,
        [
            {
                "identifier": "legacy",
                "fact_text": "legacy foreign-model record",
                "embedding_model": "foreign/v9",
                "embedding_dim": EMBEDDING_DIM,
                "embedding_b64": base64.b64encode(blob).decode("ascii"),
            }
        ],
    )
    target = MemoryDB(tmp_path / "legacy-target.sqlite")
    result = snapshot_hydrate(target, path)
    assert result.embedding_rebuilt == 1
    row = target.all_rows(include_embedding=True)[0]
    assert row["embedding_model"] == EMBEDDING_MODEL
    assert row["embedding_dim"] == EMBEDDING_DIM
    target.close()


# -- Legacy compatibility, repeat hydration, and byte vs semantic fidelity (#99 WP5) --


def test_legacy_v01_and_pre99_snapshot_v2_fixtures_hydrate_with_documented_defaults(
    tmp_path: Path,
):
    """#99 WP5: legacy minimal v0.1 JSONL and pre-#99 Snapshot v2 packages
    (using legacy file_references and omitting v2 fields) hydrate with documented
    defaults, while unsupported future schema_version/format fails at the boundary."""
    from hotmem.snapshot.format import (
        SnapshotChecksumError,
        compute_overall,
        sha256_file,
    )
    from hotmem.snapshot.reader import MANIFEST_NAME, MEMORIES_NAME, verify_manifest

    # 1. Pre-#99 Snapshot v2 fixture with legacy 'file_references' key and minimal fields.
    legacy_v2 = tmp_path / "legacy_v2_pre99"
    legacy_v2.mkdir()
    records = [
        {
            "schema_version": 2,
            "id": "leg-inl-1",
            "identifier": "legacy/inline",
            "fact_text": "Pre-99 inline record without v2 lineage fields.",
        },
        {
            "schema_version": 2,
            "id": "leg-file-1",
            "identifier": "legacy/file",
            "memory_type": "file",
            "fact_text": "",
            "fact_summary": "Pre-99 file summary",
            "source_uri": "old.csv",
            "byte_offset": 0,
            "byte_length": 42,
            "source_format": "csv",
            "source_checksum": "ab" * 32,
        },
    ]
    mem_path = legacy_v2 / MEMORIES_NAME
    mem_path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )
    mem_sha = sha256_file(mem_path)
    legacy_manifest = {
        "format": "hotmem-snapshot-v2",
        "schema_version": 2,
        "snapshot_id": "c" * 64,
        "memory_count": 2,
        "inline_count": 1,
        "file_backed_count": 1,
        "files": {
            MEMORIES_NAME: {"size": mem_path.stat().st_size, "sha256": mem_sha},
        },
        "overall_sha256": compute_overall({MEMORIES_NAME: mem_sha}),
        "file_references": [
            {
                "memory_id": "leg-file-1",
                "source_uri": "old.csv",
                "byte_offset": 0,
                "byte_length": 42,
                "source_format": "csv",
                "source_checksum": "ab" * 32,
            }
        ],
    }
    (legacy_v2 / MANIFEST_NAME).write_text(
        json.dumps(legacy_manifest, indent=2) + "\n", encoding="utf-8"
    )

    parsed_manifest = verify_manifest(legacy_v2)
    assert len(parsed_manifest.file_backed_references) == 1

    dst = MemoryDB(tmp_path / "dst_legacy_v2.sqlite")
    res = snapshot_hydrate(dst, legacy_v2)
    assert res.loaded == 2
    assert res.invalid == 0

    inl = dst.get_memory("leg-inl-1")
    fb = dst.get_memory("leg-file-1")
    assert inl is not None and fb is not None
    assert inl["source"] == "snapshot"
    assert inl["importance"] == 0.5
    assert inl["namespace"] == ""
    assert inl["tier"] == "hot"
    assert inl["promotion_state"] == "HOT"
    assert inl["promotion_candidate"] == 0
    assert inl["parent_memory"] == ""
    assert inl["related_memories"] == "[]"
    assert inl["tags"] == "[]"
    assert inl["snapshot_id"] == ""
    assert inl["updated_at"] is None
    assert inl["provenance_json"] is None
    assert inl["schema_version"] == 2
    assert fb["byte_offset"] == 0
    assert fb["byte_length"] == 42
    assert fb["source_checksum"] == "ab" * 32
    dst.close()

    # 2. Unsupported future schema_version (99) or unknown format fails at version boundary.
    future_v2 = tmp_path / "future_v99"
    future_v2.mkdir()
    (future_v2 / MEMORIES_NAME).write_bytes(mem_path.read_bytes())
    future_manifest = {**legacy_manifest, "schema_version": 99}
    (future_v2 / MANIFEST_NAME).write_text(json.dumps(future_manifest), encoding="utf-8")
    dst_future = MemoryDB(tmp_path / "dst_future.sqlite")
    with pytest.raises(SnapshotChecksumError, match="malformed"):
        snapshot_hydrate(dst_future, future_v2)
    assert dst_future.count() == 0
    dst_future.close()


def test_repeat_hydration_idempotency_and_cross_namespace_twins_preserve_state(
    tmp_path: Path,
):
    """#99 WP5: repeat hydration under #100's (namespace, content_hash) contract
    is idempotent across inline and file-backed records, keeps cross-namespace
    twins distinct, and never overwrites original id, provenance, tags, or lifecycle."""
    from hotmem.db import _MEMORY_COLUMNS
    from hotmem.memory import FileRef, add_file_backed, hydrate_memory_detailed
    from hotmem.snapshot import hydrate, snapshot, verify

    ws = tmp_path / "ws"
    ws.mkdir()
    csv_path = ws / "shared.csv"
    csv_bytes = b"metric,value\nlatency_ms,12\n"
    csv_path.write_bytes(csv_bytes)
    csv_sha = hashlib.sha256(csv_bytes).hexdigest()

    src = MemoryDB(ws / "src.sqlite")
    # Inline twin in ns/alpha and ns/beta with distinct portable metadata/provenance/lifecycle.
    for ns, rid, tier, state, tags, prov_sess in (
        ("ns/alpha", "inl-alpha", "warm", "READY", ["model:gpt-5", "project:alpha"], "sess-a"),
        ("ns/beta", "inl-beta", "cold", "PROMOTED", ["model:o4-mini", "project:beta"], "sess-b"),
    ):
        src.insert(
            id=rid,
            identifier="shared/fact",
            fact_text="Identical inline text across two namespaces.",
            embedding=pack_embedding(embed_text("Identical inline text across two namespaces.")),
            embedding_dim=EMBEDDING_DIM,
            embedding_model=EMBEDDING_MODEL,
            source=f"agent://{ns}",
            importance=0.75,
            metadata_json=json.dumps({"ns": ns}, sort_keys=True),
            content_hash=compute_content_hash(
                "shared/fact", "Identical inline text across two namespaces."
            ),
            ttl_seconds=7200,
            created_at="2026-10-05T10:00:00Z",
            namespace=ns,
            tier=tier,
            updated_at="2026-10-05T11:00:00Z",
            snapshot_id=f"snap-{ns}",
            promotion_state=state,
            promotion_candidate=1,
            parent_memory=f"parent-{ns}",
            related_memories=json.dumps([f"rel-{ns}"]),
            tags=json.dumps(tags),
            schema_version=1,
            fact_summary=f"Summary for {ns}",
            provenance_json=json.dumps({"session_id": prov_sess, "namespace": ns}, sort_keys=True),
        )

    # File-backed twin in ns/alpha and ns/beta referencing the same slice.
    file_ids: dict[str, str] = {}
    for ns, state in (("ns/alpha", "READY"), ("ns/beta", "PROMOTED")):
        fid, _ = add_file_backed(
            src,
            identifier="shared/csv",
            file_ref=FileRef(
                source_uri="shared.csv",
                byte_offset=0,
                byte_length=len(csv_bytes),
                source_format="csv",
                source_checksum=csv_sha,
            ),
            base_dir=str(ws),
            summary="Shared CSV latency metric",
            importance=0.8,
            metadata={"ns": ns},
            source=f"file-agent://{ns}",
            ttl_seconds=3600,
            created_at="2026-10-05T10:05:00Z",
            namespace=ns,
            tier="warm",
            updated_at="2026-10-05T11:05:00Z",
            snapshot_id=f"snap-file-{ns}",
            promotion_state=state,
            promotion_candidate=1,
            parent_memory=f"parent-file-{ns}",
            related_memories=[f"rel-file-{ns}"],
            tags=["model:gpt-5", f"project:{ns}"],
            schema_version=1,
            provenance={"session_id": f"file-{ns}", "namespace": ns},
        )
        file_ids[ns] = fid

    snap_dir = tmp_path / "snap_twins"
    snapshot(src, snap_dir, copy_attachments=True, base_dir=ws)
    assert verify(snap_dir)["valid"] is True

    expected_rows = {r["id"]: r for r in src.all_rows(include_embedding=True)}
    src.close()
    csv_path.unlink()

    dst = MemoryDB(tmp_path / "dst_twins.sqlite")
    first = hydrate(dst, snap_dir)
    assert first.loaded == 4
    assert first.skipped_dupes == 0
    assert dst.count() == 4

    # Repeat hydration of the exact same snapshot: 0 loaded, 4 skipped, state unchanged.
    second = hydrate(dst, snap_dir)
    assert second.loaded == 0
    assert second.skipped_dupes == 4
    assert dst.count() == 4

    # Competing snapshot with the same (namespace, content_hash) but different
    # id, source, tags, provenance, and promotion_state must NOT overwrite originals.
    competing_src = MemoryDB(tmp_path / "competing_src.sqlite")
    competing_src.insert(
        id="competing-id-999",
        identifier="shared/fact",
        fact_text="Identical inline text across two namespaces.",
        embedding=pack_embedding(embed_text("Identical inline text across two namespaces.")),
        embedding_dim=EMBEDDING_DIM,
        embedding_model=EMBEDDING_MODEL,
        source="agent://attacker-overwrite",
        importance=0.1,
        metadata_json=json.dumps({"overwritten": True}, sort_keys=True),
        content_hash=compute_content_hash(
            "shared/fact", "Identical inline text across two namespaces."
        ),
        namespace="ns/alpha",
        tier="hot",
        promotion_state="HOT",
        promotion_candidate=0,
        tags=json.dumps(["model:other"]),
        schema_version=2,
        provenance_json=json.dumps({"session_id": "overwrite"}, sort_keys=True),
    )
    competing_snap = tmp_path / "competing_snap"
    snapshot(competing_src, competing_snap)
    competing_src.close()

    third = hydrate(dst, competing_snap)
    assert third.loaded == 0
    assert third.skipped_dupes == 1
    assert dst.get_memory("competing-id-999") is None

    for rid, exp in expected_rows.items():
        got = dst.get_memory(rid)
        assert got is not None
        for col in _MEMORY_COLUMNS:
            assert got[col] == exp[col], f"row {rid} col {col}: {got[col]!r} != {exp[col]!r}"

    # File-backed attachment hydration still works after repeat hydrations.
    for fid in file_ids.values():
        detail = hydrate_memory_detailed(dst, fid, base_dir=snap_dir)
        assert detail.content == csv_bytes
        assert detail.verified is True
    dst.close()


def test_package_byte_integrity_and_semantic_record_fidelity_are_separate_properties(
    tmp_path: Path,
):
    """#99 WP5: prove package-byte integrity and semantic record fidelity
    separately: (1) a byte-valid package can carry a semantic field drift or
    invalid record detected by semantic checks, and (2) a semantically valid
    record inside a byte-tampered package is rejected by byte verification."""
    from hotmem.interchange.fingerprint import record_fingerprint
    from hotmem.interchange.record import normalize_record, validate_record
    from hotmem.snapshot import hydrate, snapshot
    from hotmem.snapshot.format import (
        SnapshotChecksumError,
        compute_overall,
        sha256_file,
    )
    from hotmem.snapshot.reader import MANIFEST_NAME, MEMORIES_NAME, verify_manifest

    src = MemoryDB(tmp_path / "sep_src.sqlite")
    src.insert(
        id="sep-1",
        identifier="ops/sep",
        fact_text="Separation of byte integrity and semantic fidelity.",
        embedding=pack_embedding(embed_text("Separation of byte integrity and semantic fidelity.")),
        embedding_dim=EMBEDDING_DIM,
        embedding_model=EMBEDDING_MODEL,
        source="agent://sep",
        importance=0.8,
        metadata_json=json.dumps({"k": "v"}, sort_keys=True),
        content_hash=compute_content_hash(
            "ops/sep", "Separation of byte integrity and semantic fidelity."
        ),
        namespace="ops",
        tier="warm",
        promotion_state="READY",
        promotion_candidate=1,
        tags=json.dumps(["model:gpt-5"]),
        schema_version=1,
    )
    orig_row = src.get_memory("sep-1")
    assert orig_row is not None
    orig_fp = record_fingerprint(orig_row)

    snap_dir = tmp_path / "sep_snap"
    snapshot(src, snap_dir)
    src.close()

    # Property A: Re-seal manifest over a semantically mutated record (promotion_state
    # changed from READY -> HOT). Byte integrity (verify_manifest) PASSES, while
    # semantic fingerprint comparison catches the state drift!
    mem_path = snap_dir / MEMORIES_NAME
    rec_dict = json.loads(mem_path.read_text(encoding="utf-8").strip())
    rec_dict["promotion_state"] = "HOT"
    mem_path.write_text(json.dumps(rec_dict, sort_keys=True) + "\n", encoding="utf-8")
    new_sha = sha256_file(mem_path)
    manifest_path = snap_dir / MANIFEST_NAME
    mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
    mdata["files"][MEMORIES_NAME] = {"size": mem_path.stat().st_size, "sha256": new_sha}
    mdata["overall_sha256"] = compute_overall({MEMORIES_NAME: new_sha})
    manifest_path.write_text(json.dumps(mdata, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    # Byte integrity passes because manifest was re-sealed:
    assert verify_manifest(snap_dir).overall_sha256 == mdata["overall_sha256"]
    # Semantic fidelity check detects that promotion_state drifted from orig_row:
    dst_drifted = MemoryDB(tmp_path / "dst_drifted.sqlite")
    hydrate(dst_drifted, snap_dir)
    drifted_row = dst_drifted.get_memory("sep-1")
    assert drifted_row is not None
    assert record_fingerprint(drifted_row) != orig_fp
    assert drifted_row["promotion_state"] != orig_row["promotion_state"]
    dst_drifted.close()

    # Property B: Restore exact semantic record (validate_record == [] and
    # record_fingerprint == orig_fp), but append a harmless newline so file bytes
    # differ from manifest. Semantic validation of the record PASSES, while
    # package-byte verification (verify_manifest / hydrate) FAILS closed!
    rec_dict["promotion_state"] = "READY"
    assert validate_record(normalize_record(rec_dict)) == []
    assert record_fingerprint(rec_dict) == orig_fp
    mem_path.write_text(json.dumps(rec_dict, sort_keys=True) + "\n\n", encoding="utf-8")

    dst_tampered = MemoryDB(tmp_path / "dst_tampered.sqlite")
    with pytest.raises(SnapshotChecksumError, match="mismatch"):
        hydrate(dst_tampered, snap_dir)
    assert dst_tampered.count() == 0
    dst_tampered.close()
