# HotMem Evolution Roadmap
This plan maps each Perplexity claim to a concrete extension of the existing architecture. Every phase preserves the core principles: local-first, single SQLite DB, single port, simple HTTP API, LLM-ready output, portable mounts, structured tracing.
## Core Principles to Preserve
* **Local-first**: everything works offline with zero external dependencies
* **Single-file DB**: SQLite remains the primary store
* **Single port sidecar**: all access through HTTP on 8711
* **LLM-ready output**: search always returns message objects you can stitch into prompts
* **Portable mounts**: any directory can be a HotMem mount
* **Zero-config start**: `hotmem serve` still works with no args
## Phase 1: Smarter Retrieval (foundation for everything else)
**Target Perplexity claim**: "semantic graph", "intent-aware reasoning"
**Touches**: `embed.py`, `db.py`, `search.py`
### 1a. Pluggable real embeddings
The extension point already exists in `embed.py` ("Swap for real model"). Add an `Embedder` protocol so users can choose:
* `hotmem-hash-v1` (current, zero-dep default)
* `sentence-transformers` (local model, e.g. `all-MiniLM-L6-v2`)
* `openai` / `anthropic` API-based
Config via `hotmem serve --embedder sentence-transformers` or env var. Hash-based stays the default to preserve zero-dep local-first.
### 1b. FTS5 full-text search
`db.py` already notes "Add FTS5". Create an FTS5 virtual table mirroring `fact_text`. This replaces the naive keyword overlap in `search.py` with proper tokenized full-text matching, dramatically improving the keyword component of hybrid search.
### 1c. Reranking / MMR diversity
`search.py` already notes "Add reranking, MMR". After hybrid scoring, apply Maximal Marginal Relevance to avoid returning near-duplicate memories. This is critical once the DB grows beyond toy size.
## Phase 2: Hot-State Awareness (the actual "HotMem" in the name)
**Target Perplexity claim**: "hot-state pooling", "fast memory reclamation", "facts agents access most often"
**Touches**: `db.py`, `search.py`, `server.py`, new `gc.py`
### 2a. Access tracking and decay
Add columns to the memories table:
* `last_accessed_at` — updated on every search hit
* `access_count` — incremented on every search hit
* `ttl_seconds` — optional per-memory expiry
Modify `search.py` scoring to incorporate recency/frequency: memories that are accessed often and recently get a boost — these are the genuinely "hot" memories.
### 2b. Hot-path caching
Keep an in-process LRU cache of the top-N most-accessed memory embeddings. This avoids re-reading SQLite for the hottest facts on every query. The cache invalidates on insert/update.
### 2c. Memory garbage collection (`gc.py`)
New module + CLI subcommand `hotmem gc`:
* Evict memories past their TTL
* Archive cold memories (access_count below threshold) to a separate `cold.jsonl` swap file
* Reclaim SQLite space with `VACUUM`
This is the "fast memory reclamation" concept — but done at the application level (eviction policies) rather than VM-level, which is the right abstraction for a sidecar.
## Phase 3: Auto-Discovery Affinity Index
**Target Perplexity claim**: "semantic network", "semantic graph of institutional knowledge"
**Touches**: `db.py`, `search.py`, `server.py`
**Design philosophy**: No managed graph. No agent-maintained edges. Relationships are discovered automatically at write time and surfaced transparently at query time. The "graph" is an emergent property of the data, not an artifact agents have to curate.
### 3a. Affinity table (minimal schema)
```SQL
CREATE TABLE IF NOT EXISTS affinities (
    memory_a TEXT NOT NULL,
    memory_b TEXT NOT NULL,
    score    REAL NOT NULL,
    PRIMARY KEY (memory_a, memory_b)
);
CREATE INDEX IF NOT EXISTS idx_aff_b ON affinities(memory_b);
```
No relation types. No weights to tune. No metadata. Just `(A, B, score)`. Bidirectional lookup via the PK index + `idx_aff_b`.
### 3b. Write-time auto-discovery
On every `POST /v1/add`, after inserting the memory and computing its embedding (already happens):
1. Run the existing `cosine_sim` UDF against all stored embeddings (single SQL query, reuses the UDF already registered in `db.py`)
2. Keep only the top-K neighbors (K=5) above a similarity threshold (e.g. 0.4)
3. INSERT those as affinity rows
Bounded cost: each insert adds at most K rows to the affinity table. Total affinity table size is O(N × K), not O(N²). For 10K memories with K=5, that's ~50K rows — trivial for SQLite.
The embedding is already computed for the insert — affinity discovery reuses it at zero extra embedding cost.
### 3c. Affinity-expanded search
Extend `search_memories()` with an optional `expand: bool = False` parameter:
1. Run normal hybrid search → top-K results
2. If `expand=True`, do a single JOIN through the affinity table to fetch 1-hop neighbors of those results that aren't already in the result set
3. Score the expanded memories, merge and re-rank
4. Return with an `affinity_source` field so the caller knows which results came from direct match vs. expansion
This is one additional SQL query — a JOIN, not a graph traversal. Stays O(K × K_neighbors).
### 3d. Performance constraints
* **K cap**: max 5 affinities per memory (configurable). On insert, if a memory would exceed K, drop the lowest-scoring edge.
* **Threshold**: only store affinities above 0.4 cosine similarity. Below that, the relationship is noise.
* **Cascade delete**: when a memory is deleted or GC'd (Phase 2), its affinity rows go with it via `ON DELETE CASCADE`. No orphan cleanup needed.
* **No backfill on insert**: when a new memory M is added and becomes a top-K neighbor of existing memory X, we do NOT retroactively update X's affinity list. X's affinities were correct at X's write time. This keeps insert cost bounded to one scan, not a full reindex.
* **Bulk skip**: `hydrate` can optionally skip affinity computation (`--skip-affinities`) for large imports, then run a one-time `hotmem reindex-affinities` to backfill.
## Phase 4: Multi-Agent and Enterprise Primitives
**Target Perplexity claim**: "enterprise-grade", "multiple agent instances sharing state", "governance-aware"
**Touches**: `db.py`, `server.py`, new `auth.py`, new `namespaces.py`
### 4a. Namespaced memory
Add optional `namespace` field to memories (default: `"default"`). Agents can read/write to their own namespace and optionally query across namespaces. This is the multi-agent "shared state" — all agents hit one sidecar but get logical isolation.
New endpoint behavior: `POST /v1/search` accepts optional `namespace` and `cross_namespace: bool` parameters.
### 4b. Audit trail
Extend the trace system to write an append-only audit log:
* Who (agent/namespace) accessed what memory, when
* All mutations (add, relate, gc) logged with before/after
* `GET /v1/audit?identifier=vendor_x` to query the trail
This enables the "governance-aware" claim.
### 4c. Auth middleware
`server.py` already notes "Add CORS, rate limiting". Add:
* API key auth via `X-HotMem-Key` header (optional, disabled by default)
* CORS configuration
* Per-namespace rate limiting
Still local-first: auth is opt-in, not required.
## Phase 5: Orchestration Hooks and Remote Sync
**Target Perplexity claim**: "orchestration layer", "ingest data from ERP, logs, documents"
**Touches**: `server.py`, `mount.py`, new `events.py`, new `ingest.py`
### 5a. Event stream
Add `GET /v1/events` (SSE) that emits real-time events when memories are added, searched, related, or evicted. Agents can subscribe to react to memory changes — enabling coordination through shared memory rather than direct messaging.
### 5b. Remote mount sync
`mount.py` already notes "Add remote sync". Add optional sync targets:
* `hotmem serve --sync s3://bucket/hotmem-mount`
* Periodic snapshot + upload, download + hydrate on startup
* Enables shared memory across distributed agents
### 5c. Bulk ingest
New `POST /v1/ingest` endpoint and `hotmem ingest` CLI:
* Accept JSONL, CSV, or newline-delimited text
* Process in batches with progress reporting
* Optional source tagging (`source: "erp"`, `source: "contract"`, etc.)
This is the "ingest data from ERP, logs, documents" claim — but as a general-purpose bulk loader, not hard-coded to specific enterprise systems.
## Phasing Summary
* **Phase 1** (Smarter Retrieval): Direct upgrades to existing extension points. No new modules. No API changes except better results.
* **Phase 2** (Hot-State): New schema columns + one new module. The feature that justifies the project name.
* **Phase 3** (Graph): New table + module. Biggest conceptual leap but SQLite handles it fine.
* **Phase 4** (Enterprise): Multi-tenancy and governance. Opt-in complexity, zero-config stays simple.
* **Phase 5** (Orchestration): Network effects. Only makes sense after Phases 1-4 are solid.
Each phase is independently shippable and backward-compatible. A `hotmem serve` with no flags still works exactly as it does today at every phase.