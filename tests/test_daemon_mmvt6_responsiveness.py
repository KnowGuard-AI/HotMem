"""MMVT 6 — Responsiveness, bounded concurrency, and transport baseline (#109).

Question/test:
    - What is the measured cost of warm pooled loopback HTTP compared with
      direct calls for the 2-client workload, and do slow operations block
      health or basic reads?
    - Run one controlled slow-operation + health/basic-read check and one
      direct-vs-loopback measurement on the same machine, store, embedder, and
      inputs (at least 20 reads/client after warmup).
    - Verify bounded write queue timeout (`503 server_busy`) and mid-flight
      rollback on error/cancellation.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.policy import CallerCredential, PolicyConfig
from hotmem.runtime import HotMemRuntime
from hotmem.server import create_app


def test_mmvt6_health_and_basic_reads_remain_responsive_during_slow_operation(
    tmp_path: Path,
):
    """While a slow operation holds `_write_slot`, health and search complete via WAL read."""
    db_path = tmp_path / "responsive.sqlite"
    policy = PolicyConfig(
        store_id="store-mmvt6",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-mmvt6",
                namespaces=("ops/shared",),
                operations=("read", "write"),
            )
        ],
    )
    rt = HotMemRuntime(db_path=db_path, policy=policy, write_timeout_s=0.15).open()
    app = create_app(db_path=db_path, policy=policy, runtime=rt)
    hdr = {
        "Authorization": "Bearer tok-a",
        "X-HotMem-Store-Id": "store-mmvt6",
        "X-HotMem-Namespace": "ops/shared",
        "X-HotMem-Session-Id": "sess-responsive",
    }

    with TestClient(app) as client:
        seed = client.post(
            "/v1/add",
            headers=hdr,
            json={
                "identifier": "ops/demo-17",
                "fact": "Incident DEMO-17: roll back worker release v2.4.1.",
                "importance": 0.9,
            },
        ).json()
        seed_mid = seed["memory_id"]

        slow_entered = threading.Event()
        release_slow = threading.Event()

        def _slow_writer() -> None:
            with rt._write_slot():
                slow_entered.set()
                release_slow.wait(timeout=3.0)

        t_slow = threading.Thread(target=_slow_writer, daemon=True)
        t_slow.start()
        assert slow_entered.wait(timeout=2.0)

        try:
            # 1. While _write_slot is held, /v1/health, /v1/search, and /v1/memory/{id}
            #    must complete promptly via a WAL read-only connection.
            t_health = time.perf_counter()
            h_resp = client.get("/v1/health", headers=hdr)
            assert time.perf_counter() - t_health < 2.0
            assert h_resp.status_code == 200
            assert h_resp.json()["memory_count"] == 1

            t_search = time.perf_counter()
            s_resp = client.post(
                "/v1/search",
                headers=hdr,
                json={"query": "DEMO-17 roll back", "top_k": 5},
            )
            assert time.perf_counter() - t_search < 2.0
            assert s_resp.status_code == 200
            assert s_resp.json()["count"] == 1

            m_resp = client.get(f"/v1/memory/{seed_mid}", headers=hdr)
            assert m_resp.status_code == 200
            assert m_resp.json()["id"] == seed_mid

            # 2. A competing write exceeding write_timeout_s fails bounded with 503 server_busy
            busy_resp = client.post(
                "/v1/add",
                headers=hdr,
                json={"identifier": "ops/queued", "fact": "queued write during saturation"},
            )
            assert busy_resp.status_code == 503
            assert busy_resp.json()["error"] == "server_busy"
        finally:
            release_slow.set()
            t_slow.join(timeout=2.0)

        # 3. Mid-flight exception/cancellation inside _write_slot rolls back cleanly
        with (
            pytest.raises(RuntimeError, match="simulated cancellation"),
            rt._write_slot() as db,
        ):
            db._conn.execute(
                "INSERT INTO memories (id, identifier, fact_text, namespace) "
                "VALUES ('dangling-1', 'ops/dangling', 'uncommitted', 'ops/shared')"
            )
            raise RuntimeError("simulated cancellation")

        assert rt.db.get_memory("dangling-1") is None

        # Subsequent write succeeds normally after rollback
        ok_resp = client.post(
            "/v1/add",
            headers=hdr,
            json={"identifier": "ops/after-cancel", "fact": "clean write after rollback"},
        )
        assert ok_resp.status_code == 200


def test_mmvt6_two_client_direct_vs_loopback_http_baseline():
    """Run the 2-client direct vs. warm pooled loopback HTTP baseline (>=20 reads/client)."""
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    bench_path = root / "bench" / "daemon" / "run_mmvt6_baseline.py"
    spec = importlib.util.spec_from_file_location("run_mmvt6_baseline", bench_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    report = mod.run_baseline(reads_per_client=20, warmup=3)
    assert report["embedder"] == "hotmem-hash-v1"
    assert report["clients"] == 2
    assert report["reads_per_client"] == 20
    assert report["direct"]["samples"] == 40
    assert report["loopback_http_pooled"]["samples"] == 40
    assert report["direct"]["payload_bytes_avg"] > 0
    assert report["loopback_http_pooled"]["payload_bytes_avg"] > 0
    assert report["direct"]["wall_p50_ms"] >= 0.0
    assert report["loopback_http_pooled"]["wall_p50_ms"] >= 0.0
    assert report["loopback_http_pooled"]["trace_p50_ms"] >= 0.0
    assert "approx_transport_delta_p50_ms" in report["loopback_http_pooled"]
    assert report["rss_mb"] >= 0.0  # best-effort on platforms without psapi
