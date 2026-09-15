"""HotMem snapshot v2 reader — verify manifest and hydrate memories.

Purpose:
     Read a Snapshot v2 directory: verify the manifest's per-file and overall
     checksums (hard error on mismatch), then stream ``memories.jsonl`` into
     the DB. File-backed memories are reconstructed as references WITHOUT
     touching the backing files (reference-not-duplicate, matching #38).

     Every record runs through the shared interchange normalization (issue
     #67): full field preservation (namespace, tier, tags, ttl, provenance,
     file references), ONE embedding compatibility rule — stored embeddings
     are reused only when model/dim/blob match, otherwise re-embedded from
     fact_text (inline) or fact_summary (file-backed); NULL embedding for
     file-backed without summary — and bounded-batch inserts with
     database-backed deduplication (no per-record commits, no full hash-set
     loads).

Interface:
     detect_v2(path) -> bool
     verify_manifest(dir) -> Manifest
     hydrate_v2(db, dir) -> HydrateResult

Deps: hotmem.db, hotmem.embed, hotmem.snapshot.format, hotmem.swap,
      hotmem.interchange, hotmem.trace
Extension: add migration from older snapshot schema versions here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hotmem.annotations import (
    AnnotationValidationError,
    merge_duplicate_annotations,
    validate_metadata,
)
from hotmem.db import MemoryDB
from hotmem.embed import Embedder
from hotmem.interchange.compat import resolve_embedding
from hotmem.interchange.paths import confined_relpath
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.snapshot.format import (
    Manifest,
    SnapshotChecksumError,
    compute_overall,
    sha256_file,
)
from hotmem.swap import _HYDRATE_BATCH, HydrateResult, record_to_memory_record
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("snapshot.reader")

MANIFEST_NAME = "manifest.json"
MEMORIES_NAME = "memories.jsonl"
METADATA_NAME = "metadata.json"


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

    Manifest-listed paths are confinement-checked (interchange contract #67):
    absolute paths, ``..`` traversal, and symlink escapes are rejected before
    any file is read.
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
        if not confined_relpath(d, rel):
            raise SnapshotChecksumError("path_escape", file=rel)
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


def hydrate_v2(
    db: MemoryDB,
    snapshot_dir: str | Path,
    *,
    embedder: Embedder | None = None,
) -> HydrateResult:
    """Verify the manifest and load all memories into the DB.

    Deduplicates by ``content_hash`` (skips rows that already exist). Never
    touches backing files for file-backed memories — references are preserved.
    Uses stored embeddings only when compatible (descriptor/dim/blob);
    otherwise re-embeds fact_text or fact_summary under ``embedder`` (issue
    #78; ``None`` = the hash default), or stores NULL embedding for
    file-backed without summary. Records that fail validation are counted
    invalid and skipped (interchange-v1 §7).
    """
    snapshot_dir = Path(snapshot_dir)
    with Timer() as t:
        manifest = verify_manifest(snapshot_dir)
        memories_path = snapshot_dir / MEMORIES_NAME
        if not memories_path.is_file():
            raise SnapshotChecksumError("missing_file", file=MEMORIES_NAME)

        counters = {
            "loaded": 0,
            "skipped": 0,
            "invalid": 0,
            "embedding_reused": 0,
            "embedding_rebuilt": 0,
            "embedding_missing": 0,
            "embedding_failed": 0,
            "annotations_merged": 0,
            "annotation_conflicts": 0,
        }
        pending: list[dict] = []
        batch_seen: set[str] = set()

        # Parse all records first (#79): local evidence references resolve
        # against the full snapshot id set (forward references included)
        # plus the existing target. See hydrate_package for the rationale.
        parsed: list[dict] = []
        has_annotations = False
        with open(memories_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    counters["invalid"] += 1
                    continue
                try:
                    rec = normalize_record(record, default_source="snapshot")
                except AnnotationValidationError as err:
                    counters["invalid"] += 1  # malformed envelope: skip record (#79)
                    _trace.debug(
                        "hydrate_v2",
                        "skipping record with malformed annotations",
                        detail={"error": str(err)},
                    )
                    continue
                if validate_record(rec):
                    counters["invalid"] += 1
                    continue
                metadata = rec.get("metadata") or {}
                if isinstance(metadata, dict) and "annotations" in metadata:
                    has_annotations = True
                parsed.append(rec)

        if has_annotations:
            known_ids = {rec["id"] for rec in parsed} | set(db.all_ids())
            for rec in list(parsed):
                metadata = rec.get("metadata") or {}
                if not (isinstance(metadata, dict) and "annotations" in metadata):
                    continue
                try:
                    validate_metadata(metadata, known_ids=known_ids)
                except AnnotationValidationError as err:
                    counters["invalid"] += 1
                    _trace.debug(
                        "hydrate_v2",
                        "skipping record with invalid annotations",
                        detail={"id": rec["id"], "error": str(err)},
                    )
                    parsed.remove(rec)

        def flush() -> None:
            if not pending:
                return
            hashes = [r["content_hash"] for r in pending]
            existing = db.batch_existing_hashes(hashes)
            todo = [r for r in pending if r["content_hash"] not in existing]
            counters["skipped"] += len(pending) - len(todo)

            merge_duplicate_annotations(
                db, [r for r in pending if r["content_hash"] in existing], counters
            )

            records = []
            for rec in todo:
                blob, model, dim, status = resolve_embedding(rec, embedder=embedder)
                counters[f"embedding_{status}"] += 1
                records.append(
                    record_to_memory_record(rec, blob, embedding_model=model, embedding_dim=dim)
                )
            loaded = db.insert_many_ignore(records)
            counters["loaded"] += loaded
            counters["skipped"] += len(records) - loaded
            pending.clear()
            batch_seen.clear()

        for rec in parsed:
            content_hash = rec["content_hash"]
            if content_hash in batch_seen:
                counters["skipped"] += 1
                continue
            batch_seen.add(content_hash)
            pending.append(rec)
            if len(pending) >= _HYDRATE_BATCH:
                flush()
        flush()

        loaded = counters["loaded"]
        skipped = counters["skipped"]
        invalid = counters["invalid"]

    _trace.info(
        "hydrate_v2",
        f"hydrated {loaded} memories, skipped {skipped} dupes, {invalid} invalid",
        detail={
            "path": str(snapshot_dir),
            "snapshot_id": manifest.snapshot_id[:12],
            "ms": round(t.ms, 2),
            **{k: counters[k] for k in counters},
        },
    )
    return HydrateResult(
        loaded=loaded,
        skipped_dupes=skipped,
        invalid=invalid,
        embedding_reused=counters["embedding_reused"],
        embedding_rebuilt=counters["embedding_rebuilt"],
        embedding_missing=counters["embedding_missing"],
        embedding_failed=counters["embedding_failed"],
        annotations_merged=counters["annotations_merged"],
        annotation_conflicts=counters["annotation_conflicts"],
    )
