"""HotMem vector index — optional, derived, disposable retrieval acceleration.

Purpose:
     Provide an optional derived vector index in front of the canonical
     cosine+FTS search path. SQLite/files/bundles/manifests remain canonical:
     the index is disposable, rebuildable, and NEVER a source of truth.
     Losing or deleting the index never loses memory — search transparently
     falls back to the deterministic SQLite scan.

     The index only supplies CANDIDATE ids; ranking is always recomputed by
     the canonical hybrid scorer (cosine + FTS + importance) so the response
     shape and ranking contract are identical with or without acceleration.

Interface:
     VectorIndexConfig(backend="none", oversample=1000)
     VectorIndex (Protocol): upsert/search/delete/is_stale/status/close/clear
     NullVectorIndex — no-op backend (default; forces fallback)
     ChromaVectorIndex — optional Chroma-backed index (lazy import)
     get_vector_index(config, base_dir) -> VectorIndex
     rebuild_vector_index(db, index) -> dict
     db_fingerprint(db) -> tuple[int, int, int]

Deps: hotmem.db, hotmem.trace (chromadb is optional — [vector] extra)
Extension: add new backends by implementing the VectorIndex Protocol.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hotmem.trace import Timer, get_tracer

_trace = get_tracer("vector_index")

# Directory name for the derived index inside the mount/base dir.
INDEX_DIR_NAME = "hotmem-vector-index"
# Rebuild marker file name inside the index directory.
REBUILD_MARKER_NAME = "rebuild_marker.json"
# Default candidate oversampling for the accelerated path.
DEFAULT_OVERSAMPLE = 1000
# Batch size for upserts during rebuild.
UPSERT_BATCH = 500

VALID_BACKENDS = ("none", "chroma")


@dataclass(frozen=True)
class VectorIndexConfig:
    """Configuration for the optional derived vector index."""

    backend: str = "none"
    oversample: int = DEFAULT_OVERSAMPLE

    def __post_init__(self) -> None:
        if self.backend not in VALID_BACKENDS:
            raise ValueError(
                f"unknown vector index backend {self.backend!r}; expected one of {VALID_BACKENDS}"
            )
        if self.oversample < 1:
            raise ValueError("oversample must be >= 1")


def db_fingerprint(db: Any) -> tuple[int, ...]:
    """Cheap canonical-store fingerprint: (COUNT, MAX(rowid), MAX(event seq)).

    Detects inserts, deletes, INSERT OR REPLACE rewrites, and server-mediated
    mutations whose count/rowid happen to be unchanged (rowid reuse). Pure
    SQL — no file I/O.
    """
    return db.fingerprint()


@runtime_checkable
class VectorIndex(Protocol):
    """Protocol for derived vector index backends.

    Implementations hold NO canonical data: every method must be safe to
    no-op or fail — search then falls back to the SQLite cosine scan.
    """

    def upsert(self, records: list[dict[str, Any]]) -> None:
        """Upsert records: {id, embedding (list[float]), metadata (dict)}."""
        ...

    def search(self, query_embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        """Return ranked hits: [{id, cosine_score}] best-first."""
        ...

    def delete(self, memory_ids: list[str]) -> None:
        """Remove entries for the given memory ids."""
        ...

    def count(self) -> int:
        """Return the number of indexed entries."""
        ...

    def oversample(self) -> int:
        """Candidate batch size for the accelerated search path.

        When the store has at most this many rows the accelerated path is
        exact (every row is a candidate); larger stores are approximate by
        design — the canonical fallback remains the correctness floor.
        """
        ...

    def is_stale(self, db: Any) -> bool:
        """True when the index does not match the canonical store fingerprint."""
        ...

    def apply_rebuild_marker(self, marker: dict[str, Any]) -> None:
        """Record the rebuild marker (store fingerprint at rebuild time)."""
        ...

    def status(self, db: Any) -> dict[str, Any]:
        """Observability snapshot: backend, counts, staleness, rebuild marker."""
        ...

    def clear(self) -> None:
        """Remove all indexed entries and the rebuild marker (index stays usable)."""
        ...

    def close(self) -> None:
        """Release backend resources."""
        ...


class _MarkerMixin:
    """Shared rebuild-marker persistence for filesystem-backed index backends."""

    marker_path: Path

    def apply_rebuild_marker(self, marker: dict[str, Any]) -> None:
        self._write_marker(marker)

    def _read_marker(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.marker_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_marker(self, marker: dict[str, Any]) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")

    def _delete_marker(self) -> None:
        with _suppress_oserror():
            self.marker_path.unlink()

    def _stale_vs_marker(self, db: Any) -> bool:
        marker = self._read_marker()
        if marker is None:
            return True
        try:
            return db_fingerprint(db) != (
                marker.get("db_count"),
                marker.get("max_rowid"),
                marker.get("max_event_seq"),
            )
        except (TypeError, AttributeError):
            return True


class NullVectorIndex(_MarkerMixin):
    """No-op index: search returns [], forcing deterministic SQLite fallback.

    Used when no backend is configured (the default — HotMem runs with zero
    vector dependencies) or when an optional backend is requested but its
    package is not installed.
    """

    def __init__(
        self,
        requested_backend: str = "none",
        base_dir: str | Path | None = None,
        oversample: int = DEFAULT_OVERSAMPLE,
    ) -> None:
        self.requested_backend = requested_backend
        self.base_dir = base_dir
        self._oversample = oversample
        if base_dir is not None:
            self.marker_path = Path(base_dir) / INDEX_DIR_NAME / REBUILD_MARKER_NAME
        else:  # markerless null index (direct library use only)
            self.marker_path = Path("/dev/null/hotmem-null-index") / REBUILD_MARKER_NAME

    def upsert(self, records: list[dict[str, Any]]) -> None:
        return None

    def search(self, query_embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        return []

    def delete(self, memory_ids: list[str]) -> None:
        return None

    def count(self) -> int:
        return 0

    def oversample(self) -> int:
        return self._oversample

    def is_stale(self, db: Any) -> bool:
        return True

    def status(self, db: Any) -> dict[str, Any]:
        return {
            "backend": "none",
            "requested_backend": self.requested_backend,
            "dependency_available": self.requested_backend == "none",
            "indexed_count": 0,
            "db_count": db.count(),
            "stale": True,
            "rebuilt_at": None,
            "path": str(self.marker_path.parent) if self.base_dir is not None else None,
        }

    def clear(self) -> None:
        self._delete_marker()

    def close(self) -> None:
        return None


class ChromaVectorIndex(_MarkerMixin):
    """Chroma-backed derived index (optional [vector] extra).

    Persists under ``<base_dir>/hotmem-vector-index/``. Embeddings are reused
    from canonical SQLite rows (same dims/model) — never recomputed. The
    collection carries only id + embedding + light metadata; deleting the
    directory loses nothing that cannot be rebuilt from SQLite.
    """

    def __init__(self, base_dir: str | Path, oversample: int = DEFAULT_OVERSAMPLE) -> None:
        try:
            import chromadb
        except ImportError as err:
            raise ImportError(
                "ChromaVectorIndex requires chromadb. "
                "Install it with: uv pip install 'hotmem[vector]'"
            ) from err

        self.base_dir = Path(base_dir)
        self.index_path = self.base_dir / INDEX_DIR_NAME
        self.marker_path = self.index_path / REBUILD_MARKER_NAME
        self._oversample = oversample
        self._client = chromadb.PersistentClient(path=str(self.index_path))
        self._collection = self._client.get_or_create_collection(
            name="hotmem_memories",
            metadata={"hnsw:space": "cosine"},
        )
        _trace.info("init", "chroma vector index opened", detail={"path": str(self.index_path)})

    def upsert(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        for start in range(0, len(records), UPSERT_BATCH):
            batch = records[start : start + UPSERT_BATCH]
            self._collection.upsert(
                ids=[r["id"] for r in batch],
                embeddings=[r["embedding"] for r in batch],
                metadatas=[self._sanitize(r.get("metadata") or {}) for r in batch],
            )

    def search(self, query_embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        if top_k < 1 or self._collection.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self._collection.count()),
            include=["distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits = []
        for mid, dist in zip(ids, distances, strict=False):
            cosine = 1.0 - float(dist) if dist is not None else 0.0
            hits.append({"id": mid, "cosine_score": max(0.0, min(1.0, cosine))})
        return hits

    def delete(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        with _suppress_exception(Exception):
            self._collection.delete(ids=list(memory_ids))

    def count(self) -> int:
        return int(self._collection.count())

    def oversample(self) -> int:
        return self._oversample

    def is_stale(self, db: Any) -> bool:
        return self._stale_vs_marker(db)

    def status(self, db: Any) -> dict[str, Any]:
        marker = self._read_marker() or {}
        fingerprint = db_fingerprint(db)
        stale = self._stale_vs_marker(db)
        return {
            "backend": "chroma",
            "requested_backend": "chroma",
            "dependency_available": True,
            "indexed_count": self.count(),
            "db_count": fingerprint[0],
            "stale": stale,
            "rebuilt_at": marker.get("rebuilt_at"),
            "path": str(self.index_path),
        }

    def clear(self) -> None:
        with _suppress_exception(Exception):
            self._client.delete_collection("hotmem_memories")
        self._collection = self._client.get_or_create_collection(
            name="hotmem_memories",
            metadata={"hnsw:space": "cosine"},
        )
        self._delete_marker()
        _trace.info("clear", "vector index cleared")

    def close(self) -> None:
        return None

    @staticmethod
    def _sanitize(metadata: dict[str, Any]) -> dict[str, Any]:
        """Keep metadata flat, non-null, and primitive-typed for Chroma."""
        clean = {}
        for key in ("identifier", "memory_type", "promotion_state"):
            value = metadata.get(key)
            if value is not None:
                clean[key] = str(value)
        return clean or {"identifier": ""}


class _suppress_oserror:
    """Context manager that swallows OSError (marker best-effort cleanup)."""

    def __enter__(self) -> _suppress_oserror:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


class _suppress_exception:
    """Context manager that swallows a given exception type."""

    def __init__(self, exc_type: type[BaseException]) -> None:
        self.exc_type = exc_type

    def __enter__(self) -> _suppress_exception:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exc_type)


def chroma_available() -> bool:
    """True when the optional chromadb package is importable."""
    try:
        import chromadb  # noqa: F401
    except ImportError:
        return False
    return True


def get_vector_index(
    config: VectorIndexConfig | None,
    base_dir: str | Path | None = None,
) -> VectorIndex:
    """Factory: build a VectorIndex from config, degrading to Null safely.

    ``backend="none"`` (or ``config=None``) → NullVectorIndex — no vector
    dependency is ever imported. ``backend="chroma"`` → ChromaVectorIndex;
    when chromadb is not installed the factory warns and returns a Null
    index tagged with the requested backend (observable via status()).
    """
    if config is None:
        config = VectorIndexConfig()
    if config.backend == "none":
        return NullVectorIndex(
            requested_backend="none", base_dir=base_dir, oversample=config.oversample
        )
    if config.backend == "chroma":
        if not chroma_available():
            _trace.warn(
                "factory",
                "chromadb is not installed; falling back to NullVectorIndex "
                "(search will use the SQLite cosine scan)",
                detail={"requested_backend": "chroma"},
            )
            return NullVectorIndex(
                requested_backend="chroma", base_dir=base_dir, oversample=config.oversample
            )
        if base_dir is None:
            raise ValueError("base_dir is required for the chroma vector index backend")
        return ChromaVectorIndex(base_dir=base_dir, oversample=config.oversample)
    raise ValueError(f"unknown vector index backend {config.backend!r}")


def rebuild_vector_index(
    db: Any,
    index: VectorIndex,
    *,
    embedding_model: str = "",
    embedding_dim: int = 0,
) -> dict[str, Any]:
    """Full rebuild of the derived index from canonical SQLite storage.

    Reads memory rows (including embeddings) via ``db.all_rows`` — a pure SQL
    read that NEVER touches the storage adapter or any backing file. Rows
    without an embedding (file-backed memories without a summary) are skipped;
    they are excluded from cosine search by design and remain fully retrievable
    via get/hydrate and (for those with text) FTS.

    The rebuild marker records the store fingerprint snapshotted BEFORE the
    row read, so any concurrent mutation makes the marker read stale and
    search falls back deterministically — an index can be stale-fresh, never
    silently incomplete.
    """
    import datetime as _dt

    with Timer() as t:
        if isinstance(index, NullVectorIndex):
            # No-op backend: nothing to index and no marker to write.
            fingerprint = db_fingerprint(db)
            return {
                "indexed_count": 0,
                "db_count": fingerprint[0],
                "skipped_no_embedding": fingerprint[0],
                "rebuilt_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "trace_ms": round(t.ms, 2),
            }

        index.clear()
        # Snapshot the fingerprint BEFORE reading rows. Any mutation landing
        # after this point changes the live fingerprint, so the marker reads
        # stale and search takes the deterministic fallback until the next
        # rebuild — the safe direction. Snapshotting after the read would
        # instead bless an index that may miss rows inserted mid-rebuild.
        fingerprint = db_fingerprint(db)
        rows = db.all_rows(include_embedding=True)
        records = []
        indexed = 0
        for row in rows:
            blob = row.get("embedding")
            if not blob:
                continue
            embedding = _unpack_blob(blob)
            if not embedding:
                continue
            records.append(
                {
                    "id": row["id"],
                    "embedding": embedding,
                    "metadata": {
                        "identifier": row.get("identifier") or "",
                        "memory_type": row.get("memory_type") or "",
                        "promotion_state": row.get("promotion_state") or "",
                    },
                }
            )
            indexed += 1
            if len(records) >= UPSERT_BATCH:
                index.upsert(records)
                records = []
        if records:
            index.upsert(records)

        marker = {
            "db_count": fingerprint[0],
            "max_rowid": fingerprint[1],
            "max_event_seq": fingerprint[2],
            "indexed_count": indexed,
            "embedding_model": embedding_model,
            "embedding_dim": embedding_dim,
            "rebuilt_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        index.apply_rebuild_marker(marker)

    result = {
        "indexed_count": indexed,
        "db_count": fingerprint[0],
        "skipped_no_embedding": fingerprint[0] - indexed,
        "rebuilt_at": marker["rebuilt_at"],
        "trace_ms": round(t.ms, 2),
    }
    _trace.info("rebuild", "vector index rebuilt from canonical storage", detail=result)
    return result


def _unpack_blob(blob: bytes) -> list[float]:
    import struct

    count = len(blob) // 4
    if count == 0:
        return []
    return list(struct.unpack(f"{count}f", blob))


def remove_index_directory(base_dir: str | Path) -> bool:
    """Delete the entire derived index directory (files only, never SQLite).

    Returns True when something was removed. The index is disposable: removal
    never loses memory — canonical storage is untouched and search falls back
    to the SQLite scan until the next rebuild.
    """
    index_dir = Path(base_dir) / INDEX_DIR_NAME
    if index_dir.exists():
        shutil.rmtree(index_dir)
        _trace.info("remove", "vector index directory removed", detail={"path": str(index_dir)})
        return True
    return False
