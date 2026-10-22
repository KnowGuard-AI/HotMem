"""MMVT 1 — First executable incident contract (#109, #110, #111).

Question/test:
    Can two callers use one intended backend while sharing only approved context?
    - One application/store and two caller identities (caller A, caller B).
    - A writes a shared incident decision (DEMO-17 rollback) and a private canary note.
    - A can retrieve its private canary.
    - B recalls the approved shared decision with explicit source/session attribution.
    - B attempts private search, direct-ID lookup, hydration, list, events, and mutation:
      all must be denied/filtered with zero canary leakage in responses, errors, counts, or events.
    - Missing credentials, revoked credentials, wrong store_id, and actor/session impersonation
      fail clearly.
    - Repeat the same contract with two pooled HTTP clients (HotMemClient) against one
      explicitly started loopback process.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hotmem.client import HotMemClient
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.server import create_app


@pytest.fixture
def incident_policy() -> PolicyConfig:
    """Two-caller incident policy for store 'store-incident-01'."""
    return PolicyConfig(
        store_id="store-incident-01",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-caller-a-secret",
                caller_id="caller-a",
                store_id="store-incident-01",
                namespaces=("ops/private", "ops/shared"),
                operations=("read", "write", "export", "hydrate", "promote"),
                allowed_sessions=("sess-a-investigation",),
                allowed_sources=("agent://investigator-a",),
            ),
            CallerCredential.from_token(
                token="tok-caller-b-secret",
                caller_id="caller-b",
                store_id="store-incident-01",
                namespaces=("ops/b-scratch",),
                operations=("read", "write"),
                allowed_sessions=("sess-b-recovery",),
                allowed_sources=("agent://recovery-b",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-demo-17",
                        namespace="ops/shared",
                        identifiers=("ops/demo-17",),
                        audience=("caller-b",),
                        operations=("read",),
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-revoked-secret",
                caller_id="caller-revoked",
                store_id="store-incident-01",
                namespaces=("ops/shared",),
                operations=("read",),
                revoked=True,
            ),
        ],
    )


def _headers(
    token: str,
    *,
    session_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    store_id: str = "store-incident-01",
) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {token}",
        "X-HotMem-Store-Id": store_id,
    }
    if session_id is not None:
        h["X-HotMem-Session-Id"] = session_id
    if source is not None:
        h["X-HotMem-Source"] = source
    if namespace is not None:
        h["X-HotMem-Namespace"] = namespace
    return h


def test_mmvt1_incident_isolation_and_attribution_in_process(
    tmp_path: Path, incident_policy: PolicyConfig
):
    """MMVT 1 core contract: A & B share only approved decision; canary stays isolated."""
    db_path = tmp_path / "incident.sqlite"
    app = create_app(db_path=db_path, policy=incident_policy)

    canary_secret = "CANARY-SECRET-9941: root token leaked in staging worker env"
    shared_decision = (
        "Incident DEMO-17: roll back worker release v2.4.1 before resuming the queue."
    )

    with TestClient(app) as client:
        # 1. Missing credentials fail closed with 401
        unauth_resp = client.get("/v1/health")
        assert unauth_resp.status_code == 401
        assert unauth_resp.json()["error"] == "missing_credential"

        # 2. Revoked credentials fail closed with 403
        revoked_resp = client.get(
            "/v1/health",
            headers=_headers("tok-revoked-secret"),
        )
        assert revoked_resp.status_code == 403
        assert revoked_resp.json()["error"] == "revoked_credential"

        # 3. Wrong store_id fails closed with 409 store_mismatch
        wrong_store_resp = client.get(
            "/v1/health",
            headers=_headers("tok-caller-a-secret", store_id="wrong-store-99"),
        )
        assert wrong_store_resp.status_code == 409
        assert wrong_store_resp.json()["error"] == "store_mismatch"

        # 4. Caller A writes approved shared decision with explicit session/source attribution
        headers_a_shared = _headers(
            "tok-caller-a-secret",
            session_id="sess-a-investigation",
            source="agent://investigator-a",
            namespace="ops/shared",
        )
        add_shared = client.post(
            "/v1/add",
            headers=headers_a_shared,
            json={
                "identifier": "ops/demo-17",
                "fact": shared_decision,
                "source": "agent://investigator-a",
                "importance": 0.9,
            },
        )
        assert add_shared.status_code == 200, add_shared.text
        shared_id = add_shared.json()["memory_id"]

        # 5. Caller A writes private investigation canary in ops/private
        headers_a_priv = _headers(
            "tok-caller-a-secret",
            session_id="sess-a-investigation",
            source="agent://investigator-a",
            namespace="ops/private",
        )
        add_priv = client.post(
            "/v1/add",
            headers=headers_a_priv,
            json={
                "identifier": "ops/canary-note",
                "fact": canary_secret,
                "source": "agent://investigator-a",
                "importance": 0.95,
            },
        )
        assert add_priv.status_code == 200, add_priv.text
        canary_id = add_priv.json()["memory_id"]

        # 6. Caller A can retrieve its own private canary via search and direct ID
        search_a = client.post(
            "/v1/search",
            headers=headers_a_priv,
            json={"query": "CANARY-SECRET-9941 staging worker", "top_k": 5},
        )
        assert search_a.status_code == 200
        assert any(canary_secret in m["content"] for m in search_a.json()["memories"])

        get_a = client.get(f"/v1/memory/{canary_id}", headers=headers_a_priv)
        assert get_a.status_code == 200
        assert get_a.json()["fact_text"] == canary_secret

        # 7. Caller B recalls the approved shared decision with source/session attribution
        headers_b = _headers(
            "tok-caller-b-secret",
            session_id="sess-b-recovery",
            source="agent://recovery-b",
        )
        search_b_shared = client.post(
            "/v1/search",
            headers=headers_b,
            json={"query": "DEMO-17 roll back worker release", "top_k": 5},
        )
        assert search_b_shared.status_code == 200
        b_memories = search_b_shared.json()["memories"]
        assert len(b_memories) == 1
        assert b_memories[0]["memory_id"] == shared_id
        assert shared_decision in b_memories[0]["content"]
        assert b_memories[0]["provenance"]["caller_id"] == "caller-a"
        assert b_memories[0]["provenance"]["session_id"] == "sess-a-investigation"
        assert b_memories[0]["provenance"]["source"] == "agent://investigator-a"
        assert b_memories[0]["provenance"]["namespace"] == "ops/shared"

        # Direct metadata lookup of shared decision by B succeeds with attribution
        get_b_shared = client.get(f"/v1/memory/{shared_id}", headers=headers_b)
        assert get_b_shared.status_code == 200
        assert get_b_shared.json()["provenance"]["session_id"] == "sess-a-investigation"

        # 8. Caller B attempts private search, direct-ID lookup, hydration, list, events, mutation
        search_b_priv = client.post(
            "/v1/search",
            headers=headers_b,
            json={"query": "CANARY-SECRET-9941 root token staging", "top_k": 10},
        )
        assert search_b_priv.status_code == 200
        for m in search_b_priv.json()["memories"]:
            assert canary_secret not in m["content"]
            assert m["memory_id"] != canary_id

        # Requesting private namespace explicitly fails with 403 scope_denied
        search_b_explicit_ns = client.post(
            "/v1/search",
            headers=_headers(
                "tok-caller-b-secret",
                session_id="sess-b-recovery",
                source="agent://recovery-b",
                namespace="ops/private",
            ),
            json={"query": "CANARY", "top_k": 5},
        )
        assert search_b_explicit_ns.status_code == 403
        assert search_b_explicit_ns.json()["error"] == "scope_denied"
        assert canary_secret not in search_b_explicit_ns.text

        # Direct ID access to canary by B is denied without leaking content
        get_b_canary = client.get(f"/v1/memory/{canary_id}", headers=headers_b)
        assert get_b_canary.status_code in (403, 404)
        assert canary_secret not in get_b_canary.text

        # Hydrate canary by B is denied
        hyd_b_canary = client.post(f"/v1/memory/{canary_id}/hydrate", headers=headers_b)
        assert hyd_b_canary.status_code in (403, 404)
        assert canary_secret not in hyd_b_canary.text

        # List memories for canary identifier by B returns 0 items
        list_b_canary = client.get(
            "/v1/memories", params={"identifier": "ops/canary-note"}, headers=headers_b
        )
        assert list_b_canary.status_code == 200
        assert list_b_canary.json()["count"] == 0

        # Events queried by B exclude ops/private canary and strip raw private state
        events_b = client.get("/v1/events", headers=headers_b)
        assert events_b.status_code == 200
        assert canary_secret not in events_b.text
        assert canary_id not in events_b.text

        # Health count visible to B does not include A's private canary (only B-visible count)
        health_b = client.get("/v1/health", headers=headers_b)
        assert health_b.status_code == 200
        assert health_b.json()["memory_count"] == 1
        assert health_b.json()["store_id"] == "store-incident-01"

        # 9. Actor / session / source impersonation by B is rejected with 403
        impersonate_session = client.post(
            "/v1/add",
            headers=_headers(
                "tok-caller-b-secret",
                session_id="sess-a-investigation",  # belongs to A, not B
                source="agent://recovery-b",
                namespace="ops/b-scratch",
            ),
            json={
                "identifier": "ops/spoof",
                "fact": "attempting session spoofing",
            },
        )
        assert impersonate_session.status_code == 403
        assert impersonate_session.json()["error"] == "session_impersonation_denied"

        impersonate_source = client.post(
            "/v1/add",
            headers=_headers(
                "tok-caller-b-secret",
                session_id="sess-b-recovery",
                source="agent://investigator-a",  # belongs to A
                namespace="ops/b-scratch",
            ),
            json={
                "identifier": "ops/spoof",
                "fact": "attempting source spoofing",
                "source": "agent://investigator-a",
            },
        )
        assert impersonate_source.status_code == 403
        assert impersonate_source.json()["error"] == "session_impersonation_denied"

        # B attempting to write into ops/shared (where B only has read share approval) fails 403
        b_write_shared = client.post(
            "/v1/add",
            headers=_headers(
                "tok-caller-b-secret",
                session_id="sess-b-recovery",
                source="agent://recovery-b",
                namespace="ops/shared",
            ),
            json={
                "identifier": "ops/demo-17",
                "fact": "unauthorized overwrite of shared decision",
            },
        )
        assert b_write_shared.status_code == 403
        assert b_write_shared.json()["error"] == "scope_denied"


def test_mmvt1_incident_with_two_pooled_http_clients_on_loopback(
    tmp_path: Path, incident_policy: PolicyConfig
):
    """MMVT 1 checkpoint: repeat fixture with two pooled HTTP clients against loopback daemon."""
    import socket
    import threading
    import time

    import uvicorn

    db_path = tmp_path / "loopback_incident.sqlite"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = create_app(db_path=db_path, port=port, policy=incident_policy)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    canary_secret = "CANARY-SECRET-9941: root token leaked in staging worker env"
    shared_decision = (
        "Incident DEMO-17: roll back worker release v2.4.1 before resuming the queue."
    )

    try:
        with (
            HotMemClient(
                base_url,
                token="tok-caller-a-secret",
                session_id="sess-a-investigation",
                source="agent://investigator-a",
                namespace="ops/shared",
                expected_store_id="store-incident-01",
            ) as client_a_shared,
            HotMemClient(
                base_url,
                token="tok-caller-a-secret",
                session_id="sess-a-investigation",
                source="agent://investigator-a",
                namespace="ops/private",
                expected_store_id="store-incident-01",
            ) as client_a_priv,
            HotMemClient(
                base_url,
                token="tok-caller-b-secret",
                session_id="sess-b-recovery",
                source="agent://recovery-b",
                expected_store_id="store-incident-01",
            ) as client_b,
        ):
            # Wait for loopback server startup
            for _ in range(50):
                try:
                    h = client_a_shared.health()
                    if h.get("status") == "ok":
                        break
                except Exception:
                    time.sleep(0.05)

            # A records shared decision and private canary
            res_shared = client_a_shared.add("ops/demo-17", shared_decision, importance=0.9)
            res_priv = client_a_priv.add("ops/canary-note", canary_secret, importance=0.95)

            # A retrieves its private canary
            a_hits = client_a_priv.search("CANARY-SECRET-9941", top_k=5)
            assert any(canary_secret in m["content"] for m in a_hits)

            # B recalls the shared decision with provenance/attribution
            b_hits = client_b.search("DEMO-17 roll back worker", top_k=5)
            assert len(b_hits) == 1
            assert b_hits[0]["memory_id"] == res_shared["memory_id"]
            assert b_hits[0]["provenance"]["caller_id"] == "caller-a"
            assert b_hits[0]["provenance"]["session_id"] == "sess-a-investigation"

            # B cannot search or directly read the canary
            b_priv_hits = client_b.search("CANARY-SECRET-9941", top_k=5)
            for m in b_priv_hits:
                assert canary_secret not in m["content"]
                assert m["memory_id"] != res_priv["memory_id"]

            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                client_b.get_memory(res_priv["memory_id"])
            assert exc_info.value.response.status_code == 404
            assert canary_secret not in exc_info.value.response.text
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

