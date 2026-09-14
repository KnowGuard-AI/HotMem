"""OKF v0.2 importer tests — walker (#68).

Conformance against the authoritative OKF v0.2 specification (§3 reserved
filenames, §4 frontmatter, §11 tolerance, §12 okf_version): bounded parsing,
safe YAML, root confinement, no remote fetching, deterministic order.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hotmem.importers.okf import (
    OkfWarning,
    PageData,
    derive_trust_tier,
    extract_links,
    is_external_target,
    iter_okf_pages,
    iter_okf_records,
    normalize_verified,
    page_to_record,
    parse_frontmatter,
    read_bundle_metadata,
)
from hotmem.interchange.canonical import canonical_dumps, compute_content_hash


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


PAGE = """---
type: Metric
title: Orders metric
description: Count of completed orders.
tags: [sales]
---
# Definition

Orders per day, joined on [customers](/tables/customers.md).
"""


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    _write(root / "index.md", "# Index\n\n* [Orders](metrics/orders.md)\n")
    _write(root / "metrics/orders.md", PAGE)
    _write(root / "metrics/index.md", "# metrics section\n")
    _write(root / "log.md", "# Directory Update Log\n\n## 2026-05-22\n* **Update**: x\n")
    return root


def test_walk_yields_only_concept_pages(bundle: Path):
    warnings: list[OkfWarning] = []
    pages = list(iter_okf_pages(bundle, warnings))
    # index.md (root + section) and log.md are reserved, never concepts (§3.1).
    assert [p.concept_id for p in pages] == ["metrics/orders"]
    page = pages[0]
    assert page.frontmatter["type"] == "Metric"
    assert page.frontmatter["tags"] == ["sales"]
    assert page.body.startswith("# Definition")
    assert len(page.sha256) == 64
    assert page.rel_path == "metrics/orders.md"
    assert warnings == []


def test_walk_order_is_sorted(tmp_path: Path):
    root = tmp_path / "b"
    for rel in ["z.md", "a/y.md", "a/x.md", "m.md", "b/n.md"]:
        _write(root / rel, "---\ntype: T\n---\nbody\n")
    warnings: list[OkfWarning] = []
    ids = [p.concept_id for p in iter_okf_pages(root, warnings)]
    assert ids == ["a/x", "a/y", "b/n", "m", "z"]


def test_missing_frontmatter_handled_safely(tmp_path: Path):
    root = tmp_path / "b"
    _write(root / "bad.md", "just markdown, no frontmatter\n")
    _write(root / "good.md", "---\ntype: T\n---\nbody\n")
    warnings: list[OkfWarning] = []
    pages = list(iter_okf_pages(root, warnings))
    assert [p.concept_id for p in pages] == ["good"]
    assert any("missing frontmatter" in w.message for w in warnings)


def test_missing_type_handled_safely(tmp_path: Path):
    root = tmp_path / "b"
    _write(root / "notype.md", "---\ntitle: no type\n---\nbody\n")
    warnings: list[OkfWarning] = []
    pages = list(iter_okf_pages(root, warnings))
    assert pages == []
    assert any("required 'type'" in w.message for w in warnings)


def test_malformed_yaml_isolated_per_file(tmp_path: Path):
    root = tmp_path / "b"
    _write(root / "broken.md", "---\ntype: [unclosed\n---\nbody\n")
    _write(root / "ok.md", "---\ntype: T\n---\nbody\n")
    warnings: list[OkfWarning] = []
    pages = list(iter_okf_pages(root, warnings))
    assert [p.concept_id for p in pages] == ["ok"]
    assert any("frontmatter parse error" in w.message for w in warnings)


def test_non_mapping_frontmatter_rejected(tmp_path: Path):
    root = tmp_path / "b"
    _write(root / "list.md", "---\n- a\n- b\n---\nbody\n")
    warnings: list[OkfWarning] = []
    assert list(iter_okf_pages(root, warnings)) == []
    assert any("YAML mapping" in w.message for w in warnings)


def test_oversized_page_skipped(tmp_path: Path):
    root = tmp_path / "b"
    _write(root / "big.md", "---\ntype: T\n---\n")
    (root / "big.md").write_text("---\ntype: T\n---\n" + "x" * (17 * 1024 * 1024))
    warnings: list[OkfWarning] = []
    assert list(iter_okf_pages(root, warnings)) == []
    assert any("too large" in w.message for w in warnings)


def test_symlink_escape_rejected(tmp_path: Path):
    root = tmp_path / "b"
    secret = tmp_path / "secret.md"
    secret.write_text("---\ntype: T\n---\nsecret body\n")
    root.mkdir()
    (root / "evil.md").symlink_to(secret)
    warnings: list[OkfWarning] = []
    pages = list(iter_okf_pages(root, warnings))
    assert pages == []
    assert any("outside the bundle root" in w.message for w in warnings)


def test_traversal_inside_bundle_only(tmp_path: Path):
    root = tmp_path / "b"
    root.mkdir()
    # A directory literally named '..' cannot exist; the confinement check
    # must reject symlinks pointing above the root even when well-named.
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "v.md"
    victim.write_text("---\ntype: T\n---\nleaked\n")
    (root / "v.md").symlink_to(victim)
    warnings: list[OkfWarning] = []
    assert list(iter_okf_pages(root, warnings)) == []


def test_bundle_root_must_be_directory(tmp_path: Path):
    warnings: list[OkfWarning] = []
    with pytest.raises(FileNotFoundError):
        list(iter_okf_pages(tmp_path / "missing", warnings))


def test_root_index_okf_version_captured(tmp_path: Path):
    root = tmp_path / "b"
    _write(root / "index.md", '---\nokf_version: "0.2"\n---\n# Index\n')
    _write(root / "p.md", "---\ntype: T\n---\nbody\n")
    assert read_bundle_metadata(root) == {"okf_version": "0.2"}
    assert read_bundle_metadata(root / "nope") == {}


def test_parse_frontmatter_variants(tmp_path: Path):
    fm, body = parse_frontmatter("---\ntype: T\n---\n\nBody here\n")
    assert fm == {"type": "T"}
    assert body == "Body here"
    assert parse_frontmatter("no delimiters") is None
    assert parse_frontmatter("---\ntype: T\n") is None  # unclosed block
    fm2, _ = parse_frontmatter("---\n---\nbody\n")  # empty frontmatter is a mapping
    assert fm2 == {}


def test_unknown_frontmatter_keys_preserved(tmp_path: Path):
    """§11: consumers MUST NOT reject documents with unrecognized fields."""
    root = tmp_path / "b"
    _write(root / "p.md", "---\ntype: T\nproducer_field: {deep: true}\n---\nbody\n")
    warnings: list[OkfWarning] = []
    pages = list(iter_okf_pages(root, warnings))
    assert pages[0].frontmatter["producer_field"] == {"deep": True}


def test_external_link_targets_detected():
    assert is_external_target("https://example.com/x")
    assert is_external_target("http://example.com")
    assert is_external_target("mailto:a@b.c")
    assert is_external_target("#anchor")
    assert not is_external_target("/tables/customers.md")
    assert not is_external_target("./other.md")
    assert not is_external_target("../computations/revenue.md")


# ── Concept pages → interchange records (#68) ───────────────────────────────

FULL_PAGE_FM = """---
type: BigQuery Table
title: Customer Orders
description: One row per completed customer order across all channels.
resource: https://example.com/orders
tags: [sales, orders]
status: deprecated
stale_after: 2026-12-31T00:00:00Z
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-05-28T14:30:00Z }
verified:
  - { by: human:ahormati, at: 2026-06-25T09:00:00Z }
  - { by: process:finance-nightly, at: 2026-06-26T02:00:00Z }
sources:
  - id: ga4-schema
    resource: https://developers.google.com/analytics/bigquery/export-schema
    title: GA4 schema
    author: team:ga4-docs
    usage_count: 5000
    last_modified: 2026-05-30T00:00:00Z
usage_window: { from: 2026-06-01T00:00:00Z, to: 2026-06-30T00:00:00Z }
---

# Schema

| Column | Type |
|---|---|
| `order_id` | STRING |

Joined with [customers](/tables/customers.md) and
[nearby](./neighbors.md), plus an image ![x](img.png) and an
[external](https://example.com) link.
"""


def _full_page(tmp_path: Path):
    root = tmp_path / "wiki"
    (root / "tables").mkdir(parents=True)
    raw = FULL_PAGE_FM.encode()
    (root / "tables" / "orders.md").write_bytes(raw)
    fm, body = parse_frontmatter(FULL_PAGE_FM)
    return root, PageData(
        rel_path="tables/orders.md",
        concept_id="tables/orders",
        frontmatter=fm,
        body=body,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )


def test_page_to_record_maps_every_family(tmp_path: Path):
    _root, page = _full_page(tmp_path)
    rec = page_to_record(page, namespace="wiki")

    assert rec["identifier"] == "tables/orders"
    assert rec["fact_text"].startswith("# Schema")
    assert rec["fact_summary"] == "One row per completed customer order across all channels."
    assert rec["content_hash"] == compute_content_hash("tables/orders", rec["fact_text"])
    assert (
        rec["id"] == hashlib.sha256(f"okf:tables/orders:{rec['content_hash']}".encode()).hexdigest()
    )
    assert rec["memory_type"] == "fact"
    assert rec["source"] == "okf"
    assert rec["namespace"] == "wiki"
    assert rec["tier"] == "hot"
    assert rec["tags"] == ["sales", "orders"]
    assert rec["source_uri"] == "tables/orders.md"
    assert rec["source_format"] == "md"
    assert rec["source_checksum"] == page.sha256
    assert rec["importance"] == 0.5
    assert "embedding" not in rec  # hydration embeds; importer output is reviewable only

    okf = rec["metadata"]["okf"]
    assert okf["type"] == "BigQuery Table"
    assert okf["status"] == "deprecated"
    assert okf["stale_after"] == "2026-12-31T00:00:00Z"
    assert okf["trust_tier"] == "human-reviewed"
    assert okf["resource"] == "https://example.com/orders"
    # Links: bundle-absolute + relative normalized to concept ids; images and
    # external URLs excluded (§6.1, broken links tolerated).
    assert okf["links"] == ["tables/customers", "tables/neighbors"]

    prov = rec["provenance"]
    assert prov["generated"] == {
        "by": "reference_agent/gemini-2.5-pro",
        "at": "2026-05-28T14:30:00Z",
    }
    assert prov["verified"][0]["by"] == "human:ahormati"
    assert prov["sources"][0]["id"] == "ga4-schema"
    assert prov["sources"][0]["usage_count"] == 5000
    assert prov["usage_window"]["from"] == "2026-06-01T00:00:00Z"


def test_status_defaults_to_stable_and_tier_unverified(tmp_path: Path):
    root = tmp_path / "b"
    (root).mkdir()
    (root / "p.md").write_text("---\ntype: T\n---\nbody\n")
    recs = list(iter_okf_records(root, namespace="b"))
    assert len(recs) == 1
    assert recs[0]["metadata"]["okf"]["status"] == "stable"
    assert recs[0]["metadata"]["okf"]["trust_tier"] == "unverified"
    assert "provenance" not in recs[0]


def test_v01_timestamp_fallback(tmp_path: Path):
    root = tmp_path / "b"
    (root).mkdir()
    (root / "old.md").write_text(
        "---\ntype: Metric\ntimestamp: '2026-05-28T22:53:05+00:00'\n---\nbody\n"
    )
    recs = list(iter_okf_records(root))
    prov = recs[0]["provenance"]
    assert prov["generated"] == {
        "by": "okf:v0.1-timestamp-fallback",
        "at": "2026-05-28T22:53:05+00:00",
    }


def test_bare_verified_mapping_counts_as_list():
    assert normalize_verified({"verified": {"by": "human:a", "at": "x"}}) == [
        {"by": "human:a", "at": "x"}
    ]
    assert normalize_verified({"verified": [{"by": "process:p", "at": "y"}]}) == [
        {"by": "process:p", "at": "y"}
    ]
    assert normalize_verified({}) is None


def test_trust_tier_derivation():
    assert derive_trust_tier(None) == "unverified"
    assert derive_trust_tier([]) == "unverified"
    assert derive_trust_tier([{"by": "process:x", "at": "y"}]) == "machine-confirmed"
    assert derive_trust_tier([{"by": "human:a", "at": "x"}]) == "human-reviewed"
    assert (
        derive_trust_tier([{"by": "process:x", "at": "y"}, {"by": "human:a", "at": "x"}])
        == "human-reviewed"
    )


def test_references_dir_pages_are_marked(tmp_path: Path):
    root = tmp_path / "b"
    (root / "references").mkdir(parents=True)
    (root / "references" / "run.md").write_text("---\ntype: Skill\n---\nrun it\n")
    (root / "top.md").write_text("---\ntype: T\n---\nbody\n")
    recs = {r["identifier"]: r for r in iter_okf_records(root, namespace="b")}
    assert recs["references/run"]["source"] == "okf:references"
    assert recs["references/run"]["metadata"]["okf"]["in_references_dir"] is True
    assert recs["top"]["source"] == "okf"


def test_importer_output_is_byte_stable(tmp_path: Path):
    """#68 acceptance: same bundle -> byte-identical JSONL, twice."""
    root, _page = _full_page(tmp_path)
    (root / "index.md").write_text('---\nokf_version: "0.2"\n---\n# idx\n')

    runs = []
    for _ in range(2):
        lines = [canonical_dumps(r) for r in iter_okf_records(root)]
        runs.append("\n".join(lines) + "\n")
    assert runs[0] == runs[1]

    # Namespace affects records; okf_version from root index lands in metadata.
    rec = next(iter(iter_okf_records(root)))
    assert rec["metadata"]["okf"]["okf_version"] == "0.2"


def test_link_extraction_normalizes_and_tolerates():
    body = "[a](/x/y.md) [b](../up.md) [c](./sib.md) [d](z.md#frag) [e](#anchor) [f](https://x.y)"
    assert extract_links("dir/page.md", body) == ["dir/sib", "dir/z", "up", "x/y"]
    # Traversal above the bundle root is dropped, not an error.
    assert extract_links("a/b.md", "[x](../../../../etc/passwd.md)") == []
