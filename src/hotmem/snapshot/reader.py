"""HotMem snapshot v2 reader — verify manifest and hydrate memories.

Purpose:
     Read a Snapshot v2 directory: verify the manifest's per-file and overall
     checksums (hard error on mismatch), then stream ``memories.jsonl`` into the
     DB. File-backed memories are reconstructed as references WITHOUT touching
     the backing files (reference-not-duplicate principle, matching #38).

     Hydration uses stored base64 embeddings when present (no re-embedding);
     falls back to embedding fact_text (inline) or fact_summary (file-backed
     with summary); stores NULL embedding for file-backed without summary.

Interface:
     detect_v2(path) -> bool
     verify_manifest(dir) -> Manifest
     hydrate_v2(db, dir) -> HydrateResult

Deps: hotmem.db, hotmem.embed, hotmem.snapshot.format, hotmem.trace
Extension: add migration from older snapshot schema versions here.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.snapshot.format import (
    Manifest,
    SnapshotChecksumError,
    compute_overall,
    sha256_file,
)
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("snapshot.reader")

MANIFEST_NAME = "manifest.json"
MEMORIES_NAME = "memories.jsonl"
METADATA_NAME = "metadata.json"


@dataclass
class HydrateResult:
    loaded: int
    skipped_dupes: int


def detect_v2(path: str | Path) -> bool:
    """True if ``path`` is a directory containing a manifest.json."""
    p = Path(path)
    return p.is_dir() and (p / MANIFEST_NAME).is_file()


def verify_manifest(snapshot_dir: str | Path) -> Manifest:
    """Read and verify the manifest; raise SnapshotChecksumError on any failure.

    Verifies every listed file's SHA-256 and size, then recomputes and verifies
    ``overall_sha256``. ``metadata.json`` is intentionally NOT verified
    (informational only, so wall-clock timestamps don't break determinism).
    Extraneous files in the directory are ignored (forward-compatible).
    """
    d = Path(snapshot_dir)
    manifest_path = d / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotChecksumError("missing_manifest", file=str(manifest_path))

    try:
        raw = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as err:
        raise SnapshotChecksumError("malformed", file=MANIFEST_NAME) from err

    manifest = Manifest.from_dict(raw)

    # Verify each listed file.
    per_file_hashes: dict[str, str] = {}
    for rel, entry in manifest.files.items():
        fpath = d / rel
        if not fpath.is_file():
            raise SnapshotChecksumError("missing_file", file=rel)
        actual_size = os.path.getsize(fpath)
        if actual_size != entry.size:
            raise SnapshotChecksumError(
                "mismatch",
                file=rel,
                expected=f"size={entry.size}",
                actual=f"size={actual_size}",
            )
        actual_sha = sha256_file(fpath)
        if actual_sha != entry.sha256:
            raise SnapshotChecksumError(
                "mismatch",
                file=rel,
                expected=entry.sha256,
                actual=actual_sha,
            )
        per_file_hashes[rel] = actual_sha

    # Verify the overall aggregate.
    expected_overall = manifest.overall_sha256
    actual_overall = compute_overall(per_file_hashes)
    if expected_overall and actual_overall != expected_overall:
        raise SnapshotChecksumError(
            "mismatch",
            file="overall_sha256",
            expected=expected_overall,
            actual=actual_overall,
        )

    return manifest


def hydrate_v2(db: MemoryDB, snapshot_dir: str | Path) -> HydrateResult:
    """Verify the manifest and load all memories into the DB.

    Deduplicates by ``content_hash`` (skips rows that already exist). Never
    touches backing files for file-backed memories — references are preserved.
    Uses stored base64 embeddings when present; otherwise embeds fact_text or
    fact_summary, or stores NULL embedding for file-backed without summary.
    """
    snapshot_dir = Path(snapshot_dir)
    with Timer() as t:
        manifest = verify_manifest(snapshot_dir)
        memories_path = snapshot_dir / MEMORIES_NAME
        if not memories_path.is_file():
            raise SnapshotChecksumError("missing_file", file=MEMORIES_NAME)

        loaded = 0
        skipped = 0

        with open(memories_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                content_hash = record.get("content_hash") or ""
                if content_hash and db.exists(content_hash):
                    skipped += 1
                    continue

                memory_type = record.get("memory_type") or "inline"
                embedding_b64 = record.get("embedding")
                embedding_blob = base64.b64decode(embedding_b64) if embedding_b64 else None

                if memory_type == "file":
                    _insert_file_backed(db, record, embedding_blob)
                else:
                    _insert_inline(db, record, embedding_blob)
                loaded += 1

    _trace.info(
        "hydrate_v2",
        f"hydrated {loaded} memories, skipped {skipped} dupes",
        detail={
            "path": str(snapshot_dir),
            "snapshot_id": manifest.snapshot_id[:12],
            "ms": round(t.ms, 2),
        },
    )
    return HydrateResult(loaded=loaded, skipped_dupes=skipped)


def _insert_inline(db: MemoryDB, record: dict[str, Any], embedding_blob: bytes | None) -> None:
    """Insert an inline memory, using the stored embedding or re-embedding fact_text."""
    fact_text = record.get("fact_text") or ""
    if embedding_blob is None:
        embedding_blob = pack_embedding(embed_text(fact_text))
        embedding_model = EMBEDDING_MODEL
        embedding_dim = EMBEDDING_DIM
    else:
        embedding_model = record.get("embedding_model") or EMBEDDING_MODEL
        embedding_dim = record.get("embedding_dim") or EMBEDDING_DIM

    db.insert(
        id=record.get("id") or uuid.uuid4().hex,
        identifier=record.get("identifier", ""),
        fact_text=fact_text,
        embedding=embedding_blob,
        embedding_dim=embedding_dim,
        embedding_model=embedding_model,
        source=record.get("source", "snapshot"),
        importance=record.get("importance", 0.5),
        metadata_json=json.dumps(record.get("metadata") or {}),
        content_hash=record.get("content_hash") or "",
    )


def _insert_file_backed(db: MemoryDB, record: dict[str, Any], embedding_blob: bytes | None) -> None:
    """Insert a file-backed memory reference (no backing file touched).

    Uses the stored embedding when present; else embeds fact_summary if present;
    else stores an empty embedding (NULL by convention in #38).
    """
    fact_summary = record.get("fact_summary")
    if embedding_blob is None:
        if fact_summary:
            embedding_blob = pack_embedding(embed_text(fact_summary))
            embedding_model = EMBEDDING_MODEL
            embedding_dim = EMBEDDING_DIM
        else:
            embedding_blob = b""
            embedding_model = ""
            embedding_dim = EMBEDDING_DIM
    else:
        embedding_model = record.get("embedding_model") or EMBEDDING_MODEL
        embedding_dim = record.get("embedding_dim") or EMBEDDING_DIM

    db.insert_file_backed(
        id=record.get("id") or uuid.uuid4().hex,
        identifier=record.get("identifier", ""),
        source_uri=record.get("source_uri") or "",
        byte_offset=int(record.get("byte_offset") or 0),
        byte_length=int(record.get("byte_length") or 0),
        source_format=record.get("source_format") or "",
        source_checksum=record.get("source_checksum"),
        fact_summary=fact_summary,
        embedding=embedding_blob,
        embedding_dim=embedding_dim,
        embedding_model=embedding_model,
        source=record.get("source", "snapshot"),
        importance=record.get("importance", 0.5),
        metadata_json=json.dumps(record.get("metadata") or {}),
        content_hash=record.get("content_hash") or "",
        provenance_json=json.dumps(record["provenance"]) if record.get("provenance") else None,
    )
