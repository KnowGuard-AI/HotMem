<p align="center">
  <img src="hotmem/hotmem-banner.png" alt="HotMem banner" />
</p>

<p align="center">
  <a href="https://github.com/KnowGuard-AI/HotMem/actions/workflows/ci.yml"><img src="https://github.com/KnowGuard-AI/HotMem/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://pypi.org/project/hotmem/"><img src="https://img.shields.io/pypi/v/hotmem" alt="PyPI" /></a>
  <a href="https://pypi.org/project/hotmem/"><img src="https://img.shields.io/pypi/pyversions/hotmem" alt="Python" /></a>
  <a href="https://codecov.io/gh/KnowGuard-AI/HotMem"><img src="https://codecov.io/gh/KnowGuard-AI/HotMem/branch/main/graph/badge.svg" alt="codecov" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/KnowGuard-AI/HotMem" alt="License: MIT" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff" /></a>
</p>

# HotMem

**Portable, local-first memory for AI agents and digital organizations.** HotMem
turns the facts, decisions, project context, and provenance an agent needs into
a queryable memory store that can be snapshotted, verified, restored, and moved
between compatible HotMem runtimes.

It is the memory layer for teams building with coding agents, enterprise
workflows, creative tools, and personal knowledge systems: one SQLite runtime,
one local HTTP port, and a portable JSONL-based interchange path. Agents can
write, retrieve, inspect, and manage their own scoped memory through HTTP,
Python, TypeScript, or MCP.

HotMem provides fast hybrid vector + keyword retrieval and returns LLM-ready
message objects you can stitch directly into prompts. It supports Python 3.11,
3.12, 3.13, and 3.14.

> **What is available today:** local-first runtime memory, portable JSONL and
> integrity-checked Snapshot v2 exports, restore/hydration, provenance,
> lifecycle events, and framework/MCP integrations. Verified interchange
> packages and incremental synchronization are active roadmap work; HotMem does
> not yet claim hosted sync, encryption, signing, or automatic multi-writer
> conflict resolution.

## Why HotMem

- **Move an agent's working brain.** Snapshot project or session memory and
  hydrate it in a clean HotMem instance without rebuilding the context by hand.
- **Keep the source of truth local.** The runtime is a SQLite mount, not a
  required hosted vector database or proprietary control plane.
- **Make memory agent-operable.** HTTP, Python, TypeScript, and MCP surfaces
  let an agent add facts, recall context, create snapshots, and inspect state.
- **Preserve provenance and integrity.** Snapshot v2 carries a versioned
  manifest, deterministic identifiers, SHA-256 file checksums, and optional
  file references.
- **Avoid a migration cliff.** HotMem supports legacy JSONL/JSONL.GZ snapshots
  and includes a one-command importer for a Mem0 SQLite history database.

Read the [agent-memory portability guide](docs/agent-memory-portability.md) for
the current contract, examples, and roadmap boundaries.

The long-term direction is intentionally explicit: the
[HotMem Vision and Canon](docs/vision-and-canon.md) records the non-negotiable
product principles and the delivery path toward a universal memory-interchange
standard. It distinguishes that destination from features that are available
in the current release.

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

## Snapshot, restore, and migration

Use a directory path for an integrity-checked Snapshot v2 package, or use
`.jsonl` / `.jsonl.gz` for the canonical portable record stream:

```bash
# Create a verified directory snapshot from one workspace.
hotmem snapshot --db ./source/hotmem.sqlite --file ./company-brain

# Restore it into a new workspace or runtime.
hotmem hydrate --db ./target/hotmem.sqlite --file ./company-brain

# Import current state from a Mem0 SQLite history database.
hotmem import --from mem0 --db ./mem0/history.db --target ./hotmem.sqlite
```

Snapshot v2 verifies SHA-256 checksums before hydration. Replaying the same
snapshot does not create duplicate logical memories. See the
[Snapshot v2 format](docs/snapshot-v2.md) and the
[interchange strategy](docs/okf/company-brain-interchange.md) for the exact
current guarantees.

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
