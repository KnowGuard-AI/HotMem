"""HotMem swap — JSONL hydration and snapshot.

Purpose:
    Load memories from a swap file (JSONL) into the database, and export
    the current database state back to a swap file. Deduplicates on content_hash.

Interface:
    hydrate(db, swap_path) -> HydrateResult
    snapshot(db, swap_path) -> SnapshotResult
    compute_content_hash(identifier, fact_text) -> str

Deps: hotmem.db, hotmem.embed, hotmem.trace
Extension: add compression, encryption, or remote swap sources here.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("swap")


def compute_content_hash(identifier: str, fact_text: str) -> str:
    """SHA-256 hash of identifier + fact_text for deduplication."""
    return hashlib.sha256(f"{identifier}:{fact_text}".encode()).hexdigest()


@dataclass
class HydrateResult:
    loaded: int
    skipped_dupes: int


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


def hydrate(db: MemoryDB, swap_path: str | Path) -> HydrateResult:
    """Load memories from a JSONL or JSONL.GZ swap file into the database.

    Deduplicates by content_hash — skips rows that already exist in the DB.
    """
    swap_path = Path(swap_path)
    if not swap_path.exists():
        _trace.warn("hydrate", "swap file not found", detail={"path": str(swap_path)})
        return HydrateResult(loaded=0, skipped_dupes=0)

    with Timer() as t:
        loaded = 0
        skipped = 0

        try:
            with _open_swap_read(swap_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)

                    identifier = record.get("identifier", "")
                    fact_text = record.get("fact_text", "")
                    content_hash = record.get(
                        "content_hash", compute_content_hash(identifier, fact_text)
                    )

                    if db.exists(content_hash):
                        skipped += 1
                        continue

                    vec = embed_text(fact_text)
                    blob = pack_embedding(vec)

                    db.insert(
                        id=record.get("id", uuid.uuid4().hex),
                        identifier=identifier,
                        fact_text=fact_text,
                        embedding=blob,
                        embedding_dim=record.get("embedding_dim", EMBEDDING_DIM),
                        embedding_model=record.get("embedding_model", EMBEDDING_MODEL),
                        source=record.get("source", "swap"),
                        importance=record.get("importance", 0.5),
                        metadata_json=json.dumps(record.get("metadata", {})),
                        content_hash=content_hash,
                        ttl_seconds=record.get("ttl_seconds"),
                        created_at=record.get("created_at"),
                    )
                    loaded += 1
        except (EOFError, OSError) as err:
            if _swap_format(swap_path) == "jsonl.gz":
                raise ValueError(f"malformed compressed swap file: {swap_path}") from err
            raise

    _trace.info(
        "hydrate",
        f"hydrated {loaded} memories, skipped {skipped} dupes",
        detail={"path": str(swap_path), "ms": round(t.ms, 2)},
    )
    return HydrateResult(loaded=loaded, skipped_dupes=skipped)


def snapshot(db: MemoryDB, swap_path: str | Path) -> SnapshotResult:
    """Export all memories from the database to a JSONL or JSONL.GZ swap file."""
    swap_path = Path(swap_path)

    with Timer() as t:
        rows = db.all_rows()
        with _open_swap_write(swap_path) as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")

    _trace.info(
        "snapshot",
        f"exported {len(rows)} memories",
        detail={"path": str(swap_path), "ms": round(t.ms, 2)},
    )
    return SnapshotResult(exported=len(rows), path=str(swap_path))
