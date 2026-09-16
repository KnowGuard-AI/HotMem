"""Tests for #101 (commit 4) — deny-by-default redaction and coverage.

Covers:
    - Every pattern kind replaces the value with [REDACTED:<kind>].
    - The fixture's secret never survives into entries, memory records, or
      redaction records (acceptance criterion 5).
    - The layer-3 output gate fails closed on leaks, with a message that
      names the kind but never the value — including on its own
      placeholders (gate stability).
    - Normal text produces no false positives.
    - Coverage assembly counts omissions, redactions, and recoverability.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.handoff.codex_source import read_codex_export
from hotmem.handoff.redact import (
    DEFAULT_ENGINE,
    PLACEHOLDER,
    RedactionLeakError,
    assert_no_secrets,
    redact_normalized,
)
from hotmem.handoff.report import build_coverage, omission

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."
SECRET_VALUE = "hotmem_sk_9f3d2c8b7a6e5f4d"


def _session():
    return read_codex_export(FIXTURE, consent=CONSENT)


# ── Pattern kinds ────────────────────────────────────────────────────────────


def test_api_key_assignment_keeps_name_redacts_value():
    text = "For the demo I set HOTMEM_API_KEY=hotmem_sk_abc123def456ghi789 ok?"
    redacted, hits = DEFAULT_ENGINE.redact_text(text)
    assert redacted == f"For the demo I set HOTMEM_API_KEY={PLACEHOLDER.format(kind='api_key')} ok?"
    assert [h["kind"] for h in hits] == ["api_key"]
    assert "abc123def456ghi789" not in redacted


def test_bearer_token_redacts_credential_keeps_scheme():
    text = "Authorization: bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    redacted, hits = DEFAULT_ENGINE.redact_text(text)
    assert "eyJhbGci" not in redacted
    assert [h["kind"] for h in hits] == ["bearer_token"]
    assert redacted == "Authorization: bearer " + PLACEHOLDER.format(kind="bearer_token")


def test_private_key_block_redacted():
    text = "signing key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpA\n-----END RSA PRIVATE KEY-----\n"
    redacted, hits = DEFAULT_ENGINE.redact_text(text)
    assert "MIIEpA" not in redacted
    assert [h["kind"] for h in hits] == ["private_key"]


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2", "prefixed_credential"),
        ("sk-" + "a1B2c3D4e5F6g7H8", "prefixed_credential"),
        ("hotmem_sk_deadbeef1234", "prefixed_credential"),
        ("AKIAIOSFODNN7EXAMPLE", "prefixed_credential"),
    ],
)
def test_prefixed_credentials_redacted_in_neutral_context(value, kind):
    # Neutral context: no keyword assignment would claim the value first.
    redacted, hits = DEFAULT_ENGINE.redact_text(f"found {value} in the log")
    assert value not in redacted
    assert kind in [h["kind"] for h in hits]


def test_normal_text_has_no_false_positives():
    samples = (
        "Use the hotmem-handoff-v1 package with the sha256 checksum.",
        "The interchange record keeps identifier and fact_text fields.",
        "Connect via http://127.0.0.1:8711 and run uv run pytest -q.",
        '"reasoning_tokens": 947 stays supported as an omission record.',
        "Resume mode bounds the stream; archive keeps the full order.",
    )
    for sample in samples:
        redacted, hits = DEFAULT_ENGINE.redact_text(sample)
        assert redacted == sample
        assert hits == []


# ── Fixture session redaction (acceptance criterion 5) ─────────────────────


def test_fixture_secret_is_redacted_and_never_survives():
    session = redact_normalized(_session())
    serialized = json.dumps(
        {
            "entries": session.entries,
            "memory_records": session.memory_records,
            "redactions": session.redactions,
            "omissions": session.omissions,
        }
    )
    assert SECRET_VALUE not in serialized
    assert "hotmem_sk_9f3d2c8b7a6e5f4d" not in serialized

    # The secret-bearing turn is marked and its record names kind + location.
    secret_entry = next(e for e in session.entries if e["source"]["source_entry_id"] == "cx-010")
    assert secret_entry["redacted"] is True
    assert "HOTMEM_API_KEY=" + PLACEHOLDER.format(kind="api_key") in secret_entry["text"]
    assert [r["kind"] for r in session.redactions] == ["api_key"]
    record = session.redactions[0]
    assert record["where"] == "cx-010"
    assert record["field"] == "text"
    assert record["recoverable"] is True
    assert SECRET_VALUE not in json.dumps(record)


def test_redaction_is_deterministic_and_non_destructive_to_other_entries():
    a = redact_normalized(_session())
    b = redact_normalized(_session())
    assert a.entries == b.entries
    assert a.redactions == b.redactions
    untouched = next(e for e in a.entries if e["source"]["source_entry_id"] == "cx-001")
    assert "redacted" not in untouched


# ── Layer-3 output gate ─────────────────────────────────────────────────────


def test_output_gate_passes_on_redacted_text():
    session = redact_normalized(_session())
    for entry in session.entries:
        assert_no_secrets(entry["text"], where="entry text")


def test_output_gate_fails_closed_naming_kind_not_value():
    with pytest.raises(RedactionLeakError) as exc_info:
        assert_no_secrets(f"leaked {SECRET_VALUE} here", where="brief")
    message = str(exc_info.value)
    assert "prefixed_credential" in message
    assert SECRET_VALUE not in message


def test_gate_is_stable_on_its_own_placeholders():
    # The engine's placeholder must not re-trigger any pattern (gate
    # stability): redacting already-redacted text is a no-op.
    once, hits = DEFAULT_ENGINE.redact_text(f"HOTMEM_API_KEY={SECRET_VALUE}")
    assert hits
    twice, hits2 = DEFAULT_ENGINE.redact_text(once)
    assert twice == once
    assert hits2 == []
    assert_no_secrets(once, where="placeholder stability")


# ── Coverage assembly ───────────────────────────────────────────────────────


def test_build_coverage_counts():
    omissions = [
        omission("cx-002", "model", "unsupported source field", recoverable=True),
        omission("cx-014", None, "policy: hidden prompts are never captured", recoverable=False),
    ]
    redactions = [
        {
            "where": "cx-010",
            "field": "text",
            "kind": "api_key",
            "reason": "credential pattern",
            "recoverable": True,
        }
    ]
    coverage = build_coverage(transferred=12, omissions=omissions, redactions=redactions)
    assert coverage["transferred"] == 12
    assert coverage["omitted_count"] == 2
    assert coverage["redacted_count"] == 1
    # One omission is policy-denied (not recoverable); everything else is.
    assert coverage["recoverable_count"] == 2
    assert coverage["omitted"] == omissions
    assert coverage["redacted"] == redactions


def test_fixture_coverage_shape_end_to_end():
    session = redact_normalized(_session())
    coverage = build_coverage(
        transferred=len(session.entries),
        omissions=session.omissions,
        redactions=session.redactions,
    )
    assert coverage["transferred"] == 12
    assert coverage["omitted_count"] == 6  # 5 unsupported fields + 1 hidden prompt
    assert coverage["redacted_count"] == 1
    assert coverage["recoverable_count"] == 6  # hidden prompt is the only non-recoverable
    assert SECRET_VALUE not in json.dumps(coverage)
