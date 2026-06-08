"""Tests for hotmem.swap — JSONL hydration and snapshot."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_MODEL, embed_text, pack_embedding
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


def test_hydrate_computes_empty_content_hash(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "swap.jsonl"
    records = [
        {"identifier": "a", "fact_text": "first fact", "content_hash": ""},
        {"identifier": "b", "fact_text": "second fact", "content_hash": ""},
    ]
    swap.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    result = hydrate(tmp_db, swap)

    assert result.loaded == 2
    assert result.skipped_dupes == 0


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
    assert data["embedding_b64"] == base64.b64encode(blob).decode("ascii")


def test_snapshot_hydrate_preserves_ttl_and_created_at(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "ttl_swap.jsonl"
    record = {
        "id": "ttl-swap",
        "identifier": "swap",
        "fact_text": "ttl fact",
        "ttl_seconds": 3600,
        "created_at": "2026-05-31T00:00:00Z",
    }
    swap.write_text(json.dumps(record) + "\n")

    result = hydrate(tmp_db, swap)
    assert result.loaded == 1

    out = tmp_path / "out.jsonl"
    snapshot(tmp_db, out)
    data = json.loads(out.read_text().strip())

    assert data["ttl_seconds"] == 3600
    assert data["created_at"] == "2026-05-31T00:00:00Z"


def test_snapshot_can_omit_embeddings(tmp_db: MemoryDB, tmp_path: Path):
    blob = pack_embedding(embed_text("test fact"))
    tmp_db.insert(id="s1", identifier="snap", fact_text="test fact", embedding=blob)

    swap = tmp_path / "out.jsonl"
    snapshot(tmp_db, swap, include_embeddings=False)

    data = json.loads(swap.read_text().strip())
    assert "embedding_b64" not in data


def test_hydrate_reuses_compatible_stored_embedding(
    tmp_db: MemoryDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    swap = tmp_path / "swap.jsonl"
    blob = pack_embedding(embed_text("stored embedding fact"))
    swap.write_text(
        json.dumps(
            {
                "identifier": "stored",
                "fact_text": "stored embedding fact",
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dim": 64,
                "embedding_b64": base64.b64encode(blob).decode("ascii"),
            }
        )
        + "\n"
    )

    def fail_embed(_text: str):
        raise AssertionError("hydrate should reuse the stored embedding")

    monkeypatch.setattr("hotmem.swap.embed_text", fail_embed)

    result = hydrate(tmp_db, swap)

    assert result.loaded == 1
    assert tmp_db.all_rows(include_embedding=True)[0]["embedding"] == blob


def test_hydrate_recomputes_incompatible_stored_embedding(
    tmp_db: MemoryDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    swap = tmp_path / "swap.jsonl"
    stored_blob = pack_embedding(embed_text("stored embedding fact"))
    recomputed_blob = pack_embedding(embed_text("fallback embedding"))
    swap.write_text(
        json.dumps(
            {
                "identifier": "stored",
                "fact_text": "stored embedding fact",
                "embedding_model": "different-model",
                "embedding_dim": 64,
                "embedding_b64": base64.b64encode(stored_blob).decode("ascii"),
            }
        )
        + "\n"
    )

    monkeypatch.setattr("hotmem.swap.embed_text", lambda _text: embed_text("fallback embedding"))

    result = hydrate(tmp_db, swap)

    assert result.loaded == 1
    assert tmp_db.all_rows(include_embedding=True)[0]["embedding"] == recomputed_blob
