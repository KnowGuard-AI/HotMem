# OKF: File-Aware Architecture

Status: Accepted
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: Architecture context for file-native HotMem

Tracks: [#35](https://github.com/KnowGuard-AI/HotMem/issues/35)
Related: [#36](https://github.com/KnowGuard-AI/HotMem/issues/36),
[#37](https://github.com/KnowGuard-AI/HotMem/issues/37),
[#38](https://github.com/KnowGuard-AI/HotMem/issues/38),
[#39](https://github.com/KnowGuard-AI/HotMem/issues/39),
[#40](https://github.com/KnowGuard-AI/HotMem/issues/40),
[#41](https://github.com/KnowGuard-AI/HotMem/issues/41),
[#42](https://github.com/KnowGuard-AI/HotMem/issues/42),
[#43](https://github.com/KnowGuard-AI/HotMem/issues/43)

## 1. Purpose

This note preserves the architecture context behind HotMem's file-native vNext
work. GitHub issues are the active implementation tracker; this document owns
the "why", boundaries, compatibility principles, and high-level sequence.

Do not use this note as a second issue tracker. If scope, acceptance criteria,
or implementation status changes, update the GitHub issue first.

## 2. Current Decision

HotMem is evolving from a JSON memory store into a file-aware, provenance-first
memory sidecar while preserving its original identity:

- local-first
- extremely lightweight
- deterministic
- embeddable
- language agnostic
- no heavy analytical engine
- optimized for fast memory ingestion, retrieval, and hydration

HotMem remains the runtime memory sidecar for local agents. It can reference
large files, but it should not become a vector database, data lake, analytical
engine, or object-storage orchestrator.

Positioning: HotMem should become the filesystem-native memory sidecar for
agent memory, similar in adoption spirit to `mem0`, but oriented around local
files, bundles, manifests, provenance, and fast hydration instead of a
canonical vector database.

## 3. GitHub Issue Map

Closed foundation issues:

| Issue | Decision |
| --- | --- |
| [#35](https://github.com/KnowGuard-AI/HotMem/issues/35) | File-aware sidecar architecture and roadmap |
| [#36](https://github.com/KnowGuard-AI/HotMem/issues/36) | Extended memory record fields, provenance, and migration |
| [#37](https://github.com/KnowGuard-AI/HotMem/issues/37) | Storage adapter abstraction and local filesystem implementation |

Open implementation issues:

| Issue | Work |
| --- | --- |
| [#38](https://github.com/KnowGuard-AI/HotMem/issues/38) | File-backed memories with URI, range, and checksum hydration |
| [#39](https://github.com/KnowGuard-AI/HotMem/issues/39) | Snapshot directory format |
| [#40](https://github.com/KnowGuard-AI/HotMem/issues/40) | Hydration profiles |
| [#41](https://github.com/KnowGuard-AI/HotMem/issues/41) | Append-only event log |
| [#42](https://github.com/KnowGuard-AI/HotMem/issues/42) | Promotion lifecycle |
| [#43](https://github.com/KnowGuard-AI/HotMem/issues/43) | API extensions |

The issue bodies own detailed scope, acceptance criteria, dependencies, and
testing requirements.

## 4. HotMem Owns vs EMOS Owns

HotMem owns:

- local hot memory records
- the memory API and client compatibility
- SQLite schema and migrations
- snapshots, hydration, and provenance
- local file references and local range reads
- storage adapter interface
- event log and promotion signals
- optional local retrieval acceleration

EMOS owns:

- durable memory hierarchy and tier movement
- distributed object storage
- HDFS/S3/ABFS/GS orchestration
- DuckDB, Polars, Arrow, and analytical execution
- Parquet partition management
- compaction beyond local hygiene hints
- lineage reconstruction beyond local provenance
- cross-instance replication

HotMem may point to larger systems. It should not become them.

## 5. Compatibility Principles

All file-native work must be additive.

- Existing `/v1/add` payloads keep working.
- Existing `/v1/search` default response shape keeps working.
- `identifier`, `fact`, and `fact_text` compatibility is preserved.
- Legacy `.jsonl` and `.jsonl.gz` hydrate remains supported.
- JSONL snapshot/export remains available.
- New file/provenance fields are optional.
- Vector indexes are optional, disposable, and rebuildable.
- Unsupported schemes and formats return clear errors.

The repo should evolve by accepting more useful local memory shapes, not by
invalidating old ones.

## 6. Architecture Shape

Canonical storage remains:

- SQLite memory records
- local files and file references
- bundle manifests where present
- snapshots and checksums

Derived or optional acceleration may include:

- FTS/search indexes
- optional vector index
- lightweight file metadata caches

Derived indexes are never canonical. If an index disagrees with SQLite and
referenced files, SQLite and files win.

## 7. Performance Posture

HotMem should speak filesystem first and stay small, but it can use specialized
native helpers where they preserve that shape.

Allowed performance paths:

- optimized SQLite hydration
- fast local range reads
- markdown and bundle indexing
- checksum acceleration
- optional vector index rebuilds
- future Rust, C, or WebAssembly helpers for hot local primitives
- future helper modules that expose basic Arrow-like metadata or scan
  primitives without becoming a query engine

Boundary constraints:

- Native helpers must be optional or gracefully degradable.
- The Python/FastAPI/SQLite path remains easy to run.
- Spark UDFs, DuckDB, Polars, Arrow, and HDFS-like systems may be integration
  targets or helper backends, not the core HotMem contract.
- Large-file support means provenance, metadata, range hydration, and optional
  indexing, not distributed analytics.

The performance goal is hyper-fast local memory operations without turning
HotMem into a compute platform.

## 8. Format Versioning

Versioning is useful for portable artifacts and audit behavior, but should not
create a migration cliff.

- Memory records can carry schema fields additively.
- Snapshot manifests should carry `schema_version`.
- Public API defaults should remain stable.
- Directory snapshots should live beside JSONL compatibility.

Use "extended memory record" or "file-aware memory record" in user-facing docs
unless a precise schema version is required.

## 9. Related OKF Notes

- [File-Native Memory Practices](file-native-memory-practices.md) owns storage
  thresholds, bundle strictness, and simple local hygiene heuristics.
- [Format and Maintenance](format-and-maintenance.md) owns the documentation
  format and GitHub issue relationship.

## 10. Open Questions

- Should directory snapshots become the default only when the target path is a
  directory?
- Should optional vector indexing live in core extras or in a separate adapter
  package?
- Which architecture decisions should move into public API docs after
  implementation?
- Which native helper surface, if any, should land first: Rust range scanner,
  C checksum helper, or WebAssembly bundle/parser primitive?
