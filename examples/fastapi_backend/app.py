"""FastAPI app using HotMem as an in-process library (no sidecar).

Endpoints:
    POST /remember  {identifier, fact}        -> {memory_id}
    POST /ask       {query, top_k?}            -> {answer, memories}
    GET  /health                              -> {memory_count}

Run: uvicorn app:app --port 8000
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from hotmem.db import MemoryDB
from hotmem.search import search_memories
from hotmem.swap import add_memory

# Private temp dir for the demo DB (atomic creation, no mktemp race).
_TMP_DIR = tempfile.mkdtemp(prefix="hotmem_fastapi_")
DB_PATH = os.path.join(_TMP_DIR, "hotmem.sqlite")
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
    memory_id, content_hash = add_memory(
        _db,
        req.identifier,
        req.fact,
        source="fastapi-example",
        importance=req.importance,
    )
    return {"memory_id": memory_id, "content_hash": content_hash}


@app.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    memories = search_memories(_db, query=req.query, top_k=req.top_k)
    return {
        "memories": memories,
        "answer": "\n".join(f"- {m['content']}" for m in memories) or "(no memories)",
    }
