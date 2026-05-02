# Coding Agent Session: Building HotMem
**A local-first memory sidecar for agent applications**
Submitted for YC Summer 2026 — Optional coding agent session evidence
---
Built April 28 - May 1, 2026 (4 days)

## Context

We're building KnowGuard — enterprise anomaly detection for financial documents. Our core pipeline already works: ingest invoices, extract features via NVIDIA NIM, score for duplicates and rate mismatches, produce evidence packs.

What it lacked was *operational memory*. Each run starts cold — no awareness of prior payments, contracted rates, or previously flagged invoices. We needed a sidecar that could turn any writable storage into on-demand semantic memory for agents, and plug into the existing pipeline without modification.

Rather than adopt an external vector DB or build memory into the core app, we scoped a standalone package: **HotMem** — one SQLite DB, one HTTP port, zero config. Any directory becomes portable agent memory.

This session shows how we built it from spec to working integration in a single agentic workflow.

---

## Our Process

We don't use AI to scaffold projects or generate boilerplate. Our codebases are hand-architected with clear foundations. What we use agents for:

1. **Plan with a reasoning model** — groom the work into a structured spec (YAML), iterate on architecture decisions, surface tradeoffs
2. **Feed scoped work packages to a coding agent** — each package has clear inputs, outputs, and boundaries
3. **Execute piece-wise** — never "generate the whole thing", always module-by-module with verification between steps
4. **Agent maintains its own state** — worklog, TODO lists, trace context so any session can resume without re-scanning the codebase

The agent doesn't replace us. It operates within the constraints we set, and we course-correct when it drifts.

---

## Session Trace

### Phase 1: Specification & Planning

**Human** provides a full YAML specification:
- One SQLite DB, one port (8711), no frontend, no auth, no external deps
- 5 API endpoints: health, add, search, hydrate, snapshot
- Swap file (JSONL) hydration with SHA-256 dedup
- Portable mount directories
- Message-shaped output for direct LLM context stitching
- Acceptance criteria: `pip install` → `hotmem serve` → working

**Agent** produces an implementation plan — 9 source files, 4 runtime deps, execution order.

**Human** refines:
> "Make sure for speed and performance we only use the most advanced, performant and low-level libraries like uv, ruff, ty... This package will always be built and maintained by agents so we need to add proper traces and agentic logs... structure it so even if we in future work piece-wise each trace is there allowing us to extend each component independently."

**Agent** revises the plan:
- `uv` for all package management (no pip/setuptools)
- `ruff` lint + format, `ty` optional type checking
- New `trace.py` module: structured JSON logging to stderr, component-tagged, agent-parseable
- Each source module gets a self-documenting header (purpose, interface, deps, extension points)
- One test file per source module — agents extend tests alongside the component they touch
- No cross-module imports except through explicit interfaces

**Human** approves. Execution begins.

### Phase 2: Piece-wise Execution

Each module built and verified independently:

```
Step 1: pyproject.toml + __init__.py (uv init, deps, ruff config, scripts entry point)
Step 2: trace.py — structured logging infra (everything else depends on this)
Step 3: embed.py — deterministic hash-based embedder (dim=64, zero external deps)
Step 4: db.py — SQLite schema, CRUD, cosine similarity registered as UDF
Step 5: search.py — hybrid ranking (0.6 cosine + 0.2 keyword + 0.2 importance)
Step 6: swap.py + mount.py — JSONL hydrate/snapshot, directory bootstrap
Step 7: server.py — FastAPI, 5 endpoints, trace middleware, X-HotMem-Trace-Id header
Step 8: cli.py — Click CLI (serve, hydrate, snapshot, status)
Step 9: client.py — HotMemClient (httpx-based, context manager)
```

After each step: lint check, import verification. After all steps:

```
$ uv run ruff check src/ tests/
All checks passed!

$ uv run pytest tests/ -v
33 passed in 0.31s
```

33 tests across 7 test files. Zero external test deps beyond pytest.

### Phase 3: Hardening & Distribution

- `.gitignore` added (agent forgot `__pycache__` on first commit — caught and fixed immediately)
- Package builds: `uv build` → `hotmem-0.1.0-py3-none-any.whl`
- Installable from GitHub: `uv add git+https://github.com/KnowGuard-AI/HotMem.git`

### Phase 4: Integration into Existing App

The sidecar plugs into KnowGuard's pipeline with 4 file changes:

1. **`core/memory.py`** — `MemoryStore` wrapper. Connects to HotMem sidecar. If sidecar is down, all operations silently no-op. Zero breakage guarantee.
2. **`reactors/leakage_hunter.py`** — Recall prior findings *before* analysis, store new findings *after*. Optional `memory` parameter — existing code path unchanged.
3. **`api/deps.py`** — Singleton wiring.
4. **`api/routes/memory.py`** — Two new endpoints for memory status and search.

Start the sidecar: `hotmem serve --mount ./data/hotmem`
Start the app: `uvicorn api.main:app`
If HotMem isn't running, the app works exactly as before.

### Phase 5: Making It Real (The Hard Part)

**Human** catches that the demo page shows identical results for cold and HotMem runs:
> "I'm a little concerned that I'm getting the same numbers with every run... this is not fabricated is it?"

**Agent** confirms: yes, the demo used hardcoded static data.

> "Now I'm anxious, so even the extra memory-enabled anomalies detected is fake? You breaking my heart."

This triggers a multi-step investigation and fix:

1. **MockClient was returning identical features for every file** — rewrote to parse actual invoice content (vendor name, invoice number, amounts from file text)
2. **Swap file had no operational memories** — seeded with 14 real indexed facts: prior payment records with IBANs, contracted rates per vendor, discount policies, vendor aliases
3. **Recall query was wrong** — searched "anomaly findings for {filename}" which missed payment ledger facts. Fixed to search by invoice number + vendor name
4. **HotMem DB accumulated junk across runs** — 90 stale findings drowning 14 seeded facts. Added `_ReadOnlyMemory` wrapper so demo reads but never writes
5. **Verified the full chain** — manual curl tests confirming seeded facts return as top results, `_check_memory_signals()` fires on duplicate resubmissions

Each fix was a separate commit with verification before and after.

### Phase 6: NIM Smoke Test

**Human** asks to verify real LLM inference works. Agent discovers:
- The configured model (`nemotron-4-340b-instruct`) was retired — 404
- API key valid but not loaded in shell (only via dotenv)
- Tested available models, found `llama-3.3-nemotron-super-49b-v1` works on Inception account
- Updated config, committed

---

## What This Demonstrates

**We don't generate codebases — we extend them.** The KnowGuard app existed before this session. HotMem was conceived, specified, and built as a modular addition. The integration touched 4 files in the existing app.

**Every module is independently workable.** Each source file has a docstring header declaring its purpose, interface, dependencies, and extension points. An agent can pick up `search.py` without reading `db.py`. One test file per module. Component-tagged traces.

**We verify before we celebrate.** The hardcoded demo results were caught by the human, not the agent. The subsequent investigation — wrong recall queries, stale DB state, missing seed data — is the real work. The agent traced each problem to its root cause with targeted diagnostic commands before writing fixes.

**Surgical commits.** Each fix is scoped: one problem, one commit, verified before and after. Not a single "fix everything" commit.

**The human sets constraints. The agent operates within them.** When the agent tried to rebuild the demo page from scratch, the human stopped it: "Who asked you to do it this way? Plan first, minimally." The agent adjusted.

---

## Deliverables

| Artifact | Description |
|---|---|
| `HotMem` package | 10 source modules, 33 tests, 4 runtime deps, pip-installable |
| `core/memory.py` | Graceful integration wrapper with no-op fallback |
| `data/hotmem/swap.jsonl` | 14 seeded operational memories (payment records, contract rates, aliases) |
| Demo page (`/demo`) | Real pipeline execution, measured metrics, findings delta, memory trace |
| `.worklog.md` | Agent resumption context — architecture map, integration state, open items |

---

## The Principle

HotMem doesn't make the model smarter. It makes the task context sharper.

Same input, same workflow, same model. Memory just means the system remembers what it already knows.
