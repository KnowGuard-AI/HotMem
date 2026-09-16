"""HotMem search — hybrid ranking with message-shaped output.

Purpose:
     Given a query, embed it, retrieve candidates from the DB, apply hybrid scoring
     (cosine + FTS5 BM25 + importance), and return LLM-ready message objects.

     File-backed memories with a summary are searched by their summary; those
     without a summary have NULL embeddings (cosine 0) and NULL fact_text, so
     they are skipped (no searchable text). The /v1/search response shape is
     unchanged: each result carries role/content/memory_id/identifier/score.

     An optional derived vector index (#49) may supply cosine CANDIDATES; the
     same hybrid scorer always recomputes final ranking from SQLite rows, so
     results are identical with or without acceleration. When the index is
     absent or stale the deterministic full-scan path is used — the index is
     never canonical.

Interface:
      search_memories(db, query, top_k, max_chars?, include_archived?, vector_index?, embedder?)

Deps: hotmem.db, hotmem.embed, hotmem.trace
Extension: add reranking, decay weighting, or MMR diversity here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hotmem.db import MemoryDB
from hotmem.embed import DEFAULT_EMBEDDER, Embedder, EmbeddingDescriptor, pack_embedding
from hotmem.rerank import IdentityReranker, Reranker, SearchCandidate, validate_rerank_output
from hotmem.trace import Timer, get_tracer

if TYPE_CHECKING:
    from hotmem.vector_index import VectorIndex

_trace = get_tracer("search")

# Scoring weights
W_COSINE = 0.6
W_FTS = 0.2
W_IMPORTANCE = 0.2


def _normalize_bm25(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Convert raw BM25 scores into 0..1 scores where 1.0 is best."""
    if not rows:
        return {}

    scores = [float(row["bm25_score"]) for row in rows]
    best = min(scores)
    worst = max(scores)
    if best == worst:
        return {row["id"]: 1.0 for row in rows}

    return {row["id"]: 1.0 - ((float(row["bm25_score"]) - best) / (worst - best)) for row in rows}


def _search_text(row: dict[str, Any]) -> str:
    """Return the text to rank against: fact_text (inline) or fact_summary (file-backed)."""
    text = row.get("fact_text")
    if text:  # truthy: catches None and "" (file-backed uses empty fact_text)
        return text
    return row.get("fact_summary") or ""


def _fetch_candidates(
    db: MemoryDB,
    query_vec: list[float],
    query_blob: bytes,
    fts_rows: list[dict[str, Any]],
    include_archived: bool,
    vector_index: VectorIndex | None,
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
) -> list[dict[str, Any]]:
    """Return candidate rows with cosine scores for hybrid ranking.

    Default (no index / stale index / empty result): the deterministic
    full-table scan via ``db.search_with_cosine`` — identical to the
    pre-#49 behavior.

    Accelerated (fresh index): the index supplies oversampled cosine
    candidate ids; those ids are re-fetched and re-scored in SQLite with the
    same TTL-live/archived predicates, unioned with the FTS match ids
    (already fetched once by the caller) so text-only matches are never
    lost. Ranking is recomputed downstream either way, so both paths produce
    identical results.

    Mixed-space safety (issue #78): ``embedding_model``/``embedding_dim``
    describe the query's embedding space. The SQL candidate fetch filters
    rows stored under other descriptors out of cosine scoring (they still
    surface via FTS/importance), and an index whose rebuild marker was
    stamped under a different descriptor reads stale — switching models
    never silently serves foreign-space candidates.
    """
    if (
        vector_index is not None
        and not vector_index.is_stale(
            db, embedding_model=embedding_model, embedding_dim=embedding_dim
        )
        # Rows with searchable text but no embedding rank via importance in
        # the full scan but can never be vector candidates — use the exact
        # scan while any exist so ranking parity is preserved.
        and not db.has_unindexed_text_rows()
    ):
        try:
            hits = vector_index.search(query_vec, top_k=vector_index.oversample())
        except Exception:  # index is disposable; never let it fail search
            _trace.warn("candidates", "vector index search failed; falling back to scan")
            hits = []
        if hits:
            candidate_ids = [h["id"] for h in hits]
            candidate_ids += [r["id"] for r in fts_rows]
            # Dedupe, preserving order (index ranking first, FTS additions after).
            seen: set[str] = set()
            unique_ids = [i for i in candidate_ids if not (i in seen or seen.add(i))]
            rows = db.search_by_ids(
                query_blob,
                unique_ids,
                include_archived=include_archived,
                embedding_model=embedding_model,
            )
            # Rows re-fetched by id already carry canonical cosine scores from
            # the SQLite UDF — the index's own scores are advisory only.
            return rows
    return db.search_with_cosine(
        query_blob, include_archived=include_archived, embedding_model=embedding_model
    )


def _apply_reranker(
    db: MemoryDB,
    reranker: Reranker,
    query: str,
    scored: list[dict[str, Any]],
    top_k: int,
    *,
    active_descriptor: EmbeddingDescriptor,
) -> list[dict[str, Any]]:
    """Run the bounded second stage over the first-stage ranking (#80).

    The reranker sees the narrow candidate view only; its vectors come
    from ONE batched ``db.fetch_embedding_blobs`` call restricted to the
    active embedding space (foreign or missing vectors read as absent and
    contribute zero similarity). Only the head the reranker can consume is
    projected — ``max(reranker.pool, top_k)`` rows — so opt-in reranking
    never allocates per-query structures proportional to the whole store.
    Any failure or contract violation falls back to the first-stage order —
    search never fails because of a reranker.
    """
    window = scored[: max(reranker.pool, top_k)]
    candidates = [
        SearchCandidate(memory_id=row["id"], score=row["final_score"], content=row["_search_text"])
        for row in window
    ]

    def fetch(memory_ids: list[str]) -> dict[str, bytes]:
        return db.fetch_embedding_blobs(memory_ids, embedding_model=active_descriptor.key)

    try:
        ordered = reranker.rerank(query, candidates, top_k=top_k, fetch_embeddings=fetch)
        if not validate_rerank_output(ordered, candidates, top_k=top_k):
            _trace.warn(
                "rerank",
                "invalid reranker output; falling back to first-stage order",
                detail={"count": len(ordered) if isinstance(ordered, list) else -1},
            )
            return scored
    except Exception as err:
        _trace.warn(
            "rerank",
            "reranker raised; falling back to first-stage order",
            detail={"error": type(err).__name__},
        )
        return scored
    by_id = {row["id"]: row for row in window}
    return [by_id[memory_id] for memory_id in ordered]


def search_memories(
    db: MemoryDB,
    query: str,
    top_k: int = 5,
    max_chars: int | None = None,
    *,
    include_archived: bool = False,
    vector_index: VectorIndex | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> list[dict[str, Any]]:
    """Search memories and return ranked, LLM-ready message objects.

    Archived memories are excluded by default; pass ``include_archived=True``
    for audit/full profiles.

    When ``vector_index`` is provided and not stale, it supplies cosine
    candidates (oversampled) which are re-scored in SQLite with the identical
    hybrid formula — the response shape and ranking are byte-identical to the
    fallback. Otherwise the deterministic full-scan path runs unchanged.

    ``embedder`` owns the query embedding (issue #78): ``None`` means the
    hash default. Pass the same runtime-owned embedder used for writes; rows
    stored under a different descriptor score zero cosine (mixed-space
    guard) and still surface via FTS/importance.

    ``reranker`` is the optional bounded second stage (issue #80):
    ``None`` (and ``IdentityReranker``) preserve the exact first-stage order
    and response shape with zero extra work. A reranker violating its output
    contract falls back to the first-stage top-k with a trace diagnostic.

    Returns:
        List of dicts with keys: role, content, memory_id, identifier, score
    """
    active = embedder if embedder is not None else DEFAULT_EMBEDDER
    with Timer() as t:
        # Embed the query under the active runtime embedder
        query_vec = active.embed(query)
        query_blob = pack_embedding(query_vec)

        # One FTS pass serves both candidate unioning and BM25 scoring (#92).
        fts_rows = db.fts_search(query, include_archived=include_archived)
        candidates = _fetch_candidates(
            db,
            query_vec,
            query_blob,
            fts_rows,
            include_archived,
            vector_index,
            embedding_model=active.descriptor.key,
            embedding_dim=active.descriptor.dimension,
        )
        fts_scores = _normalize_bm25(fts_rows)

        # Apply hybrid scoring
        scored = []
        for row in candidates:
            text = _search_text(row)
            # Skip memories with no searchable text (file-backed without summary).
            if not text:
                continue
            cosine_score = row.get("cosine_score") or 0.0
            fts_score = fts_scores.get(row["id"], 0.0)
            importance = row.get("importance", 0.5)

            final_score = W_COSINE * cosine_score + W_FTS * fts_score + W_IMPORTANCE * importance

            scored.append({**row, "final_score": final_score, "_search_text": text})

        # Sort by final score descending, take top_k
        scored.sort(key=lambda x: x["final_score"], reverse=True)

        # Optional bounded second-stage selection (#80): disabled/identity is
        # the zero-work fast path — the pre-#80 order and response shape are
        # exactly preserved. Truncation to top_k happens AFTER selection.
        if reranker is not None and not isinstance(reranker, IdentityReranker):
            scored = _apply_reranker(
                db, reranker, query, scored, top_k, active_descriptor=active.descriptor
            )
        top = scored[:top_k]

        # Build message objects
        messages = []
        char_budget = max_chars
        for item in top:
            content = item["_search_text"]
            if char_budget is not None:
                if char_budget <= 0:
                    break
                content = content[:char_budget]
                char_budget -= len(content)

            messages.append(
                {
                    "role": "system",
                    "content": content,
                    "memory_id": item["id"],
                    "identifier": item["identifier"],
                    "score": round(item["final_score"], 4),
                    "created_at": item.get("created_at"),
                }
            )

    _trace.info(
        "rank",
        f"searched {len(candidates)} memories, returned {len(messages)}",
        detail={"query_len": len(query), "top_k": top_k, "ms": round(t.ms, 2)},
    )
    return messages
