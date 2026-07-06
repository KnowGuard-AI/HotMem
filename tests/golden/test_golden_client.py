"""Golden Python client surface tests — lock HotMemClient / AsyncHotMemClient.

These lock the public method names and the request payloads they emit, plus
the return shapes clients are expected to consume. A renamed/removed method or
a changed payload fails here first.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import httpx
from fastapi.testclient import TestClient

from hotmem.client import AsyncHotMemClient, HotMemClient
from hotmem.server import create_app

from .conftest import assert_keys_exact

SYNC_METHODS = {"health", "add", "search", "list", "hydrate", "snapshot", "close"}
ASYNC_METHODS = {"health", "add", "search", "list", "hydrate", "snapshot", "close"}


def test_sync_client_public_method_set_is_locked():
    public = {
        name
        for name, _ in inspect.getmembers(HotMemClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    public |= {"__enter__", "__exit__"}  # context-manager protocol is public
    assert public >= SYNC_METHODS, f"missing sync client methods: {SYNC_METHODS - public}"


def test_async_client_public_method_set_is_locked():
    public = {
        name
        for name, _ in inspect.getmembers(AsyncHotMemClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    public |= {"__aenter__", "__aexit__"}
    assert public >= ASYNC_METHODS, f"missing async client methods: {ASYNC_METHODS - public}"


# ── request payload contracts (via MockTransport) ─────────────────────────────


def _capture() -> tuple[list[tuple[str, str, dict]], httpx.MockTransport]:
    log: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode()) if request.content else None
        if payload is None:
            log.append((request.method, request.url.path, None))
        else:
            log.append((request.method, request.url.path, payload))
        match request.url.path:
            case "/v1/health":
                return httpx.Response(200, json={"status": "ok", "memory_count": 0})
            case "/v1/add":
                return httpx.Response(
                    200,
                    json={"memory_id": "m1", "content_hash": "h" * 64, "trace_ms": 1.0},
                )
            case "/v1/search":
                return httpx.Response(
                    200,
                    json={
                        "memories": [
                            {
                                "role": "system",
                                "content": "fact",
                                "memory_id": "m1",
                                "identifier": "v",
                                "score": 0.9,
                                "created_at": "2026-07-06T00:00:00Z",
                            }
                        ],
                        "count": 1,
                        "trace_ms": 1.0,
                    },
                )
            case "/v1/memories":
                return httpx.Response(200, json={"memories": [], "count": 0, "trace_ms": 1.0})
            case "/v1/hydrate":
                return httpx.Response(200, json={"loaded": 1, "skipped_dupes": 0})
            case "/v1/snapshot":
                return httpx.Response(200, json={"exported": 1, "path": "swap.jsonl"})
        return httpx.Response(404)

    return log, httpx.MockTransport(handler)


def test_sync_client_emits_locked_payloads(tmp_path):
    """The exact request payloads the sync client sends are part of the contract."""
    log, transport = _capture()
    client = HotMemClient.__new__(HotMemClient)
    client.base_url = "http://testserver"
    client._client = httpx.Client(base_url=client.base_url, transport=transport, timeout=30.0)

    client.health()
    client.add("vendor", "fact", source="s", importance=0.7, metadata={"k": 1}, ttl_seconds=60)
    client.search("fact", top_k=2, max_chars=100)
    client.list("vendor", order="asc", limit=10)
    client.hydrate("swap.jsonl")
    client.snapshot("swap.jsonl")
    client.close()

    methods_paths = [(m, p) for m, p, _ in log]
    assert methods_paths == [
        ("GET", "/v1/health"),
        ("POST", "/v1/add"),
        ("POST", "/v1/search"),
        ("GET", "/v1/memories"),
        ("POST", "/v1/hydrate"),
        ("POST", "/v1/snapshot"),
    ]
    # add payload — exact key set
    assert set(log[1][2]) == {
        "identifier",
        "fact",
        "source",
        "importance",
        "metadata",
        "ttl_seconds",
    }
    assert log[1][2] == {
        "identifier": "vendor",
        "fact": "fact",
        "source": "s",
        "importance": 0.7,
        "metadata": {"k": 1},
        "ttl_seconds": 60,
    }
    # search payload — only the keys that matter, no surprise fields
    assert set(log[2][2]) == {"query", "top_k", "max_chars"}


def test_async_client_emits_locked_payloads():
    log, transport = _capture()

    async def run():
        client = AsyncHotMemClient.__new__(AsyncHotMemClient)
        client.base_url = "http://testserver"
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=transport, timeout=30.0
        )
        async with client as c:
            await c.health()
            await c.add("vendor", "fact", ttl_seconds=60)
            await c.search("fact", top_k=2, max_chars=100)
            await c.list("vendor")
            await c.hydrate("swap.jsonl")
            await c.snapshot("swap.jsonl")

    asyncio.run(run())

    methods_paths = [(m, p) for m, p, _ in log]
    assert methods_paths == [
        ("GET", "/v1/health"),
        ("POST", "/v1/add"),
        ("POST", "/v1/search"),
        ("GET", "/v1/memories"),
        ("POST", "/v1/hydrate"),
        ("POST", "/v1/snapshot"),
    ]
    # add payload omits ttl_seconds only when None — locked default shape
    assert set(log[1][2]) == {
        "identifier",
        "fact",
        "source",
        "importance",
        "metadata",
        "ttl_seconds",
    }


# ── return-shape contracts (against a real TestClient server) ──────────────────


def test_sync_client_return_shapes(tmp_path):
    app = create_app(db_path=tmp_path / "c.sqlite")
    with TestClient(app) as test_transport:
        client = HotMemClient.__new__(HotMemClient)
        client.base_url = "http://testserver"
        client._client = test_transport

        health = client.health()
        assert_keys_exact(
            health, {"status", "memory_count", "db_path", "uptime_s"}, "client.health()"
        )

        added = client.add("v", "invoice risk")
        assert_keys_exact(added, {"memory_id", "content_hash", "trace_ms"}, "client.add()")

        client.add("v", "payment terms")
        results = client.search("invoice", top_k=1)
        assert isinstance(results, list)
        assert_keys_exact(
            results[0],
            {"role", "content", "memory_id", "identifier", "score", "created_at"},
            "client.search()[0]",
        )

        listed = client.list("v", order="asc")
        assert isinstance(listed, list)
        assert_keys_exact(
            listed[0],
            {
                "id",
                "identifier",
                "fact_text",
                "importance",
                "metadata_json",
                "source",
                "created_at",
            },
            "client.list()[0]",
        )

        hydrated = client.hydrate()
        assert_keys_exact(hydrated, {"loaded", "skipped_dupes"}, "client.hydrate()")

        snap = client.snapshot()
        assert_keys_exact(snap, {"exported", "path"}, "client.snapshot()")


def test_sync_client_add_with_metadata_serializes_to_json_string():
    """metadata dict must be JSON-serializable in the wire payload (not a string)."""
    log, transport = _capture()
    client = HotMemClient.__new__(HotMemClient)
    client.base_url = "http://testserver"
    client._client = httpx.Client(base_url=client.base_url, transport=transport, timeout=30.0)
    client.add("v", "f", metadata={"k": [1, 2]})
    assert log[0][2]["metadata"] == {"k": [1, 2]}
    assert isinstance(log[0][2]["metadata"], dict)
    client.close()
