"""hotmem-interchange-v1 package writer — manifest, canonical payload, atomic publish.

Purpose:
     The company-brain clone package (#69, contract §6): a directory holding
     a versioned manifest plus a deterministic JSONL (or JSONL.GZ) payload
     with the full canonical record set. Publishes atomically — readers never
     observe a half-written package.

     Logical identity is content-derived: logical_id is the SHA-256 over the
     sorted content_hash list (interchange §3) — equal for equivalent DBs
     regardless of compression, record order, or export time. Gzip bytes are
     transport: written with mtime=0 and verified via the decompressed
     digest (interchange §4).

Interface:
      row_to_record(row) -> dict        (DB row → canonical interchange record)
      write_package(db, out_dir, gz=False) -> PackageResult
      FORMAT_ID / MANIFEST_NAME / PAYLOAD_NAMES

Deps: hotmem.db, hotmem.embed, hotmem.interchange.canonical.
Extension: hydrate/verify live in interchange.hydrate; dispatch surfaces in
      snapshot/__init__ and the CLI/API (C13).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL
from hotmem.interchange.canonical import canonical_line, logical_id, sha256_file
from hotmem.trace import get_tracer

_trace = get_tracer("interchange.package")

FORMAT_ID = "hotmem-interchange-v1"
MANIFEST_NAME = "manifest.json"
PAYLOAD_PLAIN = "memories.jsonl"
PAYLOAD_GZ = "memories.jsonl.gz"
PAYLOAD_NAMES = (PAYLOAD_PLAIN, PAYLOAD_GZ)


@dataclass
class PackageResult:
    exported: int
    path: str
    logical_id: str


def row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row (all_rows/iter_rows) to a canonical interchange record.

    The full field set round-trips: ttl, namespace, tier, tags, provenance,
    file-backed references, plus the stored embedding (reused on hydration
    only when compatible — §5). Serialization is canonical at write time.
    """
    embedding_blob = row.get("embedding")
    embedding_b64 = base64.b64encode(embedding_blob).decode() if embedding_blob else None
    record: dict[str, Any] = {
        "schema_version": 1,
        "id": row["id"],
        "identifier": row["identifier"],
        "memory_type": row["memory_type"],
        "fact_text": row["fact_text"],
        "fact_summary": row["fact_summary"],
        "embedding": embedding_b64,
        "embedding_dim": row["embedding_dim"],
        "embedding_model": row["embedding_model"],
        "source": row["source"],
        "importance": row["importance"],
        "metadata": _parse_json(row.get("metadata_json")),
        "content_hash": row["content_hash"],
        "ttl_seconds": row["ttl_seconds"],
        "namespace": row["namespace"],
        "tier": row["tier"],
        "tags": _parse_json(row.get("tags")) or [],
        "source_uri": row["source_uri"],
        "byte_offset": row["byte_offset"],
        "byte_length": row["byte_length"],
        "source_checksum": row["source_checksum"],
        "source_format": row["source_format"],
        "provenance": _parse_json(row.get("provenance_json")),
        "created_at": row["created_at"],
    }
    return {k: v for k, v in record.items() if v is not None}


def _parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry to disk so a rename survives a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_publish(staging: Path, final: Path) -> None:
    """Move a fully-written staging directory into place atomically.

    If ``final`` exists it is moved aside first, then removed after the
    swap — a failed publish never destroys the previous package.
    """
    backup: Path | None = None
    if final.exists():
        backup = final.with_name(f".{final.name}.old-{uuid.uuid4().hex[:8]}")
        os.replace(final, backup)
    try:
        os.replace(staging, final)
    except Exception:
        if backup is not None and not final.exists():
            os.replace(backup, final)  # restore the previous package
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _write_payload(db: MemoryDB, sink, hasher) -> tuple[int, list[str]]:
    """Stream canonical records to ``sink``, hashing payload bytes.

    Returns (record_count, content_hashes). Memory stays O(row) — rows are
    fetched in id order with fetchmany batches and written one line at a
    time (interchange #67/#69 streaming requirement).
    """
    content_hashes: list[str] = []
    count = 0
    for row in db.iter_rows():
        data = canonical_line(row_to_record(row)).encode()
        hasher.update(data)
        sink.write(data)
        content_hashes.append(row["content_hash"] or "")
        count += 1
    return count, content_hashes


def write_package(
    db: MemoryDB,
    out_dir: str | Path,
    *,
    gz: bool = False,
) -> PackageResult:
    """Write a hotmem-interchange-v1 package atomically (#69).

    Streams rows (ORDER BY id, fetchmany batches), serializes canonically,
    hashes while writing, and publishes via staging-dir + atomic rename.
    With ``gz=True`` the payload is gzip-compressed (mtime=0, level 9) and
    the manifest records the decompressed digest — verification never
    depends on zlib byte stability (contract §4).
    """
    final = Path(out_dir).resolve()
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(f".{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    payload_name = PAYLOAD_GZ if gz else PAYLOAD_PLAIN
    hasher = hashlib.sha256()

    try:
        payload_path = staging / payload_name
        with open(payload_path, "wb") as raw_file:
            if gz:
                # filename="" (no embedded name), mtime=0 → byte-stable transport.
                with gzip.GzipFile(
                    filename="", mode="wb", compresslevel=9, fileobj=raw_file, mtime=0
                ) as gz_file:
                    record_count, content_hashes = _write_payload(db, gz_file, hasher)
            else:
                record_count, content_hashes = _write_payload(db, raw_file, hasher)
            raw_file.flush()
            os.fsync(raw_file.fileno())

        payload_entry: dict[str, Any] = {
            "size": payload_path.stat().st_size,
            "sha256": sha256_file(payload_path),
        }
        if gz:
            payload_entry["decompressed_sha256"] = hasher.hexdigest()

        manifest = {
            "format": FORMAT_ID,
            "schema_version": 1,
            "record_count": record_count,
            "logical_id": logical_id(content_hashes),
            "files": {payload_name: payload_entry},
            "source": {"kind": "hotmem-dump"},
            "embedding": {"model": EMBEDDING_MODEL, "dim": EMBEDDING_DIM},
            "created_at": datetime.now(UTC).isoformat(),
            "hotmem_version": _hotmem_version(),
        }
        manifest_path = staging / MANIFEST_NAME
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())

        _fsync_dir(staging)
        _atomic_publish(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _trace.info(
        "package",
        f"published {record_count} records to {final}",
        detail={"path": str(final), "gz": gz, "logical_id": manifest["logical_id"][:12]},
    )
    return PackageResult(exported=record_count, path=str(final), logical_id=manifest["logical_id"])


def _hotmem_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("hotmem")
    except PackageNotFoundError:  # pragma: no cover - dev environments
        return "0.0.0.dev0"
