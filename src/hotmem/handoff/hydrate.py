"""Atomic, idempotent handoff hydration (#101).

Purpose:
    Restore a verified hotmem-handoff-v1 package into a HotMem database
    (handoff-v1 §11): verify first (fail-closed, target untouched), check
    the handoff ledger, then apply everything in ONE transaction — the
    selected durable memories plus exactly one resume-brief record. The
    brief is a normal interchange record (``memory_type: "fact"``,
    identifier ``handoff/<label>/resume-brief``, distinguishing data under
    ``metadata.handoff``/``provenance.handoff``) so it is retrievable
    through the unmodified search path. Insert-only: hydration never
    deletes, resurrects, or overwrites unrelated records. Idempotent: the
    ledger keys on the content-derived package_id, a repeated hydration
    writes nothing and appends no second event.

Interface:
    hydrate_handoff(db, pkg_dir, *, embedder=None) -> HandoffHydrateResult
    HandoffHydrateResult — loaded/skipped/invalid, already_applied,
        handoff_id/package_id, brief identifier, disposition()

Deps: hotmem.db, hotmem.events, hotmem.handoff.verify,
    hotmem.interchange.compat (embedding compatibility),
    hotmem.interchange.record, hotmem.swap (record conversion + batch),
    hotmem.trace.
Extension: CLI/MCP/HTTP surfaces wrap this; no other writer may insert
    handoff records.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from hotmem.events import EventType, append_event
from hotmem.handoff.verify import VerifiedHandoff, verify_handoff
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.compat import resolve_embedding
from hotmem.interchange.record import normalize_record
from hotmem.swap import HYDRATE_BATCH, record_to_memory_record
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("handoff.hydrate")

BRIEF_IMPORTANCE = 0.8


@dataclass(frozen=True)
class HandoffHydrateResult:
    """The outcome of one hydrate operation (applied or already-applied)."""

    handoff_id: str
    package_id: str
    mode: str
    loaded: int = 0
    skipped_dupes: int = 0
    invalid: int = 0
    already_applied: bool = False
    brief_identifier: str | None = None
    embedding_reused: int = 0
    embedding_rebuilt: int = 0
    embedding_missing: int = 0
    embedding_failed: int = 0

    def disposition(self) -> dict[str, int]:
        """Per-run embedding counters (one definition, shared with surfaces)."""
        return {
            "embedding_reused": self.embedding_reused,
            "embedding_rebuilt": self.embedding_rebuilt,
            "embedding_missing": self.embedding_missing,
            "embedding_failed": self.embedding_failed,
        }


def brief_identifier_for(manifest: dict[str, Any]) -> str:
    """The stable identifier of the hydrated resume-brief record."""
    label = (manifest.get("source") or {}).get("label") or "session"
    return f"handoff/{label}/resume-brief"


def build_brief_record(verified: VerifiedHandoff) -> dict[str, Any] | None:
    """Build the resume-brief interchange record from the package brief.

    Deterministic: the id derives from the package_id, so repeat hydration
    of any package carrying the same brief content deduplicates by
    content_hash anyway. Returns None when the package carries no brief
    (archive packages may omit one).
    """
    brief = verified.brief()
    if brief is None:
        return None
    manifest = verified.manifest
    identifier = brief_identifier_for(manifest)
    fact_text = str(brief.get("text") or "")
    source_meta = manifest.get("source") or {}
    record: dict[str, Any] = {
        "schema_version": 1,
        "id": hashlib.sha256(
            f"hotmem-handoff-brief:{manifest.get('package_id')}:{identifier}".encode()
        ).hexdigest(),
        "identifier": identifier,
        "memory_type": "fact",
        "fact_text": fact_text,
        "source": "handoff",
        "importance": BRIEF_IMPORTANCE,
        "metadata": {
            "handoff": {
                "handoff_id": manifest.get("handoff_id"),
                "package_id": manifest.get("package_id"),
                "mode": manifest.get("mode"),
                "source_adapter": source_meta.get("adapter"),
                "session_id": source_meta.get("session_id"),
                "time_range": source_meta.get("time_range"),
                "counts": manifest.get("counts"),
                "links": brief.get("links"),
                "kind": "resume_brief",
            }
        },
        "content_hash": compute_content_hash(identifier, fact_text),
        "provenance": {
            "handoff": {
                "handoff_id": manifest.get("handoff_id"),
                "package_id": manifest.get("package_id"),
                "mode": manifest.get("mode"),
                "source_adapter": source_meta.get("adapter"),
                "session_id": source_meta.get("session_id"),
            }
        },
    }
    return record


def hydrate_handoff(
    db,
    pkg_dir: str,
    *,
    embedder=None,
) -> HandoffHydrateResult:
    """Hydrate a verified handoff package into ``db`` atomically (#101).

    Verify → ledger check → one transaction → ledger row + one
    ``handoff.applied`` event → commit. Any failure rolls back and the
    target remains state-equivalent. A repeated hydration of the same
    package_id is a true no-op: zero writes, zero events,
    ``already_applied=True``.
    """
    with Timer() as t:
        verified = verify_handoff(pkg_dir)
        manifest = verified.manifest
        package_id = str(manifest.get("package_id"))
        handoff_id = str(manifest.get("handoff_id"))
        mode = str(manifest.get("mode"))

        ledger = db.get_handoff(package_id)
        if ledger is not None:
            _trace.info(
                "hydrate_handoff",
                f"package {package_id[:12]} already applied at {ledger['applied_at']}",
                detail={"package_id": package_id},
            )
            return HandoffHydrateResult(
                handoff_id=handoff_id,
                package_id=package_id,
                mode=mode,
                already_applied=True,
                brief_identifier=brief_identifier_for(manifest),
            )

        counters = {
            "loaded": 0,
            "skipped": 0,
            "invalid": 0,
            "embedding_reused": 0,
            "embedding_rebuilt": 0,
            "embedding_missing": 0,
            "embedding_failed": 0,
        }

        def insert_records(records: list[dict[str, Any]]) -> None:
            """Dedupe by content hash, resolve embeddings, insert, no commit."""
            if not records:
                return
            hashes = [r["content_hash"] for r in records]
            existing = db.batch_existing_hashes(hashes)
            todo = [r for r in records if r["content_hash"] not in existing]
            counters["skipped"] += len(records) - len(todo)

            rows = []
            for rec in todo:
                blob, model, dim, status = resolve_embedding(rec, embedder=embedder)
                counters[f"embedding_{status}"] += 1
                rows.append(
                    record_to_memory_record(rec, blob, embedding_model=model, embedding_dim=dim)
                )
            inserted = db.insert_many_ignore(rows, _commit=False)
            counters["loaded"] += inserted
            counters["skipped"] += len(rows) - inserted  # residual OR IGNOREs

        try:
            # 1. The selected durable memories (interchange records).
            memory_records: list[dict[str, Any]] = []
            for line in verified.memory_lines():
                try:
                    raw = json.loads(line)
                    rec = normalize_record(raw, default_source="handoff")
                except Exception:
                    counters["invalid"] += 1
                    continue
                memory_records.append(rec)
                if len(memory_records) >= HYDRATE_BATCH:
                    insert_records(memory_records)
                    memory_records = []
            insert_records(memory_records)

            # 2. Exactly one resume-brief record (searchable, linked).
            brief_record = build_brief_record(verified)
            if brief_record is not None:
                insert_records([normalize_record(brief_record, default_source="handoff")])

            # 3. Ledger + event inside the same transaction (#101 §11).
            db.record_handoff(
                package_id=package_id,
                handoff_id=handoff_id,
                mode=mode,
                counts_json=json.dumps(
                    {
                        "loaded": counters["loaded"],
                        "skipped": counters["skipped"],
                        "invalid": counters["invalid"],
                    },
                    sort_keys=True,
                ),
                applied_at=_utc_now_iso(),
                _commit=False,
            )
            append_event(
                db,
                event_type=EventType.HANDOFF_APPLIED,
                namespace="handoff",
                payload={
                    "handoff_id": handoff_id,
                    "package_id": package_id,
                    "mode": mode,
                    "loaded": counters["loaded"],
                    "skipped": counters["skipped"],
                    "invalid": counters["invalid"],
                    "brief_identifier": brief_identifier_for(manifest),
                },
                _commit=False,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    result = HandoffHydrateResult(
        handoff_id=handoff_id,
        package_id=package_id,
        mode=mode,
        loaded=counters["loaded"],
        skipped_dupes=counters["skipped"],
        invalid=counters["invalid"],
        brief_identifier=brief_identifier_for(manifest),
        embedding_reused=counters["embedding_reused"],
        embedding_rebuilt=counters["embedding_rebuilt"],
        embedding_missing=counters["embedding_missing"],
        embedding_failed=counters["embedding_failed"],
    )
    _trace.info(
        "hydrate_handoff",
        f"applied package {package_id[:12]}: {result.loaded} loaded, "
        f"{result.skipped_dupes} skipped, {result.invalid} invalid",
        detail={"path": str(pkg_dir), "ms": round(t.ms, 2)},
    )
    return result


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
