# Retrieval Quality

How HotMem measures retrieval quality, what the current stack is, and what
the evidence says today (#77, #78, #80).

## What HotMem currently uses

Ranking combines three signals with fixed weights:

```text
cosine(active embedding) * 0.6  +  normalized FTS5 BM25 * 0.2  +  importance * 0.2
```

- The default embedding is `hotmem-hash-v1`: a deterministic 64-dimensional
  character-trigram hash (see `src/hotmem/embed.py`). It is portable and
  needs no model download — but it is **not** a learned semantic embedding.
  It matches surface character patterns, so paraphrases ("building cards
  stop working" vs "badges deactivate") can rank far below exact term
  matches.
- An **optional local semantic embedder** (#78) slots in behind the same
  protocol: the `[semantic]` extra (model2vec static embeddings) loading an
  explicitly provisioned local artifact pinned by `hotmem-model.json`.
  HotMem never downloads models at import, startup, hydration, or test
  time. Rows stored under a different descriptor score zero cosine
  (mixed-space safety) and still surface via FTS/importance.
- SQLite FTS5 BM25 is lexical full-text matching.
- Importance is a static per-memory weight (0.5 by default).

Weighted score fusion is a **single-stage ranker**. An **opt-in bounded
second stage** (#80) exists since the gate was met: deterministic MMR
re-ranking over a capped candidate pool, disabled by default (the default
path is byte-identical to the single-stage ranker). There is no entity
extraction anywhere in the pipeline — annotations (#79) are preserved and
never used to rank.

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
query must return identical ordered ids and scores afterwards — under the
hash default and under the semantic runtime.

The semantic run (`--embedder local-semantic --embedder-model-path ...`) and
the reranking run (`--reranker mmr`) write **separate** committed reports;
`baseline.json` is the default-stack evidence and is never overwritten. The
README in `bench/retrieval/` documents artifact provisioning.

## Measured baseline (unchanged default stack)

Committed as `bench/retrieval/baseline.json` and guarded by a regression
test — unacknowledged ranking changes fail CI. Highlights:

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
by 66.7 percentage points (threshold 15pp) — the evidence pointed at #78
(portable embedding boundary and optional semantic embedder). #78 shipped;
the post-#78 evidence follows.

## Post-#78 evidence: the semantic runtime

Committed as `bench/retrieval/semantic-local-m2v.json` (potion-base-8M,
256-dim, provisioned locally per the runbook):

| Metric | hash default | semantic (#78) |
|---|---|---|
| Semantic-paraphrase Recall@5 | 0.333 | **1.000** |
| Exact-lexical Recall@5 | 1.000 | 1.000 (parity held; 0.0pp cost) |
| Overall Recall@5 | 0.690 | **1.000** |
| MRR@5 / nDCG@5 | 0.537 / 0.586 | **0.882 / 0.925** |
| Near-duplicate diversity duplicate-slot rate | 0.300 | 0.400 |
| Clone equivalence | 1.000 | 1.000 (same-descriptor restore reuses vectors) |

The #77 gap closed at zero lexical cost. Better similarity also groups
near-duplicates tighter — the diversity problem became MORE acute, which is
what opened the #80 gate (see `bench/retrieval/post-p2-gate-80.md`).

## Post-#80 evidence: the opt-in MMR reranker

Committed as `bench/retrieval/rerank-mmr.json` (semantic runtime, MMR
lambda 0.5, pool 50):

| Metric | semantic only | semantic + MMR |
|---|---|---|
| Near-duplicate diversity duplicate-slot rate | 0.400 | **0.167** (below the 20% gate) |
| Overall duplicate-slot rate | 0.104 | 0.021 |
| Overall Recall@5 | 1.000 | 0.885 (the documented allowance) |
| Exact-lexical Recall@5 | 1.000 | 1.000 |
| Clone equivalence | 1.000 | 1.000 (deterministic under the reranker) |
| Search latency p50 | ~2.0 ms | ~4.5 ms (bounded, one batched fetch) |

The recall allowance is real and structural: the fixtures grade
near-duplicate cluster members and revision pairs as relevant while sharing
the duplicate clusters' similarity range (relevant pairs 0.81..0.92 vs
duplicate clusters 0.83..0.99), so no similarity-based rule can suppress
duplicates without that cost; metadata-driven demotion is out of scope by
canon (annotations never rank). The lambda default (0.5) is evidence-driven
— relevance-dominant settings leave the clusters in place. Reranking stays
opt-in (`--reranker mmr`, default `none`).

## Operational examples

**Run the semantic sidecar** (model provisioned once, per the
`bench/retrieval/` runbook):

```bash
uv pip install -e ".[semantic]"
hotmem serve --embedder local-semantic \
  --embedder-model-path .models/potion-base-8M
```

`/v1/health` reports the sanitized active descriptor
(`{"embedding": {"model": "local-m2v/potion-base-8M/rev:v1/norm:l2/pp:m2v-static-v1",
"dim": 256}}`).

**Restore a foreign-space package without the optional extra:** the restore
rebuilds every vector from text under the active embedder and reports it
(`embedding_rebuilt`), keeps the canonical record, and the data stays
searchable — never hostage to a provider. Same-descriptor restores reuse
every vector (`embedding_reused`) with zero embed work.

**Deliberate reindex after switching embedders:** rebuild the derived vector
index once (`POST /v1/vector-index/rebuild`); the marker is stamped from the
active descriptor, so a switch reads stale and falls back to the exact scan
until rebuilt — never auto-migrated at startup.

**Rolling back or switching embedders (derived index):** the vector index is
disposable derived state. Delete its directory — `<base_dir>/hotmem-vector-index/`
— before serving under a different (or pre-#78) version; search then falls
back to the deterministic SQLite scan, and you rebuild deliberately once the
runtime is settled. Never copy an index across embedding spaces, and never
let a stale marker outlive a version rollback: canonical SQLite/files/bundles
are the only state that must survive.

**Enable diversity re-ranking** where duplicates crowd the top-k:

```bash
hotmem serve --embedder local-semantic \
  --embedder-model-path .models/potion-base-8M \
  --reranker mmr --reranker-lambda 0.5
```

## What the benchmark does not prove

- Fixtures are synthetic and small; results are local diagnostics on one
  machine, not cross-machine or enterprise-scale guarantees.
- No tenant authorization or isolation is claimed: cross-project leakage is
  measured as observed behavior (search has no namespace filter).
- A ranker without abstention returns false positives on no-answer queries;
  the false-positive rate reports this honestly instead of hiding it.
- Small synthetic benchmarks must never be used for marketing claims.

## Changing ranking responsibly

#78 and #80 were gated by this evidence and are now delivered; the reports
above are the record. Any ranking change must regenerate `baseline.json`
and include category-level before/after metrics in the PR — the regression
test makes silent drift impossible. New reranking or embedding work needs
new evidence gates in the same shape: a committed report, a defined
denominator, and a documented tradeoff.
