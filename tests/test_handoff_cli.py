"""Tests for #101 (commit 9) — hotmem handoff CLI surface.

Covers:
    - The full showcase flow from the fixture: prepare -> verify -> inspect
      --json -> hydrate -> search proves the brief is retrievable (AC 1).
    - Consent is required at the CLI: absent consent fails with no package
      written (AC 3).
    - Corrupt packages verify/inspect to non-zero exit with structured
      reasons (AC 7, 11).
    - Repeat hydration reports already_applied (AC 8).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from hotmem.cli import main
from hotmem.db import MemoryDB
from hotmem.search import search_memories

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."

runner = CliRunner()


def _prepare(tmp_path: Path, *extra: str) -> object:
    result = runner.invoke(
        main,
        [
            "handoff",
            "prepare",
            "--source",
            str(FIXTURE),
            "--out",
            str(tmp_path / "pkg"),
            "--consent",
            CONSENT,
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    return result


def test_prepare_outputs_package(tmp_path):
    result = _prepare(tmp_path)
    assert "handoff-prepare" in result.output
    manifest = json.loads((tmp_path / "pkg" / "manifest.json").read_text())
    assert manifest["mode"] == "resume"
    assert manifest["consent"]["statement"] == CONSENT


def test_prepare_mode_archive(tmp_path):
    result = _prepare(tmp_path, "--mode", "archive")
    assert "handoff-prepare" in result.output
    manifest = json.loads((tmp_path / "pkg" / "manifest.json").read_text())
    assert manifest["mode"] == "archive"


def test_prepare_requires_consent_and_writes_nothing(tmp_path):
    result = runner.invoke(
        main,
        [
            "handoff",
            "prepare",
            "--source",
            str(FIXTURE),
            "--out",
            str(tmp_path / "pkg"),
        ],
    )
    assert result.exit_code != 0
    assert "consent" in result.output.lower()
    assert not (tmp_path / "pkg").exists()


def test_prepare_session_mismatch_fails(tmp_path):
    result = _prepare_failing(tmp_path, "--session", "sess-other")
    assert "sess-7f3a2b" in result.output


def _prepare_failing(tmp_path: Path, *extra: str):
    return runner.invoke(
        main,
        [
            "handoff",
            "prepare",
            "--source",
            str(FIXTURE),
            "--out",
            str(tmp_path / "pkg"),
            "--consent",
            CONSENT,
            *extra,
        ],
    )


def test_verify_then_inspect_json(tmp_path):
    _prepare(tmp_path)
    verify = runner.invoke(main, ["handoff", "verify", str(tmp_path / "pkg")])
    assert verify.exit_code == 0
    assert "valid" in verify.output

    inspect = runner.invoke(main, ["handoff", "inspect", str(tmp_path / "pkg"), "--json"])
    assert inspect.exit_code == 0
    report = json.loads(inspect.output)
    assert report["valid"] is True
    assert report["mode"] == "resume"
    assert report["coverage"]["omitted_count"] == 7
    assert report["counts"]["entries"] == 11


def test_verify_corrupt_package_exits_nonzero_with_reason(tmp_path):
    _prepare(tmp_path)
    manifest_path = tmp_path / "pkg" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest))

    verify = runner.invoke(main, ["handoff", "verify", str(tmp_path / "pkg")])
    assert verify.exit_code != 0
    assert "unsupported_schema" in verify.output

    # inspect --json is a report, not a gate: exit 0 with valid:false so
    # machine consumers parse the failure block (verify is the gate).
    inspect = runner.invoke(main, ["handoff", "inspect", str(tmp_path / "pkg"), "--json"])
    assert inspect.exit_code == 0
    report = json.loads(inspect.output)
    assert report["valid"] is False
    assert report["failure"]["reason"] == "unsupported_schema"


def test_hydrate_and_repeat_is_already_applied(tmp_path):
    _prepare(tmp_path)
    target = tmp_path / "target.sqlite"

    first = runner.invoke(main, ["handoff", "hydrate", str(tmp_path / "pkg"), "--db", str(target)])
    assert first.exit_code == 0, first.output
    assert "already_applied" in first.output

    second = runner.invoke(main, ["handoff", "hydrate", str(tmp_path / "pkg"), "--db", str(target)])
    assert second.exit_code == 0, second.output
    assert "True" in second.output  # already_applied rendered True

    db = MemoryDB(str(target))
    try:
        assert db.count() == 3
        results = search_memories(db, "resume brief handoff showcase", top_k=5)
        identifiers = [r.get("identifier") for r in results]
        assert "handoff/hotmem-showcase-planning/resume-brief" in identifiers
    finally:
        db.close()


def test_hydrate_invalid_package_exits_nonzero_without_touching_target(tmp_path):
    _prepare(tmp_path)
    target = tmp_path / "target.sqlite"
    (tmp_path / "pkg" / "session.jsonl").write_text("tampered\n")

    result = runner.invoke(main, ["handoff", "hydrate", str(tmp_path / "pkg"), "--db", str(target)])
    assert result.exit_code != 0
    assert "size_mismatch" in result.output  # size gate fires before digest
    assert not target.exists() or MemoryDB(str(target)).count() == 0


def test_type_confused_manifest_reports_cleanly_without_traceback(tmp_path):
    """M2: shape errors are diagnostics, not tracebacks or 500-style crashes."""
    _prepare(tmp_path)
    manifest_path = tmp_path / "pkg" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["counts"] = ["nope"]
    manifest_path.write_text(json.dumps(manifest))

    verify = runner.invoke(main, ["handoff", "verify", str(tmp_path / "pkg")])
    assert verify.exit_code != 0
    assert "invalid_manifest_field" in verify.output
    assert "Traceback" not in verify.output

    inspect = runner.invoke(main, ["handoff", "inspect", str(tmp_path / "pkg")])
    assert inspect.exit_code != 0
    assert "Traceback" not in inspect.output


def test_type_confused_manifest_hydrate_exits_nonzero_without_writes(tmp_path):
    _prepare(tmp_path)
    target = tmp_path / "target.sqlite"
    manifest_path = tmp_path / "pkg" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["coverage"] = "nope"
    manifest_path.write_text(json.dumps(manifest))

    result = runner.invoke(main, ["handoff", "hydrate", str(tmp_path / "pkg"), "--db", str(target)])
    assert result.exit_code != 0
    assert "invalid_manifest_field" in result.output
    assert not target.exists() or MemoryDB(str(target)).count() == 0
