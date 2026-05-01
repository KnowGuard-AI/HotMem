"""Shared fixtures for HotMem tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.db import MemoryDB


@pytest.fixture
def tmp_db(tmp_path: Path) -> MemoryDB:
    """Provide a fresh in-memory-like SQLite DB for each test."""
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
