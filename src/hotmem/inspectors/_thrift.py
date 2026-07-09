"""Minimal Thrift Compact Protocol reader (production, dependency-free).

Purpose:
    Read only what HotMem needs from a Parquet footer (``FileMetaData``)
    without pulling in a Thrift runtime or pyarrow. This is the dependency-free
    bet behind issue #53: Parquet *provenance* with zero new deps.

Scope:
    Implements the subset of the Thrift Compact Protocol required to decode
    Parquet ``FileMetaData`` (structs, ints/i64, lists, strings, and stop).
    It is intentionally *not* a general Thrift implementation — it reads
    forward only and tolerates unknown fields by skipping them via the wire
    type tags.

Reference: Apache Thrift Compact Protocol specification; Apache Parquet
``parquet.thrift`` FileMetaData struct.
"""

from __future__ import annotations

import struct
from typing import Any

# Compact protocol wire type ids (low nibble of the field header byte).
_CT_STOP = 0x00
_CT_BOOL_TRUE = 0x01
_CT_BOOL_FALSE = 0x02
_CT_BYTE = 0x03
_CT_I16 = 0x04
_CT_I32 = 0x05
_CT_I64 = 0x06
_CT_DOUBLE = 0x07
_CT_BINARY = 0x08  # also STRING
_CT_LIST = 0x09
_CT_SET = 0x0A
_CT_MAP = 0x0B
_CT_STRUCT = 0x0C

_TYPE_NAMES = {
    _CT_STOP: "stop",
    _CT_BOOL_TRUE: "bool",
    _CT_BOOL_FALSE: "bool",
    _CT_BYTE: "byte",
    _CT_I16: "i16",
    _CT_I32: "i32",
    _CT_I64: "i64",
    _CT_DOUBLE: "double",
    _CT_BINARY: "string",
    _CT_LIST: "list",
    _CT_SET: "set",
    _CT_MAP: "map",
    _CT_STRUCT: "struct",
}


class ThriftCompactReader:
    """Forward-only reader over a Thrift Compact Protocol byte buffer."""

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    # ── low-level primitives ───────────────────────────────────────────────

    def _read_byte(self) -> int:
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def _read_varint(self) -> int:
        """Decode a Thrift Compact zigzag varint."""
        shift = 0
        result = 0
        while True:
            b = self.buf[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 63:
                raise ValueError("varint too long")
        # zigzag decode
        return (result >> 1) ^ -(result & 1)

    def _read_bytes(self, n: int) -> bytes:
        chunk = self.buf[self.pos : self.pos + n]
        if len(chunk) != n:
            raise ValueError("short read in thrift buffer")
        self.pos += n
        return chunk

    # ── typed value readers ───────────────────────────────────────────────

    def read_value(self, wire_type: int) -> Any:
        if wire_type in (_CT_BOOL_TRUE, _CT_BOOL_FALSE):
            return wire_type == _CT_BOOL_TRUE
        if wire_type == _CT_BYTE:
            return self._read_signed_byte()
        if wire_type == _CT_I16:
            return self._read_varint()
        if wire_type == _CT_I32:
            return self._read_varint()
        if wire_type == _CT_I64:
            return self._read_varint()
        if wire_type == _CT_DOUBLE:
            return struct.unpack("<d", self._read_bytes(8))[0]
        if wire_type == _CT_BINARY:
            n = self._read_varint()
            return self._read_bytes(n).decode("utf-8", errors="replace")
        if wire_type == _CT_LIST:
            return self.read_list()
        if wire_type == _CT_SET:
            return self.read_list()
        if wire_type == _CT_MAP:
            return self.read_map()
        if wire_type == _CT_STRUCT:
            return self.read_struct()
        raise ValueError(f"unsupported thrift wire type {wire_type}")

    def _read_signed_byte(self) -> int:
        b = self._read_byte()
        return b - 256 if b > 127 else b

    def read_list(self) -> list[Any]:
        header = self._read_byte()
        size = header >> 4
        element_type = header & 0x0F
        if size == 0x0F:
            # The element type is in the low nibble of the header byte;
            # only the size is a varint per the Compact Protocol spec.
            size = self._read_varint()
        return [self.read_value(element_type) for _ in range(size)]

    def read_map(self) -> dict[Any, Any]:
        size = self._read_varint()
        if size == 0:
            return {}
        kv_header = self._read_byte()
        key_type = (kv_header >> 4) & 0x0F
        val_type = kv_header & 0x0F
        out: dict[Any, Any] = {}
        for _ in range(size):
            k = self.read_value(key_type)
            v = self.read_value(val_type)
            out[k] = v
        return out

    def read_struct(self) -> dict[str, Any]:
        """Read a struct as a dict keyed by integer field id.

        Unknown fields are decoded by wire type so we can skip them without a
        schema — this is what makes the reader robust to Parquet version drift.
        """
        out: dict[str, Any] = {}
        last_field = 0
        while True:
            if self.pos >= len(self.buf):
                break
            header = self._read_byte()
            if header == _CT_STOP:
                break
            wire_type = header & 0x0F
            delta = header >> 4
            field_id = self._read_varint() if delta == 0 else last_field + delta
            last_field = field_id
            out[str(field_id)] = self.read_value(wire_type)
        return out


def type_name(wire_type: int) -> str:
    return _TYPE_NAMES.get(wire_type, f"type{wire_type}")
