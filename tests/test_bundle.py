"""Tests for #52 — Bundle Store: loose local markdown bundle reader.

Covers the acceptance criteria:
  1. A minimal bundle hydrates into HotMem.
  2. A bundle with metadata and attachments preserves file references and provenance.
  3. Unknown files do not fail permissive reads.
  4. Existing JSONL hydrate remains unchanged.

Plus extras: facts.json, events.jsonl, manifest.json, dedup, dispatch routing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.bundle import detect_bundle, read_bundle
from hotmem.db import MemoryDB
from hotmem.snapshot import detect_format, hydrate

# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_bundle(bundle_dir: Path, **files: str) -> Path:
    """Create a minimal bundle directory with the given files."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (bundle_dir / name).write_text(content, encoding="utf-8")
    return bundle_dir


@pytest.fixture
def minimal_bundle(tmp_path: Path) -> Path:
    """A bundle with only memory.md."""
    return _make_bundle(
        tmp_path / "bundle",
        **{"memory.md": "# Vendor Analysis\n\nAcme Corp has duplicate invoice risk."},
    )


@pytest.fixture
def full_bundle(tmp_path: Path) -> Path:
    """A bundle with all optional files."""
    b = tmp_path / "full_bundle"
    b.mkdir()

    (b / "memory.md").write_text("# Q3 Report\n\nAcme quarterly data summary.", encoding="utf-8")
    (b / "metadata.json").write_text(
        json.dumps(
            {
                "identifier": "acme_q3",
                "importance": 0.9,
                "tags": ["finance", "q3"],
                "source": "analyst",
                "namespace": "reports",
                "provenance": {"author": "jane", "reviewed": True},
            }
        ),
        encoding="utf-8",
    )
    (b / "facts.json").write_text(
        json.dumps(
            [
                {"identifier": "acme", "fact": "Invoice #1234 was duplicated"},
                {"identifier": "acme", "fact": "Payment terms are net 30", "importance": 0.7},
            ]
        ),
        encoding="utf-8",
    )
    (b / "events.jsonl").write_text(
        json.dumps({"identifier": "acme", "event": "Invoice flagged for review"})
        + "\n"
        + json.dumps({"identifier": "acme", "event": "Vendor contacted"})
        + "\n",
        encoding="utf-8",
    )

    att_dir = b / "attachments"
    att_dir.mkdir()
    (att_dir / "invoice.csv").write_text("id,amount\n1,5000\n", encoding="utf-8")
    (att_dir / "notes.txt").write_text("follow up on duplicate", encoding="utf-8")

    (b / "manifest.json").write_text(
        json.dumps({"format": "hotmem-bundle", "version": 1}), encoding="utf-8"
    )

    return b


# ── 1. A minimal bundle hydrates ──────────────────────────────────────────────


def test_detect_bundle_minimal(minimal_bundle: Path):
    assert detect_bundle(minimal_bundle) is True


def test_detect_bundle_not_a_bundle(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_bundle(empty) is False


def test_minimal_bundle_hydrates(tmp_db: MemoryDB, minimal_bundle: Path):
    result = read_bundle(tmp_db, minimal_bundle)
    assert result.loaded == 1
    assert result.skipped_dupes == 0
    assert tmp_db.count() == 1

    rows = tmp_db.all_rows()
    assert "# Vendor Analysis" in rows[0]["fact_text"]
    # identifier defaults to the bundle directory name
    assert rows[0]["identifier"] == "bundle"


def test_minimal_bundle_via_dispatch(tmp_db: MemoryDB, minimal_bundle: Path):
    """The snapshot hydrate dispatch routes bundles to read_bundle."""
    result = hydrate(tmp_db, minimal_bundle)
    assert result.loaded == 1
    assert tmp_db.count() == 1


def test_detect_format_bundle(minimal_bundle: Path):
    assert detect_format(minimal_bundle) == "bundle"


# ── 2. Bundle with metadata + attachments preserves refs + provenance ─────────


def test_full_bundle_hydrates(tmp_db: MemoryDB, full_bundle: Path):
    result = read_bundle(tmp_db, full_bundle)
    # 1 (memory.md) + 2 (facts.json) + 2 (events.jsonl) + 2 (attachments) = 7
    assert result.loaded == 7
    assert result.skipped_dupes == 0
    assert tmp_db.count() == 7
    assert len(result.warnings) == 0


def test_metadata_applied_to_memory_md(tmp_db: MemoryDB, full_bundle: Path):
    read_bundle(tmp_db, full_bundle)
    rows = tmp_db.all_rows()
    md_row = next(r for r in rows if "Q3 Report" in (r["fact_text"] or ""))
    assert md_row["identifier"] == "acme_q3"
    assert md_row["importance"] == 0.9
    assert md_row["source"] == "analyst"
    assert md_row["namespace"] == "reports"
    tags = json.loads(md_row["tags"])
    assert "finance" in tags
    provenance = json.loads(md_row["provenance_json"])
    assert provenance["author"] == "jane"


def test_facts_json_inserted(tmp_db: MemoryDB, full_bundle: Path):
    read_bundle(tmp_db, full_bundle)
    rows = tmp_db.all_rows()
    fact_texts = [r["fact_text"] for r in rows]
    assert any("Invoice #1234 was duplicated" in t for t in fact_texts)
    assert any("Payment terms are net 30" in t for t in fact_texts)


def test_events_jsonl_inserted(tmp_db: MemoryDB, full_bundle: Path):
    read_bundle(tmp_db, full_bundle)
    rows = tmp_db.all_rows()
    event_texts = [r["fact_text"] for r in rows]
    assert any("Invoice flagged for review" in t for t in event_texts)
    assert any("Vendor contacted" in t for t in event_texts)


def test_attachments_are_file_backed_references(tmp_db: MemoryDB, full_bundle: Path):
    read_bundle(tmp_db, full_bundle)
    rows = tmp_db.all_rows()
    file_rows = [r for r in rows if r["memory_type"] == "file"]
    assert len(file_rows) == 2

    for row in file_rows:
        assert row["source_uri"] is not None
        assert row["byte_offset"] == 0
        assert row["byte_length"] > 0
        assert row["source_uri"].endswith((".csv", ".txt"))
        # No bytes copied into fact_text (empty for file-backed)
        assert row["fact_text"] == ""


def test_attachment_source_format_inferred(tmp_db: MemoryDB, full_bundle: Path):
    read_bundle(tmp_db, full_bundle)
    rows = tmp_db.all_rows()
    csv_row = next(r for r in rows if r["source_uri"] and r["source_uri"].endswith(".csv"))
    assert csv_row["source_format"] == "csv"


# ── 3. Unknown files do not fail permissive reads ─────────────────────────────


def test_unknown_files_ignored_with_warning(tmp_db: MemoryDB, tmp_path: Path):
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{
            "memory.md": "# Test\n\nContent here.",
            "random.txt": "unknown file",
            "notes.md": "another unknown",
        },
    )
    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 1
    # Warnings should mention the unknown files
    warning_text = " ".join(str(w) for w in result.warnings)
    assert "random.txt" in warning_text
    assert "notes.md" in warning_text


def test_unknown_files_do_not_fail_dispatch(tmp_db: MemoryDB, tmp_path: Path):
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{
            "memory.md": "# Test",
            "unknown_file.dat": "data",
        },
    )
    result = hydrate(tmp_db, bundle)
    assert result.loaded == 1


# ── 4. Existing JSONL hydrate remains unchanged ───────────────────────────────


def test_legacy_jsonl_still_hydrates(tmp_db: MemoryDB, tmp_path: Path):
    """A .jsonl file still hydrates via the same dispatch path."""
    swap = tmp_path / "swap.jsonl"
    swap.write_text(
        json.dumps({"identifier": "x", "fact_text": "legacy fact"}) + "\n", encoding="utf-8"
    )
    result = hydrate(tmp_db, swap)
    assert result.loaded == 1
    assert tmp_db.count() == 1


def test_v2_snapshot_still_hydrates(tmp_db: MemoryDB, tmp_path: Path):
    """A v2 snapshot directory (with manifest.json) still hydrates — not mistaken for a bundle."""
    # Add a memory, snapshot to v2, then hydrate into a fresh DB.
    from hotmem.embed import embed_text, pack_embedding
    from hotmem.snapshot import snapshot as do_snapshot

    tmp_db.insert(
        id="s1",
        identifier="snap",
        fact_text="snapshot test",
        embedding=pack_embedding(embed_text("x")),
    )
    snap_dir = tmp_path / "snap"
    do_snapshot(tmp_db, snap_dir)
    assert (snap_dir / "manifest.json").is_file()
    assert detect_format(snap_dir) == "v2"

    fresh_db_path = tmp_path / "fresh.sqlite"
    fresh_db = MemoryDB(fresh_db_path)
    result = hydrate(fresh_db, snap_dir)
    assert result.loaded == 1
    fresh_db.close()


# ── Extras: dedup, missing files, manifest.json ───────────────────────────────


def test_dedup_on_rehydrate(tmp_db: MemoryDB, minimal_bundle: Path):
    result1 = read_bundle(tmp_db, minimal_bundle)
    assert result1.loaded == 1

    result2 = read_bundle(tmp_db, minimal_bundle)
    assert result2.loaded == 0
    assert result2.skipped_dupes == 1
    assert tmp_db.count() == 1


def test_bundle_without_memory_md_raises(tmp_db: MemoryDB, tmp_path: Path):
    not_a_bundle = tmp_path / "not_bundle"
    not_a_bundle.mkdir()
    (not_a_bundle / "facts.json").write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        read_bundle(tmp_db, not_a_bundle)


def test_bundle_with_metadata_yaml(tmp_db: MemoryDB, tmp_path: Path):
    """metadata.yaml is read when PyYAML is available; otherwise falls back gracefully."""
    bundle = tmp_path / "yaml_bundle"
    bundle.mkdir()
    (bundle / "memory.md").write_text("# YAML Bundle\n\nContent.", encoding="utf-8")
    (bundle / "metadata.yaml").write_text(
        "identifier: yaml_test\nimportance: 0.8\ntags:\n  - yaml\n", encoding="utf-8"
    )
    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 1

    rows = tmp_db.all_rows()
    # If PyYAML is installed, metadata is applied; if not, a warning is emitted
    # and defaults are used. Either way, the bundle hydrates.
    row = rows[0]
    try:
        import yaml  # noqa: F401

        assert row["identifier"] == "yaml_test"
        assert row["importance"] == 0.8
    except ImportError:
        # PyYAML not installed — should warn and use defaults
        assert any("PyYAML" in str(w) for w in result.warnings)
        assert row["identifier"] == "yaml_bundle"  # defaults to dir name


def test_bundle_with_invalid_metadata_json_warns(tmp_db: MemoryDB, tmp_path: Path):
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{"memory.md": "# Test", "metadata.json": "{invalid json}"},
    )
    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 1
    assert any("metadata.json" in str(w) and "parse error" in str(w) for w in result.warnings)


def test_bundle_manifest_read_but_not_enforced(tmp_db: MemoryDB, full_bundle: Path):
    """manifest.json is read as metadata but not enforced in permissive mode."""
    result = read_bundle(tmp_db, full_bundle)
    assert result.loaded == 7
    # No warnings about the manifest (it's valid JSON)
    assert not any("manifest.json" in str(w) for w in result.warnings)


def test_bundle_searchable_after_hydrate(tmp_db: MemoryDB, full_bundle: Path):
    """After hydration, the memory.md content is searchable."""
    from hotmem.search import search_memories

    read_bundle(tmp_db, full_bundle)
    results = search_memories(tmp_db, "Q3 report acme", top_k=5)
    assert len(results) > 0
    assert any("Q3" in m["content"] for m in results)


# ── parse_bundle + BundleWarning + fallbacks + spy ────────────────────────────


def test_parse_bundle_produces_records_without_db(tmp_path: Path):
    """parse_bundle() is a pure function: produces records without a DB."""
    from hotmem.bundle import parse_bundle
    from hotmem.db import MemoryRecord

    bundle = _make_bundle(
        tmp_path / "bundle",
        **{"memory.md": "# Test\n\nContent."},
    )
    records, warnings = parse_bundle(bundle)
    assert len(records) == 1
    assert isinstance(records[0], MemoryRecord)
    assert "# Test" in records[0].fact_text
    assert len(warnings) == 0


def test_parse_bundle_full_bundle(tmp_path: Path):
    """parse_bundle() on a full bundle produces all record types."""
    from hotmem.bundle import parse_bundle

    b = tmp_path / "fb"
    b.mkdir()
    (b / "memory.md").write_text("# Full\n\nContent.", encoding="utf-8")
    (b / "facts.json").write_text(
        json.dumps([{"fact": "fact one"}, {"fact": "fact two"}]), encoding="utf-8"
    )
    (b / "events.jsonl").write_text(json.dumps({"event": "event one"}) + "\n", encoding="utf-8")
    att = b / "attachments"
    att.mkdir()
    (att / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    records, warnings = parse_bundle(b)
    # 1 (memory.md) + 2 (facts) + 1 (events) + 1 (attachment) = 5
    assert len(records) == 5
    # The attachment record should be file-backed.
    file_records = [r for r in records if r.memory_type == "file"]
    assert len(file_records) == 1
    assert file_records[0].source_uri is not None
    assert file_records[0].byte_length > 0


def test_index_md_fallback(tmp_db: MemoryDB, tmp_path: Path):
    """index.md is accepted as the memory body when memory.md is absent."""
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{"index.md": "# Index Body\n\nContent from index.md."},
    )
    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 1
    rows = tmp_db.all_rows()
    assert "Index Body" in rows[0]["fact_text"]


def test_readme_md_fallback(tmp_db: MemoryDB, tmp_path: Path):
    """README.md is accepted as the memory body when memory.md and index.md are absent."""
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{"README.md": "# README Body\n\nContent from README."},
    )
    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 1
    rows = tmp_db.all_rows()
    assert "README Body" in rows[0]["fact_text"]


def test_memory_md_precedence_over_index_md(tmp_db: MemoryDB, tmp_path: Path):
    """memory.md takes precedence over index.md when both are present."""
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{
            "memory.md": "# Primary\n\nFrom memory.md.",
            "index.md": "# Secondary\n\nFrom index.md.",
        },
    )
    read_bundle(tmp_db, bundle)
    rows = tmp_db.all_rows()
    assert "From memory.md" in rows[0]["fact_text"]


def test_bundle_warnings_are_structured(tmp_path: Path):
    """BundleWarning has .path and .message fields."""
    from hotmem.bundle import BundleWarning, parse_bundle

    bundle = _make_bundle(
        tmp_path / "bundle",
        **{"memory.md": "# Test", "random.dat": "unknown"},
    )
    _, warnings = parse_bundle(bundle)
    assert len(warnings) >= 1
    assert all(isinstance(w, BundleWarning) for w in warnings)
    assert all(hasattr(w, "path") and hasattr(w, "message") for w in warnings)
    # The unknown file should produce a warning mentioning it.
    assert any("random.dat" in w.path for w in warnings)


def test_bundle_hydrate_uses_path_not_adapter(tmp_db: MemoryDB, full_bundle: Path):
    """Bundle hydrate reads via Path directly, never touches the storage adapter."""
    import hotmem.storage as storage_mod

    original_get_adapter = getattr(storage_mod, "get_adapter", None)
    adapter_calls: list[str] = []

    def _tracking_adapter(uri: str):
        adapter_calls.append(uri)
        if original_get_adapter is not None:
            return original_get_adapter(uri)
        raise RuntimeError("adapter should not be called during bundle hydrate")

    storage_mod.get_adapter = _tracking_adapter
    try:
        read_bundle(tmp_db, full_bundle)
        assert adapter_calls == [], (
            f"bundle hydrate called storage adapter {len(adapter_calls)} times (expected 0)"
        )
    finally:
        if original_get_adapter is not None:
            storage_mod.get_adapter = original_get_adapter


# ── Review fixes: symlink rejection, metadata propagation, v2 precedence ────


def test_symlink_attachment_rejected(tmp_db: MemoryDB, tmp_path: Path):
    """A symlink in attachments/ pointing outside the bundle is rejected."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "memory.md").write_text("# Test\n\nContent.", encoding="utf-8")
    att_dir = bundle / "attachments"
    att_dir.mkdir()
    # Create a symlink pointing to /etc/passwd (outside the bundle).
    link = att_dir / "evil"
    target = tmp_path / "outside_secret.txt"
    target.write_text("secret", encoding="utf-8")
    link.symlink_to(target)

    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 1  # only the memory.md body
    warning_text = " ".join(str(w) for w in result.warnings)
    assert "evil" in warning_text
    assert "outside" in warning_text.lower()


def test_metadata_propagated_to_facts_and_events(tmp_db: MemoryDB, tmp_path: Path):
    """Bundle-level metadata (namespace, tier, tags, provenance) applies to all records."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "memory.md").write_text("# Body", encoding="utf-8")
    (bundle / "metadata.json").write_text(
        json.dumps(
            {
                "identifier": "test_ns",
                "namespace": "project_x",
                "tier": "warm",
                "tags": ["dev"],
                "provenance": {"author": "zubin"},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "facts.json").write_text(json.dumps([{"fact": "a fact"}]), encoding="utf-8")
    (bundle / "events.jsonl").write_text(json.dumps({"event": "an event"}) + "\n", encoding="utf-8")

    read_bundle(tmp_db, bundle)
    rows = tmp_db.all_rows()
    # All non-file rows should carry the bundle-level metadata.
    for row in rows:
        if row["memory_type"] != "file":
            assert row["namespace"] == "project_x"
            assert row["tier"] == "warm"
            tags = json.loads(row["tags"])
            assert "dev" in tags, f"tags not propagated for {row['identifier']}"
            prov = json.loads(row["provenance_json"]) if row["provenance_json"] else None
            assert prov is not None, f"provenance not propagated for {row['identifier']}"
            assert prov["author"] == "zubin"


def test_v2_precedence_over_bundle(tmp_db: MemoryDB, tmp_path: Path):
    """When both memory.md and manifest.json exist, v2 takes precedence."""
    bundle = tmp_path / "ambiguous"
    bundle.mkdir()
    (bundle / "memory.md").write_text("# Bundle content", encoding="utf-8")
    # Create a v2 snapshot here too.
    from hotmem.embed import embed_text, pack_embedding
    from hotmem.snapshot import snapshot as do_snapshot

    tmp_db.insert(
        id="v2only",
        identifier="v2",
        fact_text="v2 content",
        embedding=pack_embedding(embed_text("v2 content")),
    )
    do_snapshot(tmp_db, bundle)
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "memory.md").is_file()

    fresh = MemoryDB(tmp_path / "fresh.sqlite")
    result = hydrate(fresh, bundle)
    # Should hydrate the v2 snapshot (1 memory), not the bundle (also 1 but different).
    assert result.loaded == 1
    rows = fresh.all_rows()
    assert rows[0]["fact_text"] == "v2 content"
    fresh.close()


def test_malformed_events_jsonl_warns(tmp_db: MemoryDB, tmp_path: Path):
    """Malformed events.jsonl lines produce BundleWarning, not silent drops."""
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{
            "memory.md": "# Test",
            "events.jsonl": (
                json.dumps({"event": "valid event"})
                + "\n"
                + "not valid json\n"
                + json.dumps({"event": "another valid"})
                + "\n"
            ),
        },
    )
    result = read_bundle(tmp_db, bundle)
    assert result.loaded == 3  # 1 body + 2 valid events
    warning_text = " ".join(str(w) for w in result.warnings)
    assert "not valid json" in warning_text or "line 2" in warning_text


def test_both_metadata_files_warns(tmp_db: MemoryDB, tmp_path: Path):
    """Having both metadata.json and metadata.yaml emits an ambiguity warning."""
    bundle = _make_bundle(
        tmp_path / "bundle",
        **{
            "memory.md": "# Test",
            "metadata.json": json.dumps({"identifier": "from_json"}),
            "metadata.yaml": "identifier: from_yaml\n",
        },
    )
    result = read_bundle(tmp_db, bundle)
    warning_text = " ".join(str(w) for w in result.warnings)
    assert "metadata.yaml" in warning_text
    assert "metadata.json" in warning_text


def test_attachment_uses_relative_uri(tmp_db: MemoryDB, tmp_path: Path):
    """Attachment source_uri is relative to the bundle dir (portable)."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "memory.md").write_text("# Test", encoding="utf-8")
    att_dir = bundle / "attachments"
    att_dir.mkdir()
    (att_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    read_bundle(tmp_db, bundle)
    rows = tmp_db.all_rows()
    file_rows = [r for r in rows if r["memory_type"] == "file"]
    assert len(file_rows) == 1
    uri = file_rows[0]["source_uri"]
    # Should be a relative path, not absolute.
    assert not Path(uri).is_absolute(), f"expected relative URI, got {uri}"
    assert "data.csv" in uri
