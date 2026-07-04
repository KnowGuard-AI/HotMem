"""Tests for hotmem.importers.mem0 — read mem0 SQLite history DB.

Builds synthetic mem0-style SQLite DBs with the exact schema from
mem0.SQLiteManager._create_history_table to validate field mapping, event
filtering, streaming, and error handling — without depending on the mem0
package itself.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from hotmem.cli import main
from hotmem.importers import IMPORTERS
from hotmem.importers.mem0 import read_mem0_sqlite

_CREATE_HISTORY = """
CREATE TABLE history (
    id           TEXT PRIMARY KEY,
    memory_id    TEXT,
    old_memory   TEXT,
    new_memory   TEXT,
    event        TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    is_deleted   INTEGER,
    actor_id     TEXT,
    role         TEXT
)
"""


def _make_mem0_db(path: Path, rows: list[tuple]) -> Path:
    """Create a mem0-style history DB with the given rows.

    rows: tuples of (id, memory_id, new_memory, event, is_deleted, actor_id, created_at)
    """
    conn = sqlite3.connect(str(path))
    conn.execute(_CREATE_HISTORY)
    for r in rows:
        rid, memory_id, new_memory, event, is_deleted, actor_id, created_at = r
        conn.execute(
            "INSERT INTO history (id, memory_id, old_memory, new_memory, event, "
            "created_at, updated_at, is_deleted, actor_id, role) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL)",
            (rid, memory_id, new_memory, event, created_at, created_at, is_deleted, actor_id),
        )
    conn.commit()
    conn.close()
    return path


# ── read_mem0_sqlite unit tests ────────────────────────────────────────


def test_registry_has_mem0():
    assert "mem0" in IMPORTERS
    assert IMPORTERS["mem0"] is read_mem0_sqlite


def test_maps_fields_correctly(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "Prefers dark mode", "ADD", 0, "user-42", "2026-01-01T00:00:00Z"),
        ],
    )
    records = list(read_mem0_sqlite(db))
    assert len(records) == 1
    r = records[0]
    assert r["fact_text"] == "Prefers dark mode"
    assert r["identifier"] == "user-42"
    assert r["created_at"] == "2026-01-01T00:00:00Z"
    assert r["source"] == "mem0"
    assert r["id"] == "h1"


def test_skips_non_add_events(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "added fact", "ADD", 0, "u1", "2026-01-01"),
            ("h2", "m1", "updated fact", "UPDATE", 0, "u1", "2026-01-02"),
            ("h3", "m1", None, "DELETE", 1, "u1", "2026-01-03"),
        ],
    )
    records = list(read_mem0_sqlite(db))
    assert len(records) == 1
    assert records[0]["fact_text"] == "added fact"


def test_skips_deleted_rows(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "alive", "ADD", 0, "u1", "2026-01-01"),
            ("h2", "m2", "deleted", "ADD", 1, "u1", "2026-01-02"),
        ],
    )
    records = list(read_mem0_sqlite(db))
    assert len(records) == 1
    assert records[0]["fact_text"] == "alive"


def test_skips_null_or_empty_memory(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "valid", "ADD", 0, "u1", "2026-01-01"),
            ("h2", "m2", None, "ADD", 0, "u1", "2026-01-02"),
            ("h3", "m3", "", "ADD", 0, "u1", "2026-01-03"),
        ],
    )
    records = list(read_mem0_sqlite(db))
    assert len(records) == 1
    assert records[0]["fact_text"] == "valid"


def test_null_actor_falls_back_to_mem0(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "no actor", "ADD", 0, None, "2026-01-01"),
            ("h2", "m2", "empty actor", "ADD", 0, "", "2026-01-02"),
        ],
    )
    records = list(read_mem0_sqlite(db))
    assert all(r["identifier"] == "mem0" for r in records)


def test_returns_iterator_not_list(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [("h1", "m1", "fact", "ADD", 0, "u1", "2026-01-01")],
    )
    gen = read_mem0_sqlite(db)
    assert isinstance(gen, Iterator)
    first = next(gen)
    assert first["fact_text"] == "fact"
    with pytest.raises(StopIteration):
        next(gen)


def test_on_progress_callback_invoked_per_row(tmp_path: Path):
    db = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "fact one", "ADD", 0, "u1", "2026-01-01"),
            ("h2", "m2", "fact two", "ADD", 0, "u1", "2026-01-02"),
            ("h3", "m3", "fact three", "ADD", 0, "u1", "2026-01-03"),
        ],
    )
    seen = []
    list(read_mem0_sqlite(db, on_progress=seen.append))
    assert seen == [1, 2, 3]


def test_rejects_missing_history_table(tmp_path: Path):
    db = tmp_path / "not_mem0.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="no 'history' table"):
        list(read_mem0_sqlite(db))


def test_rejects_wrong_schema(tmp_path: Path):
    db = tmp_path / "wrong_schema.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE history (id TEXT, foo TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="missing expected mem0 columns"):
        list(read_mem0_sqlite(db))


def test_rejects_non_sqlite_file(tmp_path: Path):
    bad = tmp_path / "not_a_db"
    bad.write_text("not sqlite")
    with pytest.raises(ValueError, match="not a SQLite database"):
        list(read_mem0_sqlite(bad))


def test_missing_file_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        list(read_mem0_sqlite(tmp_path / "ghost.db"))


def test_empty_db_yields_nothing(tmp_path: Path):
    db = _make_mem0_db(tmp_path / "empty.db", [])
    records = list(read_mem0_sqlite(db))
    assert records == []


# ── CLI end-to-end ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _force_plain(monkeypatch: pytest.MonkeyPatch):
    import hotmem.ui as ui

    monkeypatch.setattr(ui, "_use_rich", lambda: False)


def test_cli_import_mem0_round_trip(tmp_path: Path):
    source = _make_mem0_db(
        tmp_path / "mem0.db",
        [
            ("h1", "m1", "invoice total 5000", "ADD", 0, "vendor_a", "2026-01-01"),
            ("h2", "m2", "late payment risk", "ADD", 0, "vendor_b", "2026-01-02"),
            ("h3", "m3", "stale note", "ADD", 1, "vendor_c", "2026-01-03"),
        ],
    )
    target = tmp_path / "hotmem.sqlite"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["import", "--from", "mem0", "--db", str(source), "--target", str(target)],
    )
    assert result.exit_code == 0, result.output
    assert "import" in result.output
    assert "imported=2" in result.output
    assert "source=mem0" in result.output
    assert str(target) in result.output

    # Verify the memories landed and are searchable in HotMem.
    from hotmem.db import MemoryDB

    db = MemoryDB(str(target))
    assert db.count() == 2
    db.close()


def test_cli_import_out_retains_swap_file(tmp_path: Path):
    source = _make_mem0_db(
        tmp_path / "mem0.db",
        [("h1", "m1", "keep this", "ADD", 0, "u1", "2026-01-01")],
    )
    out = tmp_path / "export.jsonl"
    target = tmp_path / "t.sqlite"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "import",
            "--from",
            "mem0",
            "--db",
            str(source),
            "--target",
            str(target),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    import json

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["fact_text"] == "keep this"
    assert rec["source"] == "mem0"


def test_cli_import_rejects_missing_source(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["import", "--from", "mem0", "--db", str(tmp_path / "ghost.db")],
    )
    assert result.exit_code != 0


def test_cli_import_rejects_non_mem0_db(tmp_path: Path):
    bad = tmp_path / "bad.db"
    conn = sqlite3.connect(str(bad))
    conn.execute("CREATE TABLE x (id TEXT)")
    conn.commit()
    conn.close()
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["import", "--from", "mem0", "--db", str(bad), "--target", str(tmp_path / "t.sqlite")],
    )
    assert result.exit_code != 0
    assert "history" in result.output or "mem0" in result.output.lower()
