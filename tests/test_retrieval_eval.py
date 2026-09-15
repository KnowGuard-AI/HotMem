"""Retrieval evaluation harness tests (#77) — metrics + fixture validation.

Pure metric functions are verified against exact hand-calculated examples
(issue #77 requirement); fixture validation must name file, line, and
reason; the runner must execute through production code only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retrieval_eval.py"
_spec = importlib.util.spec_from_file_location("retrieval_eval", _SCRIPT)
retrieval_eval = importlib.util.module_from_spec(_spec)
sys.modules["retrieval_eval"] = retrieval_eval  # required for dataclass resolution
_spec.loader.exec_module(retrieval_eval)


# ── metrics: exact hand-calculated examples ────────────────────────────────


def test_recall_at_1():
    assert retrieval_eval.recall_at_k(["a", "b"], {"a": 3}, 1) == 1.0
    assert retrieval_eval.recall_at_k(["b", "a"], {"a": 3}, 1) == 0.0


def test_recall_at_5_partial():
    # Two relevant; one in the top 5 -> 0.5.
    ranked = ["x", "y", "a", "z", "w", "b"]
    assert retrieval_eval.recall_at_k(ranked, {"a": 3, "b": 2}, 5) == 0.5
    # Both relevant -> 1.0.
    assert retrieval_eval.recall_at_k(ranked, {"a": 3, "b": 2}, 6) == 1.0


def test_recall_grade_threshold():
    # Grade 1 is below the relevance threshold (2): nothing relevant remains,
    # so the metric is explicitly not applicable (denominator zero) — never
    # a fabricated zero.
    assert retrieval_eval.recall_at_k(["a"], {"a": 1}, 1) is None


def test_recall_not_applicable_when_nothing_relevant():
    assert retrieval_eval.recall_at_k(["a"], {}, 5) is None


def test_mrr_at_5():
    assert retrieval_eval.mrr_at_k(["b", "a"], {"a": 3}, 5) == 0.5
    assert retrieval_eval.mrr_at_k(["b", "c", "a"], {"a": 3}, 5) == pytest.approx(1 / 3)
    assert retrieval_eval.mrr_at_k(["b", "c"], {"a": 3}, 5) == 0.0


def test_ndcg_at_5_hand_calculated():
    # ranked grades [3,1,0,0,0]; ideal [3,1] -> DCG == IDCG -> 1.0.
    assert retrieval_eval.ndcg_at_k(["a", "b"], {"a": 3, "b": 1}, 5) == pytest.approx(1.0)
    # ranked grades [1,3]; ideal [3,1]:
    # DCG = 1/log2(2) + 3/log2(3) = 1 + 1.89279 = 2.89279
    # IDCG = 3/log2(2) + 1/log2(3) = 3.63093 -> 0.79668...
    assert retrieval_eval.ndcg_at_k(["b", "a"], {"a": 3, "b": 1}, 5) == pytest.approx(
        0.79668, abs=1e-4
    )
    # Missed relevance lowers nDCG further.
    assert retrieval_eval.ndcg_at_k(["c", "d", "a"], {"a": 3, "b": 1}, 5) < 1.0


def test_false_positive_rate():
    # Negative query: a ranker without abstention returning 3 results
    # reports FP rate 1.0 — measured over returned slots, never diluted by
    # the remaining empty k slots.
    assert retrieval_eval.false_positive_rate(["a", "b", "c"], {}, 5) == 1.0
    # One irrelevant among relevant: 1/3.
    assert retrieval_eval.false_positive_rate(
        ["a", "x", "b"], {"a": 3, "b": 2}, 3
    ) == pytest.approx(1 / 3)


def test_duplicate_slot_rate():
    groups = {"a2": "a1", "a3": "a1"}  # a2, a3 are near-duplicates of a1
    # Top-3: a1, a2 (dup slot), b -> 1 duplicate slot / 3.
    assert retrieval_eval.duplicate_slot_rate(["a1", "a2", "b"], groups, 3) == pytest.approx(1 / 3)
    # Top-4: a1, a2, a3, b -> 2 duplicate slots / 4.
    assert retrieval_eval.duplicate_slot_rate(["a1", "a2", "a3", "b"], groups, 4) == pytest.approx(
        2 / 4
    )


def test_aggregate_reports_na_explicitly():
    block = retrieval_eval.aggregate([None, None])
    assert block == {"mean": None, "n_applicable": 0, "n_queries": 2}
    block = retrieval_eval.aggregate([0.5, None, 1.0])
    assert block["mean"] == pytest.approx(0.75)
    assert block["n_applicable"] == 2


# ── fixture validation ──────────────────────────────────────────────────────


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _corpus_line(mid: str, **overrides) -> str:
    rec = {
        "memory_id": mid,
        "identifier": "proj",
        "fact": f"fact for {mid}",
        "importance": 0.5,
        "tags": [],
    }
    rec.update(overrides)
    return json.dumps(rec)


def _query_line(qid: str, category: str, **overrides) -> str:
    rec = {
        "query_id": qid,
        "category": category,
        "query": f"query {qid}",
        "relevance": {"mem-x": 3} if category != "negative_or_no_answer" else {},
    }
    rec.update(overrides)
    return json.dumps(rec)


ALL_CATEGORIES = list(retrieval_eval.REQUIRED_CATEGORIES)


def _valid_queries(tmp_path: Path) -> Path:
    lines = []
    for i, cat in enumerate(ALL_CATEGORIES):
        lines.append(_query_line(f"q-{cat}-{i}", cat))
    return _write(tmp_path / "queries.jsonl", lines)


def test_load_corpus_ok(tmp_path: Path):
    path = _write(tmp_path / "corpus.jsonl", [_corpus_line("m1"), _corpus_line("m2")])
    assert len(retrieval_eval.load_corpus(path)) == 2


def test_load_corpus_duplicate_id(tmp_path: Path):
    path = _write(tmp_path / "corpus.jsonl", [_corpus_line("m1"), _corpus_line("m1")])
    with pytest.raises(retrieval_eval.FixtureError, match="duplicate memory_id 'm1'"):
        retrieval_eval.load_corpus(path)


def test_load_corpus_missing_field_names_line(tmp_path: Path):
    path = _write(tmp_path / "corpus.jsonl", [_corpus_line("m1"), json.dumps({"memory_id": "m2"})])
    with pytest.raises(retrieval_eval.FixtureError, match=r"corpus.jsonl:2.*'identifier'"):
        retrieval_eval.load_corpus(path)


def test_load_corpus_invalid_json_names_line(tmp_path: Path):
    path = _write(tmp_path / "corpus.jsonl", [_corpus_line("m1"), "{not json"])
    with pytest.raises(retrieval_eval.FixtureError, match=r"corpus.jsonl:2: invalid JSON"):
        retrieval_eval.load_corpus(path)


def test_load_corpus_bad_importance(tmp_path: Path):
    path = _write(tmp_path / "corpus.jsonl", [_corpus_line("m1", importance=1.5)])
    with pytest.raises(retrieval_eval.FixtureError, match="importance"):
        retrieval_eval.load_corpus(path)


def test_load_queries_unknown_category(tmp_path: Path):
    path = _write(tmp_path / "queries.jsonl", [_query_line("q1", "not_a_category")])
    with pytest.raises(retrieval_eval.FixtureError, match="unknown category"):
        retrieval_eval.load_queries(path)


def test_load_queries_missing_category_coverage(tmp_path: Path):
    lines = [_query_line("q1", "exact_lexical")]
    path = _write(tmp_path / "queries.jsonl", lines)
    with pytest.raises(retrieval_eval.FixtureError, match="no queries for categories"):
        retrieval_eval.load_queries(path)


def test_load_queries_negative_requires_empty_relevance(tmp_path: Path):
    # A negative query WITH relevance is a fixture bug: the runner surfaces it
    # via category metrics; validation enforces structure only.
    lines = [_query_line(f"q-{c}-{i}", c) for i, c in enumerate(ALL_CATEGORIES)]
    lines.append(_query_line("q-neg-extra", "negative_or_no_answer", relevance={}))
    path = _write(tmp_path / "queries.jsonl", lines)
    assert retrieval_eval.load_queries(path)


# ── runner: production-path execution ───────────────────────────────────────


def _mini_corpus(tmp_path: Path) -> Path:
    lines = [
        _corpus_line("mem-a", identifier="acme", fact="Acme invoices need two approvals."),
        _corpus_line("mem-b", identifier="acme", fact="Acme ships on Tuesdays."),
        _corpus_line("mem-c", identifier="beta", fact="Beta handles support tickets."),
    ]
    return _write(tmp_path / "corpus.jsonl", lines)


def _mini_queries(tmp_path: Path) -> Path:
    lines = [
        _query_line(
            "q-exact-1",
            "exact_lexical",
            query="Acme invoices approvals",
            relevance={"mem-a": 3},
        ),
        _query_line(
            "q-neg-1", "negative_or_no_answer", query="quantum flux capacitor", relevance={}
        ),
    ]
    return _write(tmp_path / "queries.jsonl", lines)


def test_run_eval_uses_production_search_and_reports(tmp_path: Path):
    doc = retrieval_eval.run_eval(
        _mini_corpus(tmp_path),
        _mini_queries(tmp_path),
        work_dir=tmp_path / "work",
        require_all_categories=False,
    )
    assert doc["counts"]["corpus"] == 3
    assert doc["counts"]["ingested"] == 3
    exact = next(q for q in doc["per_query"] if q["query_id"] == "q-exact-1")
    assert exact["recall_at_1"] == 1.0  # production ranker finds the invoice fact
    assert exact["ranked_ids"]  # real ranked output attached
    neg = next(q for q in doc["per_query"] if q["query_id"] == "q-neg-1")
    assert neg["recall_at_5"] is None  # non-applicable, never fabricated zero
    assert "latency_ms" in doc


def test_run_eval_is_deterministic(tmp_path: Path):
    a = retrieval_eval.normalize_for_baseline(
        retrieval_eval.run_eval(
            _mini_corpus(tmp_path),
            _mini_queries(tmp_path),
            work_dir=tmp_path / "w1",
            require_all_categories=False,
        )
    )
    b = retrieval_eval.normalize_for_baseline(
        retrieval_eval.run_eval(
            _mini_corpus(tmp_path),
            _mini_queries(tmp_path),
            work_dir=tmp_path / "w2",
            require_all_categories=False,
        )
    )
    assert a == b  # timings normalized out; logical results identical


def test_run_eval_rejects_malformed_fixtures(tmp_path: Path):
    with pytest.raises(retrieval_eval.FixtureError):
        retrieval_eval.run_eval(
            _write(tmp_path / "corpus.jsonl", ["{bad"]),
            _mini_queries(tmp_path),
            work_dir=tmp_path,
            require_all_categories=False,
        )


# ── committed fixtures (#77): counts, coverage, determinism ─────────────────

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "bench" / "retrieval"


def test_committed_fixtures_meet_minimum_counts():
    corpus = retrieval_eval.load_corpus(FIXTURE_DIR / "corpus.jsonl")
    queries = retrieval_eval.load_queries(
        FIXTURE_DIR / "queries.jsonl"
    )  # all 8 categories enforced
    assert len(corpus) >= 50
    assert len(queries) >= 40
    from collections import Counter

    counts = Counter(q["category"] for q in queries)
    for category in retrieval_eval.REQUIRED_CATEGORIES:
        assert counts[category] >= 5, category
    # Duplicate groups exist for the near_duplicate_diversity metric.
    assert any("duplicate_of" in rec for rec in corpus)


def test_committed_fixture_files_are_byte_stable(tmp_path: Path):
    """Regenerating the fixtures reproduces the committed bytes exactly."""
    before = {
        "corpus": (FIXTURE_DIR / "corpus.jsonl").read_bytes(),
        "queries": (FIXTURE_DIR / "queries.jsonl").read_bytes(),
    }
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(repo_root / "bench" / "retrieval" / "gen_fixtures.py")],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (FIXTURE_DIR / "corpus.jsonl").read_bytes() == before["corpus"]
    assert (FIXTURE_DIR / "queries.jsonl").read_bytes() == before["queries"]


# ── clone-equivalence stage + report (#77) ──────────────────────────────────


def test_run_eval_full_fixtures_clone_equivalence_perfect(tmp_path: Path):
    """The #77/#69 gate: every query's ordered ids and scores survive a
    verified package export and clean hydration, byte for byte."""
    doc = retrieval_eval.run_eval(
        FIXTURE_DIR / "corpus.jsonl", FIXTURE_DIR / "queries.jsonl", work_dir=tmp_path
    )
    clone = doc["clone_equivalence"]
    assert clone["queries_compared"] == len(doc["per_query"])
    assert clone["clone_equivalence_rate"] == 1.0
    assert clone["drift"] == []
    pkg = clone["package"]
    assert pkg["gz_bytes"] > 0
    assert pkg["verify_records_per_s"] > 0
    assert pkg["hydrate_records_per_s"] > 0
    assert "not cross-machine" in clone["note"]


def test_render_report_contains_required_sections(tmp_path: Path):
    doc = retrieval_eval.run_eval(
        FIXTURE_DIR / "corpus.jsonl",
        FIXTURE_DIR / "queries.jsonl",
        work_dir=tmp_path,
    )
    report = retrieval_eval.render_report(doc)
    for section in (
        "# Retrieval evaluation report",
        "## Overall metrics",
        "## Per category",
        "## Clone equivalence",
        "## Latency",
        "## Missed relevance",
        "## Five worst queries",
        "## What this benchmark does not prove",
    ):
        assert section in report, section
    assert "hotmem-hash-v1" in report


def test_measure_cold_start_subprocess(tmp_path: Path):
    result = retrieval_eval.measure_cold_start(
        _mini_corpus(tmp_path), _mini_queries(tmp_path), top_k=5
    )
    assert result.get("error") is None, result
    assert result["cold_start_seconds"] > 0


def test_main_writes_output_and_report_files(tmp_path: Path):
    out = tmp_path / "out" / "metrics.json"
    report = tmp_path / "out" / "report.md"
    code = retrieval_eval.main(
        [
            "--corpus",
            str(FIXTURE_DIR / "corpus.jsonl"),
            "--queries",
            str(FIXTURE_DIR / "queries.jsonl"),
            "--output",
            str(out),
            "--report",
            str(report),
        ]
    )
    assert code == 0
    assert out.is_file()
    assert report.is_file() and report.read_text(encoding="utf-8").startswith("# Retrieval")


# ── committed baseline regression guard (#77) ───────────────────────────────

BASELINE_PATH = FIXTURE_DIR / "baseline.json"


def test_baseline_regression_guard(tmp_path: Path):
    """CI guard: unacknowledged changes to ordered ids or metrics fail.

    Compares a fresh in-process run (timings normalized) against the
    committed baseline. Intentional changes require regenerating the
    baseline AND category-level before/after evidence in the PR (#77
    regen policy).
    """
    baseline = json.loads(BASELINE_PATH.read_text())
    fresh = retrieval_eval.normalize_for_baseline(
        retrieval_eval.run_eval(
            FIXTURE_DIR / "corpus.jsonl", FIXTURE_DIR / "queries.jsonl", work_dir=tmp_path
        )
    )
    assert fresh == baseline, (
        "retrieval behavior changed vs the committed baseline. If intentional, "
        'regenerate with: uv run python -c "<see bench/retrieval/README.md>" '
        "and include category-level before/after metrics in the PR."
    )


def test_recommendation_decision_rule(tmp_path: Path):
    """The deterministic rule fires in the documented order, from measured values."""
    doc = retrieval_eval.run_eval(
        FIXTURE_DIR / "corpus.jsonl", FIXTURE_DIR / "queries.jsonl", work_dir=tmp_path
    )
    rec = doc["recommendation"]
    # Measured on the committed fixtures: clone equivalence is 1.0 and the
    # semantic gap is 66.7pp (>= 15pp), so the rule must point at #78.
    assert rec["measured"]["clone_equivalence_rate"] == 1.0
    assert rec["action"] == "pursue_semantic_embedding_boundary_78"
    assert "15" in rec["rationale"] or "percentage points" in rec["rationale"]

    # Rule order: a clone regression overrides everything else.
    broken = json.loads(json.dumps(doc))
    broken["clone_equivalence"]["clone_equivalence_rate"] = 0.9
    assert retrieval_eval.build_recommendation(broken)["action"] == "fix_clone_index_compatibility"
    # Duplicate-slot rule only fires when the semantic gap is closed.
    narrowed = json.loads(json.dumps(doc))
    narrowed["categories"]["semantic_paraphrase"]["recall_at_5"]["mean"] = 0.95
    narrowed["overall"]["duplicate_slot_rate"]["mean"] = 0.25
    assert retrieval_eval.build_recommendation(narrowed)["action"] == "pursue_reranking_hook_80"
    narrowed["overall"]["duplicate_slot_rate"]["mean"] = 0.1
    assert retrieval_eval.build_recommendation(narrowed)["action"] == "retain_current_stack"
