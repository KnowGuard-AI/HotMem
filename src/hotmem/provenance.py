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
     verify_bytes(source_uri, data, expected_checksum) -> None
     verify_range(adapter, source_uri, offset, length, expected_checksum) -> None

Deps: none (stdlib only)
Extension: add stronger provenance policies (e.g. signed checksums) here.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from hotmem.trace import get_tracer

_trace = get_tracer("provenance")

# Ranges above this size are verified via chunked streaming (O(chunk) memory)
# when the adapter exposes read_range_chunked; smaller ranges keep the simple
# single-read path (#88). 8 MiB = 8x the inspector/embed read chunk.
STREAM_VERIFY_THRESHOLD = 8 << 20

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


def verify_bytes(source_uri: str, data: bytes, expected_checksum: str) -> None:
    """Verify already-read bytes against the expected range SHA-256 (#87).

    Raises ChecksumMismatchError on mismatch. Missing-file and truncated
    conditions belong to the caller — it owns the read and can produce the
    precise reason from read results.

    The digest semantics are identical to ``verify_range``: SHA-256 over the
    byte RANGE the caller materialized, never the whole file.
    """
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_checksum:
        _trace.warn(
            "verify",
            "checksum mismatch",
            detail={"source_uri": source_uri, "expected": expected_checksum, "actual": actual},
        )
        raise ChecksumMismatchError(source_uri, expected=expected_checksum, actual=actual)


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

    Ranges above ``STREAM_VERIFY_THRESHOLD`` are hashed while streaming
    through ``adapter.read_range_chunked`` (an optional adapter capability —
    absent on other adapters or smaller ranges, the whole-range read runs
    unchanged). The digest and every error are identical either way (#88).
    """
    if expected_checksum is None:
        return

    chunked = getattr(adapter, "read_range_chunked", None)
    if length > STREAM_VERIFY_THRESHOLD and callable(chunked):
        _verify_streaming(source_uri, chunked, offset, length, expected_checksum)
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

    verify_bytes(source_uri, data, expected_checksum)


def _verify_streaming(
    source_uri: str,
    chunked: Any,
    offset: int,
    length: int,
    expected_checksum: str,
) -> None:
    """Stream-hash a large range with O(chunk) memory; identical errors (#88)."""
    h = hashlib.sha256()
    received = 0
    try:
        stream = chunked(source_uri, offset, length)
    except FileNotFoundError as err:
        _trace.warn("verify", "missing backing file", detail={"source_uri": source_uri})
        raise BackingFileMissingError(source_uri) from err
    try:
        for chunk in stream:
            h.update(chunk)
            received += len(chunk)
    except FileNotFoundError as err:
        _trace.warn("verify", "missing backing file", detail={"source_uri": source_uri})
        raise BackingFileMissingError(source_uri) from err

    if received < length:
        _trace.warn(
            "verify",
            "truncated backing file",
            detail={"source_uri": source_uri, "expected": length, "got": received},
        )
        raise ProvenanceError("truncated", source_uri, expected=expected_checksum)

    actual = h.hexdigest()
    if actual != expected_checksum:
        _trace.warn(
            "verify",
            "checksum mismatch",
            detail={"source_uri": source_uri, "expected": expected_checksum, "actual": actual},
        )
        raise ChecksumMismatchError(source_uri, expected=expected_checksum, actual=actual)
