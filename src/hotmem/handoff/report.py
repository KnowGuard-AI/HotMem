"""Handoff coverage report records — omissions, redactions, coverage (#101).

Purpose:
    The operator-facing honesty layer of the handoff contract (handoff-v1
    §7): every unsupported, dropped, or redacted item is recorded with a
    reason and a recoverability flag — never with the omitted or redacted
    content itself.

Interface:
    omission(where, field, reason, *, recoverable) -> dict
    redaction(where, field, kind, reason, *, recoverable) -> dict
    build_coverage(...) -> the manifest coverage block

Deps: stdlib only.
Extension: record shapes are fixed by the contract; new omission reasons
    add constants here so they stay machine-stable.
"""

from __future__ import annotations

from typing import Any

# Canonical omission reasons (machine-stable; human-readable text follows).
OMISSION_UNSUPPORTED = "unsupported source field"
OMISSION_POLICY_HIDDEN = "policy: hidden prompts are never captured"
OMISSION_TRUNCATED_ENTRY = "entry exceeded the per-entry byte bound; truncated"
OMISSION_TRUNCATED_TOOL_RESULT = "tool result exceeded the tool-result byte bound; truncated"
OMISSION_SESSION_CAPPED = "session exceeded max_session_entries; remainder not transferred"
OMISSION_MEMORIES_CAPPED = "memory items exceeded max_memories; remainder not transferred"
OMISSION_RESUME_BOUNDED = "resume mode bounds the session stream; not transferred"


def omission(where: str, field: str | None, reason: str, *, recoverable: bool) -> dict[str, Any]:
    """One omission record (handoff-v1 §7): location, reason, recoverability.

    Never includes the omitted content. ``recoverable`` means the source
    still holds the item, so the operator can go back for it.
    """
    return {"where": where, "field": field, "reason": reason, "recoverable": recoverable}


def redaction(
    where: str, field: str, kind: str, reason: str, *, recoverable: bool
) -> dict[str, Any]:
    """One redaction record (handoff-v1 §7): location and secret KIND only.

    Never includes the redacted value — not here, not in the reason, not in
    any derived text.
    """
    return {
        "where": where,
        "field": field,
        "kind": kind,
        "reason": reason,
        "recoverable": recoverable,
    }


def build_coverage(
    *,
    transferred: int,
    omissions: list[dict[str, Any]],
    redactions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the manifest coverage block (handoff-v1 §7).

    ``recoverable_count`` counts omissions and redactions whose content the
    source still holds, so the operator knows exactly what a re-export
    could recover.
    """
    recoverable = sum(1 for o in omissions if o["recoverable"]) + sum(
        1 for r in redactions if r["recoverable"]
    )
    return {
        "transferred": transferred,
        "omitted": omissions,
        "redacted": redactions,
        "omitted_count": len(omissions),
        "redacted_count": len(redactions),
        "recoverable_count": recoverable,
    }
