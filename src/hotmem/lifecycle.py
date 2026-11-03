"""HotMem lifecycle — promotion state and candidate signals.

Portable promotion state (`promotion_state`, `promotion_candidate`,
`updated_at`) is written through the #98 reconstructing `memory.updated`
contract so replay reconstructs terminal lifecycle state. The
`memory.promotion` / `memory.candidate` events remain observational signals and
are skipped by replay; `emit_event=False` suppresses only those signal events,
never the reconstructing state update.

Purpose:
     Add a local, explicit lifecycle state and promotion candidate signals
     without turning HotMem into a policy engine. HotMem stores state,
     transition metadata, and candidate signals, and emits promotion events
     through the local event log. External systems may own policy—whether,
     when, and where promotion happens—and HotMem performs no automatic remote
     migration, deletion, scheduling, approvals, or hierarchy protocol.

State model:
     HOT -> READY -> PROMOTED -> ARCHIVED

     Transitions are strict forward-only linear. Any other transition
     (including ARCHIVED -> HOT reheat, and same-state "transitions") raises
     ``InvalidTransitionError`` and does not mutate state. A caller may add a
     new memory at HOT if it needs to revive an archived one — HotMem will not
     rewind state on an existing record.

Default behavior:
     Existing memory records default to ``promotion_state="HOT"`` and
     ``promotion_candidate=0`` (already in the v2 schema). No caller must
     supply lifecycle fields; inline, file-backed, JSONL, and bundle memory
     workflows remain compatible without changes.

Interface:
     PROMOTION_STATES, VALID_TRANSITIONS
     InvalidTransitionError(memory_id, from_state, to_state)
     transition(db, memory_id, to_state, *, reason=None, actor=None, emit_event=True) -> dict
     mark_candidate(db, memory_id, candidate, *, reason=None, emit_event=True) -> dict
     (emit_event controls only the observational signal event; both functions
      always emit a reconstructing memory.updated for portable state.)
     list_candidates(db, *, namespace=None, state=None) -> list[dict]
     list_by_state(db, state, *, namespace=None) -> list[dict]

Deps: hotmem.db, hotmem.events, hotmem.trace
Extension: do NOT add policy, scheduling, approvals, or remote storage here.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from hotmem.db import MemoryDB
from hotmem.events import EventType, update_memory_record
from hotmem.trace import get_tracer

_trace = get_tracer("lifecycle")

PROMOTION_STATES: tuple[str, ...] = ("HOT", "READY", "PROMOTED", "ARCHIVED")

VALID_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("HOT", "READY"),
        ("READY", "PROMOTED"),
        ("PROMOTED", "ARCHIVED"),
    }
)


class InvalidTransitionError(Exception):
    """Raised when a requested promotion transition is not in VALID_TRANSITIONS."""

    def __init__(self, memory_id: str, from_state: str, to_state: str) -> None:
        self.memory_id = memory_id
        self.from_state = from_state
        self.to_state = to_state
        valid = sorted(f"{a}->{b}" for (a, b) in VALID_TRANSITIONS)
        super().__init__(
            f"invalid promotion transition for memory {memory_id}: "
            f"{from_state} -> {to_state}; valid transitions: {valid}"
        )

    def to_dict(self) -> dict[str, Any]:
        valid_targets = [b for (a, b) in VALID_TRANSITIONS if a == self.from_state]
        return {
            "error": "invalid_transition",
            "memory_id": self.memory_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "valid": valid_targets,
            "message": str(self),
        }


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def transition(
    db: MemoryDB,
    memory_id: str,
    to_state: str,
    *,
    reason: str | None = None,
    actor: str | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Validate and apply one forward-only promotion transition.

    Reads current state via ``get_memory`` (pure DB, no file hydration),
    validates against ``VALID_TRANSITIONS``, raises ``InvalidTransitionError``
    on any deviation, persists the new state + ``updated_at`` as a
    reconstructing ``memory.updated`` event, and (unless ``emit_event`` is
    False) appends a ``memory.promotion`` observational signal.

    Returns a dict with the resulting ``promotion_state`` and ``updated_at``.

    Raises:
        KeyError: memory not found.
        ValueError: ``to_state`` is not a known state.
        InvalidTransitionError: the transition is not valid.
    """
    if to_state not in PROMOTION_STATES:
        raise ValueError(f"unknown promotion state {to_state!r}; known: {list(PROMOTION_STATES)}")

    record = db.get_memory(memory_id)
    if record is None:
        raise KeyError(f"memory not found: {memory_id}")

    from_state = record.get("promotion_state") or "HOT"

    if (from_state, to_state) not in VALID_TRANSITIONS:
        raise InvalidTransitionError(memory_id, from_state, to_state)

    updated_at = _utc_now_iso()

    signal_payload: dict[str, Any] = {"from": from_state, "to": to_state}
    if reason is not None:
        signal_payload["reason"] = reason
    if actor is not None:
        signal_payload["actor"] = actor

    # Promotion changes portable state, so it must also emit a reconstructing
    # memory.updated alongside the observational promotion signal. The update
    # event carries the complete after-state and is written in the same
    # transaction as the row mutation, watermark, and signal.
    update_memory_record(
        db,
        memory_id,
        promotion_state=to_state,
        updated_at=updated_at,
        occurred_at=updated_at,
        reason=reason,
        signal_event_type=EventType.MEMORY_PROMOTION if emit_event else None,
        signal_payload=signal_payload,
    )

    _trace.info(
        "transition",
        f"{memory_id[:8]}… {from_state} -> {to_state}",
        detail={"memory_id": memory_id, "from": from_state, "to": to_state, "reason": reason},
    )
    return {
        "memory_id": memory_id,
        "promotion_state": to_state,
        "updated_at": updated_at,
    }


def mark_candidate(
    db: MemoryDB,
    memory_id: str,
    candidate: bool,
    *,
    reason: str | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Set the ``promotion_candidate`` flag on a memory, independent of state.

    The candidate signal is decoupled from the state machine: it may be set on
    a memory in any state, and it persists across transitions. Persists a
    reconstructing ``memory.updated`` event and records a ``memory.candidate``
    observational signal unless ``emit_event`` is False.

    Raises:
        KeyError: memory not found.
    """
    record = db.get_memory(memory_id)
    if record is None:
        raise KeyError(f"memory not found: {memory_id}")

    signal_payload: dict[str, Any] = {"candidate": bool(candidate)}
    if reason is not None:
        signal_payload["reason"] = reason

    update_memory_record(
        db,
        memory_id,
        promotion_candidate=1 if candidate else 0,
        updated_at=record.get("updated_at"),
        reason=reason,
        signal_event_type=EventType.MEMORY_CANDIDATE if emit_event else None,
        signal_payload=signal_payload,
    )

    _trace.info(
        "candidate",
        f"{memory_id[:8]}… candidate={bool(candidate)}",
        detail={"memory_id": memory_id, "candidate": bool(candidate), "reason": reason},
    )
    return {
        "memory_id": memory_id,
        "promotion_candidate": 1 if candidate else 0,
    }


def list_candidates(
    db: MemoryDB,
    *,
    namespace: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Return memories flagged as promotion candidates, optionally filtered.

    Pure DB read — no file hydration. ``state`` filters on promotion_state.
    """
    return db.list_promotion_candidates(namespace=namespace, state=state)


def list_by_state(
    db: MemoryDB,
    state: str,
    *,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Return memories in a given promotion_state, optionally by namespace.

    Pure DB read — no file hydration.
    """
    return db.list_by_promotion_state(state, namespace=namespace)


def states_model() -> dict[str, Any]:
    """Return the static lifecycle model for documentation endpoints."""
    return {
        "states": list(PROMOTION_STATES),
        "valid_transitions": [list(t) for t in sorted(VALID_TRANSITIONS)],
    }
