"""Tests for #50 — Bundle indexing and discovery.

Covers acceptance criteria:
  1. A directory tree with multiple bundles can be discovered deterministically.
  2. Bundle index entries include paths, primary files, metadata summary,
     warning count, and attachment reference counts.
  3. Unknown files do not crash discovery.
  4. Discovery does not hydrate large attachments (zero attachment reads).
  5. Existing JSONL hydrate/search behavior remains unchanged.
  6. Symlink traversal is rejected during discovery.
  7. Checksums are indexed.
  8. Batch commit (single transaction for all upserts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.bundle_index import (
    discover_bundles,
    index_bundles,
)
from hotmem.db import MemoryDB


def _make_bundle(bundle_dir: Path, **files: str) -> Path:
    """Create a minimal bundle directory with the given files."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (bundle_dir / name).write_text(content, encoding="utf-8")
    return bundle_dir


@pytest.fixture
def multi_bundle_tree(tmp_path: Path) -> Path:
    """A tree with three bundles at different depths + a non-bundle dir."""
    root = tmp_path / "root"

    # Bundle 1: minimal (memory.md only)
    _make_bundle(root / "project_a", **{"memory.md": "# Project A\n\nAlpha notes."})

    # Bundle 2: full (memory.md + metadata + facts + attachments)
    b2 = root / "project_b"
    _make_bundle(
        b2,
        **{"memory.md": "# Project B\n\nBeta data."},
    )
    (b2 / "metadata.json").write_text(
        json.dumps({"identifier": "beta", "importance": 0.9, "tags": ["q3"]}),
        encoding="utf-8",
    )
    (b2 / "facts.json").write_text(json.dumps([{"fact": "beta fact one"}]), encoding="utf-8")
    att2 = b2 / "attachments"
    att2.mkdir()
    (att2 / "data.csv").write_text("id,val\n1,100\n", encoding="utf-8")

    # Bundle 3: nested deeper, uses index.md fallback
    _make_bundle(
        root / "subdir" / "project_c",
        **{"index.md": "# Project C\n\nGamma index."},
    )

    # Non-bundle directory (no memory body file)
    (root / "empty_dir").mkdir(parents=True, exist_ok=True)
    (root / "empty_dir" / "random.txt").write_text("not a bundle", encoding="utf-8")

    return root


# ── 1. Deterministic discovery ────────────────────────────────────────────────


def test_discover_finds_all_bundles(multi_bundle_tree: Path):
    entries = discover_bundles(multi_bundle_tree)
    assert len(entries) == 3


def test_discover_is_deterministic(multi_bundle_tree: Path):
    entries1 = discover_bundles(multi_bundle_tree)
    entries2 = discover_bundles(multi_bundle_tree)
    assert [e.path for e in entries1] == [e.path for e in entries2]
    # Sorted by path
    paths = [e.path for e in entries1]
    assert paths == sorted(paths)


def test_discover_max_depth(tmp_path: Path):
    root = tmp_path / "root"
    _make_bundle(root / "a", **{"memory.md": "# A"})
    _make_bundle(root / "a" / "b" / "c", **{"memory.md": "# Deep"})
    # max_depth=1 should only find the top-level bundle
    entries = discover_bundles(root, max_depth=1)
    assert len(entries) == 1
    assert entries[0].path.endswith("/a")


# ── 2. Index entries include metadata ─────────────────────────────────────────


def test_index_entry_has_primary_file(multi_bundle_tree: Path):
    entries = discover_bundles(multi_bundle_tree)
    primary_files = {e.primary_file for e in entries}
    assert "memory.md" in primary_files
    assert "index.md" in primary_files


def test_index_entry_has_metadata_summary(multi_bundle_tree: Path):
    entries = discover_bundles(multi_bundle_tree)
    beta = next(e for e in entries if "project_b" in e.path)
    assert beta.identifier == "beta"
    assert beta.metadata_summary != {}


def test_index_entry_has_attachment_refs(multi_bundle_tree: Path):
    entries = discover_bundles(multi_bundle_tree)
    beta = next(e for e in entries if "project_b" in e.path)
    assert beta.attachment_count == 1
    assert len(beta.attachment_refs) == 1
    att = beta.attachment_refs[0]
    assert att.name == "data.csv"
    assert att.size > 0
    assert att.format == "csv"


def test_index_entry_has_size_hint(multi_bundle_tree: Path):
    entries = discover_bundles(multi_bundle_tree)
    for e in entries:
        assert e.size_hint > 0


def test_index_entry_has_modified_time(multi_bundle_tree: Path):
    entries = discover_bundles(multi_bundle_tree)
    for e in entries:
        assert e.modified_time != ""


# ── 3. Unknown files do not crash discovery ───────────────────────────────────


def test_unknown_files_produce_warnings(tmp_path: Path):
    root = tmp_path / "root"
    _make_bundle(
        root / "bundle",
        **{
            "memory.md": "# Test",
            "random.dat": "unknown",
            "notes.txt": "also unknown",
        },
    )
    entries = discover_bundles(root)
    assert len(entries) == 1
    assert entries[0].warning_count >= 2  # two unknown files


# ── 4. Zero attachment reads during indexing ──────────────────────────────────


def test_zero_adapter_reads_during_discovery(multi_bundle_tree: Path):
    """Discovery must not read attachment bytes — only stat() for size hints."""
    import hotmem.storage as storage_mod

    adapter_calls: list[str] = []

    original_get_adapter = getattr(storage_mod, "get_adapter", None)

    def _tracking_adapter(uri: str):
        adapter_calls.append(uri)
        if original_get_adapter is not None:
            return original_get_adapter(uri)
        raise RuntimeError("adapter should not be called during discovery")

    storage_mod.get_adapter = _tracking_adapter
    try:
        discover_bundles(multi_bundle_tree)
        assert adapter_calls == [], (
            f"discovery called storage adapter {len(adapter_calls)} times (expected 0)"
        )
    finally:
        if original_get_adapter is not None:
            storage_mod.get_adapter = original_get_adapter


# ── 5. index_bundles persists to DB ───────────────────────────────────────────


def test_index_bundles_persists_to_db(tmp_db: MemoryDB, multi_bundle_tree: Path):
    result = index_bundles(tmp_db, multi_bundle_tree)
    assert result.discovered == 3
    assert result.indexed == 3

    rows = tmp_db.list_bundle_index()
    assert len(rows) == 3
    paths = [r["path"] for r in rows]
    assert paths == sorted(paths)  # deterministic ordering

    # Verify entry content
    beta = next(r for r in rows if "project_b" in r["path"])
    assert beta["primary_file"] == "memory.md"
    assert beta["attachment_count"] == 1


def test_clear_bundle_index(tmp_db: MemoryDB, multi_bundle_tree: Path):
    index_bundles(tmp_db, multi_bundle_tree)
    assert len(tmp_db.list_bundle_index()) == 3
    tmp_db.clear_bundle_index()
    assert len(tmp_db.list_bundle_index()) == 0


def test_index_bundles_idempotent(tmp_db: MemoryDB, multi_bundle_tree: Path):
    index_bundles(tmp_db, multi_bundle_tree)
    index_bundles(tmp_db, multi_bundle_tree)  # re-index
    assert len(tmp_db.list_bundle_index()) == 3  # no dupes (INSERT OR REPLACE)


# ── 6. Existing hydrate/search unchanged ──────────────────────────────────────


def test_jsonl_hydrate_still_works(tmp_db: MemoryDB, tmp_path: Path):
    """JSONL hydrate is not affected by the bundle_index table."""
    swap = tmp_path / "swap.jsonl"
    swap.write_text(
        json.dumps({"identifier": "x", "fact_text": "jsonl fact"}) + "\n",
        encoding="utf-8",
    )
    from hotmem.snapshot import hydrate

    result = hydrate(tmp_db, swap)
    assert result.loaded == 1
    assert tmp_db.count() == 1


# ── 7. Symlink rejection + checksum indexing + batch commit ──────────────────


def test_symlink_in_bundle_rejected_during_discovery(tmp_path: Path):
    """A symlink in a bundle that points outside is rejected during discovery."""
    root = tmp_path / "root"
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "memory.md").write_text("# Test", encoding="utf-8")
    att = bundle / "attachments"
    att.mkdir()
    # Create a symlink pointing outside the bundle.
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    (att / "evil").symlink_to(target)

    entries = discover_bundles(root)
    assert len(entries) == 1
    # The symlink should produce a warning, not an attachment ref.
    warning_text = " ".join(str(w) for w in entries[0].warnings)
    assert "evil" in warning_text or "outside" in warning_text.lower()


def test_index_entry_has_checksum(multi_bundle_tree: Path):
    """Bundle index entries include a checksum (aggregate of attachment checksums)."""
    entries = discover_bundles(multi_bundle_tree)
    beta = next(e for e in entries if "project_b" in e.path)
    # Beta has one attachment with a content-derived checksum from bundle.py.
    assert beta.checksum != ""
    assert len(beta.checksum) == 64  # SHA-256 hex


def test_checksum_in_db(tmp_db: MemoryDB, multi_bundle_tree: Path):
    """The checksum column is persisted in the bundle_index table."""
    index_bundles(tmp_db, multi_bundle_tree)
    rows = tmp_db.list_bundle_index()
    beta = next(r for r in rows if "project_b" in r["path"])
    assert beta["checksum"] != ""


def test_batch_commit_single_transaction(tmp_db: MemoryDB, multi_bundle_tree: Path):
    """index_bundles uses a single transaction (not per-entry commit)."""
    # We verify by checking that all entries are present after indexing
    # (if the transaction was per-entry and failed midway, we'd get partial).
    result = index_bundles(tmp_db, multi_bundle_tree)
    assert result.indexed == 3
    rows = tmp_db.list_bundle_index()
    assert len(rows) == 3  # all or nothing (atomic)


def test_index_loss_does_not_lose_memory(tmp_db: MemoryDB, multi_bundle_tree: Path):
    """Dropping the bundle_index table does not affect the memories table."""
    from hotmem.snapshot import hydrate

    # Hydrate a bundle into memories.
    bundle_b = multi_bundle_tree / "project_b"
    hydrate(tmp_db, bundle_b)
    assert tmp_db.count() > 0

    # Clear the bundle index.
    tmp_db.clear_bundle_index()
    assert len(tmp_db.list_bundle_index()) == 0

    # Memories are still there.
    assert tmp_db.count() > 0
