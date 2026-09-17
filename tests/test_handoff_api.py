"""Tests for #101 (commit 11) — /v1/handoff HTTP endpoints.

Covers:
    - prepare → verify → inspect → hydrate through the local sidecar API,
      wrapping the same core functions as CLI/MCP.
    - Consent is required: 400 with no package written (AC 3).
    - Invalid packages: verify/hydrate return 409 with the structured
      body (error/reason/file/expected/actual); inspect reports as data
      (AC 7, 11).
    - hydrate targets the sidecar's database and is idempotent (AC 8),
      and the brief is retrievable through /v1/search (AC 9).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.server import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(db_path=tmp_path / "sidecar.sqlite")
    with TestClient(app) as c:
        yield c


def _prepare(client: TestClient, tmp_path: Path, **overrides) -> dict:
    body = {
        "source": str(FIXTURE),
        "output": str(tmp_path / "pkg"),
        "mode": "resume",
        "consent": CONSENT,
    }
    body.update(overrides)
    response = client.post("/v1/handoff/prepare", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_prepare_returns_identity_and_coverage(client, tmp_path):
    payload = _prepare(client, tmp_path)
    assert payload["mode"] == "resume"
    assert payload["entries"] == 11
    assert payload["memories"] == 2
    assert payload["coverage"] == {"omitted_count": 7, "redacted_count": 1, "recoverable_count": 7}
    assert len(payload["handoff_id"]) == 32
    assert len(payload["package_id"]) == 64
    assert payload["timings_ms"]["total_ms"] >= 0
    assert (tmp_path / "pkg" / "manifest.json").is_file()


def test_prepare_requires_consent_and_writes_nothing(client, tmp_path):
    response = client.post(
        "/v1/handoff/prepare",
        json={"source": str(FIXTURE), "output": str(tmp_path / "pkg"), "consent": " "},
    )
    assert response.status_code == 400
    assert "consent" in response.json()["detail"].lower()
    assert not (tmp_path / "pkg").exists()

    # Pydantic-required field missing → 422, still nothing written.
    missing = client.post(
        "/v1/handoff/prepare",
        json={"source": str(FIXTURE), "output": str(tmp_path / "pkg2")},
    )
    assert missing.status_code == 422
    assert not (tmp_path / "pkg2").exists()


def test_verify_ok_and_409_with_structured_body(client, tmp_path):
    payload = _prepare(client, tmp_path)
    ok = client.post("/v1/handoff/verify", json={"package": payload["path"]})
    assert ok.status_code == 200
    assert ok.json() == {
        "valid": True,
        "package_id": payload["package_id"],
        "mode": "resume",
        "entries": 11,
        "memories": 2,
    }

    (tmp_path / "pkg" / "session.jsonl").write_text("tampered\n")
    bad = client.post("/v1/handoff/verify", json={"package": payload["path"]})
    assert bad.status_code == 409
    body = bad.json()
    assert body["error"] == "handoff_invalid"
    assert body["reason"] == "size_mismatch"
    assert body["file"] == "session.jsonl"


def test_inspect_reports_valid_and_invalid_as_data(client, tmp_path):
    payload = _prepare(client, tmp_path)
    report = client.get("/v1/handoff/inspect", params={"package": payload["path"]}).json()
    assert report["valid"] is True
    assert report["counts"]["entries"] == 11
    assert report["coverage"]["redacted_count"] == 1

    (tmp_path / "pkg" / "manifest.json").write_text("{broken")
    report = client.get("/v1/handoff/inspect", params={"package": payload["path"]}).json()
    assert report["valid"] is False
    assert report["failure"]["reason"] == "malformed_manifest"


def test_hydrate_into_sidecar_is_idempotent_and_searchable(client, tmp_path):
    payload = _prepare(client, tmp_path)

    first = client.post("/v1/handoff/hydrate", json={"package": payload["path"]})
    assert first.status_code == 200
    body = first.json()
    assert body["loaded"] == 3
    assert body["already_applied"] is False
    assert body["brief"] == "handoff/hotmem-showcase-planning/resume-brief"
    assert body["embedding_rebuilt"] == 3

    second = client.post("/v1/handoff/hydrate", json={"package": payload["path"]})
    assert second.json()["already_applied"] is True
    assert second.json()["loaded"] == 0

    # AC 9: the brief is retrievable through the normal search endpoint.
    search = client.post("/v1/search", json={"query": "resume brief handoff showcase", "top_k": 5})
    hits = search.json()["memories"]
    identifiers = [h.get("identifier") for h in hits]
    assert "handoff/hotmem-showcase-planning/resume-brief" in identifiers


def test_hydrate_invalid_package_is_409_without_sidecar_changes(client, tmp_path):
    payload = _prepare(client, tmp_path)
    (tmp_path / "pkg" / "memories.jsonl").write_text("gone\n")

    response = client.post("/v1/handoff/hydrate", json={"package": payload["path"]})
    assert response.status_code == 409
    assert response.json()["error"] == "handoff_invalid"

    # The sidecar's store is untouched.
    health = client.get("/v1/health").json()
    assert health["status"] == "ok"
    search = client.post("/v1/search", json={"query": "resume brief", "top_k": 5})
    assert search.json()["memories"] == []


def test_handoff_flow_does_not_disturb_existing_surface(client, tmp_path):
    """Additive proof: the existing endpoints keep their shapes after a handoff."""
    _prepare(client, tmp_path)
    client.post("/v1/handoff/hydrate", json={"package": str(tmp_path / "pkg")})
    health = client.get("/v1/health").json()
    assert health["status"] == "ok"
    assert health["memory_count"] == 3
    # Health shape unchanged by the handoff surface (additive proof).
    assert set(health) == {"status", "memory_count", "db_path", "uptime_s", "embedding"}

    # Snapshot still works against the hydrated store (no second source of truth).
    snap = client.post("/v1/snapshot", json={"path": str(tmp_path / "snap.jsonl")})
    assert snap.status_code == 200
    assert snap.json()["exported"] == 3
    assert shutil.rmtree(tmp_path / "pkg") is None  # plain dir, no handles held
