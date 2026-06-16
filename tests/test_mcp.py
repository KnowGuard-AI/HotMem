"""Tests for hotmem.mcp_server — MCP tool handlers and server wiring."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="requires the optional [mcp] extra")

from hotmem.db import MemoryDB  # noqa: E402
from hotmem.mcp_server import (  # noqa: E402
    _handle_add_memory,
    _handle_hydrate,
    _handle_memory_health,
    _handle_search_memories,
    _handle_snapshot,
    _ServerState,
    create_server,
)


def _text(result) -> dict:
    """Extract the JSON payload from a CallToolResult's text content."""
    return json.loads(result.content[0].text)


@pytest.fixture
def state(tmp_path: Path) -> _ServerState:
    """Provide a server state backed by a fresh temp database."""
    db = MemoryDB(tmp_path / "mcp.sqlite")
    st = _ServerState()
    st.db = db
    st.db_path = str(tmp_path / "mcp.sqlite")
    st.swap_path = None
    st.start_time = time.time()
    yield st
    db.close()


def test_add_memory(state: _ServerState):
    result = _handle_add_memory(state, {"identifier": "vendor_x", "fact": "Invoice total $5000"})
    payload = _text(result)
    assert "memory_id" in payload
    assert "content_hash" in payload
    assert "trace_ms" in payload
    assert state.db.count() == 1


def test_add_memory_with_ttl_and_importance(state: _ServerState):
    result = _handle_add_memory(
        state,
        {"identifier": "v", "fact": "temporary note", "importance": 0.9, "ttl_seconds": 3600},
    )
    assert "error" not in _text(result)
    assert state.db.count() == 1


def test_add_memory_missing_required_argument_errors(state: _ServerState):
    with pytest.raises(KeyError):
        _handle_add_memory(state, {"identifier": "v"})


def test_search_memories(state: _ServerState):
    _handle_add_memory(state, {"identifier": "a", "fact": "duplicate invoice risk for vendor x"})
    _handle_add_memory(state, {"identifier": "b", "fact": "payment terms are net 30"})
    _handle_add_memory(state, {"identifier": "c", "fact": "vendor y has clean history"})

    result = _handle_search_memories(state, {"query": "duplicate invoice risk", "top_k": 2})
    payload = _text(result)
    assert payload["count"] <= 2
    assert len(payload["memories"]) <= 2
    assert all(m["role"] == "system" for m in payload["memories"])
    assert "trace_ms" in payload


def test_search_max_chars(state: _ServerState):
    _handle_add_memory(state, {"identifier": "x", "fact": "a" * 200})
    result = _handle_search_memories(state, {"query": "aaa", "top_k": 5, "max_chars": 50})
    payload = _text(result)
    total = sum(len(m["content"]) for m in payload["memories"])
    assert total <= 50


def test_memory_health(state: _ServerState):
    _handle_add_memory(state, {"identifier": "h", "fact": "health fact"})
    payload = _text(_handle_memory_health(state, {}))
    assert payload["status"] == "ok"
    assert payload["memory_count"] == 1
    assert payload["db_path"] == state.db_path
    assert "uptime_s" in payload


def test_snapshot_and_hydrate(state: _ServerState, tmp_path: Path):
    _handle_add_memory(state, {"identifier": "s", "fact": "snapshot test fact"})

    swap = str(tmp_path / "swap.jsonl")
    snap = _text(_handle_snapshot(state, {"file": swap}))
    assert snap["exported"] == 1
    assert Path(swap).exists()

    # Hydrate into a fresh state and confirm the fact is loaded.
    other_db = MemoryDB(tmp_path / "other.sqlite")
    other = _ServerState()
    other.db = other_db
    other.db_path = str(tmp_path / "other.sqlite")
    other.swap_path = None
    other.start_time = time.time()
    try:
        loaded = _text(_handle_hydrate(other, {"file": swap}))
        assert loaded["loaded"] == 1
        assert other_db.count() == 1
    finally:
        other_db.close()


def test_snapshot_defaults_to_state_swap_path(state: _ServerState, tmp_path: Path):
    state.swap_path = str(tmp_path / "default.jsonl")
    _handle_add_memory(state, {"identifier": "d", "fact": "default swap fact"})
    snap = _text(_handle_snapshot(state, {}))
    assert snap["path"] == state.swap_path
    assert Path(state.swap_path).exists()


def _setup_module_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MemoryDB:
    """Point the module-level _state (used by the server dispatch) at a temp DB."""
    import hotmem.mcp_server as mcp_server

    db_path = tmp_path / "module.sqlite"
    db = MemoryDB(db_path)
    monkeypatch.setattr(mcp_server._state, "db", db, raising=False)
    monkeypatch.setattr(mcp_server._state, "db_path", str(db_path), raising=False)
    monkeypatch.setattr(mcp_server._state, "swap_path", None, raising=False)
    monkeypatch.setattr(mcp_server._state, "start_time", time.time(), raising=False)
    return db


def test_list_tools_wiring():
    from mcp.types import ListToolsRequest

    server = create_server(":memory:")
    result = asyncio.run(
        server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    )
    names = {t.name for t in result.root.tools}
    assert names == {"add_memory", "search_memories", "memory_health", "snapshot", "hydrate"}


def test_call_tool_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from mcp.types import CallToolRequest, CallToolRequestParams

    db = _setup_module_state(monkeypatch, tmp_path)
    try:
        server = create_server(str(tmp_path / "module.sqlite"))
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="add_memory", arguments={"identifier": "x", "fact": "hello world"}
            ),
        )
        result = asyncio.run(server.request_handlers[CallToolRequest](req))
        assert not result.root.isError
        payload = json.loads(result.root.content[0].text)
        assert "memory_id" in payload
        assert db.count() == 1
    finally:
        db.close()


def test_call_tool_unknown_tool_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from mcp.types import CallToolRequest, CallToolRequestParams

    db = _setup_module_state(monkeypatch, tmp_path)
    try:
        server = create_server(str(tmp_path / "module.sqlite"))
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="does_not_exist", arguments={}),
        )
        result = asyncio.run(server.request_handlers[CallToolRequest](req))
        assert result.root.isError
        assert "unknown tool" in json.loads(result.root.content[0].text)["error"]
    finally:
        db.close()


def test_call_tool_missing_argument_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A call missing a schema-required argument is rejected by input validation."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    db = _setup_module_state(monkeypatch, tmp_path)
    try:
        server = create_server(str(tmp_path / "module.sqlite"))
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="add_memory", arguments={"identifier": "x"}),
        )
        result = asyncio.run(server.request_handlers[CallToolRequest](req))
        assert result.root.isError
        assert "validation error" in result.root.content[0].text.lower()
    finally:
        db.close()
