"""hotmem-delta-v1 apply tests (#73) — CAS semantics, idempotency, atomicity.

The #73 acceptance core: replaying the same delta creates no duplicates,
incompatible or missing base state is detected, conflicts are visible and
actionable (never silently discarded), and any failure leaves the target's
canonical state, checkpoint, and receipt unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.events import EventType, query_events
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.delta import (
    DeltaConflictError,
    PackageError,
    apply_delta,
    produce_delta,
    verify_delta,
)
from hotmem.interchange.fingerprint import state_fingerprint
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package
from hotmem.search import search_memories


def _fact(i: int) -> str:
    return f"delta apply fact {i} about acme operations"


def _make_source(tmp_path: Path, n: int = 3) -> MemoryDB:
    db = MemoryDB(tmp_path / "src.sqlite")
    for i in range(n):
        fact = _fact(i)
        db.insert(
            id=f"m{i}",
            identifier=f"ident-{i}",
            fact_text=fact,
            embedding=pack_embedding(embed_text(fact)),
            embedding_model="hotmem-hash-v1",
            content_hash=compute_content_hash(f"ident-{i}", fact),
            namespace="team",
        )
    return db


def _fingerprint_state(db: MemoryDB) -> tuple:
    """Canonical observable state: rows + checkpoints + sync events."""
    rows = tuple(
        (r["id"], r["content_hash"], r["promotion_state"], r["namespace"])
        for r in sorted(db.all_rows(), key=lambda r: r["id"])
    )
    checkpoints = tuple(db._conn.execute("SELECT * FROM sync_checkpoints ORDER BY id").fetchall())
    sync_events = tuple(
        e["seq"] for e in query_events(db, event_type=EventType.SYNC_APPLIED, limit=100)["events"]
    )
    return rows, checkpoints, sync_events


@pytest.fixture
def synced(tmp_path: Path) -> tuple[MemoryDB, MemoryDB, Path, MemoryDB]:
    """Source with a base package, a hydrated receiver, and a first delta."""
    source = _make_source(tmp_path)
    base_pkg = tmp_path / "base"
    write_package(source, base_pkg)

    receiver = MemoryDB(tmp_path / "receiver.sqlite")
    hydrate_package(receiver, base_pkg)

    # Source mutates: new record + promotion transition.
    new_fact = _fact(3)
    source.insert(
        id="m3",
        identifier="ident-3",
        fact_text=new_fact,
        embedding=pack_embedding(embed_text(new_fact)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("ident-3", new_fact),
        namespace="team",
    )
    source.update_promotion_state("m0", "WARM")

    delta_dir = tmp_path / "delta1"
    produce_delta(source, base_pkg, delta_dir)
    return source, receiver, base_pkg, delta_dir


# ── happy path + idempotency ────────────────────────────────────────────────


def test_apply_applies_all_ops_and_reaches_source_state(synced: tuple):
    source, receiver, base_pkg, delta_dir = synced
    result = apply_delta(receiver, delta_dir)
    assert result.applied == 2  # m3 add + m0 promotion rewrite
    assert result.skipped == 0
    assert result.conflicts == []

    # Canonical state equality with the source (the #73 acceptance core).
    assert state_fingerprint(receiver.all_rows()) == state_fingerprint(source.all_rows())
    row = next(r for r in receiver.all_rows() if r["id"] == "m0")
    assert row["promotion_state"] == "WARM"  # lifecycle transition synced

    # Checkpoint + receipt recorded.
    cp = receiver.latest_sync_checkpoint(
        json.loads((base_pkg / "manifest.json").read_text())["logical_id"]
    )
    assert cp is not None and cp["applied_ops"] == 2
    events = query_events(receiver, event_type=EventType.SYNC_APPLIED, limit=10)["events"]
    assert len(events) == 1 and events[0]["payload"]["applied"] == 2


def test_apply_is_idempotent_replay_creates_no_duplicates(synced: tuple):
    source, receiver, base_pkg, delta_dir = synced
    first = apply_delta(receiver, delta_dir)
    assert first.applied == 2
    after_first = state_fingerprint(receiver.all_rows())

    second = apply_delta(receiver, delta_dir)
    assert second.applied == 0
    assert second.skipped == 2
    assert state_fingerprint(receiver.all_rows()) == after_first
    assert receiver.count() == 4  # no duplicates


def test_applied_rows_carry_provenance_and_scope(synced: tuple):
    """Source provenance and namespace/classification survive delta transfer."""
    source, receiver, base_pkg, delta_dir = synced
    source.insert(
        id="m9",
        identifier="ident-9",
        fact_text=_fact(9),
        embedding=pack_embedding(embed_text(_fact(9))),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("ident-9", _fact(9)),
        namespace="team",
        tags='["ops"]',
        provenance_json='{"origin": "manual", "actor": "human:z"}',
        ttl_seconds=120,
    )
    delta2 = synced[2].parent / "delta2"
    produce_delta(source, base_pkg, delta2)
    apply_delta(receiver, delta2)

    row = next(r for r in receiver.all_rows() if r["id"] == "m9")
    assert row["namespace"] == "team"
    assert row["tags"] == '["ops"]'
    assert json.loads(row["provenance_json"]) == {"origin": "manual", "actor": "human:z"}
    assert row["ttl_seconds"] == 120
    assert row["source_uri"] == ""


def test_retrieval_equivalence_after_delta(synced: tuple):
    source, receiver, base_pkg, delta_dir = synced
    apply_delta(receiver, delta_dir)
    hits_src = [
        (r["identifier"], round(r["score"], 6))
        for r in search_memories(source, "acme operations fact 3", top_k=3)
    ]
    hits_rcv = [
        (r["identifier"], round(r["score"], 6))
        for r in search_memories(receiver, "acme operations fact 3", top_k=3)
    ]
    assert hits_src == hits_rcv


# ── conflicts: visible, actionable, target unchanged ────────────────────────


def test_missing_base_detected_on_empty_receiver(tmp_path: Path, synced: tuple):
    """A receiver with no base state gets base_missing, target unchanged."""
    source, _receiver, base_pkg, delta_dir = synced
    empty = MemoryDB(tmp_path / "empty.sqlite")

    before = _fingerprint_state(empty)
    with pytest.raises(DeltaConflictError) as excinfo:
        apply_delta(empty, delta_dir)
    reasons = {c.reason for c in excinfo.value.conflicts}
    assert "base_missing" in reasons
    assert _fingerprint_state(empty) == before  # target untouched
    empty.close()


def test_divergent_receiver_edit_conflicts(tmp_path: Path, synced: tuple):
    """A receiver-side edit after cloning diverges from the base image."""
    source, receiver, base_pkg, delta_dir = synced
    # Diverge: edit the same record the delta wants to rewrite.
    receiver.update_promotion_state("m0", "ARCHIVED")
    before = _fingerprint_state(receiver)

    with pytest.raises(DeltaConflictError) as excinfo:
        apply_delta(receiver, delta_dir)
    conflict = next(c for c in excinfo.value.conflicts if c.record_id == "m0")
    assert conflict.reason == "state_divergence"
    assert conflict.expected and conflict.actual and conflict.expected != conflict.actual

    assert _fingerprint_state(receiver) == before  # nothing applied


def test_id_reuse_conflict(tmp_path: Path, synced: tuple):
    source, _receiver, base_pkg, delta_dir = synced
    other = MemoryDB(tmp_path / "other.sqlite")
    other.insert(
        id="m3",  # delta expects this id to be absent
        identifier="unrelated",
        fact_text="unrelated content entirely",
        embedding=pack_embedding(embed_text("unrelated content entirely")),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("unrelated", "unrelated content entirely"),
    )
    before = _fingerprint_state(other)
    with pytest.raises(DeltaConflictError) as excinfo:
        apply_delta(other, delta_dir)
    assert any(c.reason == "id_reuse" and c.record_id == "m3" for c in excinfo.value.conflicts)
    assert _fingerprint_state(other) == before
    other.close()


def test_conflicts_list_every_divergence_not_just_first(tmp_path: Path, synced: tuple):
    source, receiver, base_pkg, delta_dir = synced
    receiver.update_promotion_state("m0", "ARCHIVED")
    receiver.update_promotion_state(
        "m1", "ARCHIVED"
    )  # m1 unchanged by delta, but still diverges? No — m1 has no op. Diverge m3 instead:
    receiver.insert(
        id="m3",
        identifier="divergent",
        fact_text="divergent local edit",
        embedding=pack_embedding(embed_text("divergent local edit")),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("divergent", "divergent local edit"),
    )
    with pytest.raises(DeltaConflictError) as excinfo:
        apply_delta(receiver, delta_dir)
    ids = {c.record_id for c in excinfo.value.conflicts}
    assert {"m0", "m3"} <= ids  # both divergences reported


# ── integrity, versions, tombstones ────────────────────────────────────────


def test_verify_delta_rejects_unsupported_operation(tmp_path: Path, synced: tuple):
    source, _receiver, _base_pkg, delta_dir = synced
    ops_path = delta_dir / "operations.jsonl"
    ops = [json.loads(line) for line in ops_path.read_text().splitlines()]
    ops[0]["op"] = "delete"  # tombstones rejected explicitly (delta-v1 §7)
    payload = (
        "\n".join(json.dumps(o, sort_keys=True, separators=(",", ":")) for o in ops) + "\n"
    ).encode()
    ops_path.write_bytes(payload)
    manifest = json.loads((delta_dir / "manifest.json").read_text())
    import hashlib

    manifest["files"]["operations.jsonl"] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (delta_dir / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(PackageError, match="unsupported_operation"):
        verify_delta(delta_dir)
    with pytest.raises(PackageError):
        apply_delta(_make_source(tmp_path), delta_dir)


def test_apply_rejects_fingerprint_version_mismatch(tmp_path: Path, synced: tuple):
    _source, _receiver, _base_pkg, delta_dir = synced
    manifest = json.loads((delta_dir / "manifest.json").read_text())
    manifest["fingerprint_version"] = 99
    (delta_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="unsupported_fingerprint"):
        apply_delta(_make_source(tmp_path), delta_dir)


def test_corrupted_operations_halt_apply(tmp_path: Path, synced: tuple):
    _source, receiver, _base_pkg, delta_dir = synced
    ops_path = delta_dir / "operations.jsonl"
    data = bytearray(ops_path.read_bytes())
    data[10] ^= 0xFF
    ops_path.write_bytes(bytes(data))
    before = _fingerprint_state(receiver)
    with pytest.raises(PackageError):
        apply_delta(receiver, delta_dir)
    assert _fingerprint_state(receiver) == before


# ── atomicity: failure leaves state, checkpoint, receipt unchanged ─────────


def test_interruption_rolls_back_records_checkpoint_and_receipt(
    tmp_path: Path, synced: tuple, monkeypatch: pytest.MonkeyPatch
):
    source, receiver, base_pkg, delta_dir = synced
    before = _fingerprint_state(receiver)

    real_insert = MemoryDB.insert
    calls = {"n": 0}

    def failing_insert(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the second record write
            raise RuntimeError("simulated crash mid-apply")
        return real_insert(self, **kwargs)

    monkeypatch.setattr(MemoryDB, "insert", failing_insert)
    with pytest.raises(RuntimeError, match="simulated crash"):
        apply_delta(receiver, delta_dir)

    assert _fingerprint_state(receiver) == before  # rows + checkpoint + receipt unchanged
    assert receiver.count() == 3  # first write rolled back too
    monkeypatch.undo()

    # Recovery: the same delta applies cleanly after the interruption.
    result = apply_delta(receiver, delta_dir)
    assert result.applied == 2


def test_chained_deltas_apply_cleanly(tmp_path: Path):
    """base -> delta1 -> delta2 against the same base: CAS makes chaining
    deterministic, and the receiver converges to the source."""
    source = _make_source(tmp_path)
    base_pkg = tmp_path / "base"
    write_package(source, base_pkg)
    receiver = MemoryDB(tmp_path / "receiver.sqlite")
    hydrate_package(receiver, base_pkg)

    # Round 1: add m3.
    fact3 = _fact(3)
    source.insert(
        id="m3",
        identifier="ident-3",
        fact_text=fact3,
        embedding=pack_embedding(embed_text(fact3)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("ident-3", fact3),
        namespace="team",
    )
    delta1 = tmp_path / "delta1"
    produce_delta(source, base_pkg, delta1)
    assert apply_delta(receiver, delta1).applied == 1

    # Round 2: change m1 + add m4 — delta2 covers everything since base.
    source.update_promotion_state("m1", "WARM")
    fact4 = _fact(4)
    source.insert(
        id="m4",
        identifier="ident-4",
        fact_text=fact4,
        embedding=pack_embedding(embed_text(fact4)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("ident-4", fact4),
        namespace="team",
    )
    delta2 = tmp_path / "delta2"
    produce_delta(source, base_pkg, delta2)
    result = apply_delta(receiver, delta2)
    assert result.applied == 2  # m1 rewrite + m4 add
    assert result.skipped == 1  # m3 already applied by delta1

    assert state_fingerprint(receiver.all_rows()) == state_fingerprint(source.all_rows())

    # Whole-brain equivalence: base + deltas == fresh full clone.
    fresh = MemoryDB(tmp_path / "fresh.sqlite")
    write_package(source, tmp_path / "final")
    hydrate_package(fresh, tmp_path / "final")
    assert state_fingerprint(fresh.all_rows()) == state_fingerprint(receiver.all_rows())
