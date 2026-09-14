# OKF: Open Knowledge Format Notes

Status: Accepted
Owner: HotMem maintainers
Last updated: 2026-09-14
Scope: OKF note index

## 1. Purpose

This section collects living development knowledge for HotMem. These documents
are intentionally practical and iterative. They capture current decisions,
heuristics, open questions, and compatibility practices before those ideas
harden into final API or format specifications.

## 2. Current Notes

- [Interchange Contract v1](interchange-v1.md) — normative `hotmem-interchange-v1`
  record, manifest, and hydration contract (#67).
- [Company-Brain Interchange Strategy](company-brain-interchange.md) — the
  accepted strategy behind #67–#73.
- [Format and Maintenance](format-and-maintenance.md)
- [File-Native Memory Practices](file-native-memory-practices.md)

`file-aware-architecture.md` and `file-native-epic.md` were removed from the
published tree by #82 and remain available in git history (`0f0b8da`) pending
re-review; nothing in the current issue set links to them.

## 3. Compatibility Rule

OKF docs may evolve quickly, but they should not erase useful prior thinking.
When a decision changes, update the document with the new decision and preserve
important rationale or superseded context where it helps future maintainers.

## 4. Publication Rule

These notes live in the repository for issue linkage and contributor context.
They are excluded from the generated documentation site (see `exclude_docs` in
`mkdocs.yml`); user-facing behavior is documented under `docs/` proper
(for example `docs/snapshot-v2.md`, `docs/agent-memory-portability.md`).
