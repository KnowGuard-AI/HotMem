# HotMem

A **local-first memory sidecar** for agent applications. One SQLite DB. One port: `8711`.

HotMem provides fast, queryable working memory with hybrid vector + keyword search. Store facts, retrieve them ranked, and get back LLM-ready message objects you can stitch directly into prompts.

## 30-second quickstart

```bash
pip install hotmem
hotmem serve --mount ./hotmem
```

In another terminal:

```bash
# Add a memory
curl -X POST http://127.0.0.1:8711/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"identifier": "user", "fact": "prefers dark mode"}'

# Search
curl -X POST http://127.0.0.1:8711/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "what theme does the user like"}'
```

## Why HotMem?

- **Local-first** — your data stays in a SQLite file. No cloud, no API keys.
- **Extremely lightweight** — stdlib-only core, no transformers, no GPU.
- **Deterministic** — same input produces same output, every time.
- **Embeddable** — runs as a sidecar (HTTP) or in-process (Python import).
- **Language agnostic** — any HTTP client works.

## Links

- [Quickstart](quickstart.md)
- [API Reference](api.md)
- [CLI](cli.md)
- [GitHub](https://github.com/KnowGuard-AI/HotMem)
