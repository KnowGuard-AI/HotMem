"""hotmem-delta-v1 — verified producer and compare-and-swap applier (#73).

Purpose:
     Produce and apply deterministic delta packages: the verified difference
     between a base `hotmem-interchange-v1` clone and the source's current
     canonical state. Every operation is a full-state compare-and-swap
     upsert (contract: docs/okf/delta-v1.md §4); removals since the base are
     counted, never applied (§7). Apply is all-or-nothing: records,
     checkpoint, and receipt commit in one transaction, and any conflict or
     integrity failure leaves the target unchanged (§6).

     Deterministic: operations sorted by record id, canonical serialization,
     stable op ids (sha256 of record id + resulting fingerprint) — the same
     base + source state always yields byte-identical deltas.

     v1 mechanism is verified state comparison: the event log is advisory
     (only server /v1/add emits per-record events — tested boundary), so the
     producer reads the verified base payload and diffs fingerprints.

Interface:
      DeltaResult / produce_delta(db, base_pkg, out_dir, gz=False)
      Conflict / DeltaConflictError / ApplyResult / VerifiedDelta
      verify_delta(delta_dir) -> VerifiedDelta
      apply_delta(db, delta_dir) -> ApplyResult

Deps: hotmem.db, hotmem.interchange.{canonical,fingerprint,paths,package,
      record,compat}, hotmem.interchange.hydrate, hotmem.events, hotmem.swap.
Extension: the event-based fast path is reserved behind a completeness
      proof; tombstones/namespace scoping are Proposed (delta-v1 §10).
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hotmem.db import _MEMORY_COLUMNS, MemoryDB
from hotmem.interchange.canonical import canonical_line, sha256_bytes, sha256_file
from hotmem.interchange.fingerprint import (
    FINGERPRINT_VERSION,
    record_fingerprint,
    state_fingerprint_from_list,
)
from hotmem.interchange.hydrate import PackageError, verify_package
from hotmem.interchange.package import (
    _atomic_publish,  # internal-but-shared atomic publish helper (#69)
    _fsync_dir,
)
from hotmem.interchange.paths import confined_relpath
from hotmem.interchange.record import normalize_record
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("interchange.delta")

FORMAT_ID = "hotmem-delta-v1"
MANIFEST_NAME = "manifest.json"
OPS_PLAIN = "operations.jsonl"
OPS_GZ = "operations.jsonl.gz"


@dataclass
class DeltaResult:
    path: str
    added: int
    changed: int
    removed_since_base: int
    total_ops: int
    resulting_state_fingerprint: str


def _op_id(record_id: str, resulting_fingerprint: str) -> str:
    """Stable operation identity: same change, same op id (delta-v1 §4)."""
    return sha256_bytes(f"upsert:{record_id}:{resulting_fingerprint}".encode())


def produce_delta(
    db: MemoryDB,
    base_pkg: str | Path,
    out_dir: str | Path,
    *,
    gz: bool = False,
) -> DeltaResult:
    """Produce a hotmem-delta-v1 package atomically (#73).

    Streams the verified base package payload and the current rows once,
    fingerprints both sides, and emits compare-and-swap upserts for added
    and changed records. Records removed since the base are counted in the
    manifest and never applied (delta-v1 §7). Publish is atomic — a failed
    produce never leaves a partial delta.
    """
    base = verify_package(base_pkg)  # hard error on any integrity failure

    # Base images: record id -> state fingerprint at base (delta-v1 §4).
    # O(base records) fingerprint strings — the documented producer cost of
    # computing pre-images without per-record manifests (delta-v1 §10).
    base_fingerprints: dict[str, str] = {}
    base_ids: set[str] = set()
    for line in base.stream():
        line = line.strip()
        if not line:
            continue
        base_record = normalize_record(json.loads(line))
        record_id = base_record["id"]
        base_ids.add(record_id)
        base_fingerprints[record_id] = record_fingerprint(base_record)

    with Timer() as t:
        final = Path(out_dir).resolve()
        final.parent.mkdir(parents=True, exist_ok=True)
        staging = final.with_name(f".{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        ops_name = OPS_GZ if gz else OPS_PLAIN
        added = changed = 0
        current_ids: set[str] = set()
        current_fingerprints: list[str] = []
        ops_hasher = hashlib.sha256()

        try:
            ops_path = staging / ops_name
            with open(ops_path, "wb") as raw_file:
                sink = raw_file
                if gz:
                    import gzip

                    sink = gzip.GzipFile(
                        filename="", mode="wb", compresslevel=9, fileobj=raw_file, mtime=0
                    )
                for row in db.iter_rows():
                    normalized = normalize_record(row)
                    record_id = normalized["id"]
                    fingerprint = record_fingerprint(normalized)
                    current_ids.add(record_id)
                    current_fingerprints.append(fingerprint)

                    if record_id not in base_ids:
                        expected: str | None = None
                        added += 1
                    elif base_fingerprints[record_id] != fingerprint:
                        expected = base_fingerprints[record_id]
                        changed += 1
                    else:
                        continue  # unchanged: no operation

                    op = {
                        "op": "upsert",
                        "op_id": _op_id(record_id, fingerprint),
                        "record_id": record_id,
                        "expected_pre_fingerprint": expected,
                        "record": normalized,
                        "resulting_fingerprint": fingerprint,
                    }
                    data = canonical_line(op).encode()
                    ops_hasher.update(data)
                    sink.write(data)
                if gz:
                    sink.close()
                raw_file.flush()
                os.fsync(raw_file.fileno())

            removed_since_base = len(base_ids - current_ids)
            resulting = state_fingerprint_from_list(current_fingerprints)

            manifest = {
                "format": FORMAT_ID,
                "schema_version": 1,
                "fingerprint_version": FINGERPRINT_VERSION,
                "source": {"kind": "hotmem-dump"},
                "base": {
                    "package_format": "hotmem-interchange-v1",
                    "logical_id": base.manifest.get("logical_id"),
                    # Base packages do not carry state fingerprints yet (delta-v1 §10).
                    "state_fingerprint": None,
                    "record_count": base.record_count,
                },
                "counts": {
                    "added": added,
                    "changed": changed,
                    "removed_since_base": removed_since_base,
                    "total_ops": added + changed,
                },
                "resulting_state_fingerprint": resulting,
                "files": {
                    ops_name: {
                        "size": ops_path.stat().st_size,
                        "sha256": sha256_file(ops_path),
                        **({"decompressed_sha256": ops_hasher.hexdigest()} if gz else {}),
                    }
                },
                "embedding": base.manifest.get("embedding"),
                "created_at": datetime.now(UTC).isoformat(),
                "hotmem_version": _hotmem_version(),
            }
            manifest_path = staging / MANIFEST_NAME
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())

            _fsync_dir(staging)
            _atomic_publish(staging, final)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    result = DeltaResult(
        path=str(final),
        added=added,
        changed=changed,
        removed_since_base=removed_since_base,
        total_ops=added + changed,
        resulting_state_fingerprint=resulting,
    )
    _trace.info(
        "delta_produce",
        f"produced {result.total_ops} operations ({added} added, {changed} changed)",
        detail={
            "path": str(final),
            "removed_since_base": removed_since_base,
            "ms": round(t.ms, 2),
        },
    )
    return result


def _hotmem_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("hotmem")
    except PackageNotFoundError:  # pragma: no cover - dev environments
        return "0.0.0.dev0"


# ── Verify + apply (#73, delta-v1 §6) ───────────────────────────────────────


@dataclass
class Conflict:
    """One actionable, non-silent conflict (delta-v1 §7)."""

    reason: str  # base_missing | state_divergence | id_reuse | invalid_record
    record_id: str | None = None
    op_id: str | None = None
    expected: str | None = None
    actual: str | None = None
    recovery: str = "re-clone from the source instance (hotmem snapshot --package)"


class DeltaConflictError(Exception):
    """Raised when a delta cannot apply: the target state diverged.

    Carries every conflict found; the target is unchanged (the apply
    transaction rolled back before this is raised).
    """

    def __init__(self, conflicts: list[Conflict]) -> None:
        self.conflicts = conflicts
        preview = "; ".join(f"{c.reason}:{c.record_id}" for c in conflicts[:3])
        super().__init__(f"{len(conflicts)} conflict(s) — delta not applied: {preview}")


@dataclass
class ApplyResult:
    applied: int
    skipped: int  # already-applied operations (idempotent replay)
    resulting_state_fingerprint: str | None
    conflicts: list[Conflict] = field(default_factory=list)


@dataclass
class VerifiedDelta:
    dir: Path
    manifest: dict[str, Any]
    ops_name: str
    _spool: Path | None = None

    def stream(self) -> Iterator[str]:
        source = self._spool if self._spool is not None else self.dir / self.ops_name
        with open(source, encoding="utf-8") as f:
            yield from f

    def cleanup(self) -> None:
        if self._spool is not None:
            with contextlib.suppress(OSError):
                self._spool.unlink()
            self._spool = None


def _load_delta_manifest(pkg: Path) -> dict[str, Any]:
    manifest_path = pkg / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PackageError("missing_manifest", file=str(manifest_path))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise PackageError("malformed_manifest", file=MANIFEST_NAME) from err
    if not isinstance(manifest, dict):
        raise PackageError("malformed_manifest", file=MANIFEST_NAME)
    if manifest.get("format") != FORMAT_ID:
        raise PackageError(
            "unsupported_format",
            file=MANIFEST_NAME,
            expected=FORMAT_ID,
            actual=str(manifest.get("format")),
        )
    if int(manifest.get("schema_version") or 0) > 1:
        raise PackageError(
            "unsupported_schema",
            file=MANIFEST_NAME,
            expected="schema_version<=1",
            actual=str(manifest.get("schema_version")),
        )
    if int(manifest.get("fingerprint_version") or 0) != FINGERPRINT_VERSION:
        raise PackageError(
            "unsupported_fingerprint",
            file=MANIFEST_NAME,
            expected=str(FINGERPRINT_VERSION),
            actual=str(manifest.get("fingerprint_version")),
        )
    return manifest


def verify_delta(delta_dir: str | Path) -> VerifiedDelta:
    """Verify a delta package completely; raise PackageError on any failure.

    Checks manifest format/schema/fingerprint versions, per-file digests
    (plus the decompressed digest for GZ), operation count against the
    manifest, operation structure (upsert-only, delta-v1 §7), and manifest
    path confinement — all before any target write.
    """
    pkg = Path(delta_dir)
    if not pkg.is_dir():
        raise PackageError("missing_manifest", file=str(pkg / MANIFEST_NAME))

    manifest = _load_delta_manifest(pkg)
    for rel in manifest.get("files") or {}:
        if not confined_relpath(pkg, rel):
            raise PackageError("path_escape", file=rel)
    files = manifest.get("files") or {}
    ops_name, entry = next(
        ((name, files[name]) for name in (OPS_PLAIN, OPS_GZ) if name in files),
        (None, None),
    )
    if ops_name is None:
        raise PackageError(
            "missing_payload", file=MANIFEST_NAME, expected=f"{OPS_PLAIN} or {OPS_GZ}"
        )

    ops_path = pkg / ops_name
    if not ops_path.is_file():
        raise PackageError("missing_file", file=ops_name)
    actual_size = ops_path.stat().st_size
    if actual_size != entry.get("size"):
        raise PackageError(
            "size_mismatch",
            file=ops_name,
            expected=str(entry.get("size")),
            actual=str(actual_size),
        )

    spool: Path | None = None
    op_count = 0
    try:
        if ops_name == OPS_GZ:
            expected_decompressed = entry.get("decompressed_sha256")
            if not expected_decompressed:
                raise PackageError("missing_decompressed_digest", file=ops_name)
            decompressed_sha = hashlib.sha256()
            fd, spool_name = tempfile.mkstemp(prefix="hotmem-delta-", suffix=".jsonl")
            spool = Path(spool_name)
            try:
                with (
                    open(ops_path, "rb") as raw,
                    os.fdopen(fd, "wb") as out,
                    gzip.GzipFile(fileobj=raw, mode="rb") as gz,
                ):
                    while chunk := gz.read(64 * 1024):
                        decompressed_sha.update(chunk)
                        out.write(chunk)
            except (OSError, EOFError, zlib.error) as err:
                raise PackageError("corrupt_compression", file=ops_name, actual=str(err)) from err
            if decompressed_sha.hexdigest() != expected_decompressed:
                raise PackageError(
                    "decompressed_digest_mismatch",
                    file=ops_name,
                    expected=expected_decompressed,
                    actual=decompressed_sha.hexdigest(),
                )
            stream: Path = spool
        else:
            ops_sha = hashlib.sha256()
            with open(ops_path, "rb") as f:
                for line in f:
                    ops_sha.update(line)
            if ops_sha.hexdigest() != entry.get("sha256"):
                raise PackageError(
                    "digest_mismatch",
                    file=ops_name,
                    expected=str(entry.get("sha256")),
                    actual=ops_sha.hexdigest(),
                )
            stream = ops_path

        with open(stream, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                op_count += 1
                try:
                    op = json.loads(line)
                except json.JSONDecodeError as err:
                    raise PackageError("malformed_operation", file=ops_name) from err
                if not isinstance(op, dict) or op.get("op") != "upsert":
                    raise PackageError(
                        "unsupported_operation",
                        file=ops_name,
                        expected="upsert",
                        actual=str(op.get("op") if isinstance(op, dict) else op),
                    )
                for required in ("op_id", "record_id", "record", "resulting_fingerprint"):
                    if not op.get(required):
                        raise PackageError(
                            "malformed_operation",
                            file=ops_name,
                            expected=required,
                            actual="missing",
                        )

        expected_count = int((manifest.get("counts") or {}).get("total_ops") or 0)
        if op_count != expected_count:
            raise PackageError(
                "record_count_mismatch",
                file=ops_name,
                expected=str(expected_count),
                actual=str(op_count),
            )
    except PackageError:
        if spool is not None:
            spool.unlink(missing_ok=True)
        raise
    except OSError as err:
        if spool is not None:
            spool.unlink(missing_ok=True)
        raise PackageError("io_error", file=ops_name, actual=str(err)) from err

    return VerifiedDelta(dir=pkg, manifest=manifest, ops_name=ops_name, _spool=spool)


def apply_delta(db: MemoryDB, delta_dir: str | Path) -> ApplyResult:
    """Verify, then apply a delta atomically with compare-and-swap semantics.

    Per operation (delta-v1 §6): the receiver's current record fingerprint
    must equal the operation's expected pre-image (write), or already equal
    the resulting fingerprint (skip — idempotent replay); anything else is a
    conflict. Conflicts abort the WHOLE delta: the transaction rolls back
    and the target, checkpoint, and receipt are unchanged.

    Records + checkpoint + sync.applied receipt commit in one transaction.
    Compatible stored embeddings are reused; incompatible ones re-embed from
    text; records without usable text are reported as invalid_record
    conflicts and never stored.
    """
    from hotmem.events import EventType, append_event
    from hotmem.interchange.compat import resolve_embedding
    from hotmem.interchange.record import normalize_record, validate_record
    from hotmem.swap import record_to_memory_record

    delta_dir = Path(delta_dir)
    with Timer() as t:
        verified = verify_delta(delta_dir)
        manifest = verified.manifest
        ops_entry = (manifest.get("files") or {}).get(verified.ops_name) or {}

        conflicts: list[Conflict] = []
        applied = 0
        skipped = 0
        receiver_was_empty = db.count() == 0

        try:
            for line in verified.stream():
                line = line.strip()
                if not line:
                    continue
                op = json.loads(line)
                record_id = op["record_id"]
                expected = op.get("expected_pre_fingerprint")
                resulting_fp = op["resulting_fingerprint"]

                current = db.get_memory(record_id)
                current_fp = record_fingerprint(current) if current is not None else None

                if current is None:
                    if expected is not None:
                        conflicts.append(
                            Conflict(
                                reason="base_missing" if receiver_was_empty else "state_divergence",
                                record_id=record_id,
                                op_id=op.get("op_id"),
                                expected=expected,
                                actual=None,
                            )
                        )
                        continue
                elif current_fp == resulting_fp:
                    skipped += 1
                    continue  # already applied — idempotent replay (delta-v1 §6.3)
                elif expected is not None and current_fp == expected:
                    pass  # pre-image matches: apply
                elif expected is None:
                    conflicts.append(
                        Conflict(
                            reason="id_reuse",
                            record_id=record_id,
                            op_id=op.get("op_id"),
                            expected=None,
                            actual=current_fp,
                        )
                    )
                    continue
                else:
                    conflicts.append(
                        Conflict(
                            reason="state_divergence",
                            record_id=record_id,
                            op_id=op.get("op_id"),
                            expected=expected,
                            actual=current_fp,
                        )
                    )
                    continue

                rec = normalize_record(op["record"], default_source="delta")
                issues = validate_record(rec)
                if issues:
                    conflicts.append(
                        Conflict(
                            reason="invalid_record",
                            record_id=record_id,
                            op_id=op.get("op_id"),
                            actual="; ".join(issues),
                        )
                    )
                    continue
                blob, model, dim, _status = resolve_embedding(rec)
                memory = record_to_memory_record(
                    rec, blob, embedding_model=model, embedding_dim=dim
                )
                db.insert(**{c: getattr(memory, c) for c in _MEMORY_COLUMNS}, _commit=False)
                applied += 1

            if conflicts:
                raise DeltaConflictError(conflicts)

            base_logical_id = (manifest.get("base") or {}).get("logical_id") or ""
            delta_digest = str(ops_entry.get("sha256") or "")
            resulting = manifest.get("resulting_state_fingerprint")
            db.record_sync_checkpoint(
                base_logical_id=base_logical_id,
                delta_digest=delta_digest,
                applied_ops=applied,
                resulting_state_fingerprint=resulting,
                applied_at=_utc_now_iso(),
                _commit=False,
            )
            append_event(
                db,
                event_type=EventType.SYNC_APPLIED,
                namespace="sync",
                payload={
                    "base_logical_id": base_logical_id,
                    "delta_digest": delta_digest,
                    "applied": applied,
                    "skipped": skipped,
                    "resulting_state_fingerprint": resulting,
                },
                _commit=False,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            verified.cleanup()

    result = ApplyResult(
        applied=applied,
        skipped=skipped,
        resulting_state_fingerprint=manifest.get("resulting_state_fingerprint"),
        conflicts=conflicts,
    )
    _trace.info(
        "delta_apply",
        f"applied {applied}, skipped {skipped}, conflicts {len(conflicts)}",
        detail={"path": str(delta_dir), "ms": round(t.ms, 2)},
    )
    return result


def _utc_now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
