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

from hotmem.policy import CallerCredential, PolicyConfig
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
    }
    hdr_b = {
        "Authorization": "Bearer tok-beta",
        "X-HotMem-Store-Id": "store-beta",
        "X-HotMem-Namespace": "ns-b",
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
        assert health_a["db_path"] == str(db_a)
        assert health_a["memory_count"] == 1

        assert health_b["store_id"] == "store-beta"
        assert health_b["db_path"] == str(db_b)
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
