"""HotMem snapshot — unified dispatch over interchange, v2, bundle, and legacy formats.

Purpose:
     Single entry point for snapshot (export) and hydrate (import) that picks
     the right format from the path, so callers keep one flag (``--file``) and
     one set of API endpoints (``/v1/snapshot``, ``/v1/hydrate``).

Path heuristic:
      snapshot(db, path, package=False, gz=False):
          - package=True                                     -> interchange v1 package
          - path ends in ``.jsonl`` or ``.jsonl.gz``         -> legacy single-file writer
          - otherwise (directory, or no extension)            -> v2 directory writer
      hydrate(db, path):
          - path is a file or ends in ``.jsonl``/``.jsonl.gz`` -> legacy reader
          - path is a directory with ``memory.md``              -> bundle reader (#52)
          - path is a directory with a hotmem-interchange-v1
            manifest                                           -> package reader (#69)
          - path is a directory with a v2 manifest              -> v2 reader
          - path is a directory with ``memories.jsonl`` only    -> legacy reader
          - otherwise                                           -> error

Interface:
      snapshot(db, path, *, package=False, gz=False, copy_attachments=False, base_dir=None)
      hydrate(db, path) -> HydrateResult
      verify(path) -> dict  (v2 manifest or interchange package verification)
      detect_format(path) -> 'package' | 'v2' | 'legacy' | 'bundle'
      SnapshotChecksumError / PackageError (re-exports)

Deps: hotmem.bundle, hotmem.interchange, hotmem.swap, hotmem.snapshot.{reader,writer}
Extension: add new formats by extending detect_format and the dispatch fns.
"""

from __future__ import annotations

import json
from pathlib import Path

from hotmem.bundle import detect_bundle, read_bundle
from hotmem.db import MemoryDB
from hotmem.interchange.hydrate import PackageError, hydrate_package, verify_package
from hotmem.interchange.package import FORMAT_ID as PACKAGE_FORMAT_ID
from hotmem.interchange.package import MANIFEST_NAME as PACKAGE_MANIFEST_NAME
from hotmem.snapshot.format import SnapshotChecksumError
from hotmem.snapshot.reader import MANIFEST_NAME, MEMORIES_NAME, detect_v2, hydrate_v2
from hotmem.snapshot.reader import verify_manifest as verify_v2_manifest
from hotmem.snapshot.writer import write_snapshot_v2
from hotmem.swap import HydrateResult, SnapshotResult
from hotmem.swap import hydrate as legacy_hydrate
from hotmem.swap import snapshot as legacy_snapshot
from hotmem.trace import get_tracer

_trace = get_tracer("snapshot")

LEGACY_SUFFIXES: tuple[str, ...] = (".jsonl", ".jsonl.gz")


def _manifest_format(p: Path) -> str | None:
    """Read the ``format`` field of a directory's manifest.json, if any.

    Distinguishes an interchange package (hotmem-interchange-v1) from a
    Snapshot v2 directory (hotmem-snapshot-v2) without guessing from layout —
    both carry manifest.json (#69 dispatch).
    """
    manifest_path = p / PACKAGE_MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(manifest, dict):
        return str(manifest.get("format") or "")
    return None


def detect_format(path: str | Path) -> str:
    """Return ``'package'``, ``'v2'``, ``'legacy'``, or ``'bundle'`` for a path.

    Uses ``detect_bundle()`` (the single source of truth for bundle detection)
    so detection matches hydrate dispatch. A path that already exists as a
    directory is classified by its contents (memory.md/index.md/README.md ->
    bundle; hotmem-interchange-v1 manifest -> package; manifest.json -> v2;
    memories.jsonl -> legacy). A path that doesn't exist is classified by its
    suffix (``.jsonl``/``.jsonl.gz`` -> legacy, else v2 directory).
    """
    p = Path(path)
    if p.exists():
        if p.is_dir():
            if detect_bundle(p):
                return "bundle"
            if (p / MANIFEST_NAME).is_file():
                return "package" if _manifest_format(p) == PACKAGE_FORMAT_ID else "v2"
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
    package: bool = False,
    gz: bool = False,
    copy_attachments: bool = False,
    base_dir: str | Path | None = None,
) -> SnapshotResult:
    """Export the DB to ``path`` using the format inferred from the path.

    ``package=True`` -> hotmem-interchange-v1 package (with ``gz=True`` for a
    compressed payload); ``.jsonl``/``.jsonl.gz`` -> legacy single-file;
    otherwise v2 directory. ``copy_attachments`` and ``base_dir`` only apply
    to the v2 writer.
    """
    if package:
        from hotmem.interchange.package import write_package

        _trace.info("dispatch", "interchange package snapshot", detail={"path": str(path)})
        result = write_package(db, path, gz=gz)
        return SnapshotResult(exported=result.exported, path=result.path)

    fmt = detect_format(path)
    if fmt == "legacy":
        _trace.info("dispatch", "legacy single-file snapshot", detail={"path": str(path)})
        return legacy_snapshot(db, path)
    _trace.info("dispatch", "v2 directory snapshot", detail={"path": str(path)})
    return write_snapshot_v2(db, path, copy_attachments=copy_attachments, base_dir=base_dir)


def hydrate(db: MemoryDB, path: str | Path) -> HydrateResult:
    """Import memories from ``path`` using the format inferred from the path.

    ``.jsonl``/``.jsonl.gz`` file, or a directory with only ``memories.jsonl``
    -> legacy reader. A directory with a hotmem-interchange-v1 manifest ->
    package reader (#69, verify-then-hydrate transactional restore). A
    directory with a v2 manifest -> v2 reader (with manifest checksum
    verification).

    When a directory has both a bundle marker (memory.md) AND a v2 manifest,
    the v2 manifest takes precedence (stricter, checksummed format) and a
    warning is logged.
    """
    p = Path(path)
    if not p.exists():
        _trace.warn("dispatch", "snapshot path not found", detail={"path": str(p)})
        return HydrateResult(loaded=0, skipped_dupes=0)

    if p.is_dir():
        is_bundle = detect_bundle(p)
        is_manifest_dir = (p / MANIFEST_NAME).is_file()
        if is_bundle and is_manifest_dir:
            _trace.warn(
                "dispatch",
                "ambiguous: both bundle marker and manifest present; preferring manifest",
                detail={"path": str(p)},
            )
            is_bundle = False
        if is_bundle:
            _trace.info("dispatch", "bundle hydrate", detail={"path": str(p)})
            return read_bundle(db, p).as_hydrate_result
        if is_manifest_dir:
            if _manifest_format(p) == PACKAGE_FORMAT_ID:
                _trace.info("dispatch", "package hydrate", detail={"path": str(p)})
                return hydrate_package(db, p)
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


def verify(path: str | Path) -> dict:
    """Verify a package or snapshot directory; return a diagnostic summary.

    Raises SnapshotChecksumError (v2) or PackageError (interchange) with
    structured reason/file/expected/actual on failure — one entry point for
    ``hotmem verify`` (#69).
    """
    fmt = detect_format(path)
    if fmt == "package":
        verified = verify_package(path)
        try:
            summary = {
                "format": fmt,
                "valid": True,
                "record_count": verified.record_count,
                "logical_id": verified.manifest.get("logical_id"),
                "payload": verified.payload_name,
            }
        finally:
            verified.cleanup()
        return summary
    if fmt == "v2":
        manifest = verify_v2_manifest(path)
        return {
            "format": fmt,
            "valid": True,
            "record_count": manifest.memory_count,
            "snapshot_id": manifest.snapshot_id,
            "payload": MEMORIES_NAME,
        }
    raise ValueError(f"not a verifiable snapshot directory: {path} (detected {fmt})")


__all__ = [
    "HydrateResult",
    "PackageError",
    "SnapshotChecksumError",
    "SnapshotResult",
    "detect_bundle",
    "detect_format",
    "detect_v2",
    "hydrate",
    "snapshot",
    "verify",
]
