"""HotMem snapshot format — constants, schema dataclasses, and checksum helpers.

Purpose:
     Define the Snapshot v2 on-disk format so writers and readers share a
     single source of truth for the manifest schema, file checksums, and the
     ``overall_sha256`` aggregate.

Snapshot v2 directory layout::

     <snapshot_dir>/
       manifest.json        # authoritative: schema_version, snapshot_id, files{},
                           #   overall_sha256, file_references[]
       memories.jsonl      # one record per memory, sorted by id, base64 embeddings
       metadata.json       # informational; NOT part of overall_sha256
       attachments/        # opt-in; <sha256-of-range> files when --attach + small range

Interface:
     SCHEMA_VERSION, FORMAT, ATTACH_THRESHOLD
     SNAPSHOT_ID_VERSION, LEGACY_SNAPSHOT_ID_VERSION
     SNAPSHOT_ID_ALGORITHM, LEGACY_SNAPSHOT_ID_ALGORITHM
     Manifest, FileEntry, FileReference, MetadataInfo (dataclasses)
     SnapshotChecksumError
     sha256_bytes(data) -> str
     sha256_file(path) -> str
     compute_overall(per_file_hashes) -> str
     compute_snapshot_id(items, ...) -> str
     compute_semantic_snapshot_id(records, ...) -> str
     compute_legacy_snapshot_id(content_hashes) -> str
     semantic_record_projection(record) -> dict
     semantic_snapshot_projection(records, ...) -> dict

Deps: stdlib + hotmem.annotations + hotmem.interchange.canonical / record
Extension: bump SCHEMA_VERSION and add fields here for future formats.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from hotmem.annotations import AnnotationValidationError
from hotmem.interchange.canonical import (
    canonical_dumps,
    compute_content_hash,
    compute_file_reference_hash,
    normalize_source_uri,
)
from hotmem.interchange.record import normalize_record

# ── Format constants ─────────────────────────────────────────────────────────

FORMAT = "hotmem-snapshot-v2"
SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"

SNAPSHOT_ID_VERSION = 2
LEGACY_SNAPSHOT_ID_VERSION = 1
SNAPSHOT_ID_ALGORITHM = "hotmem-snapshot-id-v2"
LEGACY_SNAPSHOT_ID_ALGORITHM = "hotmem-snapshot-id-v1"
SUPPORTED_SNAPSHOT_ID_VERSIONS: frozenset[int] = frozenset(
    {LEGACY_SNAPSHOT_ID_VERSION, SNAPSHOT_ID_VERSION}
)
SUPPORTED_SNAPSHOT_ID_ALGORITHMS: dict[int, str] = {
    LEGACY_SNAPSHOT_ID_VERSION: LEGACY_SNAPSHOT_ID_ALGORITHM,
    SNAPSHOT_ID_VERSION: SNAPSHOT_ID_ALGORITHM,
}
_ALGORITHM_TO_VERSION: dict[str, int] = {
    alg: ver for ver, alg in SUPPORTED_SNAPSHOT_ID_ALGORITHMS.items()
}
_RECORD_PROJECTION_DOMAIN = "hotmem-snapshot-record-v2"
_SNAPSHOT_PROJECTION_DOMAIN = "hotmem-snapshot-id-v2"

# Heuristic threshold for copying a file-backed byte range into attachments/.
# Inline text is fine up to ~8 KB; ranges above this stay referenced.
ATTACH_THRESHOLD = 8 * 1024

# Files written by every snapshot (attachments/ is dynamic).
CORE_FILES: tuple[str, ...] = ("memories.jsonl",)


# ── Error type ────────────────────────────────────────────────────────────────


class SnapshotChecksumError(Exception):
    """Raised when a snapshot's manifest checksums do not match reality.

    Carries structured fields so the server can surface a precise 409 body:
        {"error": "snapshot_checksum_mismatch", "reason": ..., "file": ...,
         "expected": ..., "actual": ...}
    """

    def __init__(
        self,
        reason: Literal["missing_manifest", "missing_file", "mismatch", "malformed"],
        file: str | None = None,
        *,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        self.reason = reason
        self.file = file
        self.expected = expected
        self.actual = actual
        msg = f"snapshot checksum failure ({reason})"
        if file:
            msg += f" for {file}"
        if expected is not None or actual is not None:
            msg += f": expected={expected} actual={actual}"
        super().__init__(msg)


# ── Schema dataclasses ───────────────────────────────────────────────────────


@dataclass
class FileEntry:
    """Per-file checksum entry in the manifest's ``files`` map."""

    size: int
    sha256: str


@dataclass
class FileReference:
    """A file-backed memory reference recorded in the manifest.

    ``attachment`` is the filename within ``attachments/`` when the byte range
    was copied in (opt-in, small ranges), or ``None`` when the memory still
    points at its original ``source_uri`` (large ranges or copy disabled).
    """

    memory_id: str
    source_uri: str
    byte_offset: int
    byte_length: int
    source_format: str | None = None
    source_checksum: str | None = None
    attachment: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MetadataInfo:
    """Informational generation metadata (NOT part of overall_sha256)."""

    hotmem_version: str
    created_at: str
    host: str
    db_path: str
    counts: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AttachmentEntry:
    """An attachment file copied into the snapshot's ``attachments/`` dir."""

    name: str
    source_uri: str
    size: int
    sha256: str


@dataclass
class Manifest:
    """The authoritative snapshot index with per-file and overall checksums."""

    format: str = FORMAT
    schema_version: int = SCHEMA_VERSION
    snapshot_id: str = ""
    snapshot_id_version: int = 0
    snapshot_id_algorithm: str = ""
    created_at: str = ""
    hotmem_version: str = ""
    memory_count: int = 0
    file_backed_count: int = 0
    inline_count: int = 0
    files: dict[str, FileEntry] = field(default_factory=dict)
    overall_sha256: str = ""
    file_backed_references: list[FileReference] = field(default_factory=list)
    attachments: list[AttachmentEntry] = field(default_factory=list)
    scope: dict | None = None
    mount_id: str = ""
    replica_id: str = ""
    history_id: str = ""
    state_fingerprint: str = ""
    replay_cursor: str = ""

    def to_dict(self) -> dict:
        data = {
            "format": self.format,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "hotmem_version": self.hotmem_version,
            "memory_count": self.memory_count,
            "file_backed_count": self.file_backed_count,
            "inline_count": self.inline_count,
            "files": {name: asdict(entry) for name, entry in self.files.items()},
            "overall_sha256": self.overall_sha256,
            "file_backed_references": [ref.to_dict() for ref in self.file_backed_references],
            "attachments": [asdict(att) for att in self.attachments],
        }
        if self.snapshot_id_version:
            data["snapshot_id_version"] = self.snapshot_id_version
        if self.snapshot_id_algorithm:
            data["snapshot_id_algorithm"] = self.snapshot_id_algorithm
        if self.scope is not None:
            data["scope"] = self.scope
        if self.mount_id:
            data["mount_id"] = self.mount_id
        if self.replica_id:
            data["replica_id"] = self.replica_id
        if self.history_id:
            data["history_id"] = self.history_id
        if self.state_fingerprint:
            data["state_fingerprint"] = self.state_fingerprint
        if self.replay_cursor:
            data["replay_cursor"] = self.replay_cursor
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        if not isinstance(data, dict):
            raise SnapshotChecksumError("malformed", file=MANIFEST_NAME)

        raw_format = data.get("format")
        if raw_format is not None and str(raw_format) != FORMAT:
            raise SnapshotChecksumError("malformed", file=MANIFEST_NAME)

        raw_schema = data.get("schema_version")
        if raw_schema is not None:
            if isinstance(raw_schema, bool):
                raise SnapshotChecksumError("malformed", file=MANIFEST_NAME)
            try:
                schema_version = int(raw_schema)
            except (TypeError, ValueError) as err:
                raise SnapshotChecksumError("malformed", file=MANIFEST_NAME) from err
            if schema_version not in (1, 2):
                raise SnapshotChecksumError("malformed", file=MANIFEST_NAME)
        else:
            schema_version = SCHEMA_VERSION

        raw_files = data.get("files")
        if raw_files is not None and not isinstance(raw_files, dict):
            raise SnapshotChecksumError("malformed", file=MANIFEST_NAME)
        files: dict[str, FileEntry] = {}
        for name, entry in (raw_files or {}).items():
            if not isinstance(entry, dict) or "sha256" not in entry or "size" not in entry:
                raise SnapshotChecksumError("malformed", file=name)
            try:
                size_val = int(entry["size"])
            except (TypeError, ValueError) as err:
                raise SnapshotChecksumError("malformed", file=name) from err
            if isinstance(entry["size"], bool) or size_val < 0:
                raise SnapshotChecksumError("malformed", file=name)
            files[name] = FileEntry(size=size_val, sha256=str(entry["sha256"]))

        # Accept both new (file_backed_references) and old (file_references) names.
        refs_data = data.get("file_backed_references")
        if refs_data is not None and not isinstance(refs_data, list):
            raise SnapshotChecksumError("malformed", file="file_backed_references")
        if not refs_data:
            # Legacy writers may emit an empty new key alongside the old one.
            legacy_refs = data.get("file_references")
            if legacy_refs is not None and not isinstance(legacy_refs, list):
                raise SnapshotChecksumError("malformed", file="file_references")
            refs_data = legacy_refs
        refs: list[FileReference] = []
        for ref in refs_data or []:
            if not isinstance(ref, dict) or "memory_id" not in ref or "source_uri" not in ref:
                raise SnapshotChecksumError("malformed", file="file_backed_references")
            try:
                bo = int(ref.get("byte_offset") or 0)
                bl = int(ref.get("byte_length") or 0)
            except (TypeError, ValueError) as err:
                raise SnapshotChecksumError("malformed", file="file_backed_references") from err
            if (
                isinstance(ref.get("byte_offset"), bool)
                or isinstance(ref.get("byte_length"), bool)
                or bo < 0
                or bl < 0
                or (not ref.get("source_uri") and not ref.get("attachment"))
            ):
                raise SnapshotChecksumError("malformed", file="file_backed_references")
            refs.append(
                FileReference(
                    memory_id=str(ref["memory_id"]),
                    source_uri=str(ref["source_uri"]),
                    byte_offset=bo,
                    byte_length=bl,
                    source_format=ref.get("source_format"),
                    source_checksum=ref.get("source_checksum"),
                    attachment=ref.get("attachment"),
                )
            )

        raw_atts = data.get("attachments")
        if raw_atts is not None and not isinstance(raw_atts, list):
            raise SnapshotChecksumError("malformed", file="attachments")
        atts: list[AttachmentEntry] = []
        for att in raw_atts or []:
            if not isinstance(att, dict) or "name" not in att or not att["name"]:
                raise SnapshotChecksumError("malformed", file="attachments")
            try:
                att_size = int(att.get("size", 0))
            except (TypeError, ValueError) as err:
                raise SnapshotChecksumError("malformed", file="attachments") from err
            if isinstance(att.get("size"), bool) or att_size < 0:
                raise SnapshotChecksumError("malformed", file="attachments")
            atts.append(
                AttachmentEntry(
                    name=str(att["name"]),
                    source_uri=str(att.get("source_uri", "")),
                    size=att_size,
                    sha256=str(att.get("sha256", "")),
                )
            )

        try:
            mem_count = int(data.get("memory_count") or 0)
            fb_count = int(data.get("file_backed_count") or 0)
            inl_count = int(data.get("inline_count") or 0)
        except (TypeError, ValueError) as err:
            raise SnapshotChecksumError("malformed", file=MANIFEST_NAME) from err

        for key in ("mount_id", "replica_id", "history_id", "state_fingerprint", "replay_cursor"):
            val = data.get(key)
            if val is not None and not isinstance(val, str):
                raise SnapshotChecksumError("malformed", file=key)

        mount_id_str = str(data.get("mount_id") or "")
        replica_id_str = str(data.get("replica_id") or "")
        history_id_str = str(data.get("history_id") or "")
        state_fp_str = str(data.get("state_fingerprint") or "")
        cursor_str = str(data.get("replay_cursor") or "")
        snapshot_id_str = str(data.get("snapshot_id") or "")

        raw_id_version = data.get("snapshot_id_version")
        raw_id_algorithm = data.get("snapshot_id_algorithm")
        snapshot_id_version = 0
        snapshot_id_algorithm = ""
        if raw_id_version is not None:
            if isinstance(raw_id_version, bool) or not isinstance(raw_id_version, int):
                raise SnapshotChecksumError("malformed", file="snapshot_id_version")
            if raw_id_version not in SUPPORTED_SNAPSHOT_ID_VERSIONS:
                raise SnapshotChecksumError("malformed", file="snapshot_id_version")
            snapshot_id_version = raw_id_version
        if raw_id_algorithm is not None:
            if (
                not isinstance(raw_id_algorithm, str)
                or raw_id_algorithm not in _ALGORITHM_TO_VERSION
            ):
                raise SnapshotChecksumError("malformed", file="snapshot_id_algorithm")
            snapshot_id_algorithm = raw_id_algorithm
            inferred_version = _ALGORITHM_TO_VERSION[raw_id_algorithm]
            if snapshot_id_version and snapshot_id_version != inferred_version:
                raise SnapshotChecksumError("malformed", file="snapshot_id_algorithm")
            snapshot_id_version = inferred_version
        elif snapshot_id_version:
            snapshot_id_algorithm = SUPPORTED_SNAPSHOT_ID_ALGORITHMS[snapshot_id_version]

        if cursor_str:
            from hotmem.events import ReplayCursorError, decode_cursor

            if not (mount_id_str and replica_id_str and history_id_str):
                raise SnapshotChecksumError(
                    "missing_metadata",
                    file="replay_cursor",
                    expected="mount_id, replica_id, and history_id alongside replay_cursor",
                    actual="missing identity field(s)",
                )
            try:
                decoded = decode_cursor(cursor_str)
            except ReplayCursorError as err:
                raise SnapshotChecksumError("malformed", file="replay_cursor") from err
            if (
                (mount_id_str and decoded.mount_id != mount_id_str)
                or (replica_id_str and decoded.replica_id != replica_id_str)
                or (history_id_str and decoded.history_id != history_id_str)
            ):
                raise SnapshotChecksumError("mismatch", file="replay_cursor")

        return cls(
            format=str(raw_format or FORMAT),
            schema_version=schema_version,
            snapshot_id=snapshot_id_str,
            snapshot_id_version=snapshot_id_version,
            snapshot_id_algorithm=snapshot_id_algorithm,
            created_at=str(data.get("created_at") or ""),
            hotmem_version=str(data.get("hotmem_version") or ""),
            memory_count=mem_count,
            file_backed_count=fb_count,
            inline_count=inl_count,
            files=files,
            overall_sha256=str(data.get("overall_sha256") or ""),
            file_backed_references=refs,
            attachments=atts,
            scope=data.get("scope") if isinstance(data.get("scope"), dict) else None,
            mount_id=mount_id_str,
            replica_id=replica_id_str,
            history_id=history_id_str,
            state_fingerprint=state_fp_str,
            replay_cursor=cursor_str,
        )


# ── Checksum & semantic identity helpers ─────────────────────────────────────


def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file's full contents (streaming)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_overall(per_file_hashes: dict[str, str]) -> str:
    """Aggregate checksum: SHA-256 of the concatenated per-file hex digests.

    Files are concatenated in sorted-by-filename order so the aggregate is
    deterministic regardless of insertion order. Only files listed in the
    manifest contribute (``metadata.json`` is excluded by construction).
    """
    parts = [per_file_hashes[name] for name in sorted(per_file_hashes)]
    return sha256_bytes("".join(parts).encode())


def _nfc(text: str) -> str:
    """Normalize a string to Unicode NFC."""
    return unicodedata.normalize("NFC", text)


def _canonicalize_json_value(value: Any) -> Any:
    """Recursively normalize strings to NFC, floats (-0.0 -> 0.0), and dict keys."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0.0
        return 0.0 if value == 0.0 else value
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, (list, tuple)):
        return [_canonicalize_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {_nfc(str(k)): _canonicalize_json_value(v) for k, v in value.items()}
    return _nfc(str(value))


def _normalize_record_safe(
    raw: Mapping[str, Any], *, default_source: str = "snapshot"
) -> dict[str, Any]:
    """Run ``normalize_record`` while tolerating malformed ``annotations`` envelopes."""
    try:
        return normalize_record(dict(raw), default_source=default_source)
    except AnnotationValidationError:
        stripped = dict(raw)
        raw_meta: dict[str, Any] = {}
        meta_val = stripped.get("metadata")
        if meta_val is None:
            meta_val = stripped.get("metadata_json")
        if isinstance(meta_val, str):
            try:
                parsed = json.loads(meta_val)
                if isinstance(parsed, dict):
                    raw_meta = dict(parsed)
            except (json.JSONDecodeError, TypeError):
                raw_meta = {}
        elif isinstance(meta_val, Mapping):
            raw_meta = dict(meta_val)
        saved_annotations = raw_meta.pop("annotations", None)
        stripped["metadata"] = raw_meta
        stripped.pop("metadata_json", None)
        normalized = normalize_record(stripped, default_source=default_source)
        if saved_annotations is not None:
            meta_out = dict(normalized.get("metadata") or {})
            meta_out["annotations"] = saved_annotations
            normalized["metadata"] = meta_out
        return normalized


def semantic_record_projection(
    raw: Mapping[str, Any],
    *,
    default_source: str = "snapshot",
    normalized: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a memory record to its 27 portable ``preserve`` columns (#96, #99).

    Covers every column marked ``preserve`` in ``MEMORY_FIELD_INVENTORY`` and
    excludes ``regenerate`` embedding columns (``embedding``, ``embedding_dim``,
    ``embedding_model``) and packaging noise. Strings and JSON keys are
    normalized to Unicode NFC, omitted/null values normalize to their documented
    defaults, and an omitted ``id`` projects to ``""`` instead of a random UUID.
    """
    if normalized is None:
        normalized = _normalize_record_safe(raw, default_source=default_source)

    raw_id_val = raw.get("id")
    proj_id = "" if raw_id_val is None else _nfc(str(raw_id_val).strip())

    raw_ident = "" if raw.get("identifier") is None else str(raw.get("identifier")).strip()
    proj_ident = _nfc(raw_ident)

    raw_fact = "" if raw.get("fact_text") is None else str(raw.get("fact_text"))
    proj_fact = _nfc(raw_fact)

    raw_summary = normalized.get("fact_summary")
    proj_summary = _nfc(str(raw_summary)) if raw_summary is not None else None

    mem_type = "file" if normalized.get("memory_type") == "file" else "fact"

    raw_uri_str = "" if raw.get("source_uri") is None else str(raw.get("source_uri")).strip()
    proj_uri = normalize_source_uri(_nfc(raw_uri_str)) if raw_uri_str else ""

    b_offset = normalized.get("byte_offset")
    b_length = normalized.get("byte_length")

    if mem_type == "file":
        canonical_hash = (
            compute_file_reference_hash(proj_ident, proj_uri, b_offset or 0, b_length or 0)
            if (proj_ident or proj_uri)
            else ""
        )
        raw_norm_uri = normalize_source_uri(raw_uri_str) if raw_uri_str else ""
        raw_default_hash = (
            compute_file_reference_hash(raw_ident, raw_norm_uri, b_offset or 0, b_length or 0)
            if (raw_ident or raw_norm_uri)
            else ""
        )
    else:
        canonical_hash = (
            compute_content_hash(proj_ident, proj_fact) if (proj_ident or proj_fact) else ""
        )
        raw_default_hash = (
            compute_content_hash(raw_ident, raw_fact) if (raw_ident or raw_fact) else ""
        )

    explicit_hash = _nfc(str(raw.get("content_hash") or "").strip())
    if not explicit_hash or explicit_hash == raw_default_hash:
        proj_content_hash = canonical_hash
    else:
        proj_content_hash = explicit_hash

    proj_namespace = _nfc(str(normalized.get("namespace") or "").strip())
    if "source" in raw and isinstance(raw.get("source"), str):
        proj_source = _nfc(raw["source"])
    else:
        proj_source = _nfc(str(normalized.get("source") or default_source))

    try:
        imp_val = float(normalized.get("importance", 0.5))
    except (TypeError, ValueError):
        imp_val = 0.5
    if not math.isfinite(imp_val):
        imp_val = 0.5
    elif imp_val == 0.0:
        imp_val = 0.0

    proj_metadata = _canonicalize_json_value(normalized.get("metadata") or {})
    # Provenance defaults only for absent/blank values. Malformed non-object
    # payloads are preserved under "value" (mirroring metadata) so distinct
    # invalid states keep distinct identities instead of collapsing to null.
    raw_prov = normalized.get("provenance")
    if isinstance(raw_prov, Mapping):
        proj_provenance = _canonicalize_json_value(raw_prov) if raw_prov else None
    elif raw_prov is None or raw_prov == "":
        proj_provenance = None
    else:
        proj_provenance = {"value": _canonicalize_json_value(raw_prov)}

    proj_tags = [_nfc(str(t).strip()) for t in (normalized.get("tags") or []) if str(t).strip()]
    proj_related = [
        _nfc(str(r).strip()) for r in (normalized.get("related_memories") or []) if str(r).strip()
    ]

    ttl_val = normalized.get("ttl_seconds")
    proj_ttl = (
        int(ttl_val)
        if isinstance(ttl_val, int) and not isinstance(ttl_val, bool) and ttl_val > 0
        else None
    )

    proj_created_at = _nfc(str(normalized.get("created_at") or "").strip())
    raw_updated = normalized.get("updated_at")
    proj_updated_at = (
        _nfc(str(raw_updated).strip())
        if raw_updated is not None and str(raw_updated).strip()
        else None
    )

    # Enum-like lifecycle fields default only when absent/blank. Invalid
    # non-blank values are preserved verbatim (deterministically) so distinct
    # invalid states never collide with a real default (#96 review).
    raw_tier = normalized.get("tier")
    proj_tier = (
        "hot" if raw_tier is None or str(raw_tier).strip() == "" else _nfc(str(raw_tier).strip())
    )
    raw_promo = normalized.get("promotion_state")
    proj_promo_state = (
        "HOT" if raw_promo is None or str(raw_promo).strip() == "" else _nfc(str(raw_promo).strip())
    )
    # Projection must be total: invalid/non-scalar values are coerced to the
    # default here, never raised. Untrusted manifests reach this code during
    # verify_manifest, which must fail closed instead of leaking ValueError.
    raw_candidate = normalized.get("promotion_candidate")
    try:
        proj_promo_candidate = 1 if int(raw_candidate or 0) == 1 else 0
    except (TypeError, ValueError):
        proj_promo_candidate = 0

    proj_parent = _nfc(str(normalized.get("parent_memory") or "").strip())
    proj_source_format = _nfc(str(normalized.get("source_format") or "").strip())
    proj_source_checksum = _nfc(str(normalized.get("source_checksum") or "").strip())
    proj_row_snapshot_id = _nfc(str(normalized.get("snapshot_id") or "").strip())

    resolved_schema = (
        normalized.get("record_schema_version") or normalized.get("schema_version") or 1
    )
    try:
        proj_schema_version = int(resolved_schema)
    except (TypeError, ValueError):
        proj_schema_version = 1

    return {
        "byte_length": b_length,
        "byte_offset": b_offset,
        "content_hash": proj_content_hash,
        "created_at": proj_created_at,
        "fact_summary": proj_summary,
        "fact_text": proj_fact,
        "id": proj_id,
        "identifier": proj_ident,
        "importance": imp_val,
        "memory_type": mem_type,
        "metadata": proj_metadata,
        "namespace": proj_namespace,
        "parent_memory": proj_parent,
        "promotion_candidate": proj_promo_candidate,
        "promotion_state": proj_promo_state,
        "provenance": proj_provenance,
        "related_memories": proj_related,
        "schema_version": proj_schema_version,
        "snapshot_id": proj_row_snapshot_id,
        "source": proj_source,
        "source_checksum": proj_source_checksum,
        "source_format": proj_source_format,
        "source_uri": proj_uri,
        "tags": proj_tags,
        "tier": proj_tier,
        "ttl_seconds": proj_ttl,
        "updated_at": proj_updated_at,
    }


def _projection_digest(projection: Mapping[str, Any]) -> str:
    payload = f"{_RECORD_PROJECTION_DOMAIN}:{canonical_dumps(projection)}".encode()
    return sha256_bytes(payload)


def semantic_record_digest(
    raw: Mapping[str, Any],
    *,
    default_source: str = "snapshot",
    normalized: Mapping[str, Any] | None = None,
) -> str:
    """Domain-separated SHA-256 hex digest of a record's canonical semantic projection."""
    proj = semantic_record_projection(raw, default_source=default_source, normalized=normalized)
    return _projection_digest(proj)


def semantic_file_reference_projection(
    ref: FileReference | Mapping[str, Any],
) -> dict[str, Any]:
    """Canonical projection of a manifest ``FileReference`` entry.

    ``attachment`` is intentionally excluded: bundling a byte range into the
    package is a packaging choice (like compression), not portable memory
    state. The referenced bytes remain identified by ``source_uri``,
    ``byte_offset``, ``byte_length``, ``source_format``, and
    ``source_checksum`` (#96).
    """
    if isinstance(ref, FileReference):
        raw_uri = ref.source_uri
        raw_mem_id = ref.memory_id
        raw_offset = ref.byte_offset
        raw_length = ref.byte_length
        raw_fmt = ref.source_format
        raw_chk = ref.source_checksum
    else:
        raw_uri = ref.get("source_uri")
        raw_mem_id = ref.get("memory_id")
        raw_offset = ref.get("byte_offset")
        raw_length = ref.get("byte_length")
        raw_fmt = ref.get("source_format")
        raw_chk = ref.get("source_checksum")

    uri_str = _nfc(str(raw_uri or "").strip())
    norm_uri = normalize_source_uri(uri_str) if uri_str else ""
    return {
        "byte_length": int(raw_length or 0),
        "byte_offset": int(raw_offset or 0),
        "memory_id": _nfc(str(raw_mem_id or "").strip()),
        "source_checksum": _nfc(str(raw_chk or "").strip()),
        "source_format": _nfc(str(raw_fmt or "").strip()),
        "source_uri": norm_uri,
    }


def semantic_scope_projection(scope: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Project semantic namespace scope, excluding transient session/caller counters."""
    if not isinstance(scope, Mapping):
        return None
    raw_ns = scope.get("namespace")
    ns = _nfc(str(raw_ns).strip()) if isinstance(raw_ns, str) and raw_ns.strip() else ""
    raw_permitted = scope.get("permitted_namespaces")
    permitted: list[str] = []
    if isinstance(raw_permitted, (list, tuple, set, frozenset)):
        permitted = sorted(
            {
                _nfc(str(item).strip())
                for item in raw_permitted
                if isinstance(item, str) and str(item).strip()
            }
        )
    if not ns and not permitted:
        return None
    return {
        "namespace": ns,
        "permitted_namespaces": permitted,
    }


def _derive_file_references_from_projections(
    projections: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    derived: list[dict[str, Any]] = []
    for proj in projections:
        if proj["memory_type"] == "file" and proj["source_uri"]:
            derived.append(
                {
                    "byte_length": int(proj["byte_length"] or 0),
                    "byte_offset": int(proj["byte_offset"] or 0),
                    "memory_id": proj["id"],
                    "source_checksum": proj["source_checksum"],
                    "source_format": proj["source_format"],
                    "source_uri": proj["source_uri"],
                }
            )
    return derived


def _sort_file_ref_projections(
    refs: Iterable[FileReference | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    projected = [semantic_file_reference_projection(r) for r in refs]
    projected.sort(
        key=lambda r: (
            r["memory_id"],
            r["source_uri"],
            r["byte_offset"],
            r["byte_length"],
            r["source_format"],
            r["source_checksum"],
        )
    )
    return projected


def semantic_snapshot_projection(
    records: Iterable[Mapping[str, Any]],
    *,
    file_backed_references: Iterable[FileReference | Mapping[str, Any]] | None = None,
    scope: Mapping[str, Any] | None = None,
    default_source: str = "snapshot",
) -> dict[str, Any]:
    """Canonical envelope for Snapshot v2 semantic identity (#96)."""
    projections = [
        semantic_record_projection(record, default_source=default_source) for record in records
    ]
    sorted_digests = sorted(_projection_digest(projection) for projection in projections)
    if file_backed_references is None:
        sorted_refs = _sort_file_ref_projections(
            _derive_file_references_from_projections(projections)
        )
    else:
        sorted_refs = _sort_file_ref_projections(file_backed_references)
    return {
        "file_backed_references": sorted_refs,
        "records": sorted_digests,
        "scope": semantic_scope_projection(scope),
        "version": SNAPSHOT_ID_VERSION,
    }


def compute_semantic_snapshot_id_from_digests(
    record_digests: Iterable[str],
    *,
    file_backed_references: Iterable[FileReference | Mapping[str, Any]] = (),
    scope: Mapping[str, Any] | None = None,
) -> str:
    """Compute the v2 semantic ``snapshot_id`` from precomputed per-record digests."""
    envelope = {
        "file_backed_references": _sort_file_ref_projections(file_backed_references),
        "records": sorted(str(d) for d in record_digests),
        "scope": semantic_scope_projection(scope),
        "version": SNAPSHOT_ID_VERSION,
    }
    payload = f"{_SNAPSHOT_PROJECTION_DOMAIN}:{canonical_dumps(envelope)}".encode()
    return sha256_bytes(payload)


def compute_semantic_snapshot_id(
    records: Iterable[Mapping[str, Any]],
    *,
    file_backed_references: Iterable[FileReference | Mapping[str, Any]] | None = None,
    scope: Mapping[str, Any] | None = None,
    default_source: str = "snapshot",
) -> str:
    """Compute the v2 semantic ``snapshot_id`` over records, attachment refs, and scope."""
    envelope = semantic_snapshot_projection(
        records,
        file_backed_references=file_backed_references,
        scope=scope,
        default_source=default_source,
    )
    payload = f"{_SNAPSHOT_PROJECTION_DOMAIN}:{canonical_dumps(envelope)}".encode()
    return sha256_bytes(payload)


def compute_legacy_snapshot_id(content_hashes: Iterable[str]) -> str:
    """Legacy v1 snapshot id: SHA-256 of the sorted ``content_hash`` list."""
    return sha256_bytes("".join(sorted(str(h) for h in content_hashes if h)).encode())


def compute_snapshot_id(
    items: Iterable[str | Mapping[str, Any]],
    *,
    file_backed_references: Iterable[FileReference | Mapping[str, Any]] | None = None,
    scope: Mapping[str, Any] | None = None,
    version: int | None = None,
) -> str:
    """Deterministic snapshot id with strict, backward-compatible dispatch.

    - ``version=1`` (legacy): ``items`` must be content hashes.
    - ``version=2`` (semantic, #96): ``items`` must be record mappings.
    - ``version=None``: inferred from the item types. Mixed or unsupported
      item types raise instead of silently dropping inputs, and scope/reference
      arguments are rejected for hash lists because they cannot apply to them.
    """
    item_list = list(items)
    has_records = any(isinstance(item, Mapping) for item in item_list)
    has_hashes = any(isinstance(item, str) for item in item_list)

    if has_records and has_hashes:
        raise TypeError("compute_snapshot_id accepts either record mappings or content hashes")
    if version not in (None, LEGACY_SNAPSHOT_ID_VERSION, SNAPSHOT_ID_VERSION):
        raise ValueError(f"unsupported snapshot id version: {version!r}")
    if version == LEGACY_SNAPSHOT_ID_VERSION:
        if has_records:
            raise TypeError("snapshot id version 1 requires content hashes")
        if file_backed_references is not None or scope is not None:
            raise ValueError("legacy snapshot id does not accept scope or file references")
        return compute_legacy_snapshot_id(str(item) for item in item_list)
    if version == SNAPSHOT_ID_VERSION:
        if has_hashes:
            raise TypeError("snapshot id version 2 requires record mappings")
        return compute_semantic_snapshot_id(
            item_list,
            file_backed_references=file_backed_references,
            scope=scope,
        )
    if has_records:
        return compute_semantic_snapshot_id(
            item_list,
            file_backed_references=file_backed_references,
            scope=scope,
        )
    if file_backed_references is not None or scope is not None:
        raise ValueError("scope/file_backed_references require record mappings")
    if any(not isinstance(item, str) for item in item_list):
        raise TypeError("snapshot id items must be record mappings or content hashes")
    return compute_legacy_snapshot_id(item_list)
