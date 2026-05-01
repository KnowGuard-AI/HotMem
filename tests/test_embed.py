"""Tests for hotmem.embed — deterministic hash-based embedder."""

from __future__ import annotations

import math

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
