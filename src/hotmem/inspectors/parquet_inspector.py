"""Parquet inspector — metadata-only footer reader, no query engine (issue #53).

Scope (#53 + file-aware-architecture.md §4):
    - Validate PAR1 magic at head and tail.
    - Read the Thrift-Compact ``FileMetaData`` footer and extract: version,
      num_rows, schema (column names + physical types), row_group count.
    - **No data-page decoding, no query engine, no pyarrow dependency.**
    - Malformed or hostile footers set ``unsupported_reason`` rather than
      raising, so a bad file becomes provenance, not an outage.

The dependency-free footer reader lives in ``_thrift.py``.
"""

from __future__ import annotations

import struct

from hotmem.storage import StorageAdapter, StorageMetadata

from ._thrift import ThriftCompactReader
from .base import FileInspection

_PARQUET_MAGIC = b"PAR1"
_TAIL_LEN = 8  # 4-byte footer length + 4-byte trailing magic
_MAX_FOOTER = 1 << 30  # reject footers claiming > 1 GiB (hostile-file guard)

# Parquet physical Type enum (parquet.thrift).
_PARQUET_TYPE = {
    0: "BOOLEAN",
    1: "INT32",
    2: "INT64",
    3: "INT96",
    4: "FLOAT",
    5: "DOUBLE",
    6: "BYTE_ARRAY",
    7: "FIXED_LEN_BYTE_ARRAY",
}

# FileMetaData field ids (parquet.thrift).
_FM_VERSION = "1"
_FM_SCHEMA = "2"
_FM_NUM_ROWS = "3"
_FM_ROW_GROUPS = "4"

# SchemaElement field ids.
_SE_TYPE = "1"
_SE_NAME = "4"
_SE_NUM_CHILDREN = "5"


class ParquetInspector:
    """Inspect a Parquet file's footer metadata only."""

    def inspect(
        self,
        uri: str,
        adapter: StorageAdapter,
        meta: StorageMetadata,
        *,
        count_rows: bool = False,  # noqa: ARG002 — num_rows comes from the footer
        sample_size: int = 0,  # noqa: ARG002 — no row sampling (metadata-only)
    ) -> FileInspection:
        size = meta["size"]
        unsupported = self._validate_magic(adapter, uri, size)
        if unsupported:
            return FileInspection(
                uri=uri,
                format="parquet",
                size=size,
                mtime=meta["mtime"],
                checksum=adapter.checksum(uri),
                unsupported_reason=unsupported,
            )

        footer = self._read_footer(adapter, uri, size)
        if isinstance(footer, str):
            return FileInspection(
                uri=uri,
                format="parquet",
                size=size,
                mtime=meta["mtime"],
                checksum=adapter.checksum(uri),
                unsupported_reason=footer,
            )

        parsed = self._parse_footer(footer)
        if isinstance(parsed, str):
            return FileInspection(
                uri=uri,
                format="parquet",
                size=size,
                mtime=meta["mtime"],
                checksum=adapter.checksum(uri),
                unsupported_reason=parsed,
            )

        version, num_rows, columns, schema_types, num_row_groups = parsed
        return FileInspection(
            uri=uri,
            format="parquet",
            size=size,
            mtime=meta["mtime"],
            checksum=adapter.checksum(uri),
            columns=columns,
            row_count=num_rows,
            num_row_groups=num_row_groups,
            schema_types=schema_types,
            metadata={"version": version},
        )

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _validate_magic(adapter: StorageAdapter, uri: str, size: int) -> str | None:
        if size < 12:
            return f"file too small ({size} bytes) to be a valid Parquet file"
        head = adapter.read_range(uri, 0, 4)
        if head != _PARQUET_MAGIC:
            return f"missing leading PAR1 magic (got {head!r})"
        tail = adapter.read_range(uri, size - 4, 4)
        if tail != _PARQUET_MAGIC:
            return f"missing trailing PAR1 magic (got {tail!r})"
        return None

    @staticmethod
    def _read_footer(adapter: StorageAdapter, uri: str, size: int) -> bytes | str:
        tail = adapter.read_range(uri, size - _TAIL_LEN, _TAIL_LEN)
        footer_length = struct.unpack("<I", tail[:4])[0]
        if footer_length <= 0 or footer_length > _MAX_FOOTER:
            return f"implausible footer length {footer_length}"
        available = size - _TAIL_LEN
        if footer_length > available:
            return f"footer length {footer_length} exceeds available bytes {available}"
        offset = available - footer_length
        return adapter.read_range(uri, offset, footer_length)

    @staticmethod
    def _parse_footer(
        footer: bytes,
    ) -> tuple[int, int | None, list[str], list[str], int | None] | str:
        try:
            reader = ThriftCompactReader(footer)
            fm = reader.read_struct()
        except (ValueError, IndexError) as err:
            return f"could not parse Parquet footer: {err}"

        version = int(fm.get(_FM_VERSION, 0)) if fm.get(_FM_VERSION) is not None else 0
        num_rows_raw = fm.get(_FM_NUM_ROWS)
        num_rows = int(num_rows_raw) if num_rows_raw is not None else None

        columns: list[str] = []
        schema_types: list[str] = []
        schema = fm.get(_FM_SCHEMA)
        if isinstance(schema, list):
            for element in schema:
                if not isinstance(element, dict):
                    continue
                name = element.get(_SE_NAME)
                num_children = element.get(_SE_NUM_CHILDREN)
                # The first element is the root group; columns follow it.
                # Skip root when it has children (it's not a leaf column).
                if num_children:
                    continue
                if name is not None:
                    columns.append(str(name))
                type_id = element.get(_SE_TYPE)
                if type_id is not None:
                    schema_types.append(_PARQUET_TYPE.get(int(type_id), "UNKNOWN"))
                else:
                    schema_types.append("UNKNOWN")

        row_groups = fm.get(_FM_ROW_GROUPS)
        num_row_groups = len(row_groups) if isinstance(row_groups, list) else None

        return version, num_rows, columns, schema_types, num_row_groups
