# Retrieval evaluation report

Corpus: 60 memories (60 ingested), 48 queries. Stack: cosine 0.6 / fts5_bm25 0.2 / importance 0.2 over hotmem-hash-v1 (64-dim).
Scope: per-query fixture namespace; minimum score: 0.4.
Scoped namespace results: 0 wrong-namespace slots out of 1 returned across 6 queries.

Abstention: 15/48 queries returned results; 6/6 no-answer queries abstained.


## Overall metrics

| Metric | Value |
|---|---|
| Recall@1 | 0.159 (n=42) |
| Recall@5 | 0.302 (n=42) |
| MRR@5 | 0.274 (n=42) |
| nDCG@5 | 0.293 (n=42) |
| False-positive rate | 0.233 (n=15) |
| Duplicate-slot rate | 0.211 (n=15) |

## Per category

| Category | Recall@5 | MRR@5 | nDCG@5 | FP rate | Queries |
|---|---|---|---|---|---|
| cross_project_isolation | 0.167 (n=6) | 0.167 (n=6) | 0.138 (n=6) | 0.000 (n=1) | 6 |
| exact_lexical | 0.500 (n=6) | 0.500 (n=6) | 0.500 (n=6) | 0.000 (n=3) | 6 |
| identifier_or_entity_name | 0.000 (n=6) | 0.000 (n=6) | 0.000 (n=6) | n/a | 6 |
| near_duplicate_diversity | 0.778 (n=6) | 0.833 (n=6) | 0.794 (n=6) | 0.000 (n=5) | 6 |
| negative_or_no_answer | n/a | n/a | n/a | n/a | 6 |
| semantic_paraphrase | 0.000 (n=6) | 0.000 (n=6) | 0.000 (n=6) | n/a | 6 |
| snapshot_hydration_equivalence | 0.167 (n=6) | 0.167 (n=6) | 0.167 (n=6) | 0.500 (n=2) | 6 |
| temporal_or_revision | 0.500 (n=6) | 0.250 (n=6) | 0.455 (n=6) | 0.625 (n=4) | 6 |

## Clone equivalence (verified package -> clean hydration)

- Identical ordered ids + scores after restore: 48/48 = 1.000
- Package: 11463 bytes gz, verify 20341.0 rec/s, hydrate 5588.2 rec/s (local diagnostics)

## Latency (separate sampling, not quality)

- Query latency p50/p95: 1.174 / 1.501 ms (48 samples)
- Fresh-process cold start to first query: 0.098 s

## Missed relevance (per-query failures)

- `q-exact-001` (exact_lexical): missed ['mem-invoice-001'] — "What approval does an Acme invoice above EUR 5,000 need?"
- `q-exact-004` (exact_lexical): missed ['mem-expense-001'] — "Do expense reports need receipts?"
- `q-exact-005` (exact_lexical): missed ['mem-deploy-001'] — "Which days can we deploy to production?"
- `q-para-001` (semantic_paraphrase): missed ['mem-badge-001'] — "When do building cards stop working after someone exits?"
- `q-para-002` (semantic_paraphrase): missed ['mem-standup-001'] — "What time is the daily short meeting?"
- `q-para-003` (semantic_paraphrase): missed ['mem-vpn-001'] — "How do staff working from home reach internal systems?"
- `q-para-004` (semantic_paraphrase): missed ['mem-expensetool-001'] — "Where do I file my trip receipts and how long do I have?"
- `q-para-005` (semantic_paraphrase): missed ['mem-mentor-001'] — "Who helps newcomers learn the codebase at the start?"
- `q-para-006` (semantic_paraphrase): missed ['mem-outage-001'] — "What happens if the website stops responding for customers?"
- `q-ent-001` (identifier_or_entity_name): missed ['mem-meridian-fin-001'] — "What does Meridian charge per transaction?"
- `q-ent-002` (identifier_or_entity_name): missed ['mem-meridian-log-001'] — "Which Meridian service handles overseas shipments?"
- `q-ent-003` (identifier_or_entity_name): missed ['mem-falcon-001'] — "What is Falcon?"
- `q-ent-004` (identifier_or_entity_name): missed ['mem-quill-001'] — "What is Quill?"
- `q-ent-005` (identifier_or_entity_name): missed ['mem-aurora-001'] — "What is Aurora?"
- `q-ent-006` (identifier_or_entity_name): missed ['mem-orchid-001'] — "What is the returns vendor called Orchid responsible for?"
- `q-temp-002` (temporal_or_revision): missed ['mem-portal-new-001'] — "Where is the vendor portal login now?"
- `q-temp-005` (temporal_or_revision): missed ['mem-portal-new-001', 'mem-portal-old-001'] — "Did the vendor portal address ever change?"
- `q-temp-006` (temporal_or_revision): missed ['mem-leadtime-old-001'] — "How long was standard delivery lead time in early 2026?"
- `q-dup-001` (near_duplicate_diversity): missed ['mem-handbook-003'] — "How often is the employee handbook reviewed?"
- `q-dup-002` (near_duplicate_diversity): missed ['mem-handbook-001', 'mem-handbook-002', 'mem-handbook-003'] — "Who reviews the staff handbook and how often?"
- `q-iso-001` (cross_project_isolation): missed ['mem-northwind-fin-001'] — "In the finance project, what are Northwind's payment terms?"
- `q-iso-002` (cross_project_isolation): missed ['mem-northwind-log-001'] — "In the logistics project, what lead time does Northwind freight need?"
- `q-iso-003` (cross_project_isolation): missed ['mem-meridian-fin-001'] — "For payments, what is Meridian's fee?"
- `q-iso-005` (cross_project_isolation): missed ['mem-invoice-001', 'mem-northwind-fin-001'] — "Northwind invoice approval limit in the finance project?"
- `q-iso-006` (cross_project_isolation): missed ['mem-orchid-001'] — "In the logistics project, which vendor processes damaged parcel returns?"
- `q-snap-002` (snapshot_hydration_equivalence): missed ['mem-backup-001'] — "What is the nightly backup retention?"
- `q-snap-003` (snapshot_hydration_equivalence): missed ['mem-mentor-001'] — "How do new engineers get a mentor?"
- `q-snap-004` (snapshot_hydration_equivalence): missed ['mem-outage-001'] — "What happens during a service outage?"
- `q-snap-005` (snapshot_hydration_equivalence): missed ['mem-expensetool-001'] — "Where do employees submit travel costs?"
- `q-snap-006` (snapshot_hydration_equivalence): missed ['mem-standup-001'] — "When is the morning team sync?"

## Five worst queries

- `q-exact-001` recall@5=0.0 mrr@5=0.0 — "What approval does an Acme invoice above EUR 5,000 need?"
- `q-exact-004` recall@5=0.0 mrr@5=0.0 — "Do expense reports need receipts?"
- `q-exact-005` recall@5=0.0 mrr@5=0.0 — "Which days can we deploy to production?"
- `q-para-001` recall@5=0.0 mrr@5=0.0 — "When do building cards stop working after someone exits?"
- `q-para-002` recall@5=0.0 mrr@5=0.0 — "What time is the daily short meeting?"

## Recommendation

- **pursue_semantic_embedding_boundary_78** — semantic Recall@5 (0.000) trails exact-lexical Recall@5 (0.500) by 50.0 percentage points (threshold: 15pp) — the deterministic hash embedder cannot bridge wording differences; pursue #78.
  - measured: clone equivalence 1.0, semantic Recall@5 0.0, exact-lexical Recall@5 0.5, duplicate-slot rate 0.21111111111111108 (near-duplicate diversity category: 0.6333333333333332)

## What this benchmark does not prove

- These are synthetic fixtures on one machine: no cross-machine guarantees, no tenant authorization claims, and no enterprise-scale performance claims.
- The hash-vector embedder is deterministic and portable, but it is NOT a learned semantic embedding; weighted score fusion is not a second-stage reranker.
- Entity extraction, MMR, and rerankers remain optional future work (gated by #78 / #80 evidence), not core requirements.
