"""HotMem MCP server — stdio transport for MCP-compatible hosts.

Purpose:
    Expose HotMem operations as MCP tools so Claude Desktop, Cursor, Warp,
    and other MCP clients can use a local HotMem instance.

Interface:
    create_server(db_path, swap_path?) -> Server
    run(db_path, swap_path?) -> coroutine — starts the stdio server

Tools:
    - add_memory(identifier, fact, importance?, ttl_seconds?)
    - search_memories(query, top_k?, max_chars?)
    - memory_health()
    - snapshot(file?)
    - hydrate(file?)

Deps: mcp, hotmem.db, hotmem.embed, hotmem.search, hotmem.swap, hotmem.trace
Extension: add new tools (e.g. delete_memory, forget_identifier) here.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.search import search_memories
from hotmem.swap import compute_content_hash
from hotmem.swap import hydrate as swap_hydrate
from hotmem.swap import snapshot as swap_snapshot
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("mcp_server")

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "add_memory": {
        "type": "object",
        "properties": {
            "identifier": {"type": "string"},
            "fact": {"type": "string"},
            "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "ttl_seconds": {"type": "integer", "minimum": 1},
        },
        "required": ["identifier", "fact"],
    },
    "search_memories": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_chars": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
    },
    "memory_health": {
        "type": "object",
        "properties": {},
    },
    "snapshot": {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
        },
    },
    "hydrate": {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
        },
    },
}


class _ServerState:
    """Mutable server state shared between lifespan and tool handlers."""

    db: MemoryDB
    db_path: str
    swap_path: str | None
    start_time: float


_state = _ServerState()


def create_server(db_path: str | Path, swap_path: str | Path | None = None) -> Server:
    """Create and configure the HotMem MCP server."""
    db_path = str(db_path)
    swap_path = str(swap_path) if swap_path else None

    server = Server("hotmem")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Declare the tools exposed by this MCP server."""
        return [
            Tool(
                name="add_memory",
                description="Store a fact in HotMem memory.",
                inputSchema=_TOOL_SCHEMAS["add_memory"],
            ),
            Tool(
                name="search_memories",
                description="Search HotMem and return ranked, LLM-ready message objects.",
                inputSchema=_TOOL_SCHEMAS["search_memories"],
            ),
            Tool(
                name="memory_health",
                description="Return HotMem status: memory count, uptime, and database path.",
                inputSchema=_TOOL_SCHEMAS["memory_health"],
            ),
            Tool(
                name="snapshot",
                description="Export all memories to a JSONL swap file.",
                inputSchema=_TOOL_SCHEMAS["snapshot"],
            ),
            Tool(
                name="hydrate",
                description="Load memories from a JSONL swap file into HotMem.",
                inputSchema=_TOOL_SCHEMAS["hydrate"],
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Dispatch an MCP tool call to the appropriate HotMem operation."""
        arguments = arguments or {}

        try:
            if name == "add_memory":
                return _handle_add_memory(_state, arguments)
            if name == "search_memories":
                return _handle_search_memories(_state, arguments)
            if name == "memory_health":
                return _handle_memory_health(_state, arguments)
            if name == "snapshot":
                return _handle_snapshot(_state, arguments)
            if name == "hydrate":
                return _handle_hydrate(_state, arguments)
        except KeyError as err:
            _trace.error("tool", f"missing required argument: {err}", detail={"tool": name})
            return _error(f"missing required argument: {err}")
        except ValueError as err:
            _trace.error("tool", f"invalid argument: {err}", detail={"tool": name})
            return _error(f"invalid argument: {err}")

        return _error(f"unknown tool: {name}")

    return server


async def run(db_path: str | Path, swap_path: str | Path | None = None) -> None:
    """Start the HotMem MCP server on stdio transport."""
    db_path = str(db_path)
    swap_path = str(swap_path) if swap_path else None

    db = MemoryDB(db_path)
    _ServerState.db = db
    _ServerState.db_path = db_path
    _ServerState.swap_path = swap_path
    _ServerState.start_time = time.time()

    if swap_path and Path(swap_path).exists():
        result = swap_hydrate(db, swap_path)
        _trace.info(
            "startup",
            f"auto-hydrated {result.loaded} memories",
            detail={"swap_path": swap_path},
        )

    _trace.info(
        "startup",
        "mcp server ready",
        detail={"db_path": db_path, "swap_path": swap_path},
    )

    server = create_server(db_path, swap_path)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        db.close()
        _trace.info("shutdown", "mcp server closed", detail={"db_path": db_path})


def _handle_add_memory(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Store a single fact in the database."""
    identifier = str(arguments["identifier"])
    fact = str(arguments["fact"])
    importance = float(arguments.get("importance", 0.5))
    ttl_seconds = arguments.get("ttl_seconds")
    if ttl_seconds is not None:
        ttl_seconds = int(ttl_seconds)

    with Timer() as t:
        memory_id = uuid.uuid4().hex
        content_hash = compute_content_hash(identifier, fact)
        vec = embed_text(fact)
        blob = pack_embedding(vec)

        state.db.insert(
            id=memory_id,
            identifier=identifier,
            fact_text=fact,
            embedding=blob,
            embedding_dim=EMBEDDING_DIM,
            embedding_model=EMBEDDING_MODEL,
            source="mcp",
            importance=importance,
            metadata_json="{}",
            content_hash=content_hash,
            ttl_seconds=ttl_seconds,
        )

    payload = {
        "memory_id": memory_id,
        "content_hash": content_hash,
        "trace_ms": round(t.ms, 2),
    }
    _trace.info("tool", "added memory", detail={"memory_id": memory_id})
    return _ok(payload)


def _handle_search_memories(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Search memories and return ranked message objects."""
    query = str(arguments["query"])
    top_k = int(arguments.get("top_k", 5))
    max_chars = arguments.get("max_chars")
    if max_chars is not None:
        max_chars = int(max_chars)

    with Timer() as t:
        messages = search_memories(state.db, query=query, top_k=top_k, max_chars=max_chars)

    payload = {
        "memories": messages,
        "count": len(messages),
        "trace_ms": round(t.ms, 2),
    }
    _trace.info("tool", "searched memories", detail={"count": len(messages)})
    return _ok(payload)


def _handle_memory_health(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Return memory count, uptime, and database path."""
    payload = {
        "status": "ok",
        "memory_count": state.db.count(),
        "db_path": state.db_path,
        "uptime_s": round(time.time() - state.start_time, 1),
    }
    _trace.info("tool", "health check", detail={"memory_count": payload["memory_count"]})
    return _ok(payload)


def _handle_snapshot(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Export all memories to a JSONL swap file."""
    swap = arguments.get("file") or state.swap_path or "swap.jsonl"
    result = swap_snapshot(state.db, swap)
    return _ok({"exported": result.exported, "path": result.path})


def _handle_hydrate(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Load memories from a JSONL swap file into the database."""
    swap = arguments.get("file") or state.swap_path or "swap.jsonl"
    result = swap_hydrate(state.db, swap)
    return _ok({"loaded": result.loaded, "skipped_dupes": result.skipped_dupes})


def _ok(payload: dict[str, Any]) -> CallToolResult:
    """Return a successful tool result as JSON text content."""
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])


def _error(message: str) -> CallToolResult:
    """Return a failed tool result as JSON text content with isError set."""
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps({"error": message}))],
    )
