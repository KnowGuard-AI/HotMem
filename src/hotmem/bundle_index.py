"""HotMem bundle index — discovery and lightweight indexing of local bundles.

Purpose:
      Make local bundle directories discoverable and searchable without eagerly
      hydrating every byte. Walks configured directory trees, recognizes bundle
      markers (memory.md, index.md, README.md), and builds a metadata-only index.

      Discovery reads the bundle's memory body and metadata (markdown + JSON/YAML),
      but does NOT read attachment bytes. The index is persisted in a lightweight
      SQLite table so it survives restarts and is queryable.

      Security: directory walking uses ``os.walk(followlinks=False)`` to prevent
      symlink-based path traversal. Attachment ``source_uri`` values are confined
      to the bundle directory before ``stat()``. Resource caps (max files, max
      bytes) prevent unbounded walks on hostile trees.

Interface:
      BundleIndexEntry (dataclass): path, primary_file, identifier, metadata_summary,
          attachment_count, attachment_refs, checksum, modified_time, size_hint,
          warning_count, warnings
      discover_bundles(root, *, max_depth=10) -> list[BundleIndexEntry]
      index_bundles(db, root, *, max_depth=10) -> BundleIndexResult

Deps: hotmem.bundle, hotmem.db, hotmem.trace
Extension: add remote bundle discovery, incremental re-indexing, watch-mode,
           or rebuild-from-SQLite-state reconciliation here.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotmem.bundle import (
    MEMORY_BODY_FILES,
    BundleWarning,
    detect_bundle,
    parse_bundle,
)
from hotmem.db import MemoryDB
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("bundle_index")

# Resource caps to prevent unbounded walks on hostile or enormous trees.
_MAX_FILES_PER_BUNDLE = 10_000
_MAX_BUNDLE_SIZE_HINT = 1024 * 1024 * 1024  # 1 GB


@dataclass
class AttachmentRef:
    """A reference to an attachment file within a bundle (metadata only)."""

    name: str
    size: int
    format: str
    checksum: str = ""


@dataclass
class BundleIndexEntry:
    """Metadata-only index entry for a discovered bundle."""

    path: str
    primary_file: str
    identifier: str
    metadata_summary: dict[str, Any] = field(default_factory=dict)
    attachment_count: int = 0
    attachment_refs: list[AttachmentRef] = field(default_factory=list)
    checksum: str = ""
    modified_time: str = ""
    size_hint: int = 0
    warning_count: int = 0
    warnings: list[BundleWarning] = field(default_factory=list)


@dataclass
class BundleIndexResult:
    """Result of indexing bundles into the DB."""

    discovered: int = 0
    indexed: int = 0
    warnings: list[BundleWarning] = field(default_factory=list)


def discover_bundles(
    root: str | Path,
    *,
    max_depth: int = 10,
) -> list[BundleIndexEntry]:
    """Walk a directory tree and discover all bundles under ``root``.

    Returns a list of :class:`BundleIndexEntry` objects sorted by path
    (deterministic ordering). Discovery reads the bundle's memory body
    (markdown) and metadata (JSON/YAML) but does NOT read attachment bytes —
    only ``os.lstat``/``Path.stat`` is called for size hints within the bundle
    directory (symlinks that escape are rejected).

    Unknown files in bundles produce warnings (permissive mode).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"bundle root not found or not a directory: {root}")

    entries: list[BundleIndexEntry] = []

    with Timer() as t:
        # followlinks=False (default) prevents symlink traversal during the walk.
        for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
            # Enforce max_depth: root is depth 0, direct children depth 1, etc.
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            if rel_depth >= max_depth:
                dirnames.clear()  # don't descend further

            bundle_dir = Path(dirpath)
            if not detect_bundle(bundle_dir):
                continue

            entry = _build_index_entry(bundle_dir)
            if entry is not None:
                entries.append(entry)

    # Deterministic ordering: sort by path.
    entries.sort(key=lambda e: e.path)

    _trace.info(
        "discover",
        f"discovered {len(entries)} bundles under {root}",
        detail={"root": str(root), "max_depth": max_depth, "ms": round(t.ms, 2)},
    )
    return entries


def _is_within_bundle(path: Path, bundle_dir: Path) -> bool:
    """Return True if ``path`` resolves to within ``bundle_dir`` (symlink-safe)."""
    try:
        path.resolve().relative_to(bundle_dir.resolve())
        return True
    except (ValueError, OSError):
        return False


def _build_index_entry(bundle_dir: Path) -> BundleIndexEntry | None:
    """Build a BundleIndexEntry from a single bundle directory.

    Uses ``parse_bundle()`` to extract records + warnings without DB insertion.
    Reads the memory body (markdown) and metadata, but does NOT read attachment
    bytes — only stat() for size hints. Symlinks that escape the bundle directory
    are rejected (path-traversal protection). A single ``os.walk`` pass collects
    both size and mtime (no double traversal).
    """
    try:
        records, warnings = parse_bundle(bundle_dir)
    except Exception as err:
        _trace.warn(
            "discover",
            f"failed to parse bundle {bundle_dir}: {err}",
            detail={"path": str(bundle_dir), "error": str(err)},
        )
        return None

    # Find the primary memory file.
    primary_file = ""
    for name in MEMORY_BODY_FILES:
        if (bundle_dir / name).is_file():
            primary_file = name
            break

    # Extract metadata from the first (primary) record.
    primary = records[0] if records else None
    identifier = primary.identifier if primary else bundle_dir.name

    # Build metadata_summary from the record's structured fields.
    metadata_summary: dict[str, Any] = {}
    if primary:
        if primary.importance != 0.5:
            metadata_summary["importance"] = primary.importance
        if primary.namespace:
            metadata_summary["namespace"] = primary.namespace
        if primary.tier and primary.tier != "hot":
            metadata_summary["tier"] = primary.tier
        if primary.source:
            metadata_summary["source"] = primary.source
        if primary.fact_summary:
            metadata_summary["summary"] = primary.fact_summary
        if primary.tags and primary.tags != "[]":
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                metadata_summary["tags"] = json.loads(primary.tags)
        if primary.metadata_json:
            try:
                inner = json.loads(primary.metadata_json)
                if inner:
                    metadata_summary["metadata"] = inner
            except (json.JSONDecodeError, TypeError):
                pass

    # Build attachment refs from file-backed records (metadata only, no byte reads).
    # Confine source_uri to the bundle directory (path-traversal protection).
    attachment_refs: list[AttachmentRef] = []
    for rec in records:
        if rec.memory_type == "file" and rec.source_uri:
            source_path = Path(rec.source_uri)
            if not source_path.is_absolute():
                source_path = bundle_dir / source_path
            # Reject refs that resolve outside the bundle directory.
            if not _is_within_bundle(source_path, bundle_dir):
                warnings.append(
                    BundleWarning(
                        str(source_path),
                        "attachment resolves outside bundle dir; skipped from index",
                    )
                )
                continue
            try:
                size = source_path.stat().st_size
            except OSError:
                size = rec.byte_length or 0
            attachment_refs.append(
                AttachmentRef(
                    name=rec.fact_summary or source_path.name,
                    size=size,
                    format=rec.source_format or "bin",
                    checksum=rec.content_hash or rec.source_checksum or "",
                )
            )

    # Single-pass walk for size_hint + modified_time (no double traversal).
    # Uses os.walk(followlinks=False) to prevent symlink-based traversal.
    size_hint = 0
    max_mtime: float = 0.0
    file_count = 0
    for dirpath, _dirnames, filenames in os.walk(bundle_dir, followlinks=False):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            # Skip symlinks (lstat to detect without following).
            if fpath.is_symlink():
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            file_count += 1
            if file_count > _MAX_FILES_PER_BUNDLE:
                warnings.append(
                    BundleWarning(
                        str(bundle_dir),
                        f"file count exceeds {_MAX_FILES_PER_BUNDLE}; size_hint truncated",
                    )
                )
                break
            size_hint += st.st_size
            if st.st_mtime > max_mtime:
                max_mtime = st.st_mtime
            if size_hint > _MAX_BUNDLE_SIZE_HINT:
                size_hint = _MAX_BUNDLE_SIZE_HINT
                break
        else:
            continue
        break  # inner break hit

    modified_time = ""
    if max_mtime > 0:
        import datetime as _dt

        modified_time = _dt.datetime.fromtimestamp(max_mtime, _dt.UTC).isoformat()

    # Bundle checksum: aggregate of attachment checksums (deterministic).
    checksum = ""
    att_checksums = sorted(a.checksum for a in attachment_refs if a.checksum)
    if att_checksums:
        import hashlib

        checksum = hashlib.sha256("".join(att_checksums).encode()).hexdigest()

    return BundleIndexEntry(
        path=str(bundle_dir.resolve()),
        primary_file=primary_file,
        identifier=identifier,
        metadata_summary=metadata_summary,
        attachment_count=len(attachment_refs),
        attachment_refs=attachment_refs,
        checksum=checksum,
        modified_time=modified_time,
        size_hint=size_hint,
        warning_count=len(warnings),
        warnings=warnings,
    )


def index_bundles(
    db: MemoryDB,
    root: str | Path,
    *,
    max_depth: int = 10,
) -> BundleIndexResult:
    """Discover bundles under ``root`` and persist their index entries in the DB.

    Upserts each :class:`BundleIndexEntry` into the ``bundle_index`` SQLite table
    in a single transaction (one commit, not per-entry). Returns a
    :class:`BundleIndexResult` with discovered/indexed counts and accumulated
    warnings.

    To rebuild the index from scratch: call ``db.clear_bundle_index()`` then
    ``index_bundles(db, root)``. To reconcile against the filesystem (detect
    stale entries), compare ``list_bundle_index()`` paths against a fresh
    ``discover_bundles(root)`` — full reconciliation is deferred to a future
    follow-up.
    """
    entries = discover_bundles(root, max_depth=max_depth)
    result = BundleIndexResult(discovered=len(entries))

    with Timer() as t:
        # Batch: single commit for all upserts (1 fsync, not N).
        for entry in entries:
            db.upsert_bundle_index(entry, _commit=False)
            result.indexed += 1
            result.warnings.extend(entry.warnings)
        db._conn.commit()  # noqa: SLF001

    _trace.info(
        "index",
        f"indexed {result.indexed} bundles into DB",
        detail={
            "root": str(root),
            "discovered": result.discovered,
            "indexed": result.indexed,
            "warnings": len(result.warnings),
            "ms": round(t.ms, 2),
        },
    )
    return result
