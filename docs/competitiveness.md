# Competitive evidence and claim boundary

**Updated:** 2026-11-18 — **Scope:** WP7 evidence that can be reproduced without calling provider APIs

This page reports what HotMem's committed benchmark establishes and identifies
what remains unmeasured. It does not claim HotMem beats Mem0 or Cognee.

## Pinned references

| Project | Reference version | Use in this report |
|---|---|---|
| HotMem | 0.2.7 source; WP3 benchmark commit [`735d78c`](https://github.com/KnowGuard-AI/HotMem/commit/735d78c) | The two local retrieval runs below |
| Mem0 Python SDK | [`v2.2.1`](https://github.com/mem0ai/mem0/releases/tag/v2.2.1) | Reference release only; not installed or benchmarked |
| Cognee | [`v1.6.7`](https://github.com/topoteretes/cognee/releases/tag/v1.6.7) | Reference release only; not installed or benchmarked |

The Mem0 and Cognee tags pin the versions considered for a future comparison;
they are not evidence that either system was run. The current importer work
validates local file formats and does not exercise either product's extraction
or retrieval pipeline.

## Reproduce HotMem's results

The reports use the same deterministic synthetic corpus of 1,000 memories and
368 graded queries. The semantic run uses the pinned local model; neither run
calls a hosted provider.

```sh
uv run python scripts/retrieval_eval.py \
  --output /tmp/hotmem-wp7-hash.json \
  --compare-to bench/retrieval/wp3-hash.json

hotmem model fetch
uv run python scripts/retrieval_eval.py \
  --embedder local-semantic \
  --output /tmp/hotmem-wp7-semantic.json \
  --compare-to bench/retrieval/wp3-semantic.json
```

For the semantic command, install the optional semantic dependency first as
described in the [retrieval benchmark guide](https://github.com/KnowGuard-AI/HotMem/blob/735d78c/bench/retrieval/README.md).
Each committed run is one-machine diagnostic evidence, not a service-level
guarantee. MRR@5 and Recall@5 are scored over 322 queries with relevant results;
false-positive rate is calculated over all 368 queries, including no-answer
queries.

## Results

| HotMem embedder | MRR@5 | Recall@5 | False-positive rate | Search p50 / p95 | Clone equivalence |
|---|---:|---:|---:|---:|---:|
| `hotmem-hash-v1` | 0.483 | 0.592 | 0.876 | 11.881 / 16.585 ms | 1.000 |
| `local-m2v/potion-base-8M` | 0.864 | 0.929 | 0.801 | 32.063 / 50.976 ms | 1.000 |

Sources: [`wp3-hash.json`](https://github.com/KnowGuard-AI/HotMem/blob/735d78c/bench/retrieval/wp3-hash.json) and
[`wp3-semantic.json`](https://github.com/KnowGuard-AI/HotMem/blob/735d78c/bench/retrieval/wp3-semantic.json). On this fixture,
the semantic embedder improves HotMem's retrieval metrics over its hash
fallback, with higher local search latency. The semantic run still has a 0.270
near-duplicate slot rate and a 0.801 false-positive rate; it does not establish
production behavior.

## What the comparison supports

- HotMem can run its retrieval evaluation locally, and its snapshot/hydration
  checks report clone equivalence of 1.000 on this fixture.
- The local semantic model improves this fixture's retrieval scores over the
  hash fallback.
- These results say nothing about Mem0 or Cognee retrieval quality, latency,
  memory use, or extraction quality. Their result cells are intentionally
  absent because no peer run was performed.
- HotMem has a documented, verifiable JSONL interchange and import paths for
  Mem0 history and Cognee COGX exports. That establishes a migration path; it
  does not establish a head-to-head advantage.
- HotMem core does not perform LLM extraction; see the
  [WP4 extraction decision](decisions/extraction-boundary.md).

No provider calls were made for this report. A head-to-head result would need
an approved spend limit and credentials for the competitor extraction steps,
then a controlled run of these pinned releases on the same benchmark. Until
that work is authorized and recorded, the supported claims are limited to
HotMem's measured local behavior and documented portability.
