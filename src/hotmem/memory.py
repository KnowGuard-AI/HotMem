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
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.provenance import (
    BackingFileMissingError,
    ChecksumMismatchError,
    ProvenanceError,
    verify_range,
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


def _file_ref_content_hash(identifier: str, ref: FileRef) -> str:
    """Deterministic SHA-256 of the reference itself (no bytes read)."""
    payload = (
        f"{identifier}:{ref.source_uri}:{ref.byte_offset}:"
        f"{ref.byte_length}:{ref.source_checksum or ''}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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
    provenance: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Add a file-backed memory with zero bytes copied.

    Validates the scheme is local (rejects s3://, hdfs://, abfs://, gs://),
    confirms the backing file exists via a cheap stat (not a full read), and
    stores the reference. If ``summary`` is provided it is embedded so the
    memory is searchable; otherwise the embedding is NULL and the memory is
    excluded from cosine/keyword search (still retrievable via get_memory).

    Returns (memory_id, content_hash). Raises UnsupportedSchemeError for
    non-local URIs and FileNotFoundError if the backing file is missing.
    """
    # Resolve relative URIs against base_dir before adapter lookup.
    resolved_uri = _resolve_uri(file_ref.source_uri, base_dir)
    adapter = get_adapter(resolved_uri)  # raises UnsupportedSchemeError for remote schemes

    with Timer() as t:
        if not adapter.exists(resolved_uri):
            raise FileNotFoundError(f"backing file not found: {file_ref.source_uri}")

        memory_id = uuid.uuid4().hex
        content_hash = _file_ref_content_hash(identifier, file_ref)

        if summary:
            vec = embed_text(summary)
            blob = pack_embedding(vec)
            embedding_model = EMBEDDING_MODEL
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
            embedding_dim=EMBEDDING_DIM,
            embedding_model=embedding_model,
            source=source,
            importance=importance,
            metadata_json=json.dumps(metadata or {}),
            content_hash=content_hash,
            provenance_json=json.dumps(provenance) if provenance else None,
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


def hydrate_memory_detailed(
    db: MemoryDB,
    memory_id: str,
    *,
    base_dir: str | None = None,
    verify: bool = True,
) -> HydratedContent:
    """Materialize a memory's payload on demand with provenance metadata.

    For inline memories: returns fact_text.encode() (no adapter use).
    For file-backed memories: reads exactly [offset, offset+length) via the
    adapter and verifies source_checksum if present and ``verify=True``.

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
    resolved_uri = _resolve_uri(source_uri, base_dir)
    adapter = get_adapter(resolved_uri)
    offset = record["byte_offset"] or 0
    length = record["byte_length"] or 0
    expected_checksum = record["source_checksum"] or None

    with Timer() as t:
        try:
            data = adapter.read_range(resolved_uri, offset, length)
        except FileNotFoundError as err:
            _trace.warn(
                "hydrate",
                "backing file missing",
                detail={"memory_id": memory_id, "source_uri": source_uri},
            )
            raise BackingFileMissingError(source_uri) from err

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
        if expected_checksum and verify:
            verify_range(adapter, resolved_uri, offset, length, expected_checksum)
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
                raise BackingFileMissingError(source_uri) from err

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
