"""Runtime embedder injection — issue #78 acceptance tests.

Two runtimes in one process, explicit injection through every library path
(writes, search, package restore, snapshot v2 hydration, delta apply), and
the four embedding-disposition statuses.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import (
    EMBEDDING_MODEL,
    EmbeddingDescriptor,
    pack_embedding,
)
from hotmem.interchange.delta import apply_delta, produce_delta
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package
from hotmem.search import search_memories
from hotmem.snapshot import hydrate as snapshot_hydrate
from hotmem.snapshot import snapshot as snapshot_write
from hotmem.snapshot.writer import write_snapshot_v2
from hotmem.swap import add_memory


def _stable_vec(text: str, dim: int) -> list[float]:
    seed = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
    rng = random.Random(seed)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class SemanticFake:
    """Deterministic 128-dim stand-in for the optional local semantic adapter."""

    def __init__(self, dim: int = 128) -> None:
        self._descriptor = EmbeddingDescriptor(
            implementation="local",
            model="semantic-fake",
            dimension=dim,
            revision="r1",
            preprocessing="pp1",
        )
        self.calls = 0

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return _stable_vec(text, self._descriptor.dimension)


def test_two_runtimes_one_process_isolated(tmp_path: Path):
    """#78: hash and semantic runtimes coexist; each stamps its own space."""
    hash_db = MemoryDB(tmp_path / "hash.sqlite")
    sem_db = MemoryDB(tmp_path / "sem.sqlite")
    semantic = SemanticFake()
    fact = "invoice risk for vendor x"

    add_memory(hash_db, "vendor", fact)
    add_memory(sem_db, "vendor", fact, embedder=semantic)

    hash_row = hash_db.all_rows(include_embedding=True)[0]
    sem_row = sem_db.all_rows(include_embedding=True)[0]
    assert hash_row["embedding_model"] == EMBEDDING_MODEL
    assert hash_row["embedding_dim"] == 64
    assert sem_row["embedding_model"] == semantic.descriptor.key
    assert sem_row["embedding_dim"] == 128

    # Each runtime finds its own record via its own query embedding.
    assert search_memories(hash_db, fact, top_k=1)[0]["content"] == fact
    assert search_memories(sem_db, fact, top_k=1, embedder=semantic)[0]["content"] == fact

    # Cross-runtime cosine is inert (mismatched dims score 0); lexical still
    # finds the record — mixed spaces never crash or mis-score.
    cross = search_memories(sem_db, fact, top_k=1)
    assert cross[0]["content"] == fact
    hash_db.close()
    sem_db.close()


def test_hydrate_reuses_semantic_vectors_within_same_space(tmp_path: Path):
    """A semantic package restored with its own embedder reuses every vector."""
    src = MemoryDB(tmp_path / "src.sqlite")
    semantic = SemanticFake()
    add_memory(src, "vendor", "semantic hydration fact", embedder=semantic)
    pkg = tmp_path / "pkg"
    write_package(src, pkg)

    target = MemoryDB(tmp_path / "target.sqlite")
    before = semantic.calls
    result = hydrate_package(target, pkg, embedder=semantic)
    assert result.loaded == 1
    assert result.embedding_reused == 1
    assert result.embedding_rebuilt == 0
    assert semantic.calls == before  # zero embed work on the restore path
    row = target.all_rows(include_embedding=True)[0]
    assert row["embedding_model"] == semantic.descriptor.key
    src.close()
    target.close()


def test_hydrate_rebuilds_semantic_vectors_under_hash_default(tmp_path: Path):
    """The same package restored with the hash default rebuilds and restamps."""
    src = MemoryDB(tmp_path / "src.sqlite")
    semantic = SemanticFake()
    add_memory(src, "vendor", "semantic hydration fact", embedder=semantic)
    pkg = tmp_path / "pkg"
    write_package(src, pkg)

    target = MemoryDB(tmp_path / "target.sqlite")
    result = hydrate_package(target, pkg)
    assert result.embedding_rebuilt == 1
    row = target.all_rows(include_embedding=True)[0]
    assert row["embedding_model"] == EMBEDDING_MODEL
    assert row["embedding_dim"] == 64
    src.close()
    target.close()


def test_hydrate_provider_failure_preserves_canonical_record(tmp_path: Path):
    """#78: a failing provider loads the record without a vector, reported."""

    class Exploding(SemanticFake):
        def embed(self, text: str) -> list[float]:
            self.calls += 1
            raise RuntimeError("provider down")

    src = MemoryDB(tmp_path / "src.sqlite")
    add_memory(src, "vendor", "provider failure fact")
    pkg = tmp_path / "pkg"
    write_package(src, pkg)
    # Corrupt the stored vector's descriptor so the restore path must re-embed;
    # update the manifest's file entry so verification still passes.
    payload = pkg / "memories.jsonl"
    lines = []
    for line in payload.read_text().splitlines():
        rec = json.loads(line)
        rec["embedding_model"] = "foreign-v9"
        lines.append(json.dumps(rec))
    payload.write_text("\n".join(lines) + "\n")
    manifest = json.loads((pkg / "manifest.json").read_text())
    manifest["files"]["memories.jsonl"] = {
        "size": payload.stat().st_size,
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest))

    target = MemoryDB(tmp_path / "target.sqlite")
    result = hydrate_package(target, pkg, embedder=Exploding())
    assert result.loaded == 1
    assert result.embedding_failed == 1
    row = target.all_rows(include_embedding=True)[0]
    assert row["fact_text"] == "provider failure fact"  # canonical record intact
    assert row["embedding"] == b""  # NULL-embedding convention
    src.close()
    target.close()


def test_v2_snapshot_hydration_reports_embedding_disposition(tmp_path: Path):
    """Snapshot v2 hydration reports reuse/rebuild under injected embedders."""
    src = MemoryDB(tmp_path / "src.sqlite")
    semantic = SemanticFake()
    add_memory(src, "vendor", "v2 disposition fact", embedder=semantic)
    snap_dir = tmp_path / "snap"
    write_snapshot_v2(src, snap_dir)

    same = MemoryDB(tmp_path / "same.sqlite")
    reused = snapshot_hydrate(same, snap_dir, embedder=semantic)
    assert reused.embedding_reused == 1
    assert reused.embedding_rebuilt == 0

    other = MemoryDB(tmp_path / "other.sqlite")
    rebuilt = snapshot_hydrate(other, snap_dir)
    assert rebuilt.embedding_rebuilt == 1
    assert rebuilt.embedding_reused == 0
    src.close()
    same.close()
    other.close()


def test_delta_apply_reuses_same_space_and_rebuilds_foreign_vectors(tmp_path: Path):
    """Delta apply reuses same-space vectors and re-embeds foreign ones.

    Delta records carry base64 embeddings (fixed: raw db blobs were once
    serialized as python reprs, forcing a rebuild on every apply); a
    same-space upsert reuses its vector, a foreign-model record rebuilds
    under the active embedder and is stamped accordingly.
    """
    base = MemoryDB(tmp_path / "base.sqlite")
    semantic = SemanticFake()
    add_memory(base, "vendor", "delta baseline fact", embedder=semantic)
    base_pkg = tmp_path / "base.pkg"
    write_package(base, base_pkg)

    producer = MemoryDB(tmp_path / "producer.sqlite")
    hydrate_package(producer, base_pkg, embedder=semantic)
    add_memory(producer, "vendor", "delta updated fact", embedder=semantic)
    # A second record stored under a FOREIGN model (mixed-space producer).
    foreign_vec = pack_embedding(_stable_vec("foreign delta fact", 64))

    import hashlib as _hl

    producer.insert(
        id="foreign-rec",
        identifier="vendor",
        fact_text="foreign delta fact",
        embedding=foreign_vec,
        embedding_dim=64,
        embedding_model="foreign/v9",
        content_hash=_hl.sha256(b"foreign").hexdigest(),
    )
    delta_dir = tmp_path / "delta"
    produce_delta(producer, base_pkg, delta_dir)

    receiver = MemoryDB(tmp_path / "receiver.sqlite")
    apply_result = apply_delta(receiver, delta_dir, embedder=semantic)
    assert apply_result.applied >= 2
    assert apply_result.embedding_reused == 1  # same-space upsert reused
    assert apply_result.embedding_rebuilt == 1  # foreign record re-embedded
    rows = {r["fact_text"]: r for r in receiver.all_rows(include_embedding=True)}
    assert rows["delta updated fact"]["embedding_model"] == semantic.descriptor.key
    assert rows["foreign delta fact"]["embedding_model"] == semantic.descriptor.key

    # Replay is idempotent: zero applies, zero embed work.
    replay = apply_delta(receiver, delta_dir, embedder=semantic)
    assert replay.applied == 0
    assert replay.embedding_rebuilt == 0 and replay.embedding_reused == 0
    for db in (base, producer, receiver):
        db.close()


def test_hydrate_result_embedding_fields_default_zero():
    """The four disposition fields are additive — existing callers unaffected."""
    from hotmem.interchange.delta import ApplyResult
    from hotmem.swap import HydrateResult

    result = HydrateResult(loaded=2, skipped_dupes=1)
    assert (result.embedding_reused, result.embedding_rebuilt) == (0, 0)
    assert (result.embedding_missing, result.embedding_failed) == (0, 0)
    apply_result = ApplyResult(applied=1, skipped=0, resulting_state_fingerprint=None)
    assert apply_result.embedding_rebuilt == 0


def test_write_and_search_through_injected_embedder(tmp_path: Path):
    """The semantic runtime's write+search path works end to end in the library."""
    db = MemoryDB(tmp_path / "e2e.sqlite")
    semantic = SemanticFake()
    add_memory(db, "vendors", "payment terms are net 30", embedder=semantic)
    add_memory(db, "vendors", "invoice approval requires a PO", embedder=semantic)
    hits = search_memories(db, "invoice approval requires a PO", top_k=1, embedder=semantic)
    assert hits[0]["content"] == "invoice approval requires a PO"
    assert hits[0]["score"] > 0
    db.close()


def test_swap_hydrate_injects_embedder_for_foreign_vectors(tmp_path: Path):
    """Legacy swap hydration rebuilds foreign-model rows under the active embedder."""
    swap = tmp_path / "foreign.jsonl"
    blob = pack_embedding(_stable_vec("foreign stored fact", 64))
    import base64

    swap.write_text(
        json.dumps(
            {
                "identifier": "foreign",
                "fact_text": "foreign stored fact",
                "embedding_model": "foreign/v9",
                "embedding_dim": 64,
                "embedding_b64": base64.b64encode(blob).decode("ascii"),
            }
        )
        + "\n"
    )
    db = MemoryDB(tmp_path / "h.sqlite")
    semantic = SemanticFake()
    result = snapshot_hydrate(db, swap, embedder=semantic)
    assert result.embedding_rebuilt == 1
    row = db.all_rows(include_embedding=True)[0]
    assert row["embedding_model"] == semantic.descriptor.key
    assert row["embedding_dim"] == 128
    db.close()


def test_snapshot_dispatch_preserves_injected_embedder_across_formats(tmp_path: Path):
    """The unified snapshot hydrate dispatcher threads the embedder everywhere."""
    db = MemoryDB(tmp_path / "dispatch.sqlite")
    semantic = SemanticFake()
    add_memory(db, "vendor", "dispatch disposition fact", embedder=semantic)

    legacy = tmp_path / "legacy.jsonl"
    snapshot_write(db, legacy)
    target = MemoryDB(tmp_path / "t1.sqlite")
    result = snapshot_hydrate(target, legacy, embedder=semantic)
    # Legacy export carries the semantic vector; same-space restore reuses it.
    assert result.embedding_reused == 1
    assert result.embedding_rebuilt == 0
    db.close()
    target.close()


# ── mixed-space safety (issue #78, C6) ───────────────────────────────────────


def _make_same_dim_foreign_row(db: MemoryDB, fact: str) -> None:
    """Insert a 64-dim row stamped with a foreign descriptor key.

    Same dimension as the hash space — the cosine UDF would happily compute
    a (meaningless) similarity without the descriptor guard.
    """
    import uuid

    from hotmem.interchange.canonical import compute_content_hash

    vec = _stable_vec(fact, 64)
    db.insert(
        id=uuid.uuid4().hex,
        identifier="foreign",
        fact_text=fact,
        embedding=pack_embedding(vec),
        embedding_dim=64,
        embedding_model="foreign/model-v9",
        content_hash=compute_content_hash("foreign", fact),
    )


def test_mixed_space_same_dim_rows_score_zero_cosine(tmp_path: Path):
    """A foreign 64-dim row is never cosine-scored against a hash query."""
    db = MemoryDB(tmp_path / "mixed.sqlite")
    add_memory(db, "vendor", "hash space fact")
    _make_same_dim_foreign_row(db, "foreign space fact that matches invoice")

    rows = db.search_with_cosine(
        pack_embedding(_stable_vec("foreign space fact that matches invoice", 64)),
        embedding_model=EMBEDDING_MODEL,
    )
    by_text = {r["fact_text"]: r["cosine_score"] for r in rows}
    assert by_text["foreign space fact that matches invoice"] == 0.0
    assert by_text["hash space fact"] >= 0.0  # hash rows still score normally
    # Unfiltered call (direct callers) keeps the historical behavior.
    unguarded = db.search_with_cosine(
        pack_embedding(_stable_vec("foreign space fact that matches invoice", 64))
    )
    by_text_unguarded = {r["fact_text"]: r["cosine_score"] for r in unguarded}
    assert by_text_unguarded["foreign space fact that matches invoice"] > 0.0
    db.close()


def test_mixed_space_lexical_hits_still_surface(tmp_path: Path):
    """Foreign-space rows remain retrievable via FTS/importance — lexical-only."""
    db = MemoryDB(tmp_path / "lexical.sqlite")
    add_memory(db, "vendor", "payment terms are net 30")
    _make_same_dim_foreign_row(db, "invoice approval requires a PO")

    # The foreign record still surfaces lexically (cosine forced to 0).
    hits = search_memories(db, "invoice approval requires a PO", top_k=2)
    assert hits[0]["content"] == "invoice approval requires a PO"
    db.close()


def test_vector_index_stale_on_descriptor_change_at_equal_fingerprint(tmp_path: Path):
    """Switching embedders invalidates acceleration even when rows are unchanged."""
    from test_vector_index import FakeVectorIndex

    from hotmem.vector_index import rebuild_vector_index

    db = MemoryDB(tmp_path / "stale.sqlite")
    add_memory(db, "vendor", "descriptor staleness fact")
    index = FakeVectorIndex()
    rebuild_vector_index(db, index, embedding_model=EMBEDDING_MODEL, embedding_dim=64)
    # Fresh under the hash descriptor...
    assert index.is_stale(db, embedding_model=EMBEDDING_MODEL, embedding_dim=64) is False
    # ...stale under any other descriptor, at an identical store fingerprint.
    assert index.is_stale(db, embedding_model="local/semantic-fake/rev:r1/norm:l2/pp:pp1") is True
    assert index.is_stale(db, embedding_model=EMBEDDING_MODEL, embedding_dim=128) is True
    db.close()


def test_vector_index_rebuild_skips_foreign_space_rows(tmp_path: Path):
    """One index serves one embedding space; foreign rows are never indexed."""
    from test_vector_index import FakeVectorIndex

    from hotmem.vector_index import rebuild_vector_index

    db = MemoryDB(tmp_path / "rebuild.sqlite")
    add_memory(db, "vendor", "hash space fact")
    _make_same_dim_foreign_row(db, "foreign space fact")
    index = FakeVectorIndex()
    result = rebuild_vector_index(db, index, embedding_model=EMBEDDING_MODEL, embedding_dim=64)
    assert result["indexed_count"] == 1
    assert result["skipped_foreign_space"] == 1
    assert result["skipped_no_embedding"] == 0
    assert index.count() == 1
    db.close()


# ── configuration path (issue #78, C7) ───────────────────────────────────────


def test_config_resolution_default_is_hash(monkeypatch: pytest.MonkeyPatch):
    from hotmem.embed import HashEmbedder, resolve_embedder_from_config

    monkeypatch.delenv("HOTMEM_EMBEDDER", raising=False)
    monkeypatch.delenv("HOTMEM_EMBEDDER_MODEL_PATH", raising=False)
    for spec in (None, "", "hash"):
        embedder = resolve_embedder_from_config(spec)
        assert isinstance(embedder, HashEmbedder)
        assert embedder.descriptor.key == EMBEDDING_MODEL


def test_config_resolution_env_fallback(monkeypatch: pytest.MonkeyPatch):
    from hotmem.embed import HashEmbedder, resolve_embedder_from_config

    monkeypatch.setenv("HOTMEM_EMBEDDER", "hash")
    embedder = resolve_embedder_from_config(None)
    assert isinstance(embedder, HashEmbedder)
    # Explicit spec wins over the environment.
    monkeypatch.setenv("HOTMEM_EMBEDDER", "bogus")
    assert isinstance(resolve_embedder_from_config("hash"), HashEmbedder)


def test_config_resolution_rejects_unknown_with_choices():
    from hotmem.embed import resolve_embedder_from_config

    with pytest.raises(ValueError, match="hash, local-semantic"):
        resolve_embedder_from_config("openai-ada")


def test_config_resolution_semantic_requires_extra_or_model_path():
    from hotmem.embed import resolve_embedder_from_config

    # Without the extra: actionable install hint. With it installed but no
    # provisioned model path: an equally actionable no-download refusal.
    if importlib.util.find_spec("model2vec") is None:
        with pytest.raises(ValueError, match=r"\[semantic\] extra"):
            resolve_embedder_from_config("local-semantic")
    else:
        with pytest.raises(ValueError, match="never downloads"):
            resolve_embedder_from_config("local-semantic")


def test_server_embedder_injection_end_to_end(tmp_path: Path):
    """create_app(embedder=...) owns add/search/hydrate/reindex in the server."""
    from fastapi.testclient import TestClient

    from hotmem.server import create_app

    semantic = SemanticFake()
    app = create_app(db_path=tmp_path / "sem.sqlite", embedder=semantic)
    with TestClient(app) as client:
        health = client.get("/v1/health").json()
        assert health["embedding"] == {
            "model": semantic.descriptor.key,
            "dim": semantic.descriptor.dimension,
        }
        added = client.post(
            "/v1/add", json={"identifier": "vendor", "fact": "semantic server fact"}
        ).json()
        assert added["memory_id"]
        row = next(r for r in MemoryDB(tmp_path / "sem.sqlite").all_rows(include_embedding=True))
        assert row["embedding_model"] == semantic.descriptor.key
        assert row["embedding_dim"] == 128

        hits = client.post("/v1/search", json={"query": "semantic server fact", "top_k": 1}).json()
        assert hits["memories"][0]["content"] == "semantic server fact"


def test_server_default_embedder_is_hash(tmp_path: Path):
    """create_app() without an embedder keeps the exact historical behavior."""
    from fastapi.testclient import TestClient

    from hotmem.server import create_app

    app = create_app(db_path=tmp_path / "plain.sqlite")
    with TestClient(app) as client:
        body = client.get("/v1/health").json()
        assert body["embedding"] == {"model": "hotmem-hash-v1", "dim": 64}
        client.post("/v1/add", json={"identifier": "v", "fact": "plain hash fact"})
        rows = MemoryDB(tmp_path / "plain.sqlite").all_rows(include_embedding=True)
        assert rows[0]["embedding_model"] == "hotmem-hash-v1"


def test_mcp_embedder_injection(tmp_path: Path):
    """create_server(embedder=...) owns the MCP add/search/health tools."""
    pytest.importorskip("mcp", reason="requires the optional [mcp] extra")
    from hotmem.mcp_server import (
        _handle_add_memory,
        _handle_memory_health,
        _handle_search_memories,
        _ServerState,
        create_server,
    )

    semantic = SemanticFake()
    create_server(tmp_path / "mcp.sqlite", None, embedder=semantic)
    state = _ServerState()
    state.db = MemoryDB(tmp_path / "mcp.sqlite")
    state.db_path = str(tmp_path / "mcp.sqlite")
    state.swap_path = None
    state.start_time = 0.0

    payload = _handle_add_memory(state, {"identifier": "vendor", "fact": "mcp semantic fact"})
    assert not payload.isError
    row = state.db.all_rows(include_embedding=True)[0]
    assert row["embedding_model"] == semantic.descriptor.key

    health = _handle_memory_health(state, {})
    assert json.loads(health.content[0].text)["embedding"] == {
        "model": semantic.descriptor.key,
        "dim": 128,
    }
    search = _handle_search_memories(state, {"query": "mcp semantic fact", "top_k": 1})
    hits = json.loads(search.content[0].text)
    assert hits["memories"][0]["content"] == "mcp semantic fact"
    state.db.close()


def test_cli_embedder_flags_fail_fast(tmp_path: Path):
    """Invalid embedder selections exit before the server starts."""
    from click.testing import CliRunner

    from hotmem.cli import main

    runner = CliRunner()
    # Unknown choice is rejected by the CLI itself.
    result = runner.invoke(
        main, ["serve", "--db", str(tmp_path / "x.sqlite"), "--embedder", "bogus"]
    )
    assert result.exit_code != 0
    # local-semantic without a provisioned model resolves to an actionable
    # error either way: the extra is missing, or no model path was given.
    result = runner.invoke(
        main, ["serve", "--db", str(tmp_path / "x.sqlite"), "--embedder", "local-semantic"]
    )
    assert result.exit_code != 0
    message = result.output + str(result.exception or "")
    assert "[semantic]" in message or "never downloads" in message
