# Changelog

All notable changes to HotMem will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added — portable embeddings, lossless annotations, gated reranking (#78/#79/#80)
- **Portable embedding boundary (#78).** `Embedder` protocol + immutable
  `EmbeddingDescriptor` whose canonical key persists as the record
  `embedding_model` (the hash default stays exactly `hotmem-hash-v1`: zero
  migration, existing packages valid). Descriptor-based compatibility:
  equal dimensions never establish compatibility; absent legacy fields keep
  hash semantics; blobs are validated for length, finite values, and
  normalization policy. Rehydration reports reused/rebuilt/missing/failed
  dispositions; rebuilds stamp the active descriptor, provider failures
  preserve the canonical record. The runtime embedder injects through every
  write/search/hydrate/reindex path (CLI `--embedder`/`--embedder-model-path`
  + env fallback, validated before serving; sanitized descriptor in
  `/v1/health`). Mixed-space safety: foreign-descriptor rows score zero
  cosine and still surface lexically; the vector index stamps its marker
  from the active descriptor and reads stale across embedder switches. One
  optional local semantic adapter (`[semantic]` extra, model2vec) loads an
  explicitly provisioned, pinned local artifact — never downloads.
  Committed evidence (`bench/retrieval/semantic-local-m2v.json`,
  potion-base-8M): the #77 66.7pp semantic gap closed at zero lexical cost
  (Recall@5 0.333 → 1.000, lexical parity 1.000), MRR@5 0.537 → 0.882,
  nDCG@5 0.586 → 0.925, clone equivalence 1.000 under the semantic space.
- **Lossless annotation envelope (#79).** Reserved `metadata.annotations`
  envelope (schema 1): namespaced items with pinned ids, confidence bounds,
  local evidence references (resolved against package + target, forward
  references supported, dangling = actionable errors) and external URIs
  (preserved verbatim, never fetched). Unknown namespaces/keys preserved
  losslessly; malformed known structure fails with actionable errors
  (400 on `/v1/add`, counted-invalid records on interchange paths). Limits:
  128 KiB, 1000 items, depth 8. Deterministic merge on every
  content-hash-duplicate path — the case that silently dropped
  annotation-only changes: disjoint items combine, same-id/same-content
  replays are no-ops, same-id/different-content is an explicit conflict
  retaining both versions; never last-write-wins. Annotation-only delta
  changes ride the existing CAS. Contract: `docs/okf/annotations-v1.md`;
  reference fixture: `bench/annotations/mapping-fixture.json`.
- **Gated reranking (#80).** `Reranker` protocol + deterministic bounded
  MMR (evidence-driven lambda 0.5, pool 50) behind
  `--reranker`/`--reranker-lambda`/`--reranker-pool` (default off — the
  default ranking is byte-identical to the single-stage path). Invalid or
  failing reranker output falls back to the first-stage order; selection
  never enters canonical identity. The gate evidence
  (`bench/retrieval/post-p2-gate-80.md`) reads: diversity duplicate-slot
  rate 0.300 hash / 0.400 semantic, both above the 20% gate. Measured
  outcome (`bench/retrieval/rerank-mmr.json`): 0.400 → 0.167, clone
  equivalence 1.0 under the reranker, bounded overhead (~13ms p50 at
  50-pool/256-dim), and the documented recall allowance (overall Recall@5
  1.000 → 0.885; lexical parity unchanged).
- Compatibility matrix (#78/#79/#80): every transfer format × embedder
  scenario, canonical-vs-derived independence, and idempotent replay —
  including a fix for delta records whose embeddings were serialized as
  python reprs, forcing rebuilds on every same-space apply.

### Changed — reconciled #80 duplicate-slot gate denominator (#77/#80)
- The #77 guide read the 48-query aggregate duplicate-slot rate (7.5%) while
  the `near_duplicate_diversity` category — where the near-duplicates
  actually live — reports 30.0%. Reconciled: #80's entry gate reads the
  `near_duplicate_diversity` category (the category that measures duplicate
  occupancy), with the aggregate reported for context; when the category is
  absent the aggregate governs. The semantic-first recommendation rule is
  unchanged and both denominators are pinned by hand-calculated tests;
  `baseline.json` regenerated with one additive measured field (identical
  metric values). #80 is re-evaluated only after #78 lands, via a committed
  post-#78 report; until then it stays open.

### Added — deterministic retrieval evaluation harness (#77)
- `scripts/retrieval_eval.py` + `bench/retrieval/{corpus,queries,baseline}.json`:
  one offline command measures Recall@1/5, MRR@5, graded nDCG@5, negative-query
  false-positive rate, duplicate-slot rate, per-category metrics, clone
  equivalence (verified package -> clean hydration, per-query drift),
  latency p50/p95, package verify/hydrate throughput, and fresh-process
  cold start — all through the production ingest/search path, never a
  copied ranking formula. 60 synthetic memories / 48 graded queries cover
  all eight required categories with frozen clocks and deterministic bytes.
  Committed baseline from the unchanged stack is CI-guarded; the
  deterministic recommendation rule measured a 66.7pp semantic-vs-lexical
  Recall@5 gap (clone equivalence 1.0, duplicate slots 7.5%) pointing at
  #78. docs/retrieval-quality.md explains interpretation and limits.

### Added — verified incremental sync implementation (#73)
- `hotmem delta produce|apply` and `hotmem.interchange.delta`: verified
  one-way incremental transfer. The producer diffs a verified base package
  against current canonical state into deterministic compare-and-swap
  upserts (byte-stable, plain or gz); the applier verifies everything
  before any write, applies in one transaction with the sync checkpoint
  and a `sync.applied` receipt event, skips already-applied operations
  (idempotent replay), and reports every conflict (base_missing,
  state_divergence, id_reuse, invalid_record) with expected/actual
  fingerprints and recovery instructions — a conflicted or failing apply
  leaves the target byte-identical.
- New `interchange.fingerprint` state fingerprints (versioned, documented
  exclusions) detect every sync-relevant mutation including promotion
  transitions — which now round-trip through clone packages, deltas, and
  v2 snapshots (previously a clone silently resurrected archived memories
  as HOT).
- Event-log correctness fixes: `replay(after_seq=...)` honors its cursor
  (was silently restarting at 0); `replay_into` batches inserts and
  reports actually-applied counts; `memory.created` payloads carry the
  full canonical column set. The tested boundary stands: non-server
  ingestion emits no per-record events, so v1 sync uses verified state
  comparison, not event replay.
- `bench/sync/`: delta apply of a 1% change set costs ~10-30ms at ~0.06MB
  peak vs 0.22-1.07s at 5.7-7.5MB for the re-clone fallback (~20x faster,
  gap widens with store size); idempotent replay is nearly free; zero
  embedding calls across every scenario.
- End-to-end gate: wiki -> JSONL -> instance -> base package -> mutations
  -> delta -> receiver convergence (state + retrieval parity) -> repeat
  apply loads zero -> diverged receiver recovers via whole-brain clone.

### Added — verified incremental sync contract (#73)
- Added `docs/okf/delta-v1.md` — the normative `hotmem-delta-v1` contract:
  verified one-way incremental transfer built on the interchange clone
  format. Compare-and-swap upsert operations with per-record state
  fingerprints (versioned exclusions per ADR-003), explicit conflict
  taxonomy, no inferred deletion or tombstones in v1, atomic
  checkpoint/receipt commits, and whole-brain clone-and-restore as the
  recovery path.

### Added — deterministic OKF wiki importer (#68)
- `hotmem import --from okf <bundle-dir> [--out review.jsonl]` converts a
  Google Open Knowledge Format v0.2 bundle (markdown pages with YAML
  frontmatter) into deterministic, reviewable HotMem interchange JSONL —
  validated against the authoritative specification
  (GoogleCloudPlatform/open-knowledge-format) and a vendored slice of the
  public `acme_retail` fixture bundle (Apache-2.0) plus synthetic
  edge fixtures.
- Every page becomes one record: identifier = concept path, fact_text =
  body, fact_summary = description/title, SHA-256 source hash, namespace,
  tags, trust tier (§5.3), status/staleness, temporal provenance
  (generated/verified/sources/usage_window, with the v0.1 `timestamp`
  fallback), and normalized link relationships. Same bundle ⇒ byte-identical
  JSONL. Raw sources stay distinct from compiled knowledge (`references/`
  marked, never fetched). Safety envelope: 16 MiB per-file cap, PyYAML
  safe_load only (new `okf` extra), symlink/root confinement, zero network,
  malformed pages warn + skip.

### Added — verified clone packaging and restore (#69)
- `hotmem snapshot --file <dir> --package [--gz]` writes a
  `hotmem-interchange-v1` package: versioned manifest (record_count,
  logical_id, file + decompressed digests, source identity, embedding
  compatibility block) plus a canonical JSONL or byte-stable gzip payload
  (mtime=0). Publish is atomic (staging dir + rename); equivalent contents
  always share the same logical identity regardless of compression, order,
  or export time.
- `hotmem hydrate --file <dir>` dispatches packages to a verify-then-hydrate
  restore: verification (files, sizes, digests, record counts, schema,
  manifest path confinement) completes before any write; the restore runs in
  one transaction — corrupted or truncated packages leave the target
  byte-identical. Compatible embeddings are reused (zero re-embeds),
  incompatible ones re-embed from text, and records without usable text are
  reported as `invalid` and never stored. Repeat restores load zero records.
- `hotmem verify <dir>` verifies packages and Snapshot v2 directories with
  structured diagnostics; `/v1/hydrate` maps verification failures to 409
  (`package_verification_failed` + reason/file/expected/actual); `/v1`
  `/v1/snapshot` gains `package`/`gz` booleans; TS client
  `snapshot(file, {package, gz})`.
- Reproducible end-to-end example: `examples/company-brain-restore/`
  (README + `restore.sh`) — wiki → JSONL → source instance → package → clean
  instance → idempotent repeat. `docs/agent-memory-portability.md` documents
  the clone workflow.

### Changed — one shared record pipeline across all readers (#67)
- New `hotmem.interchange` package (canonical serialization, digests,
  record normalization/validation, embedding compatibility) is the single
  path behind legacy JSONL/GZ, Snapshot v2, SQLite hydration, bundles, and
  packages. Field preservation is unified: namespace, tier, tags,
  fact_summary, provenance, TTL, file-backed references, and unknown
  top-level keys (preserved under `metadata._interchange_unknown`) now
  survive every round-trip; Snapshot v2 embeddings are compatibility-checked
  before reuse instead of blindly decoded.
- Hydration results report `loaded` / `skipped_dupes` / `invalid` across
  CLI, API, MCP, and the event log (additive — existing callers unaffected).
- Structural performance: exports stream (`iter_rows`, hash-while-write)
  and dedup is database-backed (chunked batch lookups) — no per-record
  commits, no full destination hash-set loads, bounded batches everywhere.
  Benchmarks vs main at 10k/50k/100k records (`bench/interchange/`):
  memory peaks fall from 226–273 MB to a flat 2–4 MB at 100k (~60–70x)
  with comparable throughput; stored-embedding restores make zero embed
  calls. Some restore paths trade throughput for the new contract
  semantics (details + tables in `bench/interchange/README.md`).

### Fixed — Snapshot v2 documentation accuracy (#67)
- `docs/snapshot-v2.md` now matches the implementation: manifest key is
  `file_backed_references` (with the historical `file_references` read-alias
  noted), the manifest example includes the informational `created_at` /
  `hotmem_version` fields, record examples include `ttl_seconds` / `namespace`
  / `tier` / `tags` (which now round-trip), the determinism section no longer
  overclaims byte-identical `manifest.json` (wall-clock informational fields
  are excluded from checksums and identity), the legacy section states the
  actual legacy embedding field (`embedding_b64`, vs v2's `embedding`), and
  the hydration section documents the shared embedding-compatibility rule,
  loaded/skipped_dupes/invalid semantics, path confinement, and batched
  database-backed dedup.

### Added — company-brain interchange contract and planning docs (#67)
- Restored the issue-linked OKF planning notes under `docs/okf/`
  (`company-brain-interchange.md`, `file-native-memory-practices.md`,
  `format-and-maintenance.md`, `index.md`) from git history; they are excluded
  from the generated docs site via mkdocs `exclude_docs` so the public surface
  added by #82 is unchanged.
- Added `docs/okf/interchange-v1.md` — the normative `hotmem-interchange-v1`
  contract: required/optional/forward-compatible record fields, canonical
  JSON serialization, logical vs file digests, gzip transport rules, embedding
  compatibility, and loaded/skipped/invalid hydration semantics. Extension
  manifest fields are documented as Proposed and are not emitted (ADR-003).

### Changed — JSONL inspection validation policy (#89)
- Inspection is **advisory** and now declares its assurance level:
  `FileInspection.metadata["validation"]` is `sampled` (default — only the
  declared sample window is parsed) or `full`. A malformed line beyond the
  sampled window is reported only under `validation="full"`; `row_count`
  semantics are unchanged. Full validation remains available via
  `inspect_file(..., validation="full")` and `hotmem inspect
  --full-validation`. Measured: 34 ms vs 115 ms per 11.6 MB file
  (~5.5x at 100 MB per the spike baseline). Authoritative verification of
  canonical content is unaffected — provenance checksums, not inspection.

### Changed — streaming verification for large ranges (#88)
- `provenance.verify_range` hashes ranges above `STREAM_VERIFY_THRESHOLD`
  (8 MiB) while streaming through the new optional adapter capability
  `LocalFilesystemAdapter.read_range_chunked` — O(chunk) memory instead of
  O(range), identical digest and `ProvenanceError` semantics. Adapters
  without the capability and ranges at/below the threshold keep the simple
  single-read path.

### Changed — single-read verified hydration (#87)
- Verified hydration now hashes the bytes it already read instead of
  re-reading the range through `provenance.verify_range` — one read per
  range (spike B1: ~+16% at 100 MB). Digest and `ProvenanceError`
  semantics are unchanged; new internal `provenance.verify_bytes` helper
  carries the identical checksum contract.

### Changed — embed_text trigram hashing (#90)
- Trigram hashing now runs through a bounded per-gram cache
  (`lru_cache`, 65 536 entries) replacing per-call md5 + hex parsing;
  vectors are **bit-identical** to the previous `hotmem-hash-v1` output
  (golden equivalence test over ASCII/unicode/random corpora). Measured
  ~5.9x faster per embedding on realistic text — directly attacks the
  37–78% of `parse_bundle` time the spike attributed to `embed_text`.

### Changed — derived vector index polish (#92)
- `search_by_ids` binds candidate ids in chunks of 900, so any configured
  `oversample` stays under SQLite's legacy 999-variable cap; chunk results
  are merged into the identical canonical order.
- The accelerated search path runs a single FTS pass — candidate unioning
  and BM25 normalization share one query (also true of the fallback path).
- Swallowed Chroma delete/clear failures are now logged at warn level; the
  derived index reuses `embed.unpack_embedding` for the blob format.

### Performance follow-ups from the native helper spike (#87–#92)

Work in progress — see PR for the unified acceptance criteria covering:
single-read verified hydration (#87), streaming range hash (#88), JSONL
inspection validation policy (#89), `embed_text` trigram batching (#90), and
derived-vector-index polish (#92).

## [0.2.4] - 2026-08-28

### Added — Optional derived vector index (#49)
- Pluggable vector index for search acceleration: `VectorIndex` protocol with
  a no-op `NullVectorIndex` (default — HotMem runs with zero vector
  dependencies) and an optional `ChromaVectorIndex` backend
  (`uv pip install 'hotmem[vector]'`, lazily imported; degrades to the null
  backend with a warning when chromadb is not installed).
- The index is disposable and rebuildable: SQLite, files, bundles, and
  manifests remain canonical storage. Index loss never loses memory —
  search falls back to the deterministic SQLite cosine+FTS scan whenever
  the index is absent or stale.
- Ranking contract preserved: the index only supplies oversampled candidate
  ids which are re-scored in SQLite with the identical hybrid formula
  (cosine + FTS + importance), so `/v1/search` responses are identical with
  or without acceleration (stores larger than the oversample window are
  approximate by design; the fallback remains the correctness floor).
- Rebuild reads only SQLite rows (embeddings reused, never recomputed) —
  backing files are never read; file-backed memories are indexed from
  eligible inline summaries/metadata only, preserving lazy content reads.
- Rebuild marker records the store fingerprint (count, max rowid, max event
  seq) for cheap staleness detection; staleness, missing dependencies, and
  rebuild state are observable via `GET /v1/vector-index/status`.
- New admin endpoints: `POST /v1/vector-index/rebuild` (emits an
  `index.rebuilt` event), `GET /v1/vector-index/status`, and
  `DELETE /v1/vector-index` (clear). `/v1/search` response shape unchanged.
- `hotmem serve --vector-index {none,chroma}` CLI flag (default `none`).

### Fixed — JSONL inspector line offsets (#86)
- `JSONLInspector._stream` computed line offsets in (carry+chunk) coordinates,
  overstating `unsupported_reason` offsets and `byte_ranges` by the carried
  byte count whenever an earlier line spanned the 1 MiB read-chunk boundary.
  Offsets are now exact file coordinates (regression test included; discovered
  by the native helper spike, #48/#84).

## [0.2.3] - 2026-08-07

### Changed
- Documentation canonical URLs now use the branded `docs.knowguardai.com`
  domain.
- Package and runtime version metadata are aligned for automated PyPI release.

## [0.2.2] - 2026-07-16

### Added — Bundle store (#52, #50)
- Loose local markdown bundle reader: a bundle is a directory containing
  `memory.md` (or `index.md`/`README.md`), optional `metadata.json`/
  `metadata.yaml`, `facts.json`, `events.jsonl`, and an `attachments/`
  directory. Files in `attachments/` become file-backed memory records
  (`memory_type="file"`) referenced by relative URI, not copied into
  SQLite. Symlinks escaping the bundle directory are rejected.
- Bundle discovery: `discover_bundles(root, max_depth=10)` walks a
  directory tree (no symlink following) and returns `BundleIndexEntry`
  records with path, primary file, metadata summary, attachment refs,
  checksum, modified time, size hint, and warnings.
- Bundle indexing: `index_bundles(db, root)` discovers and upserts into
  a `bundle_index` SQLite table in a single transaction. Resource caps:
  10 000 files per bundle, 1 GB per bundle, 16 MB per file.

### Added — Hydration profiles (#40)
- `HydrationProfile` literal: `agent` | `compact` | `audit` | `full`.
- `hydrate_with_profile(db, memory_id, *, profile, base_dir, verify)`
  returns a `ProfiledHydration` result with content, verified flag,
  provenance, warnings, and existence check.
- `compact`: metadata only, no file I/O (stat only for `exists`).
- `agent`: `fact_summary` or truncated `fact_text` (max 4096 chars), no
  file I/O — designed for LLM context windows.
- `audit`: full content (inline or file-backed bytes, lazy read), full
  provenance (`provenance_json`, checksums, warnings) — for compliance.
- `full`: everything `audit` has plus full `fact_text` for inline memories.
- `compact`/`agent` never call `adapter.read_range()`; `audit`/`full`
  may read file-backed content on demand.

### Added — API extensions (#43)
- `GET /v1/files` — list file-backed memory references.
- `GET /v1/bundles` — list bundle index entries.
- `POST /v1/discover` — trigger bundle discovery under a root directory.
- `POST /v1/memory/hydrate-batch` — batch hydrate with a profile.

### Added — Append-only event log (#41)
- `events` table with `seq` (AUTOINCREMENT PK), `event_id` (UUID4 hex),
  `memory_id` (nullable FK), `namespace`, `event_type`, `occurred_at`
  (ISO-8601 UTC), `payload_json`. Indexes on `(memory_id, seq)`,
  `(namespace, seq)`, `(event_type, seq)`, `(occurred_at, seq)`.
- `BEFORE UPDATE` and `BEFORE DELETE` triggers `RAISE(ABORT)` to enforce
  append-only semantics at the database layer.
- Built-in event types: `memory.created` (full row snapshot payload),
  `snapshot.imported`, `bundle.imported`, `bundle.discovered`,
  `hygiene.checked`, `hygiene.warning`.
- `replay(db, *, after_seq=0)` streams events in seq order (500 per
  batch) for state reconstruction.
- `replay_into(db, target_db)` reconstructs full memory state from
  `memory.created` events into a target DB via `insert_many_ignore`.
- `GET /v1/events` query endpoint with cursor pagination, filtering by
  `memory_id`, `namespace`, `event_type`, time range, and `after_seq`/
  `before_seq` cursors. `limit` 1–1000, default 100. Returns
  `next_seq` for cursor pagination.
- Atomic emission: events appended in the same transaction as the
  mutation they record.
- Retention: `trim_events(db, keep_last)` prunes old events while
  preserving the monotonic seq sequence.

### Added — Hygiene warnings (#51)
- Advisory hygiene checks surface as `hygiene.checked` and
  `hygiene.warning` events in the event log, queryable via `/v1/events`
  and `/v1/hygiene`.

### Changed
- `memory.created` event payload captures the full 30-field row
  snapshot (including `provenance_json`, `source_checksum`,
  `snapshot_id`, `promotion_state`, `parent_memory`,
  `related_memories`, `tags`, `fact_summary`) for deterministic replay.
- All existing public APIs (`hotmem.db`, `hotmem.mount`, `hotmem.search`,
  `hotmem.swap`, `hotmem.embed`) remain unchanged — the new modules are
  purely additive.

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
  boundary with HTTP 400 `unsupported_scheme`.
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
