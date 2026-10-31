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
        .find_by_namespace_and_hash(namespace, content_hash) -> dict | None
        .batch_existing_namespace_hashes(pairs) -> set[tuple[str, str]]
        .exists(content_hash: str, *, namespace: str | None = None) -> bool
        .close() -> None

Deps: hotmem.embed (for unpack_embedding), hotmem.trace
Extension: add indexes, FTS5, or WAL mode tuning here.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, cosine
from hotmem.interchange.canonical import compute_file_reference_hash, normalize_source_uri
from hotmem.trace import get_tracer

_trace = get_tracer("db")

# Ids bound per query in search_by_ids: stays under SQLite's legacy
# 999-variable limit even with the embedding parameter plus headroom (#92).
_SEARCH_BIND_CHUNK = 900

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


def _cosine_guard(embedding_model: str | None) -> tuple[str, list[str]]:
    """Mixed-space safety (issue #78): the cosine expression guarded by descriptor.

    Returns the SQL expression computing ``cosine_score`` and its bound
    parameters. Rows stored under a different descriptor key than the
    active one score exactly 0.0 — they stay candidates for FTS/importance
    ranking (lexical-only, explicit) but never mis-score across spaces;
    equal dimensions are never evidence of compatibility. The hash default
    also admits legacy empty-model rows (interchange-v1 §5). ``None`` keeps
    the historical unguarded cosine for direct callers.
    """
    if embedding_model is None:
        return "cosine_sim(embedding, ?)", []
    if embedding_model == EMBEDDING_MODEL:
        return (
            "(CASE WHEN (embedding_model = ? OR embedding_model = '') "
            "THEN cosine_sim(embedding, ?) ELSE 0.0 END)",
            [embedding_model],
        )
    return (
        "(CASE WHEN embedding_model = ? THEN cosine_sim(embedding, ?) ELSE 0.0 END)",
        [embedding_model],
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

CREATE TABLE IF NOT EXISTS bundle_index (
    path             TEXT PRIMARY KEY,
    primary_file     TEXT,
    metadata_json    TEXT DEFAULT '{{}}',
    attachment_count INTEGER DEFAULT 0,
    checksum         TEXT DEFAULT '',
    modified_time    TEXT,
    size_hint        INTEGER DEFAULT 0,
    warning_count    INTEGER DEFAULT 0,
    discovered_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- #41: Append-only local event log for memory lifecycle and store signals.
-- Ordering is deterministic via the monotonic `seq` AUTOINCREMENT; `event_id`
-- is a stable client-facing identifier that survives re-pagination. Reads are
-- pure SQL — no file hydration ever. UPDATE/DELETE are intentionally absent
-- from the public surface (retention helpers exist but are not exposed via API).
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT    NOT NULL UNIQUE,
    memory_id    TEXT,
    namespace    TEXT    DEFAULT '',
    event_type   TEXT    NOT NULL,
    occurred_at  TEXT    NOT NULL,
    payload_json TEXT    DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_events_memory_seq   ON events(memory_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_namespace_seq ON events(namespace, seq);
CREATE INDEX IF NOT EXISTS idx_events_type_seq     ON events(event_type, seq);
CREATE INDEX IF NOT EXISTS idx_events_occurred_seq ON events(occurred_at, seq);

-- Append-only enforcement: tampering with events raises an error at the DB layer.
-- Retention uses _trim_events() which temporarily disables these triggers.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE is forbidden');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE is forbidden');
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

    Delegates the math to ``hotmem.embed.cosine`` — the one canonical
    definition shared with the second-stage reranker (#80 review), so the
    ranking stages cannot drift. Returns 0.0 for NULL embeddings
    (file-backed without summary) so they rank last but don't break search
    queries.
    """
    if blob_a is None or blob_b is None:
        return 0.0
    n = len(blob_a) // 4
    if n == 0 or len(blob_b) // 4 != n:
        return 0.0  # empty or mismatched embeddings score 0
    return cosine(list(struct.unpack(f"{n}f", blob_a)), list(struct.unpack(f"{n}f", blob_b)))


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _fts_query(query: str) -> str:
    """Convert free text into a safe FTS5 prefix query."""
    terms = _FTS_TOKEN_RE.findall(query.lower())
    return " ".join(f"{term}*" for term in terms)


class MemoryDB:
    """SQLite-backed memory store with cosine similarity UDF."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        self._read_only = read_only
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("cosine_sim", 2, _cosine_similarity)
        if read_only:
            self._conn.execute("PRAGMA query_only=ON")
            return
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA recursive_triggers=ON")
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

        # #41: events table is created idempotently by _SCHEMA. We bump
        # user_version to 3 so callers can detect that the event log surface
        # is available. No backfill is performed — existing memories continue
        # to operate without lifecycle history.
        if current_version < 3:
            self._conn.execute("PRAGMA user_version = 3")
            self._conn.commit()
            _trace.info("migrate", "events table available; user_version=3")

        # #73: sync checkpoints — one row per applied delta, committed in the
        # same transaction as the delta's record writes so a rolled-back
        # apply never advances the replay checkpoint.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sync_checkpoints (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               base_logical_id TEXT NOT NULL,
               delta_digest TEXT NOT NULL,
               applied_ops INTEGER NOT NULL,
               resulting_state_fingerprint TEXT,
               applied_at TEXT NOT NULL
           )"""
        )
        if current_version < 4:
            self._conn.execute("PRAGMA user_version = 4")
            self._conn.commit()
            _trace.info("migrate", "sync checkpoints available; user_version=4")

        # #101: handoff ledger — one row per first-time applied handoff
        # package, committed in the same transaction as the package's record
        # writes so a rolled-back hydration never marks a package applied.
        # Repeat hydration of the same package_id short-circuits here.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS handoff_ledger (
               package_id TEXT PRIMARY KEY,
               handoff_id TEXT NOT NULL,
               mode TEXT NOT NULL,
               applied_at TEXT NOT NULL,
               counts_json TEXT NOT NULL
           )"""
        )
        if current_version < 5:
            self._conn.execute("PRAGMA user_version = 5")
            self._conn.commit()
            _trace.info("migrate", "handoff ledger available; user_version=5")

        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS store_meta (
               key   TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
        )
        self._conn.commit()

        # Namespace-scoped logical identity (#100) and the idempotent
        # operation ledger are part of schema v6. The migration is
        # transactional: if the namespace-scoped unique index cannot be
        # created, the whole migration rolls back instead of silently
        # dropping the uniqueness guarantee.
        self._conn.execute("SAVEPOINT migration_v6")
        try:
            idx_row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_memories_content_hash_unique'"
            ).fetchone()
            create_index = True
            if idx_row and idx_row[0]:
                if "namespace" in idx_row[0]:
                    create_index = False
                else:
                    self._conn.execute("DROP INDEX idx_memories_content_hash_unique")
            if create_index:
                self._conn.execute(
                    """CREATE UNIQUE INDEX idx_memories_content_hash_unique
                       ON memories(namespace, content_hash)
                       WHERE content_hash != ''"""
                )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS operation_ledger (
                    caller_id      TEXT NOT NULL,
                    namespace      TEXT NOT NULL,
                    operation_key  TEXT NOT NULL,
                    operation_name TEXT NOT NULL,
                    request_hash   TEXT NOT NULL,
                    memory_id      TEXT NOT NULL,
                    content_hash   TEXT NOT NULL,
                    response_json  TEXT NOT NULL,
                    created_at     TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    PRIMARY KEY (caller_id, namespace, operation_key)
                )"""
            )
            self._conn.execute("RELEASE SAVEPOINT migration_v6")
            self._conn.commit()
        except sqlite3.IntegrityError as err:
            self._conn.execute("ROLLBACK TO SAVEPOINT migration_v6")
            self._conn.execute("RELEASE SAVEPOINT migration_v6")
            self._conn.commit()
            raise sqlite3.IntegrityError(
                "cannot migrate to namespace-scoped content identity: duplicate "
                "(namespace, content_hash) rows exist; resolve duplicates before "
                "starting HotMem"
            ) from err

        if current_version < 6:
            self._conn.execute("PRAGMA user_version = 6")
            self._conn.commit()
            _trace.info("migrate", "namespace identity + operation ledger; user_version=6")

        self._ensure_mount_identity()

    def _mount_manifest_file(self) -> Path | None:
        """Return the sibling mount manifest path when this DB lives in a mount."""
        if not self.db_path or self.db_path == ":memory:":
            return None
        try:
            manifest_path = Path(self.db_path).resolve().parent / "manifest.json"
            if not manifest_path.is_file():
                return None
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("format"):
                return None
            return manifest_path
        except Exception:
            return None

    def _discover_mount_manifest_id(self) -> str:
        """Read ``mount_id`` from a sibling mount ``manifest.json`` if present."""
        manifest_path = self._mount_manifest_file()
        if manifest_path is None:
            return ""
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return str(data.get("mount_id") or "").strip()
        except Exception:
            return ""

    def _backfill_mount_manifest_id(self, mount_id: str) -> None:
        """Persist the store's logical mount_id into a legacy mount manifest."""
        manifest_path = self._mount_manifest_file()
        if manifest_path is None:
            return
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict) or data.get("mount_id") == mount_id:
            return
        data["mount_id"] = mount_id
        manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")

    def _ensure_mount_identity(self) -> None:
        """Ensure logical mount_id, runtime replica_id, and history_id exist in store_meta (#94)."""
        from hotmem.mount import new_history_id, new_mount_id, new_replica_id

        existing_mount_id = self.get_meta("mount_id")
        existing_replica_id = self.get_meta("replica_id")
        existing_history_id = self.get_meta("history_id")
        manifest_mount_id = self._discover_mount_manifest_id()
        manifest_file = self._mount_manifest_file()
        dirty = False

        if not existing_mount_id:
            mem_count = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            evt_count = int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            if (mem_count > 0 or evt_count > 0) and self.get_meta("legacy_history") is None:
                self.set_meta("legacy_history", "1", _commit=False)
                dirty = True
            if manifest_mount_id:
                self.set_meta("mount_id", manifest_mount_id, _commit=False)
                self.set_meta("mount_bound", "1", _commit=False)
            else:
                minted_mount_id = new_mount_id()
                self.set_meta("mount_id", minted_mount_id, _commit=False)
                if manifest_file is not None:
                    # Legacy mount manifest: persist the DB's identity so the
                    # directory remains portable (#94).
                    self._backfill_mount_manifest_id(minted_mount_id)
                    self.set_meta("mount_bound", "1", _commit=False)
            dirty = True
        elif manifest_mount_id and existing_mount_id != manifest_mount_id:
            raise ValueError(
                f"Store at {self.db_path} is bound to mount_id={existing_mount_id!r}, "
                f"but its mount manifest declares mount_id={manifest_mount_id!r}; "
                "refusing to mix logical identities."
            )
        elif manifest_mount_id:
            if self.get_meta("mount_bound") != "1":
                self.set_meta("mount_bound", "1", _commit=False)
                dirty = True
        elif manifest_file is not None:
            # A mount directory with a legacy manifest: backfill the manifest
            # from the store's authoritative identity.
            self._backfill_mount_manifest_id(existing_mount_id)
            if self.get_meta("mount_bound") != "1":
                self.set_meta("mount_bound", "1", _commit=False)
                dirty = True

        if not existing_replica_id:
            self.set_meta("replica_id", new_replica_id(), _commit=False)
            dirty = True

        if not existing_history_id:
            self.set_meta("history_id", new_history_id(), _commit=False)
            dirty = True

        if dirty:
            self._conn.commit()

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
        _commit: bool = True,
    ) -> None:
        """Insert a memory row.

        ``_commit=False`` lets a caller batch the insert into a wider
        transaction (e.g. the all-or-nothing delta apply, #73).
        """
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
        if _commit:
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
        ttl_seconds: int | None = None,
        created_at: str | None = None,
        namespace: str = "",
        tier: str = "hot",
        updated_at: str | None = None,
        snapshot_id: str = "",
        promotion_state: str = "HOT",
        promotion_candidate: int = 0,
        parent_memory: str = "",
        related_memories: str = "[]",
        tags: str = "[]",
        schema_version: int = 1,
        provenance_json: str | None = None,
        _commit: bool = True,
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
            ttl_seconds=ttl_seconds,
            created_at=created_at,
            namespace=namespace,
            tier=tier,
            memory_type="file",
            source_uri=source_uri,
            source_format=source_format,
            source_checksum=source_checksum or "",
            byte_offset=byte_offset,
            byte_length=byte_length,
            updated_at=updated_at,
            snapshot_id=snapshot_id,
            promotion_state=promotion_state,
            promotion_candidate=promotion_candidate,
            parent_memory=parent_memory,
            related_memories=related_memories,
            fact_summary=fact_summary,
            tags=tags,
            schema_version=schema_version,
            provenance_json=provenance_json,
            _commit=_commit,
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

    def get_memories(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return metadata rows for ``memory_ids`` keyed by id (one query per chunk)."""
        wanted = [str(i) for i in dict.fromkeys(memory_ids) if i]
        if not wanted:
            return {}
        cols = ", ".join(_MEMORY_COLUMNS)
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), 900):
            chunk = wanted[start : start + 900]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT {cols} FROM memories WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                entry = dict(row)
                found[str(entry["id"])] = entry
        return found

    # ── Promotion lifecycle (#42) ───────────────────────────────────────

    def update_promotion_state(
        self,
        memory_id: str,
        state: str,
        *,
        updated_at: str | None = None,
        _commit: bool = True,
    ) -> None:
        """Update promotion_state + updated_at on a memory row.

        Called by lifecycle.transition() after validating the transition.
        ``_commit=False`` lets the caller batch the state update + event emit
        in one transaction (atomicity). The caller must commit.
        """
        ts = updated_at or __import__("datetime").datetime.now(__import__("datetime").UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._conn.execute(
            "UPDATE memories SET promotion_state = ?, updated_at = ? WHERE id = ?",
            (state, ts, memory_id),
        )
        if _commit:
            self._conn.commit()

    def set_promotion_candidate(
        self, memory_id: str, candidate: int, *, _commit: bool = True
    ) -> None:
        """Set the promotion_candidate flag (0/1) on a memory row."""
        self._conn.execute(
            "UPDATE memories SET promotion_candidate = ? WHERE id = ?",
            (candidate, memory_id),
        )
        if _commit:
            self._conn.commit()

    def list_promotion_candidates(
        self, *, namespace: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        """Return memories flagged as promotion candidates.

        Pure DB read — no file hydration.
        """
        clauses = ["promotion_candidate = 1"]
        params: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        if state is not None:
            clauses.append("promotion_state = ?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses)
        cols = ", ".join(_MEMORY_COLUMNS)
        rows = self._conn.execute(
            f"SELECT {cols} FROM memories{where} ORDER BY created_at",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_by_promotion_state(
        self, state: str, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return memories in a given promotion_state.

        Pure DB read — no file hydration.
        """
        clauses = ["promotion_state = ?"]
        params: list[Any] = [state]
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        where = " WHERE " + " AND ".join(clauses)
        cols = ", ".join(_MEMORY_COLUMNS)
        rows = self._conn.execute(
            f"SELECT {cols} FROM memories{where} ORDER BY created_at",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

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

    def upsert_bundle_index(self, entry: Any, *, _commit: bool = True) -> None:
        """Upsert a BundleIndexEntry into the ``bundle_index`` table.

        ``entry`` is a ``hotmem.bundle_index.BundleIndexEntry``; we accept Any
        to avoid a circular import (bundle_index imports db).

        Set ``_commit=False`` to batch multiple upserts in a single transaction
        (the caller manages BEGIN/COMMIT).
        """
        import json as _json

        self._conn.execute(
            """INSERT OR REPLACE INTO bundle_index
               (path, primary_file, metadata_json, attachment_count,
                checksum, modified_time, size_hint, warning_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.path,
                entry.primary_file,
                _json.dumps(entry.metadata_summary or {}),
                entry.attachment_count,
                getattr(entry, "checksum", ""),
                entry.modified_time,
                entry.size_hint,
                entry.warning_count,
            ),
        )
        if _commit:
            self._conn.commit()

    def list_bundle_index(self) -> list[dict[str, Any]]:
        """Return all bundle index entries (metadata only; no file I/O)."""
        rows = self._conn.execute(
            """SELECT path, primary_file, metadata_json, attachment_count,
                      checksum, modified_time, size_hint, warning_count,
                      discovered_at
               FROM bundle_index ORDER BY path"""
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_bundle_index(self) -> None:
        """Remove all bundle index entries."""
        self._conn.execute("DELETE FROM bundle_index")
        self._conn.commit()

    # ── Transaction control (#41/#42) ──────────────────────────────────

    def commit(self) -> None:
        """Commit the current transaction (public batch-commit API)."""
        self._conn.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self._conn.rollback()

    def get_meta(self, key: str) -> str | None:
        """Return a store-level metadata value (e.g. persistent store_id or mount_id)."""
        try:
            row = self._conn.execute(
                "SELECT value FROM store_meta WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str, *, _commit: bool = True) -> None:
        """Persist a store-level metadata value."""
        self._conn.execute(
            "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        if _commit:
            self._conn.commit()

    def mount_identity(self, *, runtime_generation: str = "") -> Any:
        """Return the store's current MountIdentity (#94)."""
        from hotmem.mount import MountIdentity

        return MountIdentity(
            mount_id=self.get_meta("mount_id") or "",
            replica_id=self.get_meta("replica_id") or "",
            history_id=self.get_meta("history_id") or "",
            runtime_generation=runtime_generation,
            base_snapshot_id=self.get_meta("base_snapshot_id") or "",
            base_state_fingerprint=self.get_meta("base_state_fingerprint") or "",
            base_lineage_fingerprint=self.get_meta("base_lineage_fingerprint") or "",
            base_source_mount_id=self.get_meta("base_source_mount_id") or "",
            base_source_replica_id=self.get_meta("base_source_replica_id") or "",
            base_history_id=self.get_meta("base_history_id") or "",
            base_cursor=self.get_meta("base_cursor") or "",
            parent_replica_id=self.get_meta("parent_replica_id") or "",
            forked_at_seq=int(self.get_meta("forked_at_seq") or 0),
            forked_at_event_id=self.get_meta("forked_at_event_id") or "",
            cloned_at_seq=int(self.get_meta("cloned_at_seq") or 0),
            trimmed_through_seq=int(self.get_meta("trimmed_through_seq") or 0),
            legacy_history=(self.get_meta("legacy_history") == "1"),
            mount_bound=(self.get_meta("mount_bound") == "1"),
        )

    def set_mount_identity(
        self,
        mount_id: str,
        *,
        replica_id: str | None = None,
        history_id: str | None = None,
        _commit: bool = True,
    ) -> Any:
        """Bind or update the store's logical mount_id (and optional replica/history IDs)."""
        cleaned = mount_id.strip()
        if not cleaned:
            raise ValueError("mount_id must be non-empty")
        self.set_meta("mount_id", cleaned, _commit=False)
        self.set_meta("mount_bound", "1", _commit=False)
        if replica_id is not None:
            if not replica_id.strip():
                raise ValueError("replica_id must be non-empty when provided")
            self.set_meta("replica_id", replica_id.strip(), _commit=False)
        if history_id is not None:
            if not history_id.strip():
                raise ValueError("history_id must be non-empty when provided")
            self.set_meta("history_id", history_id.strip(), _commit=False)
        if _commit:
            self._conn.commit()
        return self.mount_identity()

    def record_clone_lineage(
        self,
        *,
        source_mount_id: str,
        source_replica_id: str = "",
        source_history_id: str = "",
        base_snapshot_id: str = "",
        base_state_fingerprint: str = "",
        base_cursor: str = "",
        base_snapshot_scope: str = "",
        adopt_mount_id: bool = True,
        _commit: bool = True,
    ) -> Any:
        """Record clone/restore base metadata while keeping replica/history IDs distinct (#94)."""
        from hotmem.mount import new_history_id, new_replica_id

        src_mount = (source_mount_id or "").strip()
        src_replica = (source_replica_id or "").strip()
        src_history = (source_history_id or "").strip()

        if adopt_mount_id and src_mount:
            self.set_meta("mount_id", src_mount, _commit=False)
            self.set_meta("mount_bound", "1", _commit=False)

        cur_replica = self.get_meta("replica_id") or ""
        if not cur_replica or (src_replica and cur_replica == src_replica):
            self.set_meta("replica_id", new_replica_id(), _commit=False)

        cur_history = self.get_meta("history_id") or ""
        if not cur_history or (src_history and cur_history == src_history):
            self.set_meta("history_id", new_history_id(), _commit=False)

        if src_mount:
            self.set_meta("base_source_mount_id", src_mount, _commit=False)
        if src_replica:
            self.set_meta("base_source_replica_id", src_replica, _commit=False)
        if src_history:
            self.set_meta("base_history_id", src_history, _commit=False)
        if base_snapshot_id:
            self.set_meta("base_snapshot_id", base_snapshot_id, _commit=False)
        # Semantic snapshot identity (#96) is derived from record state plus
        # the snapshot's namespace scope and reference projection. Persist the
        # latter so lineage comparison can reproduce the manifest digest
        # exactly, including for scoped exports.
        # Clear an earlier scope when the new base is unscoped. Empty
        # snapshots can be hydrated repeatedly into the same clean store, so
        # truthy-only writes would leave the previous semantic base behind.
        self.set_meta("base_snapshot_scope", base_snapshot_scope or "", _commit=False)
        if base_state_fingerprint:
            self.set_meta("base_state_fingerprint", base_state_fingerprint, _commit=False)
        # Clone-base lineage includes portable relationship fields so a hydrated
        # clone diverging only in parent_memory/related_memories is a fork (#94).
        from hotmem.interchange.fingerprint import lineage_fingerprint

        self.set_meta(
            "base_lineage_fingerprint",
            lineage_fingerprint(self.iter_rows(include_embedding=False)),
            _commit=False,
        )
        if base_cursor:
            self.set_meta("base_cursor", base_cursor, _commit=False)

        bounds = self.event_log_bounds()
        self.set_meta("cloned_at_seq", str(bounds["max_seq"]), _commit=False)
        if _commit:
            self._conn.commit()
        return self.mount_identity()

    def restore_lineage(
        self,
        *,
        source_mount_id: str,
        source_replica_id: str = "",
        source_history_id: str = "",
        base_snapshot_id: str = "",
        base_state_fingerprint: str = "",
        base_cursor: str = "",
        base_snapshot_scope: str = "",
        _commit: bool = True,
    ) -> Any:
        """Restore a mount from a snapshot/package: keep mount_id, rotate replica/history (#94)."""
        from hotmem.mount import new_history_id, new_replica_id

        self.set_meta("replica_id", new_replica_id(), _commit=False)
        self.set_meta("history_id", new_history_id(), _commit=False)
        return self.record_clone_lineage(
            source_mount_id=source_mount_id,
            source_replica_id=source_replica_id,
            source_history_id=source_history_id,
            base_snapshot_id=base_snapshot_id,
            base_state_fingerprint=base_state_fingerprint,
            base_cursor=base_cursor,
            base_snapshot_scope=base_snapshot_scope,
            adopt_mount_id=True,
            _commit=_commit,
        )

    def fork_replica(
        self,
        *,
        parent_db: MemoryDB | None = None,
        _commit: bool = True,
    ) -> Any:
        """Explicitly fork a runtime replica on the same logical mount (#94).

        Preserves ``mount_id``, records ``parent_replica_id`` and the fork event
        anchor, and generates fresh ``replica_id`` and ``history_id`` values.
        """
        from hotmem.mount import new_history_id, new_replica_id

        source_db = parent_db if parent_db is not None else self
        parent_ident = source_db.mount_identity()
        bounds = source_db.event_log_bounds()

        if parent_ident.mount_id:
            self.set_meta("mount_id", parent_ident.mount_id, _commit=False)
            self.set_meta("mount_bound", "1", _commit=False)
        self.set_meta("parent_replica_id", parent_ident.replica_id, _commit=False)
        self.set_meta("forked_at_seq", str(bounds["max_seq"]), _commit=False)
        self.set_meta("forked_at_event_id", str(bounds["head_event_id"]), _commit=False)
        self.set_meta("replica_id", new_replica_id(), _commit=False)
        self.set_meta("history_id", new_history_id(), _commit=False)
        if _commit:
            self._conn.commit()
        return self.mount_identity()

    def replace_history(
        self,
        *,
        keep_mount_id: bool = True,
        _commit: bool = True,
    ) -> Any:
        """Record a history replacement for the mount (#94).

        Rotates ``replica_id`` and ``history_id``, clears clone/fork base links
        and retention watermarks, and starts a new history epoch. Existing
        event rows remain as immutable audit history; cursors bound to the
        previous replica/history ids fail closed before mutation.
        """
        from hotmem.mount import new_history_id, new_mount_id, new_replica_id

        prev_replica = self.get_meta("replica_id") or ""
        prev_history = self.get_meta("history_id") or ""
        if prev_replica:
            self.set_meta("replaced_previous_replica_id", prev_replica, _commit=False)
        if prev_history:
            self.set_meta("replaced_previous_history_id", prev_history, _commit=False)
        if not keep_mount_id:
            self.set_meta("mount_id", new_mount_id(), _commit=False)
        self.set_meta("mount_bound", "1", _commit=False)
        self.set_meta("replica_id", new_replica_id(), _commit=False)
        self.set_meta("history_id", new_history_id(), _commit=False)
        for cleared_key in (
            "base_snapshot_id",
            "base_state_fingerprint",
            "base_lineage_fingerprint",
            "base_source_mount_id",
            "base_source_replica_id",
            "base_history_id",
            "base_cursor",
            "parent_replica_id",
            "last_replay_cursor",
        ):
            self.set_meta(cleared_key, "", _commit=False)
        self.set_meta("forked_at_seq", "0", _commit=False)
        self.set_meta("forked_at_event_id", "", _commit=False)
        self.set_meta("cloned_at_seq", "0", _commit=False)
        # A replacement starts a new history epoch; retention watermarks from
        # the previous epoch must not make new cursors unusable (#94).
        self.set_meta("trimmed_through_seq", "0", _commit=False)
        self.set_meta("last_known_head_seq", "0", _commit=False)
        self.set_meta("last_known_head_event_id", "", _commit=False)
        if _commit:
            self._conn.commit()
        return self.mount_identity()

    def find_by_namespace_and_hash(
        self, namespace: str, content_hash: str
    ) -> dict[str, Any] | None:
        """Return an existing memory row in `namespace` matching `content_hash`, if any."""
        if not content_hash:
            return None
        row = self._conn.execute(
            """SELECT id, identifier, namespace, content_hash, memory_type,
                      fact_text, fact_summary, source, importance, metadata_json,
                      provenance_json, created_at
               FROM memories
               WHERE namespace = ? AND content_hash = ?
               LIMIT 1""",
            (namespace, content_hash),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_file_record_by_reference(
        self,
        namespace: str,
        identifier: str,
        source_uri: str,
        byte_offset: int,
        byte_length: int,
        *,
        base_dir: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Find an existing file-backed row by normalized reference tuple (#100).

        Matches pre-#100 rows whose ``content_hash`` encoded a checksum, an
        unnormalized URI spelling, or a bundle attachment byte digest. Rows
        with an empty ``content_hash`` have no logical identity and are
        ignored.
        """
        target_norms = {
            normalize_source_uri(source_uri, base_dir=base_dir),
            normalize_source_uri(source_uri),
        }
        target_norms.discard("")
        if not target_norms:
            return None
        rows = self._conn.execute(
            """SELECT id, identifier, namespace, content_hash, memory_type,
                      fact_text, fact_summary, source, importance, metadata_json,
                      provenance_json, created_at, source_uri, byte_offset, byte_length
               FROM memories
               WHERE identifier = ? AND namespace = ? AND memory_type = 'file'
                 AND byte_offset = ? AND byte_length = ? AND content_hash != ''
               ORDER BY created_at ASC, rowid ASC""",
            (identifier, namespace, byte_offset, byte_length),
        ).fetchall()
        for row in rows:
            row_uri = str(row["source_uri"] or "")
            row_norms = {
                normalize_source_uri(row_uri, base_dir=base_dir),
                normalize_source_uri(row_uri),
            }
            if target_norms & row_norms:
                return dict(row)
        return None

    @staticmethod
    def record_identity_key(record: dict[str, Any] | MemoryRecord) -> tuple[str, str]:
        """Return the namespace-scoped logical identity key for ``record`` (#100).

        Inline records use ``(namespace, content_hash)``. File-backed records
        with a non-empty ``content_hash`` use ``(namespace,
        compute_file_reference_hash(...))`` so pre-#100 and post-#100 file
        hashes resolve to the same logical identity. Records with an empty
        ``content_hash`` have no logical identity and return ``(namespace, "")``.
        """
        if isinstance(record, MemoryRecord):
            ns = str(record.namespace or "")
            ch = str(record.content_hash or "")
            if not ch:
                return (ns, "")
            if record.memory_type == "file" and record.source_uri:
                return (
                    ns,
                    compute_file_reference_hash(
                        record.identifier,
                        record.source_uri,
                        int(record.byte_offset or 0),
                        int(record.byte_length or 0),
                    ),
                )
            return (ns, ch)

        ns = str(record.get("namespace") or "")
        ch = str(record.get("content_hash") or "")
        if not ch:
            return (ns, "")
        if record.get("memory_type") == "file" and record.get("source_uri"):
            return (
                ns,
                compute_file_reference_hash(
                    str(record.get("identifier") or ""),
                    str(record.get("source_uri") or ""),
                    int(record.get("byte_offset") or 0),
                    int(record.get("byte_length") or 0),
                ),
            )
        return (ns, ch)

    def batch_existing_record_identities(
        self, records: list[dict[str, Any]] | list[MemoryRecord]
    ) -> set[tuple[str, str]]:
        """Return the subset of ``record_identity_key(r)`` values already stored.

        Combines :meth:`batch_existing_namespace_hashes` with reference-tuple
        matching for ``memory_type == 'file'`` rows so hydration and SQLite
        imports deduplicate across legacy and post-#100 file hashes (#100).
        """
        if not records:
            return set()
        hash_pairs: list[tuple[str, str]] = []
        file_candidates: list[tuple[str, str, tuple[str, str]]] = []
        for r in records:
            key = self.record_identity_key(r)
            if not key[1]:
                continue
            raw_ch = (
                str(r.content_hash or "")
                if isinstance(r, MemoryRecord)
                else str(r.get("content_hash") or "")
            )
            is_file = (
                r.memory_type == "file"
                if isinstance(r, MemoryRecord)
                else r.get("memory_type") == "file"
            )
            ident = (
                str(r.identifier or "")
                if isinstance(r, MemoryRecord)
                else str(r.get("identifier") or "")
            )
            hash_pairs.append((key[0], raw_ch))
            if key[1] != raw_ch:
                hash_pairs.append(key)
            if is_file and ident:
                file_candidates.append((key[0], ident, key))

        existing_hashes = self.batch_existing_namespace_hashes(hash_pairs)
        matched: set[tuple[str, str]] = set()
        for r in records:
            key = self.record_identity_key(r)
            if not key[1]:
                continue
            raw_ch = (
                str(r.content_hash or "")
                if isinstance(r, MemoryRecord)
                else str(r.get("content_hash") or "")
            )
            if key in existing_hashes or (key[0], raw_ch) in existing_hashes:
                matched.add(key)

        unresolved_files = [
            (ns, ident, key) for ns, ident, key in file_candidates if key not in matched
        ]
        if not unresolved_files:
            return matched

        wanted_namespaces = {ns for ns, _, _ in unresolved_files}
        unique_idents = sorted({ident for _, ident, _ in unresolved_files})
        stored_file_keys: set[tuple[str, str]] = set()
        for start in range(0, len(unique_idents), _SEARCH_BIND_CHUNK):
            chunk = unique_idents[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT namespace, identifier, source_uri, byte_offset, byte_length
                    FROM memories
                    WHERE memory_type = 'file' AND content_hash != ''
                      AND identifier IN ({placeholders})""",
                chunk,
            ).fetchall()
            for row in rows:
                row_ns = str(row["namespace"] or "")
                if row_ns not in wanted_namespaces:
                    continue
                row_uri = str(row["source_uri"] or "")
                if not row_uri:
                    continue
                stored_file_keys.add(
                    (
                        row_ns,
                        compute_file_reference_hash(
                            str(row["identifier"] or ""),
                            row_uri,
                            int(row["byte_offset"] or 0),
                            int(row["byte_length"] or 0),
                        ),
                    )
                )
        for _, _, key in unresolved_files:
            if key in stored_file_keys:
                matched.add(key)
        return matched

    def batch_existing_namespace_hashes(self, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        """Return the subset of `(namespace, content_hash)` pairs already in `memories`."""
        existing: set[tuple[str, str]] = set()
        if not pairs:
            return existing
        candidate_hashes = list({h for _, h in pairs if h})
        present_hashes = list(self.batch_existing_hashes(candidate_hashes))
        if not present_hashes:
            return existing
        for start in range(0, len(present_hashes), _SEARCH_BIND_CHUNK):
            chunk = present_hashes[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT namespace, content_hash FROM memories "
                f"WHERE content_hash IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                existing.add((str(r[0] or ""), str(r[1] or "")))
        return existing & {(str(ns or ""), str(h or "")) for ns, h in pairs}

    def get_operation_ledger(
        self, caller_id: str, namespace: str, operation_key: str
    ) -> dict[str, Any] | None:
        """Look up an idempotent operation ledger record."""
        row = self._conn.execute(
            """SELECT caller_id, namespace, operation_key, operation_name,
                      request_hash, memory_id, content_hash, response_json, created_at
               FROM operation_ledger
               WHERE caller_id = ? AND namespace = ? AND operation_key = ?""",
            (caller_id, namespace, operation_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def put_operation_ledger(
        self,
        *,
        caller_id: str,
        namespace: str,
        operation_key: str,
        operation_name: str,
        request_hash: str,
        memory_id: str,
        content_hash: str,
        response_json: str,
        _commit: bool = True,
    ) -> None:
        """Record an idempotent operation outcome in the current transaction."""
        self._conn.execute(
            """INSERT OR REPLACE INTO operation_ledger
                   (caller_id, namespace, operation_key, operation_name,
                    request_hash, memory_id, content_hash, response_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                caller_id,
                namespace,
                operation_key,
                operation_name,
                request_hash,
                memory_id,
                content_hash,
                response_json,
            ),
        )
        if _commit:
            self._conn.commit()

    # ── Event log (#41) ────────────────────────────────────────────────

    def append_event(
        self,
        *,
        event_type: str,
        event_id: str,
        memory_id: str | None = None,
        namespace: str = "",
        occurred_at: str,
        payload_json: str = "{}",
        _commit: bool = True,
    ) -> int:
        """Append one row to the local event log. Returns the monotonic ``seq``.

        ``_commit=False`` lets the caller batch this INSERT into the same
        transaction as the memory write (atomicity + single fsync).
        """
        cursor = self._conn.execute(
            """INSERT INTO events
                   (event_id, memory_id, namespace, event_type, occurred_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, memory_id, namespace, event_type, occurred_at, payload_json),
        )
        if _commit:
            self._conn.commit()
        return int(cursor.lastrowid)

    def query_events(
        self,
        *,
        memory_id: str | None = None,
        namespace: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 100,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        """Return event rows matching the supplied filters, ordered by ``seq``.

        Pagination cursors:
            ``after_seq``  — return events with seq strictly greater (forward).
            ``before_seq`` — return events with seq strictly less (backward).

        When ``ascending`` is False, rows are returned newest-first, which
        flips the meaning of the cursor relative to the result window but
        keeps ``seq`` as the deterministic tiebreaker.

        Pure SQL — never touches the storage adapter or any backing file.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if memory_id is not None:
            clauses.append("memory_id = ?")
            params.append(memory_id)
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("occurred_at <= ?")
            params.append(until)
        if after_seq is not None:
            clauses.append("seq > ?")
            params.append(after_seq)
        if before_seq is not None:
            clauses.append("seq < ?")
            params.append(before_seq)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        direction = "ASC" if ascending else "DESC"
        rows = self._conn.execute(
            f"""SELECT seq, event_id, memory_id, namespace, event_type,
                      occurred_at, payload_json
               FROM events{where}
               ORDER BY seq {direction}
               LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def event_at_seq(self, seq: int) -> dict[str, Any] | None:
        """Return the event row at exact ``seq``, or None if absent (#94)."""
        row = self._conn.execute(
            """SELECT seq, event_id, memory_id, namespace, event_type,
                      occurred_at, payload_json
               FROM events WHERE seq = ?""",
            (seq,),
        ).fetchone()
        return dict(row) if row is not None else None

    def event_id_page(
        self,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return lightweight ``(seq, event_id, event_type)`` rows after ``seq``.

        Lineage comparisons use this instead of loading full event payloads, so
        identity checks stay bounded for long histories (#94).
        """
        rows = self._conn.execute(
            """SELECT seq, event_id, event_type
               FROM events WHERE seq > ?
               ORDER BY seq ASC LIMIT ?""",
            (after_seq, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def event_log_bounds(self) -> dict[str, Any]:
        """Return current event-log sequence bounds and trim watermarks (#94)."""
        agg = self._conn.execute(
            "SELECT COUNT(*) AS cnt, MIN(seq) AS min_seq, MAX(seq) AS max_seq FROM events"
        ).fetchone()
        cnt = int(agg["cnt"] or 0) if agg is not None else 0
        min_seq = int(agg["min_seq"] or 0) if agg is not None else 0
        max_seq = int(agg["max_seq"] or 0) if agg is not None else 0
        trimmed_through = int(self.get_meta("trimmed_through_seq") or 0)
        last_known_head_seq = int(self.get_meta("last_known_head_seq") or 0)
        last_known_head_event_id = self.get_meta("last_known_head_event_id") or ""

        head_event_id = ""
        if max_seq > 0:
            head_row = self._conn.execute(
                "SELECT event_id FROM events WHERE seq = ?", (max_seq,)
            ).fetchone()
            if head_row is not None:
                head_event_id = str(head_row["event_id"])
        elif last_known_head_seq > 0:
            head_event_id = last_known_head_event_id

        head_seq = max(max_seq, trimmed_through, last_known_head_seq)
        return {
            "event_count": cnt,
            "min_seq": min_seq,
            "max_seq": max_seq,
            "head_seq": head_seq,
            "head_event_id": head_event_id,
            "trimmed_through_seq": trimmed_through,
            "legacy_history": (self.get_meta("legacy_history") == "1"),
        }

    def trim_events_before_seq(self, seq: int) -> int:
        """Local retention helper: delete events with ``seq < seq``.

        Temporarily disables the append-only DELETE trigger (retention is the
        one sanctioned exception). Records ``trimmed_through_seq`` in
        ``store_meta`` so stale cursors are rejected before mutation (#94).
        Returns the number of deleted rows.
        """
        max_row = self._conn.execute(
            "SELECT MAX(seq) AS max_seq FROM events WHERE seq < ?", (seq,)
        ).fetchone()
        head_row = self._conn.execute(
            "SELECT seq, event_id FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self._conn.execute("PRAGMA recursive_triggers = OFF")
        self._conn.execute("DROP TRIGGER IF EXISTS events_no_delete")
        cursor = self._conn.execute("DELETE FROM events WHERE seq < ?", (seq,))
        deleted = cursor.rowcount if cursor.rowcount != -1 else 0
        if deleted > 0 and max_row is not None and max_row["max_seq"] is not None:
            deleted_max = int(max_row["max_seq"])
            prev_trimmed = int(self.get_meta("trimmed_through_seq") or 0)
            if deleted_max > prev_trimmed:
                self.set_meta("trimmed_through_seq", str(deleted_max), _commit=False)
            if head_row is not None:
                prev_head = int(self.get_meta("last_known_head_seq") or 0)
                if int(head_row["seq"]) >= prev_head:
                    self.set_meta("last_known_head_seq", str(int(head_row["seq"])), _commit=False)
                    self.set_meta(
                        "last_known_head_event_id", str(head_row["event_id"]), _commit=False
                    )
        self._conn.commit()
        self._conn.execute("PRAGMA recursive_triggers = ON")
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
            "BEGIN SELECT RAISE(ABORT, 'events table is append-only: DELETE is forbidden'); END"
        )
        self._conn.commit()
        return deleted

    def trim_events_by_count(self, keep: int) -> int:
        """Local retention helper: keep only the most recent ``keep`` events.

        Temporarily disables the append-only DELETE trigger and updates
        ``trimmed_through_seq`` in ``store_meta`` (#94). Returns the number
        of deleted rows.
        """
        if keep < 0:
            raise ValueError("keep must be non-negative")
        max_row = self._conn.execute(
            """SELECT MAX(seq) AS max_seq FROM events
               WHERE seq NOT IN (
                   SELECT seq FROM events ORDER BY seq DESC LIMIT ?
               )""",
            (keep,),
        ).fetchone()
        head_row = self._conn.execute(
            "SELECT seq, event_id FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self._conn.execute("DROP TRIGGER IF EXISTS events_no_delete")
        cursor = self._conn.execute(
            """DELETE FROM events
               WHERE seq NOT IN (
                   SELECT seq FROM events ORDER BY seq DESC LIMIT ?
               )""",
            (keep,),
        )
        deleted = cursor.rowcount if cursor.rowcount != -1 else 0
        if deleted > 0 and max_row is not None and max_row["max_seq"] is not None:
            deleted_max = int(max_row["max_seq"])
            prev_trimmed = int(self.get_meta("trimmed_through_seq") or 0)
            if deleted_max > prev_trimmed:
                self.set_meta("trimmed_through_seq", str(deleted_max), _commit=False)
            if head_row is not None:
                prev_head = int(self.get_meta("last_known_head_seq") or 0)
                if int(head_row["seq"]) >= prev_head:
                    self.set_meta("last_known_head_seq", str(int(head_row["seq"])), _commit=False)
                    self.set_meta(
                        "last_known_head_event_id", str(head_row["event_id"]), _commit=False
                    )
        self._conn.commit()
        self._conn.execute(
            "CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
            "BEGIN SELECT RAISE(ABORT, 'events table is append-only: DELETE is forbidden'); END"
        )
        self._conn.commit()
        return deleted

    def insert_many_ignore(self, records: Iterable[MemoryRecord], *, _commit: bool = True) -> int:
        """Insert many memory rows in one transaction, ignoring duplicate hashes/ids.

        ``_commit=False`` lets a caller batch many inserts into one wider
        transaction (e.g. the all-or-nothing package restore, #69) and commit
        or roll back explicitly.
        """
        rows = [tuple(getattr(record, c) for c in _MEMORY_COLUMNS) for record in records]
        if not rows:
            return 0

        cursor = self._conn.executemany(_INSERT_OR_IGNORE_SQL, rows)
        if _commit:
            self._conn.commit()
        inserted = cursor.rowcount if cursor.rowcount != -1 else 0
        _trace.debug("insert_many", f"stored {inserted} memories", detail={"attempted": len(rows)})
        return inserted

    def search_with_cosine(
        self,
        query_embedding: bytes,
        *,
        include_archived: bool = False,
        embedding_model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all memories with their cosine similarity to the query embedding.

        Archived memories are excluded by default; pass ``include_archived=True``
        for audit/full profiles.

        ``embedding_model`` is mixed-space safety (issue #78): when given, rows
        stored under a different descriptor key keep a cosine score of exactly
        0.0 — they still surface through FTS/importance, never mis-score.
        ``None`` keeps the historical unguarded cosine for direct callers.
        """
        archived_clause = "" if include_archived else " AND promotion_state != 'ARCHIVED'"
        cosine_expr, cosine_params = _cosine_guard(embedding_model)
        rows = self._conn.execute(
            f"""SELECT id, identifier, fact_text, fact_summary, importance,
                       metadata_json, source, created_at, namespace, provenance_json,
                       tags, {cosine_expr} AS cosine_score
                FROM memories
                WHERE {_ttl_live()}{archived_clause}
                ORDER BY cosine_score DESC, id ASC""",
            # Bind in statement-text order: the cosine ?s precede the WHERE ?s.
            (*cosine_params, query_embedding),
        ).fetchall()
        return [dict(r) for r in rows]

    def fts_search(self, query: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return full-text matches with raw BM25 scores.

        Archived memories are excluded by default; pass ``include_archived=True``
        for audit/full profiles.
        """
        fts_query = _fts_query(query)
        if not fts_query:
            return []

        archived_clause = "" if include_archived else " AND m.promotion_state != 'ARCHIVED'"
        rows = self._conn.execute(
            f"""SELECT m.id, m.identifier, m.fact_text, m.importance, m.metadata_json,
                       m.source, m.created_at, m.namespace, m.provenance_json,
                       m.tags, bm25(memories_fts) AS bm25_score
                FROM memories_fts
                JOIN memories AS m ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH ?
                  AND {_ttl_live("m.")}{archived_clause}
                ORDER BY bm25_score ASC""",
            (fts_query,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """Return total number of stored memories."""
        row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    def all_ids(self) -> list[str]:
        """All memory ids (annotation evidence resolution, #79)."""
        return [r[0] for r in self._conn.execute("SELECT id FROM memories").fetchall()]

    def fetch_existing_ids(self, memory_ids: list[str]) -> set[str]:
        """Which of the given ids exist, via ONE chunked query (#79).

        Annotation evidence resolution checks only the referenced subset
        against the store — never a full-table id scan.
        """
        existing: set[str] = set()
        ordered = sorted(memory_ids)  # deterministic chunking for any iterable
        for start in range(0, len(ordered), _SEARCH_BIND_CHUNK):
            chunk = ordered[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders})", chunk
            ).fetchall()
            existing.update(r[0] for r in rows)
        return existing

    def fetch_metadata_by_hashes(self, content_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch id + parsed metadata for rows matching content hashes (#79).

        ONE chunked query — used by the annotation merge path to compare
        incoming envelopes against stored records without re-reading rows.
        Returns ``{content_hash: {"id": ..., "metadata": dict}}``.

        Content hashes are only unique together with a namespace (#100); use
        :meth:`fetch_metadata_by_namespace_hashes` when scoping matters.
        """
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(content_hashes), _SEARCH_BIND_CHUNK):
            chunk = content_hashes[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT content_hash, id, metadata_json
                    FROM memories
                    WHERE content_hash IN ({placeholders})""",
                chunk,
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                out[str(row["content_hash"])] = {"id": row["id"], "metadata": metadata}
        return out

    def fetch_metadata_by_namespace_hashes(
        self, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Fetch id + metadata keyed by ``(namespace, content_hash)`` (#100).

        Prevents annotation merges from targeting a same-content row that
        lives in a different namespace.
        """
        out: dict[tuple[str, str], dict[str, Any]] = {}
        if not pairs:
            return out
        wanted = {(str(ns or ""), str(h or "")) for ns, h in pairs if h}
        hashes = list({h for _, h in wanted})
        for start in range(0, len(hashes), _SEARCH_BIND_CHUNK):
            chunk = hashes[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT namespace, content_hash, id, metadata_json
                    FROM memories
                    WHERE content_hash IN ({placeholders})""",
                chunk,
            ).fetchall()
            for row in rows:
                key = (str(row["namespace"] or ""), str(row["content_hash"]))
                if key not in wanted:
                    continue
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                out[key] = {"id": row["id"], "metadata": metadata}
        return out

    def update_metadata_json(
        self, memory_id: str, metadata_json: str, *, commit: bool = True
    ) -> bool:
        """Update a row's metadata_json in the open transaction (#79 merge)."""
        cursor = self._conn.execute(
            "UPDATE memories SET metadata_json = ? WHERE id = ?",
            (metadata_json, memory_id),
        )
        if commit:
            self._conn.commit()
        return cursor.rowcount > 0

    def fetch_embedding_blobs(
        self,
        memory_ids: list[str],
        *,
        embedding_model: str | None = None,
    ) -> dict[str, bytes]:
        """Batched raw embedding fetch for second-stage reranking (#80).

        ONE chunked SELECT for the whole candidate pool — never one query
        per candidate. ``embedding_model`` applies the interchange §5.1
        compatibility mapping (empty stored model = legacy hash): rows from
        other embedding spaces are simply absent, which rerankers treat as
        zero-similarity contributors, never errors. Rows without a vector
        (file-backed without summary) are absent for the same reason.
        """
        if not memory_ids:
            return {}
        out: dict[str, bytes] = {}
        for start in range(0, len(memory_ids), _SEARCH_BIND_CHUNK):
            chunk = memory_ids[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT id, embedding, embedding_model
                    FROM memories
                    WHERE id IN ({placeholders})
                      AND embedding IS NOT NULL AND length(embedding) > 0""",
                chunk,
            ).fetchall()
            for row in rows:
                stored = str(row["embedding_model"] or "")
                if embedding_model is not None:
                    if embedding_model == EMBEDDING_MODEL:
                        if stored not in ("", EMBEDDING_MODEL):
                            continue
                    elif stored != embedding_model:
                        continue
                out[str(row["id"])] = row["embedding"]
        return out

    def fingerprint(self) -> tuple[int, int, int]:
        """Return a cheap store fingerprint: (COUNT, MAX(rowid), MAX(event seq)).

        Used by the optional derived vector index (#49) to detect staleness
        against its rebuild marker. The rowid component catches INSERT OR
        REPLACE rewrites that a plain count comparison would miss; the event
        seq component catches server-mediated mutations whose count/rowid
        happen to be unchanged (rowid reuse). ``index.rebuilt`` events are
        excluded — the rebuild endpoint appends its own event AFTER
        snapshotting the fingerprint, and that event must not invalidate the
        marker it just wrote. Pure SQL — no file I/O.
        """
        row = self._conn.execute(
            """SELECT (SELECT COUNT(*) FROM memories),
                      COALESCE((SELECT MAX(rowid) FROM memories), 0),
                      COALESCE(
                          (SELECT MAX(seq) FROM events WHERE event_type != 'index.rebuilt'),
                          0)"""
        ).fetchone()
        return (int(row[0]), int(row[1]), int(row[2]))

    def has_unindexed_text_rows(self) -> bool:
        """True when any row has searchable text but no embedding.

        Such rows (legacy imports with empty embeddings) rank via importance
        in the full-scan path but cannot appear in vector-index candidates.
        The accelerated search path (#49) checks this and uses the exact
        full scan when such rows exist, preserving ranking parity.
        """
        row = self._conn.execute(
            """SELECT EXISTS(SELECT 1 FROM memories
                WHERE (embedding IS NULL OR length(embedding) = 0)
                  AND (COALESCE(fact_text, '') != '' OR COALESCE(fact_summary, '') != ''))"""
        ).fetchone()
        return bool(row[0])

    def search_by_ids(
        self,
        query_embedding: bytes,
        memory_ids: list[str],
        *,
        include_archived: bool = False,
        embedding_model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the given memories with cosine scores — same predicates as search.

        Applies the identical TTL-live and archived filters as
        ``search_with_cosine``, so ids that expired, were archived, or were
        deleted since indexing are filtered out here. Used by the accelerated
        search path (#49) to re-score vector-index candidates in SQLite.
        ``embedding_model`` applies the same mixed-space guard (issue #78).

        Ids are bound in chunks of ``_SEARCH_BIND_CHUNK`` so any configured
        ``oversample`` stays under SQLite's legacy 999-variable cap; chunk
        results are merged and re-sorted into the same canonical order.
        """
        if not memory_ids:
            return []
        archived_clause = "" if include_archived else " AND promotion_state != 'ARCHIVED'"
        cosine_expr, cosine_params = _cosine_guard(embedding_model)
        results: list[dict[str, Any]] = []
        for start in range(0, len(memory_ids), _SEARCH_BIND_CHUNK):
            chunk = memory_ids[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT id, identifier, fact_text, fact_summary, importance,
                           metadata_json, source, created_at, namespace, provenance_json,
                           tags, {cosine_expr} AS cosine_score
                    FROM memories
                    WHERE id IN ({placeholders})
                      AND {_ttl_live()}{archived_clause}""",
                # Bind in statement-text order: cosine ?s, then id IN ?s.
                (*cosine_params, query_embedding, *chunk),
            ).fetchall()
            results.extend(dict(r) for r in rows)
        # Same total order as the per-chunk SQL ORDER BY: ids are unique, so
        # (cosine DESC, id ASC) is deterministic. NULL cosine (no embedding)
        # sorts last, matching SQLite's DESC default.
        results.sort(
            key=lambda r: (
                r["cosine_score"] is None,
                -(r["cosine_score"] or 0.0),
                r["id"],
            )
        )
        return results

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

    def iter_rows(self, *, include_embedding: bool = True, batch: int = 1000) -> Iterator[dict]:
        """Stream all memory rows as dicts in id order, fetchmany-batched.

        Streaming export path (interchange #67/#69): memory stays O(batch)
        instead of loading every row at once, and the ORDER BY id makes the
        output order deterministic.
        """
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
        query = f"SELECT {base}, {v2_cols}{tail} FROM memories ORDER BY id"
        cursor = self._conn.execute(query)
        while rows := cursor.fetchmany(batch):
            yield from (dict(r) for r in rows)

    def content_hashes(self) -> set[str]:
        """Return non-empty content hashes currently stored in the database."""
        rows = self._conn.execute(
            "SELECT content_hash FROM memories WHERE content_hash != ''"
        ).fetchall()
        return {row["content_hash"] for row in rows}

    def record_sync_checkpoint(
        self,
        *,
        base_logical_id: str,
        delta_digest: str,
        applied_ops: int,
        resulting_state_fingerprint: str | None,
        applied_at: str,
        _commit: bool = True,
    ) -> None:
        """Persist one applied-delta checkpoint (#73).

        ``_commit=False`` commits together with the delta's record writes so
        the checkpoint and canonical state advance atomically.
        """
        self._conn.execute(
            """INSERT INTO sync_checkpoints
               (base_logical_id, delta_digest, applied_ops,
                resulting_state_fingerprint, applied_at)
               VALUES (?, ?, ?, ?, ?)""",
            (base_logical_id, delta_digest, applied_ops, resulting_state_fingerprint, applied_at),
        )
        if _commit:
            self._conn.commit()

    def latest_sync_checkpoint(self, base_logical_id: str) -> dict[str, Any] | None:
        """Return the most recent checkpoint for a base package, if any."""
        row = self._conn.execute(
            """SELECT base_logical_id, delta_digest, applied_ops,
                      resulting_state_fingerprint, applied_at
               FROM sync_checkpoints WHERE base_logical_id = ?
               ORDER BY id DESC LIMIT 1""",
            (base_logical_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_handoff(
        self,
        *,
        package_id: str,
        handoff_id: str,
        mode: str,
        counts_json: str,
        applied_at: str,
        _commit: bool = True,
    ) -> None:
        """Persist one applied-handoff ledger row (#101).

        ``_commit=False`` commits together with the handoff's record writes
        so the ledger and canonical state advance atomically.
        """
        self._conn.execute(
            """INSERT INTO handoff_ledger
               (package_id, handoff_id, mode, applied_at, counts_json)
               VALUES (?, ?, ?, ?, ?)""",
            (package_id, handoff_id, mode, applied_at, counts_json),
        )
        if _commit:
            self._conn.commit()

    def get_handoff(self, package_id: str) -> dict[str, Any] | None:
        """Return the ledger row for an applied handoff package, if any."""
        row = self._conn.execute(
            """SELECT package_id, handoff_id, mode, applied_at, counts_json
               FROM handoff_ledger WHERE package_id = ?""",
            (package_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def batch_existing_hashes(self, hashes: list[str]) -> set[str]:
        """Return the subset of ``hashes`` already stored (chunked SELECT).

        Hash-only lookup. Content hashes are unique only together with a
        namespace (#100), so this is a coarse pre-filter; identity decisions
        must use :meth:`batch_existing_namespace_hashes`. Empty hashes carry
        no logical identity and are never reported as present.
        """
        unique = sorted({h for h in hashes if h})
        found: set[str] = set()
        for start in range(0, len(unique), _SEARCH_BIND_CHUNK):
            chunk = unique[start : start + _SEARCH_BIND_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT content_hash FROM memories WHERE content_hash IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(row["content_hash"] for row in rows)
        return found

    def exists(self, content_hash: str, *, namespace: str | None = None) -> bool:
        """Check whether a non-empty content hash exists.

        With ``namespace`` the lookup follows the logical identity contract
        ``(namespace, content_hash)``. Without it the historical global
        lookup is preserved for callers that intentionally ask a coarser
        question. An empty hash never matches: it is the absence of logical
        identity, not a shared value (#100).
        """
        if not content_hash:
            return False
        if namespace is None:
            row = self._conn.execute(
                "SELECT 1 FROM memories WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM memories WHERE namespace = ? AND content_hash = ? LIMIT 1",
                (namespace, content_hash),
            ).fetchone()
        return row is not None

    def list_by_identifier(
        self,
        identifier: str,
        *,
        order: str = "asc",
        limit: int = 100,
        row_filter: Any | None = None,
        row_transform: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Return memories in created_at order with insertion-order tie-breaking.

        Excludes TTL-expired memories, consistent with search.
        """
        direction = "ASC" if order.lower() == "asc" else "DESC"
        if row_filter is None and row_transform is None:
            rows = self._conn.execute(
                f"""SELECT id, identifier, fact_text, importance, metadata_json, source,
                           created_at, tags, provenance_json
                    FROM memories
                    WHERE identifier = ?
                      AND {_ttl_live()}
                    ORDER BY created_at {direction}, rowid {direction}
                    LIMIT ?""",
                (identifier, limit),
            ).fetchall()
            out_rows: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                raw_tags = d.pop("tags", None)
                raw_prov = d.pop("provenance_json", None)
                parsed_tags: list[str] = []
                if raw_tags:
                    try:
                        loaded = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
                        if isinstance(loaded, list):
                            parsed_tags = [str(item) for item in loaded]
                    except (json.JSONDecodeError, TypeError):
                        parsed_tags = []
                parsed_prov: Any = None
                if raw_prov:
                    try:
                        parsed_prov = (
                            json.loads(raw_prov) if isinstance(raw_prov, str) else raw_prov
                        )
                    except (json.JSONDecodeError, TypeError):
                        parsed_prov = None
                if parsed_tags or parsed_prov is not None:
                    d["tags"] = parsed_tags
                if parsed_prov is not None:
                    d["provenance"] = parsed_prov
                out_rows.append(d)
            return out_rows

        rows = self._conn.execute(
            f"""SELECT id, identifier, fact_text, importance, metadata_json, source,
                       created_at, namespace, tags, provenance_json
                FROM memories
                WHERE identifier = ?
                  AND {_ttl_live()}
                ORDER BY created_at {direction}, rowid {direction}""",
            (identifier,),
        ).fetchall()
        filtered: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if row_filter is not None and not row_filter(d):
                continue
            if row_transform is not None:
                transformed = row_transform(d)
                if transformed is None:
                    continue
                d = transformed
            d.pop("namespace", None)
            raw_tags = d.pop("tags", None)
            raw_prov = d.pop("provenance_json", None)
            parsed_tags = []
            if raw_tags:
                try:
                    loaded = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
                    if isinstance(loaded, list):
                        parsed_tags = [str(item) for item in loaded]
                except (json.JSONDecodeError, TypeError):
                    parsed_tags = []
            d["tags"] = parsed_tags
            if raw_prov:
                try:
                    parsed_prov = json.loads(raw_prov) if isinstance(raw_prov, str) else raw_prov
                except (json.JSONDecodeError, TypeError):
                    parsed_prov = None
                if parsed_prov is not None:
                    d["provenance"] = parsed_prov
            filtered.append(d)
            if len(filtered) >= limit:
                break
        return filtered

    def import_sqlite(
        self,
        src_path: str | Path,
        *,
        target_namespace: str | None = None,
    ) -> tuple[int, int]:
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

        Deduplication follows the logical identity contract (#100):
        ``(namespace, content_hash)`` for non-empty hashes. Two identical
        records in distinct namespaces both survive; a repeat in the same
        namespace is skipped. Rows with an empty hash have no logical
        identity and are deduplicated only by their primary key id.

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

            # Database-backed dedup (interchange #67): per-batch chunked
            # SELECT instead of loading the entire destination hash set;
            # a batch-local set catches intra-batch duplicates. Identity is
            # scoped by namespace (#100), so the same content in another
            # namespace is a distinct record. Empty hashes are not identity
            # keys and never collide with each other.
            batch_seen: set[tuple[str, str]] = set()
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                batch_seen.clear()
                rows_dicts = [dict(row) for row in batch]
                if target_namespace is not None:
                    for d in rows_dicts:
                        if not d.get("namespace"):
                            d["namespace"] = target_namespace
                existing = self.batch_existing_record_identities(rows_dicts)
                records: list[MemoryRecord] = []
                for d in rows_dicts:
                    key = self.record_identity_key(d)
                    if key[1] and (key in existing or key in batch_seen):
                        skipped += 1
                        continue
                    if key[1]:
                        batch_seen.add(key)
                    # Build from the columns present; dataclass defaults fill the
                    # rest (missing v2 columns in v0.1 sources).
                    record_fields = {c: d[c] for c in select_cols}
                    if d.get("namespace"):
                        record_fields["namespace"] = str(d["namespace"])
                    records.append(MemoryRecord(**record_fields))
                if records:
                    inserted = self.insert_many_ignore(records)
                    loaded += inserted
                    skipped += len(records) - inserted
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
