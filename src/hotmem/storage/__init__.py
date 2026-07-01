"""HotMem storage adapters.

Purpose:
    Abstract file/object access behind a single interface so HotMem can
    reference large data (file ranges) without duplicating it. HotMem only
    understands the abstraction; EMOS owns distributed storage.

Interface:
    StorageAdapter (Protocol): read, read_range, exists, metadata, checksum

Extension:
    Add new adapters (S3, HDFS, Azure, GCS) by registering a scheme in the
    ADAPTERS registry below. Distributed/object storage is owned by EMOS,
    not HotMem.
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

    Distributed/object storage (s3://, hdfs://, abfs://, gcs://, ...) is
    owned by EMOS, not HotMem.
    """


ADAPTERS: dict[str, StorageAdapter] = {
    "": LocalFilesystemAdapter(),
    "file": LocalFilesystemAdapter(),
}


def get_adapter(uri: str) -> StorageAdapter:
    """Return the adapter for a URI's scheme, or raise UnsupportedSchemeError.

    Bare paths and file:// URIs resolve to the local filesystem adapter.
    Unknown schemes raise an explicit error pointing to EMOS ownership.
    """
    scheme = _scheme(uri)
    adapter = ADAPTERS.get(scheme)
    if adapter is None:
        raise UnsupportedSchemeError(
            f"unsupported URI scheme {scheme!r} for {uri!r}; "
            "distributed/object storage is owned by EMOS, not HotMem"
        )
    return adapter


def _scheme(uri: str) -> str:
    """Return the lowercase scheme of a URI, or '' for a bare path."""
    if "://" in uri:
        return uri.split("://", 1)[0].lower()
    return ""
