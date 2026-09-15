"""Bounded deterministic reranking — issue #80 acceptance tests.

Identity parity (byte-exact default), MMR determinism and duplicate
suppression, output-contract fallbacks, bounded pools, and the shared
configuration path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_MODEL, pack_embedding
from hotmem.rerank import (
    IdentityReranker,
    MMRReranker,
    SearchCandidate,
    resolve_reranker_from_config,
    validate_rerank_output,
)
from hotmem.search import search_memories
from hotmem.swap import add_memory


def _candidates(*specs: tuple[str, float, str]) -> list[SearchCandidate]:
    return [SearchCandidate(memory_id=m, score=s, content=c) for m, s, c in specs]


def _vec(text: str, dim: int = 4) -> list[float]:
    """Deterministic orthogonal-ish unit vector per text."""
    import hashlib
    import math

    seed = int.from_bytes(hashlib.md5(text.encode(), usedforsecurity=False).digest()[:4], "big")
    vec = [((seed >> (i * 3)) % 7) - 3.0 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ── identity default: byte-exact parity, zero work ──────────────────────────


def test_disabled_and_identity_are_byte_identical(tmp_path: Path):
    """#80: reranker=None and IdentityReranker produce the exact pre-#80 path."""
    db = MemoryDB(tmp_path / "p.sqlite")
    add_memory(db, "v", "invoice approval requires a PO")
    add_memory(db, "v", "payment terms are net 30")
    add_memory(db, "v", "backup rotation is weekly")

    plain = search_memories(db, "invoice approval", top_k=3)
    identity = search_memories(db, "invoice approval", top_k=3, reranker=IdentityReranker())
    assert plain == identity
    assert [m["score"] for m in plain] == [m["score"] for m in identity]
    db.close()


def test_identity_reranker_covers_top_k():
    cands = _candidates(("a", 1.0, "x"), ("b", 0.9, "y"), ("c", 0.8, "z"))
    assert IdentityReranker().rerank("q", cands, top_k=2) == ["a", "b"]
    assert IdentityReranker().rerank("q", cands, top_k=5) == ["a", "b", "c"]


# ── MMR: determinism, duplicate suppression, tie-breaks ─────────────────────


def test_mmr_suppresses_near_duplicates():
    """The gate-opening failure: near-duplicates stop consuming top-k slots."""
    cluster = [f"cache stale fact variant {i}" for i in range(4)]
    diverse = ["refund policy is 30 days", "onboarding checklist for new hires"]

    def cand(mid: str, text: str, score: float) -> tuple[str, float, str]:
        return (mid, score, text)

    cands = _candidates(
        cand("dup-1", cluster[0], 0.95),
        cand("dup-2", cluster[1], 0.94),
        cand("dup-3", cluster[2], 0.93),
        cand("div-1", diverse[0], 0.80),
        cand("div-2", diverse[1], 0.79),
    )

    def fetch(ids: list[str]) -> dict[str, bytes]:
        text_by_id = {
            "dup-1": cluster[0],
            "dup-2": cluster[1],
            "dup-3": cluster[2],
            "div-1": diverse[0],
            "div-2": diverse[1],
        }
        return {i: pack_embedding(_vec(text_by_id[i])) for i in ids}

    # Without vectors, relevance order is preserved.
    plain = MMRReranker().rerank("q", cands, top_k=3)
    assert plain[:3] == ["dup-1", "dup-2", "dup-3"]

    # Near-duplicate vectors cluster tightly; diverse docs sit orthogonal.
    dim = 8
    base = [1.0] + [0.0] * (dim - 1)  # the duplicate cluster anchor
    orthogonal = {f"div-{i}": [0.0] * dim for i in (1, 2)}
    orthogonal["div-1"][1] = 1.0
    orthogonal["div-2"][2] = 1.0

    def fetch_clustered(ids: list[str]) -> dict[str, bytes]:
        blob = {}
        for i in ids:
            vec = base if i.startswith("dup") else orthogonal[i]
            blob[i] = pack_embedding(vec)
        return blob

    ordered = MMRReranker(lambda_=0.5).rerank("q", cands, top_k=3, fetch_embeddings=fetch_clustered)
    # The first duplicate stays (top relevance); the near-duplicates are
    # demoted in favor of the orthogonal diverse pair.
    assert ordered[0] == "dup-1"
    assert set(ordered[1:3]) == {"div-1", "div-2"}


def test_mmr_missing_vectors_contribute_zero_similarity():
    """Candidates without usable vectors rank on relevance alone — no error."""
    cands = _candidates(("a", 0.9, "x"), ("b", 0.8, "y"), ("c", 0.7, "z"))
    ordered = MMRReranker().rerank("q", cands, top_k=3, fetch_embeddings=lambda ids: {})
    assert ordered == ["a", "b", "c"]
    # A fetcher returning nothing usable never raises and never reorders.
    ordered = MMRReranker().rerank("q", cands, top_k=3, fetch_embeddings=lambda ids: None)
    assert ordered == ["a", "b", "c"]


def test_mmr_deterministic_and_tie_broken_by_prerank_order():
    cands = _candidates(("m1", 0.9, "x"), ("m2", 0.9, "y"), ("m3", 0.9, "z"))
    first = MMRReranker().rerank("q", cands, top_k=3, fetch_embeddings=lambda ids: {})
    second = MMRReranker().rerank("q", cands, top_k=3, fetch_embeddings=lambda ids: {})
    assert first == second == ["m1", "m2", "m3"]  # ties: earlier pre-rerank rank


def test_mmr_pool_bounded_and_spill_preserved():
    """Candidates beyond the pool spill in unchanged order to cover top_k."""
    cands = _candidates(*[(f"m{i}", 1.0 - i / 100.0, f"t{i}") for i in range(20)])
    reranker = MMRReranker(lambda_=0.5, pool_limit=10)
    ordered = reranker.rerank("q", cands, top_k=15, fetch_embeddings=lambda ids: {})
    assert len(ordered) == 15
    assert ordered[:10] == [f"m{i}" for i in range(10)]  # constant scores: rank order
    assert ordered[10:] == [f"m{i}" for i in range(10, 15)]  # spill unchanged


def test_mmr_validation_ranges():
    with pytest.raises(ValueError, match="lambda_"):
        MMRReranker(lambda_=1.5)
    with pytest.raises(ValueError, match="pool_limit"):
        MMRReranker(pool_limit=5)
    with pytest.raises(ValueError, match="pool_limit"):
        MMRReranker(pool_limit=500)


# ── output contract: fallback, never failure ────────────────────────────────


def test_validate_rerank_output_contract():
    cands = _candidates(("a", 1.0, "x"), ("b", 0.9, "y"), ("c", 0.8, "z"))
    assert validate_rerank_output(["a", "b", "c"], cands, top_k=3)
    assert validate_rerank_output(["a", "b"], cands, top_k=2)
    assert not validate_rerank_output(["a", "a", "c"], cands, top_k=3)  # duplicates
    assert not validate_rerank_output(["a", "ghost", "c"], cands, top_k=3)  # unknown
    assert not validate_rerank_output(["a", "b"], cands, top_k=3)  # short
    assert not validate_rerank_output(["a", "b", "c", "d"], cands, top_k=3)  # long
    assert not validate_rerank_output("not-a-list", cands, top_k=3)


def test_broken_reranker_falls_back_to_first_stage(tmp_path: Path):
    """#80: an invalid or raising reranker never fails search."""
    db = MemoryDB(tmp_path / "f.sqlite")
    add_memory(db, "v", "invoice approval requires a PO")
    add_memory(db, "v", "payment terms are net 30")

    class JunkReranker:
        @property
        def descriptor(self):
            from hotmem.rerank import RerankerDescriptor

            return RerankerDescriptor(implementation="test", name="junk")

        def rerank(self, query, candidates, *, top_k, fetch_embeddings=None):
            return ["ghost-id"]

    class ExplodingReranker(JunkReranker):
        def rerank(self, query, candidates, *, top_k, fetch_embeddings=None):
            raise RuntimeError("boom")

    plain = search_memories(db, "invoice approval", top_k=2)
    junk = search_memories(db, "invoice approval", top_k=2, reranker=JunkReranker())
    boom = search_memories(db, "invoice approval", top_k=2, reranker=ExplodingReranker())
    assert junk == plain
    assert boom == plain
    db.close()


# ── configuration path ──────────────────────────────────────────────────────


def test_resolve_reranker_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HOTMEM_RERANKER", raising=False)
    assert resolve_reranker_from_config(None) is None
    assert resolve_reranker_from_config("none") is None
    mmr = resolve_reranker_from_config("mmr")
    assert isinstance(mmr, MMRReranker)
    assert mmr.descriptor.key == "hotmem/mmr-l0.5"
    monkeypatch.setenv("HOTMEM_RERANKER", "mmr")
    assert isinstance(resolve_reranker_from_config(None), MMRReranker)
    with pytest.raises(ValueError, match="none, mmr"):
        resolve_reranker_from_config("cross-encoder")


# ── batched embedding fetch ─────────────────────────────────────────────────


def test_fetch_embedding_blobs_batched_and_model_filtered(tmp_path: Path):
    db = MemoryDB(tmp_path / "b.sqlite")
    add_memory(db, "v", "hash space fact")
    rows = db.all_rows()
    foreign_id = rows[0]["id"]

    # Re-stamp one row as a foreign space with an embedding.
    db._conn.execute(
        "UPDATE memories SET embedding_model = 'foreign/v9' WHERE id = ?",
        (foreign_id,),
    )
    db._conn.commit()

    blob = db.fetch_embedding_blobs([foreign_id])
    assert foreign_id in blob  # unfiltered: legacy behavior for direct callers
    filtered = db.fetch_embedding_blobs([foreign_id], embedding_model=EMBEDDING_MODEL)
    assert filtered == {}  # mixed-space safety: foreign rows absent
    db.close()


def test_mmr_over_search_end_to_end(tmp_path: Path):
    """search_memories + MMR over a real database, default embedder."""
    db = MemoryDB(tmp_path / "e2e.sqlite")
    add_memory(db, "v", "cache stale data help")
    add_memory(db, "v", "cache stale data assistance")
    add_memory(db, "v", "refund policy is 30 days")

    plain = search_memories(db, "cache stale data help", top_k=3)
    mmr = search_memories(db, "cache stale data help", top_k=3, reranker=MMRReranker())
    assert len(mmr) == 3
    assert {m["memory_id"] for m in mmr} == {m["memory_id"] for m in plain}
    # Both orders are valid permutations of the same result set.
    db.close()
