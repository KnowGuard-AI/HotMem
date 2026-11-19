# Retrieval evaluation fixtures (#77)

Deterministic synthetic fixtures for `scripts/retrieval_eval.py` — the
evidence base for Phase 1 (Credible Search Quality). Content is synthetic
and safe to publish: no keys, no personal data, no copied text.

- `corpus.jsonl` — 1,000 memories across five namespaces (finance, logistics,
  infra, hr, social). It combines the original hand-designed cases with
  category-specific generated records, 500 extra distractors, 40 added
  duplicate groups, 40 frozen revision pairs, and 40 entity collisions.
- `queries.jsonl` — 368 graded queries, at least 40 per required category
  (issue #77): exact_lexical, semantic_paraphrase, identifier_or_entity_name,
  temporal_or_revision, near_duplicate_diversity, negative_or_no_answer,
  cross_project_isolation, snapshot_hydration_equivalence. Cross-project
  queries record their intended namespace; ordinary runs ignore it.
- `baseline-corpus.jsonl` / `baseline-queries.jsonl` — the frozen original
  60-memory / 48-query inputs for `baseline.json` and the WP2 scoped-threshold
  reports. They preserve the historical comparison and are not overwritten by
  scale-fixture generation.
- `baseline.json` — committed metrics from the unchanged hash stack over that
  original 60/48 fixture; guarded by `tests/test_retrieval_eval.py`.
- `semantic-local-m2v.json` — the separate #78 evidence report produced by
  the optional local semantic adapter over the historical 60/48 fixture. NEVER
  overwrites `baseline.json`: default-behavior evidence stays intact.
- `post-p2-gate-80.md` — the #80 go/no-go decision record: the entry gate
  (diversity duplicate-slot rate) measured post-#78 under both embedders.
- `rerank-mmr.json` — the #80 evidence report over the historical 60/48
  fixture: the semantic runtime plus the opt-in MMR reranker (lambda 0.5,
  pool 50). Same rule: separate
  artifact, never an overwrite of default-behavior evidence.
- `wp2-semantic-scope-threshold.json` / `.md` — the #141 WP2 semantic run:
  per-query namespace scope and an opt-in 0.4 score cutoff.
- `wp2-hash-scope-threshold.json` / `.md` — the same WP2 settings under the
  hash fallback, showing why the threshold must be tuned per embedder.
- `wp3-hash.json` / `.md` and `wp3-semantic.json` / `.md` — the expanded
  1,000-memory / 368-query reports used by the CI retrieval-quality gate.

## Reranking run (#80)

Reranking is opt-in (default off — the exact pre-#80 ranking) and bounded.
The gate-opening measurement and the recall tradeoff are recorded in
`post-p2-gate-80.md`; reproduce the committed report with:

```sh
uv run python scripts/retrieval_eval.py \
  --embedder local-semantic --embedder-model-path .models/potion-base-8M \
  --reranker mmr --output bench/retrieval/rerank-mmr.json
```

Measured (lambda 0.5, pool 50, semantic runtime): the near-duplicate
diversity duplicate-slot rate drops 0.400 -> 0.167 (below the 20% gate)
and clone equivalence stays 1.0 under the reranker. The recall allowance
is real and documented: overall Recall@5 1.000 -> 0.885 — the fixtures
grade near-duplicate cluster members and revision pairs as relevant while
sharing the duplicate clusters' similarity range (0.81..0.92 vs
0.83..0.99), so similarity alone cannot suppress duplicates without that
cost; metadata-driven demotion is out of scope by canon. Exact-lexical
Recall@5 is unchanged at 1.000. Overhead is bounded and measured: p50
search latency ~2.0ms -> ~4.5ms for a 50-candidate pool at 256 dims
(one batched embedding fetch + in-memory selection with incremental
max-similarity caching; no second scan).

Regenerate with `uv run python bench/retrieval/gen_fixtures.py` — the
generator is deterministic and the committed bytes must not change (a
byte-diff test locks this). Intentional fixture changes require
category-level before/after evidence in the PR (issue #77 regen policy).

Cross-project honesty: default runs are unscoped, so
`cross_project_isolation` measures observed leakage. A separate WP2 run opts
into the fixture's intended namespace and reports the remaining wrong-scope
slots; it does not change default behavior.

## Semantic run (#78)

The optional local semantic adapter (model2vec static embeddings, the
`[semantic]` extra) is evaluated over the same fixtures into its own
committed report. Provision the model artifact ONCE (an explicit local
download, never at test/startup/hydration time):

```sh
uv pip install -e ".[semantic]"
hotmem model fetch
uv run python scripts/retrieval_eval.py \
  --embedder local-semantic \
  --output bench/retrieval/semantic-local-m2v.json
```

`hotmem model fetch` stores the checksum-verified artifact under
`~/.hotmem/models/potion-base-8M/`. The model remains local and is never fetched
by the evaluator or runtime implicitly; the recorded identity travels in the
report's `runtime` block, making the evidence reproducible and auditable.
Latency and cold-start numbers are one-machine diagnostics, not
cross-machine guarantees.

Measured (potion-base-8M, 256-dim): semantic-paraphrase Recall@5 0.333 ->
1.000 with exact-lexical parity held at 1.000 (the #77 66.7pp gap closed
at 0.0pp lexical cost, within the <=5pp allowance); MRR@5 0.537 -> 0.882,
nDCG@5 0.586 -> 0.925; clone equivalence 1.000 under the semantic space
(same-descriptor restore reuses every vector). The near-duplicate
diversity duplicate-slot rate is 0.400 (hash: 0.300) — above the #80 gate
under both embedders; see the post-#78 report before any reranking work.

## Namespace and abstention run (WP2)

The optional query namespace scope prevents cross-project results from
crossing the requested scope; the inclusive score cutoff lets search return
no results. Reproduce the measured semantic run with:

```sh
hotmem model fetch
uv run python scripts/retrieval_eval.py \
  --embedder local-semantic --use-query-namespace --min-score 0.4 \
  --output bench/retrieval/wp2-semantic-scope-threshold.json \
  --report bench/retrieval/wp2-semantic-scope-threshold.md
uv run python scripts/retrieval_eval.py \
  --use-query-namespace --min-score 0.4 \
  --output bench/retrieval/wp2-hash-scope-threshold.json \
  --report bench/retrieval/wp2-hash-scope-threshold.md
```

On the historical 60-memory / 48-query fixture, semantic false-positive rate is 0.767 in
the default run and 0.154 with scope plus the 0.4 cutoff. Recall@5 is 1.000
and 0.786 respectively. All six no-answer queries abstain. Across the six
scoped cross-project queries, wrong-namespace slots fall from 8/30 to 0/9.
Only 34/48 semantic queries return results. With the same cutoff, the hash
run returns results for only 15/48 queries and Recall@5 falls to 0.302. These
synthetic small-fixture measurements show why the cutoff must be tuned per
embedder; exact scoped/cutoff search can also cost more than the ANN path.

WP2 keeps the 0.6 / 0.2 / 0.2 fusion weights. This run isolates scope and
cutoff behavior, not alternate blend weights. The WP3 reports below use the
larger fixture to measure both embedders; no weight change is implied.

## Expanded benchmark and CI gate (WP3)

The deterministic generator builds 1,000 memories and 368 queries across the
same eight categories. The original 60/48 fixture is retained separately so
the historical `baseline.json` regression test remains meaningful.

Generate the default-stack report and compare it with the committed quality
baseline using:

```sh
uv run python scripts/retrieval_eval.py \
  --output /tmp/wp3-hash.json \
  --compare-to bench/retrieval/wp3-hash.json
```

With the pinned model already provisioned, check the semantic run using:

```sh
uv run python scripts/retrieval_eval.py \
  --embedder local-semantic \
  --output /tmp/wp3-semantic.json \
  --compare-to bench/retrieval/wp3-semantic.json
```

`ci.yml` runs both gates on Python 3.13 and explicitly fetches the pinned
30 MB model once per CI run. The gate compares ordered result IDs and quality
metrics; it ignores timing and raw similarity-score differences that can vary
by machine. Local timings are diagnostics, not cross-machine performance
guarantees.

Measured on this machine, both runs ingested all 1,000 memories and preserved
clone equivalence at 1.0. Hash Recall@5 is 0.592 with a 0.876 false-positive
rate; semantic Recall@5 is 0.929 with a 0.801 false-positive rate. Semantic
paraphrase Recall@5 is 0.957 versus 0.587 for hash, while semantic
near-duplicate diversity has a 0.270 duplicate-slot rate. These are synthetic
benchmark results, not a head-to-head comparison or production workload
claim. The high false-positive rates remain visible in both reports.
