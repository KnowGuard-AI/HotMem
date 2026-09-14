"""Package dispatch surfaces — CLI, API, and format detection (#69)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

from hotmem.cli import main
from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.package import MANIFEST_NAME, write_package
from hotmem.snapshot import detect_format, hydrate, snapshot, verify


def _db_with_rows(path: Path, n: int = 2) -> MemoryDB:
    db = MemoryDB(path)
    for i in range(n):
        fact = f"surface fact {i}"
        db.insert(
            id=f"s{i}",
            identifier=f"surf-{i}",
            fact_text=fact,
            embedding=pack_embedding(embed_text(fact)),
            content_hash=compute_content_hash(f"surf-{i}", fact),
        )
    return db


# ── format detection ─────────────────────────────────────────────────────────


def test_detect_format_distinguishes_package_from_v2(tmp_path: Path):
    db = _db_with_rows(tmp_path / "src.sqlite")

    pkg_dir = tmp_path / "pkg"
    v2_dir = tmp_path / "v2"
    write_package(db, pkg_dir)
    from hotmem.snapshot.writer import write_snapshot_v2

    write_snapshot_v2(db, v2_dir)
    db.close()

    assert detect_format(pkg_dir) == "package"
    assert detect_format(v2_dir) == "v2"
    assert detect_format(tmp_path / "plain.jsonl") == "legacy"


def test_dispatch_hydrates_package(tmp_path: Path):
    src = _db_with_rows(tmp_path / "src.sqlite")
    pkg_dir = tmp_path / "pkg"
    write_package(src, pkg_dir)
    src.close()

    target = MemoryDB(tmp_path / "target.sqlite")
    result = hydrate(target, pkg_dir)  # dispatch picks the package reader
    assert result.loaded == 2
    again = hydrate(target, pkg_dir)
    assert again.loaded == 0
    assert again.skipped_dupes == 2
    target.close()


def test_dispatch_snapshot_package_flag(tmp_path: Path):
    src = _db_with_rows(tmp_path / "src.sqlite")
    out = tmp_path / "clone"
    result = snapshot(src, out, package=True)
    src.close()
    assert result.exported == 2
    assert (out / MANIFEST_NAME).is_file()
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["format"] == "hotmem-interchange-v1"


def test_verify_entrypoint_both_formats(tmp_path: Path):
    src = _db_with_rows(tmp_path / "src.sqlite")
    pkg_dir = tmp_path / "pkg"
    v2_dir = tmp_path / "v2"
    write_package(src, pkg_dir)
    from hotmem.snapshot.writer import write_snapshot_v2

    write_snapshot_v2(src, v2_dir)
    src.close()

    summary = verify(pkg_dir)
    assert summary["format"] == "package"
    assert summary["valid"] is True
    assert summary["record_count"] == 2

    summary_v2 = verify(v2_dir)
    assert summary_v2["format"] == "v2"
    assert summary_v2["valid"] is True


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_snapshot_package_and_verify(tmp_path: Path):
    db_path = tmp_path / "cli.sqlite"
    db = _db_with_rows(db_path)
    db.close()
    pkg_dir = tmp_path / "pkg"

    result = CliRunner().invoke(
        main, ["snapshot", "--file", str(pkg_dir), "--db", str(db_path), "--package"]
    )
    assert result.exit_code == 0, result.output
    assert "snapshot:" in result.output

    verify_run = CliRunner().invoke(main, ["verify", str(pkg_dir)])
    assert verify_run.exit_code == 0, verify_run.output
    assert "format=package" in verify_run.output.replace(" ", "")

    # Corrupt the payload; verify must fail with a non-zero exit.
    payload = pkg_dir / "memories.jsonl"
    data = bytearray(payload.read_bytes())
    data[-3] ^= 0xFF
    payload.write_bytes(bytes(data))
    bad = CliRunner().invoke(main, ["verify", str(pkg_dir)])
    assert bad.exit_code != 0
    assert "digest_mismatch" in bad.output


def test_cli_snapshot_package_gz(tmp_path: Path):
    db_path = tmp_path / "cli.sqlite"
    _db_with_rows(db_path).close()
    pkg_dir = tmp_path / "pkg"

    result = CliRunner().invoke(
        main, ["snapshot", "--file", str(pkg_dir), "--db", str(db_path), "--package", "--gz"]
    )
    assert result.exit_code == 0, result.output
    assert (pkg_dir / "memories.jsonl.gz").is_file()

    verify_run = CliRunner().invoke(main, ["verify", str(pkg_dir)])
    assert verify_run.exit_code == 0, verify_run.output


# ── HTTP API ────────────────────────────────────────────────────────────────


def test_api_snapshot_package_and_hydrate(tmp_path: Path):
    from hotmem.server import create_app

    src_path = tmp_path / "api.sqlite"
    _db_with_rows(src_path).close()
    app = create_app(db_path=src_path)
    with TestClient(app) as client:
        pkg_dir = tmp_path / "pkg"
        resp = client.post("/v1/snapshot", json={"path": str(pkg_dir), "package": True})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"exported", "path"}
        assert body["exported"] == 2

        verify_resp = client.post("/v1/hydrate", json={"path": str(pkg_dir)})
        assert verify_resp.status_code == 200
        assert verify_resp.json()["skipped_dupes"] == 2  # same DB: all dupes


def test_api_hydrate_corrupt_package_409(tmp_path: Path):
    from hotmem.server import create_app

    src_path = tmp_path / "api.sqlite"
    _db_with_rows(src_path).close()
    app = create_app(db_path=src_path)
    with TestClient(app) as client:
        pkg_dir = tmp_path / "pkg"
        client.post("/v1/snapshot", json={"path": str(pkg_dir), "package": True})
        payload = pkg_dir / "memories.jsonl"
        data = bytearray(payload.read_bytes())
        data[5] ^= 0xFF
        payload.write_bytes(bytes(data))

        resp = client.post("/v1/hydrate", json={"path": str(pkg_dir)})
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "package_verification_failed"
        assert body["reason"] == "digest_mismatch"
