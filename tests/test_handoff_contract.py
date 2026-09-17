"""Tests for #101 (commit 1) — handoff contract surface and Codex export fixture.

Covers:
    - Fixture conformance: envelope format, ordered seq, unique ids, and the
      required kind variety (acceptance criterion 2).
    - Stable entry ids and content hashes: determinism across calls, source
      sensitivity, and the package_id mirroring of logical_id.
    - Limits defaults (bounded package policy, handoff-v1 §4.3).
    - Fixture redaction/deny targets: a secret-bearing line and a
      hidden_prompt line exist for later commits to prove against.
"""

from __future__ import annotations

import json
from pathlib import Path

from hotmem.handoff import (
    DEFAULT_LIMITS,
    ENTRY_KINDS,
    FORMAT_ID,
    MODES,
    SCHEMA_VERSION,
    EntryKind,
    entry_content_hash,
    entry_id_for,
    package_id_for,
)
from hotmem.interchange.canonical import logical_id

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"


def load_envelope() -> dict:
    return json.loads((FIXTURE / "export.json").read_text())


def load_lines() -> list[dict]:
    lines = []
    for raw in (FIXTURE / "session.jsonl").read_text().splitlines():
        if raw.strip():
            lines.append(json.loads(raw))
    return lines


# ── Fixture conformance (acceptance criterion 2) ────────────────────────────


def test_fixture_envelope_declares_format_and_session():
    env = load_envelope()
    assert env["format"] == "codex-export-v1"
    session = env["session"]
    assert session["id"] == "sess-7f3a2b"
    assert session["label"] == "hotmem-showcase-planning"
    assert session["started_at"] < session["ended_at"]


def test_fixture_seq_strictly_increasing_with_unique_ids():
    lines = load_lines()
    seqs = [line["seq"] for line in lines]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    ids = [line["id"] for line in lines]
    assert len(set(ids)) == len(ids)


def test_fixture_has_required_kind_variety():
    lines = load_lines()
    types = [line["type"] for line in lines]
    for required in (
        "turn",
        "decision",
        "commitment",
        "unresolved_question",
        "next_action",
        "tool_call",
        "tool_result",
        "file_reference",
        "memory_item",
    ):
        assert required in types, f"fixture missing source type: {required}"
    roles = [line["role"] for line in lines if line["type"] == "turn"]
    assert "user" in roles and "assistant" in roles


def test_fixture_carries_redaction_and_deny_targets():
    lines = load_lines()
    # A secret-bearing user turn (deny-by-default redaction must catch it).
    secret_lines = [line for line in lines if "HOTMEM_API_KEY=" in line.get("text", "")]
    assert len(secret_lines) == 1
    # A hidden prompt line that must never be read (policy omission).
    assert any(line["type"] == "hidden_prompt" for line in lines)
    # Unsupported extras on a turn (omission listing must surface them).
    extras = [line for line in lines if "reasoning_tokens" in line]
    assert len(extras) == 1


# ── Contract constants and ID derivation ─────────────────────────────────────


def test_contract_constants():
    assert FORMAT_ID == "hotmem-handoff-v1"
    assert SCHEMA_VERSION == 1
    assert MODES == ("resume", "archive")
    assert EntryKind.TOOL_CALL in ENTRY_KINDS
    assert "bogus" not in ENTRY_KINDS


def test_entry_id_for_is_deterministic_and_source_sensitive():
    a = entry_id_for("codex-export", "sess-1", "cx-001")
    assert a == entry_id_for("codex-export", "sess-1", "cx-001")
    assert a != entry_id_for("codex-export", "sess-1", "cx-002")
    assert a != entry_id_for("codex-export", "sess-2", "cx-001")
    assert a != entry_id_for("other-adapter", "sess-1", "cx-001")
    assert len(a) == 64


def test_entry_content_hash_is_stable_and_order_insensitive():
    entry = {"id": "e1", "seq": 1, "kind": "turn", "text": "hello"}
    assert entry_content_hash(entry) == entry_content_hash(dict(reversed(list(entry.items()))))
    other = dict(entry, text="hello!")
    assert entry_content_hash(entry) != entry_content_hash(other)


def test_package_id_for_mirrors_logical_id_algorithm():
    hashes = ["b", "a", "c"]
    assert package_id_for(hashes) == logical_id(hashes)
    assert package_id_for(hashes) == package_id_for(reversed(hashes))
    assert package_id_for(["a"]) != package_id_for(["b"])


def test_limits_defaults_are_sane():
    limits = DEFAULT_LIMITS
    assert limits.max_session_entries >= 100
    assert limits.max_entry_bytes >= 4096
    assert limits.max_tool_result_bytes <= limits.max_entry_bytes
    assert limits.brief_char_budget >= 1000
    assert limits.max_memories >= 10
    assert limits.max_consent_chars >= 64


# ── Documentation consistency guards (L4) ───────────────────────────────────


def test_fixture_readme_inventory_matches_actual_lines():
    """The fixture README's inventory must describe the real fixture."""
    from collections import Counter

    lines = load_lines()
    turns = Counter(line["role"] for line in lines if line["type"] == "turn")
    readme = (FIXTURE / "README.md").read_text()
    assert f"{len(lines)} lines)" in readme, "README omits/mismatches the line count"
    expected = f"{sum(turns.values())} turns ({turns['user']} user / {turns['assistant']} assistant"
    assert expected in readme, "README turn counts drifted from the fixture"


def test_contract_documents_every_brief_field():
    """Every BriefDocument field must appear in the normative contract."""
    import dataclasses

    from hotmem.handoff.brief import BriefDocument

    contract = (Path(__file__).parent.parent / "docs" / "okf" / "handoff-v1.md").read_text()
    for field in dataclasses.fields(BriefDocument):
        assert field.name in contract, f"contract §6 omits BriefDocument.{field.name}"
