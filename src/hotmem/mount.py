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

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hotmem import __version__
from hotmem.trace import get_tracer

_trace = get_tracer("mount")

LOCK_FILENAME = "daemon.owner.lock"

# Process-local guard: Windows byte-range locks are not reliably exclusive
# across handles in the same process, so track held paths explicitly.
_HELD_LOCKS: set[str] = set()
_HELD_LOCKS_GUARD = threading.Lock()


class StoreOwnershipError(RuntimeError):
    """Raised when a store or mount is already owned by another live HotMem process."""

    def __init__(self, message: str, *, owner: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.owner = owner or {}


class StoreIdentityError(RuntimeError):
    """Raised when the configured store id contradicts the store's persistent identity."""


class StoreLock:
    """OS-backed exclusive store ownership lock with stale-owner recovery (#109)."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        store_id: str = "",
        port: int = 8711,
        runtime_generation: str = "",
    ) -> None:
        self.db_path = str(db_path)
        self.store_id = store_id
        self.port = port
        self.runtime_generation = runtime_generation
        self.lock_path: Path | None = None
        self._fd: int | None = None
        if self.db_path and self.db_path != ":memory:":
            resolved = Path(self.db_path).resolve()
            self.lock_path = resolved.parent / f".{resolved.name}.{LOCK_FILENAME}"

    def acquire(self) -> None:
        if self.lock_path is None or self._fd is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.lock_path)
        with _HELD_LOCKS_GUARD:
            if key in _HELD_LOCKS:
                owner = self.read_owner() or {}
                raise StoreOwnershipError(
                    f"store {self.db_path!r} is already owned by this process",
                    owner=owner,
                )
        existing_info = self.read_owner()
        fd = os.open(key, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            os.close(fd)
            owner = self.read_owner() or existing_info or {}
            pid = owner.get("pid", "unknown")
            sid = owner.get("store_id", self.store_id or "unknown")
            port = owner.get("port", "unknown")
            raise StoreOwnershipError(
                f"store {self.db_path!r} is already owned by a live HotMem process "
                f"(pid={pid}, store_id={sid}, port={port}); route requests through "
                "the running daemon (--url) or stop it before running offline operations",
                owner=owner,
            ) from err

        if existing_info and existing_info.get("pid") not in (None, os.getpid()):
            _trace.info(
                "ownership",
                "recovered stale store lock from previous process",
                detail={"previous_owner": existing_info, "db_path": self.db_path},
            )

        owner_payload = {
            "pid": os.getpid(),
            "db_path": self.db_path,
            "store_id": self.store_id,
            "port": self.port,
            "runtime_generation": self.runtime_generation,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_owner_payload(fd, owner_payload)
        self._fd = fd
        with _HELD_LOCKS_GUARD:
            _HELD_LOCKS.add(key)

    def _write_owner_payload(self, fd: int, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, raw)
        os.fsync(fd)

    def write_owner(self) -> None:
        """Refresh the owner metadata (e.g. after adopting the store's persistent id)."""
        if self._fd is None:
            return
        self._write_owner_payload(
            self._fd,
            {
                "pid": os.getpid(),
                "db_path": self.db_path,
                "store_id": self.store_id,
                "port": self.port,
                "runtime_generation": self.runtime_generation,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with _HELD_LOCKS_GUARD:
                _HELD_LOCKS.discard(str(self.lock_path))
            # The lock file is intentionally NOT unlinked: unlinking after
            # unlocking races with a competing owner (a waiter can hold a
            # lock on an unlinked inode while a third process creates and
            # locks a fresh path). Stale metadata is overwritten by the next
            # successful acquire, and the OS releases the advisory lock on
            # process death.
            with contextlib.suppress(OSError):
                os.close(fd)

    def __enter__(self) -> StoreLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    def read_owner(self) -> dict[str, Any] | None:
        if self.lock_path is None or not self.lock_path.exists():
            return None
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except Exception:
            return None


def ensure_store_available(db_path: str | Path) -> None:
    """Verify that `db_path` is not currently locked by a live daemon process."""
    lock = StoreLock(db_path)
    lock.acquire()
    lock.release()


@contextlib.contextmanager
def store_ownership(
    db_path: str | Path,
    *,
    store_id: str = "",
    port: int = 8711,
    runtime_generation: str = "",
):
    """Hold exclusive store ownership for the duration of an offline mutation.

    Offline CLI writers must keep this lock from the ownership check through
    the write/commit and close — a check-then-release window lets a daemon
    start in between and become a second writer.
    """
    lock = StoreLock(
        db_path,
        store_id=store_id,
        port=port,
        runtime_generation=runtime_generation,
    )
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


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
        config.manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        _trace.info("bootstrap", "created mount directory", detail={"path": str(mount_path)})
    else:
        _trace.info("bootstrap", "using existing mount", detail={"path": str(mount_path)})

    return config
