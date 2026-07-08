"""Shared fixtures for HotMem tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.storage import LocalFilesystemAdapter


@pytest.fixture
def tmp_db(tmp_path: Path) -> MemoryDB:
    """Provide a fresh SQLite DB for each test."""
    db_path = tmp_path / "test.sqlite"
    db = MemoryDB(db_path)
    yield db
    db.close()


@pytest.fixture
def tmp_mount(tmp_path: Path) -> Path:
    """Provide a temporary mount directory."""
    mount = tmp_path / "mount"
    mount.mkdir()
    return mount


@pytest.fixture
def fixture_file(tmp_path: Path) -> Path:
    """A deterministic binary fixture file (1024 bytes) for range reads."""
    p = tmp_path / "data.bin"
    p.write_bytes(bytes(range(256)) * 4)
    return p


@pytest.fixture
def spy_adapter(fixture_file: Path):
    """A SpyAdapter wrapping main's LocalFilesystemAdapter, rooted at the fixture dir."""
    from spy import SpyAdapter

    # Main's LocalFilesystemAdapter resolves bare paths against CWD, so we
    # give it absolute paths via the fixture's parent as base_dir in tests.
    return SpyAdapter(LocalFilesystemAdapter())


@pytest.fixture
def app_client(tmp_path: Path, fixture_file: Path):
    """A FastAPI TestClient with storage rooted at the fixture file's dir."""
    from fastapi.testclient import TestClient

    from hotmem.server import create_app

    # Copy the fixture into the mount dir so relative URIs resolve there.
    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / fixture_file.name
    target.write_bytes(fixture_file.read_bytes())

    db_path = mount / "hotmem.sqlite"
    app = create_app(db_path=db_path, base_dir=mount)
    with TestClient(app) as c:
        c.mount_dir = mount  # type: ignore[attr-defined]
        c.fixture_path = target  # type: ignore[attr-defined]
        yield c
