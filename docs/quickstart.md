# Quickstart

## Install

```bash
pip install hotmem
# or
uv pip install hotmem
```

Supports Python 3.11, 3.12, 3.13, and 3.14.

## Start the server

```bash
# Start with a mount directory (portable memory)
hotmem serve --mount ./hotmem

# Or just start (uses temp DB)
hotmem serve
```

## Add and search memories

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

## Portable memory (snapshots)

```bash
# Export to a swap file
hotmem snapshot --file swap.jsonl --db ./hotmem/hotmem.sqlite

# Hydrate on another machine
hotmem hydrate --file swap.jsonl --db ./my.sqlite
```

## Use the Python client

```python
from hotmem.client import HotMemClient

client = HotMemClient("http://127.0.0.1:8711")
client.add(identifier="user", fact="likes Rust")
results = client.search(query="programming preferences")
```

## Docker

```bash
docker run -p 8711:8711 -v ./data:/data knowguard/hotmem
```

See [CLI](cli.md) for the full command reference and [API Reference](api.md) for endpoints.
