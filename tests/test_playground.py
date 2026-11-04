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


def test_http_backend_disconnect_and_reconnect_state_machine(monkeypatch):
    import httpx

    from hotmem.playground import _HttpBackend

    backend = _HttpBackend(
        "http://127.0.0.1:8711",
        token="tok-mgmt",
        session_id="sess-mgmt",
        harness_id="hotmem-console",
        source="ui://mgmt",
        expected_store_id="store-112",
    )
    try:
        responses = [
            {
                "status": "ok",
                "memory_count": 1,
                "store_id": "store-112",
                "mount_id": "mnt_112",
                "store_state": "ready",
                "runtime_generation": "gen-1",
                "policy_enforced": True,
                "policy_revision": 1,
                "observed_at": "2026-11-04T14:00:00+00:00",
                "freshness_model": "poll_and_event_observed",
            },
            httpx.ConnectError("connection refused"),
            {
                "status": "ok",
                "memory_count": 1,
                "store_id": "store-112",
                "mount_id": "mnt_112",
                "store_state": "ready",
                "runtime_generation": "gen-2",
                "policy_enforced": True,
                "policy_revision": 1,
                "observed_at": "2026-11-04T14:00:05+00:00",
                "freshness_model": "poll_and_event_observed",
            },
        ]

        def fake_health():
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(backend._client, "health", fake_health)

        snap1 = backend.status_snapshot()
        assert snap1["connection_state"] == "connected"
        assert snap1["generation_changed"] is False

        snap_disc = backend.status_snapshot()
        assert snap_disc["connection_state"] == "disconnected"
        assert snap_disc["status"] == "disconnected"
        assert snap_disc["store_id"] == "store-112"
        assert snap_disc["runtime_generation"] == "gen-1"
        assert snap_disc["stale_age_s"] is not None
        assert "ConnectError" in snap_disc["last_error"]

        snap_rec = backend.status_snapshot()
        assert snap_rec["connection_state"] == "reconnected"
        assert snap_rec["generation_changed"] is True
        assert snap_rec["runtime_generation"] == "gen-2"
    finally:
        backend.close()
