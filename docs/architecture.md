# Architecture Overview

HotMem is a local-first memory sidecar. It keeps the canonical runtime state in
SQLite, exposes a small HTTP/API surface, and supports Python, TypeScript, and
MCP integrations.

## Runtime path

```text
agent or application
  -> HTTP, SDK, or MCP client
  -> HotMem runtime
  -> SQLite records and local file references
  -> search, inspection, snapshot, or hydration
```

The runtime is designed to be inspectable and embeddable. It does not require a
hosted database or a separate control plane for local use.

## Memory records

Small, prompt-ready facts can be stored inline. A file-backed memory can retain
the source URI, byte range, format, checksum, and optional summary without
copying the referenced content into SQLite. File references are hydrated only
when requested and are checked against their recorded provenance when a
checksum is available.

The built-in storage adapter is local filesystem-only. Unsupported remote URI
schemes fail explicitly instead of being silently fetched.

## Search and inspection

HotMem combines deterministic text embeddings, keyword overlap, and importance
to rank local memories. Read-only inspectors provide lightweight metadata for
CSV, JSONL, and Parquet files without turning the runtime into a query engine.

## Portability

JSONL and JSONL.GZ are supported portable record formats. Snapshot v2 adds a
versioned manifest, per-file checksums, an aggregate digest, and optional
attachments or file references. Hydration verifies the package before loading
records and skips equivalent logical memories on repeat imports.

New file and provenance fields are additive: existing `identifier`/`fact`
payloads, search responses, JSONL files, and client integrations remain
supported.
