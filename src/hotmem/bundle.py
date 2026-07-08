"""HotMem bundle — loose local markdown bundle reader.

Purpose:
     Read a permissive, human-authored local memory bundle into HotMem.
     Bundles make filesystem-native memory inspectable without forcing a
     strict schema too early. The reader accepts simple local authoring
     patterns first; stricter validation is deferred (OKF progressive
     strictness).

Bundle layout (all optional except ``memory.md``)::

     bundle/
       memory.md          # required — markdown content (becomes fact_text)
       metadata.yaml      # optional — identifier, importance, tags, provenance
       metadata.json      # optional — same as yaml, JSON alternative
       facts.json         # optional — array of additional inline facts
       events.jsonl       # optional — one event per line (inline memories)
       attachments/       # optional — referenced files (file-backed, not copied)
       manifest.json      # optional — stricter metadata (read, not enforced)

Unknown files are ignored in permissive mode. Attachments are referenced
by URI/path, not copied into SQLite (reference-not-duplicate, #38).

Interface:
     detect_bundle(path) -> bool
     read_bundle(db, bundle_dir, *, base_dir=None) -> BundleResult

Deps: hotmem.db, hotmem.embed, hotmem.swap, hotmem.storage, hotmem.trace
Extension: add strict validation mode, remote bundles, or bundle indexing here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotmem.db import MemoryDB, MemoryRecord
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.swap import HydrateResult, compute_content_hash
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("bundle")

MEMORY_MD = "memory.md"
INDEX_MD = "index.md"
README_MD = "README.md"
MEMORY_BODY_FILES: tuple[str, ...] = (MEMORY_MD, INDEX_MD, README_MD)
METADATA_YAML = "metadata.yaml"
METADATA_JSON = "metadata.json"
FACTS_JSON = "facts.json"
EVENTS_JSONL = "events.jsonl"
ATTACHMENTS_DIR = "attachments"
MANIFEST_JSON = "manifest.json"

_KNOWN_FILES: frozenset[str] = frozenset(
    {
        MEMORY_MD,
        INDEX_MD,
        README_MD,
        METADATA_YAML,
        METADATA_JSON,
        FACTS_JSON,
        EVENTS_JSONL,
        MANIFEST_JSON,
    }
)


@dataclass
class BundleWarning:
    """A permissive-mode warning about an ambiguous or partially invalid bundle."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class BundleResult:
    """Result of reading a bundle into HotMem."""

    loaded: int = 0
    skipped_dupes: int = 0
    warnings: list[BundleWarning] = field(default_factory=list)

    @property
    def as_hydrate_result(self) -> HydrateResult:
        """Compatibility with the snapshot dispatch HydrateResult."""
        return HydrateResult(loaded=self.loaded, skipped_dupes=self.skipped_dupes)


def detect_bundle(path: str | Path) -> bool:
    """True if ``path`` is a directory containing a memory body file.

    Accepts ``memory.md`` (preferred), ``index.md``, or ``README.md`` as
    the memory body. The first match wins; ``memory.md`` takes precedence.
    """
    p = Path(path)
    if not p.is_dir():
        return False
    return any((p / name).is_file() for name in MEMORY_BODY_FILES)


def _find_memory_body(bundle_dir: Path) -> Path | None:
    """Find the memory body file; memory.md > index.md > README.md."""
    for name in MEMORY_BODY_FILES:
        candidate = bundle_dir / name
        if candidate.is_file():
            return candidate
    return None


def parse_bundle(
    bundle_path: str | Path,
    *,
    strict: bool = False,
) -> tuple[list[MemoryRecord], list[BundleWarning]]:
    """Parse a bundle directory into MemoryRecord objects without DB insertion.

    Pure function: reads files, produces records + warnings. The caller
    hydrates via ``db.insert_many_ignore(records)``.

    Args:
        strict: when True, fail on unknown files and missing attachments
            (deferred — currently raises NotImplementedError).
    """
    if strict:
        raise NotImplementedError("strict bundle validation is not yet implemented")

    bundle_dir = Path(bundle_path).resolve()
    warnings: list[BundleWarning] = []
    records: list[MemoryRecord] = []

    if not detect_bundle(bundle_dir):
        raise FileNotFoundError(
            f"not a bundle (missing {MEMORY_MD}/{INDEX_MD}/{README_MD}): {bundle_dir}"
        )

    # Load metadata (json preferred, yaml fallback).
    metadata = _load_metadata(bundle_dir, warnings)
    identifier = metadata.get("identifier") or bundle_dir.name

    # --- memory body → inline memory ---
    body_path = _find_memory_body(bundle_dir)
    if body_path is None:
        raise FileNotFoundError(f"no memory body file in bundle: {bundle_dir}")

    body_content = body_path.read_text(encoding="utf-8")
    content_hash = compute_content_hash(identifier, body_content)
    blob = pack_embedding(embed_text(body_content))
    records.append(
        MemoryRecord(
            id=uuid.uuid4().hex,
            identifier=identifier,
            fact_text=body_content,
            embedding=blob,
            embedding_dim=EMBEDDING_DIM,
            embedding_model=EMBEDDING_MODEL,
            source=metadata.get("source", "bundle"),
            importance=metadata.get("importance", 0.5),
            metadata_json=json.dumps(metadata.get("metadata", {})),
            content_hash=content_hash,
            namespace=metadata.get("namespace", ""),
            tier=metadata.get("tier", "hot"),
            tags=json.dumps(metadata.get("tags", [])),
            fact_summary=metadata.get("summary"),
            provenance_json=json.dumps(metadata["provenance"])
            if metadata.get("provenance")
            else None,
        )
    )

    # --- facts.json → additional inline facts ---
    records.extend(_parse_facts(bundle_dir, identifier, warnings))

    # --- events.jsonl → event memories ---
    records.extend(_parse_events(bundle_dir, identifier, warnings))

    # --- attachments/ → file-backed references ---
    records.extend(_parse_attachments(bundle_dir, identifier, warnings))

    # --- manifest.json → read as additional metadata (not enforced) ---
    _load_manifest(bundle_dir, warnings)

    # --- unknown files → warn (permissive: ignored) ---
    _warn_unknown_files(bundle_dir, warnings)

    _trace.info(
        "parse_bundle",
        f"parsed {len(records)} records, {len(warnings)} warnings",
        detail={"bundle_dir": str(bundle_dir)},
    )
    return records, warnings


def read_bundle(
    db: MemoryDB,
    bundle_dir: str | Path,
    *,
    base_dir: str | Path | None = None,
) -> BundleResult:
    """Read a loose local markdown bundle into HotMem.

    Thin wrapper around ``parse_bundle()`` that inserts records via
    ``db.insert_many_ignore()``. Returns a ``BundleResult`` with loaded
    count, skipped dupes, and warnings.
    """
    bundle_dir = Path(bundle_dir)
    with Timer() as t:
        records, warnings = parse_bundle(bundle_dir)
        loaded = db.insert_many_ignore(records)
        skipped = len(records) - loaded

    _trace.info(
        "read_bundle",
        f"hydrated bundle: {loaded} loaded, {skipped} dupes",
        detail={
            "bundle_dir": str(bundle_dir),
            "warnings": len(warnings),
            "ms": round(t.ms, 2),
        },
    )
    return BundleResult(loaded=loaded, skipped_dupes=skipped, warnings=warnings)


def _load_metadata(bundle_dir: Path, warnings: list[BundleWarning]) -> dict[str, Any]:
    """Load metadata from ``metadata.json`` (preferred) or ``metadata.yaml``."""
    json_path = bundle_dir / METADATA_JSON
    yaml_path = bundle_dir / METADATA_YAML

    if json_path.is_file():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            warnings.append(BundleWarning(str(json_path), f"parse error: {err}"))
            return {}

    if yaml_path.is_file():
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            warnings.append(
                BundleWarning(
                    str(yaml_path),
                    f"PyYAML not installed; use {METADATA_JSON} instead. Skipping metadata.",
                )
            )
            return {}
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as err:
            warnings.append(BundleWarning(str(yaml_path), f"parse error: {err}"))
            return {}

    return {}


def _parse_facts(
    bundle_dir: Path,
    default_identifier: str,
    warnings: list[BundleWarning],
) -> list[MemoryRecord]:
    """Parse ``facts.json`` into MemoryRecord objects (no DB insertion)."""
    facts_path = bundle_dir / FACTS_JSON
    if not facts_path.is_file():
        return []

    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        warnings.append(BundleWarning(str(facts_path), f"parse error: {err}"))
        return []

    if not isinstance(facts, list):
        warnings.append(
            BundleWarning(str(facts_path), f"expected array, got {type(facts).__name__}")
        )
        return []

    records: list[MemoryRecord] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_text = fact.get("fact") or fact.get("fact_text") or ""
        if not fact_text:
            continue
        identifier = fact.get("identifier") or default_identifier
        content_hash = compute_content_hash(identifier, fact_text)
        blob = pack_embedding(embed_text(fact_text))
        records.append(
            MemoryRecord(
                id=fact.get("id") or uuid.uuid4().hex,
                identifier=identifier,
                fact_text=fact_text,
                embedding=blob,
                embedding_dim=EMBEDDING_DIM,
                embedding_model=EMBEDDING_MODEL,
                source=fact.get("source", "bundle:facts"),
                importance=fact.get("importance", 0.5),
                metadata_json=json.dumps(fact.get("metadata", {})),
                content_hash=content_hash,
                tags=json.dumps(fact.get("tags", [])),
            )
        )
    return records


def _parse_events(
    bundle_dir: Path,
    default_identifier: str,
    warnings: list[BundleWarning],
) -> list[MemoryRecord]:
    """Parse ``events.jsonl`` into MemoryRecord objects (no DB insertion)."""
    events_path = bundle_dir / EVENTS_JSONL
    if not events_path.is_file():
        return []

    records: list[MemoryRecord] = []
    try:
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                fact_text = event.get("event") or event.get("fact_text") or event.get("text") or ""
                if not fact_text:
                    continue
                identifier = event.get("identifier") or default_identifier
                content_hash = compute_content_hash(identifier, fact_text)
                blob = pack_embedding(embed_text(fact_text))
                records.append(
                    MemoryRecord(
                        id=event.get("id") or uuid.uuid4().hex,
                        identifier=identifier,
                        fact_text=fact_text,
                        embedding=blob,
                        embedding_dim=EMBEDDING_DIM,
                        embedding_model=EMBEDDING_MODEL,
                        source=event.get("source", "bundle:events"),
                        importance=event.get("importance", 0.5),
                        metadata_json=json.dumps(event.get("metadata", {})),
                        content_hash=content_hash,
                    )
                )
    except OSError as err:
        warnings.append(BundleWarning(str(events_path), f"read error: {err}"))

    return records


def _parse_attachments(
    bundle_dir: Path,
    default_identifier: str,
    warnings: list[BundleWarning],
) -> list[MemoryRecord]:
    """Create file-backed MemoryRecord objects for files in ``attachments/``.

    Attachments are referenced by URI/path, NOT copied into SQLite
    (reference-not-duplicate principle, #38).
    """
    att_dir = bundle_dir / ATTACHMENTS_DIR
    if not att_dir.is_dir():
        return []

    records: list[MemoryRecord] = []
    for att in sorted(att_dir.iterdir()):
        if not att.is_file():
            continue
        uri = str(att.resolve())
        try:
            size = att.stat().st_size
        except OSError as err:
            warnings.append(BundleWarning(f"attachments/{att.name}", f"stat error: {err}"))
            continue

        content_hash = hashlib.sha256(f"{default_identifier}:{uri}:0:{size}".encode()).hexdigest()

        source_format = att.suffix.lstrip(".") or "bin"

        records.append(
            MemoryRecord(
                id=uuid.uuid4().hex,
                identifier=f"{default_identifier}:{att.name}",
                fact_text="",
                embedding=b"",
                embedding_dim=EMBEDDING_DIM,
                embedding_model="",
                source="bundle:attachments",
                content_hash=content_hash,
                memory_type="file",
                source_uri=uri,
                source_format=source_format,
                byte_offset=0,
                byte_length=size,
                fact_summary=att.name,
            )
        )
    return records


def _load_manifest(bundle_dir: Path, warnings: list[BundleWarning]) -> None:
    """Read ``manifest.json`` as additional metadata (not enforced in permissive mode)."""
    manifest_path = bundle_dir / MANIFEST_JSON
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        warnings.append(BundleWarning(str(manifest_path), f"parse error: {err}"))
        return
    _trace.debug(
        "manifest",
        "bundle manifest present (permissive: not enforced)",
        detail={"keys": list(manifest.keys()) if isinstance(manifest, dict) else "non-dict"},
    )


def _warn_unknown_files(bundle_dir: Path, warnings: list[BundleWarning]) -> None:
    """Emit warnings for files not in the known set (permissive: ignored)."""
    for entry in sorted(bundle_dir.iterdir()):
        if entry.is_file() and entry.name not in _KNOWN_FILES:
            warnings.append(
                BundleWarning(
                    str(entry),
                    "unknown file ignored (use strict mode to enforce bundle schema)",
                )
            )
