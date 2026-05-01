"""HotMem search — hybrid ranking with message-shaped output.

Purpose:
    Given a query, embed it, retrieve candidates from the DB, apply hybrid scoring
    (cosine + keyword overlap + importance), and return LLM-ready message objects.

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
W_KEYWORD = 0.2
W_IMPORTANCE = 0.2


def _keyword_overlap(query: str, text: str) -> float:
    """Compute Jaccard-like keyword overlap between query and text."""
    q_words = set(query.lower().split())
    t_words = set(text.lower().split())
    if not q_words:
        return 0.0
    overlap = q_words & t_words
    return len(overlap) / len(q_words)


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

        # Apply hybrid scoring
        scored = []
        for row in candidates:
            cosine_score = row.get("cosine_score") or 0.0
            keyword_score = _keyword_overlap(query, row["fact_text"])
            importance = row.get("importance", 0.5)

            final_score = (
                W_COSINE * cosine_score + W_KEYWORD * keyword_score + W_IMPORTANCE * importance
            )

            scored.append({**row, "final_score": final_score})

        # Sort by final score descending, take top_k
        scored.sort(key=lambda x: x["final_score"], reverse=True)
        top = scored[:top_k]

        # Build message objects
        messages = []
        char_budget = max_chars
        for item in top:
            content = item["fact_text"]
            if char_budget is not None:
                if char_budget <= 0:
                    break
                content = content[:char_budget]
                char_budget -= len(content)

            messages.append({
                "role": "system",
                "content": content,
                "memory_id": item["id"],
                "identifier": item["identifier"],
                "score": round(item["final_score"], 4),
            })

    _trace.info(
        "rank",
        f"searched {len(candidates)} memories, returned {len(messages)}",
        detail={"query_len": len(query), "top_k": top_k, "ms": round(t.ms, 2)},
    )
    return messages
