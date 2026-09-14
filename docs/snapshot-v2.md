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
  "snapshot_id": "<sha256 of sorted content_hash list>",
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
- `snapshot_id`: `SHA-256` of the sorted `content_hash` list. Deterministic
  for identical DB contents regardless of insert order.
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
record payload (`schema_version: 2`). Embeddings are stored as base64 so the
JSONL is text-portable and can be rehydrated without re-embedding.

```json
{
  "schema_version": 2,
  "id": "...", "identifier": "...", "memory_type": "fact" | "file",
  "fact_text": "..." | "", "fact_summary": "..." | null,
  "embedding": "<base64 or null>", "embedding_dim": 64,
  "embedding_model": "hotmem-hash-v1", "source": "...", "importance": 0.5,
  "metadata": {}, "content_hash": "...",
  "ttl_seconds": null | 3600, "namespace": "", "tier": "hot", "tags": [],
  "source_uri": null | "...", "byte_offset": null | 0, "byte_length": null | 100,
  "source_checksum": null | "...", "source_format": null | "csv",
  "provenance": null | {}, "created_at": "..."
}
```

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
  "counts": {"inline": 39, "file": 3, "total": 42}
}
```

## `attachments/`

Empty by default. When `copy_attachments=true` (CLI `--attach`, API
`copy_attachments: true`), file-backed byte ranges smaller than 8 KB
(`ATTACH_THRESHOLD`) are copied into `attachments/<sha256-of-range>` and the
corresponding `file_backed_references[].attachment` is set to the filename.
Larger ranges always stay referenced (reference-not-duplicate principle). On
any read error, the original `source_uri` is kept and the snapshot never
fails.

## Hydration

`hydrate(db, path)` infers the format from the path:

- A path ending in `.jsonl` or `.jsonl.gz`, or an existing file -> legacy
  single-file reader (tolerates plain records, stored-embedding records, and
  v2-columns-in-jsonl).
- A directory with `manifest.json` -> v2 reader: verify all listed file
  SHA-256s + `overall_sha256` (hard error on mismatch -> `SnapshotChecksumError`
  -> HTTP 409), then stream `memories.jsonl` into the DB. File-backed
  references are reconstructed **without** touching the backing files
  (references preserved, not bytes copied).
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
- Results report `loaded`, `skipped_dupes` (content-hash duplicates), and
  `invalid` (records that parsed but failed validation and were not stored).
  Structural corruption — digest/size mismatch, unparseable line — remains a
  hard error.
- Manifest-listed paths are confinement-checked: absolute paths, `..`
  traversal, and symlink escapes fail verification before any file is read.
- Inserts run in bounded batches with database-backed deduplication; no
  per-record commits, and the destination hash set is never loaded whole.

## Determinism

Identical DBs produce byte-identical `memories.jsonl` and `attachments/`
contents, and an identical `snapshot_id`. `manifest.json` bytes may differ by
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
