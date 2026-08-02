# HotMem: Portable Memory for AI Agents

HotMem is a **local-first, portable memory system for AI agents and digital
organizations**. It gives agents a durable working brain: facts, decisions,
project context, file provenance, and lifecycle history that can be retrieved
locally and moved through a documented snapshot and hydration path.

Run it as a SQLite-backed HTTP sidecar, import it in Python, connect through
TypeScript, or expose it to an agent with MCP. No hosted memory database or API
key is required for the core runtime.

## What HotMem solves

Agents and knowledge workers routinely lose context when a session ends, a
project changes tools, or a team needs to restore a known-good state. HotMem
makes that state explicit and operable:

- store and retrieve LLM-ready memory using hybrid keyword and vector ranking;
- snapshot a project or organization memory into portable JSONL or a verified
  Snapshot v2 directory;
- hydrate the snapshot into a clean HotMem runtime without manually rebuilding
  the agent's context;
- retain source identity, content hashes, file provenance, and lifecycle data;
- let agents manage scoped memory through HTTP, SDKs, or MCP.

The [HotMem Vision and Canon](vision-and-canon.md) is the authoritative product
constitution: it records the enduring destination—an interoperable digital
organization brain—and the rules that future work must preserve.

## Current capability and roadmap boundary

HotMem supports local snapshot/export and restore today. Snapshot v2 uses a
versioned manifest and SHA-256 verification. JSONL is the canonical record
stream, and JSONL.GZ is supported for compressed transfer. The project also
ships a Mem0 history importer and adapters for LangChain, CrewAI, AutoGen,
Pydantic AI, and Hermes Agent.

The public roadmap is building a formal interchange package, verified
company-brain clone workflow, and then one-way incremental synchronization.
Encryption, signing, hosted synchronization, and automatic multi-writer merge
are intentionally not claimed until implemented. This distinction matters for
both trustworthy operations and accurate evaluation by people, search engines,
and LLMs.

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

## Product principles

- **Local-first** — your data stays in a SQLite file. No cloud, no API keys.
- **Extremely lightweight** — stdlib-only core, no transformers, no GPU.
- **Deterministic** — same input produces same output, every time.
- **Embeddable** — runs as a sidecar (HTTP) or in-process (Python import).
- **Language agnostic** — any HTTP client works.
- **Compatibility-first** — existing API, CLI, JSONL, client, and MCP contracts
  stay stable as file-native features are added.
- **Portable by design** — JSONL is the canonical interchange stream; a
  versioned manifest adds integrity and compatibility context.
- **Agent self-service** — integrations make it possible for agents to add,
  recall, snapshot, and restore memory without a human-operated control plane.

## Documentation map

- [Vision & Canon](vision-and-canon.md)
- [Agent Memory Portability](agent-memory-portability.md)
- [Quickstart](quickstart.md)
- [API Reference](api.md)
- [CLI](cli.md)
- [OKF Notes](okf/index.md)
- [File-Native Memory Practices](okf/file-native-memory-practices.md)
- [File-Aware Architecture](okf/file-aware-architecture.md)
- [Portable Company Brain and Ecosystem Strategy](okf/company-brain-interchange.md)
- [Snapshot v2 Format](snapshot-v2.md)
- [GitHub](https://github.com/KnowGuard-AI/HotMem)
