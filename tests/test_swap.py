"""Tests for hotmem.swap - JSONL hydration and snapshot."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.swap import add_memory, compute_content_hash, hydrate, snapshot


def test_compute_content_hash():
    h1 = compute_content_hash("id1", "fact1")
    h2 = compute_content_hash("id1", "fact1")
    h3 = compute_content_hash("id1", "fact2")
    assert h1 == h2
    assert h1 != h3


def test_add_memory_canonical_contract(tmp_db: MemoryDB):
    """add_memory emits the full canonical contract (embedding_model, source, metadata)."""
    memory_id, content_hash = add_memory(
        tmp_db,
        "vendor_a",
        "Invoice total $5000",
        source="test",
        importance=0.8,
        metadata={"doc": "inv-1"},
    )
    assert memory_id and content_hash
    assert tmp_db.count() == 1
    rows = tmp_db.all_rows()
    row = rows[0]
    assert row["identifier"] == "vendor_a"
    assert row["source"] == "test"
    assert row["importance"] == 0.8
    assert row["content_hash"] == content_hash
    assert json.loads(row["metadata_json"]) == {"doc": "inv-1"}
    # Canonical fields the FastAPI example previously omitted:
    assert row["embedding_model"] == EMBEDDING_MODEL
    assert row["embedding_dim"] is not None


def test_add_memory_defaults(tmp_db: MemoryDB):
    """add_memory with defaults still stamps source/metadata (no NULL drift)."""
    mid, _ = add_memory(tmp_db, "x", "fact")
    row = tmp_db.all_rows()[0]
    assert row["source"] == ""
    assert json.loads(row["metadata_json"]) == {}


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


def test_hydrate_from_compressed_swap(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "swap.jsonl.gz"
    records = [
        {"identifier": "vendor_a", "fact_text": "Compressed invoice memory"},
        {"identifier": "vendor_b", "fact_text": "Archived payment memory"},
    ]
    with gzip.open(swap, "wt") as f:
        f.write("\n".join(json.dumps(r) for r in records) + "\n")

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


def test_hydrate_rejects_unsupported_extension(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "swap.txt"
    swap.write_text(json.dumps({"identifier": "x", "fact_text": "fact"}) + "\n")

    with pytest.raises(ValueError, match=r"supported: \.jsonl, \.jsonl\.gz"):
        hydrate(tmp_db, swap)


def test_hydrate_reports_malformed_compressed_swap(tmp_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "broken.jsonl.gz"
    swap.write_text("not a gzip stream")

    with pytest.raises(ValueError, match="malformed compressed swap file"):
        hydrate(tmp_db, swap)


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


def test_snapshot_to_compressed_swap(tmp_db: MemoryDB, tmp_path: Path):
    blob = pack_embedding(embed_text("compressed snapshot fact"))
    tmp_db.insert(
        id="s1",
        identifier="snap",
        fact_text="compressed snapshot fact",
        embedding=blob,
    )

    swap = tmp_path / "out.jsonl.gz"
    result = snapshot(tmp_db, swap)

    assert result.exported == 1
    with gzip.open(swap, "rt") as f:
        data = json.loads(f.read().strip())
    assert data["fact_text"] == "compressed snapshot fact"


def test_snapshot_rejects_unsupported_extension(tmp_db: MemoryDB, tmp_path: Path):
    with pytest.raises(ValueError, match=r"supported: \.jsonl, \.jsonl\.gz"):
        snapshot(tmp_db, tmp_path / "out.ndjson")


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


def test_hydrate_sqlite_fast_path(tmp_path):
    """SQLite-to-SQLite import reuses embeddings and dedupes on content_hash."""
    src_path = tmp_path / "src.sqlite"
    dst_path = tmp_path / "dst.sqlite"

    src = MemoryDB(src_path)
    blob = pack_embedding(embed_text("fast path fact"))
    src.insert(
        id="s1",
        identifier="x",
        fact_text="fast path fact",
        embedding=blob,
        content_hash="hash-s1",
    )
    src.close()

    dst = MemoryDB(dst_path)
    result = hydrate(dst, src_path)
    assert result.loaded == 1
    assert result.skipped_dupes == 0
    assert dst.count() == 1

    row = dst.all_rows(include_embedding=True)[0]
    assert row["embedding"] == blob
    dst.close()


def test_hydrate_sqlite_skips_existing_hashes(tmp_path):
    src_path = tmp_path / "src.sqlite"
    dst_path = tmp_path / "dst.sqlite"

    blob = pack_embedding(embed_text("dup fact"))

    src = MemoryDB(src_path)
    src.insert(id="s1", identifier="x", fact_text="dup fact", embedding=blob, content_hash="dup")
    src.close()

    dst = MemoryDB(dst_path)
    dst.insert(
        id="existing", identifier="x", fact_text="dup fact", embedding=blob, content_hash="dup"
    )

    result = hydrate(dst, src_path)
    assert result.loaded == 0
    assert result.skipped_dupes == 1
    assert dst.count() == 1
    dst.close()


def test_hydrate_sqlite_rejects_non_hotmem_db(tmp_path):
    import sqlite3

    bad_path = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(bad_path)
    conn.execute("CREATE TABLE foo (x INTEGER)")
    conn.commit()
    conn.close()

    dst = MemoryDB(tmp_path / "dst.sqlite")
    with pytest.raises(ValueError, match="no 'memories' table"):
        hydrate(dst, bad_path)
    dst.close()


def test_hydrate_sqlite_imports_v01_source(tmp_path):
    """A v0.1 schema source DB (no v2 columns) imports with defaults."""
    import sqlite3

    v01_path = tmp_path / "v01.sqlite"
    conn = sqlite3.connect(v01_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY, identifier TEXT, fact_text TEXT,
            embedding BLOB, content_hash TEXT DEFAULT ''
        )"""
    )
    blob = pack_embedding(embed_text("legacy sqlite fact"))
    conn.execute(
        "INSERT INTO memories (id, identifier, fact_text, embedding, content_hash)"
        " VALUES (?,?,?,?,?)",
        ("old1", "x", "legacy sqlite fact", blob, "h-old1"),
    )
    conn.commit()
    conn.close()

    dst = MemoryDB(tmp_path / "dst.sqlite")
    result = hydrate(dst, v01_path)
    assert result.loaded == 1
    row = dst.all_rows()[0]
    assert row["promotion_state"] == "HOT"
    assert row["schema_version"] == 1
    dst.close()


def test_hydrate_sqlite_rejects_malicious_column_name(tmp_path):
    """A crafted source DB with a SQL-injection column name must not execute."""
    import sqlite3

    evil_path = tmp_path / "evil.sqlite"
    conn = sqlite3.connect(evil_path)
    # Column name that would break out of a SELECT if interpolated raw.
    evil_col = "x) UNION SELECT sql FROM sqlite_master--"
    create_sql = (
        'CREATE TABLE memories ("id" TEXT, "identifier" TEXT, '
        '"fact_text" TEXT, "embedding" BLOB, '
        f'"{evil_col}" TEXT)'
    )
    conn.execute(create_sql)
    conn.execute(
        "INSERT INTO memories (id, identifier, fact_text, embedding)"
        " VALUES ('e1', 'x', 'evil', X'00')"
    )
    conn.commit()
    conn.close()

    dst = MemoryDB(tmp_path / "dst.sqlite")
    # import_sqlite projects only whitelisted columns, so the malicious column
    # is never referenced. The row imports cleanly via the known columns.
    result = hydrate(dst, evil_path)
    assert result.loaded == 1
    assert dst.count() == 1
    # The injected column must not have leaked sqlite_master contents anywhere.
    row = dst.all_rows()[0]
    assert row["id"] == "e1"
    assert row["fact_text"] == "evil"
    dst.close()
