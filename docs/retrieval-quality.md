# Retrieval Quality

How HotMem measures retrieval quality, what the current stack is, and what
the evidence says today (#77).

## What HotMem currently uses

Ranking combines three signals with fixed weights:

```text
cosine(hotmem-hash-v1) * 0.6  +  normalized FTS5 BM25 * 0.2  +  importance * 0.2
```

- `hotmem-hash-v1` is a deterministic 64-dimensional character-trigram hash
  embedding (see `src/hotmem/embed.py`). It is portable and needs no model
  download — but it is **not** a learned semantic embedding. It matches
  surface character patterns, so paraphrases ("building cards stop working"
  vs "badges deactivate") can rank far below exact term matches.
- SQLite FTS5 BM25 is lexical full-text matching.
- Importance is a static per-memory weight (0.5 by default).

Weighted score fusion is a **single-stage ranker** — there is no second-stage
reranker, no MMR, and no entity extraction anywhere in the pipeline.

## Running the benchmark

One command, fully offline:

```bash
uv run python scripts/retrieval_eval.py \
  --output bench/retrieval/latest.json \
  --report bench/retrieval/latest.md
```

Fixtures live in `bench/retrieval/` (60 memories, 48 graded queries across
eight required categories — see that directory's README). The harness
ingests through the production database path and runs every query through
`search_memories()`; the ranking formula is never copied into the evaluator.

It also verifies the clone path end to end: the ingested instance is
exported as a verified package and hydrated into a clean database, and every
query must return identical ordered ids and scores afterwards.

## Measured baseline (unchanged stack)

Committed as `bench/retrieval/baseline.json` and guarded by a regression
test — unacknowledged ranking changes fail CI. Highlights from the current
baseline:

| Metric | Value |
|---|---|
| Recall@1 / Recall@5 | 0.310 / 0.690 |
| MRR@5 / nDCG@5 | 0.537 / 0.586 |
| Exact-lexical Recall@5 | 1.000 |
| Semantic-paraphrase Recall@5 | 0.333 |
| Duplicate-slot rate (all 48 queries) | 0.075 |
| Duplicate-slot rate (near-duplicate diversity category) | 0.300 |
| Clone equivalence | 1.000 (zero drift) |

**Deterministic recommendation:** semantic Recall@5 trails exact-lexical
by 66.7 percentage points (threshold 15pp) — the evidence points at #78
(portable derived-index contract and optional semantic embedder).

**#80 gate denominator (reconciled):** duplicate occupancy is measured on
two denominators. The 48-query aggregate (7.5%) dilutes the
`near_duplicate_diversity` category, where the near-duplicates actually
live (30.0% — above the 20% threshold). #80's entry gate reads the
`near_duplicate_diversity` category; the aggregate is reported for
context. The recommendation rule stays semantic-first: #80 is evaluated
only after #78 lands, when a committed post-#78 report re-measures both
denominators and names the remaining failure. Until that report exists,
#80 stays open and no reranking hook is added.

## What the benchmark does not prove

- Fixtures are synthetic and small; results are local diagnostics on one
  machine, not cross-machine or enterprise-scale guarantees.
- No tenant authorization or isolation is claimed: cross-project leakage is
  measured as observed behavior (search has no namespace filter).
- A ranker without abstention returns false positives on no-answer queries;
  the false-positive rate reports this honestly instead of hiding it.
- Small synthetic benchmarks must never be used for marketing claims.

## Changing ranking responsibly

#78 and #80 are gated by this evidence. #80's entry gate is the
`near_duplicate_diversity` category duplicate-slot rate, re-measured in a
committed post-#78 report; #80 remains open until that gate is met. Any
ranking change must regenerate `baseline.json` and include category-level
before/after metrics in the PR — the regression test makes silent drift
impossible.
