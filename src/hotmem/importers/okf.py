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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
            return frontmatter, body
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
