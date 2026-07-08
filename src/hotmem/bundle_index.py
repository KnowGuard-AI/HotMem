"""HotMem bundle index — discovery and lightweight indexing of local bundles.

Purpose:
     Make local bundle directories discoverable and searchable without eagerly
     hydrating every byte. Walks configured directory trees, recognizes bundle
     markers (memory.md, index.md, README.md), and builds a metadata-only index.

     Discovery reads the bundle's memory body and metadata (markdown + JSON/YAML),
     but does NOT read attachment bytes. The index is persisted in a lightweight
     SQLite table so it survives restarts and is queryable.

Interface:
     BundleIndexEntry (dataclass): path, primary_file, identifier, metadata_summary,
         attachment_count, attachment_refs, modified_time, size_hint,
         warning_count, warnings
     discover_bundles(root, *, max_depth=10) -> list[BundleIndexEntry]
     index_bundles(db, root, *, max_depth=10) -> BundleIndexResult

Deps: hotmem.bundle, hotmem.db, hotmem.trace
Extension: add remote bundle discovery, incremental re-indexing, or watch-mode here.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass, field
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


@dataclass
class AttachmentRef:
    """A reference to an attachment file within a bundle (metadata only)."""

    name: str
    size: int
    format: str


@dataclass
class BundleIndexEntry:
    """Metadata-only index entry for a discovered bundle."""

    path: str
    primary_file: str
    identifier: str
    metadata_summary: dict[str, Any] = field(default_factory=dict)
    attachment_count: int = 0
    attachment_refs: list[AttachmentRef] = field(default_factory=list)
    modified_time: str = ""
    size_hint: int = 0
    warning_count: int = 0
    warnings: list[BundleWarning] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["attachment_refs"] = [asdict(a) for a in self.attachment_refs]
        d["warnings"] = [str(w) for w in self.warnings]
        return d


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
    only ``Path.stat()`` is called for size hints.

    Unknown files in bundles produce warnings (permissive mode).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"bundle root not found or not a directory: {root}")

    entries: list[BundleIndexEntry] = []

    with Timer() as t:
        for dirpath, dirnames, _filenames in os.walk(root):
            # Enforce max_depth: root is depth 0, direct children depth 1, etc.
            # When rel_depth >= max_depth, don't descend further but still
            # process the current directory (it may be a bundle).
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


def _build_index_entry(bundle_dir: Path) -> BundleIndexEntry | None:
    """Build a BundleIndexEntry from a single bundle directory.

    Uses ``parse_bundle()`` to extract records + warnings without DB insertion.
    Reads the memory body (markdown) and metadata, but does NOT read attachment
    bytes — only stat() for size hints.
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

    # Build attachment refs from file-backed records (metadata only, no reads).
    attachment_refs: list[AttachmentRef] = []
    for rec in records:
        if rec.memory_type == "file" and rec.source_uri:
            try:
                size = Path(rec.source_uri).stat().st_size
            except OSError:
                size = rec.byte_length or 0
            attachment_refs.append(
                AttachmentRef(
                    name=rec.fact_summary or Path(rec.source_uri).name,
                    size=size,
                    format=rec.source_format or "bin",
                )
            )

    # Size hint: sum of all file sizes in the bundle directory (no reads).
    size_hint = 0
    try:
        for f in bundle_dir.rglob("*"):
            if f.is_file():
                with contextlib.suppress(OSError):
                    size_hint += f.stat().st_size
    except OSError:
        pass

    # Modified time: most recent mtime in the bundle directory.
    modified_time = ""
    try:
        mtimes = [f.stat().st_mtime for f in bundle_dir.rglob("*") if f.is_file()]
        if mtimes:
            import datetime as _dt

            modified_time = _dt.datetime.fromtimestamp(max(mtimes), _dt.UTC).isoformat()
    except OSError:
        pass

    return BundleIndexEntry(
        path=str(bundle_dir.resolve()),
        primary_file=primary_file,
        identifier=identifier,
        metadata_summary=metadata_summary,
        attachment_count=len(attachment_refs),
        attachment_refs=attachment_refs,
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

    Upserts each :class:`BundleIndexEntry` into the ``bundle_index`` SQLite table.
    Returns a :class:`BundleIndexResult` with discovered/indexed counts and
    accumulated warnings.
    """
    entries = discover_bundles(root, max_depth=max_depth)
    result = BundleIndexResult(discovered=len(entries))

    with Timer() as t:
        for entry in entries:
            db.upsert_bundle_index(entry)
            result.indexed += 1
            result.warnings.extend(entry.warnings)

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
