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

Regenerate with `uv run python bench/retrieval/gen_fixtures.py` — the
generator is deterministic and the committed bytes must not change (a
byte-diff test locks this). Intentional fixture changes require
category-level before/after evidence in the PR (issue #77 regen policy).

Cross-project honesty: search has no namespace filter, so
`cross_project_isolation` queries measure attribution/leakage as observed
behavior — the evaluator never hides wrong-project results behind a filter.
