# Native Helper Spike (#48) — Rust, C, or WebAssembly fast-path primitives

Status: **complete** · Profile: `reduced` · Runs: 5/arm (median + p95) · Corpus seed: 48

## TL;DR recommendation

**Do not ship a native helper yet.** Neither measured candidate clears the
issue's gate (≥3x on the 100MB+ checksum path AND clean single-wheel packaging
AND graceful fallback):

- **Checksum path (B1/B4):** both C arms are **~2.2x slower** than the current
  Python path. CPython's `hashlib` is already OpenSSL-backed C (AVX2, no SHA-NI
  on this host); a self-contained C or ctypes helper cannot beat it, and a
  Rust/PyO3 helper would bind to the same OpenSSL. The real inefficiency in the
  current path is algorithmic — verified hydration **reads every byte range
  twice** — and pure-Python fixes deliver more than any native arm:
  single-read = +16%, streaming = **1.26x faster + 10x less peak memory**
  (219 MB → 20 MB on a 100 MB range).
- **JSONL scanning path (B2):** the C scanner with full JSON validation is
  **3.55x faster** than the real `JSONLInspector._stream` on 100 MB (48x for
  scan-only) — it clears the 3x *number*, but its packaging story (ctypes +
  per-platform `.so`) is the fragile option in the matrix, and the *portable*
  native option (WASM) shows **no gain** (1.04x — per-byte host↔WASM boundary
  cost erases it). A pure-Python policy change (stop validating every line with
  `json.loads`) is worth ~5x on its own.
- **Bundle parse (B3):** dominated by `embed_text` (md5 per character trigram)
  at 37–78% of parse time. The lever is algorithmic, not native.

Follow-ups (pure Python, no new dependencies) are listed below. Revisit native
helpers when HotMem handles GB-scale file-backed memories in production, or if
JSONL inspection becomes hot enough to justify a Rust wheel spike.

## Environment

| | |
|---|---|
| CPU | Intel Core i7-10610U @ 1.80GHz (4C/8T, AVX2, **no SHA-NI**) |
| Kernel | 6.18.33.1-microsoft-standard-WSL2 |
| Python | 3.12.3 (`venv/`, hotmem 0.2.1 editable) — OpenSSL 3.0.13 for hashlib |
| C | gcc/cc 13.3.0, `-O2 -Wall -Wextra -std=c11` |
| WASM | wasmtime 48.0.0 (pip, bench-only) |
| Box | shared dev machine; loadavg 3–6 during the run (per-cell context in results.json) |

Environment quirks discovered and worked around (documented so numbers can be
interpreted honestly):

- `resource.ru_maxrss` is **broken on this kernel** (reported ~11x the true
  high-water mark; `/proc/self/status` VmHWM is correct). All RSS numbers here
  are `/proc`-sourced.
- `random.Random.randbytes` is pathologically slow on this box (~60 MB/s with
  RSS anomalies). Corpus bytes use SHAKE-256 counter-mode instead (~116 MB/s,
  constant RSS).

## Methodology

- **Real HotMem paths, not synthetic throughput.** The Python baselines call
  the actual production code: `LocalFilesystemAdapter.read_range` +
  `provenance.verify_range` (the exact double-read sequence
  `memory.hydrate_memory_detailed` performs), `JSONLInspector._stream` /
  `.inspect()` (with its production LRU checksum cache cleared per run), and
  `bundle.parse_bundle`.
- **Corpus** (`gen_corpus.py`, deterministic, seed 48, ~384 MB): binary files
  (1 KB → 100 MB), JSONL files (10/50/100 MB) shaped exactly like real
  `swap.jsonl` snapshot records (30 fields, ~1.2 KB/row) with one malformed
  line inserted at a known offset (~60% into each file), and loose bundle trees
  (100/1000/2500 dirs). Regeneration is byte-identical (verified); the manifest
  (`manifest.json`, committed) records sizes, SHA-256 digests, row counts and
  bad-line offsets.
- **Arms** run serially, each (bench, arm, input, cache-mode) cell in a fresh
  subprocess so `/proc` VmHWM peaks are per-cell. 5 timed runs per cell
  (median + p95 reported); warm mode adds one uncounted warm-up run.
- **Cold cache** via `posix_fadvise(DONTNEED)` on the input file before each
  cold run (unprivileged; not a full drop-caches, so cold numbers are a lower
  bound on true cold cost).
- **Memory guards:** each cell checks `MemAvailable` (guard = working-set
  estimate + 250 MB) and records `skipped: insufficient_memory` rather than
  thrashing. No cell skipped in this run.
- **Correctness parity is asserted, not assumed.** B1: every arm's digest must
  equal a plain-`hashlib` reference. B2: all arms must agree on row count;
  validating arms on the first-bad-line index; scanning arms on sampled line
  boundaries. B4: both arms must verify the identical manifest file set. The
  harness fails loudly on any mismatch. All parity checks passed (see tables).
- **Optional-boundary check:** with `HOTMEM_SPIKE_DISABLE_NATIVE=1`, C and WASM
  arms return `skipped: helper_unavailable` instead of crashing — the same
  graceful-degradation contract a production helper would need. Passed.

Reproduce:

```bash
cd bench/native_spike
python gen_corpus.py --profile reduced          # ~384 MB into corpus/ (gitignored)
make -C c_checksum && make -C c_jsonl           # or let the harness build them
python run_bench.py --profile reduced           # full matrix → results.json (~11 min)
python run_bench.py --quick                     # smoke: 1 run, small inputs
python run_bench.py --report                    # re-render markdown from results.json
```

The `full` profile adds a 200 MB checksum arm (auto-gated on memory) for a
quieter box. `results.json` (committed) is the source of truth; tables below
are generated from it.

## B1 — Range SHA-256 checksum (real verify_range path)

Median ms, warm / cold. Peak warm RSS MB in parens. `+N@off` = byte range
[offset, offset+N).

| file (range) | py double-read¹ | py single | py streaming | c pread | c mmap | py single gains | c mmap vs py single |
|---|---|---|---|---|---|---|---|
| 1 KB @0 | 0.3 / 0.6 (20) | 0.1 / 0.5 (19) | 0.0 / 0.4 (18) | 0.0 / 3.7 (18) | 0.0 / 7.2 (18) | 2.91x | 0.47x |
| 100 KB @0 | 0.7 / 1.7 (20) | 0.5 / 1.3 (19) | 0.8 / 1.6 (18) | 1.3 / 1.9 (18) | 1.1 / 1.8 (18) | 1.39x | 2.09x |
| 1 MB @0 | 10.0 / 13.4 (22) | 6.1 / 7.1 (20) | 5.9 / 7.9 (19) | 12.2 / 14.4 (19) | 13.2 / 15.0 (19) | 1.65x | 2.18x |
| 10 MB @0 | 93.9 / 90.5 (40) | 60.9 / 95.8 (29) | 88.4 / 110.9 (20) | 145.1 / 159.8 (19) | 131.4 / 146.0 (28) | 1.54x | 2.16x |
| 2 MB @5243003 | 23.0 / 25.2 (25) | 16.1 / 19.5 (21) | 17.6 / 21.9 (20) | 36.6 / 47.2 (19) | 31.2 / 43.7 (21) | 1.43x | 1.94x |
| **100 MB @0** | 850 / 986 (219) | 736 / 771 (118) | **673 / 835 (20)** | 1744 / 1716 (19) | 1625 / 1783 (118) | 1.16x | 2.21x |
| 25 MB @52428923 | 220 / 236 (70) | 157 / 232 (44) | 147 / 180 (20) | 356 / 383 (19) | 395 / 437 (43) | 1.40x | 2.51x |

¹ `py_current_double_read` reproduces today's verified hydration exactly:
`memory.py:244` reads the range, then `provenance.verify_range`
(`provenance.py:103`) reads it **again** and hashes it. Both reads hit the
page cache, so the second read is cheap — the extra cost is ~15% at 100 MB,
not the ~2x a naive double-I/O model predicts.

Parity: all arms produce identical digests on every case. **PASS**

Findings:

- **Native loses on hashing.** C pread/mmap are 2.2–2.5x *slower* than
  Python's single-read: `hashlib.sha256` is OpenSSL C with AVX2; the spike's
  self-contained C SHA-256 cannot match it (and linking OpenSSL from a helper
  would add packaging fragility for zero gain). On a SHA-NI host OpenSSL gets
  faster too, so this conclusion is stable.
- **The real win is algorithmic.** `py_single_read` (+16% at 100 MB) removes
  the redundant read; `py_streaming` is the best arm overall (1.26x vs current
  path, 673 ms median) and holds peak RSS at **20 MB regardless of range size**
  vs 219 MB for the current double-read path.
- **mmap is not free.** The C mmap arm faults in the whole range (RSS 118 MB on
  the 100 MB range) — it saves the copy but not the memory.

## B2 — JSONL scanning (real JSONLInspector path)

Median ms, warm / cold. `py_stream_real` = production `_stream` (count + full
per-line `json.loads` validation); `py_inspect_full` = production
`inspect(count_rows=True)` end-to-end (adds whole-file checksum + column
inference); scan-only arms count rows/sample boundaries without validation.

| file | py real _stream | py inspect() e2e | py scan-only | c scan+valid | c scan-only | wasm scan | rows |
|---|---|---|---|---|---|---|---|
| 10 MB | 122 / 123 | 179 / 181 | 23 / 32 | 34 / 40 | 2.7 / 17 | 113 / 101 | 8,894 |
| 50 MB | 1185 / 792 | 886 / 971 | 195 / 147 | 262 / 243 | 22 / 59 | 672 / 667 | 44,464 |
| **100 MB** | 1740 / 2259 | 2119 / 2695 | 319 / 395 | **490 / 567** | 36 / 169 | 1667 / 1119 | 88,914 |

Parity: rows agree across all six arms; first-bad-line index agrees across the
three validating arms; sampled line boundaries agree across the three scanning
arms. **PASS**

Findings:

- **Validation dominates the Python path.** Dropping per-line `json.loads`
  (py scan-only, 319 ms) is worth 5.5x at 100 MB. The scan loop itself (1 MiB
  chunks, `bytes.find`) is already efficient Python.
- **C scan+valid = 3.55x** over the real `_stream` — clears the 3x number —
  and C scan-only is 48x. The C validator is strict RFC 8259 (parity with
  `json.loads` verified down to escape sequences, numbers, nesting, and the
  first-bad-line offsets; the one accepted divergence is `NaN/Infinity`,
  which Python tolerates and the spike's corpus/validator do not use).
- **WASM shows no gain** (1.04x): the spike's WAT scanner runs the same
  per-byte loop in Cranelift-compiled code, but every byte crosses the
  host↔linear-memory boundary through `Memory.write`. This is the honest cost
  of the "host orchestrates I/O, module does CPU work" shape at byte
  granularity; a WASM helper would need the *whole* scan hot loop plus I/O
  batching to compete, at which point it is a second runtime (~5 MB dep) for
  what C achieves or Python nearly achieves with a policy change.
- Cold-mode anomalies (e.g. 50 MB `_stream` cold < warm) are shared-box noise;
  per-cell loadavg is recorded in results.json.

## B3 — Bundle parse profile (measurement only, no native arm)

Median ms, warm. `parse_e2e` = production `parse_bundle()` over the tree;
`embed_only` = the `embed_text()` share of that work (same texts, isolated).

| tree | parse e2e | embed only | embed share |
|---|---|---|---|
| bundles_100 | 1,093 | 856 | 78% |
| bundles_1000 | 20,038 | 9,908 | 49% |
| bundles_2500 | 37,524 | 14,015 | 37% |

Findings:

- **`embed_text` is the bundle bottleneck** (37–78% of parse time). It hashes
  every character trigram with a separate `hashlib.md5` call
  (`embed.py:44`). Markdown/YAML/JSON parsing — the surface a "WASM markdown
  parser" would target — is a small fraction of the remainder. A native parser
  would optimize the wrong 20%.
- Per-bundle costs are noisy on this loaded box (10–20 ms/bundle,
  super-linear at 1000+ bundles — GC pressure in the record-heavy parse);
  ratios, not absolute times, are the signal.
- The fix is algorithmic: batch the trigram hashing (one `hashlib` update over
  a packed buffer, or `zlib.crc32`-style rolling hash), or vectorize. That is
  a `embed.py` follow-up, not a native helper.

## B4 — Manifest verification (real whole-file checksum path)

Median ms, warm / cold. Both arms verify the identical 8-file manifest set
(~295 MB total): Python uses the exact 64 KiB chunk loop from
`storage/local.py:_checksum`; C uses pread.

| arm | warm | cold | files verified |
|---|---|---|---|
| py_hashlib | 1,966 | 3,497 | 8/8 |
| c_pread | 3,278 | 4,520 | 8/8 |

Parity: identical file sets verified. **PASS**

Confirms B1 at workload scale: the C helper is 1.7x *slower* than OpenSSL-backed
`hashlib` chunked streaming — the current Python checksum implementation is
already near the platform's hash ceiling (~0.15 GB/s without SHA-NI).

## Discovered: `_stream` line-offset bug (documented, not fixed here)

Parity testing surfaced a real production bug: when a line spans the 1 MiB
read-chunk boundary, `JSONLInspector._stream` computes
`line_start = offset + pos` in `(carry+chunk)` coordinates
(`src/hotmem/inspectors/jsonl_inspector.py:129`), overstating file offsets by
`len(carry)` for every subsequent line.

Evidence on `events_10mb.jsonl`: the generator placed the malformed line at
byte 6,292,320 (manifest-committed; confirmed by reading the file). The real
`_stream` reports offset 6,292,650 — off by +330 (the carry length when that
line's chunk boundary was crossed). The C scanner reports 6,292,320 exactly.
Affected outputs: `unsupported_reason` offsets and, when chunk-spanning occurs
within the first `sample_size` lines, `byte_ranges` (both feed
`FileInspection` → API/MCP/CLI). Out of spike scope to fix; recommended
follow-up: `line_start = base + pos` where `base = offset - len(carry)`
(the replica's arithmetic, which achieves full parity with the C scanner).

## Packaging matrix

| Option | Dependency | Install | Portability | Fallback | Maintenance | Verdict |
|---|---|---|---|---|---|---|
| C via ctypes (this spike) | bundled `.so` per platform | build-from-source or ship N binaries | fragile: ABI/glibc coupling, needs per-OS build matrix | Python path (works) | highest | ❌ measured 2.2x slower on checksum; only wins on JSONL scan where the win is real but packaging is worst |
| WASM via wasmtime | `wasmtime` wheel (~5 MB, pure wheels for all majors) | `pip install hotmem[wasm]` | excellent: CPU/OS-agnostic | Python path (works) | low, but adds a second runtime | ❌ measured no gain (1.04x) at byte granularity |
| Rust via PyO3 (analysis only — not built here: no toolchain on box) | `hotmem-native` wheel via maturin | optional extra; manylinux/macOS/Windows wheels via CI | good | Python path (works) | medium-high: Rust toolchain + 3-OS × N-Python CI matrix | ⚠️ plausible carrier for the C scanner's 3.55x JSONL win *if* that path ever justifies a compiled dep; would bind to the same OpenSSL for hashing, so no checksum win |
| None (pure Python fixes) | — | — | — | — | — | ✅ recommended: single-read verify, streaming hash, validation policy, embed batching |

Optional boundary (all options): helpers must stay behind an extra/import
guard, fall back to the Python path when absent, and never alter public API
defaults — demonstrated working here via `HOTMEM_SPIKE_DISABLE_NATIVE=1`.

## Recommendation vs the gate

Gate: **≥3x on the 100MB+ checksum path AND clean single-wheel packaging AND
graceful fallback.**

| Criterion | C | WASM | Outcome |
|---|---|---|---|
| ≥3x on 100MB+ checksum | 0.45x (2.2x slower) | n/a (not a checksum candidate) | ❌ fail |
| Clean single-wheel packaging | per-platform `.so` | yes, but ~5 MB second runtime | ❌ / marginal |
| Graceful fallback | yes (demonstrated) | yes (demonstrated) | ✅ |

**Recommendation: no native helper yet.** Documented bottlenecks and revisit
triggers below.

## Follow-ups recommended (all pure Python, zero new dependencies)

1. **Single-read verify in the hydrate path** — `memory.hydrate_memory_detailed`
   reads the range, then `verify_range` re-reads it; hash the already-read
   bytes (or add a streaming verify API). Expected: +16% on verified hydration
   of large ranges, and removes a redundant range copy.
2. **Streaming range hash for large ranges** — chunked seek+hash (the
   `py_streaming` arm): 1.26x vs current path at 100 MB and peak RSS 20 MB vs
   219 MB. Natural home: `provenance.verify_range` / adapter layer, switching
   on range size.
3. **Fix the `_stream` offset bug** (see above) — correctness, one line of
   arithmetic, plus a regression test with a chunk-boundary-spanning fixture
   (the spike's fixtures are reusable).
4. **JSONL validation policy** — `_stream` pays `json.loads` per line (5.5x of
   the scan cost). Options: validate only sampled lines, or make full
   validation opt-in. Semantics decision for #53 — the data says the current
   default is expensive at 100 MB scale.
5. **`embed_text` batching** (if bundle-load time matters) — 37–78% of
   `parse_bundle`; batch the per-trigram md5 calls or switch to a cheaper
   rolling hash. Keep `hotmem-hash-v1` semantics compatible or version the
   embedding model string.

## Revisit triggers for native helpers

- GB-scale file-backed memories in production where 0.15–2 GB/s hashing becomes
  a wall (note: SHA-NI hosts raise the OpenSSL ceiling too).
- JSONL/inspection becoming hot enough that a Rust scanner wheel (maturin,
  optional extra) is justified by the measured 3.5x+ — start from this spike's
  `scan.c` semantics, which already have a verified parity contract.
- Any Parquet/Arrow *metadata* scanning need — remains out of scope for core
  (HotMem references and inspects files; it does not become a query engine).

## Repository layout / what ships

```
bench/native_spike/            # spike only — NOT part of the hotmem package or test suite
  gen_corpus.py                # deterministic corpus generator (seed 48)
  manifest.json                # committed: sizes, SHA-256, row counts, bad-line offsets
  py_baseline.py               # real-path arms + pure-Python candidates + scan replica
  c_checksum/                  # sha256.{c,h} + range_hash.c + Makefile (self-contained)
  c_jsonl/                     # scan.{c,h} (line scan + strict JSON validator) + Makefile
  wasm_parser/scanner.wat      # WAT scanner, compiled at runtime by wasmtime
  benchlib.py                  # loaders (graceful-unavailable), /proc RSS, fadvise, guards
  bench_worker.py              # per-cell subprocess worker (timing + self-verification)
  run_bench.py                 # orchestrator + parity assertions + markdown report
  results.json                 # committed: full run output (source of truth)
  corpus/                      # gitignored (~384 MB generated)
  *.so                         # gitignored (built on demand)
```

Nothing under `src/`, `tests/`, or `pyproject.toml` was modified. `wasmtime`
is a bench-only venv dependency, not a project dependency. The Python/FastAPI/
SQLite path remains the default, dependency-free path; native code stays out of
the runtime.
