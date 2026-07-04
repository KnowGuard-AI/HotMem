"""Tests for hotmem.cli — rich CLI output (ticket #16).

All assertions target the PlainRenderer path (NO_COLOR=1, non-TTY) so output
is deterministic in CI/headless environments.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from hotmem.cli import main
from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.ui import PlainRenderer


@pytest.fixture(autouse=True)
def _force_plain(monkeypatch: pytest.MonkeyPatch):
    """Every test in this file runs against the PlainRenderer."""
    import hotmem.ui as ui

    monkeypatch.setattr(ui, "_use_rich", lambda: False)


# ── status ─────────────────────────────────────────────────────────────


def test_status_up_prints_health_fields():
    runner = CliRunner()
    payload = {"status": "ok", "memory_count": 7, "db_path": "/tmp/x.sqlite", "uptime_s": 1.2}
    with mock.patch("httpx.get") as g:
        g.return_value = mock.Mock(json=lambda: payload)
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "ok" in out
    assert "7" in out
    assert "/tmp/x.sqlite" in out
    assert "1.2" in out


def test_status_down_exits_nonzero():
    runner = CliRunner()
    import httpx

    with mock.patch("httpx.get", side_effect=httpx.ConnectError("nope")):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 1
    assert "No HotMem server" in result.output


# ── hydrate ────────────────────────────────────────────────────────────


def _write_swap(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_hydrate_summary_uses_renderer(tmp_path: Path):
    swap = _write_swap(
        tmp_path / "swap.jsonl",
        [
            {"identifier": "a", "fact_text": "fact one"},
            {"identifier": "b", "fact_text": "fact two"},
        ],
    )
    db_path = tmp_path / "db.sqlite"
    runner = CliRunner()
    result = runner.invoke(main, ["hydrate", "--file", str(swap), "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "hydrate" in result.output
    assert "loaded=2" in result.output
    assert "skipped_dupes=0" in result.output


def test_hydrate_progress_runs_without_error_when_piped(tmp_path: Path):
    # Piped (CliRunner) must not raise even though progress is a no-op.
    swap = _write_swap(tmp_path / "s.jsonl", [{"identifier": "x", "fact_text": "y"}])
    runner = CliRunner()
    db_path = str(tmp_path / "d.sqlite")
    result = runner.invoke(main, ["hydrate", "--file", str(swap), "--db", db_path])
    assert result.exit_code == 0


def test_hydrate_gzipped_swap_uses_indeterminate_progress(tmp_path: Path):
    """Gzipped swap uses total=None (compressed size != uncompressed bytes)."""
    import gzip

    swap = tmp_path / "swap.jsonl.gz"
    with gzip.open(swap, "wt") as f:
        f.write('{"identifier": "a", "fact_text": "gz fact one"}\n')
        f.write('{"identifier": "b", "fact_text": "gz fact two"}\n')
    runner = CliRunner()
    db_path = str(tmp_path / "d.sqlite")
    result = runner.invoke(main, ["hydrate", "--file", str(swap), "--db", db_path])
    assert result.exit_code == 0, result.output
    assert "loaded=2" in result.output


# ── snapshot ───────────────────────────────────────────────────────────


def test_snapshot_summary_includes_path(tmp_path: Path):
    db_path = tmp_path / "db.sqlite"
    db = MemoryDB(db_path)
    db.insert(
        id="1",
        identifier="a",
        fact_text="snapshot me",
        embedding=pack_embedding(embed_text("snapshot me")),
        importance=0.5,
    )
    db.close()

    out_path = tmp_path / "out.jsonl"
    runner = CliRunner()
    result = runner.invoke(main, ["snapshot", "--file", str(out_path), "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "snapshot" in result.output
    assert "exported=1" in result.output
    assert str(out_path) in result.output
    assert out_path.exists()


# ── search ─────────────────────────────────────────────────────────────


def _seed_db(db_path: Path):
    db = MemoryDB(db_path)
    for i, text in enumerate(["invoice validation required", "late payment risk", "generic note"]):
        db.insert(
            id=str(i),
            identifier="test",
            fact_text=text,
            embedding=pack_embedding(embed_text(text)),
            importance=0.5,
        )
    db.close()


def test_search_db_backend_renders_results(tmp_path: Path):
    db_path = tmp_path / "db.sqlite"
    _seed_db(db_path)
    runner = CliRunner()
    result = runner.invoke(main, ["search", "invoice", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    # PlainRenderer emits numbered rows; the top hit is the invoice fact.
    assert "1." in result.output
    assert "invoice" in result.output


def test_search_json_flag_bypasses_renderer(tmp_path: Path):
    db_path = tmp_path / "db.sqlite"
    _seed_db(db_path)
    runner = CliRunner()
    result = runner.invoke(main, ["search", "invoice", "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert all("content" in r for r in parsed)


def test_search_requires_db_or_url():
    runner = CliRunner()
    result = runner.invoke(main, ["search", "anything"])
    assert result.exit_code != 0
    assert "requires" in result.output.lower() or "db" in result.output.lower()


def test_search_url_backend(tmp_path: Path):
    runner = CliRunner()
    rows = [{"role": "system", "content": "hit", "identifier": "a", "score": 0.9}]
    fake_client = mock.Mock()
    fake_client.search = mock.Mock(return_value=rows)
    with mock.patch("hotmem.client.HotMemClient", return_value=fake_client):
        result = runner.invoke(main, ["search", "q", "--url", "http://127.0.0.1:8711"])
    assert result.exit_code == 0, result.output
    assert "hit" in result.output


def test_search_rejects_both_db_and_url(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["search", "q", "--db", str(tmp_path / "x.sqlite"), "--url", "http://127.0.0.1:8711"]
    )
    assert result.exit_code != 0
    assert "not both" in result.output.lower()


# ── renderer delegation sanity ──────────────────────────────────────────


def test_cli_status_uses_renderer_directly(monkeypatch: pytest.MonkeyPatch):
    """Ensure status() routes through ui.status rather than raw click.echo."""
    called = {}

    def fake_status(self, data):
        called["data"] = data

    monkeypatch.setattr(PlainRenderer, "status", fake_status)
    with mock.patch("httpx.get") as g:
        g.return_value = mock.Mock(json=lambda: {"status": "ok"})
        res = CliRunner().invoke(main, ["status"])
    assert res.exit_code == 0, res.output
    assert called.get("data") == {"status": "ok"}
