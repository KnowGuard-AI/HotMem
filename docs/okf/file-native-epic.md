# OKF: File-Native Memory Epic

Status: Draft
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: Epic-level coordination for file-native HotMem work

## 1. Purpose

This note collects the current file-native HotMem epic in one place. It is not
the implementation tracker. GitHub issues own ticket scope, acceptance
criteria, dependencies, and implementation status.

This note exists so contributors can understand the story before choosing a
ticket: HotMem is becoming the filesystem-native memory sidecar for agents,
with SQLite, local files, markdown bundles, manifests, provenance, and optional
indexes working together without turning HotMem into a vector database or data
lake.

## 2. Product Direction

HotMem should feel like the `mem0`-style adoption point for filesystem-based
agent memory systems:

- small enough to run locally without ceremony
- fast enough for hot hydration into SQLite
- inspectable through files, markdown, manifests, and JSONL
- capable of referencing large local files without copying them
- ready for optional vector acceleration without making vector storage
  canonical
- open to native helpers where local primitives need more speed

The core promise is simple: HotMem speaks filesystem first.

## 3. Current Issue Set

Closed foundation:

| Issue | Status | Role |
| --- | --- | --- |
| [#35](https://github.com/KnowGuard-AI/HotMem/issues/35) | Closed | Architecture and roadmap |
| [#36](https://github.com/KnowGuard-AI/HotMem/issues/36) | Closed | Extended memory record and migration |
| [#37](https://github.com/KnowGuard-AI/HotMem/issues/37) | Closed | Storage adapter and local filesystem implementation |

Open execution:

| Issue | Status | Role |
| --- | --- | --- |
| [#38](https://github.com/KnowGuard-AI/HotMem/issues/38) | Open | File-backed memories |
| [#39](https://github.com/KnowGuard-AI/HotMem/issues/39) | Open | Snapshot directory format |
| [#40](https://github.com/KnowGuard-AI/HotMem/issues/40) | Open | Hydration profiles |
| [#41](https://github.com/KnowGuard-AI/HotMem/issues/41) | Open | Append-only event log |
| [#42](https://github.com/KnowGuard-AI/HotMem/issues/42) | Open | Promotion lifecycle |
| [#43](https://github.com/KnowGuard-AI/HotMem/issues/43) | Open | API extensions |

Recent planning constraints have been added as comments to each open issue.

## 4. Execution Guardrails

Every ticket in this epic should preserve these constraints:

- No breaking API changes.
- No JSONL compatibility loss.
- No mandatory vector database.
- No data-lake or analytical-engine drift.
- Local filesystem first.
- File references before byte duplication for large content.
- Markdown bundles start permissive and become stricter through examples.
- Native helpers are allowed for hot local primitives, but not required for the
  basic path.

## 5. Storage Decision Heuristics

Use these defaults when implementing ticket behavior:

| Shape | Default handling |
| --- | --- |
| Small prompt-ready memory | SQLite inline record |
| Medium human-readable context | Markdown bundle or inline record plus bundle |
| Large local file | URI, byte range, checksum, format, optional summary |
| CSV/JSONL | File pointer plus lightweight streaming/range inspection |
| Parquet/Arrow-like file | Metadata and provenance only |
| Search acceleration | Optional, rebuildable derived index |

See [File-Native Memory Practices](file-native-memory-practices.md) for the
current thresholds and local hygiene hints.

## 6. Recommended Sequence

1. Keep compatibility tests strong.
2. Implement file pointer hydration.
3. Add directory snapshots beside JSONL.
4. Add loose local bundle reading.
5. Add hydration profiles.
6. Add API extensions around files/export.
7. Add event log and promotion signals.
8. Evaluate optional vector and native helper surfaces.

This sequence keeps the canonical storage model stable before layering
acceleration and lifecycle features on top.

## 7. PR Review Checklist

For changes in this epic, reviewers should ask:

- Does this preserve existing public contracts?
- Does this keep SQLite/files/manifests canonical?
- Does this avoid making Chroma or any vector DB required?
- Does this avoid pulling DuckDB/Polars/Arrow/Spark/HDFS into core HotMem?
- Does this explain unsupported schemes and formats clearly?
- Does this add or preserve tests for legacy JSONL and default API behavior?
- Does this update OKF docs if the decision changed?

## 8. Open Questions

- Which native helper is the best first experiment: Rust scanner, C checksum
  helper, or WebAssembly parser?
- Should optional vector indexing live behind a core extra or separate package?
- When directory snapshots land, should path shape or explicit flags select
  them by default?
