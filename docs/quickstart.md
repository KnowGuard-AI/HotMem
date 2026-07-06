# OKF: Quickstart

Status: Accepted
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: First-run HotMem setup and basic usage

## 1. Purpose

This quickstart gets a local HotMem sidecar running, stores one memory, searches
it, and shows how to move memory with JSONL snapshots.

## 2. Install

```bash
pip install hotmem
# or
uv pip install hotmem
```

Supports Python 3.11, 3.12, 3.13, and 3.14.

## 3. Start the Server

```bash
# Start with a mount directory (portable memory)
hotmem serve --mount ./hotmem

# Or just start (uses temp DB)
hotmem serve
```

## 4. Add and Search Memories

```bash
# Add a memory
curl -X POST http://127.0.0.1:8711/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"identifier": "project", "fact": "uses FastAPI and SQLite"}'

# Search
curl -X POST http://127.0.0.1:8711/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "what stack does the project use"}'
```

## 5. Portable Memory

```bash
# Export to a swap file
hotmem snapshot --file swap.jsonl --db ./hotmem/hotmem.sqlite

# Hydrate on another machine
hotmem hydrate --file swap.jsonl --db ./my.sqlite
```

JSONL remains a stable compatibility format. Future directory snapshots are
additive and must not remove this path.

## 6. Use the Python Client

```python
from hotmem.client import HotMemClient

client = HotMemClient("http://127.0.0.1:8711")
client.add(identifier="user", fact="likes Rust")
results = client.search(query="programming preferences")
```

## 7. Docker

```bash
docker run -p 8711:8711 -v ./data:/data knowguard/hotmem
```

See [CLI](cli.md) for the full command reference and [API Reference](api.md) for endpoints.

## 8. Compatibility Rules

- `/v1/add` accepts `identifier` and `fact`.
- `/v1/search` returns LLM-ready message objects by default.
- JSONL hydrate/snapshot remains supported.
- File-native features must be additive.

## 9. Open Questions

- Should the quickstart include a file-backed memory example once that feature
  lands?
