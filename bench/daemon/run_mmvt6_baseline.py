"""MMVT 6 — Reproducible 2-client direct vs. warm pooled loopback HTTP baseline (#109).

Usage:
    uv run python bench/daemon/run_mmvt6_baseline.py
"""

from __future__ import annotations

import json
import os
import resource
import socket
import statistics
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from hotmem.client import HotMemClient
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.runtime import HotMemRuntime
from hotmem.server import create_app


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 3)


def _rss_mb() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    # On macOS (darwin), ru_maxrss is in bytes; on Linux it is in kilobytes.
    divisor = (1024.0 * 1024.0) if os.uname().sysname == "Darwin" else 1024.0
    return round(ru.ru_maxrss / divisor, 2)


def _cpu_seconds() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return round(ru.ru_utime + ru.ru_stime, 4)


def run_baseline(reads_per_client: int = 25, warmup: int = 5) -> dict[str, Any]:
    assert reads_per_client >= 20, "MMVT 6 requires at least 20 reads/client after warmup"
    with tempfile.TemporaryDirectory(prefix="hotmem_mmvt6_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        policy = PolicyConfig(
            store_id="store-mmvt6-bench",
            require_auth=True,
            credentials=[
                CallerCredential.from_token(
                    token="tok-a",
                    caller_id="caller-a",
                    store_id="store-mmvt6-bench",
                    namespaces=("ops/shared", "ops/private"),
                    operations=("read", "write"),
                ),
                CallerCredential.from_token(
                    token="tok-b",
                    caller_id="caller-b",
                    store_id="store-mmvt6-bench",
                    namespaces=("ops/b-local",),
                    operations=("read", "write"),
                    share_approvals=(
                        ShareApproval(
                            approval_id="share-bench",
                            namespace="ops/shared",
                            audience=("caller-b",),
                            operations=("read",),
                        ),
                    ),
                ),
            ],
        )

        # ── 1. Direct runtime calls on store ───────────────────────────────
        direct_db = tmp_path / "direct.sqlite"
        rt = HotMemRuntime(db_path=direct_db, policy=policy, store_id="store-mmvt6-bench").open()
        ctx_a = rt.resolve_caller(
            token="tok-a",
            requested_store_id="store-mmvt6-bench",
            session_id="sess-a",
            source="agent://a",
            namespace="ops/shared",
        )
        ctx_a_priv = rt.resolve_caller(
            token="tok-a",
            requested_store_id="store-mmvt6-bench",
            session_id="sess-a",
            source="agent://a",
            namespace="ops/private",
        )
        ctx_b = rt.resolve_caller(
            token="tok-b",
            requested_store_id="store-mmvt6-bench",
            session_id="sess-b",
            source="agent://b",
        )

        for i in range(10):
            rt.add(
                ctx_a,
                identifier=f"ops/demo-{i}",
                fact=f"Incident DEMO-{i}: roll back worker release v2.4.{i} before queue resume.",
                importance=0.8,
            )
            rt.add(
                ctx_a_priv,
                identifier=f"ops/priv-{i}",
                fact=f"Private investigation canary note #{i} for staging worker.",
                importance=0.9,
            )

        for _ in range(warmup):
            rt.search(ctx_a, query="DEMO-1 roll back worker release", top_k=5)
            rt.search(ctx_b, query="DEMO-1 roll back worker release", top_k=5)

        cpu_start_direct = _cpu_seconds()
        direct_wall_ms: list[float] = []
        direct_trace_ms: list[float] = []
        direct_payload_bytes = 0

        for i in range(reads_per_client):
            q = f"DEMO-{i % 10} roll back worker release"
            for ctx in (ctx_a, ctx_b):
                t0 = time.perf_counter()
                res = rt.search(ctx, query=q, top_k=5)
                wall = (time.perf_counter() - t0) * 1000.0
                direct_wall_ms.append(wall)
                direct_trace_ms.append(float(res["trace_ms"]))
                direct_payload_bytes += len(json.dumps(res).encode("utf-8"))

        cpu_direct = round(_cpu_seconds() - cpu_start_direct, 4)
        rt.close()

        # ── 2. Warm pooled loopback HTTP with 2 clients ───────────────────
        http_db = tmp_path / "loopback.sqlite"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        app = create_app(db_path=http_db, port=port, policy=policy, store_id="store-mmvt6-bench")
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{port}"

        try:
            with (
                HotMemClient(
                    base_url,
                    token="tok-a",
                    session_id="sess-a",
                    source="agent://a",
                    namespace="ops/shared",
                    expected_store_id="store-mmvt6-bench",
                ) as client_a,
                HotMemClient(
                    base_url,
                    token="tok-a",
                    session_id="sess-a",
                    source="agent://a",
                    namespace="ops/private",
                    expected_store_id="store-mmvt6-bench",
                ) as client_a_priv,
                HotMemClient(
                    base_url,
                    token="tok-b",
                    session_id="sess-b",
                    source="agent://b",
                    expected_store_id="store-mmvt6-bench",
                ) as client_b,
            ):
                for _ in range(50):
                    try:
                        if client_a.health().get("status") == "ok":
                            break
                    except Exception:
                        time.sleep(0.05)

                for i in range(10):
                    shared_text = (
                        f"Incident DEMO-{i}: roll back worker release v2.4.{i} "
                        "before queue resume."
                    )
                    client_a.add(
                        f"ops/demo-{i}",
                        shared_text,
                        importance=0.8,
                    )
                    client_a_priv.add(
                        f"ops/priv-{i}",
                        f"Private investigation canary note #{i} for staging worker.",
                        importance=0.9,
                    )

                for _ in range(warmup):
                    client_a.search("DEMO-1 roll back worker release", top_k=5)
                    client_b.search("DEMO-1 roll back worker release", top_k=5)

                cpu_start_http = _cpu_seconds()
                http_wall_ms: list[float] = []
                http_trace_ms: list[float] = []
                http_payload_bytes = 0

                for i in range(reads_per_client):
                    q = f"DEMO-{i % 10} roll back worker release"
                    for c in (client_a, client_b):
                        t0 = time.perf_counter()
                        resp = c._client.post("/v1/search", json={"query": q, "top_k": 5})
                        wall = (time.perf_counter() - t0) * 1000.0
                        resp.raise_for_status()
                        body = resp.json()
                        http_wall_ms.append(wall)
                        http_trace_ms.append(float(body["trace_ms"]))
                        http_payload_bytes += len(resp.content)

                cpu_http = round(_cpu_seconds() - cpu_start_http, 4)
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)

    direct_wall_sorted = sorted(direct_wall_ms)
    direct_trace_sorted = sorted(direct_trace_ms)
    http_wall_sorted = sorted(http_wall_ms)
    http_trace_sorted = sorted(http_trace_ms)

    http_deltas = [max(0.0, w - t) for w, t in zip(http_wall_ms, http_trace_ms, strict=True)]
    http_deltas_sorted = sorted(http_deltas)

    return {
        "embedder": "hotmem-hash-v1",
        "clients": 2,
        "reads_per_client": reads_per_client,
        "total_samples_per_mode": reads_per_client * 2,
        "rss_mb": _rss_mb(),
        "direct": {
            "samples": len(direct_wall_ms),
            "payload_bytes_avg": round(
                direct_payload_bytes / max(1, len(direct_wall_ms)), 1
            ),
            "wall_p50_ms": _percentile(direct_wall_sorted, 50),
            "wall_p95_ms": _percentile(direct_wall_sorted, 95),
            "wall_mean_ms": round(statistics.mean(direct_wall_ms), 3),
            "trace_p50_ms": _percentile(direct_trace_sorted, 50),
            "trace_p95_ms": _percentile(direct_trace_sorted, 95),
            "cpu_s": cpu_direct,
        },
        "loopback_http_pooled": {
            "samples": len(http_wall_ms),
            "payload_bytes_avg": round(
                http_payload_bytes / max(1, len(http_wall_ms)), 1
            ),
            "wall_p50_ms": _percentile(http_wall_sorted, 50),
            "wall_p95_ms": _percentile(http_wall_sorted, 95),
            "wall_mean_ms": round(statistics.mean(http_wall_ms), 3),
            "trace_p50_ms": _percentile(http_trace_sorted, 50),
            "trace_p95_ms": _percentile(http_trace_sorted, 95),
            "approx_transport_delta_p50_ms": _percentile(http_deltas_sorted, 50),
            "approx_transport_delta_p95_ms": _percentile(http_deltas_sorted, 95),
            "cpu_s": cpu_http,
        },
    }


if __name__ == "__main__":
    report = run_baseline()
    print(json.dumps(report, indent=2))
