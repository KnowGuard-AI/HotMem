"""Tests for #101 (commit 10) — handoff MCP tools.

Covers:
    - The four tools dispatch through the same core functions as the CLI.
    - Consent is a required argument: an MCP host can never capture a
      session implicitly (acceptance criterion 3).
    - handoff_hydrate targets the server's configured database and reports
      already_applied on repeats (acceptance criterion 8).
    - Invalid packages surface structured MCP error results, not crashes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="requires the optional [mcp] extra")

from hotmem.db import MemoryDB  # noqa: E402
from hotmem.mcp_server import (  # noqa: E402
    _handle_handoff_hydrate,
    _handle_handoff_inspect,
    _handle_handoff_prepare,
    _handle_handoff_verify,
    _ServerState,
)

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."


def _text(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.fixture
def state(tmp_path: Path) -> _ServerState:
    db = MemoryDB(tmp_path / "mcp.sqlite")
    st = _ServerState()
    st.db = db
    st.db_path = str(tmp_path / "mcp.sqlite")
    st.swap_path = None
    st.start_time = time.time()
    yield st
    db.close()


def test_handoff_prepare_requires_consent(state, tmp_path):
    with pytest.raises(KeyError):
        _handle_handoff_prepare(state, {"source": str(FIXTURE), "output": str(tmp_path / "p")})
    result = _handle_handoff_prepare(
        state,
        {"source": str(FIXTURE), "output": str(tmp_path / "p"), "consent": "   "},
    )
    payload = _text(result)
    assert result.isError
    assert "consent" in payload["error"]


def test_handoff_prepare_emits_identity_and_coverage(state, tmp_path):
    result = _handle_handoff_prepare(
        state,
        {
            "source": str(FIXTURE),
            "output": str(tmp_path / "pkg"),
            "mode": "resume",
            "consent": CONSENT,
            "session": "sess-7f3a2b",
        },
    )
    payload = _text(result)
    assert not result.isError
    assert payload["entries"] == 11
    assert payload["memories"] == 2
    assert payload["omissions"] == 7
    assert payload["redactions"] == 1
    assert len(payload["handoff_id"]) == 32
    assert payload["total_ms"] >= 0


def test_handoff_inspect_and_verify_round_trip(state, tmp_path):
    _handle_handoff_prepare(
        state, {"source": str(FIXTURE), "output": str(tmp_path / "pkg"), "consent": CONSENT}
    )
    verify = _text(_handle_handoff_verify(state, {"package": str(tmp_path / "pkg")}))
    assert verify["valid"] is True
    assert verify["entries"] == 11

    report = _text(_handle_handoff_inspect(state, {"package": str(tmp_path / "pkg")}))
    assert report["valid"] is True
    assert report["coverage"]["redacted_count"] == 1


def test_handoff_verify_invalid_package_is_structured_error(state, tmp_path):
    _handle_handoff_prepare(
        state, {"source": str(FIXTURE), "output": str(tmp_path / "pkg"), "consent": CONSENT}
    )
    (tmp_path / "pkg" / "session.jsonl").write_text("gone\n")
    result = _handle_handoff_verify(state, {"package": str(tmp_path / "pkg")})
    assert result.isError
    assert "size_mismatch" in _text(result)["error"]

    report = _text(_handle_handoff_inspect(state, {"package": str(tmp_path / "pkg")}))
    assert report["valid"] is False  # inspect still reports, verify errors


def test_handoff_hydrate_uses_server_db_and_is_idempotent(state, tmp_path):
    _handle_handoff_prepare(
        state, {"source": str(FIXTURE), "output": str(tmp_path / "pkg"), "consent": CONSENT}
    )
    first = _text(_handle_handoff_hydrate(state, {"package": str(tmp_path / "pkg")}))
    assert first["loaded"] == 3
    assert first["already_applied"] is False
    assert first["brief"] == "handoff/hotmem-showcase-planning/resume-brief"
    assert state.db.count() == 3  # hydrated into the SERVER's database

    second = _text(_handle_handoff_hydrate(state, {"package": str(tmp_path / "pkg")}))
    assert second["already_applied"] is True
    assert second["loaded"] == 0
    assert state.db.count() == 3


def test_handoff_hydrate_invalid_package_is_structured_error(state, tmp_path):
    result = _handle_handoff_hydrate(state, {"package": str(tmp_path / "nope")})
    assert result.isError
    assert "verification failed" in _text(result)["error"]
    assert state.db.count() == 0
