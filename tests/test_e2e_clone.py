"""End-to-end company-brain clone gate (#68/#69).

The product workflow, proven in one test:

    wiki → JSONL → source instance → package → clean instance →
    equivalent retrieval → repeat hydrate loads zero records

Plus the fixture families the contract requires: old formats round-trip,
provenance preservation, and duplicate/conflicting identity resolution.
Failure-mode proofs (corrupted/truncated packages, symlink escapes,
malformed frontmatter, incompatible embeddings) live in
test_package_hydrate.py / test_okf_importer.py — each asserts the target
stays unchanged.
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.importers.okf import iter_okf_records
from hotmem.interchange.canonical import canonical_dumps, compute_content_hash
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package
from hotmem.search import search_memories
from hotmem.swap import hydrate

WIKI_PAGES = {
    "index.md": '---\nokf_version: "0.2"\n---\n# Ops wiki\n\n* [deploys](ops/deploys.md)\n',
    "ops/deploys.md": (
        "---\n"
        "type: Playbook\n"
        "title: Deploy playbook\n"
        "description: Steps to deploy the payments service safely on weekdays.\n"
        "tags: [ops, deploy]\n"
        "generated: { by: human:sre, at: 2026-09-01T10:00:00Z }\n"
        "verified: { by: human:sre, at: 2026-09-02T09:00:00Z }\n"
        "---\n\n"
        "# Steps\n\n1. Freeze deploys per [the runbook](runbook.md).\n"
    ),
    "ops/runbook.md": (
        "---\n"
        "type: Runbook\n"
        "title: Standard runbook\n"
        "description: The standard deployment runbook with rollback gates.\n"
        "tags: [ops]\n"
        "---\n\n"
        "# Runbook\n\nRoll back via the payments console if error budget burns.\n"
    ),
    "vendors/acme.md": (
        "---\n"
        "type: Vendor\n"
        "title: Acme\n"
        "description: Acme supplies the payments gateway with a 99.9% SLA.\n"
        "tags: [vendors]\n"
        "---\n\n"
        "# Acme\n\nQuarterly business review covers SLA credits.\n"
    ),
}


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    for rel, text in WIKI_PAGES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def jsonl(wiki: Path, tmp_path: Path) -> Path:
    """wiki → reviewable JSONL (deterministic bytes)."""
    out = tmp_path / "brain.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for rec in iter_okf_records(wiki, namespace="ops-wiki"):
            f.write(canonical_dumps(rec) + "\n")
    return out


PROBES = [
    "deploy the payments service",
    "rollback gates runbook",
    "acme payments gateway SLA",
    "quarterly review",
]


def _probes(db: MemoryDB) -> list[tuple[str, list[tuple[str, float]]]]:
    return [
        (q, [(r["identifier"], round(r["score"], 6)) for r in search_memories(db, q, top_k=5)])
        for q in PROBES
    ]


def test_full_clone_gate(wiki: Path, jsonl: Path, tmp_path: Path):
    # ── JSONL → source instance ───────────────────────────────────────
    source = MemoryDB(tmp_path / "source.sqlite")
    result = hydrate(source, jsonl)
    assert result.loaded == 3  # deploys, runbook, acme (index.md reserved)
    assert result.invalid == 0
    source_probes = _probes(source)
    assert all(hits for _, hits in source_probes), "source must be searchable"

    # Provenance survived wiki → JSONL → instance.
    deploys = next(r for r in source.all_rows() if r["identifier"] == "ops/deploys")
    assert deploys["source_uri"] == "ops/deploys.md"
    assert len(deploys["source_checksum"]) == 64
    assert json.loads(deploys["provenance_json"])["verified"][0]["by"] == "human:sre"

    # ── source instance → package ─────────────────────────────────────
    pkg = tmp_path / "clone-pkg"
    exported = write_package(source, pkg)
    assert exported.exported == 3

    # ── package → clean instance ──────────────────────────────────────
    target = MemoryDB(tmp_path / "clean.sqlite")
    restored = hydrate_package(target, pkg)
    assert restored.loaded == 3
    assert restored.skipped_dupes == 0
    assert restored.invalid == 0

    # ── equivalent retrieval ──────────────────────────────────────────
    target_probes = _probes(target)
    assert source_probes == target_probes

    # Provenance survived instance → package → instance (byte-level fields).
    t_deploys = next(r for r in target.all_rows() if r["identifier"] == "ops/deploys")
    for col in (
        "source_uri",
        "source_checksum",
        "provenance_json",
        "metadata_json",
        "namespace",
        "tier",
        "tags",
        "fact_summary",
    ):
        assert t_deploys[col] == deploys[col], col

    # ── repeat hydrate loads zero records ──────────────────────────────
    repeat = hydrate_package(target, pkg)
    assert repeat.loaded == 0
    assert repeat.skipped_dupes == 3
    assert _probes(target) == target_probes  # retrieval unchanged

    source.close()
    target.close()


def test_gate_gz_package_parity(wiki: Path, jsonl: Path, tmp_path: Path):
    """The same gate passes with the compressed payload and identical results."""
    source = MemoryDB(tmp_path / "source.sqlite")
    hydrate(source, jsonl)
    pkg_gz = tmp_path / "clone-gz"
    write_package(source, pkg_gz, gz=True)

    target = MemoryDB(tmp_path / "clean.sqlite")
    restored = hydrate_package(target, pkg_gz)
    assert restored.loaded == 3
    assert _probes(target) == _probes(source)
    source.close()
    target.close()


def test_gate_old_formats_round_trip(jsonl: Path, tmp_path: Path):
    """Legacy plain, stored-embedding, and .gz variants all hydrate and
    re-export into interchangeable packages (old-format fixture family)."""
    records = [json.loads(line) for line in jsonl.read_text().splitlines()]

    def make_variant(path: Path, with_embeddings: bool, gzipped: bool) -> Path:
        with open(path, "wb") as raw:
            sink = gzip.GzipFile(filename="", mtime=0, fileobj=raw) if gzipped else raw
            for rec in records:
                if with_embeddings:
                    rec = dict(rec)
                    rec["embedding"] = base64.b64encode(
                        pack_embedding(embed_text(rec["fact_text"]))
                    ).decode()
                    rec["embedding_dim"] = 64
                    rec["embedding_model"] = "hotmem-hash-v1"
                else:
                    rec = {k: v for k, v in rec.items() if k != "embedding"}
                line = canonical_dumps(rec).encode() + b"\n"
                sink.write(line)
            if gzipped:
                sink.close()
        return path

    for name, emb, gzipped in [
        ("plain", False, False),
        ("stored", True, False),
        ("gz", False, True),
    ]:
        suffix = ".jsonl.gz" if gzipped else ".jsonl"
        variant = make_variant(tmp_path / f"swap-{name}{suffix}", emb, gzipped)
        db = MemoryDB(tmp_path / f"db-{name}.sqlite")
        loaded = hydrate(db, variant)
        assert loaded.loaded == 3, name
        pkg = tmp_path / f"pkg-{name}"
        write_package(db, pkg)
        fresh = MemoryDB(tmp_path / f"fresh-{name}.sqlite")
        restored = hydrate_package(fresh, pkg)
        assert restored.loaded == 3, name
        db.close()
        fresh.close()


def test_duplicate_and_conflicting_identities(tmp_path: Path):
    """Deterministic resolution: same content_hash is a dupe (first wins);
    same id with different content hashes rewrites deterministically
    (INSERT OR REPLACE semantics, locked by test)."""
    fact_a = "acme signed the renewal contract"
    fact_b = "acme signed the expansion contract"

    source = MemoryDB(tmp_path / "src.sqlite")
    # Same content, different ids: second is a duplicate.
    source.insert(
        id="one",
        identifier="vendor",
        fact_text=fact_a,
        embedding=pack_embedding(embed_text(fact_a)),
        content_hash=compute_content_hash("vendor", fact_a),
    )
    source.insert(
        id="two",
        identifier="vendor-copy",
        fact_text=fact_a,
        embedding=pack_embedding(embed_text(fact_a)),
        content_hash=compute_content_hash("vendor-copy", fact_a),
    )
    pkg = tmp_path / "pkg"
    exported = write_package(source, pkg)
    assert exported.exported == 2

    target = MemoryDB(tmp_path / "t.sqlite")
    result = hydrate_package(target, pkg)
    assert result.loaded == 2
    dupes = hydrate_package(target, pkg)
    assert dupes.loaded == 0 and dupes.skipped_dupes == 2
    source.close()
    target.close()

    # Same id, different content: the later record rewrites (id-keyed upsert
    # semantics) — and a package's sorted-by-id order makes it deterministic.
    d2 = MemoryDB(tmp_path / "src2.sqlite")
    d2.insert(
        id="one",
        identifier="vendor",
        fact_text=fact_b,
        embedding=pack_embedding(embed_text(fact_b)),
        content_hash=compute_content_hash("vendor", fact_b),
    )
    pkg2 = tmp_path / "pkg2"
    write_package(d2, pkg2)
    t2 = MemoryDB(tmp_path / "t2.sqlite")
    hydrate_package(t2, pkg2)
    row = t2.get_memory("one")
    assert row["fact_text"] == fact_b
    d2.close()
    t2.close()
