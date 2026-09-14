# OKF: HotMem Interchange Contract v1

Status: Accepted (implementation contract; extension fields remain Proposed)
Owner: HotMem maintainers
Last updated: 2026-09-14
Scope: `hotmem-interchange-v1` — canonical record stream, package manifest,
integrity, embedding compatibility, and hydration semantics

Implements: [#67](https://github.com/KnowGuard-AI/HotMem/issues/67).
Feeds: [#68](https://github.com/KnowGuard-AI/HotMem/issues/68),
[#69](https://github.com/KnowGuard-AI/HotMem/issues/69).

JSONL is the canonical stream format. Compression is a transport concern. The
manifest is the integrity and compatibility layer. Logical identity is derived
from content, never from compression, record order, export timestamps, or
derived embeddings.

---

## 1. Record schema

One JSON object per line. Fields are **required**, **conditionally required**,
**optional**, or **forward-compatible** (unknown keys).

| Class | Fields |
| --- | --- |
| Required | `schema_version`, `id`, `identifier`, `content_hash`, `memory_type` |
| Conditionally required | `fact_text` — non-inline records without a usable `fact_summary`; `source_uri`, `byte_offset`, `byte_length` — `memory_type: "file"` records |
| Optional | `fact_summary`, `source`, `importance`, `metadata`, `ttl_seconds`, `created_at`, `namespace`, `tier`, `tags`, `source_format`, `source_checksum`, `provenance`, `embedding`, `embedding_dim`, `embedding_model` |
| Forward-compatible | any other top-level key |

### 1.1 Field semantics

- `schema_version`: `1`. Readers must reject records claiming a *major*
  version they do not understand and must accept unknown *additive* fields.
- `id`: record identity within a stream. Importers derive it deterministically
  (for example the OKF importer hashes `concept_id + content_hash`); the
  runtime dump uses the stored row id. `id` is **not** part of logical
  identity — `content_hash` is.
- `identifier`: the logical name the memory is filed under.
- `content_hash`: `SHA-256("<identifier>:<fact_text>")` hex. Two records with
  equal `content_hash` are the same memory; hydration keeps the first and
  reports the rest as duplicates.
- `memory_type`: `"fact"` (inline content) or `"file"` (byte-range reference).
  Readers treat any value other than `"file"` as inline.
- `fact_text` / `fact_summary`: prompt-ready content, or its summary when only
  a reference is carried.
- `embedding` / `embedding_dim` / `embedding_model`: base64 float32 blob plus
  its model and dimension — see §5.
- `metadata`: free-form JSON object preserved verbatim on round-trip.
- `provenance`: JSON object of source-originating provenance (for example OKF
  `generated`, `verified`, `sources`, `usage_window`).
- `namespace`, `tier`, `tags`, `source`, `importance`, `ttl_seconds`,
  `created_at`: retrieval and access metadata preserved on round-trip.
- `source_uri`, `source_format`, `source_checksum`, `byte_offset`,
  `byte_length`: file-backed reference metadata. External references are
  **never** dereferenced, fetched, or verified during export or hydration.

### 1.2 Forward compatibility

- Readers MUST NOT reject a record for unknown top-level keys, unknown
  `memory_type` values (treated as inline), or missing optional fields.
- HotMem preserves unknown top-level keys on hydrate under
  `metadata["_interchange_unknown"]` so a round-trip never silently drops
  producer fields.
- Runtime lifecycle state (promotion state, event-log position, index markers)
  is **not** part of the canonical record and is not exported.

## 2. Canonical serialization

Applies to **new** artifacts produced by this contract (interchange packages
and importer output). Historical Snapshot v2 and legacy swap serialization
remain readable unchanged; digests always cover the bytes actually written, so
older artifacts keep verifying.

- UTF-8, LF line endings, exactly one JSON object per line, no blank lines.
- Payload records are sorted by `id`.
- Serialization: `json.dumps(record, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False, allow_nan=False, default=str)`.
- Embeddings are base64 text, so no float formatting appears in record bytes.

## 3. Identity and digests

Three digest layers, computed in this order:

1. **Record**: `content_hash` (above) — per-record logical identity and the
   deduplication key.
2. **Logical** (`logical_id` in the manifest): `SHA-256` of the concatenation
   of all record `content_hash` values in sorted order. This is the same
   algorithm as Snapshot v2 `snapshot_id`
   (`hotmem.snapshot.format.compute_snapshot_id`); equivalent contents always
   produce an equal `logical_id`.
3. **File**: per-file `{size, sha256}` of the bytes actually stored, plus
   `decompressed_sha256` for compressed payloads.

Logical identity **excludes**: compression, record order in the payload,
export timestamps, host, tool version, and derived embeddings. Two packages
with the same `logical_id` are interchangeable regardless of how they were
compressed or when they were exported.

## 4. Compression and transport

- The standard transfer encoding is `memories.jsonl.gz`: gzip, `mtime=0`,
   empty embedded filename, fixed compresslevel (9), deflate.
- Gzip bytes are transport. Because compressed bytes are not stable across
   zlib builds, verification hashes the **decompressed** stream against
   `decompressed_sha256`; the file `sha256` covers the stored bytes as-is.
- Plain `memories.jsonl` is always valid.

## 5. Embedding compatibility

A stored embedding is reused as-is **iff all** hold:

1. `embedding_model` equals the reader's current model (`hotmem-hash-v1`),
2. `embedding_dim` equals the current dimension (64),
3. the base64 blob decodes and its length is exactly `embedding_dim * 4`.

Otherwise the record is re-embedded from `fact_text` (inline) or
`fact_summary` (file). If no usable text exists, the record is counted
`invalid` and skipped — never silently stored with a foreign embedding.
Embeddings are derived data: they are never part of logical identity and can
always be rebuilt from canonical text.

## 6. Manifest

A package is a directory:

```
<package>/
  manifest.json
  memories.jsonl | memories.jsonl.gz
```

Normative fields (readers MUST understand these):

```json
{
  "format": "hotmem-interchange-v1",
  "schema_version": 1,
  "record_count": 42,
  "logical_id": "<sha256 of sorted content_hash concatenation>",
  "files": {
    "memories.jsonl": {"size": 12345, "sha256": "..."}
  },
  "source": {"kind": "hotmem-dump"},
  "embedding": {"model": "hotmem-hash-v1", "dim": 64}
}
```

- `files` entries for compressed payloads additionally carry
  `decompressed_sha256`.
- `source.kind` ∈ `hotmem-dump | okf-import | wiki-import`.
- `created_at` and `hotmem_version`, when present, are **informational** and
  excluded from logical identity (§3).

### 6.1 Proposed fields (NOT emitted — pending contract review per ADR-003)

The following are reserved names, documented so producers do not collide with
them, but this contract version does not define or emit them:
`transfer_provenance` (import timestamps, transfer chain), `attachments`
(in-package attachment manifest), `namespaces` (per-namespace scoping and
counts), `delta` (incremental sync descriptors), `encryption` / `signing`.
Until the contract review lands, runtime timestamps stay in the append-only
event log (`occurred_at`) and attachment portability stays with Snapshot v2.

## 7. Hydration semantics

- `loaded`: records inserted.
- `skipped_dupes`: records whose `content_hash` already exists in the target
  (or earlier in the same payload). Idempotent: re-hydrating a package into
  the same instance reports `loaded == 0`.
- `invalid`: records that parsed but failed validation and were **not**
  inserted (for example: no usable text and no compatible embedding).
- **Hard failures** — hydration halts and the target remains unchanged:
  missing manifest or payload, size/digest/record-count mismatch, truncated
  or corrupted compression, unparseable JSON line, manifest path escaping the
  package directory, and `schema_version` above the reader's major.
- Verification always completes **before** the first target write.
- Package restore is transactional: either every valid, non-duplicate record
  is committed or nothing is.

## 8. Timestamps and provenance

- Canonical record bytes carry only **source-originating** time: `created_at`
  and any source provenance (OKF `generated.at`, `verified[].at`,
  `sources[].last_modified`, `usage_window`).
- **Runtime timestamps** (when an export or import happened) are transfer
  provenance: they live in the append-only event log (`occurred_at`) and
  manifest informational fields — never inside canonical record bytes.
  This is what makes importer output byte-stable across runs.

## 9. Portable attachments vs external references

- v1 packages carry inline records only. File-backed records keep their
  `source_uri` as an **external reference**: hydration preserves the
  reference and never reads, writes, fetches, or verifies the external file.
- Content-addressed in-package attachments remain a Snapshot v2 capability
  (opt-in `--attach`); an interchange `attachments` manifest section is
  Proposed (§6.1).
- Every manifest-listed path is confinement-checked before it is read:
  no absolute paths, no `..` traversal, no symlink escapes.

## 10. Relationship to existing formats

- Legacy swap JSONL/GZ: record key `embedding_b64`; supported unchanged.
- Snapshot v2 directories: record key `embedding`; manifest `format` is
  `hotmem-snapshot-v2`; supported unchanged.
- The shared reader path normalizes both spellings and applies one embedding
  compatibility rule (§5) across all formats.
- A directory whose `manifest.json` declares `hotmem-interchange-v1` is an
  interchange package; `hotmem-snapshot-v2` selects the v2 reader.

## 11. Conformance tests required by this contract

Deterministic output (same source ⇒ identical logical bytes), old-format
fixtures (plain JSONL, `embedding_b64`, v2 `embedding`, `.gz`), malformed
frontmatter, symlink escapes, corrupted and truncated packages, incompatible
embeddings, provenance preservation, and duplicate/conflicting identities —
see `tests/` (interchange, OKF, package, and end-to-end clone suites).
