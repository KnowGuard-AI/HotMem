#!/usr/bin/env python3
"""Benchmark orchestrator for the native helper spike (#48).

Runs the full benchmark matrix serially, each cell in a fresh subprocess
(bench_worker.py), with memory guards, warm/cold cache modes, per-run
output self-verification, and cross-arm parity assertions. Writes
results.json and can re-render the markdown report from it.

Usage:
    python run_bench.py [--profile reduced|full] [--quick] [--out results.json]
    python run_bench.py --report [--out results.json]

Benchmarks:
    b1  range SHA-256 checksum  — Python current/candidate paths vs C (pread, mmap)
    b2  JSONL scanning          — real inspector paths vs C scanner vs WASM scanner
    b3  bundle parse profile    — where parse_bundle() time actually goes
    b4  manifest verification   — hashing all corpus files, py vs C
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchlib  # noqa: E402

SPIKE_DIR = Path(__file__).resolve().parent

B1_ARMS = ["py_current_double_read", "py_single_read", "py_streaming", "c_pread", "c_mmap"]
B2_ARMS = [
    "py_stream_real",
    "py_inspect_full",
    "py_scan_only",
    "c_scan_full",
    "c_scan_only",
    "wasm_scan",
]
B3_ARMS = ["parse_e2e", "embed_only"]
B4_ARMS = ["py_hashlib", "c_pread"]

CACHE_MODES = ["warm", "cold"]


def spawn(
    bench: str,
    arm: str,
    *,
    path: str = "",
    offset: int = 0,
    length: int = 0,
    runs: int,
    cache: str,
    env: dict | None = None,
    timeout: int = 1800,
) -> dict:
    cmd = [
        sys.executable,
        str(SPIKE_DIR / "bench_worker.py"),
        "--bench",
        bench,
        "--arm",
        arm,
        "--runs",
        str(runs),
        "--cache",
        cache,
    ]
    if path:
        cmd += ["--path", path, "--offset", str(offset), "--length", str(length)]
    child_env = {**os.environ, **(env or {})}
    t0 = time.time()
    res = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=child_env, cwd=str(SPIKE_DIR)
    )
    wall = round(time.time() - t0, 1)
    if res.returncode != 0:
        return {"error": (res.stderr or res.stdout)[-1500:], "spawn_s": wall}
    try:
        out = json.loads(res.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": f"bad worker output: {res.stdout[:400]}", "spawn_s": wall}
    out["spawn_s"] = wall
    return out


def _ctx() -> dict:
    return {
        "loadavg": benchlib.loadavg(),
        "mem_available_mb": benchlib.mem_available_mb(),
        "ts": time.strftime("%H:%M:%S"),
    }


# --------------------------------------------------------------------- #
# b1 — range checksum                                                    #
# --------------------------------------------------------------------- #


def run_b1(manifest: dict, runs: int, quick: bool) -> dict:
    cases = []
    for e in manifest["checksum_files"]:
        if quick and e["size"] > (10 << 20):
            continue
        cases.append((e["name"], 0, e["size"]))
        if e["size"] >= (10 << 20) and not quick:
            # Mid-file, deliberately non-page-aligned range.
            cases.append((e["name"], e["size"] // 2 + 123, e["size"] // 4))

    out_cases = []
    all_ok = True
    for name, off, ln in cases:
        path = str(benchlib.CORPUS_DIR / name)
        print(f"  [b1] {name} [{off}, +{ln}] …", flush=True)
        cell = {"file": name, "offset": off, "length": ln, "context": _ctx(), "arms": {}}
        digests = {}
        for arm in B1_ARMS:
            modes = ["warm"] if quick else CACHE_MODES
            for mode in modes:
                r = spawn("b1", arm, path=path, offset=off, length=ln, runs=runs, cache=mode)
                cell["arms"].setdefault(arm, {})[mode] = r
                if "output" in r:
                    digests[arm] = r["output"].get("digest")
        vals = {a: d for a, d in digests.items() if d}
        cell["parity_all_arms_agree"] = len(set(vals.values())) <= 1 and len(vals) == len(digests)
        all_ok &= cell["parity_all_arms_agree"]
        out_cases.append(cell)
    return {"cases": out_cases, "parity": {"all_arms_agree": all_ok}}


# --------------------------------------------------------------------- #
# b2 — JSONL scanning                                                    #
# --------------------------------------------------------------------- #


def run_b2(manifest: dict, runs: int, quick: bool) -> dict:
    files = [e["name"] for e in manifest["jsonl_files"]]
    if quick:
        files = [f for f in files if "10mb" in f]

    out_files = []
    rows_ok = True
    bad_ok = True
    scan_ok = True
    for name in files:
        path = str(benchlib.CORPUS_DIR / name)
        print(f"  [b2] {name} …", flush=True)
        cell = {"file": name, "context": _ctx(), "arms": {}}
        rows = {}
        bads = {}
        firsts = {}
        for arm in B2_ARMS:
            modes = ["warm"] if quick else CACHE_MODES
            for mode in modes:
                r = spawn("b2", arm, path=path, runs=runs, cache=mode)
                cell["arms"].setdefault(arm, {})[mode] = r
                if "output" in r:
                    rows[arm] = r["output"].get("rows")
                    bads[arm] = r["output"].get("first_bad_index")
                    firsts[arm] = tuple(map(tuple, r["output"].get("first_n") or []))
        # All arms must agree on row count.
        cell["parity_rows_all_arms"] = len(set(rows.values())) <= 1
        # Arms that validate JSON must agree on the first bad line INDEX.
        validator_arms = ("py_stream_real", "py_inspect_full", "c_scan_full")
        validators = {a: bads[a] for a in validator_arms if a in bads}
        cell["parity_first_bad_index"] = len(set(validators.values())) <= 1
        # Scan-only arms must agree on the sampled line boundaries.
        scanner_arms = ("py_scan_only", "c_scan_only", "wasm_scan")
        scanners = {a: firsts[a] for a in scanner_arms if a in firsts}
        cell["parity_scan_samples"] = len(set(scanners.values())) <= 1
        rows_ok &= cell["parity_rows_all_arms"]
        bad_ok &= cell["parity_first_bad_index"]
        scan_ok &= cell["parity_scan_samples"]
        out_files.append(cell)
    return {
        "files": out_files,
        "parity": {"rows_all_arms": rows_ok, "first_bad_index": bad_ok, "scan_samples": scan_ok},
    }


# --------------------------------------------------------------------- #
# b3 — bundle parse profile                                              #
# --------------------------------------------------------------------- #


def run_b3(manifest: dict, runs: int, quick: bool) -> dict:
    trees = [t["name"] for t in manifest["bundle_trees"]]
    if quick:
        trees = [t for t in trees if t.endswith("100")]

    out_trees = []
    for name in trees:
        path = str(benchlib.CORPUS_DIR / name)
        print(f"  [b3] {name} …", flush=True)
        cell = {"tree": name, "context": _ctx(), "arms": {}}
        for arm in B3_ARMS:
            r = spawn("b3", arm, path=path, runs=max(runs - 3, 2), cache="warm")
            cell["arms"][arm] = {"warm": r}
        out_trees.append(cell)
    return {"trees": out_trees}


# --------------------------------------------------------------------- #
# b4 — manifest verification                                             #
# --------------------------------------------------------------------- #


def run_b4(runs: int, quick: bool) -> dict:
    print("  [b4] manifest verification …", flush=True)
    cell = {"context": _ctx(), "arms": {}}
    verified = {}
    for arm in B4_ARMS:
        modes = ["warm"] if quick else CACHE_MODES
        for mode in modes:
            r = spawn("b4", arm, runs=runs, cache=mode)
            cell["arms"].setdefault(arm, {})[mode] = r
            if "output" in r:
                verified[arm] = r["output"].get("files_verified")
    cell["parity_files_verified"] = len(set(verified.values())) <= 1 and len(verified) == len(
        [a for a in B4_ARMS]
    )
    return {"arms": cell["arms"], "parity_files_verified": cell["parity_files_verified"]}


# --------------------------------------------------------------------- #
# optional-boundary check (graceful degradation)                         #
# --------------------------------------------------------------------- #


def run_boundary_check() -> dict:
    """Prove helpers degrade gracefully: run a c arm and a wasm arm with
    HOTMEM_SPIKE_DISABLE_NATIVE=1; both must report skipped=helper_unavailable
    rather than crash — the same contract a production optional helper needs."""
    print("  [boundary] native disabled fallback check …", flush=True)
    env = {"HOTMEM_SPIKE_DISABLE_NATIVE": "1"}
    binf = benchlib.CORPUS_DIR / "bin_1mb.bin"
    jf = benchlib.CORPUS_DIR / "events_10mb.jsonl"
    c1 = spawn(
        "b1", "c_pread", path=str(binf), length=binf.stat().st_size, runs=1, cache="warm", env=env
    )
    c2 = spawn("b2", "wasm_scan", path=str(jf), runs=1, cache="warm", env=env)
    ok = c1.get("skipped") == "helper_unavailable" and c2.get("skipped") == "helper_unavailable"
    return {"ok": ok, "c_pread_disabled": c1, "wasm_scan_disabled": c2}


# --------------------------------------------------------------------- #
# report                                                                 #
# --------------------------------------------------------------------- #


def _fmt_ms(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def _arm_cell(cell: dict, arm: str, mode: str) -> dict:
    return cell["arms"].get(arm, {}).get(mode, {})


def _med(cell: dict, arm: str, mode: str):
    return _arm_cell(cell, arm, mode).get("median_ms")


def _peak(cell: dict, arm: str, mode: str):
    return _arm_cell(cell, arm, mode).get("rss_peak_kb")


def _ratio(a, b) -> str:
    if a and b:
        return f"{a / b:.2f}x"
    return "—"


def report(results: dict) -> str:
    lines: list[str] = []

    host = results.get("host", {})
    lines.append(
        f"Host: {host.get('cpu', '?')} | sha_ni={host.get('sha_ni')} | "
        f"kernel {host.get('kernel', '?')} | Python {host.get('python', '?')}"
    )
    lines.append(
        f"Profile: {results.get('profile')} | runs={results.get('config', {}).get('runs')}"
        f" | corpus seed={results.get('corpus_seed')}"
    )
    lines.append("")

    # ---- B1 ----
    b1 = results.get("b1_range_checksum", {})
    lines.append("## B1 — Range SHA-256 checksum (median ms, warm / cold; peak RSS MB in parens)")
    lines.append("")
    header = (
        "| file (range) | py double-read | py single | py stream | c pread | c mmap "
        "| py-single gains | c_mmap vs py_single |"
    )
    lines.append(header)
    lines.append("|" + "---|" * 8)
    for case in b1.get("cases", []):
        size = case["length"]
        label = f"{case['file']} (+{size >> 20}MB @{case['offset']})"
        if size < (1 << 20):
            label = f"{case['file']} (+{size >> 10}KB @{case['offset']})"
        cells = []
        for arm in B1_ARMS:
            w = _med(case, arm, "warm")
            c = _med(case, arm, "cold")
            rss = _peak(case, arm, "warm")
            rss_mb = f" ({rss / 1024:.0f})" if rss else ""
            cells.append(f"{_fmt_ms(w)} / {_fmt_ms(c)}{rss_mb}")
        single_gains = _ratio(
            _med(case, "py_current_double_read", "warm"), _med(case, "py_single_read", "warm")
        )
        c_vs_py = _ratio(_med(case, "c_mmap", "warm"), _med(case, "py_single_read", "warm"))
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {single_gains} | {c_vs_py} |")
    parity = b1.get("parity", {}).get("all_arms_agree")
    lines.append(f"\nParity (all arms agree on digest): **{parity}**")
    lines.append("")

    # ---- B2 ----
    b2 = results.get("b2_jsonl_scan", {})
    lines.append("## B2 — JSONL scanning (median ms, warm / cold)")
    lines.append("")
    b2_header = (
        "| file | py real _stream | py inspect() e2e | py scan-only "
        "| c scan+valid | c scan-only | wasm scan | rows |"
    )
    lines.append(b2_header)
    lines.append("|---|---|---|---|---|---|---|---|")
    for cell in b2.get("files", []):
        vals = []
        for arm in B2_ARMS:
            vals.append(f"{_fmt_ms(_med(cell, arm, 'warm'))} / {_fmt_ms(_med(cell, arm, 'cold'))}")
        rows = next(
            (
                out
                for a in B2_ARMS
                if (out := _arm_cell(cell, a, "warm").get("output", {}).get("rows")) is not None
            ),
            None,
        )
        lines.append(f"| {cell['file']} | " + " | ".join(vals) + f" | {rows} |")
    p = b2.get("parity", {})
    lines.append(
        f"\nParity: rows all arms **{p.get('rows_all_arms')}** "
        f"| first-bad index **{p.get('first_bad_index')}** "
        f"| scan samples **{p.get('scan_samples')}**"
    )
    lines.append("")

    # ---- B3 ----
    b3 = results.get("b3_bundle_profile", {})
    lines.append("## B3 — Bundle parse profile (median ms, warm)")
    lines.append("")
    lines.append("| tree | parse e2e | embed only | embed share |")
    lines.append("|---|---|---|---|")
    for tree in b3.get("trees", []):
        pe = _med(tree, "parse_e2e", "warm")
        eo = _med(tree, "embed_only", "warm")
        share = f"{eo / pe:.0%}" if pe and eo else "—"
        lines.append(f"| {tree['tree']} | {_fmt_ms(pe)} | {_fmt_ms(eo)} | {share} |")
    lines.append("")

    # ---- B4 ----
    b4 = results.get("b4_manifest_verify", {})
    lines.append("## B4 — Manifest verification (median ms, warm / cold)")
    lines.append("")
    lines.append("| arm | warm | cold | files verified |")
    lines.append("|---|---|---|---|")
    for arm in B4_ARMS:
        w = b4.get("arms", {}).get(arm, {}).get("warm", {}).get("median_ms")
        c = b4.get("arms", {}).get(arm, {}).get("cold", {}).get("median_ms")
        fv = b4.get("arms", {}).get(arm, {}).get("warm", {}).get("output", {}).get("files_verified")
        lines.append(f"| {arm} | {_fmt_ms(w)} | {_fmt_ms(c)} | {fv} |")
    lines.append(f"\nParity (same file set verified): **{b4.get('parity_files_verified')}**")
    lines.append("")

    # ---- boundary ----
    bc = results.get("optional_boundary_check", {})
    if bc:
        lines.append(
            f"## Optional-boundary check (HOTMEM_SPIKE_DISABLE_NATIVE=1): **{bc.get('ok')}**"
        )
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=["reduced", "full"], default="reduced")
    ap.add_argument("--quick", action="store_true", help="smoke the harness: 1 run, small inputs")
    ap.add_argument("--out", default="results.json")
    ap.add_argument(
        "--report", action="store_true", help="print markdown report from --out and exit"
    )
    args = ap.parse_args()

    out_path = SPIKE_DIR / args.out

    if args.report:
        if not out_path.exists():
            print(f"no results at {out_path}", file=sys.stderr)
            return 1
        print(report(json.loads(out_path.read_text(encoding="utf-8"))))
        return 0

    if not benchlib.MANIFEST_PATH.exists() or not benchlib.CORPUS_DIR.exists():
        hint = f"corpus missing — run: python gen_corpus.py --profile {args.profile}"
        print(hint, file=sys.stderr)
        return 1

    manifest = json.loads(benchlib.MANIFEST_PATH.read_text(encoding="utf-8"))
    runs = 1 if args.quick else 5

    print(f"native helper spike benchmark — profile={args.profile}, runs={runs}")
    results = {
        "spike": "#48 native helper spike",
        "profile": args.profile,
        "quick": args.quick,
        "corpus_seed": manifest.get("seed"),
        "host": benchlib.host_info(),
        "config": {"runs": runs, "cache_modes": ["warm"] if args.quick else CACHE_MODES},
    }

    t0 = time.time()
    results["b1_range_checksum"] = run_b1(manifest, runs, args.quick)
    results["b2_jsonl_scan"] = run_b2(manifest, runs, args.quick)
    results["b3_bundle_profile"] = run_b3(manifest, runs, args.quick)
    results["b4_manifest_verify"] = run_b4(runs, args.quick)
    results["optional_boundary_check"] = run_boundary_check()
    results["total_s"] = round(time.time() - t0, 1)
    results["final_context"] = _ctx()

    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path} in {results['total_s']}s")
    print(report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
