# OKF: HotMem Documentation Home

Status: Accepted
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: Documentation entrypoint and repository documentation policy

## 1. Purpose

A **local-first memory sidecar** for agent applications. One SQLite DB. One port: `8711`.

HotMem provides fast, queryable working memory with hybrid vector + keyword search. Store facts, retrieve them ranked, and get back LLM-ready message objects you can stitch directly into prompts.

This repository uses an OKF-style documentation format by default. User-facing
docs stay direct and practical, but every doc should preserve status, ownership,
scope, current decisions, and open questions where relevant.

## 2. 30-second Quickstart

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

## 3. Current Decisions

- **Local-first** — your data stays in a SQLite file. No cloud, no API keys.
- **Extremely lightweight** — stdlib-only core, no transformers, no GPU.
- **Deterministic** — same input produces same output, every time.
- **Embeddable** — runs as a sidecar (HTTP) or in-process (Python import).
- **Language agnostic** — any HTTP client works.
- **Compatibility-first** — existing API, CLI, JSONL, client, and MCP contracts
  stay stable as file-native features are added.

## 4. Documentation Map

- [Quickstart](quickstart.md)
- [API Reference](api.md)
- [CLI](cli.md)
- [OKF Notes](okf/index.md)
- [File-Native Memory Practices](okf/file-native-memory-practices.md)
- [File-Aware Architecture](okf/file-aware-architecture.md)
- [GitHub](https://github.com/KnowGuard-AI/HotMem)

## 5. Open Questions

- Which OKF notes should graduate into user-facing quickstart/API docs first?
- Which vNext decisions should remain in GitHub issues only once implemented?
