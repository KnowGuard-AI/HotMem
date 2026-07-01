"""Tests for hotmem.playground — interactive TUI backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.playground import _DirectBackend


def _fresh_db(tmp_path: Path) -> str:
    return str(tmp_path / "playground.sqlite")


def test_direct_backend_add_and_search(tmp_path):
    backend = _DirectBackend(_fresh_db(tmp_path))
    try:
        result = backend.add("user", "prefers dark mode")
        assert "memory_id" in result
        assert "content_hash" in result
        assert backend.count() == 1

        results = backend.search("theme preference")
        assert len(results) >= 1
        assert results[0]["identifier"] == "user"
        assert "score" in results[0]
    finally:
        backend.close()


def test_direct_backend_multiple_memories(tmp_path):
    backend = _DirectBackend(_fresh_db(tmp_path))
    try:
        backend.add("project", "uses FastAPI and SQLite")
        backend.add("project", "deployed with Docker")
        assert backend.count() == 2

        results = backend.search("deployment")
        assert len(results) >= 1
    finally:
        backend.close()


def test_run_playground_requires_db_or_url():
    from hotmem.playground import run_playground

    with pytest.raises(ValueError, match="specify either"):
        run_playground()


def test_run_playground_rejects_both_db_and_url(tmp_path):
    from hotmem.playground import run_playground

    with pytest.raises(ValueError, match="not both"):
        run_playground(db_path=_fresh_db(tmp_path), url="http://localhost:8711")
