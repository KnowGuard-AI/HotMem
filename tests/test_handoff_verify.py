"""Tests for #101 (commit 7) — fail-closed verification and inspect.

Covers (acceptance criterion 13's verification subset):
    - Valid fixture packages verify; inspect reports the full criterion-11
      field list.
    - Malformed manifest, wrong format, unsupported schema, unknown mode.
    - Path traversal in the files block; missing files; size and digest
      mismatches (a flipped payload byte).
    - Record-count mismatches, duplicate entry ids, unknown kinds,
      disordered seq, invalid interchange memory records.
    - Coverage arithmetic and content-derived package_id recomputation
      (payload tampering fails closed).
    - The secret gate rejects packages carrying unredacted secrets.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hotmem.handoff.package import prepare_handoff
from hotmem.handoff.verify import HandoffError, inspect_handoff, verify_handoff

FIXTURE = Path(__file__).parent / "fixtures" / "codex_export"
CONSENT = "I consent to capturing this session for the handoff showcase."


@pytest.fixture()
def pkg(tmp_path: Path) -> Path:
    prepare_handoff(FIXTURE, tmp_path / "pkg", mode="resume", consent=CONSENT)
    return tmp_path / "pkg"


def _edit_manifest(pkg: Path, mutate) -> None:
    manifest = json.loads((pkg / "manifest.json").read_text())
    mutate(manifest)
    (pkg / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))


def _rewrite_payload(pkg: Path, name: str, text: str) -> None:
    """Tamper with a payload AND refresh its manifest checksum entry.

    Structural checks only run after checksums pass, so tampering tests
    must keep the files block honest to reach the check under test.
    """
    (pkg / name).write_text(text)
    import hashlib

    digest = hashlib.sha256(text.encode()).hexdigest()
    _edit_manifest(
        pkg,
        lambda m: m["files"][name].update(size=len(text.encode()), sha256=digest),
    )


def _expect_error(pkg: Path, reason: str) -> None:
    with pytest.raises(HandoffError) as exc_info:
        verify_handoff(pkg)
    assert exc_info.value.reason == reason


# ── Valid packages ───────────────────────────────────────────────────────────


def test_valid_package_verifies(pkg):
    verified = verify_handoff(pkg)
    assert verified.entry_count == 11
    assert verified.memory_count == 2
    assert len(verified.stream_lines()) == 11
    assert len(verified.memory_lines()) == 2
    brief = verified.brief()
    assert brief["text"].startswith("# Resume brief")
    assert verified.manifest["mode"] == "resume"


def test_inspect_reports_full_field_list(pkg):
    report = inspect_handoff(pkg)
    assert report["valid"] is True
    assert report["failure"] is None
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
    ):
        assert key in report, f"inspect missing {key}"
    assert report["coverage"]["omitted_count"] == 7
    assert report["coverage"]["redacted_count"] == 1


def test_archive_package_verifies(tmp_path):
    prepare_handoff(FIXTURE, tmp_path / "pkg", mode="archive", consent=CONSENT)
    verified = verify_handoff(tmp_path / "pkg")
    assert verified.entry_count == 12


# ── Manifest failures ───────────────────────────────────────────────────────


def test_missing_manifest_fails(tmp_path):
    _expect_error(tmp_path, "missing_manifest")


def test_malformed_manifest_fails(pkg):
    (pkg / "manifest.json").write_text("{not json")
    _expect_error(pkg, "malformed_manifest")


def test_wrong_format_fails(pkg):
    _edit_manifest(pkg, lambda m: m.update(format="hotmem-other-v1"))
    _expect_error(pkg, "unsupported_format")


def test_unsupported_schema_fails(pkg):
    _edit_manifest(pkg, lambda m: m.update(schema_version=99))
    _expect_error(pkg, "unsupported_schema")


def test_unknown_mode_fails(pkg):
    _edit_manifest(pkg, lambda m: m.update(mode="vibe"))
    _expect_error(pkg, "unknown_mode")


# ── Path confinement and file integrity ─────────────────────────────────────


def test_path_traversal_in_files_block_fails(pkg):
    def mutate(manifest):
        manifest["files"]["../escape.txt"] = {"size": 1, "sha256": "0" * 64}

    _edit_manifest(pkg, mutate)
    _expect_error(pkg, "path_escape")


def test_symlink_escape_fails(pkg, tmp_path):
    outside = pkg.parent / "outside.jsonl"
    outside.write_text("{}")
    (pkg / "link.jsonl").symlink_to(outside)

    def mutate(manifest):
        manifest["files"]["link.jsonl"] = {"size": 2, "sha256": "0" * 64}

    _edit_manifest(pkg, mutate)
    _expect_error(pkg, "path_escape")


def test_missing_listed_file_fails(pkg):
    (pkg / "memories.jsonl").unlink()
    _expect_error(pkg, "missing_file")


def test_size_mismatch_fails(pkg):
    _edit_manifest(pkg, lambda m: m["files"]["memories.jsonl"].update(size=999999))
    _expect_error(pkg, "size_mismatch")


def test_digest_mismatch_on_flipped_byte_fails(pkg):
    path = pkg / "session.jsonl"
    data = bytearray(path.read_bytes())
    data[10] ^= 0x01
    path.write_bytes(bytes(data))
    _expect_error(pkg, "digest_mismatch")


def test_missing_brief_in_resume_mode_fails(pkg):
    (pkg / "resume-brief.json").unlink()
    _edit_manifest(pkg, lambda m: m["files"].pop("resume-brief.json"))
    _expect_error(pkg, "missing_file")


# ── Payload structure ───────────────────────────────────────────────────────


def test_entry_count_mismatch_fails(pkg):
    _edit_manifest(pkg, lambda m: m["counts"].update(entries=99))
    _expect_error(pkg, "record_count_mismatch")


def test_duplicate_entry_id_fails(pkg):
    lines = [line for line in (pkg / "session.jsonl").read_text().splitlines() if line.strip()]
    duplicated = lines + [lines[-1]]
    _rewrite_payload(pkg, "session.jsonl", "\n".join(duplicated) + "\n")
    _edit_manifest(pkg, lambda m: m["counts"].update(entries=len(duplicated)))
    _expect_error(pkg, "duplicate_entry_id")


def test_unknown_entry_kind_fails(pkg):
    lines = [line for line in (pkg / "session.jsonl").read_text().splitlines() if line.strip()]
    entry = json.loads(lines[0])
    entry["kind"] = "vibe_check"
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    _rewrite_payload(pkg, "session.jsonl", "\n".join(lines) + "\n")
    _expect_error(pkg, "unknown_entry_kind")


def test_disordered_seq_fails(pkg):
    lines = [line for line in (pkg / "session.jsonl").read_text().splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]
    entries.reverse()
    _rewrite_payload(
        pkg,
        "session.jsonl",
        "".join(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n" for e in entries),
    )
    _expect_error(pkg, "disordered_seq")


def test_invalid_memory_record_fails(pkg):
    lines = [line for line in (pkg / "memories.jsonl").read_text().splitlines() if line.strip()]
    record = json.loads(lines[0])
    record["identifier"] = ""  # interchange records require an identifier
    record["content_hash"] = "x"  # and a 64-hex content hash
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    _rewrite_payload(pkg, "memories.jsonl", "\n".join(lines) + "\n")
    _expect_error(pkg, "invalid_memory_record")


# ── Coverage arithmetic and identity ─────────────────────────────────────────


def test_coverage_mismatch_fails(pkg):
    _edit_manifest(pkg, lambda m: m["coverage"].update(transferred=999))
    _expect_error(pkg, "coverage_mismatch")


def test_tampered_payload_changes_package_id_and_fails(pkg):
    lines = [line for line in (pkg / "session.jsonl").read_text().splitlines() if line.strip()]
    entry = json.loads(lines[0])
    entry["text"] = entry["text"] + " tampered"
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    # Checksums are refreshed so only the content-derived identity check
    # can catch the tampering.
    _rewrite_payload(pkg, "session.jsonl", "\n".join(lines) + "\n")
    _expect_error(pkg, "package_id_mismatch")


# ── Secret gate ─────────────────────────────────────────────────────────────


def test_secret_in_payload_fails_closed(pkg):
    lines = [line for line in (pkg / "session.jsonl").read_text().splitlines() if line.strip()]
    entry = json.loads(lines[0])
    entry["text"] = "leaked hotmem_sk_deadbeef1234 in package"
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    # Refresh checksums AND the content-derived identity so the only
    # remaining invariant that can fail is the secret gate.
    _rewrite_payload(pkg, "session.jsonl", "\n".join(lines) + "\n")
    entries = [json.loads(line) for line in lines]
    memories = [
        json.loads(line)
        for line in (pkg / "memories.jsonl").read_text().splitlines()
        if line.strip()
    ]
    from hotmem.handoff import entry_content_hash, package_id_for

    refreshed_id = package_id_for(
        [entry_content_hash(e) for e in entries]
        + [str(m.get("content_hash") or "") for m in memories]
    )
    _edit_manifest(pkg, lambda m: m.update(package_id=refreshed_id))
    report = inspect_handoff(pkg)
    assert report["valid"] is False
    assert report["failure"]["reason"] == "secret_content"


def test_inspect_on_invalid_package_reports_failure_not_exception(tmp_path):
    report = inspect_handoff(tmp_path)
    assert report["valid"] is False
    assert report["failure"]["reason"] == "missing_manifest"


def test_inspect_copies_package_does_not_mutate(pkg):
    before = {p.name: p.read_bytes() for p in pkg.iterdir()}
    inspect_handoff(pkg)
    verify_handoff(pkg)
    after = {p.name: p.read_bytes() for p in pkg.iterdir()}
    assert before == after
    assert shutil.rmtree(pkg) is None  # cleanup helper, also proves plain dir


# ── Type-confusion matrix (M2: fail closed, never crash) ────────────────────

# Structurally wrong manifests must produce structured HandoffError reasons —
# not AttributeError/ValueError/TypeError escaping to a 500, a crashing
# inspect, an MCP dispatcher exception, or a CLI traceback.
MALFORMED_MANIFEST_CASES: dict[str, object] = {
    "schema-version-string": lambda m: m.update(schema_version="one"),
    "schema-version-missing": lambda m: m.pop("schema_version"),
    "schema-version-null": lambda m: m.update(schema_version=None),
    "files-not-object": lambda m: m.update(files=[]),
    "files-value-not-object": lambda m: m["files"].update({"session.jsonl": "oops"}),
    "files-size-string": lambda m: m["files"]["session.jsonl"].update(size="big"),
    "files-size-bool": lambda m: m["files"]["session.jsonl"].update(size=True),
    "files-sha-not-hex": lambda m: m["files"]["session.jsonl"].update(sha256="zz" * 32),
    "files-sha-not-string": lambda m: m["files"]["session.jsonl"].update(sha256=123),
    "counts-not-object": lambda m: m.update(counts=["nope"]),
    "counts-entries-string": lambda m: m["counts"].update(entries="many"),
    "counts-entries-null": lambda m: m["counts"].update(entries=None),
    "counts-by-kind-not-object": lambda m: m["counts"].update(entries_by_kind=["turn"]),
    "counts-by-kind-value-string": lambda m: m["counts"].update(entries_by_kind={"turn": "one"}),
    "coverage-not-object": lambda m: m.update(coverage="nope"),
    "coverage-omitted-not-array": lambda m: m["coverage"].update(omitted="nope"),
    "coverage-transferred-string": lambda m: m["coverage"].update(transferred="11"),
    "source-not-object": lambda m: m.update(source=["nope"]),
    "source-adapter-not-string": lambda m: m["source"].update(adapter=123),
    "target-not-object": lambda m: m.update(target="hotmem"),
    "limits-not-object": lambda m: m.update(limits="x"),
    "consent-not-object": lambda m: m.update(consent=[]),
    "package-id-not-string": lambda m: m.update(package_id=123),
    "handoff-id-null": lambda m: m.update(handoff_id=None),
}


@pytest.mark.parametrize("case", list(MALFORMED_MANIFEST_CASES))
def test_type_confused_manifest_fails_closed(pkg, case):
    mutate = MALFORMED_MANIFEST_CASES[case]
    _edit_manifest(pkg, mutate)

    with pytest.raises(HandoffError) as exc_info:
        verify_handoff(pkg)
    assert exc_info.value.reason, f"{case}: empty reason"

    report = inspect_handoff(pkg)
    assert report["valid"] is False, f"{case}: inspect claimed validity"
    assert report["failure"]["reason"] == exc_info.value.reason


@pytest.mark.parametrize(
    "case",
    ["source-not-object", "counts-not-object", "coverage-not-object", "files-value-not-object"],
)
def test_inspect_never_raises_on_type_confusion(pkg, case):
    """inspect promises a report for invalid packages — including these."""
    _edit_manifest(pkg, MALFORMED_MANIFEST_CASES[case])
    report = inspect_handoff(pkg)  # must not raise
    assert report["valid"] is False
    assert report["failure"]["reason"]
    assert report["path"].endswith("pkg")


def test_type_confused_manifest_never_writes_target(pkg, tmp_path):
    """Fail-closed means the target is untouched, including on shape errors."""
    from hotmem.db import MemoryDB
    from hotmem.handoff.hydrate import hydrate_handoff

    _edit_manifest(pkg, MALFORMED_MANIFEST_CASES["counts-not-object"])
    db = MemoryDB(str(tmp_path / "t.sqlite"))
    try:
        with pytest.raises(HandoffError):
            hydrate_handoff(db, str(pkg))
        assert db.count() == 0
        assert db.get_handoff("anything") is None
    finally:
        db.close()


# ── Stable derived entry ids (L1) ───────────────────────────────────────────


def test_valid_package_entry_ids_are_derived(pkg):
    """Positive control: shipped ids equal entry_id_for(source identity)."""
    from hotmem.handoff import entry_id_for

    verified = verify_handoff(pkg)
    entries = [json.loads(line) for line in verified.stream_lines()]
    for entry in entries:
        block = entry["source"]
        assert entry["id"] == entry_id_for(
            block["adapter"], block["session_id"], block["source_entry_id"]
        )


def test_non_derived_entry_id_fails_verification(pkg):
    """A 64-hex id that is NOT derived from the source identity must fail.

    Checksums and the content-derived package_id are refreshed so only the
    id-derivation rule can catch it (handoff-v1 §1/§4 promise stable ids).
    """
    from hotmem.handoff import entry_content_hash, package_id_for

    lines = [json.loads(line) for line in (pkg / "session.jsonl").read_text().splitlines()]
    lines[0]["id"] = "f" * 64  # arbitrary, unrelated to source identity
    _rewrite_payload(
        pkg,
        "session.jsonl",
        "".join(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n" for e in lines),
    )
    memories = [
        json.loads(line)
        for line in (pkg / "memories.jsonl").read_text().splitlines()
        if line.strip()
    ]
    refreshed = package_id_for(
        [entry_content_hash(e) for e in lines]
        + [str(r.get("content_hash") or "") for r in memories]
    )
    _edit_manifest(pkg, lambda m: m.update(package_id=refreshed))

    _expect_error(pkg, "entry_id_not_derived")


def test_entry_with_missing_source_entry_id_fails(pkg):
    lines = [json.loads(line) for line in (pkg / "session.jsonl").read_text().splitlines()]
    lines[0]["source"].pop("source_entry_id")
    _rewrite_payload(
        pkg,
        "session.jsonl",
        "".join(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n" for e in lines),
    )
    _expect_error(pkg, "invalid_entry")
