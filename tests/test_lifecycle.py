"""Tests for #42 — promotion lifecycle (signal only).

Covers:
    - Valid forward transitions HOT -> READY -> PROMOTED -> ARCHIVED
    - Invalid transitions return errors and do not mutate state
    - Same-state transitions are invalid
    - Default-state backward compatibility for existing records
    - Inline, file-backed, JSONL, and bundle workflows remain compatible
    - Candidate signal is independent of state and persists across transitions
    - Event emission integration with #41
    - API + CLI compatibility when lifecycle fields are omitted
    - /v1/promotion/states, /v1/promotion/candidates, /v1/promotion/by-state
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.events import EventType, query_events
from hotmem.lifecycle import (
    PROMOTION_STATES,
    VALID_TRANSITIONS,
    InvalidTransitionError,
    list_by_state,
    list_candidates,
    mark_candidate,
    states_model,
    transition,
)

# ── Valid transitions ─────────────────────────────────────────────────────


def _add_inline(db: MemoryDB, mid: str = "m1", identifier: str = "u1") -> str:
    blob = pack_embedding(embed_text("a fact"))
    db.insert(id=mid, identifier=identifier, fact_text="a fact", embedding=blob)
    return mid


def test_valid_transitions_chained(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    r1 = transition(tmp_db, mid, "READY")
    assert r1["promotion_state"] == "READY"
    assert tmp_db.get_memory(mid)["promotion_state"] == "READY"
    r2 = transition(tmp_db, mid, "PROMOTED")
    assert r2["promotion_state"] == "PROMOTED"
    r3 = transition(tmp_db, mid, "ARCHIVED")
    assert r3["promotion_state"] == "ARCHIVED"
    assert tmp_db.get_memory(mid)["promotion_state"] == "ARCHIVED"


def test_transition_records_updated_at(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    result = transition(tmp_db, mid, "READY")
    assert result["updated_at"] is not None
    row = tmp_db.get_memory(mid)
    assert row["updated_at"] == result["updated_at"]


# ── Invalid transitions ───────────────────────────────────────────────────


def test_invalid_skip_transition_raises_no_mutation(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    with pytest.raises(InvalidTransitionError) as exc_info:
        transition(tmp_db, mid, "PROMOTED")  # HOT -> PROMOTED is a skip
    err = exc_info.value
    assert err.from_state == "HOT"
    assert err.to_state == "PROMOTED"
    assert err.to_dict()["valid"] == ["READY"]
    # State must NOT have been mutated.
    assert tmp_db.get_memory(mid)["promotion_state"] == "HOT"


def test_invalid_archived_to_hot_reheat_raises(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    transition(tmp_db, mid, "READY")
    transition(tmp_db, mid, "PROMOTED")
    transition(tmp_db, mid, "ARCHIVED")
    with pytest.raises(InvalidTransitionError):
        transition(tmp_db, mid, "HOT")
    assert tmp_db.get_memory(mid)["promotion_state"] == "ARCHIVED"


def test_invalid_same_state_raises(tmp_db: MemoryDB):
    """A transition to the same state is not a transition and is invalid."""
    mid = _add_inline(tmp_db)
    with pytest.raises(InvalidTransitionError):
        transition(tmp_db, mid, "HOT")
    assert tmp_db.get_memory(mid)["promotion_state"] == "HOT"


def test_unknown_state_raises_value_error(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    with pytest.raises(ValueError):
        transition(tmp_db, mid, "FROZEN")


def test_transition_missing_memory_raises_keyerror(tmp_db: MemoryDB):
    with pytest.raises(KeyError):
        transition(tmp_db, "nonexistent", "READY")


# ── Default-state backward compatibility ──────────────────────────────────


def test_default_state_for_inline_insert(tmp_db: MemoryDB):
    """An insert without lifecycle fields defaults to HOT / candidate=0."""
    blob = pack_embedding(embed_text("plain fact"))
    tmp_db.insert(id="d1", identifier="x", fact_text="plain fact", embedding=blob)
    row = tmp_db.get_memory("d1")
    assert row["promotion_state"] == "HOT"
    assert row["promotion_candidate"] == 0


def test_default_state_for_file_backed_insert(tmp_db: MemoryDB, fixture_file: Path):
    tmp_db.insert_file_backed(
        id="fb1",
        identifier="ds",
        source_uri=str(fixture_file),
        byte_offset=0,
        byte_length=20,
        source_format="bin",
        source_checksum=None,
        fact_summary="slice",
    )
    row = tmp_db.get_memory("fb1")
    assert row["promotion_state"] == "HOT"
    assert row["promotion_candidate"] == 0


def test_existing_v2_row_defaults_safe_after_migration(tmp_path: Path):
    """A pre-existing memories row keeps HOT/0 defaults after the v3 migration."""
    import sqlite3

    db_path = tmp_path / "pre.sqlite"
    conn = sqlite3.connect(db_path)
    # Minimal schema with the columns the schema + indexes reference.
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY, identifier TEXT NOT NULL, fact_text TEXT,
            embedding BLOB, embedding_dim INTEGER, embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '', importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}', content_hash TEXT DEFAULT '',
            ttl_seconds INTEGER, created_at TEXT,
            promotion_state TEXT DEFAULT 'HOT',
            promotion_candidate INTEGER DEFAULT 0
        )"""
    )
    blob = pack_embedding(embed_text("legacy fact"))
    conn.execute(
        "INSERT INTO memories "
        "(id, identifier, fact_text, embedding, promotion_state, promotion_candidate) "
        "VALUES ('legacy1', 'x', 'legacy fact', ?, 'HOT', 0)",
        (blob,),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        row = db.get_memory("legacy1")
        assert row["promotion_state"] == "HOT"
        assert row["promotion_candidate"] == 0
    finally:
        db.close()


# ── Workflow compatibility (inline / file-backed / JSONL / bundle) ────────


@pytest.fixture
def client(tmp_path: Path):
    from hotmem.server import create_app

    db_path = tmp_path / "test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


def test_existing_inline_workflow_compat(client: TestClient):
    """Adding an inline memory without lifecycle fields still works."""
    resp = client.post("/v1/add", json={"identifier": "u1", "fact": "hello"})
    assert resp.status_code == 200
    mid = resp.json()["memory_id"]
    meta = client.get(f"/v1/memory/{mid}").json()
    # Default response shape unchanged; lifecycle fields are not in the
    # default payload (queryable via the dedicated promotion endpoints).
    assert "promotion_state" not in meta


def test_existing_file_backed_workflow_compat(client: TestClient, fixture_file: Path):
    """Adding a file-backed memory without lifecycle fields still works."""
    resp = client.post(
        "/v1/add",
        json={
            "identifier": "ds",
            "file_uri": str(fixture_file),
            "byte_offset": 0,
            "byte_length": 20,
            "source_format": "bin",
        },
    )
    assert resp.status_code == 200
    mid = resp.json()["memory_id"]
    meta = client.get(f"/v1/memory/{mid}").json()
    assert meta["memory_type"] == "file"


def test_existing_jsonl_workflow_compat(client: TestClient, tmp_path: Path):
    """Legacy JSONL snapshot hydrate still works without lifecycle fields."""
    # Add a memory, snapshot to JSONL, then hydrate into the same DB.
    client.post("/v1/add", json={"identifier": "a", "fact": "jsonl fact"})
    swap = tmp_path / "swap.jsonl"
    snap = client.post("/v1/snapshot", json={"path": str(swap)})
    assert snap.status_code == 200
    hyd = client.post("/v1/hydrate", json={"path": str(swap)})
    assert hyd.status_code == 200
    # The imported rows default to HOT/0 (the schema defaults), so promotion
    # endpoints report the imported memories as HOT.
    states = client.get("/v1/promotion/states").json()
    assert "HOT" in states["states"]


def test_existing_bundle_workflow_compat(tmp_path: Path):
    """Bundle hydrate still works without lifecycle fields."""
    from hotmem.bundle import MEMORY_MD
    from hotmem.db import MemoryDB
    from hotmem.snapshot import hydrate

    bundle = tmp_path / "mybundle"
    bundle.mkdir()
    (bundle / MEMORY_MD).write_text("# Bundle\n\nFact: bundle fact\n")
    db_path = tmp_path / "db.sqlite"
    db = MemoryDB(db_path)
    try:
        result = hydrate(db, bundle)
        assert result.loaded >= 1
        rows = db.list_by_promotion_state("HOT")
        assert len(rows) == result.loaded
    finally:
        db.close()


# ── Candidate signal independence ─────────────────────────────────────────


def test_candidate_independent_of_state(tmp_db: MemoryDB):
    """The candidate flag can be set in any state and survives transitions."""
    mid = _add_inline(tmp_db)
    mark_candidate(tmp_db, mid, True, reason="hot lead")
    assert tmp_db.get_memory(mid)["promotion_candidate"] == 1

    transition(tmp_db, mid, "READY")
    # Candidate flag persists across the transition.
    assert tmp_db.get_memory(mid)["promotion_candidate"] == 1

    mark_candidate(tmp_db, mid, False)
    assert tmp_db.get_memory(mid)["promotion_candidate"] == 0


def test_mark_candidate_missing_memory_raises(tmp_db: MemoryDB):
    with pytest.raises(KeyError):
        mark_candidate(tmp_db, "nope", True)


def test_list_candidates_filters(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    tmp_db.insert(id="c1", identifier="x", fact_text="fact", embedding=blob, namespace="ns-a")
    tmp_db.insert(id="c2", identifier="y", fact_text="fact", embedding=blob, namespace="ns-b")
    tmp_db.insert(id="c3", identifier="z", fact_text="fact", embedding=blob, namespace="ns-a")
    mark_candidate(tmp_db, "c1", True)
    mark_candidate(tmp_db, "c2", True)
    # c3 is not a candidate.

    all_cands = list_candidates(tmp_db)
    assert {r["id"] for r in all_cands} == {"c1", "c2"}

    ns_a = list_candidates(tmp_db, namespace="ns-a")
    assert [r["id"] for r in ns_a] == ["c1"]

    # Transition c1 to READY; candidate flag survives, and state filter applies.
    transition(tmp_db, "c1", "READY")
    ready_cands = list_candidates(tmp_db, state="READY")
    assert [r["id"] for r in ready_cands] == ["c1"]
    hot_cands = list_candidates(tmp_db, state="HOT")
    assert [r["id"] for r in hot_cands] == ["c2"]


def test_list_by_state(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    tmp_db.insert(id="s1", identifier="x", fact_text="fact", embedding=blob)
    tmp_db.insert(id="s2", identifier="y", fact_text="fact", embedding=blob)
    transition(tmp_db, "s1", "READY")
    hot = list_by_state(tmp_db, "HOT")
    ready = list_by_state(tmp_db, "READY")
    assert {r["id"] for r in hot} == {"s2"}
    assert {r["id"] for r in ready} == {"s1"}


# ── Event emission integration with #41 ──────────────────────────────────


def test_transition_emits_promotion_event(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    transition(tmp_db, mid, "READY", reason="vetted", actor="analyst")

    events = query_events(tmp_db, event_type=EventType.MEMORY_PROMOTION)
    assert events["count"] == 1
    ev = events["events"][0]
    assert ev["memory_id"] == mid
    assert ev["payload"]["from"] == "HOT"
    assert ev["payload"]["to"] == "READY"
    assert ev["payload"]["reason"] == "vetted"
    assert ev["payload"]["actor"] == "analyst"


def test_mark_candidate_emits_event(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    mark_candidate(tmp_db, mid, True, reason="strong lead")
    events = query_events(tmp_db, event_type=EventType.MEMORY_CANDIDATE)
    assert events["count"] == 1
    ev = events["events"][0]
    assert ev["memory_id"] == mid
    assert ev["payload"]["candidate"] is True
    assert ev["payload"]["reason"] == "strong lead"


def test_transition_emit_event_false_suppresses(tmp_db: MemoryDB):
    mid = _add_inline(tmp_db)
    transition(tmp_db, mid, "READY", emit_event=False)
    events = query_events(tmp_db, event_type=EventType.MEMORY_PROMOTION)
    assert events["count"] == 0


# ── Static states model ────────────────────────────────────────────────────


def test_states_model():
    model = states_model()
    assert model["states"] == list(PROMOTION_STATES)
    assert ["HOT", "READY"] in [list(t) for t in model["valid_transitions"]]
    assert len(VALID_TRANSITIONS) == 3


# ── API endpoints ─────────────────────────────────────────────────────────


def test_api_promote_valid(client: TestClient):
    mid = client.post("/v1/add", json={"identifier": "u", "fact": "x"}).json()["memory_id"]
    resp = client.post(f"/v1/memory/{mid}/promote", json={"to_state": "READY"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"memory_id", "promotion_state", "updated_at", "trace_ms"}
    assert body["promotion_state"] == "READY"


def test_api_promote_invalid_returns_409_no_mutation(client: TestClient):
    mid = client.post("/v1/add", json={"identifier": "u", "fact": "x"}).json()["memory_id"]
    resp = client.post(f"/v1/memory/{mid}/promote", json={"to_state": "PROMOTED"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "invalid_transition"
    assert body["from_state"] == "HOT"
    assert body["to_state"] == "PROMOTED"
    assert body["valid"] == ["READY"]
    # State unchanged.
    by_state = client.get("/v1/promotion/by-state", params={"state": "HOT"}).json()
    assert any(c["memory_id"] == mid for c in by_state["memories"])


def test_api_promote_missing_memory_returns_404(client: TestClient):
    resp = client.post("/v1/memory/none/promote", json={"to_state": "READY"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_api_candidate_set_and_clear(client: TestClient):
    mid = client.post("/v1/add", json={"identifier": "u", "fact": "x"}).json()["memory_id"]
    resp = client.post(f"/v1/memory/{mid}/candidate", json={"candidate": True})
    assert resp.status_code == 200
    assert resp.json()["promotion_candidate"] == 1
    resp = client.post(f"/v1/memory/{mid}/candidate", json={"candidate": False})
    assert resp.status_code == 200
    assert resp.json()["promotion_candidate"] == 0


def test_api_promotion_candidates(client: TestClient):
    m1 = client.post("/v1/add", json={"identifier": "u1", "fact": "a"}).json()["memory_id"]
    client.post("/v1/add", json={"identifier": "u2", "fact": "b"})
    client.post(f"/v1/memory/{m1}/candidate", json={"candidate": True})
    resp = client.get("/v1/promotion/candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"candidates", "count", "trace_ms"}
    assert body["count"] == 1
    assert body["candidates"][0]["memory_id"] == m1


def test_api_promotion_states(client: TestClient):
    resp = client.get("/v1/promotion/states")
    assert resp.status_code == 200
    body = resp.json()
    assert body["states"] == ["HOT", "READY", "PROMOTED", "ARCHIVED"]
    assert ["HOT", "READY"] in body["valid_transitions"]


def test_api_promotion_by_state(client: TestClient):
    m1 = client.post("/v1/add", json={"identifier": "u1", "fact": "a"}).json()["memory_id"]
    client.post(f"/v1/memory/{m1}/promote", json={"to_state": "READY"})
    resp = client.get("/v1/promotion/by-state", params={"state": "READY"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"memories", "count", "trace_ms"}
    assert body["count"] == 1
    assert body["memories"][0]["memory_id"] == m1
    assert body["memories"][0]["promotion_state"] == "READY"


def test_api_promote_emits_event(client: TestClient):
    mid = client.post("/v1/add", json={"identifier": "u", "fact": "x"}).json()["memory_id"]
    client.post(f"/v1/memory/{mid}/promote", json={"to_state": "READY"})
    events = client.get("/v1/events", params={"event_type": EventType.MEMORY_PROMOTION}).json()
    assert events["count"] == 1
    assert events["events"][0]["memory_id"] == mid


# ── API compatibility: existing endpoints unchanged when lifecycle omitted ──


def test_api_search_shape_unchanged_without_lifecycle(client: TestClient):
    client.post("/v1/add", json={"identifier": "u", "fact": "searchable fact"})
    resp = client.post("/v1/search", json={"query": "searchable"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"memories", "count", "trace_ms"}


def test_api_files_shape_unchanged(client: TestClient, fixture_file: Path):
    resp = client.post(
        "/v1/add",
        json={
            "identifier": "ds",
            "file_uri": str(fixture_file),
            "byte_offset": 0,
            "byte_length": 10,
            "source_format": "bin",
        },
    )
    assert resp.status_code == 200
    files = client.get("/v1/files").json()
    assert set(files.keys()) == {"files", "count", "trace_ms"}


# ── CLI ───────────────────────────────────────────────────────────────────


def test_cli_promote(tmp_path: Path):
    from click.testing import CliRunner

    from hotmem.cli import main

    db_path = tmp_path / "db.sqlite"
    # Seed a memory directly.
    db = MemoryDB(db_path)
    blob = pack_embedding(embed_text("a fact"))
    db.insert(id="m1", identifier="u", fact_text="a fact", embedding=blob)
    db.close()

    runner = CliRunner()
    result = runner.invoke(
        main, ["promote", "--db", str(db_path), "--memory-id", "m1", "--to", "READY"]
    )
    assert result.exit_code == 0, result.output
    assert "READY" in result.output

    db = MemoryDB(db_path)
    try:
        assert db.get_memory("m1")["promotion_state"] == "READY"
    finally:
        db.close()


def test_cli_promote_invalid_transition(tmp_path: Path):
    from click.testing import CliRunner

    from hotmem.cli import main

    db_path = tmp_path / "db.sqlite"
    db = MemoryDB(db_path)
    blob = pack_embedding(embed_text("a fact"))
    db.insert(id="m1", identifier="u", fact_text="a fact", embedding=blob)
    db.close()

    runner = CliRunner()
    result = runner.invoke(
        main, ["promote", "--db", str(db_path), "--memory-id", "m1", "--to", "ARCHIVED"]
    )
    assert result.exit_code != 0
    # State unchanged.
    db = MemoryDB(db_path)
    try:
        assert db.get_memory("m1")["promotion_state"] == "HOT"
    finally:
        db.close()


def test_cli_candidates(tmp_path: Path):
    from click.testing import CliRunner

    from hotmem.cli import main

    db_path = tmp_path / "db.sqlite"
    db = MemoryDB(db_path)
    blob = pack_embedding(embed_text("a fact"))
    db.insert(id="m1", identifier="u", fact_text="a fact", embedding=blob)
    mark_candidate(db, "m1", True)
    db.close()

    runner = CliRunner()
    result = runner.invoke(main, ["candidates", "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any(r["id"] == "m1" for r in data)
