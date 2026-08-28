#!/usr/bin/env python3
"""Subprocess-isolated benchmark worker for the native helper spike (#48).

One worker process = one (bench, arm, input, cache-mode) cell. It runs the
arm --runs times (plus one uncounted warm-up in warm mode), measures wall
time per run and /proc-based RSS, self-verifies output determinism and
parity, and emits a single JSON object on stdout. Launched by run_bench.py.

Why subprocess isolation: /proc VmHWM is a process-lifetime high-water
mark, so per-cell RSS peaks are only meaningful in a fresh process
(resource.ru_maxrss is broken on this box; see benchlib docstring).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchlib  # noqa: E402
import py_baseline as pb  # noqa: E402


def _measure(fn, *, runs: int, cache: str, cold_paths: list[str], guard_mb: int, summarize):
    """Run `fn` `runs` times under the memory guard; return the cell result."""
    avail = benchlib.mem_available_mb()
    if avail is not None and avail < guard_mb:
        return {
            "skipped": "insufficient_memory",
            "mem_available_mb": avail,
            "guard_mb": guard_mb,
        }

    rss_baseline = benchlib.vm_rss_kb()
    hwm_reset = benchlib.try_reset_hwm()

    if cache == "warm":
        fn()  # uncounted warm-up (also validates the arm before timing)

    times = []
    summaries = []
    for _ in range(runs):
        if cache == "cold":
            for p in cold_paths:
                benchlib.fadvise_dontneed(p)
        t0 = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t0)
        summaries.append(summarize(out))

    if any(s != summaries[0] for s in summaries[1:]):
        return {"error": "nondeterministic_output", "outputs": summaries[:3]}

    return {
        **benchlib.stats_ms(times),
        "rss_baseline_kb": rss_baseline,
        "rss_peak_kb": benchlib.vm_hwm_kb(),
        "hwm_reset": hwm_reset,
        "output": summaries[0],
        "mem_available_mb": avail,
    }


_BAD_RE = re.compile(r"line (\d+) \(offset (\d+)\)")


def _parse_bad(reason: str | None) -> dict | None:
    if not reason:
        return None
    m = _BAD_RE.search(reason)
    return {"index": int(m.group(1)), "offset": int(m.group(2))} if m else {"index": None}


def cmd_b1(args):
    path, offset, length = args.path, args.offset, args.length
    reference = pb.reference_range_digest(path, offset, length)

    if args.arm == "py_current_double_read":
        fn = lambda: pb.py_current_double_read(path, offset, length, reference)  # noqa: E731
    elif args.arm == "py_single_read":
        fn = lambda: pb.py_single_read(path, offset, length)  # noqa: E731
    elif args.arm == "py_streaming":
        fn = lambda: pb.py_streaming(path, offset, length)  # noqa: E731
    elif args.arm in ("c_pread", "c_mmap"):
        try:
            pread, mm = benchlib.c_checksum()
        except benchlib.NativeHelperUnavailable as err:
            return {"skipped": "helper_unavailable", "detail": str(err)}
        call = pread if args.arm == "c_pread" else mm
        fn = lambda: call(path, offset, length)  # noqa: E731
    else:
        return {"error": f"unknown b1 arm: {args.arm}"}

    def summarize(digest: str) -> dict:
        if digest != reference:
            raise AssertionError(f"parity fail: {digest} != {reference}")
        return {"digest": digest[:12] + "…"}

    try:
        # Peak live bytes for the double-read path is ~2x range; guard covers
        # that plus headroom so cells skip (recorded) instead of thrashing.
        return _measure(
            fn,
            runs=args.runs,
            cache=args.cache,
            cold_paths=[path],
            guard_mb=2 * (length >> 20) + 250,
            summarize=summarize,
        )
    except AssertionError as err:
        return {"error": "parity", "detail": str(err)}


def cmd_b2(args):
    path = args.path

    if args.arm == "py_stream_real":
        fn = lambda: pb.py_stream_real(path)  # noqa: E731

        def summarize(r):
            bad = r["first_bad"]
            return {
                "rows": r["row_count"],
                "first_bad_index": bad["index"] if bad else None,
                "first_n": [list(t) for t in (r["byte_ranges"] or [])],
            }

    elif args.arm == "py_inspect_full":
        from hotmem.inspectors.jsonl_inspector import JSONLInspector
        from hotmem.storage.local import LocalFilesystemAdapter, _checksum

        adapter = LocalFilesystemAdapter()
        meta = adapter.metadata(path)
        insp = JSONLInspector()

        def fn():
            # Production LRU-caches whole-file checksums per path; clear it so
            # every run pays the honest cold full-inspect cost.
            _checksum.cache_clear()
            return insp.inspect(path, adapter, meta, count_rows=True)

        def summarize(fi):
            bad = _parse_bad(fi.unsupported_reason)
            return {
                "rows": fi.row_count,
                "first_bad_index": bad["index"] if bad else None,
                "first_n": [list(t) for t in (fi.byte_ranges or [])],
                "checksum": fi.checksum[:12] + "…",
            }

    elif args.arm == "py_scan_only":
        fn = lambda: pb.py_scan_only(path)  # noqa: E731

        def summarize(r):
            return {
                "rows": r["rows"],
                "first_bad_index": None,
                "first_n": [list(t) for t in r["first_n"]],
            }

    elif args.arm == "c_scan_full":
        fn = lambda: benchlib.c_scan(path, validate=True)  # noqa: E731

        def summarize(r):
            return {
                "rows": r["rows"],
                "first_bad_index": r["first_bad"]["index"] if r["first_bad"] else None,
                "first_n": [list(t) for t in r["first_n"]],
            }

    elif args.arm == "c_scan_only":
        fn = lambda: benchlib.c_scan(path, validate=False)  # noqa: E731

        def summarize(r):
            return {
                "rows": r["rows"],
                "first_bad_index": None,
                "first_n": [list(t) for t in r["first_n"]],
            }

    elif args.arm == "wasm_scan":
        try:
            scanner = benchlib.wasm_scanner()
        except benchlib.NativeHelperUnavailable as err:
            return {"skipped": "helper_unavailable", "detail": str(err)}
        fn = lambda: scanner.scan_file(path)  # noqa: E731

        def summarize(r):
            return {
                "rows": r["rows"],
                "first_bad_index": None,
                "first_n": [list(t) for t in r["first_n"]],
            }

    else:
        return {"error": f"unknown b2 arm: {args.arm}"}

    try:
        return _measure(
            fn,
            runs=args.runs,
            cache=args.cache,
            cold_paths=[path],
            guard_mb=150,
            summarize=summarize,
        )
    except benchlib.NativeHelperUnavailable as err:
        return {"skipped": "helper_unavailable", "detail": str(err)}


def cmd_b3(args):
    tree = Path(args.path)
    dirs = sorted(d for d in tree.iterdir() if d.is_dir())

    if args.arm == "parse_e2e":
        from hotmem.bundle import parse_bundle

        def fn():
            total = 0
            warns = 0
            for d in dirs:
                records, warnings = parse_bundle(d)
                total += len(records)
                warns += len(warnings)
            return (total, warns)

        def summarize(r):
            return {"records": r[0], "warnings": r[1], "bundles": len(dirs)}

    elif args.arm == "embed_only":
        # Gather the same fact texts parse_bundle would embed (memory body,
        # facts.json items, events.jsonl lines) — uncounted one-time setup.
        texts: list[str] = []
        for d in dirs:
            texts.append((d / "memory.md").read_text(encoding="utf-8"))
            for fact in json.loads((d / "facts.json").read_text(encoding="utf-8")):
                texts.append(fact["fact"])
            with open(d / "events.jsonl", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        texts.append(json.loads(line)["event"])

        from hotmem.embed import embed_text

        def fn():
            return sum(len(embed_text(t)) for t in texts)

        def summarize(r):
            return {"vectors": r, "texts": len(texts)}

    else:
        return {"error": f"unknown b3 arm: {args.arm}"}

    return _measure(
        fn,
        runs=args.runs,
        cache=args.cache,
        cold_paths=[],
        guard_mb=150,
        summarize=summarize,
    )


def cmd_b4(args):
    manifest = json.loads(benchlib.MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = [
        (e["name"], e["size"], e["sha256"])
        for e in manifest["checksum_files"] + manifest["jsonl_files"]
    ]
    paths = [str(benchlib.CORPUS_DIR / name) for name, _, _ in entries]

    if args.arm == "py_hashlib":
        import hashlib

        def fn():
            ok = 0
            for (name, _size, want), path in zip(entries, paths, strict=True):
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    # Same 64 KiB chunk loop as storage/local.py _checksum.
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                if h.hexdigest() != want:
                    raise AssertionError(f"manifest mismatch: {name}")
                ok += 1
            return ok

    elif args.arm == "c_pread":
        try:
            pread, _ = benchlib.c_checksum()
        except benchlib.NativeHelperUnavailable as err:
            return {"skipped": "helper_unavailable", "detail": str(err)}

        def fn():
            ok = 0
            for (name, size, want), path in zip(entries, paths, strict=True):
                if pread(path, 0, size) != want:
                    raise AssertionError(f"manifest mismatch: {name}")
                ok += 1
            return ok

    else:
        return {"error": f"unknown b4 arm: {args.arm}"}

    def summarize(r):
        return {"files_verified": r}

    try:
        return _measure(
            fn,
            runs=args.runs,
            cache=args.cache,
            cold_paths=paths,
            guard_mb=150,
            summarize=summarize,
        )
    except AssertionError as err:
        return {"error": "parity", "detail": str(err)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", required=True, choices=["b1", "b2", "b3", "b4"])
    ap.add_argument("--arm", default="")
    ap.add_argument("--path", default="")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--length", type=int, default=0)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--cache", choices=["warm", "cold"], default="warm")
    args = ap.parse_args()

    dispatch = {"b1": cmd_b1, "b2": cmd_b2, "b3": cmd_b3, "b4": cmd_b4}
    result = dispatch[args.bench](args)
    result.update(
        bench=args.bench,
        arm=args.arm,
        path=args.path,
        offset=args.offset,
        length=args.length,
        cache=args.cache,
        loadavg=benchlib.loadavg(),
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
