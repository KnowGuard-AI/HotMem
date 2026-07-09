"""Inspector interfaces for lightweight file-native inspection (issue #53).

Purpose:
    Define the contract every file inspector implements and the FileInspection
    result shape. Inspectors understand *about* a file (headers, size, row
    counts, schema, byte ranges) without becoming a query engine and without
    copying large file contents into SQLite.

Interface:
    FileInspector (Protocol): inspect(uri, adapter, ...) -> FileInspection
    FileInspection: provenance + format-specific metadata dataclass
    inspect_file(uri, *, count_rows=False, sample_size=5) -> FileInspection

Deps: hotmem.storage
Extension: register a new inspector in hotmem.inspectors.__init__.ADAPTER_INSPECTORS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hotmem.storage import StorageAdapter, StorageMetadata, get_adapter


@dataclass(frozen=True)
class FileInspection:
    """Provenance + format-specific metadata for a backing file.

    Designed so a future file-backed memory (#38) can store this directly:
    URI, size, mtime, checksum (provenance) plus light format metadata and
    optional sample byte ranges. No large content is ever materialized here.
    """

    uri: str
    format: str
    size: int
    mtime: float
    checksum: str
    columns: list[str] | None = None
    row_count: int | None = None
    delimiter: str | None = None
    has_header: bool | None = None
    num_row_groups: int | None = None
    schema_types: list[str] | None = None
    sample: list[dict[str, Any]] | None = None
    byte_ranges: list[tuple[int, int]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    unsupported_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for CLI/API/MCP consumption."""
        return {
            "uri": self.uri,
            "format": self.format,
            "size": self.size,
            "mtime": self.mtime,
            "checksum": self.checksum,
            "columns": self.columns,
            "row_count": self.row_count,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "num_row_groups": self.num_row_groups,
            "schema_types": self.schema_types,
            "sample": self.sample,
            "byte_ranges": self.byte_ranges,
            "metadata": self.metadata,
            "unsupported_reason": self.unsupported_reason,
        }


@runtime_checkable
class FileInspector(Protocol):
    """Read-only metadata inspector for one backing-file format."""

    def inspect(
        self,
        uri: str,
        adapter: StorageAdapter,
        meta: StorageMetadata,
        *,
        count_rows: bool = False,
        sample_size: int = 5,
    ) -> FileInspection: ...


def resolve_adapter(uri: str) -> tuple[StorageAdapter, StorageMetadata]:
    """Return (adapter, metadata) for ``uri``, failing fast on remote schemes.

    Reuses hotmem.storage so unsupported remote schemes (s3://, hdfs://, ...)
    raise the existing EMOS-boundary UnsupportedSchemeError before any
    inspector runs.
    """
    adapter = get_adapter(uri)
    meta = adapter.metadata(uri)
    return adapter, meta
