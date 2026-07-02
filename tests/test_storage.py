"""Tests for hotmem.storage — adapter abstraction and local FS impl."""

from __future__ import annotations

import hashlib

import pytest

from hotmem.storage import (
    LocalFilesystemAdapter,
    UnsupportedSchemeError,
    get_adapter,
)


@pytest.fixture
def adapter() -> LocalFilesystemAdapter:
    return LocalFilesystemAdapter()


@pytest.fixture
def data_file(tmp_path):
    path = tmp_path / "data.txt"
    path.write_bytes(b"0123456789abcdef")
    return path


def _uri(path) -> str:
    return f"file://{path}"


def test_read_full(adapter, data_file):
    assert adapter.read(_uri(data_file)) == b"0123456789abcdef"
    assert adapter.read(str(data_file)) == b"0123456789abcdef"


def test_read_range_matches_slice(adapter, data_file):
    uri = _uri(data_file)
    assert adapter.read_range(uri, 4, 8) == b"456789ab"
    assert adapter.read_range(uri, 0, 4) == b"0123"
    assert adapter.read_range(uri, 12, 4) == b"cdef"


def test_read_range_does_not_blow_past_eof(adapter, data_file):
    assert adapter.read_range(_uri(data_file), 14, 100) == b"ef"
    assert adapter.read_range(_uri(data_file), 16, 4) == b""


def test_read_range_rejects_negative(adapter, data_file):
    with pytest.raises(ValueError):
        adapter.read_range(_uri(data_file), -1, 4)
    with pytest.raises(ValueError):
        adapter.read_range(_uri(data_file), 0, -1)


def test_exists(adapter, data_file, tmp_path):
    assert adapter.exists(_uri(data_file))
    assert adapter.exists(str(data_file))
    assert not adapter.exists(str(tmp_path / "missing.txt"))


def test_metadata(adapter, data_file):
    md = adapter.metadata(_uri(data_file))
    assert md["uri"] == _uri(data_file)
    assert md["size"] == 16
    assert md["format"] == "txt"
    assert isinstance(md["mtime"], float)


def test_checksum_is_sha256_and_stable(adapter, data_file):
    expected = hashlib.sha256(data_file.read_bytes()).hexdigest()
    assert adapter.checksum(_uri(data_file)) == expected
    assert adapter.checksum(str(data_file)) == expected
    assert adapter.checksum(_uri(data_file)) == adapter.checksum(str(data_file))


def test_checksum_distinct_for_different_files(adapter, tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert adapter.checksum(str(a)) != adapter.checksum(str(b))


def test_get_adapter_resolves_local_schemes(data_file):
    assert isinstance(get_adapter(_uri(data_file)), LocalFilesystemAdapter)
    assert isinstance(get_adapter(str(data_file)), LocalFilesystemAdapter)


def test_get_adapter_rejects_unsupported_scheme():
    with pytest.raises(UnsupportedSchemeError, match="EMOS"):
        get_adapter("s3://bucket/key")


def test_get_adapter_rejects_each_remote_scheme():
    for scheme in ("hdfs", "abfs", "gcs", "gs"):
        with pytest.raises(UnsupportedSchemeError):
            get_adapter(f"{scheme}://x/y")


def test_mmap_read_matches_file(adapter, data_file):
    assert adapter.mmap_read(_uri(data_file)) == b"0123456789abcdef"


def test_adapter_is_runtime_checkable():
    from hotmem.storage.base import StorageAdapter

    assert isinstance(LocalFilesystemAdapter(), StorageAdapter)
