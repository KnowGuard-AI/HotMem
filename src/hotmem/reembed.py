"""Re-embed an existing store under a different embedder.

Purpose:
    Rows stored under one embedding descriptor score zero cosine under any
    other (mixed-space safety, issue #78), so switching a store from the hash
    default to a semantic embedder needs its vectors rebuilt. This walks the
    rows that are not yet in the active space, re-embeds their searchable
    text, and updates vector, dimension, and descriptor together.

    The pass is batched (one transaction per batch), resumable (already
    converted rows are skipped, so an interrupted run continues where it
    stopped), and idempotent. Memory text, ids, hashes, provenance, and
    timestamps are never touched; only the derived vector changes.

Interface:
    reembed_store(db, embedder, batch_size?, dry_run?, on_progress?) -> ReembedResult

Deps: hotmem.db, hotmem.embed, hotmem.events
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from hotmem.db import MemoryDB
from hotmem.embed import Embedder, pack_embedding
from hotmem.events import EventType, append_event
from hotmem.trace import Timer, get_tracer

_trace = get_tracer("reembed")


@dataclass
class ReembedResult:
    embedding_model: str
    dry_run: bool
    reembedded: int = 0
    failed: int = 0
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "embedding_model": self.embedding_model,
            "dry_run": self.dry_run,
            "reembedded": self.reembedded,
            "failed": self.failed,
            "before": dict(self.before),
            "after": dict(self.after),
            "ms": round(self.ms, 2),
        }


def reembed_store(
    db: MemoryDB,
    embedder: Embedder,
    *,
    batch_size: int = 500,
    dry_run: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> ReembedResult:
    """Rebuild stored vectors that are not in ``embedder``'s space.

    ``dry_run`` counts the rows that would change without writing. A row whose
    text fails to embed is left exactly as it was and counted in ``failed``;
    the pass continues, and a later run retries it.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be positive (got {batch_size})")
    descriptor = embedder.descriptor
    key = descriptor.key
    result = ReembedResult(embedding_model=key, dry_run=dry_run, before=db.embedding_model_counts())

    with Timer() as timer:
        cursor = 0
        while True:
            rows = db.reembed_candidates(key, after_rowid=cursor, limit=batch_size)
            if not rows:
                break
            cursor = int(rows[-1]["_rowid"])
            if dry_run:
                result.reembedded += len(rows)
            else:
                try:
                    for row in rows:
                        text = row.get("fact_text") or row.get("fact_summary") or ""
                        try:
                            vector = embedder.embed(text)
                            if len(vector) != descriptor.dimension:
                                raise ValueError(
                                    f"embedder returned {len(vector)} dims, "
                                    f"descriptor declares {descriptor.dimension}"
                                )
                        except Exception as err:
                            result.failed += 1
                            _trace.warn(
                                "embed",
                                "row left unchanged; embedding failed",
                                detail={"id": row["id"], "error": type(err).__name__},
                            )
                            continue
                        db.update_embedding(
                            str(row["id"]),
                            pack_embedding(vector),
                            embedding_dim=descriptor.dimension,
                            embedding_model=key,
                            _commit=False,
                        )
                        result.reembedded += 1
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            if on_progress is not None:
                on_progress(len(rows))

        if not dry_run and result.reembedded:
            # Observational marker: moves the store fingerprint so a derived
            # vector index built before the pass reads stale and is rebuilt.
            append_event(
                db,
                event_type=EventType.EMBEDDING_REBUILT,
                payload={
                    "embedding_model": key,
                    "embedding_dim": descriptor.dimension,
                    "reembedded": result.reembedded,
                    "failed": result.failed,
                },
            )
    result.after = result.before if dry_run else db.embedding_model_counts()
    result.ms = timer.ms
    _trace.info(
        "reembed", f"re-embedded {result.reembedded} row(s) under {key}", detail=result.to_dict()
    )
    return result
