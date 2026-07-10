"""HotMem server - FastAPI app with traced endpoints.

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
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.memory import FileRef, add_file_backed, get_memory_metadata, hydrate_memory
from hotmem.profiles import HydrationProfile, hydrate_with_profile
from hotmem.provenance import ProvenanceError
from hotmem.search import search_memories
from hotmem.snapshot import SnapshotChecksumError
from hotmem.snapshot import hydrate as snapshot_hydrate
from hotmem.snapshot import snapshot as snapshot_write
from hotmem.storage import UnsupportedSchemeError
from hotmem.swap import compute_content_hash
from hotmem.trace import Timer, get_tracer, new_trace_id

_trace = get_tracer("server")

try:
    _VERSION = pkg_version("hotmem")
except PackageNotFoundError:
    try:
        from hotmem import __version__ as _VERSION
    except ImportError:
        _VERSION = "0.0.0+unknown"


def _safe_json(value: str | None, default: Any) -> Any:
    """Decode a JSON column defensively; return ``default`` on failure/None."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


# ── App state (set during lifespan) ──────────────────────────────────────────

_state: dict[str, Any] = {}


# ── Request / Response models ────────────────────────────────────────────────


class AddRequest(BaseModel):
    """Add an inline fact OR a file reference.

    Exactly one of ``fact`` (inline) or ``file_uri`` (file-backed) must be
    provided. Existing inline payloads using ``identifier`` + ``fact`` are
    unchanged. When ``file_uri`` is present, the memory stores a reference
    to the byte range — zero bytes copied into SQLite.
    """

    identifier: str
    fact: str | None = None
    file_uri: str | None = Field(
        default=None, description="file://, absolute, or relative path to backing file"
    )
    byte_offset: int | None = Field(default=None, ge=0)
    byte_length: int | None = Field(default=None, ge=0)
    source_format: str | None = Field(default=None, description="e.g. csv, jsonl, parquet, bin")
    source_checksum: str | None = Field(
        default=None,
        description="SHA-256 of the byte range; optional (unverified if omitted)",
    )
    summary: str | None = Field(
        default=None,
        description="Optional short summary for a file-backed memory (makes it searchable)",
    )
    source: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> AddRequest:
        has_fact = self.fact is not None
        has_file = self.file_uri is not None
        if has_fact == has_file:
            raise ValueError("provide exactly one of 'fact' or 'file_uri'")
        if has_file and (self.byte_offset is None or self.byte_length is None):
            raise ValueError("'byte_offset' and 'byte_length' are required when 'file_uri' is set")
        return self


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=100)
    max_chars: int | None = None


class HydrateRequest(BaseModel):
    """Import memories from a snapshot path (v2 directory or legacy JSONL)."""

    model_config = {"populate_by_name": True}

    path: str | None = Field(default=None, description="Snapshot directory or JSONL file")
    file: str | None = Field(
        default=None, description="Deprecated alias for 'path'", deprecated=True
    )


class SnapshotRequest(BaseModel):
    """Export memories to a snapshot path (v2 directory or legacy JSONL)."""

    model_config = {"populate_by_name": True}

    path: str | None = Field(default=None, description="Snapshot directory or JSONL file")
    file: str | None = Field(
        default=None, description="Deprecated alias for 'path'", deprecated=True
    )
    copy_attachments: bool = Field(
        default=False,
        description="Copy small file-backed byte ranges into attachments/ (v2 only)",
    )


class HydrateOneRequest(BaseModel):
    """Optional body for POST /v1/memory/{id}/hydrate — profile + verify params.

    When absent (no body), the endpoint returns raw bytes (backward-compatible).
    When present, returns a JSON ProfiledHydration dict.
    """

    profile: HydrationProfile = Field(default="agent", description="agent|compact|audit|full")
    verify: bool = Field(default=True, description="Verify source_checksum if present")


class HydrateBatchRequest(BaseModel):
    """Body for POST /v1/memory/hydrate-batch — batch profiled hydration."""

    memory_ids: list[str] = Field(..., min_length=1)
    profile: HydrationProfile = Field(default="agent", description="agent|compact|audit|full")
    verify: bool = Field(default=True, description="Verify source_checksum if present")


class DiscoverRequest(BaseModel):
    """Body for POST /v1/discover — trigger bundle discovery."""

    root: str = Field(..., description="Root directory to scan for bundles")
    max_depth: int = Field(default=10, ge=1, le=50, description="Max directory depth")


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
        result = snapshot_hydrate(db, swap_path)
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
    base_dir: str | Path | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    ``base_dir`` resolves relative file_ref URIs. Defaults to the parent
    directory of ``db_path`` (i.e. the mount dir).
    """
    _state["db_path"] = str(db_path)
    _state["swap_path"] = str(swap_path) if swap_path else None
    _state["port"] = port
    if base_dir is None:
        base_dir = str(Path(db_path).resolve().parent)
    _state["base_dir"] = str(base_dir)

    app = FastAPI(
        title="HotMem",
        description="Local-first memory sidecar for agent applications",
        version=_VERSION,
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
        base_dir: str = _state["base_dir"]
        with Timer() as t:
            if req.file_uri is not None:
                # File-backed path: store a reference, zero bytes copied.
                ref = FileRef(
                    source_uri=req.file_uri,
                    byte_offset=req.byte_offset or 0,
                    byte_length=req.byte_length or 0,
                    source_format=req.source_format or "",
                    source_checksum=req.source_checksum,
                )
                try:
                    memory_id, content_hash = add_file_backed(
                        db,
                        identifier=req.identifier,
                        file_ref=ref,
                        base_dir=base_dir,
                        summary=req.summary,
                        importance=req.importance,
                        metadata=req.metadata,
                        source=req.source,
                    )
                except UnsupportedSchemeError as err:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "unsupported_scheme",
                            "scheme": err.args[0] if err.args else "unknown",
                            "message": (
                                "only local schemes are supported (file://, "
                                "absolute, relative); remote schemes remain EMOS-owned"
                            ),
                        },
                    )
                except FileNotFoundError as err:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "backing_file_not_found",
                            "source_uri": req.file_uri,
                            "message": str(err),
                        },
                    )
            else:
                # Inline path (unchanged from v1).
                memory_id = uuid.uuid4().hex
                content_hash = compute_content_hash(req.identifier, req.fact or "")
                vec = embed_text(req.fact or "")
                blob = pack_embedding(vec)

                db.insert(
                    id=memory_id,
                    identifier=req.identifier,
                    fact_text=req.fact or "",
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

    @app.get("/v1/memories")
    async def list_memories(
        identifier: str,
        order: str = "asc",
        limit: int = 100,
    ):
        """Return memories for an identifier in created_at order."""
        db: MemoryDB = _state["db"]
        if order not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be 1..1000")
        with Timer() as t:
            rows = db.list_by_identifier(identifier, order=order, limit=limit)
        return {
            "memories": rows,
            "count": len(rows),
            "trace_ms": round(t.ms, 2),
        }

    @app.get("/v1/memory/{memory_id}")
    async def get_memory(memory_id: str):
        """Return memory metadata WITHOUT touching the backing file (lazy)."""
        db: MemoryDB = _state["db"]
        record = get_memory_metadata(db, memory_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "memory_id": memory_id},
            )
        meta = {
            "id": record["id"],
            "identifier": record["identifier"],
            "memory_type": record["memory_type"],
            "fact_text": record["fact_text"],
            "fact_summary": record.get("fact_summary"),
            "embedding_dim": record["embedding_dim"],
            "embedding_model": record["embedding_model"],
            "source": record["source"],
            "importance": record["importance"],
            "metadata": _safe_json(record["metadata_json"], {}),
            "content_hash": record["content_hash"],
            "source_uri": record["source_uri"],
            "byte_offset": record["byte_offset"],
            "byte_length": record["byte_length"],
            "source_checksum": record["source_checksum"],
            "source_format": record["source_format"],
            "provenance": _safe_json(record.get("provenance_json"), None),
            "created_at": record["created_at"],
        }
        return meta

    @app.post("/v1/memory/{memory_id}/hydrate")
    async def hydrate_one(memory_id: str, req: HydrateOneRequest | None = None):
        """Materialize a memory's payload on demand (lazy hydration).

        When no body is sent, returns raw bytes (backward-compatible).
        When a body with ``profile`` is sent, returns a JSON ProfiledHydration.
        """
        db: MemoryDB = _state["db"]
        base_dir: str = _state["base_dir"]

        if req is not None:
            with Timer() as t:
                try:
                    ph = hydrate_with_profile(
                        db,
                        memory_id,
                        profile=req.profile,
                        base_dir=base_dir,
                        verify=req.verify,
                    )
                except KeyError:
                    return JSONResponse(
                        status_code=404,
                        content={"error": "not_found", "memory_id": memory_id},
                    )
                except ProvenanceError as err:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": "provenance_mismatch",
                            "reason": err.reason,
                            "expected": err.expected,
                            "actual": err.actual,
                            "source_uri": err.source_uri,
                            "message": str(err),
                        },
                    )
                except ValueError as err:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "invalid_profile", "message": str(err)},
                    )
            d = ph.to_dict()
            d["trace_ms"] = round(t.ms, 2)
            return d

        with Timer() as t:
            try:
                payload = hydrate_memory(db, memory_id, base_dir=base_dir)
            except KeyError:
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "memory_id": memory_id},
                )
            except ProvenanceError as err:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "provenance_mismatch",
                        "reason": err.reason,
                        "expected": err.expected,
                        "actual": err.actual,
                        "source_uri": err.source_uri,
                        "message": str(err),
                    },
                )
            except UnsupportedSchemeError as err:
                return JSONResponse(
                    status_code=400,
                    content={"error": "unsupported_scheme", "message": str(err)},
                )

            record = get_memory_metadata(db, memory_id)
            verified = record is not None and bool(record["source_checksum"])
            source_format = record.get("source_format", "") if record else ""

        headers = {
            "X-HotMem-Source-Format": source_format or "",
            "X-HotMem-Provenance": "verified" if verified else "unverified",
            "X-HotMem-Trace-Ms": str(round(t.ms, 2)),
        }
        return Response(content=payload, media_type="application/octet-stream", headers=headers)

    @app.post("/v1/hydrate")
    async def hydrate(req: HydrateRequest):
        db: MemoryDB = _state["db"]
        target = req.path or req.file or _state.get("swap_path") or "swap.jsonl"
        try:
            result = snapshot_hydrate(db, target)
        except SnapshotChecksumError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "snapshot_checksum_mismatch",
                    "reason": err.reason,
                    "file": err.file,
                    "expected": err.expected,
                    "actual": err.actual,
                    "message": str(err),
                },
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        return {
            "loaded": result.loaded,
            "skipped_dupes": result.skipped_dupes,
            "path": target,
        }

    @app.post("/v1/snapshot")
    async def snapshot(req: SnapshotRequest):
        db: MemoryDB = _state["db"]
        base_dir: str = _state["base_dir"]
        target = req.path or req.file or _state.get("swap_path") or "swap.jsonl"
        try:
            result = snapshot_write(
                db, target, copy_attachments=req.copy_attachments, base_dir=base_dir
            )
        except SnapshotChecksumError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "snapshot_checksum_mismatch",
                    "reason": err.reason,
                    "file": err.file,
                    "expected": err.expected,
                    "actual": err.actual,
                    "message": str(err),
                },
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        return {
            "exported": result.exported,
            "path": result.path,
        }

    # ── Filesystem awareness endpoints (#43) ────────────────────────────

    @app.get("/v1/files")
    async def list_files(identifier: str | None = None):
        """List file-backed memory references (metadata only, no file reads)."""
        db: MemoryDB = _state["db"]
        with Timer() as t:
            rows = db.list_file_backed()
            if identifier:
                rows = [r for r in rows if r["identifier"] == identifier]
            from pathlib import Path as _P

            from hotmem.storage import get_adapter

            files = []
            for row in rows:
                source_uri = row.get("source_uri") or ""
                exists = False
                size = row.get("byte_length") or 0
                if source_uri:
                    resolved = source_uri
                    if "://" not in source_uri and not _P(source_uri).is_absolute():
                        resolved = str(_P(_state["base_dir"]) / source_uri)
                    try:
                        adapter = get_adapter(resolved)
                        exists = adapter.exists(resolved)
                        if exists:
                            meta = adapter.metadata(resolved)
                            size = meta["size"]
                    except Exception:
                        pass
                files.append(
                    {
                        "memory_id": row["id"],
                        "identifier": row["identifier"],
                        "source_uri": source_uri,
                        "byte_offset": row.get("byte_offset") or 0,
                        "byte_length": row.get("byte_length") or 0,
                        "source_format": row.get("source_format") or "",
                        "source_checksum": row.get("source_checksum") or None,
                        "exists": exists,
                        "size": size,
                    }
                )
        return {"files": files, "count": len(files), "trace_ms": round(t.ms, 2)}

    @app.get("/v1/bundles")
    async def list_bundles():
        """List discovered bundle index entries from the DB."""
        db: MemoryDB = _state["db"]
        with Timer() as t:
            rows = db.list_bundle_index()
        return {"bundles": rows, "count": len(rows), "trace_ms": round(t.ms, 2)}

    @app.post("/v1/discover")
    async def discover(req: DiscoverRequest):
        """Trigger bundle discovery under a root directory."""
        import asyncio

        from hotmem.bundle_index import index_bundles

        db: MemoryDB = _state["db"]
        with Timer() as t:
            try:
                result = await asyncio.to_thread(
                    index_bundles, db, req.root, max_depth=req.max_depth
                )
            except FileNotFoundError as err:
                return JSONResponse(
                    status_code=400,
                    content={"error": "root_not_found", "message": str(err)},
                )
        return {
            "discovered": result.discovered,
            "indexed": result.indexed,
            "warnings": len(result.warnings),
            "trace_ms": round(t.ms, 2),
        }

    @app.post("/v1/memory/hydrate-batch")
    async def hydrate_batch(req: HydrateBatchRequest):
        """Batch hydrate multiple memories with a profile."""
        db: MemoryDB = _state["db"]
        base_dir: str = _state["base_dir"]
        with Timer() as t:
            results = []
            for mid in req.memory_ids:
                try:
                    ph = hydrate_with_profile(
                        db,
                        mid,
                        profile=req.profile,
                        base_dir=base_dir,
                        verify=req.verify,
                    )
                    results.append(ph.to_dict())
                except KeyError:
                    results.append({"memory_id": mid, "error": "not_found"})
                except ProvenanceError as err:
                    results.append(
                        {
                            "memory_id": mid,
                            "error": "provenance_mismatch",
                            "reason": err.reason,
                            "source_uri": err.source_uri,
                        }
                    )
                except ValueError as err:
                    results.append(
                        {"memory_id": mid, "error": "invalid_profile", "message": str(err)}
                    )
        return {"results": results, "count": len(results), "trace_ms": round(t.ms, 2)}

    return app
