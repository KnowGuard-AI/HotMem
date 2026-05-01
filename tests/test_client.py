"""Tests for hotmem.client — HotMemClient SDK."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.client import HotMemClient
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
