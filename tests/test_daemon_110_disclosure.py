"""Layered MVT verification for #110 — Scoped disclosure & privacy controls."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.server import create_app


def _hdr(
    token: str,
    *,
    store_id: str = "store-110",
    ns: str | None = None,
    session: str | None = None,
    source: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-HotMem-Store-Id": store_id,
    }
    if ns is not None:
        headers["X-HotMem-Namespace"] = ns
    if session is not None:
        headers["X-HotMem-Session-Id"] = session
    if source is not None:
        headers["X-HotMem-Source"] = source
    return headers


def test_mvt110_1_search_preranking_scope_and_disclosure_modes(tmp_path: Path):
    """MVT-110.1: Pre-ranking scope under 25-row crowding + allow/redact/omit/fail_closed."""
    db_path = tmp_path / "mvt110_1.sqlite"
    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="agent-a",
                store_id="store-110",
                namespaces=("agent-a/private", "shared/ops"),
                operations=("read", "write", "promote", "export", "hydrate"),
                allowed_sessions=("sess-a",),
                allowed_sources=("agent://a",),
                secret_action="allow",
                private_classification_action="allow",
            ),
            CallerCredential.from_token(
                token="tok-b-redact",
                caller_id="agent-b-redact",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "write"),
                allowed_sessions=("sess-b",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-redact",
                        namespace="shared/ops",
                        audience=("agent-b-redact",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-b-omit",
                caller_id="agent-b-omit",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-omit",
                        namespace="shared/ops",
                        audience=("agent-b-omit",),
                        operations=("read",),
                        secret_action="omit",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-b-fail",
                caller_id="agent-b-fail",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-fail",
                        namespace="shared/ops",
                        audience=("agent-b-fail",),
                        operations=("read",),
                        secret_action="fail_closed",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-b-fail-cls",
                caller_id="agent-b-fail-cls",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-fail-cls",
                        namespace="shared/ops",
                        audience=("agent-b-fail-cls",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="fail_closed",
                    ),
                ),
            ),
        ],
    )

    app = create_app(db_path=db_path, base_dir=tmp_path, policy=policy)
    raw_openai_secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    raw_dsn_secret = "postgres://admin:SuperSecret99@db.internal:5432/ops"
    raw_ssn_pii = "123-45-6789"
    canary_token = "CANARY-PRIVATE-A-991"

    with TestClient(app) as client:
        # 1. Seed 25 high-importance private canary records in agent-a/private
        for idx in range(25):
            r = client.post(
                "/v1/add",
                headers=_hdr("tok-a", ns="agent-a/private", session="sess-a", source="agent://a"),
                json={
                    "identifier": f"private/canary-{idx}",
                    "fact": f"Payments incident mitigation private canary {idx} {canary_token}",
                    "importance": 0.99,
                },
            )
            assert r.status_code == 200

        # 2. Seed approved shared decision in shared/ops (lower importance than private canaries)
        shared_clean = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a", source="agent://a"),
            json={
                "identifier": "ops/decision-1",
                "fact": "Payments incident mitigation: pin pool_timeout=15s on gateway.",
                "importance": 0.55,
            },
        )
        assert shared_clean.status_code == 200
        clean_id = shared_clean.json()["memory_id"]

        # 3. Seed shared record containing synthetic secrets + PII in shared/ops
        shared_secret = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a", source="agent://a"),
            json={
                "identifier": "ops/webhook-note",
                "fact": (
                    f"Payments incident mitigation key {raw_openai_secret} "
                    f"{raw_dsn_secret} {raw_ssn_pii}"
                ),
                "importance": 0.90,
            },
        )
        assert shared_secret.status_code == 200
        secret_id = shared_secret.json()["memory_id"]

        # 4. Seed private-classified record in shared/ops (higher importance so it would rank #1)
        shared_cls = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a", source="agent://a"),
            json={
                "identifier": "ops/hr-internal",
                "fact": "Payments incident mitigation internal HR escalation note",
                "importance": 0.95,
                "metadata": {"classification": "private"},
            },
        )
        assert shared_cls.status_code == 200
        cls_id = shared_cls.json()["memory_id"]

        # Mode A: secret_action="redact", private_classification_action="omit"
        resp_redact = client.post(
            "/v1/search",
            headers=_hdr("tok-b-redact"),
            json={"query": "payments incident mitigation", "top_k": 5},
        )
        assert resp_redact.status_code == 200, resp_redact.text
        body_redact = resp_redact.json()
        raw_redact_str = json.dumps(body_redact)

        # Zero private canaries and zero raw secret/PII substrings leaked
        assert canary_token not in raw_redact_str
        assert raw_openai_secret not in raw_redact_str
        assert "SuperSecret99" not in raw_redact_str
        assert raw_ssn_pii not in raw_redact_str
        assert cls_id not in raw_redact_str

        returned_ids = [m["memory_id"] for m in body_redact["memories"]]
        assert clean_id in returned_ids
        assert secret_id in returned_ids
        assert body_redact["count"] == 2

        secret_msg = next(m for m in body_redact["memories"] if m["memory_id"] == secret_id)
        assert "[REDACTED:prefixed_credential]" in secret_msg["content"]
        assert "[REDACTED:dsn_inline_password]" in secret_msg["content"]
        assert "[REDACTED:pii_ssn]" in secret_msg["content"]

        disclosure = body_redact["disclosure"]
        assert disclosure["redacted_records"] == 1
        assert disclosure["redacted_count"] == 3
        assert disclosure["omitted_count"] == 1
        assert "policy_classification" in disclosure["reasons"]
        assert set(disclosure["patterns"]) == {
            "dsn_inline_password",
            "pii_ssn",
            "prefixed_credential",
        }

        # Verify that even when top_k=1, omitting the higher-ranked private-classified record
        # does not starve the next eligible shared record.
        resp_top1 = client.post(
            "/v1/search",
            headers=_hdr("tok-b-omit"),
            json={"query": "payments incident mitigation", "top_k": 1},
        )
        assert resp_top1.status_code == 200
        body_top1 = resp_top1.json()
        assert body_top1["count"] == 1
        assert body_top1["memories"][0]["memory_id"] == clean_id
        assert body_top1["disclosure"]["omitted_count"] == 2
        assert set(body_top1["disclosure"]["reasons"]) == {
            "policy_classification",
            "policy_secret_omitted",
        }

        # Mode B: secret_action="fail_closed" blocks disclosure with 403 and zero raw secret echo
        resp_fail = client.post(
            "/v1/search",
            headers=_hdr("tok-b-fail"),
            json={"query": "payments incident mitigation", "top_k": 5},
        )
        assert resp_fail.status_code == 403
        fail_body = resp_fail.json()
        assert fail_body["error"] == "policy_disclosure_blocked"
        assert fail_body["reason"] == "secret_detected"
        assert set(fail_body["patterns"]) == {
            "dsn_inline_password",
            "pii_ssn",
            "prefixed_credential",
        }
        assert raw_openai_secret not in resp_fail.text
        assert "SuperSecret99" not in resp_fail.text
        assert raw_ssn_pii not in resp_fail.text

        # Mode C: private_classification_action="fail_closed" blocks disclosure with 403
        resp_fail_cls = client.post(
            "/v1/search",
            headers=_hdr("tok-b-fail-cls"),
            json={"query": "payments incident mitigation", "top_k": 5},
        )
        assert resp_fail_cls.status_code == 403
        fail_cls_body = resp_fail_cls.json()
        assert fail_cls_body["error"] == "policy_disclosure_blocked"
        assert fail_cls_body["reason"] == "private_classification"
        assert fail_cls_body["memory_id"] == cls_id


def test_mvt110_2_direct_id_hydration_profiles_and_file_confinement(tmp_path: Path):
    """MVT-110.2: Direct-ID, profiles, file hydration, list, candidates, and path boundary."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True)

    runbook_path = base_dir / "ops_runbook.txt"
    gh_secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyzABCD"
    runbook_path.write_text(
        f"Drain queue; rotate webhook token {gh_secret} before restart.\n",
        encoding="utf-8",
    )
    outside_secret = outside_dir / "host_secret.txt"
    outside_secret.write_text("TOP-SECRET-HOST-FILE", encoding="utf-8")

    db_path = base_dir / "mvt110_2.sqlite"
    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="agent-a",
                store_id="store-110",
                namespaces=("agent-a/private", "shared/ops"),
                operations=("read", "write", "promote"),
            ),
            CallerCredential.from_token(
                token="tok-b-redact",
                caller_id="agent-b-redact",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "write"),
                redact_host_paths=True,
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-b",
                        namespace="shared/ops",
                        audience=("agent-b-redact",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-b-fail",
                caller_id="agent-b-fail",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read",),
                redact_host_paths=True,
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-fail",
                        namespace="shared/ops",
                        audience=("agent-b-fail",),
                        operations=("read",),
                        secret_action="fail_closed",
                        private_classification_action="omit",
                    ),
                ),
            ),
        ],
    )

    app = create_app(db_path=db_path, base_dir=base_dir, policy=policy)
    with TestClient(app) as client:
        # 1. File-ref outside base_dir is rejected fail-closed (403)
        escape_add = client.post(
            "/v1/add",
            headers=_hdr("tok-b-redact", ns="agent-b/private", session="sess-b"),
            json={
                "identifier": "ops/escape",
                "file_uri": str(outside_secret),
                "byte_offset": 0,
                "byte_length": outside_secret.stat().st_size,
                "summary": "attempted host file escape",
            },
        )
        assert escape_add.status_code == 403
        assert escape_add.json()["error"] == "path_not_permitted"

        # 2. Seed private memory, shared file-backed memory (using absolute path inside base_dir),
        #    and shared private-classified candidate
        priv_res = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="agent-a/private", session="sess-a"),
            json={"identifier": "ops/item", "fact": "PRIVATE-CANARY-2201"},
        )
        priv_id = priv_res.json()["memory_id"]

        file_res = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/item",
                "file_uri": str(runbook_path),
                "byte_offset": 0,
                "byte_length": runbook_path.stat().st_size,
                "summary": f"Runbook summary with {gh_secret}",
                "source_format": "text/plain",
            },
        )
        assert file_res.status_code == 200
        file_id = file_res.json()["memory_id"]

        cls_res = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/item",
                "fact": "Shared item classified private",
                "metadata": {"classification": "private"},
            },
        )
        cls_id = cls_res.json()["memory_id"]

        # Mark both shared items as promotion candidates
        assert (
            client.post(
                f"/v1/memory/{file_id}/candidate",
                headers=_hdr("tok-a", ns="shared/ops"),
                json={"candidate": True},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/memory/{cls_id}/candidate",
                headers=_hdr("tok-a", ns="shared/ops"),
                json={"candidate": True},
            ).status_code
            == 200
        )

        # 3. Direct-ID GET /v1/memory/{id}:
        #    - out-of-scope priv_id and omitted cls_id both return uniform 404
        for hidden_id in (priv_id, cls_id, "nonexistent-id-000"):
            r404 = client.get(f"/v1/memory/{hidden_id}", headers=_hdr("tok-b-redact"))
            assert r404.status_code == 404
            assert r404.json() == {"error": "not_found", "memory_id": hidden_id}

        #    - shared file_id returns redacted summary and sanitized relative source_uri
        r_meta = client.get(f"/v1/memory/{file_id}", headers=_hdr("tok-b-redact"))
        assert r_meta.status_code == 200
        meta_body = r_meta.json()
        assert gh_secret not in json.dumps(meta_body)
        assert "[REDACTED:prefixed_credential]" in meta_body["fact_summary"]
        assert meta_body["source_uri"] == "ops_runbook.txt"
        assert str(base_dir) not in json.dumps(meta_body)

        # 4. Profiled hydration (/v1/memory/{id}/hydrate profile="full") redacts content & path
        import base64

        r_prof = client.post(
            f"/v1/memory/{file_id}/hydrate",
            headers=_hdr("tok-b-redact"),
            json={"profile": "full"},
        )
        assert r_prof.status_code == 200
        prof_body = r_prof.json()
        assert gh_secret not in json.dumps(prof_body)
        decoded_prof_content = base64.b64decode(prof_body["content"]).decode("utf-8")
        assert gh_secret not in decoded_prof_content
        assert "[REDACTED:prefixed_credential]" in decoded_prof_content
        assert "[REDACTED:prefixed_credential]" in prof_body["fact_summary"]
        assert prof_body["source_uri"] == "ops_runbook.txt"
        assert str(base_dir) not in json.dumps(prof_body)

        # 5. Raw hydration (/v1/memory/{id}/hydrate) redacts or fails closed per policy
        r_raw = client.post(f"/v1/memory/{file_id}/hydrate", headers=_hdr("tok-b-redact"))
        assert r_raw.status_code == 200
        assert gh_secret.encode() not in r_raw.content
        assert b"[REDACTED:prefixed_credential]" in r_raw.content

        r_raw_fail = client.post(f"/v1/memory/{file_id}/hydrate", headers=_hdr("tok-b-fail"))
        assert r_raw_fail.status_code == 403
        assert r_raw_fail.json()["error"] == "policy_disclosure_blocked"
        assert gh_secret not in r_raw_fail.text

        # 6. Batch hydration (/v1/memory/hydrate-batch)
        r_batch = client.post(
            "/v1/memory/hydrate-batch",
            headers=_hdr("tok-b-redact"),
            json={"memory_ids": [priv_id, cls_id, file_id], "profile": "audit"},
        )
        assert r_batch.status_code == 200
        batch_results = {item["memory_id"]: item for item in r_batch.json()["results"]}
        assert batch_results[priv_id] == {"memory_id": priv_id, "error": "not_found"}
        assert batch_results[cls_id] == {"memory_id": cls_id, "error": "not_found"}
        decoded_batch_content = base64.b64decode(batch_results[file_id]["content"]).decode("utf-8")
        assert gh_secret not in decoded_batch_content
        assert "[REDACTED:prefixed_credential]" in decoded_batch_content
        assert batch_results[file_id]["source_uri"] == "ops_runbook.txt"

        # 7. List (/v1/memories), Files (/v1/files), and Candidates (/v1/promotion/candidates)
        r_list = client.get(
            "/v1/memories",
            headers=_hdr("tok-b-redact"),
            params={"identifier": "ops/item"},
        )
        assert r_list.status_code == 200
        list_ids = [m["id"] for m in r_list.json()["memories"]]
        assert list_ids == [file_id]
        assert gh_secret not in r_list.text
        assert str(base_dir) not in r_list.text

        r_files = client.get("/v1/files", headers=_hdr("tok-b-redact"))
        assert r_files.status_code == 200
        assert [f["memory_id"] for f in r_files.json()["files"]] == [file_id]
        assert r_files.json()["files"][0]["source_uri"] == "ops_runbook.txt"

        r_cands = client.get("/v1/promotion/candidates", headers=_hdr("tok-b-redact"))
        assert r_cands.status_code == 200
        assert [c["memory_id"] for c in r_cands.json()["candidates"]] == [file_id]


def test_mvt110_3_hydrate_and_handoff_inbound_namespace_confinement(tmp_path: Path):
    """MVT-110.3: Inbound hydrate and handoff_hydrate enforce caller write namespace atomically."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir(parents=True)

    # Prepare a valid handoff package from a synthetic codex export
    codex_src = base_dir / "codex_src"
    codex_src.mkdir(parents=True)
    (codex_src / "export.json").write_text(
        json.dumps(
            {
                "format": "codex-export-v1",
                "session": {
                    "id": "codex-sess-110",
                    "label": "ops-handoff",
                },
            }
        ),
        encoding="utf-8",
    )
    (codex_src / "session.jsonl").write_text(
        json.dumps(
            {
                "id": "e1",
                "seq": 1,
                "type": "decision",
                "text": "Decision: keep retry backoff at 250ms for payments worker.",
                "timestamp": "2026-10-23T00:00:01Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    db_path = base_dir / "mvt110_3.sqlite"
    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="agent-a",
                store_id="store-110",
                namespaces=("agent-a/private", "shared/ops"),
                operations=("read", "write", "export", "hydrate"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="agent-b",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "write", "export", "hydrate"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ops-read",
                        namespace="shared/ops",
                        audience=("agent-b",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )

    app = create_app(db_path=db_path, base_dir=base_dir, policy=policy)
    with TestClient(app) as client:
        # 1. Mixed JSONL containing one valid record + one forged record (shared/ops)
        #    must fail closed atomically with 0 rows committed.
        mixed_jsonl = base_dir / "mixed_forged.jsonl"
        mixed_jsonl.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "identifier": "ops/b-ok",
                            "fact_text": "Valid B note that must not commit if batch is forged",
                            "namespace": "agent-b/private",
                        }
                    ),
                    json.dumps(
                        {
                            "identifier": "ops/forged",
                            "fact_text": "Forged write into read-only shared/ops",
                            "namespace": "shared/ops",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        r_forged = client.post(
            "/v1/hydrate",
            headers=_hdr("tok-b"),
            json={"path": str(mixed_jsonl)},
        )
        assert r_forged.status_code == 403
        assert r_forged.json()["error"] == "scope_denied"

        # Verify atomic rejection: even the first valid line was NOT written
        r_check = client.get(
            "/v1/memories",
            headers=_hdr("tok-b"),
            params={"identifier": "ops/b-ok"},
        )
        assert r_check.json()["count"] == 0

        # 2. Caller A with multi-namespace token but header pinned to shared/ops
        #    cannot hydrate a JSONL targeting agent-a/private under that pinned request scope.
        pinned_mismatch = base_dir / "pinned_mismatch.jsonl"
        pinned_mismatch.write_text(
            json.dumps(
                {
                    "identifier": "ops/a-priv",
                    "fact_text": "Targets private namespace while header pins shared/ops",
                    "namespace": "agent-a/private",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        r_pinned = client.post(
            "/v1/hydrate",
            headers=_hdr("tok-a", ns="shared/ops"),
            json={"path": str(pinned_mismatch)},
        )
        assert r_pinned.status_code == 403
        assert r_pinned.json()["error"] == "scope_denied"

        # 3. Prepare and hydrate a handoff package as agent-b:
        #    Handoff records & resume brief must be confined into agent-b/private.
        handoff_out = base_dir / "handoff_pkg"
        r_prep = client.post(
            "/v1/handoff/prepare",
            headers=_hdr("tok-b", ns="agent-b/private"),
            json={
                "source": str(codex_src),
                "output": str(handoff_out),
                "mode": "resume",
                "consent": "approved-by-b",
            },
        )
        assert r_prep.status_code == 200, r_prep.text

        r_hh = client.post(
            "/v1/handoff/hydrate",
            headers=_hdr("tok-b", ns="agent-b/private"),
            json={"package": str(handoff_out)},
        )
        assert r_hh.status_code == 200, r_hh.text
        hh_body = r_hh.json()
        assert hh_body["loaded"] >= 1
        brief_ident = hh_body["brief"]

        # Agent B can read the hydrated handoff brief in agent-b/private
        r_b_brief = client.get(
            "/v1/memories",
            headers=_hdr("tok-b", ns="agent-b/private"),
            params={"identifier": brief_ident},
        )
        assert r_b_brief.status_code == 200
        assert r_b_brief.json()["count"] == 1

        # Agent A cannot see Agent B's hydrated handoff brief
        r_a_brief = client.get(
            "/v1/memories",
            headers=_hdr("tok-a"),
            params={"identifier": brief_ident},
        )
        assert r_a_brief.status_code == 200
        assert r_a_brief.json()["count"] == 0


def test_mvt110_4_scoped_export_modes_attachments_and_handoff_guarantees(tmp_path: Path):
    """MVT-110.4: Scoped snapshot (.jsonl, v2, package) vs handoff guarantees & disclosure modes."""
    from hotmem.snapshot import verify as verify_snapshot_artifact

    base_dir = tmp_path / "workspace"
    base_dir.mkdir(parents=True)

    runbook_path = base_dir / "runbook.txt"
    runbook_path.write_text("Step 1: drain queue before deploy.\n", encoding="utf-8")

    db_path = base_dir / "mvt110_4.sqlite"
    aws_secret = "AKIAIOSFODNN7EXAMPLE"
    private_canary = "CANARY-PRIVATE-EXPORT-881"

    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-a",
                caller_id="agent-a",
                store_id="store-110",
                namespaces=("agent-a/private", "shared/ops"),
                operations=("read", "write", "export", "hydrate"),
                allow_attachments=True,
            ),
            CallerCredential.from_token(
                token="tok-b-redact",
                caller_id="agent-b-redact",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "export", "hydrate"),
                allow_attachments=False,
                redact_host_paths=True,
                share_approvals=(
                    ShareApproval(
                        approval_id="share-export-redact",
                        namespace="shared/ops",
                        audience=("agent-b-redact",),
                        operations=("read", "export"),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-b-omit",
                caller_id="agent-b-omit",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "export"),
                allow_attachments=False,
                redact_host_paths=True,
                share_approvals=(
                    ShareApproval(
                        approval_id="share-export-omit",
                        namespace="shared/ops",
                        audience=("agent-b-omit",),
                        operations=("read", "export"),
                        secret_action="omit",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-b-fail",
                caller_id="agent-b-fail",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "export"),
                allow_attachments=False,
                redact_host_paths=True,
                share_approvals=(
                    ShareApproval(
                        approval_id="share-export-fail",
                        namespace="shared/ops",
                        audience=("agent-b-fail",),
                        operations=("read", "export"),
                        secret_action="fail_closed",
                        private_classification_action="omit",
                    ),
                ),
            ),
        ],
    )

    app = create_app(db_path=db_path, base_dir=base_dir, policy=policy)
    with TestClient(app) as client:
        # Seed 4 records:
        # 1) agent-a/private canary
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="agent-a/private", session="sess-a"),
            json={"identifier": "ops/priv", "fact": private_canary},
        )
        # 2) shared/ops clean decision
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={"identifier": "ops/clean", "fact": "Approved decision: cap retries at 3."},
        )
        # 3) shared/ops secret-bearing note
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={"identifier": "ops/secret", "fact": f"Temp key {aws_secret} used in test"},
        )
        # 4) shared/ops private-classified note + file-backed reference with absolute host path
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/classified",
                "fact": "Internal post-mortem draft",
                "metadata": {"classification": "private"},
            },
        )
        client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/file-ref",
                "file_uri": str(runbook_path),
                "byte_offset": 0,
                "byte_length": runbook_path.stat().st_size,
                "summary": "Drain queue runbook",
                "source_format": "text/plain",
            },
        )

        # 1. Attachment gating: copy_attachments=True is denied (403) when allow_attachments=False
        att_denied_dir = base_dir / "att_denied_v2"
        r_att = client.post(
            "/v1/snapshot",
            headers=_hdr("tok-b-redact"),
            json={"path": str(att_denied_dir), "copy_attachments": True},
        )
        assert r_att.status_code == 403
        assert r_att.json()["error"] == "attachments_not_permitted"
        assert not att_denied_dir.exists()

        # 2. fail_closed mode: aborts before creating any file or directory on disk
        fail_pkg_dir = base_dir / "fail_closed_pkg"
        r_fail = client.post(
            "/v1/snapshot",
            headers=_hdr("tok-b-fail"),
            json={"path": str(fail_pkg_dir), "package": True},
        )
        assert r_fail.status_code == 403
        assert r_fail.json()["error"] == "policy_disclosure_blocked"
        assert r_fail.json()["reason"] == "secret_detected"
        assert aws_secret not in r_fail.text
        assert not fail_pkg_dir.exists()

        # 3. redact mode across legacy .jsonl, Snapshot v2, and Interchange v1 package:
        #    Exports 3 records (ops/clean, redacted ops/secret, sanitized ops/file-ref),
        #    omits 2 records (agent-a/private canary + shared/ops classified).
        jsonl_path = base_dir / "redact_export.jsonl"
        v2_dir = base_dir / "redact_export_v2"
        pkg_dir = base_dir / "redact_export_pkg"

        for target_path, is_pkg in ((jsonl_path, False), (v2_dir, False), (pkg_dir, True)):
            r_exp = client.post(
                "/v1/snapshot",
                headers=_hdr("tok-b-redact"),
                json={"path": str(target_path), "package": is_pkg},
            )
            assert r_exp.status_code == 200, r_exp.text
            exp_body = r_exp.json()
            assert exp_body["exported"] == 3
            assert exp_body["disclosure"]["redacted_count"] == 1
            assert exp_body["disclosure"]["omitted_count"] == 1
            assert "policy_classification" in exp_body["disclosure"]["reasons"]

        # Verify zero secret, canary, or host path in all exported files
        for text_blob in (
            jsonl_path.read_text(encoding="utf-8"),
            (v2_dir / "memories.jsonl").read_text(encoding="utf-8"),
            (v2_dir / "manifest.json").read_text(encoding="utf-8"),
            (v2_dir / "metadata.json").read_text(encoding="utf-8"),
            (pkg_dir / "memories.jsonl").read_text(encoding="utf-8"),
            (pkg_dir / "manifest.json").read_text(encoding="utf-8"),
        ):
            assert private_canary not in text_blob
            assert aws_secret not in text_blob
            assert "Internal post-mortem draft" not in text_blob
            assert str(base_dir) not in text_blob

        assert "[REDACTED:prefixed_credential]" in (pkg_dir / "memories.jsonl").read_text(
            encoding="utf-8"
        )

        # Verify Snapshot v2 and Interchange v1 package pass cryptographic/structural verify()
        v2_verified = verify_snapshot_artifact(v2_dir)
        assert v2_verified["valid"] is True
        assert v2_verified["record_count"] == 3

        pkg_verified = verify_snapshot_artifact(pkg_dir)
        assert pkg_verified["valid"] is True
        assert pkg_verified["record_count"] == 3

        # Verify manifest scope block records exact disclosure counts
        pkg_manifest = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
        assert pkg_manifest["scope"]["exported_count"] == 3
        assert pkg_manifest["scope"]["omitted_count"] == 2
        assert pkg_manifest["scope"]["redacted_count"] == 1

        # 4. omit mode: omits both secret-bearing and private-classified records (exports 2)
        omit_pkg_dir = base_dir / "omit_export_pkg"
        r_omit = client.post(
            "/v1/snapshot",
            headers=_hdr("tok-b-omit"),
            json={"path": str(omit_pkg_dir), "package": True},
        )
        assert r_omit.status_code == 200
        assert r_omit.json()["exported"] == 2
        assert r_omit.json()["disclosure"]["omitted_count"] == 2
        assert set(r_omit.json()["disclosure"]["reasons"]) == {
            "policy_classification",
            "policy_secret_omitted",
        }
        assert verify_snapshot_artifact(omit_pkg_dir)["record_count"] == 2

        # 5. Hydrate the redacted package into a clean store and verify round-trip compatibility
        clean_db = base_dir / "restored.sqlite"
        clean_app = create_app(db_path=clean_db, base_dir=base_dir)
        with TestClient(clean_app) as clean_client:
            r_hyd = clean_client.post("/v1/hydrate", json={"path": str(pkg_dir)})
            assert r_hyd.status_code == 200
            assert r_hyd.json()["loaded"] == 3
            assert r_hyd.json()["invalid"] == 0

        # 6. Contrast with handoff_prepare: handoff requires explicit non-empty consent
        codex_src = base_dir / "codex_handoff_src"
        codex_src.mkdir(parents=True)
        (codex_src / "export.json").write_text(
            json.dumps({"format": "codex-export-v1", "session": {"id": "s-1", "label": "l-1"}}),
            encoding="utf-8",
        )
        (codex_src / "session.jsonl").write_text(
            json.dumps(
                {
                    "id": "e1",
                    "seq": 1,
                    "type": "decision",
                    "text": f"Rotate {aws_secret} and run tool",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        r_no_consent = client.post(
            "/v1/handoff/prepare",
            headers=_hdr("tok-b-redact"),
            json={
                "source": str(codex_src),
                "output": str(base_dir / "handoff_no_consent"),
                "mode": "resume",
                "consent": "   ",
            },
        )
        assert r_no_consent.status_code == 400
        assert not (base_dir / "handoff_no_consent").exists()


def test_mvt110_5_diagnostics_inspect_health_vector_revocation_and_surfaces(tmp_path: Path):
    """MVT-110.5: Health/hygiene/events scoping, warm VectorIndex revocation, and surface parity."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir(parents=True)

    runbook_path = base_dir / "drift_runbook.txt"
    runbook_path.write_text("Original runbook bytes\n", encoding="utf-8")

    db_path = base_dir / "mvt110_5.sqlite"
    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-mgmt",
                caller_id="mgmt-admin",
                store_id="store-110",
                namespaces=("*",),
                operations=("read", "write", "export", "hydrate", "promote", "manage"),
                is_management=True,
            ),
            CallerCredential.from_token(
                token="tok-a",
                caller_id="agent-a",
                store_id="store-110",
                namespaces=("agent-a/private", "shared/ops"),
                operations=("read", "write", "promote"),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="agent-b",
                store_id="store-110",
                namespaces=("agent-b/private",),
                operations=("read", "write", "export"),
                redact_host_paths=True,
                share_approvals=(
                    ShareApproval(
                        approval_id="share-vec-ops",
                        namespace="shared/ops",
                        audience=("agent-b",),
                        operations=("read", "export"),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
        ],
    )

    import math
    from typing import Any

    from hotmem.vector_index import db_fingerprint

    class SmallPoolVectorIndex:
        def __init__(self) -> None:
            self.vectors: dict[str, list[float]] = {}
            self.marker: tuple[int, ...] | None = None
            self.marker_model: str | None = None
            self.marker_dim: int | None = None

        def upsert(self, records: list[dict[str, Any]]) -> None:
            for r in records:
                self.vectors[r["id"]] = list(r["embedding"])

        def apply_rebuild_marker(self, marker: dict[str, Any]) -> None:
            self.marker = (marker["db_count"], marker["max_rowid"], marker["max_event_seq"])
            self.marker_model = str(marker.get("embedding_model") or "") or None
            self.marker_dim = marker.get("embedding_dim") or None

        def search(self, query_embedding: list[float], top_k: int) -> list[dict[str, Any]]:
            scored = []
            for mid, vec in self.vectors.items():
                dot = sum(a * b for a, b in zip(query_embedding, vec, strict=True))
                na = math.sqrt(sum(a * a for a in query_embedding))
                nb = math.sqrt(sum(b * b for b in vec))
                cosine = dot / (na * nb) if na and nb else 0.0
                scored.append({"id": mid, "cosine_score": cosine})
            scored.sort(key=lambda h: (-h["cosine_score"], h["id"]))
            return scored[:top_k]

        def delete(self, memory_ids: list[str]) -> None:
            for mid in memory_ids:
                self.vectors.pop(mid, None)

        def count(self) -> int:
            return len(self.vectors)

        def oversample(self) -> int:
            return 10

        def is_stale(
            self,
            db: Any,
            *,
            embedding_model: str | None = None,
            embedding_dim: int | None = None,
        ) -> bool:
            if self.marker != db_fingerprint(db):
                return True
            if embedding_model is not None and self.marker_model != embedding_model:
                return True
            if embedding_dim is not None:
                return self.marker_dim != embedding_dim
            return False

        def status(self, db: Any) -> dict[str, Any]:
            return {
                "backend": "fake",
                "requested_backend": "fake",
                "dependency_available": True,
                "indexed_count": self.count(),
                "db_count": db.count(),
                "stale": self.is_stale(db),
                "rebuilt_at": None,
                "path": None,
            }

        def clear(self) -> None:
            self.vectors.clear()
            self.marker = None

        def close(self) -> None:
            return None

    vec_idx = SmallPoolVectorIndex()
    app = create_app(
        db_path=db_path,
        base_dir=base_dir,
        policy=policy,
        vector_index=vec_idx,
    )
    with TestClient(app) as client:
        # 1. Seed 30 private memories in agent-a/private, 1 clean shared memory,
        #    1 private-classified shared memory, and 1 file-backed shared memory.
        for idx in range(30):
            client.post(
                "/v1/add",
                headers=_hdr("tok-a", ns="agent-a/private", session="sess-a"),
                json={
                    "identifier": f"priv/vec-{idx}",
                    "fact": f"Database failover drill checklist item {idx} PRIVATE-VEC-CANARY",
                    "importance": 0.95,
                },
            )

        r_shared = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/failover",
                "fact": "Database failover drill: promote replica-2 first.",
                "importance": 0.6,
            },
        )
        shared_id = r_shared.json()["memory_id"]

        r_cls = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/failover-secret-cls",
                "fact": "Database failover drill internal HR note",
                "metadata": {"classification": "private"},
            },
        )
        cls_id = r_cls.json()["memory_id"]

        r_file = client.post(
            "/v1/add",
            headers=_hdr("tok-a", ns="shared/ops", session="sess-a"),
            json={
                "identifier": "ops/drift-file",
                "file_uri": str(runbook_path),
                "byte_offset": 0,
                "byte_length": runbook_path.stat().st_size,
                "summary": "Runbook summary sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                "source_format": "text/plain",
            },
        )
        assert r_file.status_code == 200

        # Build the warm VectorIndex across all 33 memories (requires manage under policy)
        assert client.post("/v1/vector-index/rebuild", headers=_hdr("tok-b")).status_code == 403
        r_idx = client.post("/v1/vector-index/rebuild", headers=_hdr("tok-mgmt"))
        assert r_idx.status_code == 200
        assert r_idx.json()["indexed_count"] >= 32

        # Vector index status scopes db_count/indexed_count and redacts path for non-mgmt
        idx_stat_b = client.get("/v1/vector-index/status", headers=_hdr("tok-b")).json()
        assert idx_stat_b["db_count"] == 2
        assert idx_stat_b["path"] is None

        # 2. Health count & path hygiene:
        #    Agent B sees only the 2 non-omitted shared memories (shared_id + file_id),
        #    not the 30 private memories and not the 1 omitted private-classified memory.
        h_b = client.get("/v1/health", headers=_hdr("tok-b")).json()
        assert h_b["memory_count"] == 2
        assert h_b["total_memory_count"] == 0
        assert h_b["db_path"] is None

        h_mgmt = client.get("/v1/health", headers=_hdr("tok-mgmt")).json()
        assert h_mgmt["total_memory_count"] == 33

        # 3. Delete the backing file to trigger a hygiene warning, then verify /v1/hygiene
        #    and /v1/events strip raw host paths and omit cls_id events for Agent B.
        runbook_path.unlink()
        hyg_b = client.get("/v1/hygiene", headers=_hdr("tok-b")).json()
        assert str(base_dir) not in json.dumps(hyg_b)
        assert "PRIVATE-VEC-CANARY" not in json.dumps(hyg_b)

        ev_b = client.get("/v1/events", headers=_hdr("tok-b")).json()
        ev_b_str = json.dumps(ev_b)
        assert str(base_dir) not in ev_b_str
        assert cls_id not in ev_b_str
        assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in ev_b_str

        # 4. Warm VectorIndex search before and after share revocation (without index rebuild!)
        s_before = client.post(
            "/v1/search",
            headers=_hdr("tok-b"),
            json={"query": "database failover drill", "top_k": 3},
        ).json()
        assert shared_id in [m["memory_id"] for m in s_before["memories"]]
        assert "PRIVATE-VEC-CANARY" not in json.dumps(s_before)

        # Revoke share approval share-vec-ops via management endpoint
        r_rev = client.post(
            "/v1/policy/revoke",
            headers=_hdr("tok-mgmt"),
            json={"approval_id": "share-vec-ops"},
        )
        assert r_rev.status_code == 200
        assert r_rev.json()["revoked"] == 1

        # Immediately after revocation (with VectorIndex still warm and un-rebuilt),
        # Agent B gets 0 results on search, 404 on direct ID, and 0 events.
        s_after = client.post(
            "/v1/search",
            headers=_hdr("tok-b"),
            json={"query": "database failover drill", "top_k": 3},
        ).json()
        assert s_after["count"] == 0
        assert client.get(f"/v1/memory/{shared_id}", headers=_hdr("tok-b")).status_code == 404
        assert client.get("/v1/events", headers=_hdr("tok-b")).json()["count"] == 0
        assert client.get("/v1/health", headers=_hdr("tok-b")).json()["memory_count"] == 0


def test_mvt110_1b_tag_classification_enforced_on_search_and_list(tmp_path: Path):
    """Tag-only private classification must be enforced on search/list, not just direct ID."""
    import json as _json
    import uuid as _uuid

    from hotmem.db import MemoryDB
    from hotmem.embed import DEFAULT_EMBEDDER, pack_embedding

    db_path = tmp_path / "mvt110_1b.sqlite"
    db = MemoryDB(db_path)
    tagged_id = _uuid.uuid4().hex
    db.insert(
        id=tagged_id,
        identifier="ops/tagged-private",
        fact_text="tag-only private canary TAG-CANARY-771",
        embedding=pack_embedding(DEFAULT_EMBEDDER.embed("tag-only private canary TAG-CANARY-771")),
        embedding_dim=DEFAULT_EMBEDDER.descriptor.dimension,
        embedding_model=DEFAULT_EMBEDDER.descriptor.key,
        source="seed",
        importance=0.9,
        metadata_json=_json.dumps({}),
        content_hash="tag-canary-hash",
        ttl_seconds=None,
        namespace="shared/ops",
        tags=_json.dumps(["private"]),
    )
    db.close()

    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-tag-reader",
                caller_id="tag-reader",
                store_id="store-110",
                namespaces=("reader/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-tag",
                        namespace="shared/ops",
                        audience=("tag-reader",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            )
        ],
    )
    app = create_app(db_path=db_path, base_dir=tmp_path, policy=policy)
    with TestClient(app) as client:
        headers = _hdr("tok-tag-reader")
        search = client.post(
            "/v1/search", headers=headers, json={"query": "tag-only private canary", "top_k": 5}
        )
        assert search.status_code == 200
        assert search.json()["memories"] == []
        assert search.json()["disclosure"]["omitted_count"] == 1
        assert "TAG-CANARY-771" not in search.text

        listed = client.get(
            "/v1/memories", headers=headers, params={"identifier": "ops/tagged-private"}
        )
        assert listed.status_code == 200
        assert listed.json()["memories"] == []
        assert listed.json()["disclosure"]["omitted_count"] == 1
        assert "TAG-CANARY-771" not in listed.text

        assert client.get(f"/v1/memory/{tagged_id}", headers=headers).status_code == 404


def test_mvt110_2b_provenance_and_event_payload_disclosure(tmp_path: Path):
    """Provenance, identifier, tags, and event payloads must honor disclosure actions."""
    import json as _json
    import uuid as _uuid

    from hotmem.db import MemoryDB
    from hotmem.embed import DEFAULT_EMBEDDER, pack_embedding

    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    db_path = tmp_path / "mvt110_2b.sqlite"
    db = MemoryDB(db_path)
    prov_id = _uuid.uuid4().hex
    db.insert(
        id=prov_id,
        identifier="ops/provenance",
        fact_text="plain provenance fact",
        embedding=pack_embedding(DEFAULT_EMBEDDER.embed("plain provenance fact")),
        embedding_dim=DEFAULT_EMBEDDER.descriptor.dimension,
        embedding_model=DEFAULT_EMBEDDER.descriptor.key,
        source="seed",
        importance=0.9,
        metadata_json=_json.dumps({}),
        content_hash="prov-hash",
        ttl_seconds=None,
        namespace="shared/ops",
        provenance_json=_json.dumps({"note": f"token {secret} leaked"}),
    )
    db.close()

    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-writer",
                caller_id="writer",
                store_id="store-110",
                namespaces=("shared/ops", "shared/secrets", "shared/cls"),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-read",
                caller_id="reader",
                store_id="store-110",
                namespaces=("reader/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-read",
                        namespace="shared/ops",
                        audience=("reader",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-allow",
                caller_id="allower",
                store_id="store-110",
                namespaces=("allow/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-allow",
                        namespace="shared/ops",
                        audience=("allower",),
                        operations=("read",),
                        secret_action="allow",
                        private_classification_action="allow",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-fail",
                caller_id="failer",
                store_id="store-110",
                namespaces=("fail/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-fail",
                        namespace="shared/ops",
                        audience=("failer",),
                        operations=("read",),
                        secret_action="fail_closed",
                        private_classification_action="omit",
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=db_path, base_dir=tmp_path, policy=policy)
    with TestClient(app) as client:
        added = client.post(
            "/v1/add",
            headers=_hdr("tok-writer", ns="shared/ops", session="s-w"),
            json={
                "identifier": "ops/metadata-secret",
                "fact": "metadata carrying note",
                "metadata": {"note": f"api key {secret}"},
            },
        )
        assert added.status_code == 200

        search = client.post(
            "/v1/search",
            headers=_hdr("tok-read"),
            json={"query": "provenance fact metadata carrying", "top_k": 5},
        )
        assert search.status_code == 200
        assert secret not in search.text
        assert "[REDACTED:prefixed_credential]" in search.text
        assert search.json()["disclosure"]["redacted_count"] >= 2

        got = client.get(f"/v1/memory/{prov_id}", headers=_hdr("tok-read"))
        assert got.status_code == 200
        assert secret not in got.text
        assert "[REDACTED:prefixed_credential]" in got.text

        events = client.get("/v1/events", headers=_hdr("tok-read"))
        assert events.status_code == 200
        assert secret not in events.text
        assert "[REDACTED:prefixed_credential]" in events.text

        allowed = client.get("/v1/events", headers=_hdr("tok-allow"))
        assert allowed.status_code == 200
        assert secret in allowed.text

        blocked = client.get("/v1/events", headers=_hdr("tok-fail"))
        assert blocked.status_code == 403
        assert blocked.json()["error"] == "policy_disclosure_blocked"
        assert secret not in blocked.text


def test_mvt110_2c_sanitize_source_uri_preserves_non_local_uris(tmp_path: Path):
    """Host-path sanitization must not mangle non-local URIs or hide credentials silently."""
    from hotmem.policy import CallerContext

    credential = CallerCredential.from_token(token="t-san", caller_id="san", store_id="s-san")
    ctx = CallerContext(
        authenticated=True,
        policy_enforced=True,
        caller_id="san",
        store_id="s-san",
        credential=credential,
    )
    inside = tmp_path / "inside.txt"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "outside-san.txt"
    outside.write_text("x", encoding="utf-8")

    assert (
        ctx.sanitize_source_uri("https://user:pw@example.com/a?b=1", base_dir=tmp_path)
        == "https://user:pw@example.com/a?b=1"
    )
    assert ctx.sanitize_source_uri("s3://bucket/key", base_dir=tmp_path) == "s3://bucket/key"
    assert ctx.sanitize_source_uri(str(inside), base_dir=tmp_path) == "inside.txt"
    assert ctx.sanitize_source_uri(f"file://{inside}", base_dir=tmp_path) == "inside.txt"
    assert ctx.sanitize_source_uri(str(outside), base_dir=tmp_path) == ""
    assert ctx.sanitize_source_uri("../escape.txt", base_dir=tmp_path) == ""


def test_mvt110_4b_unportable_file_refs_are_omitted_not_dangling(tmp_path: Path):
    """Exports must not emit file-backed records whose path cannot be represented portably."""
    from hotmem.db import MemoryDB
    from hotmem.embed import DEFAULT_EMBEDDER
    from hotmem.memory import FileRef, add_file_backed
    from hotmem.snapshot import hydrate as snapshot_hydrate
    from hotmem.snapshot import verify as verify_snapshot

    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside bytes\n", encoding="utf-8")

    db_path = base_dir / "mvt110_4b.sqlite"
    db = MemoryDB(db_path)
    add_file_backed(
        db,
        "ops/outside-file",
        FileRef(
            source_uri=str(outside),
            byte_offset=0,
            byte_length=outside.stat().st_size,
            source_format="text/plain",
        ),
        summary="outside file reference",
        namespace="shared/ops",
        embedder=DEFAULT_EMBEDDER,
    )
    db.close()

    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-export",
                caller_id="exporter",
                store_id="store-110",
                namespaces=("export/private",),
                operations=("read", "export"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-export",
                        namespace="shared/ops",
                        audience=("exporter",),
                        operations=("read", "export"),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            )
        ],
    )
    app = create_app(db_path=db_path, base_dir=base_dir, policy=policy)
    with TestClient(app) as client:
        result = client.post(
            "/v1/snapshot",
            headers=_hdr("tok-export"),
            json={"path": "export/pkg", "package": True},
        )
        assert result.status_code == 200
        body = result.json()
        assert body["exported"] == 0
        assert body["disclosure"]["omitted_count"] == 1
        assert "file_reference_unavailable" in body["disclosure"]["reasons"]

        pkg = Path(base_dir, body["path"])
        assert verify_snapshot(pkg)["record_count"] == 0
        restored = MemoryDB(base_dir / "restored.sqlite")
        hydrated = snapshot_hydrate(restored, pkg)
        assert hydrated.loaded == 0
        assert hydrated.invalid == 0
        restored.close()


def test_mvt110_2d_binary_hydration_fails_closed_instead_of_corrupting(tmp_path: Path):
    """Non-UTF-8 file-backed content must never be decoded/redacted destructively."""
    from hotmem.db import MemoryDB
    from hotmem.embed import DEFAULT_EMBEDDER
    from hotmem.memory import FileRef, add_file_backed

    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    blob = base_dir / "blob.parquet"
    secret = b"sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe " + secret + b" \x00\x01")

    db_path = base_dir / "mvt110_2d.sqlite"
    db = MemoryDB(db_path)
    memory_id, _ = add_file_backed(
        db,
        "ops/binary",
        FileRef(
            source_uri=str(blob),
            byte_offset=0,
            byte_length=blob.stat().st_size,
            source_format="application/octet-stream",
        ),
        summary="binary blob",
        namespace="shared/ops",
        embedder=DEFAULT_EMBEDDER,
    )
    db.close()

    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-redact",
                caller_id="redactor",
                store_id="store-110",
                namespaces=("redactor/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-bin-redact",
                        namespace="shared/ops",
                        audience=("redactor",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-allow",
                caller_id="allower",
                store_id="store-110",
                namespaces=("allower/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-bin-allow",
                        namespace="shared/ops",
                        audience=("allower",),
                        operations=("read",),
                        secret_action="allow",
                        private_classification_action="allow",
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=db_path, base_dir=base_dir, policy=policy)
    with TestClient(app) as client:
        blocked = client.post(f"/v1/memory/{memory_id}/hydrate", headers=_hdr("tok-redact"))
        assert blocked.status_code == 403
        assert blocked.json()["error"] == "policy_disclosure_blocked"
        assert blocked.json()["reason"] == "non_text_payload"
        assert secret.decode() not in blocked.text

        allowed = client.post(f"/v1/memory/{memory_id}/hydrate", headers=_hdr("tok-allow"))
        assert allowed.status_code == 200
        assert allowed.content == blob.read_bytes()


def test_mvt110_1c_policy_defaults_apply_to_shared_access(tmp_path: Path):
    """PolicyConfig defaults must govern shared access while direct access stays allow."""
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        default_secret_action="omit",
        default_private_classification_action="fail_closed",
        credentials=[
            CallerCredential.from_token(
                token="tok-writer",
                caller_id="writer",
                store_id="store-110",
                namespaces=("shared/ops", "shared/secrets", "shared/cls"),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-secret",
                caller_id="secret-reader",
                store_id="store-110",
                namespaces=("secret/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-default-secret",
                        namespace="shared/secrets",
                        audience=("secret-reader",),
                        operations=("read",),
                    ),
                ),
            ),
            CallerCredential.from_token(
                token="tok-cls",
                caller_id="cls-reader",
                store_id="store-110",
                namespaces=("cls/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-default-cls",
                        namespace="shared/cls",
                        audience=("cls-reader",),
                        operations=("read",),
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=tmp_path / "mvt110_1c.sqlite", base_dir=tmp_path, policy=policy)
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/add",
                headers=_hdr("tok-writer", ns="shared/secrets", session="s-w"),
                json={"identifier": "ops/secret", "fact": f"token {secret} here"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/add",
                headers=_hdr("tok-writer", ns="shared/cls", session="s-w"),
                json={
                    "identifier": "ops/classified",
                    "fact": "classified default note",
                    "metadata": {"classification": "private"},
                },
            ).status_code
            == 200
        )

        omitted = client.post(
            "/v1/search", headers=_hdr("tok-secret"), json={"query": "token here", "top_k": 5}
        )
        assert omitted.status_code == 200
        assert omitted.json()["memories"] == []
        assert omitted.json()["disclosure"]["omitted_count"] == 1
        assert "policy_secret_omitted" in omitted.json()["disclosure"]["reasons"]

        direct = client.post(
            "/v1/search", headers=_hdr("tok-writer"), json={"query": "token here", "top_k": 5}
        )
        assert direct.status_code == 200
        assert secret in direct.text

        blocked = client.post(
            "/v1/search", headers=_hdr("tok-cls"), json={"query": "classified default", "top_k": 5}
        )
        assert blocked.status_code == 403
        assert blocked.json()["reason"] == "private_classification"


def test_mvt110_5b_operation_paths_are_not_disclosed(tmp_path: Path):
    """Scoped responses must not reveal absolute host/base paths."""
    import json as _json

    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    db_path = base_dir / "mvt110_5b.sqlite"
    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-writer",
                caller_id="writer",
                store_id="store-110",
                namespaces=("shared/ops",),
                operations=("read", "write"),
            ),
            CallerCredential.from_token(
                token="tok-export",
                caller_id="exporter",
                store_id="store-110",
                namespaces=("export/private",),
                operations=("read", "export", "hydrate"),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-path",
                        namespace="shared/ops",
                        audience=("exporter",),
                        operations=("read", "export", "hydrate", "write"),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
        ],
    )
    app = create_app(db_path=db_path, base_dir=base_dir, policy=policy)
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/add",
                headers=_hdr("tok-writer", ns="shared/ops", session="s-w"),
                json={"identifier": "ops/path", "fact": "path sanitization fact"},
            ).status_code
            == 200
        )
        exported = client.post(
            "/v1/snapshot",
            headers=_hdr("tok-export"),
            json={"path": "export/pkg", "package": True},
        )
        assert exported.status_code == 200
        assert exported.json()["path"] == "export/pkg"
        assert str(base_dir) not in exported.text

        hydrated = client.post(
            "/v1/hydrate",
            headers=_hdr("tok-export"),
            json={"path": "export/pkg"},
        )
        assert hydrated.status_code == 200
        assert hydrated.json()["path"] == "export/pkg"
        assert str(base_dir) not in hydrated.text

        codex = base_dir / "codex_src"
        codex.mkdir()
        (codex / "export.json").write_text(
            _json.dumps({"format": "codex-export-v1", "session": {"id": "s-path", "label": "l"}}),
            encoding="utf-8",
        )
        (codex / "session.jsonl").write_text(
            _json.dumps({"id": "e1", "seq": 1, "type": "decision", "text": "decision text"}) + "\n",
            encoding="utf-8",
        )
        prepared = client.post(
            "/v1/handoff/prepare",
            headers=_hdr("tok-export"),
            json={
                "source": "codex_src",
                "output": "handoff_pkg",
                "mode": "resume",
                "consent": "approved",
            },
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["path"] == "handoff_pkg"
        assert str(base_dir) not in prepared.text

        inspected = client.get(
            "/v1/handoff/inspect",
            headers=_hdr("tok-export"),
            params={"package": "handoff_pkg"},
        )
        assert inspected.status_code == 200
        assert inspected.json()["path"] == "handoff_pkg"
        assert str(base_dir) not in inspected.text


def test_mvt110_1d_ann_window_does_not_starve_after_disclosure_omission(tmp_path: Path):
    """Disclosure omissions must trigger an exact fallback when ANN candidates are exhausted."""
    import json as _json
    import uuid as _uuid

    from test_vector_index import FakeVectorIndex

    from hotmem.db import MemoryDB
    from hotmem.embed import DEFAULT_EMBEDDER, pack_embedding

    class SmallPoolIndex(FakeVectorIndex):
        def oversample(self) -> int:
            return 10

    db_path = tmp_path / "mvt110_1d.sqlite"
    db = MemoryDB(db_path)
    for idx in range(11):
        text = "payments incident mitigation"
        db.insert(
            id=_uuid.uuid4().hex,
            identifier=f"ops/classified-{idx}",
            fact_text=text,
            embedding=pack_embedding(DEFAULT_EMBEDDER.embed(text)),
            embedding_dim=DEFAULT_EMBEDDER.descriptor.dimension,
            embedding_model=DEFAULT_EMBEDDER.descriptor.key,
            source="seed",
            importance=0.99,
            metadata_json=_json.dumps({"classification": "private"}),
            content_hash=f"cls-{idx}",
            ttl_seconds=None,
            namespace="shared/ops",
        )
    clean_text = "gateway runbook"
    clean_id = _uuid.uuid4().hex
    db.insert(
        id=clean_id,
        identifier="ops/clean-runbook",
        fact_text=clean_text,
        embedding=pack_embedding(DEFAULT_EMBEDDER.embed(clean_text)),
        embedding_dim=DEFAULT_EMBEDDER.descriptor.dimension,
        embedding_model=DEFAULT_EMBEDDER.descriptor.key,
        source="seed",
        importance=0.1,
        metadata_json=_json.dumps({}),
        content_hash="clean-hash",
        ttl_seconds=None,
        namespace="shared/ops",
    )
    db.close()

    policy = PolicyConfig(
        store_id="store-110",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-mgmt",
                caller_id="mgmt",
                store_id="store-110",
                namespaces=("*",),
                operations=("read", "write", "export", "hydrate", "promote", "manage"),
                is_management=True,
            ),
            CallerCredential.from_token(
                token="tok-reader",
                caller_id="reader",
                store_id="store-110",
                namespaces=("reader/private",),
                operations=("read",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-ann",
                        namespace="shared/ops",
                        audience=("reader",),
                        operations=("read",),
                        secret_action="redact",
                        private_classification_action="omit",
                    ),
                ),
            ),
        ],
    )
    index = SmallPoolIndex()
    app = create_app(db_path=db_path, base_dir=tmp_path, policy=policy, vector_index=index)
    with TestClient(app) as client:
        rebuilt = client.post("/v1/vector-index/rebuild", headers=_hdr("tok-mgmt"))
        assert rebuilt.status_code == 200, rebuilt.text

        result = client.post(
            "/v1/search",
            headers=_hdr("tok-reader"),
            json={"query": "payments incident mitigation", "top_k": 1},
        )
        assert result.status_code == 200
        assert result.json()["count"] == 1
        assert result.json()["memories"][0]["memory_id"] == clean_id
