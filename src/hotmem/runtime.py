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
from collections.abc import Iterator
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
from hotmem.mount import StoreIdentityError, StoreLock
from hotmem.policy import (
    CallerContext,
    DisclosureReport,
    PolicyConfig,
    PolicyError,
    redact_structure,
    scan_and_redact_text,
    validate_request_tags,
)
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


def _iter_artifact_namespaces(path: Path) -> Iterator[str]:
    """Yield record namespaces from a JSONL/JSONL.GZ artifact (best effort).

    Malformed lines are ignored here; the hydrate/verify stage produces the
    authoritative structural error. Only the namespace is read, so the scan
    is bounded and never materializes whole records.
    """
    import gzip

    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(record, dict):
                yield str(record.get("namespace") or "")


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
        policy_path: str | None = None,
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
        self.policy_path = policy_path
        if store_id and self.policy.store_id and store_id != self.policy.store_id:
            raise StoreIdentityError(
                f"--store-id {store_id!r} contradicts the policy store_id "
                f"{self.policy.store_id!r}; refusing to start with two store identities"
            )
        self._requested_store_id: str | None = store_id or self.policy.store_id or None
        self.store_id: str = derive_store_id(self.db_path, self._requested_store_id)
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
        self._read_local = threading.local()
        self._read_connections: list[MemoryDB] = []
        self._read_connections_lock = threading.Lock()
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

            # Store identity is persisted inside the store and adopted on
            # subsequent opens, so moving/copying a store does not silently
            # change the id clients are pinned to.
            stored_store_id = self.db.get_meta("store_id")
            if stored_store_id:
                if self._requested_store_id and self._requested_store_id != stored_store_id:
                    raise StoreIdentityError(
                        f"configured store id {self._requested_store_id!r} does not "
                        f"match the store's persistent identity {stored_store_id!r}"
                    )
                self.store_id = stored_store_id
                if not self.policy.store_id:
                    self.policy.store_id = stored_store_id
            else:
                self.db.set_meta("store_id", self.store_id)
            self._store_lock.store_id = self.store_id
            self._store_lock.write_owner()

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
            with self._read_connections_lock:
                read_connections, self._read_connections = self._read_connections, []
                self._read_local = threading.local()
            for ro_db in read_connections:
                with contextlib.suppress(Exception):
                    ro_db.close()
            if self.vector_index is not None:
                with contextlib.suppress(Exception):
                    self.vector_index.close()
            if self.db is not None:
                with contextlib.suppress(Exception):
                    self.db.close()
            self.db = None
            self.vector_index = None
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

    def _confine_path(self, ctx: CallerContext, raw: str, *, kind: str) -> Path:
        """Resolve a caller-supplied path, confining daemon callers to the base dir."""
        path = Path(raw)
        if not ctx.policy_enforced:
            return path
        base = Path(self.base_dir).resolve()
        resolved = (base / path).resolve() if not path.is_absolute() else path.resolve()
        if resolved != base and base not in resolved.parents:
            raise PolicyError(
                403,
                "path_not_permitted",
                f"{kind} must be inside the daemon base directory ({base})",
            )
        return resolved

    def _guard_hydrate_scope(
        self,
        ctx: CallerContext,
        target: str,
        *,
        default_namespace: str = "",
    ) -> None:
        """Fail closed before importing records into namespaces the caller cannot write."""
        if not ctx.policy_enforced:
            return
        path = Path(target)
        if path.suffix.lower() in (".sqlite", ".db"):
            raise PolicyError(
                403,
                "unsupported_daemon_route",
                "SQLite hydration is not supported through the daemon boundary; "
                "use a JSONL, Snapshot v2, or interchange package artifact",
            )
        if path.is_file():
            artifacts = [path]
        elif path.is_dir():
            artifacts = sorted(
                p
                for p in path.iterdir()
                if p.is_file() and (p.name == "memories.jsonl" or p.name == "memories.jsonl.gz")
            )
            if not artifacts and not ctx.can_access_namespace(default_namespace, operation="write"):
                raise PolicyError(
                    403,
                    "scope_denied",
                    f"caller {ctx.caller_id!r} cannot hydrate bundle into "
                    f"namespace {default_namespace!r}",
                )
        else:
            return
        for artifact in artifacts:
            seen_any = False
            for namespace in _iter_artifact_namespaces(artifact):
                seen_any = True
                effective_ns = namespace if namespace else default_namespace
                if not ctx.can_access_namespace(effective_ns, operation="write"):
                    raise PolicyError(
                        403,
                        "scope_denied",
                        f"caller {ctx.caller_id!r} cannot hydrate records into "
                        f"namespace {effective_ns!r}",
                    )
            if not seen_any and not ctx.can_access_namespace(default_namespace, operation="write"):
                raise PolicyError(
                    403,
                    "scope_denied",
                    f"caller {ctx.caller_id!r} cannot hydrate records into "
                    f"namespace {default_namespace!r}",
                )

    @contextlib.contextmanager
    def _read_db(self):
        """Yield a connection for read-only operations.

        File-backed stores use a per-thread read-only SQLite connection so
        readers never observe uncommitted writes, never block behind a slow
        writer, and never interfere with the shared write connection.
        """
        primary = self._require_db()
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

        read_db = getattr(self._read_local, "db", None)
        if read_db is None:
            read_db = MemoryDB(self.db_path, read_only=True)
            self._read_local.db = read_db
            with self._read_connections_lock:
                self._read_connections.append(read_db)
        yield read_db

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
        harness_id: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
        envelope_version: str | None = None,
        caller_id_claim: str | None = None,
    ) -> CallerContext:
        """Resolve and authenticate caller context against this runtime's policy."""
        return self.policy.resolve_request(
            runtime_store_id=self.store_id,
            token=token,
            requested_store_id=requested_store_id,
            session_id=session_id,
            harness_id=harness_id,
            source=source,
            namespace=namespace,
            actor=actor,
            idempotency_key=idempotency_key,
            envelope_version=envelope_version,
            caller_id_claim=caller_id_claim,
        )

    def revoke_policy(
        self,
        ctx: CallerContext,
        *,
        caller_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Revoke a caller credential or a share approval at runtime (#110)."""
        ctx.require_operation("manage")
        if not caller_id and not approval_id:
            raise PolicyError(
                400,
                "invalid_request",
                "provide caller_id and/or approval_id to revoke",
            )
        revoked = 0
        if caller_id:
            revoked += self.policy.revoke_caller(caller_id)
        if approval_id:
            revoked += self.policy.revoke_share_approval(approval_id)
        return {
            "revoked": revoked,
            "caller_id": caller_id,
            "approval_id": approval_id,
            "policy_revision": self.policy.revision,
        }

    def reload_policy(self, ctx: CallerContext) -> dict[str, Any]:
        """Reload policy from the configured policy file (management only)."""
        ctx.require_operation("manage")
        if not self.policy_path:
            raise PolicyError(
                400,
                "no_policy_file",
                "this daemon was not started with a policy file; nothing to reload",
            )
        reloaded = PolicyConfig.load(self.policy_path)
        if reloaded.store_id and reloaded.store_id != self.store_id:
            raise PolicyError(
                409,
                "store_mismatch",
                f"policy file store_id {reloaded.store_id!r} does not match "
                f"active store {self.store_id!r}",
            )
        reloaded.store_id = reloaded.store_id or self.store_id
        reloaded.revision = self.policy.revision + 1
        self.policy = reloaded
        return {
            "reloaded": True,
            "policy_revision": reloaded.revision,
            "credential_count": len(reloaded.credentials),
        }

    # ── Operations ───────────────────────────────────────────────────────

    def health(self, ctx: CallerContext) -> dict[str, Any]:
        ctx.require_operation("read")
        descriptor = self.embedder.descriptor
        with self._read_db() as db:
            total_count = db.count()
            if not ctx.policy_enforced:
                return {
                    "status": "ok",
                    "memory_count": total_count,
                    "db_path": self.db_path,
                    "uptime_s": round(time.time() - self.start_time, 1),
                    "embedding": {"model": descriptor.key, "dim": descriptor.dimension},
                }

            visible_rows = ctx.filter_rows(db.all_rows(include_embedding=False), operation="read")
            if ctx.policy_enforced:
                disclosed_visible = []
                for r in visible_rows:
                    try:
                        if ctx.apply_disclosure_to_row(r, operation="read") is not None:
                            disclosed_visible.append(r)
                    except PolicyError:
                        continue
                visible_rows = disclosed_visible
            visible_count = len(visible_rows)
        if total_count == 0:
            store_state = "verified_empty"
        elif visible_count == 0:
            # The store is not empty; this caller simply has no visible rows.
            store_state = "empty_scope"
        else:
            store_state = "ready"
        return {
            "status": "ok",
            "memory_count": visible_count,
            # Absolute paths are not disclosed to scoped daemon callers.
            "db_path": None,
            "uptime_s": round(time.time() - self.start_time, 1),
            "embedding": {"model": descriptor.key, "dim": descriptor.dimension},
            "store_id": self.store_id,
            "store_state": store_state,
            "total_memory_count": (
                total_count if ctx.credential is not None and ctx.credential.is_management else 0
            ),
            "runtime_generation": self.runtime_generation,
            "policy_enforced": True,
            "policy_revision": self.policy.revision,
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
        tags: Any = None,
        harness_id: str | None = None,
        session_id: str | None = None,
        envelope_version: str | None = None,
        caller_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_db()
        ctx.require_operation("write")
        target_namespace = ctx.effective_write_namespace
        effective_session, effective_harness, effective_version = (
            self.policy.validate_write_session_and_harness(
                ctx,
                session_id=session_id,
                harness_id=harness_id,
                envelope_version=envelope_version,
                caller_id_claim=caller_id,
            )
        )
        if ctx.policy_enforced and not effective_session:
            raise PolicyError(
                400,
                "session_required",
                "policy-enforced writes require an explicit X-HotMem-Session-Id "
                "session attribution envelope (#111)",
            )
        # Reject source attribution the credential is not entitled to claim.
        self.policy.validate_write_source(ctx, source)
        effective_source = ctx.source or (source.strip() if isinstance(source, str) else "")

        if metadata is not None and not isinstance(metadata, dict):
            raise PolicyError(
                400,
                "invalid_metadata",
                "metadata must be a JSON object",
            )
        meta_dict = metadata or {}
        validate_metadata(meta_dict)
        tag_list = validate_request_tags(tags)
        tags_json = json.dumps(tag_list)

        envelope = ctx.build_session_envelope(
            namespace=target_namespace,
            request_source=effective_source,
            request_harness_id=effective_harness,
            request_session_id=effective_session,
            request_envelope_version=effective_version,
        )
        provenance_dict = envelope.to_provenance_dict() if envelope is not None else None
        provenance_json = json.dumps(provenance_dict, sort_keys=True) if provenance_dict else None

        from hotmem.events import _build_memory_created_payload

        req_canonical_dict: dict[str, Any] = {
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
        }
        if tag_list or tags is not None:
            req_canonical_dict["tags"] = tag_list
        if effective_harness:
            req_canonical_dict["harness_id"] = effective_harness
        if session_id is not None:
            req_canonical_dict["session_id"] = effective_session
        req_canonical = json.dumps(
            req_canonical_dict,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(req_canonical.encode("utf-8")).hexdigest()

        with self._write_slot() as db, Timer() as t:
            deduplicated = False
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
                if ctx.policy_enforced:
                    self._confine_path(ctx, file_uri, kind="backing file")
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
                    tags=tag_list,
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
                    # Content identity already exists in this namespace: do not
                    # silently drop the write attempt. Record an explicit
                    # deduplication event carrying this caller's attribution,
                    # and tell the caller the write was deduplicated.
                    memory_id = str(existing_row["id"])
                    deduplicated = True
                    dedup_payload: dict[str, Any] = {
                        "id": memory_id,
                        "identifier": identifier,
                        "content_hash": content_hash,
                        "caller_id": ctx.caller_id,
                        "harness_id": effective_harness,
                        "session_id": effective_session,
                        "source": effective_source,
                        "tags": tag_list,
                    }
                    if meta_dict:
                        dedup_payload["metadata_json"] = json.dumps(meta_dict)
                    if provenance_json:
                        dedup_payload["provenance_json"] = provenance_json
                    append_event(
                        db,
                        event_type=EventType.MEMORY_DEDUPLICATED,
                        memory_id=memory_id,
                        namespace=target_namespace,
                        payload=dedup_payload,
                        _commit=not bool(ctx.idempotency_key),
                    )
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
                        tags=tags_json,
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

        response = {
            "memory_id": memory_id,
            "content_hash": content_hash,
            "trace_ms": round(t.ms, 2),
        }
        if deduplicated:
            # Additive signal only on the duplicate-write path: the write was
            # accepted as an idempotent hit on existing content identity.
            response["deduplicated"] = True
        return response

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

        def _run_search(vector_index):
            run_report = DisclosureReport() if ctx.policy_enforced else None
            run_transform = (
                (lambda row: ctx.apply_disclosure_to_row(row, report=run_report, operation="read"))
                if ctx.policy_enforced
                else None
            )
            with self._read_db() as db, Timer() as run_timer:
                run_messages = search_memories(
                    db,
                    query=query,
                    top_k=top_k,
                    max_chars=max_chars,
                    vector_index=vector_index,
                    embedder=self.embedder,
                    reranker=self.reranker,
                    row_filter=row_filter,
                    row_transform=run_transform,
                )
            return run_messages, run_report, run_timer

        messages, report, t = _run_search(self.vector_index)
        if (
            ctx.policy_enforced
            and len(messages) < top_k
            and self.vector_index is not None
            and not isinstance(self.vector_index, NullVectorIndex)
        ):
            # Disclosure omissions can consume the whole ANN oversample
            # window. Re-run the exact scoped scan so eligible rows outside
            # that window are never starved.
            messages, report, t = _run_search(None)
        result: dict[str, Any] = {
            "memories": messages,
            "count": len(messages),
            "trace_ms": round(t.ms, 2),
        }
        if ctx.policy_enforced and report is not None:
            result["disclosure"] = report.to_dict()
            result["policy_revision"] = self.policy.revision
        return result

    def _apply_hydrated_dict_disclosure(
        self,
        ctx: CallerContext,
        record: dict[str, Any],
        hydrated: dict[str, Any],
        report: DisclosureReport,
    ) -> dict[str, Any] | None:
        """Apply disclosure policy and host-path sanitization to a profiled hydration dict."""
        import base64

        out = dict(hydrated)
        raw_uri = str(record.get("source_uri") or "")
        if "source_uri" in out:
            sanitized_uri = ctx.sanitize_source_uri(out.get("source_uri"), base_dir=self.base_dir)
            out["source_uri"] = sanitized_uri
            if raw_uri and raw_uri != sanitized_uri and isinstance(out.get("warnings"), list):
                out["warnings"] = [
                    str(w).replace(raw_uri, sanitized_uri or "<redacted>") for w in out["warnings"]
                ]
        if not ctx.policy_enforced or ctx.credential is None:
            return out

        secret_action, _ = ctx.effective_disclosure_actions(record, operation="read")
        if secret_action == "allow":
            return out

        row_id = str(record.get("id") or "")
        identifier = str(record.get("identifier") or "")
        field_hits: list[tuple[str, str, int]] = []
        for field_name in ("fact_text", "fact_summary", "summary"):
            val = out.get(field_name)
            if isinstance(val, str) and val:
                redacted_val, hits = scan_and_redact_text(val)
                if hits:
                    out[field_name] = redacted_val
                    for pat_name, cnt in hits:
                        field_hits.append((field_name, pat_name, cnt))

        content_val = out.get("content")
        if isinstance(content_val, str) and content_val:
            if out.get("content_encoding") == "base64":
                raw_bytes = base64.b64decode(content_val)
                try:
                    text_val = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as err:
                    raise PolicyError(
                        403,
                        "policy_disclosure_blocked",
                        f"policy cannot redact non-text payload for memory {row_id!r}",
                        detail={"reason": "non_text_payload", "memory_id": row_id},
                    ) from err
                redacted_text, hits = scan_and_redact_text(text_val)
                if hits:
                    out["content"] = base64.b64encode(redacted_text.encode("utf-8")).decode("ascii")
                    for pat_name, cnt in hits:
                        field_hits.append(("content", pat_name, cnt))
            else:
                redacted_val, hits = scan_and_redact_text(content_val)
                if hits:
                    out["content"] = redacted_val
                    for pat_name, cnt in hits:
                        field_hits.append(("content", pat_name, cnt))

        if not field_hits:
            return out

        matched_patterns = sorted({pat for _, pat, _ in field_hits})
        if secret_action == "fail_closed":
            raise PolicyError(
                403,
                "policy_disclosure_blocked",
                f"policy blocked disclosure of secret/PII pattern(s) in memory {row_id!r}",
                detail={
                    "reason": "secret_detected",
                    "patterns": matched_patterns,
                    "memory_id": row_id,
                },
            )
        if secret_action == "omit":
            report.record_omission(
                memory_id=row_id,
                identifier=identifier,
                reason="policy_secret_omitted",
                patterns=matched_patterns,
            )
            return None

        report.redacted_records += 1
        for field_name, pat_name, cnt in field_hits:
            report.record_redaction(
                memory_id=row_id,
                field_name=field_name,
                pattern=pat_name,
                count=cnt,
            )
        return out

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
        report = DisclosureReport() if ctx.policy_enforced else None
        row_transform = (
            (lambda row: ctx.apply_disclosure_to_row(row, report=report, operation="read"))
            if ctx.policy_enforced
            else None
        )
        with self._read_db() as db, Timer() as t:
            rows = db.list_by_identifier(
                identifier,
                order=order,
                limit=limit,
                row_filter=row_filter,
                row_transform=row_transform,
            )
        result: dict[str, Any] = {
            "memories": rows,
            "count": len(rows),
            "trace_ms": round(t.ms, 2),
        }
        if ctx.policy_enforced and report is not None:
            result["disclosure"] = report.to_dict()
            result["policy_revision"] = self.policy.revision
        return result

    def get_memory(self, ctx: CallerContext, memory_id: str) -> dict[str, Any] | None:
        ctx.require_operation("read")
        with self._read_db() as db:
            record = get_memory_metadata(db, memory_id)
        if record is None:
            return None
        if not ctx.can_access_row(record, operation="read"):
            # Out-of-scope rows are reported as missing (no existence oracle).
            return None
        report: DisclosureReport | None = None
        if ctx.policy_enforced:
            report = DisclosureReport()
            disclosed = ctx.apply_disclosure_to_row(record, report=report, operation="read")
            if disclosed is None:
                return None
            record = disclosed
        raw_tags = record.get("tags")
        parsed_tags = (
            _safe_json(raw_tags, [])
            if isinstance(raw_tags, str)
            else ([str(t) for t in raw_tags] if isinstance(raw_tags, list) else [])
        )
        out = {
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
            "tags": parsed_tags,
            "content_hash": record["content_hash"],
            "source_uri": ctx.sanitize_source_uri(record.get("source_uri"), base_dir=self.base_dir),
            "byte_offset": record["byte_offset"],
            "byte_length": record["byte_length"],
            "source_checksum": record["source_checksum"],
            "source_format": record["source_format"],
            "provenance": _safe_json(record.get("provenance_json"), None),
            "created_at": record["created_at"],
        }
        if report is not None:
            out["disclosure"] = report.to_dict()
            out["policy_revision"] = self.policy.revision
        return out

    def hydrate_one_profiled(
        self,
        ctx: CallerContext,
        memory_id: str,
        *,
        profile: HydrationProfile = "agent",
        verify: bool = True,
    ) -> dict[str, Any]:
        ctx.require_operation("read")
        report = DisclosureReport()
        with self._read_db() as db:
            record = get_memory_metadata(db, memory_id)
            if record is None or not ctx.can_access_row(record, operation="read"):
                # Out-of-scope ids are indistinguishable from missing ids.
                raise KeyError(memory_id)
            if ctx.policy_enforced:
                if record.get("source_uri"):
                    self._confine_path(ctx, str(record["source_uri"]), kind="backing file")
                # Check classification & metadata before reading backing file
                meta_probe = {**record, "fact_text": "", "fact_summary": ""}
                if ctx.apply_disclosure_to_row(meta_probe, report=report, operation="read") is None:
                    raise KeyError(memory_id)
            with Timer() as t:
                ph = hydrate_with_profile(
                    db,
                    memory_id,
                    profile=profile,
                    base_dir=self.base_dir,
                    verify=verify,
                )
        d = ph.to_dict()
        disclosed_d = self._apply_hydrated_dict_disclosure(ctx, record, d, report)
        if disclosed_d is None:
            raise KeyError(memory_id)
        disclosed_d["trace_ms"] = round(t.ms, 2)
        if ctx.policy_enforced:
            disclosed_d["disclosure"] = report.to_dict()
        return disclosed_d

    def hydrate_one_raw(self, ctx: CallerContext, memory_id: str) -> tuple[bytes, dict[str, str]]:
        ctx.require_operation("read")
        with self._read_db() as db:
            record = get_memory_metadata(db, memory_id)
            if record is None or not ctx.can_access_row(record, operation="read"):
                raise KeyError(memory_id)
            if ctx.policy_enforced:
                if record.get("source_uri"):
                    self._confine_path(ctx, str(record["source_uri"]), kind="backing file")
                meta_probe = {**record, "fact_text": "", "fact_summary": ""}
                if ctx.apply_disclosure_to_row(meta_probe, operation="read") is None:
                    raise KeyError(memory_id)
            with Timer() as t:
                payload = hydrate_memory(db, memory_id, base_dir=self.base_dir)
            verified = bool(record["source_checksum"])
            source_format = record.get("source_format", "") or ""
        if ctx.policy_enforced:
            secret_action, _ = ctx.effective_disclosure_actions(record, operation="read")
            if secret_action != "allow":
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError as err:
                    raise PolicyError(
                        403,
                        "policy_disclosure_blocked",
                        f"policy cannot redact non-text payload for memory {memory_id!r}",
                        detail={"reason": "non_text_payload", "memory_id": memory_id},
                    ) from err
                redacted_text, hits = scan_and_redact_text(text)
                if hits:
                    matched_patterns = sorted({pat for pat, _ in hits})
                    if secret_action == "fail_closed":
                        raise PolicyError(
                            403,
                            "policy_disclosure_blocked",
                            f"policy blocked disclosure of secret/PII pattern(s) "
                            f"in memory {memory_id!r}",
                            detail={
                                "reason": "secret_detected",
                                "patterns": matched_patterns,
                                "memory_id": memory_id,
                            },
                        )
                    if secret_action == "omit":
                        raise KeyError(memory_id)
                    payload = redacted_text.encode("utf-8")
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
        ctx.require_operation("read")
        report = DisclosureReport()
        with self._read_db() as db, Timer() as t:
            results = []
            for mid in memory_ids:
                record = get_memory_metadata(db, mid)
                if record is None or not ctx.can_access_row(record, operation="read"):
                    # Do not distinguish "exists in another scope" from missing.
                    results.append({"memory_id": mid, "error": "not_found"})
                    continue
                if ctx.policy_enforced:
                    if record.get("source_uri"):
                        self._confine_path(ctx, str(record["source_uri"]), kind="backing file")
                    meta_probe = {**record, "fact_text": "", "fact_summary": ""}
                    if (
                        ctx.apply_disclosure_to_row(meta_probe, report=report, operation="read")
                        is None
                    ):
                        results.append({"memory_id": mid, "error": "not_found"})
                        continue
                try:
                    ph = hydrate_with_profile(
                        db,
                        mid,
                        profile=profile,
                        base_dir=self.base_dir,
                        verify=verify,
                    )
                    disclosed_d = self._apply_hydrated_dict_disclosure(
                        ctx, record, ph.to_dict(), report
                    )
                    if disclosed_d is None:
                        results.append({"memory_id": mid, "error": "not_found"})
                    else:
                        results.append(disclosed_d)
                except KeyError:
                    results.append({"memory_id": mid, "error": "not_found"})
                except ProvenanceError as err:
                    results.append(
                        {
                            "memory_id": mid,
                            "error": "provenance_mismatch",
                            "reason": err.reason,
                            "source_uri": ctx.sanitize_source_uri(
                                err.source_uri, base_dir=self.base_dir
                            ),
                        }
                    )
                except ValueError as err:
                    results.append(
                        {"memory_id": mid, "error": "invalid_profile", "message": str(err)}
                    )
        out: dict[str, Any] = {
            "results": results,
            "count": len(results),
            "trace_ms": round(t.ms, 2),
        }
        if ctx.policy_enforced:
            out["disclosure"] = report.to_dict()
        return out

    def hydrate_snapshot(self, ctx: CallerContext, *, target: str | None = None) -> dict[str, Any]:
        ctx.require_operation("hydrate")
        resolved_target = str(
            self._confine_path(ctx, target or self.swap_path or "swap.jsonl", kind="hydrate target")
        )
        if ctx.policy_enforced and Path(resolved_target).is_dir():
            # Integrity first: a corrupt artifact must fail verification (409)
            # before scope processing, and long before any write.
            from hotmem.snapshot import detect_format as _detect_snapshot_format
            from hotmem.snapshot import verify as _verify_snapshot

            if _detect_snapshot_format(resolved_target) in ("v2", "package"):
                _verify_snapshot(resolved_target)
        target_ns = ctx.hydrate_target_namespace()
        # Validate every imported namespace against the caller's write scope
        # before any record is written (fail closed, no partial import).
        self._guard_hydrate_scope(ctx, resolved_target, default_namespace=target_ns)
        with self._write_slot() as db:
            result = snapshot_hydrate(
                db,
                resolved_target,
                embedder=self.embedder,
                target_namespace=target_ns or None,
            )
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
            "path": ctx.sanitize_source_uri(resolved_target, base_dir=self.base_dir),
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
        if ctx.policy_enforced and copy_attachments and not ctx.allow_attachments_enabled:
            raise PolicyError(
                403,
                "attachments_not_permitted",
                f"caller {ctx.caller_id!r} is not permitted to export raw file attachments",
            )
        resolved_target = str(
            self._confine_path(
                ctx, target or self.swap_path or "swap.jsonl", kind="snapshot target"
            )
        )
        row_filter = (
            (
                lambda row: (
                    ctx.can_access_row(row, operation="export")
                    and ctx.can_access_row(row, operation="read")
                )
            )
            if (ctx.policy_enforced or ctx.namespace is not None)
            else None
        )

        def _prepare_export_row(
            row: dict[str, Any], row_report: DisclosureReport | None = None
        ) -> dict[str, Any] | None:
            if not ctx.policy_enforced:
                return row
            if row.get("memory_type") == "file" and not ctx.sanitize_source_uri(
                row.get("source_uri"), base_dir=self.base_dir
            ):
                # A file-backed record with no expressible portable reference
                # would hydrate as invalid; omit it instead of emitting a
                # dangling pointer (portable-cassette guarantee).
                if row_report is not None:
                    row_report.record_omission(
                        memory_id=str(row.get("id") or ""),
                        identifier=str(row.get("identifier") or ""),
                        reason="file_reference_unavailable",
                    )
                return None
            disclosed = ctx.apply_disclosure_to_row(row, report=row_report, operation="export")
            if disclosed is None:
                return None
            fact_text_changed = disclosed.get("fact_text") != row.get("fact_text")
            summary_changed = disclosed.get("fact_summary") != row.get("fact_summary")
            if fact_text_changed and disclosed.get("memory_type") != "file":
                disclosed["content_hash"] = compute_content_hash(
                    str(disclosed.get("identifier") or ""),
                    str(disclosed.get("fact_text") or ""),
                )
            embed_input_changed = fact_text_changed or (
                disclosed.get("memory_type") == "file" and summary_changed
            )
            if embed_input_changed and disclosed.get("embedding") is not None:
                embed_text = str(disclosed.get("fact_text") or disclosed.get("fact_summary") or "")
                if embed_text:
                    disclosed["embedding"] = pack_embedding(self.embedder.embed(embed_text))
            if ctx.redact_host_paths_enabled and disclosed.get("source_uri"):
                disclosed["source_uri"] = ctx.sanitize_source_uri(
                    disclosed.get("source_uri"), base_dir=self.base_dir
                )
            return disclosed

        export_scope: dict[str, Any] | None = None
        report: DisclosureReport | None = None
        with self._write_slot() as db:
            if ctx.policy_enforced:
                # Count within the same write slot as the export so scope and
                # disclosure counts describe the emitted snapshot.
                report = DisclosureReport()
                exported_after_disclosure = 0
                scope_omitted = 0
                total_count = db.count()
                for row in db.iter_rows(include_embedding=False):
                    if row_filter is not None and not row_filter(row):
                        scope_omitted += 1
                        continue
                    if copy_attachments and row.get("source_uri"):
                        self._confine_path(ctx, str(row["source_uri"]), kind="backing file")
                    disclosed_probe = _prepare_export_row(row, report)
                    if disclosed_probe is not None:
                        exported_after_disclosure += 1
                cred = ctx.credential
                export_scope = {
                    "caller_id": ctx.caller_id,
                    "session_id": ctx.session_id,
                    "source": ctx.source,
                    "namespace": ctx.namespace,
                    "permitted_namespaces": list(cred.namespaces) if cred else [],
                    "share_approvals": [
                        s.approval_id
                        for s in (cred.share_approvals if cred else ())
                        if not s.revoked
                    ],
                    "exported_count": exported_after_disclosure,
                    "scope_omitted_count": scope_omitted,
                    "disclosure_omitted_count": report.omitted_count,
                    "omitted_count": max(0, total_count - exported_after_disclosure),
                    "redacted_count": report.redacted_count,
                    "disclosure": report.to_dict(),
                    "store_id": self.store_id,
                }
            result = snapshot_write(
                db,
                resolved_target,
                copy_attachments=copy_attachments,
                base_dir=self.base_dir,
                package=package,
                gz=gz,
                row_filter=row_filter,
                row_transform=_prepare_export_row if ctx.policy_enforced else None,
                redact_host_paths=ctx.redact_host_paths_enabled,
                export_scope=export_scope,
            )
        out: dict[str, Any] = {
            "exported": result.exported,
            "path": ctx.sanitize_source_uri(result.path, base_dir=self.base_dir),
        }
        if ctx.policy_enforced and report is not None:
            out["disclosure"] = report.to_dict()
            out["policy_revision"] = self.policy.revision
        return out

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
        resolved_source = str(self._confine_path(ctx, source, kind="handoff source"))
        resolved_output = str(self._confine_path(ctx, output, kind="handoff output"))
        result = prepare_handoff(
            resolved_source,
            resolved_output,
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
            "path": ctx.sanitize_source_uri(result.path, base_dir=self.base_dir),
            "timings_ms": result.timings_ms,
        }

    def handoff_verify(self, ctx: CallerContext, *, package: str) -> dict[str, Any]:
        ctx.require_operation("read")
        verified = verify_handoff(str(self._confine_path(ctx, package, kind="handoff package")))
        return {
            "valid": True,
            "package_id": verified.manifest["package_id"],
            "mode": verified.manifest["mode"],
            "entries": verified.entry_count,
            "memories": verified.memory_count,
        }

    def handoff_inspect(self, ctx: CallerContext, *, package: str) -> dict[str, Any]:
        ctx.require_operation("read")
        report = inspect_handoff(str(self._confine_path(ctx, package, kind="handoff package")))
        if isinstance(report, dict) and "path" in report:
            report["path"] = ctx.sanitize_source_uri(report.get("path"), base_dir=self.base_dir)
        return report

    def handoff_hydrate(self, ctx: CallerContext, *, package: str) -> dict[str, Any]:
        ctx.require_operation("hydrate")
        resolved_package = str(self._confine_path(ctx, package, kind="handoff package"))
        target_ns: str | None = ctx.namespace
        if ctx.policy_enforced:
            # Integrity before scope checks (and before any write).
            verify_handoff(resolved_package)
            target_ns = ctx.hydrate_target_namespace()
        self._guard_hydrate_scope(
            ctx,
            resolved_package,
            default_namespace=target_ns or "",
        )
        with self._write_slot() as db:
            result = hydrate_handoff(
                db,
                resolved_package,
                embedder=self.embedder,
                target_namespace=target_ns,
            )
        return {
            "handoff_id": result.handoff_id,
            "package_id": result.package_id,
            "loaded": result.loaded,
            "skipped": result.skipped_dupes,
            "invalid": result.invalid,
            "already_applied": result.already_applied,
            "brief": result.brief_identifier,
            "brief_accessible": result.brief_accessible,
            **result.disposition(),
        }

    def list_files(self, ctx: CallerContext, *, identifier: str | None = None) -> dict[str, Any]:
        ctx.require_operation("read")
        report = DisclosureReport() if ctx.policy_enforced else None
        with self._read_db() as db, Timer() as t:
            rows = ctx.filter_rows(db.list_file_backed(), operation="read")
            if identifier:
                rows = [r for r in rows if r["identifier"] == identifier]
            files = []
            for row in rows:
                if ctx.policy_enforced and report is not None:
                    disclosed = ctx.apply_disclosure_to_row(row, report=report, operation="read")
                    if disclosed is None:
                        continue
                    row = disclosed
                source_uri = row.get("source_uri") or ""
                display_uri = ctx.sanitize_source_uri(source_uri, base_dir=self.base_dir)
                exists = False
                size = row.get("byte_length") or 0
                stat_uri = display_uri
                if stat_uri and "://" not in stat_uri and not Path(stat_uri).is_absolute():
                    stat_uri = str(Path(self.base_dir) / stat_uri)
                if stat_uri:
                    try:
                        adapter = get_adapter(stat_uri)
                        exists = adapter.exists(stat_uri)
                        if exists:
                            meta = adapter.metadata(stat_uri)
                            size = meta["size"]
                    except Exception:
                        pass
                files.append(
                    {
                        "memory_id": row["id"],
                        "identifier": row["identifier"],
                        "source_uri": display_uri,
                        "byte_offset": row.get("byte_offset") or 0,
                        "byte_length": row.get("byte_length") or 0,
                        "source_format": row.get("source_format") or "",
                        "source_checksum": row.get("source_checksum") or None,
                        "exists": exists,
                        "size": size,
                    }
                )
        out: dict[str, Any] = {
            "files": files,
            "count": len(files),
            "trace_ms": round(t.ms, 2),
        }
        if report is not None:
            out["disclosure"] = report.to_dict()
            out["policy_revision"] = self.policy.revision
        return out

    def list_bundles(self, ctx: CallerContext) -> dict[str, Any]:
        # Bundle index entries are store-wide filesystem paths, not
        # namespace-scoped records: only management authority may list them.
        if ctx.policy_enforced:
            ctx.require_operation("manage")
        with self._read_db() as db, Timer() as t:
            rows = db.list_bundle_index()
        if ctx.redact_host_paths_enabled:
            rows = [
                {
                    **row,
                    **{
                        key: ctx.sanitize_source_uri(row.get(key), base_dir=self.base_dir)
                        for key in ("path", "primary_file")
                        if key in row
                    },
                }
                for row in rows
            ]
        return {"bundles": rows, "count": len(rows), "trace_ms": round(t.ms, 2)}

    def discover(self, ctx: CallerContext, *, root: str, max_depth: int = 10) -> dict[str, Any]:
        if ctx.policy_enforced:
            ctx.require_operation("manage")
        self._confine_path(ctx, root, kind="discover root")
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

        def _hygiene_filter(row: dict[str, Any]) -> bool:
            if not ctx.can_access_row(row, operation="read"):
                return False
            if ctx.policy_enforced:
                try:
                    if ctx.apply_disclosure_to_row(row, operation="read") is None:
                        return False
                except PolicyError:
                    return False
            return True

        row_filter = _hygiene_filter if (ctx.policy_enforced or ctx.namespace is not None) else None
        with self._read_db() as db:
            report = check_hygiene(db, base_dir=self.base_dir, row_filter=row_filter)
        counts = {
            "warning_count": len(report.warnings),
            "error_count": sum(1 for w in report.warnings if w.severity == "error"),
            "warn_count": sum(1 for w in report.warnings if w.severity == "warn"),
            "info_count": sum(1 for w in report.warnings if w.severity == "info"),
        }
        # A GET must not mutate on behalf of a read-only caller. Only callers
        # with write authority record the audit events (and may trim).
        can_write = not ctx.policy_enforced or (
            ctx.credential is not None and "write" in ctx.credential.operations
        )
        if can_write:
            with self._write_slot() as db:
                append_event(
                    db,
                    event_type=EventType.HYGIENE_CHECKED,
                    payload=counts,
                )
                for w in report.warnings:
                    if w.severity != "error":
                        continue
                    raw_uri = w.detail.get("source_uri")
                    sanitized_uri = (
                        ctx.sanitize_source_uri(raw_uri, base_dir=self.base_dir)
                        if raw_uri is not None
                        else None
                    )
                    msg = w.message
                    if raw_uri and sanitized_uri is not None and raw_uri != sanitized_uri:
                        msg = msg.replace(str(raw_uri), sanitized_uri or "<redacted>")
                    append_event(
                        db,
                        event_type=EventType.HYGIENE_WARNING,
                        memory_id=w.detail.get("memory_id"),
                        payload={
                            "category": w.category,
                            "severity": w.severity,
                            "message": msg,
                            "source_uri": sanitized_uri,
                            "identifier": w.detail.get("identifier"),
                        },
                    )
                with contextlib.suppress(Exception):
                    db.trim_events_by_count(10_000)
        out = report.to_dict()
        if ctx.redact_host_paths_enabled and isinstance(out.get("warnings"), list):
            sanitized_warnings = []
            for w_dict in out["warnings"]:
                w_copy = dict(w_dict)
                detail = dict(w_copy.get("detail") or {})
                raw_uri = detail.get("source_uri")
                if raw_uri:
                    san_uri = ctx.sanitize_source_uri(raw_uri, base_dir=self.base_dir)
                    detail["source_uri"] = san_uri
                    w_copy["detail"] = detail
                    if isinstance(w_copy.get("message"), str) and raw_uri != san_uri:
                        w_copy["message"] = w_copy["message"].replace(
                            str(raw_uri), san_uri or "<redacted>"
                        )
                sanitized_warnings.append(w_copy)
            out["warnings"] = sanitized_warnings
        return out

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
        if namespace is not None and not ctx.can_access_namespace(namespace, operation="read"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {ctx.caller_id!r} cannot read events in namespace {namespace!r}",
            )
        with self._read_db() as db, Timer() as t:
            if not ctx.policy_enforced:
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
                return {
                    "events": listing["events"],
                    "count": listing["count"],
                    "next_seq": listing["next_seq"],
                    "trace_ms": round(t.ms, 2),
                }

            # Scan forward past events the caller cannot see. The cursor must
            # advance over *scanned* events (not just visible ones), otherwise
            # a page full of another scope's events would look like the end of
            # the stream and visible events after it would never be returned.
            filtered_events: list[dict[str, Any]] = []
            scan_after = after_seq
            scan_before = before_seq
            last_scanned_seq: int | None = None
            max_scan_pages = 50
            for _ in range(max_scan_pages):
                listing = query_events(
                    db,
                    memory_id=memory_id,
                    namespace=namespace,
                    event_type=event_type,
                    since=since,
                    until=until,
                    after_seq=scan_after,
                    before_seq=scan_before,
                    limit=limit,
                    ascending=(order == "asc"),
                )
                page = listing["events"]
                if not page:
                    break
                page_ids = [
                    str(ev.get("memory_id") or (ev.get("payload") or {}).get("id") or "")
                    for ev in page
                ]
                page_meta = db.get_memories(page_ids)
                for ev in page:
                    last_scanned_seq = int(ev["seq"])
                    ev_ns = str(ev.get("namespace") or "")
                    payload = dict(ev.get("payload") or {})
                    row_probe = {
                        "id": ev.get("memory_id") or payload.get("id") or "",
                        "identifier": payload.get("identifier") or "",
                        "namespace": ev_ns or str(payload.get("namespace") or ""),
                    }
                    if not ctx.can_access_row(row_probe, operation="read"):
                        continue
                    ev_mid = ev.get("memory_id") or payload.get("id")
                    mem_row = page_meta.get(str(ev_mid)) if ev_mid else None
                    payload_tags_raw = payload.get("tags")
                    payload_tags_str = (
                        json.dumps(payload_tags_raw)
                        if isinstance(payload_tags_raw, list)
                        else (payload_tags_raw or "[]")
                    )
                    payload_probe = {
                        **row_probe,
                        "metadata_json": payload.get("metadata_json") or "{}",
                        "tags": payload_tags_str,
                        "provenance_json": payload.get("provenance_json"),
                    }
                    probe = mem_row if mem_row is not None else payload_probe
                    disclosed_probe = ctx.apply_disclosure_to_row(probe, operation="read")
                    if disclosed_probe is None:
                        continue
                    disclosed_payload_probe = (
                        ctx.apply_disclosure_to_row(payload_probe, operation="read")
                        if mem_row is not None
                        else disclosed_probe
                    )
                    if disclosed_payload_probe is None:
                        continue
                    # Strip raw embedding blobs and full fact_text from the
                    # daemon activity stream.
                    sanitized_payload = dict(payload)
                    sanitized_payload.pop("embedding_b64", None)
                    sanitized_payload.pop("fact_text", None)
                    probe_for_payload = (
                        disclosed_payload_probe
                        if ev.get("event_type") == EventType.MEMORY_DEDUPLICATED
                        else disclosed_probe
                    )
                    for key in (
                        "identifier",
                        "metadata_json",
                        "provenance_json",
                        "tags",
                        "fact_summary",
                        "parent_memory",
                        "related_memories",
                    ):
                        if key in sanitized_payload and key in probe_for_payload:
                            val = probe_for_payload[key]
                            if (
                                key == "tags"
                                and isinstance(payload.get("tags"), list)
                                and isinstance(val, str)
                            ):
                                val = _safe_json(val, [])
                            sanitized_payload[key] = val
                    secret_action, _ = ctx.effective_disclosure_actions(probe, operation="read")
                    if secret_action != "allow":
                        redacted_payload, hits = redact_structure(sanitized_payload)
                        if hits:
                            patterns = sorted({p for p, _ in hits})
                            if secret_action == "fail_closed":
                                raise PolicyError(
                                    403,
                                    "policy_disclosure_blocked",
                                    "policy blocked disclosure of secret/PII "
                                    f"pattern(s) in event {ev.get('seq')!r}",
                                    detail={"reason": "secret_detected", "patterns": patterns},
                                )
                            if secret_action == "omit":
                                continue
                            sanitized_payload = redacted_payload
                    for path_key in ("source_uri", "path", "root"):
                        if path_key in sanitized_payload and ctx.redact_host_paths_enabled:
                            sanitized_payload[path_key] = ctx.sanitize_source_uri(
                                sanitized_payload.get(path_key), base_dir=self.base_dir
                            )
                    filtered_events.append({**ev, "payload": sanitized_payload})
                if len(filtered_events) >= limit or len(page) < limit:
                    break
                if order == "asc":
                    scan_after = last_scanned_seq
                else:
                    scan_before = last_scanned_seq
            if len(filtered_events) > limit:
                filtered_events = filtered_events[:limit]
            next_seq = last_scanned_seq
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
        ctx.require_operation("promote")
        with self._write_slot() as db:
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
            with Timer() as t:
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
        ctx.require_operation("promote")
        with self._write_slot() as db:
            record = get_memory_metadata(db, memory_id)
            if record is None:
                raise KeyError(memory_id)
            if not ctx.can_access_row(record, operation="promote"):
                raise PolicyError(
                    403,
                    "scope_denied",
                    f"caller {ctx.caller_id!r} cannot modify candidate state for {memory_id!r}",
                )
            with Timer() as t:
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
        ctx.require_operation("read")
        with self._read_db() as db, Timer() as t:
            rows = ctx.filter_rows(
                list_candidates(db, namespace=namespace, state=state), operation="read"
            )
        report = DisclosureReport() if ctx.policy_enforced else None
        if ctx.policy_enforced and report is not None:
            disclosed_rows: list[dict[str, Any]] = []
            for r in rows:
                d = ctx.apply_disclosure_to_row(r, report=report, operation="read")
                if d is not None:
                    disclosed_rows.append(d)
            rows = disclosed_rows
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
        out: dict[str, Any] = {
            "candidates": candidates,
            "count": len(candidates),
            "trace_ms": round(t.ms, 2),
        }
        if report is not None:
            out["disclosure"] = report.to_dict()
            out["policy_revision"] = self.policy.revision
        return out

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
        ctx.require_operation("read")
        with self._read_db() as db, Timer() as t:
            rows = ctx.filter_rows(list_by_state(db, state, namespace=namespace), operation="read")
        report = DisclosureReport() if ctx.policy_enforced else None
        if ctx.policy_enforced and report is not None:
            disclosed_rows = []
            for r in rows:
                d = ctx.apply_disclosure_to_row(r, report=report, operation="read")
                if d is not None:
                    disclosed_rows.append(d)
            rows = disclosed_rows
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
        out: dict[str, Any] = {
            "memories": summaries,
            "count": len(summaries),
            "trace_ms": round(t.ms, 2),
        }
        if report is not None:
            out["disclosure"] = report.to_dict()
            out["policy_revision"] = self.policy.revision
        return out

    def vector_index_rebuild(self, ctx: CallerContext) -> tuple[int, dict[str, Any]]:
        self._require_db()
        if ctx.policy_enforced:
            ctx.require_operation("manage")
        else:
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
            status = dict(self.vector_index.status(db))
            if ctx.policy_enforced and not (
                ctx.credential is not None and ctx.credential.is_management
            ):
                visible = ctx.filter_rows(db.all_rows(include_embedding=False), operation="read")
                disclosed_visible = []
                for r in visible:
                    try:
                        if ctx.apply_disclosure_to_row(r, operation="read") is not None:
                            disclosed_visible.append(r)
                    except PolicyError:
                        continue
                status["db_count"] = len(disclosed_visible)
                status["indexed_count"] = min(
                    int(status.get("indexed_count") or 0), len(disclosed_visible)
                )
        if ctx.redact_host_paths_enabled:
            status["path"] = None
        status["trace_ms"] = round(t.ms, 2)
        return status

    def vector_index_clear(self, ctx: CallerContext) -> dict[str, Any]:
        if ctx.policy_enforced:
            ctx.require_operation("manage")
        else:
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
