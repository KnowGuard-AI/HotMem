"""CSV inspector — streaming header/delimiter detection, no full-file load.

Scope (#53):
    - Detect headers and delimiter basics.
    - Report size (from storage metadata) and columns.
    - Optional row count when ``count_rows=True`` (streaming newline scan).
    - Bounded sample of the first ``sample_size`` data rows.
    - Never copy file contents into SQLite; never load the whole file into RAM.
"""

from __future__ import annotations

import csv
import io
from itertools import islice
from pathlib import Path
from typing import Any

from hotmem.storage import StorageAdapter, StorageMetadata

from .base import FileInspection

_SNIFF_BYTES = 8192
_DEFAULT_DELIMITER = ","


class CSVInspector:
    """Inspect a CSV file's structure with O(head buffer) memory."""

    def inspect(
        self,
        uri: str,
        adapter: StorageAdapter,
        meta: StorageMetadata,
        *,
        count_rows: bool = False,
        sample_size: int = 5,
    ) -> FileInspection:
        head = adapter.read_range(uri, 0, min(_SNIFF_BYTES, meta["size"] or 0))
        text = head.decode("utf-8", errors="replace")

        delimiter, has_header, columns = self._sniff(text)

        sample_rows, byte_ranges, row_count = _scan(
            uri, columns, delimiter, has_header, count_rows, sample_size
        )

        return FileInspection(
            uri=uri,
            format="csv",
            size=meta["size"],
            mtime=meta["mtime"],
            checksum=adapter.checksum(uri),
            columns=columns,
            row_count=row_count,
            delimiter=delimiter,
            has_header=has_header,
            sample=sample_rows or None,
            byte_ranges=byte_ranges or None,
            metadata={},
        )

    @staticmethod
    def _sniff(head_text: str) -> tuple[str, bool, list[str]]:
        """Best-effort delimiter + header detection from a head buffer."""
        sample = io.StringIO(head_text)
        try:
            dialect = csv.Sniffer().sniff(head_text, delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = _DEFAULT_DELIMITER

        sample.seek(0)
        reader = csv.reader(sample, delimiter=delimiter)
        rows = [row for row in islice(reader, 3) if row]

        if not rows:
            return delimiter, None, []

        first = rows[0]
        has_header = _detect_header(head_text, delimiter, first, rows[1] if len(rows) > 1 else None)

        if has_header:
            columns = [c.strip() for c in first]
        else:
            columns = [f"col_{i}" for i in range(len(first))]
        return delimiter, has_header, columns


def _detect_header(
    head_text: str,
    delimiter: str,
    first: list[str],
    second: list[str] | None,
) -> bool:
    """Detect whether the first row is a header row.

    Prefers csv.Sniffer.has_header; falls back to a cheap type-based heuristic
    so a single data row or a numeric-only file still classifies correctly.
    """
    try:
        if csv.Sniffer().has_header(head_text):
            return True
    except csv.Error:
        pass
    # Fallback: a header row has no empty cells and is not all-numeric, while a
    # following data row either is numeric or has a different shape.
    if any(not c.strip() for c in first):
        return False
    if _all_numeric(first):
        return False
    if second is None:
        return True
    return True


def _all_numeric(row: list[str]) -> bool:
    if not row:
        return False
    for c in row:
        try:
            float(c.strip())
        except ValueError:
            return False
    return True


def _resolve(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[len("file://"):])
    return Path(uri)


def _scan(
    uri: str,
    columns: list[str],
    delimiter: str,
    has_header: bool | None,
    count_rows: bool,
    sample_size: int,
) -> tuple[list[dict[str, Any]], list[tuple[int, int]], int | None]:
    """Stream the file once to collect a bounded sample and optional row count.

    Memory is O(sample_size rows); the file is read in a single streaming pass.
    Byte ranges for sampled rows are recorded for provenance (#38 enabler).
    """
    path = _resolve(uri)
    sample_rows: list[dict[str, Any]] = []
    byte_ranges: list[tuple[int, int]] = []
    row_count = 0 if count_rows else None
    collected = 0
    offset = 0

    with open(path, "rb") as f:
        line_iter = iter(f)
        # Skip the header line from sampling/counting if one was detected.
        if has_header:
            header_line = next(line_iter, b"")
            offset += len(header_line)

        for raw in line_iter:
            line_len = len(raw)
            if collected < sample_size:
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    cells = next(csv.reader([text], delimiter=delimiter))
                    row_dict: dict[str, Any] = {}
                    for i, val in enumerate(cells):
                        key = columns[i] if i < len(columns) else f"col_{i}"
                        row_dict[key] = val
                    sample_rows.append(row_dict)
                    byte_ranges.append((offset, line_len))
                    collected += 1
            if count_rows and raw.strip():
                row_count = (row_count or 0) + 1
            offset += line_len

    return sample_rows, byte_ranges, row_count
