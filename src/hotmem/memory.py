"""HotMem memory — add/hydrate orchestration for inline and file-backed memories.

Purpose:
     Keep the file-backed memory lifecycle out of the HTTP layer so it can
     be tested without a server. Coordinates the DB (reference storage),
     the Storage Adapter (byte reads), and provenance verification.

Interface:
     FileRef (dataclass): source_uri, byte_offset, byte_length, source_format,
                          source_checksum (optional)
     HydratedContent (frozen dataclass): memory_id, content, verified,
                                         source_uri, byte_offset, byte_length
     normalize_source_uri(source_uri) -> str   # syntactic, no dereference
     compute_file_content_hash(identifier, file_ref) -> str   # logical digest
     legacy_file_content_hashes(identifier, file_ref) -> tuple[str, ...]
     add_file_backed(db, identifier, file_ref, *, base_dir=..., summary=..., ...)
         -> tuple[memory_id, content_hash]
     get_memory_metadata(db, memory_id) -> dict | None    # no file I/O
     hydrate_memory(db, memory_id, *, base_dir=...) -> bytes
     hydrate_memory_detailed(db, memory_id, *, base_dir=..., verify=True)
         -> HydratedContent
     hydrate_many(db, memory_ids, *, base_dir=..., verify=True)
         -> list[HydratedContent]   # URI-grouped batch (avoids repeated opens)

Deps: hotmem.db, hotmem.embed, hotmem.provenance, hotmem.storage, hotmem.swap
Extension: add hydration profiles (#40), bundle hydration (#52) here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hotmem.db import MemoryDB
from hotmem.embed import DEFAULT_EMBEDDER, Embedder, pack_embedding
from hotmem.interchange import canonical
from hotmem.provenance import (
    BackingFileMissingError,
    ChecksumMismatchError,
    ProvenanceError,
    verify_bytes,
)
from hotmem.storage import get_adapter
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("memory")


@dataclass
class FileRef:
    """Reference to a byte range in a backing file."""

    source_uri: str
    byte_offset: int
    byte_length: int
    source_format: str
    source_checksum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HydratedContent:
    """Result of hydrating a memory with provenance metadata.

    ``content`` is the raw byte payload. ``verified`` is True when the
    source_checksum was present and matched. For inline memories,
    ``source_uri``/``byte_offset``/``byte_length`` are empty/zero and
    ``verified`` is False (no backing file to verify).
    """

    memory_id: str
    content: bytes
    verified: bool
    source_uri: str
    byte_offset: int
    byte_length: int


def normalize_source_uri(
    source_uri: str,
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Syntactic source-reference normalization (see canonical module)."""
    return canonical.normalize_source_uri(source_uri, base_dir=base_dir)


def compute_file_content_hash(
    identifier: str,
    ref: FileRef,
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Logical identity digest for a file-backed reference (#100).

    Covers the identifier and normalized source reference tuple only. The
    optional ``source_checksum`` verifies bytes and is deliberately excluded:
    correcting or adding a checksum must not create a new logical memory.
    """
    return canonical.compute_file_reference_hash(
        identifier,
        ref.source_uri,
        ref.byte_offset,
        ref.byte_length,
        base_dir=base_dir,
    )


def legacy_file_content_hashes(
    identifier: str,
    ref: FileRef,
    *,
    base_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """Hashes written by pre-#100 file-backed writers, newest first.

    The legacy formula included the raw (unnormalized) URI and the optional
    checksum. Callers use these only as compatibility aliases when deciding
    whether a reference is already stored; new rows always use
    :func:`compute_file_content_hash`.
    """
    uris = [ref.source_uri, normalize_source_uri(ref.source_uri)]
    if base_dir is not None:
        uris.append(normalize_source_uri(ref.source_uri, base_dir=base_dir))
    hashes: list[str] = []
    seen: set[str] = set()
    for uri in uris:
        if not uri:
            continue
        base = f"{identifier}:{uri}:{ref.byte_offset}:{ref.byte_length}"
        candidates = [hashlib.sha256(f"{base}:{ref.source_checksum or ''}".encode()).hexdigest()]
        if ref.source_checksum:
            candidates.append(hashlib.sha256(f"{base}:".encode()).hexdigest())
        for h in candidates:
            if h not in seen:
                seen.add(h)
                hashes.append(h)
    return tuple(hashes)


def _file_ref_content_hash(
    identifier: str,
    ref: FileRef,
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Backward-compatible alias for :func:`compute_file_content_hash`."""
    return compute_file_content_hash(identifier, ref, base_dir=base_dir)


def find_existing_file_record(
    db: MemoryDB,
    namespace: str,
    identifier: str,
    ref: FileRef,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return an existing row for this reference, including legacy hashes."""
    seen: set[str] = set()
    for content_hash in (
        compute_file_content_hash(identifier, ref, base_dir=base_dir),
        compute_file_content_hash(identifier, ref),
        *legacy_file_content_hashes(identifier, ref, base_dir=base_dir),
    ):
        if not content_hash or content_hash in seen:
            continue
        seen.add(content_hash)
        row = db.find_by_namespace_and_hash(namespace, content_hash)
        if row is not None:
            return row
    return db.find_file_record_by_reference(
        namespace,
        identifier,
        ref.source_uri,
        ref.byte_offset,
        ref.byte_length,
        base_dir=base_dir,
    )


def _resolve_uri(source_uri: str, base_dir: str | None) -> str:
    """Resolve a relative URI against base_dir; leave absolute/file:// unchanged."""
    if "://" in source_uri or Path(source_uri).is_absolute():
        return source_uri
    if base_dir is not None:
        return str(Path(base_dir) / source_uri)
    return source_uri


def add_file_backed(
    db: MemoryDB,
    identifier: str,
    file_ref: FileRef,
    *,
    base_dir: str | None = None,
    summary: str | None = None,
    importance: float = 0.5,
    metadata: dict[str, Any] | None = None,
    source: str = "",
    ttl_seconds: int | None = None,
    created_at: str | None = None,
    namespace: str = "",
    tier: str = "hot",
    updated_at: str | None = None,
    snapshot_id: str = "",
    promotion_state: str = "HOT",
    promotion_candidate: int = 0,
    parent_memory: str = "",
    related_memories: list[str] | None = None,
    tags: list[str] | None = None,
    schema_version: int = 1,
    provenance: dict[str, Any] | None = None,
    embedder: Embedder | None = None,
    _commit: bool = True,
) -> tuple[str, str]:
    """Add a file-backed memory with zero bytes copied.

    Validates the scheme is local (rejects s3://, hdfs://, abfs://, gs://),
    confirms the backing file exists via a cheap stat (not a full read), and
    stores the reference. If ``summary`` is provided it is embedded under
    ``embedder`` (issue #78; ``None`` = the hash default) so the memory is
    searchable; otherwise the embedding is NULL and the memory is excluded
    from cosine/keyword search (still retrievable via get_memory).

    Returns (memory_id, content_hash). Raises UnsupportedSchemeError for
    non-local URIs and FileNotFoundError if the backing file is missing.
    """
    # Resolve relative URIs against base_dir before adapter lookup.
    resolved_uri = _resolve_uri(file_ref.source_uri, base_dir)
    adapter = get_adapter(resolved_uri)  # raises UnsupportedSchemeError for remote schemes

    with Timer() as t:
        if not adapter.exists(resolved_uri):
            raise FileNotFoundError(f"backing file not found: {file_ref.source_uri}")

        active = embedder if embedder is not None else DEFAULT_EMBEDDER
        existing = find_existing_file_record(db, namespace, identifier, file_ref, base_dir=base_dir)
        if existing is not None:
            return str(existing["id"]), str(existing["content_hash"])

        memory_id = uuid.uuid4().hex
        content_hash = compute_file_content_hash(identifier, file_ref, base_dir=base_dir)

        if summary:
            vec = active.embed(summary)
            blob = pack_embedding(vec)
            embedding_model = active.descriptor.key
        else:
            blob = b""
            embedding_model = ""

        db.insert_file_backed(
            id=memory_id,
            identifier=identifier,
            source_uri=file_ref.source_uri,
            byte_offset=file_ref.byte_offset,
            byte_length=file_ref.byte_length,
            source_format=file_ref.source_format,
            source_checksum=file_ref.source_checksum,
            fact_summary=summary,
            embedding=blob,
            embedding_dim=active.descriptor.dimension,
            embedding_model=embedding_model,
            source=source,
            importance=importance,
            metadata_json=json.dumps(metadata or {}, sort_keys=True),
            content_hash=content_hash,
            ttl_seconds=ttl_seconds,
            created_at=created_at,
            namespace=namespace,
            tier=tier,
            updated_at=updated_at,
            snapshot_id=snapshot_id,
            promotion_state=promotion_state,
            promotion_candidate=promotion_candidate,
            parent_memory=parent_memory,
            related_memories=json.dumps(list(related_memories or ())),
            tags=json.dumps(list(tags or ())),
            schema_version=schema_version,
            provenance_json=json.dumps(provenance, sort_keys=True) if provenance else None,
            _commit=_commit,
        )

    _trace.info(
        "add_file_backed",
        f"stored file-backed memory {memory_id[:8]}…",
        detail={
            "identifier": identifier,
            "source_uri": file_ref.source_uri,
            "offset": file_ref.byte_offset,
            "length": file_ref.byte_length,
            "summary": bool(summary),
            "ms": round(t.ms, 2),
        },
    )
    return memory_id, content_hash


def get_memory_metadata(db: MemoryDB, memory_id: str) -> dict[str, Any] | None:
    """Return memory metadata without touching the backing file (pure DB read)."""
    return db.get_memory(memory_id)


def hydrate_memory(
    db: MemoryDB,
    memory_id: str,
    *,
    base_dir: str | None = None,
) -> bytes:
    """Materialize a memory's payload on demand (lazy hydration). Returns raw bytes.

    Thin wrapper around ``hydrate_memory_detailed()`` that returns ``.content``.
    Use ``hydrate_memory_detailed()`` when you need verification status and
    source metadata.
    """
    return hydrate_memory_detailed(db, memory_id, base_dir=base_dir).content


def _read_snapshot_attachment(
    record: dict[str, Any],
    base_dir: str | Path | None,
) -> bytes | None:
    """If ``base_dir`` is a Snapshot v2 directory carrying an attachment for ``record``,
    return the attached byte slice; otherwise return ``None``."""
    if base_dir is None:
        return None
    root = Path(base_dir)
    attachments_dir = root / "attachments"
    if not attachments_dir.is_dir():
        return None

    att_name: str | None = None
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            refs = (
                manifest_raw.get("file_backed_references")
                or manifest_raw.get("file_references")
                or []
            )
            for ref in refs:
                if not isinstance(ref, dict) or not ref.get("attachment"):
                    continue
                if str(ref.get("memory_id") or "") == str(record.get("id") or ""):
                    att_name = str(ref["attachment"])
                    break
            if att_name is None:
                for ref in refs:
                    if not isinstance(ref, dict) or not ref.get("attachment"):
                        continue
                    if (
                        str(ref.get("source_uri") or "") == str(record.get("source_uri") or "")
                        and int(ref.get("byte_offset") or 0) == int(record.get("byte_offset") or 0)
                        and int(ref.get("byte_length") or 0) == int(record.get("byte_length") or 0)
                    ):
                        att_name = str(ref["attachment"])
                        break
        except Exception:
            att_name = None

    if att_name is None and record.get("source_checksum"):
        att_name = str(record["source_checksum"])
    if not att_name or "/" in att_name or "\\" in att_name or att_name in (".", ".."):
        return None

    try:
        resolved_root = attachments_dir.resolve()
        candidate = (attachments_dir / att_name).resolve()
        candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return candidate.read_bytes()


def hydrate_memory_detailed(
    db: MemoryDB,
    memory_id: str,
    *,
    base_dir: str | Path | None = None,
    verify: bool = True,
) -> HydratedContent:
    """Materialize a memory's payload on demand with provenance metadata.

    For inline memories: returns fact_text.encode() (no adapter use).
    For file-backed memories: reads exactly [offset, offset+length) via the
    adapter (falling back to a verified Snapshot v2 ``attachments/`` slice
    when ``base_dir`` is a snapshot directory and the external backing file is
    not present locally) and verifies source_checksum if present and
    ``verify=True``.

    Args:
        verify: when True (default), verify source_checksum if present.
            When False, skip checksum computation (bulk-read performance).

    Raises:
        KeyError: memory not found.
        BackingFileMissingError: backing file deleted.
        ChecksumMismatchError: checksum mismatch.
        ProvenanceError: truncated range or other provenance failure.
    """
    record = db.get_memory(memory_id)
    if record is None:
        raise KeyError(f"memory not found: {memory_id}")

    if record["memory_type"] != "file":
        # Inline memory — no file I/O.
        fact = record["fact_text"]
        return HydratedContent(
            memory_id=memory_id,
            content=(fact or "").encode(),
            verified=False,
            source_uri="",
            byte_offset=0,
            byte_length=0,
        )

    source_uri = record["source_uri"] or ""
    resolved_uri = _resolve_uri(source_uri, str(base_dir) if base_dir is not None else None)
    adapter = get_adapter(resolved_uri)
    offset = record["byte_offset"] or 0
    length = record["byte_length"] or 0
    expected_checksum = record["source_checksum"] or None

    with Timer() as t:
        try:
            data = adapter.read_range(resolved_uri, offset, length)
        except FileNotFoundError as err:
            attached = _read_snapshot_attachment(record, base_dir)
            if attached is None:
                _trace.warn(
                    "hydrate",
                    "backing file missing",
                    detail={"memory_id": memory_id, "source_uri": source_uri},
                )
                raise BackingFileMissingError(source_uri) from err
            data = attached

        verified = False

        # Detect truncation (short read) BEFORE checksum, for a precise reason.
        if len(data) < length:
            _trace.warn(
                "hydrate",
                "truncated range read",
                detail={
                    "memory_id": memory_id,
                    "source_uri": source_uri,
                    "expected": length,
                    "got": len(data),
                },
            )
            raise ProvenanceError("truncated", source_uri, expected=expected_checksum)

        # On-demand checksum verification (skipped if no checksum stored or verify=False).
        # Single-read verification (#87): hash the bytes already read — the
        # digest and error semantics are identical to verify_range's re-read.
        if expected_checksum and verify:
            verify_bytes(source_uri, data, expected_checksum)
            verified = True

    _trace.info(
        "hydrate",
        f"hydrated {len(data)} bytes for {memory_id[:8]}…",
        detail={
            "source_uri": source_uri,
            "offset": offset,
            "length": length,
            "verified": verified,
            "ms": round(t.ms, 2),
        },
    )
    return HydratedContent(
        memory_id=memory_id,
        content=data,
        verified=verified,
        source_uri=source_uri,
        byte_offset=offset,
        byte_length=length,
    )


def hydrate_many(
    db: MemoryDB,
    memory_ids: list[str],
    *,
    base_dir: str | None = None,
    verify: bool = True,
) -> list[HydratedContent]:
    """Hydrate multiple memories with URI-grouped batching.

    Groups file-backed memory IDs by ``source_uri`` so each backing file is
    opened at most once (seek + read per range, but a single adapter per file).
    Inline memories are hydrated directly (no file I/O).

    Raises on the first provenance failure (same exceptions as
    ``hydrate_memory_detailed``). The caller should handle partial results.
    """
    # Fetch all records first (pure DB, no file I/O).
    records: list[tuple[str, dict[str, Any]]] = []
    for mid in memory_ids:
        record = db.get_memory(mid)
        if record is None:
            raise KeyError(f"memory not found: {mid}")
        records.append((mid, record))

    results: dict[str, HydratedContent] = {}
    # Group file-backed by resolved source_uri for batch reads.
    file_backed: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)

    for mid, record in records:
        if record["memory_type"] != "file":
            # Inline — hydrate immediately.
            fact = record["fact_text"]
            results[mid] = HydratedContent(
                memory_id=mid,
                content=(fact or "").encode(),
                verified=False,
                source_uri="",
                byte_offset=0,
                byte_length=0,
            )
        else:
            source_uri = record["source_uri"] or ""
            resolved = _resolve_uri(source_uri, base_dir)
            file_backed[resolved].append((mid, record))

    # Hydrate file-backed memories grouped by source file.
    for resolved_uri, group in file_backed.items():
        adapter = get_adapter(resolved_uri)
        for mid, record in group:
            source_uri = record["source_uri"] or ""
            offset = record["byte_offset"] or 0
            length = record["byte_length"] or 0
            expected_checksum = record["source_checksum"] or None

            try:
                data = adapter.read_range(resolved_uri, offset, length)
            except FileNotFoundError as err:
                attached = _read_snapshot_attachment(record, base_dir)
                if attached is None:
                    raise BackingFileMissingError(source_uri) from err
                data = attached

            if len(data) < length:
                raise ProvenanceError("truncated", source_uri, expected=expected_checksum)

            verified = False
            if expected_checksum and verify:
                actual = hashlib.sha256(data).hexdigest()
                if actual != expected_checksum:
                    raise ChecksumMismatchError(
                        source_uri, expected=expected_checksum, actual=actual
                    )
                verified = True

            results[mid] = HydratedContent(
                memory_id=mid,
                content=data,
                verified=verified,
                source_uri=source_uri,
                byte_offset=offset,
                byte_length=length,
            )

    _trace.info(
        "hydrate_many",
        f"hydrated {len(results)} memories ({len(file_backed)} source files)",
        detail={"count": len(results), "source_files": len(file_backed)},
    )
    # Return in the same order as the input memory_ids.
    return [results[mid] for mid in memory_ids]
