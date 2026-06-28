"""Tests for hotmem_crewai."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hotmem.server import create_app


@pytest.fixture
def hotmem_url(tmp_path: Path) -> str:
    db_path = tmp_path / "crewai_test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as transport:
        yield transport


def _make_client(hotmem_url):
    from hotmem.client import HotMemClient

    c = HotMemClient.__new__(HotMemClient)
    c.base_url = "http://testserver"
    c._client = hotmem_url
    return c


def test_save_and_search(hotmem_url):
    from hotmem_crewai import HotMemMemory

    mem = HotMemMemory(client=_make_client(hotmem_url))
    mem.save("Vendor Acme has NET-30 terms", identifier="vendor:acme", importance=0.8)

    results = mem.search("acme terms", top_k=5)
    assert len(results) >= 1
    assert "NET-30" in results[0]["content"]


def test_load_alias(hotmem_url):
    from hotmem_crewai import HotMemMemory

    mem = HotMemMemory(client=_make_client(hotmem_url))
    mem.save("prefers email updates", identifier="user")

    loaded = mem.load("email", top_k=5)
    assert len(loaded) >= 1
