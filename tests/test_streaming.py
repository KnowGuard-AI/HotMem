"""Structural performance guards — streaming exports, bounded batches (#67/#69).

These lock the performance posture as behavior: exports must stream (never
materialize all rows), hydration must insert in bounded batches with
DB-backed dedup (never load the destination hash set), and no writer may
per-record commit. Real numbers (throughput, peak RSS, embedding calls) are
measured by bench/interchange (C15); these tests prove the structure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.package import write_package
from hotmem.snapshot.writer import write_snapshot_v2
from hotmem.swap import hydrate, snapshot


def _populate(db: MemoryDB, n: int) -> None:
    """Insert n rows in batches (fast: executemany path via insert_many_ignore)."""
    from hotmem.db import MemoryRecord

    batch: list[MemoryRecord] = []
    for i in range(n):
        fact = f"streaming guard fact {i} about topic {i % 97}"
        batch.append(
            MemoryRecord(
                id=f"g{i:06d}",
                identifier=f"guard-{i}",
                fact_text=fact,
                embedding=pack_embedding(embed_text(fact)),
                content_hash=compute_content_hash(f"guard-{i}", fact),
            )
        )
        if len(batch) >= 1000:
            db.insert_many_ignore(batch)
            batch = []
    if batch:
        db.insert_many_ignore(batch)


@pytest.fixture
def populated(tmp_path: Path) -> MemoryDB:
    db = MemoryDB(tmp_path / "src.sqlite")
    _populate(db, 1200)  # > one flush batch
    return db


def _forbid_all_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, **kwargs):
        raise AssertionError("all_rows() called — export must stream via iter_rows()")

    monkeypatch.setattr(MemoryDB, "all_rows", boom)


def test_v2_snapshot_streams_rows(populated: MemoryDB, tmp_path: Path, monkeypatch):
    _forbid_all_rows(monkeypatch)
    out = tmp_path / "v2"
    result = write_snapshot_v2(populated, out)
    assert result.exported == 1200


def test_package_export_streams_rows(populated: MemoryDB, tmp_path: Path, monkeypatch):
    _forbid_all_rows(monkeypatch)
    out = tmp_path / "pkg"
    result = write_package(populated, out)
    assert result.exported == 1200


def test_legacy_snapshot_streams_rows(populated: MemoryDB, tmp_path: Path, monkeypatch):
    _forbid_all_rows(monkeypatch)
    out = tmp_path / "legacy.jsonl"
    result = snapshot(populated, out)
    assert result.exported == 1200


def test_hydrate_inserts_in_bounded_batches(tmp_path: Path, monkeypatch):
    src = MemoryDB(tmp_path / "src.sqlite")
    _populate(src, 2100)
    swap_file = tmp_path / "in.jsonl"
    snapshot(src, swap_file)
    src.close()

    calls: list[int] = []
    original = MemoryDB.insert_many_ignore

    def spy(self, records, **kwargs):
        materialized = list(records)
        calls.append(len(materialized))
        return original(self, materialized, **kwargs)

    def no_full_hash_set(self):
        raise AssertionError("content_hashes() called — hydrate must dedup in the DB")

    monkeypatch.setattr(MemoryDB, "insert_many_ignore", spy)
    monkeypatch.setattr(MemoryDB, "content_hashes", no_full_hash_set)

    target = MemoryDB(tmp_path / "dst.sqlite")
    result = hydrate(target, swap_file)
    target.close()

    assert result.loaded == 2100
    assert calls and all(c <= 1000 for c in calls), calls
    assert sum(calls) >= 2100  # every record attempted exactly once
    assert len(calls) >= 3  # actually batched (2100/1000 → ≥3 flushes)


def test_v2_reader_inserts_in_bounded_batches(tmp_path: Path, monkeypatch):
    src = MemoryDB(tmp_path / "src.sqlite")
    _populate(src, 1500)
    v2_dir = tmp_path / "v2"
    write_snapshot_v2(src, v2_dir)
    src.close()

    calls: list[int] = []
    original = MemoryDB.insert_many_ignore

    def spy(self, records, **kwargs):
        materialized = list(records)
        calls.append(len(materialized))
        return original(self, materialized, **kwargs)

    monkeypatch.setattr(MemoryDB, "insert_many_ignore", spy)

    from hotmem.snapshot import hydrate as dispatch_hydrate

    target = MemoryDB(tmp_path / "dst.sqlite")
    result = dispatch_hydrate(target, v2_dir)
    target.close()

    assert result.loaded == 1500
    assert calls and all(c <= 1000 for c in calls), calls
    assert len(calls) >= 2  # 1500/1000 → ≥2 flushes, no per-record commits
