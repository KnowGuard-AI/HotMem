"""OKF v0.2 conformance suite (#68) — vendored public + synthetic fixtures.

Runs the importer against:
  - a minimal slice of the PUBLIC acme_retail bundle
    (GoogleCloudPlatform/knowledge-catalog, Apache-2.0), and
  - hand-written synthetic edge fixtures (v0.1 legacy, malformed, links).

Proves #68 acceptance against real public OKF material: determinism,
provenance preservation, hierarchy/links, safe handling, and the §11
tolerance rules — offline, from the vendored tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.importers.okf import OkfWarning, iter_okf_records
from hotmem.interchange.canonical import canonical_dumps

FIXTURES = Path(__file__).parent / "fixtures" / "okf"
ACME = FIXTURES / "acme_retail"
SYNTHETIC = FIXTURES / "synthetic"


def test_public_acme_slice_imports_cleanly():
    warnings: list[OkfWarning] = []
    records = list(iter_okf_records(ACME, namespace="acme", warnings=warnings))
    # Reserved files (index.md x5, log.md) never become records.
    assert [r["identifier"] for r in records] == [
        "computations/revenue-ytd",
        "metrics/revenue",
        "policies/revenue-recognition",
        "tables/orders",
    ]
    # README.md (the vendoring note) is markdown without frontmatter: it
    # warns and is skipped safely, never a crash.
    assert [(w.path, "missing frontmatter block" in w.message) for w in warnings] == [
        ("README.md", True)
    ]


def test_public_acme_provenance_families_preserved():
    records = {r["identifier"]: r for r in iter_okf_records(ACME, namespace="acme")}

    orders = records["tables/orders"]
    assert orders["metadata"]["okf"]["type"] == "BigQuery Table"
    # Human-verified page ⇒ highest trust tier (§5.3).
    assert orders["metadata"]["okf"]["trust_tier"] == "human-reviewed"
    # Credibility signals preserved verbatim (§5.1).
    sources = orders["provenance"]["sources"]
    assert any(s.get("usage_count") is not None for s in sources)
    assert orders["provenance"]["generated"]["by"] == "reference_agent/gemini-2.5-pro"
    # Source hash recorded for provenance (#68).
    assert len(orders["source_checksum"]) == 64

    # Attested Computation frontmatter survives round-trip (§10, §11).
    revenue_ytd = records["computations/revenue-ytd"]
    fm = revenue_ytd["metadata"]["okf"]["frontmatter"]
    assert fm["runtime"] == "bigquery"
    assert fm["parameters"][0]["name"] == "year"
    assert fm["executor"]["resource"] == "skills/run-on-bq.md"
    assert fm["attester"]["resource"] == "attesters/sql_equality.py"

    # Metric narrates the computation via links (§10.4).
    revenue = records["metrics/revenue"]
    assert "computations/revenue-ytd" in revenue["metadata"]["okf"]["links"]


def test_public_acme_output_byte_stable():
    runs = []
    for _ in range(2):
        text = "".join(canonical_dumps(r) + "\n" for r in iter_okf_records(ACME, namespace="acme"))
        runs.append(text)
    assert runs[0] == runs[1]


def test_public_acme_hydrates_and_retrieves():
    """Import output must hydrate through the existing JSONL path and search."""
    import tempfile

    from hotmem.db import MemoryDB
    from hotmem.search import search_memories
    from hotmem.swap import hydrate

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        out = tmp / "acme.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for r in iter_okf_records(ACME, namespace="acme"):
                f.write(canonical_dumps(r) + "\n")
        db_path = tmp / "hotmem.sqlite"
        db = MemoryDB(db_path)
        result = hydrate(db, out)
        assert result.loaded == 4
        assert result.invalid == 0
        hits = search_memories(db, "recognized revenue", top_k=3)
        assert hits, "imported pages must be searchable"
        db.close()


def test_synthetic_v01_legacy_timestamp_fallback():
    records = list(iter_okf_records(SYNTHETIC / "v1_legacy"))
    assert len(records) == 1
    prov = records[0]["provenance"]
    assert prov["generated"] == {
        "by": "okf:v0.1-timestamp-fallback",
        "at": "2026-05-28T22:53:05+00:00",
    }
    assert records[0]["tags"] == ["finance", "income-statement"]


def test_synthetic_malformed_pages_warn_and_skip():
    warnings: list[OkfWarning] = []
    records = list(iter_okf_records(SYNTHETIC / "malformed", warnings=warnings))
    # broken_yaml.md (parse error), no_type.md (missing required type),
    # no_frontmatter.md (missing block) all warn + skip; nothing crashes.
    assert records == []
    messages = " ".join(w.message for w in warnings)
    assert "frontmatter parse error" in messages
    assert "required 'type'" in messages
    assert "missing frontmatter block" in messages


def test_synthetic_links_hierarchy_and_tolerance():
    """Deep hierarchy, absolute/relative normalization, broken links OK (§6/§11)."""
    records = {r["identifier"]: r for r in iter_okf_records(SYNTHETIC / "links", namespace="links")}
    assert set(records) == {
        "deep/a/b/page",
        "deep/a/b/sibling",
        "deep/a/customers",
        "top",
    }  # no index.md anywhere: tolerated (§11 MUST NOT reject)

    page = records["deep/a/b/page"]
    links = page["metadata"]["okf"]["links"]
    # Bundle-absolute, same-dir relative, and upward traversal all normalize
    # to concept ids; the external URL is excluded; the broken /deep/future
    # target is recorded but never validated (§6.1: broken links tolerated).
    assert links == ["deep/a/b/sibling", "deep/a/customers", "deep/future", "top"]


def test_synthetic_deterministic_identifiers():
    r1 = list(iter_okf_records(SYNTHETIC / "links", namespace="links"))
    r2 = list(iter_okf_records(SYNTHETIC / "links", namespace="links"))
    assert [x["id"] for x in r1] == [x["id"] for x in r2]
    assert all(len(x["id"]) == 64 for x in r1)


def test_no_network_access_during_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Importer must never open sockets — sources are recorded, not fetched."""

    import socket

    def fail_socket(*args, **kwargs):
        raise AssertionError("network access attempted during OKF import")

    monkeypatch.setattr(socket, "create_connection", fail_socket)
    monkeypatch.setattr(socket, "socket", fail_socket)
    monkeypatch.setattr("urllib.request.urlopen", fail_socket)

    records = list(iter_okf_records(ACME, namespace="acme"))
    assert len(records) == 4


def test_fixture_json_is_canonicalizable():
    """Every record from every fixture serializes canonically without error."""
    for root in (ACME, SYNTHETIC / "links", SYNTHETIC / "v1_legacy"):
        for rec in iter_okf_records(root, namespace="t"):
            assert json.loads(canonical_dumps(rec))["identifier"] == rec["identifier"]
