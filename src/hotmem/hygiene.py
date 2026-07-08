"""HotMem hygiene — advisory warnings for store growth and source health.

Purpose:
     Help users keep HotMem small, inspectable, and local-first as memory
     stores grow. Produces advisory warnings without blocking normal operation,
     mutating data, or enforcing policy.

     Warnings cover:
     - Large inline payloads (fact_text > 128 KB → suggest file-backed).
     - Missing backing files (file-backed memory where the file is gone).
     - Stale bundle index entries (indexed path no longer exists).
     - Store growth thresholds (memory count, inline bytes, file-backed bytes).
     - Suspicious attachment growth (many file-backed memories added rapidly).

     No automatic migration, deletion, or escalation. Advisory only.

Interface:
     HygieneWarning (dataclass): category, severity, message, detail
     HygieneReport (dataclass): warnings, stats
     check_hygiene(db, *, base_dir=None) -> HygieneReport

Deps: hotmem.db, hotmem.storage, hotmem.trace
Extension: add custom hygiene rules or enforcement policies here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from hotmem.db import MemoryDB
from hotmem.storage import get_adapter
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("hygiene")

# Heuristic thresholds (OKF-recommended).
LARGE_INLINE_THRESHOLD = 128 * 1024  # 128 KB
STORE_COUNT_INFO = 1000  # info at 1000 memories
STORE_INLINE_BYTES_WARN = 10 * 1024 * 1024  # warn at 10 MB inline text
FILE_BACKED_INFO = 100  # info at 100 file-backed memories

Severity = Literal["info", "warn", "error"]
Category = Literal[
    "large_inline",
    "missing_backing_file",
    "stale_bundle",
    "growth",
    "attachment_growth",
]


@dataclass
class HygieneWarning:
    """A single advisory warning about store health."""

    category: Category
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HygieneReport:
    """Full hygiene report with warnings and stats."""

    warnings: list[HygieneWarning] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "warnings": [w.to_dict() for w in self.warnings],
            "stats": self.stats,
            "warning_count": len(self.warnings),
            "error_count": sum(1 for w in self.warnings if w.severity == "error"),
            "warn_count": sum(1 for w in self.warnings if w.severity == "warn"),
            "info_count": sum(1 for w in self.warnings if w.severity == "info"),
        }


def check_hygiene(
    db: MemoryDB,
    *,
    base_dir: str | None = None,
) -> HygieneReport:
    """Run advisory hygiene checks on the store. No mutations — advisory only.

    Checks:
    - Large inline payloads (fact_text > 128 KB).
    - Missing backing files (file-backed memory where the file is gone).
    - Stale bundle index entries (indexed path no longer exists).
    - Store growth thresholds (memory count, inline bytes, file-backed bytes).

    Uses ``adapter.exists()`` (stat, not read) for file existence checks.
    No file bytes are read during hygiene checks.
    """
    report = HygieneReport()
    warnings = report.warnings

    with Timer() as t:
        rows = db.all_rows()
        file_backed = db.list_file_backed()

        inline_rows = [r for r in rows if r["memory_type"] != "file"]
        file_rows = file_backed

        # Stats
        inline_bytes = sum(len(r.get("fact_text") or "") for r in inline_rows)
        file_backed_bytes = sum(r.get("byte_length") or 0 for r in file_rows)
        largest_inline = max((len(r.get("fact_text") or "") for r in inline_rows), default=0)

        report.stats = {
            "total_memories": len(rows),
            "inline_count": len(inline_rows),
            "file_backed_count": len(file_rows),
            "inline_bytes": inline_bytes,
            "file_backed_bytes": file_backed_bytes,
            "largest_inline_bytes": largest_inline,
        }

        # --- Large inline payloads ---
        for row in inline_rows:
            text_len = len(row.get("fact_text") or "")
            if text_len > LARGE_INLINE_THRESHOLD:
                warnings.append(
                    HygieneWarning(
                        category="large_inline",
                        severity="warn",
                        message=(
                            f"Inline memory '{row['identifier']}' has "
                            f"{text_len} bytes of fact_text; consider "
                            f"file-backed storage for content > 128 KB"
                        ),
                        detail={
                            "memory_id": row["id"],
                            "identifier": row["identifier"],
                            "fact_text_bytes": text_len,
                        },
                    )
                )

        # --- Missing backing files ---
        for row in file_rows:
            source_uri = row.get("source_uri") or ""
            if not source_uri:
                continue
            # Resolve relative URIs against base_dir.
            resolved = source_uri
            if base_dir and "://" not in source_uri and not Path(source_uri).is_absolute():
                resolved = str(Path(base_dir) / source_uri)
            try:
                adapter = get_adapter(resolved)
                if not adapter.exists(resolved):
                    warnings.append(
                        HygieneWarning(
                            category="missing_backing_file",
                            severity="error",
                            message=(
                                f"Backing file not found for memory "
                                f"'{row['identifier']}': {source_uri}"
                            ),
                            detail={
                                "memory_id": row["id"],
                                "identifier": row["identifier"],
                                "source_uri": source_uri,
                            },
                        )
                    )
            except Exception:
                warnings.append(
                    HygieneWarning(
                        category="missing_backing_file",
                        severity="warn",
                        message=(
                            f"Cannot check backing file for '{row['identifier']}': {source_uri}"
                        ),
                        detail={
                            "memory_id": row["id"],
                            "identifier": row["identifier"],
                            "source_uri": source_uri,
                        },
                    )
                )

        # --- Stale bundle index ---
        try:
            bundle_entries = db.list_bundle_index()
            for entry in bundle_entries:
                if not Path(entry["path"]).exists():
                    warnings.append(
                        HygieneWarning(
                            category="stale_bundle",
                            severity="warn",
                            message=(
                                f"Bundle index entry for '{entry['path']}' no longer exists on disk"
                            ),
                            detail={
                                "path": entry["path"],
                                "primary_file": entry.get("primary_file", ""),
                            },
                        )
                    )
        except Exception:
            pass  # bundle_index table may not exist

        # --- Store growth ---
        if len(rows) > STORE_COUNT_INFO:
            warnings.append(
                HygieneWarning(
                    category="growth",
                    severity="info",
                    message=(
                        f"Store has {len(rows)} memories; consider archiving "
                        f"or snapshotting older entries"
                    ),
                    detail={"total_memories": len(rows)},
                )
            )

        if inline_bytes > STORE_INLINE_BYTES_WARN:
            warnings.append(
                HygieneWarning(
                    category="growth",
                    severity="warn",
                    message=(
                        f"Inline text is {inline_bytes} bytes "
                        f"({inline_bytes / (1024 * 1024):.1f} MB); "
                        f"consider file-backed storage for large content"
                    ),
                    detail={"inline_bytes": inline_bytes},
                )
            )

        if len(file_rows) > FILE_BACKED_INFO:
            warnings.append(
                HygieneWarning(
                    category="attachment_growth",
                    severity="info",
                    message=(
                        f"Store has {len(file_rows)} file-backed memories; "
                        f"verify backing files exist periodically"
                    ),
                    detail={"file_backed_count": len(file_rows)},
                )
            )

    _trace.info(
        "check",
        f"hygiene check: {len(warnings)} warnings",
        detail={
            "warnings": len(warnings),
            "stats": report.stats,
            "ms": round(t.ms, 2),
        },
    )
    return report
