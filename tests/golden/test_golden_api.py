"""Golden API shape tests — lock down /v1 endpoint contracts (issue #54).

These are *shape* contracts: exact top-level key sets and masked value types.
A new field added to any response fails here first and must be an intentional
golden-contract update (additive only — see test_golden_additive.py).
"""

from __future__ import annotations

import pytest

from .conftest import _load_fixture, assert_keys_exact, mask

# ── /v1/health ───────────────────────────────────────────────────────────────


def test_health_shape(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"status", "memory_count", "db_path", "uptime_s"}, "GET /v1/health")
    assert mask(body) == {
        "status": "<str>",
        "memory_count": "<int>",
        "db_path": "<path>",
        "uptime_s": "<float>",
    }


def test_health_trace_header(client):
    """The X-HotMem-Trace-Id header is part of the public surface."""
    resp = client.get("/v1/health")
    assert "X-HotMem-Trace-Id" in resp.headers


# ── /v1/add ──────────────────────────────────────────────────────────────────


def test_add_minimal_payload_shape(client):
    """The legacy {identifier, fact} add payload is the baseline contract."""
    resp = client.post(
        "/v1/add",
        json={"identifier": "vendor_x", "fact": "Invoice total was $5000"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"memory_id", "content_hash", "trace_ms"}, "POST /v1/add (minimal)")
    assert mask(body) == {
        "memory_id": "<uuid>",
        "content_hash": "<hash>",
        "trace_ms": "<float>",
    }


def test_add_extended_payload_shape(client):
    """The full extended add payload returns the same response shape (additive)."""
    resp = client.post(
        "/v1/add",
        json={
            "identifier": "vendor_x",
            "fact": "Invoice total was $5000",
            "source": "erp",
            "importance": 0.8,
            "metadata": {"doc": "inv-9"},
            "ttl_seconds": 3600,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"memory_id", "content_hash", "trace_ms"}, "POST /v1/add (extended)")
    assert mask(body) == {
        "memory_id": "<uuid>",
        "content_hash": "<hash>",
        "trace_ms": "<float>",
    }


# ── /v1/search ───────────────────────────────────────────────────────────────


def test_search_response_shape(client):
    client.post("/v1/add", json={"identifier": "a", "fact": "duplicate invoice risk for vendor x"})
    client.post("/v1/add", json={"identifier": "b", "fact": "payment terms are net 30"})

    resp = client.post("/v1/search", json={"query": "duplicate invoice risk", "top_k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"memories", "count", "trace_ms"}, "POST /v1/search")

    assert body["count"] == len(body["memories"])
    assert body["count"] <= 2
    expected_msg = _load_fixture("search_expected.json")
    for m in body["memories"]:
        assert m["role"] == "system"  # role is a stable literal, part of the contract
        masked = mask(m)
        masked["role"] = m["role"]  # preserve the literal for the shape compare
        assert masked == expected_msg, f"search message drift: {masked} != {expected_msg}"


def test_search_message_object_keys_locked(client):
    """The message-object key set is the part clients depend on most."""
    client.post("/v1/add", json={"identifier": "a", "fact": "duplicate invoice risk"})
    resp = client.post("/v1/search", json={"query": "invoice", "top_k": 1})
    msg = resp.json()["memories"][0]
    assert_keys_exact(
        msg,
        {"role", "content", "memory_id", "identifier", "score", "created_at"},
        "search message object",
    )
    assert msg["role"] == "system"


# ── /v1/memories ──────────────────────────────────────────────────────────────


def test_memories_response_shape(client):
    client.post("/v1/add", json={"identifier": "chat", "fact": "message 0"})
    client.post("/v1/add", json={"identifier": "chat", "fact": "message 1"})

    resp = client.get("/v1/memories", params={"identifier": "chat", "order": "asc"})
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"memories", "count", "trace_ms"}, "GET /v1/memories")
    assert body["count"] == len(body["memories"]) == 2

    expected_row = _load_fixture("memories_expected.json")
    for row in body["memories"]:
        assert_keys_exact(
            row,
            set(expected_row.keys()),
            "memories row",
        )
        assert mask(row) == expected_row, f"memories row drift: {mask(row)} != {expected_row}"


def test_memories_rejects_bad_order(client):
    resp = client.get("/v1/memories", params={"identifier": "x", "order": "sideways"})
    assert resp.status_code == 400


def test_memories_rejects_bad_limit(client):
    resp = client.get("/v1/memories", params={"identifier": "x", "limit": 0})
    assert resp.status_code == 400


# ── /v1/hydrate ───────────────────────────────────────────────────────────────


def test_hydrate_response_shape(client, tmp_path):
    import json

    swap = tmp_path / "h.jsonl"
    swap.write_text(json.dumps({"identifier": "h", "fact_text": "hydrated fact"}) + "\n")

    resp = client.post("/v1/hydrate", json={"file": str(swap)})
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"loaded", "skipped_dupes", "path"}, "POST /v1/hydrate")
    assert mask(body) == {"loaded": "<int>", "skipped_dupes": "<int>", "path": "<path>"}


def test_hydrate_rejects_unsupported_extension(client, tmp_path):
    """Unsupported swap formats must fail with a clear, stable error string."""
    import json

    swap = tmp_path / "h.txt"
    swap.write_text(json.dumps({"identifier": "h", "fact_text": "x"}) + "\n")
    resp = client.post("/v1/hydrate", json={"file": str(swap)})
    assert resp.status_code == 400
    assert "supported: .jsonl, .jsonl.gz" in resp.json()["detail"]


# ── /v1/snapshot ──────────────────────────────────────────────────────────────


def test_snapshot_response_shape(client, tmp_path):
    client.post("/v1/add", json={"identifier": "s", "fact": "snapshot test fact"})
    swap = str(tmp_path / "snap.jsonl")

    resp = client.post("/v1/snapshot", json={"file": swap})
    assert resp.status_code == 200
    body = resp.json()
    assert_keys_exact(body, {"exported", "path"}, "POST /v1/snapshot")
    assert mask(body) == {"exported": "<int>", "path": "<path>"}


@pytest.mark.parametrize("missing", ["identifier", "fact"])
def test_add_missing_required_field_rejected(client, missing):
    """Pydantic validation rejects payloads missing schema-required fields."""
    payload = {"identifier": "x", "fact": "y"}
    payload.pop(missing)
    resp = client.post("/v1/add", json=payload)
    assert resp.status_code == 422
