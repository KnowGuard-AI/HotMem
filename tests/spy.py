"""Test helper: a counting wrapper around any storage adapter.

Wraps an adapter (main's LocalFilesystemAdapter or any StorageAdapter Protocol)
and counts calls so tests can assert metadata access performs no file I/O.
"""

from __future__ import annotations

from typing import Any


class SpyAdapter:
    """A storage adapter wrapper that counts calls to each method."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.read_range_calls = 0
        self.checksum_calls = 0
        self.exists_calls = 0
        self.read_calls = 0

    def read_range(self, uri: str, offset: int, length: int) -> bytes:
        self.read_range_calls += 1
        return self.inner.read_range(uri, offset, length)

    def read(self, uri: str) -> bytes:
        self.read_calls += 1
        return self.inner.read(uri)

    def exists(self, uri: str) -> bool:
        self.exists_calls += 1
        return self.inner.exists(uri)

    def checksum(self, uri: str) -> str:
        self.checksum_calls += 1
        return self.inner.checksum(uri)

    def metadata(self, uri: str) -> Any:
        return self.inner.metadata(uri)

    @property
    def total_file_reads(self) -> int:
        """Count of methods that open/read the backing file (excludes exists)."""
        return self.read_range_calls + self.checksum_calls + self.read_calls
