"""Tests for hotmem.swap — JSONL hydration and snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.swap import compute_content_hash, hydrate, snapshot


def test_compute_content_hash():
    h1 = compute_content_hash("id1", "fact1")
    h2 = compute_content_hash("id1", "fact1")
    h3 = compute_content_hash("id1", "fact2")
    assert h1 == h2
    assert h1 != h3


def test_hydrate_from_swap(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "swap.jsonl"
    records = [
        {"identifier": "vendor_a", "fact_text": "Invoice total $5000"},
        {"identifier": "vendor_b", "fact_text": "Late payment risk"},
    ]
    swap.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    result = hydrate(tmp_db, swap)
    assert result.loaded == 2
    assert result.skipped_dupes == 0
    assert tmp_db.count() == 2


def test_hydrate_deduplication(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "swap.jsonl"
    records = [
        {"identifier": "x", "fact_text": "same fact"},
        {"identifier": "x", "fact_text": "same fact"},
    ]
    swap.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    result = hydrate(tmp_db, swap)
    assert result.loaded == 1
    assert result.skipped_dupes == 1


def test_hydrate_missing_file(tmp_db: MemoryDB, tmp_path: Path):
    result = hydrate(tmp_db, tmp_path / "nonexistent.jsonl")
    assert result.loaded == 0


def test_snapshot_roundtrip(tmp_db: MemoryDB, tmp_path: Path):
    vec = embed_text("test fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="s1", identifier="snap", fact_text="test fact", embedding=blob)

    swap = tmp_path / "out.jsonl"
    result = snapshot(tmp_db, swap)
    assert result.exported == 1
    assert swap.exists()

    lines = swap.read_text().strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["fact_text"] == "test fact"
