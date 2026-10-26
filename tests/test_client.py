"""Tests for hotmem.client - HotMemClient SDK."""

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


def test_sync_and_async_sdk_context_and_tags_propagation(tmp_path: Path):
    """WP2 MVT: Sync and async Python SDKs propagate harness_id, session_id, and per-write tags."""
    from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval

    policy = PolicyConfig(
        store_id="store-sdk-111",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-writer",
                caller_id="writer-sdk",
                store_id="store-sdk-111",
                namespaces=("team/shared",),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-reader",
                caller_id="reader-sdk",
                store_id="store-sdk-111",
                namespaces=("reader/private",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-sdk-111",
                        namespace="team/shared",
                        audience=("reader-sdk",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=tmp_path / "sdk_111.sqlite", policy=policy)

    with TestClient(app) as tc:
        writer = HotMemClient(
            "http://testserver",
            token="tok-writer",
            expected_store_id="store-sdk-111",
            harness_id="codex",
            session_id="sess-sdk-alpha",
            source="agent://codex-sdk",
            namespace="team/shared",
        )
        # Route through TestClient while preserving client's configured headers
        tc.headers.update(writer._client.headers)
        writer._client.close()
        writer._client = tc

        r1 = writer.add(
            "sdk/decision",
            "First decision recorded with model alpha",
            tags=["model:gpt-5", "project:hotmem", "org:knowguard"],
        )
        r2 = writer.add(
            "sdk/decision",
            "Second decision recorded after model switch in same session",
            tags=["model:o4-mini", "project:hotmem"],
        )
        r3 = writer.add(
            "sdk/decision",
            "Third decision recorded with per-call session/harness override and empty tags",
            tags=[],
            harness_id="codex-cli",
            session_id="sess-sdk-alpha-override",
        )

        async def verify_with_async_client():
            transport = httpx.ASGITransport(app=app)
            async with AsyncHotMemClient(
                "http://testserver",
                token="tok-reader",
                expected_store_id="store-sdk-111",
                harness_id="claude-code",
                session_id="sess-sdk-beta",
                source="agent://claude-sdk",
            ) as reader:
                orig_headers = dict(reader._client.headers)
                await reader._client.aclose()
                reader._client = httpx.AsyncClient(
                    base_url="http://testserver",
                    transport=transport,
                    headers=orig_headers,
                )

                hits = await reader.search("decision recorded", top_k=10)
                by_id = {h["memory_id"]: h for h in hits}
                assert by_id[r1["memory_id"]]["tags"] == [
                    "model:gpt-5",
                    "project:hotmem",
                    "org:knowguard",
                ]
                assert by_id[r1["memory_id"]]["provenance"]["harness_id"] == "codex"
                assert by_id[r1["memory_id"]]["provenance"]["session_id"] == "sess-sdk-alpha"

                assert by_id[r2["memory_id"]]["tags"] == ["model:o4-mini", "project:hotmem"]
                assert by_id[r2["memory_id"]]["provenance"]["harness_id"] == "codex"
                assert by_id[r2["memory_id"]]["provenance"]["session_id"] == "sess-sdk-alpha"

                assert by_id[r3["memory_id"]]["tags"] == []
                assert by_id[r3["memory_id"]]["provenance"]["harness_id"] == "codex-cli"
                assert (
                    by_id[r3["memory_id"]]["provenance"]["session_id"] == "sess-sdk-alpha-override"
                )

                listed = await reader.list("sdk/decision")
                assert len(listed) == 3
                m1 = await reader.get_memory(r1["memory_id"])
                assert m1["tags"] == ["model:gpt-5", "project:hotmem", "org:knowguard"]
                assert m1["provenance"]["harness_id"] == "codex"

        asyncio.run(verify_with_async_client())
