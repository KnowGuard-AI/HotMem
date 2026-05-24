"""HotMem mount — portable memory directory management.

Purpose:
    Bootstrap and manage a mount directory that contains hotmem.sqlite,
    swap.jsonl, and manifest.json. Any directory can become a HotMem mount.

Interface:
    MountConfig(mount_path) — resolves paths within the mount
    bootstrap_mount(mount_path) -> MountConfig — creates dir + manifest if needed

Deps: hotmem.trace
Extension: add manifest versioning, remote mount sync, or encryption here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hotmem import __version__
from hotmem.trace import get_tracer

_trace = get_tracer("mount")


@dataclass
class MountConfig:
    """Resolved paths within a HotMem mount directory."""

    mount_path: Path
    db_path: Path
    swap_path: Path
    manifest_path: Path


def bootstrap_mount(mount_path: str | Path) -> MountConfig:
    """Ensure a mount directory exists with the expected structure.

    Creates the directory and manifest.json if they don't exist.
    Returns resolved paths for db, swap, and manifest files.
    """
    mount_path = Path(mount_path).resolve()
    mount_path.mkdir(parents=True, exist_ok=True)

    config = MountConfig(
        mount_path=mount_path,
        db_path=mount_path / "hotmem.sqlite",
        swap_path=mount_path / "swap.jsonl",
        manifest_path=mount_path / "manifest.json",
    )

    if not config.manifest_path.exists():
        manifest = {
            "hotmem_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "mount_path": str(mount_path),
        }
        config.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        _trace.info("bootstrap", "created mount directory", detail={"path": str(mount_path)})
    else:
        _trace.info("bootstrap", "using existing mount", detail={"path": str(mount_path)})

    return config
