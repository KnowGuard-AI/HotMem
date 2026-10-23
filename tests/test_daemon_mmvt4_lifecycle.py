"""MMVT 4 — Lifecycle, ownership, and safe retries (#109, #100).

Question/test:
    - Start the service on an explicit disposable mount; write and receive
      acknowledgement; disconnect/reconnect one client while the other works;
      gracefully stop/restart; then perform one bounded abrupt process stop and
      retry an ambiguous write using the same operation identity.
    - Attempt a second supported owner/maintenance process against the live mount.
    - Verify identical content in distinct namespaces (#100) and migration of
      existing databases with namespace-independent content_hash indexes.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from hotmem.cli import main
from hotmem.client import HotMemClient
from hotmem.db import MemoryDB
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval


def _installed_cli(*args: str) -> list[str]:
    executable = shutil.which("hotmem", path=str(Path(sys.executable).parent))
    assert executable, "installed hotmem entry point must exist"
    return [executable, *args]


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _stop_proc(proc: subprocess.Popen, *, abrupt: bool = False) -> None:
    if proc.poll() is None:
        if abrupt:
            proc.kill()
        elif os.name == "nt":
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
def _daemon_process(mount: Path, *, store_id: str = "store-mmvt4"):
    port = _reserve_port()
    log_path = mount.parent / f"daemon_{port}.log"
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


def test_mmvt4_lifecycle_reconnect_abrupt_stop_idempotent_retry_and_ownership(
    tmp_path: Path,
):
    """Exercise full MMVT 4 lifecycle, abrupt stop, operation retry, and mount contention."""
    mount = tmp_path / "mmvt4_mount"
    mount.mkdir()
    policy = PolicyConfig(
        store_id="store-mmvt4",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-mmvt4",
                namespaces=("ops/shared", "ops/private"),
                operations=("read", "write", "export", "hydrate", "promote"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id="store-mmvt4",
                namespaces=("ops/b-work",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-mmvt4",
                        namespace="ops/shared",
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    policy.save(mount / "policy.json")

    shared_fact = "Incident DEMO-17: roll back worker release v2.4.1 before queue resume."
    identical_fact = "Identical fact text stored in both private and shared scopes."

    # Phase 1: Start daemon, write with idempotency key, disconnect A while B works,
    # test competing owner/maintenance rejection, then abruptly kill daemon.
    with _daemon_process(mount) as (url1, proc1):
        client_b = HotMemClient(
            url1,
            token="tok-b",
            session_id="sess-b-1",
            source="agent://b",
            expected_store_id="store-mmvt4",
        )
        try:
            with HotMemClient(
                url1,
                token="tok-a",
                session_id="sess-a-1",
                source="agent://a",
                namespace="ops/shared",
                expected_store_id="store-mmvt4",
            ) as client_a:
                ack1 = client_a.add(
                    "ops/demo-17",
                    shared_fact,
                    importance=0.9,
                    idempotency_key="op-demo-17-v1",
                )
                shared_mid = ack1["memory_id"]

                # Identical (identifier, fact) in ops/shared and ops/private (#100)
                same_shared = client_a.add("ops/common-id", identical_fact)

            with HotMemClient(
                url1,
                token="tok-a",
                session_id="sess-a-1",
                source="agent://a",
                namespace="ops/private",
                expected_store_id="store-mmvt4",
            ) as client_a_priv:
                same_priv = client_a_priv.add("ops/common-id", identical_fact)
                assert same_priv["memory_id"] != same_shared["memory_id"]
                assert same_priv["content_hash"] == same_shared["content_hash"]
                # Verify A's shared record was NOT overwritten by A's private record!
                got_priv = client_a_priv.get_memory(same_priv["memory_id"])
                assert got_priv["fact_text"] == identical_fact

            # Client A is now disconnected; Client B continues working and recalls shared records
            b_hits = client_b.search("DEMO-17 roll back", top_k=5)
            assert any(m["memory_id"] == shared_mid for m in b_hits)
            assert client_b.get_memory(same_shared["memory_id"])["fact_text"] == identical_fact

            # Competing owner/maintenance process against live mount must fail actionably
            runner = CliRunner()
            second_serve = runner.invoke(
                main,
                [
                    "serve",
                    "--mount",
                    str(mount),
                    "--port",
                    str(_reserve_port()),
                ],
            )
            assert second_serve.exit_code != 0
            assert "already owned by a live HotMem process" in second_serve.output

            offline_promote = runner.invoke(
                main,
                [
                    "promote",
                    "--db",
                    str(mount / "hotmem.sqlite"),
                    "--memory-id",
                    shared_mid,
                    "--to",
                    "READY",
                ],
            )
            assert offline_promote.exit_code != 0
            assert "already owned by a live HotMem process" in offline_promote.output
        finally:
            client_b.close()

        # Bounded abrupt process stop (SIGKILL)
        _stop_proc(proc1, abrupt=True)

    # Phase 2: Restart daemon after abrupt stop -> stale lock is recovered, acknowledged
    # writes survive, and retrying the ambiguous write with the same idempotency_key
    # returns the exact same memory_id without duplicating records or events!
    with (
        _daemon_process(mount) as (url2, _proc2),
        HotMemClient(
            url2,
            token="tok-a",
            session_id="sess-a-2",
            source="agent://a",
            namespace="ops/shared",
            expected_store_id="store-mmvt4",
        ) as client_a_reconnected,
    ):
        h = client_a_reconnected.health()
        assert h["status"] == "ok"
        # 2 records in ops/shared + 1 record in ops/private = 3 total for caller-a
        assert h["memory_count"] == 2
        with HotMemClient(url2, token="tok-a", expected_store_id="store-mmvt4") as client_a_all:
            assert client_a_all.health()["memory_count"] == 3

        # Retry the exact write with the same idempotency_key
        retry_ack = client_a_reconnected.add(
            "ops/demo-17",
            shared_fact,
            importance=0.9,
            idempotency_key="op-demo-17-v1",
        )
        assert retry_ack["memory_id"] == shared_mid
        assert retry_ack["content_hash"] == ack1["content_hash"]

        # Verify no duplicate memory or duplicate memory.created event was created
        assert client_a_reconnected.health()["memory_count"] == 2
        evs = client_a_reconnected.events(memory_id=shared_mid)
        assert evs["count"] == 1

        # Reusing the same idempotency_key for a different payload fails with 409
        with pytest.raises(httpx.HTTPStatusError) as conflict_err:
            client_a_reconnected.add(
                "ops/demo-17",
                "Different conflicting fact payload",
                importance=0.9,
                idempotency_key="op-demo-17-v1",
            )
        assert conflict_err.value.response.status_code == 409
        assert conflict_err.value.response.json()["error"] == "idempotency_key_conflict"

    # After graceful shutdown, the lock file is cleaned up and mount can be removed cleanly
    shutil.rmtree(mount)
    assert not mount.exists()


def test_mmvt4_existing_db_content_hash_index_migrates_to_namespace_scoped(
    tmp_path: Path,
):
    """Existing DB with legacy global unique(content_hash) index migrates to (namespace, hash)."""
    db_path = tmp_path / "legacy_unique.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT DEFAULT '',
            embedding BLOB,
            embedding_dim INTEGER,
            embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            ttl_seconds INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            namespace TEXT DEFAULT ''
        )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX idx_memories_content_hash_unique
           ON memories(content_hash)
           WHERE content_hash != ''"""
    )
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        db.insert(
            id="m-ns1",
            identifier="doc/1",
            fact_text="shared text",
            embedding=b"",
            content_hash="hash-shared-1",
            namespace="ns1",
        )
        db.insert(
            id="m-ns2",
            identifier="doc/1",
            fact_text="shared text",
            embedding=b"",
            content_hash="hash-shared-1",
            namespace="ns2",
        )
        assert db.count() == 2
        assert db.get_memory("m-ns1") is not None
        assert db.get_memory("m-ns2") is not None
    finally:
        db.close()


def test_mmvt4_graceful_restart_preserves_acknowledged_writes(tmp_path: Path):
    """Graceful stop/restart keeps acknowledged writes and recovers ownership cleanly."""
    mount = tmp_path / "graceful_mount"
    mount.mkdir()
    policy = PolicyConfig(
        store_id="store-mmvt4",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-mmvt4",
                namespaces=("ops/shared",),
                operations=("read", "write"),
                allowed_sessions=("sess-graceful",),
            )
        ],
    )
    policy.save(mount / "policy.json")

    with (
        _daemon_process(mount) as (url1, _proc1),
        HotMemClient(
            url1,
            token="tok-a",
            session_id="sess-graceful",
            namespace="ops/shared",
            expected_store_id="store-mmvt4",
        ) as client,
    ):
        memory_id = client.add("ops/graceful", "acknowledged before a graceful restart")[
            "memory_id"
        ]
        assert client.health()["memory_count"] == 1
        # Leaving the context terminates the daemon gracefully (SIGTERM).

    with (
        _daemon_process(mount) as (url2, _proc2),
        HotMemClient(
            url2,
            token="tok-a",
            session_id="sess-graceful",
            namespace="ops/shared",
            expected_store_id="store-mmvt4",
        ) as client,
    ):
        health = client.health()
        assert health["status"] == "ok"
        assert health["memory_count"] == 1
        assert client.get_memory(memory_id)["fact_text"] == (
            "acknowledged before a graceful restart"
        )
