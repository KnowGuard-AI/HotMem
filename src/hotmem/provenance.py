"""HotMem provenance — checksum verification and typed errors.

Purpose:
     Provide a clear error hierarchy for file-backed memory hydration
     failures (checksum mismatch, missing file, truncated range) so the
     server can map it to HTTP 409 with a precise JSON body and callers
     can catch specific failure modes.

Interface:
     ProvenanceError(Exception):
         .reason: 'checksum_mismatch' | 'missing_file' | 'truncated'
         .source_uri: str
         .expected: str | None
         .actual: str | None
     ChecksumMismatchError(ProvenanceError)
     BackingFileMissingError(ProvenanceError)
     verify_range(adapter, source_uri, offset, length, expected_checksum) -> None

Deps: none (stdlib only)
Extension: add stronger provenance policies (e.g. signed checksums) here.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from hotmem.trace import get_tracer

_trace = get_tracer("provenance")

Reason = Literal["checksum_mismatch", "missing_file", "truncated"]


class ProvenanceError(Exception):
    """Raised when file-backed memory hydration cannot be proven.

    Carries structured fields so the server can produce a clear 409 body:
        {"error": "provenance_mismatch", "reason": ..., "expected": ...,
         "actual": ..., "source_uri": ...}

    ``except ProvenanceError`` catches all provenance failures. For
    specific handling, catch ``ChecksumMismatchError`` or
    ``BackingFileMissingError`` instead.
    """

    def __init__(
        self,
        reason: Reason,
        source_uri: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        self.reason: Reason = reason
        self.source_uri = source_uri
        self.expected = expected
        self.actual = actual
        msg = f"provenance failure ({reason}) for {source_uri}"
        if expected is not None or actual is not None:
            msg += f": expected={expected} actual={actual}"
        super().__init__(msg)


class ChecksumMismatchError(ProvenanceError):
    """Checksum verification failed for a file-backed byte range."""

    def __init__(self, source_uri: str, *, expected: str, actual: str) -> None:
        super().__init__("checksum_mismatch", source_uri, expected=expected, actual=actual)


class BackingFileMissingError(ProvenanceError):
    """The backing file for a file-backed memory was not found."""

    def __init__(self, source_uri: str) -> None:
        super().__init__("missing_file", source_uri)


def verify_range(
    adapter: Any,
    source_uri: str,
    offset: int,
    length: int,
    expected_checksum: str | None,
) -> None:
    """Verify a byte range's checksum on demand. Raises ProvenanceError on failure.

    - Missing file -> BackingFileMissingError.
    - Short read (< length bytes) -> ProvenanceError(reason='truncated').
    - Checksum mismatch -> ChecksumMismatchError.

    If ``expected_checksum`` is None, verification is skipped (the caller
    marks the hydration 'unverified' rather than failing).

    The checksum is computed as SHA-256 of the byte RANGE [offset, offset+length),
    NOT the whole file (main's adapter.checksum computes whole-file SHA-256,
    so we compute the range checksum ourselves via read_range + sha256).
    """
    if expected_checksum is None:
        return

    try:
        data = adapter.read_range(source_uri, offset, length)
    except FileNotFoundError as err:
        _trace.warn("verify", "missing backing file", detail={"source_uri": source_uri})
        raise BackingFileMissingError(source_uri) from err

    if len(data) < length:
        _trace.warn(
            "verify",
            "truncated backing file",
            detail={"source_uri": source_uri, "expected": length, "got": len(data)},
        )
        raise ProvenanceError("truncated", source_uri, expected=expected_checksum)

    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_checksum:
        _trace.warn(
            "verify",
            "checksum mismatch",
            detail={"source_uri": source_uri, "expected": expected_checksum, "actual": actual},
        )
        raise ChecksumMismatchError(source_uri, expected=expected_checksum, actual=actual)
