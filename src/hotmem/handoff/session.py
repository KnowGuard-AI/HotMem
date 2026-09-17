"""Adapter-neutral normalized session contract for handoff (#101).

Purpose:
    The shape every source adapter produces and every downstream layer
    (redaction, brief, package writer) consumes. It lives here, outside any
    concrete adapter, so a second adapter (or a test double) does not have
    to import from ``handoff.codex_source`` to participate in the pipeline.

Interface:
    NormalizedSession — adapter, adapter_version, session_id, label,
    time_range, codex_version, entries, memory_records, omissions,
    redactions (all lists default empty; the dataclass is frozen and is
    replaced, never mutated, by the redaction and resume-bounding steps).

Deps: stdlib only.
Extension: ``codex_version`` is the one adapter-specific extra today
    (null for other adapters). A second adapter should generalize it — with
    the corresponding handoff-v1 manifest key — rather than overloading it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormalizedSession:
    """The adapter output consumed by the package writer."""

    adapter: str
    adapter_version: str
    session_id: str
    label: str
    time_range: dict[str, Any]
    codex_version: str | None
    entries: list[dict[str, Any]] = field(default_factory=list)
    memory_records: list[dict[str, Any]] = field(default_factory=list)
    omissions: list[dict[str, Any]] = field(default_factory=list)
    redactions: list[dict[str, Any]] = field(default_factory=list)
