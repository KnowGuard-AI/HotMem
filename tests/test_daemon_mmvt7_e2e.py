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
def _daemon(mount: Path, *, store_id: str = "store-mmvt7"):
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
            with HotMemClient(url, token="tok-a", expected_store_id=store_id) as probe:
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
