# Sync benchmarks (#73): incremental delta vs full clone

`run_bench.py` measures the company-brain sync workflow: full clone
(#69 baseline path), verified delta produce/apply for a small change set
(1% mutations + 5 adds), idempotent replay, verification, and the
re-clone fallback a receiver would pay without incremental sync.

```bash
uv run python bench/sync/run_bench.py --sizes 1000,5000 --label branch
```

Committed `results.json` is PR evidence. Embedding calls are expected to
be zero everywhere (delta records carry compatible stored embeddings —
the #73 acceptance metric).

## Measured (Apple Silicon, CPython 3.11, single run)

| Scenario (1% change set) | 1,000 records | 5,000 records |
|---|---:|---:|
| delta_produce | 0.201 s / 1.87 MB | 0.952 s / 5.15 MB |
| **delta_apply** | **0.011 s / 0.07 MB** | **0.031 s / 0.06 MB** |
| delta_apply_repeat (idempotent) | 0.004 s / 0.04 MB | 0.011 s / 0.04 MB |
| delta_verify | 0.002 s / 0.03 MB | 0.004 s / 0.03 MB |
| reclone fallback (export + hydrate) | 0.222 s / 5.70 MB | 1.073 s / 7.53 MB |
| embed calls | 0 | 0 |

## Reading the numbers

- **Apply is the hot path and it is tiny**: applying a 1% delta costs
  ~10–30 ms and ~0.06 MB — roughly 20× faster and ~40× lighter in peak
  allocations than the re-clone fallback for the same change, and the gap
  widens with store size because apply is O(changed records) while a
  re-clone is O(store).
- **Idempotent replay is nearly free** (one fingerprint comparison per
  op, one transaction) — repeated delivery is safe to run blindly.
- **Produce is O(base)** (0.2–0.95 s): it reads the verified base payload
  to compute per-record pre-images, the documented v1 cost without
  per-record fingerprints in clone manifests (delta-v1 §10 Proposed).
- **Zero embedding calls across every scenario** — compatible stored
  embeddings are reused; no re-embedding during sync.
