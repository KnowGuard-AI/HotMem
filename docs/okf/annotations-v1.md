# Annotation Envelope v1 (issue #79)

Contract notes for the reserved `metadata.annotations` envelope — HotMem as
the lossless transport for external enrichment. Not in the public docs site
(team contract notes, like the other OKF pages).

## Position

HotMem preserves enrichment; it does not produce, interpret, rank on, or
verify it. Entities, aliases, typed relationships, classifications, and
provenance supplied by external tools survive every movement path — JSONL,
JSONL.GZ, Snapshot v2, interchange packages, repeat hydration, one-way
delta replay — byte-for-byte in meaning and deterministically in form.
Annotations never influence ranking, query expansion, or canonical
identity beyond integrity (see delta-v1 §6): preservation, provenance,
deterministic merge, lossless movement.

## Envelope (schema 1)

Carried inside `metadata_json` under the reserved `annotations` key — no
new columns (no table or column per annotation type), no new record keys,
and fingerprinted through the existing metadata channel, so annotation
changes are integrity-protected and conflict-visible through the normal CAS
without any fingerprint version bump.

```json
{
  "schema_version": 1,
  "namespaces": {
    "org.example.entities": [
      {
        "id": "vendor-x",
        "type": "Organization",
        "confidence": 0.92,
        "evidence": [
          {"memory_id": "0f9c..."},
          {"uri": "https://example.com/vendor-x"}
        ],
        "scope": "finance",
        "authority": "manual",
        "valid_time": {"start": "2026-01-01"},
        "classification": "internal"
      }
    ]
  },
  "producers": {"org.example.enricher": {"version": "1.2"}}
}
```

Rules (structural, never semantic):

- The envelope is an object with `schema_version` 1. Future versions are
  gated — never silently reinterpreted.
- Namespace names are reverse-DNS style (`org.example.entities`); each maps
  to a list of item objects.
- Every item has a non-empty string `id`, unique within its namespace.
  `confidence`, when supplied, is a finite number in [0, 1]. `evidence`,
  when supplied, is a list of local references (`{"memory_id": ...}`,
  resolved against the incoming package and the existing target — forward
  references included; dangling references are actionable errors) or
  external URIs (`{"uri": ...}` or plain strings — preserved verbatim,
  never fetched, never executed).
- Enterprise provenance fields (scope, authority, valid_time, recorded_time,
  classification) are preserved as supplied; their semantics stay with the
  producer.
- Unknown namespaces and unknown keys are preserved as JSON values:
  canonicalized object key order, preserved array order. Malformed KNOWN
  structure fails validation with actionable errors; unknown content never
  does.
- Limits: 128 KiB serialized envelope per record, 1000 total items, JSON
  depth 8.

## Validation surface

- `POST /v1/add` — actionable `400 invalid_annotations`.
- `normalize_record` (every interchange path) — malformed records count
  `invalid` and are skipped (interchange-v1 §7), never a whole-restore
  failure.
- Package and snapshot hydration resolve local evidence against the full
  incoming id set plus the existing target (forward references supported).
- Records without annotations pay one dict membership check — the
  annotation-less path adds no work.

## Merge (deterministic, by namespace + item id)

When hydrating a record whose canonical content hash already exists but
whose envelope differs — the case content-hash dedup used to silently skip:

- same id + same content → no-op (idempotent replay)
- disjoint ids → combined, in-transaction, serialized with canonical key
  order so map insertion order never affects identity or fingerprints
- same id + different content → an explicit conflict: the target version is
  retained, the incoming version is preserved in the merge outcome, and the
  hydrate result counts it (`annotation_conflicts`). Never
  last-write-wins, never a silent drop.

Hydration reports `annotations_merged` (items added) and
`annotation_conflicts` (conflicting items) alongside the embedding
dispositions (#78). Delta annotation-only changes ride the existing CAS:
fingerprints include metadata, so the change flows as a normal upsert with
preconditions, rollback, and idempotent replay.

## Multi-writer merge

Out of scope for v1: reconciliation ACROSS diverged replicas is a delta/CAS
concern, not an envelope concern. The envelope guarantees both sides of a
conflict are retained with provenance when they meet.

## Fixtures

`bench/annotations/mapping-fixture.json` is the synthetic reference mapping
(entity + alias, typed relationship, producer/version, confidence, local
and external evidence) used by the contract tests — no vendor runtime, no
network, safe to publish.
