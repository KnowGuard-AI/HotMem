"""JSONL inspector — streamed line count + seek-based range sampling.

Scope (#53):
    - Stream line counts and selected ranges without full ingestion.
    - Validate sampled lines are JSON; report the first malformed line offset
      in ``unsupported_reason`` instead of crashing (provenance, not outage).
    - O(file size) byte read, O(1) memory via buffered newline scanning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hotmem.storage import StorageAdapter, StorageMetadata

from .base import FileInspection

_READ_CHUNK = 1 << 20  # 1 MiB read window — constant memory regardless of file size.


class JSONLInspector:
    """Inspect a JSONL file's structure with O(1) streaming memory."""

    def inspect(
        self,
        uri: str,
        adapter: StorageAdapter,
        meta: StorageMetadata,
        *,
        count_rows: bool = False,
        sample_size: int = 5,
    ) -> FileInspection:
        row_count: int | None
        sample_rows: list[dict[str, Any]] = []
        byte_ranges: list[tuple[int, int]] = []
        unsupported_reason: str | None = None

        path = _resolve(uri)
        row_count, sample_rows, byte_ranges, unsupported_reason = _stream(
            path, count_rows=count_rows, sample_size=sample_size
        )

        columns = _infer_columns(sample_rows)

        return FileInspection(
            uri=uri,
            format="jsonl",
            size=meta["size"],
            mtime=meta["mtime"],
            checksum=adapter.checksum(uri),
            columns=columns,
            row_count=row_count,
            delimiter="\n",
            has_header=None,
            sample=sample_rows or None,
            byte_ranges=byte_ranges or None,
            metadata={},
            unsupported_reason=unsupported_reason,
        )


def _resolve(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[len("file://") :])
    return Path(uri)


def _stream(
    path: Path,
    *,
    count_rows: bool,
    sample_size: int,
) -> tuple[int | None, list[dict[str, Any]], list[tuple[int, int]], str | None]:
    """One streaming pass: count lines, collect a bounded sample, validate JSON.

    Counts newlines in fixed-size chunks (cheap, allocation-free) and samples
    the first ``sample_size`` complete records by capturing byte offsets.
    """
    row_count = 0 if count_rows else None
    sample_rows: list[dict[str, Any]] = []
    byte_ranges: list[tuple[int, int]] = []
    unsupported_reason: str | None = None

    line_index = 0
    line_start = 0
    carry = b""

    with open(path, "rb") as f:
        offset = 0
        while True:
            chunk = f.read(_READ_CHUNK)
            if not chunk:
                # Final partial line without a trailing newline.
                if carry:
                    if count_rows and carry.strip():
                        row_count = (row_count or 0) + 1
                    if unsupported_reason is None:
                        unsupported_reason = _validate(carry, line_index, line_start)
                    _handle_line(
                        carry,
                        line_index,
                        line_start,
                        len(carry),
                        sample_size,
                        sample_rows,
                        byte_ranges,
                    )
                break

            data = carry + chunk
            chunk_end = offset + len(chunk)
            pos = 0
            nl = data.find(b"\n", pos)
            while nl != -1:
                complete = data[pos : nl + 1]
                line_len = len(complete)
                if count_rows and complete.strip():
                    row_count = (row_count or 0) + 1
                if unsupported_reason is None:
                    unsupported_reason = _validate(complete, line_index, line_start)
                _handle_line(
                    complete,
                    line_index,
                    line_start,
                    line_len,
                    sample_size,
                    sample_rows,
                    byte_ranges,
                )
                line_index += 1
                pos = nl + 1
                line_start = offset + pos
                nl = data.find(b"\n", pos)
            carry = data[pos:]
            offset = chunk_end

    return row_count, sample_rows, byte_ranges, unsupported_reason


def _handle_line(
    raw: bytes,
    line_index: int,
    line_start: int,
    line_len: int,
    sample_size: int,
    sample_rows: list[dict[str, Any]],
    byte_ranges: list[tuple[int, int]],
) -> None:
    """Append one complete line to the bounded sample if it parses as JSON."""
    if line_index >= sample_size:
        return
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict):
        sample_rows.append(obj)
    else:
        sample_rows.append({"value": obj})
    byte_ranges.append((line_start, line_len))


def _validate(raw: bytes, line_index: int, line_start: int) -> str | None:
    """Return a human-readable reason if a line is not valid JSON, else None."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError as err:
        return f"line {line_index} (offset {line_start}): {err.msg}"
    return None


def _infer_columns(sample_rows: list[dict[str, Any]]) -> list[str] | None:
    """Union of keys across the sample, in first-seen order."""
    if not sample_rows:
        return None
    seen: dict[str, None] = {}
    for row in sample_rows:
        for k in row:
            seen.setdefault(k, None)
    return list(seen)
