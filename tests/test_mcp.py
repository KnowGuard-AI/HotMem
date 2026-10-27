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
    _handle_list_memories,
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


def test_mcp_add_memory_forwards_metadata_tags_and_context(tmp_path: Path):
    """WP3 MVT: MCP add_memory advertises & forwards metadata, tags, harness_id, session_id."""
    import httpx
    from fastapi.testclient import TestClient
    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    from hotmem.client import AsyncHotMemClient
    from hotmem.mcp_server import create_relay_server
    from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
    from hotmem.server import create_app

    # 1. Schema check
    srv = create_server(":memory:")
    tools_res = asyncio.run(
        srv.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    )
    add_tool = next(t for t in tools_res.root.tools if t.name == "add_memory")
    props = add_tool.inputSchema["properties"]
    assert {
        "identifier",
        "fact",
        "importance",
        "ttl_seconds",
        "metadata",
        "tags",
        "source",
        "namespace",
        "session_id",
        "harness_id",
        "idempotency_key",
    } <= set(props)
    assert add_tool.inputSchema["required"] == ["identifier", "fact"]

    # 2. Embedded handler persists metadata, tags, and explicit session/harness context
    emb_db = MemoryDB(tmp_path / "emb_111.sqlite")
    emb_state = _ServerState()
    emb_state.db = emb_db
    emb_state.db_path = str(tmp_path / "emb_111.sqlite")
    emb_state.swap_path = None
    emb_state.start_time = time.time()
    try:
        emb_srv = create_server(emb_state.db_path, state=emb_state)
        call_req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="add_memory",
                arguments={
                    "identifier": "mcp/emb-111",
                    "fact": "Embedded MCP fact with metadata and tags",
                    "metadata": {"ticket": "111", "stage": "wp3"},
                    "tags": ["model:claude-sonnet-4", "project:hotmem", "org:knowguard"],
                    "harness_id": "claude-code",
                    "session_id": "sess-emb-111",
                    "source": "mcp://claude-embedded",
                },
            ),
        )
        call_res = asyncio.run(emb_srv.request_handlers[CallToolRequest](call_req))
        assert not call_res.root.isError
        emb_mid = _text(call_res.root)["memory_id"]
        stored = emb_db.get_memory(emb_mid)
        assert stored is not None
        assert json.loads(stored["metadata_json"]) == {"ticket": "111", "stage": "wp3"}
        assert json.loads(stored["tags"]) == [
            "model:claude-sonnet-4",
            "project:hotmem",
            "org:knowguard",
        ]
        prov = json.loads(stored["provenance_json"])
        assert prov["harness_id"] == "claude-code"
        assert prov["session_id"] == "sess-emb-111"
        assert prov["source"] == "mcp://claude-embedded"

        # Embedded MCP duplicate write preserves first writer attribution & tags,
        # and idempotency_key conflict is detected in embedded mode.
        dup_req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="add_memory",
                arguments={
                    "identifier": "mcp/emb-111",
                    "fact": "Embedded MCP fact with metadata and tags",
                    "tags": ["model:other"],
                    "harness_id": "codex",
                    "session_id": "sess-emb-222",
                    "idempotency_key": "emb-idem-1",
                },
            ),
        )
        dup_res = asyncio.run(emb_srv.request_handlers[CallToolRequest](dup_req))
        assert not dup_res.root.isError
        assert _text(dup_res.root)["memory_id"] == emb_mid
        assert _text(dup_res.root)["deduplicated"] is True
        stored_after = emb_db.get_memory(emb_mid)
        assert json.loads(stored_after["tags"]) == [
            "model:claude-sonnet-4",
            "project:hotmem",
            "org:knowguard",
        ]
        assert json.loads(stored_after["provenance_json"])["harness_id"] == "claude-code"

        conflict_emb = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="add_memory",
                arguments={
                    "identifier": "mcp/emb-111",
                    "fact": "Different fact under same embedded idempotency key",
                    "idempotency_key": "emb-idem-1",
                },
            ),
        )
        conflict_emb_res = asyncio.run(emb_srv.request_handlers[CallToolRequest](conflict_emb))
        assert conflict_emb_res.root.isError
        assert "idempotency_key_conflict" in conflict_emb_res.root.content[0].text

        # Malformed tags / metadata / context in embedded MCP fail cleanly with isError=True
        for bad_args in (
            {"identifier": "mcp/bad", "fact": "x", "tags": [""]},
            {"identifier": "mcp/bad", "fact": "x", "metadata": "not-an-object"},
            {"identifier": "mcp/bad", "fact": "x", "harness_id": "   "},
        ):
            bad_req = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="add_memory", arguments=bad_args),
            )
            bad_res = asyncio.run(emb_srv.request_handlers[CallToolRequest](bad_req))
            assert bad_res.root.isError
    finally:
        emb_db.close()

    # 3. Daemon relay forwards metadata, tags, harness_id, session_id to daemon runtime
    policy = PolicyConfig(
        store_id="store-mcp-111",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-mcp-a",
                caller_id="caller-mcp-a",
                store_id="store-mcp-111",
                namespaces=("team/shared",),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-mcp-b",
                caller_id="caller-mcp-b",
                store_id="store-mcp-111",
                namespaces=("b/private",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-mcp-111",
                        namespace="team/shared",
                        audience=("caller-mcp-b",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=tmp_path / "daemon_mcp_111.sqlite", policy=policy)
    with TestClient(app) as tc:

        async def run_relay_check():
            transport = httpx.ASGITransport(app=app)
            relay_client = AsyncHotMemClient(
                "http://testserver",
                token="tok-mcp-a",
                expected_store_id="store-mcp-111",
                harness_id="codex",
                session_id="sess-relay-1",
                source="mcp://codex-relay",
                namespace="team/shared",
            )
            orig_headers = dict(relay_client._client.headers)
            await relay_client._client.aclose()
            relay_client._client = httpx.AsyncClient(
                base_url="http://testserver",
                transport=transport,
                headers=orig_headers,
            )
            relay_srv, _ = create_relay_server(
                "http://testserver",
                token="tok-mcp-a",
                session_id="sess-relay-1",
                harness_id="codex",
                source="mcp://codex-relay",
                namespace="team/shared",
                expected_store_id="store-mcp-111",
                client=relay_client,
            )
            try:
                req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="add_memory",
                        arguments={
                            "identifier": "ops/mcp-relay-111",
                            "fact": "Relay fact with metadata and tags",
                            "metadata": {"component": "relay", "ticket": 111},
                            "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
                        },
                    ),
                )
                res = await relay_srv.request_handlers[CallToolRequest](req)
                assert not res.root.isError, res.root.content[0].text
                mid = _text(res.root)["memory_id"]

                list_req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="list_memories",
                        arguments={
                            "identifier": "ops/mcp-relay-111",
                            "order": "desc",
                            "limit": 1,
                        },
                    ),
                )
                list_res = await relay_srv.request_handlers[CallToolRequest](list_req)
                assert not list_res.root.isError, list_res.root.content[0].text
                listed = _text(list_res.root)
                assert listed["count"] == 1
                assert listed["memories"][0]["id"] == mid
                assert listed["memories"][0]["provenance"]["session_id"] == "sess-relay-1"

                # Conflicting session_id in tool call vs relay startup session_id fails clearly
                conflict_req = CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="add_memory",
                        arguments={
                            "identifier": "ops/mcp-relay-conflict",
                            "fact": "Conflicting session id",
                            "session_id": "sess-different",
                        },
                    ),
                )
                conflict_res = await relay_srv.request_handlers[CallToolRequest](conflict_req)
                assert conflict_res.root.isError
                assert "context_conflict" in conflict_res.root.content[0].text
                return mid
            finally:
                await relay_client.close()

        mid = asyncio.run(run_relay_check())

        # Reader B verifies metadata, tags, harness_id, and session_id on the daemon
        got = tc.get(
            f"/v1/memory/{mid}",
            headers={
                "Authorization": "Bearer tok-mcp-b",
                "X-HotMem-Store-Id": "store-mcp-111",
                "X-HotMem-Session-Id": "sess-b-1",
                "X-HotMem-Harness-Id": "claude-code",
            },
        )
        assert got.status_code == 200
        body = got.json()
        assert body["metadata"] == {"component": "relay", "ticket": 111}
        assert body["tags"] == ["model:gpt-5", "project:hotmem", "org:knowguard"]
        assert body["provenance"]["harness_id"] == "codex"
        assert body["provenance"]["session_id"] == "sess-relay-1"
        assert body["provenance"]["caller_id"] == "caller-mcp-a"


def test_add_memory_missing_required_argument_errors(state: _ServerState):
    with pytest.raises(KeyError):
        _handle_add_memory(state, {"identifier": "v"})


def test_list_memories_exact_identifier_order_and_validation(state: _ServerState):
    for identifier, fact in (
        ("tasks/current", "pending"),
        ("tasks/other", "unrelated"),
        ("tasks/current", "completed"),
    ):
        _handle_add_memory(state, {"identifier": identifier, "fact": fact})

    payload = _text(
        _handle_list_memories(
            state,
            {"identifier": "tasks/current", "order": "desc", "limit": 1},
        )
    )
    assert payload["count"] == 1
    assert [row["fact_text"] for row in payload["memories"]] == ["completed"]
    assert all(row["identifier"] == "tasks/current" for row in payload["memories"])

    with pytest.raises(ValueError, match="order"):
        _handle_list_memories(state, {"identifier": "tasks/current", "order": "newest"})
    with pytest.raises(ValueError, match="limit"):
        _handle_list_memories(state, {"identifier": "tasks/current", "limit": 0})
    with pytest.raises(ValueError, match="identifier"):
        _handle_list_memories(state, {})


def test_list_memories_embedded_namespace_and_relay_disclosure(state: _ServerState):
    _handle_add_memory(
        state,
        {"identifier": "shared/name", "fact": "namespace A", "namespace": "tenant-a"},
    )
    _handle_add_memory(
        state,
        {"identifier": "shared/name", "fact": "namespace B", "namespace": "tenant-b"},
    )
    state.namespace = "tenant-a"
    payload = _text(_handle_list_memories(state, {"identifier": "shared/name"}))
    assert payload["count"] == 1
    assert payload["memories"][0]["fact_text"] == "namespace A"

    class _RelayListClient:
        async def list_with_disclosure(self, identifier, *, order, limit):
            assert identifier == "tasks/current"
            assert order == "desc"
            assert limit == 1
            return {
                "memories": [{"id": "m1", "identifier": identifier}],
                "count": 1,
                "disclosure": {"omitted_count": 1, "redacted_count": 0},
                "trace_ms": 1.5,
            }

    from mcp.types import CallToolRequest, CallToolRequestParams

    from hotmem.mcp_server import create_relay_server

    relay, _client = create_relay_server(client=_RelayListClient())
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="list_memories",
            arguments={"identifier": "tasks/current", "order": "desc", "limit": 1},
        ),
    )
    response = asyncio.run(relay.request_handlers[CallToolRequest](req))
    assert not response.root.isError
    relay_payload = _text(response.root)
    assert relay_payload["disclosure"]["omitted_count"] == 1
    assert relay_payload["trace_ms"] == 1.5


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
        # Disposition parity with the HTTP surface (issues #78/#79): the
        # one shared definition reports embedding AND annotation counters.
        assert loaded["embedding_reused"] == 1
        assert loaded["annotations_merged"] == 0
        assert loaded["annotation_conflicts"] == 0
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
    # Session handoff surface added additively (#101).
    assert names == {
        "add_memory",
        "search_memories",
        "list_memories",
        "memory_health",
        "snapshot",
        "hydrate",
        "handoff_prepare",
        "handoff_inspect",
        "handoff_verify",
        "handoff_hydrate",
    }


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


@pytest.mark.parametrize("suffix", ["", ".jsonl", ".jsonl.gz"])
def test_installed_mcp_stdio_session_restore_and_cleanup(tmp_path: Path, suffix: str):
    """Actual stdio framing, optional-extra entry point, EOF, and clean restore."""
    import shutil
    import sys
    from contextlib import asynccontextmanager
    from datetime import timedelta

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    executable = shutil.which("hotmem", path=str(Path(sys.executable).parent))
    assert executable
    root = tmp_path / "mcp caf\u00e9 with spaces"
    root.mkdir()
    snapshot = root / f"verified snapshot{suffix}"

    @asynccontextmanager
    async def connected(mount: Path, log):
        params = StdioServerParameters(
            command=executable,
            args=["mcp", "--mount", str(mount), "--embedder", "hash", "--reranker", "none"],
        )
        async with (
            stdio_client(params, errlog=log) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session,
        ):
            await session.initialize()
            yield session

    async def call(session, name, arguments):
        result = await session.call_tool(name, arguments)
        assert not result.isError, result
        return _text(result)

    async def exercise():
        with (root / "stderr.log").open("w", encoding="utf-8") as log:
            async with connected(root / "source", log) as session:
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert {"add_memory", "search_memories", "snapshot", "hydrate"} <= tools
                fact = "MCP caf\u00e9 handoff\nsecond line"
                added = await call(session, "add_memory", {"identifier": "mcp-test", "fact": fact})
                assert added["memory_id"]
                assert (await call(session, "memory_health", {}))["memory_count"] == 1
                before = await call(session, "search_memories", {"query": "MCP caf\u00e9"})
                assert before["memories"][0]["content"] == fact
                assert (await call(session, "snapshot", {"file": str(snapshot)}))["exported"] == 1
            # Closing stdio sends EOF; the server must close SQLite and exit.
            async with connected(root / "source", log) as session:
                assert (await call(session, "memory_health", {}))["memory_count"] == 1
            async with connected(root / "clone", log) as session:
                loaded = await call(session, "hydrate", {"file": str(snapshot)})
                assert loaded["loaded"] == loaded["embedding_reused"] == 1
                assert loaded["embedding_rebuilt"] == 0
                replay = await call(session, "hydrate", {"file": str(snapshot)})
                assert replay["loaded"] == 0 and replay["skipped_dupes"] == 1
                if not suffix:
                    with (snapshot / "memories.jsonl").open("ab") as handle:
                        handle.write(b"corruption")
                    corrupt = await session.call_tool("hydrate", {"file": str(snapshot)})
                    assert corrupt.isError
                    assert "mismatch" in corrupt.content[0].text
                invalid = await session.call_tool("add_memory", {"identifier": "missing-fact"})
                assert invalid.isError
                assert (await call(session, "memory_health", {}))["memory_count"] == 1

    asyncio.run(asyncio.wait_for(exercise(), timeout=45))
    stderr = (root / "stderr.log").read_text(encoding="utf-8")
    assert "mcp server ready" in stderr
    assert stderr.count("mcp server closed") == 3
    shutil.rmtree(root)
    assert not root.exists()
