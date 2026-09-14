"""HotMem swap - JSONL hydration and snapshot.

Purpose:
    Load memories from a swap file (JSONL) into the database, and export
    the current database state back to a swap file. Deduplicates on content_hash.

    Hydration runs every record through the shared interchange normalization
    (hotmem.interchange, issue #67): all v2 fields survive the round-trip,
    one embedding-compatibility rule applies, and parsing is bounded-batch
    with database-backed deduplication.

Interface:
    hydrate(db, swap_path) -> HydrateResult
    snapshot(db, swap_path, include_embeddings=True) -> SnapshotResult
    compute_content_hash(identifier, fact_text) -> str   (re-exported)

Deps: hotmem.db, hotmem.embed, hotmem.interchange, hotmem.trace
Extension: add compression, encryption, or remote swap sources here.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from hotmem.db import MemoryDB, MemoryRecord
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.compat import resolve_embedding
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.trace import Timer, get_tracer

__all__ = [
    "HydrateResult",
    "SnapshotResult",
    "add_memory",
    "compute_content_hash",
    "hydrate",
    "snapshot",
    "write_record",
]

_trace = get_tracer("swap")


@dataclass
class HydrateResult:
    loaded: int
    skipped_dupes: int
    invalid: int = 0


@dataclass
class SnapshotResult:
    exported: int
    path: str


def _swap_format(path: Path) -> str:
    """Return the supported swap format for path, or raise a clear error."""
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if suffixes[-1:] == [".jsonl"]:
        return "jsonl"
    if suffixes[-2:] == [".jsonl", ".gz"]:
        return "jsonl.gz"

    supported = ".jsonl, .jsonl.gz"
    raise ValueError(f"unsupported swap file extension for {path}; supported: {supported}")


@contextmanager
def _open_swap_read(path: Path) -> Iterator[TextIO]:
    swap_format = _swap_format(path)
    if swap_format == "jsonl.gz":
        with gzip.open(path, "rt") as f:
            yield f
    else:
        with open(path) as f:
            yield f


@contextmanager
def _open_swap_write(path: Path) -> Iterator[TextIO]:
    swap_format = _swap_format(path)
    if swap_format == "jsonl.gz":
        with gzip.open(path, "wt") as f:
            yield f
    else:
        with open(path, "w") as f:
            yield f


# Hydration flushes in bounded batches: bounded memory + batched (database-backed)
# dedup instead of loading the entire destination hash set (interchange #67).
_HYDRATE_BATCH = 1000


def record_to_memory_record(
    rec: dict,
    blob: bytes,
    *,
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
) -> MemoryRecord:
    """Build a db-ready MemoryRecord from a normalized interchange record.

    Every canonical field survives: namespace, tier, tags, provenance,
    fact_summary, file-backed references, and lifecycle columns that legacy
    writers emit — previously dropped by this path (#67 field preservation).
    Shared with the Snapshot v2 reader (issue #67) so both build rows
    identically. ``embedding_model``/``embedding_dim`` override the record's
    recorded pair when resolve_embedding decided a re-embed (§5: re-embedded
    rows carry the CURRENT model, never the stale recorded one).
    """
    return MemoryRecord(
        id=rec["id"],
        identifier=rec["identifier"],
        fact_text=rec["fact_text"],
        embedding=blob,
        embedding_dim=embedding_dim if embedding_dim is not None else rec["embedding_dim"],
        embedding_model=embedding_model if embedding_model is not None else rec["embedding_model"],
        source=rec["source"],
        importance=rec["importance"],
        metadata_json=json.dumps(rec["metadata"]),
        content_hash=rec["content_hash"],
        ttl_seconds=rec["ttl_seconds"],
        created_at=rec["created_at"],
        namespace=rec["namespace"],
        tier=rec["tier"],
        memory_type=rec["memory_type"],
        source_uri=rec["source_uri"],
        source_format=rec["source_format"],
        source_checksum=rec["source_checksum"] or "",
        byte_offset=rec["byte_offset"],
        byte_length=rec["byte_length"],
        updated_at=rec["updated_at"],
        snapshot_id=rec["snapshot_id"],
        promotion_state=rec["promotion_state"] or "HOT",
        promotion_candidate=rec["promotion_candidate"] or 0,
        parent_memory=rec["parent_memory"],
        related_memories=json.dumps(rec["related_memories"]),
        tags=json.dumps(rec["tags"]),
        schema_version=rec["schema_version"],
        fact_summary=rec["fact_summary"],
        provenance_json=json.dumps(rec["provenance"]) if rec["provenance"] else None,
    )


def _flush_batch(
    db: MemoryDB,
    pending: list[dict],
    *,
    counters: dict,
) -> None:
    """Resolve embeddings and insert one bounded batch; update counters in place.

    Database-backed dedup: one chunked SELECT finds destination duplicates
    before any embedding work, then insert_many_ignore handles residual
    races. loaded/skipped/reused/computed counters live in ``counters``.
    """
    existing = db.batch_existing_hashes([r["content_hash"] for r in pending])
    todo = [r for r in pending if r["content_hash"] not in existing]
    counters["skipped"] += len(pending) - len(todo)

    records: list[MemoryRecord] = []
    for rec in todo:
        blob, model, dim, reused = resolve_embedding(rec, embed_fn=embed_text)
        if reused:
            counters["reused_embeddings"] += 1
        else:
            counters["computed_embeddings"] += 1
        records.append(record_to_memory_record(rec, blob, embedding_model=model, embedding_dim=dim))

    loaded = db.insert_many_ignore(records)
    counters["loaded"] += loaded
    counters["skipped"] += len(records) - loaded


def hydrate(
    db: MemoryDB,
    swap_path: str | Path,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> HydrateResult:
    """Load memories from a swap file into the database.

    Accepts JSONL, JSONL.GZ, or a HotMem SQLite database (.sqlite/.db).
    Deduplicates by content_hash - skips rows that already exist in the DB.
    For SQLite sources, embeddings are reused as-is (fast-path, no recompute).

    Every record goes through the shared interchange normalization (issue
    #67): all v2 fields (namespace, tier, tags, provenance, fact_summary,
    file references) survive the round-trip, embeddings are reused only when
    compatible, and parsing is bounded-batch with database-backed dedup.

    on_progress, if given, is invoked once per parsed line with the byte
    length of that line — enabling byte-based progress reporting without
    coupling swap.py to any UI library.
    """
    swap_path = Path(swap_path)
    if not swap_path.exists():
        _trace.warn("hydrate", "swap file not found", detail={"path": str(swap_path)})
        return HydrateResult(loaded=0, skipped_dupes=0)

    if swap_path.suffix.lower() in (".sqlite", ".db"):
        loaded, skipped = db.import_sqlite(swap_path)
        return HydrateResult(loaded=loaded, skipped_dupes=skipped)

    with Timer() as t:
        counters = {
            "loaded": 0,
            "skipped": 0,
            "invalid": 0,
            "parsed": 0,
            "bytes_read": 0,
            "reused_embeddings": 0,
            "computed_embeddings": 0,
        }
        pending: list[dict] = []
        batch_seen: set[str] = set()

        def flush() -> None:
            if pending:
                _flush_batch(db, pending, counters=counters)
                pending.clear()
                batch_seen.clear()

        try:
            with _open_swap_read(swap_path) as f:
                for line in f:
                    line_bytes_len = len(line.encode())
                    counters["bytes_read"] += line_bytes_len
                    line = line.strip()
                    if not line:
                        if on_progress is not None:
                            on_progress(line_bytes_len)
                        continue
                    record = json.loads(line)
                    counters["parsed"] += 1
                    if on_progress is not None:
                        on_progress(line_bytes_len)

                    if not isinstance(record, dict):
                        counters["invalid"] += 1
                        continue
                    rec = normalize_record(record)
                    if validate_record(rec):
                        counters["invalid"] += 1
                        continue

                    content_hash = rec["content_hash"]
                    if content_hash in batch_seen:
                        counters["skipped"] += 1
                        continue
                    batch_seen.add(content_hash)
                    pending.append(rec)
                    if len(pending) >= _HYDRATE_BATCH:
                        flush()
        except (EOFError, OSError) as err:
            if _swap_format(swap_path) == "jsonl.gz":
                raise ValueError(f"malformed compressed swap file: {swap_path}") from err
            raise

        flush()
        loaded = counters["loaded"]
        skipped = counters["skipped"]
        invalid = counters["invalid"]

    _trace.info(
        "hydrate",
        f"hydrated {loaded} memories, skipped {skipped} dupes, {invalid} invalid",
        detail={
            "path": str(swap_path),
            "ms": round(t.ms, 2),
            **{k: counters[k] for k in counters},
        },
    )
    return HydrateResult(loaded=loaded, skipped_dupes=skipped, invalid=invalid)


def write_record(f: TextIO, record: dict) -> None:
    """Serialize one swap record to a JSONL file handle.

    Centralizes the swap-record serialization contract (json + default=str +
    trailing newline) so callers (snapshot, import) cannot drift apart.
    """
    f.write(json.dumps(record, default=str) + "\n")


def add_memory(
    db: MemoryDB,
    identifier: str,
    fact: str,
    *,
    source: str = "",
    importance: float = 0.5,
    metadata: dict | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, str]:
    """Insert one memory into the DB using the canonical add contract.

    Centralizes the uuid → content_hash → embed → pack → insert sequence so
    callers (server, mcp, playground, examples) cannot drift apart. Returns
    (memory_id, content_hash).
    """
    import uuid

    memory_id = uuid.uuid4().hex
    content_hash = compute_content_hash(identifier, fact)
    vec = embed_text(fact)
    blob = pack_embedding(vec)
    db.insert(
        id=memory_id,
        identifier=identifier,
        fact_text=fact,
        embedding=blob,
        embedding_dim=EMBEDDING_DIM,
        embedding_model=EMBEDDING_MODEL,
        source=source,
        importance=importance,
        metadata_json=json.dumps(metadata or {}),
        content_hash=content_hash,
        ttl_seconds=ttl_seconds,
    )
    return memory_id, content_hash


def snapshot(
    db: MemoryDB,
    swap_path: str | Path,
    *,
    include_embeddings: bool = True,
    on_progress: Callable[[int], None] | None = None,
) -> SnapshotResult:
    """Export all memories from the database to a JSONL or JSONL.GZ swap file.

    Streams rows in id order (fetchmany batches) so memory stays O(batch)
    for large stores (interchange #67). on_progress, if given, is invoked
    once per exported row with the count of rows written so far (cumulative).
    """
    swap_path = Path(swap_path)

    with Timer() as t:
        exported = 0
        with _open_swap_write(swap_path) as f:
            for row in db.iter_rows(include_embedding=include_embeddings):
                embedding = row.pop("embedding", None)
                if embedding is not None:
                    row["embedding_b64"] = base64.b64encode(embedding).decode("ascii")
                write_record(f, row)
                exported += 1
                if on_progress is not None:
                    on_progress(exported)

    _trace.info(
        "snapshot",
        f"exported {exported} memories",
        detail={"path": str(swap_path), "ms": round(t.ms, 2)},
    )
    return SnapshotResult(exported=exported, path=str(swap_path))
