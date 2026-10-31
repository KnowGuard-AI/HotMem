# OKF: HotMem Interchange Contract v1

Status: Accepted (implementation contract; extension fields remain Proposed)
Owner: HotMem maintainers
Last updated: 2026-09-14
Scope: `hotmem-interchange-v1` — canonical record stream, package manifest,
integrity, embedding compatibility, and hydration semantics

Implements: [#67](https://github.com/KnowGuard-AI/HotMem/issues/67).
Feeds: [#68](https://github.com/KnowGuard-AI/HotMem/issues/68),
[#69](https://github.com/KnowGuard-AI/HotMem/issues/69).

JSONL is the canonical stream format. Compression is a transport concern. The
manifest is the integrity and compatibility layer. Logical identity is derived
from content, never from compression, record order, export timestamps, or
derived embeddings.

---

## 1. Record schema

One JSON object per line. Fields are **required**, **conditionally required**,
**optional**, or **forward-compatible** (unknown keys).

| Class | Fields |
| --- | --- |
| Required | `schema_version`, `id`, `identifier`, `content_hash`, `memory_type` |
| Conditionally required | `fact_text` — non-inline records without a usable `fact_summary`; `source_uri`, `byte_offset`, `byte_length` — `memory_type: "file"` records |
| Optional | `fact_summary`, `source`, `importance`, `metadata`, `ttl_seconds`, `created_at`, `namespace`, `tier`, `tags`, `source_format`, `source_checksum`, `provenance`, `embedding` (b64), `embedding_dim`, `embedding_model`, `promotion_state`, `promotion_candidate`, `record_schema_version` |
| Forward-compatible | any other top-level key |

### 1.1 Field semantics

- `schema_version`: `1`. Readers must reject records claiming a *major*
  version they do not understand and must accept unknown *additive* fields.
- `id`: record identity within a stream. Importers derive it deterministically
  (for example the OKF importer hashes `concept_id + content_hash`); the
  runtime dump uses the stored row id. `id` is **not** part of logical
  identity.
- `identifier`: the logical name the memory is filed under.
- `content_hash`: the record's content digest. Inline records use
  `SHA-256("<identifier>:<fact_text>")`; file-backed records use
  `SHA-256("hotmem-file-ref-v1:<identifier>:<normalized_source_uri>:<byte_offset>:<byte_length>")`.
  `content_hash` is one half of logical identity; the other half is
  `namespace` (section 3.1). Hydration keeps the first record it sees for a given
  `(namespace, content_hash)` and reports later records as duplicates.
- `memory_type`: `"fact"` (inline content) or `"file"` (byte-range reference).
  Readers treat any value other than `"file"` as inline.
- `source_checksum`: optional **byte-integrity evidence** for the referenced
  range. It is verified on hydration only and is never part of logical
  identity: adding or correcting a checksum must not create a new memory.
- `fact_text` / `fact_summary`: prompt-ready content, or its summary when only
  a reference is carried.
- `embedding` / `embedding_dim` / `embedding_model`: base64 float32 blob plus
  its model and dimension — see §5.
- `metadata`: free-form JSON object preserved verbatim on round-trip.
- `provenance`: JSON object of source-originating provenance (for example OKF
  `generated`, `verified`, `sources`, `usage_window`).
- `namespace`, `tier`, `tags`, `source`, `importance`, `ttl_seconds`,
  `created_at`: retrieval and access metadata preserved on round-trip.
- `source_uri`, `source_format`, `source_checksum`, `byte_offset`,
  `byte_length`: file-backed reference metadata. External references are
  **never** dereferenced, fetched, or verified during export or hydration.
- `record_schema_version` (additive): the stored row's HotMem schema version
  when it differs from the interchange record schema (`1`). It preserves the
  row version (#99) without changing this contract's `schema_version`.
  Readers that do not understand it keep it as a forward-compatible field.

### 1.1a Complete 30-column `memories` field disposition (#99)

`hotmem.interchange.MEMORY_FIELD_INVENTORY` classifies every column in
`hotmem.db._MEMORY_COLUMNS`:

| Disposition | Columns | Contract |
| --- | --- | --- |
| `preserve` | `id`, `identifier`, `fact_text`, `source`, `importance`, `metadata_json`, `content_hash`, `ttl_seconds`, `created_at`, `namespace`, `tier`, `memory_type`, `source_uri`, `source_format`, `source_checksum`, `byte_offset`, `byte_length`, `updated_at`, `snapshot_id`, `promotion_state`, `promotion_candidate`, `parent_memory`, `related_memories`, `tags`, `schema_version`, `fact_summary`, `provenance_json` | Exported and restored without silent reset across clean runtimes. |
| `regenerate` | `embedding`, `embedding_dim`, `embedding_model` | Reused when compatible with the active `EmbeddingDescriptor` (#78); rebuilt from `fact_text`/`fact_summary` otherwise. |
| `exclude` | *(none on `memories`; store-local tables `store_meta`, `events`, `operation_ledger`, `sync_checkpoints`, `handoff_ledger`, `bundle_index` are outside `memories.jsonl`)* | Instance-local operational state is never mixed into portable memory records. |

Older-package defaults distinguish omitted/null legacy fields from explicit
empty, zero, or false values:
- `source`: omitted/null defaults to the reader's format label (`"swap"`,
  `"snapshot"`, `"interchange"`); explicit `""` is preserved as `""`.
- `importance`: omitted/null defaults to `0.5`; explicit `0.0` is preserved as
  `0.0`.
- `byte_offset`: omitted/null is `None`; explicit `0` is preserved as `0`.
- `byte_length`: omitted/null for `memory_type: "file"` is invalid; explicit
  `0` is a preserved zero-length slice (old packages keep hydrating).
- `promotion_candidate`: omitted/null defaults to `0`; explicit `0` or `1` is
  preserved.

### 1.2 Forward compatibility

- Readers MUST NOT reject a record for unknown top-level keys, unknown
  `memory_type` values (treated as inline), or missing optional fields.
- HotMem preserves unknown top-level keys on hydrate under
  `metadata["_interchange_unknown"]` so a round-trip never silently drops
  producer fields.
- Promotion state (`promotion_state`, `promotion_candidate`) **is** canonical
  access state — it changes retrieval behavior (archived memories are
  excluded by default) — and round-trips through clones and deltas
  (amended by #73). Event-log position, index markers, and other runtime
  bookkeeping remain outside the canonical record and are not exported.

## 2. Canonical serialization

Applies to **new** artifacts produced by this contract (interchange packages
and importer output). Historical Snapshot v2 and legacy swap serialization
remain readable unchanged; digests always cover the bytes actually written, so
older artifacts keep verifying.

- UTF-8, LF line endings, exactly one JSON object per line, no blank lines.
- Payload records are sorted by `id`.
- Serialization: `json.dumps(record, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False, allow_nan=False, default=str)`.
- Embeddings are base64 text, so no float formatting appears in record bytes.

## 3. Identity and digests

Three digest layers, computed in this order:

1. **Record**: `(namespace, content_hash)` (above) -- per-record logical
   identity and the deduplication key.
2. **Logical** (`logical_id` in the interchange package manifest): `SHA-256` of
   the concatenation of all record `content_hash` values in sorted order
   (`hotmem.interchange.canonical.logical_id` /
   `hotmem.snapshot.format.compute_legacy_snapshot_id`); equivalent content
   sets always produce an equal `logical_id`. This content-set digest does not
   encode namespaces (#100); it is not a per-record identity check. Snapshot v2
   manifests (`hotmem-snapshot-v2`) additionally version their `snapshot_id`
   with `snapshot_id_version: 2` (`hotmem-snapshot-id-v2`, issue #96) over all
   27 `preserve` columns, attachment references, and semantic namespace scope
   (see `docs/snapshot-v2.md`).
3. **File**: per-file `{size, sha256}` of the bytes actually stored, plus
   `decompressed_sha256` for compressed payloads.

Logical identity **excludes**: compression, record order in the payload,
export timestamps, host, tool version, and derived embeddings. Two packages
with the same `logical_id` are interchangeable regardless of how they were
compressed or when they were exported.

### 3.1 Namespace-scoped identity contract (#100)

Logical identity is the pair:

```
(namespace, content_hash)
```

`namespace` is a portable data scope, not authentication or authorization.
It participates in uniqueness and deduplication but is deliberately not
folded into `content_hash`, so the same inline fact or file reference can
exist once per namespace.

| Kind | Values that establish identity | Values that only verify content |
| --- | --- | --- |
| Inline | `namespace`, `content_hash = SHA-256(identifier + ":" + fact_text)` | none |
| File-backed | `namespace`, `content_hash = SHA-256("hotmem-file-ref-v1:" + identifier + ":" + normalized_source_uri + ":" + byte_offset + ":" + byte_length)` | `source_checksum` (SHA-256 of the referenced byte range) |

Source-reference normalization is syntactic and never reads, stats, or
dereferences the target. `file://` URIs are reduced to their filesystem path,
`.`/`..` components are collapsed, backslashes become `/`, Windows drive
letters are uppercased, paths inside `base_dir` are relativized to `base_dir`,
and non-`file` URI scheme and authority are lowercased. A remote/opaque URI is
preserved except for those syntactic normalizations.

Rules:

- **Same scope, same identity**: hydration/storage reports a skip and keeps
  the original row id, provenance, tags, and metadata. Later writers do not
  replace the original record.
- **Different namespaces, same content**: both records are stored, exported,
  and hydrated independently. `id` remains a store-wide primary key, so
  logically distinct records must carry distinct ids (the runtime generates
  a new id per write; importers preserve ids from the artifact).
- **Empty `content_hash`**: there is no logical identity. The SQLite unique
  index is partial (`WHERE content_hash != ''`), and empty hashes never match
  each other or an `exists` lookup. Such rows are deduplicated only by their
  primary-key `id`.
- **Legacy content hashes**: hashes written before #100 are opaque identity
  tokens and are preserved. Existing databases migrate the uniqueness scope
  from global `content_hash` to `(namespace, content_hash)` when the partial
  index is absent (schema v6). If a legacy database already contains duplicate
  `(namespace, content_hash)` rows, migration fails atomically with an
  actionable error instead of dropping data or silently weakening
  uniqueness. Pre-#100 file hashes (raw URI plus checksum) and pre-#100 bundle
  attachment hashes (byte digest) remain recognized as compatibility aliases
  across writes, hydration, and SQLite imports via normalized reference-tuple
  matching, so an upgrade does not duplicate them.
- **Surfaces**: every ingestion path uses this pair or delegates to one that
  does:

| Surface | Disposition |
| --- | --- |
| SQLite single/bulk insert and the v6 unique index | Uses `(namespace, content_hash)`; empty hashes excluded |
| `MemoryDB.exists` / `batch_existing_record_identities` | Namespace-scoped (with reference-tuple matching for legacy file rows); empty hashes never match |
| SQLite fast-path import (`import_sqlite`) | Namespace-aware batch dedup and `target_namespace` propagation; same-scope repeats skip |
| Legacy JSONL / JSONL.GZ, Snapshot v2, interchange package | Namespace-aware batch dedup before insert; intra-batch and cross-batch duplicate annotations merge cumulatively |
| Handoff hydration, bundles, annotations | Namespace-aware dedup/merge; bundle write-scope guard inspects `metadata.json` and record namespaces |
| Event replay | Inserts are deduplicated by the scoped unique index |
| HTTP, embedded/relay MCP, CLI, Python clients, TypeScript SDK, `swap.add_memory`, `Playground` | Delegate to the runtime or check `(namespace, content_hash)` before insert; preserve original row id and attribution |
| OKF and mem0 importers | Emit canonical records consumed by the hydrate paths above |
| Delta apply | Keyed by record `id` with CAS check; rejects a new `record_id` that collides with another row's `(namespace, content_hash)` as a conflict |
| Low-level `batch_existing_hashes` / `fetch_metadata_by_hashes` | Coarse hash-only helpers; never the final identity decision |

  `MemoryDB.fetch_metadata_by_namespace_hashes` is the scoped lookup used by
  annotation merging.

## 4. Compression and transport

- The standard transfer encoding is `memories.jsonl.gz`: gzip, `mtime=0`,
   empty embedded filename, fixed compresslevel (9), deflate.
- Gzip bytes are transport. Because compressed bytes are not stable across
   zlib builds, verification hashes the **decompressed** stream against
   `decompressed_sha256`; the file `sha256` covers the stored bytes as-is.
- Plain `memories.jsonl` is always valid.

## 5. Embedding compatibility

A stored embedding is reused as-is **iff all** hold:

1. `embedding_model` equals the reader's active embedding descriptor key,
2. `embedding_dim` equals the active descriptor's dimension,
3. the base64 blob decodes, its length is exactly `embedding_dim * 4` bytes,
   every value is finite, and the vector satisfies the descriptor's
   normalization policy (`l2` → unit norm within float32 tolerance).

Otherwise the record is re-embedded from `fact_text` (inline) or
`fact_summary` (file) under the **active** embedder and stamped with its
current descriptor — a stored vector is never relabeled as another model's.
If no usable text exists, the record is counted `invalid` and skipped —
never silently stored with a foreign embedding. Embeddings are derived data:
they are never part of logical identity and can always be rebuilt from
canonical text.

### 5.1 Embedding descriptors (issue #78)

`embedding_model` carries the canonical key of the producing embedder's
immutable descriptor: implementation, model, revision (when known),
dimension, normalization (`none`/`l2`), metric (`cosine`), and a
preprocessing/version identifier. The key is a compact structured
fingerprint — `implementation/model/rev:<r>/norm:<n>/pp:<p>` — so key equality
means full descriptor equality. Equal dimensions alone never establish
compatibility.

| Stored values | Meaning |
|---|---|
| `embedding_model` absent/empty | Legacy default: `hotmem-hash-v1` semantics |
| `embedding_dim` absent | Legacy default: 64 |
| `hotmem-hash-v1` | The deterministic hash default — key unchanged, so existing records, packages, and markers stay valid |
| structured key (e.g. `local/mini/rev:r1/norm:l2/pp:pp1`) | A semantic adapter's descriptor; reusable only by an active embedder with the identical key and dimension |

The package/delta manifest `embedding` block is derived from the payload's
embedded rows: a uniform store records `{"model": <descriptor key>,
"dim": <dimension>}`; a mixed-space store records `{"model": "mixed",
"dim": 0}` because no single key can describe it (per-record fields stay
authoritative); a store with no embedded rows keeps the hash default.
Hash-only packages are byte-identical to previous versions. Producers may
add further descriptor detail additively — readers ignore unknown manifest
fields. Rehydration reports four embedding statuses per
run: `embedding_reused`, `embedding_rebuilt`, `embedding_missing`
(textless file-backed rows keep the NULL-embedding convention), and
`embedding_failed` (provider errors: the canonical record still loads; an
explicitly requested reindex surfaces the provider error instead).

## 6. Manifest

A package is a directory:

```
<package>/
  manifest.json
  memories.jsonl | memories.jsonl.gz
```

Normative fields (readers MUST understand these):

```json
{
  "format": "hotmem-interchange-v1",
  "schema_version": 1,
  "record_count": 42,
  "logical_id": "<sha256 of sorted content_hash concatenation>",
  "files": {
    "memories.jsonl": {"size": 12345, "sha256": "..."}
  },
  "source": {"kind": "hotmem-dump"},
  "embedding": {"model": "hotmem-hash-v1", "dim": 64}
}
```

- `files` entries for compressed payloads additionally carry
  `decompressed_sha256`.
- `source.kind` ∈ `hotmem-dump | okf-import | wiki-import`.
- `created_at` and `hotmem_version`, when present, are **informational** and
  excluded from logical identity (§3).
- `scope`, when present, is an **informational** block describing a scoped
  export: `caller_id`, `session_id`, `source`, `namespace`,
  `permitted_namespaces`, `share_approvals`, `exported_count`,
  `omitted_count`, and `store_id`. It is excluded from logical identity and
  digests; readers ignore it if unknown.
- `mount_id`, `replica_id`, `history_id`, `state_fingerprint`, and
  `replay_cursor` (plus `source.mount_id`, `source.replica_id`, and
  `source.history_id`) are **additive optional** fields (issue #94); readers
  that do not know them ignore them. When `replay_cursor` is present, the
  identity fields are required and `verify_package` validates the cursor and
  recomputes `logical_id`/`state_fingerprint` from the payload before any target
  write. `state_fingerprint` covers records that fingerprint successfully;
  `logical_id` and `record_count` cover every exported record. Legacy manifests
  that omit these fields remain readable. Hydrating into a clean target store
  adopts `mount_id` and records the clone base lineage (including a target
  already bound to the same `mount_id`); targets bound to a different mount keep
  their identity. `compare_mount_lineage` classifies `"same_replica"`,
  `"clone"`, `"fork"`, `"replacement"`, and `"different_mount"`. Multi-writer
  ordering and conflict resolution across divergent runtime forks are undefined.

### 6.1 Proposed fields (NOT emitted — pending contract review per ADR-003)

The following are reserved names, documented so producers do not collide with
them, but this contract version does not define or emit them:
`transfer_provenance` (import timestamps, transfer chain), `attachments`
(in-package attachment manifest), `namespaces` (per-namespace scoping and
counts), `delta` (incremental sync descriptors), `encryption` / `signing`.
Until the contract review lands, runtime timestamps stay in the append-only
event log (`occurred_at`) and attachment portability stays with Snapshot v2.

## 7. Hydration semantics

- `loaded`: records inserted.
- `skipped_dupes`: records whose `(namespace, content_hash)` already exists in
  the target (or earlier in the same payload). Idempotent: re-hydrating a
  package into the same namespace reports `loaded == 0`. Identical records in
  another namespace are loaded, not skipped. Records with an empty
  `content_hash` have no identity and are not deduplicated by hash.
- `invalid`: records that parsed but failed validation and were **not**
  inserted (for example: no usable text and no compatible embedding).
- **Hard failures** — hydration halts and the target remains unchanged:
  missing manifest or payload, size/digest/record-count mismatch, truncated
  or corrupted compression, unparseable JSON line, manifest path escaping the
  package directory, and `schema_version` above the reader's major.
- Verification always completes **before** the first target write.
- Package restore is transactional: either every valid, non-duplicate record
  is committed or nothing is.

## 8. Timestamps and provenance

- Canonical record bytes carry only **source-originating** time: `created_at`
  and any source provenance (OKF `generated.at`, `verified[].at`,
  `sources[].last_modified`, `usage_window`).
- **Runtime timestamps** (when an export or import happened) are transfer
  provenance: they live in the append-only event log (`occurred_at`) and
  manifest informational fields — never inside canonical record bytes.
  This is what makes importer output byte-stable across runs.

## 9. Portable attachments vs external references

- v1 packages carry inline records only. File-backed records keep their
  `source_uri` as an **external reference**: hydration preserves the
  reference and never reads, writes, fetches, or verifies the external file.
- Content-addressed in-package attachments remain a Snapshot v2 capability
  (opt-in `--attach`); an interchange `attachments` manifest section is
  Proposed (§6.1).
- Every manifest-listed path is confinement-checked before it is read:
  no absolute paths, no `..` traversal, no symlink escapes.

## 10. Relationship to existing formats

- Legacy swap JSONL/GZ: record key `embedding_b64`; supported unchanged.
- Snapshot v2 directories: record key `embedding`; manifest `format` is
  `hotmem-snapshot-v2`; supported unchanged.
- The shared reader path normalizes both spellings and applies one embedding
  compatibility rule (§5) across all formats.
- A directory whose `manifest.json` declares `hotmem-interchange-v1` is an
  interchange package; `hotmem-snapshot-v2` selects the v2 reader.

## 11. Conformance tests required by this contract

Deterministic output (same source ⇒ identical logical bytes), old-format
fixtures (plain JSONL, `embedding_b64`, v2 `embedding`, `.gz`), malformed
frontmatter, symlink escapes, corrupted and truncated packages, incompatible
embeddings, provenance preservation, and duplicate/conflicting identities —
see `tests/` (interchange, OKF, package, and end-to-end clone suites).
