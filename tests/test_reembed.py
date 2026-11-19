"""Re-embedding only changes derived vectors and is safe to resume."""

from __future__ import annotations

import hashlib
import math

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EmbeddingDescriptor
from hotmem.events import EventType, query_events
from hotmem.reembed import reembed_store
from hotmem.swap import add_memory


class FakeEmbedder:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self._descriptor = EmbeddingDescriptor(
            implementation="test",
            model="reembed",
            dimension=3,
            revision="r1",
            preprocessing="fixture-v1",
        )
        self.fail_on = fail_on
        self.calls = 0

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("synthetic embed failure")
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [seed[0] + 1.0, seed[1] + 1.0, seed[2] + 1.0]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


def _add(db: MemoryDB, identifier: str, fact: str) -> str:
    memory_id, _content_hash = add_memory(db, identifier, fact)
    return memory_id


def test_dry_run_counts_without_embedding_or_mutation(tmp_path):
    db = MemoryDB(str(tmp_path / "dry-run.sqlite"))
    try:
        _add(db, "one", "first hash memory")
        _add(db, "two", "second hash memory")
        before = db.fingerprint()
        embedder = FakeEmbedder()

        result = reembed_store(db, embedder, batch_size=1, dry_run=True)

        assert result.dry_run is True
        assert result.reembedded == 2
        assert result.failed == 0
        assert embedder.calls == 0
        assert result.before == result.after == {"hotmem-hash-v1": 2}
        assert db.fingerprint() == before
        assert query_events(db, event_type=EventType.EMBEDDING_REBUILT)["count"] == 0
    finally:
        db.close()


def test_reembed_preserves_memory_state_and_resumes_failures(tmp_path):
    db = MemoryDB(str(tmp_path / "reembed.sqlite"))
    try:
        first_id = _add(db, "one", "first hash memory")
        second_id = _add(db, "two", "second hash memory")
        before = {row["id"]: row for row in db.all_rows(include_embedding=True)}
        embedder = FakeEmbedder(fail_on="second")

        first = reembed_store(db, embedder, batch_size=1)

        assert first.reembedded == 1
        assert first.failed == 1
        rows = {row["id"]: row for row in db.all_rows(include_embedding=True)}
        changed = rows[first_id]
        unchanged = rows[second_id]
        assert changed["embedding_model"] == embedder.descriptor.key
        assert changed["embedding_dim"] == embedder.descriptor.dimension
        assert changed["embedding"] != before[first_id]["embedding"]
        assert unchanged == before[second_id]
        for field in before[first_id]:
            if field not in {"embedding", "embedding_dim", "embedding_model"}:
                assert changed[field] == before[first_id][field]
        assert first.after == {"hotmem-hash-v1": 1, embedder.descriptor.key: 1}
        assert query_events(db, event_type=EventType.EMBEDDING_REBUILT)["count"] == 1

        embedder.fail_on = None
        resumed = reembed_store(db, embedder, batch_size=1)
        assert resumed.reembedded == 1
        assert resumed.failed == 0
        assert resumed.after == {embedder.descriptor.key: 2}
        assert query_events(db, event_type=EventType.EMBEDDING_REBUILT)["count"] == 2
    finally:
        db.close()


def test_repeat_reembed_is_a_noop(tmp_path):
    db = MemoryDB(str(tmp_path / "repeat.sqlite"))
    try:
        _add(db, "one", "same-space memory")
        embedder = FakeEmbedder()
        assert reembed_store(db, embedder).reembedded == 1
        calls = embedder.calls
        repeat = reembed_store(db, embedder)

        assert repeat.reembedded == 0
        assert repeat.failed == 0
        assert embedder.calls == calls
        assert query_events(db, event_type=EventType.EMBEDDING_REBUILT)["count"] == 1
    finally:
        db.close()


def test_interrupted_batch_invalidates_index_and_resumes(tmp_path):
    db = MemoryDB(tmp_path / "interrupted.sqlite")
    try:
        _add(db, "one", "first memory")
        _add(db, "two", "second memory")
        before = db.fingerprint()
        embedder = FakeEmbedder()

        def interrupt(_count):
            raise RuntimeError("interrupted after batch commit")

        with pytest.raises(RuntimeError, match="interrupted"):
            reembed_store(db, embedder, batch_size=1, on_progress=interrupt)
        assert db.fingerprint() != before
        assert query_events(db, event_type=EventType.EMBEDDING_REBUILT)["count"] == 1
        assert reembed_store(db, embedder, batch_size=1).reembedded == 1
    finally:
        db.close()
