# OKF: Portable Company Brain and Ecosystem Strategy

Status: Accepted
Owner: HotMem maintainers
Last updated: 2026-07-30
Scope: Portable memory interchange, company-brain cloning, and ecosystem demonstrations

## 1. Purpose

HotMem will treat JSONL as its canonical memory interchange stream. A small,
versioned, checksummed, compressed HotMem package will make that stream portable
enough to hydrate a clean HotMem instance into a verified copy of a company
brain.

This direction makes HotMem the local-first runtime for agent memory that can
be inspected, transferred, restored, and integrated without requiring a
proprietary hosted control plane.

## 2. Strategic Decision

The intended model is:

```text
knowledge source or agent memory
  -> deterministic importer or HotMem dump
  -> versioned JSONL interchange stream
  -> checksummed compressed package
  -> verify and hydrate
  -> runnable HotMem company-brain clone
```

JSONL is the protocol layer. Compression is a transport concern. A manifest is
the trust and compatibility layer. The SQLite mount remains HotMem's local
runtime store.

The first release is a trusted local transfer and clone workflow. It must not
claim encryption, signing, remote synchronization, or multi-writer conflict
resolution until those capabilities are explicitly designed and implemented.

## 3. Compatibility Commitments

- Existing JSONL and JSONL.GZ hydrate and snapshot workflows remain supported.
- New package metadata is additive and versioned.
- Hydration is idempotent and reports loaded, skipped, and invalid records.
- Content hashes, source identity, source paths or URIs, and provenance remain
  available after import and hydration.
- Stored embeddings are reused only when compatible. Otherwise, HotMem may
  re-embed from the canonical record content.
- The core remains local-first, SQLite-backed, and free of required external
  services or storage engines.

## 4. Input Formats

The first importer targets are:

1. Google Open Knowledge Format v0.2 bundles: Markdown, YAML frontmatter,
   hierarchy, links, provenance, freshness, and generated `index.md` files.
2. Karpathy-style living Markdown wikis: an index, compact linked pages, and
   a clear separation between preserved source material and compiled knowledge.
3. Existing HotMem JSONL, JSONL.GZ, and supported snapshot artifacts.

Importers run locally. They preserve source identity and hashes, do not fetch
remote links, and reject paths that escape the selected bundle root. Imported
material is evidence for memory retrieval, not an automatic declaration that
every source claim is verified or safe to act on.

## 5. Ecosystem Demonstrations

### Hermes Agent

`hotmem-hermes` is the reference deep integration. The showcase should prove
that HotMem persists and retrieves useful memory across the Hermes lifecycle,
then survives a dump and hydrate cycle. It should use the existing memory
provider contract rather than introduce a second Hermes integration path.

### OpenClaw

OpenClaw currently has a Markdown workspace-memory model and pluggable memory
engines. The first HotMem issue is a compatibility spike that chooses and
documents the supported boundary: workspace import and sidecar retrieval,
memory-plugin integration, or a wiki bridge. No public claim of native
replacement should be made before that spike is complete.

## 6. Delivery Sequence

1. Define the interchange and clone-package contract, including schema,
   manifest, integrity, provenance, and compatibility tests.
2. Build the OKF and living-wiki importer to deterministic JSONL.
3. Build and verify the dump, compressed package, and clean-instance hydrate
   workflow.
4. Publish the Hermes reference showcase.
5. Complete the OpenClaw compatibility spike, then publish the chosen
   integration showcase.
6. Design incremental sync only after the clone workflow is stable. It needs
   an explicit delta, ordering, provenance, and conflict contract.

## 7. Boundaries and Open Questions

- HotMem is not becoming a remote data lake, distributed database, or required
  vector service.
- Relational, vector, cache, and data-lake integrations belong behind optional
  adapters. They must not replace the canonical local memory record or make
  external services mandatory.
- The documented Snapshot v2 directory contract and the active hydrate and
  snapshot implementation must be verified together before the clone package
  is marketed as a stable public contract.
- Incremental synchronization needs a deliberate conflict policy. Whole-brain
  clone and restore ship first.

## 8. Issue Relationship

GitHub issues own implementation scope and acceptance criteria. The current
delivery set is:

1. [#67 Interchange and clone-package contract](https://github.com/KnowGuard-AI/HotMem/issues/67)
2. [#68 OKF and living-wiki import](https://github.com/KnowGuard-AI/HotMem/issues/68)
3. [#69 Verified portable company-brain dump and hydrate](https://github.com/KnowGuard-AI/HotMem/issues/69)
4. [#70 Hermes Agent reference showcase](https://github.com/KnowGuard-AI/HotMem/issues/70)
5. [#71 OpenClaw compatibility spike](https://github.com/KnowGuard-AI/HotMem/issues/71)
6. [#72 OpenClaw reference showcase](https://github.com/KnowGuard-AI/HotMem/issues/72)
7. [#73 Incremental company-brain synchronization](https://github.com/KnowGuard-AI/HotMem/issues/73)

Update this note when an issue changes the direction or when an implementation
contract becomes public behavior.
