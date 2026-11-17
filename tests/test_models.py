"""Explicit local model provisioning is pinned, verified, and offline-testable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hotmem.models import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    PIN_NAME,
    ModelError,
    ModelSpec,
    default_model_path,
    fetch_model,
    resolve_provisioned_model_path,
    verify_model,
)


def _spec_and_sources(tmp_path: Path) -> tuple[ModelSpec, dict[str, Path]]:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    files = {"config.json": b'{"model": "fixture"}\n', "weights.bin": b"pinned weights"}
    sources = {}
    hashes = {}
    for name, content in files.items():
        path = source_dir / name
        path.write_bytes(content)
        sources[name] = path
        hashes[name] = hashlib.sha256(content).hexdigest()
    return (
        ModelSpec(
            name="fixture-model",
            repo="example/fixture-model",
            commit="0123456789abcdef",
            files=hashes,
            pin={"model": "fixture-model", "revision": "v1", "preprocessing": "test-v1"},
            dimension=2,
            size_mb=1,
        ),
        sources,
    )


def test_fetch_verifies_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec, sources = _spec_and_sources(tmp_path)
    monkeypatch.setitem(KNOWN_MODELS, spec.name, spec)
    target = tmp_path / "models" / spec.name
    calls: list[str] = []

    def downloader(_spec: ModelSpec, filename: str) -> Path:
        calls.append(filename)
        return sources[filename]

    fetched = fetch_model(spec.name, target, downloader=downloader)
    assert fetched.already_present is False
    assert fetched.files == 2
    assert verify_model(target).verified
    assert json.loads((target / PIN_NAME).read_text(encoding="utf-8")) == spec.pin
    assert sorted(calls) == sorted(spec.files)

    calls_after_fetch = list(calls)
    repeated = fetch_model(
        spec.name,
        target,
        downloader=lambda *_: pytest.fail("verified artifacts should not be downloaded again"),
    )
    assert repeated.already_present is True
    assert calls == calls_after_fetch


def test_bad_checksum_does_not_replace_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec, sources = _spec_and_sources(tmp_path)
    monkeypatch.setitem(KNOWN_MODELS, spec.name, spec)
    target = tmp_path / "models" / spec.name
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("existing artifact", encoding="utf-8")

    def downloader(_spec: ModelSpec, filename: str) -> Path:
        if filename == "weights.bin":
            bad = tmp_path / "bad-weights.bin"
            bad.write_bytes(b"wrong weights")
            return bad
        return sources[filename]

    with pytest.raises(ModelError, match="checksum mismatch"):
        fetch_model(spec.name, target, downloader=downloader, force=True)

    assert marker.read_text(encoding="utf-8") == "existing artifact"
    assert not (target / "config.json").exists()


def test_failed_publish_restores_previous_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec, sources = _spec_and_sources(tmp_path)
    monkeypatch.setitem(KNOWN_MODELS, spec.name, spec)
    target = tmp_path / "models" / spec.name
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("existing artifact", encoding="utf-8")
    real_replace = __import__("hotmem.models", fromlist=["os"]).os.replace
    fail_once = True

    def replace(source, destination):
        nonlocal fail_once
        if (
            fail_once
            and Path(destination) == target
            and Path(source).name.startswith(f".{spec.name}.")
        ):
            fail_once = False
            raise OSError("synthetic publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr("hotmem.models.os.replace", replace)
    with pytest.raises(OSError, match="synthetic publish failure"):
        fetch_model(
            spec.name,
            target,
            downloader=lambda _spec, filename: sources[filename],
            force=True,
        )

    assert marker.read_text(encoding="utf-8") == "existing artifact"
    assert not (target / "config.json").exists()


def test_corrupt_known_default_artifact_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOTMEM_MODELS_DIR", str(tmp_path / "models"))
    path = default_model_path()
    path.mkdir(parents=True)
    (path / PIN_NAME).write_text(json.dumps(KNOWN_MODELS[DEFAULT_MODEL].pin), encoding="utf-8")

    status = verify_model(path)
    assert status.provisioned is True
    assert status.known is True
    assert status.verified is False
    with pytest.raises(ModelError, match="failed checksum verification"):
        resolve_provisioned_model_path()


def test_manually_pinned_artifact_is_reported_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOTMEM_MODELS_DIR", str(tmp_path / "models"))
    path = default_model_path()
    path.mkdir(parents=True)
    pin = {"model": "operator-model", "revision": "local-v3", "preprocessing": "custom-v1"}
    (path / PIN_NAME).write_text(json.dumps(pin), encoding="utf-8")

    status = verify_model(path)
    assert status.provisioned is True
    assert status.known is False
    assert status.verified is False
    assert resolve_provisioned_model_path() == path
