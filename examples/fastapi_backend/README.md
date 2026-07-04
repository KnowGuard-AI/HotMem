# fastapi_backend

A standalone FastAPI app that uses HotMem **as a library** (in-process
`MemoryDB` + `search_memories`) — no sidecar required. Exposes `/remember`
and `/ask` endpoints.

## Setup

See [../README.md](../README.md#prerequisites) for the common HotMem install
steps. This example needs the dev extra (no sidecar required):

```sh
pip install -e ".[dev]"           # HotMem itself
pip install fastapi uvicorn
```

## Run

```sh
uvicorn app:app --port 8000
```

## Try it

```sh
curl -s -XPOST localhost:8000/remember -H 'content-type: application/json' \
  -d '{"identifier":"user-1","fact":"Prefers dark mode"}'

curl -s -XPOST localhost:8000/ask -H 'content-type: application/json' \
  -d '{"query":"what UI do I like?"}'
```
