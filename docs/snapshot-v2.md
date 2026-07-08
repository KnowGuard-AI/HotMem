# Snapshot v2 Format

This document specifies the HotMem Snapshot v2 directory format introduced in
#39. GitHub issues own scope and acceptance criteria; this doc owns the format
specification and rationale.

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
determinism.

```json
{
  "format": "hotmem-snapshot-v2",
  "schema_version": 2,
  "snapshot_id": "<sha256 of sorted content_hash list>",
  "memory_count": 42,
  "file_backed_count": 3,
  "inline_count": 39,
  "files": {
    "memories.jsonl": {"size": 12345, "sha256": "..."},
    "attachments/<name>": {"size": 1024, "sha256": "..."}
  },
  "overall_sha256": "<sha256 of sorted per-file sha256 concatenation>",
  "file_references": [
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
- `files`: per-file `{size, sha256}`. Only `memories.jsonl` and any copied
  `attachments/<name>` are listed. `metadata.json` is intentionally **not**
  listed (informational only, so wall-clock timestamps don't break
  determinism or verification).
- `overall_sha256`: `SHA-256` of the concatenated per-file `sha256` hex
  digests, in sorted-by-filename order.
- `file_references`: one entry per file-backed memory. `attachment` is the
  filename within `attachments/` when the byte range was copied in (opt-in,
  small ranges), or `null` when the memory still points at its original
  `source_uri`.

## `memories.jsonl`

One JSON object per line, sorted by `id`. Each record carries the full Memory
Record v2 payload (`schema_version: 2`). Embeddings are stored as base64 so
the jsonl is text-portable and can be rehydrated without re-embedding
(stored-embedding variant, #25).

```json
{
  "schema_version": 2,
  "id": "...", "identifier": "...", "memory_type": "inline" | "file",
  "fact_text": "..." | null, "fact_summary": "..." | null,
  "embedding": "<base64 or null>", "embedding_dim": 64,
  "embedding_model": "hotmem-hash-v1", "source": "...", "importance": 0.5,
  "metadata": {}, "content_hash": "...",
  "source_uri": null | "...", "byte_offset": null | 0, "byte_length": null | 100,
  "source_checksum": null | "...", "source_format": null | "csv",
  "provenance": null | {}, "created_at": "..."
}
```

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
corresponding `file_references[].attachment` is set to the filename. Larger
ranges always stay referenced (reference-not-duplicate principle). On any read
error, the original `source_uri` is kept and the snapshot never fails.

## Hydration

`hydrate(db, path)` infers the format from the path:

- A path ending in `.jsonl` or `.jsonl.gz`, or an existing file -> legacy
  single-file reader (tolerates plain records, stored-embedding records, and
  v2-columns-in-jsonl).
- A directory with `manifest.json` -> v2 reader: verify all listed file
  SHA-256s + `overall_sha256` (hard error on mismatch -> `SnapshotChecksumError`
  -> HTTP 409), then stream `memories.jsonl` into the DB using stored base64
  embeddings when present. File-backed references are reconstructed **without**
  touching the backing files (references preserved, not bytes copied).
- A directory with `memories.jsonl` but no `manifest.json` -> legacy reader on
  that file.
- A directory with neither -> `SnapshotChecksumError("missing_manifest")`.

## Determinism

Identical DBs produce byte-identical `manifest.json` + `memories.jsonl` +
`attachments/` contents. Only `metadata.json.created_at` (and `host` if the
hostname differs) may vary, and neither is checksummed.

## Legacy compatibility

- `swap.jsonl` (plain, no stored embedding) -> re-embeds `fact_text` on hydrate
  (original v0.1 behavior).
- `swap.jsonl` with base64 `embedding` field per record (#25) -> uses the
  stored embedding directly.
- `.jsonl.gz` -> gzip-compressed legacy JSONL.
- The legacy writer now emits v2 columns + base64 embeddings, so legacy
  snapshots are also stored-embedding-capable and round-trip file-backed
  references intact.
- `hotmem.swap` remains a re-export shim; existing imports keep working.

## Versioning

Bump `SCHEMA_VERSION` in `src/hotmem/snapshot/format.py` and add a migration
path in `reader.py` for future incompatible format changes. Older snapshots
remain readable; the reader dispatches by `schema_version`.
