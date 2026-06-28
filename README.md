![HotMem banner](hotmem/hotmem-banner.png)

# HotMem

[![CI](https://github.com/KnowGuard-AI/HotMem/actions/workflows/ci.yml/badge.svg)](https://github.com/KnowGuard-AI/HotMem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hotmem)](https://pypi.org/project/hotmem/)
[![Python](https://img.shields.io/pypi/pyversions/hotmem)](https://pypi.org/project/hotmem/)
[![codecov](https://codecov.io/gh/KnowGuard-AI/HotMem/branch/main/graph/badge.svg)](https://codecov.io/gh/KnowGuard-AI/HotMem)
[![License: MIT](https://img.shields.io/github/license/KnowGuard-AI/HotMem)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A local-first memory sidecar for agent applications. One SQLite DB. One port: 8711.

HotMem provides fast, queryable working memory with hybrid vector + keyword search. Store facts, retrieve them ranked, and get back LLM-ready message objects you can stitch directly into prompts.

Supports Python 3.11, 3.12, 3.13, and 3.14.

## Install

```bash
pip install hotmem
# or
uv pip install hotmem
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
hotmem hydrate --file swap.jsonl.gz --db ./my.sqlite
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

## Ecosystem

HotMem core stays zero-dep. Framework adapters live in `adapters/`, each a separate
pip-installable package wrapping `HotMemClient`:

| Package | Framework |
| --- | --- |
| `hotmem-langchain` | LangChain (`BaseChatMessageHistory`, `BaseRetriever`) |
| `hotmem-crewai` | CrewAI memory backend |
| `hotmem-autogen` | AutoGen memory plugin |
| `hotmem-pydanticai` | Pydantic AI dependency + tools |
| `hotmem-hermes` | [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider plugin |

The `hotmem-hermes` adapter is the deep integration: HotMem implements the Hermes
[Memory Provider Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin)
interface, so Hermes calls into HotMem at every lifecycle point automatically
(prefetch, sync, memory-write mirroring, pre-compress extraction, session-end snapshot).

A typed TypeScript client (`npm install hotmem`) lives in `ts/` — zero-dependency,
works in Node 18+, Deno, Bun, and edge runtimes.

## Mounting

Any directory can be a HotMem mount. The mount contains:

- `hotmem.sqlite` - the database
- `swap.jsonl` - portable JSONL backup
- `manifest.json` - mount metadata

Plain `.jsonl` is the canonical portable swap format. HotMem can also hydrate
from and snapshot to `.jsonl.gz` for compressed archives.

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

## Architecture

Each source module is self-contained with a docstring header describing its purpose and interface:

| Module | Purpose |
|--------|---------|
| `trace.py` | Structured JSON logging |
| `embed.py` | Hash-based embedder (dim=64) |
| `db.py` | SQLite storage + cosine similarity UDF |
| `search.py` | Hybrid ranking (cosine + keyword + importance) |
| `swap.py` | JSONL hydrate/snapshot |
| `mount.py` | Portable directory management |
| `server.py` | FastAPI endpoints |
| `cli.py` | Click CLI |
| `client.py` | Python SDK (httpx) |

Every operation emits structured JSON traces to stderr with component tags:

```bash
hotmem serve --mount ./data 2>&1 | grep '"component": "search"'
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT - see [LICENSE](LICENSE).
