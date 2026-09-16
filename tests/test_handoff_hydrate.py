"""Tests for #101 (commit 8) — atomic, idempotent handoff hydration.

Covers:
    - Clean-target round trip: ids, annotations-bearing metadata,
      provenance, timestamps, and promotion state survive (AC 8, 12).
    - Idempotence: the handoff ledger short-circuits a repeat hydration —
      zero writes, zero new events, identical fingerprint (AC 8).
    - Rollback: a mid-transaction failure leaves the target
      state-equivalent (AC 7, 13).
    - Insert-only: unrelated target records are preserved untouched
      (AC 8, 13).
    - The resume brief hydrates as ONE searchable record and is findable
      through the normal search path (AC 9 groundwork).
    - Documented limitation: a package record deleted at a used target is
      re-inserted on repeat hydration (no tombstones until #98).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.events import EventType, query_events
from hotmem.handoff.hydrate import brief_identifier_for, hydrate_handoff
from hotmem.handoff.package import prepare_handoff
from hotmem.handoff.verify import HandoffError
from hotmem.search import search_memories

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."


@pytest.fixture()
def pkg(tmp_path: Path) -> Path:
    result = prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent=CONSENT)
    return Path(result.path)


@pytest.fixture()
def db(tmp_path: Path):
    database = MemoryDB(str(tmp_path / "target.sqlite"))
    yield database
    database.close()


def _memory_lines(pkg: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (pkg / "memories.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _hydrate(db: MemoryDB, pkg: Path):
    return hydrate_handoff(db, str(pkg))


# ── Clean-target round trip (AC 8) ───────────────────────────────────────────


def test_hydrate_applies_memories_and_exactly_one_brief(db, pkg):
    result = _hydrate(db, pkg)
    assert result.loaded == 3  # 2 memories + 1 brief
    assert result.skipped_dupes == 0
    assert result.invalid == 0
    assert result.already_applied is False
    assert result.brief_identifier == "handoff/hotmem-showcase-planning/resume-brief"
    assert db.count() == 3

    # Package memory ids and fields survive the round trip.
    memory_lines = _memory_lines(pkg)
    for record in memory_lines:
        row = db.get_memory(record["id"])
        assert row is not None
        assert row["identifier"] == record["identifier"]
        assert row["fact_text"] == record["fact_text"]
        assert row["source"] == record["source"]
        assert row["created_at"] == record["created_at"]
        provenance = json.loads(row["provenance_json"])
        assert provenance["handoff"]["session_id"] == "sess-7f3a2b"


def test_brief_record_is_searchable_through_normal_path(db, pkg):
    _hydrate(db, pkg)
    results = search_memories(db, "resume brief handoff showcase", top_k=5)
    identifiers = [r.get("identifier") for r in results]
    assert "handoff/hotmem-showcase-planning/resume-brief" in identifiers
    # The brief text carries source ids and the tool-history marker.
    brief_row = next(
        db.get_memory(r["id"])
        for r in db.all_rows()
        if r["identifier"] == "handoff/hotmem-showcase-planning/resume-brief"
    )
    assert "do not re-run" in brief_row["fact_text"]
    metadata = json.loads(brief_row["metadata_json"])
    assert metadata["handoff"]["kind"] == "resume_brief"
    assert metadata["handoff"]["package_id"]
    assert "[source: cx-003]" in brief_row["fact_text"]


def test_decision_memory_is_retrievable(db, pkg):
    _hydrate(db, pkg)
    results = search_memories(db, "canonical handoff package CLI MCP HTTP", top_k=5)
    texts = [r.get("fact_text", r.get("content", "")) for r in results]
    assert any("hotmem-handoff-v1" in str(t) for t in texts)


def test_ledger_row_and_event_are_recorded_once(db, pkg):
    result = _hydrate(db, pkg)
    ledger = db.get_handoff(result.package_id)
    assert ledger is not None
    assert ledger["handoff_id"] == result.handoff_id
    assert ledger["mode"] == "resume"

    events = query_events(db, event_type=EventType.HANDOFF_APPLIED)
    assert events["count"] == 1
    payload = events["events"][0]["payload"]
    assert payload["package_id"] == result.package_id
    assert payload["brief_identifier"] == result.brief_identifier


# ── Idempotence (AC 8) ──────────────────────────────────────────────────────


def test_repeat_hydration_is_a_true_no_op(db, pkg):
    _hydrate(db, pkg)
    fingerprint_before = db.fingerprint()
    events_before = query_events(db)["count"]

    second = _hydrate(db, pkg)

    assert second.already_applied is True
    assert second.loaded == 0
    assert second.skipped_dupes == 0
    assert db.fingerprint() == fingerprint_before
    assert query_events(db)["count"] == events_before  # no second event
    assert db.count() == 3


def test_two_different_packages_hydrate_independently(db, tmp_path):
    prepare_handoff(FIXTURE, tmp_path / "a", mode="resume", consent=CONSENT)
    prepare_handoff(FIXTURE, tmp_path / "b", mode="archive", consent=CONSENT)
    ra = hydrate_handoff(db, str(tmp_path / "a"))
    rb = hydrate_handoff(db, str(tmp_path / "b"))
    assert ra.package_id != rb.package_id
    # Content-hash identity: package b's memories are logical duplicates of
    # a's (same source content), so only b's (distinct) brief loads.
    assert ra.loaded == 3
    assert rb.loaded == 1
    assert rb.skipped_dupes == 2
    assert db.count() == 4  # 2 shared memories + brief a + brief b
    assert query_events(db, event_type=EventType.HANDOFF_APPLIED)["count"] == 2


# ── Verification before hydration (AC 7) ────────────────────────────────────


def test_invalid_package_leaves_target_untouched(db, tmp_path):
    prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent=CONSENT)
    manifest_path = tmp_path / "pkg" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(HandoffError):
        _hydrate(db, tmp_path / "pkg")
    assert db.count() == 0
    assert db.get_handoff(manifest.get("package_id", "unknown")) is None


# ── Rollback (AC 7, 13) ──────────────────────────────────────────────────────


def test_partial_write_failure_rolls_back_state_equivalent(db, pkg, monkeypatch):
    _hydrate(db, pkg)  # first application succeeds
    # Reset the ledger to force a second real application, then fail
    # mid-transaction on the second batch insert.
    db._conn.execute("DELETE FROM handoff_ledger")  # noqa: SLF001 — test seam
    db.commit()
    fingerprint_before = db.fingerprint()

    real_insert = db.insert_many_ignore
    calls: list[int] = []

    def failing_insert(records, *, _commit=True):
        calls.append(len(calls))
        if len(calls) == 2:  # memories batch went through; brief batch fails
            raise sqlite3.OperationalError("simulated mid-transaction failure")
        return real_insert(records, _commit=_commit)

    monkeypatch.setattr(db, "insert_many_ignore", failing_insert)
    with pytest.raises(sqlite3.OperationalError):
        _hydrate(db, pkg)

    assert db.fingerprint() == fingerprint_before
    assert db.get_handoff(_package_id_of(pkg)) is None  # ledger rolled back too
    assert query_events(db, event_type=EventType.HANDOFF_APPLIED)["count"] == 1


def _package_id_of(pkg: Path) -> str:
    manifest = json.loads((pkg / "manifest.json").read_text())
    return manifest["package_id"]


# ── Insert-only semantics (AC 8, 13) ────────────────────────────────────────


def test_unrelated_target_records_are_preserved(db, pkg, tmp_path):
    for i in range(3):
        db.insert(
            id=f"unrelated-{i}",
            identifier=f"prior/thing-{i}",
            fact_text=f"unrelated fact {i}",
            embedding=b"\x00" * 8,
            embedding_dim=8,
            embedding_model="hotmem-hash-v1",
            source="manual",
            importance=0.5,
            metadata_json="{}",
            content_hash=f"hash-{i}",
        )
    db.commit()
    fingerprint_unrelated = {r["id"]: r["fact_text"] for r in db.all_rows()}

    _hydrate(db, pkg)

    rows = {r["id"]: r for r in db.all_rows()}
    for memory_id, fact_text in fingerprint_unrelated.items():
        assert rows[memory_id]["fact_text"] == fact_text
    assert db.count() == 3 + 3  # 3 unrelated + 2 memories + 1 brief


def test_deleted_package_record_is_reinserted_documented_limitation(db, pkg):
    """No tombstones until #98: a deleted package record comes back (documented)."""
    _hydrate(db, pkg)
    memory_lines = _memory_lines(pkg)
    victim = memory_lines[0]["id"]

    # Operator deletes the memory directly (no tombstone recorded).
    conn = sqlite3.connect(db._conn.execute("PRAGMA database_list").fetchone()[2])
    conn.execute("DELETE FROM memories WHERE id = ?", (victim,))
    conn.commit()
    conn.close()
    assert db.get_memory(victim) is None

    # Wipe the ledger to simulate a re-run against a fresh export import.
    db._conn.execute("DELETE FROM handoff_ledger")
    db.commit()
    again = _hydrate(db, pkg)
    assert again.loaded == 1  # deleted record re-inserted (documented limitation)
    assert db.get_memory(victim) is not None


def test_archive_package_hydrates_without_brief_when_absent(db, tmp_path):
    prepare_handoff(FIXTURE, tmp_path / "arch", mode="archive", consent=CONSENT)
    # Archive packages carry a brief in v1, but hydration must tolerate its
    # absence if a future writer omits it.
    arch = tmp_path / "arch"
    (arch / "resume-brief.json").unlink()
    manifest_path = arch / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].pop("resume-brief.json")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))
    result = hydrate_handoff(db, str(arch))
    assert result.loaded == 2  # memories only
    assert result.brief_identifier == brief_identifier_for(manifest)


def test_embeddings_are_rebuilt_under_runtime_embedder(db, pkg):
    result = _hydrate(db, pkg)
    assert result.embedding_rebuilt == 3  # no stored vectors in handoff packages
    assert result.embedding_reused == 0
    assert result.disposition() == {
        "embedding_reused": 0,
        "embedding_rebuilt": 3,
        "embedding_missing": 0,
        "embedding_failed": 0,
    }
