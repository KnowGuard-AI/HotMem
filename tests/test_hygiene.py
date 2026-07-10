"""Tests for #51 — Local hygiene and store growth warnings."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.hygiene import check_hygiene


@pytest.fixture
def app_client_with_data(tmp_path: Path, fixture_file: Path):
    """A TestClient with one inline + one file-backed memory."""
    from hotmem.server import create_app

    mount = tmp_path / "mount"
    mount.mkdir()
    target = mount / fixture_file.name
    target.write_bytes(fixture_file.read_bytes())

    app = create_app(db_path=mount / "hotmem.sqlite", base_dir=mount)
    with TestClient(app) as c:
        c.mount_dir = mount  # type: ignore[attr-defined]
        c.fixture_path = target  # type: ignore[attr-defined]
        c.post("/v1/add", json={"identifier": "vendor_x", "fact": "inline fact about acme"})
        import hashlib

        chk = hashlib.sha256(fixture_file.read_bytes()[0:20]).hexdigest()
        resp = c.post(
            "/v1/add",
            json={
                "identifier": "dataset",
                "file_uri": fixture_file.name,
                "byte_offset": 0,
                "byte_length": 20,
                "source_format": "bin",
                "source_checksum": chk,
                "summary": "acme data slice",
            },
        )
        c._file_backed_id = resp.json()["memory_id"]  # type: ignore[attr-defined]
        yield c


# ── Hygiene checks ────────────────────────────────────────────────────────────


def test_no_warnings_for_small_store(app_client_with_data: TestClient):
    resp = app_client_with_data.get("/v1/hygiene")
    assert resp.status_code == 200
    body = resp.json()
    assert body["warning_count"] == 0
    assert body["stats"]["total_memories"] >= 2


def test_detects_missing_backing_file(app_client_with_data: TestClient):
    Path(app_client_with_data.fixture_path).unlink()  # type: ignore[attr-defined]
    resp = app_client_with_data.get("/v1/hygiene")
    body = resp.json()
    errors = [w for w in body["warnings"] if w["severity"] == "error"]
    assert len(errors) >= 1
    assert any("not found" in w["message"].lower() for w in errors)


def test_detects_large_inline(tmp_path: Path):
    """Large inline payloads (> 128 KB) produce warnings."""
    db = MemoryDB(tmp_path / "big.sqlite")
    big_text = "x" * (130 * 1024)
    blob = pack_embedding(embed_text(big_text))
    db.insert(id="big1", identifier="big", fact_text=big_text, embedding=blob)
    report = check_hygiene(db)
    db.close()
    large = [w for w in report.warnings if w.category == "large_inline"]
    assert len(large) >= 1
    assert large[0].severity == "warn"


def test_detects_stale_bundle_index(tmp_path: Path):
    """Stale bundle index entries (path no longer exists) produce warnings."""
    db = MemoryDB(tmp_path / "stale.sqlite")

    # Manually insert a bundle index entry pointing at a nonexistent path
    from hotmem.bundle_index import BundleIndexEntry

    entry = BundleIndexEntry(
        path="/nonexistent/bundle",
        primary_file="memory.md",
        identifier="ghost",
    )
    db.upsert_bundle_index(entry)
    report = check_hygiene(db)
    db.close()
    stale = [w for w in report.warnings if w.category == "stale_bundle"]
    assert len(stale) >= 1


def test_no_warning_for_normal_small_store(tmp_db: MemoryDB):
    """A normal small store with no issues produces no warnings."""
    blob = pack_embedding(embed_text("small inline fact"))
    tmp_db.insert(id="s1", identifier="x", fact_text="small inline fact", embedding=blob)
    report = check_hygiene(tmp_db)
    assert len(report.warnings) == 0
    assert report.stats["total_memories"] == 1
    assert report.stats["inline_count"] == 1
    assert report.stats["file_backed_count"] == 0


def test_hygiene_report_to_dict(tmp_db: MemoryDB):
    """HygieneReport.to_dict() produces a serializable dict."""
    report = check_hygiene(tmp_db)
    d = report.to_dict()
    assert "warnings" in d
    assert "stats" in d
    assert "warning_count" in d
    assert "error_count" in d
    assert "warn_count" in d
    assert "info_count" in d


def test_warnings_do_not_alter_existing_api(app_client_with_data: TestClient):
    """Hygiene warnings don't change existing API behavior."""
    # Run hygiene
    app_client_with_data.get("/v1/hygiene")

    # Existing endpoints still work
    assert (
        app_client_with_data.post("/v1/search", json={"query": "acme", "top_k": 5}).status_code
        == 200
    )
    assert app_client_with_data.post("/v1/hydrate", json={}).status_code == 200
