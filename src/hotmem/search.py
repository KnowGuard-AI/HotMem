"""HotMem search — hybrid ranking with message-shaped output.

Purpose:
     Given a query, embed it, retrieve candidates from the DB, apply hybrid scoring
     (cosine + FTS5 BM25 + importance), and return LLM-ready message objects.

     File-backed memories with a summary are searched by their summary; those
     without a summary have NULL embeddings (cosine 0) and NULL fact_text, so
     they are skipped (no searchable text). The /v1/search response shape is
     unchanged: each result carries role/content/memory_id/identifier/score.

Interface:
     search_memories(db, query, top_k, max_chars?) -> list[MessageObject]

Deps: hotmem.db, hotmem.embed, hotmem.trace
Extension: add reranking, decay weighting, or MMR diversity here.
"""

from __future__ import annotations

from typing import Any

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.trace import Timer, get_tracer

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


def search_memories(
    db: MemoryDB,
    query: str,
    top_k: int = 5,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Search memories and return ranked, LLM-ready message objects.

    Returns:
        List of dicts with keys: role, content, memory_id, identifier, score
    """
    with Timer() as t:
        # Embed the query
        query_vec = embed_text(query)
        query_blob = pack_embedding(query_vec)

        # Get all candidates with cosine scores from DB
        candidates = db.search_with_cosine(query_blob)
        fts_scores = _normalize_bm25(db.fts_search(query))

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
