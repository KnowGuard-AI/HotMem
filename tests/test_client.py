"""Tests for hotmem.client — HotMemClient SDK."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hotmem.client import AsyncHotMemClient, HotMemClient
from hotmem.server import create_app


@pytest.fixture
def mock_client(tmp_path: Path):
    """Create a client backed by a test server (no network)."""
    db_path = tmp_path / "client_test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as test_transport:
        client = HotMemClient.__new__(HotMemClient)
        client.base_url = "http://testserver"
        client._client = test_transport
        yield client


def test_add_and_search(mock_client: HotMemClient):
    result = mock_client.add("vendor_a", "high risk invoice pattern detected")
    assert "memory_id" in result

    memories = mock_client.search("invoice risk", top_k=3)
    assert len(memories) >= 1
    assert memories[0]["role"] == "system"
    assert "score" in memories[0]


def test_add_with_ttl(mock_client: HotMemClient):
    result = mock_client.add("vendor_a", "temporary client fact", ttl_seconds=3600)
    assert "memory_id" in result

    memories = mock_client.search("temporary client fact", top_k=1)
    assert len(memories) == 1


def test_health(mock_client: HotMemClient):
    data = mock_client.health()
    assert data["status"] == "ok"


def test_context_manager(tmp_path: Path):
    """Verify the client works as a context manager."""
    db_path = tmp_path / "ctx_test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as test_transport:
        client = HotMemClient.__new__(HotMemClient)
        client.base_url = "http://testserver"
        client._client = test_transport

        with client as c:
            c.add("x", "test fact")
            assert c.health()["memory_count"] == 1


def test_async_client_methods():
    requests: list[tuple[str, str, dict | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode()) if request.content else None
        requests.append((request.method, request.url.path, payload))
        match request.url.path:
            case "/v1/health":
                return httpx.Response(200, json={"status": "ok", "memory_count": 0})
            case "/v1/add":
                return httpx.Response(200, json={"memory_id": "m1"})
            case "/v1/search":
                return httpx.Response(
                    200,
                    json={
                        "memories": [
                            {
                                "role": "system",
                                "content": "remembered fact",
                                "score": 1.0,
                            }
                        ]
                    },
                )
            case "/v1/hydrate":
                return httpx.Response(200, json={"loaded": 1, "skipped_dupes": 0})
            case "/v1/snapshot":
                return httpx.Response(200, json={"exported": 1, "path": "swap.jsonl"})
        return httpx.Response(404)

    async def run_client():
        transport = httpx.MockTransport(handler)
        client = AsyncHotMemClient.__new__(AsyncHotMemClient)
        client.base_url = "http://testserver"
        client._client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=transport,
            timeout=30.0,
        )

        async with client as c:
            assert await c.health() == {"status": "ok", "memory_count": 0}
            assert await c.add("vendor", "async fact", ttl_seconds=60) == {"memory_id": "m1"}
            assert (await c.search("fact", top_k=2, max_chars=100))[0]["score"] == 1.0
            assert await c.hydrate("swap.jsonl") == {"loaded": 1, "skipped_dupes": 0}
            assert await c.snapshot("swap.jsonl") == {"exported": 1, "path": "swap.jsonl"}

        assert client._client.is_closed

    asyncio.run(run_client())

    assert requests == [
        ("GET", "/v1/health", None),
        (
            "POST",
            "/v1/add",
            {
                "identifier": "vendor",
                "fact": "async fact",
                "source": "",
                "importance": 0.5,
                "metadata": {},
                "ttl_seconds": 60,
            },
        ),
        ("POST", "/v1/search", {"query": "fact", "top_k": 2, "max_chars": 100}),
        ("POST", "/v1/hydrate", {"file": "swap.jsonl"}),
        ("POST", "/v1/snapshot", {"file": "swap.jsonl"}),
    ]
