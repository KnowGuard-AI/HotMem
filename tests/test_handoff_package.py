"""Tests for #101 (commit 6) — hotmem-handoff-v1 package writer.

Covers:
    - Happy path from the fixture: all four files, a complete manifest
      (identity, mode, counts, checksums, consent, coverage, limits) whose
      per-file checksums match a fresh read (acceptance 6).
    - Determinism: payloads are byte-stable across re-prepares; manifests
      differ only in handoff_id/created_at; package_id is content-derived
      and stable (acceptance 4, 6).
    - Mode semantics: resume bounds the turn stream with omission records;
      archive keeps the full ordered stream — demonstrably different
      packages (acceptance 10).
    - Fail-closed: invalid mode or missing consent writes nothing; a
      publish failure leaves no partial directory and no destroyed
      previous package.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hotmem.handoff import (
    BRIEF_NAME,
    MANIFEST_NAME,
    MEMORIES_NAME,
    SESSION_STREAM_NAME,
)
from hotmem.handoff.codex_source import ConsentError
from hotmem.handoff.package import prepare_handoff
from hotmem.interchange.canonical import sha256_file

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."
SECRET_VALUE = "hotmem_sk_9f3d2c8b7a6e5f4d"


def _prepare(out: Path, mode: str = "resume", **kwargs) -> object:
    return prepare_handoff(FIXTURE, out, mode=mode, consent=CONSENT, **kwargs)


def _load_manifest(pkg: Path) -> dict:
    return json.loads((pkg / MANIFEST_NAME).read_text())


# ── Happy path and manifest completeness (AC 6) ─────────────────────────────


def test_resume_package_layout_and_manifest(tmp_path):
    result = _prepare(tmp_path / "pkg")
    pkg = Path(result.path)

    for name in (MANIFEST_NAME, SESSION_STREAM_NAME, MEMORIES_NAME, BRIEF_NAME):
        assert (pkg / name).is_file(), f"missing {name}"

    manifest = _load_manifest(pkg)
    assert manifest["format"] == "hotmem-handoff-v1"
    assert manifest["schema_version"] == 1
    assert manifest["mode"] == "resume"
    assert manifest["package_id"] == result.package_id
    assert manifest["handoff_id"] == result.handoff_id
    assert manifest["source"]["session_id"] == "sess-7f3a2b"
    assert manifest["source"]["selection_scope"]["scope"] == "session:sess-7f3a2b"
    assert manifest["target"] == {"adapter": "hotmem"}
    assert manifest["compatibility"]["interchange_schema"] == 1
    assert manifest["consent"]["given"] is True
    assert manifest["consent"]["statement"] == CONSENT
    assert manifest["limits"]["brief_char_budget"] == 6000
    assert manifest["hotmem_version"]

    # Counts match the actual payload lines.
    session_lines = [
        line for line in (pkg / SESSION_STREAM_NAME).read_text().splitlines() if line.strip()
    ]
    memory_lines = [line for line in (pkg / MEMORIES_NAME).read_text().splitlines() if line.strip()]
    assert manifest["counts"]["entries"] == len(session_lines) == 11  # 12 - 1 bounded turn
    assert manifest["counts"]["memories"] == len(memory_lines) == 2
    assert manifest["counts"]["entries_by_kind"]["turn"] == 4

    # Every listed file checksum matches a fresh read (self-describing).
    for name, entry in manifest["files"].items():
        assert entry["sha256"] == sha256_file(pkg / name)
        assert entry["size"] == (pkg / name).stat().st_size


def test_coverage_report_is_complete_and_leak_free(tmp_path):
    result = _prepare(tmp_path / "pkg")
    coverage = result.coverage
    assert coverage["transferred"] == 11
    assert coverage["omitted_count"] == 7  # 6 source omissions + 1 resume-bounded turn
    assert coverage["redacted_count"] == 1
    assert any("resume mode bounds" in o["reason"] for o in coverage["omitted"])
    assert SECRET_VALUE not in json.dumps(result.manifest)
    assert SECRET_VALUE not in (Path(result.path) / BRIEF_NAME).read_text()


def test_brief_payload_is_linked_and_bounded(tmp_path):
    result = _prepare(tmp_path / "pkg")
    brief = json.loads((Path(result.path) / BRIEF_NAME).read_text())
    assert brief["char_count"] == len(brief["text"])
    assert brief["text"].startswith("# Resume brief — hotmem-showcase-planning")
    assert len(brief["links"]["memory_ids"]) == 2
    assert result.manifest["counts"]["brief_chars"] == brief["char_count"]


# ── Determinism (AC 4, 6) ────────────────────────────────────────────────────


def test_reprepare_is_byte_stable_with_stable_package_id(tmp_path):
    a = _prepare(tmp_path / "a")
    b = _prepare(tmp_path / "b")

    assert a.package_id == b.package_id
    assert a.handoff_id != b.handoff_id  # per-prepare identity differs

    for name in (SESSION_STREAM_NAME, MEMORIES_NAME, BRIEF_NAME):
        assert (Path(a.path) / name).read_bytes() == (Path(b.path) / name).read_bytes()

    ma, mb = _load_manifest(Path(a.path)), _load_manifest(Path(b.path))
    for volatile in ("handoff_id", "created_at"):
        ma.pop(volatile), mb.pop(volatile)
    ma["consent"].pop("at"), mb["consent"].pop("at")
    assert ma == mb


# ── Mode semantics (AC 10) ───────────────────────────────────────────────────


def test_archive_keeps_full_stream_resume_bounds_turns(tmp_path):
    resume = _prepare(tmp_path / "resume", mode="resume")
    archive = _prepare(tmp_path / "archive", mode="archive")

    resume_lines = (Path(resume.path) / SESSION_STREAM_NAME).read_text().splitlines()
    archive_lines = (Path(archive.path) / SESSION_STREAM_NAME).read_text().splitlines()
    assert len(resume_lines) == 11
    assert len(archive_lines) == 12
    assert "turn" in {json.loads(line)["kind"] for line in archive_lines}

    # The resume package omits exactly the middle turn(s) — visibly.
    dropped = [o for o in resume.coverage["omitted"] if "resume mode bounds" in o["reason"]]
    assert [o["where"] for o in dropped] == ["cx-010"]
    assert all(o["recoverable"] for o in dropped)

    # Modes differ in content and coverage, not just the label.
    assert resume.package_id != archive.package_id


# ── Fail-closed behavior ────────────────────────────────────────────────────


def test_invalid_mode_writes_nothing(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        prepare_handoff(FIXTURE, tmp_path / "pkg", mode="vibe", consent=CONSENT)
    assert not (tmp_path / "pkg").exists()
    assert not list(tmp_path.iterdir())


def test_missing_consent_writes_nothing(tmp_path):
    with pytest.raises(ConsentError):
        prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent="  ")
    assert not list(tmp_path.iterdir())


def test_publish_failure_leaves_no_partial_and_keeps_previous(tmp_path, monkeypatch):
    first = _prepare(tmp_path / "pkg")
    original = first.package_id

    import hotmem.handoff.package as package_module

    def boom(staging, final):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(package_module, "atomic_publish", boom)
    with pytest.raises(OSError, match="simulated publish failure"):
        _prepare(tmp_path / "pkg")
    # The previous package is intact and no staging leftovers remain.
    assert _load_manifest(tmp_path / "pkg")["package_id"] == original
    assert [p.name for p in tmp_path.iterdir()] == ["pkg"]


def test_consent_statement_is_truncated_to_limit(tmp_path):
    long_consent = "x" * 10_000
    result = prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent=long_consent)
    assert result.manifest["consent"]["statement"] == "x" * 512
