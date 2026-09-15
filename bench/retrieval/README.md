# Retrieval evaluation fixtures (#77)

Deterministic synthetic fixtures for `scripts/retrieval_eval.py` — the
evidence base for Phase 1 (Credible Search Quality). Content is synthetic
and safe to publish: no keys, no personal data, no copied text.

- `corpus.jsonl` — 60 memories across four namespaces (finance, logistics,
  infra, hr, social) with deliberate distractors, near-duplicate groups,
  temporal revision pairs (frozen `created_at`), and two cross-project
  entity-name collisions (Meridian, Northwind).
- `queries.jsonl` — 48 graded queries, >= 6 per required category (issue
  #77): exact_lexical, semantic_paraphrase, identifier_or_entity_name,
  temporal_or_revision, near_duplicate_diversity, negative_or_no_answer,
  cross_project_isolation, snapshot_hydration_equivalence.
- `baseline.json` — committed metrics from the UNCHANGED production stack
  (hash vectors + FTS5 BM25 + importance, 0.6/0.2/0.2); guarded by
  `tests/test_retrieval_eval.py` (see the baseline regression test).
- `semantic-local-m2v.json` — the separate #78 evidence report produced by
  the optional local semantic adapter over the same fixtures. NEVER
  overwrites `baseline.json`: default-behavior evidence stays intact.

Regenerate with `uv run python bench/retrieval/gen_fixtures.py` — the
generator is deterministic and the committed bytes must not change (a
byte-diff test locks this). Intentional fixture changes require
category-level before/after evidence in the PR (issue #77 regen policy).

Cross-project honesty: search has no namespace filter, so
`cross_project_isolation` queries measure attribution/leakage as observed
behavior — the evaluator never hides wrong-project results behind a filter.

## Semantic run (#78)

The optional local semantic adapter (model2vec static embeddings, the
`[semantic]` extra) is evaluated over the same fixtures into its own
committed report. Provision the model artifact ONCE (an explicit local
download, never at test/startup/hydration time):

```sh
uv pip install -e ".[semantic]"
mkdir -p .models/potion-base-8M
uv run python -c "
from model2vec import StaticModel
from pathlib import Path
import json
model = StaticModel.from_pretrained('minishlab/potion-base-8M')
model.save_pretrained('.models/potion-base-8M')
Path('.models/potion-base-8M/hotmem-model.json').write_text(json.dumps(
    {'model': 'potion-base-8M', 'revision': 'v1', 'preprocessing': 'm2v-static-v1'}))
"
uv run python scripts/retrieval_eval.py \
  --embedder local-semantic --embedder-model-path .models/potion-base-8M \
  --output bench/retrieval/semantic-local-m2v.json
```

`.models/` is gitignored: the artifact is provisioned per environment and
pinned via its `hotmem-model.json` — the recorded identity travels in the
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
