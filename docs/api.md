# API Reference

All endpoints are under `/v1`. Default: `http://127.0.0.1:8711`

Interactive Swagger UI is available at `http://127.0.0.1:8711/docs` when the server is running.

## Health

```http
GET /v1/health
```

Returns server status, memory count, DB path, and uptime.

## Add memory

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

## Search

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

## Hydrate

```http
POST /v1/hydrate
Content-Type: application/json

{
  "file": "swap.jsonl"
}
```

Loads memories from a JSONL or JSONL.GZ swap file. Deduplicates by `content_hash`.

## Snapshot

```http
POST /v1/snapshot
Content-Type: application/json

{
  "file": "swap.jsonl"
}
```

Exports all memories to a JSONL or JSONL.GZ swap file.

## OpenAPI spec

Export the machine-readable spec:

```bash
hotmem openapi --output openapi.json
hotmem openapi --output openapi.yaml --format yaml
```

Or fetch it from a running server: `GET /openapi.json`
