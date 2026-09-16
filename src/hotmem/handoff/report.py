"""Handoff coverage report records — omissions, redactions, coverage (#101).

Purpose:
    The operator-facing honesty layer of the handoff contract (handoff-v1
    §7): every unsupported, dropped, or redacted item is recorded with a
    reason and a recoverability flag — never with the omitted or redacted
    content itself.

Interface:
    omission(where, field, reason, recoverable) -> dict
    redaction(where, field, kind, reason, recoverable) -> dict  (commit 4)
    CoverageReport / build_coverage(...)                        (commit 4)
    OMISSION_POLICY_HIDDEN, OMISSION_UNSUPPORTED, ... — canonical reasons

Deps: stdlib only.
Extension: the redaction engine and coverage assembly land with the
    redaction commit; record shapes are fixed here so adapters emit them
    from day one.
"""

from __future__ import annotations

from typing import Any

# Canonical omission reasons (machine-stable; human-readable text follows).
OMISSION_UNSUPPORTED = "unsupported source field"
OMISSION_POLICY_HIDDEN = "policy: hidden prompts are never captured"
OMISSION_POLICY_CREDENTIAL = "policy: credential-bearing source field is never read"
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
