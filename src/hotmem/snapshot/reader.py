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
from dataclasses import dataclass
from pathlib import Path

from hotmem.db import MemoryDB
from hotmem.interchange.compat import resolve_embedding
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.snapshot.format import (
    Manifest,
    SnapshotChecksumError,
    compute_overall,
    sha256_file,
)
from hotmem.swap import _HYDRATE_BATCH, record_to_memory_record
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("snapshot.reader")

MANIFEST_NAME = "manifest.json"
MEMORIES_NAME = "memories.jsonl"
METADATA_NAME = "metadata.json"


@dataclass
class HydrateResult:
    loaded: int
    skipped_dupes: int
    invalid: int = 0


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


def confined_relpath(root: Path, rel: str) -> bool:
    """True if ``rel`` names a path inside ``root`` without traversal/symlinks.

    A crafted manifest must never make the verifier read outside the package
    (interchange contract #67, path confinement). Absolute paths and any
    component escaping the root are rejected; symlinked entries are rejected
    because they can point outside even with a clean relative name.
    """
    if not rel or rel.startswith("/") or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return False
    resolved = (root / rel).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return False
    return os.path.realpath(root / rel) == str(resolved) and not os.path.islink(root / rel)


def hydrate_v2(db: MemoryDB, snapshot_dir: str | Path) -> HydrateResult:
    """Verify the manifest and load all memories into the DB.

    Deduplicates by ``content_hash`` (skips rows that already exist). Never
    touches backing files for file-backed memories — references are preserved.
    Uses stored embeddings only when compatible (model/dim/blob); otherwise
    re-embeds fact_text or fact_summary, or stores NULL embedding for
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
            "reused_embeddings": 0,
            "computed_embeddings": 0,
        }
        pending: list[dict] = []
        batch_seen: set[str] = set()

        def flush() -> None:
            if not pending:
                return
            existing = db.batch_existing_hashes([r["content_hash"] for r in pending])
            todo = [r for r in pending if r["content_hash"] not in existing]
            counters["skipped"] += len(pending) - len(todo)

            records = []
            for rec in todo:
                blob, model, dim, reused = resolve_embedding(rec)
                counters["reused_embeddings" if reused else "computed_embeddings"] += 1
                records.append(
                    record_to_memory_record(rec, blob, embedding_model=model, embedding_dim=dim)
                )
            loaded = db.insert_many_ignore(records)
            counters["loaded"] += loaded
            counters["skipped"] += len(records) - loaded
            pending.clear()
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
                rec = normalize_record(record, default_source="snapshot")
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
    return HydrateResult(loaded=loaded, skipped_dupes=skipped, invalid=invalid)
