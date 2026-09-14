"""OKF v0.2 importer tests — walker (#68).

Conformance against the authoritative OKF v0.2 specification (§3 reserved
filenames, §4 frontmatter, §11 tolerance, §12 okf_version): bounded parsing,
safe YAML, root confinement, no remote fetching, deterministic order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.importers.okf import (
    OkfWarning,
    is_external_target,
    iter_okf_pages,
    parse_frontmatter,
    read_bundle_metadata,
)


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
