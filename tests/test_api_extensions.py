"""Tests for #43 — API extensions: /v1/files, /v1/bundles, /v1/discover, hydrate-batch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
        c._inline_id = c.post(  # type: ignore[attr-defined]
            "/v1/add", json={"identifier": "vendor_y", "fact": "another fact"}
        ).json()["memory_id"]
        yield c


# ── /v1/files ──────────────────────────────────────────────────────────────────


def test_list_files(app_client_with_data: TestClient):
    resp = app_client_with_data.get("/v1/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
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


def test_list_files_shows_missing(app_client_with_data: TestClient):
    Path(app_client_with_data.fixture_path).unlink()  # type: ignore[attr-defined]
    resp = app_client_with_data.get("/v1/files")
    entry = resp.json()["files"][0]
    assert entry["exists"] is False


# ── /v1/bundles + /v1/discover ────────────────────────────────────────────────


def test_discover_and_list_bundles(app_client_with_data: TestClient):
    bundle = app_client_with_data.mount_dir / "mybundle"  # type: ignore[attr-defined]
    bundle.mkdir()
    (bundle / "memory.md").write_text("# Test Bundle\n\nContent.")

    resp = app_client_with_data.post(
        "/v1/discover",
        json={"root": str(app_client_with_data.mount_dir)},  # type: ignore[attr-defined]
    )
    assert resp.status_code == 200
    assert resp.json()["discovered"] == 1
    assert resp.json()["indexed"] == 1

    resp2 = app_client_with_data.get("/v1/bundles")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["count"] == 1
    assert body["bundles"][0]["primary_file"] == "memory.md"


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
        assert "content" not in r


def test_hydrate_batch_missing_memory(app_client_with_data: TestClient):
    resp = app_client_with_data.post(
        "/v1/memory/hydrate-batch",
        json={"memory_ids": ["nonexistent-id"], "profile": "agent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["error"] == "not_found"


# ── Existing API compatibility ────────────────────────────────────────────────


def test_existing_add_unchanged(app_client_with_data: TestClient):
    resp = app_client_with_data.post(
        "/v1/add", json={"identifier": "compat", "fact": "compatibility test"}
    )
    assert resp.status_code == 200
    assert "memory_id" in resp.json()


def test_existing_search_unchanged(app_client_with_data: TestClient):
    resp = app_client_with_data.post("/v1/search", json={"query": "acme", "top_k": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"memories", "count", "trace_ms"}


def test_existing_hydrate_unchanged(app_client_with_data: TestClient):
    resp = app_client_with_data.post("/v1/hydrate", json={})
    assert resp.status_code == 200
    assert "loaded" in resp.json()
