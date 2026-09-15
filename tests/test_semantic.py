"""Optional local semantic adapter — the [semantic] extra (issue #78).

Default-suite tests stub model2vec so the adapter's validation, descriptor
pinning, and normalization are proven without the optional dependency; the
real-model integration test is opt-in via HOTMEM_TEST_SEMANTIC_MODEL.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import random
import sys
import types
from pathlib import Path

import pytest

_STUB_DIM = 256


def _stub_vec(text: str) -> list[float]:
    seed = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
    rng = random.Random(seed)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(_STUB_DIM)]
    return [x * 3.0 for x in vec]  # deliberately unnormalized


class _StubStaticModel:
    """Deterministic stand-in for model2vec.StaticModel (unnormalized)."""

    def __init__(self, _path: str) -> None:
        self.loaded_from = _path
        self.dim = _STUB_DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_stub_vec(t) for t in texts]

    @staticmethod
    def from_pretrained(path: str) -> _StubStaticModel:
        return _StubStaticModel(path)


@pytest.fixture
def stubbed_model2vec(monkeypatch: pytest.MonkeyPatch):
    """Install a stub model2vec and import hotmem.semantic against it."""
    monkeypatch.setitem(
        sys.modules, "model2vec", types.SimpleNamespace(StaticModel=_StubStaticModel)
    )
    sys.modules.pop("hotmem.semantic", None)
    return importlib.import_module("hotmem.semantic")


_artifact_seq = 0


def _artifact(tmp_path: Path, pin: dict | None, name: str = "semantic-model") -> Path:
    art = tmp_path / name
    art.mkdir()
    (art / "model.onnx").write_bytes(b"stub model bytes")
    if pin is not None:
        (art / "hotmem-model.json").write_text(json.dumps(pin))
    return art


def test_module_gated_behind_semantic_extra():
    """Without model2vec the adapter module refuses to import, with the hint."""
    if importlib.util.find_spec("model2vec") is not None:
        pytest.skip("model2vec installed — gating not observable")
    with pytest.raises(ImportError, match=r"\[semantic\] extra"):
        importlib.import_module("hotmem.semantic")


def test_adapter_rejects_missing_artifact_directory(stubbed_model2vec, tmp_path: Path):
    """A nonexistent model path fails before anything is loaded — no download."""
    LocalSemanticEmbedder = stubbed_model2vec.LocalSemanticEmbedder
    with pytest.raises(ValueError, match="never downloads"):
        LocalSemanticEmbedder(tmp_path / "does-not-exist")


def test_adapter_requires_identity_pin(stubbed_model2vec, tmp_path: Path):
    LocalSemanticEmbedder = stubbed_model2vec.LocalSemanticEmbedder
    with pytest.raises(ValueError, match="hotmem-model.json"):
        LocalSemanticEmbedder(_artifact(tmp_path, pin=None))
    with pytest.raises(ValueError, match="not valid JSON"):
        bad = _artifact(tmp_path, pin=None, name="bad-json")
        (bad / "hotmem-model.json").write_text("{not json")
        LocalSemanticEmbedder(bad)
    with pytest.raises(ValueError, match="required"):
        no_model = _artifact(tmp_path, pin=None, name="no-model")
        (no_model / "hotmem-model.json").write_text(json.dumps({"model": ""}))
        LocalSemanticEmbedder(no_model)


def test_adapter_pins_descriptor_from_artifact(stubbed_model2vec, tmp_path: Path):
    """The descriptor identity comes from the provisioned pin, not discovery."""
    LocalSemanticEmbedder = stubbed_model2vec.LocalSemanticEmbedder
    art = _artifact(
        tmp_path, pin={"model": "minilm-l12-v2", "revision": "r1", "preprocessing": "pp1"}
    )
    embedder = LocalSemanticEmbedder(art)
    assert embedder.descriptor.implementation == "local-m2v"
    assert embedder.descriptor.model == "minilm-l12-v2"
    assert embedder.descriptor.revision == "r1"
    assert embedder.descriptor.dimension == _STUB_DIM
    assert embedder.descriptor.normalization == "l2"
    assert embedder.descriptor.key == "local-m2v/minilm-l12-v2/rev:r1/norm:l2/pp:pp1"


def test_adapter_l2_normalizes_output(stubbed_model2vec, tmp_path: Path):
    """The stub returns unnormalized vectors; the adapter enforces the pin."""
    LocalSemanticEmbedder = stubbed_model2vec.LocalSemanticEmbedder
    art = _artifact(tmp_path, pin={"model": "m", "revision": "r1"})
    embedder = LocalSemanticEmbedder(art)
    raw_norm = math.sqrt(sum(x * x for x in _stub_vec("normalization probe")))
    assert raw_norm > 2.0  # the stub really is unnormalized
    vec = embedder.embed("normalization probe")
    assert len(vec) == _STUB_DIM
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-6)
    # Deterministic: same text, same vector.
    assert embedder.embed("normalization probe") == vec
    assert embedder.embed("a different fact") != vec


def test_resolver_builds_local_semantic_from_artifact(
    stubbed_model2vec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """resolve_embedder_from_config('local-semantic') loads the provisioned pin."""
    from hotmem.embed import resolve_embedder_from_config

    art = _artifact(tmp_path, pin={"model": "m", "revision": "r1", "preprocessing": "pp1"})
    monkeypatch.delenv("HOTMEM_EMBEDDER_MODEL_PATH", raising=False)
    embedder = resolve_embedder_from_config("local-semantic", model_path=str(art))
    assert embedder.descriptor.implementation == "local-m2v"
    assert embedder.descriptor.dimension == _STUB_DIM


def test_resolver_local_semantic_requires_model_path():
    """Without a provisioned model path the resolver refuses — never downloads."""
    from hotmem.embed import resolve_embedder_from_config

    if importlib.util.find_spec("model2vec") is None:
        # Without the extra the import gate fires first.
        with pytest.raises(ValueError, match=r"\[semantic\] extra"):
            resolve_embedder_from_config("local-semantic", model_path=None)
    else:
        with pytest.raises(ValueError, match="never downloads"):
            resolve_embedder_from_config("local-semantic", model_path=None)


def test_semantic_runtime_end_to_end_through_library(
    stubbed_model2vec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Write+search+snapshot round-trip under the adapter's descriptor."""
    from hotmem.embed import resolve_embedder_from_config
    from hotmem.search import search_memories
    from hotmem.snapshot import hydrate as snapshot_hydrate
    from hotmem.snapshot import snapshot as snapshot_write
    from hotmem.swap import add_memory

    art = _artifact(tmp_path, pin={"model": "m", "revision": "r1", "preprocessing": "pp1"})
    embedder = resolve_embedder_from_config("local-semantic", model_path=str(art))

    db_dir = tmp_path / "runtime"
    db_dir.mkdir()
    from hotmem.db import MemoryDB

    db = MemoryDB(db_dir / "sem.sqlite")
    add_memory(db, "vendor", "semantic runtime end to end fact", embedder=embedder)
    hits = search_memories(db, "semantic runtime end to end fact", top_k=1, embedder=embedder)
    assert hits[0]["content"] == "semantic runtime end to end fact"

    snap = db_dir / "snapshot.jsonl"
    snapshot_write(db, snap)
    target = MemoryDB(db_dir / "target.sqlite")
    result = snapshot_hydrate(target, snap, embedder=embedder)
    assert result.embedding_reused == 1  # same-space restore reuses vectors
    db.close()
    target.close()


def test_real_model_integration():
    """Opt-in: real model2vec artifact via HOTMEM_TEST_SEMANTIC_MODEL.

    Provisions per docs/retrieval-quality.md; skipped when the variable is
    unset (the default suite never downloads or loads real models).
    """
    import os

    model_path = os.environ.get("HOTMEM_TEST_SEMANTIC_MODEL")
    if not model_path or not Path(model_path).is_dir():
        pytest.skip("HOTMEM_TEST_SEMANTIC_MODEL unset or not a directory")
    pytest.importorskip("model2vec", reason="requires the optional [semantic] extra")

    from hotmem.semantic import LocalSemanticEmbedder

    embedder = LocalSemanticEmbedder(Path(model_path))
    vec = embedder.embed("integration probe fact")
    assert len(vec) == embedder.descriptor.dimension
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-6)
    assert embedder.descriptor.key.startswith("local-m2v/")
