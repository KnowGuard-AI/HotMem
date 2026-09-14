# Interchange benchmarks (#67 / #68 / #69)

`run_bench.py` measures export, verification, and restore at representative
corpus sizes against the `main` baseline. Metrics: wall time, records/s,
**tracemalloc peak** (Python allocations — the structural memory bound), and
**embedding calls** (spy over every resolution site). Run:

```bash
uv run python bench/interchange/run_bench.py --sizes 10000,50000,100000 --label branch
git archive main | tar -x -C /tmp/hotmem-main
PYTHONPATH=/tmp/hotmem-main/src uv run python bench/interchange/run_bench.py \
    --sizes 10000,50000,100000 --label main --append
```

The baseline run loads `main`'s code via `PYTHONPATH` (verified by the
`hotmem_file` recorded in `results.json`), so both runs share one machine and
one dependency set. Generated corpora go to `bench/interchange/corpus/`
(gitignored); `results.json` is committed as PR evidence.

## Evidence — 100,000 records (Apple Silicon, CPython 3.13, single run)

| Scenario | main s | branch s | main MB | branch MB |
|---|---:|---:|---:|---:|
| export_plain | 7.83 | 8.21 | 226.5 | **3.0** |
| export_gz | 12.96 | 13.12 | 226.5 | **3.2** |
| export_plain_noemb | 7.18 | 7.48 | 198.2 | **2.4** |
| restore_initial | 15.00 | 21.02 | 273.1 | **4.2** |
| restore_repeat | 2.74 | 5.79 | 26.7 | **2.3** |
| restore_reembed | 55.22 | 38.80 | 273.1 | **3.9** |
| package_export | — | 8.10 | — | 23.8 |
| package_export_gz | — | 14.97 | — | 23.8 |
| package_verify | — | 1.06 | — | 0.3 |
| package_restore_initial | — | 17.80 | — | 4.2 |
| package_restore_repeat | — | 4.93 | — | 2.3 |

10k/50k ladders are in `results.json` and show the same shape.

## Reading the numbers

- **Memory is structural, not tuned.** `main` materializes every row
  (all_rows) and the entire destination hash set: peaks grow linearly to
  226–273 MB at 100k records. This branch streams exports
  (`iter_rows`, fetchmany) and dedups against the database in bounded
  batches: peaks stay at **2–4 MB flat** across 10k→100k — a ~60–70×
  reduction, and the curve no longer scales with store size.
  `package_export` peaks at ~24 MB at 100k because `logical_id` holds every
  content hash string (64 chars each) — the documented O(N)-strings cost of
  content-derived logical identity.
- **Throughput is comparable; where branch is slower, the cost is the
  contract.** Exports match `main` within ~5%. `restore_initial` pays
  ~40% for per-record normalization + validation (field preservation and
  `invalid` semantics) and batched DB-backed dedup; `restore_repeat` pays
  ~2× over main's in-memory hash set (5.8s vs 2.7s at 100k) — the price of
  never loading the destination hash set. `restore_reembed` is ~30% faster.
- **Embedding calls behave exactly as specified.** Restores from
  stored-embedding payloads reuse them: **0** embed calls (10k/50k/100k).
  The no-embedding payload re-embeds exactly N (100,000) calls. The package
  restore makes **0** embed calls when embeddings are compatible — the #69
  acceptance metric.
- **Verification is cheap and read-only**: full package verification
  (digests + record count + decompressed spool) runs at ~94k records/s.

## Reproduce

Everything is deterministic except wall-clock: same corpus facts, same
embedder (`hotmem-hash-v1`), same canonical bytes. `results.json` carries
`meta.hotmem_file` per run so reviewers can see exactly which checkout each
row came from.
