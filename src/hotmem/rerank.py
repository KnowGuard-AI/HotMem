"""HotMem reranking — bounded, deterministic second-stage selection (issue #80).

Purpose:
    An optional reranking seam over the first-stage hybrid ranking. The
    default is no reranking at all: ``IdentityReranker`` (and ``None``)
    preserve the exact current order and response shape with zero extra
    work. One deterministic strategy ships: MMR, which trades a fraction of
    first-stage relevance for diversity so near-duplicates stop consuming
    the top-k (the gate-opening failure in
    ``bench/retrieval/post-p2-gate-80.md``).

Design rules (#80):
    - Bounded: a candidate pool cap (default 50, documented safe range
      10..200); work is batched — no per-candidate queries, no second
      corpus pass.
    - Deterministic: lambda and score scaling are defined unambiguously,
      ties break by pre-rerank order then memory id; missing or
      incompatible candidate embeddings contribute exactly zero
      similarity, never an error.
    - Disposable: an invalid reranker result (duplicate/unknown ids,
      cardinality or bound violations) falls back to the pre-rerank top-k
      with a trace diagnostic — search never fails because of a reranker.
    - Non-canonical: reranker selection never enters canonical records,
      snapshots, or sync identity; it is runtime configuration like the
      embedder.

Interface:
    Reranker (Protocol) / RerankerDescriptor
    SearchCandidate — the narrow candidate view (id, score, content)
    IdentityReranker — the zero-work default
    MMRReranker — deterministic maximal marginal relevance
    resolve_reranker_from_config — the shared CLI/server config path

Deps: hotmem.embed (unpack only), hotmem.trace
Extension: hosted rerankers implement the protocol behind the same seam;
    cross-encoders and RRF remain out of scope (canon).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Protocol

from hotmem.trace import get_tracer

_trace = get_tracer("rerank")

DEFAULT_LAMBDA = 0.5
DEFAULT_POOL_LIMIT = 50
_POOL_RANGE = (10, 200)
_LAMBDA_RANGE = (0.0, 1.0)


@dataclass(frozen=True)
class RerankerDescriptor:
    """Identity of a reranker for observability (never canonical identity)."""

    implementation: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.implementation}/{self.name}"


@dataclass(frozen=True)
class SearchCandidate:
    """The narrow reranker view of a first-stage result.

    Rerankers see the memory id, the first-stage relevance score, and the
    display content — nothing else. They never see metadata, sources, or
    storage internals (issue #80: minimal surface, no prompt-injection
    fan-out).
    """

    memory_id: str
    score: float
    content: str


class CandidateEmbeddingFetch(Protocol):
    """Batched embedding access: one call for the whole candidate pool."""

    def __call__(self, memory_ids: list[str]) -> dict[str, bytes]: ...


class Reranker(Protocol):
    """Deterministic second-stage selection over first-stage candidates."""

    @property
    def descriptor(self) -> RerankerDescriptor: ...

    def rerank(
        self,
        query: str,
        candidates: list[SearchCandidate],
        *,
        top_k: int,
        fetch_embeddings: CandidateEmbeddingFetch | None = None,
    ) -> list[str]:
        """Return ordered memory ids covering ``min(top_k, len(candidates))``.

        Contract: unique ids, all drawn from ``candidates``; violating the
        contract lets the caller fall back to the pre-rerank top-k.
        ``fetch_embeddings`` (when provided) fetches candidate vectors in
        ONE batched call; rerankers must not query per candidate.
        """
        ...


@dataclass(frozen=True)
class IdentityReranker:
    """The zero-work default: first-stage order, exactly.

    Search treats ``None`` and ``IdentityReranker`` identically and skips
    the rerank stage entirely — byte-exact parity with the pre-#80 path.
    """

    @property
    def descriptor(self) -> RerankerDescriptor:
        return _IDENTITY_DESCRIPTOR

    def rerank(
        self,
        query: str,
        candidates: list[SearchCandidate],
        *,
        top_k: int,
        fetch_embeddings: CandidateEmbeddingFetch | None = None,
    ) -> list[str]:
        return [c.memory_id for c in candidates[: max(0, top_k)]]


_IDENTITY_DESCRIPTOR = RerankerDescriptor(implementation="hotmem", name="identity")


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass(frozen=True)
class MMRReranker:
    """Deterministic maximal marginal relevance (issue #80).

    Selection score for candidate ``i`` given already-selected ``S``:

        ``lambda * rel[i] - (1 - lambda) * max_{j in S} cos(v_i, v_j)``

    The default ``lambda`` (0.5) is evidence-driven, not aesthetic: on the
    committed #80 gate fixtures, relevance-dominant settings (>= 0.55) leave
    the near-duplicate clusters in place (diversity duplicate-slot 0.400),
    while 0.5 crosses the 20% gate (0.167). The measured recall allowance at
    0.5 is documented in ``bench/retrieval/rerank-mmr.json`` — similarity
    alone cannot distinguish graded-relevant revision/paraphrase pairs from
    duplicate clusters, and metadata-driven demotion is out of scope by
    canon. Reranking stays opt-in; the default is off.

    - ``rel`` is the first-stage score min-max normalized within the pool
      (a constant pool scores 1.0 for all — equal relevance, diversity
      decides).
    - Vectors come from ONE batched fetch; candidates whose vectors are
      missing or incompatible contribute exactly zero similarity — they
      rank on relevance alone, deterministically, never an error.
    - Ties break by pre-rerank order (earlier first), then memory id —
      the output is a pure function of the inputs.
    - The pool is capped at ``pool_limit`` (default 50, safe range
      10..200); candidates beyond the pool spill in unchanged order, so
      ``min(top_k, len(candidates))`` is always covered.
    """

    lambda_: float = DEFAULT_LAMBDA
    pool_limit: int = DEFAULT_POOL_LIMIT

    def __post_init__(self) -> None:
        if not (_LAMBDA_RANGE[0] <= self.lambda_ <= _LAMBDA_RANGE[1]):
            raise ValueError(
                f"MMRReranker.lambda_ must be within {_LAMBDA_RANGE} (got {self.lambda_})"
            )
        if not (_POOL_RANGE[0] <= self.pool_limit <= _POOL_RANGE[1]):
            raise ValueError(
                f"MMRReranker.pool_limit must be within {_POOL_RANGE} (got {self.pool_limit})"
            )

    @property
    def descriptor(self) -> RerankerDescriptor:
        return RerankerDescriptor(implementation="hotmem", name=f"mmr-l{self.lambda_}")

    def rerank(
        self,
        query: str,
        candidates: list[SearchCandidate],
        *,
        top_k: int,
        fetch_embeddings: CandidateEmbeddingFetch | None = None,
    ) -> list[str]:
        if not candidates:
            return []
        target = min(max(0, top_k), len(candidates))
        if target == 0:
            return []

        pool_size = min(self.pool_limit, len(candidates))
        pool = candidates[:pool_size]

        # Min-max relevance within the pool; constant pools score 1.0.
        scores = [c.score for c in pool]
        lo, hi = min(scores), max(scores)
        rel = [1.0 if hi == lo else (s - lo) / (hi - lo) for s in scores]

        vectors: dict[str, list[float] | None] = {}
        if fetch_embeddings is not None and pool:
            blobs = fetch_embeddings([c.memory_id for c in pool]) or {}
            for c in pool:
                blob = blobs.get(c.memory_id)
                if blob:
                    count = len(blob) // 4
                    vectors[c.memory_id] = list(struct.unpack(f"{count}f", blob))
                else:
                    vectors[c.memory_id] = None

        lam = self.lambda_
        order = {c.memory_id: idx for idx, c in enumerate(pool)}
        selected: list[SearchCandidate] = []
        selected_vecs: list[list[float] | None] = []
        remaining = list(pool)

        while remaining and len(selected) < target:
            best = None
            best_key = None
            for cand in remaining:
                vec = vectors.get(cand.memory_id)
                max_sim = 0.0
                if vec is not None:
                    for sv in selected_vecs:
                        if sv is not None:
                            sim = _cosine(vec, sv)
                            if sim > max_sim:
                                max_sim = sim
                mmr = lam * rel[order[cand.memory_id]] - (1.0 - lam) * max_sim
                # Tie-break: higher MMR first; then earlier pre-rerank rank;
                # then memory id (deterministic total order).
                key = (-mmr, order[cand.memory_id], cand.memory_id)
                if best_key is None or key < best_key:
                    best_key = key
                    best = cand
            selected.append(best)
            selected_vecs.append(vectors.get(best.memory_id))
            remaining.remove(best)

        ordered = [c.memory_id for c in selected]
        if len(ordered) < target:
            pool_ids = {c.memory_id for c in pool}
            spilled = [c.memory_id for c in candidates if c.memory_id not in pool_ids]
            ordered.extend(spilled[: target - len(ordered)])
        return ordered


_RERANKER_CHOICES = ("none", "mmr")


def resolve_reranker_from_config(
    spec: str | None = None,
    *,
    lambda_: float = DEFAULT_LAMBDA,
    pool_limit: int = DEFAULT_POOL_LIMIT,
) -> Reranker | None:
    """Resolve the runtime reranker from the shared configuration path.

    ``"none"`` (the default) disables reranking entirely — the exact
    pre-#80 search path. ``"mmr"`` selects the deterministic MMR strategy.
    Flags take precedence over ``HOTMEM_RERANKER``; resolution happens
    before serving so invalid selections fail fast (issue #80).
    """
    name = (spec or os.environ.get("HOTMEM_RERANKER") or "none").strip().lower()
    if name in ("", "none"):
        return None
    if name == "mmr":
        return MMRReranker(lambda_=lambda_, pool_limit=pool_limit)
    raise ValueError(f"unknown reranker {name!r}; expected one of: {', '.join(_RERANKER_CHOICES)}")


def validate_rerank_output(
    ordered_ids: list[str],
    candidates: list[SearchCandidate],
    *,
    top_k: int,
) -> bool:
    """Contract check for reranker output (#80): unique, known, bounded.

    ``True`` iff the ids are unique, all drawn from the candidates, and
    cover ``min(top_k, len(candidates))`` results — the caller falls back
    to the pre-rerank top-k otherwise.
    """
    if not isinstance(ordered_ids, list):
        return False
    expected = min(max(0, top_k), len(candidates))
    if len(ordered_ids) != expected:
        return False
    if len(set(ordered_ids)) != len(ordered_ids):
        return False
    known = {c.memory_id for c in candidates}
    return all(memory_id in known for memory_id in ordered_ids)
