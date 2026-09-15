# OKF: HotMem Delta Contract v1

Status: Accepted (implementation contract; extension fields remain Proposed)
Owner: HotMem maintainers
Last updated: 2026-09-15
Scope: `hotmem-delta-v1` — verified one-way incremental transfer between
compatible HotMem instances

Implements: [#73](https://github.com/KnowGuard-AI/HotMem/issues/73).
Depends on: the `hotmem-interchange-v1` clone contract
([interchange-v1.md](interchange-v1.md), #67/#69). Whole-brain
clone-and-restore remains the recovery path of record.

---

## 1. Model

A **delta** is the verified, deterministic difference between a **base
package** (a `hotmem-interchange-v1` clone of the source) and the source's
current canonical state. A **receiver** that was hydrated from that base
package applies the delta once; re-applying it changes nothing.

One-way transfer only. The receiver never writes back. Multi-writer merge,
real-time replication, and hosted synchronization are out of scope and
remain separate future decisions.

v1 mechanism: **verified base-to-current state comparison**. The event log
is advisory — only server `/v1/add` ingestion emits per-record events today
(tested boundary), so event replay cannot reconstruct imported state and is
not a proven replication log. An event-based fast path may be added later,
gated on a completeness proof.

## 2. Package layout

```
<delta>/
  manifest.json
  operations.jsonl | operations.jsonl.gz
```

## 3. Manifest

Normative fields (readers MUST understand these):

```json
{
  "format": "hotmem-delta-v1",
  "schema_version": 1,
  "fingerprint_version": 1,
  "source": {"kind": "hotmem-dump"},
  "base": {
    "package_format": "hotmem-interchange-v1",
    "logical_id": "<base package logical_id>",
    "state_fingerprint": "<base state fingerprint>",
    "record_count": 42
  },
  "counts": {"added": 2, "changed": 1, "removed_since_base": 0, "total_ops": 3},
  "resulting_state_fingerprint": "<source state fingerprint after ops>",
  "files": {
    "operations.jsonl": {"size": 123, "sha256": "...",
                          "decompressed_sha256": "..."}
  },
  "embedding": {"model": "hotmem-hash-v1", "dim": 64}
}
```

- `fingerprint_version`: the state-fingerprint contract version (see §5).
  Receivers reject fingerprints they cannot interpret.
- `counts.removed_since_base`: records present in the base but absent from
  the current source. **Reported, never applied** — see §7.
- `resulting_state_fingerprint`: the source's state fingerprint when the
  delta was produced. An up-to-date receiver applies the delta and then
  holds this fingerprint; the next delta's base check compares against it.
- `created_at` / `hotmem_version`, when present, are informational.
- Compression: identical rules to interchange §4 (gzip `mtime=0`, verify
  via `decompressed_sha256`).

## 4. Operations

`operations.jsonl` is the canonical interchange serialization (sorted keys,
compact, UTF-8, LF), one operation per line, **sorted by record id** — the
same bundle state always produces byte-identical output.

```json
{
  "op": "upsert",
  "op_id": "<sha256 of record id + resulting_fingerprint>",
  "record_id": "<record id>",
  "expected_pre_fingerprint": "<state fingerprint the receiver must currently hold, or null>",
  "record": {"...full hotmem-interchange-v1 record..."},
  "resulting_fingerprint": "<fingerprint of record>"
}
```

- **Full-state upserts only.** Every sync-relevant mutation — content,
  metadata, importance, TTL, namespace, tags, provenance, promotion state —
  is expressed as a whole-record upsert. There are no field-level diffs and
  no separate lifecycle op types: promotion transitions change the record
  fingerprint and appear as upserts.
- `op_id` is derived from (`record_id`, `resulting_fingerprint`) and is
  stable across regenerations of the same change.
- `expected_pre_fingerprint` semantics:
  - a fingerprint string: the receiver must currently hold a record with
    this id AND that fingerprint (the base image of the record);
  - `null`: the receiver must NOT hold a record with this id.

## 5. State fingerprints

Per record: SHA-256 over the canonical sorted JSON of the sync-relevant
field set, computed on the normalized record. Collection: SHA-256 over the
sorted per-record fingerprint concatenation (order-independent).

Excluded from fingerprints (versioned contract, `fingerprint_version`):
`embedding` blob/dim/model (derived and rebuildable — interchange §5; model
or dimension changes are caught by the manifest compatibility block),
`updated_at` (runtime bookkeeping), `snapshot_id` (export provenance),
`id` (carried alongside). `parent_memory` / `related_memories` are excluded
pending contract review (ADR-003); adding them bumps
`fingerprint_version`.

## 6. Apply semantics (compare-and-swap)

Verification completes **before any target write** (files, sizes, digests,
record counts, versions, path confinement — interchange §7 rules apply).
Then, in one SQLite transaction:

For each operation, in file order:

1. Read the receiver's current record state for `record_id`.
2. If the current fingerprint equals `expected_pre_fingerprint` → apply
   (write the full record).
3. If the current fingerprint equals `resulting_fingerprint` → **skip**
   (already applied — this is what makes replay idempotent).
4. Otherwise → **conflict**: the receiver's state diverged from the base.
   Stop and report; nothing from this delta is applied.

Aggregate precondition: when a checkpoint for the same `source` exists, the
receiver compares `base.state_fingerprint` / `resulting_state_fingerprint`
continuity across chained deltas. A receiver holding neither the base image
nor a matching checkpoint gets a `base_missing` error with recovery
instructions.

Applied operations, the checkpoint row (source id, base identity, delta
digest, applied op ids, resulting fingerprint), and the `sync.applied`
receipt event commit **atomically**: any failure rolls back the records,
the checkpoint, and the receipt together. The replay checkpoint never
advances on failure.

Diagnostics carry: source identity, `op_id`/`record_id`, expected and
actual fingerprints, a reason, and the recovery instruction (re-clone or
re-base).

## 7. Conflicts, duplicates, and deletion policy

- **Duplicate delivery**: replaying an applied delta skips every operation
  (rule 3) and reports `applied: 0`. Duplicates never create duplicates
  (content-hash uniqueness plus CAS skips).
- **Conflict classes** (all actionable, none silent):
  `base_missing`, `state_divergence` (pre-image mismatch),
  `id_reuse` (receiver holds the id with an unrelated fingerprint and the
  delta expected absence), `unsupported_schema` / `unsupported_fingerprint`,
  `integrity` (digest/count/confinement failures), `unsupported_operation`.
- **Deletion policy (v1)**: no inferred deletion from absent records, no
  tombstones, no hard-delete propagation. Records removed at the source
  are counted in `removed_since_base`; receivers keep them. Removing a
  record is achieved by re-cloning (whole-brain recovery) or by a future
  explicit tombstone contract. An operation type other than `upsert` is
  rejected as `unsupported_operation`.
- **Recovery**: whole-brain clone and restore
  (`hotmem snapshot --package` → `hotmem hydrate`) remains the recovery
  path of record for diverged receivers, trimmed history, or policy
  changes.

## 8. Timestamps and provenance

Canonical operation records carry only source-originating time; runtime
apply timestamps live in the `sync.applied` event (`occurred_at`) and the
checkpoint row — never inside record bytes. Source provenance (sources[],
verified[], generated, usage_window, source_checksum, source_uri) is part
of the upserted record and survives transfer byte-for-byte.

## 9. Relationship to existing formats and surfaces

- Base packages and delta payloads reuse interchange serialization,
  confinement, digest, and compression rules — one format stack.
- Receivers keep every existing behavior: legacy JSONL hydrate, Snapshot v2,
  and bundle flows are unchanged; deltas apply on top of hydrated clones.
- Sync surface: `hotmem delta produce|apply` CLI plus the
  `hotmem.interchange.delta` library. No HTTP, MCP, or TypeScript response
  shape changes in v1.

## 10. Proposed fields (NOT emitted — pending contract review per ADR-003)

`per_record_fingerprints` in clone manifests (producer-side diff without
reading base payloads), tombstone operations with scoped re-add semantics,
namespace-scoped deltas with per-namespace counts, delta chaining
signatures, encryption/signing. Until reviewed: the producer reads the base
package payload to compute pre-images, and deltas are unauthenticated
local artifacts.

## 11. Conformance tests required by this contract

Deterministic producer output; idempotent replay; missing base; divergent
receiver edits; id reuse; tombstone and version rejection; corruption and
path escapes; interruption rollback (checkpoint and canonical state
unchanged); base+deltas equivalence against a fresh full clone (canonical
field equality and retrieval parity). See `tests/test_delta*.py`.
