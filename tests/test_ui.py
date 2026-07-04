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
