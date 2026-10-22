"""HotMem server - FastAPI app with traced endpoints and instance-owned runtime (#109).

Purpose:
    HTTP daemon/sidecar serving memory operations on a single port.
    Every response includes X-HotMem-Trace-Id header and trace_ms timing.
    All routes delegate to an instance-owned `HotMemRuntime` (`app.state.runtime`)
    rather than module-global state, enforcing policy (#110) and session
    attribution (#111) uniformly.

Interface:
    create_app(db_path, swap_path?, ..., policy?, runtime?) -> FastAPI
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from hotmem.annotations import AnnotationValidationError
from hotmem.embed import Embedder
from hotmem.handoff.codex_source import SourceError
from hotmem.handoff.verify import HandoffError
from hotmem.interchange.hydrate import PackageError
from hotmem.lifecycle import InvalidTransitionError
from hotmem.policy import CallerContext, PolicyConfig, PolicyError
from hotmem.profiles import HydrationProfile
from hotmem.provenance import ProvenanceError
from hotmem.rerank import Reranker
from hotmem.runtime import HotMemRuntime
from hotmem.snapshot import SnapshotChecksumError
from hotmem.storage import UnsupportedSchemeError
from hotmem.trace import Timer, get_tracer, new_trace_id
from hotmem.vector_index import VectorIndex

_trace = get_tracer("server")

try:
    _VERSION = pkg_version("hotmem")
except PackageNotFoundError:
    try:
        from hotmem import __version__ as _VERSION
    except ImportError:
        _VERSION = "0.0.0+unknown"


# Exposed daemon route inventory (#109): every supported HTTP route is declared
# here and backed by HotMemRuntime.
DAEMON_ROUTE_INVENTORY: tuple[tuple[str, str, str], ...] = (
    ("GET", "/v1/health", "read"),
    ("POST", "/v1/add", "write"),
    ("POST", "/v1/search", "read"),
    ("GET", "/v1/memories", "read"),
    ("GET", "/v1/memory/{memory_id}", "read"),
    ("POST", "/v1/memory/{memory_id}/hydrate", "read"),
    ("POST", "/v1/memory/hydrate-batch", "read"),
    ("POST", "/v1/hydrate", "hydrate"),
    ("POST", "/v1/snapshot", "export"),
    ("POST", "/v1/handoff/prepare", "export"),
    ("POST", "/v1/handoff/verify", "read"),
    ("GET", "/v1/handoff/inspect", "read"),
    ("POST", "/v1/handoff/hydrate", "hydrate"),
    ("GET", "/v1/files", "read"),
    ("GET", "/v1/bundles", "read"),
    ("POST", "/v1/discover", "write"),
    ("GET", "/v1/hygiene", "read"),
    ("GET", "/v1/events", "read"),
    ("POST", "/v1/memory/{memory_id}/promote", "promote"),
    ("POST", "/v1/memory/{memory_id}/candidate", "promote"),
    ("GET", "/v1/promotion/candidates", "read"),
    ("GET", "/v1/promotion/states", "read"),
    ("GET", "/v1/promotion/by-state", "read"),
    ("POST", "/v1/vector-index/rebuild", "write"),
    ("GET", "/v1/vector-index/status", "read"),
    ("DELETE", "/v1/vector-index", "write"),
    ("POST", "/v1/policy/revoke", "manage"),
    ("POST", "/v1/policy/reload", "manage"),
)


# ── Request / Response models ────────────────────────────────────────────────


class AddRequest(BaseModel):
    """Add an inline fact OR a file reference."""

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
    """Export memories to a snapshot path (package, v2 directory, or legacy JSONL)."""

    model_config = {"populate_by_name": True}

    path: str | None = Field(default=None, description="Snapshot directory or JSONL file")
    file: str | None = Field(
        default=None, description="Deprecated alias for 'path'", deprecated=True
    )
    copy_attachments: bool = Field(
        default=False,
        description="Copy small file-backed byte ranges into attachments/ (v2 only)",
    )
    package: bool = Field(
        default=False,
        description="Write a hotmem-interchange-v1 clone package (#69)",
    )
    gz: bool = Field(
        default=False,
        description="Gzip the package payload (with package=true)",
    )


class HandoffPrepareRequest(BaseModel):
    """Prepare a handoff package from a source export (#101)."""

    source: str = Field(description="Source export directory (codex-export-v1).")
    output: str = Field(description="Handoff package output directory.")
    mode: str = Field(
        default="resume",
        description="resume (bounded brief + memories) or archive (full ordered stream)",
    )
    consent: str = Field(
        description="Explicit consent statement. Required; capture is never implicit."
    )
    session: str | None = Field(
        default=None,
        description="Require this session id in the export envelope (fail if it differs).",
    )


class HandoffPackageRequest(BaseModel):
    """Point at a handoff package directory."""

    package: str = Field(description="Handoff package directory (hotmem-handoff-v1).")


class HydrateOneRequest(BaseModel):
    """Optional body for POST /v1/memory/{id}/hydrate — profile + verify params."""

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


class PromoteRequest(BaseModel):
    """Body for POST /v1/memory/{id}/promote — apply one lifecycle transition."""

    to_state: str = Field(..., description="HOT | READY | PROMOTED | ARCHIVED")
    reason: str | None = None
    actor: str | None = None


class CandidateRequest(BaseModel):
    """Body for POST /v1/memory/{id}/candidate — set the promotion_candidate flag."""

    candidate: bool = True
    reason: str | None = None


class PolicyRevokeRequest(BaseModel):
    """Body for POST /v1/policy/revoke — revoke a caller and/or share approval."""

    caller_id: str | None = None
    approval_id: str | None = None


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth:
        parts = auth.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return auth.strip()
    token_hdr = request.headers.get("x-hotmem-token")
    if token_hdr:
        return token_hdr.strip()
    return None


def _resolve_caller(runtime: HotMemRuntime, request: Request) -> CallerContext:
    return runtime.resolve_caller(
        token=_extract_token(request),
        requested_store_id=request.headers.get("x-hotmem-store-id"),
        session_id=request.headers.get("x-hotmem-session-id"),
        source=request.headers.get("x-hotmem-source"),
        namespace=request.headers.get("x-hotmem-namespace") or None,
        actor=request.headers.get("x-hotmem-actor"),
        idempotency_key=request.headers.get("x-hotmem-idempotency-key"),
    )


def create_app(
    db_path: str | Path,
    swap_path: str | Path | None = None,
    port: int = 8711,
    base_dir: str | Path | None = None,
    vector_backend: str = "none",
    vector_index: VectorIndex | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    policy: PolicyConfig | None = None,
    store_id: str | None = None,
    runtime: HotMemRuntime | None = None,
    policy_path: str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application with an instance-owned HotMemRuntime."""
    if runtime is not None and str(runtime.db_path) != str(db_path):
        raise ValueError(
            f"runtime store {runtime.db_path!r} does not match db_path {str(db_path)!r}"
        )
    owned_runtime = (
        runtime
        if runtime is not None
        else HotMemRuntime(
            db_path=db_path,
            swap_path=swap_path,
            port=port,
            base_dir=base_dir,
            vector_backend=vector_backend,
            vector_index=vector_index,
            embedder=embedder,
            reranker=reranker,
            policy=policy,
            store_id=store_id,
            policy_path=policy_path,
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_runtime.open()
        app.state.runtime = owned_runtime
        try:
            yield
        finally:
            owned_runtime.close()

    app = FastAPI(
        title="HotMem",
        description="Local-first memory sidecar for agent applications",
        version=_VERSION,
        lifespan=lifespan,
    )
    app.state.runtime = owned_runtime

    @app.exception_handler(PolicyError)
    async def policy_error_handler(request: Request, exc: PolicyError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

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
    async def health(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.health, ctx)

    @app.post("/v1/add")
    async def add_memory(req: AddRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.add,
                ctx,
                identifier=req.identifier,
                fact=req.fact,
                file_uri=req.file_uri,
                byte_offset=req.byte_offset,
                byte_length=req.byte_length,
                source_format=req.source_format,
                source_checksum=req.source_checksum,
                summary=req.summary,
                source=req.source,
                importance=req.importance,
                metadata=req.metadata,
                ttl_seconds=req.ttl_seconds,
            )
        except AnnotationValidationError as err:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_annotations", "message": str(err)},
            )
        except UnsupportedSchemeError as err:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unsupported_scheme",
                    "scheme": err.args[0] if err.args else "unknown",
                    "message": (
                        "only local schemes are supported (file://, "
                        "absolute, relative); remote schemes are not supported"
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

    @app.post("/v1/search")
    async def search(req: SearchRequest, request: Request):
        ctx = _resolve_caller(owned_runtime, request)
        return owned_runtime.search(
            ctx,
            query=req.query,
            top_k=req.top_k,
            max_chars=req.max_chars,
        )

    @app.get("/v1/memories")
    async def list_memories(
        request: Request,
        identifier: str,
        order: str = "asc",
        limit: int = 100,
    ):
        ctx = _resolve_caller(owned_runtime, request)
        if order not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be 1..1000")
        return owned_runtime.list_memories(
            ctx,
            identifier=identifier,
            order=order,
            limit=limit,
        )

    @app.get("/v1/memory/{memory_id}")
    async def get_memory(memory_id: str, request: Request):
        ctx = _resolve_caller(owned_runtime, request)
        meta = owned_runtime.get_memory(ctx, memory_id)
        if meta is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "memory_id": memory_id},
            )
        return meta

    @app.post("/v1/memory/{memory_id}/hydrate")
    async def hydrate_one(
        memory_id: str,
        request: Request,
        req: HydrateOneRequest | None = None,
    ):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        if req is not None:
            try:
                return await asyncio.to_thread(
                    owned_runtime.hydrate_one_profiled,
                    ctx,
                    memory_id,
                    profile=req.profile,
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

        try:
            payload, headers = await asyncio.to_thread(
                owned_runtime.hydrate_one_raw, ctx, memory_id
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
        except UnsupportedSchemeError as err:
            return JSONResponse(
                status_code=400,
                content={"error": "unsupported_scheme", "message": str(err)},
            )
        return Response(content=payload, media_type="application/octet-stream", headers=headers)

    @app.post("/v1/hydrate")
    async def hydrate(req: HydrateRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        target = req.path or req.__dict__.get("file")
        try:
            return await asyncio.to_thread(
                owned_runtime.hydrate_snapshot, ctx, target=target
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
        except PackageError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "package_verification_failed",
                    "reason": err.reason,
                    "file": err.file,
                    "expected": err.expected,
                    "actual": err.actual,
                    "message": str(err),
                },
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

    @app.post("/v1/snapshot")
    async def snapshot(req: SnapshotRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        target = req.path or req.__dict__.get("file")
        try:
            return await asyncio.to_thread(
                owned_runtime.snapshot,
                ctx,
                target=target,
                copy_attachments=req.copy_attachments,
                package=req.package,
                gz=req.gz,
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

    # ── Session handoff endpoints (#101) ───────────────────────────────

    @app.post("/v1/handoff/prepare")
    async def handoff_prepare(req: HandoffPrepareRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.handoff_prepare,
                ctx,
                source=req.source,
                output=req.output,
                mode=req.mode,
                consent=req.consent,
                session=req.session,
            )
        except (SourceError, ValueError) as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

    @app.post("/v1/handoff/verify")
    async def handoff_verify(req: HandoffPackageRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.handoff_verify, ctx, package=req.package
            )
        except HandoffError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "handoff_invalid",
                    "reason": err.reason,
                    "file": err.file,
                    "expected": err.expected,
                    "actual": err.actual,
                    "message": str(err),
                },
            )

    @app.get("/v1/handoff/inspect")
    async def handoff_inspect(package: str, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(
            owned_runtime.handoff_inspect, ctx, package=package
        )

    @app.post("/v1/handoff/hydrate")
    async def handoff_hydrate(req: HandoffPackageRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.handoff_hydrate, ctx, package=req.package
            )
        except HandoffError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "handoff_invalid",
                    "reason": err.reason,
                    "file": err.file,
                    "expected": err.expected,
                    "actual": err.actual,
                    "message": str(err),
                },
            )

    # ── Filesystem awareness endpoints (#43) ────────────────────────────

    @app.get("/v1/files")
    async def list_files(request: Request, identifier: str | None = None):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(
            owned_runtime.list_files, ctx, identifier=identifier
        )

    @app.get("/v1/bundles")
    async def list_bundles(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.list_bundles, ctx)

    @app.post("/v1/discover")
    async def discover(req: DiscoverRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.discover, ctx, root=req.root, max_depth=req.max_depth
            )
        except FileNotFoundError as err:
            return JSONResponse(
                status_code=400,
                content={"error": "root_not_found", "message": str(err)},
            )

    @app.post("/v1/memory/hydrate-batch")
    async def hydrate_batch(req: HydrateBatchRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(
            owned_runtime.hydrate_batch,
            ctx,
            req.memory_ids,
            profile=req.profile,
            verify=req.verify,
        )

    # ── Hygiene endpoint (#51) ─────────────────────────────────────────

    @app.get("/v1/hygiene")
    async def hygiene(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.hygiene, ctx)

    @app.get("/v1/events")
    async def list_events(
        request: Request,
        memory_id: str | None = None,
        namespace: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 100,
        order: str = "asc",
    ):
        ctx = _resolve_caller(owned_runtime, request)
        if order not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
        return owned_runtime.list_events(
            ctx,
            memory_id=memory_id,
            namespace=namespace,
            event_type=event_type,
            since=since,
            until=until,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            order=order,
        )

    # ── Promotion lifecycle endpoints (#42, signal only) ────────────────

    @app.post("/v1/memory/{memory_id}/promote")
    async def promote_memory(memory_id: str, req: PromoteRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.promote_memory,
                ctx,
                memory_id,
                to_state=req.to_state,
                reason=req.reason,
                actor=req.actor,
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "memory_id": memory_id},
            )
        except InvalidTransitionError as err:
            return JSONResponse(status_code=409, content=err.to_dict())

    @app.post("/v1/memory/{memory_id}/candidate")
    async def set_candidate(memory_id: str, req: CandidateRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        try:
            return await asyncio.to_thread(
                owned_runtime.set_candidate,
                ctx,
                memory_id,
                candidate=req.candidate,
                reason=req.reason,
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "memory_id": memory_id},
            )

    @app.get("/v1/promotion/candidates")
    async def promotion_candidates(
        request: Request,
        namespace: str | None = None,
        state: str | None = None,
    ):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(
            owned_runtime.promotion_candidates, ctx, namespace=namespace, state=state
        )

    @app.get("/v1/promotion/states")
    async def promotion_states(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.promotion_states, ctx)

    @app.get("/v1/promotion/by-state")
    async def promotion_by_state(
        request: Request,
        state: str,
        namespace: str | None = None,
    ):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(
            owned_runtime.promotion_by_state, ctx, state=state, namespace=namespace
        )

    # ── Vector index endpoints (#49 — optional, derived, disposable) ────

    @app.post("/v1/vector-index/rebuild")
    async def rebuild_index(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        status_code, payload = await asyncio.to_thread(
            owned_runtime.vector_index_rebuild, ctx
        )
        if status_code != 200:
            return JSONResponse(status_code=status_code, content=payload)
        return payload

    @app.get("/v1/vector-index/status")
    async def vector_index_status(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.vector_index_status, ctx)

    @app.delete("/v1/vector-index")
    async def clear_index(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.vector_index_clear, ctx)

    # ── Policy management (#110: immediate revocation) ─────────────────

    @app.post("/v1/policy/revoke")
    async def policy_revoke(req: PolicyRevokeRequest, request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(
            owned_runtime.revoke_policy,
            ctx,
            caller_id=req.caller_id,
            approval_id=req.approval_id,
        )

    @app.post("/v1/policy/reload")
    async def policy_reload(request: Request):
        import asyncio

        ctx = _resolve_caller(owned_runtime, request)
        return await asyncio.to_thread(owned_runtime.reload_policy, ctx)

    return app
