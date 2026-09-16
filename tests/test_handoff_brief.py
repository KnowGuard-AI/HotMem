"""Tests for #101 (commit 5) — bounded deterministic resume brief.

Covers:
    - Fixture brief: every section renders, items link their source entry
      ids, the no-re-run tool marker is explicit (acceptance 10), the goal
      is the first user turn, and memory identifiers are indexed.
    - Determinism: the same session yields byte-identical text.
    - Budget: oversized sessions trim least-important items first and
      never exceed the budget; tiny budgets still produce valid text.
    - The redaction gate: the fixture's secret never reaches brief text.
    - Empty sections are omitted; a session with no user turn degrades
      gracefully.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotmem.handoff.brief import TOOL_HISTORY_MARKER, build_brief
from hotmem.handoff.codex_source import NormalizedSession, read_codex_export
from hotmem.handoff.redact import RedactionLeakError, redact_normalized

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."
SECRET_VALUE = "hotmem_sk_9f3d2c8b7a6e5f4d"


def _session() -> NormalizedSession:
    return redact_normalized(read_codex_export(FIXTURE, consent=CONSENT))


def _synthetic(entries: list[dict], memories: list[dict] | None = None) -> NormalizedSession:
    return NormalizedSession(
        adapter="codex-export",
        adapter_version="1",
        session_id="sess-t",
        label="synthetic",
        time_range={"started_at": "2026-09-16T09:00:00Z", "ended_at": "2026-09-16T10:00:00Z"},
        codex_version=None,
        entries=entries,
        memory_records=memories or [],
    )


def _entry(seq: int, kind: str, text: str, role: str | None = None) -> dict:
    entry = {
        "schema_version": 1,
        "id": f"e{seq}",
        "seq": seq,
        "kind": kind,
        "text": text,
        "source": {"source_entry_id": f"cx-{seq:03d}", "source_seq": seq},
    }
    if role:
        entry["role"] = role
    return entry


# ── Fixture brief ────────────────────────────────────────────────────────────


def test_fixture_brief_renders_all_sections_with_links():
    brief = build_brief(_session())
    text = brief.text
    assert text.startswith("# Resume brief — hotmem-showcase-planning")
    for header in (
        "## Goal",
        "## Decisions",
        "## Commitments",
        "## Unresolved questions",
        "## Next actions",
        f"## {TOOL_HISTORY_MARKER}",
        "## Files",
        "## Durable memories",
    ):
        assert header in text, f"missing section: {header}"
    # Items link their source entry ids (acceptance 4: brief links).
    assert "[source: cx-003]" in text
    assert "[source: cx-009]" in text
    # The goal is the first user turn.
    assert "Prepare the end-to-end session handoff showcase" in text
    # Memory index carries identifiers and id prefixes.
    assert "showcase/handoff-package-contract [memory: " in text
    # Coverage is visible.
    assert "6 omission(s), 1 redaction(s)" in text


def test_tool_history_is_explicitly_marked_do_not_rerun():
    brief = build_brief(_session())
    marker_index = brief.text.index(TOOL_HISTORY_MARKER)
    tool_line = next(line for line in brief.text.splitlines() if line.startswith("- shell "))
    assert brief.text.index(tool_line) > marker_index


def test_brief_links_carry_source_and_memory_ids():
    brief = build_brief(_session())
    assert "cx-003" in brief.links["source_entry_ids"]
    assert "cx-008" in brief.links["source_entry_ids"]
    assert len(brief.links["memory_ids"]) == 2
    assert brief.char_count == len(brief.text)
    assert brief.to_dict()["char_count"] == brief.char_count


def test_brief_is_deterministic():
    a = build_brief(_session())
    b = build_brief(_session())
    assert a.text == b.text
    assert a.sections == b.sections
    assert a.links == b.links


def test_fixture_secret_never_reaches_brief_text():
    brief = build_brief(_session())
    assert SECRET_VALUE not in brief.text


def test_secret_in_goal_user_turn_is_placeholdered_in_brief():
    entries = [
        _entry(1, "turn", "my key is hotmem_sk_deadbeef1234 ok", role="user"),
        _entry(2, "decision", "a decision"),
    ]
    # The real flow: layer-2 redaction runs before the brief is built.
    session = redact_normalized(_synthetic(entries))
    brief = build_brief(session, budget=100_000)
    assert "hotmem_sk_deadbeef1234" not in brief.text
    assert "[REDACTED:prefixed_credential]" in brief.text


def test_output_gate_fails_closed_on_unredacted_brief_content():
    # Defense in depth: if layer 2 is ever skipped, the brief builder's
    # own gate (layer 3) refuses to emit the text.
    entries = [_entry(1, "turn", "my key is hotmem_sk_deadbeef1234 ok", role="user")]
    with pytest.raises(RedactionLeakError, match="resume brief"):
        build_brief(_synthetic(entries), budget=100_000)


# ── Budget behavior ──────────────────────────────────────────────────────────


def test_budget_trims_least_important_items_first():
    entries = [
        _entry(1, "turn", "what is the goal of this session?", role="user"),
        _entry(2, "decision", "decide " + "d" * 300),
        _entry(3, "commitment", "commit " + "c" * 300),
        _entry(4, "file_reference", "f" * 300),
        _entry(5, "tool_call", "t" * 300),
    ]
    session = _synthetic(entries)
    full = build_brief(session, budget=100_000)
    assert len(full.text) > 1400  # everything fits at a large budget
    assert "## Files" in full.text and "## Decisions" in full.text

    trimmed = build_brief(session, budget=1200)
    assert len(trimmed.text) <= 1200
    # Files and tools dropped first; decisions/commitments (with the goal)
    # survive as the highest-value context.
    assert "## Files" not in trimmed.text
    assert "decide" in trimmed.text and "commit" in trimmed.text
    assert trimmed.char_count <= 1200


def test_tiny_budget_still_produces_valid_text():
    brief = build_brief(_session(), budget=80)
    assert len(brief.text) <= 80
    assert brief.text.endswith("\n")


def test_recent_items_win_when_sections_cap():
    entries = [_entry(i, "decision", f"decision {i}") for i in range(1, 16)]
    brief = build_brief(_synthetic(entries), budget=100_000)
    decisions = brief.sections["decisions"]
    assert len(decisions) == 10
    # The most recent 10 survive (deterministic recency priority).
    assert decisions[0]["source_entry_id"] == "cx-006"
    assert decisions[-1]["source_entry_id"] == "cx-015"


# ── Graceful degradation ──────────────────────────────────────────────────────


def test_empty_sections_are_omitted_and_no_user_turn_degrades():
    entries = [_entry(1, "decision", "only a decision")]
    brief = build_brief(_synthetic(entries), budget=100_000)
    assert "## Commitments" not in brief.text
    assert "(no user turn captured)" in brief.text
    assert brief.omissions_note is None


def test_no_false_section_for_empty_session():
    brief = build_brief(_synthetic([]), budget=100_000)
    assert "## Decisions" not in brief.text
    assert brief.links == {"source_entry_ids": [], "memory_ids": []}
