"""Tests for #40 — Hydration Profiles.

Covers acceptance criteria:
  1. Each profile returns a stable documented shape.
  2. Default hydrate behavior is unchanged.
  3. audit includes source URI, byte ranges, checksum state, warnings.
  4. agent and compact avoid unnecessary file reads.
  5. Missing backing files surface as provenance errors or warnings.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.memory import FileRef, add_file_backed, hydrate_memory
from hotmem.profiles import (
    AGENT_MAX_CONTENT,
    hydrate_with_profile,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def inline_memory(tmp_db: MemoryDB) -> str:
    """An inline memory with known text."""
    text = "Vendor acme has duplicate invoice risk"
    blob = pack_embedding(embed_text(text))
    tmp_db.insert(
        id="inline1",
        identifier="vendor_x",
        fact_text=text,
        embedding=blob,
        content_hash=hashlib.sha256(b"vendor_x:" + text.encode()).hexdigest(),
    )
    return "inline1"


@pytest.fixture
def file_backed_memory(tmp_db: MemoryDB, fixture_file: Path) -> str:
    """A file-backed memory with a checksum."""
    expected = hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest()
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=expected,
    )
    mid, _ = add_file_backed(tmp_db, identifier="dataset", file_ref=ref, summary="a slice")
    return mid


# ── 1. Each profile returns a stable shape ────────────────────────────────────


def test_compact_profile_shape(tmp_db: MemoryDB, inline_memory: str):
    result = hydrate_with_profile(tmp_db, inline_memory, profile="compact")
    assert result.profile == "compact"
    assert result.memory_id == inline_memory
    assert result.identifier == "vendor_x"
    assert result.memory_type != "file"
    assert result.content is None  # compact: no content
    assert result.verified is False


def test_agent_profile_shape(tmp_db: MemoryDB, inline_memory: str):
    result = hydrate_with_profile(tmp_db, inline_memory, profile="agent")
    assert result.profile == "agent"
    assert result.content is not None  # agent: has content
    assert b"duplicate invoice" in result.content
    assert result.fact_text is not None


def test_audit_profile_shape(tmp_db: MemoryDB, inline_memory: str):
    result = hydrate_with_profile(tmp_db, inline_memory, profile="audit")
    assert result.profile == "audit"
    assert result.content is not None
    assert b"Vendor acme" in result.content


def test_full_profile_shape(tmp_db: MemoryDB, inline_memory: str):
    result = hydrate_with_profile(tmp_db, inline_memory, profile="full")
    assert result.profile == "full"
    assert result.content is not None
    assert result.fact_text is not None


# ── 2. Default hydrate behavior unchanged ─────────────────────────────────────


def test_default_hydrate_returns_bytes(tmp_db: MemoryDB, inline_memory: str):
    """hydrate_memory() (no profile) still returns raw bytes."""
    data = hydrate_memory(tmp_db, inline_memory)
    assert isinstance(data, bytes)
    assert b"Vendor acme" in data


# ── 3. Audit includes provenance for file-backed ──────────────────────────────


def test_audit_file_backed_includes_provenance(
    tmp_db: MemoryDB, file_backed_memory: str, fixture_file: Path
):
    result = hydrate_with_profile(tmp_db, file_backed_memory, profile="audit")
    assert result.profile == "audit"
    assert result.content is not None
    assert result.content == fixture_file.read_bytes()[10:30]
    assert result.verified is True
    assert result.source_uri == str(fixture_file)
    assert result.byte_offset == 10
    assert result.byte_length == 20
    assert result.source_checksum is not None


# ── 4. agent and compact avoid file reads ─────────────────────────────────────


def test_compact_no_file_reads(tmp_db: MemoryDB, file_backed_memory: str):
    """compact profile must not read the backing file."""
    from spy import SpyAdapter

    from hotmem.storage.local import LocalFilesystemAdapter

    spy = SpyAdapter(LocalFilesystemAdapter())
    import hotmem.profiles as profiles_mod

    orig = profiles_mod.get_adapter
    profiles_mod.get_adapter = lambda uri: spy

    try:
        reads_before = spy.total_file_reads
        result = hydrate_with_profile(tmp_db, file_backed_memory, profile="compact")
        reads_after = spy.total_file_reads
        assert reads_after == reads_before, "compact must not read the backing file"
        assert result.content is None  # no content
        assert result.exists is True  # stat-only check is OK
    finally:
        profiles_mod.get_adapter = orig


def test_agent_no_file_reads(tmp_db: MemoryDB, file_backed_memory: str):
    """agent profile must not read the backing file (uses summary only)."""
    from spy import SpyAdapter

    from hotmem.storage.local import LocalFilesystemAdapter

    spy = SpyAdapter(LocalFilesystemAdapter())
    import hotmem.profiles as profiles_mod

    orig = profiles_mod.get_adapter
    profiles_mod.get_adapter = lambda uri: spy

    try:
        reads_before = spy.total_file_reads
        result = hydrate_with_profile(tmp_db, file_backed_memory, profile="agent")
        reads_after = spy.total_file_reads
        assert reads_after == reads_before, "agent must not read the backing file"
        # agent uses fact_summary, not the file
        assert result.content is not None
        assert b"a slice" in result.content
    finally:
        profiles_mod.get_adapter = orig


def test_agent_truncates_large_inline(tmp_db: MemoryDB):
    """agent profile truncates inline content over AGENT_MAX_CONTENT."""
    long_text = "x" * (AGENT_MAX_CONTENT + 1000)
    blob = pack_embedding(embed_text(long_text))
    tmp_db.insert(id="long1", identifier="z", fact_text=long_text, embedding=blob)
    result = hydrate_with_profile(tmp_db, "long1", profile="agent")
    assert len(result.content) <= AGENT_MAX_CONTENT + 10  # truncated + ellipsis


# ── 5. Missing backing files ──────────────────────────────────────────────────


def test_audit_missing_file_warns(tmp_db: MemoryDB, file_backed_memory: str, fixture_file: Path):
    """audit profile surfaces missing backing file as a warning (not crash)."""
    fixture_file.unlink()
    result = hydrate_with_profile(tmp_db, file_backed_memory, profile="audit")
    assert result.content is None  # couldn't read
    assert result.exists is False
    assert any("missing" in w.lower() for w in result.warnings)


def test_compact_missing_file_warns(tmp_db: MemoryDB, file_backed_memory: str, fixture_file: Path):
    """compact profile checks existence (stat) and warns if missing."""
    fixture_file.unlink()
    result = hydrate_with_profile(tmp_db, file_backed_memory, profile="compact")
    assert result.exists is False
    assert any("missing" in w.lower() for w in result.warnings)


def test_audit_checksum_mismatch_warns(
    tmp_db: MemoryDB, file_backed_memory: str, fixture_file: Path
):
    """audit profile surfaces checksum mismatch as a warning (not crash)."""
    fixture_file.write_bytes(b"X" * 1024)  # corrupt the file
    result = hydrate_with_profile(tmp_db, file_backed_memory, profile="audit")
    assert result.verified is False
    assert any("provenance" in w.lower() for w in result.warnings)


# ── to_dict serialization ─────────────────────────────────────────────────────


def test_profiled_hydration_to_dict(tmp_db: MemoryDB, inline_memory: str):
    result = hydrate_with_profile(tmp_db, inline_memory, profile="audit")
    d = result.to_dict()
    assert d["memory_id"] == inline_memory
    assert d["profile"] == "audit"
    assert "content" in d
    assert d["content_encoding"] == "base64"
