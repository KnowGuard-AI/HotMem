"""hotmem-interchange-v1 package verification and transactional restore (#69).

Purpose:
     Stage — verify — commit. Verification completes BEFORE any target write:
     required files, sizes, digests (file AND decompressed), record counts,
     schema compatibility, and manifest path confinement. Compressed payloads
     are streamed to a bounded-memory spool while their decompressed digest is
     checked, so the restore is all-or-nothing with O(batch) memory.

     Any failure — verification error, structural corruption mid-stream —
     rolls back and leaves the target byte-identical (contract §7).

Interface:
      PackageError(reason, file, expected, actual)  — structured diagnostics
      verify_package(pkg_dir) -> VerifiedPackage (manifest + readable stream)
      hydrate_package(db, pkg_dir) -> HydrateResult

Deps: hotmem.db, hotmem.embed, hotmem.interchange, hotmem.swap,
      hotmem.snapshot.reader (confinement helper), hotmem.trace.
Extension: package WRITING lives in interchange.package; dispatch surfaces
      in snapshot/__init__ + CLI/API (C13).
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotmem.interchange.compat import resolve_embedding
from hotmem.interchange.package import (
    FORMAT_ID,
    MANIFEST_NAME,
    PAYLOAD_GZ,
    PAYLOAD_NAMES,
)
from hotmem.interchange.paths import confined_relpath
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.swap import _HYDRATE_BATCH, HydrateResult, record_to_memory_record
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("interchange.hydrate")

_SUPPORTED_SCHEMA = 1
_SPOOL_CHUNK = 64 * 1024


class PackageError(Exception):
    """Structured package verification failure (maps to HTTP 409 diagnostics)."""

    def __init__(
        self,
        reason: str,
        *,
        file: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        self.reason = reason
        self.file = file
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{reason}: {file or 'package'}"
            + (f" (expected {expected}, got {actual})" if expected else "")
        )


@dataclass
class VerifiedPackage:
    """A verified package with a readable payload stream and cleanup duties."""

    dir: Path
    manifest: dict[str, Any]
    payload_name: str
    record_count: int
    _spool: Path | None = field(default=None, repr=False)

    def stream(self) -> Iterator[str]:
        """Yield payload lines from the (possibly spooled) verified stream."""
        source = self._spool if self._spool is not None else self.dir / self.payload_name
        with open(source, encoding="utf-8") as f:
            yield from f

    def cleanup(self) -> None:
        if self._spool is not None:
            with contextlib.suppress(OSError):
                self._spool.unlink()
            self._spool = None


def _load_manifest(pkg: Path) -> dict[str, Any]:
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
    if int(manifest.get("schema_version") or 0) > _SUPPORTED_SCHEMA:
        raise PackageError(
            "unsupported_schema",
            file=MANIFEST_NAME,
            expected=f"schema_version<={_SUPPORTED_SCHEMA}",
            actual=str(manifest.get("schema_version")),
        )
    return manifest


def _payload_entry(manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    files = manifest.get("files") or {}
    for name in PAYLOAD_NAMES:
        if name in files:
            return name, files[name]
    raise PackageError("missing_payload", file=MANIFEST_NAME, expected=" or ".join(PAYLOAD_NAMES))


def verify_package(pkg_dir: str | Path) -> VerifiedPackage:
    """Verify a package completely; raise PackageError on any failure.

    GZ payloads are decompressed to a spool file during verification so the
    record count and decompressed digest are checked over the same stream the
    restore will read (bounded memory, single decompression pass).
    """
    pkg = Path(pkg_dir)
    if not pkg.is_dir():
        raise PackageError("missing_manifest", file=str(pkg / MANIFEST_NAME))

    manifest = _load_manifest(pkg)
    # Every manifest-listed path is confinement-checked BEFORE any read
    # (contract §9: no absolute paths, no .. traversal, no symlink escapes).
    for rel in manifest.get("files") or {}:
        if not confined_relpath(pkg, rel):
            raise PackageError("path_escape", file=rel)
    payload_name, entry = _payload_entry(manifest)

    payload_path = pkg / payload_name
    if not payload_path.is_file():
        raise PackageError("missing_file", file=payload_name)

    actual_size = payload_path.stat().st_size
    if actual_size != entry.get("size"):
        raise PackageError(
            "size_mismatch",
            file=payload_name,
            expected=str(entry.get("size")),
            actual=str(actual_size),
        )

    spool: Path | None = None
    record_count = 0
    try:
        if payload_name == PAYLOAD_GZ:
            expected_decompressed = entry.get("decompressed_sha256")
            if not expected_decompressed:
                raise PackageError("missing_decompressed_digest", file=payload_name)
            decompressed_sha = hashlib.sha256()
            fd, spool_name = tempfile.mkstemp(prefix="hotmem-pkg-", suffix=".jsonl")
            spool = Path(spool_name)
            try:
                with (
                    open(payload_path, "rb") as raw,
                    os.fdopen(fd, "wb") as out,
                    gzip.GzipFile(fileobj=raw, mode="rb") as gz,
                ):
                    while chunk := gz.read(_SPOOL_CHUNK):
                        decompressed_sha.update(chunk)
                        out.write(chunk)
            except (OSError, EOFError, zlib.error) as err:
                raise PackageError(
                    "corrupt_compression", file=payload_name, actual=str(err)
                ) from err
                # Count non-blank lines as we go: split on the last complete
                # chunk boundary is fiddly — count from the spool instead.
                out.flush()
            with open(spool, "rb") as f:
                for line in f:
                    if line.strip():
                        record_count += 1
            if decompressed_sha.hexdigest() != expected_decompressed:
                raise PackageError(
                    "decompressed_digest_mismatch",
                    file=payload_name,
                    expected=expected_decompressed,
                    actual=decompressed_sha.hexdigest(),
                )
        else:
            payload_sha = hashlib.sha256()
            with open(payload_path, "rb") as f:
                for line in f:
                    payload_sha.update(line)
                    if line.strip():
                        record_count += 1
            if payload_sha.hexdigest() != entry.get("sha256"):
                raise PackageError(
                    "digest_mismatch",
                    file=payload_name,
                    expected=str(entry.get("sha256")),
                    actual=payload_sha.hexdigest(),
                )

        expected_count = int(manifest.get("record_count") or 0)
        if record_count != expected_count:
            raise PackageError(
                "record_count_mismatch",
                file=payload_name,
                expected=str(expected_count),
                actual=str(record_count),
            )
    except PackageError:
        if spool is not None:
            spool.unlink(missing_ok=True)
        raise
    except OSError as err:
        if spool is not None:
            spool.unlink(missing_ok=True)
        raise PackageError("io_error", file=payload_name, actual=str(err)) from err

    return VerifiedPackage(
        dir=pkg,
        manifest=manifest,
        payload_name=payload_name,
        record_count=record_count,
        _spool=spool,
    )


def hydrate_package(db, pkg_dir: str | Path) -> HydrateResult:
    """Verify, then restore a package in one transaction (#69).

    All-or-nothing: verification finishes before any write, inserts run in
    bounded batches inside a single transaction, and any error rolls back —
    the target remains byte-identical. Compatible stored embeddings are
    reused; incompatible ones are re-embedded from text; records without
    usable text count invalid (contract §5/§7) and are never stored.
    Idempotent: a repeated restore loads zero records.
    """
    pkg_dir = Path(pkg_dir)
    with Timer() as t:
        verified = verify_package(pkg_dir)

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
            inserted = db.insert_many_ignore(records, _commit=False)
            counters["loaded"] += inserted
            counters["skipped"] += len(records) - inserted  # residual OR IGNOREs
            pending.clear()
            batch_seen.clear()

        try:
            for line in verified.stream():
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)  # structural corruption: hard error
                if not isinstance(record, dict):
                    counters["invalid"] += 1
                    continue
                rec = normalize_record(record, default_source="interchange")
                issues = validate_record(rec)
                if issues:
                    counters["invalid"] += 1
                    _trace.debug(
                        "hydrate_pkg",
                        "skipping invalid record",
                        detail={"id": rec["id"], "issues": issues},
                    )
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
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            verified.cleanup()

    loaded = counters["loaded"]
    skipped = counters["skipped"]
    invalid = counters["invalid"]
    _trace.info(
        "hydrate_pkg",
        f"restored {loaded} records, skipped {skipped} dupes, {invalid} invalid",
        detail={
            "path": str(pkg_dir),
            "ms": round(t.ms, 2),
            **{k: counters[k] for k in counters},
        },
    )
    return HydrateResult(loaded=loaded, skipped_dupes=skipped, invalid=invalid)
