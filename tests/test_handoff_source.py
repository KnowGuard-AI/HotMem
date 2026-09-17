"""Tests for #101 (commit 3) — Codex export source adapter.

Covers:
    - Consent is required and checked before any content is read (AC 3).
    - Session selection: explicit id must match the export (AC 3).
    - Normalization: ordering, entry kinds, stable ids, provenance links,
      deterministic memory records valid as interchange records (AC 4, 12).
    - Honesty: unsupported fields are listed, hidden prompts are never
      read, unknown types are recorded (AC 4, 5 groundwork).
    - Bounds: entry/tool-result truncation, session and memory caps —
      omission records, never crashes (handoff-v1 §4.3).
    - Malformed sources fail closed with line-numbered diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.handoff import DEFAULT_LIMITS, EntryKind, Limits, entry_id_for
from hotmem.handoff.codex_source import (
    STREAM_NAME,
    ConsentError,
    ExportFormatError,
    MalformedSourceError,
    SelectionError,
    read_codex_export,
)
from hotmem.interchange.canonical import canonical_line
from hotmem.interchange.record import normalize_record, validate_record

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"

CONSENT = "I consent to capturing this session for the handoff showcase."


def _read_fixture(**kwargs) -> object:
    return read_codex_export(FIXTURE, consent=CONSENT, **kwargs)


def _write_export(root: Path, lines: list[dict], session_id: str = "sess-x") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "export.json").write_text(
        json.dumps(
            {
                "format": "codex-export-v1",
                "session": {
                    "id": session_id,
                    "label": "test-session",
                    "started_at": "2026-09-16T09:00:00Z",
                    "ended_at": "2026-09-16T10:00:00Z",
                },
            }
        )
    )
    with open(root / STREAM_NAME, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return root


def _turn(seq: int, text: str = "hello") -> dict:
    return {"type": "turn", "id": f"cx-{seq:03d}", "seq": seq, "role": "user", "text": text}


# ── Consent and selection (AC 3) ─────────────────────────────────────────────


@pytest.mark.parametrize("bad_consent", [None, "", "   ", "\t\n"])
def test_consent_is_required_before_any_read(bad_consent):
    with pytest.raises(ConsentError, match="consent"):
        read_codex_export(FIXTURE, consent=bad_consent)


def test_session_selection_must_match_envelope(tmp_path):
    with pytest.raises(SelectionError, match="sess-7f3a2b"):
        _read_fixture(session_id="sess-other")
    matched = _read_fixture(session_id="sess-7f3a2b")
    assert matched.session_id == "sess-7f3a2b"


def test_missing_or_wrong_envelope_fails_closed(tmp_path):
    with pytest.raises(ExportFormatError, match="export.json"):
        read_codex_export(tmp_path, consent=CONSENT)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "export.json").write_text(json.dumps({"format": "not-codex"}))
    with pytest.raises(ExportFormatError, match="unsupported export format"):
        read_codex_export(bad, consent=CONSENT)


# ── Normalization of the fixture (AC 2, 4) ───────────────────────────────────


def test_fixture_normalizes_with_expected_counts():
    session = _read_fixture()
    # 15 fixture lines: 12 session entries, 2 memory records,
    # 1 policy-denied hidden prompt (+ 5 unsupported-field omissions).
    assert len(session.entries) == 12
    assert len(session.memory_records) == 2
    kinds = [e["kind"] for e in session.entries]
    assert kinds.count(EntryKind.TURN) == 5
    for kind in (
        EntryKind.DECISION,
        EntryKind.COMMITMENT,
        EntryKind.UNRESOLVED_QUESTION,
        EntryKind.NEXT_ACTION,
        EntryKind.TOOL_CALL,
        EntryKind.TOOL_RESULT,
        EntryKind.FILE_REFERENCE,
    ):
        assert kind in kinds


def test_ordering_and_stable_ids():
    session = _read_fixture()
    seqs = [e["seq"] for e in session.entries]
    assert seqs == sorted(seqs)
    for entry in session.entries:
        assert entry["id"] == entry_id_for(
            session.adapter, session.session_id, entry["source"]["source_entry_id"]
        )
    assert len({e["id"] for e in session.entries}) == len(session.entries)


def test_entries_carry_provenance_and_timestamps():
    session = _read_fixture()
    first = session.entries[0]
    assert first["schema_version"] == 1
    assert first["source"]["adapter"] == "codex-export"
    assert first["source"]["session_id"] == session.session_id
    assert first["created_at"] == "2026-09-16T09:02:11Z"
    # Tool entries are inert data by construction (AC 10 groundwork).
    tool_call = next(e for e in session.entries if e["kind"] == EntryKind.TOOL_CALL)
    assert tool_call["executable"] is False
    assert tool_call["summary"] == "shell"
    tool_result = next(e for e in session.entries if e["kind"] == EntryKind.TOOL_RESULT)
    assert tool_result["executable"] is False
    assert tool_result["summary"] == "shell: ok"
    file_ref = next(e for e in session.entries if e["kind"] == EntryKind.FILE_REFERENCE)
    assert file_ref["artifacts"] == [
        {
            "path": "docs/okf/handoff-v1.md",
            "note": "The normative handoff contract this session produced.",
        }
    ]


def test_memory_records_are_valid_interchange_records():
    session = _read_fixture()
    for record in session.memory_records:
        normalized = normalize_record(record, default_source="handoff")
        assert validate_record(normalized) == []
        assert record["source"] == "handoff:codex-export"
        assert record["provenance"]["handoff"]["session_id"] == session.session_id
        assert "embedding" not in record
    ids = [r["id"] for r in session.memory_records]
    assert len(set(ids)) == len(ids)


def test_read_is_deterministic():
    a = _read_fixture()
    b = _read_fixture()
    la = "".join(canonical_line(e) for e in a.entries)
    lb = "".join(canonical_line(e) for e in b.entries)
    assert la == lb
    assert a.memory_records == b.memory_records


# ── Honesty: omissions and the deny list (AC 4) ──────────────────────────────


def test_unsupported_fields_are_listed_not_dropped():
    session = _read_fixture()
    field_omissions = [o for o in session.omissions if o["field"] in ("model", "reasoning_tokens")]
    assert {o["field"] for o in field_omissions} == {"model", "reasoning_tokens"}
    assert all(o["recoverable"] for o in field_omissions)
    # owner / asked_by / priority are also unsupported extras on typed lines.
    assert {"owner", "asked_by", "priority"} <= {o["field"] for o in session.omissions}


def test_hidden_prompt_is_never_read_only_recorded():
    session = _read_fixture()
    policy = [o for o in session.omissions if o["reason"].startswith("policy")]
    assert len(policy) == 1
    assert policy[0]["recoverable"] is False
    # The hidden prompt's content must not appear anywhere in the result.
    serialized = json.dumps(
        {"e": session.entries, "m": session.memory_records, "o": session.omissions}
    )
    assert "system prompt content" not in serialized


def test_unknown_line_type_is_recorded(tmp_path):
    root = _write_export(tmp_path, [_turn(1), {"type": "vibe_check", "id": "cx-002", "seq": 2}])
    session = read_codex_export(root, consent=CONSENT)
    assert len(session.entries) == 1
    assert any("vibe_check" in o["reason"] for o in session.omissions)


# ── Bounds produce omission records, never crashes (handoff-v1 §4.3) ────────


def test_entry_truncation_records_omission(tmp_path):
    big = "x" * 300
    root = _write_export(tmp_path, [_turn(1, big)])
    session = read_codex_export(
        root, consent=CONSENT, limits=Limits(max_entry_bytes=100, brief_char_budget=600)
    )
    entry = session.entries[0]
    # L2: the marker is budgeted inside the bound — the stored text never
    # exceeds the byte bound the omission record refers to.
    assert len(entry["text"].encode()) <= 100
    assert entry["text"].endswith("…[truncated]")
    marker_bytes = len("…[truncated]".encode())
    assert entry["text"].startswith("x" * (100 - marker_bytes))
    assert any(o["reason"].startswith("entry exceeded") for o in session.omissions)


@pytest.mark.parametrize("cap", [1, 5, 14, 15, 64, 100, 1024])
def test_truncation_never_exceeds_the_byte_bound(tmp_path, cap):
    """L2: bound honesty across caps, including caps below the marker size."""
    root = _write_export(tmp_path, [_turn(1, "z" * 4096)])
    session = read_codex_export(
        root, consent=CONSENT, limits=Limits(max_entry_bytes=cap, brief_char_budget=600)
    )
    text = session.entries[0]["text"]
    assert len(text.encode()) <= cap
    # The result must remain valid UTF-8 (no half-cut code point).
    assert text.encode().decode() == text
    if cap > len("…[truncated]".encode()):
        assert text.endswith("…[truncated]")


def test_tool_result_bound_is_tighter_than_entry_bound(tmp_path):
    lines = [
        _turn(1),
        {
            "type": "tool_result",
            "id": "cx-002",
            "seq": 2,
            "tool": "shell",
            "status": "ok",
            "result_summary": "y" * 500,
        },
    ]
    root = _write_export(tmp_path, lines)
    session = read_codex_export(
        root, consent=CONSENT, limits=Limits(max_entry_bytes=5000, max_tool_result_bytes=100)
    )
    tool_result = session.entries[1]
    assert tool_result["text"].endswith("…[truncated]")
    assert any("tool result exceeded" in o["reason"] for o in session.omissions)


def test_session_cap_bounds_stream_with_single_omission(tmp_path):
    limits = Limits(max_session_entries=3)
    root = _write_export(tmp_path, [_turn(i) for i in range(1, 8)])
    session = read_codex_export(root, consent=CONSENT, limits=limits)
    assert len(session.entries) == 3
    caps = [o for o in session.omissions if "max_session_entries" in o["reason"]]
    assert len(caps) == 1


def test_memory_cap_bounds_with_per_item_omissions(tmp_path):
    limits = Limits(max_memories=1)
    lines = [_turn(1)] + [
        {
            "type": "memory_item",
            "id": f"cx-{i:03d}",
            "seq": i,
            "identifier": f"m/{i}",
            "fact": f"fact {i}",
        }
        for i in range(2, 5)
    ]
    root = _write_export(tmp_path, lines)
    session = read_codex_export(root, consent=CONSENT, limits=limits)
    assert len(session.memory_records) == 1
    assert sum(1 for o in session.omissions if "max_memories" in o["reason"]) == 2


# ── Malformed sources fail closed ────────────────────────────────────────────


def test_malformed_json_line_reports_line_number(tmp_path):
    root = _write_export(tmp_path, [_turn(1)])
    with open(root / STREAM_NAME, "a") as f:
        f.write("{not json}\n")
    with pytest.raises(MalformedSourceError, match="session.jsonl:2"):
        read_codex_export(root, consent=CONSENT)


def test_duplicate_or_disordered_seq_fail_closed(tmp_path):
    root = _write_export(tmp_path, [_turn(1), _turn(1)])
    with pytest.raises(MalformedSourceError, match="duplicate source id"):
        read_codex_export(root, consent=CONSENT)
    root2 = _write_export(tmp_path / "b", [_turn(5), _turn(2)])
    with pytest.raises(MalformedSourceError, match="does not increase"):
        read_codex_export(root2, consent=CONSENT)


def test_missing_required_fields_fail_closed(tmp_path):
    root = _write_export(tmp_path, [{"type": "turn", "id": "cx-001", "seq": 1}])
    with pytest.raises(MalformedSourceError, match="requires"):
        read_codex_export(root, consent=CONSENT)
    root2 = _write_export(tmp_path / "b", [{"type": "memory_item", "id": "cx-001", "seq": 1}])
    with pytest.raises(MalformedSourceError, match="requires"):
        read_codex_export(root2, consent=CONSENT)


def test_missing_stream_file_fails_closed(tmp_path):
    root = _write_export(tmp_path, [])
    (root / STREAM_NAME).unlink()
    with pytest.raises(MalformedSourceError, match="missing session.jsonl"):
        read_codex_export(root, consent=CONSENT)


def test_default_limits_fixture_passes_unbounded():
    session = _read_fixture(limits=DEFAULT_LIMITS)
    assert len(session.omissions) == 6  # 5 unsupported fields + 1 hidden prompt
    assert all(o["recoverable"] or o["reason"].startswith("policy") for o in session.omissions)


def test_credential_bearing_unknown_field_is_named_not_transferred(tmp_path):
    """L3: unmapped source fields (incl. credentials) are never carried.

    The contract's layer-1 guarantee is "never transferred, recorded by
    name only" — a nested credential value must not appear anywhere in the
    normalized session.
    """
    root = _write_export(
        tmp_path,
        [
            _turn(1),
            {
                "type": "turn",
                "id": "cx-002",
                "seq": 2,
                "role": "user",
                "text": "ok",
                "credentials": {"api_key": "abcd1234efgh"},
            },
        ],
    )
    session = read_codex_export(root, consent=CONSENT)
    serialized = json.dumps({"e": session.entries, "o": session.omissions})
    assert "abcd1234efgh" not in serialized
    named = [o for o in session.omissions if o["field"] == "credentials"]
    assert named and named[0]["recoverable"] is True
