#!/usr/bin/env python3
"""Deterministic retrieval evaluation harness (#77).

Purpose:
     Measure where HotMem's retrieval stack (hotmem-hash-v1 trigram vectors
     + SQLite FTS5 BM25 + importance, fused 0.6/0.2/0.2) succeeds and fails,
     using graded synthetic fixtures and the REAL production path. This is
     evidence tooling for Phase 1 (Credible Search Quality): it gates
     ranking changes (#78 semantic embedder, #80 reranker) with
     reproducible numbers instead of opinions.

     Hard rules (issue #77): production code is called, never copied —
     ingestion goes through the normal database path and every query runs
     through hotmem.search.search_memories(). No network, no model
     downloads, no new required dependencies, no writes outside a temporary
     directory, production ranking untouched.

Interface:
      uv run python scripts/retrieval_eval.py [--corpus P] [--queries P]
          [--output P] [--report P] [--top-k K] [--repeat N]

Deps: hotmem only (stdlib otherwise).
Extension: baseline regression lives in tests/test_retrieval_eval.py;
      docs/retrieval-quality.md explains interpretation and limits.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

RELEVANT_GRADE_THRESHOLD = 2  # grades 2-3 count as relevant (issue #77)
METRICS_SCHEMA_VERSION = 1
DEFAULT_TOP_K = 5
EVAL_TOP_K = 5  # @1/@5 metrics are always computed (documented extra passes)


# ── fixture validation ──────────────────────────────────────────────────────


class FixtureError(Exception):
    """A malformed fixture line: message names file, line number, reason."""


def _load_jsonl(path: Path, kind: str) -> list[dict]:
    if not path.is_file():
        raise FixtureError(f"{path}: file not found")
    records: list[dict] = []
    text = path.read_text(encoding="utf-8")  # UnicodeDecodeError → invalid UTF-8
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as err:
            raise FixtureError(f"{path}:{lineno}: invalid JSON ({err})") from err
        if not isinstance(record, dict):
            raise FixtureError(f"{path}:{lineno}: expected a JSON object")
        records.append(record)
    if not records:
        raise FixtureError(f"{path}: no {kind} records")
    return records


def load_corpus(path: Path) -> list[dict]:
    """Load and validate corpus.jsonl (issue #77 fixture contract)."""
    records = _load_jsonl(path, "corpus")
    seen_ids: set[str] = set()
    for i, rec in enumerate(records, 1):
        where = f"{path}:{i}"
        for required in ("memory_id", "identifier", "fact"):
            if not rec.get(required):
                raise FixtureError(f"{where}: missing required field '{required}'")
        if not isinstance(rec["fact"], str):
            raise FixtureError(f"{where}: 'fact' must be a string")
        mid = rec["memory_id"]
        if mid in seen_ids:
            raise FixtureError(f"{where}: duplicate memory_id '{mid}'")
        seen_ids.add(mid)
        importance = rec.get("importance", 0.5)
        if not isinstance(importance, (int, float)) or not 0.0 <= importance <= 1.0:
            raise FixtureError(f"{where}: 'importance' must be a number in [0, 1]")
        tags = rec.get("tags", [])
        if not isinstance(tags, list):
            raise FixtureError(f"{where}: 'tags' must be a list")
    return records


REQUIRED_CATEGORIES = (
    "exact_lexical",
    "semantic_paraphrase",
    "identifier_or_entity_name",
    "temporal_or_revision",
    "near_duplicate_diversity",
    "negative_or_no_answer",
    "cross_project_isolation",
    "snapshot_hydration_equivalence",
)


def load_queries(path: Path, *, require_all_categories: bool = True) -> list[dict]:
    """Load and validate queries.jsonl (issue #77 fixture contract).

    ``require_all_categories=False`` relaxes the every-category coverage
    rule for small in-test fixture slices; the committed fixtures always
    run with the default.
    """
    records = _load_jsonl(path, "query")
    seen_ids: set[str] = set()
    category_counts: dict[str, int] = {}
    for i, rec in enumerate(records, 1):
        where = f"{path}:{i}"
        for required in ("query_id", "category", "query"):
            if not rec.get(required):
                raise FixtureError(f"{where}: missing required field '{required}'")
        qid = rec["query_id"]
        if qid in seen_ids:
            raise FixtureError(f"{where}: duplicate query_id '{qid}'")
        seen_ids.add(qid)
        category = rec["category"]
        if category not in REQUIRED_CATEGORIES:
            raise FixtureError(f"{where}: unknown category '{category}'")
        category_counts[category] = category_counts.get(category, 0) + 1
        relevance = rec.get("relevance")
        if not isinstance(relevance, dict):
            raise FixtureError(f"{where}: 'relevance' must be an object")
        for mid, grade in relevance.items():
            if not isinstance(grade, int) or not 0 <= grade <= 3:
                raise FixtureError(f"{where}: relevance grade for '{mid}' must be an int in [0, 3]")
    if require_all_categories:
        missing = [c for c in REQUIRED_CATEGORIES if category_counts.get(c, 0) == 0]
        if missing:
            raise FixtureError(f"{path}: no queries for categories: {', '.join(missing)}")
    return records


# ── metrics (pure functions — hand-calculated in tests) ─────────────────────


def _relevant_ids(relevance: dict[str, int], threshold: int) -> set[str]:
    return {mid for mid, grade in relevance.items() if grade >= threshold}


def recall_at_k(
    ranked_ids: list[str],
    relevance: dict[str, int],
    k: int,
    threshold: int = RELEVANT_GRADE_THRESHOLD,
) -> float | None:
    """Recall@k over graded-relevant ids; None when nothing is relevant."""
    relevant = _relevant_ids(relevance, threshold)
    if not relevant:
        return None
    hits = sum(1 for mid in ranked_ids[:k] if mid in relevant)
    return hits / len(relevant)


def mrr_at_k(
    ranked_ids: list[str],
    relevance: dict[str, int],
    k: int,
    threshold: int = RELEVANT_GRADE_THRESHOLD,
) -> float | None:
    """Reciprocal rank of the first graded-relevant result; None if n/a."""
    relevant = _relevant_ids(relevance, threshold)
    if not relevant:
        return None
    for rank, mid in enumerate(ranked_ids[:k], 1):
        if mid in relevant:
            return 1.0 / rank
    return 0.0


def _dcg(grades: list[float]) -> float:
    from math import log2

    return sum(grade / log2(rank + 1) for rank, grade in enumerate(grades, 1))


def ndcg_at_k(ranked_ids: list[str], relevance: dict[str, int], k: int) -> float | None:
    """Graded nDCG@k; None when the query has no graded relevance at all."""
    grades = [float(relevance.get(mid, 0)) for mid in ranked_ids[:k]]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    if not ideal or max(ideal) <= 0:
        return None
    idcg = _dcg([float(g) for g in ideal])
    if idcg == 0:
        return None
    return _dcg(grades) / idcg


def false_positive_rate(ranked_ids: list[str], relevance: dict[str, int], k: int) -> float | None:
    """Fraction of RETURNED slots holding an irrelevant (sub-threshold) memory.

    Measured over returned slots (not k): a ranker without abstention that
    returns anything for a negative query honestly reports rate 1.0 rather
    than being diluted by empty slots (#77: expose false positives as they
    are, never diluted).
    """
    relevant = _relevant_ids(relevance, RELEVANT_GRADE_THRESHOLD)
    returned = ranked_ids[:k]
    if not returned:
        return None
    false_positives = sum(1 for mid in returned if mid not in relevant)
    return false_positives / len(returned)


def duplicate_slot_rate(
    ranked_ids: list[str], duplicate_groups: dict[str, str], k: int
) -> float | None:
    """Fraction of top-k slots occupied by a near-duplicate of an earlier slot.

    ``duplicate_groups`` maps memory_id -> group representative id. A slot
    counts as a duplicate slot when its group was already represented by an
    earlier slot in the ranking.
    """
    returned = ranked_ids[:k]
    if not returned:
        return None
    seen_groups: set[str] = set()
    dup_slots = 0
    for mid in returned:
        group = duplicate_groups.get(mid, mid)
        if group in seen_groups:
            dup_slots += 1
        else:
            seen_groups.add(group)
    return dup_slots / len(returned)


def aggregate(values: list[float | None]) -> dict[str, float | int | str | None]:
    """Mean over applicable queries; explicit n/a when none apply."""
    applicable = [v for v in values if v is not None]
    if not applicable:
        return {"mean": None, "n_applicable": 0, "n_queries": len(values)}
    return {
        "mean": sum(applicable) / len(applicable),
        "n_applicable": len(applicable),
        "n_queries": len(values),
    }


# ── evaluation run ──────────────────────────────────────────────────────────


@dataclass
class QueryResult:
    query_id: str
    category: str
    query: str
    ranked_ids: list[str]
    ranked_scores: list[float] = field(default_factory=list)
    recall_at_1: float | None = None
    recall_at_5: float | None = None
    mrr_at_5: float | None = None
    ndcg_at_5: float | None = None
    false_positive_rate: float | None = None
    duplicate_slot_rate: float | None = None
    expected_order: list[str] = field(default_factory=list)
    missed_relevant: list[str] = field(default_factory=list)


def ingest_corpus(records: list[dict], db) -> int:
    """Ingest fixture records through the production database path.

    Fixture ``created_at`` (optional) is passed through db.insert so
    temporal/revision fixtures grade deterministically regardless of wall
    clock — production code, frozen clock (#77).
    """
    from hotmem.db import MemoryRecord
    from hotmem.embed import embed_text, pack_embedding

    batch: list[MemoryRecord] = []
    count = 0
    for rec in records:
        fact = rec["fact"]
        batch.append(
            MemoryRecord(
                id=rec["memory_id"],
                identifier=rec["identifier"],
                fact_text=fact,
                fact_summary=rec.get("fact_summary"),
                embedding=pack_embedding(embed_text(fact)),
                embedding_model="hotmem-hash-v1",
                content_hash=rec.get("content_hash") or _fixture_content_hash(rec),
                source=rec.get("source", "retrieval-eval"),
                importance=rec.get("importance", 0.5),
                tags=json.dumps(rec.get("tags", [])),
                namespace=rec.get("namespace", "retrieval-eval"),
                created_at=rec.get("created_at"),
            )
        )
        count += 1
        if len(batch) >= 500:
            db.insert_many_ignore(batch)
            batch = []
    if batch:
        db.insert_many_ignore(batch)
    return count


def _fixture_content_hash(rec: dict) -> str:
    import hashlib

    return hashlib.sha256(f"{rec['identifier']}:{rec['fact']}".encode()).hexdigest()


def evaluate_query(
    db, query_rec: dict, *, top_k: int, duplicate_groups: dict[str, str]
) -> QueryResult:
    """Run one query through production search and score it.

    @1/@5 metrics are always computed: when the displayed top-k differs
    from 5, an additional pass with top_k=5 executes (documented extra
    query execution, excluded from latency aggregation).
    """
    from hotmem.search import search_memories

    started = time.perf_counter()
    rows = search_memories(db, query_rec["query"], top_k=top_k)
    display_elapsed = time.perf_counter() - started

    ranked_ids = [r["memory_id"] for r in rows]
    ranked_scores = [round(float(r["score"]), 6) for r in rows]

    eval_top = max(EVAL_TOP_K, top_k)
    if eval_top != top_k:
        rows = search_memories(db, query_rec["query"], top_k=eval_top)
        ranked_ids = [r["memory_id"] for r in rows]
    _ = display_elapsed  # latency is sampled separately; see measure_latency

    relevance = query_rec.get("relevance") or {}
    result = QueryResult(
        query_id=query_rec["query_id"],
        category=query_rec["category"],
        query=query_rec["query"],
        ranked_ids=ranked_ids,
        ranked_scores=ranked_scores,
    )
    result.recall_at_1 = recall_at_k(ranked_ids, relevance, 1)
    result.recall_at_5 = recall_at_k(ranked_ids, relevance, EVAL_TOP_K)
    result.mrr_at_5 = mrr_at_k(ranked_ids, relevance, EVAL_TOP_K)
    result.ndcg_at_5 = ndcg_at_k(ranked_ids, relevance, EVAL_TOP_K)
    result.false_positive_rate = false_positive_rate(ranked_ids, relevance, EVAL_TOP_K)
    result.duplicate_slot_rate = duplicate_slot_rate(ranked_ids, duplicate_groups, len(ranked_ids))

    relevant = _relevant_ids(relevance, RELEVANT_GRADE_THRESHOLD)
    result.missed_relevant = sorted(relevant - set(ranked_ids[:EVAL_TOP_K]))
    return result


def measure_latency(db, queries: list[dict], *, top_k: int, repeat: int) -> dict:
    """Latency p50/p95 sampled separately from quality runs (issue #77)."""
    from hotmem.search import search_memories

    samples_ms: list[float] = []
    for _ in range(max(1, repeat)):
        for rec in queries:
            started = time.perf_counter()
            search_memories(db, rec["query"], top_k=top_k)
            samples_ms.append((time.perf_counter() - started) * 1000.0)
    if not samples_ms:
        return {"p50_ms": None, "p95_ms": None, "samples": 0}
    samples_ms.sort()

    def percentile(p: float) -> float:
        idx = min(len(samples_ms) - 1, round(p / 100.0 * (len(samples_ms) - 1)))
        return round(samples_ms[idx], 3)

    return {
        "p50_ms": percentile(50),
        "p95_ms": percentile(95),
        "samples": len(samples_ms),
    }


def build_duplicate_groups(corpus: list[dict]) -> dict[str, str]:
    """memory_id -> group representative id, via the optional 'duplicate_of'
    fixture field (near_duplicate_diversity category, issue #77)."""
    groups: dict[str, str] = {}
    for rec in corpus:
        rep = rec.get("duplicate_of")
        if rep:
            groups[rec["memory_id"]] = rep
    return groups


def run_clone_equivalence(
    corpus: list[dict],
    queries: list[dict],
    duplicate_groups: dict[str, str],
    per_query: list[QueryResult],
    tmp: Path,
) -> dict:
    """Clone stage (#77 snapshot_hydration_equivalence): export the ingested
    instance as a verified package, hydrate a CLEAN target, re-run every
    query, and compare ordered ids + scores with per-query drift."""
    import time

    from hotmem.db import MemoryDB
    from hotmem.interchange.hydrate import hydrate_package, verify_package
    from hotmem.interchange.package import write_package

    source_db = MemoryDB(Path(tmp) / "eval.sqlite")
    pkg = Path(tmp) / "clone-pkg"
    started = time.perf_counter()
    write_package(source_db, pkg, gz=True)
    export_seconds = time.perf_counter() - started

    started = time.perf_counter()
    verified = verify_package(pkg)
    verified.cleanup()
    verify_seconds = time.perf_counter() - started
    package_bytes = (pkg / "memories.jsonl.gz").stat().st_size

    target_db = MemoryDB(Path(tmp) / "clone-target.sqlite")
    started = time.perf_counter()
    hydrate_package(target_db, pkg)
    hydrate_seconds = time.perf_counter() - started

    drift: list[dict] = []
    identical = 0
    for base in per_query:
        rerun = evaluate_query(
            target_db,
            {"query_id": base.query_id, "category": base.category, "query": base.query},
            top_k=DEFAULT_TOP_K,
            duplicate_groups=duplicate_groups,
        )
        ids_match = rerun.ranked_ids == base.ranked_ids
        scores_match = rerun.ranked_scores == base.ranked_scores
        identical += 1 if (ids_match and scores_match) else 0
        if not (ids_match and scores_match):
            drift.append(
                {
                    "query_id": base.query_id,
                    "before_ids": base.ranked_ids,
                    "after_ids": rerun.ranked_ids,
                    "before_scores": base.ranked_scores,
                    "after_scores": rerun.ranked_scores,
                }
            )

    source_db.close()
    target_db.close()
    package_count = len(corpus)
    return {
        "clone_equivalence_rate": identical / len(per_query) if per_query else None,
        "queries_compared": len(per_query),
        "drift": drift,
        "package": {
            "gz_bytes": package_bytes,
            "records": package_count,
            "export_seconds": round(export_seconds, 4),
            "verify_seconds": round(verify_seconds, 4),
            "verify_records_per_s": round(package_count / verify_seconds, 1)
            if verify_seconds
            else None,
            "hydrate_seconds": round(hydrate_seconds, 4),
            "hydrate_records_per_s": round(package_count / hydrate_seconds, 1)
            if hydrate_seconds
            else None,
        },
        "note": "local diagnostics on one machine — not cross-machine guarantees",
    }


def measure_cold_start(corpus_path: Path, queries_path: Path, *, top_k: int) -> dict:
    """Fresh-process cold-start-to-first-query time (issue #77).

    Re-invokes this script in a subprocess with a small internal mode:
    ingest + first query + exit. Wall time is the parent's measurement.
    """
    import subprocess

    started = time.perf_counter()
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--cold-start-internal",
            "--corpus",
            str(corpus_path),
            "--queries",
            str(queries_path),
            "--top-k",
            str(top_k),
        ],
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        return {"cold_start_seconds": None, "error": proc.stderr.strip()[-200:]}
    return {"cold_start_seconds": round(elapsed, 3)}


def run_cold_start_internal(corpus_path: Path, queries_path: Path, *, top_k: int) -> int:
    """Internal mode for measure_cold_start: ingest + first query + exit."""
    corpus = load_corpus(corpus_path)
    queries = load_queries(queries_path, require_all_categories=False)
    from hotmem.db import MemoryDB

    with tempfile.TemporaryDirectory(prefix="hotmem-cold-start-") as tmp:
        db = MemoryDB(Path(tmp) / "eval.sqlite")
        ingest_corpus(corpus, db)
        if queries:
            from hotmem.search import search_memories

            search_memories(db, queries[0]["query"], top_k=top_k)
        db.close()
    return 0


def run_eval(
    corpus_path: Path,
    queries_path: Path,
    *,
    top_k: int = DEFAULT_TOP_K,
    repeat: int = 1,
    work_dir: Path | None = None,
    require_all_categories: bool = True,
) -> dict:
    """Run the full evaluation; return the metrics document (JSON-safe)."""
    corpus = load_corpus(corpus_path)
    queries = load_queries(queries_path, require_all_categories=require_all_categories)
    duplicate_groups = build_duplicate_groups(corpus)

    import contextlib

    from hotmem.db import MemoryDB

    own_tmp = work_dir is None
    if own_tmp:
        tmp = tempfile.mkdtemp(prefix="hotmem-retrieval-eval-")
    else:
        tmp = Path(work_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        tmp = str(tmp)
    db_path = Path(tmp) / "eval.sqlite"
    try:
        db = MemoryDB(db_path)
        ingested = ingest_corpus(corpus, db)

        per_query: list[QueryResult] = []
        for rec in queries:
            per_query.append(
                evaluate_query(db, rec, top_k=top_k, duplicate_groups=duplicate_groups)
            )
        latency = measure_latency(db, queries, top_k=top_k, repeat=repeat)

        clone = run_clone_equivalence(corpus, queries, duplicate_groups, per_query, Path(tmp))
        cold = measure_cold_start(corpus_path, queries_path, top_k=top_k)
        db.close()
    finally:
        if own_tmp:
            with contextlib.suppress(OSError):
                import shutil

                shutil.rmtree(tmp, ignore_errors=True)

    by_category: dict[str, list[QueryResult]] = {}
    for qr in per_query:
        by_category.setdefault(qr.category, []).append(qr)

    def category_block(qrs: list[QueryResult]) -> dict:
        return {
            "recall_at_1": aggregate([q.recall_at_1 for q in qrs]),
            "recall_at_5": aggregate([q.recall_at_5 for q in qrs]),
            "mrr_at_5": aggregate([q.mrr_at_5 for q in qrs]),
            "ndcg_at_5": aggregate([q.ndcg_at_5 for q in qrs]),
            "false_positive_rate": aggregate([q.false_positive_rate for q in qrs]),
            "duplicate_slot_rate": aggregate([q.duplicate_slot_rate for q in qrs]),
            "queries": len(qrs),
        }

    doc = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "runtime": {
            "embedding_model": "hotmem-hash-v1",
            "embedding_dim": 64,
            "fusion": "cosine 0.6 / fts5_bm25 0.2 / importance 0.2",
            "top_k": top_k,
            "repeat": repeat,
        },
        "counts": {"corpus": len(corpus), "queries": len(queries), "ingested": ingested},
        "overall": category_block(per_query),
        "categories": {cat: category_block(qrs) for cat, qrs in sorted(by_category.items())},
        "per_query": [
            {
                "query_id": q.query_id,
                "category": q.category,
                "query": q.query,
                "ranked_ids": q.ranked_ids,
                "scores": q.ranked_scores,
                "recall_at_1": q.recall_at_1,
                "recall_at_5": q.recall_at_5,
                "mrr_at_5": q.mrr_at_5,
                "ndcg_at_5": q.ndcg_at_5,
                "false_positive_rate": q.false_positive_rate,
                "duplicate_slot_rate": q.duplicate_slot_rate,
                "missed_relevant": q.missed_relevant,
            }
            for q in per_query
        ],
        "latency_ms": latency,
        "clone_equivalence": clone,
        "cold_start": cold,
    }
    doc["recommendation"] = build_recommendation(doc)
    return doc


def build_recommendation(doc: dict) -> dict:
    """Deterministic next-investment rule (issue #77): evidence, not opinion.

    Order: clone equivalence below 100% -> fix clone/index compatibility;
    else semantic Recall@5 at least 15pp below lexical -> #78 (portable
    derived-index / optional semantic embedder boundary); else duplicate
    slots above 20% -> #80 (optional reranking hook); else retain the
    stack and expand fixtures. These prioritize work; they are not release
    thresholds. Never recommend entity extraction from this benchmark.
    """
    overall = doc["overall"]
    categories = doc["categories"]
    clone_rate = (doc.get("clone_equivalence") or {}).get("clone_equivalence_rate")

    def cat_mean(category: str, metric: str) -> float | None:
        block = categories.get(category, {}).get(metric) or {}
        return block.get("mean")

    sem = cat_mean("semantic_paraphrase", "recall_at_5")
    lex = cat_mean("exact_lexical", "recall_at_5")
    dup = (overall.get("duplicate_slot_rate") or {}).get("mean") or 0.0

    measured = {
        "clone_equivalence_rate": clone_rate,
        "semantic_recall_at_5": sem,
        "exact_lexical_recall_at_5": lex,
        "duplicate_slot_rate": dup,
    }
    if clone_rate is not None and clone_rate < 1.0:
        return {
            "action": "fix_clone_index_compatibility",
            "rationale": f"clone equivalence {clone_rate:.3f} is below 1.0: restored instances "
            "do not retrieve identically; fix that before any ranking change.",
            "measured": measured,
        }
    if sem is not None and lex is not None and (lex - sem) >= 0.15:
        return {
            "action": "pursue_semantic_embedding_boundary_78",
            "rationale": f"semantic Recall@5 ({sem:.3f}) trails exact-lexical Recall@5 ({lex:.3f}) "
            f"by {(lex - sem) * 100:.1f} percentage points (threshold: 15pp) — the deterministic "
            "hash embedder cannot bridge wording differences; pursue #78.",
            "measured": measured,
        }
    if dup > 0.20:
        return {
            "action": "pursue_reranking_hook_80",
            "rationale": f"duplicate-slot rate {dup:.3f} exceeds 20%: near-duplicates consume "
            "diverse top-k slots; pursue #80 (evidence-gated optional reranking hook).",
            "measured": measured,
        }
    return {
        "action": "retain_current_stack",
        "rationale": "no category shows a gap above the decision thresholds; retain the stack "
        "and expand fixture coverage before changing ranking.",
        "measured": measured,
    }


def normalize_for_baseline(doc: dict) -> dict:
    """Strip volatile fields (timings, sizes) for deterministic comparison."""
    trimmed = json.loads(json.dumps(doc))  # deep copy
    trimmed.pop("latency_ms", None)
    trimmed.pop("cold_start", None)
    clone = trimmed.get("clone_equivalence")
    if isinstance(clone, dict):
        clone.pop("package", None)
    return trimmed


def render_report(doc: dict) -> str:
    """Render the human-readable Markdown report (issue #77 user outcome)."""
    lines: list[str] = []
    overall = doc["overall"]

    def fmt(block: dict, key: str) -> str:
        entry = block.get(key) or {}
        mean = entry.get("mean")
        return "n/a" if mean is None else f"{mean:.3f} (n={entry.get('n_applicable', 0)})"

    lines.append("# Retrieval evaluation report")
    lines.append("")
    lines.append(
        f"Corpus: {doc['counts']['corpus']} memories ({doc['counts']['ingested']} ingested), "
        f"{doc['counts']['queries']} queries. "
        f"Stack: {doc['runtime']['fusion']} over {doc['runtime']['embedding_model']} "
        f"({doc['runtime']['embedding_dim']}-dim)."
    )
    lines.append("")
    lines.append("## Overall metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for label, key in (
        ("Recall@1", "recall_at_1"),
        ("Recall@5", "recall_at_5"),
        ("MRR@5", "mrr_at_5"),
        ("nDCG@5", "ndcg_at_5"),
        ("False-positive rate", "false_positive_rate"),
        ("Duplicate-slot rate", "duplicate_slot_rate"),
    ):
        lines.append(f"| {label} | {fmt(overall, key)} |")
    lines.append("")
    lines.append("## Per category")
    lines.append("")
    lines.append("| Category | Recall@5 | MRR@5 | nDCG@5 | FP rate | Queries |")
    lines.append("|---|---|---|---|---|---|")
    for cat, block in doc["categories"].items():
        lines.append(
            f"| {cat} | {fmt(block, 'recall_at_5')} | {fmt(block, 'mrr_at_5')} | "
            f"{fmt(block, 'ndcg_at_5')} | {fmt(block, 'false_positive_rate')} "
            f"| {block['queries']} |"
        )
    lines.append("")

    clone = doc.get("clone_equivalence") or {}
    lines.append("## Clone equivalence (verified package -> clean hydration)")
    lines.append("")
    rate = clone.get("clone_equivalence_rate")
    lines.append(
        f"- Identical ordered ids + scores after restore: "
        f"{clone.get('queries_compared')}/{clone.get('queries_compared')} "
        f"= {rate if rate is None else f'{rate:.3f}'}"
    )
    pkg = clone.get("package") or {}
    lines.append(
        f"- Package: {pkg.get('gz_bytes')} bytes gz, "
        f"verify {pkg.get('verify_records_per_s')} rec/s, "
        f"hydrate {pkg.get('hydrate_records_per_s')} rec/s (local diagnostics)"
    )
    lines.append("")

    latency = doc.get("latency_ms") or {}
    cold = doc.get("cold_start") or {}
    lines.append("## Latency (separate sampling, not quality)")
    lines.append("")
    lines.append(
        f"- Query latency p50/p95: {latency.get('p50_ms')} / {latency.get('p95_ms')} ms "
        f"({latency.get('samples')} samples)"
    )
    lines.append(f"- Fresh-process cold start to first query: {cold.get('cold_start_seconds')} s")
    lines.append("")

    failures = [q for q in doc["per_query"] if q["missed_relevant"]]
    lines.append("## Missed relevance (per-query failures)")
    lines.append("")
    if failures:
        for q in failures:
            lines.append(
                f"- `{q['query_id']}` ({q['category']}): missed {q['missed_relevant']} "
                f'— "{q["query"]}"'
            )
    else:
        lines.append("- None: every graded-relevant memory appeared in the top 5.")
    lines.append("")

    worst = sorted(
        (q for q in doc["per_query"] if q["recall_at_5"] is not None),
        key=lambda q: (q["recall_at_5"], q["mrr_at_5"] or 0),
    )[:5]
    lines.append("## Five worst queries")
    lines.append("")
    for q in worst:
        lines.append(
            f"- `{q['query_id']}` recall@5={q['recall_at_5']} mrr@5={q['mrr_at_5']} "
            f'— "{q["query"]}"'
        )
    lines.append("")
    rec = doc.get("recommendation") or {}
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- **{rec.get('action')}** — {rec.get('rationale')}")
    m = rec.get("measured") or {}
    lines.append(
        f"  - measured: clone equivalence {m.get('clone_equivalence_rate')}, "
        f"semantic Recall@5 {m.get('semantic_recall_at_5')}, "
        f"exact-lexical Recall@5 {m.get('exact_lexical_recall_at_5')}, "
        f"duplicate-slot rate {m.get('duplicate_slot_rate')}"
    )
    lines.append("")
    lines.append("## What this benchmark does not prove")
    lines.append("")
    lines.append(
        "- These are synthetic fixtures on one machine: no cross-machine guarantees, "
        "no tenant authorization claims, and no enterprise-scale performance claims."
    )
    lines.append(
        "- The hash-vector embedder is deterministic and portable, but it is NOT a "
        "learned semantic embedding; weighted score fusion is not a second-stage reranker."
    )
    lines.append(
        "- Entity extraction, MMR, and rerankers remain optional future work "
        "(gated by #78 / #80 evidence), not core requirements."
    )
    return "\n".join(lines) + "\n"


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic retrieval evaluation (#77).")
    parser.add_argument("--corpus", type=Path, default=Path("bench/retrieval/corpus.jsonl"))
    parser.add_argument("--queries", type=Path, default=Path("bench/retrieval/queries.jsonl"))
    parser.add_argument("--output", type=Path, default=None, help="JSON result path")
    parser.add_argument("--report", type=Path, default=None, help="Markdown report path")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--repeat", type=int, default=1, help="Latency sampling only")
    parser.add_argument("--cold-start-internal", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.cold_start_internal:
        return run_cold_start_internal(args.corpus, args.queries, top_k=args.top_k)

    try:
        doc = run_eval(args.corpus, args.queries, top_k=args.top_k, repeat=args.repeat)
    except FixtureError as err:
        print(f"fixture error: {err}", file=sys.stderr)
        return 2

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_report(doc), encoding="utf-8")
    if not args.output and not args.report:
        print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
