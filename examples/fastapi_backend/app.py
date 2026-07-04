"""FastAPI app using HotMem as an in-process library (no sidecar).

Endpoints:
    POST /remember  {identifier, fact}        -> {memory_id}
    POST /ask       {query, top_k?}            -> {answer, memories}
    GET  /health                              -> {memory_count}

Run: uvicorn app:app --port 8000
"""

from __future__ import annotations

import tempfile
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.search import search_memories
from hotmem.swap import compute_content_hash

DB_PATH = tempfile.mktemp(suffix=".sqlite", prefix="hotmem_fastapi_")
_db = MemoryDB(DB_PATH)

app = FastAPI(title="hotmem-fastapi-example")


class RememberRequest(BaseModel):
    identifier: str
    fact: str
    importance: float = 0.5


class AskRequest(BaseModel):
    query: str
    top_k: int = 5


@app.on_event("shutdown")
def _shutdown() -> None:
    _db.close()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"memory_count": _db.count()}


@app.post("/remember")
def remember(req: RememberRequest) -> dict[str, Any]:
    import uuid

    blob = pack_embedding(embed_text(req.fact))
    content_hash = compute_content_hash(req.identifier, req.fact)
    memory_id = uuid.uuid4().hex
    _db.insert(
        id=memory_id,
        identifier=req.identifier,
        fact_text=req.fact,
        embedding=blob,
        importance=req.importance,
        content_hash=content_hash,
    )
    return {"memory_id": memory_id}


@app.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    memories = search_memories(_db, query=req.query, top_k=req.top_k)
    return {
        "memories": memories,
        "answer": "\n".join(f"- {m['content']}" for m in memories) or "(no memories)",
    }
