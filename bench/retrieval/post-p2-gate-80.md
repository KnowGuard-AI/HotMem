# Post-P2 gate report: #80 reranking (issue #80)

Companion to `bench/retrieval/baseline.json` (hash default) and
`bench/retrieval/semantic-local-m2v.json` (post-#78 semantic adapter). This
report records the go/no-go decision the #80 entry gate requires — it is
produced once, after the #78 work landed, and is the evidence link the issue
demands before any reranking hook is built.

## Gate definition (reconciled in this PR's C1)

#80's entry gate is the `near_duplicate_diversity` category duplicate-slot
rate — the category that directly measures near-duplicates occupying
diverse top-k slots. The 48-query aggregate dilutes the measurement with
duplicate-free categories and is reported for context only. Threshold:
20%. Both denominators are pinned by hand-calculated tests in
`tests/test_retrieval_eval.py`.

## Measurements (post-P2)

| Embedder | Diversity dup-slot | Aggregate dup-slot | Source |
|---|---|---|---|
| hash (`hotmem-hash-v1`, default) | 0.300 | 0.075 | `baseline.json` |
| semantic (`local-m2v/potion-base-8M/rev:v1/norm:l2/pp:m2v-static-v1`) | **0.400** | 0.104 | `semantic-local-m2v.json` |

The semantic adapter closed the #77 gap (semantic-paraphrase Recall@5
0.333 -> 1.000) with exact-lexical parity held at 1.000 (0.0pp cost,
within the <=5pp allowance) — and the duplicate-slot problem became MORE
acute, not less: better semantic similarity groups the near-duplicates
tighter, so they now monopolize the top-k together.

## Candidate-ordering failure (observed)

Every one of the six `near_duplicate_diversity` queries shows the same
pattern under the semantic embedder: the near-duplicate group occupies 2
of the top-5 slots (0.400) while equally-relevant diverse documents from
other topics sit below the cut. Example — `q-dup-003` ranks
`mem-cache-002, mem-cache-001, mem-cache-003` in the top three: the cache
near-duplicate cluster crowds out diverse relevant material; the
hash run shows the same shape on `q-dup-005/006` (0.400). Recall@5 stays
1.0 only because the fixtures keep enough relevant documents in range —
at production top-k budgets the displaced diverse results are lost. This
is exactly the "near-duplicates consume diverse top-k slots" failure #80
names.

## Integration requirement check

- Documented candidate-ordering failure: YES (above, per-query evidence
  in both committed reports).
- Production integration requirement with a local benchmark: none
  documented to date — this entry condition is NOT met and is not claimed.
- Duplicate-slot rate above 20%: YES under both embedders (0.300 hash,
  0.400 semantic).

## Decision

GO. The #80 entry gate is met on the diversity-category denominator under
both the default hash runtime and the post-#78 semantic runtime, and the
deterministic #77 recommendation now returns `pursue_reranking_hook_80`
on the committed semantic report. #80 proceeds with the bounded,
deterministic design recorded in the PR plan:

- `Reranker` / `RerankerDescriptor` protocols + a narrow `SearchCandidate`
  type; `IdentityReranker` is the default and MUST preserve the exact
  current order and response shape with zero extra work (byte-exact
  parity with this baseline).
- One deterministic MMR strategy: lambda and score scaling defined
  unambiguously, tie-break by pre-rerank order then memory id, missing or
  incompatible candidate embeddings contribute zero similarity
  (documented), bounded candidate pool (<= 50 default, documented safe
  range), final truncation after selection, batched work only — no
  per-candidate queries, no second corpus pass.
- Invalid reranker output (duplicate/unknown ids, cardinality or bound
  violations) falls back to the pre-rerank top-k with a trace diagnostic.
- Reranker selection never enters canonical records, snapshots, or sync
  identity; config mirrors the embedder path.
- Acceptance evidence: duplicate-slot reduction on the gate-opening
  benchmark case + Recall@5 allowance documented before acceptance + p95
  overhead for 20/50 candidate pools + disabled-overhead measurement
  against the main search path.

If implementation cannot meet the acceptance evidence, #80 returns to
open without the hook — the gate decision is recorded here either way.
