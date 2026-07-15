"""HotMem events — append-only local event log for memory lifecycle and signals.

Purpose:
     Retain an inspectable local history of memory changes, hydration-relevant
     provenance changes, lifecycle transitions, snapshot/bundle imports, and
     advisory warnings. Retention is local and explicit — there is no remote
     event infrastructure, no distributed log, and no public write endpoint:
     events are appended as side effects of HotMem actions.

     EMOS owns policy, workflows, approvals, remote storage, and
     orchestration. HotMem only records local, append-only facts.

Interface:
     EventType — string constants for the canonical event types.
     append_event(db, *, event_type, memory_id=None, namespace="", payload=None,
                  occurred_at=None, _commit=True) -> dict
     query_events(db, *, memory_id=None, namespace=None, event_type=None,
                  since=None, until=None, after_seq=None, before_seq=None,
                  limit=100, ascending=True) -> dict
     replay(db, *, after_seq=0) -> Iterator[dict]

Event ordering:
     Events are ordered by the monotonically increasing ``seq`` column (a
     SQLite AUTOINCREMENT primary key). Monotonicity is guaranteed; gap-free
     is not — rollbacks and retention trimming can introduce gaps. Each event
     also carries a stable ``event_id`` (UUID4 hex).

Payload contract:
     ``payload`` is a free-form dict, stored as JSON. The ``memory.created``
     payload captures the full memory row (all columns) so that replay can
     reconstruct state deterministically. Other event types carry the fields
     documented at their emit sites.

Append-only enforcement:
     The ``events`` table has ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers
     that ``RAISE(ABORT)`` — tampering is detected at the DB layer. Retention
     uses a separate privileged method that temporarily disables the triggers.

Deps: hotmem.db
Extension: add new event types here and document their payload shape above.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from collections.abc import Iterator
from typing import Any

from hotmem.db import MemoryDB


class EventType:
    """Canonical event type strings. Stored as plain text for SQL introspection."""

    MEMORY_CREATED = "memory.created"
    MEMORY_PROMOTION = "memory.promotion"
    MEMORY_CANDIDATE = "memory.candidate"
    SNAPSHOT_IMPORTED = "snapshot.imported"
    BUNDLE_IMPORTED = "bundle.imported"
    BUNDLE_DISCOVERED = "bundle.discovered"
    HYGIENE_CHECKED = "hygiene.checked"
    HYGIENE_WARNING = "hygiene.warning"


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 (stdlib only)."""
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_memory_created_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Build a full-state payload from a memory row for deterministic replay.

    Captures all columns needed to reconstruct the row via INSERT OR IGNORE.
    Embeddings are base64-encoded so the payload stays JSON-safe.
    """
    import base64

    payload: dict[str, Any] = {
        "id": record.get("id", ""),
        "identifier": record.get("identifier", ""),
        "fact_text": record.get("fact_text") or "",
        "embedding_dim": record.get("embedding_dim") or 0,
        "embedding_model": record.get("embedding_model") or "",
        "source": record.get("source") or "",
        "importance": record.get("importance") or 0.5,
        "metadata_json": record.get("metadata_json") or "{}",
        "content_hash": record.get("content_hash") or "",
        "namespace": record.get("namespace") or "",
        "tier": record.get("tier") or "hot",
        "memory_type": record.get("memory_type") or "fact",
        "source_uri": record.get("source_uri") or "",
        "source_format": record.get("source_format") or "",
        "source_checksum": record.get("source_checksum") or "",
        "byte_offset": record.get("byte_offset"),
        "byte_length": record.get("byte_length"),
        "promotion_state": record.get("promotion_state") or "HOT",
        "promotion_candidate": record.get("promotion_candidate") or 0,
        "tags": record.get("tags") or "[]",
        "fact_summary": record.get("fact_summary"),
        "provenance_json": record.get("provenance_json"),
        "snapshot_id": record.get("snapshot_id") or "",
    }
    emb = record.get("embedding")
    if emb:
        payload["embedding_b64"] = base64.b64encode(emb).decode("ascii")
    ttl = record.get("ttl_seconds")
    if ttl is not None:
        payload["ttl_seconds"] = ttl
    return payload


def append_event(
    db: MemoryDB,
    *,
    event_type: str,
    memory_id: str | None = None,
    namespace: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    _commit: bool = True,
) -> dict[str, Any]:
    """Append one event to the local log and return the stored row as a dict.

    ``_commit=False`` lets the caller batch the event INSERT into the same
    transaction as the memory write (atomicity). The caller must commit.
    """
    event_id = uuid.uuid4().hex
    occurred_at = occurred_at or _utc_now_iso()
    payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
    seq = db.append_event(
        event_type=event_type,
        event_id=event_id,
        memory_id=memory_id,
        namespace=namespace,
        occurred_at=occurred_at,
        payload_json=payload_json,
        _commit=_commit,
    )
    return {
        "seq": seq,
        "event_id": event_id,
        "memory_id": memory_id,
        "namespace": namespace,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload": payload or {},
    }


def query_events(
    db: MemoryDB,
    *,
    memory_id: str | None = None,
    namespace: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    after_seq: int | None = None,
    before_seq: int | None = None,
    limit: int = 100,
    ascending: bool = True,
) -> dict[str, Any]:
    """Return a paginated, filtered event listing.

    Response shape:
        {"events": [...], "count": N, "next_seq": int|None}

    Pure SQL read — never triggers file hydration.
    """
    rows = db.query_events(
        memory_id=memory_id,
        namespace=namespace,
        event_type=event_type,
        since=since,
        until=until,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
        ascending=ascending,
    )
    events = [_row_to_event(r) for r in rows]
    next_seq = None if not events else events[-1]["seq"]
    return {
        "events": events,
        "count": len(events),
        "next_seq": next_seq,
    }


def replay(db: MemoryDB, *, after_seq: int = 0) -> Iterator[dict[str, Any]]:
    """Stream events from the log as an iterator (constant memory).

    Yields event dicts in seq order, starting after ``after_seq``. The caller
    can reconstruct memory state by replaying ``memory.created`` events:
    each payload carries the full row snapshot (via _build_memory_created_payload).
    """
    cursor = 0
    while True:
        result = query_events(db, after_seq=cursor, limit=500, ascending=True)
        events = result["events"]
        if not events:
            break
        yield from events
        cursor = events[-1]["seq"]
        if result["next_seq"] is None or len(events) < 500:
            break


def replay_into(db: MemoryDB, target_db: MemoryDB) -> int:
    """Reconstruct memory state from the event log into ``target_db``.

    Replays all ``memory.created`` events and inserts them via
    ``insert_many_ignore``. Returns the number of memories reconstructed.
    Non-``memory.created`` events are skipped (they don't carry full rows).
    """
    import base64

    from hotmem.db import MemoryRecord

    count = 0
    for event in replay(db):
        if event["event_type"] != EventType.MEMORY_CREATED:
            continue
        p = event.get("payload") or {}
        if not p.get("id"):
            continue
        emb_b64 = p.get("embedding_b64")
        emb = base64.b64decode(emb_b64) if emb_b64 else b""
        record = MemoryRecord(
            id=p["id"],
            identifier=p.get("identifier", ""),
            fact_text=p.get("fact_text", ""),
            embedding=emb,
            embedding_dim=p.get("embedding_dim") or 0,
            embedding_model=p.get("embedding_model") or "",
            source=p.get("source") or "",
            importance=p.get("importance") or 0.5,
            metadata_json=p.get("metadata_json") or "{}",
            content_hash=p.get("content_hash") or "",
            ttl_seconds=p.get("ttl_seconds"),
            namespace=p.get("namespace") or "",
            tier=p.get("tier") or "hot",
            memory_type=p.get("memory_type") or "fact",
            source_uri=p.get("source_uri") or "",
            source_format=p.get("source_format") or "",
            source_checksum=p.get("source_checksum") or "",
            byte_offset=p.get("byte_offset"),
            byte_length=p.get("byte_length"),
            promotion_state=p.get("promotion_state") or "HOT",
            promotion_candidate=p.get("promotion_candidate") or 0,
            tags=p.get("tags") or "[]",
            fact_summary=p.get("fact_summary"),
            provenance_json=p.get("provenance_json"),
            snapshot_id=p.get("snapshot_id") or "",
        )
        target_db.insert_many_ignore([record])
        count += 1
    return count


def _row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    """Decode a stored events row into the public event dict shape."""
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    else:
        payload = payload or {}
    return {
        "seq": row["seq"],
        "event_id": row["event_id"],
        "memory_id": row["memory_id"],
        "namespace": row["namespace"],
        "event_type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "payload": payload,
    }


def emit_import_event(
    db: MemoryDB,
    *,
    path: str,
    loaded: int,
    skipped_dupes: int,
    _commit: bool = True,
) -> dict[str, Any]:
    """Emit a SNAPSHOT_IMPORTED or BUNDLE_IMPORTED event (deduplicated helper).

    Detects the format from the path and selects the event type. Shared by
    the lifespan auto-hydrate path and the /v1/hydrate endpoint.
    """
    try:
        from hotmem.snapshot import detect_format

        fmt = detect_format(path)
    except Exception:
        fmt = "legacy"
    event_type = EventType.BUNDLE_IMPORTED if fmt == "bundle" else EventType.SNAPSHOT_IMPORTED
    return append_event(
        db,
        event_type=event_type,
        payload={
            "loaded": loaded,
            "skipped_dupes": skipped_dupes,
            "path": str(path),
            "format": fmt,
        },
        _commit=_commit,
    )
