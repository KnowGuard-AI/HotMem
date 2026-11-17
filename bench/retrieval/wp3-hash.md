# Retrieval evaluation report

Corpus: 1000 memories (1000 ingested), 368 queries. Stack: cosine 0.6 / fts5_bm25 0.2 / importance 0.2 over hotmem-hash-v1 (64-dim).

## Overall metrics

| Metric | Value |
|---|---|
| Recall@1 | 0.282 (n=322) |
| Recall@5 | 0.592 (n=322) |
| MRR@5 | 0.483 (n=322) |
| nDCG@5 | 0.449 (n=322) |
| False-positive rate | 0.876 (n=368) |
| Duplicate-slot rate | 0.009 (n=368) |

## Per category

| Category | Recall@5 | MRR@5 | nDCG@5 | FP rate | Queries |
|---|---|---|---|---|---|
| cross_project_isolation | 0.435 (n=46) | 0.313 (n=46) | 0.290 (n=46) | 0.913 (n=46) | 46 |
| exact_lexical | 0.804 (n=46) | 0.544 (n=46) | 0.610 (n=46) | 0.839 (n=46) | 46 |
| identifier_or_entity_name | 0.565 (n=46) | 0.420 (n=46) | 0.451 (n=46) | 0.887 (n=46) | 46 |
| near_duplicate_diversity | 0.406 (n=46) | 0.940 (n=46) | 0.506 (n=46) | 0.757 (n=46) | 46 |
| negative_or_no_answer | n/a | n/a | n/a | 1.000 (n=46) | 46 |
| semantic_paraphrase | 0.587 (n=46) | 0.266 (n=46) | 0.345 (n=46) | 0.883 (n=46) | 46 |
| snapshot_hydration_equivalence | 0.522 (n=46) | 0.378 (n=46) | 0.414 (n=46) | 0.896 (n=46) | 46 |
| temporal_or_revision | 0.826 (n=46) | 0.517 (n=46) | 0.525 (n=46) | 0.830 (n=46) | 46 |

## Clone equivalence (verified package -> clean hydration)

- Identical ordered ids + scores after restore: 368/368 = 1.000
- Package: 132226 bytes gz, verify 31139.1 rec/s, hydrate 5498.9 rec/s (local diagnostics)

## Latency (separate sampling, not quality)

- Query latency p50/p95: 11.881 / 16.585 ms (368 samples)
- Fresh-process cold start to first query: 0.463 s

## Missed relevance (per-query failures)

- `q-exact-005` (exact_lexical): missed ['mem-deploy-001'] — "Which days can we deploy to production?"
- `q-para-001` (semantic_paraphrase): missed ['mem-badge-001'] — "When do building cards stop working after someone exits?"
- `q-para-002` (semantic_paraphrase): missed ['mem-standup-001'] — "What time is the daily short meeting?"
- `q-para-003` (semantic_paraphrase): missed ['mem-vpn-001'] — "How do staff working from home reach internal systems?"
- `q-para-004` (semantic_paraphrase): missed ['mem-expensetool-001'] — "Where do I file my trip receipts and how long do I have?"
- `q-para-005` (semantic_paraphrase): missed ['mem-mentor-001'] — "Who helps newcomers learn the codebase at the start?"
- `q-para-006` (semantic_paraphrase): missed ['mem-outage-001'] — "What happens if the website stops responding for customers?"
- `q-ent-003` (identifier_or_entity_name): missed ['mem-falcon-001'] — "What is Falcon?"
- `q-ent-004` (identifier_or_entity_name): missed ['mem-quill-001'] — "What is Quill?"
- `q-ent-005` (identifier_or_entity_name): missed ['mem-aurora-001'] — "What is Aurora?"
- `q-ent-006` (identifier_or_entity_name): missed ['mem-orchid-001'] — "What is the returns vendor called Orchid responsible for?"
- `q-dup-001` (near_duplicate_diversity): missed ['mem-handbook-003'] — "How often is the employee handbook reviewed?"
- `q-dup-002` (near_duplicate_diversity): missed ['mem-handbook-001', 'mem-handbook-002'] — "Who reviews the staff handbook and how often?"
- `q-iso-001` (cross_project_isolation): missed ['mem-northwind-fin-001'] — "In the finance project, what are Northwind's payment terms?"
- `q-iso-002` (cross_project_isolation): missed ['mem-northwind-log-001'] — "In the logistics project, what lead time does Northwind freight need?"
- `q-iso-003` (cross_project_isolation): missed ['mem-meridian-fin-001'] — "For payments, what is Meridian's fee?"
- `q-iso-005` (cross_project_isolation): missed ['mem-invoice-001', 'mem-northwind-fin-001'] — "Northwind invoice approval limit in the finance project?"
- `q-snap-003` (snapshot_hydration_equivalence): missed ['mem-mentor-001'] — "How do new engineers get a mentor?"
- `q-snap-004` (snapshot_hydration_equivalence): missed ['mem-outage-001'] — "What happens during a service outage?"
- `q-scale-exact-002` (exact_lexical): missed ['scale-exact-002'] — "How many days does archive ARC-002 retain signed delivery receipts?"
- `q-scale-exact-004` (exact_lexical): missed ['scale-exact-004'] — "How many days does archive ARC-004 retain signed delivery receipts?"
- `q-scale-exact-010` (exact_lexical): missed ['scale-exact-010'] — "How many days does archive ARC-010 retain signed delivery receipts?"
- `q-scale-exact-020` (exact_lexical): missed ['scale-exact-020'] — "How many days does archive ARC-020 retain signed delivery receipts?"
- `q-scale-exact-026` (exact_lexical): missed ['scale-exact-026'] — "How many days does archive ARC-026 retain signed delivery receipts?"
- `q-scale-exact-028` (exact_lexical): missed ['scale-exact-028'] — "How many days does archive ARC-028 retain signed delivery receipts?"
- `q-scale-exact-029` (exact_lexical): missed ['scale-exact-029'] — "How many days does archive ARC-029 retain signed delivery receipts?"
- `q-scale-exact-038` (exact_lexical): missed ['scale-exact-038'] — "How many days does archive ARC-038 retain signed delivery receipts?"
- `q-scale-semantic-001` (semantic_paraphrase): missed ['scale-semantic-001'] — "How long must SITE-001 keep those returned access badges before they can be discarded?"
- `q-scale-semantic-002` (semantic_paraphrase): missed ['scale-semantic-002'] — "How long must SITE-002 keep those damaged parcel labels before they can be discarded?"
- `q-scale-semantic-003` (semantic_paraphrase): missed ['scale-semantic-003'] — "How long must SITE-003 keep those retired signing keys before they can be discarded?"
- `q-scale-semantic-004` (semantic_paraphrase): missed ['scale-semantic-004'] — "How long must SITE-004 keep those sealed claim packets before they can be discarded?"
- `q-scale-semantic-005` (semantic_paraphrase): missed ['scale-semantic-005'] — "How long must SITE-005 keep those returned access badges before they can be discarded?"
- `q-scale-semantic-006` (semantic_paraphrase): missed ['scale-semantic-006'] — "How long must SITE-006 keep those damaged parcel labels before they can be discarded?"
- `q-scale-semantic-009` (semantic_paraphrase): missed ['scale-semantic-009'] — "How long must SITE-009 keep those returned access badges before they can be discarded?"
- `q-scale-semantic-011` (semantic_paraphrase): missed ['scale-semantic-011'] — "How long must SITE-011 keep those retired signing keys before they can be discarded?"
- `q-scale-semantic-013` (semantic_paraphrase): missed ['scale-semantic-013'] — "How long must SITE-013 keep those returned access badges before they can be discarded?"
- `q-scale-semantic-018` (semantic_paraphrase): missed ['scale-semantic-018'] — "How long must SITE-018 keep those damaged parcel labels before they can be discarded?"
- `q-scale-semantic-025` (semantic_paraphrase): missed ['scale-semantic-025'] — "How long must SITE-025 keep those returned access badges before they can be discarded?"
- `q-scale-semantic-027` (semantic_paraphrase): missed ['scale-semantic-027'] — "How long must SITE-027 keep those retired signing keys before they can be discarded?"
- `q-scale-semantic-037` (semantic_paraphrase): missed ['scale-semantic-037'] — "How long must SITE-037 keep those returned access badges before they can be discarded?"
- `q-scale-identifier-000` (identifier_or_entity_name): missed ['scale-identifier-000'] — "What pressure reading triggers the warning for metric MTR-000-Q?"
- `q-scale-identifier-001` (identifier_or_entity_name): missed ['scale-identifier-001'] — "What pressure reading triggers the warning for metric MTR-001-Q?"
- `q-scale-identifier-002` (identifier_or_entity_name): missed ['scale-identifier-002'] — "What pressure reading triggers the warning for metric MTR-002-Q?"
- `q-scale-identifier-004` (identifier_or_entity_name): missed ['scale-identifier-004'] — "What pressure reading triggers the warning for metric MTR-004-Q?"
- `q-scale-identifier-005` (identifier_or_entity_name): missed ['scale-identifier-005'] — "What pressure reading triggers the warning for metric MTR-005-Q?"
- `q-scale-identifier-006` (identifier_or_entity_name): missed ['scale-identifier-006'] — "What pressure reading triggers the warning for metric MTR-006-Q?"
- `q-scale-identifier-008` (identifier_or_entity_name): missed ['scale-identifier-008'] — "What pressure reading triggers the warning for metric MTR-008-Q?"
- `q-scale-identifier-010` (identifier_or_entity_name): missed ['scale-identifier-010'] — "What pressure reading triggers the warning for metric MTR-010-Q?"
- `q-scale-identifier-015` (identifier_or_entity_name): missed ['scale-identifier-015'] — "What pressure reading triggers the warning for metric MTR-015-Q?"
- `q-scale-identifier-019` (identifier_or_entity_name): missed ['scale-identifier-019'] — "What pressure reading triggers the warning for metric MTR-019-Q?"
- `q-scale-identifier-022` (identifier_or_entity_name): missed ['scale-identifier-022'] — "What pressure reading triggers the warning for metric MTR-022-Q?"
- `q-scale-identifier-025` (identifier_or_entity_name): missed ['scale-identifier-025'] — "What pressure reading triggers the warning for metric MTR-025-Q?"
- `q-scale-identifier-029` (identifier_or_entity_name): missed ['scale-identifier-029'] — "What pressure reading triggers the warning for metric MTR-029-Q?"
- `q-scale-identifier-031` (identifier_or_entity_name): missed ['scale-identifier-031'] — "What pressure reading triggers the warning for metric MTR-031-Q?"
- `q-scale-identifier-036` (identifier_or_entity_name): missed ['scale-identifier-036'] — "What pressure reading triggers the warning for metric MTR-036-Q?"
- `q-scale-identifier-037` (identifier_or_entity_name): missed ['scale-identifier-037'] — "What pressure reading triggers the warning for metric MTR-037-Q?"
- `q-scale-temporal-000` (temporal_or_revision): missed ['scale-revision-000-current'] — "What is the current timeout for the ROUTE-000 circuit breaker?"
- `q-scale-temporal-001` (temporal_or_revision): missed ['scale-revision-001-current'] — "What is the current timeout for the ROUTE-001 circuit breaker?"
- `q-scale-temporal-002` (temporal_or_revision): missed ['scale-revision-002-current'] — "What is the current timeout for the ROUTE-002 circuit breaker?"
- `q-scale-temporal-007` (temporal_or_revision): missed ['scale-revision-007-current'] — "What is the current timeout for the ROUTE-007 circuit breaker?"
- `q-scale-temporal-013` (temporal_or_revision): missed ['scale-revision-013-current'] — "What is the current timeout for the ROUTE-013 circuit breaker?"
- `q-scale-temporal-015` (temporal_or_revision): missed ['scale-revision-015-current'] — "What is the current timeout for the ROUTE-015 circuit breaker?"
- `q-scale-temporal-027` (temporal_or_revision): missed ['scale-revision-027-current'] — "What is the current timeout for the ROUTE-027 circuit breaker?"
- `q-scale-temporal-034` (temporal_or_revision): missed ['scale-revision-034-current'] — "What is the current timeout for the ROUTE-034 circuit breaker?"
- `q-scale-duplicate-000` (near_duplicate_diversity): missed ['scale-duplicate-000-a', 'scale-duplicate-000-c'] — "How often is the badge printer at UNIT-000 inspected?"
- `q-scale-duplicate-001` (near_duplicate_diversity): missed ['scale-duplicate-001-a', 'scale-duplicate-001-c'] — "How often is the audit queue at UNIT-001 inspected?"
- `q-scale-duplicate-002` (near_duplicate_diversity): missed ['scale-duplicate-002-a', 'scale-duplicate-002-c'] — "How often is the packing station at UNIT-002 inspected?"
- `q-scale-duplicate-003` (near_duplicate_diversity): missed ['scale-duplicate-003-a', 'scale-duplicate-003-c'] — "How often is the archive index at UNIT-003 inspected?"
- `q-scale-duplicate-004` (near_duplicate_diversity): missed ['scale-duplicate-004-a', 'scale-duplicate-004-c'] — "How often is the badge printer at UNIT-004 inspected?"
- `q-scale-duplicate-005` (near_duplicate_diversity): missed ['scale-duplicate-005-a', 'scale-duplicate-005-c'] — "How often is the audit queue at UNIT-005 inspected?"
- `q-scale-duplicate-006` (near_duplicate_diversity): missed ['scale-duplicate-006-a', 'scale-duplicate-006-c'] — "How often is the packing station at UNIT-006 inspected?"
- `q-scale-duplicate-007` (near_duplicate_diversity): missed ['scale-duplicate-007-a', 'scale-duplicate-007-c'] — "How often is the archive index at UNIT-007 inspected?"
- `q-scale-duplicate-008` (near_duplicate_diversity): missed ['scale-duplicate-008-a', 'scale-duplicate-008-c'] — "How often is the badge printer at UNIT-008 inspected?"
- `q-scale-duplicate-009` (near_duplicate_diversity): missed ['scale-duplicate-009-a', 'scale-duplicate-009-c'] — "How often is the audit queue at UNIT-009 inspected?"
- `q-scale-duplicate-010` (near_duplicate_diversity): missed ['scale-duplicate-010-a', 'scale-duplicate-010-c'] — "How often is the packing station at UNIT-010 inspected?"
- `q-scale-duplicate-011` (near_duplicate_diversity): missed ['scale-duplicate-011-a', 'scale-duplicate-011-c'] — "How often is the archive index at UNIT-011 inspected?"
- `q-scale-duplicate-012` (near_duplicate_diversity): missed ['scale-duplicate-012-a', 'scale-duplicate-012-c'] — "How often is the badge printer at UNIT-012 inspected?"
- `q-scale-duplicate-013` (near_duplicate_diversity): missed ['scale-duplicate-013-a', 'scale-duplicate-013-c'] — "How often is the audit queue at UNIT-013 inspected?"
- `q-scale-duplicate-014` (near_duplicate_diversity): missed ['scale-duplicate-014-a', 'scale-duplicate-014-c'] — "How often is the packing station at UNIT-014 inspected?"
- `q-scale-duplicate-015` (near_duplicate_diversity): missed ['scale-duplicate-015-a', 'scale-duplicate-015-c'] — "How often is the archive index at UNIT-015 inspected?"
- `q-scale-duplicate-016` (near_duplicate_diversity): missed ['scale-duplicate-016-a', 'scale-duplicate-016-c'] — "How often is the badge printer at UNIT-016 inspected?"
- `q-scale-duplicate-017` (near_duplicate_diversity): missed ['scale-duplicate-017-a', 'scale-duplicate-017-c'] — "How often is the audit queue at UNIT-017 inspected?"
- `q-scale-duplicate-018` (near_duplicate_diversity): missed ['scale-duplicate-018-c'] — "How often is the packing station at UNIT-018 inspected?"
- `q-scale-duplicate-019` (near_duplicate_diversity): missed ['scale-duplicate-019-a', 'scale-duplicate-019-c'] — "How often is the archive index at UNIT-019 inspected?"
- `q-scale-duplicate-020` (near_duplicate_diversity): missed ['scale-duplicate-020-a', 'scale-duplicate-020-c'] — "How often is the badge printer at UNIT-020 inspected?"
- `q-scale-duplicate-021` (near_duplicate_diversity): missed ['scale-duplicate-021-a', 'scale-duplicate-021-c'] — "How often is the audit queue at UNIT-021 inspected?"
- `q-scale-duplicate-022` (near_duplicate_diversity): missed ['scale-duplicate-022-a', 'scale-duplicate-022-c'] — "How often is the packing station at UNIT-022 inspected?"
- `q-scale-duplicate-023` (near_duplicate_diversity): missed ['scale-duplicate-023-a', 'scale-duplicate-023-c'] — "How often is the archive index at UNIT-023 inspected?"
- `q-scale-duplicate-024` (near_duplicate_diversity): missed ['scale-duplicate-024-a', 'scale-duplicate-024-c'] — "How often is the badge printer at UNIT-024 inspected?"
- `q-scale-duplicate-025` (near_duplicate_diversity): missed ['scale-duplicate-025-a', 'scale-duplicate-025-c'] — "How often is the audit queue at UNIT-025 inspected?"
- `q-scale-duplicate-026` (near_duplicate_diversity): missed ['scale-duplicate-026-a', 'scale-duplicate-026-c'] — "How often is the packing station at UNIT-026 inspected?"
- `q-scale-duplicate-027` (near_duplicate_diversity): missed ['scale-duplicate-027-a', 'scale-duplicate-027-c'] — "How often is the archive index at UNIT-027 inspected?"
- `q-scale-duplicate-028` (near_duplicate_diversity): missed ['scale-duplicate-028-a', 'scale-duplicate-028-c'] — "How often is the badge printer at UNIT-028 inspected?"
- `q-scale-duplicate-029` (near_duplicate_diversity): missed ['scale-duplicate-029-a', 'scale-duplicate-029-c'] — "How often is the audit queue at UNIT-029 inspected?"
- `q-scale-duplicate-030` (near_duplicate_diversity): missed ['scale-duplicate-030-a', 'scale-duplicate-030-c'] — "How often is the packing station at UNIT-030 inspected?"
- `q-scale-duplicate-031` (near_duplicate_diversity): missed ['scale-duplicate-031-a', 'scale-duplicate-031-c'] — "How often is the archive index at UNIT-031 inspected?"
- `q-scale-duplicate-032` (near_duplicate_diversity): missed ['scale-duplicate-032-a', 'scale-duplicate-032-c'] — "How often is the badge printer at UNIT-032 inspected?"
- `q-scale-duplicate-033` (near_duplicate_diversity): missed ['scale-duplicate-033-a', 'scale-duplicate-033-c'] — "How often is the audit queue at UNIT-033 inspected?"
- `q-scale-duplicate-034` (near_duplicate_diversity): missed ['scale-duplicate-034-a', 'scale-duplicate-034-c'] — "How often is the packing station at UNIT-034 inspected?"
- `q-scale-duplicate-035` (near_duplicate_diversity): missed ['scale-duplicate-035-a', 'scale-duplicate-035-c'] — "How often is the archive index at UNIT-035 inspected?"
- `q-scale-duplicate-036` (near_duplicate_diversity): missed ['scale-duplicate-036-a', 'scale-duplicate-036-c'] — "How often is the badge printer at UNIT-036 inspected?"
- `q-scale-duplicate-037` (near_duplicate_diversity): missed ['scale-duplicate-037-a', 'scale-duplicate-037-c'] — "How often is the audit queue at UNIT-037 inspected?"
- `q-scale-duplicate-038` (near_duplicate_diversity): missed ['scale-duplicate-038-a', 'scale-duplicate-038-c'] — "How often is the packing station at UNIT-038 inspected?"
- `q-scale-duplicate-039` (near_duplicate_diversity): missed ['scale-duplicate-039-a', 'scale-duplicate-039-c'] — "How often is the archive index at UNIT-039 inspected?"
- `q-scale-cross-001` (cross_project_isolation): missed ['scale-cross-001-finance'] — "In finance, at what EUR amount does Beacon 001 need invoice review?"
- `q-scale-cross-003` (cross_project_isolation): missed ['scale-cross-003-finance'] — "In finance, at what EUR amount does Beacon 003 need invoice review?"
- `q-scale-cross-004` (cross_project_isolation): missed ['scale-cross-004-logistics'] — "In logistics, how many days does Beacon 004 freight take to reach the hub?"
- `q-scale-cross-005` (cross_project_isolation): missed ['scale-cross-005-finance'] — "In finance, at what EUR amount does Beacon 005 need invoice review?"
- `q-scale-cross-008` (cross_project_isolation): missed ['scale-cross-008-logistics'] — "In logistics, how many days does Beacon 008 freight take to reach the hub?"
- `q-scale-cross-011` (cross_project_isolation): missed ['scale-cross-011-finance'] — "In finance, at what EUR amount does Beacon 011 need invoice review?"
- `q-scale-cross-012` (cross_project_isolation): missed ['scale-cross-012-logistics'] — "In logistics, how many days does Beacon 012 freight take to reach the hub?"
- `q-scale-cross-013` (cross_project_isolation): missed ['scale-cross-013-finance'] — "In finance, at what EUR amount does Beacon 013 need invoice review?"
- `q-scale-cross-018` (cross_project_isolation): missed ['scale-cross-018-logistics'] — "In logistics, how many days does Beacon 018 freight take to reach the hub?"
- `q-scale-cross-019` (cross_project_isolation): missed ['scale-cross-019-finance'] — "In finance, at what EUR amount does Beacon 019 need invoice review?"
- `q-scale-cross-021` (cross_project_isolation): missed ['scale-cross-021-finance'] — "In finance, at what EUR amount does Beacon 021 need invoice review?"
- `q-scale-cross-024` (cross_project_isolation): missed ['scale-cross-024-logistics'] — "In logistics, how many days does Beacon 024 freight take to reach the hub?"
- `q-scale-cross-025` (cross_project_isolation): missed ['scale-cross-025-finance'] — "In finance, at what EUR amount does Beacon 025 need invoice review?"
- `q-scale-cross-026` (cross_project_isolation): missed ['scale-cross-026-logistics'] — "In logistics, how many days does Beacon 026 freight take to reach the hub?"
- `q-scale-cross-027` (cross_project_isolation): missed ['scale-cross-027-finance'] — "In finance, at what EUR amount does Beacon 027 need invoice review?"
- `q-scale-cross-029` (cross_project_isolation): missed ['scale-cross-029-finance'] — "In finance, at what EUR amount does Beacon 029 need invoice review?"
- `q-scale-cross-031` (cross_project_isolation): missed ['scale-cross-031-finance'] — "In finance, at what EUR amount does Beacon 031 need invoice review?"
- `q-scale-cross-033` (cross_project_isolation): missed ['scale-cross-033-finance'] — "In finance, at what EUR amount does Beacon 033 need invoice review?"
- `q-scale-cross-034` (cross_project_isolation): missed ['scale-cross-034-logistics'] — "In logistics, how many days does Beacon 034 freight take to reach the hub?"
- `q-scale-cross-035` (cross_project_isolation): missed ['scale-cross-035-finance'] — "In finance, at what EUR amount does Beacon 035 need invoice review?"
- `q-scale-cross-037` (cross_project_isolation): missed ['scale-cross-037-finance'] — "In finance, at what EUR amount does Beacon 037 need invoice review?"
- `q-scale-cross-039` (cross_project_isolation): missed ['scale-cross-039-finance'] — "In finance, at what EUR amount does Beacon 039 need invoice review?"
- `q-scale-snapshot-001` (snapshot_hydration_equivalence): missed ['scale-snapshot-001'] — "When does the weekly maintenance window for NODE-001 begin?"
- `q-scale-snapshot-002` (snapshot_hydration_equivalence): missed ['scale-snapshot-002'] — "When does the weekly maintenance window for NODE-002 begin?"
- `q-scale-snapshot-003` (snapshot_hydration_equivalence): missed ['scale-snapshot-003'] — "When does the weekly maintenance window for NODE-003 begin?"
- `q-scale-snapshot-005` (snapshot_hydration_equivalence): missed ['scale-snapshot-005'] — "When does the weekly maintenance window for NODE-005 begin?"
- `q-scale-snapshot-006` (snapshot_hydration_equivalence): missed ['scale-snapshot-006'] — "When does the weekly maintenance window for NODE-006 begin?"
- `q-scale-snapshot-009` (snapshot_hydration_equivalence): missed ['scale-snapshot-009'] — "When does the weekly maintenance window for NODE-009 begin?"
- `q-scale-snapshot-010` (snapshot_hydration_equivalence): missed ['scale-snapshot-010'] — "When does the weekly maintenance window for NODE-010 begin?"
- `q-scale-snapshot-011` (snapshot_hydration_equivalence): missed ['scale-snapshot-011'] — "When does the weekly maintenance window for NODE-011 begin?"
- `q-scale-snapshot-015` (snapshot_hydration_equivalence): missed ['scale-snapshot-015'] — "When does the weekly maintenance window for NODE-015 begin?"
- `q-scale-snapshot-017` (snapshot_hydration_equivalence): missed ['scale-snapshot-017'] — "When does the weekly maintenance window for NODE-017 begin?"
- `q-scale-snapshot-018` (snapshot_hydration_equivalence): missed ['scale-snapshot-018'] — "When does the weekly maintenance window for NODE-018 begin?"
- `q-scale-snapshot-023` (snapshot_hydration_equivalence): missed ['scale-snapshot-023'] — "When does the weekly maintenance window for NODE-023 begin?"
- `q-scale-snapshot-025` (snapshot_hydration_equivalence): missed ['scale-snapshot-025'] — "When does the weekly maintenance window for NODE-025 begin?"
- `q-scale-snapshot-026` (snapshot_hydration_equivalence): missed ['scale-snapshot-026'] — "When does the weekly maintenance window for NODE-026 begin?"
- `q-scale-snapshot-030` (snapshot_hydration_equivalence): missed ['scale-snapshot-030'] — "When does the weekly maintenance window for NODE-030 begin?"
- `q-scale-snapshot-031` (snapshot_hydration_equivalence): missed ['scale-snapshot-031'] — "When does the weekly maintenance window for NODE-031 begin?"
- `q-scale-snapshot-033` (snapshot_hydration_equivalence): missed ['scale-snapshot-033'] — "When does the weekly maintenance window for NODE-033 begin?"
- `q-scale-snapshot-034` (snapshot_hydration_equivalence): missed ['scale-snapshot-034'] — "When does the weekly maintenance window for NODE-034 begin?"
- `q-scale-snapshot-037` (snapshot_hydration_equivalence): missed ['scale-snapshot-037'] — "When does the weekly maintenance window for NODE-037 begin?"
- `q-scale-snapshot-038` (snapshot_hydration_equivalence): missed ['scale-snapshot-038'] — "When does the weekly maintenance window for NODE-038 begin?"

## Five worst queries

- `q-exact-005` recall@5=0.0 mrr@5=0.0 — "Which days can we deploy to production?"
- `q-para-001` recall@5=0.0 mrr@5=0.0 — "When do building cards stop working after someone exits?"
- `q-para-002` recall@5=0.0 mrr@5=0.0 — "What time is the daily short meeting?"
- `q-para-003` recall@5=0.0 mrr@5=0.0 — "How do staff working from home reach internal systems?"
- `q-para-004` recall@5=0.0 mrr@5=0.0 — "Where do I file my trip receipts and how long do I have?"

## Recommendation

- **pursue_semantic_embedding_boundary_78** — semantic Recall@5 (0.587) trails exact-lexical Recall@5 (0.804) by 21.7 percentage points (threshold: 15pp) — the deterministic hash embedder cannot bridge wording differences; pursue #78.
  - measured: clone equivalence 1.0, semantic Recall@5 0.5869565217391305, exact-lexical Recall@5 0.8043478260869565, duplicate-slot rate 0.008695652173913045 (near-duplicate diversity category: 0.04347826086956521)

## What this benchmark does not prove

- These are synthetic fixtures on one machine: no cross-machine guarantees, no tenant authorization claims, and no enterprise-scale performance claims.
- The hash-vector embedder is deterministic and portable, but it is NOT a learned semantic embedding; weighted score fusion is not a second-stage reranker.
- Entity extraction, MMR, and rerankers remain optional future work (gated by #78 / #80 evidence), not core requirements.
