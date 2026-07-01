"""Tests for hotmem.db — SQLite storage and cosine UDF."""

from __future__ import annotations

import sqlite3

from hotmem.db import MemoryDB, MemoryRecord
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


def test_insert_many_ignore_deduplicates_content_hash(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("same fact"))
    inserted = tmp_db.insert_many_ignore(
        [
            MemoryRecord(
                id="bulk1",
                identifier="x",
                fact_text="same fact",
                embedding=blob,
                content_hash="same-hash",
            ),
            MemoryRecord(
                id="bulk2",
                identifier="x",
                fact_text="same fact",
                embedding=blob,
                content_hash="same-hash",
            ),
        ]
    )

    assert inserted == 1
    assert tmp_db.count() == 1


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


def test_all_rows_can_include_embedding(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    tmp_db.insert(id="r1", identifier="x", fact_text="fact", embedding=blob)

    rows = tmp_db.all_rows(include_embedding=True)

    assert rows[0]["embedding"] == blob


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


def test_v2_migration_opens_v01_db(tmp_path):
    db_path = tmp_path / "v01.sqlite"
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
            ttl_seconds INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    blob = pack_embedding(embed_text("legacy fact"))
    conn.execute(
        """INSERT INTO memories (id, identifier, fact_text, embedding)
           VALUES ('legacy1', 'x', 'legacy fact', ?)""",
        (blob,),
    )
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        columns = {row["name"] for row in db._conn.execute("PRAGMA table_info(memories)")}
        for col in (
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
        ):
            assert col in columns, f"missing v2 column: {col}"

        version = db._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2

        row = db.all_rows()[0]
        assert row["id"] == "legacy1"
        assert row["promotion_state"] == "HOT"
        assert row["schema_version"] == 1
        assert row["tags"] == "[]"
        assert db.count() == 1
    finally:
        db.close()


def test_v2_fields_roundtrip(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("provenance fact"))
    tmp_db.insert(
        id="v2-1",
        identifier="ns-a",
        fact_text="provenance fact",
        embedding=blob,
        namespace="contracts",
        tier="hot",
        memory_type="fact",
        source_uri="file:///data/contract.pdf",
        source_format="pdf",
        source_checksum="sha256:abc123",
        byte_offset=4096,
        byte_length=128,
        updated_at="2026-07-01T00:00:00Z",
        snapshot_id="snap-1",
        promotion_state="READY",
        promotion_candidate=1,
        parent_memory="parent-1",
        related_memories='["m1","m2"]',
        tags='["legal","contract"]',
        schema_version=2,
    )

    row = tmp_db.all_rows()[0]
    assert row["namespace"] == "contracts"
    assert row["tier"] == "hot"
    assert row["memory_type"] == "fact"
    assert row["source_uri"] == "file:///data/contract.pdf"
    assert row["source_format"] == "pdf"
    assert row["source_checksum"] == "sha256:abc123"
    assert row["byte_offset"] == 4096
    assert row["byte_length"] == 128
    assert row["updated_at"] == "2026-07-01T00:00:00Z"
    assert row["snapshot_id"] == "snap-1"
    assert row["promotion_state"] == "READY"
    assert row["promotion_candidate"] == 1
    assert row["parent_memory"] == "parent-1"
    assert row["related_memories"] == '["m1","m2"]'
    assert row["tags"] == '["legal","contract"]'
    assert row["schema_version"] == 2


def test_v2_fields_default_when_unset(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("plain fact"))
    tmp_db.insert(id="d1", identifier="x", fact_text="plain fact", embedding=blob)

    row = tmp_db.all_rows()[0]
    assert row["namespace"] == ""
    assert row["tier"] == "hot"
    assert row["memory_type"] == "fact"
    assert row["source_uri"] == ""
    assert row["promotion_state"] == "HOT"
    assert row["promotion_candidate"] == 0
    assert row["tags"] == "[]"
    assert row["schema_version"] == 1
    assert row["byte_offset"] is None
    assert row["byte_length"] is None
