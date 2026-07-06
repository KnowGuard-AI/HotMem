"""Golden MCP surface tests — lock tool names and inputSchema shapes (issue #54).

MCP clients (Claude Desktop, Cursor, Warp) bind to tool names and schema
``required`` arrays. Renaming a tool or dropping a required field is a silent
breaking change. These tests make that contract executable.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="requires the optional [mcp] extra")

from mcp.types import ListToolsRequest  # noqa: E402

from hotmem.db import MemoryDB  # noqa: E402
from hotmem.mcp_server import _ServerState, create_server  # noqa: E402

from .conftest import mask  # noqa: E402

# Locked tool-name set. Adding a tool is allowed (and will fail this test,
# forcing an intentional golden update); removing/renaming one is a break.
LOCKED_TOOLS = {
    "add_memory",
    "search_memories",
    "memory_health",
    "snapshot",
    "hydrate",
}

LOCKED_REQUIRED = {
    "add_memory": ["identifier", "fact"],
    "search_memories": ["query"],
    "memory_health": [],
    "snapshot": [],
    "hydrate": [],
}


def _tools(db_path: Path) -> dict[str, dict]:
    server = create_server(db_path)
    result = asyncio.run(
        server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    )
    return {
        t.name: {"inputSchema": t.inputSchema, "description": t.description}
        for t in result.root.tools
    }


def test_mcp_tool_name_set_is_locked(tmp_path):
    tools = _tools(tmp_path / "m.sqlite")
    assert set(tools) == LOCKED_TOOLS


def test_mcp_tool_required_fields_are_locked(tmp_path):
    tools = _tools(tmp_path / "m.sqlite")
    for name, required in LOCKED_REQUIRED.items():
        schema = tools[name]["inputSchema"]
        assert schema.get("required", []) == required, (
            f"MCP tool {name}: required drift. expected {required}, "
            f"got {schema.get('required')}"
        )


def test_mcp_tool_top_level_schema_keys_locked(tmp_path):
    """Every tool's inputSchema is a JSON object; no surprise top-level keys."""
    tools = _tools(tmp_path / "m.sqlite")
    for name, spec in tools.items():
        schema = spec["inputSchema"]
        assert schema.get("type") == "object", f"{name} schema type != object"
        assert "properties" in schema, f"{name} missing properties"
        # `required` may be omitted when a tool has no required fields.
        assert set(schema) <= {"type", "properties", "required"}, (
            f"{name} has unexpected schema keys: {set(schema) - {'type', 'properties', 'required'}}"
        )


def test_mcp_add_memory_argument_shape_locked(tmp_path):
    tools = _tools(tmp_path / "m.sqlite")
    props = tools["add_memory"]["inputSchema"]["properties"]
    assert set(props) == {"identifier", "fact", "importance", "ttl_seconds"}
    assert props["identifier"] == {"type": "string"}
    assert props["fact"] == {"type": "string"}
    assert props["importance"] == {"type": "number", "minimum": 0.0, "maximum": 1.0}
    assert props["ttl_seconds"] == {"type": "integer", "minimum": 1}


def test_mcp_search_memory_argument_shape_locked(tmp_path):
    tools = _tools(tmp_path / "m.sqlite")
    props = tools["search_memories"]["inputSchema"]["properties"]
    assert set(props) == {"query", "top_k", "max_chars"}
    assert props["query"] == {"type": "string"}
    assert props["top_k"] == {"type": "integer", "minimum": 1, "maximum": 100}
    assert props["max_chars"] == {"type": "integer", "minimum": 1}


# ── payload (CallToolResult) shape contracts ──────────────────────────────────


def _state(tmp_path: Path) -> _ServerState:
    db = MemoryDB(tmp_path / "s.sqlite")
    st = _ServerState()
    st.db = db
    st.db_path = str(tmp_path / "s.sqlite")
    st.swap_path = None
    st.start_time = time.time()
    return st


def _text(result) -> dict:
    return json.loads(result.content[0].text)


def test_mcp_add_memory_result_shape_locked(tmp_path):
    from hotmem.mcp_server import _handle_add_memory

    st = _state(tmp_path)
    try:
        payload = _text(_handle_add_memory(st, {"identifier": "v", "fact": "f"}))
        assert set(payload) == {"memory_id", "content_hash", "trace_ms"}
        assert mask(payload) == {
            "memory_id": "<uuid>",
            "content_hash": "<hash>",
            "trace_ms": "<float>",
        }
    finally:
        st.db.close()


def test_mcp_search_result_shape_locked(tmp_path):
    from hotmem.mcp_server import _handle_add_memory, _handle_search_memories

    st = _state(tmp_path)
    try:
        _handle_add_memory(st, {"identifier": "a", "fact": "invoice risk"})
        payload = _text(_handle_search_memories(st, {"query": "invoice", "top_k": 1}))
        assert set(payload) == {"memories", "count", "trace_ms"}
        assert payload["count"] == len(payload["memories"])
        msg = payload["memories"][0]
        assert set(msg) == {"role", "content", "memory_id", "identifier", "score", "created_at"}
        assert msg["role"] == "system"
    finally:
        st.db.close()


def test_mcp_health_result_shape_locked(tmp_path):
    from hotmem.mcp_server import _handle_memory_health

    st = _state(tmp_path)
    try:
        payload = _text(_handle_memory_health(st, {}))
        assert set(payload) == {"status", "memory_count", "db_path", "uptime_s"}
    finally:
        st.db.close()


def test_mcp_unknown_tool_is_error(tmp_path):
    from mcp.types import CallToolRequest, CallToolRequestParams

    import hotmem.mcp_server as mcp_server

    db = MemoryDB(tmp_path / "u.sqlite")
    mcp_server._state.db = db
    mcp_server._state.db_path = str(tmp_path / "u.sqlite")
    mcp_server._state.swap_path = None
    mcp_server._state.start_time = time.time()
    try:
        server = create_server(str(tmp_path / "u.sqlite"))
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="does_not_exist", arguments={}),
        )
        result = asyncio.run(server.request_handlers[CallToolRequest](req))
        assert result.root.isError
        assert "unknown tool" in json.loads(result.root.content[0].text)["error"]
    finally:
        db.close()
