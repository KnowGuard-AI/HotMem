"""Tests for hotmem.embed — deterministic hash default + portable protocol (#78)."""

from __future__ import annotations

import hashlib
import math
import random

import pytest

from hotmem.embed import (
    DEFAULT_EMBEDDER,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    HASH_DESCRIPTOR,
    EmbeddingDescriptor,
    HashEmbedder,
    embed_text,
    pack_embedding,
    unpack_embedding,
)


def test_embedding_dimension():
    vec = embed_text("hello world")
    assert len(vec) == EMBEDDING_DIM


def test_deterministic():
    a = embed_text("same input")
    b = embed_text("same input")
    assert a == b


def test_normalized():
    vec = embed_text("some text here")
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6


def test_different_inputs_differ():
    a = embed_text("apple banana")
    b = embed_text("quantum physics")
    assert a != b


def test_similar_inputs_closer():
    a = embed_text("the quick brown fox")
    b = embed_text("the quick brown dog")
    c = embed_text("quantum entanglement theory")

    def cosine(x, y):
        dot = sum(xi * yi for xi, yi in zip(x, y, strict=True))
        return dot  # already normalized

    sim_ab = cosine(a, b)
    sim_ac = cosine(a, c)
    assert sim_ab > sim_ac


def test_pack_unpack_roundtrip():
    vec = embed_text("roundtrip test")
    blob = pack_embedding(vec)
    recovered = unpack_embedding(blob)
    for a, b in zip(vec, recovered, strict=True):
        assert abs(a - b) < 1e-6


def _reference_embed(text: str) -> list[float]:
    """The pre-#90 algorithm, verbatim — the bit-compatibility oracle."""
    vec = [0.0] * EMBEDDING_DIM
    text_lower = text.lower()
    for i in range(max(1, len(text_lower) - 2)):
        gram = text_lower[i : i + 3]
        h = int(hashlib.md5(gram.encode(), usedforsecurity=False).hexdigest(), 16)
        bucket = h % EMBEDDING_DIM
        sign = 1.0 if (h >> 64) % 2 == 0 else -1.0
        vec[bucket] += sign
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else vec


def test_embed_text_bit_exact_against_reference():
    """#90: the cached implementation must produce bit-identical vectors —
    hotmem-hash-v1 is a compatibility contract, not an implementation detail."""
    cases = [
        "",
        "a",
        "ab",
        "abc",
        "hello world",
        "Héllo Wörld — über café",
        "日本語のテキストと絵文字🎉",
        "invoice validation rules for vendor x",
        "  mixed\tCASE and   spacing  ",
    ]
    rng = random.Random(90)
    alphabet = "abcdefghijklmnopqrstuvwxyz "
    cases += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 300))) for _ in range(100)]
    unicode_alphabet = "aäöü日本語🎉éè "
    cases += [
        "".join(rng.choice(unicode_alphabet) for _ in range(rng.randint(0, 120))) for _ in range(50)
    ]
    for text in cases:
        assert embed_text(text) == _reference_embed(text), f"vector drifted for {text!r}"


def test_embed_text_trigram_cache_engages():
    """#90: repeated trigrams hit the cache instead of re-hashing md5."""
    from hotmem import embed

    text = "invoice validation rules " * 100
    embed._gram_bucket_sign.cache_clear()
    embed.embed_text(text)
    info = embed._gram_bucket_sign.cache_info()
    assert info.hits > 0
    assert info.misses < info.hits  # repeated vocabulary dominates on real text


# ── portable embedding boundary (issue #78) ────────────────────────────────


def test_hash_embedder_bit_exact_with_embed_text():
    """HashEmbedder delegates to embed_text - byte-identical output."""
    for text in ("", "a", "hello world", "invoice approval EUR 5,000"):
        assert HashEmbedder().embed(text) == embed_text(text)
        assert DEFAULT_EMBEDDER.embed(text) == embed_text(text)


def test_default_embedder_is_hash():
    assert isinstance(DEFAULT_EMBEDDER, HashEmbedder)
    assert DEFAULT_EMBEDDER.descriptor == HASH_DESCRIPTOR
    assert DEFAULT_EMBEDDER.descriptor.key == EMBEDDING_MODEL == "hotmem-hash-v1"
    assert DEFAULT_EMBEDDER.descriptor.dimension == EMBEDDING_DIM


def test_hash_descriptor_key_pinned():
    """The hash descriptor's key is exactly the legacy identifier, so stored
    records and packages are unchanged (zero-migration descriptor storage)."""
    assert HASH_DESCRIPTOR.key == "hotmem-hash-v1"
    assert (
        EmbeddingDescriptor(
            implementation="hotmem",
            model="hash-v1",
            dimension=EMBEDDING_DIM,
            normalization="l2",
            metric="cosine",
            preprocessing="trigram-hash-v1",
        )
        == HASH_DESCRIPTOR
    )


def test_descriptor_inequality_exhaustive():
    """Equal descriptors only - every field participates in the decision."""
    base = dict(
        implementation="local",
        model="mini",
        dimension=384,
        revision="r1",
        normalization="l2",
        metric="cosine",
        preprocessing="pp1",
    )
    reference = EmbeddingDescriptor(**base)
    assert reference == EmbeddingDescriptor(**base)
    for override in (
        {"implementation": "other"},
        {"model": "other"},
        {"dimension": 128},
        {"revision": "r2"},
        {"revision": ""},
        {"normalization": "none"},
        {"preprocessing": "pp2"},
        {"preprocessing": ""},
    ):
        assert reference != EmbeddingDescriptor(**{**base, **override})
    # Equal dimensions never make different models compatible.
    assert reference != EmbeddingDescriptor(**{**base, "model": "other"})


def test_descriptor_key_deterministic_and_distinct():
    local = EmbeddingDescriptor(
        implementation="local", model="mini", dimension=384, preprocessing="pp1"
    )
    assert local.key == "local/mini/norm:l2/pp:pp1"
    revised = EmbeddingDescriptor(
        implementation="local",
        model="mini",
        dimension=384,
        revision="r1",
        normalization="none",
        preprocessing="pp1",
    )
    assert revised.key == "local/mini/rev:r1/norm:none/pp:pp1"
    assert local.key != revised.key
    # Every descriptor field change changes the key.
    assert (
        local.key
        != EmbeddingDescriptor(
            implementation="local", model="mini", dimension=384, preprocessing="pp2"
        ).key
    )


def test_descriptor_validation_rejects_invalid():
    with pytest.raises(ValueError, match="implementation"):
        EmbeddingDescriptor(implementation="", model="m", dimension=8)
    with pytest.raises(ValueError, match="model"):
        EmbeddingDescriptor(implementation="a", model="x/y", dimension=8)
    with pytest.raises(ValueError, match="revision"):
        EmbeddingDescriptor(implementation="a", model="m", dimension=8, revision="r@1")
    with pytest.raises(ValueError, match="dimension"):
        EmbeddingDescriptor(implementation="a", model="m", dimension=0)
    with pytest.raises(ValueError, match="normalization"):
        EmbeddingDescriptor(implementation="a", model="m", dimension=8, normalization="l1")
    with pytest.raises(ValueError, match="metric"):
        EmbeddingDescriptor(implementation="a", model="m", dimension=8, metric="dot")


def test_cosine_is_the_one_canonical_definition():
    """Cosine semantics shared by the SQL UDF and the reranker (#80 review)."""
    from hotmem.embed import cosine

    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine([2.0, 0.0], [3.0, 0.0]) == pytest.approx(1.0)  # scale-invariant
    # Degenerate inputs score exactly 0.0, never raise or divide.
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # zero norm
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0  # length mismatch
    assert cosine([1.0], []) == 0.0


def test_two_hash_instances_are_isolated_but_equal():
    """Two runtime-owned instances coexist safely - no mutable global state."""
    first, second = HashEmbedder(), HashEmbedder()
    assert first is not second
    assert first.descriptor == second.descriptor == DEFAULT_EMBEDDER.descriptor
    assert first.embed("isolation probe") == second.embed("isolation probe")
