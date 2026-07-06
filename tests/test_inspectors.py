"""Tests for hotmem.inspectors — CSV, JSONL, Parquet, registry, error paths.

Covers issue #53 acceptance criteria:
  - CSV fixture inspection returns stable metadata.
  - JSONL fixture inspection streams/samples without full ingestion.
  - Parquet-like fixture returns metadata only or an explicit unsupported error.
  - Inspectors do not copy large file contents into SQLite.
  - Existing hydrate/search behavior remains unchanged (guarded by golden tests).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _parquet_fixtures import write_parquet_fixture

from hotmem.inspectors import (
    UnsupportedFormatError,
    inspect_file,
)
from hotmem.storage import UnsupportedSchemeError

# ── CSV ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "invoices.csv"
    path.write_text("id,vendor,amount\n1,Acme,5000\n2,Globex,3000\n3,Initech,7500\n")
    return path


def test_csv_inspection_returns_stable_metadata(csv_file):
    insp = inspect_file(str(csv_file), count_rows=True, sample_size=2)
    assert insp.format == "csv"
    assert insp.uri == str(csv_file)
    assert insp.columns == ["id", "vendor", "amount"]
    assert insp.has_header is True
    assert insp.delimiter == ","
    assert insp.row_count == 3
    assert insp.size == csv_file.stat().st_size
    assert isinstance(insp.mtime, float)
    assert len(insp.checksum) == 64
    assert insp.sample is not None and len(insp.sample) == 2
    assert insp.sample[0] == {"id": "1", "vendor": "Acme", "amount": "5000"}
    assert insp.byte_ranges is not None and len(insp.byte_ranges) == 2


def test_csv_row_count_optional_by_default(csv_file):
    insp = inspect_file(str(csv_file))
    assert insp.row_count is None
    # Sample still works without counting (capped by available rows).
    assert insp.sample is not None and len(insp.sample) == 3


def test_csv_semicolon_delimiter(tmp_path):
    path = tmp_path / "semi.csv"
    path.write_text("a;b\n1;2\n3;4\n")
    insp = inspect_file(str(path))
    assert insp.delimiter == ";"
    assert insp.columns == ["a", "b"]


def test_csv_file_uri_scheme(csv_file):
    insp = inspect_file(f"file://{csv_file}", count_rows=True)
    assert insp.format == "csv"
    assert insp.columns == ["id", "vendor", "amount"]


# ── JSONL ────────────────────────────────────────────────────────────────────


@pytest.fixture
def jsonl_file(tmp_path: Path) -> Path:
    path = tmp_path / "records.jsonl"
    lines = [
        {"id": 1, "name": "alice"},
        {"id": 2, "name": "bob"},
        {"id": 3, "name": "carol"},
        {"id": 4, "name": "dave"},
        {"id": 5, "name": "eve"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    return path


def test_jsonl_inspection_streams_and_samples(jsonl_file):
    insp = inspect_file(str(jsonl_file), count_rows=True, sample_size=3)
    assert insp.format == "jsonl"
    assert insp.row_count == 5
    assert insp.columns == ["id", "name"]
    assert insp.sample is not None and len(insp.sample) == 3
    assert insp.sample[0] == {"id": 1, "name": "alice"}
    assert insp.byte_ranges is not None and len(insp.byte_ranges) == 3
    # Byte ranges point at real offsets that round-trip back to the record.
    off, length = insp.byte_ranges[0]
    raw = jsonl_file.read_bytes()[off : off + length]
    assert json.loads(raw.decode().strip()) == {"id": 1, "name": "alice"}
    assert insp.unsupported_reason is None


def test_jsonl_malformed_line_reported_not_raised(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": 1}\n{not json}\n{"id": 3}\n')
    insp = inspect_file(str(path), count_rows=True, sample_size=5)
    assert insp.row_count == 3
    assert insp.unsupported_reason is not None
    assert "line 1" in insp.unsupported_reason


def test_jsonl_handles_no_trailing_newline(tmp_path):
    path = tmp_path / "notrail.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}')  # no trailing newline
    insp = inspect_file(str(path), count_rows=True, sample_size=5)
    assert insp.row_count == 2
    assert insp.sample is not None and len(insp.sample) == 2


def test_jsonl_sample_bounded_memory_does_not_load_full_file(tmp_path):
    # A large-ish JSONL file: confirm inspection returns sane metadata without
    # materializing every record into memory (sample_size is the cap).
    path = tmp_path / "big.jsonl"
    with open(path, "w") as f:
        for i in range(1000):
            f.write(json.dumps({"i": i, "blob": "x" * 200}) + "\n")
    insp = inspect_file(str(path), count_rows=True, sample_size=4)
    assert insp.row_count == 1000
    assert insp.sample is not None and len(insp.sample) == 4


# ── Parquet ───────────────────────────────────────────────────────────────────


@pytest.fixture
def parquet_file(tmp_path: Path) -> Path:
    path = tmp_path / "data.parquet"
    write_parquet_fixture(path, num_rows=3, columns=[("id", 1), ("name", 6)])
    return path


def test_parquet_inspection_returns_metadata_only(parquet_file):
    insp = inspect_file(str(parquet_file))
    assert insp.format == "parquet"
    assert insp.row_count == 3
    assert insp.num_row_groups == 1
    assert insp.columns == ["id", "name"]
    assert insp.schema_types == ["INT32", "BYTE_ARRAY"]
    assert insp.metadata.get("version") == 1
    assert insp.unsupported_reason is None
    # Metadata-only: no sample rows are decoded (data pages are not read).
    assert insp.sample is None


def test_parquet_bad_magic_reports_unsupported_reason(tmp_path):
    path = tmp_path / "broken.parquet"
    path.write_bytes(
        b"PAR1" + b"hello world this is not a parquet footer at all" + b"\x00\x00\x00\x00" + b"PAR1"
    )
    insp = inspect_file(str(path))
    assert insp.format == "parquet"
    assert insp.unsupported_reason is not None


def test_parquet_too_small_reports_unsupported_reason(tmp_path):
    path = tmp_path / "tiny.parquet"
    path.write_bytes(b"PAR1")
    insp = inspect_file(str(path))
    assert insp.unsupported_reason is not None
    assert "too small" in insp.unsupported_reason


# ── Registry + error paths ─────────────────────────────────────────────────────


def test_inspect_file_unknown_format_raises(tmp_path):
    path = tmp_path / "data.xlsx"
    path.write_bytes(b"PK\x03\x04not really")
    with pytest.raises(UnsupportedFormatError, match="EMOS"):
        inspect_file(str(path))


def test_inspect_file_remote_scheme_raises(tmp_path):
    with pytest.raises(UnsupportedSchemeError, match="EMOS"):
        inspect_file("s3://bucket/key.csv")


def test_inspect_file_unknown_txt_format(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("just some prose\n")
    with pytest.raises(UnsupportedFormatError):
        inspect_file(str(path))


def test_inspection_to_dict_is_json_serializable(csv_file):
    import json

    insp = inspect_file(str(csv_file), count_rows=True)
    d = insp.to_dict()
    json.dumps(d)  # must not raise
    assert d["format"] == "csv"
    assert d["row_count"] == 3


# ── Non-regression: inspectors don't touch SQLite ─────────────────────────────


def test_inspect_file_does_not_create_a_database(csv_file, tmp_path):
    # Inspection is read-only and side-effect free: no .sqlite files appear.
    before = {p for p in tmp_path.rglob("*") if p.suffix in (".sqlite", ".db")}
    inspect_file(str(csv_file), count_rows=True)
    after = {p for p in tmp_path.rglob("*") if p.suffix in (".sqlite", ".db")}
    assert before == after
