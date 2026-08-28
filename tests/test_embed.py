"""Tests for hotmem.embed — deterministic hash-based embedder."""

from __future__ import annotations

import hashlib
import math
import random

from hotmem.embed import EMBEDDING_DIM, embed_text, pack_embedding, unpack_embedding


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
        "".join(rng.choice(unicode_alphabet) for _ in range(rng.randint(0, 120)))
        for _ in range(50)
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
