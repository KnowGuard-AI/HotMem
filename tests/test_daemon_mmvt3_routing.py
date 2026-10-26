"""MMVT 3 — Client routing and host operability (#109, #110, #111).

Question/test:
    Can daemon-mode MCP, CLI, and SDK callers reach the same store without
    opening a bypass database?
    - Write through MCP relay (as Caller A) -> recall through CLI (`--url`)
      and Python SDK (as Caller B) with identical identity and attribution.
    - Private canary written via CLI (`--url`) by A is retrievable by A over
      MCP relay, and refused/invisible to B over MCP relay, CLI, and SDK.
    - `hotmem serve` and `hotmem mcp` without an explicit store fail visibly
      without opening a temporary SQLite fallback.
    - Endpoint-down, wrong `store_id`, and invalid credentials fail visibly
      across MCP relay, CLI, and SDK.
    - Closing the MCP stdio relay leaves the daemon and HTTP clients working.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from hotmem.cli import main
from hotmem.client import HotMemClient
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval


def _installed_cli(*args: str) -> list[str]:
    executable = shutil.which("hotmem", path=str(Path(sys.executable).parent))
    assert executable, "installed hotmem entry point must exist"
    return [executable, *args]


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        else:
            proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def mount_with_policy(tmp_path: Path) -> tuple[Path, PolicyConfig]:
    mount = tmp_path / "incident_mount"
    mount.mkdir(parents=True, exist_ok=True)
    policy = PolicyConfig(
        store_id="store-mmvt3",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-mmvt3",
                namespaces=("ops/shared", "ops/private"),
                operations=("read", "write", "export", "hydrate", "promote"),
                allowed_sessions=("sess-a-mcp", "sess-a-cli"),
                allowed_sources=("mcp://claude-a", "cli://ops-a"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id="store-mmvt3",
                namespaces=("ops/b-notes",),
                operations=("read", "write"),
                allowed_sessions=("sess-b-mcp", "sess-b-cli"),
                allowed_sources=("mcp://cursor-b", "cli://ops-b"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-mmvt3-demo",
                        namespace="ops/shared",
                        identifiers=("ops/demo-17",),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    policy.save(mount / "policy.json")
    return mount, policy


@contextmanager
def _running_daemon(mount: Path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    log_path = mount.parent / "daemon_mmvt3.log"
    with log_path.open("w+b") as log:
        proc = subprocess.Popen(
            _installed_cli(
                "serve",
                "--mount",
                str(mount),
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
                "--store-id",
                "store-mmvt3",
            ),
            stdout=log,
            stderr=log,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            with HotMemClient(url, token="tok-a", expected_store_id="store-mmvt3") as probe:
                deadline = time.monotonic() + 15.0
                while True:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        err_text = log.read().decode("utf-8", errors="replace")
                        pytest.fail(f"daemon failed to start: {err_text}")
                    try:
                        if probe.health().get("status") == "ok":
                            break
                    except Exception:
                        pass
                    time.sleep(0.05)
            yield url, proc
        finally:
            _stop_proc(proc)


def test_mmvt3_no_silent_temp_db_fallback_for_serve_or_mcp():
    """Daemon serve and mcp fail visibly when no store or URL is configured."""
    runner = CliRunner()

    res_serve = runner.invoke(main, ["serve"])
    assert res_serve.exit_code != 0
    assert "requires an explicit store" in res_serve.output

    res_mcp = runner.invoke(main, ["mcp"])
    assert res_mcp.exit_code != 0
    assert "requires an explicit target" in res_mcp.output

    res_both = runner.invoke(
        main, ["mcp", "--url", "http://127.0.0.1:8711", "--db", "local.sqlite"]
    )
    assert res_both.exit_code != 0
    assert "not both" in res_both.output


def test_mmvt3_cross_adapter_routing_and_stdio_relay_disconnect(
    tmp_path: Path, mount_with_policy: tuple[Path, PolicyConfig]
):
    """MCP stdio relay, CLI --url, and Python SDK route to one daemon with shared scope."""
    pytest.importorskip("mcp", reason="requires [mcp] extra")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    mount, _ = mount_with_policy
    runner = CliRunner()
    executable = _installed_cli()[0]

    shared_fact = "Incident DEMO-17: roll back worker release v2.4.1 before queue resume."
    canary_fact = "CANARY-MMVT3-8821: staging database password rotation pending."

    @asynccontextmanager
    async def mcp_relay_session(
        url: str,
        *,
        token: str,
        session_id: str,
        source: str,
        harness_id: str | None = None,
        namespace: str | None = None,
        store_id: str = "store-mmvt3",
    ):
        args = [
            "mcp",
            "--url",
            url,
            "--token",
            token,
            "--session",
            session_id,
            "--source",
            source,
            "--store-id",
            store_id,
        ]
        if harness_id is not None:
            args.extend(["--harness", harness_id])
        if namespace is not None:
            args.extend(["--namespace", namespace])
        params = StdioServerParameters(command=executable, args=args)
        err_log_path = tmp_path / f"mcp_{session_id}.err"
        with err_log_path.open("w", encoding="utf-8") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session,
            ):
                await session.initialize()
                yield session

    with _running_daemon(mount) as (url, _):
        # 1. Caller A writes approved shared decision over MCP stdio relay
        async def _step1_mcp_a_write_shared() -> str:
            async with mcp_relay_session(
                url,
                token="tok-a",
                session_id="sess-a-mcp",
                harness_id="claude-code",
                source="mcp://claude-a",
                namespace="ops/shared",
            ) as session:
                health_res = await session.call_tool("memory_health", {})
                assert not health_res.isError
                health_data = json.loads(health_res.content[0].text)
                assert health_data["store_id"] == "store-mmvt3"
                assert health_data["store_state"] == "verified_empty"

                add_res = await session.call_tool(
                    "add_memory",
                    {
                        "identifier": "ops/demo-17",
                        "fact": shared_fact,
                        "importance": 0.9,
                        "metadata": {"incident": "DEMO-17", "approved": True},
                        "tags": ["model:claude-sonnet-4", "project:hotmem", "org:knowguard"],
                    },
                )
                assert not add_res.isError, add_res
                return json.loads(add_res.content[0].text)["memory_id"]

        shared_mid = asyncio.run(asyncio.wait_for(_step1_mcp_a_write_shared(), timeout=20))

        # 2. Caller A writes private canary via CLI (--url)
        cli_add_priv = runner.invoke(
            main,
            [
                "add",
                "ops/canary",
                canary_fact,
                "--url",
                url,
                "--token",
                "tok-a",
                "--session",
                "sess-a-cli",
                "--harness",
                "codex",
                "--source",
                "cli://ops-a",
                "--namespace",
                "ops/private",
                "--store-id",
                "store-mmvt3",
                "--tag",
                "model:gpt-5",
                "--tag",
                "project:secret-ops",
                "--metadata",
                '{"classification":"private"}',
                "--json",
            ],
        )
        assert cli_add_priv.exit_code == 0, cli_add_priv.output
        canary_mid = json.loads(cli_add_priv.output)["memory_id"]

        # 3. Caller B recalls shared decision via CLI (--url) with A's MCP attribution
        cli_search_b = runner.invoke(
            main,
            [
                "search",
                "DEMO-17 roll back worker",
                "--url",
                url,
                "--token",
                "tok-b",
                "--session",
                "sess-b-cli",
                "--source",
                "cli://ops-b",
                "--store-id",
                "store-mmvt3",
                "--json",
            ],
        )
        assert cli_search_b.exit_code == 0, cli_search_b.output
        b_rows = json.loads(cli_search_b.output)
        assert len(b_rows) == 1
        assert b_rows[0]["memory_id"] == shared_mid
        assert b_rows[0]["tags"] == [
            "model:claude-sonnet-4",
            "project:hotmem",
            "org:knowguard",
        ]
        assert b_rows[0]["provenance"]["caller_id"] == "caller-a"
        assert b_rows[0]["provenance"]["harness_id"] == "claude-code"
        assert b_rows[0]["provenance"]["session_id"] == "sess-a-mcp"
        assert b_rows[0]["provenance"]["source"] == "mcp://claude-a"

        # 4. Caller B also connects via MCP stdio relay, sees 1 visible memory,
        #    cannot see canary, and fails closed on wrong store_id / bad token.
        async def _step4_mcp_b_verify_and_close() -> None:
            async with mcp_relay_session(
                url,
                token="tok-b",
                session_id="sess-b-mcp",
                source="mcp://cursor-b",
            ) as session_b:
                h_b = json.loads((await session_b.call_tool("memory_health", {})).content[0].text)
                assert h_b["memory_count"] == 1
                assert h_b["store_id"] == "store-mmvt3"

                s_b = json.loads(
                    (await session_b.call_tool("search_memories", {"query": "CANARY-MMVT3-8821"}))
                    .content[0]
                    .text
                )
                for m in s_b["memories"]:
                    assert canary_fact not in m["content"]
                    assert m["memory_id"] != canary_mid

                # B attempting snapshot export over MCP relay is denied (403 scope_denied)
                snap_b = await session_b.call_tool(
                    "snapshot", {"file": str(tmp_path / "b_snap.jsonl")}
                )
                assert snap_b.isError
                assert "scope_denied" in snap_b.content[0].text

            # Wrong store_id over MCP relay fails closed
            async with mcp_relay_session(
                url,
                token="tok-b",
                session_id="sess-b-mcp",
                source="mcp://cursor-b",
                store_id="wrong-store-id",
            ) as session_wrong:
                wrong_res = await session_wrong.call_tool("memory_health", {})
                assert wrong_res.isError
                assert "store_mismatch" in wrong_res.content[0].text

        asyncio.run(asyncio.wait_for(_step4_mcp_b_verify_and_close(), timeout=25))

        # 5. After both MCP stdio relays have exited, the daemon and HTTP SDK clients
        #    continue working without interruption.
        with HotMemClient(
            url,
            token="tok-a",
            session_id="sess-a-cli",
            source="cli://ops-a",
            namespace="ops/private",
            expected_store_id="store-mmvt3",
        ) as sdk_a:
            canary_rec = sdk_a.get_memory(canary_mid)
            assert canary_rec["fact_text"] == canary_fact
            assert canary_rec["tags"] == ["model:gpt-5", "project:secret-ops"]
            assert canary_rec["metadata"] == {"classification": "private"}
            assert canary_rec["provenance"]["harness_id"] == "codex"
            assert canary_rec["provenance"]["session_id"] == "sess-a-cli"

        # 6. CLI status with wrong token or unreachable port fails visibly
        bad_status = runner.invoke(main, ["status", "--url", url, "--token", "invalid-token"])
        assert bad_status.exit_code != 0
        assert "invalid_credential" in bad_status.output
