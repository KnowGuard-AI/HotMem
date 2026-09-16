"""Codex export source adapter — explicit selection, consent, normalization (#101).

Purpose:
    Read a documented local ``codex-export-v1`` directory (envelope +
    ordered JSONL session log; see tests/fixtures/codex_export/README.md and
    handoff-v1 §12) and normalize it into the handoff representation:
    ordered typed entries, deterministic durable-memory records, and
    machine-readable omission records for everything unsupported or
    policy-denied.

    Safety envelope: consent is required and checked BEFORE any session
    content is read; hidden prompts and credential-bearing source fields are
    never read (policy omissions); every bound produces an omission record,
    never a crash; malformed sources fail closed with actionable errors.

Interface:
    read_codex_export(path, *, session_id=None, consent=None, limits)
        -> NormalizedSession
    SourceError / ConsentError / SelectionError / ExportFormatError /
        MalformedSourceError — fail-closed diagnostics (location, never
        content)

Deps: stdlib + hotmem.handoff (ids/limits) + hotmem.interchange.canonical
    (content hashes) + hotmem.trace.
Extension: additional source adapters implement the same read contract;
    package/verify/hydrate layers are adapter-agnostic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotmem.handoff import (
    DEFAULT_LIMITS,
    SOURCE_ADAPTER_ID,
    SOURCE_ADAPTER_VERSION,
    EntryKind,
    Limits,
    entry_id_for,
)
from hotmem.handoff.report import (
    OMISSION_MEMORIES_CAPPED,
    OMISSION_POLICY_HIDDEN,
    OMISSION_SESSION_CAPPED,
    OMISSION_TRUNCATED_ENTRY,
    OMISSION_TRUNCATED_TOOL_RESULT,
    OMISSION_UNSUPPORTED,
    omission,
)
from hotmem.interchange.canonical import compute_content_hash
from hotmem.trace import get_tracer

_trace = get_tracer("handoff.codex_source")

ENVELOPE_NAME = "export.json"
STREAM_NAME = "session.jsonl"
EXPORT_FORMAT_ID = "codex-export-v1"

_TRUNCATION_MARKER = "…[truncated]"

# Line types that map to the durable-memory stream instead of session entries.
_MEMORY_TYPES = frozenset({"memory_item"})
# Line types never read (handoff-v1 §7 layer-1 deny list).
_POLICY_DENIED_TYPES = frozenset({"hidden_prompt"})

# Supported raw fields per type — everything else becomes an omission record.
_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "turn": ("id", "seq", "type", "role", "text", "timestamp"),
    "decision": ("id", "seq", "type", "text", "timestamp"),
    "commitment": ("id", "seq", "type", "text", "timestamp"),
    "unresolved_question": ("id", "seq", "type", "text", "timestamp"),
    "next_action": ("id", "seq", "type", "text", "timestamp"),
    "tool_call": ("id", "seq", "type", "tool", "args_summary", "timestamp"),
    "tool_result": ("id", "seq", "type", "tool", "status", "result_summary", "timestamp"),
    "file_reference": ("id", "seq", "type", "path", "note", "timestamp"),
    "memory_item": ("id", "seq", "type", "identifier", "fact", "importance", "tags", "timestamp"),
    # hidden_prompt intentionally absent: its fields are never read.
}

# Fields that must be present and non-empty per type (fail-closed).
_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "turn": ("role", "text"),
    "decision": ("text",),
    "commitment": ("text",),
    "unresolved_question": ("text",),
    "next_action": ("text",),
    "tool_call": ("tool", "args_summary"),
    "tool_result": ("result_summary",),
    "file_reference": ("path",),
    "memory_item": ("identifier", "fact"),
}


class SourceError(Exception):
    """Base fail-closed source error. Carries locations, never content."""


class ConsentError(SourceError):
    """Raised when explicit consent is absent (before any content read)."""


class SelectionError(SourceError):
    """Raised when the requested session does not match the export."""


class ExportFormatError(SourceError):
    """Raised when the export envelope is missing or not codex-export-v1."""


class MalformedSourceError(SourceError):
    """Raised on structurally invalid source lines (with line numbers)."""


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


def _require_consent(consent: str | None) -> str:
    """Consent must be an explicit non-empty statement (handoff-v1 §9)."""
    if not isinstance(consent, str) or not consent.strip():
        raise ConsentError(
            "explicit consent is required before any session content is read; "
            "pass a non-empty consent statement (CLI: --consent, MCP/API: consent)"
        )
    return consent.strip()


def _load_envelope(root: Path) -> dict[str, Any]:
    envelope_path = root / ENVELOPE_NAME
    if not envelope_path.is_file():
        raise ExportFormatError(f"not a codex-export directory (missing {ENVELOPE_NAME}): {root}")
    try:
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ExportFormatError(f"malformed {ENVELOPE_NAME}: {err}") from err
    if not isinstance(envelope, dict) or envelope.get("format") != EXPORT_FORMAT_ID:
        actual = envelope.get("format") if isinstance(envelope, dict) else None
        raise ExportFormatError(
            f"unsupported export format (expected {EXPORT_FORMAT_ID}, got {actual!r})"
        )
    session = envelope.get("session")
    if not isinstance(session, dict) or not session.get("id"):
        raise ExportFormatError(f"{ENVELOPE_NAME} is missing the session block")
    return envelope


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate to ``max_bytes`` on a UTF-8 boundary with a visible marker."""
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode(errors="ignore") + _TRUNCATION_MARKER


def _record_unsupported_fields(
    source_id: str, raw: dict[str, Any], supported: tuple[str, ...], omissions: list[dict[str, Any]]
) -> None:
    """List — never silently drop — unsupported fields (handoff-v1 §7)."""
    for name in sorted(raw):
        if name not in supported:
            omissions.append(omission(source_id, name, OMISSION_UNSUPPORTED, recoverable=True))


def _source_block(raw: dict[str, Any], session_id: str) -> dict[str, Any]:
    """The provenance link back to the source line (handoff-v1 §4)."""
    return {
        "adapter": SOURCE_ADAPTER_ID,
        "adapter_version": SOURCE_ADAPTER_VERSION,
        "session_id": session_id,
        "source_entry_id": str(raw.get("id", "")),
        "source_seq": int(raw.get("seq", 0)),
    }


def _build_entry(raw: dict[str, Any], line_type: str, session_id: str) -> dict[str, Any]:
    """Map one supported source line to its normalized session entry."""
    seq = int(raw["seq"])
    text = str(raw.get("text") or "")
    entry: dict[str, Any] = {
        "schema_version": 1,
        "id": entry_id_for(SOURCE_ADAPTER_ID, session_id, str(raw["id"])),
        "seq": seq,
        "kind": _KIND_BY_TYPE[line_type],
        "text": text,
        "source": _source_block(raw, session_id),
    }
    if line_type == "turn":
        entry["role"] = str(raw.get("role") or "")
    if line_type == "tool_call":
        tool = str(raw.get("tool") or "")
        args = str(raw.get("args_summary") or "")
        entry["text"] = f"{tool} {args}".strip()
        entry["summary"] = tool
        entry["executable"] = False
    if line_type == "tool_result":
        entry["text"] = str(raw.get("result_summary") or "")
        entry["summary"] = f"{raw.get('tool', '')}: {raw.get('status', '')}".strip(": ")
        entry["executable"] = False
    if line_type == "file_reference":
        note = str(raw.get("note") or "")
        entry["artifacts"] = [{"path": str(raw.get("path") or ""), "note": note}]
        entry["text"] = note or str(raw.get("path") or "")
    if raw.get("timestamp"):
        entry["created_at"] = str(raw["timestamp"])
    return entry


_KIND_BY_TYPE = {
    "turn": EntryKind.TURN,
    "decision": EntryKind.DECISION,
    "commitment": EntryKind.COMMITMENT,
    "unresolved_question": EntryKind.UNRESOLVED_QUESTION,
    "next_action": EntryKind.NEXT_ACTION,
    "tool_call": EntryKind.TOOL_CALL,
    "tool_result": EntryKind.TOOL_RESULT,
    "file_reference": EntryKind.FILE_REFERENCE,
}


def _build_memory_record(
    raw: dict[str, Any], session_id: str, time_range: dict[str, Any]
) -> dict[str, Any]:
    """Map one memory_item line to a deterministic interchange record."""
    identifier = str(raw.get("identifier") or "")
    fact = str(raw.get("fact") or "")
    content_hash = compute_content_hash(identifier, fact)
    record: dict[str, Any] = {
        "schema_version": 1,
        "id": hashlib.sha256(
            f"hotmem-handoff-memory:{SOURCE_ADAPTER_ID}:{session_id}:"
            f"{raw.get('id')}:{content_hash}".encode()
        ).hexdigest(),
        "identifier": identifier,
        "memory_type": "fact",
        "fact_text": fact,
        "source": f"handoff:{SOURCE_ADAPTER_ID}",
        "importance": float(raw.get("importance", 0.5)),
        "metadata": None,
        "content_hash": content_hash,
        "tags": [str(t) for t in raw["tags"]] if isinstance(raw.get("tags"), list) else [],
        "provenance": {
            "handoff": {
                "adapter": SOURCE_ADAPTER_ID,
                "adapter_version": SOURCE_ADAPTER_VERSION,
                "session_id": session_id,
                "source_entry_id": str(raw.get("id", "")),
                "source_seq": int(raw.get("seq", 0)),
                "session_time_range": time_range,
            }
        },
    }
    if raw.get("timestamp"):
        record["created_at"] = str(raw["timestamp"])
    return record


def read_codex_export(
    path: str | Path,
    *,
    session_id: str | None = None,
    consent: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> NormalizedSession:
    """Read and normalize one codex-export-v1 session (#101).

    Fail-closed order: consent first, then the envelope, then the requested
    session match — all before any session content is read. Lines map to
    ordered typed entries and deterministic interchange memory records;
    unsupported fields, policy-denied lines, and bound exceedances become
    omission records. Malformed structure raises MalformedSourceError with
    the offending line number.
    """
    _require_consent(consent)
    root = Path(path)
    envelope = _load_envelope(root)
    session = envelope["session"]

    if session_id is not None and session_id != session["id"]:
        raise SelectionError(f"export holds session {session['id']!r}, requested {session_id!r}")

    stream_path = root / STREAM_NAME
    if not stream_path.is_file():
        raise MalformedSourceError(f"missing {STREAM_NAME} in export: {root}")

    time_range = {
        "started_at": session.get("started_at"),
        "ended_at": session.get("ended_at"),
    }
    result = NormalizedSession(
        adapter=SOURCE_ADAPTER_ID,
        adapter_version=SOURCE_ADAPTER_VERSION,
        session_id=str(session["id"]),
        label=str(session.get("label") or session["id"]),
        time_range=time_range,
        codex_version=str(session.get("codex_version")) or None,
    )

    seen_ids: set[str] = set()
    last_seq = 0
    transferred = 0
    session_cap_recorded = False

    with open(stream_path, encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as err:
                raise MalformedSourceError(
                    f"{STREAM_NAME}:{line_no}: malformed JSON: {err.msg}"
                ) from err
            if not isinstance(raw, dict):
                raise MalformedSourceError(f"{STREAM_NAME}:{line_no}: not a JSON object")

            source_id = str(raw.get("id") or "")
            line_type = str(raw.get("type") or "")
            seq = raw.get("seq")
            if not source_id or not line_type or not isinstance(seq, int):
                raise MalformedSourceError(
                    f"{STREAM_NAME}:{line_no}: id, type, and integer seq are required"
                )
            if source_id in seen_ids:
                raise MalformedSourceError(
                    f"{STREAM_NAME}:{line_no}: duplicate source id {source_id!r}"
                )
            if seq <= last_seq:
                raise MalformedSourceError(
                    f"{STREAM_NAME}:{line_no}: seq {seq} does not increase past {last_seq}"
                )
            seen_ids.add(source_id)
            last_seq = seq

            required = _REQUIRED_BY_TYPE.get(line_type)
            if required and not all(str(raw.get(name) or "").strip() for name in required):
                missing = ", ".join(
                    name for name in required if not str(raw.get(name) or "").strip()
                )
                raise MalformedSourceError(
                    f"{STREAM_NAME}:{line_no}: {line_type} requires non-empty {missing}"
                )

            # Layer-1 deny list: never read the content, record the policy.
            if line_type in _POLICY_DENIED_TYPES:
                result.omissions.append(
                    omission(source_id, None, OMISSION_POLICY_HIDDEN, recoverable=False)
                )
                continue

            if line_type in _MEMORY_TYPES:
                if len(result.memory_records) >= limits.max_memories:
                    result.omissions.append(
                        omission(
                            source_id, "memory_item", OMISSION_MEMORIES_CAPPED, recoverable=True
                        )
                    )
                    _record_unsupported_fields(
                        source_id, raw, _FIELD_MAP[line_type], result.omissions
                    )
                    continue
                record = _build_memory_record(raw, result.session_id, time_range)
                # Bound the memory fact like any other entry text.
                if len(record["fact_text"].encode()) > limits.max_entry_bytes:
                    record["fact_text"] = _truncate(record["fact_text"], limits.max_entry_bytes)
                    result.omissions.append(
                        omission(source_id, "fact", OMISSION_TRUNCATED_ENTRY, recoverable=True)
                    )
                result.memory_records.append(record)
                continue

            if line_type not in _KIND_BY_TYPE:
                result.omissions.append(
                    omission(
                        source_id,
                        "type",
                        f"unknown source line type {line_type!r} is not transferred",
                        recoverable=True,
                    )
                )
                continue

            if transferred >= limits.max_session_entries:
                if not session_cap_recorded:
                    result.omissions.append(
                        omission("session", None, OMISSION_SESSION_CAPPED, recoverable=True)
                    )
                    session_cap_recorded = True
                continue

            entry = _build_entry(raw, line_type, result.session_id)
            # Bounds: entry text and tool results truncate with omission records.
            if len(entry["text"].encode()) > limits.max_entry_bytes:
                entry["text"] = _truncate(entry["text"], limits.max_entry_bytes)
                result.omissions.append(
                    omission(source_id, "text", OMISSION_TRUNCATED_ENTRY, recoverable=True)
                )
            if entry["kind"] == EntryKind.TOOL_RESULT and len(entry["text"].encode()) > (
                limits.max_tool_result_bytes
            ):
                entry["text"] = _truncate(entry["text"], limits.max_tool_result_bytes)
                result.omissions.append(
                    omission(source_id, "text", OMISSION_TRUNCATED_TOOL_RESULT, recoverable=True)
                )
            _record_unsupported_fields(source_id, raw, _FIELD_MAP[line_type], result.omissions)
            result.entries.append(entry)
            transferred += 1

    _trace.info(
        "codex_source",
        f"normalized session {result.session_id}: "
        f"{len(result.entries)} entries, {len(result.memory_records)} memories, "
        f"{len(result.omissions)} omissions",
        detail={"root": str(root), "session_id": result.session_id},
    )
    return result
