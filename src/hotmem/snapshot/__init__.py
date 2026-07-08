"""HotMem snapshot — unified dispatch over v2 directory and legacy JSONL formats.

Purpose:
     Single entry point for snapshot (export) and hydrate (import) that picks
     the right format from the path, so callers keep one flag (``--file``) and
     one set of API endpoints (``/v1/snapshot``, ``/v1/hydrate``).

Path heuristic:
      snapshot(db, path):
          - path ends in ``.jsonl`` or ``.jsonl.gz`` -> legacy single-file writer
          - otherwise (directory, or no extension)        -> v2 directory writer
      hydrate(db, path):
          - path is a file or ends in ``.jsonl``/``.jsonl.gz`` -> legacy reader
          - path is a directory with ``memory.md``             -> bundle reader (#52)
          - path is a directory with ``manifest.json``         -> v2 reader
          - path is a directory with ``memories.jsonl`` only   -> legacy reader
          - otherwise                                            -> error

Interface:
      snapshot(db, path, *, copy_attachments=False, base_dir=None) -> SnapshotResult
      hydrate(db, path) -> HydrateResult
      detect_format(path) -> 'v2' | 'legacy' | 'bundle'
      SnapshotChecksumError (re-export)

Deps: hotmem.bundle, hotmem.swap, hotmem.snapshot.reader, hotmem.snapshot.writer
Extension: add new formats by extending detect_format and the dispatch fns.
"""

from __future__ import annotations

from pathlib import Path

from hotmem.bundle import MEMORY_MD as BUNDLE_MARKER
from hotmem.bundle import detect_bundle, read_bundle
from hotmem.db import MemoryDB
from hotmem.snapshot.format import SnapshotChecksumError
from hotmem.snapshot.reader import MANIFEST_NAME, MEMORIES_NAME, detect_v2, hydrate_v2
from hotmem.snapshot.writer import write_snapshot_v2
from hotmem.swap import HydrateResult, SnapshotResult
from hotmem.swap import hydrate as legacy_hydrate
from hotmem.swap import snapshot as legacy_snapshot
from hotmem.trace import get_tracer

_trace = get_tracer("snapshot")

LEGACY_SUFFIXES: tuple[str, ...] = (".jsonl", ".jsonl.gz")


def detect_format(path: str | Path) -> str:
    """Return ``'v2'``, ``'legacy'``, or ``'bundle'`` for a snapshot path.

    A path that already exists as a directory is classified by its contents
    (memory.md -> bundle; manifest.json -> v2; memories.jsonl -> legacy).
    A path that doesn't exist is classified by its suffix
    (``.jsonl``/``.jsonl.gz`` -> legacy, else v2 directory).
    """
    p = Path(path)
    if p.exists():
        if p.is_dir():
            if (p / BUNDLE_MARKER).is_file():
                return "bundle"
            if (p / MANIFEST_NAME).is_file():
                return "v2"
            if (p / MEMORIES_NAME).is_file():
                return "legacy"
            raise FileNotFoundError(f"no snapshot found in directory: {p}")
        # Existing file -> legacy.
        return "legacy"
    # Not-yet-created path: classify by suffix.
    name = p.name
    if any(name.endswith(suf) for suf in LEGACY_SUFFIXES):
        return "legacy"
    return "v2"


def snapshot(
    db: MemoryDB,
    path: str | Path,
    *,
    copy_attachments: bool = False,
    base_dir: str | Path | None = None,
) -> SnapshotResult:
    """Export the DB to ``path`` using the format inferred from the path.

    ``.jsonl``/``.jsonl.gz`` -> legacy single-file; otherwise v2 directory.
    ``copy_attachments`` and ``base_dir`` only apply to the v2 writer.
    """
    fmt = detect_format(path)
    if fmt == "legacy":
        _trace.info("dispatch", "legacy single-file snapshot", detail={"path": str(path)})
        return legacy_snapshot(db, path)
    _trace.info("dispatch", "v2 directory snapshot", detail={"path": str(path)})
    return write_snapshot_v2(db, path, copy_attachments=copy_attachments, base_dir=base_dir)


def hydrate(db: MemoryDB, path: str | Path) -> HydrateResult:
    """Import memories from ``path`` using the format inferred from the path.

    ``.jsonl``/``.jsonl.gz`` file, or a directory with only ``memories.jsonl``
    -> legacy reader. A directory with ``manifest.json`` -> v2 reader (with
    manifest checksum verification).
    """
    p = Path(path)
    if not p.exists():
        _trace.warn("dispatch", "snapshot path not found", detail={"path": str(p)})
        return HydrateResult(loaded=0, skipped_dupes=0)

    if p.is_dir():
        if detect_bundle(p):
            _trace.info("dispatch", "bundle hydrate", detail={"path": str(p)})
            return read_bundle(db, p).as_hydrate_result
        if detect_v2(p):
            _trace.info("dispatch", "v2 directory hydrate", detail={"path": str(p)})
            return hydrate_v2(db, p)
        if (p / MEMORIES_NAME).is_file():
            _trace.info(
                "dispatch",
                "legacy hydrate (memories.jsonl, no manifest)",
                detail={"path": str(p / MEMORIES_NAME)},
            )
            return legacy_hydrate(db, p / MEMORIES_NAME)
        # Directory with neither memory.md, manifest, nor memories.jsonl.
        raise SnapshotChecksumError("missing_manifest", file=str(p / MANIFEST_NAME))

    # File -> legacy reader.
    _trace.info("dispatch", "legacy single-file hydrate", detail={"path": str(p)})
    return legacy_hydrate(db, p)


__all__ = [
    "HydrateResult",
    "SnapshotChecksumError",
    "SnapshotResult",
    "detect_bundle",
    "detect_format",
    "detect_v2",
    "hydrate",
    "snapshot",
]
