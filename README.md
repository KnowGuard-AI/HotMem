# HotMem

A local-first memory sidecar for agent applications. One SQLite DB. One port: 8711.

HotMem provides fast, queryable working memory with hybrid vector + keyword search. Store facts, retrieve them ranked, and get back LLM-ready message objects you can stitch directly into prompts.

## Install

```bash
pip install hotmem@git+https://github.com/KnowGuard-AI/HotMem.git
# or
uv add hotmem@git+https://github.com/KnowGuard-AI/HotMem.git
```

Or add to `requirements.txt`:

```
hotmem @ git+https://github.com/KnowGuard-AI/HotMem.git
```

## Quick Start

```bash
# Start with a mount directory (portable memory)
hotmem serve --mount ./hotmem

# Or just start (uses temp DB)
hotmem serve
```

## CLI

```bash
hotmem serve --port 8711 --mount ./data/hotmem
hotmem serve --db ./my.sqlite
hotmem hydrate --file swap.jsonl --db ./my.sqlite
hotmem snapshot --file swap.jsonl --db ./my.sqlite
hotmem status
```

## API

All endpoints under `/v1`. Default: `http://127.0.0.1:8711`

### `GET /v1/health`

```json
{"status": "ok", "memory_count": 42, "db_path": "...", "uptime_s": 120.5}
```

### `POST /v1/add`

```json
{"identifier": "vendor_x", "fact": "Invoice total was $5000", "importance": 0.8}
```

### `POST /v1/search`

```json
{"query": "duplicate invoice risk", "top_k": 5, "max_chars": 1500}
```

Returns ranked message objects ready for LLM stitching:

```json
{
  "memories": [
    {"role": "system", "content": "...", "memory_id": "...", "identifier": "...", "score": 0.87}
  ],
  "count": 5,
  "trace_ms": 2.1
}
```

### `POST /v1/hydrate`

```json
{"file": "swap.jsonl"}
```

### `POST /v1/snapshot`

```json
{"file": "swap.jsonl"}
```

## Python Client

```python
from hotmem.client import HotMemClient

with HotMemClient("http://127.0.0.1:8711") as client:
    client.add("vendor_x", "Invoice total $5000", importance=0.8)

    memories = client.search("duplicate invoice risk", top_k=5, max_chars=1500)

    # memories are LLM-ready message objects
    messages = memories + [{"role": "user", "content": "Analyze this vendor."}]
```

## Mounting

Any directory can be a HotMem mount. The mount contains:

- `hotmem.sqlite` — the database
- `swap.jsonl` — portable JSONL backup
- `manifest.json` — mount metadata

```bash
hotmem serve --mount /mnt/usb/hotmem     # portable memory on USB
hotmem serve --mount ./data/hotmem        # local project memory
```

## Development

```bash
uv sync                          # install deps
uv run pytest                    # run tests
uv run ruff check src/ tests/    # lint
uv run ruff format src/ tests/   # format
uv build                         # build wheel
```

## Architecture (for agents)

Each source module is independently extensible with self-documenting headers:

| Module | Purpose | Extension Point |
|--------|---------|------------------|
| `trace.py` | Structured JSON logging | Add sinks, formatters |
| `embed.py` | Hash-based embedder (dim=64) | Swap for real model |
| `db.py` | SQLite + cosine UDF | Add FTS5, indexes |
| `search.py` | Hybrid ranking | Add reranking, MMR |
| `swap.py` | JSONL hydrate/snapshot | Add compression |
| `mount.py` | Directory bootstrap | Add remote sync |
| `server.py` | FastAPI endpoints | Add CORS, new routes |
| `cli.py` | Click CLI | Add subcommands |
| `client.py` | httpx SDK | Add async client |

Every operation emits structured JSON traces to stderr with component tags:

```bash
hotmem serve --mount ./data 2>&1 | grep '"component": "search"'
```
