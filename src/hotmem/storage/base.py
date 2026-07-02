"""Storage adapter interface.

HotMem references large data (URI + offset + length + checksum) instead of
duplicating it. This protocol is the seam between HotMem and any backing
store. The local filesystem adapter is the only built-in implementation;
distributed/object adapters are owned by EMOS.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class StorageMetadata(TypedDict):
    """Metadata for a backing object."""

    uri: str
    size: int
    mtime: float
    format: str


@runtime_checkable
class StorageAdapter(Protocol):
    """Read access to a backing store without copying entire datasets."""

    def read(self, uri: str) -> bytes:
        """Read the full contents of a backing object."""
        ...

    def read_range(self, uri: str, offset: int, length: int) -> bytes:
        """Read a byte range [offset, offset + length) without full-file load."""
        ...

    def exists(self, uri: str) -> bool:
        """Return True if the backing object exists."""
        ...

    def metadata(self, uri: str) -> StorageMetadata:
        """Return size, mtime, and inferred format for a backing object."""
        ...

    def checksum(self, uri: str) -> str:
        """Return a sha256 hex digest of the backing object's contents."""
        ...
