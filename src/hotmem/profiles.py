"""HotMem hydration profiles — profile-aware content and provenance retrieval.

Purpose:
     Allow consumers to ask for different levels of memory content and
     provenance without changing existing hydrate defaults. Profiles are
     additive request parameters; the default hydrate behavior is unchanged.

Profiles:
     agent:  concise content (fact_summary or truncated fact_text), no file reads.
     compact: smallest representation — metadata only, no content, no file reads.
     audit:   content + provenance, checksums, source references, warnings.
     full:    maximum detail — inline content, file references, diagnostics.

Rules:
     - ``compact`` and ``agent`` never call ``adapter.read_range()``.
     - ``audit`` and ``full`` may read file-backed content (lazy, on demand).
     - Missing backing files surface as provenance errors (audit/full) or
       warnings (agent/compact).
     - Default hydrate (no profile) returns raw bytes — unchanged.

Interface:
     HydrationProfile = Literal["agent", "compact", "audit", "full"]
     ProfiledHydration (dataclass): memory_id, identifier, memory_type, content,
         verified, source_uri, byte_offset, byte_length, source_checksum,
         fact_summary, warnings, profile
     hydrate_with_profile(db, memory_id, *, profile="agent", base_dir=None,
         verify=True) -> ProfiledHydration

Deps: hotmem.db, hotmem.memory, hotmem.provenance, hotmem.storage, hotmem.trace
Extension: add custom profiles or profile-specific transformations here.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from hotmem.db import MemoryDB
from hotmem.memory import hydrate_memory_detailed
from hotmem.provenance import BackingFileMissingError, ProvenanceError
from hotmem.storage import get_adapter
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("profiles")

HydrationProfile = Literal["agent", "compact", "audit", "full"]

# Maximum content length for the "agent" profile (truncates large inline text).
AGENT_MAX_CONTENT = 4096


@dataclass
class ProfiledHydration:
    """Result of profile-aware hydration. Fields vary by profile.

    - ``compact``: metadata only (content=None, no file reads).
    - ``agent``: concise content (fact_summary or truncated fact_text).
    - ``audit``: content + provenance + verification state.
    - ``full``: everything — content, provenance, diagnostics, warnings.
    """

    memory_id: str
    identifier: str
    memory_type: str
    profile: str
    content: bytes | None = None
    verified: bool = False
    source_uri: str | None = None
    byte_offset: int = 0
    byte_length: int = 0
    source_checksum: str | None = None
    source_format: str | None = None
    fact_summary: str | None = None
    fact_text: str | None = None
    warnings: list[str] = field(default_factory=list)
    exists: bool | None = None  # whether the backing file currently exists

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict. ``content`` is base64-encoded if present."""
        import base64

        d: dict[str, Any] = {
            "memory_id": self.memory_id,
            "identifier": self.identifier,
            "memory_type": self.memory_type,
            "profile": self.profile,
            "verified": self.verified,
        }
        if self.content is not None:
            d["content"] = base64.b64encode(self.content).decode("ascii")
            d["content_encoding"] = "base64"
        if self.source_uri is not None:
            d["source_uri"] = self.source_uri
        if self.byte_offset:
            d["byte_offset"] = self.byte_offset
        if self.byte_length:
            d["byte_length"] = self.byte_length
        if self.source_checksum:
            d["source_checksum"] = self.source_checksum
        if self.source_format:
            d["source_format"] = self.source_format
        if self.fact_summary:
            d["fact_summary"] = self.fact_summary
        if self.fact_text:
            d["fact_text"] = self.fact_text
        if self.warnings:
            d["warnings"] = self.warnings
        if self.exists is not None:
            d["exists"] = self.exists
        return d


def hydrate_with_profile(
    db: MemoryDB,
    memory_id: str,
    *,
    profile: HydrationProfile = "agent",
    base_dir: str | None = None,
    verify: bool = True,
) -> ProfiledHydration:
    """Hydrate a memory with a profile-aware response shape.

    ``compact`` and ``agent`` perform zero file reads. ``audit`` and ``full``
    may read file-backed content (lazy, on demand). The default hydrate path
    (no profile) is unchanged — this function is only called when a profile
    is explicitly requested.
    """
    record = db.get_memory(memory_id)
    if record is None:
        raise KeyError(f"memory not found: {memory_id}")

    with Timer() as t:
        if profile == "compact":
            result = _hydrate_compact(record)
        elif profile == "agent":
            result = _hydrate_agent(record, base_dir)
        elif profile == "audit":
            result = _hydrate_audit(db, record, base_dir, verify)
        elif profile == "full":
            result = _hydrate_full(db, record, base_dir, verify)
        else:
            raise ValueError(f"unknown profile: {profile!r}")

    _trace.info(
        "hydrate_profile",
        f"hydrated {memory_id[:8]}… with profile={profile}",
        detail={
            "profile": profile,
            "memory_type": record["memory_type"],
            "ms": round(t.ms, 2),
        },
    )
    return result


def _hydrate_compact(record: dict[str, Any]) -> ProfiledHydration:
    """Metadata only — no content, no file reads."""
    warnings: list[str] = []
    exists = None

    if record["memory_type"] == "file" and record.get("source_uri"):
        # Check if backing file exists (stat only, no read).
        try:
            adapter = get_adapter(record["source_uri"])
            exists = adapter.exists(record["source_uri"])
            if not exists:
                warnings.append(f"backing file missing: {record['source_uri']}")
        except Exception:
            warnings.append(f"cannot check backing file: {record['source_uri']}")

    return ProfiledHydration(
        memory_id=record["id"],
        identifier=record["identifier"],
        memory_type=record["memory_type"],
        profile="compact",
        source_uri=record.get("source_uri") or None,
        byte_offset=record.get("byte_offset") or 0,
        byte_length=record.get("byte_length") or 0,
        source_checksum=record.get("source_checksum") or None,
        source_format=record.get("source_format") or None,
        fact_summary=record.get("fact_summary"),
        warnings=warnings,
        exists=exists,
    )


def _hydrate_agent(record: dict[str, Any], base_dir: str | None) -> ProfiledHydration:
    """Concise content — fact_summary or truncated fact_text. No file reads."""
    compact = _hydrate_compact(record)

    # For inline memories: use fact_text (truncated).
    if record["memory_type"] != "file":
        fact = record.get("fact_text") or ""
        if len(fact) > AGENT_MAX_CONTENT:
            fact = fact[:AGENT_MAX_CONTENT] + "…"
        compact.content = fact.encode()
        compact.fact_text = fact
    else:
        # For file-backed: use fact_summary if available.
        summary = record.get("fact_summary")
        if summary:
            compact.content = summary.encode()
            compact.fact_text = summary
        # No summary → no content (agent doesn't read files)

    compact.profile = "agent"
    return compact


def _hydrate_audit(
    db: MemoryDB,
    record: dict[str, Any],
    base_dir: str | None,
    verify: bool,
) -> ProfiledHydration:
    """Content + provenance + checksums + source references + warnings."""
    result = _hydrate_compact(record)
    result.profile = "audit"

    if record["memory_type"] != "file":
        # Inline: content is fact_text.
        result.content = (record.get("fact_text") or "").encode()
        result.fact_text = record.get("fact_text")
    else:
        # File-backed: actually hydrate (lazy read + verify).
        try:
            hydrated = hydrate_memory_detailed(db, record["id"], base_dir=base_dir, verify=verify)
            result.content = hydrated.content
            result.verified = hydrated.verified
            result.source_uri = hydrated.source_uri
            result.byte_offset = hydrated.byte_offset
            result.byte_length = hydrated.byte_length
            result.exists = True
        except BackingFileMissingError as err:
            result.warnings.append(f"backing file missing: {err.source_uri}")
            result.exists = False
        except ProvenanceError as err:
            result.warnings.append(f"provenance error ({err.reason}): {err.source_uri}")
            result.verified = False

    return result


def _hydrate_full(
    db: MemoryDB,
    record: dict[str, Any],
    base_dir: str | None,
    verify: bool,
) -> ProfiledHydration:
    """Maximum detail — content, provenance, diagnostics, warnings."""
    result = _hydrate_audit(db, record, base_dir, verify)
    result.profile = "full"

    # Add full fact_text for inline memories (audit already has it).
    if record["memory_type"] != "file":
        result.fact_text = record.get("fact_text")

    # Add provenance_json if present.
    provenance = record.get("provenance_json")
    if provenance:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            result.warnings.append(f"provenance: {json.loads(provenance)}")

    return result
