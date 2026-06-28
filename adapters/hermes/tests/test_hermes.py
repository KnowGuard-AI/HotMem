"""Tests for hotmem_hermes — provider lifecycle, tools, sync, round-trip."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hotmem.server import create_app


@pytest.fixture
def hotmem_app(tmp_path: Path):
    """A running in-process HotMem server."""
    db_path = tmp_path / "hermes_test.sqlite"
    swap_path = tmp_path / "swap.jsonl"
    app = create_app(db_path=db_path, swap_path=str(swap_path))
    with TestClient(app) as transport:
        yield transport, swap_path


def _make_async_client(hotmem_app):
    import httpx
    from hotmem.client import AsyncHotMemClient

    transport, _ = hotmem_app
    client = AsyncHotMemClient.__new__(AsyncHotMemClient)
    client.base_url = "http://testserver"
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.ASGITransport(app=transport.app),
    )
    return client


def test_provider_name_and_config_schema():
    from hotmem_hermes import HotMemMemoryProvider

    p = HotMemMemoryProvider()
    assert p.name == "hotmem"
    schema = p.get_config_schema()
    keys = [f["key"] for f in schema]
    assert "hotmem_url" in keys


def test_is_available_with_env(monkeypatch):
    from hotmem_hermes import HotMemMemoryProvider

    monkeypatch.setenv("HOTMEM_URL", "http://localhost:8711")
    p = HotMemMemoryProvider()
    assert p.is_available() is True


def test_save_config_writes_json(tmp_path):
    from hotmem_hermes import HotMemMemoryProvider

    p = HotMemMemoryProvider()
    p.save_config({"hotmem_url": "http://x:1"}, str(tmp_path))
    cfg = json.loads((tmp_path / "hotmem.json").read_text())
    assert cfg["hotmem_url"] == "http://x:1"


def test_tool_schemas():
    from hotmem_hermes import HotMemMemoryProvider

    p = HotMemMemoryProvider()
    names = [t["name"] for t in p.get_tool_schemas()]
    assert names == ["hotmem_search", "hotmem_store"]


def test_handle_tool_store_and_search(hotmem_app):
    from hotmem_hermes import HotMemMemoryProvider

    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)

    async def go():
        res = await p.handle_tool_call(
            "hotmem_store",
            {
                "identifier": "vendor_a",
                "fact": "Invoice total $5000",
                "importance": 0.8,
            },
        )
        assert "memory_id" in res

        out = await p.handle_tool_call("hotmem_search", {"query": "invoice", "top_k": 5})
        assert out["count"] >= 1
        assert "Invoice" in out["memories"][0]["content"]

    asyncio.run(go())


def test_prefetch_returns_recall(hotmem_app):
    from hotmem_hermes import HotMemMemoryProvider

    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)
    asyncio.run(p._client.add("env", "Staging SSH on port 2222"))

    recall = asyncio.run(p.prefetch("staging ssh port"))
    assert "HotMem recall:" in recall
    assert "2222" in recall


def test_sync_turn_persists_async(hotmem_app):
    from hotmem_hermes import HotMemMemoryProvider

    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)
    p._session_id = "sess-1"

    p.sync_turn("what is the staging port?", "it is 2222")
    if p._sync_thread:
        p._sync_thread.join(timeout=10.0)

    memories = asyncio.run(p._client.search("staging port"))
    contents = " ".join(m["content"] for m in memories)
    assert "2222" in contents


def test_attach_sync_adds_hooks(hotmem_app):
    from hotmem_hermes import HotMemMemoryProvider
    from hotmem_hermes.sync import attach_sync

    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)
    attach_sync(p)
    assert callable(p.on_memory_write)
    assert callable(p.on_pre_compress)
    assert callable(p.on_session_end)


def test_on_memory_write_mirrors(hotmem_app):
    from hotmem_hermes import HotMemMemoryProvider
    from hotmem_hermes.sync import attach_sync

    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)
    attach_sync(p)

    p.on_memory_write("add", "user", "Prefers terse answers")
    p.on_memory_write("add", "memory", "Staging on port 2222")
    if p._sync_thread:
        p._sync_thread.join(timeout=10.0)

    memories = asyncio.run(p._client.search("user preferences"))
    contents = " ".join(m["content"] for m in memories)
    assert "terse" in contents


def test_on_pre_compress_extracts_durable(hotmem_app):
    from hotmem_hermes import HotMemMemoryProvider
    from hotmem_hermes.sync import attach_sync

    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)
    attach_sync(p)

    messages = [
        {"role": "user", "content": "what time is it?"},
        {"role": "assistant", "content": "it is noon"},
        {"role": "user", "content": "actually, remember that deploy uses blue-green"},
    ]
    p.on_pre_compress(messages)
    if p._sync_thread:
        p._sync_thread.join(timeout=10.0)

    memories = asyncio.run(p._client.search("blue-green deploy"))
    assert len(memories) >= 1


def test_on_session_end_snapshot_hydrate_round_trip(hotmem_app, tmp_path):
    """No data loss on restart: snapshot -> hydrate recovers memories."""
    from hotmem_hermes import HotMemMemoryProvider
    from hotmem_hermes.sync import attach_sync

    transport, swap_path = hotmem_app
    p = HotMemMemoryProvider()
    p._client = _make_async_client(hotmem_app)
    p._swap_path = str(swap_path)
    attach_sync(p)

    asyncio.run(p._client.add("persist", "survive restart", importance=0.9))
    p.on_session_end([])
    if p._sync_thread:
        p._sync_thread.join(timeout=10.0)

    assert Path(swap_path).exists()

    # New DB + hydrate from swap
    new_db = tmp_path / "restarted.sqlite"
    new_app = create_app(db_path=new_db, swap_path=str(swap_path))
    with TestClient(new_app) as new_transport:
        import httpx
        from hotmem.client import AsyncHotMemClient

        c = AsyncHotMemClient.__new__(AsyncHotMemClient)
        c.base_url = "http://testserver"
        c._client = httpx.AsyncClient(
            base_url=c.base_url, transport=httpx.ASGITransport(app=new_transport.app)
        )
        recovered = asyncio.run(c.search("survive restart"))
        asyncio.run(c.close())

    assert len(recovered) >= 1
    assert "survive restart" in recovered[0]["content"]


def test_register_attaches_sync():
    from hotmem_hermes.provider import register

    class FakeCtx:
        provider = None

        def register_memory_provider(self, p):
            self.provider = p

    ctx = FakeCtx()
    register(ctx)
    assert ctx.provider.name == "hotmem"
    assert callable(ctx.provider.on_memory_write)
