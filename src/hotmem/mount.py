"""HotMem mount -- portable memory directory management and lineage contract.

Purpose:
    Bootstrap and manage a mount directory that contains hotmem.sqlite,
    swap.jsonl, and manifest.json, and define portable logical mount identity
    separately from runtime/replica and event-history identity (#94).

Interface:
    MountConfig(mount_path, db_path, swap_path, manifest_path, mount_id)
    MountIdentity(mount_id, replica_id, history_id, ...)
    bootstrap_mount(mount_path, *, mount_id=None) -> MountConfig
    compare_mount_lineage(source, target) -> dict

Deps: hotmem.trace, hotmem.interchange.fingerprint
Extension: add manifest versioning, remote mount sync, or encryption here.
           Multi-writer ordering and automatic fork conflict resolution are
           intentionally undefined (#94).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hotmem import __version__
from hotmem.trace import get_tracer

_trace = get_tracer("mount")

VALID_MOUNT_RELATIONSHIPS = (
    "same_replica",
    "clone",
    "fork",
    "replacement",
    "different_mount",
)

MUTATING_EVENT_TYPES = frozenset(
    {
        "memory.created",
        "memory.promotion",
        "memory.candidate",
        "sync.applied",
        "handoff.applied",
    }
)


def new_mount_id() -> str:
    """Mint a new logical mount/workspace identifier."""
    return f"mnt-{uuid.uuid4().hex}"


def new_replica_id() -> str:
    """Mint a new runtime/replica store instance identifier."""
    return f"rep-{uuid.uuid4().hex}"


def new_history_id() -> str:
    """Mint a new local SQLite event-history lineage identifier."""
    return f"hist-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class MountIdentity:
    """Portable logical mount identity vs. runtime replica and history lineage (#94).

    - ``mount_id`` identifies the logical workspace/cassette and survives clone,
      restore, and fork of the same mount.
    - ``replica_id`` identifies a concrete SQLite store instance and is
      regenerated on clone, restore, fork, or replacement.
    - ``history_id`` identifies the local SQLite event-log epoch for this
      replica and is regenerated whenever a replica is cloned, restored, forked,
      or replaced.
    - ``runtime_generation`` is process-ephemeral (regenerated per daemon/runtime
      startup).

    Multi-writer ordering and conflict resolution across divergent replicas are
    intentionally undefined.
    """

    mount_id: str
    replica_id: str
    history_id: str
    runtime_generation: str = ""
    base_snapshot_id: str = ""
    base_state_fingerprint: str = ""
    base_source_mount_id: str = ""
    base_source_replica_id: str = ""
    base_history_id: str = ""
    base_cursor: str = ""
    parent_replica_id: str = ""
    forked_at_seq: int = 0
    forked_at_event_id: str = ""
    cloned_at_seq: int = 0
    trimmed_through_seq: int = 0
    legacy_history: bool = False
    mount_bound: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mount_id": self.mount_id,
            "replica_id": self.replica_id,
            "history_id": self.history_id,
            "runtime_generation": self.runtime_generation,
            "base_snapshot_id": self.base_snapshot_id,
            "base_state_fingerprint": self.base_state_fingerprint,
            "base_source_mount_id": self.base_source_mount_id,
            "base_source_replica_id": self.base_source_replica_id,
            "base_history_id": self.base_history_id,
            "base_cursor": self.base_cursor,
            "parent_replica_id": self.parent_replica_id,
            "forked_at_seq": self.forked_at_seq,
            "forked_at_event_id": self.forked_at_event_id,
            "cloned_at_seq": self.cloned_at_seq,
            "trimmed_through_seq": self.trimmed_through_seq,
            "legacy_history": self.legacy_history,
            "mount_bound": self.mount_bound,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MountIdentity:
        source_block = data.get("source") if isinstance(data.get("source"), Mapping) else {}
        return cls(
            mount_id=str(data.get("mount_id") or source_block.get("mount_id") or ""),
            replica_id=str(data.get("replica_id") or source_block.get("replica_id") or ""),
            history_id=str(data.get("history_id") or source_block.get("history_id") or ""),
            runtime_generation=str(data.get("runtime_generation") or ""),
            base_snapshot_id=str(
                data.get("base_snapshot_id")
                or data.get("snapshot_id")
                or data.get("logical_id")
                or ""
            ),
            base_state_fingerprint=str(
                data.get("base_state_fingerprint") or data.get("state_fingerprint") or ""
            ),
            base_source_mount_id=str(data.get("base_source_mount_id") or ""),
            base_source_replica_id=str(data.get("base_source_replica_id") or ""),
            base_history_id=str(data.get("base_history_id") or ""),
            base_cursor=str(data.get("base_cursor") or data.get("replay_cursor") or ""),
            parent_replica_id=str(data.get("parent_replica_id") or ""),
            forked_at_seq=int(data.get("forked_at_seq") or 0),
            forked_at_event_id=str(data.get("forked_at_event_id") or ""),
            cloned_at_seq=int(data.get("cloned_at_seq") or 0),
            trimmed_through_seq=int(data.get("trimmed_through_seq") or 0),
            legacy_history=bool(data.get("legacy_history", False)),
            mount_bound=bool(data.get("mount_bound", False)),
        )


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
    the write/commit and close -- a check-then-release window lets a daemon
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
    """Resolved paths and logical mount identity within a HotMem mount directory."""

    mount_path: Path
    db_path: Path
    swap_path: Path
    manifest_path: Path
    mount_id: str = ""


def bootstrap_mount(
    mount_path: str | Path,
    *,
    mount_id: str | None = None,
) -> MountConfig:
    """Ensure a mount directory exists with the expected structure and logical mount_id.

    Creates the directory and manifest.json if they don't exist.
    Returns resolved paths for db, swap, and manifest files along with mount_id.
    """
    mount_path = Path(mount_path).resolve()
    mount_path.mkdir(parents=True, exist_ok=True)
    explicit_mount_id = (mount_id or "").strip()

    manifest_path = mount_path / "manifest.json"
    resolved_mount_id = explicit_mount_id

    if not manifest_path.exists():
        resolved_mount_id = explicit_mount_id or new_mount_id()
        manifest = {
            "hotmem_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "mount_path": str(mount_path),
            "mount_id": resolved_mount_id,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        _trace.info(
            "bootstrap",
            "created mount directory",
            detail={"path": str(mount_path), "mount_id": resolved_mount_id},
        )
    else:
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        stored_mount_id = (
            str(existing.get("mount_id") or "").strip() if isinstance(existing, dict) else ""
        )
        if explicit_mount_id and stored_mount_id and explicit_mount_id != stored_mount_id:
            raise ValueError(
                f"Mount manifest at {manifest_path} is bound to mount_id={stored_mount_id!r}, "
                f"not {explicit_mount_id!r}."
            )
        resolved_mount_id = stored_mount_id or explicit_mount_id
        _trace.info(
            "bootstrap",
            "using existing mount",
            detail={"path": str(mount_path), "mount_id": resolved_mount_id},
        )

    return MountConfig(
        mount_path=mount_path,
        db_path=mount_path / "hotmem.sqlite",
        swap_path=mount_path / "swap.jsonl",
        manifest_path=manifest_path,
        mount_id=resolved_mount_id,
    )


@dataclass
class _LineageProbe:
    identity: MountIdentity
    snapshot_id: str
    state_fingerprint: str
    head_seq: int
    head_event_id: str
    event_map: dict[int, str]
    post_base_mutations: int


def _extract_lineage_probe(target: Any) -> _LineageProbe:
    """Normalize a MemoryDB, MountIdentity, manifest Path, or mapping for comparison."""
    from hotmem.interchange.fingerprint import state_fingerprint as compute_state_fp

    if isinstance(target, MountIdentity):
        return _LineageProbe(
            identity=target,
            snapshot_id=target.base_snapshot_id,
            state_fingerprint=target.base_state_fingerprint,
            head_seq=max(target.cloned_at_seq, target.forked_at_seq, target.trimmed_through_seq),
            head_event_id=target.forked_at_event_id,
            event_map={},
            post_base_mutations=0,
        )

    if isinstance(target, (str, Path)):
        path = Path(target)
        manifest_file = path / "manifest.json" if path.is_dir() else path
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        return _extract_lineage_probe(data)

    if isinstance(target, Mapping):
        ident = MountIdentity.from_dict(target)
        snap_id = str(
            target.get("snapshot_id") or target.get("logical_id") or ident.base_snapshot_id or ""
        )
        state_fp = str(target.get("state_fingerprint") or ident.base_state_fingerprint or "")
        return _LineageProbe(
            identity=ident,
            snapshot_id=snap_id,
            state_fingerprint=state_fp,
            head_seq=int(target.get("head_seq") or 0),
            head_event_id=str(target.get("head_event_id") or ""),
            event_map={},
            post_base_mutations=int(target.get("post_base_mutations") or 0),
        )

    if hasattr(target, "mount_identity") and hasattr(target, "all_rows"):
        ident = target.mount_identity()
        bounds = target.event_log_bounds()
        rows = target.all_rows()
        state_fp = compute_state_fp(rows)
        from hotmem.interchange.canonical import logical_id

        snap_id = logical_id(r.get("content_hash", "") for r in rows if r.get("content_hash"))
        event_rows = target.query_events(limit=10000, ascending=True)
        event_map = {int(r["seq"]): str(r["event_id"]) for r in event_rows}
        anchor_seq = max(ident.cloned_at_seq, ident.forked_at_seq)
        post_events = sum(
            1
            for r in event_rows
            if int(r["seq"]) > anchor_seq and str(r["event_type"]) in MUTATING_EVENT_TYPES
        )
        fp_diverged = bool(
            ident.base_state_fingerprint and state_fp != ident.base_state_fingerprint
        )
        post_mutations = post_events + (1 if fp_diverged else 0)
        return _LineageProbe(
            identity=ident,
            snapshot_id=snap_id,
            state_fingerprint=state_fp,
            head_seq=int(bounds["head_seq"]),
            head_event_id=str(bounds["head_event_id"]),
            event_map=event_map,
            post_base_mutations=post_mutations,
        )

    raise TypeError(f"Unsupported lineage target for comparison: {type(target)!r}")


def compare_mount_lineage(source: Any, target: Any) -> dict[str, Any]:
    """Classify the relationship between two mounts, replicas, or manifests (#94).

    Returns a dictionary containing:
    - ``relationship``: one of ``same_replica``, ``clone``, ``fork``,
      ``replacement``, or ``different_mount``
    - ``same_mount``: bool
    - ``same_replica``: bool
    - ``reason``: machine-readable classification reason
    - ``source_mount_id``, ``target_mount_id``
    - ``source_replica_id``, ``target_replica_id``
    - ``source_history_id``, ``target_history_id``
    """
    src = _extract_lineage_probe(source)
    dst = _extract_lineage_probe(target)
    s_id = src.identity
    t_id = dst.identity

    base_result = {
        "source_mount_id": s_id.mount_id,
        "target_mount_id": t_id.mount_id,
        "source_replica_id": s_id.replica_id,
        "target_replica_id": t_id.replica_id,
        "source_history_id": s_id.history_id,
        "target_history_id": t_id.history_id,
    }

    if not s_id.mount_id or not t_id.mount_id or s_id.mount_id != t_id.mount_id:
        return {
            **base_result,
            "relationship": "different_mount",
            "same_mount": False,
            "same_replica": False,
            "reason": "mount_id_mismatch",
        }

    if (
        s_id.replica_id
        and s_id.replica_id == t_id.replica_id
        and s_id.history_id == t_id.history_id
    ):
        shared_seqs = set(src.event_map).intersection(dst.event_map)
        for seq in sorted(shared_seqs):
            if src.event_map[seq] != dst.event_map[seq]:
                return {
                    **base_result,
                    "relationship": "fork",
                    "same_mount": True,
                    "same_replica": False,
                    "reason": "divergent_event_id_at_same_seq",
                }
        if (
            src.state_fingerprint
            and dst.state_fingerprint
            and src.state_fingerprint != dst.state_fingerprint
            and src.head_seq == dst.head_seq
        ):
            return {
                **base_result,
                "relationship": "fork",
                "same_mount": True,
                "same_replica": False,
                "reason": "divergent_state_on_copied_replica",
            }
        return {
            **base_result,
            "relationship": "same_replica",
            "same_mount": True,
            "same_replica": True,
            "reason": "identical_replica_and_history",
        }

    dst_cloned_from_src = bool(
        (t_id.base_source_replica_id and t_id.base_source_replica_id == s_id.replica_id)
        or (
            t_id.base_snapshot_id
            and t_id.base_snapshot_id in (src.snapshot_id, s_id.base_snapshot_id)
        )
    )
    src_cloned_from_dst = bool(
        (s_id.base_source_replica_id and s_id.base_source_replica_id == t_id.replica_id)
        or (
            s_id.base_snapshot_id
            and s_id.base_snapshot_id in (dst.snapshot_id, t_id.base_snapshot_id)
        )
    )
    explicit_fork_link = bool(
        (t_id.parent_replica_id and t_id.parent_replica_id == s_id.replica_id)
        or (s_id.parent_replica_id and s_id.parent_replica_id == t_id.replica_id)
        or (
            t_id.parent_replica_id
            and s_id.parent_replica_id
            and t_id.parent_replica_id == s_id.parent_replica_id
        )
    )

    if explicit_fork_link:
        if (
            dst.post_base_mutations > 0
            or src.post_base_mutations > 0
            or (
                src.state_fingerprint
                and dst.state_fingerprint
                and src.state_fingerprint != dst.state_fingerprint
            )
        ):
            return {
                **base_result,
                "relationship": "fork",
                "same_mount": True,
                "same_replica": False,
                "reason": "divergent_runtime_fork",
            }
        return {
            **base_result,
            "relationship": "clone",
            "same_mount": True,
            "same_replica": False,
            "reason": "unmodified_fork_branch",
        }

    if dst_cloned_from_src:
        if dst.post_base_mutations > 0:
            return {
                **base_result,
                "relationship": "fork",
                "same_mount": True,
                "same_replica": False,
                "reason": "clone_diverged_after_base",
            }
        return {
            **base_result,
            "relationship": "clone",
            "same_mount": True,
            "same_replica": False,
            "reason": "clean_clone_of_mount",
        }

    if src_cloned_from_dst:
        if src.post_base_mutations > 0:
            return {
                **base_result,
                "relationship": "fork",
                "same_mount": True,
                "same_replica": False,
                "reason": "clone_diverged_after_base",
            }
        return {
            **base_result,
            "relationship": "clone",
            "same_mount": True,
            "same_replica": False,
            "reason": "clean_clone_of_mount",
        }

    return {
        **base_result,
        "relationship": "replacement",
        "same_mount": True,
        "same_replica": False,
        "reason": "unanchored_replica_history_replacement",
    }
