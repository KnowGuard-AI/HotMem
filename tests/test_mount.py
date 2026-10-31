"""Tests for hotmem.mount -- directory bootstrapping and mount lineage (#94)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.interchange.canonical import compute_content_hash, logical_id
from hotmem.interchange.fingerprint import state_fingerprint
from hotmem.mount import MountIdentity, bootstrap_mount, compare_mount_lineage
from hotmem.runtime import HotMemRuntime
from hotmem.swap import add_memory


def test_bootstrap_creates_directory(tmp_path: Path):
    mount_dir = tmp_path / "new_mount"
    config = bootstrap_mount(mount_dir)
    assert config.mount_path.exists()
    assert config.manifest_path.exists()
    assert config.db_path == mount_dir.resolve() / "hotmem.sqlite"
    assert config.swap_path == mount_dir.resolve() / "swap.jsonl"
    assert config.mount_id.startswith("mnt-")


def test_bootstrap_creates_manifest(tmp_path: Path):
    config = bootstrap_mount(tmp_path / "m")
    manifest = json.loads(config.manifest_path.read_text())
    assert "hotmem_version" in manifest
    assert "created_at" in manifest
    assert manifest["mount_id"] == config.mount_id


def test_bootstrap_idempotent(tmp_path: Path):
    mount_dir = tmp_path / "m"
    config1 = bootstrap_mount(mount_dir)
    manifest1 = config1.manifest_path.read_text()
    config2 = bootstrap_mount(mount_dir)
    manifest2 = config2.manifest_path.read_text()
    assert manifest1 == manifest2
    assert config1.mount_id == config2.mount_id


def test_bootstrap_explicit_mount_id_and_conflict_rejection(tmp_path: Path):
    mount_dir = tmp_path / "explicit_m"
    cfg = bootstrap_mount(mount_dir, mount_id="mnt-custom-workspace-01")
    assert cfg.mount_id == "mnt-custom-workspace-01"
    assert bootstrap_mount(mount_dir, mount_id="mnt-custom-workspace-01").mount_id == (
        "mnt-custom-workspace-01"
    )
    with pytest.raises(ValueError, match="bound to mount_id"):
        bootstrap_mount(mount_dir, mount_id="mnt-different-workspace")


def test_mount_identity_adopts_manifest_and_separates_runtime_identities(tmp_path: Path):
    mount_dir = tmp_path / "ws"
    cfg = bootstrap_mount(mount_dir, mount_id="mnt-ops-brain")

    rt1 = HotMemRuntime(cfg.db_path, store_id="store-ops-01").open()
    try:
        ident1 = rt1.db.mount_identity(runtime_generation=rt1.runtime_generation)
        assert ident1.mount_id == "mnt-ops-brain"
        assert ident1.mount_bound is True
        assert ident1.replica_id.startswith("rep-")
        assert ident1.history_id.startswith("hist-")
        assert ident1.runtime_generation == rt1.runtime_generation
        assert rt1.store_id == "store-ops-01"
        assert len({ident1.mount_id, ident1.replica_id, ident1.history_id, rt1.store_id}) == 4
    finally:
        rt1.close()

    # Restarting the runtime regenerates runtime_generation while keeping
    # mount_id, replica_id, history_id, and store_id stable.
    rt2 = HotMemRuntime(cfg.db_path, store_id="store-ops-01").open()
    try:
        ident2 = rt2.db.mount_identity(runtime_generation=rt2.runtime_generation)
        assert ident2.mount_id == ident1.mount_id
        assert ident2.replica_id == ident1.replica_id
        assert ident2.history_id == ident1.history_id
        assert ident2.runtime_generation != ident1.runtime_generation
    finally:
        rt2.close()


def test_compare_mount_lineage_distinguishes_same_clone_fork_replacement_and_different(
    tmp_path: Path,
):
    src = MemoryDB(tmp_path / "src.sqlite")
    add_memory(src, "ops/runbook", "Primary runbook fact")
    src_ident = src.mount_identity()
    src_rows = src.all_rows(include_embedding=True)
    src_snap_id = logical_id(r["content_hash"] for r in src_rows)
    src_fp = state_fingerprint(src_rows)

    # 1. Same replica.
    same_cmp = compare_mount_lineage(src, src)
    assert same_cmp["relationship"] == "same_replica"
    assert same_cmp["same_mount"] is True
    assert same_cmp["same_replica"] is True

    # 2. Different mount.
    other = MemoryDB(tmp_path / "other.sqlite")
    add_memory(other, "ops/runbook", "Primary runbook fact")
    diff_cmp = compare_mount_lineage(src, other)
    assert diff_cmp["relationship"] == "different_mount"
    assert diff_cmp["same_mount"] is False
    assert diff_cmp["same_replica"] is False

    # 3. Clean clone of the same logical mount (distinct replica_id, zero post-base mutations).
    clone = MemoryDB(tmp_path / "clone.sqlite")
    clone.insert(
        id=src_rows[0]["id"],
        identifier=src_rows[0]["identifier"],
        fact_text=src_rows[0]["fact_text"],
        embedding=src_rows[0]["embedding"],
        content_hash=src_rows[0]["content_hash"],
    )
    clone.record_clone_lineage(
        source_mount_id=src_ident.mount_id,
        source_replica_id=src_ident.replica_id,
        source_history_id=src_ident.history_id,
        base_snapshot_id=src_snap_id,
        base_state_fingerprint=src_fp,
    )
    clone_cmp = compare_mount_lineage(src, clone)
    assert clone_cmp["relationship"] == "clone"
    assert clone_cmp["same_mount"] is True
    assert clone_cmp["same_replica"] is False
    assert clone.mount_identity().replica_id != src_ident.replica_id

    # 4. Divergent runtime fork when the clone mutates locally after base hydration.
    add_memory(clone, "ops/local-divergence", "Clone-only local write")
    fork_cmp = compare_mount_lineage(src, clone)
    assert fork_cmp["relationship"] == "fork"
    assert fork_cmp["same_mount"] is True
    assert fork_cmp["same_replica"] is False

    # 5. Explicit fork_replica followed by write on parent or fork.
    branch = MemoryDB(tmp_path / "branch.sqlite")
    branch.fork_replica(parent_db=src)
    add_memory(branch, "ops/branch-item", "Branch write")
    branch_cmp = compare_mount_lineage(src, branch)
    assert branch_cmp["relationship"] == "fork"

    # 6. Raw filesystem copy (shutil.copy) where both copies diverge via state and events.
    src.close()
    raw_copy_path = tmp_path / "raw_copy.sqlite"
    shutil.copy2(tmp_path / "src.sqlite", raw_copy_path)
    src = MemoryDB(tmp_path / "src.sqlite")
    raw_copy = MemoryDB(raw_copy_path)
    add_memory(src, "ops/after-copy-a", "Source side write after raw copy")
    add_memory(raw_copy, "ops/after-copy-b", "Copy side write after raw copy")
    raw_state_cmp = compare_mount_lineage(src, raw_copy)
    assert raw_state_cmp["relationship"] == "fork"
    assert raw_state_cmp["reason"] == "divergent_state_on_copied_replica"

    src.append_event(
        event_id="evt-copy-a",
        event_type="memory.created",
        occurred_at="2026-10-30T00:00:00Z",
    )
    raw_copy.append_event(
        event_id="evt-copy-b",
        event_type="memory.created",
        occurred_at="2026-10-30T00:00:00Z",
    )
    raw_fork_cmp = compare_mount_lineage(src, raw_copy)
    assert raw_fork_cmp["relationship"] == "fork"
    assert raw_fork_cmp["reason"] == "divergent_event_id_at_same_seq"

    # 7. History replacement at the same mount_id.
    replaced = MemoryDB(tmp_path / "replaced.sqlite")
    replaced.set_mount_identity(src_ident.mount_id)
    replaced.replace_history(keep_mount_id=True)
    rep_cmp = compare_mount_lineage(src, replaced)
    assert rep_cmp["relationship"] == "replacement"
    assert rep_cmp["same_mount"] is True
    assert rep_cmp["same_replica"] is False

    for db in (src, other, clone, branch, raw_copy, replaced):
        db.close()


def test_mount_identity_roundtrips_dict():
    ident = MountIdentity(
        mount_id="mnt-1",
        replica_id="rep-1",
        history_id="hist-1",
        runtime_generation="gen-1",
        base_snapshot_id="snap-1",
        base_state_fingerprint="fp-1",
        base_source_mount_id="mnt-1",
        base_source_replica_id="rep-0",
        base_history_id="hist-0",
        base_cursor="hmc1.cursor",
        parent_replica_id="rep-0",
        forked_at_seq=4,
        forked_at_event_id="evt-4",
        cloned_at_seq=2,
        trimmed_through_seq=1,
        legacy_history=True,
        mount_bound=True,
    )
    assert MountIdentity.from_dict(ident.to_dict()) == ident
    _ = compute_content_hash("x", "y")


def test_copied_replica_with_different_head_and_imported_state_is_a_fork(tmp_path: Path):
    """#94: same replica/history must not hide out-of-band state divergence just
    because the sequence heads differ."""
    import shutil

    src = MemoryDB(tmp_path / "head_src.sqlite")
    src.append_event(
        event_id="evt-1",
        event_type="memory.created",
        occurred_at="2026-10-30T00:00:00Z",
    )
    src.close()
    copy_path = tmp_path / "head_copy.sqlite"
    shutil.copy2(tmp_path / "head_src.sqlite", copy_path)

    src = MemoryDB(tmp_path / "head_src.sqlite")
    copy = MemoryDB(copy_path)
    try:
        copy.append_event(
            event_id="evt-2",
            event_type="memory.created",
            occurred_at="2026-10-30T00:00:00Z",
        )
        copy.insert(
            id="imported-row",
            identifier="imported",
            fact_text="state imported without an event",
            embedding=b"",
            content_hash="imported-hash",
        )
        result = compare_mount_lineage(src, copy)
        assert result["relationship"] == "fork"
        assert result["reason"] == "divergent_state_on_copied_replica"
    finally:
        src.close()
        copy.close()
