"""Interchange export/verify/restore benchmark (#67/#68/#69 evidence).

Measures plain export, compressed export, package export/verify, initial
restore, re-embedding restore, and repeat restore at representative corpus
sizes. Metrics per scenario: wall time, records/second, tracemalloc peak
(Python allocations), and embedding calls. Results are written to
results.json for the PR.

Runs against BOTH this branch and a pristine `main` checkout (via
PYTHONPATH pointing at main's src) so every comparable scenario reports a
baseline; scenarios that do not exist on main (package, verify) are
reported as branch-only evidence.

Usage:
    uv run python bench/interchange/run_bench.py --sizes 10000 --label branch
    PYTHONPATH=/tmp/hotmem-main/src uv run python bench/interchange/run_bench.py \
        --sizes 10000 --label main --append results.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import hotmem  # noqa: F401  (resolution check below)

_BRANCH_MARKERS = ("hotmem.interchange", "hotmem.importers.okf")


def _on_branch() -> bool:
    import importlib.util

    return all(
        importlib.util.find_spec(m) is not None
        for m in ("hotmem.interchange", "hotmem.importers.okf")
    )


def _fact(i: int) -> str:
    return (
        f"fact {i}: acme operations deploy cadence {i % 997} uses vendor "
        f"{i % 89} with sla tier {i % 5} and quarterly review gate {i % 12}"
    )


def gen_corpus(db_path: Path, size: int) -> None:
    """Generate a deterministic source instance (corpus generation not timed)."""
    from hotmem.db import MemoryDB, MemoryRecord
    from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
    from hotmem.swap import compute_content_hash  # exists on main and this branch

    if db_path.exists():
        db_path.unlink()
    db = MemoryDB(db_path)
    batch: list[MemoryRecord] = []
    for i in range(size):
        fact = _fact(i)
        batch.append(
            MemoryRecord(
                id=f"bench{i:07d}",
                identifier=f"bench-{i:07d}",
                fact_text=fact,
                embedding=pack_embedding(embed_text(fact)),
                embedding_dim=EMBEDDING_DIM,
                embedding_model=EMBEDDING_MODEL,
                content_hash=compute_content_hash(f"bench-{i:07d}", fact),
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
    """Count embed calls through every resolution site (#78 injection).

    All hydration paths resolve against the call-time default in
    ``hotmem.interchange.compat`` (swap, package, snapshot v2 readers), so
    one counting embedder observes every site without module patching.
    """

    def __init__(self) -> None:
        import hotmem.interchange.compat as compat_mod

        self._compat = compat_mod
        self._orig = compat_mod.DEFAULT_EMBEDDER
        self.calls = 0

    def _spy(self) -> object:
        orig = self._orig
        counter = self

        class CountingEmbedder:
            @property
            def descriptor(self):
                return orig.descriptor

            def embed(self, text: str):
                counter.calls += 1
                return orig.embed(text)

        return CountingEmbedder()

    def __enter__(self) -> EmbedCounter:
        self._compat.DEFAULT_EMBEDDER = self._spy()
        return self

    def __exit__(self, *exc) -> None:
        self._compat.DEFAULT_EMBEDDER = self._orig


def _timed(fn):
    """Run fn; return (seconds, tracemalloc peak MB)."""
    tracemalloc.start()
    start = time.perf_counter()
    fn()
    seconds = time.perf_counter() - start
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return seconds, peak / (1024 * 1024)


def run_scenarios(size: int, work: Path) -> list[dict]:
    from hotmem.db import MemoryDB
    from hotmem.swap import hydrate, snapshot

    on_branch = _on_branch()
    rows: list[dict] = []

    corpus = work / f"corpus-{size}.sqlite"
    if not corpus.exists():
        gen_corpus(corpus, size)

    plain = work / f"out-{size}.jsonl"
    gz = work / f"out-{size}.jsonl.gz"
    plain_noemb = work / f"out-noemb-{size}.jsonl"

    def record(name: str, seconds: float, peak: float, embeds: int, count: int) -> None:
        rows.append(
            {
                "scenario": name,
                "size": size,
                "seconds": round(seconds, 3),
                "rec_per_s": round(count / seconds, 1) if seconds else None,
                "tracemalloc_peak_mb": round(peak, 2),
                "embed_calls": embeds,
                "branch_only": name.startswith("package_") or name == "package_verify",
            }
        )

    # ── export: plain / compressed / no-embeddings ──────────────────────
    src = MemoryDB(corpus)
    secs, peak = _timed(lambda: snapshot(src, plain))
    record("export_plain", secs, peak, 0, size)
    secs, peak = _timed(lambda: snapshot(src, gz))
    record("export_gz", secs, peak, 0, size)
    secs, peak = _timed(lambda: snapshot(src, plain_noemb, include_embeddings=False))
    record("export_plain_noemb", secs, peak, 0, size)

    if on_branch:
        from hotmem.interchange.package import write_package

        pkg = work / f"pkg-{size}"
        pkg_gz = work / f"pkg-gz-{size}"
        secs, peak = _timed(lambda: write_package(src, pkg))
        record("package_export", secs, peak, 0, size)
        secs, peak = _timed(lambda: write_package(src, pkg_gz, gz=True))
        record("package_export_gz", secs, peak, 0, size)
        src.close()

        from hotmem.interchange.hydrate import verify_package

        def _verify() -> None:
            verified = verify_package(pkg_gz)
            verified.cleanup()

        secs, peak = _timed(_verify)
        record("package_verify", secs, peak, 0, size)
    else:
        src.close()

    # ── restore: initial / re-embed / repeat ─────────────────────────────
    dst = MemoryDB(work / f"dst-{size}.sqlite")
    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: hydrate(dst, plain))
    record("restore_initial", secs, peak, counter.calls, size)
    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: hydrate(dst, plain))
    record("restore_repeat", secs, peak, counter.calls, 0)
    dst.close()

    dst2 = MemoryDB(work / f"dst-noemb-{size}.sqlite")
    with EmbedCounter() as counter:
        secs, peak = _timed(lambda: hydrate(dst2, plain_noemb))
    record("restore_reembed", secs, peak, counter.calls, size)
    dst2.close()

    if on_branch:
        from hotmem.interchange.hydrate import hydrate_package

        dst3 = MemoryDB(work / f"dst-pkg-{size}.sqlite")
        with EmbedCounter() as counter:
            secs, peak = _timed(lambda: hydrate_package(dst3, pkg))
        record("package_restore_initial", secs, peak, counter.calls, size)
        with EmbedCounter() as counter:
            secs, peak = _timed(lambda: hydrate_package(dst3, pkg))
        record("package_restore_repeat", secs, peak, counter.calls, 0)
        dst3.close()
        shutil.rmtree(pkg, ignore_errors=True)
        shutil.rmtree(pkg_gz, ignore_errors=True)

    # Clean per-size artifacts (keep corpus for cross-run reuse).
    for p in (
        plain,
        gz,
        plain_noemb,
        work / f"dst-{size}.sqlite",
        work / f"dst-noemb-{size}.sqlite",
        work / f"dst-pkg-{size}.sqlite",
    ):
        Path(p).unlink(missing_ok=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=str, default="10000,50000,100000")
    parser.add_argument("--label", type=str, required=True, help="branch | main")
    parser.add_argument("--out", type=str, default=None, help="results json path")
    parser.add_argument("--append", action="store_true", help="merge into existing results")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    out_path = Path(args.out) if args.out else Path(__file__).parent / "results.json"

    meta = {
        "label": args.label,
        "hotmem_file": str(Path(hotmem.__file__).parent.parent),
        "on_branch": _on_branch(),
        "python": sys.version.split()[0],
    }
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="hotmem-bench-") as td:
        work = Path(td)
        for size in sizes:
            t0 = time.perf_counter()
            scenario_rows = run_scenarios(size, work)
            wall = time.perf_counter() - t0
            print(f"[{args.label}] size={size}: {wall:.1f}s wall", flush=True)
            rows.extend(scenario_rows)

    if args.append and out_path.exists():
        existing = json.loads(out_path.read_text())
        existing["runs"].append({"meta": meta, "rows": rows})
        out_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    else:
        out_path.write_text(
            json.dumps({"runs": [{"meta": meta, "rows": rows}]}, indent=2, sort_keys=True) + "\n"
        )
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
