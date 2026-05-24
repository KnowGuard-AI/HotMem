"""Tests for hotmem.db — SQLite storage and cosine UDF."""

from __future__ import annotations

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


def test_all_rows(tmp_db: MemoryDB):
    vec = embed_text("fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="r1", identifier="x", fact_text="fact", embedding=blob)
    rows = tmp_db.all_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == "r1"
