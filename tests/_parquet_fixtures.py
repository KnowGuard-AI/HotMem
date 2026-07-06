"""Minimal, dependency-free Parquet fixture generator (test-only).

Produces a valid Parquet file whose *footer* encodes a realistic
FileMetaData (version, num_rows, schema, one row group) so the
ParquetInspector's Thrift-Compact footer parser is exercised against a
real footer shape. No data pages are written — the inspector only reads
metadata, so a footer-only file is sufficient and keeps the fixture tiny.

This is deliberately test-only: production never writes Parquet.
"""

from __future__ import annotations

import struct
from pathlib import Path

# Thrift Compact wire type ids (mirror of src/hotmem/inspectors/_thrift.py).
_CT_STOP = 0x00
_CT_BOOL_TRUE = 0x01
_CT_I16 = 0x04
_CT_I32 = 0x05
_CT_I64 = 0x06
_CT_BINARY = 0x08
_CT_LIST = 0x09
_CT_STRUCT = 0x0C

_PARQUET_MAGIC = b"PAR1"


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _varint(n: int) -> bytes:
    n = _zigzag(n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


class _CompactWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def byte(self, b: int) -> None:
        self.buf.append(b & 0xFF)

    def varint(self, n: int) -> None:
        self.buf += _varint(n)

    def field_header(self, field_id: int, wire_type: int, last_field: int) -> int:
        delta = field_id - last_field
        if 0 < delta <= 15:
            self.byte((delta << 4) | wire_type)
        else:
            self.byte(wire_type)  # delta == 0 means explicit field id follows
            self.varint(field_id)
        return field_id

    def write_i32(self, field_id: int, value: int, last: int) -> int:
        lf = self.field_header(field_id, _CT_I32, last)
        self.varint(value)
        return lf

    def write_i64(self, field_id: int, value: int, last: int) -> int:
        lf = self.field_header(field_id, _CT_I64, last)
        self.varint(value)
        return lf

    def write_string(self, field_id: int, value: str, last: int) -> int:
        lf = self.field_header(field_id, _CT_BINARY, last)
        data = value.encode("utf-8")
        self.varint(len(data))
        self.buf += data
        return lf

    def write_struct_field(self, field_id: int, last: int) -> int:
        return self.field_header(field_id, _CT_STRUCT, last)

    def write_list_header(self, field_id: int, size: int, element_type: int, last: int) -> int:
        lf = self.field_header(field_id, _CT_LIST, last)
        if size < 15:
            self.byte((size << 4) | element_type)
        else:
            self.byte(0xF0 | element_type)
            self.varint(size)
        return lf

    def stop(self) -> None:
        self.byte(_CT_STOP)


def _schema_element(w: _CompactWriter, name: str, type_id: int, num_children: int | None) -> None:
    """Write one SchemaElement struct (ascending field ids, then STOP)."""
    last = 0
    if type_id is not None:
        last = w.write_i32(1, type_id, last)  # Type
    last = w.write_string(4, name, last)  # name (required)
    if num_children is not None:
        last = w.write_i32(5, num_children, last)  # num_children
    w.stop()


def _row_group(w: _CompactWriter, num_rows: int, total_byte_size: int, num_columns: int) -> None:
    """Write one RowGroup struct (parquet.thrift field ids)."""
    last = 0
    # field 1: list<ColumnChunk> columns
    last = w.write_list_header(1, num_columns, _CT_STRUCT, last)
    for _ in range(num_columns):
        # ColumnChunk: field 1 file_offset (i64), field 2 meta (ColumnMetaData)
        cc_last = 0
        cc_last = w.write_i64(1, 0, cc_last)  # file_offset
        w.write_struct_field(2, cc_last)  # ColumnMetaData struct
        cm_last = 0
        cm_last = w.write_i32(1, 6, cm_last)  # type = BYTE_ARRAY
        cm_last = w.write_list_header(2, 0, _CT_BINARY, cm_last)  # encodings (empty)
        cm_last = w.write_list_header(3, 0, _CT_BINARY, cm_last)  # path_in_schema (empty)
        cm_last = w.write_i32(4, 1, cm_last)  # codec = UNCOMPRESSED (i32)
        cm_last = w.write_i64(5, 0, cm_last)  # num_values (i64)
        cm_last = w.write_i64(6, 0, cm_last)  # total_uncompressed_size (i64)
        cm_last = w.write_i64(7, 0, cm_last)  # total_compressed_size (i64)
        cm_last = w.write_i64(9, 4, cm_last)  # data_page_offset (i64)
        w.stop()  # end ColumnMetaData
        w.stop()  # end ColumnChunk
    last = w.write_i64(2, total_byte_size, last)  # total_byte_size (i64)
    last = w.write_i64(3, num_rows, last)  # num_rows (i64)
    w.stop()


def write_parquet_fixture(
    path: Path,
    *,
    num_rows: int = 3,
    columns: list[tuple[str, int]] | None = None,
) -> Path:
    """Write a minimal valid Parquet file (footer-only) to ``path``.

    ``columns`` is a list of (name, parquet_type_id) pairs. The file has one
    row group. No data pages are written; the inspector only reads metadata.
    """
    if columns is None:
        columns = [("id", 1), ("name", 6)]  # INT32, BYTE_ARRAY

    w = _CompactWriter()
    last = 0
    last = w.write_i32(1, 1, last)  # version = 1

    # schema: root element + one element per column.
    last = w.write_list_header(2, len(columns) + 1, _CT_STRUCT, last)
    _schema_element(w, "root", None, len(columns))  # root, num_children = n
    for name, type_id in columns:
        _schema_element(w, name, type_id, None)

    last = w.write_i64(3, num_rows, last)  # num_rows

    # row_groups: one row group with len(columns) ColumnChunks.
    last = w.write_list_header(4, 1, _CT_STRUCT, last)
    _row_group(w, num_rows=num_rows, total_byte_size=0, num_columns=len(columns))

    w.stop()  # end FileMetaData

    footer = bytes(w.buf)
    out = bytearray()
    out += _PARQUET_MAGIC
    out += footer
    out += struct.pack("<I", len(footer))
    out += _PARQUET_MAGIC
    path.write_bytes(bytes(out))
    return path
