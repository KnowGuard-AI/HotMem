"""Tests for #43 — API extensions: /v1/files, /v1/bundles, /v1/discover,
hydrate-batch, hydrate profile params, and #51 — /v1/hygiene."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding


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
        # Add inline memory
        c.post(
            "/v1/add",
            json={"identifier": "vendor_x", "fact": "inline fact about acme"},
        )
        # Add file-backed memory
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
        c._inline_id = c.post(
            "/v1/add", json={"identifier": "vendor_y", "fact": "another fact"}
        ).json()["memory_id"]  # type: ignore[attr-defined]
        yield c


# ── /v1/files ──────────────────────────────────────────────────────────────────


def test_list_files(app_client_with_data: TestClient):
    resp = app_client_with_data.get("/v1/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1  # only the file-backed memory
    entry = body["files"][0]
    assert entry["identifier"] == "dataset"
    assert entry["source_uri"] is not None
    assert entry["exists"] is True
    assert entry["byte_length"] == 20


def test_list_files_filter_by_identifier(app_client_with_data: TestClient):
    resp = app_client_with_data.get("/v1/files?identifier=dataset")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    resp2 = app_client_with_data.get("/v1/files?identifier=nonexistent")
    assert resp2.json()["count"] == 0


def test_list_files_shows_missing(app_client_with_data: TestClient, fixture_file: Path):
    # Delete the backing file
    Path(app_client_with_data.fixture_path).unlink()  # type: ignore[attr-defined]
    resp = app_client_with_data.get("/v1/files")
    entry = resp.json()["files"][0]
    assert entry["exists"] is False


# ── /v1/bundles + /v1/discover ────────────────────────────────────────────────


def test_discover_and_list_bundles(app_client_with_data: TestClient, tmp_path: Path):
    # Create a bundle under the mount dir
    bundle = app_client_with_data.mount_dir / "mybundle"  # type: ignore[attr-defined]
    bundle.mkdir()
    (bundle / "memory.md").write_text("# Test Bundle\n\nContent.")

    # Discover
    resp = app_client_with_data.post(
        "/v1/discover",
        json={"root": str(app_client_with_data.mount_dir)},  # type: ignore[attr-defined]
    )
    assert resp.status_code == 200
    assert resp.json()["discovered"] == 1
    assert resp.json()["indexed"] == 1

    # List
    resp2 = app_client_with_data.get("/v1/bundles")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["count"] == 1
    entry = body["bundles"][0]
    assert entry["primary_file"] == "memory.md"


# ── Hydrate with profile params ───────────────────────────────────────────────


def test_hydrate_with_profile_returns_json(app_client_with_data: TestClient):
    """POST /v1/memory/{id}/hydrate with a body returns JSON, not bytes."""
    mid = app_client_with_data._inline_id  # type: ignore[attr-defined]
    resp = app_client_with_data.post(
        f"/v1/memory/{mid}/hydrate",
        json={"profile": "agent", "verify": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"] == "agent"
    assert "content" in body
    assert body["content_encoding"] == "base64"


def test_hydrate_without_body_returns_bytes(app_client_with_data: TestClient):
    """POST /v1/memory/{id}/hydrate without a body returns raw bytes (unchanged)."""
    mid = app_client_with_data._inline_id  # type: ignore[attr-defined]
    resp = app_client_with_data.post(f"/v1/memory/{mid}/hydrate")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert b"another fact" in resp.content


def test_hydrate_compact_no_content(app_client_with_data: TestClient):
    mid = app_client_with_data._inline_id  # type: ignore[attr-defined]
    resp = app_client_with_data.post(
        f"/v1/memory/{mid}/hydrate",
        json={"profile": "compact"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"] == "compact"
    assert "content" not in body  # compact: no content


def test_hydrate_audit_file_backed(app_client_with_data: TestClient):
    mid = app_client_with_data._file_backed_id  # type: ignore[attr-defined]
    resp = app_client_with_data.post(
        f"/v1/memory/{mid}/hydrate",
        json={"profile": "audit", "verify": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"] == "audit"
    assert body["verified"] is True
    assert body["source_uri"] is not None
    assert "content" in body


# ── /v1/memory/hydrate-batch ──────────────────────────────────────────────────


def test_hydrate_batch(app_client_with_data: TestClient):
    inline_id = app_client_with_data._inline_id  # type: ignore[attr-defined]
    file_id = app_client_with_data._file_backed_id  # type: ignore[attr-defined]
    resp = app_client_with_data.post(
        "/v1/memory/hydrate-batch",
        json={
            "memory_ids": [inline_id, file_id],
            "profile": "compact",
            "verify": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    for r in body["results"]:
        assert r["profile"] == "compact"
        assert "content" not in r  # compact: no content


def test_hydrate_batch_missing_memory(app_client_with_data: TestClient):
    resp = app_client_with_data.post(
        "/v1/memory/hydrate-batch",
        json={"memory_ids": ["nonexistent-id"], "profile": "agent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["error"] == "not_found"


# ── /v1/hygiene ──────────────────────────────────────────────────────────────


def test_hygiene_no_warnings_for_small_store(app_client_with_data: TestClient):
    resp = app_client_with_data.get("/v1/hygiene")
    assert resp.status_code == 200
    body = resp.json()
    assert body["warning_count"] == 0
    assert body["stats"]["total_memories"] >= 2


def test_hygiene_detects_missing_backing_file(app_client_with_data: TestClient, tmp_path: Path):
    # Delete the backing file
    Path(app_client_with_data.fixture_path).unlink()  # type: ignore[attr-defined]
    resp = app_client_with_data.get("/v1/hygiene")
    body = resp.json()
    errors = [w for w in body["warnings"] if w["severity"] == "error"]
    assert len(errors) >= 1
    assert any("not found" in w["message"].lower() for w in errors)


def test_hygiene_detects_large_inline(tmp_path: Path):
    """Large inline payloads (> 128 KB) produce warnings."""
    from hotmem.hygiene import check_hygiene

    db = MemoryDB(tmp_path / "big.sqlite")
    big_text = "x" * (130 * 1024)  # 130 KB > 128 KB threshold
    blob = pack_embedding(embed_text(big_text))
    db.insert(id="big1", identifier="big", fact_text=big_text, embedding=blob)
    report = check_hygiene(db)
    db.close()
    large = [w for w in report.warnings if w.category == "large_inline"]
    assert len(large) >= 1
    assert large[0].severity == "warn"


# ── Existing API compatibility ────────────────────────────────────────────────


def test_existing_add_unchanged(app_client_with_data: TestClient):
    """Existing /v1/add with identifier + fact still works."""
    resp = app_client_with_data.post(
        "/v1/add", json={"identifier": "compat", "fact": "compatibility test"}
    )
    assert resp.status_code == 200
    assert "memory_id" in resp.json()


def test_existing_search_unchanged(app_client_with_data: TestClient):
    """Existing /v1/search shape is unchanged."""
    resp = app_client_with_data.post("/v1/search", json={"query": "acme", "top_k": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"memories", "count", "trace_ms"}


def test_existing_hydrate_unchanged(app_client_with_data: TestClient):
    """Existing /v1/hydrate (empty body) still works."""
    resp = app_client_with_data.post("/v1/hydrate", json={})
    assert resp.status_code == 200
    assert "loaded" in resp.json()
