# HotMem Evolution Roadmap (v2)
## Strategic Position
HotMem is the **open wedge product** — a portable memory sidecar that becomes the default go-to package for developers building agents with memory. It replaces mem0 by winning on developer experience, ecosystem reach, and ease of adoption — not on algorithmic depth.
Advanced memory intelligence (real embeddings, hot-state scoring, affinity discovery, GC, multi-agent governance) lives in the **EMOS platform packages** — a separate roadmap. HotMem creates the developer funnel; EMOS captures enterprise value.
**Package architecture (Option B):** emos-core is the runtime substrate. HotMem is a deliberately scoped distribution. HotMem does NOT expose extension points or pluggable protocols — for that, developers graduate to EMOS.
## Core Principles
* **Local-first**: everything works offline with zero external dependencies
* **Single-file DB**: SQLite remains the primary store
* **Single port sidecar**: all access through HTTP on 8711
* **LLM-ready output**: search always returns message objects you can stitch into prompts
* **Portable mounts**: any directory can be a HotMem mount
* **Zero-config start**: `hotmem serve` still works with no args
* **Zero dependencies**: no ML libraries, no vector DBs, no external services
## Boundary: What stays in EMOS (not HotMem)
The following features are explicitly reserved for the EMOS platform:
* Pluggable real embeddings (sentence-transformers, OpenAI, Anthropic)
* Access tracking / decay scoring / hot-state intelligence
* Hot-path embedding cache
* Affinity index / auto-discovery
* MMR reranking
* GC with cold archival
* Multi-namespace / multi-agent isolation
* Audit trails
* Remote mount sync
* Enterprise auth / governance
* Event streams / orchestration hooks
* Bulk enterprise ingest
## Phase 1: Credible Search Quality
**Goal**: Make search results good enough that developers trust HotMem for real work. Without this, the wedge doesn't stick.
**Touches**: `db.py`, `search.py`, `server.py`
### 1a. FTS5 full-text search
`db.py` already notes "Add FTS5". Create an FTS5 virtual table mirroring `fact_text`. This replaces the naive `_keyword_overlap()` in `search.py` with proper tokenized full-text matching using BM25 ranking.
FTS5 is a built-in SQLite feature — no external dependency, no proprietary IP. But it takes keyword search from toy to production-credible.
* FTS5 virtual table + sync triggers in `db.py`
* `MemoryDB.fts_search(query)` method returning rows with BM25 scores
* Replace `_keyword_overlap` in hybrid scoring with FTS5 BM25
* Rebalance weights: `W_COSINE`, `W_FTS`, `W_IMPORTANCE`
**GitHub**: issue #2
### 1b. Basic TTL
Simple per-memory expiry. No decay scoring, no access tracking — just "this memory expires after N seconds."
* Add `ttl_seconds INTEGER` column to memories table (nullable, NULL = never expires)
* `POST /v1/add` accepts optional `ttl_seconds` field
* `search_memories()` filters out expired rows: `WHERE ttl_seconds IS NULL OR (strftime('%s','now') - strftime('%s', created_at)) < ttl_seconds`
* No background job — filtering happens at query time only
This prevents unbounded DB growth without giving away the full GC/decay system reserved for EMOS.
**GitHub**: new issue needed
## Phase 2: Ecosystem Reach
**Goal**: Make HotMem available everywhere agents run. This is how HotMem beats mem0 — not better algorithms, but better reach.
### 2a. MCP server
Ship `hotmem mcp` command that starts a Model Context Protocol server. Instant integration with Claude Desktop, Cursor, Warp, and any MCP-compatible tool.
MCP tools to expose:
* `add_memory(identifier, fact, importance?, ttl_seconds?)` → adds a fact
* `search_memories(query, top_k?)` → returns ranked results
* `memory_health()` → status check
* `snapshot(file?)` → export to JSONL
* `hydrate(file?)` → import from JSONL
This alone could drive more adoption than any algorithm improvement. Mem0 doesn't have MCP support.
**GitHub**: new issue needed
### 2b. Framework adapters
Thin wrappers over `HotMemClient` for major agent frameworks. Each is a separate lightweight package so HotMem core stays zero-dep:
* `hotmem-langchain` — `HotMemChatMessageHistory`, `HotMemRetriever`
* `hotmem-crewai` — memory backend
* `hotmem-autogen` — memory plugin
* `hotmem-pydanticai` — tool/dependency provider
Each adapter is ~50-100 lines wrapping the existing HTTP client. The adapters make HotMem discoverable in each framework's ecosystem.
**GitHub**: new issue needed (one per adapter, or one umbrella issue)
### 2c. TypeScript/JS client
NPM package `hotmem` — typed client hitting the same `/v1` HTTP API. Most agent developers work in both Python and TS. The client mirrors the Python `HotMemClient` interface.
**GitHub**: new issue needed
## Phase 3: Developer Experience Polish
**Goal**: Make the first 5 minutes effortless. Reduce every friction point between "I heard about HotMem" and "I'm using it in my project."
### 3a. Async Python client
`client.py` already notes "add async client." Add `AsyncHotMemClient` using `httpx.AsyncClient`. Same interface as `HotMemClient` but with `await`. Most agent frameworks are async.
**GitHub**: new issue needed
### 3b. Docker image
`docker run -p 8711:8711 -v ./data:/data knowguard/hotmem`
Zero-install adoption path. Multi-arch (amd64 + arm64). Publish to Docker Hub and GHCR. Dockerfile is trivial — `uvicorn` on Alpine.
**GitHub**: new issue needed
### 3c. `hotmem playground`
Terminal UI for interactive add/search/inspect. Makes demos and debugging instant without needing `curl` or a Python script. Could use `rich` or `textual` for a polished look.
**GitHub**: new issue needed
### 3d. OpenAPI spec + docs site
FastAPI already auto-generates OpenAPI. Publish it + a minimal docs site (GitHub Pages). Include:
* Quickstart (30 seconds to first memory)
* API reference (auto-generated from OpenAPI)
* Framework integration guides
* "Why HotMem vs mem0" comparison page
**GitHub**: new issue needed
## Phase 4: Adoption Accelerators
**Goal**: Remove remaining barriers to switching and lower the cost of choosing HotMem.
### 4a. mem0 import
`hotmem import --from mem0 --db ./memories.db`
Read mem0's storage format and convert to HotMem JSONL, then hydrate. Make switching a one-command operation.
**GitHub**: new issue needed
### 4b. Rich CLI output
Pretty-printed search results, colored status, progress bars for hydrate/snapshot. The CLI is the first thing developers interact with — it should feel polished.
**GitHub**: new issue needed
### 4c. Examples repository
Standalone example projects showing HotMem with each framework: LangChain agent, CrewAI crew, AutoGen group, standalone FastAPI app, MCP integration. Each example is copy-paste-runnable.
**GitHub**: new issue or separate `hotmem-examples` repo
## Phasing Summary
* **Phase 1** (Search Quality): FTS5 + basic TTL. Minimum to make the wedge credible. Small scope, high impact.
* **Phase 2** (Ecosystem Reach): MCP, framework adapters, TS client. This is the mem0 killer — being everywhere agents run.
* **Phase 3** (DX Polish): Async client, Docker, playground, docs. Reduces adoption friction to near-zero.
* **Phase 4** (Adoption): mem0 import, rich CLI, examples. Removes the last switching barriers.
Each phase is independently shippable. `hotmem serve` with no flags works exactly as it does today at every phase. No phase gives away EMOS IP.