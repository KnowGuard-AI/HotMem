"""Local filesystem storage adapter.

Handles file:// URIs and bare paths. Range reads use seek + read (no
full-file load); checksums are sha256 with an LRU cache; metadata infers
format from the file extension. An optional mmap path is available for
known-stable local files.
"""

from __future__ import annotations

import hashlib
import mmap
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from .base import StorageMetadata

_FORMAT_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".txt": "txt",
    ".json": "json",
    ".jsonl": "jsonl",
    ".csv": "csv",
    ".parquet": "parquet",
    ".md": "markdown",
    ".html": "html",
    ".xml": "xml",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
}


def _to_path(uri: str) -> Path:
    """Resolve a file:// URI or bare path to a filesystem Path."""
    if uri.startswith("file://"):
        return Path(uri[len("file://") :])
    return Path(uri)


class LocalFilesystemAdapter:
    """Read-only local filesystem adapter for file:// URIs and bare paths."""

    def read(self, uri: str) -> bytes:
        return _to_path(uri).read_bytes()

    def read_range(self, uri: str, offset: int, length: int) -> bytes:
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if length < 0:
            raise ValueError(f"length must be non-negative, got {length}")
        path = _to_path(uri)
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def read_range_chunked(
        self, uri: str, offset: int, length: int, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        """Yield [offset, offset+length) in bounded chunks (O(chunk) memory).

        Optional capability consumed by provenance.verify_range for
        large-range streaming verification (#88); adapters without it fall
        back to whole-range reads. Raises the same FileNotFoundError as
        read_range; a short stream is the caller's truncation signal.
        """
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if length < 0:
            raise ValueError(f"length must be non-negative, got {length}")
        path = _to_path(uri)
        remaining = length
        with open(path, "rb") as f:
            f.seek(offset)
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

    def exists(self, uri: str) -> bool:
        return _to_path(uri).exists()

    def metadata(self, uri: str) -> StorageMetadata:
        path = _to_path(uri)
        stat = path.stat()
        return StorageMetadata(
            uri=uri,
            size=stat.st_size,
            mtime=stat.st_mtime,
            format=_FORMAT_MAP.get(path.suffix.lower(), "unknown"),
        )

    def checksum(self, uri: str) -> str:
        return _checksum(_to_path(uri))

    def mmap_read(self, uri: str) -> bytes:
        """Memory-map a backing file. Caller must ensure file stability."""
        path = _to_path(uri)
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return m[:]


@lru_cache(maxsize=256)
def _checksum(path: Path) -> str:
    """Cached sha256 of a file path. Cache keyed on Path (not URI)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
