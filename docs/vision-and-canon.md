# HotMem Vision and Canon

**Status:** Canonical product direction · **Owner:** HotMem maintainers ·
**Adopted:** 2026-08-02

Scope: Every HotMem runtime, interchange format, integration, service, and public
product claim.

## Purpose

HotMem exists to make a digital organization brain **portable, durable,
agent-operable, and trustworthy**.

People and agents create valuable context continuously: facts, decisions,
preferences, project state, working knowledge, source material, provenance,
and operational history. That context must not become stranded when a session
ends, a team changes tools, a project moves, a device is replaced, or an agent
runtime changes.

HotMem is the memory layer that makes this knowledge capturable, inspectable,
transferable, restorable, and manageable. Its destination is a world in which
moving a working digital brain is as normal and dependable as moving a file:
snapshot one session or workspace, begin in another compatible environment,
hydrate the context, and resume useful work.

This document is the durable product constitution. It preserves the intended
standard even when specific implementation work is staged over time. It does
not turn planned features into false claims about the current release.

## The non-negotiable promise

HotMem will be the open, plug-and-play memory foundation for agents and digital
organizations: a self-service system that can preserve and move relevant
knowledge across projects, agent harnesses, platforms, devices, and trusted
environments.

The desired experience is simple:

```text
capture a workspace or agent session
  -> create a portable HotMem snapshot
  -> move it to a compatible target
  -> verify it
  -> hydrate it
  -> continue with the same useful context
```

## North-star handoff: three actions, no context archaeology

The user experience HotMem is working toward is a three-action handoff:

1. **Snapshot:** capture the useful knowledge, facts, decisions, state, and
   provenance from a source session, workspace, or organization brain.
2. **Transfer:** move the verified HotMem interchange package by a trusted
   local, cloud, or mobile transport path.
3. **Hydrate:** load the package in a supported target environment and continue
   work with the recovered context available to its people and agents.

The intended example is direct and memorable: a user working in Codex,
ChatGPT, Claude Code, or another supported environment can hand off a project
brain to a new compatible target without manually re-explaining its history.
The HotMem JSONL interchange is the bridge; source and target adapters are the
tested platform boundaries.

“Three actions” is a north-star UX requirement, not a claim that every named
platform already has a native HotMem adapter. Until an adapter is implemented,
tested, and documented, HotMem must describe the route as planned rather than
seamless.

Whether the user is an enterprise team, an independent researcher, a creative
studio, or an individual, the outcome is the same: their digital brain should
remain theirs, remain understandable, and remain able to travel.

## Canonical principles

### 1. JSONL is the language of portable memory

The HotMem JSONL record stream is the canonical interchange language for memory
and context. It must remain simple to inspect, stream, version, validate,
archive, and transform. Storage engines, embeddings, indexes, cloud services,
and platform-specific adapters may evolve around it, but they must not make the
canonical memory artifact opaque or hostage to a vendor.

A versioned manifest supplies integrity, provenance, compatibility information,
and package identity. Compression is transport. Local SQLite is an efficient
runtime store. The portable record is the enduring common language.

### 2. The brain must move without starting over

HotMem must make it practical to clone, migrate, restore, and eventually
synchronize memory between compatible runtimes. A project should be able to
move from one agent harness to another without manually reconstructing prior
decisions, requirements, known facts, and working state.

The target experience is a small, explicit workflow—not an archaeological
exercise:

```text
Codex, ChatGPT, Claude Code, or another source environment
  -> export through a supported HotMem adapter/importer
  -> HotMem JSONL package
  -> verified transfer (local, cloud, or mobile transport)
  -> supported target adapter/importer
  -> Codex, ChatGPT, Claude Code, or another target environment
```

This applies to company brains, personal knowledge, creative projects, agent
sessions, and shared project state. An adapter is required at each external
platform boundary; HotMem will never pretend a native cross-platform transfer
exists until that export/import path is implemented and tested.

### 3. Agents must be able to manage memory safely themselves

HotMem is made for agent self-service. A compatible agent must be able to
capture useful memory, retrieve it, inspect its provenance, create a snapshot,
hydrate approved context, and report what happened through clear, auditable
interfaces.

Agent autonomy does not mean invisible authority. Memory operations must have
clear scope, provenance, validation, lifecycle visibility, and policy hooks.
Human and organization control remains possible at every boundary.

### 4. Trust is a product requirement, not a marketing adjective

Portable memory has to be fault-tolerant and security-conscious. The standard
must favor deterministic records, checksums, idempotent replay, verification
before hydration, explicit compatibility checks, observable errors, backups,
provenance, and recovery paths.

For sensitive transfer, HotMem's destination includes encryption, signing,
identity, authorization, and secure key management. These capabilities must be
implemented against a documented threat model and independently testable before
they are marketed as guarantees. No real system should promise to be literally
"unbreakable"; HotMem instead makes verifiable, bounded security guarantees and
improves them transparently.

### 5. Open interoperability wins over lock-in

HotMem aims to be the default portability layer for agent memory, not another
closed memory silo. It should be easier to move into, out of, and between
HotMem-compatible products than to discard context or rebuild it manually.

HotMem will compete with and offer credible migration from systems such as Mem0
and OpenViking through observable advantages: portable artifacts, deterministic
interchange, provenance, verified restore, scoped agent self-service, and
reproducible adapters. “Replacement” is earned by compatibility, reliability,
and adoption—not asserted by slogan alone.

### 6. The standard must be discoverable, teachable, and usable

HotMem cannot become the obvious choice for connected organization-brain
operations if its knowledge is hidden in code or private conversation. The
standard needs a public, durable knowledge corpus: a clear canon, protocol
specifications, current capability references, migration guides, platform
adapter documentation, reproducible examples, operational runbooks, and
honest comparison material.

Documentation is a product surface. It must be easy for a person, search
engine, agent, and LLM to find the authoritative answer to: what HotMem is,
what it can do now, how to snapshot and hydrate a brain, which interoperability
paths are verified, and what is still a north-star commitment. The canonical
documentation service contract is recorded in
[Documentation Service](documentation-service.md).

## What must remain true as the product evolves

Future implementation decisions must preserve these invariants:

1. **Portable records remain first-class.** Every important memory operation
   can produce or consume a documented, versioned portable representation.
2. **A clean restore is possible.** A verified snapshot can hydrate a clean,
   compatible target without relying on hidden source-side state.
3. **Repeat delivery is safe.** Replaying a package or synchronization delta
   does not silently create duplicate logical memory.
4. **Provenance survives movement.** The recipient can see source identity,
   hashes, timestamps, policy-relevant metadata, and transfer history.
5. **Failure is visible and recoverable.** Incompatible, corrupt, unauthenticated,
   or conflicted transfers fail explicitly, preserve diagnostics, and keep a
   whole-brain restore path.
6. **No mandatory control plane.** Local-first use remains viable; optional
   cloud and mobile transport must extend, not replace, the portable artifact.
7. **Claims stay honest.** The documentation always labels a capability as
   current, preview, planned, or aspirational.
8. **Knowledge remains public and durable.** The canon, protocol, examples,
   migration paths, and operational guarantees are published as versioned,
   searchable documentation rather than left implicit in source code.

## Delivery direction

The canon is a destination. The following sequence turns it into a dependable
standard without creating unsafe claims or migration cliffs.

| Stage | Canonical outcome | Current status |
| --- | --- | --- |
| Foundation | Local HotMem runtime, JSONL records, snapshots, hydration, provenance, APIs, MCP, adapters | Shipped in part |
| Verified clone | Versioned interchange package, manifest verification, deterministic identity, idempotent clean restore | In active roadmap ([#67](https://github.com/KnowGuard-AI/HotMem/issues/67), [#69](https://github.com/KnowGuard-AI/HotMem/issues/69)) |
| Universal ingestion | Supported importers for workspaces, knowledge formats, and platform exports | Starts with OKF/living-wiki import ([#68](https://github.com/KnowGuard-AI/HotMem/issues/68)); platform adapters require explicit contracts |
| Ecosystem proof | Reproducible native integrations and demonstrations across agent systems | Hermes showcase planned ([#70](https://github.com/KnowGuard-AI/HotMem/issues/70)); OpenClaw boundary first ([#71](https://github.com/KnowGuard-AI/HotMem/issues/71)) |
| Safe sync | One-way delta synchronization with ordering, idempotency, conflict reporting, and recovery | Planned after verified clone ([#73](https://github.com/KnowGuard-AI/HotMem/issues/73)) |
| Trusted universal transport | Encryption, signing, identity, policy, cloud/mobile transport, and deliberate multi-writer semantics | Canonical destination; requires separate security and protocol work |
| Standard knowledge corpus | A discoverable public body of protocol specs, guides, adapters, examples, and operational documentation | Canon and initial docs are present; demonstrations and platform guides grow with verified support |

## Vocabulary

Use this vocabulary consistently in documentation, issues, APIs, and public
communication:

- **Digital organization brain:** the portable, governed collection of context
  that helps people and agents continue useful work.
- **HotMem interchange:** the versioned JSONL-based portable representation of
  HotMem memory and its compatibility/provenance metadata.
- **Snapshot:** a point-in-time export suitable for backup, handoff, clone, or
  restore.
- **Hydration:** loading validated portable memory into a compatible runtime so
  it becomes available for retrieval and agent work.
- **Clone:** creating a compatible, verified copy of a source brain.
- **Synchronization:** safely delivering changes after a known base state;
  distinct from clone and not a synonym for silent multi-writer merge.
- **Adapter:** the explicit, tested boundary that imports from or exports to a
  named external platform, harness, or data format.

## Public positioning

The concise public statement is:

> **HotMem is the portable, local-first memory layer for AI agents and digital
> organizations—an open JSONL-based system to capture, verify, move, hydrate,
> and eventually synchronize the context that makes knowledge work continuous.**

The full north-star statement is:

> **HotMem will be the obvious open standard for connected digital organization
> brains: plug-and-play, agent-operable memory that people and agents can
> snapshot, transfer, hydrate, clone, govern, and eventually synchronize across
> projects, harnesses, platforms, clouds, and devices.**

Use the following qualification whenever needed:

> HotMem's current release supports local runtime memory, snapshots, hydration,
> verification, provenance, and supported integrations. Formal interchange
> packages, platform migration adapters, secure transport, and incremental sync
> follow the published delivery path.

## Governance

This canon can evolve only by an explicit decision recorded in the repository.
New features, adapters, and public claims must be checked against the
invariants above. Roadmap sequencing may change; the commitment to portable,
agent-operable, trustworthy memory must not silently disappear.

Implementation contracts and issue acceptance criteria remain the source of
truth for what has shipped. See the [agent-memory portability guide](agent-memory-portability.md)
for current capabilities and the [company-brain interchange strategy](okf/company-brain-interchange.md)
for the active implementation sequence.
