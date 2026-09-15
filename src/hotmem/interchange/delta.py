"""hotmem-delta-v1 producer — verified base-to-current state diff (#73).

Purpose:
     Produce a deterministic delta package: the verified difference between
     a base `hotmem-interchange-v1` clone and the source's current canonical
     state. Every operation is a full-state compare-and-swap upsert
     (contract: docs/okf/delta-v1.md §4); removals since the base are
     counted, never applied (§7).

     Deterministic: operations sorted by record id, canonical serialization,
     stable op ids (sha256 of record id + resulting fingerprint) — the same
     base + source state always yields byte-identical deltas.

     v1 mechanism is verified state comparison: the event log is advisory
     (only server /v1/add emits per-record events — tested boundary), so the
     producer reads the verified base payload and diffs fingerprints.

Interface:
      DeltaResult(path, added, changed, removed_since_base, total_ops,
                  resulting_state_fingerprint)
      produce_delta(db, base_pkg, out_dir, gz=False) -> DeltaResult

Deps: hotmem.db, hotmem.interchange.{canonical,fingerprint,package,record},
      hotmem.interchange.hydrate (verified base streaming).
Extension: apply/verify live in apply_delta (same module); the event-based
      fast path is reserved behind a completeness proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hotmem.db import MemoryDB
from hotmem.interchange.canonical import canonical_line, sha256_bytes, sha256_file
from hotmem.interchange.fingerprint import (
    FINGERPRINT_VERSION,
    record_fingerprint,
    state_fingerprint_from_list,
)
from hotmem.interchange.hydrate import verify_package
from hotmem.interchange.package import (
    _atomic_publish,  # internal-but-shared atomic publish helper (#69)
    _fsync_dir,
)
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
