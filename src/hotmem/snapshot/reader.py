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
from hotmem.interchange.hydrate import _scan_annotation_context
from hotmem.interchange.paths import confined_relpath
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.snapshot.format import (
    Manifest,
    SnapshotChecksumError,
    compute_overall,
    sha256_file,
)
from hotmem.swap import HYDRATE_BATCH, HydrateResult, record_to_memory_record
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
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SnapshotChecksumError("malformed", file=MANIFEST_NAME) from err

    manifest = Manifest.from_dict(raw)

    if MEMORIES_NAME not in manifest.files:
        raise SnapshotChecksumError("missing_file", file=MEMORIES_NAME)

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

    # Verify declared attachments and file-backed reference dependencies (#99 WP3).
    for att in manifest.attachments:
        rel_att = f"attachments/{att.name}"
        if (
            "/" in att.name
            or "\\" in att.name
            or att.name in ("", ".", "..")
            or not confined_relpath(d, rel_att)
        ):
            raise SnapshotChecksumError("path_escape", file=rel_att)
        if rel_att not in manifest.files:
            raise SnapshotChecksumError("missing_file", file=rel_att)
        fentry = manifest.files[rel_att]
        if att.size != fentry.size:
            raise SnapshotChecksumError(
                "mismatch",
                file=rel_att,
                expected=f"size={att.size}",
                actual=f"size={fentry.size}",
            )
        if att.sha256 and att.sha256 != fentry.sha256:
            raise SnapshotChecksumError(
                "mismatch",
                file=rel_att,
                expected=att.sha256,
                actual=fentry.sha256,
            )

    for ref in manifest.file_backed_references:
        if ref.attachment is not None:
            att_name = str(ref.attachment)
            rel_att = f"attachments/{att_name}"
            if (
                "/" in att_name
                or "\\" in att_name
                or att_name in ("", ".", "..")
                or not confined_relpath(d, rel_att)
            ):
                raise SnapshotChecksumError("path_escape", file=rel_att)
            if rel_att not in manifest.files:
                raise SnapshotChecksumError("missing_file", file=rel_att)
            fentry = manifest.files[rel_att]
            if ref.byte_length > 0 and fentry.size != ref.byte_length:
                raise SnapshotChecksumError(
                    "mismatch",
                    file=rel_att,
                    expected=f"size={ref.byte_length}",
                    actual=f"size={fentry.size}",
                )
            if ref.source_checksum and fentry.sha256 != ref.source_checksum:
                raise SnapshotChecksumError(
                    "mismatch",
                    file=rel_att,
                    expected=ref.source_checksum,
                    actual=fentry.sha256,
                )

    return manifest


def hydrate_v2(
    db: MemoryDB,
    snapshot_dir: str | Path,
    *,
    embedder: Embedder | None = None,
    target_namespace: str | None = None,
) -> HydrateResult:
    """Verify the manifest and load all memories into the DB.

    Deduplicates by ``(namespace, content_hash)`` (skips rows that already
    exist in the same namespace). Never
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
        intra_batch_dups: list[dict] = []
        batch_seen: set[tuple[str, str]] = set()

        # Bounded-memory annotation context pass (#79): ids and evidence
        # references only -- never records -- so streaming stays O(batch).
        # See hydrate_package._scan_annotation_context for the rationale.
        with open(memories_path, encoding="utf-8") as f:
            package_ids, referenced_ids, has_annotations = _scan_annotation_context(f)
        known_ids: set[str] | None = None
        if has_annotations:
            known_ids = package_ids | db.fetch_existing_ids(referenced_ids)

        def flush() -> None:
            if not pending and not intra_batch_dups:
                return
            existing = db.batch_existing_record_identities(pending)
            todo = [r for r in pending if db.record_identity_key(r) not in existing]
            db_dups = [r for r in pending if db.record_identity_key(r) in existing]
            counters["skipped"] += len(pending) - len(todo)

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

            merge_duplicate_annotations(
                db,
                db_dups + intra_batch_dups,
                counters,
                # Per-batch autocommit path: no later commit exists to carry
                # an all-duplicate batch's merge (durability, #79 review).
                commit=True,
            )
            pending.clear()
            intra_batch_dups.clear()
            batch_seen.clear()

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
                    if target_namespace is not None and not rec.get("namespace"):
                        rec["namespace"] = target_namespace
                except AnnotationValidationError as err:
                    counters["invalid"] += 1  # malformed envelope: skip record (#79)
                    _trace.debug(
                        "hydrate_v2",
                        "skipping record with malformed annotations",
                        detail={"error": str(err)},
                    )
                    continue
                if has_annotations:
                    metadata = rec.get("metadata") or {}
                    if isinstance(metadata, dict) and "annotations" in metadata:
                        try:
                            validate_metadata(metadata, known_ids=known_ids)
                        except AnnotationValidationError as err:
                            counters["invalid"] += 1
                            _trace.debug(
                                "hydrate_v2",
                                "skipping record with invalid annotations",
                                detail={"id": rec["id"], "error": str(err)},
                            )
                            continue
                if validate_record(rec):
                    counters["invalid"] += 1
                    continue
                dedup_key = db.record_identity_key(rec)
                if dedup_key[1] and dedup_key in batch_seen:
                    counters["skipped"] += 1
                    if isinstance(rec.get("metadata"), dict) and "annotations" in (
                        rec["metadata"] or {}
                    ):
                        intra_batch_dups.append(rec)
                    continue
                if dedup_key[1]:
                    batch_seen.add(dedup_key)
                pending.append(rec)
                if len(pending) >= HYDRATE_BATCH:
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
