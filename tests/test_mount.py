"""Tests for hotmem.mount — directory bootstrapping."""

from __future__ import annotations

import json
from pathlib import Path

from hotmem.mount import bootstrap_mount


def test_bootstrap_creates_directory(tmp_path: Path):
    mount_dir = tmp_path / "new_mount"
    config = bootstrap_mount(mount_dir)
    assert config.mount_path.exists()
    assert config.manifest_path.exists()
    assert config.db_path == mount_dir.resolve() / "hotmem.sqlite"
    assert config.swap_path == mount_dir.resolve() / "swap.jsonl"


def test_bootstrap_creates_manifest(tmp_path: Path):
    config = bootstrap_mount(tmp_path / "m")
    manifest = json.loads(config.manifest_path.read_text())
    assert "hotmem_version" in manifest
    assert "created_at" in manifest


def test_bootstrap_idempotent(tmp_path: Path):
    mount_dir = tmp_path / "m"
    config1 = bootstrap_mount(mount_dir)
    manifest1 = config1.manifest_path.read_text()
    config2 = bootstrap_mount(mount_dir)
    manifest2 = config2.manifest_path.read_text()
    assert manifest1 == manifest2
