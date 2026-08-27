"""Tests for #49 — optional derived vector index.

Covers the required cases from the issue:
  1. HotMem runs with no vector backend installed (NullVectorIndex default).
  2. Index rebuildable from canonical storage (rebuild parity).
  3. Search fallback when index absent (directory deleted).
  4. Search fallback when index stale.
  5. Index loss does not lose memory (SQLite remains canonical).
  6. Default behavior unchanged (no config → identical results).
  7. Large referenced files are NOT eagerly read for indexing.
  8. Deterministic fallback-search behavior.
  9. ChromaVectorIndex path (import-guarded; skipped when chromadb absent).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.memory import FileRef, add_file_backed
from hotmem.search import search_memories
from hotmem.vector_index import (
    ChromaVectorIndex,
    NullVectorIndex,
    VectorIndexConfig,
    db_fingerprint,
    get_vector_index,
    rebuild_vector_index,
    remove_index_directory,
)

# ── Test double: faithful in-memory index ────────────────────────────────────


class FakeVectorIndex:
    """In-memory VectorIndex implementing the protocol exactly.

    Uses exact cosine over all stored vectors (no approximation) and the
    same fingerprint/marker staleness contract as the real backends.
    """

    def __init__(self) -> None:
        self.vectors: dict[str, list[float]] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.marker: tuple[int, ...] | None = None
        self.upsert_calls = 0

    def upsert(self, records: list[dict[str, Any]]) -> None:
        self.upsert_calls += 1
        for r in records:
            self.vectors[r["id"]] = list(r["embedding"])
            self.metadata[r["id"]] = dict(r.get("metadata") or {})

    def apply_rebuild_marker(self, marker: dict[str, Any]) -> None:
        self.marker = (marker["db_count"], marker["max_rowid"], marker["max_event_seq"])
        self.rebuilt_at = marker.get("rebuilt_at")

    def search(self, query_embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        scored = []
        for mid, vec in self.vectors.items():
            dot = sum(a * b for a, b in zip(query_embedding, vec, strict=True))
            na = math.sqrt(sum(a * a for a in query_embedding))
            nb = math.sqrt(sum(b * b for b in vec))
            cosine = dot / (na * nb) if na and nb else 0.0
            scored.append({"id": mid, "cosine_score": cosine})
        scored.sort(key=lambda h: (-h["cosine_score"], h["id"]))
        return scored[:top_k]

    def delete(self, memory_ids: list[str]) -> None:
        for mid in memory_ids:
            self.vectors.pop(mid, None)
            self.metadata.pop(mid, None)

    def count(self) -> int:
        return len(self.vectors)

    def oversample(self) -> int:
        return 1000

    def is_stale(self, db: MemoryDB) -> bool:
        return self.marker != db_fingerprint(db)

    def status(self, db: MemoryDB) -> dict[str, Any]:
        return {
            "backend": "fake",
            "requested_backend": "fake",
            "dependency_available": True,
            "indexed_count": self.count(),
            "db_count": db.count(),
            "stale": self.is_stale(db),
            "rebuilt_at": None,
            "path": None,
        }

    def clear(self) -> None:
        self.vectors.clear()
        self.metadata.clear()
        self.marker = None

    def close(self) -> None:
        return None


# ── Seed helpers ─────────────────────────────────────────────────────────────


def _add_fact(
    db: MemoryDB,
    id: str,
    text: str,
    importance: float = 0.5,
    **kwargs: Any,
) -> None:
    vec = embed_text(text)
    db.insert(
        id=id,
        identifier="test",
        fact_text=text,
        embedding=pack_embedding(vec),
        importance=importance,
        **kwargs,
    )


def _add_file_backed(
    db: MemoryDB,
    id: str,
    uri: str,
    *,
    summary: str | None = None,
) -> str:
    Path(uri).parent.mkdir(parents=True, exist_ok=True)
    Path(uri).write_bytes(b"0123456789")  # ensure the backing file exists (stat check)
    ref = FileRef(source_uri=uri, byte_offset=0, byte_length=10, source_format="bin")
    memory_id, _ = add_file_backed(
        db,
        identifier="test",
        file_ref=ref,
        base_dir=None,
        summary=summary,
    )
    return memory_id


def _seed_store(db: MemoryDB) -> None:
    """Mixed seeds: inline facts, file-backed w/ + w/o summary, archived, TTL-dead."""
    base = Path(db.db_path).parent
    _add_fact(db, "f1", "invoice validation rules for vendor x", importance=0.9)
    _add_fact(db, "f2", "payment terms are net 30 days", importance=0.1)
    _add_fact(db, "f3", "duplicate invoice risk mitigation", importance=0.5)
    _add_fact(db, "arch", "archived invoice policy", promotion_state="ARCHIVED")
    _add_fact(
        db,
        "expired",
        "expired invoice memory",
        ttl_seconds=1,
        created_at="2000-01-01T00:00:00Z",
    )
    _add_file_backed(db, "fs1", str(base / "seed-1.bin"), summary="Q3 revenue summary data")
    _add_file_backed(db, "fs2", str(base / "seed-2.bin"), summary=None)


def _messages(results: list[dict[str, Any]]) -> list[tuple[str, float]]:
    return [(r["memory_id"], r["score"]) for r in results]


# ── 1. Default: no backend, no vector dependency ─────────────────────────────


def test_default_backend_is_null_no_chroma_import(tmp_path: Path):
    index = get_vector_index(None, base_dir=tmp_path)
    assert isinstance(index, NullVectorIndex)
    assert index.requested_backend == "none"
    # Subprocess: prove the default path never imports chromadb even when it
    # IS installed (isolation from this session's already-imported modules).
    import subprocess
    import sys

    code = (
        "import sys; "
        "from hotmem.vector_index import get_vector_index, VectorIndexConfig; "
        f"idx = get_vector_index(VectorIndexConfig(backend='none'), base_dir={str(tmp_path)!r}); "
        "assert idx.search([0.0], top_k=1) == []; "
        "assert 'chromadb' not in sys.modules, 'default backend must not import chromadb'; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_null_index_search_returns_empty_and_is_stale(tmp_db: MemoryDB, tmp_path: Path):
    _add_fact(tmp_db, "1", "hello world")
    index = get_vector_index(None, base_dir=tmp_path)
    assert index.search(embed_text("hello"), top_k=5) == []
    assert index.is_stale(tmp_db) is True
    assert index.count() == 0


def test_config_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unknown vector index backend"):
        VectorIndexConfig(backend="qdrant")


def test_search_with_null_index_equals_plain_search(tmp_db: MemoryDB, tmp_path: Path):
    _seed_store(tmp_db)
    index = get_vector_index(None, base_dir=tmp_path)
    plain = search_memories(tmp_db, "invoice", top_k=5)
    with_null = search_memories(tmp_db, "invoice", top_k=5, vector_index=index)
    assert _messages(plain) == _messages(with_null)


# ── 2. Rebuild parity ────────────────────────────────────────────────────────


def test_rebuild_indexes_rows_with_embeddings_only(tmp_db: MemoryDB):
    _seed_store(tmp_db)
    index = FakeVectorIndex()
    result = rebuild_vector_index(tmp_db, index)

    # f1..f3 + arch + expired (expired still has an embedding; TTL applies at
    # query time) + fs1 (summary embedding). fs2 has no summary -> no embedding.
    assert result["indexed_count"] == 6
    assert result["db_count"] == 7
    assert result["skipped_no_embedding"] == 1
    assert index.count() == 6
    assert index.is_stale(tmp_db) is False


def test_rebuild_parity_accelerated_equals_fallback(tmp_db: MemoryDB):
    _seed_store(tmp_db)
    index = FakeVectorIndex()
    rebuild_vector_index(tmp_db, index)

    for query in ("invoice", "payment terms", "duplicate invoice risk", "Q3 revenue", "zzz"):
        fallback = search_memories(tmp_db, query, top_k=5)
        accelerated = search_memories(tmp_db, query, top_k=5, vector_index=index)
        assert _messages(fallback) == _messages(accelerated), f"parity broke for {query!r}"


def test_rebuild_parity_includes_archived_when_requested(tmp_db: MemoryDB):
    _seed_store(tmp_db)
    index = FakeVectorIndex()
    rebuild_vector_index(tmp_db, index)

    fallback = search_memories(tmp_db, "invoice policy", top_k=5, include_archived=True)
    accelerated = search_memories(
        tmp_db, "invoice policy", top_k=5, include_archived=True, vector_index=index
    )
    assert _messages(fallback) == _messages(accelerated)
    assert "arch" in [m[0] for m in _messages(accelerated)]


def test_rebuild_on_null_index_is_noop(tmp_db: MemoryDB, tmp_path: Path):
    _add_fact(tmp_db, "1", "hello")
    index = get_vector_index(None, base_dir=tmp_path)
    result = rebuild_vector_index(tmp_db, index)
    assert result["indexed_count"] == 0
    assert result["db_count"] == 1


# ── 3. Stale index -> fallback ───────────────────────────────────────────────


def test_stale_index_falls_back_and_still_finds_new_memory(tmp_db: MemoryDB):
    _seed_store(tmp_db)
    index = FakeVectorIndex()
    rebuild_vector_index(tmp_db, index)

    # Mutate canonical storage without updating the index.
    _add_fact(tmp_db, "new", "fresh invoice fact after rebuild")

    assert index.is_stale(tmp_db) is True
    results = search_memories(tmp_db, "fresh invoice", top_k=3, vector_index=index)
    assert results[0]["memory_id"] == "new"


def test_fingerprint_changes_on_mutation(tmp_db: MemoryDB):
    _add_fact(tmp_db, "1", "one")
    before = db_fingerprint(tmp_db)
    _add_fact(tmp_db, "2", "two")
    assert db_fingerprint(tmp_db) != before


def test_index_search_failure_falls_back(tmp_db: MemoryDB):
    _add_fact(tmp_db, "1", "invoice validation")

    class ExplodingIndex(FakeVectorIndex):
        def search(self, query_embedding, top_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("index backend exploded")

    index = ExplodingIndex()
    index.marker = db_fingerprint(tmp_db)  # fresh, so the search path is taken
    index.upsert([{"id": "1", "embedding": embed_text("invoice validation"), "metadata": {}}])
    assert index.is_stale(tmp_db) is False
    results = search_memories(tmp_db, "invoice", top_k=3, vector_index=index)
    assert results[0]["memory_id"] == "1"


# ── 4/5. Index loss never loses memory ───────────────────────────────────────


def test_missing_index_directory_search_still_works(tmp_db: MemoryDB, tmp_path: Path):
    _seed_store(tmp_db)
    config = VectorIndexConfig(backend="chroma")
    # When chromadb is absent the factory degrades to Null; when present the
    # index directory does not exist yet. Either way search must work —
    # the index is optional and never required for correctness.
    index = get_vector_index(config, base_dir=tmp_path)
    assert index.is_stale(tmp_db) is True  # nothing rebuilt yet
    results = search_memories(tmp_db, "invoice", top_k=5, vector_index=index)
    assert results, "search must work when the index is absent"


def test_index_loss_does_not_lose_memory(tmp_db: MemoryDB, tmp_path: Path):
    _seed_store(tmp_db)
    index = FakeVectorIndex()
    rebuild_vector_index(tmp_db, index)
    assert index.count() == 6

    index.clear()  # simulate total index loss
    assert index.count() == 0
    assert tmp_db.count() == 7  # canonical storage untouched

    results = search_memories(tmp_db, "invoice validation", top_k=5, vector_index=index)
    assert results, "search falls back to the SQLite scan"

    rebuild_vector_index(tmp_db, index)  # rebuild restores acceleration
    assert index.count() == 6


def test_remove_index_directory_never_touches_sqlite(tmp_db: MemoryDB, tmp_path: Path):
    db_path = tmp_path / "hotmem.sqlite"
    db = MemoryDB(db_path)
    try:
        _add_fact(db, "1", "invoice rule")
        index_dir = tmp_path / "hotmem-vector-index"
        index_dir.mkdir()
        (index_dir / "rebuild_marker.json").write_text("{}")
        assert remove_index_directory(tmp_path) is True
        assert not index_dir.exists()
        assert db_path.exists()
        assert db.count() == 1
        assert remove_index_directory(tmp_path) is False  # idempotent
    finally:
        db.close()


# ── 6. Deterministic fallback ────────────────────────────────────────────────


def test_fallback_search_is_deterministic(tmp_db: MemoryDB):
    _seed_store(tmp_db)
    first = search_memories(tmp_db, "invoice", top_k=5)
    second = search_memories(tmp_db, "invoice", top_k=5)
    assert _messages(first) == _messages(second)


def test_stale_index_results_equal_fallback_results(tmp_db: MemoryDB):
    _seed_store(tmp_db)
    index = FakeVectorIndex()
    rebuild_vector_index(tmp_db, index)
    _add_fact(tmp_db, "late", "late arriving invoice fact")
    stale = search_memories(tmp_db, "invoice fact", top_k=5, vector_index=index)
    fallback = search_memories(tmp_db, "invoice fact", top_k=5)
    assert _messages(stale) == _messages(fallback)


# ── 7. No eager file reads during indexing ──────────────────────────────────


def test_rebuild_performs_no_file_reads(
    tmp_db: MemoryDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from spy import SpyAdapter

    import hotmem.storage as storage_module

    big_file = tmp_path / "big.parquet"
    big_file.write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2 MB "large" backing file
    file_memory_id = _add_file_backed(tmp_db, "big", str(big_file), summary="large parquet summary")
    _add_fact(tmp_db, "f1", "inline invoice fact")

    spy = SpyAdapter(storage_module.LocalFilesystemAdapter())
    monkeypatch.setitem(storage_module.ADAPTERS, "", spy)
    monkeypatch.setitem(storage_module.ADAPTERS, "file", spy)

    index = FakeVectorIndex()
    result = rebuild_vector_index(tmp_db, index)

    assert result["indexed_count"] == 2
    assert spy.total_file_reads == 0  # no read/read_range/checksum ever
    # The file-backed memory is indexed from its summary embedding only —
    # the 2 MB backing file was never opened.
    assert file_memory_id in index.vectors


# ── HTTP surface ─────────────────────────────────────────────────────────────


class _VectorApp:
    """App fixture with an injected fake index (no chromadb needed)."""

    def __init__(self, tmp_path: Path, vector_index: Any) -> TestClient:
        from hotmem.server import create_app

        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        app = create_app(
            db_path=mount / "hotmem.sqlite",
            base_dir=mount,
            vector_backend="chroma",
            vector_index=vector_index,
        )
        self.client = TestClient(app)
        self.mount = mount


def test_status_endpoint_reports_staleness(tmp_path: Path):
    holder = _VectorApp(tmp_path, FakeVectorIndex())
    with holder.client as c:
        resp = c.get("/v1/vector-index/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend"] == "fake"
        assert body["stale"] is True  # never rebuilt
        assert body["db_count"] == 0

        c.post("/v1/add", json={"identifier": "test", "fact": "invoice validation"})
        resp = c.get("/v1/vector-index/status")
        assert resp.json()["db_count"] == 1


def test_rebuild_endpoint_and_event(tmp_path: Path):
    index = FakeVectorIndex()
    holder = _VectorApp(tmp_path, index)
    with holder.client as c:
        c.post("/v1/add", json={"identifier": "test", "fact": "invoice validation"})
        # Backing file must exist for the file-backed add path.
        (holder.mount / "data.bin").write_bytes(b"\x01\x02\x03\x04")
        resp = c.post(
            "/v1/add",
            json={
                "identifier": "test",
                "file_uri": "data.bin",
                "byte_offset": 0,
                "byte_length": 4,
                "source_format": "bin",
            },
        )
        assert resp.status_code == 200
        resp = c.post("/v1/vector-index/rebuild")
        assert resp.status_code == 200
        body = resp.json()
        # 1 inline fact + 1 file-backed WITHOUT summary (skipped: no embedding).
        assert body["indexed_count"] == 1
        assert body["db_count"] == 2
        assert body["skipped_no_embedding"] == 1

        # Staleness observable through the status endpoint.
        status = c.get("/v1/vector-index/status").json()
        assert status["stale"] is False

        # Rebuild event appended to the append-only event log.
        events = c.get("/v1/events", params={"event_type": "index.rebuilt"}).json()
        assert events["count"] == 1
        assert events["events"][0]["payload"]["indexed_count"] == 1


def test_search_response_shape_unchanged_with_acceleration(tmp_path: Path):
    index = FakeVectorIndex()
    holder = _VectorApp(tmp_path, index)
    with holder.client as c:
        c.post("/v1/add", json={"identifier": "test", "fact": "invoice validation rules"})
        c.post("/v1/vector-index/rebuild")
        accelerated = c.post("/v1/search", json={"query": "invoice", "top_k": 5}).json()
        assert set(accelerated.keys()) == {"memories", "count", "trace_ms"}
        msg_keys = {
            "role",
            "content",
            "memory_id",
            "identifier",
            "score",
            "created_at",
        }
        assert all(set(m.keys()) == msg_keys for m in accelerated["memories"])


def test_clear_endpoint(tmp_path: Path):
    index = FakeVectorIndex()
    holder = _VectorApp(tmp_path, index)
    with holder.client as c:
        c.post("/v1/add", json={"identifier": "test", "fact": "invoice validation"})
        c.post("/v1/vector-index/rebuild")
        assert index.count() == 1

        resp = c.delete("/v1/vector-index")
        assert resp.status_code == 200
        assert resp.json()["cleared"] is True
        assert resp.json()["db_count"] == 1  # memories survive
        assert index.count() == 0
        assert c.get("/v1/vector-index/status").json()["stale"] is True

        # Search still works after clearing.
        search = c.post("/v1/search", json={"query": "invoice"}).json()
        assert search["count"] == 1


def test_rebuild_rejected_without_backend(tmp_path: Path):
    from hotmem.server import create_app

    mount = tmp_path / "mount"
    mount.mkdir()
    app = create_app(db_path=mount / "hotmem.sqlite", base_dir=mount)
    with TestClient(app) as c:
        resp = c.post("/v1/vector-index/rebuild")
        assert resp.status_code == 400
        assert resp.json()["error"] == "vector_index_disabled"

        status = c.get("/v1/vector-index/status").json()
        assert status == {
            "backend": "none",
            "requested_backend": "none",
            "dependency_available": True,
            "indexed_count": 0,
            "db_count": 0,
            "stale": True,
            "rebuilt_at": None,
            "path": str(mount / "hotmem-vector-index"),
            "trace_ms": status["trace_ms"],
        }


def test_rebuild_rejected_when_dependency_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import hotmem.vector_index as vi

    monkeypatch.setattr(vi, "chroma_available", lambda: False)
    from hotmem.server import create_app

    mount = tmp_path / "mount"
    mount.mkdir()
    app = create_app(db_path=mount / "hotmem.sqlite", base_dir=mount, vector_backend="chroma")
    with TestClient(app) as c:
        resp = c.post("/v1/vector-index/rebuild")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "vector_dependency_missing"
        assert body["requested_backend"] == "chroma"


def test_create_app_rejects_unknown_backend(tmp_path: Path):
    from hotmem.server import create_app

    with pytest.raises(ValueError, match="unknown vector_backend"):
        create_app(db_path=tmp_path / "x.sqlite", vector_backend="qdrant")


# ── 9. ChromaVectorIndex (skipped when chromadb is not installed) ────────────

try:
    import chromadb  # noqa: F401

    _CHROMA = True
except ImportError:
    _CHROMA = False

requires_chroma = pytest.mark.skipif(not _CHROMA, reason="chromadb not installed")


@requires_chroma
def test_chroma_rebuild_and_search_parity(tmp_path: Path):
    db_path = tmp_path / "hotmem.sqlite"
    db = MemoryDB(db_path)
    try:
        _seed_store(db)
        index = ChromaVectorIndex(base_dir=tmp_path)
        result = rebuild_vector_index(db, index)

        assert result["indexed_count"] == 6
        assert index.count() == 6
        assert index.is_stale(db) is False

        marker = tmp_path / "hotmem-vector-index" / "rebuild_marker.json"
        assert marker.exists()

        for query in ("invoice", "payment terms", "Q3 revenue"):
            fallback = search_memories(db, query, top_k=5)
            accelerated = search_memories(db, query, top_k=5, vector_index=index)
            assert _messages(fallback) == _messages(accelerated), query
    finally:
        db.close()


@requires_chroma
def test_chroma_stale_after_insert(tmp_path: Path):
    db_path = tmp_path / "hotmem.sqlite"
    db = MemoryDB(db_path)
    try:
        _add_fact(db, "1", "invoice validation")
        index = ChromaVectorIndex(base_dir=tmp_path)
        rebuild_vector_index(db, index)
        assert index.is_stale(db) is False

        _add_fact(db, "2", "another fact")
        assert index.is_stale(db) is True

        results = search_memories(db, "another fact", top_k=2, vector_index=index)
        assert results[0]["memory_id"] == "2"  # fallback found it
    finally:
        db.close()


@requires_chroma
def test_chroma_clear_and_delete(tmp_path: Path):
    db_path = tmp_path / "hotmem.sqlite"
    db = MemoryDB(db_path)
    try:
        _add_fact(db, "1", "invoice validation")
        index = ChromaVectorIndex(base_dir=tmp_path)
        rebuild_vector_index(db, index)
        assert index.count() == 1

        index.clear()
        assert index.count() == 0
        assert index.is_stale(db) is True

        rebuild_vector_index(db, index)
        assert index.count() == 1
        index.delete(["1"])
        assert index.count() == 0
    finally:
        db.close()


@requires_chroma
def test_chroma_persistence_across_instances(tmp_path: Path):
    db_path = tmp_path / "hotmem.sqlite"
    db = MemoryDB(db_path)
    try:
        _add_fact(db, "1", "invoice validation")
        first = ChromaVectorIndex(base_dir=tmp_path)
        rebuild_vector_index(db, first)
        first.close()

        second = ChromaVectorIndex(base_dir=tmp_path)
        assert second.count() == 1
        assert second.is_stale(db) is False  # marker survived persistence
    finally:
        db.close()
