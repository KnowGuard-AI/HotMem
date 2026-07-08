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

# Single source of truth for the memories table column order. Drives INSERT
# statement generation and SQLite-to-SQLite import projection so the three
# write paths cannot drift.
_MEMORY_COLUMNS: tuple[str, ...] = (
    "id",
    "identifier",
    "fact_text",
    "embedding",
    "embedding_dim",
    "embedding_model",
    "source",
    "importance",
    "metadata_json",
    "content_hash",
    "ttl_seconds",
    "created_at",
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
    "fact_summary",
    "provenance_json",
)


# TTL-live predicate — single source of truth so search and list agree on what
# "expired" means. `alias` is "" for the unqualified table or "m." for joins.
def _ttl_live(alias: str = "") -> str:
    return (
        f"({alias}ttl_seconds IS NULL OR "
        f"(strftime('%s', 'now') - strftime('%s', {alias}created_at)) < {alias}ttl_seconds)"
    )


def _insert_placeholders() -> str:
    """Build the VALUES placeholder list, with COALESCE on created_at."""
    return ", ".join(
        "COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))" if c == "created_at" else "?"
        for c in _MEMORY_COLUMNS
    )


_INSERT_OR_REPLACE_SQL = (
    f"INSERT OR REPLACE INTO memories ({', '.join(_MEMORY_COLUMNS)}) "
    f"VALUES ({_insert_placeholders()})"
)
_INSERT_OR_IGNORE_SQL = (
    f"INSERT OR IGNORE INTO memories ({', '.join(_MEMORY_COLUMNS)}) "
    f"VALUES ({_insert_placeholders()})"
)


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    identifier      TEXT NOT NULL,
    fact_text       TEXT,
    embedding       BLOB,
    embedding_dim   INTEGER DEFAULT {EMBEDDING_DIM},
    embedding_model TEXT DEFAULT '',
    source          TEXT DEFAULT '',
    importance      REAL DEFAULT 0.5,
    metadata_json   TEXT DEFAULT '{{}}',
    content_hash    TEXT DEFAULT '',
    ttl_seconds     INTEGER,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    namespace       TEXT DEFAULT '',
    tier            TEXT DEFAULT 'hot',
    memory_type     TEXT DEFAULT 'fact',
    source_uri      TEXT DEFAULT '',
    source_format   TEXT DEFAULT '',
    source_checksum TEXT DEFAULT '',
    byte_offset     INTEGER,
    byte_length     INTEGER,
    updated_at      TEXT,
    snapshot_id     TEXT DEFAULT '',
    promotion_state TEXT DEFAULT 'HOT',
    promotion_candidate INTEGER DEFAULT 0,
    parent_memory   TEXT DEFAULT '',
    related_memories TEXT DEFAULT '[]',
    tags            TEXT DEFAULT '[]',
    schema_version  INTEGER DEFAULT 1,
    fact_summary    TEXT,
    provenance_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_identifier ON memories(identifier);
CREATE INDEX IF NOT EXISTS idx_memories_identifier_created ON memories(identifier, created_at);
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
    namespace: str = ""
    tier: str = "hot"
    memory_type: str = "fact"
    source_uri: str = ""
    source_format: str = ""
    source_checksum: str = ""
    byte_offset: int | None = None
    byte_length: int | None = None
    updated_at: str | None = None
    snapshot_id: str = ""
    promotion_state: str = "HOT"
    promotion_candidate: int = 0
    parent_memory: str = ""
    related_memories: str = "[]"
    tags: str = "[]"
    schema_version: int = 1
    fact_summary: str | None = None
    provenance_json: str | None = None


def _cosine_similarity(blob_a: bytes | None, blob_b: bytes | None) -> float | None:
    """SQLite UDF: cosine similarity between two packed float32 blobs.

    Returns 0.0 for NULL embeddings (file-backed without summary) so they
    rank last but don't break search queries.
    """
    if blob_a is None or blob_b is None:
        return 0.0
    n = len(blob_a) // 4
    if n == 0 or len(blob_b) // 4 != n:
        return 0.0  # empty or mismatched embeddings score 0
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

        _V2_COLUMNS: dict[str, str] = {
            "namespace": "TEXT DEFAULT ''",
            "tier": "TEXT DEFAULT 'hot'",
            "memory_type": "TEXT DEFAULT 'fact'",
            "source_uri": "TEXT DEFAULT ''",
            "source_format": "TEXT DEFAULT ''",
            "source_checksum": "TEXT DEFAULT ''",
            "byte_offset": "INTEGER",
            "byte_length": "INTEGER",
            "updated_at": "TEXT",
            "snapshot_id": "TEXT DEFAULT ''",
            "promotion_state": "TEXT DEFAULT 'HOT'",
            "promotion_candidate": "INTEGER DEFAULT 0",
            "parent_memory": "TEXT DEFAULT ''",
            "related_memories": "TEXT DEFAULT '[]'",
            "tags": "TEXT DEFAULT '[]'",
            "schema_version": "INTEGER DEFAULT 1",
            "fact_summary": "TEXT",
            "provenance_json": "TEXT",
        }
        added = []
        for column, decl in _V2_COLUMNS.items():
            if not _has_column(self._conn, "memories", column):
                self._conn.execute(f"ALTER TABLE memories ADD COLUMN {column} {decl}")
                added.append(column)
        if added:
            self._conn.commit()
            _trace.info("migrate", "added v2 columns", detail={"columns": added})

        current_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 2:
            self._conn.execute("PRAGMA user_version = 2")
            self._conn.commit()

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
        namespace: str = "",
        tier: str = "hot",
        memory_type: str = "fact",
        source_uri: str = "",
        source_format: str = "",
        source_checksum: str = "",
        byte_offset: int | None = None,
        byte_length: int | None = None,
        updated_at: str | None = None,
        snapshot_id: str = "",
        promotion_state: str = "HOT",
        promotion_candidate: int = 0,
        parent_memory: str = "",
        related_memories: str = "[]",
        tags: str = "[]",
        schema_version: int = 1,
        fact_summary: str | None = None,
        provenance_json: str | None = None,
    ) -> None:
        """Insert a memory row."""
        values = {
            "id": id,
            "identifier": identifier,
            "fact_text": fact_text,
            "embedding": embedding,
            "embedding_dim": embedding_dim,
            "embedding_model": embedding_model,
            "source": source,
            "importance": importance,
            "metadata_json": metadata_json,
            "content_hash": content_hash,
            "ttl_seconds": ttl_seconds,
            "created_at": created_at,
            "namespace": namespace,
            "tier": tier,
            "memory_type": memory_type,
            "source_uri": source_uri,
            "source_format": source_format,
            "source_checksum": source_checksum,
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "updated_at": updated_at,
            "snapshot_id": snapshot_id,
            "promotion_state": promotion_state,
            "promotion_candidate": promotion_candidate,
            "parent_memory": parent_memory,
            "related_memories": related_memories,
            "tags": tags,
            "schema_version": schema_version,
            "fact_summary": fact_summary,
            "provenance_json": provenance_json,
        }
        self._conn.execute(
            _INSERT_OR_REPLACE_SQL,
            tuple(values[c] for c in _MEMORY_COLUMNS),
        )
        self._conn.commit()
        _trace.debug("insert", f"stored memory {id[:8]}…", detail={"identifier": identifier})

    def insert_file_backed(
        self,
        id: str,
        identifier: str,
        source_uri: str,
        byte_offset: int,
        byte_length: int,
        source_format: str,
        source_checksum: str | None,
        *,
        fact_summary: str | None = None,
        embedding: bytes | None = None,
        embedding_dim: int = EMBEDDING_DIM,
        embedding_model: str = "",
        source: str = "",
        importance: float = 0.5,
        metadata_json: str = "{}",
        content_hash: str = "",
        provenance_json: str | None = None,
    ) -> None:
        """Insert a file-backed memory row (zero bytes copied).

        ``fact_text`` is NULL for file-backed memories; ``fact_summary`` may
        carry a short summary (and its embedding) so the memory is searchable.
        """
        self.insert(
            id=id,
            identifier=identifier,
            fact_text="",  # NOT NULL constraint relaxed; use empty string fallback
            embedding=embedding or b"",
            embedding_dim=embedding_dim,
            embedding_model=embedding_model,
            source=source,
            importance=importance,
            metadata_json=metadata_json,
            content_hash=content_hash,
            memory_type="file",
            source_uri=source_uri,
            source_format=source_format,
            source_checksum=source_checksum or "",
            byte_offset=byte_offset,
            byte_length=byte_length,
            fact_summary=fact_summary,
            provenance_json=provenance_json,
        )
        _trace.debug(
            "insert_file",
            f"stored file-backed memory {id[:8]}…",
            detail={"identifier": identifier, "source_uri": source_uri},
        )

    def get_memory(self, id: str) -> dict[str, Any] | None:
        """Return one memory row as a dict (metadata only; no file I/O)."""
        cols = ", ".join(_MEMORY_COLUMNS)
        row = self._conn.execute(
            f"SELECT {cols} FROM memories WHERE id = ?",
            (id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_file_backed(self) -> list[dict[str, Any]]:
        """Return all file-backed memory rows (metadata only; no file I/O).

        Filters on ``memory_type = 'file'`` and returns the same columns as
        ``get_memory()``. Used for provenance queries and batch hydration
        without touching any backing file.
        """
        cols = ", ".join(_MEMORY_COLUMNS)
        rows = self._conn.execute(
            f"SELECT {cols} FROM memories WHERE memory_type = 'file'"
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_many_ignore(self, records: Iterable[MemoryRecord]) -> int:
        """Insert many memory rows in one transaction, ignoring duplicate hashes/ids."""
        rows = [tuple(getattr(record, c) for c in _MEMORY_COLUMNS) for record in records]
        if not rows:
            return 0

        cursor = self._conn.executemany(_INSERT_OR_IGNORE_SQL, rows)
        self._conn.commit()
        inserted = cursor.rowcount if cursor.rowcount != -1 else 0
        _trace.debug("insert_many", f"stored {inserted} memories", detail={"attempted": len(rows)})
        return inserted

    def search_with_cosine(self, query_embedding: bytes) -> list[dict[str, Any]]:
        """Return all memories with their cosine similarity to the query embedding."""
        rows = self._conn.execute(
            f"""SELECT id, identifier, fact_text, fact_summary, importance,
                      metadata_json, source, created_at,
                      cosine_sim(embedding, ?) AS cosine_score
               FROM memories
               WHERE {_ttl_live()}
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
            f"""SELECT m.id, m.identifier, m.fact_text, m.importance, m.metadata_json,
                      m.source, m.created_at, bm25(memories_fts) AS bm25_score
               FROM memories_fts
               JOIN memories AS m ON m.rowid = memories_fts.rowid
               WHERE memories_fts MATCH ?
                 AND {_ttl_live("m.")}
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
        v2_cols = (
            "namespace, tier, memory_type, source_uri, source_format, "
            "source_checksum, byte_offset, byte_length, updated_at, snapshot_id, "
            "promotion_state, promotion_candidate, parent_memory, "
            "related_memories, tags, schema_version, fact_summary, provenance_json"
        )
        base = (
            "id, identifier, fact_text, embedding_dim, embedding_model, source, "
            "importance, metadata_json, content_hash, ttl_seconds, created_at"
        )
        tail = ", embedding" if include_embedding else ""
        query = f"SELECT {base}, {v2_cols}{tail} FROM memories"
        rows = self._conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def content_hashes(self) -> set[str]:
        """Return non-empty content hashes currently stored in the database."""
        rows = self._conn.execute(
            "SELECT content_hash FROM memories WHERE content_hash != ''"
        ).fetchall()
        return {row["content_hash"] for row in rows}

    def exists(self, content_hash: str) -> bool:
        """Check if a memory with this content_hash already exists."""
        row = self._conn.execute(
            "SELECT 1 FROM memories WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row is not None

    def list_by_identifier(
        self,
        identifier: str,
        *,
        order: str = "asc",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return memories for an identifier in created_at order (chronological).

        Excludes TTL-expired memories, consistent with search.
        """
        direction = "ASC" if order.lower() == "asc" else "DESC"
        rows = self._conn.execute(
            f"""SELECT id, identifier, fact_text, importance, metadata_json, source,
                       created_at
                FROM memories
                WHERE identifier = ?
                  AND {_ttl_live()}
                ORDER BY created_at {direction}
                LIMIT ?""",
            (identifier, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def import_sqlite(self, src_path: str | Path) -> tuple[int, int]:
        """Merge memories from another HotMem SQLite database (fast-path).

        Opens the source on a SEPARATE connection and only ever SELECTs from it,
        so a crafted source DB's triggers cannot affect the main database (a
        different connection). Rows are streamed in batches and inserted via
        insert_many_ignore, deduping on content_hash through the unique index.
        Embeddings are reused as-is — no recompute. Handles v0.1 and v2 source
        schemas (missing optional columns take dataclass defaults).

        Using a separate connection (rather than ATTACH) avoids connection-global
        schema state on the shared check_same_thread=False connection and works
        with WAL-mode source databases across SQLite versions.

        Returns (loaded, skipped_dupes).
        """
        resolved = Path(src_path).resolve()
        src_conn = sqlite3.connect(str(resolved))
        src_conn.row_factory = sqlite3.Row
        loaded = 0
        skipped = 0
        try:
            tables = {
                row["name"]
                for row in src_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "memories" not in tables:
                raise ValueError(
                    f"source database {resolved} has no 'memories' table; not a HotMem DB"
                )

            src_columns = {
                row["name"] for row in src_conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            required = {"id", "identifier", "fact_text", "embedding"}
            missing = required - src_columns
            if missing:
                raise ValueError(
                    f"source memories table missing required columns: {sorted(missing)}"
                )

            # SELECT only whitelisted canonical columns that exist in the source.
            # Source-provided column names are never interpolated into SQL, so a
            # crafted column name cannot inject.
            select_cols = [c for c in _MEMORY_COLUMNS if c in src_columns]
            col_sql = ", ".join(select_cols)
            cursor = src_conn.execute(f"SELECT {col_sql} FROM memories")

            seen_hashes = self.content_hashes()
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                records: list[MemoryRecord] = []
                for row in batch:
                    d = dict(row)
                    ch = d.get("content_hash", "")
                    if ch and ch in seen_hashes:
                        skipped += 1
                        continue
                    if ch:
                        seen_hashes.add(ch)
                    # Build from the columns present; dataclass defaults fill the
                    # rest (missing v2 columns in v0.1 sources).
                    records.append(MemoryRecord(**{c: d[c] for c in select_cols}))
                if records:
                    loaded += self.insert_many_ignore(records)
        finally:
            src_conn.close()

        _trace.info(
            "import_sqlite",
            f"imported {loaded} memories from {resolved}",
            detail={"loaded": loaded, "skipped_dupes": skipped, "src": str(resolved)},
        )
        return loaded, skipped

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
        _trace.info("close", "database closed", detail={"path": self.db_path})
