"""Tests for hotmem_pydanticai."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hotmem.server import create_app


@pytest.fixture
def hotmem_url(tmp_path: Path) -> str:
    db_path = tmp_path / "pydanticai_test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as transport:
        yield transport


def _make_client(hotmem_url):
    from hotmem.client import HotMemClient

    c = HotMemClient.__new__(HotMemClient)
    c.base_url = "http://testserver"
    c._client = hotmem_url
    return c


def test_deps_recall(hotmem_url):
    from hotmem_pydanticai import HotMemDeps

    client = _make_client(hotmem_url)
    client.add("proj", "Uses tabs, 120-char lines, Google docstrings", importance=0.8)

    deps = HotMemDeps(client=client, top_k=5)
    recall = deps.recall("code style conventions")
    assert "Relevant memories:" in recall
    assert "tabs" in recall


def test_deps_recall_empty(hotmem_url):
    from hotmem_pydanticai import HotMemDeps

    client = _make_client(hotmem_url)
    deps = HotMemDeps(client=client)
    assert deps.recall("nothing here at all zzzz") == ""


def test_recall_system_prompt(hotmem_url):
    import asyncio

    from hotmem_pydanticai import HotMemDeps, recall_system_prompt

    client = _make_client(hotmem_url)
    client.add("user", "Prefers terse answers", importance=0.9)

    class FakeCtx:
        deps = HotMemDeps(client=client, top_k=5)
        prompt = "answer style"

    result = asyncio.run(recall_system_prompt(FakeCtx()))
    assert "terse" in result
