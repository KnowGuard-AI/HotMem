"""Tests for hotmem.ui — renderer factory + Plain/Rich behaviour."""

from __future__ import annotations

import os
from unittest import mock

import pytest

import hotmem.ui as ui


@pytest.fixture
def force_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the PlainRenderer for tests that need deterministic output."""
    monkeypatch.setattr(ui, "_use_rich", lambda: False)


def test_factory_returns_plain_when_not_tty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ui.sys, "stdout", mock.Mock(isatty=lambda: False))
    r = ui.get_renderer()
    assert isinstance(r, ui.PlainRenderer)


def test_factory_returns_plain_when_no_color(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ui.sys, "stdout", mock.Mock(isatty=lambda: True))
    monkeypatch.setattr(os, "environ", {"NO_COLOR": "1"})
    r = ui.get_renderer()
    assert isinstance(r, ui.PlainRenderer)


def test_factory_returns_plain_when_term_dumb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ui.sys, "stdout", mock.Mock(isatty=lambda: True))
    monkeypatch.setattr(os, "environ", {"TERM": "dumb"})
    r = ui.get_renderer()
    assert isinstance(r, ui.PlainRenderer)


def test_factory_returns_plain_when_rich_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ui.sys, "stdout", mock.Mock(isatty=lambda: True))
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(ui, "_rich_available", lambda: False)
    r = ui.get_renderer()
    assert isinstance(r, ui.PlainRenderer)


def test_plain_status_emits_all_keys(capsys: pytest.CaptureFixture[str], force_plain):
    ui.get_renderer().status(
        {"status": "ok", "memory_count": 7, "db_path": "/tmp/x.sqlite", "uptime_s": 12.3}
    )
    out = capsys.readouterr().out
    assert "Status" in out
    assert "ok" in out
    assert "Memory Count" in out
    assert "7" in out
    assert "/tmp/x.sqlite" in out
    assert "12.3" in out


def test_plain_status_skips_missing_keys(capsys: pytest.CaptureFixture[str], force_plain):
    ui.get_renderer().status({"status": "ok"})
    out = capsys.readouterr().out
    assert "Status" in out
    assert "Memory Count" not in out


def test_plain_search_results_numbered(capsys: pytest.CaptureFixture[str], force_plain):
    rows = [
        {"score": 0.9, "identifier": "a", "content": "fact one"},
        {"score": 0.5, "identifier": "b", "content": "fact two"},
    ]
    ui.get_renderer().search_results(rows)
    out = capsys.readouterr().out
    assert "1." in out and "0.9" in out and "fact one" in out
    assert "2." in out and "fact two" in out


def test_plain_search_results_empty(capsys: pytest.CaptureFixture[str], force_plain):
    ui.get_renderer().search_results([])
    out = capsys.readouterr().out
    assert "No memories" in out


def test_plain_summary(capsys: pytest.CaptureFixture[str], force_plain):
    ui.get_renderer().summary("hydrate", loaded=3, skipped_dupes=1)
    out = capsys.readouterr().out
    assert "hydrate" in out and "loaded=3" in out and "skipped_dupes=1" in out


def test_plain_progress_silent(capsys: pytest.CaptureFixture[str], force_plain):
    with ui.get_renderer().progress(total=100, desc="x") as tick:
        for _ in range(100):
            tick(1)
    out = capsys.readouterr().out
    assert out == ""


def test_rich_renderer_status_outputs_to_console(monkeypatch: pytest.MonkeyPatch):
    # RichRenderer is exercised in CI via the dev extra; skip if not installed.
    pytest.importorskip("rich")
    r = ui.RichRenderer()
    # Should not raise; output goes to the rich console (captured by capsys-ish).
    r.status({"status": "ok", "memory_count": 1, "db_path": "p", "uptime_s": 0.1})


def test_rich_renderer_search_results(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("rich")
    r = ui.RichRenderer()
    r.search_results([{"score": 0.9, "identifier": "a", "content": "x"}])
    r.search_results([])


def test_rich_progress_contextmanager():
    pytest.importorskip("rich")
    r = ui.RichRenderer()
    with r.progress(total=10, desc="test") as tick:
        for _ in range(10):
            tick(1)


def test_rich_progress_indeterminate_total_none():
    pytest.importorskip("rich")
    r = ui.RichRenderer()
    with r.progress(total=None, desc="reading") as tick:
        tick(1)
        tick(1)


def test_plain_status_renders_scoped_daemon_management_view(
    capsys: pytest.CaptureFixture[str], force_plain
):
    payload = {
        "status": "ok",
        "memory_count": 2,
        "db_path": None,
        "uptime_s": 5.4,
        "policy_enforced": True,
        "policy_revision": 3,
        "store_id": "store-inc-112",
        "mount_id": "mnt_abc123",
        "store_state": "ready",
        "total_memory_count": 3,
        "runtime_generation": "gen-112",
        "observed_at": "2026-11-04T14:00:00+00:00",
        "freshness_model": "poll_and_event_observed",
        "permission_state": {
            "caller_id": "caller-mgmt",
            "is_management": True,
            "revoked": False,
            "namespaces": ["ops/shared"],
            "operations": ["read"],
            "active_share_approvals": [
                {
                    "approval_id": "share-inc-112",
                    "namespace": "ops/shared",
                    "identifiers": ["ops/incident-112/decision"],
                    "operations": ["read"],
                    "audience": ["caller-b"],
                }
            ],
            "active_caller_ids": ["caller-a", "caller-b", "caller-mgmt"],
            "revoked_caller_ids": ["caller-old"],
            "revoked_share_approvals": ["share-expired"],
        },
        "observed_clients": [
            {
                "caller_id": "caller-a",
                "harness_id": "codex-cli",
                "session_id": "sess-codex-1",
                "source": "mcp://codex-a",
                "namespace": "ops/shared",
                "last_event_seq": 2,
                "last_event_type": "memory.created",
                "last_observed_at": "2026-11-04T13:59:59+00:00",
                "observed_event_count": 2,
                "observation_basis": "recorded_events",
            }
        ],
        "recent_activity": {
            "observation_basis": "recorded_events",
            "latest_seq": 2,
            "count": 1,
            "events": [
                {
                    "seq": 2,
                    "event_type": "memory.created",
                    "identifier": "ops/incident-112/decision",
                    "namespace": "ops/shared",
                    "caller_id": "caller-a",
                    "harness_id": "codex-cli",
                    "session_id": "sess-codex-1",
                }
            ],
        },
    }
    ui.get_renderer().status(payload)
    out = capsys.readouterr().out
    assert "Connection: connected (freshness=poll_and_event_observed" in out
    assert "Store: store-inc-112 (mount_id=mnt_abc123, state=ready, generation=gen-112)" in out
    assert "Total memory count: 3" in out
    assert "Permissions: caller=caller-mgmt management=True policy_revision=3" in out
    assert "Policy state: active_callers=[caller-a, caller-b, caller-mgmt]" in out
    assert "Observed clients (observation_basis=recorded_events):" in out
    assert "caller=caller-a harness=codex-cli session=sess-codex-1" in out
    assert "Permitted activity (observation_basis=recorded_events, latest_seq=2):" in out
    assert "seq=2 memory.created id=ops/incident-112/decision" in out
