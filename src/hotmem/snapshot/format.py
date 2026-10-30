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
     Manifest, FileEntry, FileReference, MetadataInfo (dataclasses)
     SnapshotChecksumError
     sha256_bytes(data) -> str
     sha256_file(path) -> str
     compute_overall(per_file_hashes) -> str
     compute_snapshot_id(content_hashes) -> str

Deps: none (stdlib only)
Extension: bump SCHEMA_VERSION and add fields here for future formats.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

# ── Format constants ─────────────────────────────────────────────────────────

FORMAT = "hotmem-snapshot-v2"
SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"

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
        if self.scope is not None:
            data["scope"] = self.scope
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

        return cls(
            format=str(raw_format or FORMAT),
            schema_version=schema_version,
            snapshot_id=str(data.get("snapshot_id") or ""),
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
        )


# ── Checksum helpers ─────────────────────────────────────────────────────────


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


def compute_snapshot_id(content_hashes: list[str]) -> str:
    """Deterministic snapshot id: SHA-256 of the sorted content_hash list.

    Two snapshots of the same DB produce the same ``snapshot_id`` regardless
    of insert order, so identical inputs yield identical manifests.
    """
    return sha256_bytes("".join(sorted(content_hashes)).encode())
