"""HotMem MCP server — stdio transport for MCP-compatible hosts.

Purpose:
    Expose HotMem operations as MCP tools so Claude Desktop, Cursor, Warp,
    and other MCP clients can use a local HotMem instance.

Interface:
    create_server(db_path, swap_path?, embedder?) -> Server
    run(db_path, swap_path?, embedder?) -> coroutine — starts the stdio server

Tools:
    - add_memory(identifier, fact, importance?, ttl_seconds?)
    - search_memories(query, top_k?, max_chars?)
    - memory_health()
    - snapshot(file?)
    - hydrate(file?)
    - handoff_prepare(source, output, mode?, consent, session?)  (#101)
    - handoff_inspect(package)
    - handoff_verify(package)
    - handoff_hydrate(package)

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
from hotmem.embed import DEFAULT_EMBEDDER, Embedder, pack_embedding
from hotmem.rerank import Reranker
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
    "handoff_prepare": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source export directory."},
            "output": {"type": "string", "description": "Handoff package output directory."},
            "mode": {"type": "string", "enum": ["resume", "archive"]},
            "consent": {
                "type": "string",
                "description": "Explicit consent statement. Required; capture is never implicit.",
            },
            "session": {"type": "string", "description": "Required session id in the envelope."},
        },
        "required": ["source", "output", "consent"],
    },
    "handoff_inspect": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Handoff package directory."},
        },
        "required": ["package"],
    },
    "handoff_verify": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Handoff package directory."},
        },
        "required": ["package"],
    },
    "handoff_hydrate": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Handoff package directory."},
        },
        "required": ["package"],
    },
}


class _ServerState:
    """Mutable server state shared between lifespan and tool handlers."""

    db: MemoryDB
    db_path: str
    swap_path: str | None
    start_time: float
    embedder: Embedder = DEFAULT_EMBEDDER  # runtime-owned (issue #78)
    reranker: Reranker | None = None  # optional second stage (#80)


_state = _ServerState()


def create_server(
    db_path: str | Path,
    swap_path: str | Path | None = None,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> Server:
    """Create and configure the HotMem MCP server.

    ``embedder`` is the runtime-owned embedding implementation (issue #78);
    ``None`` means the hash default. ``reranker`` is the optional bounded
    second stage (issue #80); ``None`` preserves the first-stage ranking.
    """
    db_path = str(db_path)
    swap_path = str(swap_path) if swap_path else None
    _state.embedder = embedder if embedder is not None else DEFAULT_EMBEDDER
    _state.reranker = reranker

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
            Tool(
                name="handoff_prepare",
                description=(
                    "Prepare a hotmem-handoff-v1 package from a source export "
                    "(#101). Requires explicit consent; capture is never implicit."
                ),
                inputSchema=_TOOL_SCHEMAS["handoff_prepare"],
            ),
            Tool(
                name="handoff_inspect",
                description=(
                    "Read-only inspection of a handoff package: identity, counts, "
                    "coverage, omissions, redactions, or the failure reason."
                ),
                inputSchema=_TOOL_SCHEMAS["handoff_inspect"],
            ),
            Tool(
                name="handoff_verify",
                description="Fail-closed verification of a handoff package.",
                inputSchema=_TOOL_SCHEMAS["handoff_verify"],
            ),
            Tool(
                name="handoff_hydrate",
                description=(
                    "Hydrate a verified handoff package into this HotMem instance "
                    "(atomic, idempotent)."
                ),
                inputSchema=_TOOL_SCHEMAS["handoff_hydrate"],
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
            if name == "handoff_prepare":
                return _handle_handoff_prepare(_state, arguments)
            if name == "handoff_inspect":
                return _handle_handoff_inspect(_state, arguments)
            if name == "handoff_verify":
                return _handle_handoff_verify(_state, arguments)
            if name == "handoff_hydrate":
                return _handle_handoff_hydrate(_state, arguments)
        except KeyError as err:
            _trace.error("tool", f"missing required argument: {err}", detail={"tool": name})
            return _error(f"missing required argument: {err}")
        except ValueError as err:
            _trace.error("tool", f"invalid argument: {err}", detail={"tool": name})
            return _error(f"invalid argument: {err}")

        return _error(f"unknown tool: {name}")

    return server


async def run(
    db_path: str | Path,
    swap_path: str | Path | None = None,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> None:
    """Start the HotMem MCP server on stdio transport."""
    db_path = str(db_path)
    swap_path = str(swap_path) if swap_path else None

    db = MemoryDB(db_path)
    _ServerState.db = db
    _ServerState.db_path = db_path
    _ServerState.swap_path = swap_path
    _ServerState.start_time = time.time()

    if swap_path and Path(swap_path).exists():
        result = swap_hydrate(db, swap_path, embedder=embedder)
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

    server = create_server(db_path, swap_path, embedder=embedder, reranker=reranker)

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
        active = state.embedder
        memory_id = uuid.uuid4().hex
        content_hash = compute_content_hash(identifier, fact)
        vec = active.embed(fact)
        blob = pack_embedding(vec)

        state.db.insert(
            id=memory_id,
            identifier=identifier,
            fact_text=fact,
            embedding=blob,
            embedding_dim=active.descriptor.dimension,
            embedding_model=active.descriptor.key,
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
        messages = search_memories(
            state.db,
            query=query,
            top_k=top_k,
            max_chars=max_chars,
            embedder=state.embedder,
            reranker=state.reranker,
        )

    payload = {
        "memories": messages,
        "count": len(messages),
        "trace_ms": round(t.ms, 2),
    }
    _trace.info("tool", "searched memories", detail={"count": len(messages)})
    return _ok(payload)


def _handle_memory_health(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Return memory count, uptime, and database path."""
    descriptor = state.embedder.descriptor
    payload = {
        "status": "ok",
        "memory_count": state.db.count(),
        "db_path": state.db_path,
        "uptime_s": round(time.time() - state.start_time, 1),
        # Sanitized active embedding descriptor (issue #78; additive).
        "embedding": {"model": descriptor.key, "dim": descriptor.dimension},
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
    result = swap_hydrate(state.db, swap, embedder=state.embedder)
    return _ok(
        {
            "loaded": result.loaded,
            "skipped_dupes": result.skipped_dupes,
            "invalid": result.invalid,
            # Embedding + annotation dispositions (issues #78/#79; additive)
            # — one shared definition with HTTP and CLI.
            **result.disposition(),
        }
    )


def _handle_handoff_prepare(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Prepare a handoff package from a source export (#101).

    Consent is required and checked before any session content is read —
    an MCP host can never capture a session implicitly. Argument types are
    validated here because MCP hosts are not required to honour the
    declared inputSchema: coercing a missing/null consent to the string
    "None" would record a false consent statement.
    """
    from hotmem.handoff.codex_source import SourceError
    from hotmem.handoff.package import prepare_handoff

    consent = arguments["consent"]
    if not isinstance(consent, str) or not consent.strip():
        return _error(
            "explicit consent is required before any session content is read; "
            "pass a non-empty consent string"
        )
    source = arguments["source"]
    output = arguments["output"]
    for name, value in (("source", source), ("output", output)):
        if not isinstance(value, str) or not value.strip():
            return _error(f"{name} must be a non-empty path string")

    try:
        result = prepare_handoff(
            source,
            output,
            mode=str(arguments.get("mode") or "resume"),
            consent=consent,
            session_id=arguments.get("session"),
        )
    except (SourceError, ValueError) as err:
        return _error(str(err))
    coverage = result.coverage
    return _ok(
        {
            "handoff_id": result.handoff_id,
            "package_id": result.package_id,
            "mode": result.manifest["mode"],
            "entries": result.manifest["counts"]["entries"],
            "memories": result.manifest["counts"]["memories"],
            "omissions": coverage["omitted_count"],
            "redactions": coverage["redacted_count"],
            "path": result.path,
            "total_ms": result.timings_ms["total_ms"],
        }
    )


def _handle_handoff_inspect(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Read-only package inspection; reports failures instead of raising."""
    from hotmem.handoff.verify import inspect_handoff

    report = inspect_handoff(str(arguments["package"]))
    return _ok(report)


def _handle_handoff_verify(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Fail-closed verification: an invalid package is an MCP error result."""
    from hotmem.handoff.verify import HandoffError, verify_handoff

    try:
        verified = verify_handoff(str(arguments["package"]))
    except HandoffError as err:
        return _error(f"verification failed ({err.reason}): {err}")
    return _ok(
        {
            "valid": True,
            "package_id": verified.manifest["package_id"],
            "mode": verified.manifest["mode"],
            "entries": verified.entry_count,
            "memories": verified.memory_count,
        }
    )


def _handle_handoff_hydrate(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Hydrate a verified package into the server's database (atomic)."""
    from hotmem.handoff.hydrate import hydrate_handoff
    from hotmem.handoff.verify import HandoffError

    try:
        result = hydrate_handoff(state.db, str(arguments["package"]), embedder=state.embedder)
    except HandoffError as err:
        return _error(f"hydration not applied — verification failed ({err.reason}): {err}")
    return _ok(
        {
            "handoff_id": result.handoff_id,
            "package_id": result.package_id,
            "loaded": result.loaded,
            "skipped": result.skipped_dupes,
            "invalid": result.invalid,
            "already_applied": result.already_applied,
            "brief": result.brief_identifier,
            **result.disposition(),
        }
    )


def _ok(payload: dict[str, Any]) -> CallToolResult:
    """Return a successful tool result as JSON text content."""
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])


def _error(message: str) -> CallToolResult:
    """Return a failed tool result as JSON text content with isError set."""
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps({"error": message}))],
    )
