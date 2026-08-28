# API Reference

## 1. Purpose

This document records the stable HotMem HTTP API. All endpoints are under
`/v1`. Default: `http://127.0.0.1:8711`.

Interactive Swagger UI is available at `http://127.0.0.1:8711/docs` when the server is running.

## 2. Compatibility Rules

- Existing request fields remain valid.
- Default response shapes remain stable.
- New file-native fields are optional.
- Unsupported future formats should fail with explicit errors.

## 3. Health

```http
GET /v1/health
```

Returns server status, memory count, DB path, and uptime.

## 4. Add Memory

```http
POST /v1/add
Content-Type: application/json

{
  "identifier": "user",
  "fact": "prefers dark mode",
  "source": "chat",
  "importance": 0.5,
  "metadata": {},
  "ttl_seconds": null
}
```

Returns `memory_id`, `content_hash`, and `trace_ms`.

## 5. Search

```http
POST /v1/search
Content-Type: application/json

{
  "query": "what theme does the user like",
  "top_k": 5,
  "max_chars": null
}
```

Returns ranked `memories` (LLM-ready message objects) with `count` and `trace_ms`.

## 6. Hydrate

```http
POST /v1/hydrate
Content-Type: application/json

{
  "file": "swap.jsonl"
}
```

Loads memories from a JSONL or JSONL.GZ swap file. Deduplicates by `content_hash`.

## 7. Snapshot

```http
POST /v1/snapshot
Content-Type: application/json

{
  "file": "swap.jsonl"
}
```

Exports all memories to a JSONL or JSONL.GZ swap file.

## 8. Vector Index (optional, derived)

The vector index is an **optional acceleration layer** — it is disposable,
rebuildable, and **never canonical storage**. SQLite, files, bundles, and
manifests remain the source of truth. Losing or deleting the index never
loses memory: search transparently falls back to the deterministic SQLite
cosine scan, and the index can be fully rebuilt from canonical storage.

Enable it at server start (`hotmem serve --vector-index chroma`, requires the
optional `hotmem[vector]` extra). The index only supplies candidate ids; final
ranking is always recomputed with the canonical hybrid scorer, so the
`/v1/search` response shape and ranking are identical with or without
acceleration.

```http
POST /v1/vector-index/rebuild
```

Rebuilds the index from SQLite rows (embeddings are reused, never recomputed).
Reads no backing files. Returns `indexed_count`, `db_count`,
`skipped_no_embedding`, `rebuilt_at`, and `trace_ms`. Returns
`400 vector_index_disabled` when no backend is configured, or
`400 vector_dependency_missing` when the backend package is not installed.
Emits an `index.rebuilt` event.

```http
GET /v1/vector-index/status
```

Returns `backend`, `requested_backend`, `dependency_available`,
`indexed_count`, `db_count`, `stale`, `rebuilt_at`, `path`, and `trace_ms`.
`stale: true` means search is currently served by the deterministic fallback.

```http
DELETE /v1/vector-index
```

Clears the index (entries + rebuild marker). Canonical storage is untouched.

## 9. OpenAPI Spec

Export the machine-readable spec:

```bash
hotmem openapi --output openapi.json
hotmem openapi --output openapi.yaml --format yaml
```

Or fetch it from a running server: `GET /openapi.json`
