"""End-to-end acceptance flow for #101 — the automated showcase mirror.

One scripted pass over the whole contract, mirroring the acceptance
criteria: prepare from the fixture export, verify fail-closed, inspect the
criterion-11 report, hydrate a clean target atomically, retrieve the
resume brief and durable memories through the UNMODIFIED search path with
source ids visible, repeat hydration as a true no-op, prove the two modes
differ, prove tool entries are inert data, prove the secret never leaks
into any package artifact, and prove hydrated rows re-export through the
EXISTING interchange writer (no second source of truth).

Byte-stability goldens live in tests/golden/fixtures/handoff/ (payloads
committed verbatim; the manifest masks handoff_id/created_at/consent.at).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.events import EventType, query_events
from hotmem.handoff import EntryKind
from hotmem.handoff.hydrate import hydrate_handoff
from hotmem.handoff.package import prepare_handoff
from hotmem.handoff.verify import inspect_handoff, verify_handoff
from hotmem.interchange.hydrate import verify_package
from hotmem.interchange.package import write_package
from hotmem.search import search_memories

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
GOLDEN = Path(__file__).parent / "golden" / "fixtures" / "handoff"
CONSENT = "I consent to capturing this session for the handoff showcase."
SECRET_VALUE = "hotmem_sk_9f3d2c8b7a6e5f4d"


# ── Byte-stability goldens (AC 6, 12) ───────────────────────────────────────


def test_reprepare_matches_committed_goldens(tmp_path):
    result = prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent=CONSENT)
    pkg = Path(result.path)

    for name in ("session.jsonl", "memories.jsonl", "resume-brief.json"):
        assert (pkg / name).read_bytes() == (GOLDEN / name).read_bytes(), name

    actual = json.loads((pkg / "manifest.json").read_text())
    golden = json.loads((GOLDEN / "manifest.json").read_text())
    actual["handoff_id"] = "<uuid>"
    actual["created_at"] = "<ts>"
    actual["consent"]["at"] = "<ts>"
    assert actual == golden


def test_golden_package_itself_verifies():
    """The committed golden (masked volatiles) remains a valid package."""
    verified = verify_handoff(GOLDEN)
    assert verified.entry_count == 11
    assert verified.memory_count == 2


# ── The full acceptance flow (AC 1-14 mirror) ──────────────────────────────


def test_full_handoff_flow(tmp_path):
    # 1. Prepare (resume) from the source export — explicit consent only.
    prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent=CONSENT)

    # 2. Verify fail-closed before any target write.
    verified = verify_handoff(tmp_path / "pkg")
    assert verified.entry_count == 11

    # 3. Inspect reports the full criterion-11 field list.
    report = inspect_handoff(tmp_path / "pkg")
    for key in (
        "handoff_id",
        "package_id",
        "mode",
        "source",
        "target",
        "compatibility",
        "counts",
        "entries_by_kind",
        "files",
        "coverage",
        "limits",
        "created_at",
        "hotmem_version",
        "valid",
        "failure",
    ):
        assert key in report, f"criterion-11 field missing: {key}"

    # 4. Hydrate a clean target atomically.
    db = MemoryDB(str(tmp_path / "target.sqlite"))
    try:
        applied = hydrate_handoff(db, str(tmp_path / "pkg"))
        assert (applied.loaded, applied.invalid) == (3, 0)

        # 5. AC 9: retrieval through the normal search path, source ids visible.
        brief_hits = search_memories(db, "resume brief handoff showcase", top_k=5)
        brief = next(
            h
            for h in brief_hits
            if h.get("identifier") == "handoff/hotmem-showcase-planning/resume-brief"
        )
        assert "[source: cx-003]" in brief["content"]  # source ids visible
        assert "do not re-run" in brief["content"]

        decision_hits = search_memories(db, "canonical handoff package surfaces", top_k=5)
        assert decision_hits, "durable memory not retrievable"

        # 6. Repeat hydration is a true no-op; identical fingerprint.
        fingerprint = db.fingerprint()
        repeat = hydrate_handoff(db, str(tmp_path / "pkg"))
        assert repeat.already_applied and repeat.loaded == 0
        assert db.fingerprint() == fingerprint

        # 7. Exactly one handoff event; nothing else appeared.
        events = query_events(db, event_type=EventType.HANDOFF_APPLIED)
        assert events["count"] == 1
        assert events["events"][0]["payload"]["package_id"] == applied.package_id

        # 8. AC 12: hydrated rows re-export through the EXISTING interchange
        #    writer and the clone verifies — no second source of truth.
        write_package(db, tmp_path / "clone")
        clone = verify_package(tmp_path / "clone")
        assert clone.record_count == 3
    finally:
        db.close()


def test_modes_are_demonstrably_different(tmp_path):
    resume = prepare_handoff(FIXTURE, tmp_path / "resume", mode="resume", consent=CONSENT)
    archive = prepare_handoff(FIXTURE, tmp_path / "archive", mode="archive", consent=CONSENT)

    resume_entries = len(verify_handoff(tmp_path / "resume").stream_lines())
    archive_entries = len(verify_handoff(tmp_path / "archive").stream_lines())
    assert resume_entries == 11 < archive_entries == 12
    assert resume.package_id != archive.package_id
    assert inspect_handoff(tmp_path / "resume")["coverage"]["omitted_count"] == 7
    assert inspect_handoff(tmp_path / "archive")["coverage"]["omitted_count"] == 6


def test_tool_entries_are_inert_data(tmp_path):
    """AC 10: tool history is inspectable data; nothing is ever executed."""
    prepare_handoff(FIXTURE, tmp_path / "pkg", mode="archive", consent=CONSENT)
    entries = [
        json.loads(line) for line in (tmp_path / "pkg" / "session.jsonl").read_text().splitlines()
    ]
    tools = [e for e in entries if e["kind"] in (EntryKind.TOOL_CALL, EntryKind.TOOL_RESULT)]
    assert tools, "fixture must carry tool entries"
    for entry in tools:
        assert entry["executable"] is False
        assert entry["text"] and "subprocess" not in entry["text"].lower()

    # The brief renders the no-re-run marker above any tool line.
    brief = json.loads((tmp_path / "pkg" / "resume-brief.json").read_text())
    assert brief["text"].index("do not re-run") < brief["text"].index("- shell ")

    # Hydration inserts rows only — the store grows by memories + brief
    # and nothing beyond the handoff event ever appears in the log.
    db = MemoryDB(str(tmp_path / "t.sqlite"))
    try:
        result = hydrate_handoff(db, str(tmp_path / "pkg"))
        assert db.count() == result.loaded
        assert {e["event_type"] for e in query_events(db)["events"]} == {EventType.HANDOFF_APPLIED}
    finally:
        db.close()


def test_secret_never_appears_in_any_package_artifact(tmp_path):
    """AC 5: redacted values never reach any package — any file, any byte."""
    packages = []
    for mode in ("resume", "archive"):
        prepare_handoff(FIXTURE, tmp_path / mode, mode=mode, consent=CONSENT)
        packages.append(tmp_path / mode)
    for pkg in packages:
        for artifact in pkg.iterdir():
            assert SECRET_VALUE.encode() not in artifact.read_bytes(), f"leak in {artifact.name}"

    # In the archive package the redacted turn survives as a placeholder
    # (resume mode bounds that middle turn out with an omission record).
    session_text = (tmp_path / "archive" / "session.jsonl").read_text()
    assert "[REDACTED:api_key]" in session_text
    assert "HOTMEM_API_KEY=" in session_text  # the name stays visible


@pytest.mark.parametrize("mode", ["resume", "archive"])
def test_inspect_json_is_script_friendly(tmp_path, mode):
    """The showcase runbook drives everything off inspect --json output."""
    prepare_handoff(FIXTURE, tmp_path / "pkg", mode=mode, consent=CONSENT)
    report = inspect_handoff(tmp_path / "pkg")
    # Round-trips through JSON cleanly (paths and None included).
    assert json.loads(json.dumps(report)) == report
    assert report["valid"] is True
    assert report["mode"] == mode
