"""hotmem-handoff-v1 package writer — manifest, payloads, atomic publish (#101).

Purpose:
    Assemble the canonical handoff package (handoff-v1 §2): an ordered
    session stream, the selected durable memories in interchange format, a
    bounded resume brief, and a self-describing versioned manifest with
    identity, counts, per-file checksums, consent, and the full
    coverage/omission/redaction report. Publishes atomically (staging +
    rename via hotmem.fsutil) — readers never observe a half-written
    package and a failed publish never destroys the previous one.

    Determinism: session.jsonl and memories.jsonl are byte-stable across
    re-prepares; package_id is content-derived (mirrors the interchange
    logical_id algorithm); only handoff_id (uuid) and created_at differ per
    prepare. Resume mode bounds the turn stream to head/tail windows with
    omission records; archive keeps the full ordered stream (§8).

Interface:
    prepare_handoff(source, out_dir, *, mode, consent, session_id=None,
                    limits=DEFAULT_LIMITS) -> PrepareResult
    PrepareResult — handoff_id, package_id, path, manifest, coverage,
                    timings_ms

Deps: hotmem.handoff (ids, limits, constants), handoff.codex_source,
    handoff.redact, handoff.brief, handoff.report, hotmem.fsutil,
    hotmem.interchange.canonical (serialization + digests).
Extension: verification/inspection live in handoff.verify; hydration in
    handoff.hydrate; CLI/MCP/HTTP surfaces stay thin wrappers.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from hotmem.fsutil import atomic_publish, fsync_dir
from hotmem.handoff import (
    BRIEF_NAME,
    DEFAULT_LIMITS,
    FORMAT_ID,
    MANIFEST_NAME,
    MEMORIES_NAME,
    MODE_RESUME,
    MODES,
    SCHEMA_VERSION,
    SESSION_STREAM_NAME,
    TARGET_ADAPTER_ID,
    Limits,
    entry_content_hash,
    package_id_for,
)
from hotmem.handoff.brief import build_brief
from hotmem.handoff.codex_source import read_codex_export
from hotmem.handoff.redact import redact_normalized
from hotmem.handoff.report import OMISSION_RESUME_BOUNDED, build_coverage, omission
from hotmem.handoff.session import NormalizedSession
from hotmem.interchange.canonical import canonical_line, sha256_file
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("handoff.package")

# Resume-mode turn windows: the head and tail turns are always kept; the
# middle turns are bounded out with omission records (handoff-v1 §8).
_RESUME_TURN_HEAD = 2
_RESUME_TURN_TAIL = 2


@dataclass(frozen=True)
class PrepareResult:
    """The outcome of one prepare operation."""

    handoff_id: str
    package_id: str
    path: str
    manifest: dict[str, Any]
    coverage: dict[str, Any]
    timings_ms: dict[str, float] = field(default_factory=dict)


def _hotmem_version() -> str:
    try:
        return version("hotmem")
    except PackageNotFoundError:  # pragma: no cover - dev environments
        return "0.0.0.dev0"


def _resume_bound(session: NormalizedSession) -> NormalizedSession:
    """Bound the resume-mode turn stream (handoff-v1 §8).

    Typed entries (decisions, commitments, …) always stay; only middle
    turns drop, one omission record per dropped turn — the archive mode
    keeps the full ordered stream, so the two modes are demonstrably
    different.
    """
    from dataclasses import replace

    from hotmem.handoff import EntryKind

    turns = [e for e in session.entries if e["kind"] == EntryKind.TURN]
    if len(turns) <= _RESUME_TURN_HEAD + _RESUME_TURN_TAIL:
        return session
    keep_ids = {e["id"] for e in turns[:_RESUME_TURN_HEAD] + turns[-_RESUME_TURN_TAIL:]}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for entry in session.entries:
        if entry["kind"] == EntryKind.TURN and entry["id"] not in keep_ids:
            dropped.append(entry)
        else:
            kept.append(entry)
    omissions = list(session.omissions) + [
        omission(
            entry["source"]["source_entry_id"],
            None,
            OMISSION_RESUME_BOUNDED,
            recoverable=True,
        )
        for entry in dropped
    ]
    return replace(session, entries=kept, omissions=omissions)


def _write_payload_lines(path: Path, records: list[dict[str, Any]]) -> None:
    """Stream canonical JSONL records to a file and fsync it."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(canonical_line(record))
        f.flush()
        os.fsync(f.fileno())


def _file_entry(path: Path) -> dict[str, Any]:
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def prepare_handoff(
    source: str | Path,
    out_dir: str | Path,
    *,
    mode: str,
    consent: str,
    session_id: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> PrepareResult:
    """Prepare one hotmem-handoff-v1 package atomically (#101).

    Fail-closed order: consent and selection are validated during the read
    (before any content is normalized); nothing is written until every
    payload and the manifest are complete, checksummed, and self-checked.
    """
    if mode not in MODES:
        raise ValueError(f"unknown handoff mode {mode!r} (expected one of {MODES})")

    started = time.perf_counter()
    with Timer() as t_read:
        session = read_codex_export(source, session_id=session_id, consent=consent, limits=limits)
    with Timer() as t_redact:
        session = redact_normalized(session)
    if mode == MODE_RESUME:
        with Timer() as t_bound:
            session = _resume_bound(session)
    else:
        t_bound = None
    with Timer() as t_brief:
        brief = build_brief(session, budget=limits.brief_char_budget)

    handoff_id = uuid.uuid4().hex
    created_at = datetime.now(UTC).isoformat()
    entry_hashes = [entry_content_hash(e) for e in session.entries]
    memory_hashes = [str(r.get("content_hash") or "") for r in session.memory_records]
    package_id = package_id_for(entry_hashes + memory_hashes)

    entries_by_kind: dict[str, int] = {}
    for entry in session.entries:
        entries_by_kind[entry["kind"]] = entries_by_kind.get(entry["kind"], 0) + 1
    coverage = build_coverage(
        transferred=len(session.entries),
        omissions=session.omissions,
        redactions=session.redactions,
    )
    consent_statement = consent.strip()[: limits.max_consent_chars]

    final = Path(out_dir).resolve()
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(f".{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    try:
        staging.mkdir(parents=True)
        with Timer() as t_write:
            session_path = staging / SESSION_STREAM_NAME
            _write_payload_lines(session_path, session.entries)
            memories_path = staging / MEMORIES_NAME
            _write_payload_lines(memories_path, session.memory_records)
            brief_path = staging / BRIEF_NAME
            with open(brief_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(brief.to_dict(), sort_keys=True, indent=2, ensure_ascii=False))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())

            files = {
                SESSION_STREAM_NAME: _file_entry(session_path),
                MEMORIES_NAME: _file_entry(memories_path),
                BRIEF_NAME: _file_entry(brief_path),
            }
            manifest = {
                "format": FORMAT_ID,
                "schema_version": SCHEMA_VERSION,
                "package_id": package_id,
                "handoff_id": handoff_id,
                "mode": mode,
                "created_at": created_at,
                "hotmem_version": _hotmem_version(),
                "source": {
                    "adapter": session.adapter,
                    "adapter_version": session.adapter_version,
                    "session_id": session.session_id,
                    "label": session.label,
                    "time_range": session.time_range,
                    "selection_scope": {
                        "scope": f"session:{session.session_id}",
                        "requested_session": session_id,
                    },
                    "codex_version": session.codex_version,
                },
                "target": {"adapter": TARGET_ADAPTER_ID},
                "compatibility": {"min_hotmem": "0.2.4", "interchange_schema": 1},
                "counts": {
                    "entries": len(session.entries),
                    "entries_by_kind": entries_by_kind,
                    "memories": len(session.memory_records),
                    "brief_chars": brief.char_count,
                },
                "files": files,
                "coverage": coverage,
                "limits": {
                    "max_session_entries": limits.max_session_entries,
                    "max_entry_bytes": limits.max_entry_bytes,
                    "max_tool_result_bytes": limits.max_tool_result_bytes,
                    "brief_char_budget": limits.brief_char_budget,
                    "max_memories": limits.max_memories,
                    "max_consent_chars": limits.max_consent_chars,
                },
                "consent": {
                    "given": True,
                    "statement": consent_statement,
                    "at": created_at,
                },
            }
            manifest_path = staging / MANIFEST_NAME
            with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())

            # Self-check before publish: the checksums we are about to
            # publish must match a fresh read of every payload.
            for name, entry in files.items():
                actual = _file_entry(staging / name)
                if actual != entry:
                    raise OSError(
                        f"self-check failed for {name}: manifest {entry} vs actual {actual}"
                    )

        fsync_dir(staging)
        atomic_publish(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    timings = {
        "read_ms": round(t_read.ms, 2),
        "redact_ms": round(t_redact.ms, 2),
        "bound_ms": round(t_bound.ms, 2) if t_bound else 0.0,
        "brief_ms": round(t_brief.ms, 2),
        "write_ms": round(t_write.ms, 2),
        "total_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    _trace.info(
        "prepare",
        f"published {mode} package {package_id[:12]} to {final}",
        detail={
            "path": str(final),
            "mode": mode,
            "handoff_id": handoff_id,
            "entries": len(session.entries),
            "memories": len(session.memory_records),
            **timings,
        },
    )
    return PrepareResult(
        handoff_id=handoff_id,
        package_id=package_id,
        path=str(final),
        manifest=manifest,
        coverage=coverage,
        timings_ms=timings,
    )
