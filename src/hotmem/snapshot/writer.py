"""HotMem snapshot v2 writer — directory layout with manifest + jsonl + attachments.

Purpose:
     Export a MemoryDB to a portable, replayable directory: manifest.json
     (authoritative, checksummed), memories.jsonl (sorted, base64 embeddings),
     metadata.json (informational), and an opt-in attachments/ dir that copies
     small file-backed byte ranges (large data stays referenced, not copied).

     Deterministic for identical input: memories sorted by id, manifest written
     with sort_keys=True, snapshot_id derived from sorted content_hashes, and
     metadata.json excluded from the overall checksum.

Interface:
     write_snapshot_v2(db, out_dir, *, copy_attachments=False, base_dir=None,
                       attach_threshold=ATTACH_THRESHOLD) -> SnapshotResult

Deps: hotmem.db, hotmem.snapshot.format, hotmem.storage.local, hotmem.trace
Extension: add encryption, remote sync, or attachment policies here.
"""

from __future__ import annotations

import base64
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hotmem import __version__
from hotmem.db import MemoryDB
from hotmem.snapshot.format import (
    ATTACH_THRESHOLD,
    AttachmentEntry,
    FileEntry,
    FileReference,
    Manifest,
    MetadataInfo,
    compute_overall,
    compute_snapshot_id,
    sha256_bytes,
    sha256_file,
)
from hotmem.storage.local import LocalFilesystemAdapter
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("snapshot.writer")

MANIFEST_NAME = "manifest.json"
MEMORIES_NAME = "memories.jsonl"
METADATA_NAME = "metadata.json"
ATTACHMENTS_DIR = "attachments"


@dataclass
class SnapshotResult:
    exported: int
    path: str


def _parse_json_field(value: str | None) -> Any:
    """Decode a JSON column defensively; return None on failure/None."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row (all_rows) to a v2 memories.jsonl record.

    Embeddings are base64-encoded so the jsonl is text-portable and can be
    rehydrated without re-embedding (stored-embedding variant, #25).
    """
    embedding_blob: bytes | None = row.get("embedding")
    embedding_b64 = base64.b64encode(embedding_blob).decode() if embedding_blob else None
    return {
        "schema_version": 2,
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
        "metadata": _parse_json_field(row["metadata_json"]),
        "content_hash": row["content_hash"],
        "source_uri": row["source_uri"],
        "byte_offset": row["byte_offset"],
        "byte_length": row["byte_length"],
        "source_checksum": row["source_checksum"],
        "source_format": row["source_format"],
        "provenance": _parse_json_field(row["provenance_json"]),
        "created_at": row["created_at"],
    }


def write_snapshot_v2(
    db: MemoryDB,
    out_dir: str | Path,
    *,
    copy_attachments: bool = False,
    base_dir: str | Path | None = None,
    attach_threshold: int = ATTACH_THRESHOLD,
) -> SnapshotResult:
    """Write the full v2 snapshot directory.

    Args:
        db: source MemoryDB.
        out_dir: destination directory (created if absent; overwritten if present).
        copy_attachments: when True, copy small file-backed byte ranges into
            ``attachments/`` and rewrite their references with ``attachment://``.
            Large ranges (>= ``attach_threshold`` bytes) stay referenced. On any
            read error the original URI is kept (the snapshot never fails over a copy).
        base_dir: base directory for resolving relative source URIs when copying
            attachments. Defaults to the parent of the DB path.
        attach_threshold: max byte length to copy as an attachment.

    Returns:
        SnapshotResult with the exported count and directory path.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir = out_dir / ATTACHMENTS_DIR
    if copy_attachments:
        attachments_dir.mkdir(parents=True, exist_ok=True)

    adapter = LocalFilesystemAdapter() if copy_attachments else None

    with Timer() as t:
        rows = db.all_rows(include_embedding=True)
        # Deterministic ordering: sort by id.
        rows.sort(key=lambda r: r["id"])

        records = [_row_to_record(r) for r in rows]
        content_hashes = [r["content_hash"] for r in rows if r["content_hash"]]

        # Build file references; copy small ranges into attachments/ if enabled.
        file_references: list[FileReference] = []
        copied: dict[str, str] = {}  # attachment filename -> memory_id (for trace)
        for rec in records:
            if rec["memory_type"] != "file" or not rec["source_uri"]:
                continue
            ref = FileReference(
                memory_id=rec["id"],
                source_uri=rec["source_uri"],
                byte_offset=rec["byte_offset"] or 0,
                byte_length=rec["byte_length"] or 0,
                source_format=rec["source_format"],
                source_checksum=rec["source_checksum"],
                attachment=None,
            )
            if copy_attachments and adapter is not None and 0 < ref.byte_length < attach_threshold:
                att_name = _copy_attachment(adapter, ref, attachments_dir, base_dir)
                if att_name is not None:
                    ref.attachment = att_name
                    copied[att_name] = ref.memory_id
            file_references.append(ref)

        # Write memories.jsonl (sorted, one record per line, compact JSON).
        memories_path = out_dir / MEMORIES_NAME
        with open(memories_path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")

        inline_count = sum(1 for r in records if r["memory_type"] != "file")
        file_backed_count = sum(1 for r in records if r["memory_type"] == "file")

        # Write metadata.json (informational only — NOT included in checksums
        # so wall-clock timestamps/host don't break manifest determinism).
        meta = MetadataInfo(
            hotmem_version=__version__,
            created_at=_utc_now_iso(),
            host=socket.gethostname(),
            db_path=str(db.db_path),
            counts={"inline": inline_count, "file": file_backed_count, "total": len(records)},
        )
        metadata_path = out_dir / METADATA_NAME
        metadata_path.write_text(json.dumps(meta.to_dict(), sort_keys=True, indent=2) + "\n")

        # Compute per-file checksums for manifest (metadata.json excluded for
        # determinism — it carries created_at/host which vary per run).
        per_file: dict[str, FileEntry] = {
            MEMORIES_NAME: FileEntry(
                size=os.path.getsize(memories_path), sha256=sha256_file(memories_path)
            ),
        }
        # List attachment files in deterministic (sorted) order.
        attachment_entries: list[AttachmentEntry] = []
        if attachments_dir.exists():
            for att in sorted(attachments_dir.iterdir()):
                if att.is_file():
                    rel = f"{ATTACHMENTS_DIR}/{att.name}"
                    att_sha = sha256_file(att)
                    att_size = os.path.getsize(att)
                    per_file[rel] = FileEntry(size=att_size, sha256=att_sha)
                    # Find the source_uri for this attachment from file_references.
                    att_ref = next((r for r in file_references if r.attachment == att.name), None)
                    attachment_entries.append(
                        AttachmentEntry(
                            name=att.name,
                            source_uri=att_ref.source_uri if att_ref else "",
                            size=att_size,
                            sha256=att_sha,
                        )
                    )

        overall = compute_overall({name: e.sha256 for name, e in per_file.items()})
        snapshot_id = compute_snapshot_id(content_hashes)

        manifest = Manifest(
            snapshot_id=snapshot_id,
            created_at=_utc_now_iso(),
            hotmem_version=__version__,
            memory_count=len(records),
            file_backed_count=file_backed_count,
            inline_count=inline_count,
            files=per_file,
            overall_sha256=overall,
            file_backed_references=file_references,
            attachments=attachment_entries,
        )

        # Write manifest.json with stable key ordering.
        manifest_path = out_dir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n")

    _trace.info(
        "snapshot_v2",
        f"exported {len(records)} memories to {out_dir}",
        detail={
            "path": str(out_dir),
            "manifest": str(manifest_path),
            "inline": inline_count,
            "file_backed": file_backed_count,
            "attachments_copied": len(copied),
            "snapshot_id": snapshot_id[:12],
            "ms": round(t.ms, 2),
        },
    )
    return SnapshotResult(exported=len(records), path=str(out_dir))


def _copy_attachment(
    adapter: LocalFilesystemAdapter,
    ref: FileReference,
    attachments_dir: Path,
    base_dir: str | Path | None = None,
) -> str | None:
    """Copy a small byte range into attachments/; return the filename or None on error.

    The attachment filename is the SHA-256 of the byte range (content-addressed,
    stable across runs). On any read/IO error the original URI is kept and the
    snapshot continues (references, not duplicates — never fail over a copy).
    """
    # Resolve relative URIs against base_dir before passing to the adapter.
    uri = ref.source_uri
    if base_dir is not None and "://" not in uri and not Path(uri).is_absolute():
        uri = str(Path(base_dir) / uri)
    try:
        data = adapter.read_range(uri, ref.byte_offset, ref.byte_length)
    except (FileNotFoundError, OSError, ValueError) as err:
        _trace.warn(
            "attachment",
            "skipped attachment copy; keeping original URI",
            detail={"source_uri": ref.source_uri, "memory_id": ref.memory_id, "err": str(err)},
        )
        return None
    name = sha256_bytes(data)
    (attachments_dir / name).write_bytes(data)
    return name


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (stdlib only, avoids datetime import churn)."""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat()
