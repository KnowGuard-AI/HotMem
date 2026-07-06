# OKF: File-Native Memory Practices

Status: Draft
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: HotMem vNext planning and implementation guidance

## 1. Purpose

This note captures the current working decisions for evolving HotMem into a
file-native memory sidecar without losing its lightweight local-first identity.
It is intentionally a living OKF-style knowledge artifact: clear enough to
guide implementation now, but expected to evolve as the repository and product
language mature.

The main rule is compatibility first:

- Existing APIs continue to work.
- Existing JSONL hydrate/snapshot continues to work.
- Existing `/v1/search` message objects keep their default shape.
- `identifier` and `fact` remain valid request fields.
- Chroma or any vector index remains optional and rebuildable.
- New formats are additive and discoverable, not migration cliffs.

HotMem should evolve by accepting more useful local memory shapes, not by
invalidating old ones.

## 2. Current Direction

HotMem should become a file-native memory sidecar with optional vector
acceleration.

The target balance is:

- 60-80% filesystem, files, bundles, manifests, provenance, large-file pointers.
- 20-40% optional vector acceleration.

The vector index is never canonical storage. SQLite records, local files,
bundle manifests, and snapshots remain the source of truth.

Working product language: HotMem is the filesystem-native memory sidecar for
agents. It should choose the best local memory shape for the job: inline SQLite
records for small hot facts, markdown bundles for inspectable context, file
pointers for large content, and optional indexes for speed.

## 3. Current Threshold Decisions

These are the current working thresholds for implementation planning. They are
heuristics, not hard limits.

| Question | Current answer |
| --- | --- |
| When does a memory stay inline in SQLite? | Up to about 8 KB of prompt-ready text. |
| When does markdown bundle become preferred? | Around 8 KB to 128 KB, or sooner when the memory is human-authored, multi-file, or attachment-heavy. |
| When does HotMem stop copying content and use file pointers? | Above about 128 KB, or whenever duplication would hide provenance or inflate SQLite. |
| When does CSV/JSONL stay as a file? | When row/range access, streaming, or repeated inspection matters more than copying into memory rows. |
| When does Parquet/Arrow stay outside HotMem? | Always for analytical data. HotMem records URI, checksum, format, metadata, and optional summary only. |
| When does optional vector indexing become relevant? | When search latency or record count warrants acceleration; the index remains rebuildable. |

In short:

- SQLite is for small, hot, prompt-ready facts.
- Markdown bundles are for inspectable local knowledge and medium-sized context.
- File pointers are for large or provenance-sensitive content.
- Parquet-like files remain referenced analytical artifacts, not HotMem-owned
  tables.

## 4. Why Version Anything?

Versioning is useful for portable artifacts and audit behavior. It should not
be presented as a hard product rewrite.

Memory records already have additive file/provenance fields in the database:
`source_uri`, `source_format`, `source_checksum`, `byte_offset`,
`byte_length`, `schema_version`, and related lifecycle fields. These fields
make file-backed memory possible, but they do not require callers to adopt a new
API shape.

Recommended language:

- Prefer "extended memory record" or "file-aware memory record" in user-facing
  docs.
- Use "schema version" in manifests and export payloads where replay and
  validation matter.
- Avoid implying that current memory records are obsolete.

Snapshots benefit more clearly from versioned manifests because a directory
snapshot can contain checksums, file references, attachments, and metadata that
a flat JSONL file cannot represent cleanly.

Compatibility rules:

- Legacy `.jsonl` and `.jsonl.gz` remain readable.
- JSONL export remains available.
- Directory snapshots use a manifest with `schema_version`.
- Path or explicit format selection chooses the format.
- No default behavior changes without compatibility tests and a staged release
  note.

## 5. Bundle Strictness

Bundle support should start loose and become stricter through real examples.

Initial bundle reader:

- Accepts a minimal `memory.md`.
- Accepts optional `metadata.yaml`, `metadata.json`, `facts.json`,
  `events.jsonl`, `attachments/`, and `manifest.json`.
- Ignores unknown files unless strict mode is requested.
- Treats attachments as referenced local files by default.
- Emits warnings for ambiguous or partially invalid structure.
- Does not require a manifest for simple local authoring.

Later bundle validation:

- Add a documented draft bundle manifest.
- Add `schema_version` once the shape stabilizes.
- Add `--strict` validation for CI, publishing, or archival workflows.
- Keep permissive local reads for everyday agent memory.

The bundle rule is progressive strictness: permissive for capture, stricter for
portability and audit.

## 6. Storage Shape Heuristics

HotMem should use simple size and structure heuristics instead of a full memory
hierarchy or escalation protocol.

These thresholds are starting points, not hard product limits.

Terminology note: when planning says "table" or "NoSQL-style table" here, the
HotMem implementation should still mean its simple local SQLite memory table
unless a future ticket explicitly introduces another local record store. The
choice is about record shape and file references, not adopting a separate
database product.

| Memory shape | Suggested storage | Heuristic |
| --- | --- | --- |
| Small fact or note | SQLite inline record | Up to about 8 KB of text |
| Medium text memory | SQLite inline record plus optional markdown bundle | About 8 KB to 128 KB, especially if human-authored |
| Human-readable multi-file context | Local markdown bundle | Multiple related files, attachments, or recurring project context |
| Large text or binary file | File pointer with byte range and checksum | Larger than about 128 KB, or expensive to duplicate |
| Structured CSV/JSONL | File pointer plus lightweight inspector | Many rows, streaming-friendly, or useful by row/range |
| Parquet/Arrow-like data | File pointer plus metadata only | Columnar/analytical data; HotMem does not query it |
| Hot retrieval accelerator | Optional vector index | Rebuildable from SQLite/files, never canonical |

Practical guidance:

- Inline records are best for fast small facts.
- Markdown bundles are best for inspectable, editable local knowledge.
- File pointers are best when copying bytes would hide provenance or inflate the
  DB.
- Parquet stays a referenced file with metadata; analytical execution belongs to
  EMOS or a future helper outside the HotMem core path.

## 7. Growing Database Heuristics

HotMem can make good local decisions based on DB growth without owning a full
hierarchy.

Suggested warning thresholds:

| Signal | Practice |
| --- | --- |
| More than 10,000 records | Recommend snapshot/export hygiene in status output |
| More than 100 MB SQLite DB | Recommend moving large repeated content to file pointers or bundles |
| More than 500 MB SQLite DB | Warn that HotMem is being used as bulk storage |
| Single memory over 128 KB | Prefer file pointer or bundle reference |
| Repeated attachment content | Store once as file reference, link many memories |
| Search latency regression | Offer optional derived index rebuild, not mandatory vector DB |

These are health hints. They should not block writes by default.

## 8. NoSQL Table, Markdown Bundle, or Parquet Pointer?

Use this decision path:

1. If the memory is a small fact needed in prompts, store it inline in SQLite.
2. If the memory is human-authored context that should be reviewed or edited,
   store it as or alongside a markdown bundle.
3. If the memory references a large local file, store only URI, byte range,
   checksum, format, and optional summary.
4. If the file is CSV or JSONL, HotMem may inspect headers, rows, counts, or
   selected ranges.
5. If the file is Parquet/Arrow or another analytical format, HotMem records
   metadata and provenance only.
6. If retrieval gets slow, add or rebuild an optional index.

This keeps HotMem local and useful while avoiding a data-engine shape.

## 9. Fast Path Practices

Hydration and retrieval should be fast by default, with native acceleration
available where it keeps HotMem simple.

Preferred fast paths:

- SQLite remains the fast hot-memory store.
- JSONL hydrate should stay optimized and streaming-friendly.
- Markdown bundles may be indexed locally for quick discovery.
- Large local files should use range reads and metadata inspection.
- Parquet/Arrow-like files should be referenced with metadata and optional
  lightweight inspection, not fully queried by HotMem.
- Optional vector indexes can accelerate retrieval but must be rebuildable.

Future native helper guidance:

- Rust, C, or WebAssembly helpers are acceptable for tight local primitives such
  as scanning, checksums, parsing, or range slicing.
- Spark UDFs, Polars, DuckDB, Arrow, and HDFS-like paths are integration or
  helper surfaces, not a reason to make HotMem a distributed compute engine.
- Native helpers must not become required for the basic local-first path.

## 10. Compatibility Acceptance Criteria

Every file-native implementation ticket should include these checks:

- Existing `/v1/add` payload still works.
- Existing `/v1/search` default response still works.
- Existing Python and TypeScript client methods still work.
- Existing MCP tools still work.
- Existing JSONL/GZ hydrate still works.
- Existing JSONL/GZ snapshot remains available.
- New payload fields are optional.
- Unsupported schemes and formats produce clear errors.
- Optional acceleration can be disabled or rebuilt.

## 11. HotMem Owns vs EMOS Owns

HotMem owns:

- Local hot memory records.
- SQLite schema and migrations.
- Local file references.
- Range reads for local files.
- Provenance capture.
- Bundle reading.
- Snapshot and hydrate portability.
- Optional local retrieval acceleration.
- Health hints based on local DB growth.

EMOS owns:

- Memory hierarchy and durable tier movement.
- Distributed object storage.
- HDFS/S3/ABFS/GS orchestration.
- Analytical execution.
- DuckDB/Polars/Arrow query planning.
- Data lake layout and partitioning.
- Cross-instance replication.

HotMem may point to larger systems. It should not become them.

## 12. Open Questions

- Should directory snapshots become the default only when the target path is a
  directory, leaving file paths as JSONL forever?
- Should bundle manifests be optional forever for local-only bundles?
- What exact DB-size threshold should trigger status warnings in practice?
- Should checksums be whole-file only at first, or include range-level checksums?
- Should optional vector indexing live in core behind extras, or in a separate
  adapter package?
- Which native helper is most useful first: Rust scanner, C checksum helper, or
  WebAssembly parser?

## 13. Recommended Initial Order

1. Compatibility hardening and golden tests.
2. File pointer hydration.
3. Directory snapshot format beside JSONL.
4. Loose local bundle reader.
5. Hydration profiles.
6. Optional vector index.
7. Lightweight file inspectors.

This order keeps the smallest, most durable concepts first.
