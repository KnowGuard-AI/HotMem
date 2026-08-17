"""HotMem storage adapters.

Purpose:
    Abstract file/object access behind a single interface so HotMem can
    reference large data (file ranges) without duplicating it. HotMem only
    understands the abstraction; the built-in adapter is local-only.

Interface:
    StorageAdapter (Protocol): read, read_range, exists, metadata, checksum

Extension:
    Add new adapters (S3, HDFS, Azure, GCS) by registering a scheme in the
    ADAPTERS registry below. Remote and distributed storage are not built-in.
"""

from __future__ import annotations

from .base import StorageAdapter, StorageMetadata
from .local import LocalFilesystemAdapter

__all__ = [
    "StorageAdapter",
    "StorageMetadata",
    "LocalFilesystemAdapter",
    "get_adapter",
    "UnsupportedSchemeError",
]


class UnsupportedSchemeError(ValueError):
    """Raised when a URI scheme is not handled by any HotMem adapter.

    Remote and distributed URI schemes are not supported by the built-in
    local adapter.
    """


ADAPTERS: dict[str, StorageAdapter] = {
    "": LocalFilesystemAdapter(),
    "file": LocalFilesystemAdapter(),
}


def get_adapter(uri: str) -> StorageAdapter:
    """Return the adapter for a URI's scheme, or raise UnsupportedSchemeError.

    Bare paths and file:// URIs resolve to the local filesystem adapter.
    Unknown schemes raise an explicit error instead of being fetched silently.
    """
    scheme = _scheme(uri)
    adapter = ADAPTERS.get(scheme)
    if adapter is None:
        raise UnsupportedSchemeError(
            f"unsupported URI scheme {scheme!r} for {uri!r}; "
            "only local filesystem storage is supported by the built-in adapter"
        )
    return adapter


def _scheme(uri: str) -> str:
    """Return the lowercase scheme of a URI, or '' for a bare path."""
    if "://" in uri:
        return uri.split("://", 1)[0].lower()
    return ""
