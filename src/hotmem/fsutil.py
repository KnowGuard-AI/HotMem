"""Filesystem crash-safety primitives shared across HotMem writers.

Purpose:
    One implementation of the durability-critical publish sequence: fsync a
    directory entry so a rename survives a crash, and move a fully-written
    staging directory into place atomically — readers never observe a
    half-written artifact and a failed publish never destroys the previous
    one.

    Extracted verbatim from hotmem.interchange.package (#69) so the handoff
    package writer (#101) reuses the same primitive instead of duplicating
    crash-safety-critical code.

Interface:
    fsync_dir(path) — flush a directory entry to disk
    atomic_publish(staging, final) — staging-dir + rename publish

Deps: stdlib only.
Extension: additional staging-based writers (snapshot, delta, handoff)
    import from here; never reimplement.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


def fsync_dir(path: Path) -> None:
    """Flush a directory entry to disk so a rename survives a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_publish(staging: Path, final: Path) -> None:
    """Move a fully-written staging directory into place atomically.

    If ``final`` exists it is moved aside first, then removed after the
    swap — a failed publish never destroys the previous artifact.
    """
    backup: Path | None = None
    if final.exists():
        backup = final.with_name(f".{final.name}.old-{uuid.uuid4().hex[:8]}")
        os.replace(final, backup)
    try:
        os.replace(staging, final)
    except Exception:
        if backup is not None and not final.exists():
            os.replace(backup, final)  # restore the previous artifact
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
