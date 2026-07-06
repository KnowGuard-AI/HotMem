"""HotMem file inspectors — lightweight, read-only, no query engine (issue #53).

Purpose:
    Understand *about* large local files (CSV, JSONL, Parquet) enough to
    reference, hydrate ranges, and expose provenance — without becoming
    DuckDB, Polars, Arrow, or a data lake, and without copying large file
    contents into SQLite.

Interface:
    inspect_file(uri, *, count_rows=False, sample_size=5) -> FileInspection
    get_inspector(uri) -> FileInspector
    UnsupportedFormatError — raised when no inspector handles a format

Deps: hotmem.storage, hotmem.inspectors.{csv,jsonl,parquet}
Extension: register a new inspector by format name in INSPECTORS below.
"""

from __future__ import annotations

from hotmem.storage import UnsupportedSchemeError

from .base import FileInspection, FileInspector, resolve_adapter
from .csv_inspector import CSVInspector
from .jsonl_inspector import JSONLInspector
from .parquet_inspector import ParquetInspector

__all__ = [
    "FileInspector",
    "FileInspection",
    "CSVInspector",
    "JSONLInspector",
    "ParquetInspector",
    "UnsupportedFormatError",
    "inspect_file",
    "get_inspector",
]

INSPECTORS: dict[str, FileInspector] = {
    "csv": CSVInspector(),
    "jsonl": JSONLInspector(),
    "parquet": ParquetInspector(),
}


class UnsupportedFormatError(ValueError):
    """Raised when a backing file's format has no HotMem inspector.

    Mirrors hotmem.storage.UnsupportedSchemeError so the two failure modes
    feel symmetrical to callers. Analytical execution (DuckDB/Polars/Arrow
    query) is owned by EMOS, not HotMem.
    """


def get_inspector(uri: str) -> FileInspector:
    """Return the inspector for ``uri``'s format, or raise.

    Resolves the storage adapter first so remote/unsupported schemes fail fast
    with the existing EMOS-boundary UnsupportedSchemeError before we look at
    format.
    """
    _, meta = resolve_adapter(uri)
    fmt = meta["format"]
    inspector = INSPECTORS.get(fmt)
    if inspector is None:
        raise UnsupportedFormatError(
            f"no inspector for format {fmt!r}; "
            "analytical execution (DuckDB/Polars/Arrow) is owned by EMOS, not HotMem"
        )
    return inspector


def inspect_file(
    uri: str,
    *,
    count_rows: bool = False,
    sample_size: int = 5,
) -> FileInspection:
    """Inspect a backing file and return provenance + light metadata.

    Never copies large file contents into SQLite; the inspectors stream or
    read only the file's metadata/footer. ``UnsupportedSchemeError`` is
    raised for remote schemes; ``UnsupportedFormatError`` for unknown
    formats; otherwise a FileInspection (which may carry
    ``unsupported_reason`` for a recognized-but-malformed file).
    """
    try:
        adapter, meta = resolve_adapter(uri)
    except UnsupportedSchemeError:
        raise

    fmt = meta["format"]
    inspector = INSPECTORS.get(fmt)
    if inspector is None:
        raise UnsupportedFormatError(
            f"no inspector for format {fmt!r}; "
            "analytical execution (DuckDB/Polars/Arrow) is owned by EMOS, not HotMem"
        )
    return inspector.inspect(uri, adapter, meta, count_rows=count_rows, sample_size=sample_size)
