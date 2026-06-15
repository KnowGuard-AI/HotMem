# Changelog

All notable changes to HotMem will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.6] - 2026-06-09

### Added
- Batched JSONL hydration with SQLite-native duplicate skipping.
- Snapshot embedding export via `embedding_b64` for faster compatible rehydration.
- Hydration trace counters for parsed rows, loaded rows, duplicate skips, bytes read, and embedding reuse.

### Fixed
- Package version metadata now matches the runtime `hotmem.__version__`.

## [0.1.0] - 2025-05-02

### Added
- FastAPI sidecar server on port 8711
- SQLite storage with cosine similarity UDF
- Hash-based embedding (dim=64, zero external dependencies)
- Hybrid search: cosine similarity + keyword overlap + importance weighting
- LLM-ready message object output from search
- JSONL hydrate/snapshot for portable backup
- Mount directory concept (SQLite + swap + manifest)
- Python client SDK (`HotMemClient`)
- CLI: `serve`, `hydrate`, `snapshot`, `status`
- Structured JSON tracing to stderr
