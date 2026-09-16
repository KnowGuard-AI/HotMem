# OKF: HotMem Session Handoff Contract v1

Status: Accepted (implementation contract; extension fields remain Proposed)
Owner: HotMem maintainers
Last updated: 2026-09-16
Scope: `hotmem-handoff-v1` — package layout, manifest schema, session stream,
resume brief, coverage/omission/redaction records, verification, hydration
semantics, and the source-adapter boundary

Implements: [#101](https://github.com/KnowGuard-AI/HotMem/issues/101).
Delivery contract: PR #107. Feeds: the `0.2.5` release gate.

The product promise is **verified continuity with preserved context**. It is
not native Codex or Claude transcript cloning. Every unsupported, redacted,
or provider-specific field must be visible to the operator through explicit
coverage, omission, and redaction records.

---

## 1. Scope and identity

HotMem is the portable intermediary: it owns the handoff representation,
integrity, hydration, and adapter seams. A handoff moves *useful working
context* — an ordered session stream, a bounded resume brief, and selected
durable memories — from a source agent session to a target session.

- Session history and durable memories stay distinct. The ordered stream
  lives in the package; the target database receives the memories plus
  exactly one resume-brief record. Nothing overloads the interchange
  memory-record semantics, and no second source of truth is introduced.
- Identity is content-derived: `package_id` is the SHA-256 over the sorted
  per-entry content hashes (the interchange `logical_id` algorithm), stable
  across re-prepares. `handoff_id` is a uuid minted per prepare operation and
  recorded in the manifest; idempotence and the hydration ledger key on
  `package_id`.
- Stable entry ids: `sha256("hotmem-handoff-entry:{adapter}:{session_id}:{source_entry_id}")`.
  The same source line always maps to the same package entry id.

## 2. Package layout

One directory, published atomically (staging + rename; a failed publish
never destroys a previous package):

```
<package>/
  manifest.json        # authoritative: identity, mode, counts, checksums, coverage
  session.jsonl        # ordered normalized entries, canonical JSONL, one per line
  memories.jsonl       # selected durable memories, hotmem-interchange-v1 records
  resume-brief.json    # bounded BriefDocument {text, sections, links, budgets}
```

## 3. Manifest schema

One JSON object, sorted keys. Fields are required, optional, or
forward-compatible (unknown keys tolerated; readers must not reject them).

| Class | Fields |
| --- | --- |
| Required | `format`, `schema_version`, `package_id`, `handoff_id`, `mode`, `created_at`, `hotmem_version`, `source`, `target`, `counts`, `files`, `coverage`, `limits` |
| Optional | `compatibility`, `consent` |
| Forward-compatible | any other key |

- `format`: `hotmem-handoff-v1`. Readers reject any other value.
- `schema_version`: `1`. Readers reject values above the highest version they
  understand and accept unknown additive fields.
- `mode`: `resume` or `archive` (§8). Never inferred at the target.
- `source`: `{adapter, adapter_version, session_id, label, time_range,
  selection_scope}`. `adapter` names the documented export format the
  package was prepared from (§12).
- `target`: `{adapter: "hotmem"}`.
- `counts`: `{entries, entries_by_kind, memories, brief_chars}`.
- `files`: `{name: {size, sha256}}` for every package file listed in §2.
  Manifest-listed paths must stay confined to the package root: no absolute
  paths, no `..` traversal, no symlink escapes (§10).
- `coverage`: the coverage/omission/redaction report (§7).
- `limits`: the effective `Limits` values the package was produced under.
- `consent`: `{given: true, statement, at}` — the recorded (truncated)
  explicit consent statement (§9). Required in practice: prepare refuses an
  empty consent before reading session content.

## 4. Session entry schema

One canonical JSON object per `session.jsonl` line.

| Class | Fields |
| --- | --- |
| Required | `schema_version`, `id`, `seq`, `kind`, `text` |
| Optional | `summary`, `role`, `created_at`, `source`, `artifacts`, `links`, `redacted`, `executable` |
| Forward-compatible | any other key |

- `kind`: one of `turn`, `decision`, `commitment`, `unresolved_question`,
  `next_action`, `tool_call`, `tool_result`, `file_reference`.
- `seq`: the source order, strictly increasing. Ordering is deterministic.
- `id`: the stable entry id (§1). Duplicate ids fail verification (§10).
- `source`: `{adapter, adapter_version, session_id, source_entry_id,
  source_seq}` — the provenance link back to the source line.
- `tool_call`/`tool_result` entries carry `executable: false`. Tool entries
  are inert data: nothing in HotMem ever dispatches them, and the resume
  brief renders them under an explicit historical-activity marker. "Replay"
  means inspectable history only — historical tools are never executed
  automatically.
- Bounds (`Limits`): `max_session_entries`, `max_entry_bytes`,
  `max_tool_result_bytes`. Bounds produce omission records, never crashes.

## 5. Durable memories

`memories.jsonl` holds canonical `hotmem-interchange-v1` records — unchanged
from the interchange contract. The source adapter derives deterministic ids
(hash of adapter/session/source id + content hash), sets
`source: "handoff:<adapter>"`, and records the handoff provenance block
under `provenance.handoff`. Embeddings are not carried by handoff packages;
hydration re-embeds under the target runtime's configured embedder through
the standard compatibility path, reusing compatible stored vectors when
present.

## 6. Resume brief

`resume-brief.json` is a `BriefDocument`: `{text, sections, links,
char_count}`.

- `text` is the bounded, target-friendly markdown a fresh session reads to
  continue the work: goal, decisions, commitments, unresolved questions,
  next actions, historical tool activity (marked "do not re-run"), file
  references, and the durable-memory index.
- Every listed item links its source entry id; the brief never duplicates the
  full stream.
- The brief respects `Limits.brief_char_budget`. Deterministic selection
  (kind priority, then recency) means the same package always yields the
  same brief.
- Redaction applies to brief text exactly as to entries (§7), and an output
  gate re-scans the final brief so redacted values can never leak.

## 7. Coverage, omissions, and redactions

The manifest `coverage` block is the operator-facing honesty layer:

```
{transferred, omitted: [OmissionRecord], redacted: [RedactionRecord],
 omitted_count, redacted_count, recoverable_count}
```

- `OmissionRecord`: `{where, field, reason, recoverable}` — what was left
  out (unsupported source field, oversize entry, resume-mode stream
  bounding, policy-denied source line) and whether the source still holds
  it. Never includes the omitted content.
- `RedactionRecord`: `{where, field, kind, reason, recoverable}` — the
  secret *kind* and location, never the value. Redaction is deny-by-default
  for credentials, tokens, hidden prompts, and unrelated content.
- Redaction is layered: (1) source fields never read — hidden prompts and
  credential-bearing source metadata are skipped and recorded; (2) secret
  patterns over all transferred text, replaced with `[REDACTED:<kind>]`;
  (3) an output gate that re-scans rendered briefs and error strings.
  Redacted values must never appear in the package, resume brief, logs, or
  error text.

## 8. Modes

- **resume** — the package is optimized for a clean target session to
  continue useful work quickly: a bounded resume brief plus the selected
  memories. The session stream is bounded too (deterministic head/tail/
  typed-significant windows) with omission records for everything dropped.
- **archive** — the full ordered history and provenance are preserved for
  audit or later inspection, within the global `Limits` caps.

Modes differ in package content and coverage, not in hydration semantics.
The mode is recorded in the manifest and cannot be inferred implicitly at
the target.

## 9. Consent and selection

Source selection and consent are explicit inputs. `prepare` requires a
non-empty consent statement, checked *before* session content is read, and
records it (truncated to `Limits.max_consent_chars`) with the selection
scope in the manifest. No surface (CLI, MCP, HTTP, library) may capture a
session implicitly: an absent consent fails closed with no package written.

## 10. Verification (fail-closed, before any target write)

`verify` is read-only and completes before hydration. It rejects:

- a missing or malformed manifest, a wrong `format`, a `schema_version`
  above the supported maximum, or an unknown `mode`;
- any manifest-listed path that is not confined to the package root
  (absolute paths, `..` traversal, symlink escapes);
- size or SHA-256 mismatches for any listed file;
- record-count mismatches (stream lines and memory lines vs manifest
  counts);
- duplicate entry ids, unknown entry kinds, or entries missing required
  fields;
- memory records that do not validate as interchange records;
- coverage arithmetic inconsistencies, or a missing brief in resume mode.

Failures carry structured diagnostics (`reason`, `file`, `expected`,
`actual`) and leave the target unchanged. Corrupt, incompatible, incomplete,
checksum-invalid, or path-escaping packages fail closed.

## 11. Hydration

- Hydration requires an explicit verified package and target database.
- Verify → ledger check → one transaction → commit. Any failure rolls back
  and the target remains state-equivalent (byte-for-byte where SQLite
  guarantees it).
- Insert-only: hydration never deletes, resurrects, or overwrites unrelated
  records. Unrelated target state is untouched.
- Idempotent: the `handoff_ledger` records `package_id` on first
  application; a repeated hydration of the same verified package writes
  nothing, emits no second event, and reports `already_applied`.
- The target receives the memories plus exactly one resume-brief record
  (a normal interchange record: `memory_type: "fact"`, identifier
  `handoff/<session-label>/resume-brief`, distinguishing data under
  `metadata.handoff` and `provenance.handoff`) so the brief is retrievable
  through the normal search path.
- An additive `handoff.applied` event is appended once per package.
- Known limitation (until #98 tombstone semantics land): a package record
  previously deleted at a used target will be re-inserted on hydration,
  because deletions are not tracked. Documented, never silent. The showcase
  targets a clean database.

## 12. Source-adapter boundary and support matrix

The `codex-export-v1` source is a **documented local export format** — an
envelope (`export.json`) plus an ordered JSONL session log (`session.jsonl`)
that an operator or adapter script produces from a Codex session. It is not
a claim that Codex exposes a native session-export API. When a provider
capability is unavailable, HotMem ships the adapter boundary and an explicit
limitation rather than simulating completeness.

Supported and tested: the `codex-export-v1` fixture adapter; fresh HotMem
SQLite targets ≥ 0.2.4 on Python 3.11–3.14; the existing Claude Desktop MCP
example as the target surface; local/file-native transfer only.

Not implied: native Codex/Claude transcript restoration, cloud transport,
encryption/authentication, multi-writer synchronization, or automatic tool
replay.

## 13. Compatibility policy

The handoff contract is additive to every existing surface: interchange
packages, deltas, Snapshot v2, the event log, and the MCP tool list remain
backwards compatible. New manifest/entry fields follow the
forward-compatible rule (§3/§4); golden fixtures prove round-trip
compatibility and stable serialization. The showcase must not create a
competing memory store or prematurely depend on later traversal APIs
(#105/#106) or the portable-workspace foundation (#94–#100).