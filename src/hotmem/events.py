"""HotMem events -- append-only local event log and portable replay cursors.

Purpose:
     Retain an inspectable local history of memory changes, hydration-relevant
     provenance changes, lifecycle transitions, snapshot/bundle imports, and
     advisory warnings, and provide an opaque versioned replay cursor contract
     bound to mount/replica/history identity (#94). Retention is local and
     explicit -- there is no remote event infrastructure, no distributed log,
     and no public write endpoint: events are appended as side effects of
     HotMem actions.

     External policy, workflow, approval, storage, and orchestration systems
     can consume these local facts. HotMem does not apply those policies.

Interface:
     EventType -- string constants for the canonical event types.
     ReplayCursor -- opaque versioned replay cursor bound to source/history/scope.
     ReplayCursorError -- actionable cursor validation and replay error.
     issue_replay_cursor(db, ...) -> ReplayCursor
     encode_cursor(cursor) -> str
     decode_cursor(raw) -> ReplayCursor
     append_event(db, *, event_type, memory_id=None, namespace="", payload=None,
                  occurred_at=None, _commit=True) -> dict
     query_events(db, *, memory_id=None, namespace=None, event_type=None,
                  since=None, until=None, after_seq=None, before_seq=None,
                  limit=100, ascending=True) -> dict
     replay(db, *, after_seq=0, cursor=None, namespace=None) -> Iterator[dict]

Event ordering:
     Events are ordered by the monotonically increasing ``seq`` column (a
     SQLite AUTOINCREMENT primary key). Monotonicity is guaranteed; gap-free
     is not -- rollbacks and retention trimming can introduce gaps. Each event
     also carries a stable ``event_id`` (UUID4 hex). A local ``seq`` integer is
     never a portable cursor without its source/history identity (#94).
     Multi-writer ordering and automatic fork conflict resolution are
     intentionally undefined.

Payload contract:
     ``payload`` is a free-form dict, stored as JSON. The ``memory.created``
     payload captures the full memory row (all columns) so that replay can
     reconstruct state deterministically. Other event types carry the fields
     documented at their emit sites.

Append-only enforcement:
     The ``events`` table has ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers
     that ``RAISE(ABORT)`` -- tampering is detected at the DB layer. Retention
     uses a separate privileged method that temporarily disables the triggers.

Deps: hotmem.db
Extension: add new event types here and document their payload shape above.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from hotmem.db import MemoryDB

CURSOR_VERSION = 1
CURSOR_PREFIX = "hmc1"
DEFAULT_REPLAY_EVENT_TYPES: tuple[str, ...] = ("memory.created",)


class ReplayCursorError(ValueError):
    """Actionable error raised when a replay cursor is invalid or incompatible (#94)."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
        recovery: str = "",
    ) -> None:
        self.reason = reason
        self.expected = expected
        self.actual = actual
        self.recovery = recovery or (
            "Issue a cursor from the authoritative source mount or restore a clean clone."
        )
        super().__init__(f"{reason}: {message} (recovery: {self.recovery})")


def _normalize_event_types(event_types: Sequence[str] | None) -> tuple[str, ...]:
    if event_types is None:
        return DEFAULT_REPLAY_EVENT_TYPES
    if isinstance(event_types, (str, bytes)) or not isinstance(event_types, Sequence):
        raise ReplayCursorError(
            "malformed_cursor",
            "Cursor scope.event_types must be a sequence of non-empty event type strings.",
        )
    cleaned: list[str] = []
    for item in event_types:
        if not isinstance(item, str) or not item.strip():
            raise ReplayCursorError(
                "malformed_cursor",
                "Cursor scope.event_types entries must be non-empty strings.",
            )
        val = item.strip()
        if val not in cleaned:
            cleaned.append(val)
    if not cleaned:
        raise ReplayCursorError(
            "malformed_cursor",
            "Cursor scope.event_types must contain at least one event type.",
        )
    return tuple(sorted(cleaned))


def _normalize_scope_namespace(namespace: Any) -> str | None:
    if namespace is None:
        return None
    if not isinstance(namespace, str):
        raise ReplayCursorError(
            "malformed_cursor",
            f"Cursor scope.namespace must be a string or null, got {type(namespace).__name__}.",
        )
    return namespace


@dataclass(frozen=True)
class ReplayCursor:
    """Opaque, versioned replay cursor bound to source, history, scope, and seq (#94).

    Callers should treat serialized cursor strings (``hmc1.<payload>.<checksum>``)
    as opaque tokens and pass them through ``encode_cursor`` / ``decode_cursor``
    or the replay helpers.
    """

    mount_id: str
    replica_id: str
    history_id: str
    seq: int = 0
    anchor_event_id: str = ""
    base_snapshot_id: str = ""
    namespace: str | None = None
    event_types: tuple[str, ...] = DEFAULT_REPLAY_EVENT_TYPES
    version: int = CURSOR_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int:
            raise ReplayCursorError(
                "malformed_cursor",
                f"Cursor version must be an int, got {type(self.version).__name__}.",
            )
        if self.version != CURSOR_VERSION:
            raise ReplayCursorError(
                "unsupported_cursor_version",
                f"Unsupported cursor version {self.version!r}; expected {CURSOR_VERSION}.",
                expected=CURSOR_VERSION,
                actual=self.version,
            )
        if (
            not isinstance(self.mount_id, str)
            or not self.mount_id.strip()
            or not isinstance(self.replica_id, str)
            or not self.replica_id.strip()
            or not isinstance(self.history_id, str)
            or not self.history_id.strip()
        ):
            raise ReplayCursorError(
                "unanchored_sequence",
                "Replay cursor requires non-empty mount_id, replica_id, and history_id.",
            )
        if type(self.seq) is not int or self.seq < 0:
            raise ReplayCursorError(
                "malformed_cursor",
                f"Cursor seq must be a non-negative integer, got {self.seq!r}.",
            )
        if not isinstance(self.anchor_event_id, str):
            raise ReplayCursorError(
                "malformed_cursor",
                "Cursor anchor_event_id must be a string.",
            )
        if self.seq == 0 and self.anchor_event_id != "":
            raise ReplayCursorError(
                "malformed_cursor",
                "Cursor at seq=0 must have an empty anchor_event_id.",
            )
        if self.seq > 0 and not self.anchor_event_id.strip():
            raise ReplayCursorError(
                "unanchored_sequence",
                f"Cursor at seq={self.seq} is missing its anchor_event_id.",
            )
        if not isinstance(self.base_snapshot_id, str):
            raise ReplayCursorError(
                "malformed_cursor",
                "Cursor base_snapshot_id must be a string.",
            )
        norm_ns = _normalize_scope_namespace(self.namespace)
        norm_types = _normalize_event_types(self.event_types)
        object.__setattr__(self, "namespace", norm_ns)
        object.__setattr__(self, "event_types", norm_types)

    @property
    def scope(self) -> dict[str, Any]:
        return {
            "event_types": list(self.event_types),
            "namespace": self.namespace,
        }

    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "anchor_event_id": self.anchor_event_id,
            "base_snapshot_id": self.base_snapshot_id,
            "history_id": self.history_id,
            "mount_id": self.mount_id,
            "replica_id": self.replica_id,
            "scope": {
                "event_types": list(self.event_types),
                "namespace": self.namespace,
            },
            "seq": self.seq,
            "v": self.version,
        }

    def encode(self) -> str:
        raw_json = json.dumps(
            self.to_payload_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        b64 = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
        signing_input = f"{CURSOR_PREFIX}.{b64}".encode("ascii")
        checksum = hashlib.sha256(signing_input).hexdigest()[:8]
        return f"{CURSOR_PREFIX}.{b64}.{checksum}"

    @classmethod
    def decode(cls, raw: Any) -> ReplayCursor:
        return decode_cursor(raw)


def encode_cursor(cursor: ReplayCursor | str) -> str:
    """Serialize a ``ReplayCursor`` into its canonical opaque ``hmc1`` token."""
    if isinstance(cursor, str):
        return decode_cursor(cursor).encode()
    if isinstance(cursor, ReplayCursor):
        return cursor.encode()
    raise ReplayCursorError(
        "malformed_cursor",
        f"Expected ReplayCursor or opaque cursor string, got {type(cursor).__name__}.",
    )


def decode_cursor(raw: Any) -> ReplayCursor:
    """Parse and integrity-verify an opaque ``hmc1`` replay cursor token (#94).

    Never accepts a local SQLite integer sequence, numeric string, or unanchored
    mapping as a portable cursor.
    """
    if isinstance(raw, ReplayCursor):
        return raw
    if raw is None:
        raise ReplayCursorError(
            "unanchored_sequence",
            "A portable replay cursor is required; got None.",
        )
    if isinstance(raw, bool):
        raise ReplayCursorError(
            "malformed_cursor",
            "Cursor must be an opaque hmc1 token, not bool.",
        )
    if isinstance(raw, int):
        raise ReplayCursorError(
            "unanchored_sequence",
            f"Local SQLite sequence {raw} is never accepted as a portable cursor without "
            "source/history identity.",
            expected="hmc1.<payload>.<checksum>",
            actual=raw,
        )
    if isinstance(raw, Mapping):
        if (
            not str(raw.get("mount_id") or "").strip()
            or not str(raw.get("replica_id") or "").strip()
            or not str(raw.get("history_id") or "").strip()
        ):
            raise ReplayCursorError(
                "unanchored_sequence",
                "Unanchored sequence mapping is missing mount_id, replica_id, or history_id.",
                expected="hmc1.<payload>.<checksum>",
                actual=dict(raw),
            )
        raise ReplayCursorError(
            "malformed_cursor",
            "Cursor must be passed as an opaque serialized token or ReplayCursor, not a raw dict.",
        )
    if not isinstance(raw, str):
        raise ReplayCursorError(
            "malformed_cursor",
            f"Cursor must be an opaque string token, got {type(raw).__name__}.",
        )

    token = raw.strip()
    if not token or token.lstrip("-+").isdigit():
        raise ReplayCursorError(
            "unanchored_sequence",
            f"Local SQLite sequence {raw!r} is never accepted as a portable cursor without "
            "source/history identity.",
            expected="hmc1.<payload>.<checksum>",
            actual=raw,
        )

    parts = token.split(".")
    prefix = parts[0]
    if prefix.startswith("hmc") and prefix != CURSOR_PREFIX:
        raise ReplayCursorError(
            "unsupported_cursor_version",
            f"Unsupported cursor prefix {prefix!r}; expected {CURSOR_PREFIX!r}.",
            expected=CURSOR_PREFIX,
            actual=prefix,
        )
    if len(parts) != 3 or prefix != CURSOR_PREFIX or not parts[1] or not parts[2]:
        raise ReplayCursorError(
            "malformed_cursor",
            f"Malformed replay cursor token; expected '{CURSOR_PREFIX}.<payload>.<checksum>'.",
            expected=f"{CURSOR_PREFIX}.<payload>.<checksum>",
            actual=raw,
        )

    payload_b64, sig = parts[1], parts[2]
    expected_sig = hashlib.sha256(f"{prefix}.{payload_b64}".encode("ascii")).hexdigest()[:8]
    if sig != expected_sig:
        raise ReplayCursorError(
            "malformed_cursor",
            "Replay cursor checksum mismatch.",
            expected=expected_sig,
            actual=sig,
        )

    pad = "=" * (-len(payload_b64) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        data = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:
        raise ReplayCursorError(
            "malformed_cursor",
            f"Replay cursor payload could not be decoded: {exc}",
        ) from exc

    if not isinstance(data, dict):
        raise ReplayCursorError(
            "malformed_cursor",
            "Replay cursor payload must decode to a JSON object.",
        )
    if "v" not in data:
        raise ReplayCursorError(
            "malformed_cursor",
            "Replay cursor payload is missing version field 'v'.",
        )
    version = data["v"]
    if type(version) is not int:
        raise ReplayCursorError(
            "malformed_cursor",
            f"Replay cursor version 'v' must be an int, got {type(version).__name__}.",
        )
    if version != CURSOR_VERSION:
        raise ReplayCursorError(
            "unsupported_cursor_version",
            f"Unsupported cursor version {version!r}; expected {CURSOR_VERSION}.",
            expected=CURSOR_VERSION,
            actual=version,
        )

    scope_raw = data.get("scope")
    if not isinstance(scope_raw, Mapping):
        raise ReplayCursorError(
            "malformed_cursor",
            "Replay cursor payload is missing 'scope' object.",
        )

    return ReplayCursor(
        version=version,
        mount_id=data.get("mount_id", ""),
        replica_id=data.get("replica_id", ""),
        history_id=data.get("history_id", ""),
        base_snapshot_id=data.get("base_snapshot_id", ""),
        namespace=scope_raw.get("namespace"),
        event_types=scope_raw.get("event_types"),
        seq=data.get("seq", -1),
        anchor_event_id=data.get("anchor_event_id", ""),
    )


def issue_replay_cursor(
    db: MemoryDB,
    *,
    position: Literal["start", "head"] = "head",
    seq: int | None = None,
    base_snapshot_id: str | None = None,
    namespace: str | None = None,
    event_types: Sequence[str] | None = None,
) -> ReplayCursor:
    """Issue a portable ``ReplayCursor`` anchored to ``db``'s mount and history (#94).

    - ``position="start"`` issues a cursor at ``seq=0, anchor_event_id=""``.
    - ``position="head"`` issues a cursor at the store's current event-log head
      (``seq=0`` if the event log is empty).
    - ``seq=<n>`` issues a cursor anchored to exact event ``seq=<n>`` (which must
      exist in ``db`` when ``n > 0``).
    """
    ident = db.mount_identity()
    bounds = db.event_log_bounds()
    resolved_base = base_snapshot_id if base_snapshot_id is not None else ident.base_snapshot_id

    if seq is not None:
        if type(seq) is not int or seq < 0:
            raise ReplayCursorError(
                "malformed_cursor",
                f"Cursor seq must be a non-negative integer, got {seq!r}.",
            )
        if seq == 0:
            target_seq = 0
            anchor_id = ""
        else:
            row = db.event_at_seq(seq)
            if row is None:
                raise ReplayCursorError(
                    "stale_base",
                    f"Cannot issue cursor at seq={seq}: event is absent or trimmed "
                    f"(trimmed_through_seq={bounds['trimmed_through_seq']}, "
                    f"max_seq={bounds['max_seq']}).",
                    expected=f"present event at seq={seq}",
                    actual=None,
                )
            target_seq = int(row["seq"])
            anchor_id = str(row["event_id"])
    elif position == "start":
        target_seq = 0
        anchor_id = ""
    elif position == "head":
        target_seq = int(bounds["head_seq"])
        anchor_id = str(bounds["head_event_id"]) if target_seq > 0 else ""
    else:
        raise ValueError(f"Unsupported cursor position {position!r}; expected 'start' or 'head'.")

    return ReplayCursor(
        mount_id=ident.mount_id,
        replica_id=ident.replica_id,
        history_id=ident.history_id,
        base_snapshot_id=resolved_base,
        namespace=namespace,
        event_types=_normalize_event_types(event_types),
        seq=target_seq,
        anchor_event_id=anchor_id,
    )


class EventType:
    """Canonical event type strings. Stored as plain text for SQL introspection."""

    MEMORY_CREATED = "memory.created"
    MEMORY_DEDUPLICATED = "memory.deduplicated"
    MEMORY_PROMOTION = "memory.promotion"
    MEMORY_CANDIDATE = "memory.candidate"
    SNAPSHOT_IMPORTED = "snapshot.imported"
    BUNDLE_IMPORTED = "bundle.imported"
    BUNDLE_DISCOVERED = "bundle.discovered"
    HYGIENE_CHECKED = "hygiene.checked"
    HYGIENE_WARNING = "hygiene.warning"
    INDEX_REBUILT = "index.rebuilt"
    SYNC_APPLIED = "sync.applied"
    HANDOFF_APPLIED = "handoff.applied"


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
        "parent_memory": record.get("parent_memory") or "",
        "related_memories": record.get("related_memories") or "[]",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
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

    Yields event dicts in seq order, starting strictly after ``after_seq``.
    The caller can reconstruct memory state by replaying ``memory.created``
    events: each payload carries the full row snapshot (via
    _build_memory_created_payload).

    Note: only server ``/v1/add`` ingestion emits ``memory.created`` events
    today — imported/hydrated state has no per-record events. Replay is an
    audit/repair surface, not a proven replication log (#73).
    """
    cursor = after_seq
    while True:
        result = query_events(db, after_seq=cursor, limit=500, ascending=True)
        events = result["events"]
        if not events:
            break
        yield from events
        cursor = events[-1]["seq"]
        if result["next_seq"] is None or len(events) < 500:
            break


_REPLAY_BATCH = 1000


def replay_into(db: MemoryDB, target_db: MemoryDB) -> int:
    """Reconstruct memory state from the event log into ``target_db``.

    Replays ``memory.created`` events and inserts them via bounded
    ``insert_many_ignore`` batches (no per-record commits). Returns the
    number of records actually applied -- records whose
    ``(namespace, content_hash)`` already exists in the target are skipped and
    NOT counted (#73/#100): the return value is truthful applied work, not
    attempted work. Rows with an empty hash have no identity and rely on
    their primary key.
    Non-``memory.created`` events are skipped (they don't carry full rows).
    """
    import base64

    from hotmem.db import MemoryRecord

    applied = 0
    pending: list[MemoryRecord] = []
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
            parent_memory=p.get("parent_memory") or "",
            related_memories=p.get("related_memories") or "[]",
            created_at=p.get("created_at"),
            updated_at=p.get("updated_at"),
        )
        pending.append(record)
        if len(pending) >= _REPLAY_BATCH:
            applied += target_db.insert_many_ignore(pending)
            pending.clear()
    if pending:
        applied += target_db.insert_many_ignore(pending)
    return applied


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
    invalid: int = 0,
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
            "invalid": invalid,
            "path": str(path),
            "format": fmt,
        },
        _commit=_commit,
    )
