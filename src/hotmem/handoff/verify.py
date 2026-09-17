"""Fail-closed handoff package verification and read-only inspection (#101).

Purpose:
    Complete verification of a hotmem-handoff-v1 package BEFORE any target
    write (handoff-v1 §10): manifest integrity, path confinement, per-file
    checksums, record counts, entry schema, duplicate ids, interchange
    validity of memories, coverage arithmetic, provenance consistency,
    content-derived package_id recomputation, and the layer-3 secret gate
    over transferred text. Every failure is structured (reason, file,
    expected, actual) and leaves the target untouched.

Interface:
    HandoffError(reason, file=, expected=, actual=) — structured diagnostics
    verify_handoff(pkg_dir) -> VerifiedHandoff (stream/memories/brief access)
    inspect_handoff(pkg_dir) -> HandoffReport (read-only; works on invalid
        packages and reports the failure reason — acceptance criterion 11)

Deps: hotmem.handoff (constants, ids), hotmem.interchange.canonical,
    hotmem.interchange.paths (confinement), hotmem.interchange.record
    (memory validation), hotmem.handoff.redact (secret gate).
Extension: hydration consumes VerifiedHandoff; CLI/MCP/HTTP surfaces map
    HandoffError to exit codes and 409 diagnostics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hotmem.handoff import (
    BRIEF_NAME,
    ENTRY_KINDS,
    FORMAT_ID,
    MANIFEST_NAME,
    MEMORIES_NAME,
    MODES,
    SCHEMA_VERSION,
    SESSION_STREAM_NAME,
    entry_content_hash,
    entry_id_for,
    package_id_for,
)
from hotmem.handoff.redact import assert_no_secrets
from hotmem.interchange.canonical import sha256_file
from hotmem.interchange.paths import confined_relpath
from hotmem.interchange.record import normalize_record, validate_record
from hotmem.trace import get_tracer

_trace = get_tracer("handoff.verify")

_REQUIRED_FILES = (SESSION_STREAM_NAME, MEMORIES_NAME)


class HandoffError(Exception):
    """Structured package verification failure (maps to exit codes / 409)."""

    def __init__(
        self,
        reason: str,
        *,
        file: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        self.reason = reason
        self.file = file
        self.expected = expected
        self.actual = actual
        message = f"{reason}: {file or 'package'}"
        if expected:
            message += f" (expected {expected}, got {actual})"
        super().__init__(message)


def _describe(value: Any) -> str:
    """Short type/value description for diagnostics (never full payloads)."""
    text = f"{type(value).__name__} {value!r}"
    return text if len(text) <= 40 else text[:37] + "..."


def _field_error(field: str, value: Any, expected: str) -> HandoffError:
    """Structured error for a malformed manifest field shape.

    Type confusion in the manifest must fail closed like any other
    corruption, not surface as AttributeError/ValueError/TypeError — the
    surfaces only map ``HandoffError`` to their documented responses
    (409 bodies, MCP error results, CLI diagnostics).
    """
    return HandoffError(
        "invalid_manifest_field",
        file=MANIFEST_NAME,
        expected=f"{field} {expected}",
        actual=_describe(value),
    )


def _object_field(container: dict[str, Any], field: str) -> dict[str, Any]:
    """Require ``container[field]`` to be a JSON object."""
    value = container.get(field)
    if not isinstance(value, dict):
        raise _field_error(field, value, "object")
    return value


def _int_field(container: dict[str, Any], field: str) -> int:
    """Require ``container[field]`` to be a JSON integer (not a bool)."""
    value = container.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _field_error(field, value, "integer")
    return value


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True)
class VerifiedHandoff:
    """A fully verified package with streamed access to its payloads."""

    dir: Path
    manifest: dict[str, Any]
    entry_count: int
    memory_count: int

    def stream_lines(self) -> list[str]:
        """All session-stream payload lines (already verified)."""
        with open(self.dir / SESSION_STREAM_NAME, encoding="utf-8") as f:
            return [line for line in f if line.strip()]

    def memory_lines(self) -> list[str]:
        """All memory payload lines (already verified)."""
        with open(self.dir / MEMORIES_NAME, encoding="utf-8") as f:
            return [line for line in f if line.strip()]

    def brief(self) -> dict[str, Any] | None:
        """The parsed resume brief, when the package carries one."""
        path = self.dir / BRIEF_NAME
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _load_manifest(pkg: Path) -> dict[str, Any]:
    manifest_path = pkg / MANIFEST_NAME
    if not manifest_path.is_file():
        raise HandoffError("missing_manifest", file=str(manifest_path))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise HandoffError("malformed_manifest", file=MANIFEST_NAME) from err
    if not isinstance(manifest, dict):
        raise HandoffError("malformed_manifest", file=MANIFEST_NAME)
    if manifest.get("format") != FORMAT_ID:
        raise HandoffError(
            "unsupported_format",
            file=MANIFEST_NAME,
            expected=FORMAT_ID,
            actual=str(manifest.get("format")),
        )
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise _field_error("schema_version", schema_version, "integer")
    if schema_version > SCHEMA_VERSION:
        raise HandoffError(
            "unsupported_schema",
            file=MANIFEST_NAME,
            expected=f"schema_version<={SCHEMA_VERSION}",
            actual=str(manifest.get("schema_version")),
        )
    if manifest.get("mode") not in MODES:
        raise HandoffError(
            "unknown_mode",
            file=MANIFEST_NAME,
            expected=" or ".join(MODES),
            actual=str(manifest.get("mode")),
        )
    for required_str in ("package_id", "handoff_id"):
        value = manifest.get(required_str)
        if not isinstance(value, str) or not value:
            raise _field_error(required_str, value, "non-empty string")
    # Validate every block shape up front so no later reader can meet an
    # unexpected type (see _field_error).
    _object_field(manifest, "source")
    _object_field(manifest, "target")
    _object_field(manifest, "counts")
    _object_field(manifest, "coverage")
    _object_field(manifest, "limits")
    if "consent" in manifest:
        _object_field(manifest, "consent")
    return manifest


def _verify_files(pkg: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise HandoffError("missing_files_block", file=MANIFEST_NAME)

    # Confinement BEFORE any read (handoff-v1 §10).
    for name in files:
        if not confined_relpath(pkg, str(name)):
            raise HandoffError("path_escape", file=str(name))

    for required in _REQUIRED_FILES:
        if required not in files:
            raise HandoffError("missing_file", file=required)
    if manifest.get("mode") == "resume" and BRIEF_NAME not in files:
        raise HandoffError("missing_file", file=BRIEF_NAME)

    for name, entry in files.items():
        if not isinstance(entry, dict):
            raise HandoffError(
                "invalid_file_entry",
                file=str(name),
                expected="object with size + sha256",
                actual=_describe(entry),
            )
        expected_size = entry.get("size")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int):
            raise HandoffError(
                "invalid_file_entry",
                file=str(name),
                expected="size integer",
                actual=_describe(expected_size),
            )
        expected_sha = entry.get("sha256")
        if not _is_hex64(expected_sha):
            raise HandoffError(
                "invalid_file_entry",
                file=str(name),
                expected="sha256 64-char hex",
                actual=_describe(expected_sha),
            )
        path = pkg / str(name)
        if not path.is_file():
            raise HandoffError("missing_file", file=str(name))
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise HandoffError(
                "size_mismatch",
                file=str(name),
                expected=str(expected_size),
                actual=str(actual_size),
            )
        actual_sha = sha256_file(path)
        if actual_sha != expected_sha:
            raise HandoffError(
                "digest_mismatch",
                file=str(name),
                expected=str(expected_sha),
                actual=actual_sha,
            )
    return files


def _verify_session_stream(pkg: Path, manifest: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """Parse + validate every session entry; return (entries, raw_lines)."""
    counts = _object_field(manifest, "counts")
    expected_entries = _int_field(counts, "entries")

    raw_lines: list[str] = []
    with open(pkg / SESSION_STREAM_NAME, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                raw_lines.append(line)
    if len(raw_lines) != expected_entries:
        raise HandoffError(
            "record_count_mismatch",
            file=SESSION_STREAM_NAME,
            expected=str(expected_entries),
            actual=str(len(raw_lines)),
        )

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    last_seq = 0
    for line in raw_lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as err:
            raise HandoffError("malformed_entry", file=SESSION_STREAM_NAME) from err
        if not isinstance(entry, dict):
            raise HandoffError("malformed_entry", file=SESSION_STREAM_NAME)
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or len(entry_id) != 64:
            raise HandoffError(
                "invalid_entry_id", file=SESSION_STREAM_NAME, actual=str(entry_id)[:16]
            )
        if entry_id in seen_ids:
            raise HandoffError("duplicate_entry_id", file=SESSION_STREAM_NAME, actual=entry_id)
        seen_ids.add(entry_id)
        kind = entry.get("kind")
        if kind not in ENTRY_KINDS:
            raise HandoffError("unknown_entry_kind", file=SESSION_STREAM_NAME, actual=str(kind))
        # Validate shapes before any reader touches them: a non-dict source
        # block, non-string text, or non-int schema_version must fail
        # closed, not crash a surface with AttributeError/TypeError.
        schema_version = entry.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise HandoffError(
                "invalid_entry",
                file=SESSION_STREAM_NAME,
                actual=f"{entry_id[:12]} schema_version",
            )
        if not isinstance(entry.get("text"), str):
            raise HandoffError(
                "invalid_entry",
                file=SESSION_STREAM_NAME,
                actual=f"{entry_id[:12]} text",
            )
        if not isinstance(entry.get("source"), dict):
            raise HandoffError(
                "invalid_entry",
                file=SESSION_STREAM_NAME,
                actual=f"{entry_id[:12]} source",
            )
        summary = entry.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise HandoffError(
                "invalid_entry",
                file=SESSION_STREAM_NAME,
                actual=f"{entry_id[:12]} summary",
            )
        # Stable ids are part of the contract (handoff-v1 §1/§4): every entry
        # id must be derivable from its source identity, so brief links and
        # re-prepares stay stable. A package carrying arbitrary ids fails.
        block = entry["source"]
        for label in ("adapter", "session_id", "source_entry_id"):
            value = block.get(label)
            if not isinstance(value, str) or not value:
                raise HandoffError(
                    "invalid_entry",
                    file=SESSION_STREAM_NAME,
                    actual=f"{entry_id[:12]} source.{label}",
                )
        expected_id = entry_id_for(block["adapter"], block["session_id"], block["source_entry_id"])
        if entry_id != expected_id:
            raise HandoffError(
                "entry_id_not_derived",
                file=SESSION_STREAM_NAME,
                expected=expected_id[:12],
                actual=entry_id[:12],
            )
        seq = entry.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= last_seq:
            raise HandoffError(
                "disordered_seq",
                file=SESSION_STREAM_NAME,
                actual=f"{entry_id[:12]} seq={seq}",
            )
        last_seq = seq
        entries.append(entry)

    # Provenance consistency: entries must belong to the manifest session.
    source_meta = _object_field(manifest, "source")
    adapter = source_meta.get("adapter")
    session_id = source_meta.get("session_id")
    for label, value in (("source.adapter", adapter), ("source.session_id", session_id)):
        if not isinstance(value, str) or not value:
            raise _field_error(label, value, "non-empty string")
    for entry in entries:
        block = entry["source"]
        if block.get("adapter") != adapter or block.get("session_id") != session_id:
            raise HandoffError(
                "provenance_mismatch",
                file=SESSION_STREAM_NAME,
                actual=f"{entry['id'][:12]}",
            )
    return entries, raw_lines


def _verify_memories(pkg: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    counts = _object_field(manifest, "counts")
    expected = _int_field(counts, "memories")
    records: list[dict[str, Any]] = []
    with open(pkg / MEMORIES_NAME, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as err:
                raise HandoffError("malformed_memory_record", file=MEMORIES_NAME) from err
            try:
                normalized = normalize_record(raw, default_source="handoff")
            except Exception as err:  # normalize is defensive; map to structured
                raise HandoffError(
                    "invalid_memory_record", file=MEMORIES_NAME, actual=str(raw.get("id"))[:16]
                ) from err
            if validate_record(normalized):
                raise HandoffError(
                    "invalid_memory_record", file=MEMORIES_NAME, actual=str(raw.get("id"))[:16]
                )
            # Handoff-level strictness: content_hash is the hydration dedupe
            # key, so it must be a well-formed SHA-256 hex digest.
            if not _is_hex64(normalized.get("content_hash")):
                raise HandoffError(
                    "invalid_memory_record",
                    file=MEMORIES_NAME,
                    actual=f"content_hash of {str(raw.get('id'))[:16]}",
                )
            records.append(normalized)
    if len(records) != expected:
        raise HandoffError(
            "record_count_mismatch",
            file=MEMORIES_NAME,
            expected=str(expected),
            actual=str(len(records)),
        )
    return records


def _verify_coverage_and_identity(
    manifest: dict[str, Any], entries: list[dict[str, Any]], memories: list[dict[str, Any]]
) -> None:
    counts = _object_field(manifest, "counts")
    coverage = _object_field(manifest, "coverage")
    by_kind = counts.get("entries_by_kind")
    if not isinstance(by_kind, dict):
        raise _field_error("entries_by_kind", by_kind, "object")
    for kind, count in by_kind.items():
        if isinstance(count, bool) or not isinstance(count, int):
            raise _field_error(f"entries_by_kind.{kind}", count, "integer")
    for list_field in ("omitted", "redacted"):
        value = coverage.get(list_field)
        if value is not None and not isinstance(value, list):
            raise _field_error(f"coverage.{list_field}", value, "array")
    for int_field in ("transferred", "omitted_count", "redacted_count"):
        _int_field(coverage, int_field)

    if coverage["transferred"] != len(entries):
        raise HandoffError(
            "coverage_mismatch",
            file=MANIFEST_NAME,
            expected=str(len(entries)),
            actual=str(coverage["transferred"]),
        )
    if coverage["omitted_count"] != len(coverage.get("omitted") or []):
        raise HandoffError("coverage_mismatch", file=MANIFEST_NAME, actual="omitted_count")
    if coverage["redacted_count"] != len(coverage.get("redacted") or []):
        raise HandoffError("coverage_mismatch", file=MANIFEST_NAME, actual="redacted_count")
    if sum(by_kind.values()) != len(entries):
        raise HandoffError("coverage_mismatch", file=MANIFEST_NAME, actual="entries_by_kind sum")

    # Content-derived identity: any payload tampering changes the hash.
    entry_hashes = [entry_content_hash(e) for e in entries]
    memory_hashes = [str(r.get("content_hash") or "") for r in memories]
    actual_package_id = package_id_for(entry_hashes + memory_hashes)
    if manifest.get("package_id") != actual_package_id:
        raise HandoffError(
            "package_id_mismatch",
            file=MANIFEST_NAME,
            expected=str(manifest.get("package_id")),
            actual=actual_package_id,
        )


def _verify_secret_gate(entries: list[dict[str, Any]], brief: dict[str, Any] | None) -> None:
    """The layer-3 gate as a package invariant (handoff-v1 §7)."""
    for entry in entries:
        try:
            assert_no_secrets(str(entry.get("text") or ""), where="session entry")
            summary = entry.get("summary")
            if isinstance(summary, str) and summary:
                assert_no_secrets(summary, where="session entry summary")
        except Exception as err:  # RedactionLeakError — structured re-raise
            raise HandoffError(
                "secret_content",
                file=SESSION_STREAM_NAME,
                actual=f"{entry.get('id', '')[:12]}: {err}",
            ) from err
    if brief is not None:
        try:
            assert_no_secrets(str(brief.get("text") or ""), where="resume brief")
        except Exception as err:
            raise HandoffError("secret_content", file=BRIEF_NAME, actual=str(err)) from err


def verify_handoff(pkg_dir: str | Path) -> VerifiedHandoff:
    """Verify a handoff package completely; raise HandoffError on any failure.

    Read-only. Every check from handoff-v1 §10 runs before this returns;
    hydration may only proceed on a VerifiedHandoff.
    """
    pkg = Path(pkg_dir)
    if not pkg.is_dir():
        raise HandoffError("missing_manifest", file=str(pkg / MANIFEST_NAME))

    manifest = _load_manifest(pkg)
    _verify_files(pkg, manifest)
    entries, _raw = _verify_session_stream(pkg, manifest)
    memories = _verify_memories(pkg, manifest)

    brief: dict[str, Any] | None = None
    if (pkg / BRIEF_NAME).is_file():
        try:
            brief = json.loads((pkg / BRIEF_NAME).read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise HandoffError("malformed_brief", file=BRIEF_NAME) from err
        if not isinstance(brief, dict) or not isinstance(brief.get("text"), str):
            raise HandoffError("malformed_brief", file=BRIEF_NAME)
        if brief.get("char_count") != len(brief["text"]):
            raise HandoffError("malformed_brief", file=BRIEF_NAME, actual="char_count")

    _verify_coverage_and_identity(manifest, entries, memories)
    _verify_secret_gate(entries, brief)

    _trace.info(
        "verify",
        f"verified package {manifest.get('package_id', '')[:12]}: "
        f"{len(entries)} entries, {len(memories)} memories",
        detail={"path": str(pkg), "mode": manifest.get("mode")},
    )
    return VerifiedHandoff(
        dir=pkg,
        manifest=manifest,
        entry_count=len(entries),
        memory_count=len(memories),
    )


def inspect_handoff(pkg_dir: str | Path) -> dict[str, Any]:
    """Read-only inspection report (acceptance criterion 11).

    Works on valid AND invalid packages: a failure becomes a structured
    ``failure`` block instead of an exception, so operators (and agents)
    can inspect before any target write.
    """
    pkg = Path(pkg_dir)
    report: dict[str, Any] = {"path": str(pkg), "valid": False, "failure": None}
    try:
        verified = verify_handoff(pkg)
    except HandoffError as err:
        report["failure"] = {
            "reason": err.reason,
            "file": err.file,
            "expected": err.expected,
            "actual": err.actual,
        }
        # Best-effort manifest context for the failure report.
        manifest_path = pkg / MANIFEST_NAME
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    for key in ("format", "schema_version", "mode", "package_id", "handoff_id"):
                        if key in manifest:
                            report[key] = manifest[key]
            except json.JSONDecodeError:
                pass
        return report

    manifest = verified.manifest
    coverage = manifest.get("coverage") or {}
    report.update(
        valid=True,
        handoff_id=manifest.get("handoff_id"),
        package_id=manifest.get("package_id"),
        mode=manifest.get("mode"),
        source=manifest.get("source"),
        target=manifest.get("target"),
        compatibility=manifest.get("compatibility"),
        counts=manifest.get("counts"),
        entries_by_kind=(manifest.get("counts") or {}).get("entries_by_kind"),
        files=manifest.get("files"),
        coverage={
            "transferred": coverage.get("transferred"),
            "omitted_count": coverage.get("omitted_count"),
            "redacted_count": coverage.get("redacted_count"),
            "recoverable_count": coverage.get("recoverable_count"),
            "omitted": coverage.get("omitted"),
            "redacted": coverage.get("redacted"),
        },
        limits=manifest.get("limits"),
        created_at=manifest.get("created_at"),
        hotmem_version=manifest.get("hotmem_version"),
    )
    return report
