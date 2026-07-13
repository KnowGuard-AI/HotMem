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
                  occurred_at=None) -> dict
     query_events(db, *, memory_id=None, namespace=None, event_type=None,
                  since=None, until=None, after_seq=None, before_seq=None,
                  limit=100, ascending=True) -> dict

Event ordering & identifiers:
     Events are ordered deterministically by the monotonic ``seq`` column (a
     SQLite AUTOINCREMENT primary key). Each event also carries a stable
     ``event_id`` (UUID4 hex) so clients can reference a specific event across
     re-pagination without depending on the internal sequence.

Payload contract:
     ``payload`` is a free-form dict, stored as JSON. Each ``event_type`` has a
     documented expected shape below; unknown extra keys are tolerated so new
     fields can be added additively without breaking existing readers.

     memory.created:
         {memory_type: "fact"|"file", identifier: str, content_hash: str,
          source_uri?: str, byte_offset?: int, byte_length?: int,
          source_format?: str}
     memory.updated:
         {identifier: str, content_hash: str, reason?: str}
     memory.provenance_changed:
         {source_uri: str, byte_offset: int, byte_length: int,
          source_checksum?: str}
     memory.promotion:
         {from: str, to: str, reason?: str, actor?: str}
     memory.candidate:
         {candidate: bool, reason?: str}
     snapshot.imported:
         {loaded: int, skipped_dupes: int, path: str, format: str}
     bundle.imported:
         {loaded: int, skipped_dupes: int, path: str, identifier: str}
     bundle.discovered:
         {root: str, discovered: int, indexed: int, warnings: int}
     hygiene.checked:
         {warning_count: int, error_count: int, warn_count: int, info_count: int}
     hygiene.warning:
         {category: str, severity: str, memory_id?: str, source_uri?: str,
          message: str}

Deps: hotmem.db, hotmem.trace
Extension: add new event types here and document their payload shape above.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any

from hotmem.db import MemoryDB
from hotmem.trace import get_tracer

_trace = get_tracer("events")


# Canonical event type strings. String literals (not enums) match the
# codebase convention for stored values and keep the log introspectable
# via plain SQL without importing this module.
class EventType:
    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_PROVENANCE_CHANGED = "memory.provenance_changed"
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


def append_event(
    db: MemoryDB,
    *,
    event_type: str,
    memory_id: str | None = None,
    namespace: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Append one event to the local log and return the stored row as a dict.

    ``occurred_at`` defaults to the current UTC time. The payload is JSON-encoded
    with ``sort_keys=True`` for stable diffability. The returned dict matches
    the shape returned by ``query_events`` (one entry under ``events``).
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
        {"events": [...], "count": N, "next_seq": int|None, "trace_ms": ...}

    ``next_seq`` is the seq of the last (ascending) or first (descending)
    returned event, or None when the result window is empty or exhausted.
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
    # The cursor is the seq of the last item in display order — the boundary
    # the caller feeds back as ``after_seq`` (ascending) or ``before_seq``
    # (descending) to fetch the next page.
    next_seq = None if not events else events[-1]["seq"]
    return {
        "events": events,
        "count": len(events),
        "next_seq": next_seq,
    }


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
