"""HotMem runtime — instance-owned store, warm resources, and operation boundary (#109).

Purpose:
    Own a single store (`MemoryDB`), active `Embedder`, optional `Reranker`,
    optional `VectorIndex`, and `PolicyConfig` without module-global state.
    All transport adapters (HTTP server, MCP server/relay, CLI) delegate
    operations through `HotMemRuntime` so persistence, policy (#110),
    session attribution (#111), and diagnostics are enforced uniformly.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from hotmem.annotations import validate_metadata
from hotmem.bundle_index import index_bundles
from hotmem.db import MemoryDB
from hotmem.embed import DEFAULT_EMBEDDER, Embedder, pack_embedding
from hotmem.events import EventType, append_event, emit_import_event, query_events
from hotmem.handoff.hydrate import hydrate_handoff
from hotmem.handoff.package import prepare_handoff
from hotmem.handoff.verify import inspect_handoff, verify_handoff
from hotmem.hygiene import check_hygiene
from hotmem.lifecycle import (
    list_by_state,
    list_candidates,
    mark_candidate,
    states_model,
    transition,
)
from hotmem.memory import FileRef, add_file_backed, get_memory_metadata, hydrate_memory
from hotmem.mount import StoreLock
from hotmem.policy import CallerContext, PolicyConfig, PolicyError
from hotmem.profiles import HydrationProfile, hydrate_with_profile
from hotmem.provenance import ProvenanceError
from hotmem.rerank import Reranker
from hotmem.search import search_memories
from hotmem.snapshot import hydrate as snapshot_hydrate
from hotmem.snapshot import snapshot as snapshot_write
from hotmem.storage import get_adapter
from hotmem.swap import compute_content_hash
from hotmem.trace import Timer, get_tracer
from hotmem.vector_index import (
    VALID_BACKENDS,
    NullVectorIndex,
    VectorIndex,
    VectorIndexConfig,
    get_vector_index,
    rebuild_vector_index,
)

_trace = get_tracer("runtime")


def _safe_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def derive_store_id(db_path: str | Path, explicit_store_id: str | None = None) -> str:
    """Return explicit store_id or a deterministic store identifier from resolved path."""
    if explicit_store_id:
        return explicit_store_id
    resolved = Path(db_path).resolve()
    manifest_path = resolved.parent / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("store_id"):
                return str(manifest["store_id"])
        except Exception:
            pass
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"store-{digest}"


class HotMemRuntime:
    """Instance-owned HotMem runtime and common operation boundary."""

    def __init__(
        self,
        db_path: str | Path,
        swap_path: str | Path | None = None,
        *,
        port: int = 8711,
        base_dir: str | Path | None = None,
        vector_backend: str = "none",
        vector_index: VectorIndex | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        policy: PolicyConfig | None = None,
        store_id: str | None = None,
        write_timeout_s: float = 10.0,
    ) -> None:
        if vector_backend not in VALID_BACKENDS:
            raise ValueError(
                f"unknown vector_backend {vector_backend!r}; expected one of {VALID_BACKENDS}"
            )
        self.db_path = str(db_path)
        self.swap_path = str(swap_path) if swap_path else None
        self.port = port
        self.base_dir = (
            str(base_dir) if base_dir is not None else str(Path(db_path).resolve().parent)
        )
        self.vector_backend = vector_backend
        self._vector_index_injected = vector_index
        self.embedder: Embedder = embedder if embedder is not None else DEFAULT_EMBEDDER
        self.reranker: Reranker | None = reranker
        self.policy: PolicyConfig = policy if policy is not None else PolicyConfig()
        self.store_id: str = derive_store_id(
            self.db_path, store_id or self.policy.store_id or None
        )
        if not self.policy.store_id:
            self.policy.store_id = self.store_id
        self.runtime_generation: str = uuid.uuid4().hex[:12]
        self.start_time: float = 0.0
        self.write_timeout_s: float = write_timeout_s
        self.db: MemoryDB | None = None
        self.vector_index: VectorIndex | None = None
        self._store_lock = StoreLock(
            self.db_path,
            store_id=self.store_id,
            port=self.port,
            runtime_generation=self.runtime_generation,
        )
        self._write_lock = threading.RLock()
        self._closed = False

    def open(self) -> HotMemRuntime:
        """Open SQLite store, optional vector index, and auto-hydrate if configured."""
        if self.db is not None and not self._closed:
            return self
        self._store_lock.acquire()
        try:
            self.db = MemoryDB(self.db_path)
            self.start_time = time.time()
            self._closed = False

            if self._vector_index_injected is None:
                self.vector_index = get_vector_index(
                    VectorIndexConfig(backend=self.vector_backend),
                    base_dir=self.base_dir,
                )
            else:
                self.vector_index = self._vector_index_injected

            if self.swap_path and Path(self.swap_path).exists():
                result = snapshot_hydrate(self.db, self.swap_path, embedder=self.embedder)
                emit_import_event(
                    self.db,
                    path=str(self.swap_path),
                    loaded=result.loaded,
                    skipped_dupes=result.skipped_dupes,
                    invalid=result.invalid,
                )
                _trace.info(
                    "startup",
                    f"auto-hydrated {result.loaded} memories",
                    detail={"swap_path": str(self.swap_path)},
                )
        except Exception:
            if self.db is not None:
                with contextlib.suppress(Exception):
                    self.db.close()
                self.db = None
            self._store_lock.release()
            raise

        _trace.info(
            "startup",
            "runtime ready",
            detail={
                "db_path": self.db_path,
                "store_id": self.store_id,
                "port": self.port,
            },
        )
        return self

    def close(self) -> None:
        """Gracefully close vector index, SQLite database, and store ownership lock."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.vector_index is not None:
                with contextlib.suppress(Exception):
                    self.vector_index.close()
            if self.db is not None:
                with contextlib.suppress(Exception):
                    self.db.close()
        finally:
            self._store_lock.release()

    def _require_db(self) -> MemoryDB:
        if self.db is None or self._closed:
            raise PolicyError(
                503,
                "store_unavailable",
                "HotMem store is not open or is currently unavailable",
            )
        return self.db

    @contextlib.contextmanager
    def _read_db(self):
        """Yield a MemoryDB for read-only operations without blocking behind slow writes."""
        primary = self._require_db()
        if self._write_lock.acquire(blocking=False):
            try:
                yield primary
            finally:
                self._write_lock.release()
            return

        if self.db_path == ":memory:":
            if not self._write_lock.acquire(timeout=self.write_timeout_s):
                raise PolicyError(
                    503,
                    "server_busy",
                    "daemon queue saturated waiting for in-memory store lock",
                )
            try:
                yield primary
            finally:
                self._write_lock.release()
            return

        ro_db = MemoryDB(self.db_path, read_only=True)
        try:
            yield ro_db
        finally:
            ro_db.close()

    @contextlib.contextmanager
    def _write_slot(self):
        """Acquire the bounded write lock and guarantee rollback on error/cancellation."""
        db = self._require_db()
        if not self._write_lock.acquire(timeout=self.write_timeout_s):
            raise PolicyError(
                503,
                "server_busy",
                f"daemon write queue saturated (timeout={self.write_timeout_s}s)",
            )
        try:
            yield db
        except BaseException:
            with contextlib.suppress(Exception):
                db.rollback()
            raise
        finally:
            self._write_lock.release()

    def resolve_caller(
        self,
        *,
        token: str | None = None,
        requested_store_id: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> CallerContext:
        """Resolve and authenticate caller context against this runtime's policy."""
        return self.policy.resolve_request(
            runtime_store_id=self.store_id,
            token=token,
            requested_store_id=requested_store_id,
            session_id=session_id,
            source=source,
            namespace=namespace,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    # ── Operations ───────────────────────────────────────────────────────

    def health(self, ctx: CallerContext) -> dict[str, Any]:
        descriptor = self.embedder.descriptor
        with self._read_db() as db:
            if not ctx.policy_enforced:
                return {
                    "status": "ok",
                    "memory_count": db.count(),
                    "db_path": self.db_path,
                    "uptime_s": round(time.time() - self.start_time, 1),
                    "embedding": {"model": descriptor.key, "dim": descriptor.dimension},
                }

            visible_rows = ctx.filter_rows(
                db.all_rows(include_embedding=False), operation="read"
            )
            visible_count = len(visible_rows)
        return {
            "status": "ok",
            "memory_count": visible_count,
            "db_path": self.db_path,
            "uptime_s": round(time.time() - self.start_time, 1),
            "embedding": {"model": descriptor.key, "dim": descriptor.dimension},
            "store_id": self.store_id,
            "store_state": "verified_empty" if visible_count == 0 else "ready",
            "runtime_generation": self.runtime_generation,
        }

    def add(
        self,
        ctx: CallerContext,
        *,
        identifier: str,
        fact: str | None = None,
        file_uri: str | None = None,
        byte_offset: int | None = None,
        byte_length: int | None = None,
        source_format: str | None = None,
        source_checksum: str | None = None,
        summary: str | None = None,
        source: str = "",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        self._require_db()
        ctx.require_operation("write")
        target_namespace = ctx.effective_write_namespace
        effective_source = ctx.source or source
        # Verify request body source doesn't spoof another caller's bound source
        if ctx.policy_enforced and source and ctx.source and source != ctx.source:
            msg = (
                f"request payload source {source!r} conflicts with "
                f"session header source {ctx.source!r}"
            )
            raise PolicyError(
                403,
                "session_impersonation_denied",
                msg,
            )
        if (
            ctx.policy_enforced
            and source
            and not ctx.source
            and ctx.credential is not None
            and ctx.credential.allowed_sources
            and source not in ctx.credential.allowed_sources
        ):
            raise PolicyError(
                403,
                "session_impersonation_denied",
                f"source {source!r} is not permitted for caller {ctx.caller_id!r}",
            )

        meta_dict = metadata or {}
        validate_metadata(meta_dict)

        envelope = ctx.build_session_envelope(
            namespace=target_namespace, request_source=effective_source
        )
        provenance_dict = envelope.to_provenance_dict() if envelope is not None else None
        provenance_json = json.dumps(provenance_dict, sort_keys=True) if provenance_dict else None

        from hotmem.events import _build_memory_created_payload

        req_canonical = json.dumps(
            {
                "identifier": identifier,
                "fact": fact,
                "file_uri": file_uri,
                "byte_offset": byte_offset,
                "byte_length": byte_length,
                "source_format": source_format,
                "source_checksum": source_checksum,
                "summary": summary,
                "source": effective_source,
                "importance": importance,
                "metadata": meta_dict,
                "ttl_seconds": ttl_seconds,
                "namespace": target_namespace,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(req_canonical.encode("utf-8")).hexdigest()

        with self._write_slot() as db, Timer() as t:
            if ctx.idempotency_key:
                ledger_entry = db.get_operation_ledger(
                    ctx.caller_id, target_namespace, ctx.idempotency_key
                )
                if ledger_entry is not None:
                    if ledger_entry["request_hash"] != request_hash:
                        msg = (
                            f"idempotency key {ctx.idempotency_key!r} was already "
                            "used with a different request payload"
                        )
                        raise PolicyError(409, "idempotency_key_conflict", msg)
                    saved = json.loads(ledger_entry["response_json"])
                    return {
                        "memory_id": saved["memory_id"],
                        "content_hash": saved["content_hash"],
                        "trace_ms": round(t.ms, 2),
                    }

            if file_uri is not None:
                ref = FileRef(
                    source_uri=file_uri,
                    byte_offset=byte_offset or 0,
                    byte_length=byte_length or 0,
                    source_format=source_format or "",
                    source_checksum=source_checksum,
                )
                memory_id, content_hash = add_file_backed(
                    db,
                    identifier=identifier,
                    file_ref=ref,
                    base_dir=self.base_dir,
                    summary=summary,
                    importance=importance,
                    metadata=meta_dict,
                    source=effective_source,
                    namespace=target_namespace,
                    provenance=provenance_dict,
                    embedder=self.embedder,
                    _commit=False,
                )
                record = db.get_memory(memory_id) or {}
                append_event(
                    db,
                    event_type=EventType.MEMORY_CREATED,
                    memory_id=memory_id,
                    namespace=target_namespace,
                    payload=_build_memory_created_payload(record),
                    _commit=not bool(ctx.idempotency_key),
                )
            else:
                active = self.embedder
                content_hash = compute_content_hash(identifier, fact or "")
                existing_row = db.find_by_namespace_and_hash(target_namespace, content_hash)
                if existing_row is not None:
                    memory_id = str(existing_row["id"])
                else:
                    memory_id = uuid.uuid4().hex
                    vec = active.embed(fact or "")
                    blob = pack_embedding(vec)

                    db.insert(
                        id=memory_id,
                        identifier=identifier,
                        fact_text=fact or "",
                        embedding=blob,
                        embedding_dim=active.descriptor.dimension,
                        embedding_model=active.descriptor.key,
                        source=effective_source,
                        importance=importance,
                        metadata_json=json.dumps(meta_dict),
                        content_hash=content_hash,
                        ttl_seconds=ttl_seconds,
                        namespace=target_namespace,
                        provenance_json=provenance_json,
                        _commit=False,
                    )
                    record = db.get_memory(memory_id) or {}
                    append_event(
                        db,
                        event_type=EventType.MEMORY_CREATED,
                        memory_id=memory_id,
                        namespace=target_namespace,
                        payload=_build_memory_created_payload(record),
                        _commit=not bool(ctx.idempotency_key),
                    )

            if ctx.idempotency_key:
                db.put_operation_ledger(
                    caller_id=ctx.caller_id,
                    namespace=target_namespace,
                    operation_key=ctx.idempotency_key,
                    operation_name="add",
                    request_hash=request_hash,
                    memory_id=memory_id,
                    content_hash=content_hash,
                    response_json=json.dumps(
                        {"memory_id": memory_id, "content_hash": content_hash},
                        sort_keys=True,
                    ),
                    _commit=True,
                )

        return {
            "memory_id": memory_id,
            "content_hash": content_hash,
            "trace_ms": round(t.ms, 2),
        }

    def search(
        self,
        ctx: CallerContext,
        *,
        query: str,
        top_k: int = 5,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        ctx.require_operation("read")
        row_filter = (
            (lambda row: ctx.can_access_row(row, operation="read"))
            if (ctx.policy_enforced or ctx.namespace is not None)
            else None
        )
        with self._read_db() as db, Timer() as t:
            messages = search_memories(
                db,
                query=query,
                top_k=top_k,
                max_chars=max_chars,
                vector_index=self.vector_index,
                embedder=self.embedder,
                reranker=self.reranker,
                row_filter=row_filter,
            )
        return {
            "memories": messages,
            "count": len(messages),
            "trace_ms": round(t.ms, 2),
        }

    def list_memories(
        self,
        ctx: CallerContext,
        *,
        identifier: str,
        order: str = "asc",
        limit: int = 100,
    ) -> dict[str, Any]:
        ctx.require_operation("read")
        row_filter = (
            (lambda row: ctx.can_access_row(row, operation="read"))
            if (ctx.policy_enforced or ctx.namespace is not None)
            else None
        )
        with self._read_db() as db, Timer() as t:
            rows = db.list_by_identifier(
                identifier, order=order, limit=limit, row_filter=row_filter
            )
        return {
            "memories": rows,
            "count": len(rows),
            "trace_ms": round(t.ms, 2),
        }

    def get_memory(self, ctx: CallerContext, memory_id: str) -> dict[str, Any] | None:
        ctx.require_operation("read")
        with self._read_db() as db:
            record = get_memory_metadata(db, memory_id)
        if record is None:
            return None
        if not ctx.can_access_row(record, operation="read"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} is not permitted to read memory {memory_id!r}",
            )
        return {
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

    def hydrate_one_profiled(
        self,
        ctx: CallerContext,
        memory_id: str,
        *,
        profile: HydrationProfile = "agent",
        verify: bool = True,
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("read")
        record = get_memory_metadata(db, memory_id)
        if record is None:
            raise KeyError(memory_id)
        if not ctx.can_access_row(record, operation="read"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} is not permitted to hydrate memory {memory_id!r}",
            )
        with Timer() as t:
            ph = hydrate_with_profile(
                db,
                memory_id,
                profile=profile,
                base_dir=self.base_dir,
                verify=verify,
            )
        d = ph.to_dict()
        d["trace_ms"] = round(t.ms, 2)
        return d

    def hydrate_one_raw(
        self, ctx: CallerContext, memory_id: str
    ) -> tuple[bytes, dict[str, str]]:
        db = self._require_db()
        ctx.require_operation("read")
        record = get_memory_metadata(db, memory_id)
        if record is None:
            raise KeyError(memory_id)
        if not ctx.can_access_row(record, operation="read"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} is not permitted to hydrate memory {memory_id!r}",
            )
        with Timer() as t:
            payload = hydrate_memory(db, memory_id, base_dir=self.base_dir)
            verified = bool(record["source_checksum"])
            source_format = record.get("source_format", "") or ""
        headers = {
            "X-HotMem-Source-Format": source_format,
            "X-HotMem-Provenance": "verified" if verified else "unverified",
            "X-HotMem-Trace-Ms": str(round(t.ms, 2)),
        }
        return payload, headers

    def hydrate_batch(
        self,
        ctx: CallerContext,
        memory_ids: list[str],
        *,
        profile: HydrationProfile = "agent",
        verify: bool = True,
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("read")
        with Timer() as t:
            results = []
            for mid in memory_ids:
                record = get_memory_metadata(db, mid)
                if record is None:
                    results.append({"memory_id": mid, "error": "not_found"})
                    continue
                if not ctx.can_access_row(record, operation="read"):
                    results.append({"memory_id": mid, "error": "scope_denied"})
                    continue
                try:
                    ph = hydrate_with_profile(
                        db,
                        mid,
                        profile=profile,
                        base_dir=self.base_dir,
                        verify=verify,
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

    def hydrate_snapshot(
        self, ctx: CallerContext, *, target: str | None = None
    ) -> dict[str, Any]:
        ctx.require_operation("hydrate")
        resolved_target = target or self.swap_path or "swap.jsonl"
        with self._write_slot() as db:
            result = snapshot_hydrate(db, resolved_target, embedder=self.embedder)
            emit_import_event(
                db,
                path=resolved_target,
                loaded=result.loaded,
                skipped_dupes=result.skipped_dupes,
                invalid=result.invalid,
            )
        return {
            "loaded": result.loaded,
            "skipped_dupes": result.skipped_dupes,
            "invalid": result.invalid,
            "path": resolved_target,
            **result.disposition(),
        }

    def snapshot(
        self,
        ctx: CallerContext,
        *,
        target: str | None = None,
        copy_attachments: bool = False,
        package: bool = False,
        gz: bool = False,
    ) -> dict[str, Any]:
        ctx.require_operation("export")
        resolved_target = target or self.swap_path or "swap.jsonl"
        row_filter = (
            (
                lambda row: ctx.can_access_row(row, operation="export")
                and ctx.can_access_row(row, operation="read")
            )
            if (ctx.policy_enforced or ctx.namespace is not None)
            else None
        )
        with self._write_slot() as db:
            result = snapshot_write(
                db,
                resolved_target,
                copy_attachments=copy_attachments,
                base_dir=self.base_dir,
                package=package,
                gz=gz,
                row_filter=row_filter,
            )
        return {
            "exported": result.exported,
            "path": result.path,
        }

    def handoff_prepare(
        self,
        ctx: CallerContext,
        *,
        source: str,
        output: str,
        mode: str = "resume",
        consent: str,
        session: str | None = None,
    ) -> dict[str, Any]:
        ctx.require_operation("export")
        result = prepare_handoff(
            source,
            output,
            mode=mode,
            consent=consent,
            session_id=session,
        )
        counts = result.manifest["counts"]
        return {
            "handoff_id": result.handoff_id,
            "package_id": result.package_id,
            "mode": result.manifest["mode"],
            "entries": counts["entries"],
            "memories": counts["memories"],
            "coverage": {
                "omitted_count": result.coverage["omitted_count"],
                "redacted_count": result.coverage["redacted_count"],
                "recoverable_count": result.coverage["recoverable_count"],
            },
            "path": result.path,
            "timings_ms": result.timings_ms,
        }

    def handoff_verify(self, ctx: CallerContext, *, package: str) -> dict[str, Any]:
        ctx.require_operation("read")
        verified = verify_handoff(package)
        return {
            "valid": True,
            "package_id": verified.manifest["package_id"],
            "mode": verified.manifest["mode"],
            "entries": verified.entry_count,
            "memories": verified.memory_count,
        }

    def handoff_inspect(self, ctx: CallerContext, *, package: str) -> dict[str, Any]:
        ctx.require_operation("read")
        return inspect_handoff(package)

    def handoff_hydrate(self, ctx: CallerContext, *, package: str) -> dict[str, Any]:
        ctx.require_operation("hydrate")
        with self._write_slot() as db:
            result = hydrate_handoff(db, package, embedder=self.embedder)
        return {
            "handoff_id": result.handoff_id,
            "package_id": result.package_id,
            "loaded": result.loaded,
            "skipped": result.skipped_dupes,
            "invalid": result.invalid,
            "already_applied": result.already_applied,
            "brief": result.brief_identifier,
            **result.disposition(),
        }

    def list_files(
        self, ctx: CallerContext, *, identifier: str | None = None
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("read")
        with Timer() as t:
            rows = ctx.filter_rows(db.list_file_backed(), operation="read")
            if identifier:
                rows = [r for r in rows if r["identifier"] == identifier]
            files = []
            for row in rows:
                source_uri = row.get("source_uri") or ""
                exists = False
                size = row.get("byte_length") or 0
                if source_uri:
                    resolved = source_uri
                    if "://" not in source_uri and not Path(source_uri).is_absolute():
                        resolved = str(Path(self.base_dir) / source_uri)
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

    def list_bundles(self, ctx: CallerContext) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("read")
        with Timer() as t:
            rows = db.list_bundle_index()
        return {"bundles": rows, "count": len(rows), "trace_ms": round(t.ms, 2)}

    def discover(
        self, ctx: CallerContext, *, root: str, max_depth: int = 10
    ) -> dict[str, Any]:
        ctx.require_operation("write")
        with self._write_slot() as db, Timer() as t:
            result = index_bundles(db, root, max_depth=max_depth)
            append_event(
                db,
                event_type=EventType.BUNDLE_DISCOVERED,
                payload={
                    "root": root,
                    "discovered": result.discovered,
                    "indexed": result.indexed,
                    "warnings": len(result.warnings),
                },
            )
        return {
            "discovered": result.discovered,
            "indexed": result.indexed,
            "warnings": len(result.warnings),
            "trace_ms": round(t.ms, 2),
        }

    def hygiene(self, ctx: CallerContext) -> dict[str, Any]:
        ctx.require_operation("read")
        with self._write_slot() as db:
            report = check_hygiene(db, base_dir=self.base_dir)
            counts = {
                "warning_count": len(report.warnings),
                "error_count": sum(1 for w in report.warnings if w.severity == "error"),
                "warn_count": sum(1 for w in report.warnings if w.severity == "warn"),
                "info_count": sum(1 for w in report.warnings if w.severity == "info"),
            }
            append_event(
                db,
                event_type=EventType.HYGIENE_CHECKED,
                payload=counts,
            )
            for w in report.warnings:
                if w.severity != "error":
                    continue
                append_event(
                    db,
                    event_type=EventType.HYGIENE_WARNING,
                    memory_id=w.detail.get("memory_id"),
                    payload={
                        "category": w.category,
                        "severity": w.severity,
                        "message": w.message,
                        "source_uri": w.detail.get("source_uri"),
                        "identifier": w.detail.get("identifier"),
                    },
                )
            with contextlib.suppress(Exception):
                db.trim_events_by_count(10_000)
        return report.to_dict()

    def list_events(
        self,
        ctx: CallerContext,
        *,
        memory_id: str | None = None,
        namespace: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = 100,
        order: str = "asc",
    ) -> dict[str, Any]:
        ctx.require_operation("read")
        if namespace is not None and not ctx.can_access_namespace(
            namespace, operation="read"
        ):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} cannot read events in namespace {namespace!r}",
            )
        with self._read_db() as db, Timer() as t:
            listing = query_events(
                db,
                memory_id=memory_id,
                namespace=namespace,
                event_type=event_type,
                since=since,
                until=until,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                ascending=(order == "asc"),
            )
            if not ctx.policy_enforced:
                return {
                    "events": listing["events"],
                    "count": listing["count"],
                    "next_seq": listing["next_seq"],
                    "trace_ms": round(t.ms, 2),
                }

            filtered_events: list[dict[str, Any]] = []
            for ev in listing["events"]:
                ev_ns = str(ev.get("namespace") or "")
                payload = dict(ev.get("payload") or {})
                row_probe = {
                    "id": ev.get("memory_id") or payload.get("id") or "",
                    "identifier": payload.get("identifier") or "",
                    "namespace": ev_ns or str(payload.get("namespace") or ""),
                }
                if not ctx.can_access_row(row_probe, operation="read"):
                    continue
                # Strip raw embedding blobs and full fact_text from daemon activity stream
                sanitized_payload = dict(payload)
                sanitized_payload.pop("embedding_b64", None)
                sanitized_payload.pop("fact_text", None)
                filtered_events.append(
                    {
                        **ev,
                        "payload": sanitized_payload,
                    }
                )
            next_seq = None if not filtered_events else filtered_events[-1]["seq"]
        return {
            "events": filtered_events,
            "count": len(filtered_events),
            "next_seq": next_seq,
            "store_id": self.store_id,
            "runtime_generation": self.runtime_generation,
            "policy_revision": self.policy.revision,
            "trace_ms": round(t.ms, 2),
        }

    def promote_memory(
        self,
        ctx: CallerContext,
        memory_id: str,
        *,
        to_state: str,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("promote")
        record = get_memory_metadata(db, memory_id)
        if record is None:
            raise KeyError(memory_id)
        if not ctx.can_access_row(record, operation="promote"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} cannot promote memory {memory_id!r}",
            )
        if ctx.policy_enforced and actor and actor != ctx.caller_id:
            raise PolicyError(
                403,
                "session_impersonation_denied",
                f"caller {ctx.caller_id!r} cannot impersonate actor {actor!r}",
            )
        with self._write_slot() as db, Timer() as t:
            result = transition(
                db,
                memory_id,
                to_state,
                reason=reason,
                actor=actor or (ctx.caller_id if ctx.policy_enforced else None),
            )
        result["trace_ms"] = round(t.ms, 2)
        return result

    def set_candidate(
        self,
        ctx: CallerContext,
        memory_id: str,
        *,
        candidate: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("promote")
        record = get_memory_metadata(db, memory_id)
        if record is None:
            raise KeyError(memory_id)
        if not ctx.can_access_row(record, operation="promote"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} cannot modify candidate state for {memory_id!r}",
            )
        with self._write_slot() as db, Timer() as t:
            result = mark_candidate(db, memory_id, candidate, reason=reason)
        result["trace_ms"] = round(t.ms, 2)
        return result

    def promotion_candidates(
        self,
        ctx: CallerContext,
        *,
        namespace: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("read")
        with Timer() as t:
            rows = ctx.filter_rows(
                list_candidates(db, namespace=namespace, state=state), operation="read"
            )
        candidates = [
            {
                "memory_id": r["id"],
                "identifier": r["identifier"],
                "namespace": r.get("namespace") or "",
                "promotion_state": r.get("promotion_state") or "HOT",
                "memory_type": r.get("memory_type") or "fact",
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]
        return {
            "candidates": candidates,
            "count": len(candidates),
            "trace_ms": round(t.ms, 2),
        }

    def promotion_states(self, ctx: CallerContext) -> dict[str, Any]:
        ctx.require_operation("read")
        return states_model()

    def promotion_by_state(
        self,
        ctx: CallerContext,
        *,
        state: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        db = self._require_db()
        ctx.require_operation("read")
        with Timer() as t:
            rows = ctx.filter_rows(
                list_by_state(db, state, namespace=namespace), operation="read"
            )
        summaries = [
            {
                "memory_id": r["id"],
                "identifier": r["identifier"],
                "namespace": r.get("namespace") or "",
                "promotion_state": r.get("promotion_state") or "HOT",
                "promotion_candidate": bool(r.get("promotion_candidate") or 0),
                "memory_type": r.get("memory_type") or "fact",
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ]
        return {
            "memories": summaries,
            "count": len(summaries),
            "trace_ms": round(t.ms, 2),
        }

    def vector_index_rebuild(self, ctx: CallerContext) -> tuple[int, dict[str, Any]]:
        self._require_db()
        ctx.require_operation("write")
        vector_index = self.vector_index
        if vector_index is None or isinstance(vector_index, NullVectorIndex):
            req_backend = (
                vector_index.requested_backend
                if isinstance(vector_index, NullVectorIndex)
                else "none"
            )
            if req_backend == "none":
                return 400, {
                    "error": "vector_index_disabled",
                    "message": (
                        "no vector index backend is configured; start with "
                        "--vector-index chroma to enable the derived index"
                    ),
                }
            return 400, {
                "error": "vector_dependency_missing",
                "requested_backend": req_backend,
                "message": (
                    f"backend {req_backend!r} was requested but its "
                    "package is not installed; install with: "
                    "uv pip install 'hotmem[vector]'"
                ),
            }
        with self._write_slot() as db:
            result = rebuild_vector_index(
                db,
                vector_index,
                embedding_model=self.embedder.descriptor.key,
                embedding_dim=self.embedder.descriptor.dimension,
            )
            append_event(
                db,
                event_type=EventType.INDEX_REBUILT,
                payload={
                    "backend": vector_index.status(db)["backend"],
                    "indexed_count": result["indexed_count"],
                    "db_count": result["db_count"],
                    "skipped_no_embedding": result["skipped_no_embedding"],
                },
            )
        return 200, result

    def vector_index_status(self, ctx: CallerContext) -> dict[str, Any]:
        ctx.require_operation("read")
        assert self.vector_index is not None
        with self._read_db() as db, Timer() as t:
            status = self.vector_index.status(db)
        status["trace_ms"] = round(t.ms, 2)
        return status

    def vector_index_clear(self, ctx: CallerContext) -> dict[str, Any]:
        ctx.require_operation("write")
        assert self.vector_index is not None
        with self._write_slot() as db, Timer() as t:
            self.vector_index.clear()
            cleared_count = db.count()
        return {
            "cleared": True,
            "db_count": cleared_count,
            "trace_ms": round(t.ms, 2),
        }
