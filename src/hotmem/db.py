"""HotMem database — SQLite storage with cosine similarity UDF.

Purpose:
    Manage the memories table: create schema, insert, query, count.
    Registers a pure-python cosine similarity function as a SQLite UDF
    so vector search runs inside the DB engine.

Interface:
    MemoryDB(db_path: str | Path)
        .insert(id, identifier, fact_text, embedding_blob, ...) -> None
        .insert_many_ignore(records) -> int
        .count() -> int
        .all_rows(include_embedding=False) -> list[Row]
        .exists(content_hash: str) -> bool
        .close() -> None

Deps: hotmem.embed (for unpack_embedding), hotmem.trace
Extension: add indexes, FTS5, or WAL mode tuning here.
"""

from __future__ import annotations

import math
import re
import sqlite3
import struct
from collections.abc import Iterable
from dataclasses import dataclass
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
    ttl_seconds     INTEGER,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_memories_identifier ON memories(identifier);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    fact_text,
    content='memories',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, fact_text) VALUES (new.rowid, new.fact_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, fact_text)
    VALUES('delete', old.rowid, old.fact_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, fact_text)
    VALUES('delete', old.rowid, old.fact_text);
    INSERT INTO memories_fts(rowid, fact_text) VALUES (new.rowid, new.fact_text);
END;
"""

_FTS_TOKEN_RE = re.compile(r"[\w]+")


@dataclass(frozen=True)
class MemoryRecord:
    """Database-ready memory row."""

    id: str
    identifier: str
    fact_text: str
    embedding: bytes
    embedding_dim: int = EMBEDDING_DIM
    embedding_model: str = ""
    source: str = ""
    importance: float = 0.5
    metadata_json: str = "{}"
    content_hash: str = ""
    ttl_seconds: int | None = None
    created_at: str | None = None


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


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _fts_query(query: str) -> str:
    """Convert free text into a safe FTS5 prefix query."""
    terms = _FTS_TOKEN_RE.findall(query.lower())
    return " ".join(f"{term}*" for term in terms)


class MemoryDB:
    """SQLite-backed memory store with cosine similarity UDF."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA recursive_triggers=ON")
        self._conn.create_function("cosine_sim", 2, _cosine_similarity)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        _trace.info("init", "database opened", detail={"path": self.db_path})

    def _migrate(self) -> None:
        """Apply additive schema updates for existing HotMem databases."""
        if not _has_column(self._conn, "memories", "ttl_seconds"):
            self._conn.execute("ALTER TABLE memories ADD COLUMN ttl_seconds INTEGER")
            self._conn.commit()
            _trace.info("migrate", "added ttl_seconds column")

        try:
            self._conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash_unique
                   ON memories(content_hash)
                   WHERE content_hash != ''"""
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            _trace.warn(
                "migrate",
                "skipped unique content_hash index because duplicate hashes exist",
            )

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
        ttl_seconds: int | None = None,
        created_at: str | None = None,
    ) -> None:
        """Insert a memory row."""
        self._conn.execute(
            """INSERT OR REPLACE INTO memories
            (id, identifier, fact_text, embedding, embedding_dim, embedding_model,
             source, importance, metadata_json, content_hash, ttl_seconds, created_at)
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )""",
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
                ttl_seconds,
                created_at,
            ),
        )
        self._conn.commit()
        _trace.debug("insert", f"stored memory {id[:8]}…", detail={"identifier": identifier})

    def insert_many_ignore(self, records: Iterable[MemoryRecord]) -> int:
        """Insert many memory rows in one transaction, ignoring duplicate hashes/ids."""
        rows = [
            (
                record.id,
                record.identifier,
                record.fact_text,
                record.embedding,
                record.embedding_dim,
                record.embedding_model,
                record.source,
                record.importance,
                record.metadata_json,
                record.content_hash,
                record.ttl_seconds,
                record.created_at,
            )
            for record in records
        ]
        if not rows:
            return 0

        cursor = self._conn.executemany(
            """INSERT OR IGNORE INTO memories
            (id, identifier, fact_text, embedding, embedding_dim, embedding_model,
             source, importance, metadata_json, content_hash, ttl_seconds, created_at)
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )""",
            rows,
        )
        self._conn.commit()
        inserted = cursor.rowcount if cursor.rowcount != -1 else 0
        _trace.debug("insert_many", f"stored {inserted} memories", detail={"attempted": len(rows)})
        return inserted

    def search_with_cosine(self, query_embedding: bytes) -> list[dict[str, Any]]:
        """Return all memories with their cosine similarity to the query embedding."""
        rows = self._conn.execute(
            """SELECT id, identifier, fact_text, importance, metadata_json, source,
                      cosine_sim(embedding, ?) AS cosine_score
               FROM memories
               WHERE ttl_seconds IS NULL
                  OR (strftime('%s', 'now') - strftime('%s', created_at)) < ttl_seconds
               ORDER BY cosine_score DESC""",
            (query_embedding,),
        ).fetchall()
        return [dict(r) for r in rows]

    def fts_search(self, query: str) -> list[dict[str, Any]]:
        """Return full-text matches with raw BM25 scores."""
        fts_query = _fts_query(query)
        if not fts_query:
            return []

        rows = self._conn.execute(
            """SELECT m.id, m.identifier, m.fact_text, m.importance, m.metadata_json,
                      m.source, bm25(memories_fts) AS bm25_score
               FROM memories_fts
               JOIN memories AS m ON m.rowid = memories_fts.rowid
               WHERE memories_fts MATCH ?
                 AND (
                    m.ttl_seconds IS NULL
                    OR (strftime('%s', 'now') - strftime('%s', m.created_at)) < m.ttl_seconds
                 )
               ORDER BY bm25_score ASC""",
            (fts_query,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """Return total number of stored memories."""
        row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    def all_rows(self, *, include_embedding: bool = False) -> list[dict[str, Any]]:
        """Return all memory rows as dicts (for snapshot export)."""
        query = (
            """SELECT id, identifier, fact_text, embedding_dim, embedding_model,
                      source, importance, metadata_json, content_hash, ttl_seconds, created_at,
                      embedding
               FROM memories"""
            if include_embedding
            else """SELECT id, identifier, fact_text, embedding_dim, embedding_model,
                           source, importance, metadata_json, content_hash, ttl_seconds, created_at
                    FROM memories"""
        )
        rows = self._conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def content_hashes(self) -> set[str]:
        """Return non-empty content hashes currently stored in the database."""
        rows = self._conn.execute(
            "SELECT content_hash FROM memories WHERE content_hash != ''"
        ).fetchall()
        return {row["content_hash"] for row in rows}

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
