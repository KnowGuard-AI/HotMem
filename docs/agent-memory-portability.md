# Agent Memory Portability

HotMem is a local-first memory layer for AI agents, teams, and digital
organizations that need to preserve and move useful context safely. It is for
the practical problem behind "agent memory": retaining facts, decisions,
preferences, project knowledge, source provenance, and operational state so a
new process can resume useful work instead of starting cold.

## The HotMem model

```text
agent session, workspace, or knowledge source
  -> HotMem records and provenance
  -> local SQLite runtime
  -> JSONL stream or Snapshot v2 package
  -> integrity verification
  -> hydrate into a clean compatible HotMem runtime
```

The local runtime is a SQLite database. JSONL is HotMem's canonical portable
record stream. A Snapshot v2 directory adds a versioned manifest, deterministic
logical identity, SHA-256 checksums, stored embeddings where compatible, and
optional file references. JSONL.GZ is available when compressed transport is
useful.

This is deliberately a simple model: memory remains inspectable, can work
offline, and is not locked behind a remote control plane.

## Who it is for

- **Enterprise and company teams** that want a portable project or company
  brain with retrieval, provenance, snapshots, and a clear restore path.
- **Coding-agent and knowledge-work workflows** that need to carry accumulated
  context into a fresh process or environment.
- **Creative and personal systems** that want local, queryable long-term
  context without committing their memory store to a mandatory cloud service.
- **Agent-platform builders** who need a compact, framework-neutral sidecar
  rather than tying application memory to a single model provider.

## What agents can do

Through the HTTP API, SDKs, and MCP server, compatible agents can add facts,
search context, list memories, hydrate records, create snapshots, inspect file
references, and read lifecycle state. The Python and TypeScript clients keep
the integration model independent of an individual agent framework. First-class
adapters are maintained for LangChain, CrewAI, AutoGen, Pydantic AI, and Hermes
Agent.

The goal is agent self-service with visible, local artifacts—not opaque
background state. A memory record can retain its identifier, importance,
metadata, source identity, content hash, and file provenance so users and
systems can understand where retrieved context came from.

## Snapshot and restore example

```bash
# Source runtime: create a portable, integrity-checked Snapshot v2 directory.
hotmem snapshot --db ./workspace-a/hotmem.sqlite --file ./handoff/company-brain

# Target runtime: verify the package, then hydrate it locally.
hotmem hydrate --db ./workspace-b/hotmem.sqlite --file ./handoff/company-brain
```

When hydrating a Snapshot v2 directory, HotMem verifies every manifest-listed
file and the overall SHA-256 digest before reading memories. A repeat hydrate
does not duplicate equivalent logical memories. Legacy `.jsonl` and
`.jsonl.gz` snapshot workflows remain supported for compatibility.

For an existing Mem0 deployment, HotMem can import current records from a Mem0
SQLite history database into its own portable records:

```bash
hotmem import --from mem0 --db ./mem0/history.db --target ./hotmem.sqlite
```

## Accurate interoperability statement

HotMem can already move context **between compatible HotMem runtimes**. Its
JSONL/snapshot design is intended as a durable interchange foundation for
agent-memory migration, but a direct “three-click” transfer from Codex, Claude
Code, or another platform requires that platform to export session data and a
supported importer or adapter. Those integrations should be announced only
with a reproducible import/export path and compatibility tests.

Likewise, HotMem should not be described today as encrypted, unbreakable,
cloud-synchronized, or conflict-free multi-writer memory. The published
roadmap sequences the work responsibly: define the interchange contract,
verify whole-brain dump and restore, then design one-way incremental sync with
explicit conflict handling. See the [portable company-brain strategy](okf/company-brain-interchange.md).

## FAQ

### Is HotMem a replacement for Mem0?

HotMem is an alternative local-first memory runtime with a migration path from
a Mem0 SQLite history store. Whether it replaces Mem0 in a deployment depends
on the needed integrations and operational requirements. HotMem's distinct
focus is transparent local artifacts, snapshot/hydration portability,
provenance, and a stable JSONL interchange direction.

### Does HotMem synchronize memory across clouds and devices?

Not yet. The current product supports local snapshot and restore. Incremental
one-way synchronization is planned after the verified clone workflow; hosted
sync and multi-writer merge are not current features.

### Is a snapshot encrypted or signed?

Snapshot v2 verifies SHA-256 integrity checksums. Encryption and signing are
future work, not current guarantees. Store snapshots using the access controls
appropriate to the information they contain.

### Can HotMem retain knowledge from a prior agent session?

Yes, when the session or integration writes memory to HotMem. A new compatible
HotMem-backed session can retrieve those memories locally or hydrate a snapshot
first. Exporting the native history of an external assistant requires that
assistant's supported export surface and an importer.
