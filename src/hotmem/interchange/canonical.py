"""Canonical serialization and digest primitives for the interchange contract.

Purpose:
     Single source of truth for the byte-level contract (interchange-v1 §2–§3):
     canonical JSON serialization, per-record content hashes, the logical
     identity aggregate, and streaming file digests. Every writer/reader that
     needs byte-stable output or identity derives from here.

Interface:
      canonical_dumps(record) -> str
      canonical_line(record) -> str
      compute_content_hash(identifier, fact_text) -> str
      normalize_source_uri(source_uri, *, base_dir=None) -> str
      compute_file_reference_hash(identifier, source_uri, offset, length, *, base_dir=None) -> str
      logical_id(content_hashes) -> str
      sha256_bytes(data) -> str
      sha256_file(path) -> str

Deps: stdlib only. No hotmem imports — snapshot/swap import FROM this module,
      never the reverse (import-cycle safety).
Extension: package manifest digests reuse these helpers (interchange.package).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit


def canonical_dumps(record: dict[str, Any]) -> str:
    """Serialize one record to its canonical JSON form (interchange-v1 §2).

    Deterministic: sorted keys, compact separators, UTF-8 text (ensure_ascii
    off), NaN rejected, non-JSON scalars coerced via str. New artifacts use
    this form; readers accept historical spacings because digests always
    cover the bytes actually written.
    """
    return json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def canonical_line(record: dict[str, Any]) -> str:
    """One canonical JSONL line (canonical form + trailing newline)."""
    return canonical_dumps(record) + "\n"


def write_canonical(f, record: dict[str, Any]) -> None:
    """Serialize one canonical JSONL record to a file handle.

    Mirror of hotmem.swap.write_record for the canonical byte form, so writer
    call sites can swap serialization without changing shape.
    """
    f.write(canonical_line(record))


def compute_content_hash(identifier: str, fact_text: str) -> str:
    """SHA-256 of identifier + fact_text -- the inline content digest.

    This digest is deterministic and portable across HotMem versions. It is
    not, by itself, a logical identity: identity is ``(namespace,
    content_hash)`` (#100), so the same fact may exist once in each
    namespace. The formula is unchanged from pre-#100 artifacts.
    """
    return hashlib.sha256(f"{identifier}:{fact_text}".encode()).hexdigest()


_FILE_IDENTITY_DOMAIN = "hotmem-file-ref-v1"


def normalize_source_uri(
    source_uri: str,
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Normalize a reference syntactically without reading or resolving it.

    Equivalent spellings of the same portable reference produce the same
    identity: ``./runbook.txt`` and ``runbook.txt``, a ``file://`` URI and
    its filesystem path, or an absolute path inside ``base_dir`` and its
    ``base_dir``-relative path. Normalization never stats, opens, or
    dereferences the target, and preserves URI schemes other than ``file``.
    """
    raw = str(source_uri).strip()
    if not raw:
        return ""
    if raw.lower().startswith("file://"):
        legacy = raw[len("file://") :]
        if len(legacy) >= 3 and legacy[0].isalpha() and legacy[1] == ":" and legacy[2] in "/\\":
            with contextlib.suppress(UnicodeDecodeError):
                legacy = unquote(legacy, encoding="utf-8", errors="strict")
            raw = legacy
        else:
            parts = urlsplit(raw)
            authority = parts.netloc
            if authority and authority.lower() != "localhost":
                if len(authority) == 2 and authority[0].isalpha() and authority[1] == ":":
                    candidate = authority + parts.path
                    with contextlib.suppress(UnicodeDecodeError):
                        candidate = unquote(candidate, encoding="utf-8", errors="strict")
                    raw = candidate
            else:
                candidate = parts.path
                with contextlib.suppress(UnicodeDecodeError):
                    candidate = unquote(candidate, encoding="utf-8", errors="strict")
                if (
                    len(candidate) >= 3
                    and candidate[0] == "/"
                    and candidate[1].isalpha()
                    and candidate[2] == ":"
                ):
                    candidate = candidate[1:]
                raw = candidate
    if "://" in raw:
        parts = urlsplit(raw)
        path = posixpath.normpath(parts.path or "/")
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, parts.fragment)
        )
    norm = posixpath.normpath(raw.replace("\\", "/"))
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    if len(norm) >= 2 and norm[0].isalpha() and norm[1] == ":":
        norm = norm[0].upper() + norm[1:]
    if base_dir is not None:
        base_norm = normalize_source_uri(str(base_dir)).rstrip("/")
        if base_norm and base_norm != ".":
            if norm == base_norm:
                return "."
            if norm.startswith(base_norm + "/"):
                rel = norm[len(base_norm) + 1 :]
                if rel and not rel.startswith(".."):
                    return rel
    return norm


def compute_file_reference_hash(
    identifier: str,
    source_uri: str,
    byte_offset: int,
    byte_length: int,
    *,
    base_dir: str | Path | None = None,
) -> str:
    """Logical content digest for a file-backed reference (#100).

    Covers the identifier and normalized source reference tuple only. Byte
    checksums verify content and are deliberately excluded from identity.
    """
    norm_uri = normalize_source_uri(source_uri, base_dir=base_dir)
    payload = f"{_FILE_IDENTITY_DOMAIN}:{identifier}:{norm_uri}:{byte_offset}:{byte_length}"
    return hashlib.sha256(payload.encode()).hexdigest()


def logical_id(content_hashes: list[str]) -> str:
    """Deterministic logical identity of a record set (interchange-v1 §3).

    SHA-256 over the sorted content_hash concatenation: equal for equivalent
    content sets regardless of record order, compression, export time, or
    embeddings. This is a content-set digest, not the per-record identity
    tuple; it does not encode namespaces (#100). Same algorithm as Snapshot
    v2's ``snapshot_id``.
    """
    return sha256_bytes("".join(sorted(content_hashes)).encode())


def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file's full contents (streaming, 64 KiB)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
