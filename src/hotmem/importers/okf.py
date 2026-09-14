"""Google Open Knowledge Format (OKF) v0.2 importer — bundle walker (#68).

Purpose:
     Walk a local OKF v0.2 bundle — a directory tree of markdown concept
     documents with YAML frontmatter — and yield page-level data for
     deterministic HotMem interchange records.

     Validated against the authoritative specification
     (GoogleCloudPlatform/open-knowledge-format, SPEC.md v0.2):
       - §3: bundles are directory trees; reserved filenames index.md /
         log.md are never concepts.
       - §4: frontmatter is a YAML block delimited by --- lines; `type` is
         the only required key.
       - §11: consumers MUST tolerate unknown frontmatter keys, unknown
         type values, broken links, and missing index files — and MUST NOT
         reject optional families.
       - §12: a bundle-root index.md MAY declare okf_version.
       - §13: v0.1 fallbacks (timestamp -> generated.at).

     Safety envelope (#68 acceptance): bounded parsing (per-file size cap),
     safe YAML only (yaml.safe_load, per-file error isolation), root
     confinement (no symlink escapes, no traversal), and zero network —
     nothing is fetched; source URIs are recorded, never dereferenced.

Interface:
      OkfWarning(path, message)
      PageData(rel_path, concept_id, frontmatter, body, sha256, size)
      parse_frontmatter(text) -> (frontmatter, body) | None
      read_bundle_metadata(root) -> {"okf_version": ...}
      iter_okf_pages(root, warnings) -> Iterator[PageData]

Deps: stdlib + optional PyYAML (extra `okf`; lazy import with an actionable
      error when missing).
Extension: record mapping lives in iter_okf_records (same module, #68);
      living-wiki conventions are documented in docs/cli.md.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hotmem.interchange.canonical import compute_content_hash
from hotmem.trace import get_tracer

_trace = get_tracer("importers.okf")

# Per-file size cap — same bound as bundle.py (16 MiB) — bounded parsing.
MAX_PAGE_SIZE = 16 * 1024 * 1024

INDEX_MD = "index.md"
LOG_MD = "log.md"
RESERVED_FILENAMES = frozenset({INDEX_MD, LOG_MD})


class OkfWarning:
    """A per-page or bundle-level warning; never fatal on its own."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class PageData:
    """One parsed OKF concept page."""

    rel_path: str  # bundle-relative posix path, e.g. "tables/orders.md"
    concept_id: str  # §2: rel path without .md, e.g. "tables/orders"
    frontmatter: dict[str, Any]
    body: str
    sha256: str  # SHA-256 of the raw file bytes (source hash)
    size: int


def _require_yaml() -> Any:
    """Import PyYAML lazily with an actionable error (safe_load only)."""
    try:
        import yaml
    except ImportError as err:  # pragma: no cover - depends on env
        raise ImportError(
            "OKF import requires PyYAML. Install it with: pip install 'hotmem[okf]'"
        ) from err
    return yaml


def _jsonify_yaml(value: Any) -> Any:
    """Recursively convert YAML-parsed scalars to JSON-native values.

    PyYAML auto-parses ISO-8601 timestamps into ``datetime`` objects; records
    must be JSON-native and deterministic, so datetimes become ISO-8601
    strings again (``Z`` for UTC, matching the OKF spelling). Dates, dicts,
    and lists recurse; everything else passes through.
    """
    import datetime as _dt

    if isinstance(value, dict):
        return {str(k): _jsonify_yaml(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify_yaml(v) for v in value]
    if isinstance(value, _dt.datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    """Split a page into (frontmatter, body); None when no frontmatter block.

    Per §4: the file starts with a `---` line and the block closes with a
    `---` line. Everything after the closing line is the body.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            yaml_text = "\n".join(lines[1:idx])
            # Body: everything after the closing delimiter line. A single
            # trailing newline terminator is standard; keep interior blank
            # lines verbatim so content is faithful.
            body = "\n".join(lines[idx + 1 :])
            if body.startswith("\n"):
                body = body[1:]
            yaml_mod = _require_yaml()
            try:
                frontmatter = yaml_mod.safe_load(yaml_text)
            except Exception as err:  # yaml.YAMLError and friends
                raise ValueError(f"frontmatter parse error: {err}") from err
            if frontmatter is None:
                frontmatter = {}
            if not isinstance(frontmatter, dict):
                raise ValueError(
                    f"frontmatter must be a YAML mapping, got {type(frontmatter).__name__}"
                )
            return _jsonify_yaml(frontmatter), body
    return None


def read_bundle_metadata(root: Path) -> dict[str, Any]:
    """Read bundle-level metadata from a bundle-root index.md (§12).

    The root index.md is the only index allowed frontmatter, and only the
    okf_version key is meaningful there. Never a concept page.
    """
    root = root.resolve()
    index_path = root / INDEX_MD
    meta: dict[str, Any] = {}
    if not index_path.is_file():
        return meta
    try:
        text = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return meta
    parsed = parse_frontmatter(text)
    if parsed is None:
        return meta
    frontmatter, _body = parsed
    version = frontmatter.get("okf_version")
    if version:
        meta["okf_version"] = str(version)
    return meta


def _confined(root: Path, candidate: Path) -> bool:
    """Root confinement: the page must resolve inside the bundle root.

    Symlinks that escape the root are rejected (#68 acceptance: files
    outside the selected root are never read). ``candidate`` is a directory
    entry from the walk, so an escape can only happen via a symlinked file.
    """
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return False
    return True


def iter_okf_pages(
    root: str | Path,
    warnings: list[OkfWarning],
    *,
    max_file_size: int = MAX_PAGE_SIZE,
) -> Iterator[PageData]:
    """Yield parsed OKF concept pages in deterministic sorted order.

    Pages with malformed frontmatter, oversized files, or non-dict YAML
    produce a warning and are skipped — handled safely, never a crash
    (#68). Reserved filenames (index.md, log.md) are never concepts (§3.1).

    Candidates are collected as paths and globally sorted (by bundle-relative
    path) before any file is read, so output order is byte-stable regardless
    of directory layout while memory stays O(paths).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not an OKF bundle directory: {root}")

    candidates: list[str] = []
    for dirpath, dirnames, filenames in os_walk_sorted(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".md") or name in RESERVED_FILENAMES:
                continue
            candidates.append((dirpath / name).relative_to(root).as_posix())
    candidates.sort()

    for rel in candidates:
        path = root / rel
        concept_id = rel[: -len(".md")]

        if not _confined(root, path):
            warnings.append(OkfWarning(rel, "resolves outside the bundle root (symlink?); skipped"))
            continue
        try:
            size = path.stat().st_size
        except OSError as err:
            warnings.append(OkfWarning(rel, f"stat error: {err}"))
            continue
        if size > max_file_size:
            warnings.append(
                OkfWarning(rel, f"file too large ({size} > {max_file_size} bytes); skipped")
            )
            continue
        try:
            raw = path.read_bytes()
        except OSError as err:
            warnings.append(OkfWarning(rel, f"read error: {err}"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(OkfWarning(rel, "not valid UTF-8; skipped"))
            continue

        sha256 = hashlib.sha256(raw).hexdigest()
        try:
            parsed = parse_frontmatter(text)
        except ValueError as err:
            warnings.append(OkfWarning(rel, str(err)))
            continue
        if parsed is None:
            warnings.append(OkfWarning(rel, "missing frontmatter block; skipped"))
            continue
        frontmatter, body = parsed
        if not frontmatter.get("type"):
            warnings.append(OkfWarning(rel, "frontmatter missing required 'type'; skipped"))
            continue

        yield PageData(
            rel_path=rel,
            concept_id=concept_id,
            frontmatter=frontmatter,
            body=body,
            sha256=sha256,
            size=size,
        )


def os_walk_sorted(root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    """os.walk with sorted dirs and files — deterministic page order.

    Sorted order matters twice: it makes the importer's output byte-stable,
    and it matches the "sorted Markdown pages" requirement of #68.
    """
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        yield Path(dirpath), dirnames, filenames


def is_external_target(target: str) -> bool:
    """True when a markdown link target is external (never dereferenced)."""
    if target.startswith(("#", "mailto:")):
        return True
    try:
        return urlparse(target).scheme in ("http", "https", "ftp", "file")
    except ValueError:
        return True


# ── Concept pages → interchange records (#68) ───────────────────────────────

# Markdown links: [text](target "title"?) — negative lookbehind excludes
# images (![alt](src)). Per §6.1 links assert untyped relationships; broken
# targets are tolerated (never validated).
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _normalize_link(page_rel: str, target: str) -> str | None:
    """Normalize a link target to a bundle-relative concept path (§6).

    Absolute (``/tables/x.md``) resolves against the bundle root; relative
    (``./x.md``, ``../y.md``) against the page's directory. Fragments are
    dropped. Returns a concept id (``.md`` stripped) or None for
    non-pathlike targets.
    """
    target = target.split("#", 1)[0].strip()
    if not target or is_external_target(target):
        return None
    page_dir = posixpath.dirname(page_rel)
    if target.startswith("/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(page_dir, target))
    if normalized.startswith("../") or normalized == "..":
        return None  # escapes the bundle root — tolerate silently (broken link)
    if normalized.endswith(".md"):
        normalized = normalized[: -len(".md")]
    return normalized or None


def extract_links(page_rel: str, body: str) -> list[str]:
    """Deterministic list of concept ids the page links to (sorted, unique)."""
    targets = {
        norm
        for match in _LINK_RE.findall(body)
        if (norm := _normalize_link(page_rel, match)) is not None
    }
    return sorted(targets)


def derive_trust_tier(verified: list[dict[str, Any]] | None) -> str:
    """OKF §5.3: human-reviewed > machine-confirmed > unverified."""
    if not verified:
        return "unverified"
    for entry in verified:
        if isinstance(entry, dict) and str(entry.get("by", "")).startswith("human:"):
            return "human-reviewed"
    return "machine-confirmed"


def normalize_verified(fm: dict[str, Any]) -> list[dict[str, Any]] | None:
    """OKF §5.2: a bare {by, at} mapping counts as a one-element list."""
    if "verified" not in fm:
        return None
    value = fm["verified"]
    if value is None:
        return None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _okf_metadata(
    page: PageData, fm: dict[str, Any], verified: list[dict[str, Any]] | None
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "type": str(fm.get("type", "")),
        "status": str(fm.get("status") or "stable"),  # §5.4: absent ⇒ stable
        "trust_tier": derive_trust_tier(verified),
    }
    for key in ("title", "resource", "stale_after"):
        if fm.get(key) is not None:
            meta[key] = fm[key]
    links = extract_links(page.rel_path, page.body)
    if links:
        meta["links"] = links
    if "references" in Path(page.rel_path).parts:
        meta["in_references_dir"] = True
    return meta


def _okf_provenance(fm: dict[str, Any]) -> dict[str, Any] | None:
    """Temporal + source provenance (§5.1–5.2, §13.1 fallback), verbatim."""
    prov: dict[str, Any] = {}
    generated = fm.get("generated")
    if isinstance(generated, dict):
        prov["generated"] = generated
    elif fm.get("timestamp") is not None:
        # §13.1: v0.1 timestamp supersedes to generated.at; mark the fallback.
        prov["generated"] = {"by": "okf:v0.1-timestamp-fallback", "at": fm["timestamp"]}
    verified = normalize_verified(fm)
    if verified is not None:
        prov["verified"] = verified
    for key in ("sources", "usage_window"):
        if fm.get(key) is not None:
            prov[key] = fm[key]
    return prov or None


def page_to_record(
    page: PageData,
    *,
    namespace: str,
    okf_version: str | None = None,
) -> dict[str, Any]:
    """Map one OKF concept page to a canonical interchange record (#68).

    Deterministic: id is hash-derived from (concept_id, content_hash), so the
    same bundle always produces byte-identical records. The record carries no
    embedding — hydration embeds from fact_text (compiled knowledge), while
    raw sources stay provenance (sources[] / references/) and are never
    inlined or fetched.
    """
    fm = page.frontmatter
    verified = normalize_verified(fm)
    content_hash = compute_content_hash(page.concept_id, page.body)

    record: dict[str, Any] = {
        "schema_version": 1,
        "id": hashlib.sha256(f"okf:{page.concept_id}:{content_hash}".encode()).hexdigest(),
        "identifier": page.concept_id,
        "fact_text": page.body,
        "fact_summary": fm.get("description") or fm.get("title") or None,
        "memory_type": "fact",
        "source": "okf:references" if "references" in Path(page.rel_path).parts else "okf",
        "importance": 0.5,  # no silent inference; trust signals live in metadata
        "metadata": {"okf": _okf_metadata(page, fm, verified)},
        "content_hash": content_hash,
        "namespace": namespace,
        "tier": "hot",
        "tags": [str(t) for t in fm["tags"]] if isinstance(fm.get("tags"), list) else [],
        "source_uri": page.rel_path,
        "source_format": "md",
        "source_checksum": page.sha256,
    }
    if okf_version:
        record["metadata"]["okf"]["okf_version"] = okf_version
    provenance = _okf_provenance(fm)
    if provenance:
        record["provenance"] = provenance
    return record


def iter_okf_records(
    root: str | Path,
    *,
    namespace: str | None = None,
    warnings: list[OkfWarning] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield deterministic interchange records for every page of a bundle.

    The reviewable JSONL output (#68): sorted pages, canonical field set,
    hash-derived ids — the same bundle produces byte-identical lines.
    """
    root_path = Path(root)
    own_warnings: list[OkfWarning] = []
    if warnings is None:
        warnings = own_warnings

    meta = read_bundle_metadata(root_path)
    okf_version = meta.get("okf_version")
    ns = namespace if namespace is not None else root_path.resolve().name

    count = 0
    for page in iter_okf_pages(root_path, warnings):
        yield page_to_record(page, namespace=ns, okf_version=okf_version)
        count += 1

    _trace.info(
        "okf_import",
        f"mapped {count} pages to records",
        detail={"root": str(root_path), "warnings": len(warnings), "namespace": ns},
    )


def read_okf(path: str | Path) -> Iterator[dict[str, Any]]:
    """Registry-compatible reader: OKF bundle path -> record dicts.

    Same interface as the mem0 importer so `hotmem import --from okf`
    dispatches without CLI changes. Warnings are trace-logged.
    """
    warnings: list[OkfWarning] = []
    yield from iter_okf_records(path, warnings=warnings)
    for warning in warnings:
        _trace.warn("okf_import", str(warning), detail={"path": warning.path})
