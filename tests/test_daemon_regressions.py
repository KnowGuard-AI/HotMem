"""Regression tests for the shared-daemon review findings (#109–#112).

Each test pins a behavior that was previously incorrect: cross-scope hygiene
leaks, hydration scope escapes, cross-namespace annotation merges, dirty reads,
store-id oracles, spoofed attribution, event cursor starvation, export metadata
leaks, and runtime policy revocation.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.annotations import merge_duplicate_annotations
from hotmem.db import MemoryDB
from hotmem.mount import StoreIdentityError, StoreOwnershipError, store_ownership
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.runtime import HotMemRuntime
from hotmem.server import create_app
from hotmem.snapshot import verify as verify_snapshot


def _policy(store_id: str = "store-reg") -> PolicyConfig:
    return PolicyConfig(
        store_id=store_id,
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id=store_id,
                namespaces=("ops/shared", "ops/private"),
                operations=("read", "write", "export", "hydrate", "promote"),
                allowed_sessions=("sess-a",),
                allowed_sources=("agent://a",),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id=store_id,
                namespaces=("ops/b",),
                operations=("read", "write"),
                allowed_sessions=("sess-b",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-demo",
                        namespace="ops/shared",
                        identifiers=("ops/demo", "ops/public"),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-mgmt",
                caller_id="mgmt",
                store_id=store_id,
                namespaces=("ops/shared",),
                operations=("read",),
                is_management=True,
            ),
            CallerCredential.from_token(
                token="tok-export-only",
                caller_id="export-only",
                store_id=store_id,
                namespaces=("ops/shared",),
                operations=("export",),
            ),
            CallerCredential.from_token(
                token="tok-wild",
                caller_id="wild",
                store_id=store_id,
                namespaces=("*",),
                operations=("read", "write"),
                allowed_sessions=("sess-wild",),
            ),
        ],
    )


def _hdr(
    token: str,
    *,
    ns: str | None = None,
    session: str | None = None,
    store_id: str = "store-reg",
    **extra: str,
) -> dict:
    headers = {"Authorization": f"Bearer {token}", "X-HotMem-Store-Id": store_id}
    if ns is not None:
        headers["X-HotMem-Namespace"] = ns
    if session is not None:
        headers["X-HotMem-Session-Id"] = session
    headers.update(extra)
    return headers


@pytest.fixture
def daemon_app(tmp_path: Path):
    app = create_app(db_path=tmp_path / "reg.sqlite", policy=_policy(), base_dir=tmp_path)
    with TestClient(app) as client:
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
            json={"identifier": "ops/demo", "fact": "public decision"},
        )
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
            json={"identifier": "ops/public", "fact": "second public decision"},
        )
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="ops/private", session="sess-a"),
            json={"identifier": "ops/secret", "fact": "PRIVATE CANARY"},
        )
        yield app, client


# ── Cross-scope disclosure ───────────────────────────────────────────────────


def test_hygiene_is_scoped_and_read_only_callers_do_not_mutate_events(daemon_app):
    app, client = daemon_app
    before = client.get("/v1/events", headers=_hdr("tok-mgmt")).json()["count"]

    hy = client.get("/v1/hygiene", headers=_hdr("tok-b")).json()
    # Only B's share-approved rows are visible in stats.
    assert hy["stats"]["total_memories"] == 2
    assert "PRIVATE CANARY" not in json.dumps(hy)

    after = client.get("/v1/events", headers=_hdr("tok-mgmt")).json()["count"]
    assert after == before  # GET did not append hygiene events for a read-only caller


def test_hygiene_warnings_do_not_leak_private_identifiers(daemon_app, tmp_path: Path):
    app, client = daemon_app
    (tmp_path / "secret.txt").write_text("secret bytes")
    add = client.post(
        "/v1/add",
        headers=_hdr("tok-a", ns="ops/private", session="sess-a"),
        json={
            "identifier": "ops/private-file",
            "file_uri": "secret.txt",
            "byte_offset": 0,
            "byte_length": 12,
            "summary": "private file",
        },
    )
    assert add.status_code == 200
    (tmp_path / "secret.txt").unlink()

    body = client.get("/v1/hygiene", headers=_hdr("tok-b")).json()
    payload = json.dumps(body)
    assert "ops/private-file" not in payload
    assert "secret.txt" not in payload
    assert add.json()["memory_id"] not in payload


def test_health_hides_paths_and_distinguishes_empty_scope(daemon_app):
    app, client = daemon_app
    b = client.get("/v1/health", headers=_hdr("tok-b")).json()
    assert b["db_path"] is None
    assert b["store_state"] == "ready"  # 2 visible shared rows

    only_private = CallerCredential.from_token(
        token="tok-noscope",
        caller_id="noscope",
        store_id="store-reg",
        namespaces=("ops/none",),
        operations=("read",),
    )
    app.state.runtime.policy.credentials.append(only_private)
    none_visible = client.get(
        "/v1/health", headers=_hdr("tok-noscope")
    ).json()
    assert none_visible["memory_count"] == 0
    assert none_visible["store_state"] == "empty_scope"

    # A credential without the read operation cannot read diagnostics.
    assert client.get("/v1/health", headers=_hdr("tok-export-only")).status_code == 403


def test_store_mismatch_requires_authentication_first(tmp_path: Path):
    app = create_app(db_path=tmp_path / "oracle.sqlite", policy=_policy())
    with TestClient(app) as client:
        resp = client.get("/v1/health", headers={"X-HotMem-Store-Id": "wrong"})
        assert resp.status_code == 401
        assert "store-reg" not in resp.text


# ── Attribution and namespaces ───────────────────────────────────────────────


def test_body_source_cannot_impersonate_another_callers_source(daemon_app):
    app, client = daemon_app
    resp = client.post(
        "/v1/add",
        headers=_hdr("tok-b", ns="ops/b", session="sess-b"),
        json={"identifier": "i", "fact": "f", "source": "agent://a"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "session_impersonation_denied"


def test_policy_writes_require_session_attribution(daemon_app):
    app, client = daemon_app
    resp = client.post(
        "/v1/add",
        headers=_hdr("tok-a", ns="ops/shared"),
        json={"identifier": "i", "fact": "f"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "session_required"


def test_empty_namespace_header_is_not_a_filter_and_wildcard_cannot_write(daemon_app):
    app, client = daemon_app
    health = client.get("/v1/health", headers=_hdr("tok-a", ns="")).json()
    assert health["memory_count"] == 3

    resp = client.post(
        "/v1/add",
        headers=_hdr("tok-wild", session="sess-wild"),
        json={"identifier": "i", "fact": "wild"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "namespace_required"


# ── Hydration scope and portability ──────────────────────────────────────────


def test_hydrate_cannot_write_outside_the_callers_namespace(tmp_path: Path):
    policy = PolicyConfig(
        store_id="s-hydrate",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-c",
                caller_id="caller-c",
                store_id="s-hydrate",
                namespaces=("ops/shared",),
                operations=("read", "write", "hydrate"),
                allowed_sessions=("sess-c",),
            )
        ],
    )
    app = create_app(db_path=tmp_path / "h.sqlite", policy=policy, base_dir=tmp_path)
    artifact = tmp_path / "legacy.jsonl"
    artifact.write_text(
        json.dumps(
            {
                "id": "injected",
                "identifier": "ops/injected",
                "fact_text": "evil",
                "namespace": "ops/private",
            }
        )
        + "\n"
    )
    hdr = {
        "Authorization": "Bearer tok-c",
        "X-HotMem-Store-Id": "s-hydrate",
        "X-HotMem-Namespace": "ops/shared",
        "X-HotMem-Session-Id": "sess-c",
    }
    with TestClient(app) as client:
        resp = client.post("/v1/hydrate", headers=hdr, json={"path": str(artifact)})
        assert resp.status_code == 403
        assert resp.json()["error"] == "scope_denied"
        assert app.state.runtime.db.get_memory("injected") is None


def test_daemon_paths_are_confined_to_the_base_dir(tmp_path: Path):
    base = tmp_path / "store"
    app = create_app(db_path=base / "p.sqlite", policy=_policy(), base_dir=base)
    hdr = _hdr("tok-a", ns="ops/shared", session="sess-a")
    with TestClient(app) as client:
        outside = tmp_path / "outside_snapshot"
        resp = client.post("/v1/snapshot", headers=hdr, json={"path": str(outside)})
        assert resp.status_code == 403
        assert resp.json()["error"] == "path_not_permitted"
        assert not outside.exists()


def test_scoped_export_records_scope_omissions_and_redacts_host_paths(daemon_app):
    app, client = daemon_app
    out = app.state.runtime.base_dir
    target = Path(out) / "scoped_export"
    resp = client.post(
        "/v1/snapshot",
        headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
        json={"path": str(target)},
    )
    assert resp.status_code == 200
    assert resp.json()["exported"] == 2

    metadata = json.loads((target / "metadata.json").read_text())
    manifest = json.loads((target / "manifest.json").read_text())
    assert metadata["host"] == ""
    assert metadata["db_path"] == ""
    assert manifest["scope"]["exported_count"] == 2
    assert manifest["scope"]["omitted_count"] == 1
    record = json.loads((target / "memories.jsonl").read_text().splitlines()[0])
    assert record["schema_version"] == 2
    assert verify_snapshot(target)["valid"] is True

    pkg = Path(out) / "scoped_pkg"
    resp = client.post(
        "/v1/snapshot",
        headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
        json={"path": str(pkg), "package": True},
    )
    assert resp.status_code == 200
    pkg_manifest = json.loads((pkg / "manifest.json").read_text())
    assert pkg_manifest["schema_version"] == 1
    assert pkg_manifest["scope"]["omitted_count"] == 1
    pkg_record = json.loads((pkg / "memories.jsonl").read_text().splitlines()[0])
    assert pkg_record["schema_version"] == 1


# ── Consistency, retry visibility, cursors ───────────────────────────────────


def test_reads_never_observe_uncommitted_writes(tmp_path: Path):
    rt = HotMemRuntime(db_path=tmp_path / "dirty.sqlite").open()
    entered = threading.Event()
    release = threading.Event()

    def writer() -> None:
        try:
            with rt._write_slot() as db:
                db._conn.execute(
                    "INSERT INTO memories (id, identifier, fact_text, memory_type, "
                    "namespace, source_uri, byte_offset, byte_length, content_hash) "
                    "VALUES ('dirty','ops/dirty','','file','ops/x','s',0,1,'h')"
                )
                entered.set()
                release.wait(5)
                raise RuntimeError("rollback")
        except RuntimeError:
            pass

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert entered.wait(3)
        files = rt.list_files(rt.resolve_caller())
        assert files["count"] == 0
    finally:
        release.set()
        thread.join()
        rt.close()


def test_duplicate_write_is_explicit_not_silent(daemon_app):
    app, client = daemon_app
    first = client.post(
        "/v1/add",
        headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
        json={"identifier": "ops/demo", "fact": "public decision"},
    ).json()
    dup = client.post(
        "/v1/add",
        headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
        json={"identifier": "ops/demo", "fact": "public decision"},
    ).json()
    assert dup["memory_id"] == first["memory_id"]
    assert dup["deduplicated"] is True
    events = client.get(
        "/v1/events",
        headers=_hdr("tok-mgmt"),
        params={"memory_id": first["memory_id"]},
    ).json()
    assert any(e["event_type"] == "memory.deduplicated" for e in events["events"])


def test_event_cursor_advances_past_invisible_events(daemon_app):
    app, client = daemon_app
    for i in range(4):
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="ops/private", session="sess-a"),
            json={"identifier": f"ops/p{i}", "fact": f"private {i}"},
        )
    client.post(
        "/v1/add",
        headers=_hdr("tok-a", ns="ops/shared", session="sess-a"),
        json={"identifier": "ops/public", "fact": "late public event"},
    )
    page = client.get("/v1/events", headers=_hdr("tok-b"), params={"limit": 2}).json()
    assert page["count"] >= 1
    assert page["next_seq"] is not None


# ── Runtime policy management and identity ──────────────────────────────────


def test_management_can_revoke_and_reload_policy(tmp_path: Path):
    policy_path = tmp_path / "policy.json"
    policy = _policy("s-policy")
    policy.save(policy_path)
    app = create_app(db_path=tmp_path / "p.sqlite", policy=policy, policy_path=str(policy_path))
    with TestClient(app) as client:
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="ops/shared", session="sess-a", store_id="s-policy"),
            json={"identifier": "ops/demo", "fact": "public decision"},
        )
        assert (
            client.get("/v1/memory/x", headers=_hdr("tok-mgmt", store_id="s-policy")).status_code
            == 404
        )

        rev = client.post(
            "/v1/policy/revoke",
            headers=_hdr("tok-mgmt", store_id="s-policy"),
            json={"approval_id": "share-demo"},
        )
        assert rev.status_code == 200
        assert rev.json()["policy_revision"] == 2
        # B loses the shared approval immediately.
        assert (
            client.post(
                "/v1/search",
                headers=_hdr("tok-b", store_id="s-policy"),
                json={"query": "public", "top_k": 5},
            ).json()["count"]
            == 0
        )

        reloaded = client.post(
            "/v1/policy/reload", headers=_hdr("tok-mgmt", store_id="s-policy")
        )
        assert reloaded.status_code == 200
        assert reloaded.json()["reloaded"] is True

        # Non-management callers cannot manage policy.
        denied = client.post(
            "/v1/policy/revoke",
            headers=_hdr("tok-b", store_id="s-policy"),
            json={"caller_id": "caller-a"},
        )
        assert denied.status_code == 403


def test_store_identity_is_persistent_and_mismatch_fails(tmp_path: Path):
    path = tmp_path / "identity.sqlite"
    rt = HotMemRuntime(db_path=path, store_id="stable").open()
    rt.close()

    adopted = HotMemRuntime(db_path=path).open()
    assert adopted.store_id == "stable"
    adopted.close()

    with pytest.raises(StoreIdentityError):
        HotMemRuntime(db_path=path, store_id="other").open()


def test_runtime_rejects_conflicting_policy_and_cli_store_id(tmp_path: Path):
    policy = PolicyConfig(store_id="policy-id", require_auth=True)
    with pytest.raises(StoreIdentityError):
        HotMemRuntime(db_path=tmp_path / "m.sqlite", policy=policy, store_id="cli-id")


def test_store_ownership_context_blocks_a_second_writer(tmp_path: Path):
    path = tmp_path / "owned.sqlite"
    with store_ownership(path), pytest.raises(StoreOwnershipError), store_ownership(path):
        pass


def test_create_app_rejects_runtime_path_mismatch(tmp_path: Path):
    rt = HotMemRuntime(db_path=tmp_path / "a.sqlite").open()
    try:
        with pytest.raises(ValueError):
            create_app(db_path=tmp_path / "b.sqlite", runtime=rt)
    finally:
        rt.close()


def test_annotation_merge_is_namespace_scoped(tmp_path: Path):
    db = MemoryDB(tmp_path / "ann.sqlite")
    try:
        db.insert(
            id="rowA",
            identifier="doc",
            fact_text="same text",
            embedding=b"",
            content_hash="H1",
            namespace="A",
        )
        db.insert(
            id="rowB",
            identifier="doc",
            fact_text="same text",
            embedding=b"",
            content_hash="H1",
            namespace="B",
        )
        counters = {"annotations_merged": 0, "annotation_conflicts": 0}
        merge_duplicate_annotations(
            db,
            [
                {
                    "content_hash": "H1",
                    "namespace": "A",
                    "metadata": {"annotations": {"items": [{"id": "i1", "content": "a"}]}},
                }
            ],
            counters,
            commit=True,
        )
        assert json.loads(db.get_memory("rowA")["metadata_json"])["annotations"]
        assert json.loads(db.get_memory("rowB")["metadata_json"]) == {}
    finally:
        db.close()
