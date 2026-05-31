"""Tests for hotmem.db — SQLite storage and cosine UDF."""

from __future__ import annotations

import sqlite3

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding


def test_insert_and_count(tmp_db: MemoryDB):
    vec = embed_text("test fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="abc123", identifier="test", fact_text="test fact", embedding=blob)
    assert tmp_db.count() == 1


def test_insert_replace(tmp_db: MemoryDB):
    vec = embed_text("fact one")
    blob = pack_embedding(vec)
    tmp_db.insert(id="same_id", identifier="x", fact_text="fact one", embedding=blob)
    tmp_db.insert(id="same_id", identifier="x", fact_text="fact two", embedding=blob)
    assert tmp_db.count() == 1


def test_exists(tmp_db: MemoryDB):
    vec = embed_text("fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="a", identifier="x", fact_text="fact", embedding=blob, content_hash="hash1")
    assert tmp_db.exists("hash1")
    assert not tmp_db.exists("hash2")


def test_cosine_search(tmp_db: MemoryDB):
    for text in ["the quick brown fox", "hello world", "machine learning is great"]:
        vec = embed_text(text)
        blob = pack_embedding(vec)
        tmp_db.insert(id=text[:5], identifier="test", fact_text=text, embedding=blob)

    query_vec = embed_text("quick fox")
    query_blob = pack_embedding(query_vec)
    results = tmp_db.search_with_cosine(query_blob)

    assert len(results) == 3
    # The fox sentence should rank highest
    assert results[0]["fact_text"] == "the quick brown fox"


def test_fts_search(tmp_db: MemoryDB):
    vec = embed_text("invoice validation required")
    blob = pack_embedding(vec)
    tmp_db.insert(
        id="fts1",
        identifier="test",
        fact_text="invoice validation required",
        embedding=blob,
    )

    results = tmp_db.fts_search("invoice valid")

    assert len(results) == 1
    assert results[0]["id"] == "fts1"
    assert "bm25_score" in results[0]


def test_fts_search_updates_on_replace(tmp_db: MemoryDB):
    vec = embed_text("old invoice text")
    blob = pack_embedding(vec)
    tmp_db.insert(id="same", identifier="test", fact_text="old invoice text", embedding=blob)
    tmp_db.insert(id="same", identifier="test", fact_text="new contract text", embedding=blob)

    assert tmp_db.fts_search("old invoice") == []
    results = tmp_db.fts_search("new contract")
    assert [r["id"] for r in results] == ["same"]


def test_all_rows(tmp_db: MemoryDB):
    vec = embed_text("fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="r1", identifier="x", fact_text="fact", embedding=blob)
    rows = tmp_db.all_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == "r1"
    assert rows[0]["ttl_seconds"] is None


def test_insert_with_ttl(tmp_db: MemoryDB):
    vec = embed_text("temporary fact")
    blob = pack_embedding(vec)
    tmp_db.insert(
        id="ttl1",
        identifier="x",
        fact_text="temporary fact",
        embedding=blob,
        ttl_seconds=3600,
    )

    row = tmp_db.all_rows()[0]
    assert row["ttl_seconds"] == 3600


def test_existing_db_gets_ttl_column(tmp_path):
    db_path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            embedding BLOB,
            embedding_dim INTEGER,
            embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        columns = {row["name"] for row in db._conn.execute("PRAGMA table_info(memories)")}
        assert "ttl_seconds" in columns
    finally:
        db.close()
