"""Tests for #41 — append-only event log and /v1/events.

Covers:
    - Append + query basic ordering
    - Filters (memory_id, namespace, event_type, time range)
    - Deterministic ordering across interleaved appends
    - Cursor pagination (after_seq / before_seq, asc/desc)
    - Regression: existing /v1/add and /v1/search response shapes unchanged
    - Migration: events table is added to an existing v1/v2 SQLite DB
    - Event reads never trigger file hydration (SpyAdapter)
    - File-backed and bundle imports emit the right events
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.events import EventType, append_event, query_events
from hotmem.storage.local import LocalFilesystemAdapter

# ── Unit-level append + query ─────────────────────────────────────────────


def test_append_and_query_basic(tmp_db: MemoryDB):
    e1 = append_event(
        tmp_db,
        event_type=EventType.MEMORY_CREATED,
        memory_id="m1",
        payload={"memory_type": "fact", "identifier": "u1", "content_hash": "h1"},
        occurred_at="2026-07-13T00:00:00Z",
    )
    e2 = append_event(
        tmp_db,
        event_type=EventType.MEMORY_PROMOTION,
        memory_id="m1",
        payload={"from": "HOT", "to": "READY"},
        occurred_at="2026-07-13T00:00:01Z",
    )

    listing = query_events(tmp_db)
    assert listing["count"] == 2
    seqs = [e["seq"] for e in listing["events"]]
    assert seqs == [e1["seq"], e2["seq"]]
    assert seqs[0] < seqs[1], "seq must be monotonic"
    # Stable event identifiers are present and unique.
    ids = {e["event_id"] for e in listing["events"]}
    assert len(ids) == 2
    assert all(len(i) == 32 for i in ids)
    # Payloads round-trip as dicts.
    assert listing["events"][0]["payload"]["identifier"] == "u1"
    assert listing["events"][1]["payload"]["from"] == "HOT"


def test_filter_by_memory_id(tmp_db: MemoryDB):
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id="m1")
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id="m2")
    append_event(tmp_db, event_type=EventType.MEMORY_PROMOTION, memory_id="m1")

    listing = query_events(tmp_db, memory_id="m1")
    assert listing["count"] == 2
    assert all(e["memory_id"] == "m1" for e in listing["events"])


def test_filter_by_namespace(tmp_db: MemoryDB):
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, namespace="contracts")
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, namespace="billing")
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, namespace="contracts")

    listing = query_events(tmp_db, namespace="contracts")
    assert listing["count"] == 2
    assert all(e["namespace"] == "contracts" for e in listing["events"])


def test_filter_by_event_type(tmp_db: MemoryDB):
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id="m1")
    append_event(tmp_db, event_type=EventType.MEMORY_PROMOTION, memory_id="m1")
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id="m2")

    listing = query_events(tmp_db, event_type=EventType.MEMORY_PROMOTION)
    assert listing["count"] == 1
    assert listing["events"][0]["event_type"] == EventType.MEMORY_PROMOTION


def test_filter_by_time_range(tmp_db: MemoryDB):
    append_event(
        tmp_db,
        event_type=EventType.MEMORY_CREATED,
        occurred_at="2026-07-13T00:00:00Z",
    )
    append_event(
        tmp_db,
        event_type=EventType.MEMORY_CREATED,
        occurred_at="2026-07-13T12:00:00Z",
    )
    append_event(
        tmp_db,
        event_type=EventType.MEMORY_CREATED,
        occurred_at="2026-07-14T00:00:00Z",
    )

    listing = query_events(
        tmp_db,
        since="2026-07-13T06:00:00Z",
        until="2026-07-13T23:59:59Z",
    )
    assert listing["count"] == 1
    assert listing["events"][0]["occurred_at"] == "2026-07-13T12:00:00Z"


# ── Pagination + deterministic ordering ──────────────────────────────────


def test_pagination_after_seq_forward(tmp_db: MemoryDB):
    seqs = []
    for i in range(5):
        e = append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id=f"m{i}")
        seqs.append(e["seq"])

    page1 = query_events(tmp_db, limit=2)
    assert page1["count"] == 2
    assert [e["seq"] for e in page1["events"]] == seqs[:2]
    assert page1["next_seq"] == seqs[1]

    page2 = query_events(tmp_db, limit=2, after_seq=page1["next_seq"])
    assert [e["seq"] for e in page2["events"]] == seqs[2:4]
    assert page2["next_seq"] == seqs[3]

    page3 = query_events(tmp_db, limit=2, after_seq=page2["next_seq"])
    assert [e["seq"] for e in page3["events"]] == [seqs[4]]
    assert page3["next_seq"] == seqs[4]

    # Empty page beyond the end returns next_seq of the last element seen
    # would be seqs[4]; here we ask after it, so result is empty + null.
    page4 = query_events(tmp_db, limit=2, after_seq=page3["next_seq"])
    assert page4["count"] == 0
    assert page4["next_seq"] is None


def test_pagination_desc_with_before_seq(tmp_db: MemoryDB):
    seqs = []
    for i in range(4):
        e = append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id=f"m{i}")
        seqs.append(e["seq"])

    page1 = query_events(tmp_db, limit=2, ascending=False)
    assert [e["seq"] for e in page1["events"]] == [seqs[3], seqs[2]]
    # Cursor is the last item in display order (smallest seq in this window).
    assert page1["next_seq"] == seqs[2]

    page2 = query_events(tmp_db, limit=2, ascending=False, before_seq=page1["next_seq"])
    assert [e["seq"] for e in page2["events"]] == [seqs[1], seqs[0]]
    assert page2["next_seq"] == seqs[0]


def test_deterministic_order_across_interleaved_inserts(tmp_db: MemoryDB):
    """Two logical clients appending sequentially must observe monotonic seqs.

    SQLite AUTOINCREMENT guarantees strictly increasing seq across commits, so
    interleaved appends from any number of MemoryDB connections on the same DB
    produce a total order. We simulate this with sequential calls on the same
    connection (the sidecar's single-writer model).
    """
    seqs = []
    for i in range(10):
        e = append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id=f"m{i}")
        seqs.append(e["seq"])
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 10


# ── Regression: existing /v1/* response shapes unchanged ─────────────────


@pytest.fixture
def client(tmp_path: Path):
    from hotmem.server import create_app

    db_path = tmp_path / "test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


def test_regression_add_response_unchanged(client: TestClient):
    resp = client.post(
        "/v1/add",
        json={"identifier": "vendor_x", "fact": "Invoice total was $5000"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"memory_id", "content_hash", "trace_ms"}


def test_regression_search_response_unchanged(client: TestClient):
    client.post("/v1/add", json={"identifier": "a", "fact": "duplicate invoice risk for vendor x"})
    resp = client.post("/v1/search", json={"query": "duplicate invoice", "top_k": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"memories", "count", "trace_ms"}


def test_regression_hydrate_response_unchanged(client: TestClient, tmp_path: Path):
    # Snapshot then hydrate; response keys must remain {loaded, skipped_dupes, path}.
    swap = tmp_path / "swap.jsonl"
    snap = client.post("/v1/snapshot", json={"path": str(swap)})
    assert snap.status_code == 200
    assert set(snap.json().keys()) == {"exported", "path"}

    hyd = client.post("/v1/hydrate", json={"path": str(swap)})
    assert hyd.status_code == 200
    assert set(hyd.json().keys()) == {"loaded", "skipped_dupes", "path"}


def test_regression_discover_response_unchanged(client: TestClient, tmp_path: Path):
    root = tmp_path / "bundles"
    root.mkdir()
    resp = client.post("/v1/discover", json={"root": str(root)})
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"discovered", "indexed", "warnings", "trace_ms"}


def test_regression_hygiene_response_unchanged(client: TestClient):
    resp = client.get("/v1/hygiene")
    assert resp.status_code == 200
    data = resp.json()
    assert "warnings" in data and "stats" in data
    assert set(data.keys()) >= {"warnings", "stats", "warning_count"}


# ── Migration: existing SQLite DB gets the events table ──────────────────


def test_migration_adds_events_table(tmp_path: Path):
    """An existing v2 DB (no events table) is migrated to include events."""
    db_path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(db_path)
    # Minimal v2 schema with the columns the _SCHEMA indexes/triggers reference.
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT,
            embedding BLOB,
            content_hash TEXT DEFAULT '',
            created_at TEXT
        )"""
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        tables = {
            row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "events" in tables
        # Queries against the empty log succeed.
        listing = query_events(db)
        assert listing["count"] == 0
        assert listing["next_seq"] is None
        # Existing memories count is unaffected.
        assert db.count() == 0
        # user_version bumped to 3.
        version = db._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3
    finally:
        db.close()


def test_migration_is_idempotent_for_events(tmp_path: Path):
    db_path = tmp_path / "v3.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    # Opening again must not error and must keep user_version at 3.
    db = MemoryDB(db_path)
    try:
        version = db._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3
    finally:
        db.close()


# ── Zero file reads during event reads (SpyAdapter) ───────────────────────


def test_event_query_zero_file_reads(tmp_db: MemoryDB, fixture_file: Path):
    """Querying events for a file-backed memory must not read backing bytes."""
    from spy import SpyAdapter

    spy = SpyAdapter(LocalFilesystemAdapter())
    import hotmem.memory as mem_mod

    orig = mem_mod.get_adapter
    mem_mod.get_adapter = lambda uri: spy
    try:
        from hotmem.memory import FileRef, add_file_backed

        ref = FileRef(
            source_uri=str(fixture_file),
            byte_offset=0,
            byte_length=20,
            source_format="bin",
        )
        mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="s")
        # add_file_backed stats the file (exists check) but does NOT read it.
        reads_after_add = spy.total_file_reads
        assert reads_after_add == 0, "add_file_backed must not read backing bytes"

        append_event(
            tmp_db,
            event_type=EventType.MEMORY_CREATED,
            memory_id=mid,
            payload={"memory_type": "file", "source_uri": str(fixture_file)},
        )
        listing = query_events(tmp_db, memory_id=mid)
        assert listing["count"] == 1
        # Event query must not have added any file reads.
        assert spy.total_file_reads == 0, "event query must not trigger file hydration"
    finally:
        mem_mod.get_adapter = orig


# ── HTTP /v1/events endpoint ──────────────────────────────────────────────


@pytest.fixture
def client_with_events(tmp_path: Path):
    from hotmem.server import create_app

    db_path = tmp_path / "test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        c.post("/v1/add", json={"identifier": "u1", "fact": "first fact"})
        c.post("/v1/add", json={"identifier": "u2", "fact": "second fact"})
        yield c


def test_events_endpoint_lists_created(client_with_events: TestClient):
    resp = client_with_events.get("/v1/events")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"events", "count", "next_seq", "trace_ms"}
    # Two add calls each emit memory.created; hygiene was not invoked here.
    types = [e["event_type"] for e in body["events"]]
    assert types == [EventType.MEMORY_CREATED, EventType.MEMORY_CREATED]
    assert body["count"] == 2
    assert body["next_seq"] is not None


def test_events_endpoint_filter_by_event_type(client_with_events: TestClient):
    resp = client_with_events.get("/v1/events", params={"event_type": EventType.MEMORY_CREATED})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2

    resp = client_with_events.get("/v1/events", params={"event_type": EventType.MEMORY_PROMOTION})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_events_endpoint_pagination(client_with_events: TestClient):
    first = client_with_events.get("/v1/events", params={"limit": 1}).json()
    assert first["count"] == 1
    next_seq = first["next_seq"]
    second = client_with_events.get("/v1/events", params={"limit": 1, "after_seq": next_seq}).json()
    assert second["count"] == 1
    assert second["events"][0]["seq"] > first["events"][0]["seq"]


def test_events_endpoint_rejects_bad_order(client_with_events: TestClient):
    resp = client_with_events.get("/v1/events", params={"order": "sideways"})
    assert resp.status_code == 400


def test_events_endpoint_rejects_bad_limit(client_with_events: TestClient):
    resp = client_with_events.get("/v1/events", params={"limit": 0})
    assert resp.status_code == 400
    resp = client_with_events.get("/v1/events", params={"limit": 5000})
    assert resp.status_code == 400


def test_file_backed_add_emits_created_event(tmp_path: Path, fixture_file: Path):
    """The file-backed add path emits memory.created with memory_type=file."""
    from hotmem.server import create_app

    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / fixture_file.name
    target.write_bytes(fixture_file.read_bytes())
    app = create_app(db_path=mount / "hotmem.sqlite", base_dir=mount)
    with TestClient(app) as c:
        resp = c.post(
            "/v1/add",
            json={
                "identifier": "ds",
                "file_uri": fixture_file.name,
                "byte_offset": 0,
                "byte_length": 20,
                "source_format": "bin",
                "summary": "slice",
            },
        )
        assert resp.status_code == 200
        mid = resp.json()["memory_id"]

        events = c.get("/v1/events", params={"memory_id": mid}).json()
        assert events["count"] == 1
        ev = events["events"][0]
        assert ev["event_type"] == EventType.MEMORY_CREATED
        assert ev["payload"]["memory_type"] == "file"
        assert ev["payload"]["source_uri"] == fixture_file.name
        assert ev["payload"]["byte_length"] == 20


def test_snapshot_hydrate_emits_imported_event(client_with_events: TestClient, tmp_path: Path):
    swap = tmp_path / "swap.jsonl"
    snap = client_with_events.post("/v1/snapshot", json={"path": str(swap)})
    assert snap.status_code == 200
    # Hydrate into the same DB; dupes skipped, but an import event is recorded.
    hyd = client_with_events.post("/v1/hydrate", json={"path": str(swap)})
    assert hyd.status_code == 200

    events = client_with_events.get(
        "/v1/events", params={"event_type": EventType.SNAPSHOT_IMPORTED}
    ).json()
    assert events["count"] == 1
    ev = events["events"][0]
    assert ev["payload"]["path"] == str(swap)
    assert ev["payload"]["format"] == "legacy"


def test_hygiene_emits_summary_and_warning_events(tmp_path: Path, fixture_file: Path):
    """Hygiene emits one hygiene.checked summary plus one hygiene.warning per error."""
    from hotmem.server import create_app

    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / fixture_file.name
    target.write_bytes(fixture_file.read_bytes())
    app = create_app(db_path=mount / "hotmem.sqlite", base_dir=mount)
    with TestClient(app) as c:
        # Add a file-backed memory then delete its backing file -> error.
        resp = c.post(
            "/v1/add",
            json={
                "identifier": "ds",
                "file_uri": fixture_file.name,
                "byte_offset": 0,
                "byte_length": 20,
                "source_format": "bin",
            },
        )
        mid = resp.json()["memory_id"]
        target.unlink()

        c.get("/v1/hygiene")

        summary = c.get("/v1/events", params={"event_type": EventType.HYGIENE_CHECKED}).json()
        assert summary["count"] == 1
        assert summary["events"][0]["payload"]["error_count"] >= 1

        warnings = c.get("/v1/events", params={"event_type": EventType.HYGIENE_WARNING}).json()
        assert warnings["count"] >= 1
        warn = warnings["events"][0]
        assert warn["payload"]["category"] == "missing_backing_file"
        assert warn["memory_id"] == mid


# ── Retention helpers (unexposed) ─────────────────────────────────────────


def test_trim_events_before_seq(tmp_db: MemoryDB):
    for i in range(5):
        append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id=f"m{i}")
    deleted = tmp_db.trim_events_before_seq(3)
    assert deleted == 2
    remaining = query_events(tmp_db)
    assert remaining["count"] == 3
    assert all(e["seq"] >= 3 for e in remaining["events"])


def test_trim_events_by_count(tmp_db: MemoryDB):
    for i in range(5):
        append_event(tmp_db, event_type=EventType.MEMORY_CREATED, memory_id=f"m{i}")
    deleted = tmp_db.trim_events_by_count(keep=2)
    assert deleted == 3
    remaining = query_events(tmp_db)
    assert remaining["count"] == 2
    # Kept the most recent two.
    seqs = [e["seq"] for e in remaining["events"]]
    assert max(seqs) == 5


def test_trim_events_by_count_zero_clears(tmp_db: MemoryDB):
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED)
    append_event(tmp_db, event_type=EventType.MEMORY_CREATED)
    deleted = tmp_db.trim_events_by_count(keep=0)
    assert deleted == 2
    assert query_events(tmp_db)["count"] == 0


def test_trim_events_by_count_negative_rejected(tmp_db: MemoryDB):
    with pytest.raises(ValueError):
        tmp_db.trim_events_by_count(keep=-1)
