"""HotMem database — SQLite storage with cosine similarity UDF.

Purpose:
    Manage the memories table: create schema, insert, query, count.
    Registers a pure-python cosine similarity function as a SQLite UDF
    so vector search runs inside the DB engine.

Interface:
    MemoryDB(db_path: str | Path)
        .insert(id, identifier, fact_text, embedding_blob, ...) -> None
        .search_all() -> list[Row]
        .count() -> int
        .all_rows() -> list[Row]
        .exists(content_hash: str) -> bool
        .close() -> None

Deps: hotmem.embed (for unpack_embedding), hotmem.trace
Extension: add indexes, FTS5, or WAL mode tuning here.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path
from typing import Any

from hotmem.embed import EMBEDDING_DIM
from hotmem.trace import get_tracer

_trace = get_tracer("db")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    identifier      TEXT NOT NULL,
    fact_text       TEXT NOT NULL,
    embedding       BLOB,
    embedding_dim   INTEGER DEFAULT {EMBEDDING_DIM},
    embedding_model TEXT DEFAULT '',
    source          TEXT DEFAULT '',
    importance      REAL DEFAULT 0.5,
    metadata_json   TEXT DEFAULT '{{}}',
    content_hash    TEXT DEFAULT '',
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_memories_identifier ON memories(identifier);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);
"""


def _cosine_similarity(blob_a: bytes | None, blob_b: bytes | None) -> float | None:
    """SQLite UDF: cosine similarity between two packed float32 blobs."""
    if blob_a is None or blob_b is None:
        return None
    n = len(blob_a) // 4
    a = struct.unpack(f"{n}f", blob_a)
    b = struct.unpack(f"{n}f", blob_b)
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class MemoryDB:
    """SQLite-backed memory store with cosine similarity UDF."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.create_function("cosine_sim", 2, _cosine_similarity)
        self._conn.executescript(_SCHEMA)
        _trace.info("init", "database opened", detail={"path": self.db_path})

    def insert(
        self,
        id: str,
        identifier: str,
        fact_text: str,
        embedding: bytes,
        *,
        embedding_dim: int = EMBEDDING_DIM,
        embedding_model: str = "",
        source: str = "",
        importance: float = 0.5,
        metadata_json: str = "{}",
        content_hash: str = "",
    ) -> None:
        """Insert a memory row."""
        self._conn.execute(
            """INSERT OR REPLACE INTO memories
            (id, identifier, fact_text, embedding, embedding_dim, embedding_model,
             source, importance, metadata_json, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id,
                identifier,
                fact_text,
                embedding,
                embedding_dim,
                embedding_model,
                source,
                importance,
                metadata_json,
                content_hash,
            ),
        )
        self._conn.commit()
        _trace.debug("insert", f"stored memory {id[:8]}…", detail={"identifier": identifier})

    def search_with_cosine(self, query_embedding: bytes) -> list[dict[str, Any]]:
        """Return all memories with their cosine similarity to the query embedding."""
        rows = self._conn.execute(
            """SELECT id, identifier, fact_text, importance, metadata_json, source,
                      cosine_sim(embedding, ?) AS cosine_score
               FROM memories
               ORDER BY cosine_score DESC""",
            (query_embedding,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """Return total number of stored memories."""
        row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    def all_rows(self) -> list[dict[str, Any]]:
        """Return all memory rows as dicts (for snapshot export)."""
        rows = self._conn.execute(
            """SELECT id, identifier, fact_text, embedding_dim, embedding_model,
                      source, importance, metadata_json, content_hash, created_at
               FROM memories"""
        ).fetchall()
        return [dict(r) for r in rows]

    def exists(self, content_hash: str) -> bool:
        """Check if a memory with this content hash already exists."""
        row = self._conn.execute(
            "SELECT 1 FROM memories WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row is not None

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
        _trace.info("close", "database closed", detail={"path": self.db_path})
