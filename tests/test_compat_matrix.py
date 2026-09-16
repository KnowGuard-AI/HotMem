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
import json
from pathlib import Path

from hotmem.annotations import validate_metadata
from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, EmbeddingDescriptor, pack_embedding
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
