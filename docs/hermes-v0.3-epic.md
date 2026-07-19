# HotMem-Hermes v0.3.0 Epic: Official Memory Partner Campaign

**Status:** Planning  
**Branch:** `docs/hermes-v0.3-epic`  
**Created:** 2026-07-18  
**Owner:** HotMem Core Team

---

## Executive Summary

This epic documents the 16-week campaign to establish HotMem as the official memory provider for Hermes Agent and Hermes Workspace. The campaign combines engineering delivery (shipping production-ready adapters), credibility building (benchmarks), and co-marketing execution (tutorial series + joint partnership announcement).

### North Star Metrics

- HotMem becomes the recommended memory provider in official Hermes documentation
- `hotmem-hermes` reaches 1,000+ weekly downloads on PyPI
- Tutorial series achieves 50K+ cumulative views across YouTube channels
- Joint partnership video published with NousResearch

### Target Partners

- **NousResearch** (Hermes core team) — formal co-marketing partnership
- **AI Engineer** (YouTube) — long-form engineering deep dives
- **Cargo** (YouTube) — quick tutorial/demo cadence

---

## Campaign Architecture

The campaign is structured in four phases over 16 weeks:

| Phase | Weeks | Focus | Key Deliverable |
|-------|-------|-------|-----------------|
| 0 | 1–2 | Foundation | Workspace adapter + benchmarks |
| 1 | 3–5 | Credibility | "The Memory Problem" arc |
| 2 | 5–8 | Distribution | Build-in-public serialization |
| 3 | 8–10 | Partnership | Official Hermes integration |
| 4 | 11–16 | Moat | Advanced features + ecosystem story |

---

## Phase 0: Foundation (Weeks 1–2)

**Goal:** Make the existing adapter rock-solid and production-credible before any media.

### WP0.1 — Workspace Adapter Parity

**Status:** Not started  
**Effort:** 2 weeks  
**Owner:** TBD

#### Description

Build `adapters/hermes-workspace/` that mirrors the agent adapter. Workspace is multi-user, multi-session and requires:

- Tenant-scoped swap files (one per workspace)
- Workspace-level search (filter by workspace_id)
- Shared memory policies (read/write permissions per user)
- Session isolation (multi-session within same workspace)

#### Deliverables

- `adapters/hermes-workspace/` directory structure
- `hotmem_hermes_workspace` Python package (separate from `hotmem-hermes`)
- `pyproject.toml` with dependencies: `hotmem>=0.2.0`, `hermes-workspace>=1.0`
- Implement `HotMemWorkspaceProvider` subclassing Hermes Workspace MemoryProvider ABC
- Tenant isolation via workspace_id metadata on all memories
- Workspace-level swap files: `$WORKSPACE_HOME/{workspace_id}/swap.jsonl`
- Shared memory policies API:
  ```python
  provider.set_policy(workspace_id, user_id, permissions=["read", "write"])
  provider.get_policy(workspace_id, user_id) -> list[str]
  ```
- Workspace-aware tools:
  - `hotmem_workspace_search(query, workspace_id, top_k=5)`
  - `hotmem_workspace_store(workspace_id, identifier, fact, importance=0.5)`
- CLI commands:
  - `hermes workspace memory status`
  - `hermes workspace memory config`
- Bundled skill: `skill/hotmem-workspace-memory/SKILL.md`
- Test suite: `tests/test_workspace_*.py`
- README.md with quickstart

#### Acceptance Criteria

- Passes Hermes Workspace plugin discovery
- Multi-user scenario: User A stores fact, User B can search it (with read permission)
- Session isolation: Two concurrent sessions in same workspace don't interfere
- Swap file hydration preserves workspace_id metadata
- All tests pass in Hermes Workspace CI example matrix

#### Dependencies

- Hermes Workspace MemoryProvider ABC (need to confirm interface matches Agent)
- HotMem v0.2.2+ (current)

#### Risks

- Hermes Workspace may not have the same plugin interface as Agent
- Tenant isolation may require HotMem core changes (metadata filtering)

---

### WP0.2 — Benchmarks + Eval Harness

**Status:** Not started  
**Effort:** 1 week  
**Owner:** TBD

#### Description

HotMem needs numbers to compete against Zep, Mem0, Letta's built-in memory. Build a reproducible recall-benchmark suite using LOCOMO (or similar long-context memory benchmark) and publish results.

#### Deliverables

- `benchmarks/` directory at repo root
- `benchmarks/locomo/` — LOCOMO dataset integration
- `benchmarks/harness.py` — generic eval harness:
  ```python
  class MemoryBenchmark:
      def __init__(self, dataset: str, provider: MemoryProvider):
          ...
      def run(self) -> BenchmarkResult:
          # Returns precision@k, recall@k, latency stats
          ...
  ```
- Implement adapters for competitors:
  - `benchmarks/providers/mem0.py`
  - `benchmarks/providers/zep.py`
  - `benchmarks/providers/letta.py`
  - `benchmarks/providers/hotmem.py`
- Run LOCOMO benchmark with identical queries across all providers
- Publish results in `benchmarks/RESULTS.md`:
  - Precision@1, @5, @10
  - Recall@5, @10
  - Mean latency (p50, p95, p99)
  - Memory usage
- Script to reproduce: `uv run python benchmarks/run_all.py`
- CI integration: Run benchmarks nightly, publish to GitHub Pages

#### Acceptance Criteria

- HotMem scores within 10% of Mem0/Zep on LOCOMO precision@5
- HotMem latency is 50%+ lower than Mem0/Zep (local-first advantage)
- Results are reproducible via `make benchmark`
- Published to `docs/benchmarks.md`

#### Metrics

- **LOCOMO precision@5:** Target 0.75+
- **LOCOMO recall@10:** Target 0.85+
- **p50 search latency:** Target <50ms
- **p99 search latency:** Target <200ms

#### Risks

- LOCOMO may not be representative of real agent workloads
- Competitors may have optimized their embeddings for benchmark datasets
- Benchmark gaming: Results may not translate to production

---

### WP0.3 — Hermes Monorepo Alignment

**Status:** Not started  
**Effort:** 3 days  
**Owner:** TBD

#### Description

Confirm the adapter works against the current Hermes release branch, match their Python version pin, run it in their CI example matrix.

#### Deliverables

- Pin `hotmem-hermes` Python version to match Hermes (currently 3.11+)
- Add Hermes as a test dependency in `adapters/hermes/pyproject.toml`:
  ```toml
  [project.optional-dependencies]
  test = [
      "hermes-agent>=1.0",
      "pytest>=9",
  ]
  ```
- Create `adapters/hermes/tests/test_hermes_integration.py`:
  - Load provider via Hermes plugin discovery
  - Run prefetch/sync_turn/on_memory_write hooks
  - Verify tool schemas match Hermes expectations
- PR to Hermes repo adding HotMem to their example matrix:
  - `examples/memory_providers/hotmem/` with quickstart
  - Update their `docs/memory.md` with HotMem mention
- Get explicit "works with Hermes X.Y" badge from their team

#### Acceptance Criteria

- `hotmem-hermes` tests pass against Hermes `main` branch
- Hermes CI example runs HotMem successfully
- Hermes docs mention HotMem in memory provider section

#### Risks

- Hermes may not accept PR (need to pitch to their team first)
- Hermes interface may change between versions

---

## Phase 1: "The Memory Problem" Arc (Weeks 3–5)

**Goal:** Frame the problem generically, build credibility, pitch NousResearch partnership.

### WP1.1 — Release hotmem-hermes v0.3.0

**Status:** Not started  
**Effort:** 1 week  
**Owner:** TBD

#### Description

Ship the first production-ready release with Workspace adapter + polished docs + bundled skill.

#### Deliverables

- Bump version to 0.3.0 in `adapters/hermes/pyproject.toml`
- Merge WP0.1 (Workspace adapter) into this release
- Update `adapters/hermes/README.md`:
  - Quickstart section (3 commands)
  - Architecture diagram (prefetch/sync/memory-write flow)
  - Configuration reference
  - Troubleshooting section
- Update bundled skill `skill/hotmem-memory/SKILL.md`:
  - Add examples of when to use `hotmem_store`
  - Add anti-patterns (don't store ephemeral state)
- Publish to PyPI: `uv build && uv publish`
- GitHub release with changelog
- Announce on Twitter/Hacker News

#### Acceptance Criteria

- `pip install hotmem-hermes==0.3.0` works
- README has complete quickstart
- Bundled skill is installable via `hermes skills install`
- GitHub release has clear changelog

---

### WP1.2 — Video: "Why AI Agents Have No Memory"

**Status:** Not started  
**Effort:** 2 weeks (script + production)  
**Partner:** AI Engineer (YouTube)  
**Owner:** TBD

#### Description

Long-form deep dive framing the memory problem generically. Don't sell HotMem — sell the *shape* of the problem.

#### Script Outline

1. **Hook (0:00–1:00):**
   - Demo: Ask same agent a question in two sessions, get contradictory answers
   - "This is what happens when agents have no memory"

2. **The Problem (1:00–4:00):**
   - Context window is not memory
   - MEMORY.md is a hack (flat file, no recall)
   - Vector-only recall loses keyword precision
   - Hybrid search (vector + FTS5) is the right shape

3. **What Good Memory Looks Like (4:00–7:00):**
   - Prefetch before each turn
   - Async sync after each turn (non-blocking)
   - Mirror built-in memory writes (additive, not replacement)
   - Pre-compress extraction (save facts before context truncation)
   - Session-end snapshot (portable across machines)

4. **The Memory Provider Plugin Pattern (7:00–9:00):**
   - Hermes has the right abstraction
   - Anyone can implement the MemoryProvider ABC
   - This is how memory should work in 2026

5. **What We're Building (9:00–10:00):**
   - Tease HotMem as "one implementation of this pattern"
   - Don't hard-sell, just mention it exists
   - CTA: "In the next video, we'll build one from scratch"

#### Deliverables

- 10-minute video script (5,000 words)
- B-roll: Hermes agent demo (screen recording)
- Diagrams: memory provider architecture
- Publish to AI Engineer YouTube channel
- Blog post companion on Medium/Substack

#### Acceptance Criteria

- Video published on AI Engineer channel
- 20K+ views in first month
- 90%+ positive sentiment in comments
- Drives 500+ visits to HotMem GitHub

---

### WP1.3 — Hermes Co-Blog Post Draft

**Status:** Not started  
**Effort:** 1 week  
**Partner:** NousResearch  
**Owner:** TBD

#### Description

Pitch NousResearch on a "Partners" announcement where they link HotMem from their docs as the recommended local-first memory.

#### Approach

1. **Email pitch to NousResearch team:**
   - Subject: "Partnership proposal: HotMem as official Hermes memory provider"
   - Body:
     - We've built a production-ready memory provider for Hermes
     - Benchmarks show it's competitive with Mem0/Zep (attach RESULTS.md)
     - We'd like to propose a co-marketing partnership
     - What we're offering:
       - Joint blog post
       - Joint video (their founder + our host)
       - "Recommended" badge in Hermes docs
     - What we're asking:
       - Mention in Hermes docs
       - Co-branded tutorial series
       - Shoutout in their release notes

2. **Follow-up call:**
   - Demo the integration live
   - Show benchmark results
   - Discuss co-marketing calendar

3. **Draft blog post:**
   - Title: "Announcing HotMem: The Official Local-First Memory Provider for Hermes"
   - Co-authored by NousResearch + HotMem teams
   - Announce partnership, show demo, link to tutorials

#### Deliverables

- Email pitch (500 words)
- Draft blog post (2,000 words)
- Partnership agreement (informal, email-based)

#### Acceptance Criteria

- NousResearch agrees to partnership
- Blog post drafted and reviewed
- Timeline agreed for joint video (Phase 3)

#### Risks

- NousResearch may not be interested in formal partnership
- They may already have a preferred memory provider
- Timing may not align with their release schedule

---

## Phase 2: Build-In-Public Serialization (Weeks 5–8)

**Goal:** Ship tutorial series demonstrating real use cases, build distribution.

### WP2.1 — Video: "Making an Agent Remember Everything"

**Status:** Not started  
**Effort:** 1 week  
**Partner:** Cargo (YouTube)  
**Owner:** TBD

#### Description

Ship a Hermes Agent + HotMem sidecar tutorial. Show the prefetch/sync-turn/on-memory-write flow live.

#### Script Outline

1. **Setup (0:00–2:00):**
   ```bash
   pip install hermes hotmem-hermes
   hotmem serve --mount ./hotmem
   hermes config set memory.provider hotmem
   ```

2. **First session (2:00–5:00):**
   - Run Hermes agent
   - Tell it: "My name is Zubin, I work on AI infrastructure"
   - Show `hotmem_search` being called implicitly (prefetch)
   - Exit session

3. **Second session (5:00–7:00):**
   - Start new Hermes session
   - Ask: "What's my name?"
   - Agent recalls from HotMem
   - Show swap file contents

4. **Under the hood (7:00–9:00):**
   - Explain prefetch/sync_turn/on_memory_write hooks
   - Show HotMem sidecar logs
   - Explain hybrid search (vector + FTS5)

5. **Wrap-up (9:00–10:00):**
   - "Now your agent remembers across sessions"
   - CTA: Next video covers Workspace

#### Deliverables

- 10-minute video script
- Screen recording of Hermes + HotMem sidecar
- Tutorial repo: `github.com/KnowGuard-AI/hotmem-hermes-tutorials`
- Publish to Cargo YouTube channel

#### Acceptance Criteria

- Video published on Cargo channel
- 15K+ views in first month
- Tutorial repo has working code
- Drives 300+ `hotmem-hermes` installs

---

### WP2.2 — Video: "Your Workspace Agent Should Know Everyone"

**Status:** Not started  
**Effort:** 1 week  
**Partner:** Cargo (YouTube)  
**Owner:** TBD

#### Description

Hermes Workspace + shared swap files. Demo multi-user memory isolation vs. team-shared facts.

#### Script Outline

1. **Problem (0:00–1:30):**
   - "You have a team using Hermes Workspace"
   - "User A learns a fact, User B should benefit"
   - "But User A's private notes should stay private"

2. **Setup (1:30–3:00):**
   ```bash
   pip install hotmem-hermes-workspace
   hermes workspace memory setup --provider hotmem
   ```

3. **Multi-user demo (3:00–6:00):**
   - User A stores: "Our API rate limit is 1000 req/min"
   - User B asks: "What's the API rate limit?"
   - User B gets answer from shared memory
   - User A stores private note: "I prefer dark mode"
   - User B doesn't see private note (policy-based isolation)

4. **Policies (6:00–8:00):**
   ```python
   provider.set_policy(workspace_id, user_id, permissions=["read", "write"])
   ```
   - Explain read vs. write permissions
   - Show audit logs

5. **Wrap-up (8:00–10:00):**
   - "Workspace memory: shared by default, private when needed"

#### Deliverables

- 10-minute video script
- Screen recording of Hermes Workspace multi-user scenario
- Add to tutorial repo
- Publish to Cargo YouTube channel

#### Acceptance Criteria

- Video published on Cargo channel
- 10K+ views in first month
- Tutorial repo has Workspace example

---

### WP2.3 — Video: "Swap Files Are the Sleeper Feature"

**Status:** Not started  
**Effort:** 1 week  
**Partner:** Cargo (YouTube)  
**Owner:** TBD

#### Description

The mount + snapshot/hydrate + `.jsonl.gz` portable memory concept. Frame as "USB-stick memory for air-gapped workspaces."

#### Script Outline

1. **Hook (0:00–1:00):**
   - "What if you could put your agent's memory on a USB stick?"
   - "And plug it into an air-gapped machine?"

2. **Swap file basics (1:00–4:00):**
   ```bash
   hotmem snapshot --file swap.jsonl --db ./hotmem.sqlite
   hotmem hydrate --file swap.jsonl --db ./new-machine.sqlite
   ```
   - Show JSONL format (human-readable, portable)
   - Explain snapshot/hydrate cycle

3. **Compressed archives (4:00–6:00):**
   ```bash
   hotmem snapshot --file swap.jsonl.gz --db ./hotmem.sqlite
   hotmem hydrate --file swap.jsonl.gz --db ./new-machine.sqlite
   ```
   - Show gzip compression (10x smaller)
   - Explain use case: email/archive memory snapshots

4. **Mount directory (6:00–8:00):**
   ```bash
   hotmem serve --mount /mnt/usb/hotmem
   ```
   - Explain mount concept (SQLite + swap + manifest)
   - Show portable workflow: USB stick → air-gapped machine

5. **Real-world use case (8:00–10:00):**
   - "You train an agent on-site at a client"
   - "Export memory to swap file"
   - "Email it to client's air-gapped cluster"
   - "Hydrate on their side"
   - "Agent works offline with full memory"

#### Deliverables

- 10-minute video script
- Screen recording of swap file workflow
- Add to tutorial repo
- Publish to Cargo YouTube channel

#### Acceptance Criteria

- Video published on Cargo channel
- 8K+ views in first month
- Tutorial repo has swap file example

---

### WP2.4 — Tutorial Repo

**Status:** Not started  
**Effort:** 1 week  
**Owner:** TBD

#### Description

Create `github.com/KnowGuard-AI/hotmem-hermes-tutorials` with three worked notebooks.

#### Deliverables

- **Repo structure:**
  ```
  hotmem-hermes-tutorials/
  ├── README.md
  ├── 01-agent-basics/
  │   ├── README.md
  │   ├── notebook.ipynb
  │   └── requirements.txt
  ├── 02-workspace-multi-user/
  │   ├── README.md
  │   ├── notebook.ipynb
  │   └── requirements.txt
  ├── 03-air-gapped-swap/
  │   ├── README.md
  │   ├── notebook.ipynb
  │   └── requirements.txt
  ```

- **01-agent-basics:**
  - Start HotMem sidecar
  - Configure Hermes Agent with HotMem
  - Run a session, show prefetch/sync_turn
  - Inspect swap file
  - Hydrate in new session

- **02-workspace-multi-user:**
  - Start HotMem sidecar
  - Configure Hermes Workspace with HotMem
  - Simulate two users (User A, User B)
  - Show shared memory vs. private memory
  - Set policies, show audit logs

- **03-air-gapped-swap:**
  - Train agent on machine A
  - Snapshot to swap.jsonl
  - Transfer to machine B (simulate air-gap)
  - Hydrate on machine B
  - Show agent works with full memory

- **Tutorial site via mkdocs:**
  - Publish to `hotmem-hermes-tutorials.readthedocs.io`
  - Include video embeds from Cargo series
  - Include benchmark results from WP0.2

#### Acceptance Criteria

- All three notebooks run end-to-end without errors
- Tutorial site is live and discoverable
- Each tutorial has a "Run in Colab" button

---

## Phase 3: The "Official" Partnership (Weeks 8–10)

**Goal:** Convert credibility into a formal co-marketing moment.

### WP3.1 — Joint Video with NousResearch

**Status:** Not started  
**Effort:** 2 weeks  
**Partner:** NousResearch  
**Owner:** TBD

#### Description

"The Official Memory Layer for Hermes" — their founder + your host on camera, demoing the integration from a fresh install.

#### Format

- 30-minute interview + demo
- Guests: NousResearch founder (e.g., Karan Malhotra) + HotMem host

#### Script Outline

1. **Intro (0:00–3:00):**
   - Introduce Hermes: "Open-source agent framework"
   - Introduce HotMem: "Local-first memory sidecar"
   - Announce partnership: "HotMem is now the recommended memory provider"

2. **Why memory matters (3:00–8:00):**
   - NousResearch founder explains memory challenges in agent frameworks
   - Discuss context decay, session continuity, user preferences
   - Frame HotMem as solving these problems

3. **Live demo (8:00–20:00):**
   - Fresh install: `pip install hermes hotmem-hermes`
   - Start sidecar: `hotmem serve`
   - Run Hermes agent, show memory working
   - Show Workspace multi-user scenario
   - Show swap file portability

4. **Under the hood (20:00–25:00):**
   - Discuss architecture (prefetch/sync/memory-write hooks)
   - Discuss benchmarks (HotMem vs. Mem0 vs. Zep)
   - Discuss design philosophy (local-first, zero-dependency, portable)

5. **Roadmap (25:00–28:00):**
   - HotMem v1.0 (stable release)
   - Hermes plugin marketplace listing
   - Cross-ecosystem story (LangChain, CrewAI, AutoGen adapters)

6. **Wrap-up (28:00–30:00):**
   - CTA: "Try HotMem with Hermes today"
   - Links to tutorials, docs, GitHub

#### Deliverables

- 30-minute video script
- Joint recording session
- Publish to AI Engineer YouTube channel
- Cross-promote on NousResearch Twitter/Hacker News

#### Acceptance Criteria

- Video published on AI Engineer channel
- 50K+ views in first month
- NousResearch promotes on their channels
- Drives 1,000+ `hotmem-hermes` installs

---

### WP3.2 — Hermes Docs Integration

**Status:** Not started  
**Effort:** 1 week  
**Partner:** NousResearch  
**Owner:** TBD

#### Description

Get a "HotMem (Recommended)" section in the official Hermes memory docs, with a one-command install.

#### Deliverables

- PR to Hermes repo: `docs/memory.md`
  - Add section: "HotMem (Recommended)"
  - Include:
    ```bash
    hermes memory setup --provider hotmem
    ```
  - Link to HotMem docs
  - Mention benchmarks
- PR to Hermes repo: `examples/memory_providers/hotmem/`
  - Quickstart README
  - Example config
  - Link to tutorial repo
- PR to Hermes repo: `README.md`
  - Add "Partners" section with HotMem logo
  - Link to partnership announcement

#### Acceptance Criteria

- PRs merged into Hermes repo
- HotMem mentioned in official Hermes docs
- "Recommended" badge visible on memory provider page

---

### WP3.3 — Release hotmem v1.0

**Status:** Not started  
**Effort:** 1 week  
**Owner:** TBD

#### Description

Cut a stable release on the back of the partnership — this is the press-release moment.

#### Deliverables

- Bump version to 1.0.0 in `pyproject.toml`
- Write release notes:
  - Stable API (no breaking changes)
  - Benchmarks published
  - Official Hermes partnership
  - Tutorial series live
- Publish to PyPI: `uv build && uv publish`
- GitHub release with changelog
- Press release (Hacker News, Twitter, Reddit)
- Announce on NousResearch channels

#### Acceptance Criteria

- `pip install hotmem==1.0.0` works
- Release has complete changelog
- Press release drives 1,000+ GitHub stars

---

## Phase 4: Moat Widening (Weeks 11–16)

**Goal:** Ship advanced features that differentiate HotMem from competitors.

### WP4.1 — LLM-Based Fact Extraction

**Status:** Not started  
**Effort:** 2 weeks  
**Owner:** TBD

#### Description

Replace the regex heuristic in `on_pre_compress` with a lightweight local model call. Frame as open research.

#### Implementation

```python
def on_pre_compress(self, messages: list[Any], **kwargs: Any) -> None:
    """Extract durable facts using a local LLM."""
    client = self._provider._client
    trailing = messages[-6:] if len(messages) > 6 else messages
    
    async def _go() -> None:
        for msg in trailing:
            text = _msg_text(msg)
            if not text:
                continue
            
            # Use local model (e.g., Phi-3-mini) to extract facts
            facts = await extract_facts_with_llm(
                text,
                model="microsoft/Phi-3-mini-4k-instruct",
                device="cpu",  # or "mps" for macOS
            )
            
            for fact in facts:
                await client.add(
                    "hermes:context",
                    fact,
                    source="hermes:pre_compress",
                    importance=0.6,
                    metadata={"phase": "pre_compress"},
                )
    
    self._run(_go)
```

#### Deliverables

- Implement `extract_facts_with_llm()` function
- Use local model (Phi-3-mini, 1.5B params, CPU-friendly)
- Fallback to regex heuristic if model not available
- Benchmark: LLM extraction vs. regex extraction
- Publish comparison in `benchmarks/FACT_EXTRACTION.md`

#### Acceptance Criteria

- LLM extraction improves precision@5 by 15%+ over regex
- Latency <200ms per message on M1 MacBook Air
- Falls back gracefully to regex if model not available

---

### WP4.2 — Memory Policies for Workspace

**Status:** Not started  
**Effort:** 2 weeks  
**Owner:** TBD

#### Description

Role-based memory, department scoping, audit logs. Frame as enterprise readiness.

#### Implementation

```python
class MemoryPolicy:
    def __init__(self, workspace_id: str, user_id: str):
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.permissions: list[str] = []  # ["read", "write", "admin"]
        self.scope: str = "workspace"  # or "department", "private"
        self.department: str | None = None

class HotMemWorkspaceProvider:
    def set_policy(self, workspace_id: str, user_id: str, policy: MemoryPolicy) -> None:
        # Store policy in HotMem metadata
        self._client.add(
            f"policy:{workspace_id}:{user_id}",
            json.dumps(policy.to_dict()),
            importance=1.0,
            metadata={"type": "policy"},
        )
    
    def check_policy(self, workspace_id: str, user_id: str, action: str) -> bool:
        # Retrieve policy, check if action is allowed
        policy = self.get_policy(workspace_id, user_id)
        return action in policy.permissions
    
    def audit_log(self, workspace_id: str, user_id: str, action: str, details: dict) -> None:
        # Log all memory operations for compliance
        self._client.add(
            f"audit:{workspace_id}",
            f"{user_id} performed {action}: {json.dumps(details)}",
            source="hermes:audit",
            importance=0.3,
            metadata={"type": "audit", "user_id": user_id, "action": action},
        )
```

#### Deliverables

- Implement `MemoryPolicy` class
- Add `set_policy()`, `get_policy()`, `check_policy()` methods
- Add `audit_log()` method (all operations logged)
- Workspace-level search respects policies (only return memories user can read)
- Write tests for policy enforcement
- Document in `docs/policies.md`

#### Acceptance Criteria

- User without "read" permission cannot search workspace memory
- User without "write" permission cannot store to workspace memory
- Audit log captures all operations with timestamp + user_id
- Policies survive swap file hydrate (stored as metadata)

---

### WP4.3 — Hermes Plugin Marketplace Listing

**Status:** Not started  
**Effort:** 1 week  
**Owner:** TBD

#### Description

Ship a proper `manifest.json` with Hermes' plugin spec so HotMem appears in whatever directory/discovery Hermes ships.

#### Deliverables

- Create `adapters/hermes/manifest.json`:
  ```json
  {
    "name": "hotmem",
    "version": "0.3.0",
    "description": "Local-first memory provider for Hermes",
    "author": "HotMem Core Team",
    "homepage": "https://github.com/KnowGuard-AI/HotMem",
    "repository": "https://github.com/KnowGuard-AI/HotMem",
    "license": "MIT",
    "tags": ["memory", "local-first", "hybrid-search"],
    "install": "pip install hotmem-hermes",
    "documentation": "https://hotmem.readthedocs.io/adapters/hermes",
    "benchmarks": "https://hotmem.readthedocs.io/benchmarks",
    "compatibility": {
      "hermes-agent": ">=1.0",
      "hermes-workspace": ">=1.0"
    }
  }
  ```
- Submit to Hermes plugin directory
- Get listed on Hermes website

#### Acceptance Criteria

- `manifest.json` passes Hermes plugin validation
- HotMem appears in Hermes plugin directory
- Users can discover HotMem via `hermes plugins search memory`

---

### WP4.4 — Cross-Ecosystem Story

**Status:** Not started  
**Effort:** 2 weeks  
**Partner:** AI Engineer (YouTube)  
**Owner:** TBD

#### Description

"HotMem already has LangChain, CrewAI, AutoGen, Pydantic AI adapters — now Hermes is first-class." One video showing that this isn't just a Hermes plugin, it's *the* memory layer for the whole agent stack.

#### Script Outline

1. **Hook (0:00–2:00):**
   - "You built an agent in LangChain"
   - "You switched to CrewAI for multi-agent"
   - "You're considering AutoGen for enterprise"
   - "What if your memory worked the same everywhere?"

2. **HotMem ecosystem (2:00–6:00):**
   - Show all adapters: LangChain, CrewAI, AutoGen, Pydantic AI, Hermes
   - Explain shared memory format (JSONL swap files)
   - Demo: Train agent in LangChain, export memory, hydrate in Hermes

3. **Cross-framework demo (6:00–9:00):**
   - Start HotMem sidecar
   - Use LangChain agent, store some facts
   - Switch to Hermes agent, show recall working
   - Swap file is the bridge

4. **Why this matters (9:00–11:00):**
   - "Memory should be framework-agnostic"
   - "Swap files are the universal format"
   - "HotMem is the sidecar that works everywhere"

5. **Wrap-up (11:00–12:00):**
   - "HotMem: The memory layer for the agent stack"

#### Deliverables

- 12-minute video script
- Screen recording of cross-framework demo
- Publish to AI Engineer YouTube channel

#### Acceptance Criteria

- Video published on AI Engineer channel
- 30K+ views in first month
- Drives installs across all adapters (not just Hermes)

---

## Content Strategy

### Video Partners

| Partner | Style | Cadence | Episodes |
|---------|-------|---------|----------|
| **AI Engineer** | Long-form deep dives (20–30 min) | Monthly | 3 videos |
| **Cargo** | Quick tutorials (8–12 min) | Bi-weekly | 4 videos |

### Content Calendar

| Week | Partner | Title | Phase |
|------|---------|-------|-------|
| 3 | AI Engineer | "Why AI Agents Have No Memory" | 1 |
| 5 | Cargo | "Making an Agent Remember Everything" | 2 |
| 6 | Cargo | "Your Workspace Agent Should Know Everyone" | 2 |
| 7 | Cargo | "Swap Files Are the Sleeper Feature" | 2 |
| 8 | AI Engineer | "Official Memory Layer for Hermes" (joint) | 3 |
| 12 | AI Engineer | "LLM-Based Fact Extraction" | 4 |
| 14 | AI Engineer | "Memory Policies for Enterprise" | 4 |
| 16 | AI Engineer | "Cross-Ecosystem Memory" | 4 |

### Blog Posts

| Title | Platform | Phase |
|-------|----------|-------|
| "Announcing HotMem: The Official Local-First Memory Provider for Hermes" | Substack + NousResearch blog | 3 |
| "HotMem v1.0: Stable API, Benchmarks, Partnership" | Substack + Hacker News | 3 |
| "How We Extract Facts from Context Before Compression" | Substack | 4 |
| "Memory Policies: Who Should See What" | Substack | 4 |

---

## Success Metrics

### Engineering Metrics

- **Benchmark precision@5:** Target 0.75+ (LOCOMO)
- **Benchmark recall@10:** Target 0.85+ (LOCOMO)
- **p50 search latency:** Target <50ms
- **p99 search latency:** Target <200ms

### Distribution Metrics

- **PyPI downloads:** Target 1,000+/week for `hotmem-hermes`
- **GitHub stars:** Target 2,000+ (from 1,000 baseline)
- **Tutorial repo stars:** Target 500+

### Media Metrics

- **YouTube views:** Target 150K+ cumulative across all videos
- **YouTube CTR:** Target 8%+ (high-quality thumbnails/titles)
- **Tutorial completion rate:** Target 60%+ (users finish all 3 tutorials)

### Partnership Metrics

- **Hermes docs integration:** HotMem mentioned in official docs
- **Joint video:** 50K+ views in first month
- **Co-branded tutorials:** 10K+ installs from tutorial links

---

## Risks & Mitigations

### Risk 1: Hermes Workspace Adapter Is Harder Than Expected

**Likelihood:** Medium  
**Impact:** High  
**Mitigation:**
- Start WP0.1 early (Week 1)
- If Workspace interface diverges significantly from Agent, scope down to Agent-only for v0.3.0
- Publish Workspace adapter as v0.4.0 instead

### Risk 2: Benchmarks Are Not Competitive

**Likelihood:** Low (local-first should have latency advantage)  
**Impact:** High  
**Mitigation:**
- Run benchmarks early (Week 1)
- If HotMem underperforms, investigate:
  - Embedding quality (hash-based vs. learned)
  - Search algorithm (cosine vs. hybrid)
  - Indexing strategy
- Publish results even if not #1 (transparency builds trust)

### Risk 3: NousResearch Not Interested in Partnership

**Likelihood:** Medium  
**Impact:** Medium  
**Mitigation:**
- Build credibility first (Phase 0 + Phase 1)
- Approach them with data (benchmarks, tutorial series)
- If they decline, proceed with organic content strategy
- HotMem still benefits from "Hermes-compatible" positioning

### Risk 4: Tutorial Series Underperforms

**Likelihood:** Low (Cargo has strong distribution)  
**Impact:** Low  
**Mitigation:**
- A/B test thumbnails/titles
- Cross-promote on Twitter/Hacker News/Reddit
- Engage with comments, build community

### Risk 5: Competitors Ship Faster

**Likelihood:** High (Mem0/Zep are well-funded)  
**Impact:** Medium  
**Mitigation:**
- Compete on local-first story (they can't match without cloud rearch)
- Compete on portability (swap files are genuinely unique)
- Compete on ecosystem breadth (5 framework adapters vs. their 1–2)
- Move fast — 16-week campaign is aggressive for a reason

### Risk 6: Hermes Interface Changes Mid-Campaign

**Likelihood:** Medium (Hermes is pre-1.0)  
**Impact:** Medium  
**Mitigation:**
- Pin against Hermes release tags, not `main`
- Run integration tests weekly against Hermes `main`
- If they ship breaking changes, absorb quickly (small adapter surface)

### Risk 7: YouTube Partners Bail

**Likelihood:** Low (Cargo/AI Engineer are reliable)  
**Impact:** Medium  
**Mitigation:**
- Self-publish fallback channel (KnowGuard-AI YouTube)
- Blog posts carry the content if video underperforms
- Tutorial repo + mkdocs site works without any video

---

## Open Questions

Items requiring decisions before work starts:

1. **Workspace ABC confirmation** — Does Hermes Workspace expose the same `MemoryProvider` ABC as Agent? Need to audit Hermes source before committing to WP0.1 shape.
2. **NousResearch relationship warmth** — Do we have a direct line to the Hermes team, or is this a cold outbound? Cold outreach extends Phase 1 timeline.
3. **Budget for video production** — Do we have existing relationships with AI Engineer / Cargo, or are we pitching them cold?
4. **Benchmarks license** — LOCOMO dataset licensing — confirm it's open-research-friendly before building harness around it.
5. **HotMem v1.0 scope** — Does the core `hotmem` package need features beyond Hermes for v1.0, or is the Hermes partnership the entire v1.0 story?
6. **Workspace vs. Agent release cadence** — Ship Workspace in v0.3.0 alongside Agent, or split to v0.3.0 (Agent) + v0.4.0 (Workspace)?

---

## Appendix A: Source of Truth

This document is the single source of truth for the hotmem-hermes v0.3.0 epic.

- **Branch:** `docs/hermes-v0.3-epic`
- **File:** `docs/hermes-v0.3-epic.md`
- **Update policy:** Any work package scope, timeline, or deliverable change MUST be reflected here before implementation starts. PRs that touch hotmem-hermes v0.3.0 work should reference this doc.
- **Decision log:** Append decisions from "Open Questions" below as they're resolved.

## Appendix B: Related Documents

- `PLAN.md` — overall HotMem roadmap
- `CHANGELOG.md` — release history
- `adapters/hermes/README.md` — current adapter docs
- `adapters/hermes/hotmem_hermes/plugin.yaml` — plugin spec

## Appendix C: Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-18 | Created epic doc on `docs/hermes-v0.3-epic` branch | Central planning artifact for v0.3.0 + partnership campaign |
| — | *append decisions here as they resolve* | — |
