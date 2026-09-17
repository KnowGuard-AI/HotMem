"""HotMem session handoff — verified continuity with preserved context (#101).

Purpose:
    One canonical handoff contract for moving useful working context from a
    source agent session (Codex) through HotMem into a target session (Claude):
    a versioned package holding an ordered session stream, a bounded resume
    brief, the selected durable memories in the existing interchange format,
    and explicit coverage/omission/redaction records. The product promise is
    verified continuity with preserved context — never native transcript
    cloning. Everything unsupported, redacted, or provider-specific is visible
    to the operator.

    The normative contract lives in docs/okf/handoff-v1.md; this module is
    its executable surface. Session history and durable memories stay
    distinct: the ordered stream lives in the package, the memories and one
    resume-brief record hydrate into the target database, and nothing
    overloads the interchange memory-record semantics.

Interface:
    FORMAT_ID / SCHEMA_VERSION / package file names / MODES
    EntryKind constants + ENTRY_KINDS
    Limits / DEFAULT_LIMITS — the bounded package policy
    entry_content_hash(entry) -> per-entry canonical content hash
    entry_id_for(adapter, session_id, source_entry_id) -> stable entry id
    package_id_for(entry_hashes) -> content-derived package identity

Deps: stdlib + hotmem.interchange.canonical (byte-stable serialization and
    digest primitives — the same source of truth as interchange-v1).
Extension: source adapters live in handoff.codex_source; redaction in
    handoff.redact; the brief in handoff.brief; the package writer in
    handoff.package; verification/inspection in handoff.verify; hydration in
    handoff.hydrate. CLI/MCP/HTTP surfaces stay thin wrappers over these.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from hotmem.interchange.canonical import canonical_dumps, sha256_bytes

FORMAT_ID = "hotmem-handoff-v1"
SCHEMA_VERSION = 1

MANIFEST_NAME = "manifest.json"
SESSION_STREAM_NAME = "session.jsonl"
MEMORIES_NAME = "memories.jsonl"
BRIEF_NAME = "resume-brief.json"

MODE_RESUME = "resume"
MODE_ARCHIVE = "archive"
MODES = (MODE_RESUME, MODE_ARCHIVE)

SOURCE_ADAPTER_ID = "codex-export"
SOURCE_ADAPTER_VERSION = "1"
TARGET_ADAPTER_ID = "hotmem"


class EntryKind:
    """Canonical session-stream entry kinds (handoff-v1 §4)."""

    TURN = "turn"
    DECISION = "decision"
    COMMITMENT = "commitment"
    UNRESOLVED_QUESTION = "unresolved_question"
    NEXT_ACTION = "next_action"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_REFERENCE = "file_reference"


ENTRY_KINDS = frozenset(
    {
        EntryKind.TURN,
        EntryKind.DECISION,
        EntryKind.COMMITMENT,
        EntryKind.UNRESOLVED_QUESTION,
        EntryKind.NEXT_ACTION,
        EntryKind.TOOL_CALL,
        EntryKind.TOOL_RESULT,
        EntryKind.FILE_REFERENCE,
    }
)


@dataclass(frozen=True)
class Limits:
    """Bounded package policy (handoff-v1 §4.3).

    Bounds are enforced with omission records — never a crash. Entries over
    the per-entry byte cap are truncated-and-recorded; a session beyond the
    entry cap is bounded in resume mode and recorded in coverage.
    """

    max_session_entries: int = 5000
    max_entry_bytes: int = 64 * 1024
    max_tool_result_bytes: int = 16 * 1024
    brief_char_budget: int = 6000
    max_memories: int = 200
    max_consent_chars: int = 512


DEFAULT_LIMITS = Limits()


def entry_content_hash(entry: dict[str, Any]) -> str:
    """SHA-256 over the entry's canonical serialization — per-entry identity.

    The entry's canonical JSON form is the hash preimage, so identity is
    independent of field order or transport compression.
    """
    return sha256_bytes(canonical_dumps(entry).encode())


def entry_id_for(adapter: str, session_id: str, source_entry_id: str) -> str:
    """Stable, deterministic entry id derived from the source identity.

    The same source line always maps to the same package entry id, so links
    from the resume brief and hydrated memories back to source entries stay
    stable across re-prepares (handoff-v1 §4.2).
    """
    return hashlib.sha256(
        f"hotmem-handoff-entry:{adapter}:{session_id}:{source_entry_id}".encode()
    ).hexdigest()


def package_id_for(entry_hashes: list[str]) -> str:
    """Content-derived package identity over the session stream (§3).

    Mirrors the interchange ``logical_id`` algorithm: SHA-256 over the sorted
    content-hash concatenation — equal for equivalent packages regardless of
    export time, entry field order, or the per-prepare ``handoff_id``.
    """
    return sha256_bytes("".join(sorted(entry_hashes)).encode())
