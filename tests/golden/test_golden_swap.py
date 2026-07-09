"""Golden swap-file compatibility tests — lock JSONL & JSONL.GZ round trips.

Guards the compatibility promise in file-native-memory-practices.md §10:
"Legacy .jsonl and .jsonl.gz remain readable. JSONL export remains available."
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.swap import hydrate, snapshot

from .conftest import mask


def _first_record_keys(path: Path) -> set[str]:
    with open(path) as f:
        first = f.readline().strip()
    return set(json.loads(first))


def _all_records(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _all_records_gz(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


# ── JSONL round trip ──────────────────────────────────────────────────────────


def test_jsonl_snapshot_record_key_set_is_locked(tmp_path: Path):
    db = MemoryDB(tmp_path / "src.sqlite")
    try:
        db.insert(
            id="a" * 32,
            identifier="vendor_x",
            fact_text="Invoice total was $5000",
            embedding=b"\x00" * (32 * 4),
            content_hash="c" * 64,
        )
        swap = tmp_path / "out.jsonl"
        snapshot(db, swap)

        keys = _first_record_keys(swap)
        # The full extended-record key set — locked so a column rename/add is
        # an intentional, additive change caught here.
        assert keys == {
            "id",
            "identifier",
            "fact_text",
            "embedding_dim",
            "embedding_model",
            "source",
            "importance",
            "metadata_json",
            "content_hash",
            "ttl_seconds",
            "created_at",
            "namespace",
            "tier",
            "memory_type",
            "source_uri",
            "source_format",
            "source_checksum",
            "byte_offset",
            "byte_length",
            "updated_at",
            "snapshot_id",
            "promotion_state",
            "promotion_candidate",
            "parent_memory",
            "related_memories",
            "tags",
            "schema_version",
            "fact_summary",
            "provenance_json",
            "embedding_b64",
        }
    finally:
        db.close()


def test_jsonl_snapshot_hydrate_round_trip_preserves_facts(tmp_path: Path):
    """snapshot → (fresh DB) → hydrate → snapshot is fact-stable."""
    src = MemoryDB(tmp_path / "src.sqlite")
    swap1 = tmp_path / "a.jsonl"
    try:
        src.insert(
            id="1" * 32,
            identifier="u",
            fact_text="first fact",
            embedding=b"\x00" * (32 * 4),
            content_hash="a" * 64,
        )
        src.insert(
            id="2" * 32,
            identifier="u",
            fact_text="second fact",
            embedding=b"\x00" * (32 * 4),
            content_hash="b" * 64,
        )
        snapshot(src, swap1)
        facts_before = sorted(r["fact_text"] for r in _all_records(swap1))
    finally:
        src.close()

    dst = MemoryDB(tmp_path / "dst.sqlite")
    swap2 = tmp_path / "b.jsonl"
    try:
        result = hydrate(dst, swap1)
        assert result.loaded == 2
        assert result.skipped_dupes == 0
        snapshot(dst, swap2)
        facts_after = sorted(r["fact_text"] for r in _all_records(swap2))
    finally:
        dst.close()

    assert facts_before == facts_after == ["first fact", "second fact"]


def test_jsonl_hydrate_dedup_is_stable(tmp_path: Path):
    """Re-hydrating the same swap into the same DB skips all duplicates."""
    db = MemoryDB(tmp_path / "d.sqlite")
    swap = tmp_path / "s.jsonl"
    swap.write_text(
        json.dumps({"identifier": "u", "fact_text": "stable fact", "content_hash": "h" * 64}) + "\n"
    )
    try:
        first = hydrate(db, swap)
        assert first.loaded == 1 and first.skipped_dupes == 0
        second = hydrate(db, swap)
        assert second.loaded == 0 and second.skipped_dupes == 1
    finally:
        db.close()


def test_jsonl_hydrate_accepts_minimal_v01_record(tmp_path: Path):
    """A v0.1-style record (only identifier + fact_text) still hydrates."""
    db = MemoryDB(tmp_path / "v01.sqlite")
    swap = tmp_path / "v01.jsonl"
    swap.write_text(json.dumps({"identifier": "legacy", "fact_text": "old fact"}) + "\n")
    try:
        result = hydrate(db, swap)
        assert result.loaded == 1
        rows = db.all_rows()
        assert rows[0]["identifier"] == "legacy"
        assert rows[0]["fact_text"] == "old fact"
        # Defaults are applied, not required in the payload:
        assert rows[0]["importance"] == 0.5
        assert rows[0]["source"] == "swap"
    finally:
        db.close()


# ── JSONL.GZ round trip ───────────────────────────────────────────────────────


def test_jsonl_gz_snapshot_hydrate_round_trip(tmp_path: Path):
    db = MemoryDB(tmp_path / "gz.sqlite")
    swap = tmp_path / "out.jsonl.gz"
    try:
        db.insert(
            id="9" * 32,
            identifier="g",
            fact_text="compressed fact",
            embedding=b"\x00" * (32 * 4),
            content_hash="d" * 64,
        )
        result = snapshot(db, swap)
        assert result.exported == 1
        assert swap.exists()

        records = _all_records_gz(swap)
        assert len(records) == 1
        assert records[0]["fact_text"] == "compressed fact"
        assert "embedding_b64" in records[0]
    finally:
        db.close()

    dst = MemoryDB(tmp_path / "gz_dst.sqlite")
    try:
        loaded = hydrate(dst, swap)
        assert loaded.loaded == 1
        assert dst.count() == 1
    finally:
        dst.close()


def test_jsonl_gz_record_key_set_matches_plain_jsonl(tmp_path: Path):
    """The .gz variant must serialize the same record key set as plain .jsonl."""
    db = MemoryDB(tmp_path / "k.sqlite")
    try:
        db.insert(
            id="1" * 32,
            identifier="u",
            fact_text="x",
            embedding=b"\x00" * (32 * 4),
            content_hash="a" * 64,
        )
        gz = tmp_path / "g.jsonl.gz"
        plain = tmp_path / "g.jsonl"
        snapshot(db, gz)
        snapshot(db, plain)
        gz_keys = set(_all_records_gz(gz)[0])
        plain_keys = set(_all_records(plain)[0])
        assert gz_keys == plain_keys
    finally:
        db.close()


def test_jsonl_gz_malformed_raises_clear_error(tmp_path: Path):
    """A corrupt .gz must surface a clear ValueError, not a silent skip."""
    bad = tmp_path / "bad.jsonl.gz"
    bad.write_bytes(b"not a gzip stream at all")
    db = MemoryDB(tmp_path / "bad.sqlite")
    try:
        with pytest.raises(ValueError, match="malformed compressed swap file"):
            hydrate(db, bad)
    finally:
        db.close()


# ── SQLite fast-path round trip ───────────────────────────────────────────────


def test_sqlite_hydrate_fast_path_preserves_facts(tmp_path: Path):
    """The SQLite→SQLite import fast path reuses embeddings and preserves facts."""
    src = MemoryDB(tmp_path / "src.sqlite")
    try:
        src.insert(
            id="5" * 32,
            identifier="fp",
            fact_text="fast path fact",
            embedding=b"\x0a" * (32 * 4),
            content_hash="e" * 64,
        )
    finally:
        src.close()

    dst = MemoryDB(tmp_path / "dst.sqlite")
    try:
        result = hydrate(dst, tmp_path / "src.sqlite")
        assert result.loaded == 1
        rows = dst.all_rows()
        assert rows[0]["fact_text"] == "fast path fact"
    finally:
        dst.close()


# ── Additive: snapshot is JSON-parseable and types are stable ──────────────────


def test_jsonl_snapshot_values_are_json_serializable_and_maskable(tmp_path: Path):
    """Every snapshot record must round-trip through json without custom types."""
    db = MemoryDB(tmp_path / "ser.sqlite")
    swap = tmp_path / "ser.jsonl"
    try:
        db.insert(
            id="7" * 32,
            identifier="u",
            fact_text="serializable",
            embedding=b"\x00" * (32 * 4),
            content_hash="f" * 64,
            metadata_json='{"k": 1}',
        )
        snapshot(db, swap)
        records = _all_records(swap)
        masked = mask(records[0])
        # The masked record must be a pure dict of sentinels — no raw bytes.
        assert isinstance(masked, dict)
        assert all(isinstance(v, str) for v in masked.values())
    finally:
        db.close()
