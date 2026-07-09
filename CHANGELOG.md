# Changelog

All notable changes to HotMem will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.1] - 2026-07-08

### Added — Snapshot v2 directory format (#39)
- Snapshot v2 directory layout: `manifest.json` (authoritative, checksummed),
  `memories.jsonl` (sorted by id, base64 embeddings), `metadata.json`
  (informational, not checksummed), and an opt-in `attachments/` directory.
- `manifest.json` carries `schema_version`, `snapshot_id`, per-file SHA-256
  + size, an `overall_sha256` aggregate, and a `file_references` array for
  file-backed memories.
- Unified path-based dispatch: `hotmem snapshot/hydrate --file <path>` and
  `/v1/snapshot`/`/v1/hydrate` infer the format from the path (`.jsonl`/
  `.jsonl.gz` -> legacy single-file; directory -> v2). No new required flags.
- `--attach` flag (v2 only) copies small file-backed byte ranges (< 8 KB)
  into `attachments/`; large ranges stay referenced (reference-not-duplicate).
- Deterministic output: identical DBs produce byte-identical manifest +
  memories + attachments (only `metadata.json.created_at` differs).
- Manifest checksum verification on hydrate: any mismatch/missing listed file
  raises `SnapshotChecksumError` -> HTTP 409 with a precise JSON body.
- Legacy reader now tolerates stored-embedding records (#25) and v2 columns
  in JSONL; legacy writer now emits v2 columns + base64 embeddings.
- `.jsonl.gz` gzip support on legacy read/write.
- `/v1/snapshot` and `/v1/hydrate` accept `path` (new) with `file` as a
  deprecated alias for backwards compatibility.

### Changed
- `MemoryDB.all_rows()` now includes the `embedding` BLOB so snapshots can
  store embeddings instead of re-embedding on hydrate.
- `hotmem.swap` is now a thin re-export shim over `hotmem.snapshot.legacy`;
  existing imports and `tests/test_swap.py` keep working unchanged.

## [0.2.0] - 2026-07-08

### Added — File-backed memories (#38)
- Memory Record v2 schema with provenance columns (`memory_type`,
  `source_uri`, `byte_offset`, `byte_length`, `source_checksum`,
  `source_format`, `fact_summary`, `provenance_json`).
- Additive v1 -> v2 migration that relaxes `fact_text` to nullable and
  backfills existing rows as `memory_type='inline'`. Inline memories and
  existing `/v1/add` payloads using `identifier` + `fact` are unchanged.
- Storage Adapter abstraction (`hotmem.storage`) with a local-filesystem
  implementation (`hotmem.storage.local`) providing `read_range`,
  `checksum` (SHA-256 of the byte range), `exists`, and `stat`.
- `POST /v1/add` now accepts an optional `file_ref` object (URI + byte
  offset + byte length + format + optional checksum), mutually exclusive
  with `fact`. Stores a reference with **zero bytes copied** into SQLite.
- `GET /v1/memory/{id}` returns metadata without touching the backing file
  (lazy by construction).
- `POST /v1/memory/{id}/hydrate` materializes the payload on demand: inline
  memories return `fact_text` as bytes; file-backed memories read exactly
  `[offset, offset+length)` via the adapter and verify `source_checksum`
  on demand. Mismatch / missing / truncated files return HTTP 409 with a
  clear JSON body (`{"error":"provenance_mismatch", ...}`).
- Optional `summary` on file-backed memories is embedded so they remain
  searchable; the `/v1/search` response shape is unchanged.
- `HotMemClient.add_file_ref()`, `.get_memory()`, `.hydrate_memory()`.

### Changed
- Only local schemes are supported for file refs (`file://`, absolute,
  relative paths resolved against the mount dir). Remote schemes
  (`s3://`, `hdfs://`, `abfs://`, `gs://`) are rejected at the add
  boundary with HTTP 400 `unsupported_scheme` (EMOS-owned).
- Cosine UDF returns `0.0` for NULL embeddings (file-backed without
  summary) so they are excluded from ranked search but still retrievable
  via the metadata endpoint.

## [0.1.0] - 2025-05-02

### Added
- FastAPI sidecar server on port 8711
- SQLite storage with cosine similarity UDF
- Hash-based embedding (dim=64, zero external dependencies)
- Hybrid search: cosine similarity + keyword overlap + importance weighting
- LLM-ready message object output from search
- JSONL hydrate/snapshot for portable backup
- Mount directory concept (SQLite + swap + manifest)
- Python client SDK (`HotMemClient`)
- CLI: `serve`, `hydrate`, `snapshot`, `status`
- Structured JSON tracing to stderr
