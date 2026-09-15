#!/usr/bin/env python3
"""Delta sync benchmarks: incremental transfer vs full clone (#73 evidence).

Measures the company-brain sync workflow at representative sizes:

  - clone_initial:      full verified package export (the #69 baseline path)
  - hydrate_full_clone: clean-target restore of that package
  - delta_produce:      verified base-to-current diff for a small change set
  - delta_apply:        atomic CAS apply on the receiver
  - delta_apply_repeat: idempotent replay (must change nothing)
  - delta_verify:       package verification only
  - reclone_fallback:   a fresh full clone for the same small change — the
                        cost a receiver pays without incremental sync

Metrics per scenario: wall seconds, bytes written, tracemalloc peak (Python
allocations), and embedding calls. Results are written to results.json for
the PR. Embedding calls are expected to be ZERO everywhere: delta records
carry compatible stored embeddings.

Usage:
    uv run python bench/sync/run_bench.py --sizes 1000,5000 --label branch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
import tracemalloc
from pathlib import Path


def _fact(i: int) -> str:
    return (
        f"sync benchmark fact {i}: acme operations runbook section {i % 97} "
        f"references vendor {i % 89} with sla tier {i % 5} and review gate {i % 12}"
    )


def gen_corpus(db_path: Path, size: int) -> None:
    """Generate a deterministic source instance (not timed)."""
    from hotmem.db import MemoryDB, MemoryRecord
    from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
    from hotmem.interchange.canonical import compute_content_hash

    if db_path.exists():
        db_path.unlink()
    db = MemoryDB(db_path)
    batch: list[MemoryRecord] = []
    for i in range(size):
        fact = _fact(i)
        batch.append(
            MemoryRecord(
                id=f"sync{i:07d}",
                identifier=f"sync-{i:07d}",
                fact_text=fact,
                embedding=pack_embedding(embed_text(fact)),
                embedding_dim=EMBEDDING_DIM,
                embedding_model=EMBEDDING_MODEL,
                content_hash=compute_content_hash(f"sync-{i:07d}", fact),
                namespace="bench",
            )
        )
        if len(batch) >= 1000:
            db.insert_many_ignore(batch)
            batch = []
    if batch:
        db.insert_many_ignore(batch)
    db.close()


class EmbedCounter:
    """Count embed_text calls through every resolution site."""

    def __init__(self) -> None:
        import hotmem.interchange.compat as compat_mod
        import hotmem.swap as swap_mod

        self._sites = [swap_mod, compat_mod]
        self._origs = {id(m): m.embed_text for m in self._sites}
        self.calls = 0

    def _spy_for(self, mod):
        orig = self._origs[id(mod)]

        def spy(text: str):
            self.calls += 1
            return orig(text)

        return spy

    def __enter__(self) -> EmbedCounter:
        for mod in self._sites:
            mod.embed_text = self._spy_for(mod)
        return self

    def __exit__(self, *exc) -> None:
        for mod in self._sites:
            mod.embed_text = self._origs[id(mod)]


def _timed(fn) -> tuple[float, float]:
    tracemalloc.start()
    start = time.perf_counter()
    fn()
    seconds = time.perf_counter() - start
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return seconds, peak / (1024 * 1024)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def run_scenarios(size: int, work: Path) -> list[dict]:
    from hotmem.db import MemoryDB
    from hotmem.embed import embed_text, pack_embedding
    from hotmem.interchange.delta import apply_delta, produce_delta, verify_delta
    from hotmem.interchange.hydrate import hydrate_package
    from hotmem.interchange.package import write_package

    rows: list[dict] = []
    corpus = work / f"corpus-{size}.sqlite"
    if not corpus.exists():
        gen_corpus(corpus, size)

    def record(name: str, seconds: float, peak: float, embeds: int, bytes_written: int = 0) -> None:
        rows.append(
            {
                "scenario": name,
                "size": size,
                "seconds": round(seconds, 4),
                "bytes": bytes_written,
                "tracemalloc_peak_mb": round(peak, 2),
                "embed_calls": embeds,
            }
        )

    # Base package + clean receiver hydrated from it (setup, not timed).
    source = MemoryDB(corpus)
    base_pkg = work / f"base-{size}"
    write_package(source, base_pkg)
    receiver = MemoryDB(work / f"receiver-{size}.sqlite")
    hydrate_package(receiver, base_pkg)

    # Small change set: 1% mutations (promotion flips) + a few adds.
    changed = max(1, size // 100)
    for i in range(changed):
        source.update_promotion_state(f"sync{i:07d}", "WARM")
    for i in range(5):
        fact = f"new sync fact {size}-{i} appended after the base clone"
        source.insert(
            id=f"new{i:07d}",
            identifier=f"new-{size}-{i}",
            fact_text=fact,
            embedding=pack_embedding(embed_text(fact)),
            embedding_model="hotmem-hash-v1",
            content_hash=hashlib.sha256(f"new-{size}-{i}:{fact}".encode()).hexdigest(),
            namespace="bench",
        )
    source.commit()

    delta = work / f"delta-{size}"
    secs, peak = _timed(lambda: produce_delta(source, base_pkg, delta))
    record("delta_produce", secs, peak, 0, _dir_size(delta))

    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: apply_delta(receiver, delta))
    record(
        "delta_apply",
        secs,
        peak,
        counter.calls,
        changed + 5,
    )

    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: apply_delta(receiver, delta))
    record("delta_apply_repeat", secs, peak, counter.calls, 0)

    def _verify() -> None:
        verified = verify_delta(delta)
        verified.cleanup()

    secs, peak = _timed(_verify)
    record("delta_verify", secs, peak, 0, _dir_size(delta))

    # Full-clone fallback for the same small change: what a receiver pays
    # without incremental sync.
    fallback_pkg = work / f"reclone-{size}"
    secs, peak = _timed(lambda: write_package(source, fallback_pkg))
    record("reclone_fallback_export", secs, peak, 0, _dir_size(fallback_pkg))
    fresh = MemoryDB(work / f"fresh-{size}.sqlite")
    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: hydrate_package(fresh, fallback_pkg))
    record("reclone_fallback_hydrate", secs, peak, counter.calls, 0)

    # Full-clone reference numbers for context.
    secs, peak = _timed(lambda: write_package(source, work / f"clone-{size}"))
    record("clone_initial", secs, peak, 0, _dir_size(work / f"clone-{size}"))
    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: hydrate_package(receiver, base_pkg))
    record("hydrate_full_clone", secs, peak, counter.calls, 0)

    source.close()
    receiver.close()
    fresh.close()
    for p in (delta, fallback_pkg, work / f"clone-{size}"):
        shutil.rmtree(p, ignore_errors=True)
    for p in (work / f"receiver-{size}.sqlite", work / f"fresh-{size}.sqlite"):
        Path(p).unlink(missing_ok=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=str, default="1000,5000")
    parser.add_argument("--label", type=str, required=True)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    out_path = Path(args.out) if args.out else Path(__file__).parent / "results.json"

    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="hotmem-sync-bench-") as td:
        work = Path(td)
        for size in sizes:
            started = time.perf_counter()
            rows.extend(run_scenarios(size, work))
            print(
                f"[{args.label}] size={size}: {time.perf_counter() - started:.1f}s wall", flush=True
            )

    doc = {"runs": [{"meta": {"label": args.label}, "rows": rows}]}
    if args.append and out_path.exists():
        existing = json.loads(out_path.read_text())
        existing["runs"].append({"meta": {"label": args.label}, "rows": rows})
        doc = existing
    out_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
