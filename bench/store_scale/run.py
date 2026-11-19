#!/usr/bin/env python3
"""Measure HotMem SQLite ingest, exact-search latency, disk use, and peak RSS.

Runs one isolated worker process per requested store size so peak RSS is
measured independently. Data and reports are synthetic local diagnostics;
the script makes no network calls and writes only its temporary store and the
optional --output report.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hotmem.db import MemoryDB, MemoryRecord  # noqa: E402
from hotmem.embed import embed_text, pack_embedding  # noqa: E402
from hotmem.search import search_memories  # noqa: E402


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        return None
    return int(raw if sys.platform == "darwin" else raw * 1024)


def _disk_bytes(db_path: Path) -> int:
    return sum(
        path.stat().st_size for path in db_path.parent.glob(db_path.name + "*") if path.is_file()
    )


def _worker(size: int, repeats: int, directory: Path) -> dict:
    db_path = directory / "scale.sqlite"
    db = MemoryDB(db_path)
    vectors = [pack_embedding(embed_text(f"scale group {group}")) for group in range(64)]

    ingest_start = time.perf_counter()
    batch_size = 2_000
    for start in range(0, size, batch_size):
        stop = min(size, start + batch_size)
        batch = [
            MemoryRecord(
                id=f"scale-{index:09d}",
                identifier="scale-benchmark",
                fact_text=(
                    f"Synthetic scale record markerx{index:09d} "
                    f"group{index % 64:02d} retention{index % 31:02d}"
                ),
                embedding=vectors[index % len(vectors)],
                importance=0.5,
            )
            for index in range(start, stop)
        ]
        inserted = db.insert_many_ignore(batch)
        if inserted != len(batch):
            raise RuntimeError(f"inserted {inserted} of {len(batch)} rows")
    ingest_seconds = time.perf_counter() - ingest_start

    # Mix exact one-row lexical hits with no-match searches. Every search uses
    # the production default full-scan path and the hash embedder.
    exact_id = f"scale-{(size // 2):09d}"
    exact_query = f"markerx{(size // 2):09d}"
    if exact_id not in {row["id"] for row in db.fts_search(exact_query)}:
        raise RuntimeError("synthetic exact-match query did not hit its expected row")
    if db.fts_search("missingbenchmarktoken"):
        raise RuntimeError("synthetic no-match query unexpectedly matched rows")

    search_times_ms = []
    for repeat in range(repeats):
        if repeat % 4 == 3:
            query = "missingbenchmarktoken"
        else:
            row = (repeat * max(1, size // repeats)) % size
            query = f"markerx{row:09d}"
        start = time.perf_counter()
        search_memories(db, query, top_k=5)
        search_times_ms.append((time.perf_counter() - start) * 1000)

    peak_rss = _peak_rss_bytes()
    db.close()
    return {
        "memories": size,
        "batch_size": batch_size,
        "ingest_seconds": round(ingest_seconds, 3),
        "search_repeats": repeats,
        "search_latency_ms": {
            "p50": round(statistics.median(search_times_ms), 3),
            "p95": round(_percentile(search_times_ms, 0.95), 3),
            "samples": [round(value, 3) for value in search_times_ms],
        },
        "peak_rss_bytes": peak_rss,
        "database_and_sidecars_bytes": _disk_bytes(db_path),
        "row_count": size,
        "workload_checks": {"exact_match": True, "no_match": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000])
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_worker", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--_directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if any(size < 1 for size in args.sizes):
        parser.error("--sizes values must be positive")

    if args._worker is not None:
        assert args._directory is not None
        print(json.dumps(_worker(args._worker, args.repeats, args._directory), sort_keys=True))
        return 0

    results = []
    for size in args.sizes:
        with tempfile.TemporaryDirectory(prefix=f"hotmem-scale-{size}-") as temp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--_worker",
                    str(size),
                    "--_directory",
                    temp,
                    "--repeats",
                    str(args.repeats),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            results.append(json.loads(completed.stdout))

    report = {
        "schema_version": 1,
        "benchmark": "HotMem SQLite scale / default exact-search path",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
        "method": {
            "data": "synthetic unique text, deterministic repeated 64-d hash vectors",
            "search": "search_memories default hash embedder; full-scan fallback; top_k=5",
            "latency_samples": "mixed exact single-token FTS hits and no-match searches",
            "targets": "not specified in the source work plan; measurements are diagnostic",
        },
        "results": results,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
