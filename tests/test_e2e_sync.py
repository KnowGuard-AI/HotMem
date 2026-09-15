"""End-to-end company-brain sync gate (#73).

The product workflow, proven in one test:

    wiki -> JSONL -> source instance -> base package -> receiver ->
    source mutations -> delta -> receiver converges -> retrieval parity ->
    repeat apply loads zero -> diverged receiver recovers via full clone

Failure semantics (replay without duplicates, conflicts leaving the target
unchanged, interruption rollback) are proven in test_delta_apply.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.importers.okf import iter_okf_records
from hotmem.interchange.canonical import canonical_dumps, compute_content_hash
from hotmem.interchange.delta import DeltaConflictError, apply_delta, produce_delta
from hotmem.interchange.fingerprint import state_fingerprint
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package
from hotmem.search import search_memories
from hotmem.swap import hydrate

WIKI = {
    "index.md": '---\nokf_version: "0.2"\n---\n# Ops wiki\n\n* [deploys](ops/deploys.md)\n',
    "ops/deploys.md": (
        "---\n"
        "type: Playbook\n"
        "title: Deploy playbook\n"
        "description: Steps to deploy the payments service safely.\n"
        "tags: [ops]\n"
        "verified: { by: human:sre, at: 2026-09-01T09:00:00Z }\n"
        "---\n\n"
        "# Steps\n\n1. Follow the [runbook](runbook.md).\n"
    ),
    "ops/runbook.md": (
        "---\n"
        "type: Runbook\n"
        "title: Standard runbook\n"
        "description: Rollback gates for the payments service.\n"
        "---\n\n"
        "# Runbook\n\nRoll back via the payments console if the error budget burns.\n"
    ),
}


def _write_wiki(root: Path, pages: dict[str, str]) -> Path:
    for rel, text in pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _import_jsonl(wiki: Path, out: Path) -> Path:
    with open(out, "w", encoding="utf-8") as f:
        for rec in iter_okf_records(wiki, namespace="ops-wiki"):
            f.write(canonical_dumps(rec) + "\n")
    return out


def _probes(db: MemoryDB) -> list[tuple[str, list[tuple[str, float]]]]:
    probes = ["deploy the payments service", "rollback gates runbook", "vendor acme SLA"]
    return [
        (q, [(r["identifier"], round(r["score"], 6)) for r in search_memories(db, q, top_k=5)])
        for q in probes
    ]


def test_full_sync_gate_with_recovery(wiki_tmp: Path, tmp_path: Path):
    wiki = _write_wiki(wiki_tmp, WIKI)

    # wiki -> reviewable JSONL -> source instance (existing JSONL path).
    jsonl = _import_jsonl(wiki, tmp_path / "brain.jsonl")
    source = MemoryDB(tmp_path / "source.sqlite")
    assert hydrate(source, jsonl).loaded == 2

    # Add a non-wiki memory (imported via the JSONL path — the no-events case).
    vendor_fact = "vendor acme guarantees a 99.9 percent SLA"
    source.insert(
        id="vendor-1",
        identifier="vendors/acme",
        fact_text=vendor_fact,
        embedding=pack_embedding(embed_text(vendor_fact)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("vendors/acme", vendor_fact),
        namespace="ops-wiki",
    )
    assert source.count() == 3

    # Base clone -> receiver.
    base_pkg = tmp_path / "base"
    write_package(source, base_pkg)
    receiver = MemoryDB(tmp_path / "receiver.sqlite")
    hydrate_package(receiver, base_pkg)

    # Source moves on: a new wiki page + a promotion transition.
    wiki2 = dict(WIKI)
    wiki2["ops/oncall.md"] = (
        "---\n"
        "type: Playbook\n"
        "title: On-call rotation\n"
        "description: Weekly on-call handoff happens Monday at 10:00.\n"
        "tags: [ops]\n"
        "---\n\n"
        "# On-call\n\nHandoff includes a written summary of open incidents.\n"
    )
    _write_wiki(wiki_tmp, wiki2)
    jsonl2 = _import_jsonl(wiki_tmp, tmp_path / "brain2.jsonl")
    hydrate(source, jsonl2)
    deploys_id = next(r["id"] for r in source.all_rows() if r["identifier"] == "ops/deploys")
    source.update_promotion_state(deploys_id, "WARM")

    # delta -> apply -> receiver converges to the source.
    delta_dir = tmp_path / "delta1"
    produce_delta(source, base_pkg, delta_dir)
    result = apply_delta(receiver, delta_dir)
    assert result.applied == 2  # new page + promotion rewrite
    assert result.conflicts == []

    assert state_fingerprint(receiver.all_rows()) == state_fingerprint(source.all_rows())
    assert _probes(receiver) == _probes(source)  # retrieval parity

    # Repeat apply: zero changes (no duplicates, no drift).
    again = apply_delta(receiver, delta_dir)
    assert again.applied == 0 and again.skipped == 2
    assert _probes(receiver) == _probes(source)

    # Receiver diverges badly (a local edit the source never made) —
    # conflicts are visible and nothing is applied.
    divergent_fact = "local experiment: rolled out canary deploys daily"
    receiver.insert(
        id="local-1",
        identifier="experiments/canary",
        fact_text=divergent_fact,
        embedding=pack_embedding(embed_text(divergent_fact)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("experiments/canary", divergent_fact),
        namespace="ops-wiki",
    )
    runbook_id = next(r["id"] for r in source.all_rows() if r["identifier"] == "ops/runbook")
    source.update_promotion_state(runbook_id, "ARCHIVED")
    source.update_promotion_state(deploys_id, "ARCHIVED")
    delta2_dir = tmp_path / "delta2"
    produce_delta(source, base_pkg, delta2_dir)
    with pytest.raises(DeltaConflictError) as excinfo:
        apply_delta(receiver, delta2_dir)
    assert any(c.reason in ("id_reuse", "state_divergence") for c in excinfo.value.conflicts)
    assert receiver.count() == 5  # local edit preserved; delta not applied

    # Recovery path of record: whole-brain clone and restore.
    recovery_pkg = tmp_path / "recovery"
    write_package(source, recovery_pkg)
    recovered = MemoryDB(tmp_path / "recovered.sqlite")
    hydrate_package(recovered, recovery_pkg)
    assert state_fingerprint(recovered.all_rows()) == state_fingerprint(source.all_rows())
    assert _probes(recovered) == _probes(source)

    source.close()
    receiver.close()
    recovered.close()


@pytest.fixture
def wiki_tmp(tmp_path: Path) -> Path:
    return tmp_path / "wiki"
