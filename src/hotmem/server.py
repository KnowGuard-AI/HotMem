"""HotMem server — FastAPI app with traced endpoints.

Purpose:
    HTTP sidecar serving memory operations on a single port.
    5 endpoints under /v1: health, add, search, hydrate, snapshot.
    Every response includes X-HotMem-Trace-Id header and trace_ms timing.

Interface:
    create_app(db_path, swap_path?) -> FastAPI
    The app is created with a lifespan that opens the DB and optionally hydrates.

Deps: fastapi, hotmem.db, hotmem.embed, hotmem.search, hotmem.swap, hotmem.trace
Extension: add middleware, CORS, rate limiting, or new endpoint groups here.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.search import search_memories
from hotmem.swap import compute_content_hash
from hotmem.swap import hydrate as swap_hydrate
from hotmem.swap import snapshot as swap_snapshot
from hotmem.trace import Timer, get_tracer, new_trace_id

_trace = get_tracer("server")

# ── App state (set during lifespan) ──────────────────────────────────────────

_state: dict[str, Any] = {}


# ── Request / Response models ────────────────────────────────────────────────


class AddRequest(BaseModel):
    identifier: str
    fact: str
    source: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = Field(default=None, ge=1)


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=100)
    max_chars: int | None = None


class HydrateRequest(BaseModel):
    file: str | None = None


class SnapshotRequest(BaseModel):
    file: str | None = None


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open DB, optionally hydrate from swap, yield, then close."""
    db_path = _state["db_path"]
    swap_path = _state.get("swap_path")

    db = MemoryDB(db_path)
    _state["db"] = db
    _state["start_time"] = time.time()

    # Auto-hydrate if swap file exists
    if swap_path and Path(swap_path).exists():
        result = swap_hydrate(db, swap_path)
        _trace.info(
            "startup",
            f"auto-hydrated {result.loaded} memories",
            detail={"swap_path": str(swap_path)},
        )

    _trace.info(
        "startup",
        "server ready",
        detail={"db_path": str(db_path), "port": _state.get("port", 8711)},
    )
    yield
    db.close()


# ── App factory ──────────────────────────────────────────────────────────────


def create_app(
    db_path: str | Path,
    swap_path: str | Path | None = None,
    port: int = 8711,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    _state["db_path"] = str(db_path)
    _state["swap_path"] = str(swap_path) if swap_path else None
    _state["port"] = port

    app = FastAPI(
        title="HotMem",
        description="Local-first memory sidecar for agent applications",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Trace middleware ─────────────────────────────────────────────────

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = new_trace_id()
        with Timer() as t:
            response = await call_next(request)
        response.headers["X-HotMem-Trace-Id"] = trace_id
        _trace.info(
            "request",
            f"{request.method} {request.url.path}",
            detail={"ms": round(t.ms, 2), "status": response.status_code},
            trace_id=trace_id,
        )
        return response

    # ── Endpoints ────────────────────────────────────────────────────────

    @app.get("/v1/health")
    async def health():
        db: MemoryDB = _state["db"]
        return {
            "status": "ok",
            "memory_count": db.count(),
            "db_path": _state["db_path"],
            "uptime_s": round(time.time() - _state["start_time"], 1),
        }

    @app.post("/v1/add")
    async def add_memory(req: AddRequest):
        db: MemoryDB = _state["db"]
        with Timer() as t:
            memory_id = uuid.uuid4().hex
            content_hash = compute_content_hash(req.identifier, req.fact)
            vec = embed_text(req.fact)
            blob = pack_embedding(vec)

            db.insert(
                id=memory_id,
                identifier=req.identifier,
                fact_text=req.fact,
                embedding=blob,
                embedding_dim=EMBEDDING_DIM,
                embedding_model=EMBEDDING_MODEL,
                source=req.source,
                importance=req.importance,
                metadata_json=json.dumps(req.metadata),
                content_hash=content_hash,
                ttl_seconds=req.ttl_seconds,
            )
        return {
            "memory_id": memory_id,
            "content_hash": content_hash,
            "trace_ms": round(t.ms, 2),
        }

    @app.post("/v1/search")
    async def search(req: SearchRequest):
        db: MemoryDB = _state["db"]
        with Timer() as t:
            messages = search_memories(
                db, query=req.query, top_k=req.top_k, max_chars=req.max_chars
            )
        return {
            "memories": messages,
            "count": len(messages),
            "trace_ms": round(t.ms, 2),
        }

    @app.post("/v1/hydrate")
    async def hydrate(req: HydrateRequest):
        db: MemoryDB = _state["db"]
        swap = req.file or _state.get("swap_path") or "swap.jsonl"
        result = swap_hydrate(db, swap)
        return {
            "loaded": result.loaded,
            "skipped_dupes": result.skipped_dupes,
        }

    @app.post("/v1/snapshot")
    async def snapshot(req: SnapshotRequest):
        db: MemoryDB = _state["db"]
        swap = req.file or _state.get("swap_path") or "swap.jsonl"
        result = swap_snapshot(db, swap)
        return {
            "exported": result.exported,
            "path": result.path,
        }

    return app
