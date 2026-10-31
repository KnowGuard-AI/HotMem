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

     Serialized cursors (``hmc1.<payload>.<checksum>``) are opaque to callers.
     The 8-hex checksum detects accidental corruption only; it is not a
     signature or authentication mechanism. Treat cursors and manifests as
     untrusted input and verify source/base identity before replay.

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
# Untrusted cursor inputs are bounded to keep parsing cheap (resource safety).
MAX_CURSOR_TOKEN = 64 * 1024
MAX_CURSOR_FIELD = 512
MAX_CURSOR_EVENT_TYPES = 256
MAX_CURSOR_EVENT_TYPE_LEN = 128


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
        for name, value in (
            ("mount_id", self.mount_id),
            ("replica_id", self.replica_id),
            ("history_id", self.history_id),
            ("anchor_event_id", self.anchor_event_id),
            ("base_snapshot_id", self.base_snapshot_id),
        ):
            if len(value) > MAX_CURSOR_FIELD:
                raise ReplayCursorError(
                    "malformed_cursor",
                    f"Cursor {name} exceeds {MAX_CURSOR_FIELD} characters.",
                )
        norm_ns = _normalize_scope_namespace(self.namespace)
        norm_types = _normalize_event_types(self.event_types)
        if len(norm_types) > MAX_CURSOR_EVENT_TYPES or any(
            len(item) > MAX_CURSOR_EVENT_TYPE_LEN for item in norm_types
        ):
            raise ReplayCursorError(
                "malformed_cursor",
                "Cursor scope.event_types exceeds size limits.",
            )
        if norm_ns is not None and len(norm_ns) > MAX_CURSOR_FIELD:
            raise ReplayCursorError(
                "malformed_cursor",
                f"Cursor scope.namespace exceeds {MAX_CURSOR_FIELD} characters.",
            )
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
    if len(token) > MAX_CURSOR_TOKEN:
        raise ReplayCursorError(
            "malformed_cursor",
            f"Replay cursor token exceeds {MAX_CURSOR_TOKEN} bytes.",
        )
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
    trimmed_through = int(bounds["trimmed_through_seq"])
    resolved_base = base_snapshot_id if base_snapshot_id is not None else ident.base_snapshot_id

    # Never mint a cursor whose anchor the validator will immediately reject:
    # a full-history start cursor is unavailable after retention trimming, and
    # a head cursor is unusable once every replayable event has been trimmed.
    if ((seq is None and position == "start") or seq == 0) and trimmed_through > 0:
        raise ReplayCursorError(
            "stale_base",
            f"Cannot issue a start cursor: history has been trimmed through seq={trimmed_through}.",
            expected=f"seq>={trimmed_through}",
            actual=0,
            recovery=(
                "Export a full snapshot/package and hydrate a clean clone before resuming replay."
            ),
        )
    if position == "head" and seq is None and int(bounds["head_seq"]) > int(bounds["max_seq"]):
        raise ReplayCursorError(
            "stale_base",
            "Cannot issue a head cursor: the event history has been fully trimmed.",
            expected="at least one replayable event",
            actual={"head_seq": bounds["head_seq"], "max_seq": bounds["max_seq"]},
            recovery=(
                "Export a full snapshot/package and hydrate a clean clone before resuming replay."
            ),
        )

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


@dataclass(frozen=True)
class ReplayResult:
    """Result of a cursor-governed replay step (#94)."""

    applied: int
    skipped: int
    events_scanned: int
    cursor: str
    completed: bool


def validate_replay_cursor(
    db: MemoryDB,
    cursor: Any,
    *,
    target_db: MemoryDB | None = None,
    namespace: str | None = None,
    event_types: Sequence[str] | None = None,
    allow_legacy_history: bool = True,
) -> ReplayCursor:
    """Validate a portable replay cursor against ``db`` and optional ``target_db`` (#94).

    Runs strictly before any mutation. Raises ``ReplayCursorError`` with an
    actionable reason and recovery instruction on any mismatch.
    """
    decoded = decode_cursor(cursor)
    src_ident = db.mount_identity()
    src_bounds = db.event_log_bounds()

    if not allow_legacy_history and src_ident.legacy_history:
        raise ReplayCursorError(
            "legacy_history_unanchored",
            f"Source store mount_id={src_ident.mount_id!r} has a pre-#94 legacy history "
            "and allow_legacy_history=False.",
            expected="anchored history",
            actual="legacy_history",
            recovery=(
                "Export a full snapshot/package from the legacy store and hydrate a clean clone."
            ),
        )

    if decoded.mount_id != src_ident.mount_id:
        raise ReplayCursorError(
            "wrong_source",
            f"Cursor mount_id={decoded.mount_id!r} does not match source "
            f"mount_id={src_ident.mount_id!r}.",
            expected=src_ident.mount_id,
            actual=decoded.mount_id,
            recovery="Use a cursor issued for this logical mount or switch to the matching mount.",
        )

    if decoded.replica_id != src_ident.replica_id or decoded.history_id != src_ident.history_id:
        raise ReplayCursorError(
            "replaced_history",
            f"Cursor replica/history ({decoded.replica_id!r}, {decoded.history_id!r}) "
            f"does not match source ({src_ident.replica_id!r}, {src_ident.history_id!r}).",
            expected={"replica_id": src_ident.replica_id, "history_id": src_ident.history_id},
            actual={"replica_id": decoded.replica_id, "history_id": decoded.history_id},
            recovery="Re-clone or restore from a current snapshot/package of the active replica.",
        )

    tgt_base_snap = target_db.mount_identity().base_snapshot_id if target_db is not None else ""
    if src_ident.base_snapshot_id and decoded.base_snapshot_id != src_ident.base_snapshot_id:
        raise ReplayCursorError(
            "incompatible_base",
            f"Cursor base_snapshot_id={decoded.base_snapshot_id!r} does not match "
            f"source base_snapshot_id={src_ident.base_snapshot_id!r}.",
            expected=src_ident.base_snapshot_id,
            actual=decoded.base_snapshot_id,
            recovery="Restore a clone from the matching base snapshot before replaying.",
        )
    if decoded.base_snapshot_id and decoded.base_snapshot_id not in (
        src_ident.base_snapshot_id,
        tgt_base_snap,
    ):
        raise ReplayCursorError(
            "incompatible_base",
            f"Cursor base_snapshot_id={decoded.base_snapshot_id!r} is incompatible with "
            f"source base={src_ident.base_snapshot_id!r} and target base={tgt_base_snap!r}.",
            expected=tgt_base_snap or src_ident.base_snapshot_id,
            actual=decoded.base_snapshot_id,
            recovery="Restore a clone from the matching base snapshot before replaying.",
        )

    if namespace is not None and namespace != decoded.namespace:
        raise ReplayCursorError(
            "scope_mismatch",
            f"Requested replay namespace={namespace!r} does not match cursor "
            f"scope.namespace={decoded.namespace!r}.",
            expected=decoded.namespace,
            actual=namespace,
            recovery="Issue a replay cursor for the requested namespace scope.",
        )

    if event_types is not None:
        norm_types = _normalize_event_types(event_types)
        if norm_types != decoded.event_types:
            raise ReplayCursorError(
                "scope_mismatch",
                f"Requested event_types={list(norm_types)!r} does not match cursor "
                f"scope.event_types={list(decoded.event_types)!r}.",
                expected=list(decoded.event_types),
                actual=list(norm_types),
                recovery="Issue a replay cursor with the matching event_types scope.",
            )

    trimmed_through = int(src_bounds["trimmed_through_seq"])
    head_seq = int(src_bounds["head_seq"])
    max_seq = int(src_bounds["max_seq"])

    if head_seq == 0 and trimmed_through == 0:
        if decoded.seq > 0:
            raise ReplayCursorError(
                "stale_base",
                f"Source history is empty (head_seq=0); cursor seq={decoded.seq} "
                "cannot be satisfied.",
                expected=0,
                actual=decoded.seq,
                recovery=(
                    "Issue a new cursor from the empty store or restore from a valid snapshot."
                ),
            )
    else:
        if decoded.seq < trimmed_through:
            raise ReplayCursorError(
                "stale_base",
                f"Cursor seq={decoded.seq} precedes trimmed history watermark "
                f"(trimmed_through_seq={trimmed_through}).",
                expected=f"seq>={trimmed_through}",
                actual=decoded.seq,
                recovery="Export a fresh full snapshot/package and re-hydrate the target clone.",
            )
        if decoded.seq > max_seq:
            raise ReplayCursorError(
                "stale_base",
                f"Cursor seq={decoded.seq} is not available in source events "
                f"(max_seq={max_seq}, head_seq={head_seq}).",
                expected=f"seq<={max_seq}",
                actual=decoded.seq,
                recovery="Export a fresh full snapshot/package and re-hydrate the target clone.",
            )
        if decoded.seq > 0:
            anchor_row = db.event_at_seq(decoded.seq)
            if anchor_row is None:
                raise ReplayCursorError(
                    "stale_base",
                    f"Anchor event at seq={decoded.seq} is missing from source history.",
                    expected=decoded.anchor_event_id,
                    actual=None,
                    recovery=(
                        "Export a fresh full snapshot/package and re-hydrate the target clone."
                    ),
                )
            actual_event_id = str(anchor_row["event_id"])
            if actual_event_id != decoded.anchor_event_id:
                raise ReplayCursorError(
                    "incompatible_base",
                    f"Anchor event mismatch at seq={decoded.seq}: cursor expects "
                    f"{decoded.anchor_event_id!r}, source has {actual_event_id!r}.",
                    expected=decoded.anchor_event_id,
                    actual=actual_event_id,
                    recovery=(
                        "Source and cursor diverged at or before seq; "
                        "restore from a clean snapshot."
                    ),
                )

    if target_db is not None:
        from hotmem.mount import compare_mount_lineage

        tgt_ident = target_db.mount_identity()
        tgt_bounds = target_db.event_log_bounds()
        tgt_saved_cursor = (
            target_db.get_meta("last_replay_cursor") or target_db.get_meta("base_cursor") or ""
        )
        target_has_events = tgt_bounds["head_seq"] > 0

        # An advanced cursor asserts state that already existed at ``seq``. A
        # target with no event history must prove it holds that base (matching
        # saved checkpoint or base snapshot); otherwise suffix-only replay
        # would silently produce a partial clone before mutation (#94).
        if decoded.seq > 0 and not target_has_events:
            saved_matches = False
            saved_base_matches = False
            saved_ahead = False
            if tgt_saved_cursor:
                try:
                    saved = decode_cursor(tgt_saved_cursor)
                except ReplayCursorError:
                    saved = None
                if saved is not None:
                    same_history = (
                        saved.mount_id == decoded.mount_id
                        and saved.replica_id == decoded.replica_id
                        and saved.history_id == decoded.history_id
                    )
                    saved_matches = saved.encode() == decoded.encode()
                    saved_ahead = same_history and saved.seq >= decoded.seq
                    saved_base_matches = bool(
                        decoded.base_snapshot_id
                        and saved.base_snapshot_id == decoded.base_snapshot_id
                    )
            base_matches = bool(
                decoded.base_snapshot_id and tgt_ident.base_snapshot_id == decoded.base_snapshot_id
            )
            if not (saved_matches or saved_ahead or saved_base_matches or base_matches):
                raise ReplayCursorError(
                    "incompatible_base",
                    f"Cursor seq={decoded.seq} requires target base state, but the target "
                    "has no event history or matching saved checkpoint.",
                    expected=f"checkpoint at seq={decoded.seq} or base_snapshot_id="
                    f"{decoded.base_snapshot_id!r}",
                    actual={"seq": 0, "base_snapshot_id": tgt_ident.base_snapshot_id},
                    recovery=(
                        "Hydrate the matching snapshot/package before resuming, or issue "
                        "a start cursor for full replay."
                    ),
                )

        target_is_bound = bool(
            tgt_ident.mount_bound
            or tgt_bounds["head_seq"] > 0
            or target_db.get_meta("last_replay_cursor")
        )
        if target_is_bound:
            if tgt_ident.mount_id != src_ident.mount_id:
                raise ReplayCursorError(
                    "wrong_source",
                    f"Target mount_id={tgt_ident.mount_id!r} does not match source "
                    f"mount_id={src_ident.mount_id!r}.",
                    expected=tgt_ident.mount_id,
                    actual=src_ident.mount_id,
                    recovery="Replay only into a clone/target bound to the same logical mount_id.",
                )
            rel = compare_mount_lineage(db, target_db)
            if rel["relationship"] == "fork":
                raise ReplayCursorError(
                    "fork_diverged",
                    f"Target replica {tgt_ident.replica_id!r} is a divergent runtime fork "
                    f"of source {src_ident.replica_id!r} ({rel['reason']}); multi-writer "
                    "conflict resolution is undefined.",
                    expected="clean clone or follower replica",
                    actual=rel["relationship"],
                    recovery=(
                        "Export and restore an authoritative full snapshot/package "
                        "to resolve the fork."
                    ),
                )
            if rel["relationship"] == "replacement":
                raise ReplayCursorError(
                    "replaced_history",
                    f"Target replica {tgt_ident.replica_id!r} has an unanchored replacement "
                    f"history incompatible with source {src_ident.replica_id!r}.",
                    expected=src_ident.history_id,
                    actual=tgt_ident.history_id,
                    recovery="Re-hydrate the target from a snapshot/package of the source mount.",
                )

    return decoded


def replay(
    db: MemoryDB,
    *,
    after_seq: int = 0,
    cursor: Any = None,
    namespace: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream events from the log as an iterator (constant memory).

    When ``cursor`` is provided, validates it as an opaque portable
    ``ReplayCursor`` against ``db`` (rejecting raw SQLite sequences or wrong
    sources/bases before streaming) and filters by the cursor's scope.
    When ``cursor`` is omitted, streams intra-store events strictly after
    ``after_seq``.
    """
    allowed_types: tuple[str, ...] | None = None
    filter_ns = namespace
    if cursor is not None:
        validated = validate_replay_cursor(db, cursor, namespace=namespace)
        seq_cursor = validated.seq
        filter_ns = validated.namespace
        allowed_types = validated.event_types
    else:
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ReplayCursorError(
                "unanchored_sequence",
                f"after_seq must be a non-negative integer, got {after_seq!r}.",
            )
        seq_cursor = after_seq

    while True:
        result = query_events(
            db,
            namespace=filter_ns,
            after_seq=seq_cursor,
            limit=500,
            ascending=True,
        )
        events = result["events"]
        if not events:
            break
        for evt in events:
            if allowed_types is not None and evt["event_type"] not in allowed_types:
                continue
            yield evt
        seq_cursor = events[-1]["seq"]
        if result["next_seq"] is None or len(events) < 500:
            break


_REPLAY_BATCH = 1000


def _event_to_memory_record(event: dict[str, Any]) -> Any | None:
    """Convert a ``memory.created`` event payload into a ``MemoryRecord``."""
    from hotmem.db import MemoryRecord

    p = event.get("payload") or {}
    if not p.get("id"):
        return None
    emb_b64 = p.get("embedding_b64")
    emb = base64.b64decode(emb_b64) if emb_b64 else b""
    return MemoryRecord(
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


def replay_with_cursor(
    db: MemoryDB,
    target_db: MemoryDB,
    *,
    cursor: Any = None,
    max_events: int | None = None,
    namespace: str | None = None,
    allow_legacy_history: bool = True,
) -> ReplayResult:
    """Validate cursor and replay ``memory.created`` events into ``target_db`` (#94).

    - Validates source identity, base/history identity, trim watermarks, and
      target clone/fork compatibility before any target mutation.
    - Runs all inserts and cursor checkpoint updates in one transaction on
      ``target_db``; any failure rolls back so interrupted runs can resume
      cleanly from the last committed cursor.
    - Reusing a completed cursor is idempotent (``applied == 0``).
    """
    from hotmem.annotations import AnnotationValidationError
    from hotmem.interchange.fingerprint import (
        record_fingerprint,
        state_fingerprint_from_list,
    )

    if max_events is not None and (isinstance(max_events, bool) or max_events <= 0):
        raise ValueError("max_events must be a positive integer when provided")

    if cursor is not None:
        resolved_cursor = decode_cursor(cursor)
    else:
        saved_cursor = target_db.get_meta("last_replay_cursor") or target_db.get_meta("base_cursor")
        if saved_cursor:
            resolved_cursor = decode_cursor(saved_cursor)
        else:
            resolved_cursor = issue_replay_cursor(
                db,
                position="start",
                namespace=namespace,
            )

    validated = validate_replay_cursor(
        db,
        resolved_cursor,
        target_db=target_db,
        namespace=namespace,
        allow_legacy_history=allow_legacy_history,
    )

    saved_last_cursor = target_db.get_meta("last_replay_cursor") or ""
    tgt_guard_ident = target_db.mount_identity()
    tgt_guard_saved = (
        target_db.get_meta("last_replay_cursor") or target_db.get_meta("base_cursor") or ""
    )
    needs_foreign_guard = bool(
        not tgt_guard_ident.mount_bound
        and target_db.event_log_bounds()["head_seq"] == 0
        and not tgt_guard_saved
        and target_db.count() > 0
    )
    if needs_foreign_guard and max_events is not None:
        raise ReplayCursorError(
            "incompatible_base",
            "Bounded replay cannot verify an unbound target that already holds local "
            "records; run a full replay or replay into a clean target.",
            expected="empty target or a bound clone of the source mount",
            actual=f"{target_db.count()} local records",
            recovery="Use max_events=None for the initial replay, or hydrate a clean clone first.",
        )

    # Pass 1 (no mutation): determine the replay window, checkpoint, and -- for a
    # dedupe-only target -- the source identity keys. Records themselves are not
    # retained, so memory stays bounded by one page (#94).
    seq_cursor = validated.seq
    last_seq = validated.seq
    last_event_id = validated.anchor_event_id
    events_scanned = 0
    reached_end = True
    source_keys: set[tuple[str, str]] = set()

    while True:
        page = query_events(
            db,
            namespace=validated.namespace,
            after_seq=seq_cursor,
            limit=500,
            ascending=True,
        )
        events = page["events"]
        if not events:
            break
        stop = False
        for idx, evt in enumerate(events):
            last_seq = int(evt["seq"])
            last_event_id = str(evt["event_id"])
            events_scanned += 1
            if (
                evt["event_type"] in validated.event_types
                and evt["event_type"] == EventType.MEMORY_CREATED
                and needs_foreign_guard
            ):
                rec = _event_to_memory_record(evt)
                if rec is not None:
                    source_keys.add(target_db.record_identity_key(rec))
            if max_events is not None and events_scanned >= max_events:
                has_more_in_page = idx + 1 < len(events)
                has_more_pages = (
                    len(
                        db.query_events(
                            namespace=validated.namespace,
                            after_seq=last_seq,
                            limit=1,
                            ascending=True,
                        )
                    )
                    > 0
                )
                if has_more_in_page or has_more_pages:
                    reached_end = False
                stop = True
                break
        if stop:
            break
        seq_cursor = events[-1]["seq"]
        if page["next_seq"] is None or len(events) < 500:
            break

    # Replay is a one-way clone operation. An unbound target that already holds
    # rows is allowed only when every existing row is a duplicate of a source
    # record (dedupe replay). Foreign local rows would otherwise be laundered
    # into the new clone base and mount identity (#94).
    if needs_foreign_guard:
        foreign_keys = {
            target_db.record_identity_key(row) for row in target_db.all_rows()
        } - source_keys
        if foreign_keys:
            sample = sorted(key[1] or key[0] for key in foreign_keys)[:5]
            raise ReplayCursorError(
                "incompatible_base",
                "Target contains local records that are not part of the source replay; "
                "refusing to adopt the source mount identity and mix state.",
                expected="empty target or a bound clone of the source mount",
                actual=f"{len(foreign_keys)} foreign record(s), e.g. {sample}",
                recovery=(
                    "Replay into a clean target, or hydrate this store from its own "
                    "mount snapshot before replaying."
                ),
            )

    next_cursor = ReplayCursor(
        version=validated.version,
        mount_id=validated.mount_id,
        replica_id=validated.replica_id,
        history_id=validated.history_id,
        base_snapshot_id=validated.base_snapshot_id,
        namespace=validated.namespace,
        event_types=validated.event_types,
        seq=last_seq,
        anchor_event_id=last_event_id if last_seq > 0 else "",
    )
    encoded_next = next_cursor.encode()

    # If the cursor was already at head and target is already bound with this cursor,
    # return idempotently without mutating the target DB.
    if events_scanned == 0 and saved_last_cursor == encoded_next:
        return ReplayResult(
            applied=0,
            skipped=0,
            events_scanned=0,
            cursor=encoded_next,
            completed=True,
        )

    # Pass 2: stream the same window and insert one page at a time inside a
    # single transaction. Any failure rolls back to the last committed cursor.
    applied = 0
    skipped = 0
    src_ident = db.mount_identity()

    try:
        seq_cursor = validated.seq
        stop = False
        while not stop:
            page = query_events(
                db,
                namespace=validated.namespace,
                after_seq=seq_cursor,
                limit=500,
                ascending=True,
            )
            events = page["events"]
            if not events:
                break
            page_records: list[Any] = []
            for evt in events:
                if max_events is not None and int(evt["seq"]) > last_seq:
                    stop = True
                    break
                if (
                    evt["event_type"] in validated.event_types
                    and evt["event_type"] == EventType.MEMORY_CREATED
                ):
                    rec = _event_to_memory_record(evt)
                    if rec is not None:
                        page_records.append(rec)
            if page_records:
                inserted = target_db.insert_many_ignore(page_records, _commit=False)
                applied += inserted
                skipped += len(page_records) - inserted
            if stop:
                break
            seq_cursor = events[-1]["seq"]
            if max_events is not None and seq_cursor >= last_seq:
                break
            if page["next_seq"] is None or len(events) < 500:
                break

        target_fps: list[str] = []
        for row in target_db.iter_rows(include_embedding=False):
            try:
                target_fps.append(record_fingerprint(row))
            except AnnotationValidationError:
                continue
        target_fp = state_fingerprint_from_list(target_fps)
        target_db.record_clone_lineage(
            source_mount_id=src_ident.mount_id,
            source_replica_id=src_ident.replica_id,
            source_history_id=src_ident.history_id,
            base_snapshot_id=validated.base_snapshot_id
            or target_db.mount_identity().base_snapshot_id,
            base_state_fingerprint=target_fp,
            base_cursor=encoded_next,
            adopt_mount_id=True,
            _commit=False,
        )
        target_db.set_meta("last_replay_cursor", encoded_next, _commit=False)
        target_db.commit()
    except Exception:
        target_db.rollback()
        raise

    return ReplayResult(
        applied=applied,
        skipped=skipped,
        events_scanned=events_scanned,
        cursor=encoded_next,
        completed=reached_end,
    )


def replay_into(
    db: MemoryDB,
    target_db: MemoryDB,
    *,
    cursor: Any = None,
    max_events: int | None = None,
    namespace: str | None = None,
    allow_legacy_history: bool = True,
) -> int:
    """Reconstruct memory state from the event log into ``target_db`` (#73/#94/#100).

    Validates source/base/history identity before any mutation and replays
    ``memory.created`` events via bounded ``insert_many_ignore`` batches in a
    single transaction. Returns the number of records actually applied.
    """
    res = replay_with_cursor(
        db,
        target_db,
        cursor=cursor,
        max_events=max_events,
        namespace=namespace,
        allow_legacy_history=allow_legacy_history,
    )
    return res.applied


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
