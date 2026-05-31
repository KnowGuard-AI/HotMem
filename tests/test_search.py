"""Tests for hotmem.search — hybrid ranking and message output."""

from __future__ import annotations

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.search import search_memories


def _add_fact(db: MemoryDB, id: str, text: str, importance: float = 0.5):
    vec = embed_text(text)
    blob = pack_embedding(vec)
    db.insert(id=id, identifier="test", fact_text=text, embedding=blob, importance=importance)


def test_search_returns_message_objects(tmp_db: MemoryDB):
    _add_fact(tmp_db, "1", "invoices should be validated")
    results = search_memories(tmp_db, "invoice validation", top_k=5)
    assert len(results) == 1
    msg = results[0]
    assert msg["role"] == "system"
    assert "content" in msg
    assert "memory_id" in msg
    assert "identifier" in msg
    assert "score" in msg


def test_search_top_k(tmp_db: MemoryDB):
    for i in range(10):
        _add_fact(tmp_db, str(i), f"fact number {i}")
    results = search_memories(tmp_db, "fact", top_k=3)
    assert len(results) == 3


def test_search_max_chars(tmp_db: MemoryDB):
    _add_fact(tmp_db, "1", "a" * 100)
    _add_fact(tmp_db, "2", "b" * 100)
    results = search_memories(tmp_db, "aaa", top_k=5, max_chars=50)
    total_chars = sum(len(r["content"]) for r in results)
    assert total_chars <= 50


def test_search_ranking_uses_importance(tmp_db: MemoryDB):
    _add_fact(tmp_db, "low", "generic fact here", importance=0.1)
    _add_fact(tmp_db, "high", "generic fact here", importance=1.0)
    results = search_memories(tmp_db, "generic fact", top_k=2)
    assert results[0]["memory_id"] == "high"


def test_search_ranking_uses_fts(tmp_db: MemoryDB):
    _add_fact(tmp_db, "exact", "duplicate invoice risk for vendor x", importance=0.1)
    _add_fact(tmp_db, "other", "payment terms are net 30", importance=1.0)

    results = search_memories(tmp_db, "duplicate invoice", top_k=2)

    assert results[0]["memory_id"] == "exact"


def test_search_empty_db(tmp_db: MemoryDB):
    results = search_memories(tmp_db, "anything", top_k=5)
    assert results == []


def test_search_filters_expired_memories(tmp_db: MemoryDB):
    _add_fact(tmp_db, "permanent", "invoice memory permanent")
    vec = embed_text("invoice memory expired")
    blob = pack_embedding(vec)
    tmp_db.insert(
        id="expired",
        identifier="test",
        fact_text="invoice memory expired",
        embedding=blob,
        ttl_seconds=1,
        created_at="2000-01-01T00:00:00Z",
    )

    results = search_memories(tmp_db, "invoice memory", top_k=5)

    assert [r["memory_id"] for r in results] == ["permanent"]
