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
      search_memories(db, query, top_k, max_chars?, include_archived?, vector_index?)

Deps: hotmem.db, hotmem.embed, hotmem.trace
Extension: add reranking, decay weighting, or MMR diversity here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
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
    query: str,
    query_vec: list[float],
    query_blob: bytes,
    include_archived: bool,
    vector_index: VectorIndex | None,
) -> list[dict[str, Any]]:
    """Return candidate rows with cosine scores for hybrid ranking.

    Default (no index / stale index / empty result): the deterministic
    full-table scan via ``db.search_with_cosine`` — identical to the
    pre-#49 behavior.

    Accelerated (fresh index): the index supplies oversampled cosine
    candidate ids; those ids are re-fetched and re-scored in SQLite with the
    same TTL-live/archived predicates, unioned with FTS match ids so text-only
    matches are never lost. Ranking is recomputed downstream either way, so
    both paths produce identical results.
    """
    if (
        vector_index is not None
        and not vector_index.is_stale(db)
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
            fts_rows = db.fts_search(query, include_archived=include_archived)
            candidate_ids += [r["id"] for r in fts_rows]
            # Dedupe, preserving order (index ranking first, FTS additions after).
            seen: set[str] = set()
            unique_ids = [i for i in candidate_ids if not (i in seen or seen.add(i))]
            rows = db.search_by_ids(query_blob, unique_ids, include_archived=include_archived)
            # Rows re-fetched by id already carry canonical cosine scores from
            # the SQLite UDF — the index's own scores are advisory only.
            return rows
    return db.search_with_cosine(query_blob, include_archived=include_archived)


def search_memories(
    db: MemoryDB,
    query: str,
    top_k: int = 5,
    max_chars: int | None = None,
    *,
    include_archived: bool = False,
    vector_index: VectorIndex | None = None,
) -> list[dict[str, Any]]:
    """Search memories and return ranked, LLM-ready message objects.

    Archived memories are excluded by default; pass ``include_archived=True``
    for audit/full profiles.

    When ``vector_index`` is provided and not stale, it supplies cosine
    candidates (oversampled) which are re-scored in SQLite with the identical
    hybrid formula — the response shape and ranking are byte-identical to the
    fallback. Otherwise the deterministic full-scan path runs unchanged.

    Returns:
        List of dicts with keys: role, content, memory_id, identifier, score
    """
    with Timer() as t:
        # Embed the query
        query_vec = embed_text(query)
        query_blob = pack_embedding(query_vec)

        candidates = _fetch_candidates(
            db, query, query_vec, query_blob, include_archived, vector_index
        )
        fts_scores = _normalize_bm25(db.fts_search(query, include_archived=include_archived))

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
