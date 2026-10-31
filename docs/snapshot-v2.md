# Snapshot v2 Format

This document specifies the HotMem Snapshot v2 directory format.

## Layout

```
<snapshot_dir>/
  manifest.json        # authoritative index + checksums
  memories.jsonl      # one record per memory, sorted by id, base64 embeddings
  metadata.json        # informational generation info; NOT checksummed
  attachments/         # opt-in; <sha256-of-range> files when copy_attachments=true
```

## `manifest.json`

The authoritative index. Written with `sort_keys=True, indent=2` for
determinism of the checksummed payload; the manifest itself also records the
export time (`created_at`) and `hotmem_version`, which are informational.

```json
{
  "format": "hotmem-snapshot-v2",
  "schema_version": 2,
  "snapshot_id": "<sha256 of canonical semantic snapshot projection>",
  "snapshot_id_version": 2,
  "snapshot_id_algorithm": "hotmem-snapshot-id-v2",
  "memory_count": 42,
  "file_backed_count": 3,
  "inline_count": 39,
  "created_at": "2026-07-08T14:00:00+00:00",
  "hotmem_version": "0.2.5",
  "files": {
    "memories.jsonl": {"size": 12345, "sha256": "..."},
    "attachments/<name>": {"size": 1024, "sha256": "..."}
  },
  "overall_sha256": "<sha256 of sorted per-file sha256 concatenation>",
  "file_backed_references": [
    {
      "memory_id": "...",
      "source_uri": "...",
      "byte_offset": 0,
      "byte_length": 100,
      "source_checksum": "...",
      "source_format": "csv",
      "attachment": null
    }
  ]
}
```

### Field semantics

- `format`: always `"hotmem-snapshot-v2"`.
- `schema_version`: `2`. Bumped for future incompatible changes.
- `scope` (optional, informational): present on scoped daemon exports;
  records `caller_id`, `session_id`, `source`, `namespace`,
  `permitted_namespaces`, `share_approvals`, `exported_count`,
  `omitted_count`, and `store_id`. It is never part of `overall_sha256`.
  Semantic namespace partition keys (`namespace`, `permitted_namespaces`)
  participate in v2 semantic `snapshot_id`; caller/session/counter provenance
  inside `scope` is excluded as packaging noise.
- `snapshot_id`, `snapshot_id_version`, `snapshot_id_algorithm` (#96):
  - New Snapshot v2 writers emit `snapshot_id_version: 2` and
    `snapshot_id_algorithm: "hotmem-snapshot-id-v2"`.
  - Under v2 (`hotmem-snapshot-id-v2`), `snapshot_id` is the domain-separated
    `SHA-256` digest of the canonical semantic snapshot projection over all
    27 `preserve` record columns from `MEMORY_FIELD_INVENTORY` (#99),
    canonical `file_backed_references` (including `attachment` bindings), and
    normalized namespace `scope` (`compute_semantic_snapshot_id`).
  - Two snapshots with the same `content_hash` values in different namespaces,
    or with different portable metadata, provenance, lifecycle, relationship,
    or attachment reference state, produce distinct v2 `snapshot_id` values.
  - Legacy v1 manifests (`snapshot_id_version: 1`,
    `snapshot_id_algorithm: "hotmem-snapshot-id-v1"`, or unversioned pre-#96
    manifests) use `compute_legacy_snapshot_id` (`SHA-256` of sorted
    `content_hash` strings) and remain verifiable under an explicit
    backward-compatibility rule.
- `created_at` / `hotmem_version`: informational provenance of the export.
  They are not covered by any digest, so re-exporting the same DB changes
  `manifest.json` bytes but never changes `snapshot_id`.
- `files`: per-file `{size, sha256}`. Only `memories.jsonl` and any copied
  `attachments/<name>` are listed. `metadata.json` is intentionally **not**
  listed (informational only, so wall-clock timestamps don't break
  determinism or verification).
- `overall_sha256`: `SHA-256` of the concatenated per-file `sha256` hex
  digests, in sorted-by-filename order.
- `file_backed_references`: one entry per file-backed memory. `attachment` is
  the filename within `attachments/` when the byte range was copied in (opt-in,
  small ranges), or `null` when the memory still points at its original
  `source_uri`. Older snapshots wrote this key as `file_references`; the
  reader accepts both spellings for compatibility.

## `memories.jsonl`

One JSON object per line, sorted by `id`. Each record carries the full memory
record payload (`schema_version: 2`, plus optional `record_schema_version`
carrying the row's stored SQLite `memories.schema_version` when it differs from
`2`; row version `2` is implicit in the `2` envelope). Embeddings are stored
as base64 so the JSONL is text-portable and can be rehydrated without
re-embedding.

```json
{
  "schema_version": 2,
  "record_schema_version": 1,
  "id": "...", "identifier": "...", "memory_type": "fact" | "file",
  "fact_text": "..." | "", "fact_summary": "..." | null,
  "embedding": "<base64 or null>", "embedding_dim": 64,
  "embedding_model": "hotmem-hash-v1", "source": "...", "importance": 0.5,
  "metadata": {}, "content_hash": "...",
  "ttl_seconds": null | 3600, "namespace": "", "tier": "hot", "tags": [],
  "related_memories": [], "parent_memory": "",
  "source_uri": null | "...", "byte_offset": null | 0, "byte_length": null | 100,
  "source_checksum": null | "...", "source_format": null | "csv",
  "provenance": null | {}, "created_at": "...",
  "updated_at": null | "...", "snapshot_id": "",
  "promotion_state": "HOT", "promotion_candidate": 0
}
```

### 30-column field disposition contract (#99)

Every column of the SQLite `memories` table (`hotmem.db._MEMORY_COLUMNS`) is
governed by `hotmem.interchange.MEMORY_FIELD_INVENTORY`:

- **`preserve` (27 columns):** `id`, `identifier`, `fact_text`, `source`,
  `importance`, `metadata_json` (`metadata`), `content_hash`, `ttl_seconds`,
  `created_at`, `namespace`, `tier`, `memory_type`, `source_uri`,
  `source_format`, `source_checksum`, `byte_offset`, `byte_length`,
  `updated_at`, `snapshot_id`, `promotion_state`, `promotion_candidate`,
  `parent_memory`, `related_memories`, `tags`, `schema_version`
  (`record_schema_version`), `fact_summary`, and `provenance_json`
  (`provenance`).
- **`regenerate` (3 columns):** `embedding`, `embedding_dim`,
  `embedding_model`. Compatible stored vectors (matching `(embedding_model,
  embedding_dim)` and finite unit-norm float32 payload) are reused without
  recomputation; incompatible or missing vectors are rebuilt from
  `fact_text` / `fact_summary` under the active embedder.
- **Store-local tables (`store_meta`, `events`, `operation_ledger`,
  `sync_checkpoints`, `handoff_ledger`, `bundle_index`):** intentionally
  excluded from `memories.jsonl` because they represent instance-local state.

All fields round-trip: `namespace`, `tier`, `tags`, `ttl_seconds`, and
`provenance` survive snapshot -> hydrate (verified by tests). Unknown
top-level keys are preserved on hydrate under
`metadata["_interchange_unknown"]`.

## `metadata.json`

Informational only. Excluded from `overall_sha256` so two snapshots of the
same DB differ only in `created_at` (and `host`).

```json
{
  "hotmem_version": "0.2.1",
  "created_at": "2026-07-08T14:00:00+00:00",
  "host": "<hostname>",
  "db_path": "...",
  "counts": {"inline": 39, "file": 3, "total": 42},
  "scope": {"caller_id": "caller-a", "exported_count": 39,
            "scope_omitted_count": 1, "disclosure_omitted_count": 2,
            "omitted_count": 3, "redacted_count": 0, "...": "..."}
}
```

`host` and `db_path` are empty strings on scoped daemon exports (no path
disclosure). `scope` is present only for scoped exports; it is informational
and never checksummed.

## `attachments/`

Empty by default. When `copy_attachments=true` (CLI `--attach`, API
`copy_attachments: true`), file-backed byte ranges smaller than 8 KB
(`ATTACH_THRESHOLD`) are copied into `attachments/<sha256-of-range>` and the
corresponding `file_backed_references[].attachment` is set to the filename.
Larger ranges always stay referenced (reference-not-duplicate principle). On
any read error, truncation, or `source_checksum` mismatch, the attachment is
**not** written, the original `source_uri` is kept, and the snapshot never
fails; the record remains a declared external dependency.

Every declared dependency is verified before hydration: `attachments/` names
must be single path segments confined to the snapshot, listed in
`manifest.files`, and match the declared size and `source_checksum` (or
`sha256`) of `file_backed_references` / `attachments`.

When the original backing file is absent from a clean runtime, on-demand byte
hydration (`hydrate_memory_detailed` / `hydrate_many`) can materialize the
verified slice from `attachments/` if `base_dir` points at this snapshot
directory. The returned bytes are length-checked and verified against
`source_checksum` when present.

## Hydration

`hydrate(db, path)` infers the format from the path:

- A path ending in `.jsonl` or `.jsonl.gz`, or an existing file -> legacy
  single-file reader (tolerates plain records, stored-embedding records, and
  v2-columns-in-jsonl).
- A directory with `manifest.json` -> v2 reader: verify all listed file
  SHA-256s + `overall_sha256` (hard error on mismatch -> `SnapshotChecksumError`
  -> HTTP 409), then stream `memories.jsonl` into the DB in one transaction
  (a failure rolls the whole hydrate back). File-backed references are
  reconstructed **without** touching the backing files (references
  preserved, not bytes copied); reading their bytes later can use the
  snapshot's verified `attachments/` when `base_dir` is the snapshot
  directory.
- A directory with `memories.jsonl` but no `manifest.json` -> legacy reader on
  that file.
- A directory with neither -> `SnapshotChecksumError("missing_manifest")`.

Hydration semantics (shared with the interchange contract,
`docs/okf/interchange-v1.md` §7):

- Stored embeddings are reused only when they are **compatible**:
  `embedding_model == hotmem-hash-v1`, `embedding_dim == 64`, and the base64
  blob decodes to exactly 256 bytes. Otherwise the record is re-embedded from
  `fact_text` (inline) or `fact_summary` (file-backed); a file-backed record
  with no summary gets an empty embedding (NULL-embedding convention, #38).
- Results report `loaded`, `skipped_dupes`, and `invalid` (records that
  parsed but failed validation and were not stored). Deduplication follows
  the logical identity contract: `(namespace, content_hash)` for non-empty
  hashes (`docs/okf/interchange-v1.md` section 3.1). Repeating a restore in the same
  namespace reports skips; identical records in another namespace are stored.
  Structural corruption -- digest/size mismatch, unparseable line -- remains a
  hard error.
- Manifest-listed paths are confinement-checked: absolute paths, `..`
  traversal, and symlink escapes fail verification before any file is read.
- Inserts run in bounded batches with database-backed deduplication; no
  per-record commits, and the destination hash set is never loaded whole.

## Determinism

Identical DBs produce byte-identical `memories.jsonl` and `attachments/`
contents, and an identical `snapshot_id`. `memories.jsonl` is always written as
raw UTF-8 with LF (`\n`) record terminators on every platform (including
Windows) so on-disk file sizes and SHA-256 digests match `manifest.json`
regardless of host OS newline conventions. `manifest.json` bytes may differ by
the informational `created_at`/`hotmem_version` fields, and `metadata.json`
by `created_at`/`host` — neither is checksummed, so verification and logical
identity are unaffected.

## Legacy compatibility

- `swap.jsonl` (plain, no stored embedding) -> re-embeds `fact_text` on hydrate
  (original v0.1 behavior).
- `swap.jsonl` with a base64 `embedding_b64` field per record -> reuses the
  stored embedding when its `embedding_dim`/`embedding_model` are compatible
  (this is the legacy field name; Snapshot v2 records use `embedding`).
- `.jsonl.gz` -> gzip-compressed legacy JSONL.
- The legacy writer emits the full v2 column set + `embedding_b64`, and the
  reader preserves all of them on hydrate (namespace, tier, tags, provenance,
  file-backed references), so legacy snapshots round-trip intact.
- A v2 record carrying `embedding` hydrates through the same compatibility
  rule as `embedding_b64`; the two spellings are interchangeable to the
  readers.
- `hotmem.swap` remains a re-export shim; existing imports keep working.

## Versioning

Bump `SCHEMA_VERSION` in `src/hotmem/snapshot/format.py` and add a migration
path in `reader.py` for future incompatible format changes. Older snapshots
remain readable; the reader dispatches by `schema_version`.

## Canonical semantic snapshot identity (issue #96)

Snapshot v2 separates **semantic snapshot identity** (`snapshot_id`) from
**package-byte integrity** (`files` and `overall_sha256`), **per-memory logical
identity** (`(namespace, content_hash)`, #100), **delta CAS fingerprinting**
(`record_fingerprint`, #73), and **replica clone/fork divergence**
(`lineage_fingerprint`, #94):

1. **Per-record semantic projection (`semantic_record_projection` /
   `semantic_record_digest`)**:
   - Includes all 27 `preserve` columns from `MEMORY_FIELD_INVENTORY` (#99):
     `id`, `identifier`, `fact_text`, `fact_summary`, `memory_type`,
     `content_hash`, `namespace`, `source`, `importance`, `metadata`,
     `provenance`, `tags`, `ttl_seconds`, `created_at`, `updated_at`, `tier`,
     `promotion_state`, `promotion_candidate`, `parent_memory`,
     `related_memories`, `source_uri`, `source_format`, `source_checksum`,
     `byte_offset`, `byte_length`, `snapshot_id` (row origin-snapshot column),
     and resolved row `schema_version` (`record_schema_version` when present).
   - Excludes the 3 `regenerate` columns (`embedding`, `embedding_dim`,
     `embedding_model`) and store-local tables.
   - Normalizes all string scalars and JSON object keys to Unicode NFC,
     recursively sorts JSON object keys (`metadata`, `provenance`), normalizes
     `-0.0` to `0.0`, normalizes `source_uri` via `normalize_source_uri`, and
     resolves omitted/null values to their documented defaults while preserving
     explicit `source: ""` and `byte_offset: 0` / `byte_length: 0`.
   - Domain prefix: `hotmem-snapshot-record-v2:<canonical_json>`.
2. **Snapshot envelope (`semantic_snapshot_projection` /
   `compute_semantic_snapshot_id`)**:
   - `records`: ascending-sorted list of per-record `semantic_record_digest`
     hex strings (order-independent over `memories.jsonl` lines; duplicate
     logical records are preserved as a deterministic sorted multiset).
   - `file_backed_references`: canonically sorted list of projected
     `FileReference` dicts (`memory_id`, normalized `source_uri`,
     `byte_offset`, `byte_length`, `source_format`, `source_checksum`,
     `attachment`).
   - `scope`: `{"namespace": ..., "permitted_namespaces": [...]}` when a
     semantic namespace filter is present, or `null` when unscoped (excluding
     transient caller/session/counter fields).
   - `version`: `2`.
   - Domain prefix: `hotmem-snapshot-id-v2:<canonical_json>`.
   - Empty unscoped snapshots hash `{"file_backed_references":[],"records":[],"scope":null,"version":2}`
     to the fixed digest
     `f8a6cf35d0d17fab750458fdd94399434cf4e5816fa10cb166d01ac22946b3dd`.
3. **Versioned verification & legacy compatibility (`verify_manifest`)**:
   - `snapshot_id_version: 2` (`"hotmem-snapshot-id-v2"`): recomputes and
     enforces the v2 semantic digest over `memories.jsonl`,
     `file_backed_references`, and `scope`.
   - `snapshot_id_version: 1` (`"hotmem-snapshot-id-v1"`): recomputes and
     enforces the legacy v1 content-hash-list digest
     (`compute_legacy_snapshot_id`).
   - Unversioned legacy manifests: cursor-bearing (#94) manifests verify
     against the v1 (or v2) digest; pre-#94 cursorless manifests preserve
     byte-integrity compatibility. Unknown or mismatched
     `(snapshot_id_version, snapshot_id_algorithm)` metadata fails closed with
     `SnapshotChecksumError("malformed")`.

## Portable mount identity and replay cursors (issue #94)

Snapshot v2 manifests carry additive lineage and replay cursor fields:

- `mount_id`: logical mount/workspace identity (`mnt-<hex>` or explicit mount
  configuration). Preserved across clones, restores, and forks of the same
  workspace.
- `replica_id`: runtime SQLite replica identity (`rep-<hex>`). Exported as
  source provenance; a hydrating target always keeps or generates its own
  distinct `replica_id`.
- `history_id`: source replica event-log epoch (`hist-<hex>`). Regenerated
  whenever a replica is cloned, restored, forked, or replaced.
- `state_fingerprint`: canonical state fingerprint over exported records that
  fingerprint successfully (annotation-invalid rows are exported and counted but
  excluded from this digest).
- `replay_cursor`: opaque, versioned `hmc1.<payload>.<checksum>` replay cursor
  bound to `(mount_id, replica_id, history_id, base_snapshot_id, scope, seq,
  anchor_event_id)`. The checksum is the full SHA-256 digest of the token and
  detects corruption/accidental drift, but it is not authentication (an
  explicit non-goal); use authenticated transport for untrusted channels.
  Cursors and manifests must be treated as untrusted input.
  Scope (`namespace`, `event_types`) is fixed at issuance; replay callers cannot
  narrow it later.

Derived identity fields on a manifest that carries `replay_cursor` are verified
against the payload during `verify_manifest` (`snapshot_id`, `memory_count`,
`state_fingerprint`), and identity fields are required alongside the cursor.
Legacy manifests without a cursor keep byte-only verification.

Verification and hydration rules:

- A bare SQLite `seq` integer, numeric string, or unanchored mapping is never
  accepted as a portable cursor (`ReplayCursorError(reason="unanchored_sequence")`).
- If `replay_cursor` is present in `manifest.json`, `verify_manifest` validates
  its `hmc1` prefix, version, checksum, and alignment with `mount_id`,
  `replica_id`, and `history_id` before any record is read. Legacy pre-#94
  manifests without `replay_cursor` remain readable.
- Hydrating into an empty/clean store adopts `mount_id` (when `target_namespace`
  is unset) and records `base_snapshot_id`, `base_state_fingerprint`,
  `base_source_replica_id`, `base_history_id`, and `base_cursor`, establishing
  a `"clone"` relationship (`compare_mount_lineage`).
- Independent local writes on a cloned replica transition its lineage to
  `"fork"`. Multi-writer ordering and automatic fork conflict resolution are
  intentionally undefined; cursor replay against a wrong source, stale/trimmed
  base, replaced history, or divergent fork fails closed before mutation.
