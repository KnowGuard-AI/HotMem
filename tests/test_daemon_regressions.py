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
    none_visible = client.get("/v1/health", headers=_hdr("tok-noscope")).json()
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

        reloaded = client.post("/v1/policy/reload", headers=_hdr("tok-mgmt", store_id="s-policy"))
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


def test_wp4_deduplication_preserves_writer_and_idempotency_conflicts(tmp_path: Path):
    """WP4: Deduplication preserves original writer attribution while logging later attempts;
    idempotency key replay preserves writer attribution and rejects changed tags/harness/context.
    """
    policy = PolicyConfig(
        store_id="store-wp4",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="caller-a",
                store_id="store-wp4",
                namespaces=("ops/shared",),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id="store-wp4",
                namespaces=("ops/shared",),
                operations=("read", "write"),
            ),
        ],
    )
    app = create_app(db_path=tmp_path / "wp4.sqlite", policy=policy)
    hdr_a = {
        "Authorization": "Bearer tok-a",
        "X-HotMem-Store-Id": "store-wp4",
        "X-HotMem-Harness-Id": "codex",
        "X-HotMem-Session-Id": "sess-a-orig",
        "X-HotMem-Source": "agent://codex-a",
        "X-HotMem-Namespace": "ops/shared",
    }
    hdr_b = {
        "Authorization": "Bearer tok-b",
        "X-HotMem-Store-Id": "store-wp4",
        "X-HotMem-Harness-Id": "claude-code",
        "X-HotMem-Session-Id": "sess-b-second",
        "X-HotMem-Source": "agent://claude-b",
        "X-HotMem-Namespace": "ops/shared",
    }

    with TestClient(app) as client:
        # 1. Writer A creates memory with idempotency key
        w_a = client.post(
            "/v1/add",
            headers={**hdr_a, "X-HotMem-Idempotency-Key": "idem-wp4-1"},
            json={
                "identifier": "ops/runbook-111",
                "fact": "Drain worker queue before rotating credentials.",
                "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
            },
        )
        assert w_a.status_code == 200
        mid = w_a.json()["memory_id"]
        assert not w_a.json().get("deduplicated")

        # 2. Exact retry with same idempotency key (even from reconnected session) replays
        #    without mutating original writer attribution.
        replay = client.post(
            "/v1/add",
            headers={
                **hdr_a,
                "X-HotMem-Session-Id": "sess-a-reconnect",
                "X-HotMem-Idempotency-Key": "idem-wp4-1",
            },
            json={
                "identifier": "ops/runbook-111",
                "fact": "Drain worker queue before rotating credentials.",
                "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
            },
        )
        assert replay.status_code == 200
        assert replay.json()["memory_id"] == mid

        # 3. Changed tags, harness_id, source, metadata, or explicit body session_id under
        #    same idempotency key fail with 409 idempotency_key_conflict.
        for diff_hdr, diff_body in (
            (
                hdr_a,
                {
                    "identifier": "ops/runbook-111",
                    "fact": "Drain worker queue before rotating credentials.",
                    "tags": ["model:o4-mini", "project:hotmem"],
                },
            ),
            (
                {**hdr_a, "X-HotMem-Harness-Id": "codex-cli"},
                {
                    "identifier": "ops/runbook-111",
                    "fact": "Drain worker queue before rotating credentials.",
                    "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
                },
            ),
            (
                {**hdr_a, "X-HotMem-Source": "agent://codex-other"},
                {
                    "identifier": "ops/runbook-111",
                    "fact": "Drain worker queue before rotating credentials.",
                    "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
                },
            ),
            (
                hdr_a,
                {
                    "identifier": "ops/runbook-111",
                    "fact": "Drain worker queue before rotating credentials.",
                    "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
                    "metadata": {"extra": True},
                },
            ),
        ):
            conflict = client.post(
                "/v1/add",
                headers={**diff_hdr, "X-HotMem-Idempotency-Key": "idem-wp4-1"},
                json=diff_body,
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"] == "idempotency_key_conflict"

        # 4. Writer B writes identical (namespace, identifier, fact) with different context/tags:
        #    content-hash deduplication preserves Writer A's stored attribution & tags, while
        #    recording Writer B's attempt in memory.deduplicated event.
        w_b = client.post(
            "/v1/add",
            headers=hdr_b,
            json={
                "identifier": "ops/runbook-111",
                "fact": "Drain worker queue before rotating credentials.",
                "tags": ["model:claude-sonnet-4", "project:hotmem"],
            },
        )
        assert w_b.status_code == 200
        assert w_b.json()["memory_id"] == mid
        assert w_b.json()["deduplicated"] is True

        # Reader B reads via get, list, and search -> Writer A attribution & tags remain intact
        got = client.get(f"/v1/memory/{mid}", headers=hdr_b).json()
        assert got["tags"] == ["model:gpt-5", "project:hotmem", "org:knowguard"]
        assert got["provenance"] == {
            "envelope_version": "hotmem-session-v1",
            "caller_id": "caller-a",
            "harness_id": "codex",
            "session_id": "sess-a-orig",
            "source": "agent://codex-a",
            "namespace": "ops/shared",
            "store_id": "store-wp4",
        }

        evs = client.get(
            "/v1/events",
            headers=hdr_b,
            params={"memory_id": mid},
        ).json()["events"]
        dedup_evs = [e for e in evs if e["event_type"] == "memory.deduplicated"]
        assert len(dedup_evs) == 1
        assert dedup_evs[0]["payload"]["caller_id"] == "caller-b"
        assert dedup_evs[0]["payload"]["harness_id"] == "claude-code"
        assert dedup_evs[0]["payload"]["session_id"] == "sess-b-second"
        assert dedup_evs[0]["payload"]["tags"] == ["model:claude-sonnet-4", "project:hotmem"]


def test_wp4_concurrent_sessions_without_context_contamination(tmp_path: Path):
    """WP4: Concurrent sessions (distinct conversations in one harness + across harnesses)
    write and read simultaneously without shared mutable context bleed.
    """
    from concurrent.futures import ThreadPoolExecutor

    policy = PolicyConfig(
        store_id="store-conc-111",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-codex",
                caller_id="caller-codex",
                store_id="store-conc-111",
                namespaces=("team/shared",),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-claude",
                caller_id="caller-claude",
                store_id="store-conc-111",
                namespaces=("team/shared",),
                operations=("read", "write"),
            ),
        ],
    )
    app = create_app(db_path=tmp_path / "conc.sqlite", policy=policy)
    sessions = [
        (
            "tok-codex",
            "caller-codex",
            "codex",
            "sess-codex-conv-1",
            "agent://codex-1",
            "model:gpt-5",
        ),
        (
            "tok-codex",
            "caller-codex",
            "codex",
            "sess-codex-conv-2",
            "agent://codex-2",
            "model:o4-mini",
        ),
        (
            "tok-claude",
            "caller-claude",
            "claude-code",
            "sess-claude-conv-1",
            "agent://claude-1",
            "model:claude-sonnet-4",
        ),
    ]

    with TestClient(app) as client:

        def worker(idx: int) -> tuple[str, str, str, str, str]:
            tok, cid, harness, sess, src, model_tag = sessions[idx % len(sessions)]
            resp = client.post(
                "/v1/add",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "X-HotMem-Store-Id": "store-conc-111",
                    "X-HotMem-Harness-Id": harness,
                    "X-HotMem-Session-Id": f"{sess}-{idx}",
                    "X-HotMem-Source": src,
                    "X-HotMem-Namespace": "team/shared",
                },
                json={
                    "identifier": f"conc/item-{idx}",
                    "fact": f"Concurrent decision payload number {idx} from {sess}",
                    "tags": [model_tag, f"seq:{idx}"],
                },
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["memory_id"], cid, harness, f"{sess}-{idx}", model_tag

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(worker, range(18)))

        for mid, expected_cid, expected_harness, expected_sess, expected_model in results:
            rec = client.get(
                f"/v1/memory/{mid}",
                headers={
                    "Authorization": "Bearer tok-claude",
                    "X-HotMem-Store-Id": "store-conc-111",
                    "X-HotMem-Harness-Id": "claude-code",
                    "X-HotMem-Session-Id": "sess-verifier",
                },
            ).json()
            assert rec["provenance"]["caller_id"] == expected_cid
            assert rec["provenance"]["harness_id"] == expected_harness
            assert rec["provenance"]["session_id"] == expected_sess
            assert expected_model in rec["tags"]
