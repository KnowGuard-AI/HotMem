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

    mem0's history is an audit log: ADD creates a memory, UPDATE records a
    new current text (the original ADD row is left untouched), DELETE marks
    the memory gone. To import the *current* state we replay events per
    memory_id in created_at order and emit one record per live memory_id.

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

# Replay history per memory_id in deterministic order. created_at may tie, so
# the row id is the tiebreaker. Ordering by memory_id first lets us stream
# the replay one memory at a time rather than loading the whole table.
_REPLAY_SQL = """
    SELECT memory_id, new_memory, event, is_deleted, actor_id, created_at, id
    FROM history
    WHERE memory_id IS NOT NULL
    ORDER BY memory_id, created_at, id
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
    """Yield HotMem swap-record dicts representing the *current* state of a mem0 DB.

    Replays the audit log per memory_id in created_at order: ADD/UPDATE set the
    current text, DELETE removes the memory. Emits one record per live
    memory_id with its latest non-empty new_memory. Rows whose latest event is
    a DELETE, or whose latest new_memory is NULL/empty, are skipped.

    Maps (from the latest live row per memory_id):
        new_memory  -> fact_text
        actor_id    -> identifier  (falls back to "mem0" when NULL/empty)
        created_at  -> created_at
        source      -> "mem0"

    HotMem memory ids are intentionally NOT carried from mem0 — hydrate
    auto-generates uuid4 ids and dedups on content_hash, so re-import is
    idempotent and foreign PK collisions cannot silently drop memories.

    on_progress, if given, is invoked once per yielded record with the
    1-based count.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"mem0 source DB not found: {path}")

    conn = sqlite3.connect(str(path))
    count = 0
    try:
        _validate_mem0_schema(conn)
        cursor = conn.execute(_REPLAY_SQL)

        current_memory_id: str | None = None
        current_text: str | None = None
        current_actor: str | None = None
        current_created: str | None = None
        current_live = False

        def _flush() -> Iterator[dict]:
            nonlocal count
            if (
                current_memory_id is not None
                and current_live
                and current_text
                and current_text.strip()
            ):
                count += 1
                yield {
                    "identifier": current_actor or "mem0",
                    "fact_text": current_text,
                    "source": "mem0",
                    "created_at": current_created,
                }
                if on_progress is not None:
                    on_progress(count)

        while True:
            row = cursor.fetchone()
            if row is None:
                yield from _flush()
                break

            memory_id, new_memory, event, is_deleted, actor_id, created_at, _row_id = row

            if current_memory_id is not None and memory_id != current_memory_id:
                yield from _flush()
                current_text = None
                current_actor = None
                current_created = None
                current_live = False

            current_memory_id = memory_id
            # Replay: latest non-DELETE event wins. is_deleted on the row is
            # also honored so a row marked deleted (even with event='ADD')
            # removes the memory.
            if event == "DELETE" or is_deleted:
                current_live = False
                current_text = None
            else:
                # ADD or UPDATE: adopt this row's text/identity/timestamp.
                if new_memory is not None and new_memory.strip():
                    current_text = new_memory
                    current_actor = actor_id
                    current_created = created_at
                current_live = True
    finally:
        conn.close()

    _trace.info(
        "importers.mem0",
        f"replayed mem0 history, emitted {count} live memories",
        detail={"path": str(path), "count": count},
    )
