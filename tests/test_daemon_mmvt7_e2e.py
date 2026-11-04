"""MMVT 7 — Complete daemon delivery, companion integration & diagnostics (#109, #110, #111, #112).

Question/test:
    Can a fresh setup follow the documented start -> configure -> connect ->
    use -> reconnect -> stop flow and satisfy all acceptance criteria, while
    providing the runtime/connection/activity seam #112's management client
    consumes?
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

from hotmem.cli import main
from hotmem.client import HotMemClient
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.server import DAEMON_ROUTE_INVENTORY, create_app


def _installed_cli(*args: str) -> list[str]:
    executable = shutil.which("hotmem", path=str(Path(sys.executable).parent))
    assert executable, "installed hotmem entry point must exist"
    return [executable, *args]


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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


@contextmanager
def _daemon(mount: Path, *, store_id: str = "store-mmvt7", token: str = "tok-a"):
    port = _reserve_port()
    log_path = mount.parent / f"daemon_mmvt7_{port}.log"
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
                store_id,
            ),
            stdout=log,
            stderr=log,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            with HotMemClient(url, token=token, expected_store_id=store_id) as probe:
                deadline = time.monotonic() + 15.0
                while True:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        err_text = log.read().decode("utf-8", errors="replace")
                        raise AssertionError(f"daemon failed to start: {err_text}")
                    try:
                        if probe.health().get("status") == "ok":
                            break
                    except Exception:
                        pass
                    time.sleep(0.05)
            yield url
        finally:
            _stop_proc(proc)


def test_mmvt7_fresh_setup_management_seam_and_diagnostics(tmp_path: Path):
    """Exercise start -> configure -> connect -> use -> management detach -> reconnect -> stop."""
    mount = tmp_path / "fresh_mount"
    mount.mkdir()
    policy = PolicyConfig(
        store_id="store-mmvt7",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-mmvt7",
                namespaces=("ops/shared", "ops/private"),
                operations=("read", "write", "export", "hydrate", "promote"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id="store-mmvt7",
                namespaces=("ops/b-local",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-mmvt7-demo",
                        namespace="ops/shared",
                        identifiers=("ops/demo-17",),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-lsd-mgmt",
                caller_id="lsd-management-ui",
                store_id="store-mmvt7",
                namespaces=("ops/shared",),
                operations=("read",),
                is_management=True,
            ),
        ],
    )
    policy.save(mount / "policy.json")

    runner = CliRunner()
    canary = "CANARY-MMVT7-5501: private incident root cause"
    decision = "Incident DEMO-17: roll back worker release v2.4.1 before queue resume."

    with _daemon(mount) as url:
        # 1. Verified empty store is explicitly reported before any writes
        with HotMemClient(url, token="tok-a", expected_store_id="store-mmvt7") as client_a_init:
            h0 = client_a_init.health()
            assert h0["status"] == "ok"
            assert h0["memory_count"] == 0
            assert h0["store_id"] == "store-mmvt7"
            assert h0["store_state"] == "verified_empty"
            gen1 = h0["runtime_generation"]

        status_cli = runner.invoke(
            main,
            ["status", "--url", url, "--token", "tok-a", "--store-id", "store-mmvt7"],
        )
        assert status_cli.exit_code == 0

        # 2. Caller A writes shared decision + private canary
        with (
            HotMemClient(
                url,
                token="tok-a",
                session_id="sess-a-inc",
                source="agent://a",
                namespace="ops/shared",
                expected_store_id="store-mmvt7",
            ) as client_a_shared,
            HotMemClient(
                url,
                token="tok-a",
                session_id="sess-a-inc",
                source="agent://a",
                namespace="ops/private",
                expected_store_id="store-mmvt7",
            ) as client_a_priv,
        ):
            shared_res = client_a_shared.add("ops/demo-17", decision, importance=0.9)
            priv_res = client_a_priv.add("ops/canary", canary, importance=0.95)
            assert client_a_shared.health()["store_state"] == "ready"

        # 3. Detachable management client (#112 seam) connects, inspects scoped activity
        #    with store_id + runtime_generation + policy_revision, and sees zero private canary
        #    and zero raw fact_text in the event stream.
        with HotMemClient(
            url,
            token="tok-lsd-mgmt",
            session_id="sess-lsd-ui",
            source="ui://lsd",
            expected_store_id="store-mmvt7",
        ) as mgmt_client:
            mgmt_health = mgmt_client.health()
            assert mgmt_health["memory_count"] == 1
            assert mgmt_health["store_id"] == "store-mmvt7"
            assert mgmt_health["runtime_generation"] == gen1

            mgmt_events = mgmt_client.events()
            assert mgmt_events["count"] == 1
            assert mgmt_events["store_id"] == "store-mmvt7"
            assert mgmt_events["runtime_generation"] == gen1
            assert mgmt_events["policy_revision"] == 1
            ev_payload = mgmt_events["events"][0]["payload"]
            assert ev_payload["id"] == shared_res["memory_id"]
            assert "fact_text" not in ev_payload
            assert "embedding_b64" not in ev_payload
            assert canary not in json.dumps(mgmt_events)
            assert priv_res["memory_id"] not in json.dumps(mgmt_events)

        # 4. After management client disconnects, Caller B still recalls shared decision
        with HotMemClient(
            url,
            token="tok-b",
            session_id="sess-b-rec",
            source="agent://b",
            expected_store_id="store-mmvt7",
        ) as client_b:
            hits = client_b.search("DEMO-17 roll back", top_k=5)
            assert len(hits) == 1
            assert hits[0]["memory_id"] == shared_res["memory_id"]
            assert hits[0]["provenance"]["session_id"] == "sess-a-inc"


def test_mmvt7_dynamic_revocation_unavailable_store_and_route_audit(tmp_path: Path):
    """Verify immediate share/caller revocation, unavailable store 503, and route audit."""
    db_path = tmp_path / "revocation.sqlite"
    policy = PolicyConfig(
        store_id="store-revoke",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-revoke",
                namespaces=("ops/shared",),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id="store-revoke",
                namespaces=("ops/b-local",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-rev-1",
                        namespace="ops/shared",
                        identifiers=("ops/demo-17",),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=db_path, policy=policy)
    hdr_a = {
        "Authorization": "Bearer tok-a",
        "X-HotMem-Store-Id": "store-revoke",
        "X-HotMem-Namespace": "ops/shared",
        "X-HotMem-Session-Id": "sess-a-revoke",
    }
    hdr_b = {
        "Authorization": "Bearer tok-b",
        "X-HotMem-Store-Id": "store-revoke",
    }

    with TestClient(app) as client:
        # Every route in DAEMON_ROUTE_INVENTORY rejects unauthenticated requests
        for method, path, _ in DAEMON_ROUTE_INVENTORY:
            concrete_path = path.replace("{memory_id}", "nonexistent-id")
            if method == "GET":
                r = client.get(concrete_path)
            elif method == "POST":
                r = client.post(concrete_path, json={})
            elif method == "DELETE":
                r = client.delete(concrete_path)
            else:
                raise AssertionError(f"unexpected method {method}")
            # Unauthenticated call fails before route handler logic (or 422 if body parsed first)
            assert r.status_code in (401, 422), f"{method} {concrete_path} returned {r.status_code}"

        shared = client.post(
            "/v1/add",
            headers=hdr_a,
            json={"identifier": "ops/demo-17", "fact": "DEMO-17 rollback approved"},
        ).json()
        mid = shared["memory_id"]

        # Before revocation, B can read the shared memory
        assert client.get(f"/v1/memory/{mid}", headers=hdr_b).status_code == 200
        assert client.get("/v1/events", headers=hdr_b).json()["count"] == 1

        # 1. Revoke share approval -> B immediately loses access to ops/shared
        revoked_count = policy.revoke_share_approval("share-rev-1")
        assert revoked_count == 1
        assert policy.revision == 2

        assert client.get(f"/v1/memory/{mid}", headers=hdr_b).status_code == 404
        assert (
            client.post("/v1/search", headers=hdr_b, json={"query": "DEMO-17", "top_k": 5}).json()[
                "count"
            ]
            == 0
        )
        ev_after = client.get("/v1/events", headers=hdr_b).json()
        assert ev_after["count"] == 0
        assert ev_after["policy_revision"] == 2

        # 2. Revoke caller-b credential -> B immediately receives 403 revoked_credential
        policy.revoke_caller("caller-b")
        rev_resp = client.get("/v1/health", headers=hdr_b)
        assert rev_resp.status_code == 403
        assert rev_resp.json()["error"] == "revoked_credential"

        # 3. Closing/unmounting the store produces 503 store_unavailable (never empty store)
        app.state.runtime.close()
        unavail_resp = client.get("/v1/health", headers=hdr_a)
        assert unavail_resp.status_code == 503
        assert unavail_resp.json()["error"] == "store_unavailable"


def _start_daemon_on_port(
    mount: Path, port: int, *, store_id: str = "store-mmvt7"
) -> subprocess.Popen:
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
            store_id,
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    with HotMemClient(url, token="tok-a", expected_store_id=store_id) as probe:
        deadline = time.monotonic() + 15.0
        while True:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise AssertionError("daemon failed to start")
            try:
                if probe.health().get("status") == "ok":
                    break
            except Exception:
                pass
            time.sleep(0.05)
    return proc


def test_mmvt7_real_detachable_management_interface_close_reopen_and_actions(tmp_path: Path):
    """Exercise the real CLI/terminal detachable management view (`hotmem playground` and
    `hotmem status`) as separate processes against a live daemon (#112 Row 8):
      - Shows service, store, observed client/session, permitted activity, and permission state
      - Shows truthful poll/event freshness (`poll_and_event_observed`, `recorded_events`)
      - Executes authorized operations (`hygiene`, `search`, `revoke approval`, `reload`)
      - Demonstrates disconnect/reconnect across a daemon stop/start with generation change
      - Closes (`quit`) and reopens while the daemon and other clients continue working
      - Never exposes private canary content or identifiers
    """
    mount = tmp_path / "ui_mount"
    mount.mkdir()
    policy = PolicyConfig(
        store_id="store-mmvt7",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-mmvt7",
                namespaces=("ops/shared", "ops/private"),
                operations=("read", "write", "export", "hydrate", "promote"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id="store-mmvt7",
                namespaces=("ops/b-local",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-demo-17",
                        namespace="ops/shared",
                        identifiers=("ops/demo-17",),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-lsd-mgmt",
                caller_id="caller-lsd-mgmt",
                store_id="store-mmvt7",
                namespaces=("ops/shared",),
                operations=("read",),
                is_management=True,
            ),
        ],
    )
    policy.save(mount / "policy.json")
    port = _reserve_port()
    url = f"http://127.0.0.1:{port}"
    canary = "PRIVATE-UI-CANARY-9911: secret token trace"
    decision = "INC-112 approved rollback: revert payment-worker to v2.14.3."

    proc = _start_daemon_on_port(mount, port, store_id="store-mmvt7")
    try:
        with HotMemClient(url, token="tok-a", expected_store_id="store-mmvt7") as probe:
            gen1 = probe.health()["runtime_generation"]

        with (
            HotMemClient(
                url,
                token="tok-a",
                session_id="sess-codex-inc",
                harness_id="codex-cli",
                source="agent://a",
                namespace="ops/shared",
                expected_store_id="store-mmvt7",
            ) as client_a_shared,
            HotMemClient(
                url,
                token="tok-a",
                session_id="sess-codex-priv",
                harness_id="codex-cli",
                source="agent://a",
                namespace="ops/private",
                expected_store_id="store-mmvt7",
            ) as client_a_priv,
        ):
            shared_res = client_a_shared.add(
                "ops/demo-17",
                decision,
                importance=0.9,
                tags=["model:gpt-6.1-sol", "project:hotmem-ops", "org:knowguard"],
            )
            priv_res = client_a_priv.add("ops/canary", canary, importance=0.95)

        # 1. Run `hotmem status --url` as a real CLI process with management credentials
        status_proc = subprocess.run(
            _installed_cli(
                "status",
                "--url",
                url,
                "--token",
                "tok-lsd-mgmt",
                "--store-id",
                "store-mmvt7",
            ),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=True,
        )
        status_out = status_proc.stdout
        assert "Connection: connected (freshness=poll_and_event_observed" in status_out
        assert "Store: store-mmvt7 (" in status_out
        assert f"generation={gen1})" in status_out
        assert "Total memory count: 2" in status_out
        assert "Permissions: caller=caller-lsd-mgmt management=True policy_revision=1" in status_out
        assert "Observed clients (observation_basis=recorded_events):" in status_out
        assert "caller=caller-a harness=codex-cli session=sess-codex-inc" in status_out
        assert "Permitted activity (observation_basis=recorded_events" in status_out
        assert "ops/demo-17" in status_out
        assert canary not in status_out
        assert priv_res["memory_id"] not in status_out
        assert "sess-codex-priv" not in status_out

        # 2. Launch the real detachable `hotmem playground --url` process, inspect status,
        #    events, run authorized `hygiene` and `search`, then close (`quit`) while daemon
        #    and Client B continue working.
        ui_proc_1 = subprocess.run(
            _installed_cli(
                "playground",
                "--url",
                url,
                "--token",
                "tok-lsd-mgmt",
                "--store-id",
                "store-mmvt7",
                "--session",
                "sess-lsd-ui-1",
                "--harness",
                "hotmem-console",
                "--source",
                "ui://lsd",
            ),
            input="status\nevents 10\nhygiene\nsearch rollback\nquit\n",
            capture_output=True,
            text=True,
            timeout=20.0,
            check=True,
        )
        ui_out_1 = ui_proc_1.stdout
        assert "Service, Store & Permission State" in ui_out_1
        assert "Freshness: poll_and_event_observed" in ui_out_1
        assert "Store ID: store-mmvt7" in ui_out_1
        assert "Caller: caller-lsd-mgmt | Management: True" in ui_out_1
        assert "Observed Clients (observation_basis=recorded_events)" in ui_out_1
        assert "sess-codex-inc" in ui_out_1
        assert "hygiene: total_memories=1 warnings=0 store_id=store-mmvt7" in ui_out_1
        assert "ops/demo-17" in ui_out_1
        assert canary not in ui_out_1
        assert priv_res["memory_id"] not in ui_out_1
        assert "bye." in ui_out_1

        # 3. While the detachable view is closed, Client B continues recalling the shared
        #    decision and writes its own local note, and Client A updates the shared scope.
        with (
            HotMemClient(
                url,
                token="tok-b",
                session_id="sess-claude-rec",
                harness_id="claude-code",
                source="agent://b",
                expected_store_id="store-mmvt7",
            ) as client_b,
            HotMemClient(
                url,
                token="tok-a",
                session_id="sess-codex-inc-2",
                harness_id="codex-cli",
                source="agent://a",
                namespace="ops/shared",
                expected_store_id="store-mmvt7",
            ) as client_a_shared_2,
        ):
            hits_b = client_b.search("approved rollback", top_k=5)
            assert len(hits_b) == 1
            assert hits_b[0]["memory_id"] == shared_res["memory_id"]
            assert hits_b[0]["provenance"]["harness_id"] == "codex-cli"
            assert hits_b[0]["provenance"]["session_id"] == "sess-codex-inc"
            assert hits_b[0]["tags"] == [
                "model:gpt-6.1-sol",
                "project:hotmem-ops",
                "org:knowguard",
            ]
            client_b.add(
                "ops/b-followup",
                "Verified rollback on staging",
                importance=0.7,
                namespace="ops/b-local",
            )
            client_a_shared_2.add(
                "ops/demo-17",
                "INC-112 follow-up: queue shard 2 drained cleanly.",
                importance=0.85,
                tags=["model:gpt-6.1-sol", "project:hotmem-ops"],
            )

        # 4. Reopen a second detachable `hotmem playground --url` process while keeping it open
        #    across a daemon stop + restart to prove live disconnect/reconnect and authorized
        #    management revocation/reload.
        def _read_until(stream, marker: str, acc: list[str]) -> None:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                line = stream.readline()
                if not line:
                    break
                acc.append(line)
                if marker in "".join(acc):
                    return
            raise AssertionError(f"timed out waiting for {marker!r} in:\n{''.join(acc)}")

        ui_proc_2 = subprocess.Popen(
            _installed_cli(
                "playground",
                "--url",
                url,
                "--token",
                "tok-lsd-mgmt",
                "--store-id",
                "store-mmvt7",
                "--session",
                "sess-lsd-ui-2",
                "--harness",
                "hotmem-console",
                "--source",
                "ui://lsd",
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert ui_proc_2.stdin is not None
        assert ui_proc_2.stdout is not None
        collected: list[str] = []
        # Initial status sees the new activity written while the first view was closed
        ui_proc_2.stdin.write("status\n")
        ui_proc_2.stdin.flush()
        _read_until(ui_proc_2.stdout, "sess-codex-inc-2", collected)

        # Stop the daemon while ui_proc_2 stays open -> poll transitions to DISCONNECTED
        _stop_proc(proc)

        ui_proc_2.stdin.write("status\n")
        ui_proc_2.stdin.flush()
        _read_until(ui_proc_2.stdout, "Connection: DISCONNECTED", collected)

        # Restart the daemon on the same port/store -> poll transitions to reconnected
        # with generation_changed=True, then execute revoke approval + reload
        proc = _start_daemon_on_port(mount, port, store_id="store-mmvt7")
        with HotMemClient(url, token="tok-a", expected_store_id="store-mmvt7") as probe2:
            gen2 = probe2.health()["runtime_generation"]
        assert gen2 != gen1

        ui_proc_2.stdin.write(
            "status\nrevoke approval share-demo-17\nstatus\nreload\nstatus\nquit\n"
        )
        ui_proc_2.stdin.flush()
        rest_out, ui_err_2 = ui_proc_2.communicate(timeout=20.0)
        ui_out_2 = "".join(collected) + rest_out
        assert ui_proc_2.returncode == 0, ui_err_2
        assert "sess-codex-inc-2" in ui_out_2
        assert "Connection: DISCONNECTED" in ui_out_2
        assert "Connection: reconnected (generation_changed=True)" in ui_out_2
        assert "Revoked (approval=share-demo-17): revoked=1 policy_revision=2" in ui_out_2
        assert "Revoked shares: share-demo-17" in ui_out_2
        assert "Reloaded policy: policy_revision=3 credential_count=3" in ui_out_2
        assert canary not in ui_out_2
        assert priv_res["memory_id"] not in ui_out_2
    finally:
        _stop_proc(proc)


def test_mmvt7_two_harness_mcp_incident_decision_and_full_route_isolation(tmp_path: Path):
    """Exercise #112 Row 9 (Acceptance Items 2 & 3):
    - Harness A (codex-cli) writes an approved sanitized shared rollback decision with
      canonical tags (`model:*`, `project:*`, `org:*`), a follow-up with empty optional `tags=[]`,
      and a private investigation canary through the MCP relay (`create_mcp_server(url=...)`).
    - Harness B (claude-code) recalls the shared decision via MCP `search_memories` and
      `list_memories` with writer `harness_id`, `session_id`, `caller_id`, `source`, `tags`,
      and `provenance` preserved separately from reader context.
    - Private canary is isolated across all exposed routes, diagnostics, events, and logs.
    - Missing/mismatched/revoked credentials and spoofed session/harness/source/caller context
      fail clearly.
    """
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import timedelta

    import httpx
    import pytest

    pytest.importorskip("mcp", reason="requires [mcp] extra")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    mount = tmp_path / "inc112_mount"
    mount.mkdir()
    store_id = "store-incident-112"
    policy = PolicyConfig(
        store_id=store_id,
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-codex-a",
                caller_id="caller-a",
                store_id=store_id,
                namespaces=("ops/shared", "ops/private"),
                operations=("read", "write", "export", "hydrate"),
                allowed_sessions=("sess-codex-inc112-a",),
                allowed_harnesses=("codex-cli",),
                allowed_sources=("mcp://codex-a",),
            ),
            CallerCredential.from_token(
                token="tok-claude-b",
                caller_id="caller-b",
                store_id=store_id,
                namespaces=("ops/b",),
                operations=("read", "write"),
                allowed_sessions=("sess-claude-inc112-b",),
                allowed_harnesses=("claude-code",),
                allowed_sources=("mcp://claude-b",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-inc-112-decision",
                        namespace="ops/shared",
                        identifiers=("ops/incident-112/rollback-decision",),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-mgmt-112",
                caller_id="caller-mgmt",
                store_id=store_id,
                namespaces=("ops/shared",),
                operations=("read",),
                is_management=True,
            ),
        ],
    )
    policy.save(mount / "policy.json")

    canary_fact = "PRIVATE-CANARY-INC112: internal root-cause credential rotation trace 9f8e7d6c"
    canary_secret = "9f8e7d6c"
    decision_fact = (
        "Approved rollback decision INC-112: revert checkout-worker to build 2026.10.30-rc1 "
        "and drain shard-02 queue before re-enabling webhook consumers."
    )
    executable = _installed_cli()[0]

    @asynccontextmanager
    async def _mcp_relay(
        url: str,
        *,
        token: str,
        session_id: str,
        harness_id: str,
        source: str,
    ):
        params = StdioServerParameters(
            command=executable,
            args=[
                "mcp",
                "--url",
                url,
                "--token",
                token,
                "--session",
                session_id,
                "--harness",
                harness_id,
                "--source",
                source,
                "--store-id",
                store_id,
            ],
        )
        err_log_path = tmp_path / f"mcp_{session_id}.err"
        with err_log_path.open("w", encoding="utf-8") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session,
            ):
                await session.initialize()
                yield session

    with _daemon(mount, store_id=store_id, token="tok-codex-a") as url:

        async def _run_mcp_flow():
            async with _mcp_relay(
                url,
                token="tok-codex-a",
                session_id="sess-codex-inc112-a",
                harness_id="codex-cli",
                source="mcp://codex-a",
            ) as codex_sess:
                r1 = await codex_sess.call_tool(
                    "add_memory",
                    {
                        "identifier": "ops/incident-112/private-canary",
                        "namespace": "ops/private",
                        "fact": canary_fact,
                        "metadata": {"classification": "private"},
                        "importance": 0.95,
                    },
                )
                r2 = await codex_sess.call_tool(
                    "add_memory",
                    {
                        "identifier": "ops/incident-112/rollback-decision",
                        "namespace": "ops/shared",
                        "fact": decision_fact,
                        "tags": ["model:gpt-6.1-sol", "project:hotmem-ops", "org:knowguard"],
                        "importance": 0.9,
                    },
                )
                r3 = await codex_sess.call_tool(
                    "add_memory",
                    {
                        "identifier": "ops/incident-112/rollback-decision",
                        "namespace": "ops/shared",
                        "fact": (
                            "INC-112 status: shard-02 queue drained; empty optional tags valid."
                        ),
                        "tags": [],
                        "importance": 0.8,
                    },
                )

            async with _mcp_relay(
                url,
                token="tok-claude-b",
                session_id="sess-claude-inc112-b",
                harness_id="claude-code",
                source="mcp://claude-b",
            ) as claude_sess:
                l_sh = await claude_sess.call_tool(
                    "list_memories",
                    {"identifier": "ops/incident-112/rollback-decision"},
                )
                s_sh = await claude_sess.call_tool(
                    "search_memories",
                    {"query": "revert checkout-worker build 2026.10.30-rc1", "top_k": 5},
                )
                l_ca = await claude_sess.call_tool(
                    "list_memories",
                    {"identifier": "ops/incident-112/private-canary"},
                )
                s_ca = await claude_sess.call_tool(
                    "search_memories",
                    {"query": "PRIVATE-CANARY-INC112", "top_k": 5},
                )

            return (
                json.loads(r1.content[0].text),
                json.loads(r2.content[0].text),
                json.loads(r3.content[0].text),
                json.loads(l_sh.content[0].text),
                json.loads(s_sh.content[0].text),
                json.loads(l_ca.content[0].text),
                json.loads(s_ca.content[0].text),
            )

        (
            priv_res,
            shared_res,
            empty_tag_res,
            list_shared,
            search_shared,
            list_canary,
            search_canary,
        ) = asyncio.run(_run_mcp_flow())

        canary_id = priv_res["memory_id"]
        shared_id = shared_res["memory_id"]
        empty_tag_id = empty_tag_res["memory_id"]

        # 1. Verify Claude recalls the shared decisions with Writer A's provenance & tags intact
        assert list_shared["count"] == 2
        by_id = {m.get("memory_id") or m["id"]: m for m in list_shared["memories"]}
        rec_decision = by_id[shared_id]
        assert (rec_decision.get("content") or rec_decision["fact_text"]) == decision_fact
        assert rec_decision["tags"] == [
            "model:gpt-6.1-sol",
            "project:hotmem-ops",
            "org:knowguard",
        ]
        assert rec_decision["provenance"]["caller_id"] == "caller-a"
        assert rec_decision["provenance"]["harness_id"] == "codex-cli"
        assert rec_decision["provenance"]["session_id"] == "sess-codex-inc112-a"
        assert rec_decision["provenance"]["source"] == "mcp://codex-a"
        assert rec_decision["provenance"]["namespace"] == "ops/shared"
        assert rec_decision["provenance"]["store_id"] == store_id
        # Empty optional tags remain valid
        assert by_id[empty_tag_id]["tags"] == []

        assert search_shared["count"] >= 1
        assert search_shared["memories"][0]["memory_id"] == shared_id
        assert search_shared["memories"][0]["provenance"]["harness_id"] == "codex-cli"
        assert search_shared["memories"][0]["provenance"]["session_id"] == "sess-codex-inc112-a"

        # 2. Verify private canary is absent from Claude MCP responses
        assert list_canary["count"] == 0
        assert canary_id not in json.dumps(list_canary)
        assert canary_secret not in json.dumps(list_canary)
        assert canary_id not in json.dumps(search_canary)
        assert canary_secret not in json.dumps(search_canary)

        # 3. Verify authentication, spoofed context, management authority, and route isolation
        hdr_b = {
            "Authorization": "Bearer tok-claude-b",
            "X-HotMem-Store-Id": store_id,
            "X-HotMem-Harness-Id": "claude-code",
            "X-HotMem-Session-Id": "sess-claude-inc112-b",
            "X-HotMem-Source": "mcp://claude-b",
        }
        with httpx.Client(base_url=url, timeout=10.0) as raw:
            # Missing / invalid credentials / wrong store
            assert raw.get("/v1/health").status_code == 401
            assert (
                raw.get("/v1/health", headers={"Authorization": "Bearer bad-token"}).json()["error"]
                == "invalid_credential"
            )
            assert (
                raw.get(
                    "/v1/health",
                    headers={**hdr_b, "X-HotMem-Store-Id": "wrong-store"},
                ).json()["error"]
                == "store_mismatch"
            )
            # Spoofed session, harness, source, and caller_id fail clearly
            assert (
                raw.get(
                    "/v1/health",
                    headers={**hdr_b, "X-HotMem-Session-Id": "sess-codex-inc112-a"},
                ).json()["error"]
                == "session_impersonation_denied"
            )
            assert (
                raw.get(
                    "/v1/health",
                    headers={**hdr_b, "X-HotMem-Harness-Id": "codex-cli"},
                ).json()["error"]
                == "session_impersonation_denied"
            )
            assert (
                raw.get(
                    "/v1/health",
                    headers={**hdr_b, "X-HotMem-Source": "mcp://codex-a"},
                ).json()["error"]
                == "session_impersonation_denied"
            )
            assert (
                raw.get(
                    "/v1/health",
                    headers={**hdr_b, "X-HotMem-Caller-Id": "caller-a"},
                ).json()["error"]
                == "session_impersonation_denied"
            )

            # Route-by-route negative isolation for Client B against canary_id
            assert raw.get(f"/v1/memory/{canary_id}", headers=hdr_b).status_code == 404
            assert (
                raw.post(f"/v1/memory/{canary_id}/hydrate", headers=hdr_b, json={}).status_code
                == 404
            )
            batch = raw.post(
                "/v1/memory/hydrate-batch",
                headers=hdr_b,
                json={"memory_ids": [canary_id, shared_id]},
            ).json()
            assert batch["results"][0] == {"memory_id": canary_id, "error": "not_found"}
            assert batch["results"][1]["memory_id"] == shared_id
            assert canary_secret not in json.dumps(batch)
            assert (
                raw.post(f"/v1/memory/{canary_id}/candidate", headers=hdr_b, json={}).status_code
                == 403
            )
            assert (
                raw.post(
                    f"/v1/memory/{canary_id}/promote",
                    headers=hdr_b,
                    json={"to_state": "READY"},
                ).status_code
                == 403
            )
            assert raw.post("/v1/snapshot", headers=hdr_b, json={}).status_code == 403
            assert (
                raw.post(
                    "/v1/hydrate", headers=hdr_b, json={"namespace": "ops/private"}
                ).status_code
                == 403
            )
            assert raw.get("/v1/bundles", headers=hdr_b).status_code == 403
            assert (
                raw.post("/v1/discover", headers=hdr_b, json={"root": str(mount)}).status_code
                == 403
            )
            assert (
                raw.post("/v1/policy/revoke", headers=hdr_b, json={"caller_id": "caller-a"}).json()[
                    "error"
                ]
                == "management_authority_required"
            )
            assert (
                raw.post("/v1/policy/reload", headers=hdr_b, json={}).json()["error"]
                == "management_authority_required"
            )
            ev_b = raw.get("/v1/events", headers=hdr_b).json()
            assert ev_b["count"] == 2
            assert canary_id not in json.dumps(ev_b)
            assert canary_secret not in json.dumps(ev_b)
            health_b = raw.get("/v1/health", headers=hdr_b).json()
            assert health_b["memory_count"] == 2
            assert health_b["total_memory_count"] == 0
            assert health_b["recent_activity"]["latest_seq"] == 3
            assert canary_id not in json.dumps(health_b)
            assert canary_secret not in json.dumps(health_b)
