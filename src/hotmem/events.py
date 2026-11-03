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
     The checksum is the full SHA-256 digest of the token prefix and payload; it
     detects corruption and accidental metadata drift, but it is not a
     signature or authentication mechanism (authentication is an explicit
     non-goal for this contract -- use authenticated transport). Treat cursors
     and manifests as untrusted input and verify source/base identity before
     replay.

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
SCOPE_MODE_LEGACY = "legacy"
SCOPE_MODE_EXPLICIT = "explicit"
SCOPE_MODE_RECONSTRUCTING = "reconstructing"
RECONSTRUCTING_EVENT_TYPES: tuple[str, ...] = (
    "memory.created",
    "memory.deleted",
    "memory.updated",
)
EVENT_PAYLOAD_SCHEMA_VERSION = 1
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


class ReplayExecutionError(ReplayCursorError):
    """Error raised when a state-transition event fails pre-validation or replay (#98).

    Raised strictly before any target mutation (or after full transaction rollback)
    when a replayed event has an unknown schema version, missing base record,
    invalid transition order, tombstone resurrection attempt, or identity mismatch.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
        recovery: str = "",
        event_seq: int | None = None,
        event_id: str = "",
        memory_id: str = "",
    ) -> None:
        self.event_seq = event_seq
        self.event_id = event_id
        self.memory_id = memory_id
        super().__init__(
            reason,
            message,
            expected=expected,
            actual=actual,
            recovery=recovery
            or (
                "Re-hydrate the target from an authoritative snapshot/package "
                "or repair the malformed event stream before replaying."
            ),
        )


class TombstoneCompactionError(ReplayCursorError):
    """Error raised when tombstone compaction preconditions are violated (#98)."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
        recovery: str = "",
    ) -> None:
        super().__init__(
            reason,
            message,
            expected=expected,
            actual=actual,
            recovery=recovery
            or (
                "Trim the event log through the tombstone deletion sequence and ensure "
                "all downstream replay cursors have advanced past that horizon first."
            ),
        )


def _require_persistent_identity(ident: Any, where: str) -> None:
    """Fail with an actionable diagnostic when a store has no persistent identity (#94)."""
    if not (ident.mount_id and ident.replica_id and ident.history_id):
        raise ReplayCursorError(
            "no_persistent_identity",
            f"{where} store has no persistent mount/replica/history identity "
            "(pre-#94 database or read-only open).",
            expected="persisted mount_id, replica_id, and history_id",
            actual={
                "mount_id": ident.mount_id,
                "replica_id": ident.replica_id,
                "history_id": ident.history_id,
            },
            recovery="Open the store once in writable mode to initialize its identity.",
        )


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
    scope_mode: str = SCOPE_MODE_LEGACY
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
        if self.scope_mode not in (
            SCOPE_MODE_LEGACY,
            SCOPE_MODE_EXPLICIT,
            SCOPE_MODE_RECONSTRUCTING,
        ):
            raise ReplayCursorError(
                "malformed_cursor",
                f"Cursor scope_mode must be one of {SCOPE_MODE_LEGACY!r}, "
                f"{SCOPE_MODE_EXPLICIT!r}, or {SCOPE_MODE_RECONSTRUCTING!r}; "
                f"got {self.scope_mode!r}.",
            )

    @property
    def scope(self) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "event_types": list(self.event_types),
            "namespace": self.namespace,
        }
        if self.scope_mode != SCOPE_MODE_LEGACY:
            scope["scope_mode"] = self.scope_mode
        return scope

    def to_payload_dict(self) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "event_types": list(self.event_types),
            "namespace": self.namespace,
        }
        if self.scope_mode != SCOPE_MODE_LEGACY:
            scope["scope_mode"] = self.scope_mode
        return {
            "anchor_event_id": self.anchor_event_id,
            "base_snapshot_id": self.base_snapshot_id,
            "history_id": self.history_id,
            "mount_id": self.mount_id,
            "replica_id": self.replica_id,
            "scope": scope,
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
        checksum = hashlib.sha256(signing_input).hexdigest()
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
    expected_sig = hashlib.sha256(f"{prefix}.{payload_b64}".encode("ascii")).hexdigest()
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
    if "event_types" not in scope_raw:
        raise ReplayCursorError(
            "malformed_cursor",
            "Replay cursor payload is missing scope.event_types.",
        )

    return ReplayCursor(
        version=version,
        mount_id=data.get("mount_id", ""),
        replica_id=data.get("replica_id", ""),
        history_id=data.get("history_id", ""),
        base_snapshot_id=data.get("base_snapshot_id", ""),
        namespace=scope_raw.get("namespace"),
        event_types=scope_raw.get("event_types"),
        scope_mode=scope_raw.get("scope_mode", SCOPE_MODE_LEGACY),
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
    _require_persistent_identity(ident, "Source")
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

    if event_types is None:
        resolved_types = RECONSTRUCTING_EVENT_TYPES
        scope_mode = SCOPE_MODE_RECONSTRUCTING
    else:
        resolved_types = _normalize_event_types(event_types)
        scope_mode = (
            SCOPE_MODE_RECONSTRUCTING
            if resolved_types == RECONSTRUCTING_EVENT_TYPES
            else SCOPE_MODE_EXPLICIT
        )
    return ReplayCursor(
        mount_id=ident.mount_id,
        replica_id=ident.replica_id,
        history_id=ident.history_id,
        base_snapshot_id=resolved_base,
        namespace=namespace,
        event_types=resolved_types,
        scope_mode=scope_mode,
        seq=target_seq,
        anchor_event_id=anchor_id,
    )


class EventType:
    """Canonical event type strings. Stored as plain text for SQL introspection."""

    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_DELETED = "memory.deleted"
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


OBSERVATIONAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.MEMORY_DEDUPLICATED,
        EventType.MEMORY_PROMOTION,
        EventType.MEMORY_CANDIDATE,
        EventType.SNAPSHOT_IMPORTED,
        EventType.BUNDLE_IMPORTED,
        EventType.BUNDLE_DISCOVERED,
        EventType.HYGIENE_CHECKED,
        EventType.HYGIENE_WARNING,
        EventType.INDEX_REBUILT,
        EventType.SYNC_APPLIED,
        EventType.HANDOFF_APPLIED,
    }
)


def is_reconstructing_event(event_or_type: str | Mapping[str, Any]) -> bool:
    """Return True iff the event or event_type is a state-reconstructing mutation (#98).

    Observational events (deduplication, promotion/candidate signals, hygiene,
    imports, index rebuilds, sync/handoff summaries) are never reconstructing
    unless their contract explicitly declares otherwise.
    """
    if isinstance(event_or_type, str):
        return event_or_type in RECONSTRUCTING_EVENT_TYPES
    if isinstance(event_or_type, Mapping):
        etype = str(event_or_type.get("event_type") or "")
        payload = event_or_type.get("payload")
        if isinstance(payload, Mapping):
            category = payload.get("event_category")
            if category == "observational":
                return False
        return etype in RECONSTRUCTING_EVENT_TYPES
    return False


def classify_event(
    event_or_type: str | Mapping[str, Any],
) -> Literal["reconstructing", "observational"]:
    """Return ``'reconstructing'`` or ``'observational'`` for an event or type (#98)."""
    return "reconstructing" if is_reconstructing_event(event_or_type) else "observational"


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 (stdlib only)."""
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_origin_identity(db: MemoryDB | None) -> dict[str, str]:
    """Return the source/runtime identity envelope for ``db`` (#98)."""
    if db is None:
        return {
            "mount_id": "",
            "replica_id": "",
            "history_id": "",
            "runtime_generation": "",
        }
    ident = db.mount_identity()
    return {
        "mount_id": str(ident.mount_id or ""),
        "replica_id": str(ident.replica_id or ""),
        "history_id": str(ident.history_id or ""),
        "runtime_generation": str(ident.runtime_generation or ""),
    }


_ENVELOPE_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "payload_schema_version",
        "event_category",
        "transition",
        "record_fingerprint",
        "previous_content_hash",
        "previous_record_fingerprint",
        "origin",
        "occurred_at",
        "deleted_at",
        "reason",
    }
)


def _strip_envelope_keys(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``record`` without reconstructing-envelope-only metadata keys."""
    return {k: v for k, v in record.items() if k not in _ENVELOPE_ONLY_KEYS}


def _safe_record_fingerprint(record: Mapping[str, Any]) -> str:
    """Compute the canonical #99 record_fingerprint for a record dict, or '' on malformed input."""
    from hotmem.interchange.fingerprint import record_fingerprint

    try:
        return record_fingerprint(_strip_envelope_keys(record))
    except Exception:
        return ""


def _serialize_json_column(val: Any, default: str | None) -> str | None:
    if val is None:
        return default
    if isinstance(val, str):
        return val if val else default
    return json.dumps(val, sort_keys=True)


def _build_memory_created_payload(
    record: dict[str, Any],
    *,
    origin: dict[str, str] | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Build a full-state v1 payload from a memory row for deterministic replay (#98, #99).

    Captures all 27 portable columns required by #99/#100 plus the #98
    reconstructing envelope metadata (`payload_schema_version`,
    `event_category`, `transition`, `record_fingerprint`, `origin`).
    Embeddings are base64-encoded so the payload stays JSON-safe.
    """
    raw_imp = record.get("importance")
    importance = 0.5 if raw_imp is None else float(raw_imp)
    raw_schema_ver = record.get("schema_version")
    schema_version = 1 if raw_schema_ver is None else int(raw_schema_ver)
    raw_cand = record.get("promotion_candidate")
    promotion_candidate = 0 if raw_cand is None else int(bool(raw_cand))

    payload: dict[str, Any] = {
        "payload_schema_version": EVENT_PAYLOAD_SCHEMA_VERSION,
        "event_category": "reconstructing",
        "transition": "create",
        "id": record.get("id", ""),
        "identifier": record.get("identifier", ""),
        "fact_text": record.get("fact_text") or "",
        "embedding_dim": record.get("embedding_dim") or 0,
        "embedding_model": record.get("embedding_model") or "",
        "source": record.get("source") or "",
        "importance": importance,
        "metadata_json": _serialize_json_column(record.get("metadata_json"), "{}"),
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
        "promotion_candidate": promotion_candidate,
        "tags": _serialize_json_column(record.get("tags"), "[]"),
        "schema_version": schema_version,
        "fact_summary": record.get("fact_summary"),
        "provenance_json": _serialize_json_column(record.get("provenance_json"), None),
        "snapshot_id": record.get("snapshot_id") or "",
        "parent_memory": record.get("parent_memory") or "",
        "related_memories": _serialize_json_column(record.get("related_memories"), "[]"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }
    emb = record.get("embedding")
    if emb:
        payload["embedding_b64"] = base64.b64encode(emb).decode("ascii")
    elif record.get("embedding_b64"):
        payload["embedding_b64"] = str(record["embedding_b64"])
    ttl = record.get("ttl_seconds")
    if ttl is not None:
        payload["ttl_seconds"] = ttl
    payload["record_fingerprint"] = _safe_record_fingerprint(payload)
    if origin is not None:
        payload["origin"] = dict(origin)
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    return payload


def _build_memory_updated_payload(
    after_record: dict[str, Any],
    *,
    before_record: Mapping[str, Any] | None = None,
    origin: dict[str, str] | None = None,
    occurred_at: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a versioned complete-after-state update payload (#98)."""
    payload = _build_memory_created_payload(
        after_record,
        origin=origin,
        occurred_at=occurred_at,
    )
    payload["transition"] = "update"
    if before_record is not None:
        payload["previous_content_hash"] = str(before_record.get("content_hash") or "")
        payload["previous_record_fingerprint"] = _safe_record_fingerprint(before_record)
    if reason:
        payload["reason"] = str(reason)
    return payload


def _build_memory_deleted_payload(
    before_record: Mapping[str, Any],
    *,
    deleted_at: str | None = None,
    origin: dict[str, str] | None = None,
    occurred_at: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a versioned delete/tombstone event payload (#98)."""
    ts = deleted_at or occurred_at or _utc_now_iso()
    payload: dict[str, Any] = {
        "payload_schema_version": EVENT_PAYLOAD_SCHEMA_VERSION,
        "event_category": "reconstructing",
        "transition": "delete",
        "id": str(before_record.get("id") or ""),
        "identifier": str(before_record.get("identifier") or ""),
        "namespace": str(before_record.get("namespace") or ""),
        "content_hash": str(before_record.get("content_hash") or ""),
        "record_fingerprint": _safe_record_fingerprint(before_record),
        "deleted_at": ts,
    }
    if origin is not None:
        payload["origin"] = dict(origin)
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    if reason:
        payload["reason"] = str(reason)
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
    stored_payload: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
    if (
        event_type in RECONSTRUCTING_EVENT_TYPES
        and stored_payload.get("payload_schema_version") == EVENT_PAYLOAD_SCHEMA_VERSION
    ):
        if "origin" not in stored_payload:
            stored_payload["origin"] = _extract_origin_identity(db)
        if "occurred_at" not in stored_payload:
            stored_payload["occurred_at"] = occurred_at
    payload_json = json.dumps(stored_payload, sort_keys=True, default=str)
    seq = db.append_event(
        event_type=event_type,
        event_id=event_id,
        memory_id=memory_id,
        namespace=namespace,
        occurred_at=occurred_at,
        payload_json=payload_json,
        _commit=_commit,
    )
    reconstructing = is_reconstructing_event({"event_type": event_type, "payload": stored_payload})
    return {
        "seq": seq,
        "event_id": event_id,
        "memory_id": memory_id,
        "namespace": namespace,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "reconstructing": reconstructing,
        "event_category": "reconstructing" if reconstructing else "observational",
        "payload": stored_payload,
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
    _require_persistent_identity(src_ident, "Source")
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
        non_recon = [t for t in norm_types if t not in RECONSTRUCTING_EVENT_TYPES]
        if non_recon:
            raise ReplayCursorError(
                "non_reconstructing_event_type",
                f"Requested event_types contains non-reconstructing event type(s): {non_recon}.",
                expected=list(RECONSTRUCTING_EVENT_TYPES),
                actual=list(norm_types),
                recovery="Restrict replay cursors to reconstructing state-mutation event types.",
            )
        legacy_default_scope = (
            decoded.scope_mode == SCOPE_MODE_LEGACY
            and decoded.event_types == DEFAULT_REPLAY_EVENT_TYPES
        )
        if norm_types != decoded.event_types and not (
            legacy_default_scope and norm_types == RECONSTRUCTING_EVENT_TYPES
        ):
            raise ReplayCursorError(
                "scope_mismatch",
                f"Requested event_types={list(norm_types)!r} does not match cursor "
                f"scope.event_types={list(decoded.event_types)!r}.",
                expected=list(decoded.event_types),
                actual=list(norm_types),
                recovery="Issue a replay cursor with the matching event_types scope.",
            )

    non_recon_cursor = [t for t in decoded.event_types if t not in RECONSTRUCTING_EVENT_TYPES]
    if non_recon_cursor:
        raise ReplayCursorError(
            "non_reconstructing_event_type",
            f"Cursor scope.event_types contains non-reconstructing event type(s): "
            f"{non_recon_cursor}.",
            expected=list(RECONSTRUCTING_EVENT_TYPES),
            actual=list(decoded.event_types),
            recovery="Issue a replay cursor with only reconstructing event types.",
        )

    trimmed_through = int(src_bounds["trimmed_through_seq"])
    compacted_tombstones_through = int(db.get_meta("compacted_tombstones_through_seq") or 0)
    head_seq = int(src_bounds["head_seq"])
    max_seq = int(src_bounds["max_seq"])

    if compacted_tombstones_through > 0 and decoded.seq < compacted_tombstones_through:
        raise ReplayCursorError(
            "stale_base",
            f"Cursor seq={decoded.seq} precedes compacted tombstone watermark "
            f"(compacted_tombstones_through_seq={compacted_tombstones_through}).",
            expected=f"seq>={compacted_tombstones_through}",
            actual=decoded.seq,
            recovery="Export a fresh full snapshot/package and re-hydrate the target clone.",
        )

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
        allowed_types = _effective_replay_event_types(validated)
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
    """Convert a ``memory.created`` or ``memory.updated`` event payload into a ``MemoryRecord``."""
    from hotmem.db import MemoryRecord

    p = event.get("payload") or {}
    if not p.get("id"):
        return None
    emb_b64 = p.get("embedding_b64")
    emb = base64.b64decode(emb_b64) if emb_b64 else b""
    raw_imp = p.get("importance")
    importance = 0.5 if raw_imp is None else float(raw_imp)
    raw_schema_ver = p.get("schema_version")
    schema_version = 1 if raw_schema_ver is None else int(raw_schema_ver)
    raw_cand = p.get("promotion_candidate")
    promotion_candidate = 0 if raw_cand is None else int(bool(raw_cand))
    return MemoryRecord(
        id=p["id"],
        identifier=p.get("identifier", ""),
        fact_text=p.get("fact_text", ""),
        embedding=emb,
        embedding_dim=p.get("embedding_dim") or 0,
        embedding_model=p.get("embedding_model") or "",
        source=p.get("source") or "",
        importance=importance,
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
        promotion_candidate=promotion_candidate,
        tags=p.get("tags") or "[]",
        schema_version=schema_version,
        fact_summary=p.get("fact_summary"),
        provenance_json=p.get("provenance_json"),
        snapshot_id=p.get("snapshot_id") or "",
        parent_memory=p.get("parent_memory") or "",
        related_memories=p.get("related_memories") or "[]",
        created_at=p.get("created_at"),
        updated_at=p.get("updated_at"),
    )


def _effective_replay_event_types(
    cursor: ReplayCursor,
    explicit_event_types: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Resolve the active reconstructing event types for a replay operation (#98)."""
    if explicit_event_types is not None:
        return _normalize_event_types(explicit_event_types)
    if cursor.scope_mode == SCOPE_MODE_LEGACY and cursor.event_types == DEFAULT_REPLAY_EVENT_TYPES:
        # Cursors issued before #98 were implicitly create-only. Upgrade them
        # on first use so they cannot silently omit update/delete transitions.
        return RECONSTRUCTING_EVENT_TYPES
    return cursor.event_types


def _load_target_replay_state(
    target_db: MemoryDB,
) -> tuple[
    dict[str, dict[str, str]],
    dict[tuple[str, str], str],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Load target live row fingerprints, tombstones, and watermarks for Pass 1 (#98)."""
    tombstones_by_id = target_db.get_tombstones()
    watermarks_by_id = target_db.get_replay_watermarks()
    live_by_id: dict[str, dict[str, str]] = {}
    live_id_by_ns_hash: dict[tuple[str, str], str] = {}
    if target_db.count() > 0:
        for row in target_db.iter_rows(include_embedding=False):
            mid = str(row.get("id") or "")
            if not mid:
                continue
            ns = str(row.get("namespace") or "")
            chash = str(row.get("content_hash") or "")
            fp = _safe_record_fingerprint(row)
            live_by_id[mid] = {
                "id": mid,
                "namespace": ns,
                "content_hash": chash,
                "record_fingerprint": fp,
            }
            if chash:
                live_id_by_ns_hash[(ns, chash)] = mid
    return live_by_id, live_id_by_ns_hash, tombstones_by_id, watermarks_by_id


def _validate_and_plan_event_step(
    evt: Mapping[str, Any],
    *,
    prev_seq: int,
    effective_event_types: tuple[str, ...],
    src_ident: Any,
    live_by_id: dict[str, dict[str, str]],
    live_id_by_ns_hash: dict[tuple[str, str], str],
    tombstones_by_id: dict[str, dict[str, Any]],
    watermarks_by_id: dict[str, dict[str, Any]],
    mutated_in_this_run: set[str],
    created_in_this_run: set[str],
    legacy_created_ids: set[str] | None = None,
) -> tuple[str, int, str, str, str]:
    """Validate one event in Pass 1 and advance the simulated state (#98).

    Returns ``(disposition, seq, namespace, content_hash, after_fingerprint)``
    where ``disposition`` is one of:
    - ``'observational'``
    - ``'skip_idempotent'``
    - ``'apply_create'``
    - ``'apply_update'``
    - ``'apply_delete'``
    """
    from hotmem.annotations import AnnotationValidationError
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.interchange.fingerprint import record_fingerprint
    from hotmem.interchange.record import normalize_record, validate_record

    raw_seq = evt.get("seq")
    evt_id = str(evt.get("event_id") or "")
    if type(raw_seq) is not int or raw_seq <= 0 or raw_seq <= prev_seq:
        raise ReplayExecutionError(
            "invalid_event_order",
            f"Event sequence must be strictly increasing positive integers; "
            f"got seq={raw_seq!r} after prev_seq={prev_seq}.",
            expected=f"seq>{prev_seq}",
            actual=raw_seq,
            event_seq=raw_seq if type(raw_seq) is int else None,
            event_id=evt_id,
        )
    seq = int(raw_seq)

    payload = evt.get("payload")
    if not isinstance(payload, dict):
        raise ReplayExecutionError(
            "malformed_event_payload",
            f"Event at seq={seq} has non-object payload ({type(payload).__name__}).",
            expected="JSON object",
            actual=type(payload).__name__,
            event_seq=seq,
            event_id=evt_id,
        )

    etype = str(evt.get("event_type") or "")
    if etype not in RECONSTRUCTING_EVENT_TYPES:
        if etype not in OBSERVATIONAL_EVENT_TYPES:
            raise ReplayExecutionError(
                "unsupported_event_type",
                f"Unknown event_type={etype!r} at seq={seq}.",
                expected=list(RECONSTRUCTING_EVENT_TYPES),
                actual=etype,
                event_seq=seq,
                event_id=evt_id,
            )
        if payload.get("event_category") == "reconstructing":
            raise ReplayExecutionError(
                "non_reconstructing_event_type",
                f"Observational event {etype!r} at seq={seq} cannot declare "
                "event_category='reconstructing'.",
                expected="observational",
                actual="reconstructing",
                event_seq=seq,
                event_id=evt_id,
            )
        return ("observational", seq, "", "", "")

    if payload.get("event_category") == "observational":
        return ("observational", seq, "", "", "")

    if etype not in effective_event_types:
        raise ReplayExecutionError(
            "incomplete_reconstructing_scope",
            f"Replay scope {list(effective_event_types)!r} excludes reconstructing "
            f"event {etype!r} at seq={seq}; skipping state transitions is forbidden.",
            expected=list(RECONSTRUCTING_EVENT_TYPES),
            actual=list(effective_event_types),
            event_seq=seq,
            event_id=evt_id,
        )

    if "payload_schema_version" in payload:
        pver = payload["payload_schema_version"]
        if type(pver) is not int or pver != EVENT_PAYLOAD_SCHEMA_VERSION:
            raise ReplayExecutionError(
                "unsupported_payload_version",
                f"Unsupported payload_schema_version={pver!r} at seq={seq}; "
                f"expected {EVENT_PAYLOAD_SCHEMA_VERSION}.",
                expected=EVENT_PAYLOAD_SCHEMA_VERSION,
                actual=pver,
                event_seq=seq,
                event_id=evt_id,
            )
        is_v1 = True
    else:
        if etype != EventType.MEMORY_CREATED:
            raise ReplayExecutionError(
                "missing_payload_version",
                f"Reconstructing event {etype!r} at seq={seq} is missing required "
                f"payload_schema_version={EVENT_PAYLOAD_SCHEMA_VERSION}.",
                expected=EVENT_PAYLOAD_SCHEMA_VERSION,
                actual=None,
                event_seq=seq,
                event_id=evt_id,
            )
        is_v1 = False

    mid = payload.get("id")
    if not isinstance(mid, str) or not mid.strip():
        raise ReplayExecutionError(
            "malformed_event_payload",
            f"Reconstructing event {etype!r} at seq={seq} is missing non-empty payload 'id'.",
            expected="non-empty string id",
            actual=mid,
            event_seq=seq,
            event_id=evt_id,
        )

    top_mid = evt.get("memory_id")
    if top_mid is not None and str(top_mid) != "" and str(top_mid) != mid:
        raise ReplayExecutionError(
            "identity_mismatch",
            f"Event at seq={seq} has envelope memory_id={top_mid!r} != payload.id={mid!r}.",
            expected=mid,
            actual=top_mid,
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )

    raw_ns = payload.get("namespace", "")
    if raw_ns is not None and not isinstance(raw_ns, str):
        raise ReplayExecutionError(
            "malformed_event_payload",
            f"Event at seq={seq} has non-string payload.namespace={raw_ns!r}.",
            expected="string",
            actual=raw_ns,
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )
    ns = raw_ns or ""
    top_ns = evt.get("namespace")
    if is_v1 and top_ns is not None and str(top_ns) != ns:
        raise ReplayExecutionError(
            "identity_mismatch",
            f"Event at seq={seq} has envelope namespace={top_ns!r} != payload.namespace={ns!r}.",
            expected=ns,
            actual=top_ns,
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )

    src_hist = str(getattr(src_ident, "history_id", "") or "") if src_ident is not None else ""
    src_mount = str(getattr(src_ident, "mount_id", "") or "") if src_ident is not None else ""
    src_legacy = (
        bool(getattr(src_ident, "legacy_history", False)) if src_ident is not None else False
    )

    if "origin" in payload and payload["origin"] is not None:
        origin = payload["origin"]
        if not isinstance(origin, dict):
            raise ReplayExecutionError(
                "malformed_event_payload",
                f"Event at seq={seq} has non-object payload.origin.",
                expected="object",
                actual=type(origin).__name__,
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )
        orig_mount = str(origin.get("mount_id") or "")
        orig_hist = str(origin.get("history_id") or "")
        if orig_mount and src_mount and orig_mount != src_mount and not src_legacy:
            raise ReplayExecutionError(
                "origin_identity_mismatch",
                f"Event at seq={seq} origin.mount_id={orig_mount!r} does not match "
                f"source mount_id={src_mount!r}.",
                expected=src_mount,
                actual=orig_mount,
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )
        if orig_hist and src_hist and orig_hist != src_hist and not src_legacy:
            raise ReplayExecutionError(
                "origin_identity_mismatch",
                f"Event at seq={seq} origin.history_id={orig_hist!r} does not match "
                f"source history_id={src_hist!r}.",
                expected=src_hist,
                actual=orig_hist,
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

    if etype in (EventType.MEMORY_CREATED, EventType.MEMORY_UPDATED):
        expected_transition = "create" if etype == EventType.MEMORY_CREATED else "update"
        if payload.get("transition") not in (None, expected_transition):
            raise ReplayExecutionError(
                "malformed_event_payload",
                f"Event {etype!r} at seq={seq} has mismatched "
                f"transition={payload.get('transition')!r}.",
                expected=expected_transition,
                actual=payload.get("transition"),
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )
        clean_record = _strip_envelope_keys(payload)
        try:
            normalized = normalize_record(clean_record)
        except (TypeError, ValueError, AnnotationValidationError) as exc:
            raise ReplayExecutionError(
                "malformed_event_payload",
                f"Event {etype!r} at seq={seq} carries invalid record payload: {exc}",
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            ) from exc

        chash = str(payload.get("content_hash") or "")
        if not chash:
            raise ReplayExecutionError(
                "malformed_event_payload",
                f"Event {etype!r} at seq={seq} is missing non-empty content_hash.",
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        if is_v1:
            issues = validate_record(normalized)
            if issues:
                raise ReplayExecutionError(
                    "malformed_event_payload",
                    f"Event {etype!r} at seq={seq} failed #99 portable record validation: "
                    f"{'; '.join(issues)}",
                    event_seq=seq,
                    event_id=evt_id,
                    memory_id=mid,
                )
            source_uri = str(payload.get("source_uri") or "")
            if not source_uri:
                computed_hash = compute_content_hash(
                    str(payload.get("identifier") or ""),
                    str(payload.get("fact_text") or ""),
                )
                if chash != computed_hash:
                    raise ReplayExecutionError(
                        "identity_mismatch",
                        f"Event {etype!r} at seq={seq} has content_hash={chash!r} != "
                        f"canonical hash {computed_hash!r} for record {mid!r}.",
                        expected=computed_hash,
                        actual=chash,
                        event_seq=seq,
                        event_id=evt_id,
                        memory_id=mid,
                    )
            after_fp = record_fingerprint(clean_record, normalized=normalized)
            declared_fp = payload.get("record_fingerprint")
            if declared_fp is not None and str(declared_fp) != after_fp:
                raise ReplayExecutionError(
                    "identity_mismatch",
                    f"Event {etype!r} at seq={seq} has record_fingerprint mismatch "
                    f"for record {mid!r}.",
                    expected=after_fp,
                    actual=declared_fp,
                    event_seq=seq,
                    event_id=evt_id,
                    memory_id=mid,
                )
        else:
            after_fp = record_fingerprint(clean_record, normalized=normalized)

        if etype == EventType.MEMORY_CREATED:
            if mid in tombstones_by_id:
                tomb = tombstones_by_id[mid]
                tomb_seq = int(tomb.get("deleted_at_seq") or 0)
                tomb_hist = str(tomb.get("origin_history_id") or "")
                if (
                    seq <= tomb_seq
                    and (not tomb_hist or not src_hist or tomb_hist == src_hist)
                    and mid not in created_in_this_run
                ):
                    return ("skip_idempotent", seq, ns, chash, after_fp)
                raise ReplayExecutionError(
                    "tombstoned_record_resurrection",
                    f"Cannot apply memory.created for tombstoned record {mid!r} at seq={seq} "
                    f"(deleted at seq={tomb_seq}); deleted records cannot be resurrected.",
                    expected="un-tombstoned record id",
                    actual=f"tombstoned at seq={tomb_seq}",
                    event_seq=seq,
                    event_id=evt_id,
                    memory_id=mid,
                )

            if mid in live_by_id:
                live = live_by_id[mid]
                if live["namespace"] != ns:
                    raise ReplayExecutionError(
                        "identity_mismatch",
                        f"Record {mid!r} exists in namespace {live['namespace']!r}, "
                        f"cannot create in namespace {ns!r} at seq={seq}.",
                        expected=live["namespace"],
                        actual=ns,
                        event_seq=seq,
                        event_id=evt_id,
                        memory_id=mid,
                    )
                if live["record_fingerprint"] == after_fp and live["content_hash"] == chash:
                    return ("skip_idempotent", seq, ns, chash, after_fp)
                wm = watermarks_by_id.get(mid)
                if (
                    wm is not None
                    and mid not in mutated_in_this_run
                    and int(wm.get("last_seq") or 0) >= seq
                    and (
                        not wm.get("history_id") or not src_hist or wm.get("history_id") == src_hist
                    )
                    and live["record_fingerprint"] == wm.get("record_fingerprint")
                ):
                    return ("skip_idempotent", seq, ns, chash, after_fp)
                raise ReplayExecutionError(
                    "identity_mismatch",
                    f"Cannot apply memory.created at seq={seq}: record {mid!r} already exists "
                    "with conflicting state.",
                    expected=after_fp,
                    actual=live["record_fingerprint"],
                    event_seq=seq,
                    event_id=evt_id,
                    memory_id=mid,
                )

            existing_id = live_id_by_ns_hash.get((ns, chash))
            if existing_id is not None and existing_id != mid:
                if is_v1:
                    raise ReplayExecutionError(
                        "identity_mismatch",
                        f"Cannot apply memory.created at seq={seq}: (namespace={ns!r}, "
                        f"content_hash={chash!r}) is already bound to memory_id={existing_id!r}, "
                        f"not {mid!r}.",
                        expected=existing_id,
                        actual=mid,
                        event_seq=seq,
                        event_id=evt_id,
                        memory_id=mid,
                    )
                # Legacy memory.created without payload_schema_version delegates
                # content_hash deduplication to INSERT OR IGNORE (#73).
                return ("apply_create", seq, ns, chash, after_fp)

            live_by_id[mid] = {
                "id": mid,
                "namespace": ns,
                "content_hash": chash,
                "record_fingerprint": after_fp,
            }
            live_id_by_ns_hash[(ns, chash)] = mid
            mutated_in_this_run.add(mid)
            created_in_this_run.add(mid)
            if not is_v1 and legacy_created_ids is not None:
                legacy_created_ids.add(mid)
            return ("apply_create", seq, ns, chash, after_fp)

        # EventType.MEMORY_UPDATED
        if mid in tombstones_by_id:
            tomb = tombstones_by_id[mid]
            tomb_seq = int(tomb.get("deleted_at_seq") or 0)
            tomb_hist = str(tomb.get("origin_history_id") or "")
            if (
                seq <= tomb_seq
                and (not tomb_hist or not src_hist or tomb_hist == src_hist)
                and mid not in mutated_in_this_run
            ):
                return ("skip_idempotent", seq, ns, chash, after_fp)
            raise ReplayExecutionError(
                "tombstoned_record_mutation",
                f"Cannot apply memory.updated for tombstoned record {mid!r} at seq={seq} "
                f"(deleted at seq={tomb_seq}).",
                expected="live record",
                actual=f"tombstoned at seq={tomb_seq}",
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        if mid not in live_by_id:
            raise ReplayExecutionError(
                "missing_target_record",
                f"Cannot apply memory.updated at seq={seq}: target record {mid!r} does not exist.",
                expected=f"existing record {mid!r}",
                actual=None,
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        live = live_by_id[mid]
        if live["namespace"] != ns:
            raise ReplayExecutionError(
                "identity_mismatch",
                f"Cannot update memory {mid!r} across namespaces from {live['namespace']!r} "
                f"to {ns!r} at seq={seq}.",
                expected=live["namespace"],
                actual=ns,
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        prev_chash = payload.get("previous_content_hash")
        prev_fp = payload.get("previous_record_fingerprint")
        if (
            live["record_fingerprint"] == after_fp
            and live["content_hash"] == chash
            and (
                prev_chash in (None, "")
                or prev_chash != chash
                or live["content_hash"] == str(prev_chash)
            )
            and (
                prev_fp in (None, "")
                or prev_fp != after_fp
                or live["record_fingerprint"] == str(prev_fp)
            )
        ):
            return ("skip_idempotent", seq, ns, chash, after_fp)

        wm = watermarks_by_id.get(mid)
        if (
            wm is not None
            and mid not in mutated_in_this_run
            and int(wm.get("last_seq") or 0) >= seq
            and (not wm.get("history_id") or not src_hist or wm.get("history_id") == src_hist)
            and live["record_fingerprint"] == wm.get("record_fingerprint")
        ):
            return ("skip_idempotent", seq, ns, chash, after_fp)

        if (
            prev_chash is not None
            and str(prev_chash) != ""
            and live["content_hash"] != str(prev_chash)
        ):
            raise ReplayExecutionError(
                "base_state_mismatch",
                f"Cannot apply memory.updated for {mid!r} at seq={seq}: target "
                f"content_hash={live['content_hash']!r} != previous_content_hash={prev_chash!r}.",
                expected=prev_chash,
                actual=live["content_hash"],
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        is_legacy_base = legacy_created_ids is not None and mid in legacy_created_ids
        if (
            not is_legacy_base
            and prev_fp is not None
            and str(prev_fp) != ""
            and live["record_fingerprint"] != str(prev_fp)
        ):
            raise ReplayExecutionError(
                "base_state_mismatch",
                f"Cannot apply memory.updated for {mid!r} at seq={seq}: target "
                f"record_fingerprint={live['record_fingerprint'][:12]} != "
                f"previous_record_fingerprint={str(prev_fp)[:12]}.",
                expected=prev_fp,
                actual=live["record_fingerprint"],
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        existing_id = live_id_by_ns_hash.get((ns, chash))
        if existing_id is not None and existing_id != mid:
            raise ReplayExecutionError(
                "content_hash_conflict",
                f"Cannot apply memory.updated for {mid!r} at seq={seq}: "
                f"(namespace={ns!r}, content_hash={chash!r}) collides with record {existing_id!r}.",
                expected=mid,
                actual=existing_id,
                event_seq=seq,
                event_id=evt_id,
                memory_id=mid,
            )

        if live["content_hash"] != chash:
            live_id_by_ns_hash.pop((ns, live["content_hash"]), None)
        live_by_id[mid] = {
            "id": mid,
            "namespace": ns,
            "content_hash": chash,
            "record_fingerprint": after_fp,
        }
        live_id_by_ns_hash[(ns, chash)] = mid
        mutated_in_this_run.add(mid)
        if legacy_created_ids is not None:
            legacy_created_ids.discard(mid)
        return ("apply_update", seq, ns, chash, after_fp)

    # EventType.MEMORY_DELETED
    if payload.get("transition") not in (None, "delete"):
        raise ReplayExecutionError(
            "malformed_event_payload",
            f"Event {etype!r} at seq={seq} has mismatched "
            f"transition={payload.get('transition')!r}.",
            expected="delete",
            actual=payload.get("transition"),
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )
    del_at = payload.get("deleted_at") or evt.get("occurred_at")
    if not isinstance(del_at, str) or not del_at.strip():
        raise ReplayExecutionError(
            "malformed_event_payload",
            f"Event memory.deleted at seq={seq} is missing non-empty deleted_at timestamp.",
            expected="ISO-8601 timestamp",
            actual=del_at,
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )
    expected_chash = str(payload.get("content_hash") or "")
    expected_fp = str(payload.get("record_fingerprint") or "")

    if mid in tombstones_by_id:
        tomb = tombstones_by_id[mid]
        tomb_seq = int(tomb.get("deleted_at_seq") or 0)
        tomb_evt_id = str(tomb.get("delete_event_id") or "")
        tomb_hist = str(tomb.get("origin_history_id") or "")
        if (
            mid not in live_by_id
            and mid not in mutated_in_this_run
            and (
                (evt_id and tomb_evt_id == evt_id)
                or (seq <= tomb_seq and (not tomb_hist or not src_hist or tomb_hist == src_hist))
            )
        ):
            return ("skip_idempotent", seq, ns, expected_chash, expected_fp)
        raise ReplayExecutionError(
            "already_tombstoned",
            f"Cannot apply memory.deleted at seq={seq}: record {mid!r} is already "
            f"tombstoned (deleted at seq={tomb_seq}).",
            expected="live record",
            actual=f"tombstoned at seq={tomb_seq}",
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )

    if mid not in live_by_id:
        raise ReplayExecutionError(
            "missing_target_record",
            f"Cannot apply memory.deleted at seq={seq}: target record {mid!r} does not exist.",
            expected=f"existing record {mid!r}",
            actual=None,
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )

    live = live_by_id[mid]
    if live["namespace"] != ns:
        raise ReplayExecutionError(
            "identity_mismatch",
            f"Cannot delete memory {mid!r} at seq={seq}: target namespace={live['namespace']!r} "
            f"!= event namespace={ns!r}.",
            expected=live["namespace"],
            actual=ns,
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )
    if expected_chash and live["content_hash"] != expected_chash:
        raise ReplayExecutionError(
            "base_state_mismatch",
            f"Cannot apply memory.deleted for {mid!r} at seq={seq}: target "
            f"content_hash={live['content_hash']!r} != expected {expected_chash!r}.",
            expected=expected_chash,
            actual=live["content_hash"],
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )
    if expected_fp and live["record_fingerprint"] != expected_fp:
        raise ReplayExecutionError(
            "base_state_mismatch",
            f"Cannot apply memory.deleted for {mid!r} at seq={seq}: target "
            f"record_fingerprint={live['record_fingerprint'][:12]} != "
            f"expected {expected_fp[:12]}.",
            expected=expected_fp,
            actual=live["record_fingerprint"],
            event_seq=seq,
            event_id=evt_id,
            memory_id=mid,
        )

    live_id_by_ns_hash.pop((ns, live["content_hash"]), None)
    del live_by_id[mid]
    tombstones_by_id[mid] = {
        "memory_id": mid,
        "namespace": ns,
        "content_hash": expected_chash or live["content_hash"],
        "record_fingerprint": expected_fp or live["record_fingerprint"],
        "deleted_at": del_at,
        "deleted_at_seq": seq,
        "delete_event_id": evt_id,
        "origin_history_id": src_hist,
    }
    mutated_in_this_run.add(mid)
    return (
        "apply_delete",
        seq,
        ns,
        expected_chash or live["content_hash"],
        expected_fp or live["record_fingerprint"],
    )


def apply_event_range(
    target_db: MemoryDB,
    events: Sequence[Mapping[str, Any]],
    *,
    source_identity: Any = None,
    effective_event_types: Sequence[str] | None = None,
) -> tuple[int, int]:
    """Validate and atomically apply an in-memory sequence of events to ``target_db`` (#98).

    Performs full Pass 1 pre-flight validation before any SQLite write and
    executes Pass 2 inside one transaction with rollback on any error.
    Returns ``(applied, skipped)``.
    """
    active_types = (
        _normalize_event_types(effective_event_types)
        if effective_event_types is not None
        else RECONSTRUCTING_EVENT_TYPES
    )
    non_recon = [t for t in active_types if t not in RECONSTRUCTING_EVENT_TYPES]
    if non_recon:
        raise ReplayCursorError(
            "non_reconstructing_event_type",
            f"effective_event_types contains non-reconstructing event type(s): {non_recon}.",
            expected=list(RECONSTRUCTING_EVENT_TYPES),
            actual=list(active_types),
        )

    live_by_id, live_id_by_ns_hash, tombstones_by_id, watermarks_by_id = _load_target_replay_state(
        target_db
    )
    mutated_in_this_run: set[str] = set()
    created_in_this_run: set[str] = set()
    legacy_created_ids: set[str] = set()
    plan_by_seq: dict[int, tuple[str, str]] = {}
    prev_seq = 0

    for evt in events:
        disposition, seq, _ns, _chash, after_fp = _validate_and_plan_event_step(
            evt,
            prev_seq=prev_seq,
            effective_event_types=active_types,
            src_ident=source_identity,
            live_by_id=live_by_id,
            live_id_by_ns_hash=live_id_by_ns_hash,
            tombstones_by_id=tombstones_by_id,
            watermarks_by_id=watermarks_by_id,
            mutated_in_this_run=mutated_in_this_run,
            created_in_this_run=created_in_this_run,
            legacy_created_ids=legacy_created_ids,
        )
        prev_seq = seq
        plan_by_seq[seq] = (disposition, after_fp)

    applied = 0
    skipped = 0
    src_mount = str(getattr(source_identity, "mount_id", "") or "")
    src_rep = str(getattr(source_identity, "replica_id", "") or "")
    src_hist = str(getattr(source_identity, "history_id", "") or "")

    try:
        page_records: list[Any] = []
        page_watermarks: list[dict[str, Any]] = []

        def _flush_created() -> None:
            nonlocal applied, skipped
            if not page_records:
                return
            inserted = target_db.insert_many_ignore(page_records, _commit=False)
            applied += inserted
            skipped += len(page_records) - inserted
            for wm in page_watermarks:
                target_db.set_replay_watermark(**wm, _commit=False)
            page_records.clear()
            page_watermarks.clear()

        for evt in events:
            seq = int(evt["seq"])
            disposition, after_fp = plan_by_seq[seq]
            if disposition == "observational":
                continue
            if disposition == "skip_idempotent":
                skipped += 1
                continue
            payload = evt["payload"]
            evt_id = str(evt.get("event_id") or "")
            if disposition == "apply_create":
                rec = _event_to_memory_record(dict(evt))
                if rec is not None:
                    page_records.append(rec)
                    page_watermarks.append(
                        {
                            "memory_id": rec.id,
                            "namespace": rec.namespace,
                            "content_hash": rec.content_hash,
                            "record_fingerprint": after_fp,
                            "last_seq": seq,
                            "last_event_id": evt_id,
                            "history_id": src_hist,
                        }
                    )
                    if len(page_records) >= _REPLAY_BATCH:
                        _flush_created()
            elif disposition == "apply_update":
                _flush_created()
                rec = _event_to_memory_record(dict(evt))
                assert rec is not None
                if target_db.replace_memory_record(rec, _commit=False):
                    applied += 1
                    target_db.set_replay_watermark(
                        memory_id=rec.id,
                        namespace=rec.namespace,
                        content_hash=rec.content_hash,
                        record_fingerprint=after_fp,
                        last_seq=seq,
                        last_event_id=evt_id,
                        history_id=src_hist,
                        _commit=False,
                    )
                else:
                    skipped += 1
            elif disposition == "apply_delete":
                _flush_created()
                mid = str(payload["id"])
                ns = str(payload.get("namespace") or "")
                deleted = target_db.delete_memory_row(mid, _commit=False)
                target_db.delete_replay_watermark(mid, _commit=False)
                origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
                target_db.record_tombstone(
                    memory_id=mid,
                    namespace=ns,
                    identifier=str(payload.get("identifier") or ""),
                    content_hash=str(payload.get("content_hash") or ""),
                    record_fingerprint=after_fp,
                    deleted_at=str(payload.get("deleted_at") or evt.get("occurred_at") or ""),
                    deleted_at_seq=seq,
                    delete_event_id=evt_id,
                    origin_mount_id=str(origin.get("mount_id") or src_mount),
                    origin_replica_id=str(origin.get("replica_id") or src_rep),
                    origin_history_id=str(origin.get("history_id") or src_hist),
                    reason=str(payload.get("reason") or ""),
                    _commit=False,
                )
                if deleted:
                    applied += 1
                else:
                    skipped += 1

        _flush_created()
        target_db.commit()
    except Exception:
        target_db.rollback()
        raise

    return applied, skipped


def replay_with_cursor(
    db: MemoryDB,
    target_db: MemoryDB,
    *,
    cursor: Any = None,
    max_events: int | None = None,
    namespace: str | None = None,
    event_types: Sequence[str] | None = None,
    allow_legacy_history: bool = True,
) -> ReplayResult:
    """Validate cursor and replay state-reconstructing events into ``target_db`` (#94, #98).

    - Validates source identity, base/history identity, trim/tombstone watermarks,
      target clone/fork compatibility, and the full event transition sequence
      (`memory.created`, `memory.updated`, `memory.deleted`) before any target
      mutation.
    - Runs all inserts/updates/deletes, tombstone writes, and cursor checkpoint
      updates in one transaction on ``target_db``; any failure rolls back so
      interrupted runs can resume cleanly from the last committed cursor.
    - Reusing a completed cursor or replaying the same range twice is idempotent
      (``applied == 0``) and never resurrects tombstoned records.
    - ``max_events`` bounds the number of source events examined per step
      (including observational events that are not replayed), so ``applied``
      can be lower than the budget. Memory is bounded by one page of source events.
    """
    from hotmem.annotations import AnnotationValidationError
    from hotmem.interchange.fingerprint import (
        record_fingerprint,
        state_fingerprint_from_list,
    )

    if max_events is not None and (type(max_events) is not int or max_events <= 0):
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
                event_types=event_types,
            )

    validated = validate_replay_cursor(
        db,
        resolved_cursor,
        target_db=target_db,
        namespace=namespace,
        event_types=event_types,
        allow_legacy_history=allow_legacy_history,
    )
    effective_types = _effective_replay_event_types(validated, event_types)

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

    src_ident = db.mount_identity()
    live_by_id, live_id_by_ns_hash, tombstones_by_id, watermarks_by_id = _load_target_replay_state(
        target_db
    )
    mutated_in_this_run: set[str] = set()
    created_in_this_run: set[str] = set()
    legacy_created_ids: set[str] = set()
    plan_by_seq: dict[int, tuple[str, str]] = {}

    # Pass 1 (no mutation): validate the replay window, transition sequence,
    # checkpoint, and -- for a dedupe-only target -- the source identity keys.
    seq_cursor = validated.seq
    last_seq = validated.seq
    last_event_id = validated.anchor_event_id
    events_scanned = 0
    reached_end = True
    source_keys: set[tuple[str, str]] = set()
    prev_seq = validated.seq

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
            disposition, seq, evt_ns, evt_chash, after_fp = _validate_and_plan_event_step(
                evt,
                prev_seq=prev_seq,
                effective_event_types=effective_types,
                src_ident=src_ident,
                live_by_id=live_by_id,
                live_id_by_ns_hash=live_id_by_ns_hash,
                tombstones_by_id=tombstones_by_id,
                watermarks_by_id=watermarks_by_id,
                mutated_in_this_run=mutated_in_this_run,
                created_in_this_run=created_in_this_run,
                legacy_created_ids=legacy_created_ids,
            )
            prev_seq = seq
            last_seq = seq
            last_event_id = str(evt["event_id"])
            events_scanned += 1
            plan_by_seq[seq] = (disposition, after_fp)
            if needs_foreign_guard and evt_chash:
                source_keys.add((evt_ns, evt_chash))
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
        event_types=effective_types,
        scope_mode=(
            SCOPE_MODE_RECONSTRUCTING
            if effective_types == RECONSTRUCTING_EVENT_TYPES
            else SCOPE_MODE_EXPLICIT
        ),
        seq=last_seq,
        anchor_event_id=last_event_id if last_seq > 0 else "",
    )
    encoded_next = next_cursor.encode()

    # If the cursor was already at head and the target already records this
    # checkpoint (last replay or hydrated base cursor), return idempotently
    # without writing any metadata.
    saved_checkpoints = {
        cursor_value
        for cursor_value in (
            saved_last_cursor,
            target_db.get_meta("base_cursor") or "",
        )
        if cursor_value
    }
    if events_scanned == 0:
        for saved_value in saved_checkpoints:
            try:
                saved_cursor = decode_cursor(saved_value)
            except ReplayCursorError:
                continue
            if (
                saved_cursor.mount_id == validated.mount_id
                and saved_cursor.replica_id == validated.replica_id
                and saved_cursor.history_id == validated.history_id
                and saved_cursor.base_snapshot_id == validated.base_snapshot_id
                and saved_cursor.namespace == validated.namespace
                and saved_cursor.seq == validated.seq
                and saved_cursor.anchor_event_id == validated.anchor_event_id
            ):
                # No source events were examined. Keep the hydrated/last
                # checkpoint byte-stable even when a legacy create-only cursor
                # would otherwise be upgraded to the wider #98 scope.
                return ReplayResult(
                    applied=0,
                    skipped=0,
                    events_scanned=0,
                    cursor=saved_value,
                    completed=True,
                )

    # Pass 2: stream the same window and apply transitions one page at a time
    # inside a single transaction. Any failure rolls back to the last committed cursor.
    applied = 0
    skipped = 0

    try:
        seq_cursor = validated.seq
        stop = False
        page_records: list[Any] = []
        page_watermarks: list[dict[str, Any]] = []

        def _flush_page_created() -> None:
            nonlocal applied, skipped
            if not page_records:
                return
            inserted = target_db.insert_many_ignore(page_records, _commit=False)
            applied += inserted
            skipped += len(page_records) - inserted
            for wm in page_watermarks:
                target_db.set_replay_watermark(**wm, _commit=False)
            page_records.clear()
            page_watermarks.clear()

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

            for evt in events:
                seq = int(evt["seq"])
                if max_events is not None and seq > last_seq:
                    stop = True
                    break
                disposition, after_fp = plan_by_seq.get(seq, ("observational", ""))
                if disposition == "observational":
                    continue
                if disposition == "skip_idempotent":
                    skipped += 1
                    continue
                payload = evt["payload"]
                evt_id = str(evt["event_id"])
                if disposition == "apply_create":
                    rec = _event_to_memory_record(evt)
                    if rec is not None:
                        page_records.append(rec)
                        page_watermarks.append(
                            {
                                "memory_id": rec.id,
                                "namespace": rec.namespace,
                                "content_hash": rec.content_hash,
                                "record_fingerprint": after_fp,
                                "last_seq": seq,
                                "last_event_id": evt_id,
                                "history_id": src_ident.history_id,
                            }
                        )
                elif disposition == "apply_update":
                    _flush_page_created()
                    rec = _event_to_memory_record(evt)
                    assert rec is not None
                    if target_db.replace_memory_record(rec, _commit=False):
                        applied += 1
                        target_db.set_replay_watermark(
                            memory_id=rec.id,
                            namespace=rec.namespace,
                            content_hash=rec.content_hash,
                            record_fingerprint=after_fp,
                            last_seq=seq,
                            last_event_id=evt_id,
                            history_id=src_ident.history_id,
                            _commit=False,
                        )
                    else:
                        skipped += 1
                elif disposition == "apply_delete":
                    _flush_page_created()
                    mid = str(payload["id"])
                    ns = str(payload.get("namespace") or "")
                    deleted = target_db.delete_memory_row(mid, _commit=False)
                    target_db.delete_replay_watermark(mid, _commit=False)
                    origin = (
                        payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
                    )
                    target_db.record_tombstone(
                        memory_id=mid,
                        namespace=ns,
                        identifier=str(payload.get("identifier") or ""),
                        content_hash=str(payload.get("content_hash") or ""),
                        record_fingerprint=after_fp,
                        deleted_at=str(payload.get("deleted_at") or evt["occurred_at"]),
                        deleted_at_seq=seq,
                        delete_event_id=evt_id,
                        origin_mount_id=str(origin.get("mount_id") or src_ident.mount_id),
                        origin_replica_id=str(origin.get("replica_id") or src_ident.replica_id),
                        origin_history_id=str(origin.get("history_id") or src_ident.history_id),
                        reason=str(payload.get("reason") or ""),
                        _commit=False,
                    )
                    if deleted:
                        applied += 1
                    else:
                        skipped += 1

            _flush_page_created()
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
    ``memory.created`` events via bounded page batches in a single transaction.
    A clean target becomes a clone of the source mount (identity binding is a
    documented side effect); targets with foreign local state are rejected.
    Returns the number of records actually applied.
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
    reconstructing = is_reconstructing_event({"event_type": row["event_type"], "payload": payload})
    return {
        "seq": row["seq"],
        "event_id": row["event_id"],
        "memory_id": row["memory_id"],
        "namespace": row["namespace"],
        "event_type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "reconstructing": reconstructing,
        "event_category": "reconstructing" if reconstructing else "observational",
        "payload": payload,
    }


_UNSET = object()


def update_memory_record(
    db: MemoryDB,
    memory_id: str,
    *,
    identifier: str | None = None,
    fact_text: str | None = None,
    importance: float | None = None,
    metadata: dict[str, Any] | str | None = None,
    source: str | None = None,
    ttl_seconds: Any = _UNSET,
    tier: str | None = None,
    promotion_state: str | None = None,
    promotion_candidate: int | bool | None = None,
    parent_memory: str | None = None,
    related_memories: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    schema_version: int | None = None,
    fact_summary: Any = _UNSET,
    provenance: Any = _UNSET,
    snapshot_id: str | None = None,
    source_uri: str | None = None,
    source_format: str | None = None,
    source_checksum: str | None = None,
    byte_offset: Any = _UNSET,
    byte_length: Any = _UNSET,
    updated_at: str | None = None,
    occurred_at: str | None = None,
    reason: str | None = None,
    embedder: Any = None,
    idempotency_key: str | None = None,
    caller_id: str = "local",
    _commit: bool = True,
) -> dict[str, Any]:
    """Atomically replace/update a memory record and emit a ``memory.updated`` event (#98).

    Enforces #99 portable record validation, #100 namespace-scoped content_hash
    uniqueness, and tombstone resurrection prevention. Supports optional
    ``idempotency_key`` via ``operation_ledger`` (#109) for safe retry recovery.
    """
    from hotmem.embed import DEFAULT_EMBEDDER, pack_embedding
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.interchange.record import normalize_record, validate_record

    clean_key = (idempotency_key or "").strip()
    request_digest = ""
    if clean_key:
        req_spec = {
            "op": "update_memory_record",
            "memory_id": memory_id,
            "identifier": identifier,
            "fact_text": fact_text,
            "importance": importance,
            "metadata": metadata,
            "source": source,
            "ttl_seconds": None if ttl_seconds is _UNSET else ttl_seconds,
            "tier": tier,
            "promotion_state": promotion_state,
            "promotion_candidate": promotion_candidate,
            "parent_memory": parent_memory,
            "related_memories": related_memories,
            "tags": tags,
            "schema_version": schema_version,
            "fact_summary": None if fact_summary is _UNSET else fact_summary,
            "provenance": None if provenance is _UNSET else provenance,
            "snapshot_id": snapshot_id,
            "source_uri": source_uri,
            "source_format": source_format,
            "source_checksum": source_checksum,
            "byte_offset": None if byte_offset is _UNSET else byte_offset,
            "byte_length": None if byte_length is _UNSET else byte_length,
            "reason": reason,
        }
        request_digest = hashlib.sha256(
            json.dumps(req_spec, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    before_row = db.get_memory(memory_id)
    tombstone = db.get_tombstone(memory_id)

    if clean_key:
        probe_ns = str((before_row or tombstone or {}).get("namespace") or "")
        existing_op = db.get_operation_ledger(caller_id, probe_ns, clean_key)
        if existing_op is not None:
            if existing_op["request_hash"] != request_digest:
                raise ValueError(
                    f"idempotency_key {clean_key!r} already used with a different update payload"
                )
            cached = json.loads(existing_op["response_json"])
            cached["idempotent_replay"] = True
            return cached

    if tombstone is not None and before_row is None:
        raise KeyError(
            f"memory {memory_id!r} has been deleted "
            f"(tombstoned at seq={tombstone['deleted_at_seq']})"
        )
    if before_row is None:
        raise KeyError(f"memory {memory_id!r} not found")

    ns = str(before_row.get("namespace") or "")
    new_ident = before_row["identifier"] if identifier is None else str(identifier)
    new_fact = (before_row.get("fact_text") or "") if fact_text is None else str(fact_text)
    new_summary = (
        before_row.get("fact_summary")
        if fact_summary is _UNSET
        else (None if fact_summary is None else str(fact_summary))
    )
    new_uri = str(before_row.get("source_uri") or "") if source_uri is None else str(source_uri)
    new_mem_type = "file" if new_uri else str(before_row.get("memory_type") or "fact")

    if new_uri:
        # File-backed record: if identifier or summary or checksum changed, recompute hash
        new_checksum = (
            str(before_row.get("source_checksum") or "")
            if source_checksum is None
            else str(source_checksum)
        )
        if (
            identifier is not None
            or fact_text is not None
            or fact_summary is not _UNSET
            or source_checksum is not None
        ):
            hash_basis = new_fact or new_summary or new_checksum or new_uri
            new_chash = compute_content_hash(new_ident, hash_basis)
        else:
            new_chash = str(before_row.get("content_hash") or "")
    else:
        if identifier is not None or fact_text is not None:
            new_chash = compute_content_hash(new_ident, new_fact)
        else:
            new_chash = str(before_row.get("content_hash") or "")

    # Check #100 (namespace, content_hash) uniqueness against other records
    if new_chash:
        existing_by_hash = db.find_by_namespace_and_hash(ns, new_chash)
        if existing_by_hash is not None and str(existing_by_hash["id"]) != memory_id:
            raise ValueError(
                f"update would collide with existing memory {existing_by_hash['id']!r} "
                f"in namespace {ns!r} for content_hash {new_chash!r}"
            )

    text_changed = (fact_text is not None and new_fact != (before_row.get("fact_text") or "")) or (
        fact_summary is not _UNSET and new_summary != before_row.get("fact_summary")
    )
    if text_changed or embedder is not None:
        active_embedder = embedder if embedder is not None else DEFAULT_EMBEDDER
        search_text = new_fact if new_fact else (new_summary or "")
        if search_text:
            vec = active_embedder.embed(search_text)
            emb_bytes = pack_embedding(vec)
            emb_dim = active_embedder.descriptor.dimension
            emb_model = active_embedder.descriptor.key
        else:
            emb_bytes = b""
            emb_dim = 0
            emb_model = ""
    else:
        emb_bytes = before_row.get("embedding") or b""
        emb_dim = int(before_row.get("embedding_dim") or 0)
        emb_model = str(before_row.get("embedding_model") or "")

    ts = updated_at or occurred_at or _utc_now_iso()
    evt_time = occurred_at or ts

    after_dict: dict[str, Any] = {
        "id": memory_id,
        "identifier": new_ident,
        "fact_text": new_fact,
        "embedding": emb_bytes,
        "embedding_dim": emb_dim,
        "embedding_model": emb_model,
        "source": str(before_row.get("source") or "") if source is None else str(source),
        "importance": (
            (0.5 if before_row.get("importance") is None else float(before_row["importance"]))
            if importance is None
            else float(importance)
        ),
        "metadata_json": (
            str(before_row.get("metadata_json") or "{}")
            if metadata is None
            else _serialize_json_column(metadata, "{}")
        ),
        "content_hash": new_chash,
        "ttl_seconds": (before_row.get("ttl_seconds") if ttl_seconds is _UNSET else ttl_seconds),
        "created_at": before_row.get("created_at"),
        "namespace": ns,
        "tier": str(before_row.get("tier") or "hot") if tier is None else str(tier),
        "memory_type": new_mem_type,
        "source_uri": new_uri,
        "source_format": (
            str(before_row.get("source_format") or "")
            if source_format is None
            else str(source_format)
        ),
        "source_checksum": (
            str(before_row.get("source_checksum") or "")
            if source_checksum is None
            else str(source_checksum)
        ),
        "byte_offset": (before_row.get("byte_offset") if byte_offset is _UNSET else byte_offset),
        "byte_length": (before_row.get("byte_length") if byte_length is _UNSET else byte_length),
        "updated_at": ts,
        "snapshot_id": (
            str(before_row.get("snapshot_id") or "") if snapshot_id is None else str(snapshot_id)
        ),
        "promotion_state": (
            str(before_row.get("promotion_state") or "HOT")
            if promotion_state is None
            else str(promotion_state)
        ),
        "promotion_candidate": (
            int(bool(before_row.get("promotion_candidate")))
            if promotion_candidate is None
            else int(bool(promotion_candidate))
        ),
        "parent_memory": (
            str(before_row.get("parent_memory") or "")
            if parent_memory is None
            else str(parent_memory)
        ),
        "related_memories": (
            str(before_row.get("related_memories") or "[]")
            if related_memories is None
            else _serialize_json_column(related_memories, "[]")
        ),
        "tags": (
            str(before_row.get("tags") or "[]")
            if tags is None
            else _serialize_json_column(tags, "[]")
        ),
        "schema_version": (
            int(before_row.get("schema_version") or 1)
            if schema_version is None
            else int(schema_version)
        ),
        "fact_summary": new_summary,
        "provenance_json": (
            before_row.get("provenance_json")
            if provenance is _UNSET
            else _serialize_json_column(provenance, None)
        ),
    }

    normalized = normalize_record(after_dict)
    errors = validate_record(normalized)
    if errors:
        raise ValueError(f"invalid updated memory record {memory_id!r}: {'; '.join(errors)}")

    origin = _extract_origin_identity(db)
    payload = _build_memory_updated_payload(
        after_dict,
        before_record=before_row,
        origin=origin,
        occurred_at=evt_time,
        reason=reason,
    )
    record_obj = _event_to_memory_record({"payload": payload})
    assert record_obj is not None

    try:
        db.replace_memory_record(record_obj, _commit=False)
        ev = append_event(
            db,
            event_type=EventType.MEMORY_UPDATED,
            memory_id=memory_id,
            namespace=ns,
            payload=payload,
            occurred_at=evt_time,
            _commit=False,
        )
        db.set_replay_watermark(
            memory_id=memory_id,
            namespace=ns,
            content_hash=new_chash,
            record_fingerprint=payload["record_fingerprint"],
            last_seq=int(ev["seq"]),
            last_event_id=str(ev["event_id"]),
            history_id=origin["history_id"],
            _commit=False,
        )
        result = {
            "memory_id": memory_id,
            "namespace": ns,
            "content_hash": new_chash,
            "record_fingerprint": payload["record_fingerprint"],
            "previous_content_hash": payload.get("previous_content_hash", ""),
            "previous_record_fingerprint": payload.get("previous_record_fingerprint", ""),
            "updated_at": ts,
            "event": ev,
            "idempotent_replay": False,
        }
        if clean_key:
            db.put_operation_ledger(
                caller_id=caller_id,
                namespace=ns,
                operation_key=clean_key,
                operation_name="update_memory_record",
                request_hash=request_digest,
                memory_id=memory_id,
                content_hash=new_chash,
                response_json=json.dumps(result, sort_keys=True, default=str),
                _commit=False,
            )
        if _commit:
            db.commit()
    except Exception:
        if _commit:
            db.rollback()
        raise

    return result


def delete_memory_record(
    db: MemoryDB,
    memory_id: str,
    *,
    reason: str = "",
    deleted_at: str | None = None,
    occurred_at: str | None = None,
    idempotency_key: str | None = None,
    caller_id: str = "local",
    _commit: bool = True,
) -> dict[str, Any]:
    """Atomically delete a memory record, persist its tombstone, and emit ``memory.deleted`` (#98).

    Supports optional ``idempotency_key`` via ``operation_ledger`` (#109) so
    retrying an ambiguous delete after a disconnect/restart returns the recorded
    deletion outcome without emitting a duplicate ``memory.deleted`` event.
    """
    clean_key = (idempotency_key or "").strip()
    request_digest = ""
    if clean_key:
        req_spec = {
            "op": "delete_memory_record",
            "memory_id": memory_id,
            "reason": reason,
        }
        request_digest = hashlib.sha256(
            json.dumps(req_spec, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    before_row = db.get_memory(memory_id)
    existing_tombstone = db.get_tombstone(memory_id)

    if clean_key:
        probe_ns = str((before_row or existing_tombstone or {}).get("namespace") or "")
        existing_op = db.get_operation_ledger(caller_id, probe_ns, clean_key)
        if existing_op is not None:
            if existing_op["request_hash"] != request_digest:
                raise ValueError(
                    f"idempotency_key {clean_key!r} already used with a different delete payload"
                )
            cached = json.loads(existing_op["response_json"])
            cached["idempotent_replay"] = True
            return cached

    if before_row is None:
        if existing_tombstone is not None:
            raise KeyError(
                f"memory {memory_id!r} is already deleted "
                f"(tombstoned at seq={existing_tombstone['deleted_at_seq']})"
            )
        raise KeyError(f"memory {memory_id!r} not found")

    ns = str(before_row.get("namespace") or "")
    ts = deleted_at or occurred_at or _utc_now_iso()
    evt_time = occurred_at or ts
    origin = _extract_origin_identity(db)
    payload = _build_memory_deleted_payload(
        before_row,
        deleted_at=ts,
        origin=origin,
        occurred_at=evt_time,
        reason=reason,
    )

    try:
        db.delete_memory_row(memory_id, _commit=False)
        db.delete_replay_watermark(memory_id, _commit=False)
        ev = append_event(
            db,
            event_type=EventType.MEMORY_DELETED,
            memory_id=memory_id,
            namespace=ns,
            payload=payload,
            occurred_at=evt_time,
            _commit=False,
        )
        db.record_tombstone(
            memory_id=memory_id,
            namespace=ns,
            identifier=str(before_row.get("identifier") or ""),
            content_hash=str(before_row.get("content_hash") or ""),
            record_fingerprint=payload["record_fingerprint"],
            deleted_at=ts,
            deleted_at_seq=int(ev["seq"]),
            delete_event_id=str(ev["event_id"]),
            origin_mount_id=origin["mount_id"],
            origin_replica_id=origin["replica_id"],
            origin_history_id=origin["history_id"],
            reason=reason,
            _commit=False,
        )
        result = {
            "memory_id": memory_id,
            "namespace": ns,
            "identifier": str(before_row.get("identifier") or ""),
            "content_hash": str(before_row.get("content_hash") or ""),
            "record_fingerprint": payload["record_fingerprint"],
            "deleted_at": ts,
            "deleted_at_seq": int(ev["seq"]),
            "delete_event_id": str(ev["event_id"]),
            "event": ev,
            "idempotent_replay": False,
        }
        if clean_key:
            db.put_operation_ledger(
                caller_id=caller_id,
                namespace=ns,
                operation_key=clean_key,
                operation_name="delete_memory_record",
                request_hash=request_digest,
                memory_id=memory_id,
                content_hash=str(before_row.get("content_hash") or ""),
                response_json=json.dumps(result, sort_keys=True, default=str),
                _commit=False,
            )
        if _commit:
            db.commit()
    except Exception:
        if _commit:
            db.rollback()
        raise

    return result


def compact_tombstones(
    db: MemoryDB,
    *,
    through_seq: int,
    acked_cursor_seq: int | None = None,
    namespace: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Compact persistent tombstones through ``through_seq`` when safe (#98).

    Preconditions preventing resurrection:
    1. ``through_seq`` must not exceed ``trimmed_through_seq`` (when ``strict=True``,
       otherwise clamped to ``trimmed_through_seq``), so no pre-delete
       ``memory.created`` or ``memory.updated`` events remain in the active
       replayable event window.
    2. When ``acked_cursor_seq`` is provided, ``through_seq`` must not exceed
       ``acked_cursor_seq`` (proving downstream replay consumers have advanced
       past the tombstone horizon).
    3. For every candidate tombstone with ``deleted_at_seq <= effective_seq``:
       - no live row with ``id == tombstone.memory_id`` may exist in ``memories``;
       - no retained event with ``seq <= tombstone.deleted_at_seq`` may exist in ``events``.
    4. ``store_meta['compacted_tombstones_through_seq']`` is monotonically advanced,
       and ``validate_replay_cursor`` rejects any cursor behind that watermark.
    """
    if type(through_seq) is not int or through_seq < 0:
        raise ValueError("through_seq must be a non-negative integer")
    if acked_cursor_seq is not None and (type(acked_cursor_seq) is not int or acked_cursor_seq < 0):
        raise ValueError("acked_cursor_seq must be a non-negative integer when provided")

    bounds = db.event_log_bounds()
    trimmed_through = int(bounds["trimmed_through_seq"])
    prev_compacted = int(db.get_meta("compacted_tombstones_through_seq") or 0)

    if strict and acked_cursor_seq is not None and through_seq > acked_cursor_seq:
        raise TombstoneCompactionError(
            "unacknowledged_tombstone_horizon",
            f"Cannot compact tombstones through seq={through_seq} beyond "
            f"acked_cursor_seq={acked_cursor_seq}.",
            expected=f"through_seq<={acked_cursor_seq}",
            actual=through_seq,
        )

    if strict and through_seq > trimmed_through:
        raise TombstoneCompactionError(
            "events_not_trimmed_through_tombstone",
            f"Cannot compact tombstones through seq={through_seq}: event log is only "
            f"trimmed through seq={trimmed_through}; compacting tombstones while "
            "pre-delete events remain in the replay window could resurrect deleted records.",
            expected=f"through_seq<={trimmed_through}",
            actual=through_seq,
        )

    effective_seq = through_seq
    if not strict:
        effective_seq = min(through_seq, trimmed_through)
        if acked_cursor_seq is not None:
            effective_seq = min(effective_seq, acked_cursor_seq)

    all_tombstones = db.list_tombstones(namespace=namespace)
    candidates = [t for t in all_tombstones if int(t.get("deleted_at_seq") or 0) <= effective_seq]

    for t in candidates:
        mid = str(t["memory_id"])
        t_seq = int(t.get("deleted_at_seq") or 0)
        if db.get_memory(mid) is not None:
            raise TombstoneCompactionError(
                "live_record_conflict",
                f"Cannot compact tombstone for {mid!r}: an active live record with "
                "the same id exists in memories.",
                expected="no live record",
                actual=mid,
            )
        if db.has_events_at_or_before_seq(t_seq, memory_id=mid):
            raise TombstoneCompactionError(
                "events_not_trimmed_through_tombstone",
                f"Cannot compact tombstone for {mid!r} (deleted_at_seq={t_seq}): "
                f"events at or before seq={t_seq} still exist in the event log.",
                expected=f"no retained events with seq<={t_seq}",
                actual=f"retained events for {mid!r}",
            )

    compacted_ids = [str(t["memory_id"]) for t in candidates]
    new_watermark = max(prev_compacted, effective_seq) if effective_seq > 0 else prev_compacted

    try:
        removed = db.delete_tombstones(compacted_ids, _commit=False)
        if new_watermark > prev_compacted:
            db.set_meta(
                "compacted_tombstones_through_seq",
                str(new_watermark),
                _commit=False,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    remaining = len(db.list_tombstones(namespace=namespace))
    return {
        "compacted": removed,
        "remaining": remaining,
        "compacted_through_seq": new_watermark,
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
