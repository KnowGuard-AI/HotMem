"""Tests for hotmem.cli — rich CLI output (ticket #16).

All assertions target the PlainRenderer path (NO_COLOR=1, non-TTY) so output
is deterministic in CI/headless environments.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import closing, contextmanager
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


def test_ingestion_commands_share_the_embedder_config(tmp_path: Path):
    """#78 review: hydrate / delta apply / import accept the shared embedder
    configuration and fail fast before touching data."""
    runner = CliRunner()
    for args in (["hydrate", "--help"], ["delta", "apply", "--help"], ["import", "--help"]):
        result = runner.invoke(main, args)
        assert result.exit_code == 0, result.output
        assert "--embedder" in result.output
        assert "--embedder-model-path" in result.output

    # Invalid selection resolves before the DB is opened: actionable error.
    result = runner.invoke(
        main,
        [
            "hydrate",
            "--file",
            str(tmp_path / "missing.jsonl"),
            "--db",
            str(tmp_path / "x.sqlite"),
            "--embedder",
            "local-semantic",
        ],
    )
    assert result.exit_code != 0
    message = result.output + str(result.exception or "")
    assert "never downloads" in message or "[semantic]" in message
    assert not (tmp_path / "x.sqlite").exists()  # failed before opening the DB


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


# ── inspect ────────────────────────────────────────────────────────────


def test_inspect_csv_json_output(tmp_path: Path):
    path = tmp_path / "data.csv"
    path.write_text("id,name\n1,alice\n2,bob\n")
    result = CliRunner().invoke(main, ["inspect", str(path), "--json", "--count-rows"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["format"] == "csv"
    assert data["columns"] == ["id", "name"]
    assert data["row_count"] == 2
    assert len(data["checksum"]) == 64


def test_inspect_csv_plain_output(tmp_path: Path):
    path = tmp_path / "data.csv"
    path.write_text("id,name\n1,alice\n2,bob\n")
    result = CliRunner().invoke(main, ["inspect", str(path), "--count-rows"])
    assert result.exit_code == 0, result.output
    assert "format=csv" in result.output
    assert "columns: id, name" in result.output


def test_inspect_unsupported_format_errors(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("just prose\n")
    result = CliRunner().invoke(main, ["inspect", str(path)])
    assert result.exit_code != 0
    assert "not supported by HotMem" in result.output


def test_inspect_remote_scheme_errors():
    result = CliRunner().invoke(main, ["inspect", "s3://bucket/key.csv"])
    assert result.exit_code != 0
    assert "only local filesystem" in result.output


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


# Real installed entry points, sockets, persistence, and file handles (#118).
def _installed_cli(*args: str) -> list[str]:
    executable = shutil.which("hotmem", path=str(Path(sys.executable).parent))
    assert executable, "the installed hotmem entry point must be available"
    return [executable, *args]


def _run_cli(*args: str, success: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        _installed_cli(*args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    return result


@contextmanager
def _sidecar(mount: Path):
    """Bound startup and teardown; log to a file so no pipe can fill."""
    import httpx

    from hotmem.client import HotMemClient

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log_path = mount.parent / f"{mount.name}.log"
    with log_path.open("w+b") as log:
        proc = subprocess.Popen(
            _installed_cli(
                "serve",
                "--mount",
                str(mount),
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
                "--embedder",
                "hash",
                "--reranker",
                "none",
            ),
            stdout=log,
            stderr=log,
        )
        try:
            with HotMemClient(f"http://127.0.0.1:{port}") as client:
                deadline = time.monotonic() + 15
                while True:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        pytest.fail(f"sidecar did not start: {log.read().decode('utf-8')}")
                    try:
                        if client.health()["status"] == "ok":
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                yield client, proc
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            assert proc.poll() is not None


def test_installed_cli_http_restart_and_verified_restore(tmp_path: Path):
    """Real wheel entry point, acknowledged-write recovery, and clean restore."""
    import hashlib

    import httpx

    from hotmem.snapshot.reader import verify_manifest

    workspace = tmp_path / "workspace caf\u00e9 with spaces"
    workspace.mkdir()
    mount = workspace / "source"
    snapshot = workspace / "verified snapshot"
    facts = ["Invoice caf\u00e9 approval\nnext line\r\nlast line", "Payment terms are net 30"]
    assert "0.2.6" in _run_cli("--version").stdout
    with _sidecar(mount) as (client, proc):
        for fact in facts:
            client.add("windows-test", fact, source="synthetic-fixture", metadata={"run": "118"})
        before = client.search("invoice approval", top_k=2)
        assert len(before) == 2
        assert all(row["role"] == "system" for row in before)
        assert client.snapshot(str(snapshot))["exported"] == 2
        # Windows terminate() is an abrupt stop: acknowledged writes must survive.
        proc.kill()
        proc.wait(timeout=10)
    with _sidecar(mount) as (client, _):
        assert client.health()["memory_count"] == 2
        rows = client.list("windows-test")
        assert {row["fact_text"] for row in rows} == set(facts)
        assert {row["source"] for row in rows} == {"synthetic-fixture"}
        assert client.search("invoice approval", top_k=2) == before
        assert "ok" in _run_cli("status", "--port", client.base_url.rsplit(":", 1)[1]).stdout

    verify_manifest(snapshot)
    payload = (snapshot / "memories.jsonl").read_bytes()
    assert b"\r\n" not in payload  # embedded CRLF remains escaped JSON content
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["memories.jsonl"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["files"]["memories.jsonl"]["size"] == len(payload)
    clone = workspace / "clone"
    with _sidecar(clone) as (client, _):
        restored = client.hydrate(str(snapshot))
        assert restored["loaded"] == restored["embedding_reused"] == 2
        assert restored["embedding_rebuilt"] == restored["invalid"] == 0
        assert client.search("invoice approval", top_k=2) == before
        repeated = client.hydrate(str(snapshot))
        assert repeated["loaded"] == 0 and repeated["skipped_dupes"] == 2
        (snapshot / "memories.jsonl").write_bytes(payload + b"corruption")
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.hydrate(str(snapshot))
        assert error.value.response.status_code == 409
        assert client.health()["memory_count"] == 2
    # Windows rejects this removal if any process/SQLite handle was left open.
    shutil.rmtree(workspace)
    assert not workspace.exists()


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_installed_cli_legacy_restore_and_failure_cleanup(tmp_path: Path, suffix: str):
    """Offline CLI round trips and failed verification release all DB/file handles."""
    workspace = tmp_path / "portable caf\u00e9 workspace"
    workspace.mkdir()
    source = workspace / "source.sqlite"
    with closing(MemoryDB(source)) as db:
        from hotmem.swap import add_memory

        add_memory(db, "portable", "Legacy caf\u00e9\nline two", source="synthetic")
    artifact = workspace / f"legacy snapshot{suffix}"
    target = workspace / "target.sqlite"
    _run_cli("snapshot", "--db", str(source), "--file", str(artifact))
    first = _run_cli("hydrate", "--db", str(target), "--file", str(artifact))
    assert "loaded=1" in first.stdout and "embedding_reused=1" in first.stdout
    assert (
        "skipped_dupes=1"
        in _run_cli("hydrate", "--db", str(target), "--file", str(artifact)).stdout
    )
    assert json.loads(_run_cli("search", "Legacy", "--db", str(target), "--json").stdout)
    snapshot = workspace / "corrupt snapshot"
    _run_cli("snapshot", "--db", str(source), "--file", str(snapshot))
    with (snapshot / "memories.jsonl").open("ab") as handle:
        handle.write(b"corruption")
    failure = _run_cli("hydrate", "--db", str(target), "--file", str(snapshot), success=False)
    assert "verification" in (failure.stdout + failure.stderr).lower()
    with closing(MemoryDB(target)) as db:
        assert db.count() == 1
    shutil.rmtree(workspace)
    assert not workspace.exists()


def test_installed_http_file_reference_ranges_and_relocation(tmp_path: Path):
    """Reference-only snapshots need explicit backing-file relocation."""
    import hashlib

    import httpx

    source = tmp_path / "source mount"
    source.mkdir()
    name = "caf\u00e9 data 100%.txt"
    data = b"header\r\nverified payload\r\ntrailer"
    (source / name).write_bytes(data)
    offset, length = 8, 16
    expected = data[offset : offset + length]
    snapshot = tmp_path / "reference snapshot"
    with _sidecar(source) as (client, _):
        checksum = hashlib.sha256(expected).hexdigest()
        record = client.add_file_ref(
            "file-ref",
            name,
            byte_offset=offset,
            byte_length=length,
            source_checksum=checksum,
            summary="Synthetic range reference",
        )
        mid = record["memory_id"]
        assert client.hydrate_memory(mid) == expected
        absolute = client.add_file_ref(
            "uri-ref",
            (source / name).as_uri(),
            byte_offset=offset,
            byte_length=length,
            source_checksum=checksum,
        )
        assert client.hydrate_memory(absolute["memory_id"]) == expected
        client.snapshot(str(snapshot))
    assert not (snapshot / name).exists()  # Snapshot v2 does not transport source files.
    target = tmp_path / "relocated mount"
    with _sidecar(target) as (client, _):
        assert client.hydrate(str(snapshot))["loaded"] == 2
        with pytest.raises(httpx.HTTPStatusError) as missing:
            client.hydrate_memory(mid)
        assert missing.value.response.status_code == 409
        assert missing.value.response.json()["reason"] == "missing_file"
        (target / name).write_bytes(data)
        assert client.hydrate_memory(mid) == expected
        (target / name).write_bytes(b"x" * len(data))
        with pytest.raises(httpx.HTTPStatusError) as mismatch:
            client.hydrate_memory(mid)
        assert mismatch.value.response.status_code == 409
    shutil.rmtree(source)
    shutil.rmtree(target)


def test_installed_server_bind_failure_releases_mount(tmp_path: Path):
    """A failed startup exits within the CLI deadline and leaves no locked store."""
    mount = tmp_path / "failed startup mount"
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        result = _run_cli(
            "serve",
            "--mount",
            str(mount),
            "--host",
            "127.0.0.1",
            "--port",
            str(occupied.getsockname()[1]),
            success=False,
        )
        assert "error" in result.stderr.lower()
    with _sidecar(mount) as (client, _):
        assert client.health()["memory_count"] == 0
    shutil.rmtree(mount)
    assert not mount.exists()
