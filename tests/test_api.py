"""Tests for hotmem.server — FastAPI endpoint integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.server import create_app


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["memory_count"] == 0
    assert "X-HotMem-Trace-Id" in resp.headers


def test_add_memory(client: TestClient):
    resp = client.post(
        "/v1/add",
        json={
            "identifier": "vendor_x",
            "fact": "Invoice total was $5000",
            "importance": 0.8,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "memory_id" in data
    assert "content_hash" in data
    assert "trace_ms" in data


def test_add_and_search(client: TestClient):
    # Add some facts
    client.post("/v1/add", json={"identifier": "a", "fact": "duplicate invoice risk for vendor x"})
    client.post("/v1/add", json={"identifier": "b", "fact": "payment terms are net 30"})
    client.post("/v1/add", json={"identifier": "c", "fact": "vendor y has clean history"})

    # Search
    resp = client.post(
        "/v1/search",
        json={
            "query": "duplicate invoice risk",
            "top_k": 2,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] <= 2
    assert len(data["memories"]) <= 2
    assert all(m["role"] == "system" for m in data["memories"])
    assert "trace_ms" in data


def test_search_max_chars(client: TestClient):
    client.post("/v1/add", json={"identifier": "x", "fact": "a" * 200})
    resp = client.post("/v1/search", json={"query": "aaa", "top_k": 5, "max_chars": 50})
    data = resp.json()
    total = sum(len(m["content"]) for m in data["memories"])
    assert total <= 50


def test_snapshot_and_hydrate(client: TestClient, tmp_path: Path):
    client.post("/v1/add", json={"identifier": "s", "fact": "snapshot test fact"})

    swap = str(tmp_path / "test_swap.jsonl")

    # Snapshot
    resp = client.post("/v1/snapshot", json={"file": swap})
    assert resp.status_code == 200
    assert resp.json()["exported"] == 1

    # Verify file exists
    assert Path(swap).exists()
    lines = Path(swap).read_text().strip().split("\n")
    assert len(lines) == 1


def test_hydrate_endpoint(client: TestClient, tmp_path: Path):
    swap = tmp_path / "h.jsonl"
    swap.write_text(json.dumps({"identifier": "h", "fact_text": "hydrated fact"}) + "\n")

    resp = client.post("/v1/hydrate", json={"file": str(swap)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["loaded"] == 1

    # Verify it's searchable
    health = client.get("/v1/health").json()
    assert health["memory_count"] == 1
