# Decision: Keep extraction outside HotMem core

**Date:** 2026-11-18 — **Status:** Decided for WP4

## Decision

HotMem core stores and retrieves memories supplied by an agent or imported
from another memory system. It does not call an LLM to extract facts from
source text during ingest. The agent remains responsible for choosing what to
write.

An optional extractor may be proposed later as a separate adapter. It must be
off by default, make provider use explicit, and leave the canonical HotMem
record format provider-neutral. This decision does not add that adapter.

## Why

- HotMem's product direction centers on owner-controlled, portable memory and
  explicit boundaries around external providers; see
  [Vision and Canon](../vision-and-canon.md).
- WP3 measures retrieval from submitted records. It provides no evidence that
  adding extraction to core improves the supported use cases.
- The WP3 recommendation keeps entity extraction and other transformations as
  optional future work rather than core requirements; see
  [expanded benchmark evidence](https://github.com/KnowGuard-AI/HotMem/blob/735d78c/bench/retrieval/wp3-semantic.md).
- Importers translate an existing memory export into HotMem records. That
  migration mapping is distinct from generating new facts from arbitrary
  source material.

## Consequences

- Core ingest remains provider-free and predictable; users can inspect the
  records that agents chose to store.
- HotMem does not claim to extract facts from raw conversations or documents.
- Future extraction work needs a concrete workload, separate evidence, and an
  explicit optional adapter proposal. It does not silently enter core ingest.
