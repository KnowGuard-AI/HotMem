# Retrieval evaluation report

Corpus: 1000 memories (1000 ingested), 368 queries. Stack: cosine 0.6 / fts5_bm25 0.2 / importance 0.2 over local-m2v/potion-base-8M/rev:v1/norm:l2/pp:m2v-static-v1 (256-dim).

## Overall metrics

| Metric | Value |
|---|---|
| Recall@1 | 0.697 (n=322) |
| Recall@5 | 0.929 (n=322) |
| MRR@5 | 0.864 (n=322) |
| nDCG@5 | 0.828 (n=322) |
| False-positive rate | 0.801 (n=368) |
| Duplicate-slot rate | 0.035 (n=368) |

## Per category

| Category | Recall@5 | MRR@5 | nDCG@5 | FP rate | Queries |
|---|---|---|---|---|---|
| cross_project_isolation | 0.935 (n=46) | 0.783 (n=46) | 0.696 (n=46) | 0.809 (n=46) | 46 |
| exact_lexical | 0.978 (n=46) | 0.880 (n=46) | 0.904 (n=46) | 0.804 (n=46) | 46 |
| identifier_or_entity_name | 0.913 (n=46) | 0.672 (n=46) | 0.730 (n=46) | 0.817 (n=46) | 46 |
| near_duplicate_diversity | 0.717 (n=46) | 1.000 (n=46) | 0.761 (n=46) | 0.570 (n=46) | 46 |
| negative_or_no_answer | n/a | n/a | n/a | 1.000 (n=46) | 46 |
| semantic_paraphrase | 0.957 (n=46) | 0.899 (n=46) | 0.914 (n=46) | 0.809 (n=46) | 46 |
| snapshot_hydration_equivalence | 1.000 (n=46) | 0.868 (n=46) | 0.901 (n=46) | 0.800 (n=46) | 46 |
| temporal_or_revision | 1.000 (n=46) | 0.946 (n=46) | 0.894 (n=46) | 0.796 (n=46) | 46 |

## Clone equivalence (verified package -> clean hydration)

- Identical ordered ids + scores after restore: 368/368 = 1.000
- Package: 1105151 bytes gz, verify 25323.5 rec/s, hydrate 4894.5 rec/s (local diagnostics)

## Latency (separate sampling, not quality)

- Query latency p50/p95: 32.063 / 50.976 ms (368 samples)
- Fresh-process cold start to first query: 0.864 s

## Missed relevance (per-query failures)

- `q-para-004` (semantic_paraphrase): missed ['mem-expensetool-001'] — "Where do I file my trip receipts and how long do I have?"
- `q-para-005` (semantic_paraphrase): missed ['mem-mentor-001'] — "Who helps newcomers learn the codebase at the start?"
- `q-scale-exact-020` (exact_lexical): missed ['scale-exact-020'] — "How many days does archive ARC-020 retain signed delivery receipts?"
- `q-scale-identifier-008` (identifier_or_entity_name): missed ['scale-identifier-008'] — "What pressure reading triggers the warning for metric MTR-008-Q?"
- `q-scale-identifier-017` (identifier_or_entity_name): missed ['scale-identifier-017'] — "What pressure reading triggers the warning for metric MTR-017-Q?"
- `q-scale-identifier-023` (identifier_or_entity_name): missed ['scale-identifier-023'] — "What pressure reading triggers the warning for metric MTR-023-Q?"
- `q-scale-identifier-024` (identifier_or_entity_name): missed ['scale-identifier-024'] — "What pressure reading triggers the warning for metric MTR-024-Q?"
- `q-scale-duplicate-000` (near_duplicate_diversity): missed ['scale-duplicate-000-c'] — "How often is the badge printer at UNIT-000 inspected?"
- `q-scale-duplicate-001` (near_duplicate_diversity): missed ['scale-duplicate-001-c'] — "How often is the audit queue at UNIT-001 inspected?"
- `q-scale-duplicate-002` (near_duplicate_diversity): missed ['scale-duplicate-002-c'] — "How often is the packing station at UNIT-002 inspected?"
- `q-scale-duplicate-003` (near_duplicate_diversity): missed ['scale-duplicate-003-c'] — "How often is the archive index at UNIT-003 inspected?"
- `q-scale-duplicate-004` (near_duplicate_diversity): missed ['scale-duplicate-004-c'] — "How often is the badge printer at UNIT-004 inspected?"
- `q-scale-duplicate-005` (near_duplicate_diversity): missed ['scale-duplicate-005-c'] — "How often is the audit queue at UNIT-005 inspected?"
- `q-scale-duplicate-006` (near_duplicate_diversity): missed ['scale-duplicate-006-c'] — "How often is the packing station at UNIT-006 inspected?"
- `q-scale-duplicate-007` (near_duplicate_diversity): missed ['scale-duplicate-007-c'] — "How often is the archive index at UNIT-007 inspected?"
- `q-scale-duplicate-008` (near_duplicate_diversity): missed ['scale-duplicate-008-c'] — "How often is the badge printer at UNIT-008 inspected?"
- `q-scale-duplicate-009` (near_duplicate_diversity): missed ['scale-duplicate-009-c'] — "How often is the audit queue at UNIT-009 inspected?"
- `q-scale-duplicate-010` (near_duplicate_diversity): missed ['scale-duplicate-010-c'] — "How often is the packing station at UNIT-010 inspected?"
- `q-scale-duplicate-011` (near_duplicate_diversity): missed ['scale-duplicate-011-c'] — "How often is the archive index at UNIT-011 inspected?"
- `q-scale-duplicate-012` (near_duplicate_diversity): missed ['scale-duplicate-012-c'] — "How often is the badge printer at UNIT-012 inspected?"
- `q-scale-duplicate-013` (near_duplicate_diversity): missed ['scale-duplicate-013-c'] — "How often is the audit queue at UNIT-013 inspected?"
- `q-scale-duplicate-014` (near_duplicate_diversity): missed ['scale-duplicate-014-c'] — "How often is the packing station at UNIT-014 inspected?"
- `q-scale-duplicate-015` (near_duplicate_diversity): missed ['scale-duplicate-015-c'] — "How often is the archive index at UNIT-015 inspected?"
- `q-scale-duplicate-016` (near_duplicate_diversity): missed ['scale-duplicate-016-c'] — "How often is the badge printer at UNIT-016 inspected?"
- `q-scale-duplicate-017` (near_duplicate_diversity): missed ['scale-duplicate-017-c'] — "How often is the audit queue at UNIT-017 inspected?"
- `q-scale-duplicate-018` (near_duplicate_diversity): missed ['scale-duplicate-018-c'] — "How often is the packing station at UNIT-018 inspected?"
- `q-scale-duplicate-019` (near_duplicate_diversity): missed ['scale-duplicate-019-c'] — "How often is the archive index at UNIT-019 inspected?"
- `q-scale-duplicate-020` (near_duplicate_diversity): missed ['scale-duplicate-020-c'] — "How often is the badge printer at UNIT-020 inspected?"
- `q-scale-duplicate-021` (near_duplicate_diversity): missed ['scale-duplicate-021-c'] — "How often is the audit queue at UNIT-021 inspected?"
- `q-scale-duplicate-023` (near_duplicate_diversity): missed ['scale-duplicate-023-c'] — "How often is the archive index at UNIT-023 inspected?"
- `q-scale-duplicate-024` (near_duplicate_diversity): missed ['scale-duplicate-024-c'] — "How often is the badge printer at UNIT-024 inspected?"
- `q-scale-duplicate-025` (near_duplicate_diversity): missed ['scale-duplicate-025-c'] — "How often is the audit queue at UNIT-025 inspected?"
- `q-scale-duplicate-026` (near_duplicate_diversity): missed ['scale-duplicate-026-c'] — "How often is the packing station at UNIT-026 inspected?"
- `q-scale-duplicate-027` (near_duplicate_diversity): missed ['scale-duplicate-027-c'] — "How often is the archive index at UNIT-027 inspected?"
- `q-scale-duplicate-028` (near_duplicate_diversity): missed ['scale-duplicate-028-c'] — "How often is the badge printer at UNIT-028 inspected?"
- `q-scale-duplicate-029` (near_duplicate_diversity): missed ['scale-duplicate-029-c'] — "How often is the audit queue at UNIT-029 inspected?"
- `q-scale-duplicate-030` (near_duplicate_diversity): missed ['scale-duplicate-030-c'] — "How often is the packing station at UNIT-030 inspected?"
- `q-scale-duplicate-031` (near_duplicate_diversity): missed ['scale-duplicate-031-c'] — "How often is the archive index at UNIT-031 inspected?"
- `q-scale-duplicate-032` (near_duplicate_diversity): missed ['scale-duplicate-032-c'] — "How often is the badge printer at UNIT-032 inspected?"
- `q-scale-duplicate-033` (near_duplicate_diversity): missed ['scale-duplicate-033-c'] — "How often is the audit queue at UNIT-033 inspected?"
- `q-scale-duplicate-034` (near_duplicate_diversity): missed ['scale-duplicate-034-c'] — "How often is the packing station at UNIT-034 inspected?"
- `q-scale-duplicate-035` (near_duplicate_diversity): missed ['scale-duplicate-035-c'] — "How often is the archive index at UNIT-035 inspected?"
- `q-scale-duplicate-036` (near_duplicate_diversity): missed ['scale-duplicate-036-c'] — "How often is the badge printer at UNIT-036 inspected?"
- `q-scale-duplicate-037` (near_duplicate_diversity): missed ['scale-duplicate-037-c'] — "How often is the audit queue at UNIT-037 inspected?"
- `q-scale-duplicate-038` (near_duplicate_diversity): missed ['scale-duplicate-038-c'] — "How often is the packing station at UNIT-038 inspected?"
- `q-scale-duplicate-039` (near_duplicate_diversity): missed ['scale-duplicate-039-c'] — "How often is the archive index at UNIT-039 inspected?"
- `q-scale-cross-021` (cross_project_isolation): missed ['scale-cross-021-finance'] — "In finance, at what EUR amount does Beacon 021 need invoice review?"
- `q-scale-cross-031` (cross_project_isolation): missed ['scale-cross-031-finance'] — "In finance, at what EUR amount does Beacon 031 need invoice review?"
- `q-scale-cross-039` (cross_project_isolation): missed ['scale-cross-039-finance'] — "In finance, at what EUR amount does Beacon 039 need invoice review?"

## Five worst queries

- `q-para-004` recall@5=0.0 mrr@5=0.0 — "Where do I file my trip receipts and how long do I have?"
- `q-para-005` recall@5=0.0 mrr@5=0.0 — "Who helps newcomers learn the codebase at the start?"
- `q-scale-exact-020` recall@5=0.0 mrr@5=0.0 — "How many days does archive ARC-020 retain signed delivery receipts?"
- `q-scale-identifier-008` recall@5=0.0 mrr@5=0.0 — "What pressure reading triggers the warning for metric MTR-008-Q?"
- `q-scale-identifier-017` recall@5=0.0 mrr@5=0.0 — "What pressure reading triggers the warning for metric MTR-017-Q?"

## Recommendation

- **pursue_reranking_hook_80** — near-duplicate diversity duplicate-slot rate 0.270 exceeds 20%: near-duplicates consume diverse top-k slots (aggregate over all queries: 0.035); pursue #80 (evidence-gated optional reranking hook).
  - measured: clone equivalence 1.0, semantic Recall@5 0.9565217391304348, exact-lexical Recall@5 0.9782608695652174, duplicate-slot rate 0.03532608695652172 (near-duplicate diversity category: 0.2695652173913042)

## What this benchmark does not prove

- These are synthetic fixtures on one machine: no cross-machine guarantees, no tenant authorization claims, and no enterprise-scale performance claims.
- The hash-vector embedder is deterministic and portable, but it is NOT a learned semantic embedding; weighted score fusion is not a second-stage reranker.
- Entity extraction, MMR, and rerankers remain optional future work (gated by #78 / #80 evidence), not core requirements.
