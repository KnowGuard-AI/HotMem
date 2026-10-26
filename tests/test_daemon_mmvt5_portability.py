"""MMVT 5 — Permitted portable-state path (#109, #99, #100, #110, #111).

Question/test:
    When the incident state is exported through the daemon boundary to
    JSONL/Snapshot v2/interchange package, verified, and restored into a clean
    store, do decision identity, provenance, session attribution, and scope
    survive while private records stay excluded?
    - Permitted records round-trip intact with Record v2 fields (#99:
      updated_at, snapshot_id, parent_memory, related_memories, schema_version,
      promotion_state, provenance/session envelope).
    - Private canary records stay excluded from all exported artifacts.
    - Repeat hydration is idempotent (loaded == 0, skipped_dupes == N).
    - Corrupt or tampered artifacts fail closed (409) before mutating the store.
    - Legacy JSONL artifacts remain readable.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval
from hotmem.server import create_app
from hotmem.snapshot import verify as verify_artifact


def _make_policy(store_id: str) -> PolicyConfig:
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
                allowed_sessions=("sess-a-portability",),
                allowed_sources=("agent://investigator-a",),
            ),
            CallerCredential.from_token(
                token="tok-b",
                caller_id="caller-b",
                store_id=store_id,
                namespaces=("ops/b-local",),
                operations=("read", "write", "export"),
                allowed_sessions=("sess-b-portability",),
                allowed_sources=("agent://recovery-b",),
                share_approvals=(
                    ShareApproval(
                        approval_id="share-portability-demo-17",
                        namespace="ops/shared",
                        identifiers=("ops/demo-17",),
                        audience=("caller-b",),
                        operations=("read", "export"),
                    ),
                ),
            ),
        ],
    )


def test_mmvt5_scoped_daemon_export_verify_restore_and_record_v2_fidelity(
    tmp_path: Path,
):
    """Export permitted incident state across JSONL, v2, and package; restore into clean daemon."""
    src_db = tmp_path / "src.sqlite"
    src_app = create_app(db_path=src_db, policy=_make_policy("store-src"))

    canary_secret = "CANARY-PORTABILITY-7712: private root key rotation schedule"
    shared_decision = "Incident DEMO-17: roll back worker release v2.4.1 before queue resume."

    hdr_a_shared = {
        "Authorization": "Bearer tok-a",
        "X-HotMem-Store-Id": "store-src",
        "X-HotMem-Harness-Id": "codex",
        "X-HotMem-Session-Id": "sess-a-portability",
        "X-HotMem-Source": "agent://investigator-a",
        "X-HotMem-Namespace": "ops/shared",
    }
    hdr_a_priv = {
        **hdr_a_shared,
        "X-HotMem-Namespace": "ops/private",
    }
    hdr_b_src = {
        "Authorization": "Bearer tok-b",
        "X-HotMem-Store-Id": "store-src",
        "X-HotMem-Harness-Id": "claude-code",
        "X-HotMem-Session-Id": "sess-b-portability",
        "X-HotMem-Source": "agent://recovery-b",
    }
    canonical_tags = ["model:gpt-5", "project:hotmem", "org:knowguard"]

    with TestClient(src_app) as src_client:
        # 1. A writes shared decision + private canary
        res_shared = src_client.post(
            "/v1/add",
            headers=hdr_a_shared,
            json={
                "identifier": "ops/demo-17",
                "fact": shared_decision,
                "importance": 0.9,
                "tags": canonical_tags,
            },
        ).json()
        shared_id = res_shared["memory_id"]

        res_priv = src_client.post(
            "/v1/add",
            headers=hdr_a_priv,
            json={
                "identifier": "ops/canary",
                "fact": canary_secret,
                "importance": 0.95,
            },
        ).json()
        canary_id = res_priv["memory_id"]

        # Promote shared decision to READY and enrich Record v2 lineage/relationship columns (#99)
        prom = src_client.post(
            f"/v1/memory/{shared_id}/promote",
            headers=hdr_a_shared,
            json={"to_state": "READY", "reason": "approved for incident handoff"},
        )
        assert prom.status_code == 200
        updated_at = prom.json()["updated_at"]

        # Stamp Record v2 lineage & relationship fields on the shared decision (#99)
        src_app.state.runtime.db._conn.execute(
            """UPDATE memories
               SET snapshot_id = ?, parent_memory = ?, related_memories = ?, schema_version = 2
               WHERE id = ?""",
            ("snap-origin-01", "parent-inc-00", '["rel-1","rel-2"]', shared_id),
        )
        src_app.state.runtime.db.commit()

        # 2. Export through daemon boundary as Caller B (export approval on ops/shared)
        v2_dir = tmp_path / "export_v2"
        pkg_dir = tmp_path / "export_pkg"
        jsonl_file = tmp_path / "export_legacy.jsonl"

        snap_v2 = src_client.post(
            "/v1/snapshot",
            headers=hdr_b_src,
            json={"path": str(v2_dir)},
        )
        assert snap_v2.status_code == 200, snap_v2.text
        assert snap_v2.json()["exported"] == 1

        snap_pkg = src_client.post(
            "/v1/snapshot",
            headers=hdr_b_src,
            json={"path": str(pkg_dir), "package": True, "gz": True},
        )
        assert snap_pkg.status_code == 200, snap_pkg.text
        assert snap_pkg.json()["exported"] == 1

        snap_jsonl = src_client.post(
            "/v1/snapshot",
            headers=hdr_b_src,
            json={"path": str(jsonl_file)},
        )
        assert snap_jsonl.status_code == 200, snap_jsonl.text
        assert snap_jsonl.json()["exported"] == 1

    # 3. Verify artifacts and confirm private canary is completely excluded
    v2_summary = verify_artifact(v2_dir)
    assert v2_summary["valid"] is True
    assert v2_summary["record_count"] == 1
    assert canary_secret not in (v2_dir / "memories.jsonl").read_text(encoding="utf-8")
    assert canary_id not in (v2_dir / "memories.jsonl").read_text(encoding="utf-8")

    pkg_summary = verify_artifact(pkg_dir)
    assert pkg_summary["valid"] is True
    assert pkg_summary["record_count"] == 1
    assert canary_secret not in jsonl_file.read_text(encoding="utf-8")

    # 4. Restore into clean daemon stores for both Snapshot v2 and Interchange Package
    for label, artifact_path in (("v2", v2_dir), ("pkg", pkg_dir), ("jsonl", jsonl_file)):
        dst_store_id = f"store-dst-{label}"
        dst_db = tmp_path / f"dst_{label}.sqlite"
        dst_app = create_app(db_path=dst_db, policy=_make_policy(dst_store_id))
        hdr_a_dst = {
            "Authorization": "Bearer tok-a",
            "X-HotMem-Store-Id": dst_store_id,
            "X-HotMem-Session-Id": "sess-a-portability",
            "X-HotMem-Source": "agent://investigator-a",
            "X-HotMem-Namespace": "ops/shared",
        }
        hdr_b_dst = {
            "Authorization": "Bearer tok-b",
            "X-HotMem-Store-Id": dst_store_id,
            "X-HotMem-Session-Id": "sess-b-portability",
            "X-HotMem-Source": "agent://recovery-b",
        }

        with TestClient(dst_app) as dst_client:
            hyd1 = dst_client.post(
                "/v1/hydrate",
                headers=hdr_a_dst,
                json={"path": str(artifact_path)},
            )
            assert hyd1.status_code == 200, hyd1.text
            assert hyd1.json()["loaded"] == 1
            assert hyd1.json()["skipped_dupes"] == 0

            # Repeat hydration is idempotent
            hyd2 = dst_client.post(
                "/v1/hydrate",
                headers=hdr_a_dst,
                json={"path": str(artifact_path)},
            )
            assert hyd2.status_code == 200
            assert hyd2.json()["loaded"] == 0
            assert hyd2.json()["skipped_dupes"] == 1

            # Caller B recalls the restored decision with full provenance & scope
            b_search = dst_client.post(
                "/v1/search",
                headers=hdr_b_dst,
                json={"query": "DEMO-17 roll back worker release", "top_k": 5},
            )
            assert b_search.status_code == 200
            memories = b_search.json()["memories"]
            assert len(memories) == 1
            assert memories[0]["memory_id"] == shared_id
            assert memories[0]["tags"] == canonical_tags
            assert memories[0]["provenance"]["caller_id"] == "caller-a"
            assert memories[0]["provenance"]["harness_id"] == "codex"
            assert memories[0]["provenance"]["session_id"] == "sess-a-portability"
            assert memories[0]["provenance"]["source"] == "agent://investigator-a"
            assert memories[0]["provenance"]["namespace"] == "ops/shared"
            assert memories[0]["provenance"]["store_id"] == "store-src"

            # Verify Record v2 columns (#99) survived round-trip in SQLite
            restored_row = dst_app.state.runtime.db.get_memory(shared_id)
            assert restored_row is not None
            assert restored_row["namespace"] == "ops/shared"
            assert restored_row["promotion_state"] == "READY"
            assert restored_row["updated_at"] == updated_at
            assert restored_row["snapshot_id"] == "snap-origin-01"
            assert restored_row["parent_memory"] == "parent-inc-00"
            assert json.loads(restored_row["related_memories"]) == ["rel-1", "rel-2"]
            assert restored_row["schema_version"] == 2

            # Private canary is completely absent in destination store
            assert dst_app.state.runtime.db.get_memory(canary_id) is None


def test_mmvt5_corrupt_artifact_fails_before_mutating_daemon_store(tmp_path: Path):
    """Tampered snapshot/package fails with 409 and leaves the target store unchanged."""
    db_path = tmp_path / "clean.sqlite"
    app = create_app(db_path=db_path, policy=_make_policy("store-clean"))
    hdr_a = {
        "Authorization": "Bearer tok-a",
        "X-HotMem-Store-Id": "store-clean",
        "X-HotMem-Session-Id": "sess-a-portability",
        "X-HotMem-Source": "agent://investigator-a",
        "X-HotMem-Namespace": "ops/shared",
    }

    tampered_v2 = tmp_path / "tampered_v2"
    with TestClient(app) as client:
        client.post(
            "/v1/add",
            headers=hdr_a,
            json={"identifier": "ops/demo-17", "fact": "initial valid record"},
        )
        client.post("/v1/snapshot", headers=hdr_a, json={"path": str(tampered_v2)})

        # Corrupt memories.jsonl after manifest checksum computation
        (tampered_v2 / "memories.jsonl").write_text(
            '{"id":"injected","identifier":"ops/injected","fact_text":"corrupt"}\n',
            encoding="utf-8",
        )

        before_count = client.get("/v1/health", headers=hdr_a).json()["memory_count"]
        resp = client.post("/v1/hydrate", headers=hdr_a, json={"path": str(tampered_v2)})
        assert resp.status_code == 409
        assert resp.json()["error"] == "snapshot_checksum_mismatch"
        after_count = client.get("/v1/health", headers=hdr_a).json()["memory_count"]
        assert after_count == before_count
        assert app.state.runtime.db.get_memory("injected") is None

        # Legacy minimal JSONL artifact remains readable
        legacy_jsonl = tmp_path / "legacy_v025.jsonl"
        legacy_jsonl.write_text(
            json.dumps(
                {
                    "id": "legacy-025-1",
                    "identifier": "ops/legacy-note",
                    "fact_text": "Legacy v0.2.5 record without v2 fields",
                    "namespace": "ops/shared",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        leg_resp = client.post("/v1/hydrate", headers=hdr_a, json={"path": str(legacy_jsonl)})
        assert leg_resp.status_code == 200
        assert leg_resp.json()["loaded"] == 1


def test_mmvt5_handoff_source_context_vs_caller_authority_and_legacy_025_fixture(tmp_path: Path):
    """WP5 (#111): Handoff preserves source session/harness/tags distinct from preparing and
    importing callers, and existing 0.2.5 golden handoff packages hydrate cleanly.
    """
    import shutil

    base_dir = tmp_path / "handoff_wp5"
    base_dir.mkdir()
    src_export = base_dir / "codex_src"
    src_export.mkdir()
    (src_export / "export.json").write_text(
        json.dumps(
            {
                "format": "codex-export-v1",
                "harness_id": "codex",
                "session": {
                    "id": "sess-codex-origin-77",
                    "harness_id": "codex",
                    "label": "wp5-handoff",
                    "started_at": "2026-04-20T10:00:00Z",
                    "ended_at": "2026-04-20T10:05:00Z",
                    "codex_version": "0.1.0",
                },
            }
        ),
        encoding="utf-8",
    )
    (src_export / "session.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "e1",
                        "seq": 1,
                        "type": "decision",
                        "text": (
                            "Preserve source session identity separately from importing caller."
                        ),
                        "timestamp": "2026-04-20T10:01:00Z",
                    }
                ),
                json.dumps(
                    {
                        "id": "m1",
                        "seq": 2,
                        "type": "memory_item",
                        "identifier": "arch/handoff-111",
                        "fact": "Handoff memory item carries canonical model/project/org tags.",
                        "importance": 0.9,
                        "tags": ["model:gpt-5", "project:hotmem", "org:knowguard"],
                        "timestamp": "2026-04-20T10:02:00Z",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    policy = PolicyConfig(
        store_id="store-handoff-111",
        require_auth=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-preparer",
                caller_id="caller-preparer",
                store_id="store-handoff-111",
                namespaces=("prep/workspace",),
                operations=("read", "export"),
            ),
            CallerCredential.from_token(
                token="tok-importer",
                caller_id="caller-importer",
                store_id="store-handoff-111",
                namespaces=("claude/workspace",),
                operations=("read", "write", "hydrate"),
            ),
        ],
    )
    app = create_app(
        db_path=base_dir / "handoff_111.sqlite",
        base_dir=base_dir,
        policy=policy,
    )
    prep_hdr = {
        "Authorization": "Bearer tok-preparer",
        "X-HotMem-Store-Id": "store-handoff-111",
        "X-HotMem-Harness-Id": "codex-exporter",
        "X-HotMem-Session-Id": "sess-preparer-99",
        "X-HotMem-Source": "cli://exporter",
        "X-HotMem-Namespace": "prep/workspace",
    }
    imp_hdr = {
        "Authorization": "Bearer tok-importer",
        "X-HotMem-Store-Id": "store-handoff-111",
        "X-HotMem-Harness-Id": "claude-code",
        "X-HotMem-Session-Id": "sess-importer-42",
        "X-HotMem-Source": "mcp://claude-code",
        "X-HotMem-Namespace": "claude/workspace",
    }

    with TestClient(app) as client:
        prep = client.post(
            "/v1/handoff/prepare",
            headers=prep_hdr,
            json={
                "source": "codex_src",
                "output": "pkg_111",
                "mode": "resume",
                "consent": "owner approved handoff for #111",
            },
        )
        assert prep.status_code == 200, prep.text

        insp = client.get(
            "/v1/handoff/inspect",
            headers=prep_hdr,
            params={"package": "pkg_111"},
        ).json()
        assert insp["valid"] is True
        # Source session identity is distinct from preparing caller identity
        assert insp["source"]["session_id"] == "sess-codex-origin-77"
        assert insp["source"]["harness_id"] == "codex"
        assert insp["prepared_by"]["caller_id"] == "caller-preparer"
        assert insp["prepared_by"]["harness_id"] == "codex-exporter"
        assert insp["prepared_by"]["session_id"] == "sess-preparer-99"

        ver = client.post(
            "/v1/handoff/verify",
            headers=imp_hdr,
            json={"package": "pkg_111"},
        ).json()
        assert ver["valid"] is True

        hyd = client.post(
            "/v1/handoff/hydrate",
            headers=imp_hdr,
            json={"package": "pkg_111"},
        )
        assert hyd.status_code == 200, hyd.text
        assert hyd.json()["loaded"] == 2

        # Recall hydrated memory as importer: destination namespace is claude/workspace,
        # while record provenance retains source session sess-codex-origin-77 & harness codex,
        # and canonical tags survive intact.
        listed = client.get(
            "/v1/memories",
            headers=imp_hdr,
            params={"identifier": "arch/handoff-111"},
        ).json()["memories"]
        assert len(listed) == 1
        assert listed[0]["tags"] == ["model:gpt-5", "project:hotmem", "org:knowguard"]
        assert listed[0]["provenance"]["handoff"]["session_id"] == "sess-codex-origin-77"
        assert listed[0]["provenance"]["handoff"]["harness_id"] == "codex"

        evs = client.get(
            "/v1/events",
            headers=imp_hdr,
            params={"event_type": "handoff.applied"},
        ).json()["events"]
        assert len(evs) == 1
        assert evs[0]["payload"]["hydrated_by"]["caller_id"] == "caller-importer"
        assert evs[0]["payload"]["hydrated_by"]["harness_id"] == "claude-code"
        assert evs[0]["payload"]["hydrated_by"]["session_id"] == "sess-importer-42"

        # Existing 0.2.5 golden handoff fixture remains readable, verifiable, and hydratable
        golden_src = Path(__file__).parent / "golden" / "fixtures" / "handoff"
        golden_copy = base_dir / "legacy_025_pkg"
        shutil.copytree(golden_src, golden_copy)
        ver_025 = client.post(
            "/v1/handoff/verify",
            headers=imp_hdr,
            json={"package": "legacy_025_pkg"},
        )
        assert ver_025.status_code == 200
        assert ver_025.json()["valid"] is True
        hyd_025 = client.post(
            "/v1/handoff/hydrate",
            headers=imp_hdr,
            json={"package": "legacy_025_pkg"},
        )
        assert hyd_025.status_code == 200
        assert hyd_025.json()["loaded"] >= 1
