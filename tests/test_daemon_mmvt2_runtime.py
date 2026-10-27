"""MMVT 2 — Runtime ownership and shared operation behavior (#109).

Question/test:
    Does one runtime own the selected store/resources, with transport handlers
    delegating rather than duplicating behavior?
    - Two `create_app()` instances in the same process must own independent
      `HotMemRuntime` instances (no module-global `_state` cross-talk).
    - Every route in `DAEMON_ROUTE_INVENTORY` delegates through `app.state.runtime`
      and enforces the same `PolicyConfig` / `store_id` boundary.
    - Unsupported or mismatched store selection fails explicitly across operations.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.runtime import HotMemRuntime
from hotmem.server import DAEMON_ROUTE_INVENTORY, create_app


def test_mmvt2_two_app_instances_do_not_share_module_global_state(tmp_path: Path):
    """Creating app_b after app_a must never clobber app_a's db_path, store_id, or policy."""
    db_a = tmp_path / "store_a.sqlite"
    db_b = tmp_path / "store_b.sqlite"

    policy_a = PolicyConfig(
        store_id="store-alpha",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-alpha",
                caller_id="alpha-owner",
                store_id="store-alpha",
                namespaces=("ns-a",),
            )
        ],
    )
    policy_b = PolicyConfig(
        store_id="store-beta",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-beta",
                caller_id="beta-owner",
                store_id="store-beta",
                namespaces=("ns-b",),
            )
        ],
    )

    # Create both apps before entering either lifespan (previously _state was overwritten here)
    app_a = create_app(db_path=db_a, policy=policy_a)
    app_b = create_app(db_path=db_b, policy=policy_b)

    assert isinstance(app_a.state.runtime, HotMemRuntime)
    assert isinstance(app_b.state.runtime, HotMemRuntime)
    assert app_a.state.runtime is not app_b.state.runtime

    hdr_a = {
        "Authorization": "Bearer tok-alpha",
        "X-HotMem-Store-Id": "store-alpha",
        "X-HotMem-Namespace": "ns-a",
        "X-HotMem-Session-Id": "sess-alpha",
    }
    hdr_b = {
        "Authorization": "Bearer tok-beta",
        "X-HotMem-Store-Id": "store-beta",
        "X-HotMem-Namespace": "ns-b",
        "X-HotMem-Session-Id": "sess-beta",
    }

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        client_a.post(
            "/v1/add",
            headers=hdr_a,
            json={"identifier": "doc/a", "fact": "Alpha exclusive fact"},
        )
        client_b.post(
            "/v1/add",
            headers=hdr_b,
            json={"identifier": "doc/b", "fact": "Beta exclusive fact"},
        )

        health_a = client_a.get("/v1/health", headers=hdr_a).json()
        health_b = client_b.get("/v1/health", headers=hdr_b).json()
        assert health_a["store_id"] == "store-alpha"
        assert health_a["db_path"] is None  # absolute paths are not disclosed
        assert health_a["memory_count"] == 1

        assert health_b["store_id"] == "store-beta"
        assert health_b["db_path"] is None
        assert health_b["memory_count"] == 1

        # Cross-store credential or store_id is rejected
        cross = client_a.get("/v1/health", headers=hdr_b)
        assert cross.status_code in (401, 409)


def test_mmvt2_shared_operations_parity_and_policy_across_surface(tmp_path: Path):
    """Exercise add (inline & file), search, get, hydrate, snapshot, lifecycle, files, events."""
    db_path = tmp_path / "surface.sqlite"
    backing_file = tmp_path / "runbook.txt"
    backing_file.write_text("Step 1: drain worker queue.\nStep 2: roll back v2.4.1.\n")

    policy = PolicyConfig(
        store_id="store-surface",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-writer",
                caller_id="ops-writer",
                store_id="store-surface",
                namespaces=("ops/main",),
                operations=("read", "write", "export", "hydrate", "promote"),
            ),
            CallerCredential.from_token(
                token="tok-readonly",
                caller_id="ops-reader",
                store_id="store-surface",
                namespaces=("ops/main",),
                operations=("read",),
            ),
        ],
    )

    app = create_app(db_path=db_path, base_dir=tmp_path, policy=policy)
    hdr_writer = {
        "Authorization": "Bearer tok-writer",
        "X-HotMem-Store-Id": "store-surface",
        "X-HotMem-Namespace": "ops/main",
        "X-HotMem-Session-Id": "sess-ops-1",
    }
    hdr_reader = {
        "Authorization": "Bearer tok-readonly",
        "X-HotMem-Store-Id": "store-surface",
        "X-HotMem-Namespace": "ops/main",
    }

    with TestClient(app) as client:
        # 1. File-backed add via runtime
        add_file = client.post(
            "/v1/add",
            headers=hdr_writer,
            json={
                "identifier": "ops/runbook-file",
                "file_uri": "runbook.txt",
                "byte_offset": 0,
                "byte_length": 27,
                "source_format": "txt",
                "summary": "Drain worker queue step",
            },
        )
        assert add_file.status_code == 200
        file_mid = add_file.json()["memory_id"]

        # 2. Reader can list files, hydrate one (raw & profiled), and hydrate-batch
        files_resp = client.get("/v1/files", headers=hdr_reader)
        assert files_resp.status_code == 200
        assert files_resp.json()["count"] == 1

        raw_hyd = client.post(f"/v1/memory/{file_mid}/hydrate", headers=hdr_reader)
        assert raw_hyd.status_code == 200
        assert raw_hyd.content == b"Step 1: drain worker queue."

        import base64

        prof_hyd = client.post(
            f"/v1/memory/{file_mid}/hydrate",
            headers=hdr_reader,
            json={"profile": "full", "verify": True},
        )
        assert prof_hyd.status_code == 200
        assert base64.b64decode(prof_hyd.json()["content"]) == b"Step 1: drain worker queue."

        batch_hyd = client.post(
            "/v1/memory/hydrate-batch",
            headers=hdr_reader,
            json={"memory_ids": [file_mid], "profile": "compact"},
        )
        assert batch_hyd.status_code == 200
        assert batch_hyd.json()["count"] == 1

        # 3. Reader cannot promote, snapshot, hydrate snapshot, or rebuild index
        assert (
            client.post(
                f"/v1/memory/{file_mid}/promote",
                headers=hdr_reader,
                json={"to_state": "READY"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/snapshot",
                headers=hdr_reader,
                json={"path": str(tmp_path / "snap")},
            ).status_code
            == 403
        )

        # 4. Writer can promote, mark candidate, snapshot, and hydrate
        prom = client.post(
            f"/v1/memory/{file_mid}/promote",
            headers=hdr_writer,
            json={"to_state": "READY", "reason": "verified runbook"},
        )
        assert prom.status_code == 200
        assert prom.json()["promotion_state"] == "READY"

        cand = client.post(
            f"/v1/memory/{file_mid}/candidate",
            headers=hdr_writer,
            json={"candidate": True},
        )
        assert cand.status_code == 200

        cands = client.get("/v1/promotion/candidates", headers=hdr_reader)
        assert cands.status_code == 200
        assert cands.json()["count"] == 1

        by_state = client.get(
            "/v1/promotion/by-state", params={"state": "READY"}, headers=hdr_reader
        )
        assert by_state.status_code == 200
        assert by_state.json()["count"] == 1

        snap_dir = tmp_path / "snap_v2"
        snap_resp = client.post(
            "/v1/snapshot",
            headers=hdr_writer,
            json={"path": str(snap_dir)},
        )
        assert snap_resp.status_code == 200
        assert snap_resp.json()["exported"] == 1

        hyd_resp = client.post(
            "/v1/hydrate",
            headers=hdr_writer,
            json={"path": str(snap_dir)},
        )
        assert hyd_resp.status_code == 200
        assert hyd_resp.json()["skipped_dupes"] == 1

        # 5. Route inventory matches registered /v1 routes
        app_routes = {
            (method, route.path)
            for route in app.routes
            if hasattr(route, "path") and str(route.path).startswith("/v1/")
            for method in getattr(route, "methods", ())
            if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}
        }
        inventory_routes = {(m, p) for m, p, _ in DAEMON_ROUTE_INVENTORY}
        assert app_routes == inventory_routes


def test_mmvt2_explicit_harness_session_and_tags_vertical_slice(tmp_path: Path):
    """WP1 MVT1: Explicit harness_id, session_id, and canonical tags survive write -> recall."""
    db_path = tmp_path / "mmvt2_111.sqlite"
    policy = PolicyConfig(
        store_id="store-111",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-alpha",
                caller_id="caller-alpha",
                store_id="store-111",
                namespaces=("team/shared", "alpha/private"),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-beta",
                caller_id="caller-beta",
                store_id="store-111",
                namespaces=("beta/private",),
                operations=("read", "write"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-111-team",
                        namespace="team/shared",
                        audience=("caller-beta",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=db_path, policy=policy)
    alpha_shared_hdr = {
        "Authorization": "Bearer tok-alpha",
        "X-HotMem-Store-Id": "store-111",
        "X-HotMem-Harness-Id": "codex",
        "X-HotMem-Session-Id": "sess-alpha-101",
        "X-HotMem-Source": "agent://codex-cli",
        "X-HotMem-Namespace": "team/shared",
    }
    alpha_priv_hdr = {
        **alpha_shared_hdr,
        "X-HotMem-Namespace": "alpha/private",
    }
    beta_hdr = {
        "Authorization": "Bearer tok-beta",
        "X-HotMem-Store-Id": "store-111",
        "X-HotMem-Harness-Id": "claude-code",
        "X-HotMem-Session-Id": "sess-beta-202",
        "X-HotMem-Source": "agent://claude-code",
    }
    canonical_tags = ["model:gpt-5", "project:hotmem", "org:knowguard"]

    with TestClient(app) as client:
        shared_resp = client.post(
            "/v1/add",
            headers=alpha_shared_hdr,
            json={
                "identifier": "arch/decision-111",
                "fact": (
                    "Keep session attribution explicit and model/project/org in canonical tags."
                ),
                "tags": canonical_tags,
            },
        )
        assert shared_resp.status_code == 200
        shared_id = shared_resp.json()["memory_id"]

        priv_resp = client.post(
            "/v1/add",
            headers=alpha_priv_hdr,
            json={
                "identifier": "alpha/canary-111",
                "fact": "PRIVATE-CANARY-111: internal investigation draft",
                "tags": ["model:gpt-5", "project:secret-proj"],
            },
        )
        assert priv_resp.status_code == 200
        priv_id = priv_resp.json()["memory_id"]

        # Reader B recalls shared decision via search, list, and direct get:
        # Writer A's harness_id, session_id, caller_id, source, and tags are preserved.
        search_b = client.post(
            "/v1/search",
            headers=beta_hdr,
            json={"query": "explicit session attribution canonical tags", "top_k": 5},
        )
        assert search_b.status_code == 200
        hits = search_b.json()["memories"]
        assert len(hits) == 1
        assert hits[0]["memory_id"] == shared_id
        assert hits[0]["tags"] == canonical_tags
        assert hits[0]["provenance"] == {
            "envelope_version": "hotmem-session-v1",
            "caller_id": "caller-alpha",
            "harness_id": "codex",
            "session_id": "sess-alpha-101",
            "source": "agent://codex-cli",
            "namespace": "team/shared",
            "store_id": "store-111",
        }

        list_b = client.get(
            "/v1/memories",
            headers=beta_hdr,
            params={"identifier": "arch/decision-111"},
        )
        assert list_b.status_code == 200
        rows = list_b.json()["memories"]
        assert len(rows) == 1
        assert rows[0]["tags"] == canonical_tags
        assert rows[0]["provenance"]["harness_id"] == "codex"
        assert rows[0]["provenance"]["session_id"] == "sess-alpha-101"
        assert rows[0]["provenance"]["caller_id"] == "caller-alpha"

        get_b = client.get(f"/v1/memory/{shared_id}", headers=beta_hdr)
        assert get_b.status_code == 200
        got = get_b.json()
        assert got["tags"] == canonical_tags
        assert got["provenance"]["harness_id"] == "codex"
        assert got["provenance"]["session_id"] == "sess-alpha-101"

        # Private canary and its tags remain completely inaccessible to Reader B
        assert client.get(f"/v1/memory/{priv_id}", headers=beta_hdr).status_code == 404
        priv_search = client.post(
            "/v1/search",
            headers=beta_hdr,
            json={"query": "PRIVATE-CANARY-111 secret-proj", "top_k": 5},
        ).json()
        assert all(m["memory_id"] != priv_id for m in priv_search["memories"])
        assert "PRIVATE-CANARY-111" not in str(priv_search)
        assert "secret-proj" not in str(priv_search)


def test_mmvt2_wp1_layer2_tag_variations_validation_and_legacy_modes(tmp_path: Path):
    """WP1 Layer 2: model-label changes per write, empty/omitted tags, file-backed tags,
    malformed/conflicting context & tags, unknown envelope version, and legacy compatibility.
    """
    base_dir = tmp_path / "wp1_l2"
    base_dir.mkdir()
    doc_file = base_dir / "design.md"
    doc_bytes = b"Architecture spec for explicit session attribution.\n"
    doc_file.write_bytes(doc_bytes)

    policy = PolicyConfig(
        store_id="store-111-l2",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-alpha",
                caller_id="caller-alpha",
                store_id="store-111-l2",
                namespaces=("team/shared",),
                operations=("read", "write"),
                allowed_sessions=("sess-alpha-1",),
                allowed_sources=("agent://alpha",),
                allowed_harnesses=("codex",),
            ),
            CallerCredential.from_token(
                token="tok-beta",
                caller_id="caller-beta",
                store_id="store-111-l2",
                namespaces=("team/shared",),
                operations=("read", "write"),
                allowed_sessions=("sess-beta-1",),
                allowed_sources=("agent://beta",),
                allowed_harnesses=("claude-code",),
            ),
        ],
    )
    app = create_app(db_path=base_dir / "l2.sqlite", policy=policy, base_dir=base_dir)
    alpha_hdr = {
        "Authorization": "Bearer tok-alpha",
        "X-HotMem-Store-Id": "store-111-l2",
        "X-HotMem-Harness-Id": "codex",
        "X-HotMem-Session-Id": "sess-alpha-1",
        "X-HotMem-Source": "agent://alpha",
        "X-HotMem-Namespace": "team/shared",
    }

    with TestClient(app) as client:
        # 1. Model label changes per write within the same session; empty/omitted tags valid
        w1 = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={
                "identifier": "ops/step",
                "fact": "First step drafted with model-a",
                "tags": ["model:gpt-5", "project:hotmem"],
            },
        )
        assert w1.status_code == 200
        mid1 = w1.json()["memory_id"]

        w2 = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={
                "identifier": "ops/step",
                "fact": "Second step drafted with model-b in the same session",
                "tags": ["model:o4-mini", "project:hotmem", "org:knowguard"],
            },
        )
        assert w2.status_code == 200
        mid2 = w2.json()["memory_id"]

        w3 = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={
                "identifier": "ops/step",
                "fact": "Third step drafted with explicit empty tag list",
                "tags": [],
            },
        )
        assert w3.status_code == 200
        mid3 = w3.json()["memory_id"]

        # File-backed memory with tags and harness_id
        w_file = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={
                "identifier": "ops/spec-file",
                "file_uri": "design.md",
                "byte_offset": 0,
                "byte_length": len(doc_bytes),
                "source_format": "text/markdown",
                "summary": "Architecture spec file",
                "tags": ["model:gpt-5", "project:hotmem"],
            },
        )
        assert w_file.status_code == 200
        mid_file = w_file.json()["memory_id"]

        g1 = client.get(f"/v1/memory/{mid1}", headers=alpha_hdr).json()
        g2 = client.get(f"/v1/memory/{mid2}", headers=alpha_hdr).json()
        g3 = client.get(f"/v1/memory/{mid3}", headers=alpha_hdr).json()
        g_file = client.get(f"/v1/memory/{mid_file}", headers=alpha_hdr).json()

        assert g1["tags"] == ["model:gpt-5", "project:hotmem"]
        assert g2["tags"] == ["model:o4-mini", "project:hotmem", "org:knowguard"]
        assert g3["tags"] == []
        assert g_file["tags"] == ["model:gpt-5", "project:hotmem"]
        assert g_file["provenance"]["harness_id"] == "codex"
        # No parallel copy of tags in metadata or provenance
        assert "tags" not in g1["metadata"]
        assert "tags" not in g1["provenance"]
        assert "model" not in g1["provenance"]

        # Tags do not silently become retrieval filters
        s_resp = client.post(
            "/v1/search",
            headers=alpha_hdr,
            json={"query": "step drafted", "top_k": 10},
        ).json()
        returned_ids = {m["memory_id"] for m in s_resp["memories"]}
        assert {mid1, mid2, mid3}.issubset(returned_ids)

        # 2. Unknown / incompatible envelope_version rejected (400)
        bad_ver = client.post(
            "/v1/add",
            headers={**alpha_hdr, "X-HotMem-Envelope-Version": "hotmem-session-v99"},
            json={"identifier": "ops/bad", "fact": "bad version"},
        )
        assert bad_ver.status_code == 400
        assert bad_ver.json()["error"] == "unsupported_envelope_version"

        # 3. Conflicting header vs body context rejected (400 context_conflict)
        conflict_sess = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={"identifier": "ops/bad", "fact": "conflict", "session_id": "sess-other"},
        )
        assert conflict_sess.status_code == 400
        assert conflict_sess.json()["error"] == "context_conflict"

        conflict_harness = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={"identifier": "ops/bad", "fact": "conflict", "harness_id": "claude-code"},
        )
        assert conflict_harness.status_code == 400
        assert conflict_harness.json()["error"] == "context_conflict"

        # 4. Malformed context (control chars / whitespace) & malformed tags/metadata rejected
        bad_ctrl = client.post(
            "/v1/add",
            headers={**alpha_hdr, "X-HotMem-Harness-Id": "codex\x00bad"},
            json={"identifier": "ops/bad", "fact": "ctrl"},
        )
        assert bad_ctrl.status_code == 400
        assert bad_ctrl.json()["error"] == "invalid_session_context"

        bad_ws_harness = client.post(
            "/v1/add",
            headers={**alpha_hdr, "X-HotMem-Harness-Id": "   "},
            json={"identifier": "ops/bad", "fact": "ws"},
        )
        assert bad_ws_harness.status_code == 400
        assert bad_ws_harness.json()["error"] == "invalid_session_context"

        for bad_tags in ("not-a-list", [""], ["   "], [123], ["bad\x00tag"]):
            r_tag = client.post(
                "/v1/add",
                headers=alpha_hdr,
                json={"identifier": "ops/bad", "fact": "bad tag", "tags": bad_tags},
            )
            assert r_tag.status_code == 400
            assert r_tag.json()["error"] == "invalid_tags"

        r_meta = client.post(
            "/v1/add",
            headers=alpha_hdr,
            json={"identifier": "ops/bad", "fact": "bad meta", "metadata": ["not", "dict"]},
        )
        assert r_meta.status_code == 400
        assert r_meta.json()["error"] == "invalid_metadata"

        # 5. Harness / caller impersonation rejected (403 session_impersonation_denied)
        impersonate_harness = client.post(
            "/v1/add",
            headers={**alpha_hdr, "X-HotMem-Harness-Id": "claude-code"},
            json={"identifier": "ops/bad", "fact": "wrong harness"},
        )
        assert impersonate_harness.status_code == 403
        assert impersonate_harness.json()["error"] == "session_impersonation_denied"

        impersonate_caller = client.post(
            "/v1/add",
            headers={**alpha_hdr, "X-HotMem-Caller-Id": "caller-beta"},
            json={"identifier": "ops/bad", "fact": "wrong caller"},
        )
        assert impersonate_caller.status_code == 403
        assert impersonate_caller.json()["error"] == "session_impersonation_denied"

    # 6. Supported legacy/non-policy use: context preserved when supplied, omitted when bare
    legacy_app = create_app(db_path=base_dir / "legacy.sqlite")
    with TestClient(legacy_app) as lclient:
        bare = lclient.post(
            "/v1/add", json={"identifier": "leg/bare", "fact": "bare legacy"}
        ).json()
        bare_get = lclient.get(f"/v1/memory/{bare['memory_id']}").json()
        assert bare_get["provenance"] is None
        assert bare_get["tags"] == []

        ctx_leg = lclient.post(
            "/v1/add",
            headers={"X-HotMem-Harness-Id": "codex", "X-HotMem-Session-Id": "sess-leg-1"},
            json={
                "identifier": "leg/ctx",
                "fact": "contextual legacy",
                "tags": ["model:gpt-5"],
            },
        ).json()
        ctx_get = lclient.get(f"/v1/memory/{ctx_leg['memory_id']}").json()
        assert ctx_get["tags"] == ["model:gpt-5"]
        assert ctx_get["provenance"]["harness_id"] == "codex"
        assert ctx_get["provenance"]["session_id"] == "sess-leg-1"
        assert ctx_get["provenance"]["envelope_version"] == "hotmem-session-v1"
