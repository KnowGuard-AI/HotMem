"""Tests for hotmem_autogen."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hotmem.server import create_app


@pytest.fixture
def hotmem_url(tmp_path: Path) -> str:
    db_path = tmp_path / "autogen_test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as transport:
        yield transport


def _make_client(hotmem_url):
    from hotmem.client import HotMemClient

    c = HotMemClient.__new__(HotMemClient)
    c.base_url = "http://testserver"
    c._client = hotmem_url
    return c


def test_add_context_returns_formatted(hotmem_url):
    from hotmem_autogen import HotMemMemoryPlugin

    plugin = HotMemMemoryPlugin(client=_make_client(hotmem_url), top_k=3)
    plugin.save("Server staging runs on port 2222", importance=0.8)

    ctx = plugin.add_context("staging server port")
    assert "Relevant memories:" in ctx
    assert "port 2222" in ctx


def test_add_context_empty(hotmem_url):
    from hotmem_autogen import HotMemMemoryPlugin

    plugin = HotMemMemoryPlugin(client=_make_client(hotmem_url))
    # empty store returns an empty context string
    assert plugin.add_context("zzzznomatchquery12345") == ""


def test_save_and_search(hotmem_url):
    from hotmem_autogen import HotMemMemoryPlugin

    plugin = HotMemMemoryPlugin(client=_make_client(hotmem_url))
    plugin.save("user prefers concise answers", identifier="user", importance=0.9)

    results = plugin.search("user preferences", top_k=5)
    assert len(results) >= 1
