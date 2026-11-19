# Retrieval evaluation report

Corpus: 60 memories (60 ingested), 48 queries. Stack: cosine 0.6 / fts5_bm25 0.2 / importance 0.2 over local-m2v/potion-base-8M/rev:v1/norm:l2/pp:m2v-static-v1 (256-dim).
Scope: per-query fixture namespace; minimum score: 0.4.
Scoped namespace results: 0 wrong-namespace slots out of 9 returned across 6 queries.

Abstention: 34/48 queries returned results; 6/6 no-answer queries abstained.


## Overall metrics

| Metric | Value |
|---|---|
| Recall@1 | 0.571 (n=42) |
| Recall@5 | 0.786 (n=42) |
| MRR@5 | 0.738 (n=42) |
| nDCG@5 | 0.747 (n=42) |
| False-positive rate | 0.154 (n=34) |
| Duplicate-slot rate | 0.113 (n=34) |

## Per category

| Category | Recall@5 | MRR@5 | nDCG@5 | FP rate | Queries |
|---|---|---|---|---|---|
| cross_project_isolation | 0.833 (n=6) | 0.833 (n=6) | 0.746 (n=6) | 0.333 (n=6) | 6 |
| exact_lexical | 1.000 (n=6) | 1.000 (n=6) | 1.000 (n=6) | 0.083 (n=6) | 6 |
| identifier_or_entity_name | 0.667 (n=6) | 0.667 (n=6) | 0.609 (n=6) | 0.000 (n=4) | 6 |
| near_duplicate_diversity | 1.000 (n=6) | 1.000 (n=6) | 1.000 (n=6) | 0.042 (n=6) | 6 |
| negative_or_no_answer | n/a | n/a | n/a | n/a | 6 |
| semantic_paraphrase | 0.333 (n=6) | 0.333 (n=6) | 0.333 (n=6) | 0.000 (n=2) | 6 |
| snapshot_hydration_equivalence | 0.667 (n=6) | 0.667 (n=6) | 0.667 (n=6) | 0.000 (n=4) | 6 |
| temporal_or_revision | 1.000 (n=6) | 0.667 (n=6) | 0.875 (n=6) | 0.417 (n=6) | 6 |

## Clone equivalence (verified package -> clean hydration)

- Identical ordered ids + scores after restore: 48/48 = 1.000
- Package: 68998 bytes gz, verify 16276.0 rec/s, hydrate 4477.3 rec/s (local diagnostics)

## Latency (separate sampling, not quality)

- Query latency p50/p95: 2.261 / 2.67 ms (48 samples)
- Fresh-process cold start to first query: 0.405 s

## Missed relevance (per-query failures)

- `q-para-001` (semantic_paraphrase): missed ['mem-badge-001'] — "When do building cards stop working after someone exits?"
- `q-para-004` (semantic_paraphrase): missed ['mem-expensetool-001'] — "Where do I file my trip receipts and how long do I have?"
- `q-para-005` (semantic_paraphrase): missed ['mem-mentor-001'] — "Who helps newcomers learn the codebase at the start?"
- `q-para-006` (semantic_paraphrase): missed ['mem-outage-001'] — "What happens if the website stops responding for customers?"
- `q-ent-004` (identifier_or_entity_name): missed ['mem-quill-001'] — "What is Quill?"
- `q-ent-005` (identifier_or_entity_name): missed ['mem-aurora-001'] — "What is Aurora?"
- `q-iso-001` (cross_project_isolation): missed ['mem-northwind-fin-001'] — "In the finance project, what are Northwind's payment terms?"
- `q-snap-003` (snapshot_hydration_equivalence): missed ['mem-mentor-001'] — "How do new engineers get a mentor?"
- `q-snap-004` (snapshot_hydration_equivalence): missed ['mem-outage-001'] — "What happens during a service outage?"

## Five worst queries

- `q-para-001` recall@5=0.0 mrr@5=0.0 — "When do building cards stop working after someone exits?"
- `q-para-004` recall@5=0.0 mrr@5=0.0 — "Where do I file my trip receipts and how long do I have?"
- `q-para-005` recall@5=0.0 mrr@5=0.0 — "Who helps newcomers learn the codebase at the start?"
- `q-para-006` recall@5=0.0 mrr@5=0.0 — "What happens if the website stops responding for customers?"
- `q-ent-004` recall@5=0.0 mrr@5=0.0 — "What is Quill?"

## Recommendation

- **pursue_semantic_embedding_boundary_78** — semantic Recall@5 (0.333) trails exact-lexical Recall@5 (1.000) by 66.7 percentage points (threshold: 15pp) — the deterministic hash embedder cannot bridge wording differences; pursue #78.
  - measured: clone equivalence 1.0, semantic Recall@5 0.3333333333333333, exact-lexical Recall@5 1.0, duplicate-slot rate 0.11274509803921567 (near-duplicate diversity category: 0.6388888888888888)

## What this benchmark does not prove

- These are synthetic fixtures on one machine: no cross-machine guarantees, no tenant authorization claims, and no enterprise-scale performance claims.
- The hash-vector embedder is deterministic and portable, but it is NOT a learned semantic embedding; weighted score fusion is not a second-stage reranker.
- Entity extraction, MMR, and rerankers remain optional future work (gated by #78 / #80 evidence), not core requirements.
