# RFC 0001: File-Aware Memory Sidecar

Status: Draft
Tracks: #35
Related: #36 #37 #38 #39 #40 #41 #42 #43

## 1. Goal

Evolve HotMem from a JSON memory store into a **file-aware, provenance-first
memory sidecar** while preserving its core principles:

- local-first
- extremely lightweight
- deterministic
- embeddable
- language agnostic
- no heavy analytical engine
- optimized for fast memory ingestion and hydration

HotMem remains the **runtime memory device for EMOS**, not the analytical or
storage engine.

## 2. HotMem-owns / EMOS-owns boundary

HotMem owns:
- hot memory, memory API, memory schema
- snapshots, hydration, provenance
- storage abstraction (interface only)
- event log, promotion signalling

EMOS owns:
- Parquet, Arrow, DuckDB, Polars
- HDFS, S3, object storage, data lake
- analytical execution, compaction
- memory hierarchy, lineage reconstruction

HotMem must NOT become DuckDB/Polars/a data lake, manage Parquet partitions,
execute analytical workloads, or own object-storage orchestration.

## 3. Memory Record v2

### 3.1 v0.1 → v2 field mapping

| v0.1 column | v2 field | Notes |
|---|---|---|
| id | id | unchanged |
| identifier | identifier | unchanged |
| fact_text | content | renamed in payload only; column stays `fact_text` for compat |
| embedding | embedding | unchanged |
| embedding_dim | embedding_dim | unchanged |
| embedding_model | embedding_model | unchanged |
| source | source | kept (free-text legacy); superseded by `source_uri` |
| importance | importance | unchanged |
| metadata_json | metadata_json | unchanged |
| content_hash | content_hash | unchanged |
| ttl_seconds | ttl_seconds | unchanged |
| created_at | created_at | pre-existing (owned by #34 for surfacing, not #36) |
| — | namespace | new |
| — | tier | new (default `hot`) |
| — | memory_type | new (default `fact`) |
| — | source_uri | new (provenance) |
| — | source_format | new |
| — | source_checksum | new |
| — | byte_offset | new (file ref) |
| — | byte_length | new (file ref) |
| — | updated_at | new (#36 owns this; `created_at` already exists) |
| — | snapshot_id | new |
| — | promotion_state | new (default `HOT`) |
| — | promotion_candidate | new (default 0) |
| — | parent_memory | new |
| — | related_memories | new (JSON array string) |
| — | tags | new (JSON array string) |
| — | schema_version | new (default 1; DB `user_version` pragma = 2) |

### 3.2 Compatibility rules

- All new columns are **additive** with safe defaults; v0.1 DBs open unchanged.
- `fact_text` column name is retained (payload alias `content` is a future API concern, not schema).
- `source` (singular) is retained for legacy callers; `source_uri` is the v2 provenance field.
- `created_at` is **not** a #36 addition; #34 owns its surfacing and retrieval.
- DB-level version tracked via `PRAGMA user_version` (1 → 2).

## 4. Storage Adapter

### 4.1 Interface

```python
class StorageAdapter(Protocol):
    def read(self, uri: str) -> bytes: ...
    def read_range(self, uri: str, offset: int, length: int) -> bytes: ...
    def exists(self, uri: str) -> bool: ...
    def metadata(self, uri: str) -> dict: ...  # size, mtime, format
    def checksum(self, uri: str) -> str: ...   # sha256 hex
```

### 4.2 Scheme registry

A literal dict in `storage/__init__.py` maps scheme → adapter. Unknown
schemes raise an explicit error: "scheme `<x>` is owned by EMOS, not HotMem".

### 4.3 Initial implementation

Local filesystem only (`file://` and bare paths). Range reads use seek+read
(no full-file load); checksum is sha256 with `lru_cache`; metadata infers
format from extension. Optional mmap path for large files.

S3/HDFS/Azure/GCS are **future** and out of scope — EMOS owns distributed
storage; HotMem only understands the abstraction.

## 5. Snapshot v2

### 5.1 Layout

```
snapshot/
  manifest.json
  memories.jsonl
  attachments/
  metadata.json
```

### 5.2 Manifest schema

```json
{
  "schema_version": 2,
  "snapshot_id": "<uuid>",
  "created_at": "<iso8601>",
  "hashes": { "memories.jsonl": "<sha256>", "metadata.json": "<sha256>" },
  "overall_checksum": "<sha256>",
  "file_references": [ { "uri": "...", "offset": 0, "length": 1024, "checksum": "..." } ],
  "attachment_metadata": [ { "name": "...", "size": 0, "checksum": "..." } ],
  "counts": { "memories": 0 }
}
```

### 5.3 Compatibility

- `hotmem snapshot`/`hydrate` keep flags; v2 directory is the new default.
- Hydrate reads legacy single-file `swap.jsonl` (plain + stored-embedding,
  per #25) unchanged — path heuristic: directory → v2, `.jsonl[.gz]` → legacy.
- Manifest checksums verified on hydrate; mismatch is a hard error.
- Deterministic ordering/hashing for identical input.

## 6. Event Log

### 6.1 Vocabulary

`MemoryAdded`, `MemoryUpdated`, `MemoryMerged`, `MemoryArchived`,
`SnapshotCreated`, `PromotionRequested`.

### 6.2 Shape

Each event: id, type, timestamp, target memory id(s), before/after payloads
as needed, monotonic sequence number.

### 6.3 Storage + replay

- Append-only (SQLite table or JSONL under mount); no in-place
  update/delete.
- `GET /v1/events` with cursor + limit (tail-style).
- Replay reconstructs memory state deterministically (recovery/sync, not the
  hot read path).
- A baseline snapshot can seed the log on first vNext boot (documented here,
  implemented in #41).

## 7. Promotion Lifecycle

### 7.1 State machine

```
HOT ──► READY ──► PROMOTED ──► ARCHIVED
```

- Default state `HOT`.
- `promotion_candidate` flag + heuristics (age, importance, tier, size) mark
  `READY` without promoting.
- `POST /v1/promote` transitions state and emits `PromotionRequested`; HotMem
  records the transition, **EMOS performs the actual tier movement**.
- Archive is a state, not deletion (v0.1 has no delete); archived memories
  excluded from default search, retrievable via audit/full profiles.
- Provenance preserved across all transitions.

## 8. Hydration Profiles

| Profile | Context | Metadata | Provenance | Files |
|---|---|---|---|---|
| agent | small/summarized, bounded | core only | none | none |
| compact | minimal | minimal | none | none |
| audit | full | full | full | refs only |
| full | complete | full | full | refs + payload (lazy) |

Default (no profile) == current v0.1 payload shape.

## 9. API Extensions

- `GET /v1/files` — list file references (uri, format, size, checksum,
  referencing memory ids). Read-only.
- `POST /v1/export` — JSONL and JSON (Arrow/Parquet future; unsupported
  formats error explicitly). Export-only, no querying.
- `GET /v1/memory/{id}` — full v2 record.
- `POST /v1/promote` — promotion transition (EMOS performs movement).
- `GET /v1/events` — append-only event tail (cursor + limit).
- Hydrate gains `profile=`, `include_files=`, `include_provenance=`.

## 10. Backwards Compatibility

- v0.1 schema opens and serves without manual intervention (additive ALTERs).
- Legacy `swap.jsonl` (plain + stored-embedding) hydrates identically.
- Existing `/v1/add` and `/v1/search` payloads unchanged for callers that
  don't set v2 fields.
- FTS5 triggers + indexes remain functional.
- No new runtime dependencies (stdlib only for M0/M1).

## 11. Trade-offs

1. **Reference vs copy.** File-backed memories reference (URI + offset +
   length + checksum) rather than duplicate. Trade-off: hydration requires the
   backing file present and checksum-stable; loss of the file means loss of
   hydration, not loss of the memory record.
2. **mmap vs explicit range reads.** mmap is fast for repeated access but
   couples memory lifetime to file lifetime and risks SIGBUS on truncation.
   Decision: range reads by default; mmap optional behind a flag for known
   stable local files.
3. **Event log growth.** Append-only log grows unboundedly. Trade-off:
   replay/sync value vs storage cost. Decision: log is durable under the mount;
   compaction (snapshot-seed + truncate) is a future concern, owned here as a
   design note, not implemented in M0/M1.
4. **Manifest cost.** v2 snapshot adds a manifest + per-file checksums vs the
   single-file legacy format. Trade-off: portability/replay vs size. Decision:
   directory format is the new default; legacy reader preserved so cost is
   opt-in by choosing the new format.
5. **`source` vs `source_uri`.** Keeping the legacy free-text `source` column
   avoids a breaking rename but leaves two provenance-ish fields. Trade-off:
   compat vs clarity. Decision: keep both; `source_uri` is canonical in v2,
   `source` is legacy-superseded and documented as such.

## 12. Roadmap (milestones → tickets)

| Milestone | Ticket | Title |
|---|---|---|
| M0: Design | #35 | RFC: File-Aware Memory Sidecar (this doc) |
| M1: Foundations | #36 | Memory Record v2 schema + provenance + migration |
| M1: Foundations | #37 | Storage Adapter abstraction + local FS impl |
| M2: File Awareness | #38 | File-backed memories (URI + range + checksum hydration) |
| M2: File Awareness | #39 | Snapshot v2 directory format |
| M3: Hydration + API | #40 | Hydration Profiles |
| M3: Hydration + API | #43 | API extensions |
| M4: Lifecycle | #41 | Append-only Event Log + /v1/events |
| M4: Lifecycle | #42 | Promotion lifecycle |

## 13. M0/M1 testing strategy (inherited by later milestones)

- **#36:** migration opens v0.1 fixture DB → serves rows with defaults;
  write/read round-trip preserves all v2 fields incl. provenance; legacy
  callers behave identically to v0.1; full existing suite green.
- **#37:** read, read_range (slice correctness vs full read), checksum
  determinism + cache, unsupported-scheme error, range-beyond-EOF.
- Shared: round-trip snapshot↔hydrate, legacy-format compat, event replay
  determinism, range-read checksum verification (landed by their owning
  tickets).
