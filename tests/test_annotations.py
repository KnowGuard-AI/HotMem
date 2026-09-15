"""Lossless annotation envelope — issue #79 acceptance tests.

Validation taxonomy (structural only, unknown preserved), deterministic
merge matrix, transport round-trips (JSONL, gz, Snapshot v2, package,
delta), idempotent replay, conflict retention, and fingerprint sensitivity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.annotations import (
    AnnotationValidationError,
    merge_annotations,
    validate_annotations,
    validate_metadata,
)
from hotmem.db import MemoryDB
from hotmem.interchange.delta import apply_delta, produce_delta
from hotmem.interchange.fingerprint import state_fingerprint
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package
from hotmem.snapshot import hydrate as snapshot_hydrate
from hotmem.snapshot import snapshot as snapshot_write
from hotmem.snapshot.writer import write_snapshot_v2
from hotmem.swap import add_memory


def _envelope(
    ns: str = "org.example.entities",
    items: list | None = None,
    **extra: object,
) -> dict:
    return {
        "schema_version": 1,
        "namespaces": {
            ns: items
            if items is not None
            else [{"id": "vendor-x", "type": "Organization", "confidence": 0.9}]
        },
        **extra,
    }


def _meta(annotations: dict) -> dict:
    return {"annotations": annotations}


# ── validation taxonomy ──────────────────────────────────────────────────────


def test_valid_envelope_with_full_surface():
    envelope = _envelope(
        items=[
            {
                "id": "vendor-x",
                "type": "Organization",
                "confidence": 0.92,
                "scope": "finance",
                "authority": "manual",
                "valid_time": {"start": "2026-01-01"},
                "classification": "internal",
                "evidence": [
                    {"memory_id": "mem-1"},  # local, resolved
                    {"uri": "https://example.com/doc"},  # external, never fetched
                    "urn:example:plain-string",  # external string form
                ],
            }
        ],
        producers={"org.example.enricher": {"version": "1.2"}},
    )
    validate_annotations(envelope, known_ids={"mem-1"})  # no raise


def test_malformed_envelope_structural_errors():
    cases = [
        ([], "must be a JSON object"),
        ({"schema_version": 2}, "must be 1"),
        ({"schema_version": 1, "namespaces": "nope"}, "must be an object"),
        ({"schema_version": 1, "namespaces": {"singlelabel": []}}, "reverse-DNS"),
        ({"schema_version": 1, "namespaces": {"org.bad-Label": []}}, "lowercase"),
        ({"schema_version": 1, "namespaces": {"org.x": {"not": "list"}}}, "list of item"),
        ({"schema_version": 1, "namespaces": {"org.x": ["not-an-object"]}}, "objects"),
        (
            {
                "schema_version": 1,
                "namespaces": {"org.x": [{"type": "Organization"}]},
            },
            "non-empty string 'id'",
        ),
        (
            {"schema_version": 1, "namespaces": {"org.x": [{"id": "a", "confidence": 1.5}]}},
            "in \[0, 1\]",
        ),
        (
            {"schema_version": 1, "namespaces": {"org.x": [{"id": "a", "confidence": "high"}]}},
            "must be a number",
        ),
        (
            {"schema_version": 1, "namespaces": {"org.x": [{"id": "a", "evidence": {"x": 1}}]}},
            "must be a list",
        ),
        (
            {
                "schema_version": 1,
                "namespaces": {"org.x": [{"id": "a", "evidence": [{"weird": 1}]}]},
            },
            "evidence entries must carry",
        ),
        (
            {
                "schema_version": 1,
                "namespaces": {"org.x": [{"id": "a"}, {"id": "a"}]},
            },
            "duplicate item id",
        ),
        ({"schema_version": 1, "producers": "nope"}, "must be an object"),
    ]
    for payload, needle in cases:
        with pytest.raises(AnnotationValidationError, match=needle):
            validate_annotations(payload)


def test_unknown_namespaces_and_keys_are_preserved():
    """Unknown content never fails validation and round-trips losslessly."""
    envelope = _envelope(
        items=[{"id": "a", "future_field": {"nested": [1, 2, {"k": "v"}]}}],
        producers={"org.unknown.tool": {"anything": True}},
    )
    envelope["future_top_level"] = {"preserved": ["in", "order"]}
    validate_annotations(envelope)  # unknown never fails
    # A future schema version IS a known field: gated, not silently read.
    with pytest.raises(AnnotationValidationError, match="must be 1"):
        validate_annotations({**envelope, "schema_version": 2})


def test_local_evidence_resolution_and_dangling_errors():
    envelope = _envelope(items=[{"id": "a", "evidence": [{"memory_id": "mem-fwd"}]}])
    # Without a resolver: structural check only.
    validate_annotations(envelope)
    # Forward reference resolved when the id set includes the whole package.
    validate_annotations(envelope, known_ids={"mem-fwd", "mem-other"})
    # Dangling local reference: actionable error.
    with pytest.raises(AnnotationValidationError, match="unknown memory 'mem-fwd'"):
        validate_annotations(envelope, known_ids={"mem-other"})
    # External URIs are never fetched or validated beyond non-emptiness.
    external = _envelope(items=[{"id": "a", "evidence": [{"uri": "https://anywhere.example/x"}]}])
    validate_annotations(external, known_ids=set())


def test_envelope_limits_enforced():
    big = _envelope(items=[{"id": f"i{n}", "blob": "x" * 100} for n in range(1001)])
    with pytest.raises(AnnotationValidationError, match="1000 total items"):
        validate_annotations(big)
    wide = _envelope(items=[{"id": "a", "blob": "x" * (130 * 1024)}])
    with pytest.raises(AnnotationValidationError, match="exceeds"):
        validate_annotations(wide)
    deep_value = {"v": 1}
    for _ in range(12):
        deep_value = {"nested": deep_value}
    with pytest.raises(AnnotationValidationError, match="maximum JSON depth"):
        envelope = {"schema_version": 1, "namespaces": {"org.x": [{"id": "a", "d": deep_value}]}}
        validate_annotations(envelope)


def test_metadata_without_annotations_has_zero_overhead_path():
    validate_metadata(None)  # no raise
    validate_metadata({"unrelated": True})  # one membership check
    validate_metadata({"annotations": {"schema_version": 1}})  # valid minimal
    with pytest.raises(AnnotationValidationError):
        validate_metadata({"annotations": {"schema_version": 9}})


# ── merge matrix ──────────────────────────────────────────────────────────────


def test_merge_rules_by_namespace_and_item_id():
    target = _envelope(
        items=[
            {"id": "a", "confidence": 0.9},
            {"id": "b", "confidence": 0.8},
        ]
    )
    incoming = _envelope(
        items=[
            {"id": "a", "confidence": 0.9},  # same id, same content -> no-op
            {"id": "b", "confidence": 0.1},  # same id, different content -> conflict
            {"id": "c", "confidence": 0.7},  # disjoint -> merged
        ]
    )
    outcome = merge_annotations(target, incoming)
    assert outcome.merged_items == 1
    assert len(outcome.conflicts) == 1
    conflict = outcome.conflicts[0]
    assert conflict.item_id == "b"
    assert conflict.target == {"id": "b", "confidence": 0.8}  # retained
    assert conflict.incoming == {"id": "b", "confidence": 0.1}  # preserved
    merged_ids = {item["id"] for item in outcome.merged["namespaces"]["org.example.entities"]}
    assert merged_ids == {"a", "b", "c"}
    assert outcome.merged["namespaces"]["org.example.entities"][1]["confidence"] == 0.8


def test_merge_is_map_order_insensitive():
    """MAP insertion order never affects identity — arrays are data, kept."""
    first_in = {
        "namespaces": {"org.example.entities": [{"id": "a", "note": "x"}]},
        "schema_version": 1,
    }
    second_in = {
        "schema_version": 1,
        "namespaces": {"org.example.entities": [{"note": "x", "id": "a"}]},
    }
    first = merge_annotations(_envelope(items=[]), first_in).merged
    second = merge_annotations(_envelope(items=[]), second_in).merged
    assert first is not None and second is not None
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_merge_idempotent_replay_is_noop():
    target = _envelope()
    outcome = merge_annotations(target, target)
    assert outcome.merged is None
    assert outcome.conflicts == []
    assert outcome.merged_items == 0


def test_merge_into_empty_and_from_empty():
    envelope = _envelope()
    assert merge_annotations({}, envelope).merged == envelope
    assert merge_annotations(envelope, {}).merged is None


def test_merge_preserves_producers_and_unknown_keys():
    target = _envelope(producers={"org.a": {"version": "1"}}, future_top={"keep": 1})
    incoming = _envelope(producers={"org.b": {"version": "2"}})
    outcome = merge_annotations(target, incoming)
    assert outcome.merged["producers"] == {"org.a": {"version": "1"}, "org.b": {"version": "2"}}
    assert outcome.merged["future_top"] == {"keep": 1}


# ── transport round-trips: lossless movement ─────────────────────────────────


def _seed_annotated_db(tmp_path: Path, name: str) -> MemoryDB:
    db = MemoryDB(tmp_path / f"{name}.sqlite")
    add_memory(
        db,
        "vendor-x",
        "vendor x invoices are paid net 30",
        metadata=_meta(_envelope(items=[{"id": "vendor-x", "type": "Organization"}])),
    )
    return db


def _assert_envelope_survives(db: MemoryDB, expected_items: int = 1) -> None:
    rows = db.all_rows()
    metadata = json.loads(rows[0]["metadata_json"])
    envelope = metadata["annotations"]
    assert envelope["schema_version"] == 1
    items = envelope["namespaces"]["org.example.entities"]
    assert len(items) == expected_items
    assert items[0]["id"] == "vendor-x"
    assert items[0]["type"] == "Organization"


def test_round_trip_through_jsonl_and_gz(tmp_path: Path):
    db = _seed_annotated_db(tmp_path, "src")
    swap = tmp_path / "swap.jsonl"
    snapshot_write(db, swap)
    for path in (swap, tmp_path / "swap.jsonl.gz"):
        if path != swap:
            import gzip

            with gzip.open(path, "wt", encoding="utf-8") as gz:
                gz.write(swap.read_text())
        target = MemoryDB(tmp_path / f"t-{path.name}.sqlite")
        result = snapshot_hydrate(target, path)
        assert result.loaded == 1
        _assert_envelope_survives(target)
        target.close()
    db.close()


def test_round_trip_through_snapshot_v2(tmp_path: Path):
    db = _seed_annotated_db(tmp_path, "v2src")
    snap_dir = tmp_path / "snap"
    write_snapshot_v2(db, snap_dir)
    target = MemoryDB(tmp_path / "v2target.sqlite")
    result = snapshot_hydrate(target, snap_dir)
    assert result.loaded == 1
    _assert_envelope_survives(target)
    db.close()
    target.close()


def test_round_trip_through_package_and_repeat_hydrate(tmp_path: Path):
    db = _seed_annotated_db(tmp_path, "pkgsrc")
    pkg = tmp_path / "pkg"
    write_package(db, pkg, gz=True)

    target = MemoryDB(tmp_path / "pkgtarget.sqlite")
    first = hydrate_package(target, pkg)
    assert first.loaded == 1
    _assert_envelope_survives(target)
    second = hydrate_package(target, pkg)
    assert second.loaded == 0 and second.skipped_dupes == 1
    assert second.annotations_merged == 0  # identical replay is a pure no-op
    db.close()
    target.close()


def test_hydrate_merges_annotation_only_changes(tmp_path: Path):
    """The #79 battleground: same content, new annotations -> merged, not skipped."""
    db = _seed_annotated_db(tmp_path, "mergesrc")
    pkg = tmp_path / "pkg1"
    write_package(db, pkg, gz=True)

    target = MemoryDB(tmp_path / "mergetarget.sqlite")
    hydrate_package(target, pkg)

    # A second package: same canonical fact, an ADDITIONAL disjoint item.
    producer = MemoryDB(tmp_path / "producer.sqlite")
    hydrate_package(producer, pkg)
    # Same content hash, richer envelope.
    row = producer.all_rows()[0]
    enriched = _envelope(
        items=[
            {"id": "vendor-x", "type": "Organization"},
            {"id": "vendor-x-alias", "type": "Alias", "value": "VX"},
        ]
    )
    producer.update_metadata_json(row["id"], json.dumps(_meta(enriched), sort_keys=True))
    pkg2 = tmp_path / "pkg2"
    write_package(producer, pkg2, gz=True)

    result = hydrate_package(target, pkg2)
    assert result.loaded == 0  # canonical content unchanged
    assert result.skipped_dupes == 1
    assert result.annotations_merged == 1  # the disjoint alias item merged
    assert result.annotation_conflicts == 0
    _assert_envelope_survives(target, expected_items=2)
    db.close()
    target.close()
    producer.close()


def test_hydrate_conflicts_retain_both_versions(tmp_path: Path):
    """Same id, different content: target retained, incoming preserved, reported."""
    db = _seed_annotated_db(tmp_path, "confsrc")
    pkg = tmp_path / "pkgA"
    write_package(db, pkg, gz=True)
    target = MemoryDB(tmp_path / "conftarget.sqlite")
    hydrate_package(target, pkg)

    producer = MemoryDB(tmp_path / "confproducer.sqlite")
    hydrate_package(producer, pkg)
    row = producer.all_rows()[0]
    conflicting = _envelope(
        items=[{"id": "vendor-x", "type": "Person"}],  # same id, different content
    )
    producer.update_metadata_json(row["id"], json.dumps(_meta(conflicting), sort_keys=True))
    pkgB = tmp_path / "pkgB"
    write_package(producer, pkgB, gz=True)

    result = hydrate_package(target, pkgB)
    assert result.annotation_conflicts == 1
    assert result.annotations_merged == 0
    metadata = json.loads(target.all_rows()[0]["metadata_json"])
    # Target version retained — never last-write-wins.
    items = metadata["annotations"]["namespaces"]["org.example.entities"]
    assert items[0]["type"] == "Organization"
    db.close()
    target.close()
    producer.close()


def test_annotation_only_delta_through_real_cas(tmp_path: Path):
    """Annotation-only changes flow as CAS upserts; fingerprints react."""
    db = _seed_annotated_db(tmp_path, "deltasrc")
    base_pkg = tmp_path / "base.pkg"
    write_package(db, base_pkg, gz=True)

    producer = MemoryDB(tmp_path / "deltaproducer.sqlite")
    hydrate_package(producer, base_pkg)
    original_fingerprint = state_fingerprint([dict(r) for r in producer.all_rows()])
    row = producer.all_rows()[0]
    enriched = _envelope(
        items=[{"id": "vendor-x", "type": "Organization", "source_verified": True}]
    )
    producer.update_metadata_json(row["id"], json.dumps(_meta(enriched), sort_keys=True))
    assert state_fingerprint([dict(r) for r in producer.all_rows()]) != (original_fingerprint), (
        "annotation changes must be integrity-visible"
    )

    delta_dir = tmp_path / "delta"
    produce_delta(producer, base_pkg, delta_dir)

    receiver = MemoryDB(tmp_path / "deltareceiver.sqlite")
    hydrate_package(receiver, base_pkg)  # receiver starts from the base state
    result = apply_delta(receiver, delta_dir)
    assert result.applied == 1
    metadata = json.loads(receiver.all_rows()[0]["metadata_json"])
    assert (
        metadata["annotations"]["namespaces"]["org.example.entities"][0]["source_verified"] is True
    )
    # Idempotent replay: zero applies, no annotation churn.
    replay = apply_delta(receiver, delta_dir)
    assert replay.applied == 0
    db.close()
    producer.close()
    receiver.close()


def test_invalid_envelope_record_is_counted_invalid(tmp_path: Path):
    db = _seed_annotated_db(tmp_path, "validsrc")
    pkg = tmp_path / "validpkg"
    write_package(db, pkg, gz=True)

    # Corrupt one record's envelope in a second package: malformed known
    # structure — the record is invalid, not the whole restore.
    producer = MemoryDB(tmp_path / "badproducer.sqlite")
    hydrate_package(producer, pkg)
    row = producer.all_rows()[0]
    producer.update_metadata_json(
        row["id"], json.dumps(_meta({"schema_version": 9, "namespaces": {}}), sort_keys=True)
    )
    bad_pkg = tmp_path / "bad.pkg"
    write_package(producer, bad_pkg, gz=True)

    target = MemoryDB(tmp_path / "badtarget.sqlite")
    result = hydrate_package(target, bad_pkg)
    assert result.invalid == 1  # counted invalid and skipped
    assert target.count() == 0  # never stored malformed
    db.close()
    target.close()
    producer.close()


def test_server_add_validates_envelope_with_actionable_error(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hotmem.server import create_app

    app = create_app(db_path=tmp_path / "server.sqlite")
    with TestClient(app) as client:
        bad = client.post(
            "/v1/add",
            json={
                "identifier": "v",
                "fact": "fact text",
                "metadata": {"annotations": {"schema_version": 9}},
            },
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid_annotations"
        assert "must be 1" in bad.json()["message"]

        good = client.post(
            "/v1/add",
            json={
                "identifier": "v",
                "fact": "fact text",
                "metadata": _meta(_envelope()),
            },
        )
        assert good.status_code == 200
        row = MemoryDB(tmp_path / "server.sqlite").all_rows()[0]
        assert json.loads(row["metadata_json"])["annotations"]["schema_version"] == 1
