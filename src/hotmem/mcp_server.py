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
from hotmem.runtime import HotMemRuntime
from hotmem.search import search_memories
from hotmem.snapshot import hydrate as snapshot_hydrate
from hotmem.snapshot import snapshot as snapshot_export
from hotmem.swap import compute_content_hash
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
    state: _ServerState | None = None,
) -> Server:
    """Create and configure the HotMem MCP server.

    ``embedder`` is the runtime-owned embedding implementation (issue #78);
    ``None`` means the hash default. ``reranker`` is the optional bounded
    second stage (issue #80); ``None`` preserves the first-stage ranking.

    ``state`` is the explicit state container owned by the caller. Production
    startup (`run`) passes a runtime-backed instance; the module-level
    ``_state`` fallback exists only for direct handler tests and is never
    used by the serving path.
    """
    db_path = str(db_path)
    swap_path = str(swap_path) if swap_path else None
    active_state = state if state is not None else _state
    active_state.embedder = embedder if embedder is not None else DEFAULT_EMBEDDER
    active_state.reranker = reranker

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
                description=(
                    "Export memories to a verified Snapshot v2 directory or JSONL/JSONL.GZ."
                ),
                inputSchema=_TOOL_SCHEMAS["snapshot"],
            ),
            Tool(
                name="hydrate",
                description="Restore a verified snapshot directory or JSONL/JSONL.GZ into HotMem.",
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
                return _handle_add_memory(active_state, arguments)
            if name == "search_memories":
                return _handle_search_memories(active_state, arguments)
            if name == "memory_health":
                return _handle_memory_health(active_state, arguments)
            if name == "snapshot":
                return _handle_snapshot(active_state, arguments)
            if name == "hydrate":
                return _handle_hydrate(active_state, arguments)
            if name == "handoff_prepare":
                return _handle_handoff_prepare(active_state, arguments)
            if name == "handoff_inspect":
                return _handle_handoff_inspect(active_state, arguments)
            if name == "handoff_verify":
                return _handle_handoff_verify(active_state, arguments)
            if name == "handoff_hydrate":
                return _handle_handoff_hydrate(active_state, arguments)
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
    """Start the HotMem MCP server on stdio transport (embedded compatibility mode)."""
    db_path = str(db_path)
    swap_path = str(swap_path) if swap_path else None

    # Embedded mode still owns exactly one runtime: this acquires the
    # exclusive store lock, runs the same startup hydration, and is the only
    # writer for the life of the stdio session (#109).
    runtime = HotMemRuntime(
        db_path=db_path,
        swap_path=swap_path,
        embedder=embedder,
        reranker=reranker,
    ).open()
    state = _ServerState()
    state.db = runtime.db  # type: ignore[assignment]
    state.db_path = db_path
    state.swap_path = swap_path
    state.start_time = runtime.start_time
    state.embedder = runtime.embedder
    state.reranker = runtime.reranker

    _trace.info(
        "startup",
        "mcp server ready",
        detail={"db_path": db_path, "swap_path": swap_path},
    )

    server = create_server(
        db_path,
        swap_path,
        embedder=embedder,
        reranker=reranker,
        state=state,
    )

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        runtime.close()
        _trace.info("shutdown", "mcp server closed", detail={"db_path": db_path})


def _build_tool_list() -> list[Tool]:
    """Return the canonical MCP tool list shared by embedded and daemon relay servers."""
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


def _format_http_error(err: Exception) -> str:
    """Format an httpx transport or status error into an actionable MCP error string."""
    import httpx

    if isinstance(err, httpx.HTTPStatusError):
        resp = err.response
        try:
            body = resp.json()
            if isinstance(body, dict):
                code = body.get("error") or f"http_{resp.status_code}"
                msg = body.get("message") or body.get("detail") or resp.text
                reason = body.get("reason")
                if reason:
                    return f"{code} ({reason}): {msg}"
                return f"{code}: {msg}"
        except Exception:
            pass
        return f"HTTP {resp.status_code}: {resp.text}"
    if isinstance(err, httpx.RequestError):
        return f"daemon_unreachable: {err}"
    return str(err)


def create_relay_server(
    base_url: str = "http://127.0.0.1:8711",
    *,
    token: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    expected_store_id: str | None = None,
    client: Any | None = None,
) -> tuple[Server, Any]:
    """Create a thin MCP stdio-to-daemon relay server (#109).

    Uses a persistent `AsyncHotMemClient` so connections are pooled across tool
    invocations and never opens a local or temporary SQLite database.
    Returns `(server, async_client)`.
    """
    from hotmem.client import AsyncHotMemClient

    owned_client = (
        client
        if client is not None
        else AsyncHotMemClient(
            base_url=base_url,
            token=token,
            session_id=session_id,
            source=source or "mcp",
            namespace=namespace,
            expected_store_id=expected_store_id,
        )
    )
    server = Server("hotmem")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _build_tool_list()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        arguments = arguments or {}
        try:
            if name == "add_memory":
                identifier = str(arguments["identifier"])
                fact = str(arguments["fact"])
                importance = float(arguments.get("importance", 0.5))
                ttl_seconds = arguments.get("ttl_seconds")
                if ttl_seconds is not None:
                    ttl_seconds = int(ttl_seconds)
                res = await owned_client.add(
                    identifier,
                    fact,
                    source=source or "mcp",
                    importance=importance,
                    ttl_seconds=ttl_seconds,
                )
                return _ok(res)

            if name == "search_memories":
                query = str(arguments["query"])
                top_k = int(arguments.get("top_k", 5))
                max_chars = arguments.get("max_chars")
                if max_chars is not None:
                    max_chars = int(max_chars)
                payload: dict[str, Any] = {"query": query, "top_k": top_k}
                if max_chars is not None:
                    payload["max_chars"] = max_chars
                resp = await owned_client._client.post("/v1/search", json=payload)
                resp.raise_for_status()
                return _ok(resp.json())

            if name == "memory_health":
                res = await owned_client.health()
                return _ok(res)

            if name == "snapshot":
                res = await owned_client.snapshot(arguments.get("file"))
                return _ok(res)

            if name == "hydrate":
                res = await owned_client.hydrate(arguments.get("file"))
                # Keep MCP hydrate output shape consistent with embedded handler
                out = dict(res)
                out.pop("path", None)
                return _ok(out)

            if name == "handoff_prepare":
                consent = arguments.get("consent")
                if not isinstance(consent, str) or not consent.strip():
                    return _error(
                        "explicit consent is required before any session content is read; "
                        "pass a non-empty consent string"
                    )
                src = arguments.get("source")
                output = arguments.get("output")
                for field_name, val in (("source", src), ("output", output)):
                    if not isinstance(val, str) or not val.strip():
                        return _error(f"{field_name} must be a non-empty path string")
                res = await owned_client.handoff_prepare(
                    src,
                    output,
                    consent=consent,
                    mode=str(arguments.get("mode") or "resume"),
                    session=arguments.get("session"),
                )
                cov = res.get("coverage") or {}
                timings = res.get("timings_ms") or {}
                return _ok(
                    {
                        "handoff_id": res["handoff_id"],
                        "package_id": res["package_id"],
                        "mode": res["mode"],
                        "entries": res["entries"],
                        "memories": res["memories"],
                        "omissions": cov.get("omitted_count", 0),
                        "redactions": cov.get("redacted_count", 0),
                        "path": res["path"],
                        "total_ms": timings.get("total_ms", 0.0),
                    }
                )

            if name == "handoff_inspect":
                res = await owned_client.handoff_inspect(str(arguments["package"]))
                return _ok(res)

            if name == "handoff_verify":
                res = await owned_client.handoff_verify(str(arguments["package"]))
                return _ok(res)

            if name == "handoff_hydrate":
                res = await owned_client.handoff_hydrate(str(arguments["package"]))
                return _ok(res)

        except KeyError as err:
            return _error(f"missing required argument: {err}")
        except ValueError as err:
            return _error(f"invalid argument: {err}")
        except Exception as err:
            return _error(_format_http_error(err))

        return _error(f"unknown tool: {name}")

    return server, owned_client


async def run_relay(
    base_url: str = "http://127.0.0.1:8711",
    *,
    token: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    expected_store_id: str | None = None,
) -> None:
    """Start the thin MCP stdio relay connected to a running HotMem daemon."""
    server, client = create_relay_server(
        base_url=base_url,
        token=token,
        session_id=session_id,
        source=source,
        namespace=namespace,
        expected_store_id=expected_store_id,
    )
    _trace.info(
        "startup",
        "mcp relay ready",
        detail={"url": base_url, "expected_store_id": expected_store_id},
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await client.close()
        _trace.info("shutdown", "mcp relay closed", detail={"url": base_url})


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
    """Export through the shared CLI/HTTP snapshot format dispatch."""
    swap = arguments.get("file") or state.swap_path or "swap.jsonl"
    result = snapshot_export(state.db, swap)
    return _ok({"exported": result.exported, "path": result.path})


def _handle_hydrate(state: _ServerState, arguments: dict[str, Any]) -> CallToolResult:
    """Restore through the shared verify-before-hydrate format dispatch."""
    swap = arguments.get("file") or state.swap_path or "swap.jsonl"
    result = snapshot_hydrate(state.db, swap, embedder=state.embedder)
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
