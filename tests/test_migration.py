"""Tests for hotmem.db — v1 -> v2 schema migration (additive).

Verifies that a v1 fixture DB (fact_text NOT NULL, no v2 columns) is
migrated to include fact_summary, provenance_json, and the other v2 columns,
with existing inline rows preserved.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from hotmem.db import MemoryDB

V1_FIXTURE = Path(__file__).parent / "fixtures" / "v1_memories.sqlite"


@pytest.fixture
def v1_db_path(tmp_path: Path) -> Path:
    """Copy the v1 fixture into tmp so the migration runs on a throwaway copy."""
    out = tmp_path / "v1.sqlite"
    shutil.copy(V1_FIXTURE, out)
    return out


def _columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
    conn.close()
    return cols


def test_migration_adds_fact_summary_and_provenance(v1_db_path: Path):
    before = _columns(v1_db_path)
    assert "fact_summary" not in before
    assert "provenance_json" not in before

    db = MemoryDB(v1_db_path)
    db.close()

    after = _columns(v1_db_path)
    assert "fact_summary" in after
    assert "provenance_json" in after
    # Also confirm the v2 columns from main are present.
    for col in ("memory_type", "source_uri", "byte_offset", "byte_length"):
        assert col in after, f"missing column after migration: {col}"


def test_migration_preserves_inline_data(v1_db_path: Path):
    db = MemoryDB(v1_db_path)
    rows = db.all_rows()
    db.close()
    assert len(rows) == 2
    assert any("Invoice total" in (r["fact_text"] or "") for r in rows)
    assert any("net 30" in (r["fact_text"] or "") for r in rows)


def test_migration_is_idempotent(v1_db_path: Path):
    """Opening the DB twice runs migration only once (columns already exist)."""
    db1 = MemoryDB(v1_db_path)
    db1.close()
    # Use a fresh copy to avoid WAL lock contention on the same file.
    import shutil

    copy = v1_db_path.parent / "v1_copy.sqlite"
    shutil.copy(v1_db_path, copy)
    db2 = MemoryDB(copy)
    rows_after_second = db2.all_rows()
    db2.close()
    assert len(rows_after_second) == 2


def test_inline_insert_works_on_migrated_db(v1_db_path: Path):
    """After migration, the inline insert path still works."""
    from hotmem.embed import embed_text, pack_embedding

    db = MemoryDB(v1_db_path)
    blob = pack_embedding(embed_text("a new inline fact"))
    db.insert(id="new1", identifier="z", fact_text="a new inline fact", embedding=blob)
    got = db.get_memory("new1")
    db.close()

    assert got is not None
    assert got["memory_type"] == "fact"  # main's default
    assert got["fact_text"] == "a new inline fact"


def test_file_backed_insert_works_on_migrated_db(v1_db_path: Path):
    """After migration, file-backed inserts work alongside inline rows."""
    db = MemoryDB(v1_db_path)
    db.insert_file_backed(
        id="fb1",
        identifier="dataset",
        source_uri="file:///tmp/x.bin",
        byte_offset=0,
        byte_length=100,
        source_format="bin",
        source_checksum="abc",
        fact_summary="a binary slice",
    )
    got = db.get_memory("fb1")
    db.close()

    assert got is not None
    assert got["memory_type"] == "file"
    assert got["fact_summary"] == "a binary slice"
    assert got["source_uri"] == "file:///tmp/x.bin"
    assert got["byte_offset"] == 0
    assert got["byte_length"] == 100
    assert got["source_checksum"] == "abc"


def test_migration_adds_events_table(v1_db_path: Path):
    """#41: an existing v1 DB is migrated to include the append-only events table."""
    db = MemoryDB(v1_db_path)
    try:
        tables = {
            row[0] for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "events" in tables
        # The event log starts empty; no backfill of historical rows.
        rows = db._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert rows == 0
        # user_version bumped to 3.
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        db.close()
