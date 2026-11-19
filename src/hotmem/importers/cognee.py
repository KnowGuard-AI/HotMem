"""Import Cognee COGX exports as HotMem swap records.

The reader accepts the COGX directory emitted by ``cognee.export(...,
format="cogx")`` and Cognee's packed ``.cogx.tar.gz`` transport. It translates
typed COGX records into textual HotMem memories; graph relations remain in
record metadata and are not reconstructed as a graph.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from hotmem.trace import get_tracer

_trace = get_tracer("importers.cognee")

_RECORD_FILES = {
    "document": "documents.jsonl",
    "episode": "episodes.jsonl",
    "entity": "entities.jsonl",
    "fact": "facts.jsonl",
    "memory": "memories.jsonl",
    "memory_block": "memory_blocks.jsonl",
    "raw_node": "nodes.jsonl",
}
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_RECORD_BYTES = 512 * 1024 * 1024


def _confined(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return not path.is_symlink()


def _extract_archive(archive_path: Path, destination: Path) -> None:
    total_bytes = 0
    member_count = 0
    root = destination.resolve()
    try:
        archive_context = tarfile.open(archive_path, mode="r:gz")  # noqa: SIM115 - entered below
    except (OSError, tarfile.TarError) as err:
        raise ValueError(f"not a valid Cognee COGX tar.gz archive: {err}") from err

    with archive_context as archive:
        for member in archive:
            member_count += 1
            if member_count > _MAX_ARCHIVE_MEMBERS:
                raise ValueError("Cognee COGX archive has too many entries")
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.name:
                raise ValueError(f"unsafe path in Cognee COGX archive: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"unsupported entry in Cognee COGX archive: {member.name!r}")
            if member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(f"Cognee COGX archive member is too large: {member.name!r}")
            total_bytes += member.size
            if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Cognee COGX archive exceeds the total uncompressed size limit")

            target = root.joinpath(*relative.parts)
            try:
                target.resolve().relative_to(root)
            except ValueError as err:
                raise ValueError(f"unsafe path in Cognee COGX archive: {member.name!r}") from err
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists():
                raise ValueError(f"duplicate path in Cognee COGX archive: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"unable to read Cognee COGX archive member: {member.name!r}")
            written = 0
            with source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > member.size:
                        raise ValueError(
                            f"invalid size for Cognee COGX archive member: {member.name!r}"
                        )
                    output.write(chunk)
            if written != member.size:
                raise ValueError(f"truncated Cognee COGX archive member: {member.name!r}")


@contextlib.contextmanager
def _cogx_directory(path: Path) -> Iterator[Path]:
    if path.is_dir():
        yield path
        return
    if not path.is_file() or not path.name.endswith(".cogx.tar.gz"):
        raise ValueError("Cognee source must be a COGX export directory or a .cogx.tar.gz archive")
    with tempfile.TemporaryDirectory(prefix="hotmem_cognee_") as temporary:
        directory = Path(temporary)
        _extract_archive(path, directory)
        yield directory


def _read_manifest(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    if not _confined(directory, manifest_path):
        raise ValueError("Cognee COGX export is missing a safe manifest.json")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("Cognee COGX manifest is too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"invalid Cognee COGX manifest: {err}") from err
    if not isinstance(manifest, dict):
        raise ValueError("Cognee COGX manifest must be a JSON object")
    version = manifest.get("cogx_version")
    try:
        major = int(str(version).split(".", maxsplit=1)[0])
    except (TypeError, ValueError) as err:
        raise ValueError(f"unrecognized Cognee COGX version: {version!r}") from err
    if major > 0:
        raise ValueError(f"Cognee COGX major version {major} is not supported")
    if manifest.get("source_system") != "cognee":
        raise ValueError("COGX manifest source_system must be 'cognee'")
    counts = manifest.get("counts", {})
    if not isinstance(counts, dict) or any(
        not isinstance(name, str)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for name, count in counts.items()
    ):
        raise ValueError("Cognee COGX manifest counts must be non-negative integers")
    unknown = {name for name, count in counts.items() if count and name not in _RECORD_FILES}
    if unknown:
        raise ValueError(f"unsupported Cognee COGX record kinds: {sorted(unknown)}")
    return manifest


def _checksum(line: str) -> str:
    # The upstream writer emits compact JSON and hashes that same serialization
    # without the checksum field. Preserve the original value bytes: Python's
    # JSON encoder can render valid floats differently from Pydantic's encoder.
    decoder = json.JSONDecoder()
    source = line.strip()
    position = 1
    fields = []
    while position < len(source) - 1:
        start = position
        key, position = decoder.raw_decode(source, position)
        if position >= len(source) or source[position] != ":":
            raise ValueError("invalid Cognee checksum serialization")
        _, position = decoder.raw_decode(source, position + 1)
        if key != "checksum":
            fields.append(source[start:position])
        if source[position] == ",":
            position += 1
        elif source[position] != "}":
            raise ValueError("invalid Cognee checksum serialization")
    encoded = "{" + ",".join(fields) + "}"
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_text(kind: str, record: dict[str, Any]) -> str | None:
    if kind in {"document", "memory"}:
        text = record.get("content")
    elif kind == "memory_block":
        text = record.get("value")
    elif kind == "episode":
        turns = record.get("turns")
        if not isinstance(turns, list):
            raise ValueError("Cognee episode turns must be an array")
        rendered = []
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("content"), str):
                raise ValueError("Cognee episode turn must include text content")
            role = turn.get("role")
            rendered.append(f"{role}: {turn['content']}" if role else turn["content"])
        text = "\n".join(rendered)
    elif kind == "entity":
        name = record.get("name")
        description = record.get("description")
        text = f"{name}: {description}" if name and description else name
    elif kind == "fact":
        text = record.get("fact_text")
        if not text:
            subject = record.get("subject_ref")
            predicate = record.get("predicate")
            object_ = record.get("object_ref")
            text = " ".join(str(value) for value in (subject, predicate, object_) if value)
    else:
        properties = record.get("properties", record)
        if not isinstance(properties, dict):
            raise ValueError("Cognee raw node properties must be a JSON object")
        text = next(
            (
                properties.get(field)
                for field in ("text", "content", "description", "name", "label", "title")
                if isinstance(properties.get(field), str) and properties[field].strip()
            ),
            None,
        )
    if text is not None and not isinstance(text, str):
        raise ValueError(f"Cognee {kind} text must be a string")
    return text.strip() if isinstance(text, str) and text.strip() else None


def _to_swap_record(kind: str, record: dict[str, Any], fact_text: str) -> dict[str, Any]:
    scope = record.get("scope") or {}
    if not isinstance(scope, dict):
        raise ValueError("Cognee COGX scope must be a JSON object")
    identifier = next(
        (
            str(scope[key])
            for key in ("user_id", "agent_id", "session_id", "run_id")
            if isinstance(scope.get(key), str) and scope[key]
        ),
        str(record.get("external_system") or "cognee"),
    )
    extra = {
        "kind": kind,
        "external_system": record.get("external_system"),
        "external_id": record.get("external_id") or record.get("id"),
        "scope": scope,
        "metadata": record.get("metadata") or {},
    }
    for field in (
        "origin",
        "semantic_kind",
        "valid_from",
        "valid_to",
        "invalidated_by",
        "evidence",
        "confidence",
        "categories",
        "subject_ref",
        "predicate",
        "object_ref",
        "title",
        "mime_type",
        "label",
        "limit",
    ):
        if field in record:
            extra[field] = record[field]
    tags = record.get("tags") or []
    categories = record.get("categories") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Cognee COGX tags must be an array of strings")
    if not isinstance(categories, list) or not all(
        isinstance(category, str) for category in categories
    ):
        raise ValueError("Cognee COGX categories must be an array of strings")
    return {
        "identifier": identifier,
        "fact_text": fact_text,
        "source": "cognee",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "tags": list(dict.fromkeys([*tags, *categories])),
        "metadata": {"cognee": extra},
    }


def read_cognee_export(
    path: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield text-bearing records from a Cognee COGX 0.x export.

    Typed records become one HotMem memory apiece. A raw graph node is emitted
    only when it has a text, content, description, name, label, or title field.
    COGX row checksums are verified when present; permissions.json is ignored.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Cognee COGX source not found: {path}")

    emitted = 0
    with _cogx_directory(path) as directory:
        manifest = _read_manifest(directory)
        counts = manifest.get("counts", {})
        for kind, file_name in _RECORD_FILES.items():
            expected = counts.get(kind, 0)
            file_path = directory / file_name
            if not file_path.exists():
                if expected:
                    raise ValueError(f"Cognee COGX export is missing {file_name}")
                continue
            if not _confined(directory, file_path) or not file_path.is_file():
                raise ValueError(f"unsafe Cognee COGX record file: {file_name}")
            found = 0
            try:
                with file_path.open(encoding="utf-8") as source:
                    line_number = 0
                    while line := source.readline(_MAX_RECORD_BYTES + 1):
                        line_number += 1
                        if len(line.encode("utf-8")) > _MAX_RECORD_BYTES:
                            raise ValueError(
                                f"Cognee record is too large in {file_name}:{line_number}"
                            )
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as err:
                            raise ValueError(
                                f"invalid JSON in {file_name}:{line_number}: {err}"
                            ) from err
                        if not isinstance(record, dict):
                            raise ValueError(f"expected an object in {file_name}:{line_number}")
                        record_kind = "raw_node" if kind == "raw_node" else record.get("kind")
                        if record_kind != kind:
                            raise ValueError(
                                f"record kind mismatch in {file_name}:{line_number}: "
                                f"expected {kind!r}, got {record_kind!r}"
                            )
                        checksum = record.get("checksum")
                        if checksum is not None and checksum != _checksum(line):
                            raise ValueError(f"checksum mismatch in {file_name}:{line_number}")
                        found += 1
                        text = _record_text(kind, record)
                        if text is None:
                            continue
                        emitted += 1
                        yield _to_swap_record(kind, record, text)
                        if on_progress is not None:
                            on_progress(emitted)
            except UnicodeDecodeError as err:
                raise ValueError(f"invalid UTF-8 in Cognee COGX file {file_name}") from err
            except OSError as err:
                raise ValueError(f"unable to read Cognee COGX file {file_name}: {err}") from err
            if found != expected:
                raise ValueError(
                    f"Cognee COGX count mismatch for {kind}: "
                    f"manifest says {expected}, found {found}"
                )

    _trace.info(
        "importers.cognee",
        f"read Cognee COGX export, emitted {emitted} textual records",
        detail={"path": str(path), "count": emitted},
    )
