"""mem0 importer — read a mem0 SQLite history DB and yield HotMem swap records.

Purpose:
    Convert the canonical, always-present mem0 storage format (the SQLite
    `history` table written by mem0.SQLiteManager) into HotMem swap-record
    dicts so `swap.hydrate` can ingest them in one command.

    mem0's vector store (Qdrant/Chroma/...) holds embeddings at dimensions
    that differ from HotMem's, so embeddings are intentionally NOT reused —
    hydrate re-embeds with HotMem's embedder, identical to the JSONL path
    with no stored embedding. The history table has the same text + identity
    + timestamps, requires no extra client libs, and is a single file.

Interface:
    read_mem0_sqlite(path, on_progress=None) -> Iterator[dict]

Deps: stdlib sqlite3 only.

Extension: add readers for other mem0 backends (qdrant dump, chroma sqlite)
in sibling modules.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from hotmem.trace import get_tracer

_trace = get_tracer("importers.mem0")

_MEM0_HISTORY_COLUMNS = {
    "id",
    "memory_id",
    "old_memory",
    "new_memory",
    "event",
    "created_at",
    "updated_at",
    "is_deleted",
    "actor_id",
    "role",
}

_SELECT_SQL = """
    SELECT new_memory, actor_id, created_at, id
    FROM history
    WHERE event = 'ADD'
      AND is_deleted = 0
      AND new_memory IS NOT NULL
      AND new_memory != ''
"""


def _validate_mem0_schema(conn: sqlite3.Connection) -> None:
    """Raise ValueError if the DB does not look like a mem0 history DB."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='history'"
        ).fetchall()
    except sqlite3.DatabaseError as err:
        raise ValueError(f"not a SQLite database: {err}") from err

    if not rows:
        raise ValueError("no 'history' table found; this does not appear to be a mem0 SQLite DB")

    cols = {row[1] for row in conn.execute("PRAGMA table_info(history)").fetchall()}
    missing = _MEM0_HISTORY_COLUMNS - cols
    if missing:
        raise ValueError(f"history table is missing expected mem0 columns: {sorted(missing)}")


def read_mem0_sqlite(
    path: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> Iterator[dict]:
    """Yield HotMem swap-record dicts from a mem0 SQLite history DB.

    Only rows with event='ADD' and is_deleted=0 are emitted (live memories).
    Maps:
        new_memory  -> fact_text
        actor_id    -> identifier  (falls back to "mem0" when NULL/empty)
        created_at  -> created_at
        source      -> "mem0"
        id          -> id  (mem0 history row id, reused as HotMem memory id)

    on_progress, if given, is invoked once per yielded row with the 1-based
    row count, enabling byte/row-based progress reporting without coupling
    this module to any UI library.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"mem0 source DB not found: {path}")

    conn = sqlite3.connect(str(path))
    try:
        _validate_mem0_schema(conn)
        cursor = conn.execute(_SELECT_SQL)
        count = 0
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            new_memory, actor_id, created_at, row_id = row
            identifier = actor_id or "mem0"
            count += 1
            yield {
                "id": row_id,
                "identifier": identifier,
                "fact_text": new_memory,
                "source": "mem0",
                "created_at": created_at,
            }
            if on_progress is not None:
                on_progress(count)
    finally:
        conn.close()

    _trace.info(
        "importers.mem0",
        f"read {count} live memories from mem0 history DB",
        detail={"path": str(path), "count": count},
    )
