"""Bounded deterministic resume brief for handoff packages (#101).

Purpose:
    Render the target-friendly summary a fresh Claude session reads to
    continue the work (handoff-v1 §6): goal, decisions, commitments,
    unresolved questions, next actions, historical tool activity (marked
    "do not re-run"), file references, and the durable-memory index — every
    item linked back to its source entry id, never duplicating the full
    stream.

    Deterministic: the same normalized session always yields byte-identical
    brief text (fixed section order, seq-ordered items, no clocks). Bounded:
    the text respects the char budget by dropping the least-important items
    first (files, then tools, then the oldest item of each typed section),
    and never crashes. The layer-3 redaction gate re-scans the rendered
    text before it leaves this module.

Interface:
    build_brief(session, *, budget) -> BriefDocument
    BriefDocument.text / .sections / .links / .char_count / .to_dict()
    TOOL_HISTORY_MARKER — the explicit no-re-run marker (acceptance 10)

Deps: hotmem.handoff (kinds), hotmem.handoff.session (NormalizedSession
    shape), hotmem.handoff.redact (output gate).
Extension: section set and trim order are contract-visible; change them only
    with a golden-fixture update.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from hotmem.handoff import DEFAULT_LIMITS, EntryKind
from hotmem.handoff.redact import assert_no_secrets
from hotmem.handoff.session import NormalizedSession
from hotmem.trace import get_tracer

_trace = get_tracer("handoff.brief")

TOOL_HISTORY_MARKER = "Historical tool activity — do not re-run"

_GOAL_MAX_CHARS = 1000
_ITEM_MAX_CHARS = 400
_PER_SECTION_ITEMS = 10
_MEMORY_ITEMS = 20

_SECTION_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("decisions", (EntryKind.DECISION,), "## Decisions"),
    ("commitments", (EntryKind.COMMITMENT,), "## Commitments"),
    ("unresolved_questions", (EntryKind.UNRESOLVED_QUESTION,), "## Unresolved questions"),
    ("next_actions", (EntryKind.NEXT_ACTION,), "## Next actions"),
    ("tool_activity", (EntryKind.TOOL_CALL, EntryKind.TOOL_RESULT), f"## {TOOL_HISTORY_MARKER}"),
    ("file_references", (EntryKind.FILE_REFERENCE,), "## Files"),
)

# Trim order when the budget is exceeded: files drop first, then tools,
# then the oldest item of each typed section (most recent wins).
_TRIM_ORDER = (
    "file_references",
    "tool_activity",
    "decisions",
    "commitments",
    "unresolved_questions",
    "next_actions",
)


@dataclass(frozen=True)
class BriefDocument:
    """The serialized resume-brief.json payload (handoff-v1 §6)."""

    text: str
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    links: dict[str, Any] = field(default_factory=dict)
    char_count: int = 0
    omissions_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _item(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_entry_id": entry["source"]["source_entry_id"],
        "kind": entry["kind"],
        "text": _clip(str(entry.get("text") or ""), _ITEM_MAX_CHARS),
    }


def _collect(session: NormalizedSession) -> tuple[dict[str, list[dict[str, Any]]], str]:
    """Collect goal + section items in deterministic (seq) order."""
    sections: dict[str, list[dict[str, Any]]] = {}
    goal = ""
    for entry in session.entries:  # already seq-ordered
        if not goal and entry["kind"] == EntryKind.TURN and entry.get("role") == "user":
            goal = _clip(str(entry.get("text") or ""), _GOAL_MAX_CHARS)
    for name, kinds, _header in _SECTION_SPECS:
        items = [_item(e) for e in session.entries if e["kind"] in kinds]
        sections[name] = items[-_PER_SECTION_ITEMS:]
    return sections, goal


def _render(
    session: NormalizedSession, sections: dict[str, list[dict[str, Any]]], goal: str
) -> str:
    lines: list[str] = [f"# Resume brief — {session.label}", ""]
    lines.append(
        f"Session `{session.session_id}` "
        f"({session.time_range.get('started_at')} → {session.time_range.get('ended_at')}) "
        f"captured from {session.adapter} v{session.adapter_version}."
    )
    lines.append("")
    lines.append("## Goal")
    lines.append(goal or "(no user turn captured)")
    for name, _kinds, header in _SECTION_SPECS:
        items = sections.get(name) or []
        if not items:
            continue
        lines.append("")
        lines.append(header)
        for item in items:
            lines.append(f"- {item['text']} [source: {item['source_entry_id']}]")
    memories = session.memory_records[-_MEMORY_ITEMS:]
    if memories:
        lines.append("")
        lines.append("## Durable memories")
        for record in memories:
            lines.append(f"- {record['identifier']} [memory: {record['id'][:12]}]")
    omitted = len(session.omissions)
    redacted = len(session.redactions)
    if omitted or redacted:
        lines.append("")
        lines.append(
            f"Coverage: {len(session.entries)} entries transferred; "
            f"{omitted} omission(s), {redacted} redaction(s) — see the package "
            "coverage report."
        )
    return "\n".join(lines) + "\n"


def _trim_to_budget(
    session: NormalizedSession,
    sections: dict[str, list[dict[str, Any]]],
    goal: str,
    budget: int,
) -> str:
    """Drop the least-important items until the brief fits the budget."""
    text = _render(session, sections, goal)
    for section_name in _TRIM_ORDER:
        while len(text) > budget and sections.get(section_name):
            sections[section_name] = sections[section_name][1:]
            text = _render(session, sections, goal)
    if len(text) > budget:
        # Hard floor: clip to the budget with an explicit marker; the
        # marker + newline keep the result exactly within the budget.
        text = text[: max(budget - 2, 0)].rstrip() + "…\n"
    return text


def build_brief(
    session: NormalizedSession,
    *,
    budget: int = DEFAULT_LIMITS.brief_char_budget,
) -> BriefDocument:
    """Build the bounded, deterministic resume brief (handoff-v1 §6).

    The layer-3 redaction gate re-scans the rendered text, so a redacted
    value can never reach the package even if a future section renderer
    reintroduced one.
    """
    sections, goal = _collect(session)
    text = _trim_to_budget(session, sections, goal, budget)
    assert_no_secrets(text, where="resume brief")

    linked_ids = [item["source_entry_id"] for items in sections.values() for item in items]
    links = {
        "source_entry_ids": linked_ids,
        "memory_ids": [r["id"] for r in session.memory_records],
    }
    omissions_note = (
        f"{len(session.omissions)} omission(s), {len(session.redactions)} redaction(s)"
        if session.omissions or session.redactions
        else None
    )
    _trace.info(
        "brief",
        f"built brief for {session.session_id}: {len(text)} chars, "
        f"{sum(len(v) for v in sections.values())} linked items",
        detail={"session_id": session.session_id, "budget": budget},
    )
    return BriefDocument(
        text=text,
        sections=sections,
        links=links,
        char_count=len(text),
        omissions_note=omissions_note,
    )
