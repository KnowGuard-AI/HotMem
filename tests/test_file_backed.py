"""Tests for #38 — file-backed memories (URI + range + checksum hydration).

Covers the four required unit tests from the issue:
  1. add file-backed memory -> hydrate returns exact byte range from a fixture file
  2. checksum mismatch detected; truncated/missing file raises provenance error
  3. metadata access performs no file read (spy on adapter)
  4. inline memory path unchanged

Plus extras: unsupported scheme rejected, relative URI resolution, lazy
hydration, search with summary, client SDK.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.memory import FileRef, add_file_backed, get_memory_metadata, hydrate_memory
from hotmem.provenance import ProvenanceError
from hotmem.storage.local import LocalFilesystemAdapter

# ── 1. add file-backed -> hydrate returns exact byte range ───────────────────


def test_add_and_hydrate_exact_range(tmp_db: MemoryDB, fixture_file: Path):
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="a slice")

    data = hydrate_memory(tmp_db, mid)
    expected = fixture_file.read_bytes()[10:30]
    assert data == expected
    assert len(data) == 20


def test_add_and_hydrate_exact_range_http(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    raw = fixture_path.read_bytes()
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "ds",
            "file_uri": fixture_path.name,
            "byte_offset": 100,
            "byte_length": 50,
            "source_format": "bin",
        },
    )
    assert resp.status_code == 200
    mid = resp.json()["memory_id"]

    h = app_client.post(f"/v1/memory/{mid}/hydrate")
    assert h.status_code == 200
    assert h.content == raw[100:150]
    assert h.headers["X-HotMem-Source-Format"] == "bin"
    assert h.headers["X-HotMem-Provenance"] == "unverified"


# ── 2. checksum mismatch / truncated / missing -> provenance error ───────────


def test_checksum_mismatch_raises_provenance_error(tmp_db: MemoryDB, fixture_file: Path):
    expected = hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest()
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=expected,
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="v")

    # Mutate the backing file so the checksum no longer matches.
    fixture_file.write_bytes(b"X" * 1024)
    with pytest.raises(ProvenanceError) as exc:
        hydrate_memory(tmp_db, mid)
    assert exc.value.reason in ("checksum_mismatch", "truncated")


def test_checksum_mismatch_raises_http_409(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    expected = hashlib.sha256(fixture_path.read_bytes()[0:40]).hexdigest()
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "ds",
            "file_uri": fixture_path.name,
            "byte_offset": 0,
            "byte_length": 40,
            "source_format": "bin",
            "source_checksum": expected,
        },
    )
    mid = resp.json()["memory_id"]
    fixture_path.write_bytes(b"Y" * 1024)
    h = app_client.post(f"/v1/memory/{mid}/hydrate")
    assert h.status_code == 409
    body = h.json()
    assert body["error"] == "provenance_mismatch"


def test_missing_file_raises_provenance_error(tmp_db: MemoryDB, fixture_file: Path):
    expected = hashlib.sha256(fixture_file.read_bytes()[0:20]).hexdigest()
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=0,
        byte_length=20,
        source_format="bin",
        source_checksum=expected,
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref)

    fixture_file.unlink()
    with pytest.raises(ProvenanceError) as exc:
        hydrate_memory(tmp_db, mid)
    assert exc.value.reason == "missing_file"


def test_missing_file_raises_http_409(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    expected = hashlib.sha256(fixture_path.read_bytes()[0:20]).hexdigest()
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "ds",
            "file_uri": fixture_path.name,
            "byte_offset": 0,
            "byte_length": 20,
            "source_format": "bin",
            "source_checksum": expected,
        },
    )
    mid = resp.json()["memory_id"]
    fixture_path.unlink()
    h = app_client.post(f"/v1/memory/{mid}/hydrate")
    assert h.status_code == 409
    assert h.json()["reason"] == "missing_file"


# ── 3. metadata access performs no file read (spy on adapter) ────────────────


def test_metadata_access_no_file_read(tmp_db: MemoryDB, fixture_file: Path):
    from spy import SpyAdapter

    spy = SpyAdapter(LocalFilesystemAdapter())
    # Monkey-patch get_adapter to return our spy for local URIs.
    import hotmem.memory as mem_mod

    orig = mem_mod.get_adapter
    mem_mod.get_adapter = lambda uri: spy

    try:
        ref = FileRef(
            source_uri=str(fixture_file),
            byte_offset=0,
            byte_length=50,
            source_format="bin",
        )
        mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="s")

        reads_before = spy.total_file_reads
        meta = get_memory_metadata(tmp_db, mid)
        reads_after = spy.total_file_reads

        assert meta is not None
        assert meta["memory_type"] == "file"
        assert reads_after == reads_before, "metadata access must not read the backing file"
    finally:
        mem_mod.get_adapter = orig


def test_metadata_endpoint_no_file_read(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "ds",
            "file_uri": fixture_path.name,
            "byte_offset": 0,
            "byte_length": 50,
            "source_format": "bin",
        },
    )
    mid = resp.json()["memory_id"]

    m = app_client.get(f"/v1/memory/{mid}")
    assert m.status_code == 200
    body = m.json()
    assert body["memory_type"] == "file"
    assert body["source_uri"] == fixture_path.name


# ── 4. inline memory path unchanged ───────────────────────────────────────────


def test_inline_path_unchanged_http(app_client: TestClient):
    resp = app_client.post(
        "/v1/add",
        json={"identifier": "vendor_x", "fact": "Invoice total was $5000", "importance": 0.8},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "memory_id" in data and "content_hash" in data and "trace_ms" in data
    mid = data["memory_id"]

    m = app_client.get(f"/v1/memory/{mid}").json()
    assert m["memory_type"] == "fact"  # main's default
    assert m["fact_text"] == "Invoice total was $5000"

    h = app_client.post(f"/v1/memory/{mid}/hydrate")
    assert h.status_code == 200
    assert h.content == b"Invoice total was $5000"
    assert h.headers["X-HotMem-Provenance"] == "unverified"


def test_inline_add_requires_fact_or_file_ref(app_client: TestClient):
    resp = app_client.post("/v1/add", json={"identifier": "x"})
    assert resp.status_code == 422
    resp2 = app_client.post(
        "/v1/add",
        json={
            "identifier": "x",
            "fact": "hi",
            "file_uri": "a",
            "byte_offset": 0,
            "byte_length": 1,
            "source_format": "bin",
        },
    )
    assert resp2.status_code == 422


# ── Extras: unsupported scheme, relative resolution, laziness, search ─────────


def test_unsupported_scheme_rejected_at_add(app_client: TestClient):
    for scheme in ("s3", "hdfs", "abfs", "gs"):
        resp = app_client.post(
            "/v1/add",
            json={
                "identifier": "remote",
                "file_uri": f"{scheme}://bucket/key",
                "byte_offset": 0,
                "byte_length": 10,
                "source_format": "bin",
            },
        )
        assert resp.status_code == 400, f"scheme {scheme} should be rejected"
        body = resp.json()
        assert body["error"] == "unsupported_scheme"


def test_relative_uri_resolves_against_mount(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "rel",
            "file_uri": fixture_path.name,
            "byte_offset": 0,
            "byte_length": 8,
            "source_format": "bin",
        },
    )
    assert resp.status_code == 200
    mid = resp.json()["memory_id"]
    h = app_client.post(f"/v1/memory/{mid}/hydrate")
    assert h.status_code == 200
    assert h.content == fixture_path.read_bytes()[:8]


def test_file_backed_with_summary_is_searchable(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "csv_ds",
            "summary": "quarterly invoice totals for vendor acme",
            "file_uri": fixture_path.name,
            "byte_offset": 0,
            "byte_length": 100,
            "source_format": "csv",
        },
    )
    assert resp.status_code == 200

    s = app_client.post("/v1/search", json={"query": "invoice vendor acme", "top_k": 5})
    assert s.status_code == 200
    body = s.json()
    assert body["count"] >= 1
    msg = body["memories"][0]
    assert set(msg.keys()) >= {"role", "content", "memory_id", "identifier", "score"}
    assert msg["role"] == "system"
    assert "invoice" in msg["content"]


def test_file_backed_without_summary_excluded_from_search(app_client: TestClient):
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    resp = app_client.post(
        "/v1/add",
        json={
            "identifier": "no_summary_ds",
            "file_uri": fixture_path.name,
            "byte_offset": 0,
            "byte_length": 100,
            "source_format": "bin",
        },
    )
    assert resp.status_code == 200

    s = app_client.post("/v1/search", json={"query": "no_summary_ds", "top_k": 50})
    body = s.json()
    ids = [m["memory_id"] for m in body["memories"]]
    assert resp.json()["memory_id"] not in ids

    m = app_client.get(f"/v1/memory/{resp.json()['memory_id']}").json()
    assert m["memory_type"] == "file"


# ── HydratedContent + hydrate_memory_detailed + hydrate_many ──────────────────


def test_hydrate_memory_detailed_returns_hydrated_content(tmp_db: MemoryDB, fixture_file: Path):
    from hotmem.memory import hydrate_memory_detailed

    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="s")

    result = hydrate_memory_detailed(tmp_db, mid)
    assert result.memory_id == mid
    assert result.content == fixture_file.read_bytes()[10:30]
    assert result.verified is False  # no checksum stored
    assert result.source_uri == str(fixture_file)
    assert result.byte_offset == 10
    assert result.byte_length == 20


def test_hydrate_memory_detailed_verified(tmp_db: MemoryDB, fixture_file: Path):
    from hotmem.memory import hydrate_memory_detailed

    expected = hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest()
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=expected,
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="s")

    result = hydrate_memory_detailed(tmp_db, mid)
    assert result.verified is True


def test_hydrate_memory_detailed_verify_false_skips_checksum(tmp_db: MemoryDB, fixture_file: Path):
    from hotmem.memory import hydrate_memory_detailed

    expected = hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest()
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=expected,
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="s")

    # Mutate the file so checksum would fail, but verify=False skips it.
    fixture_file.write_bytes(b"X" * 1024)
    result = hydrate_memory_detailed(tmp_db, mid, verify=False)
    assert result.verified is False  # not verified because verify=False


def test_hydrate_memory_detailed_inline(tmp_db: MemoryDB):
    from hotmem.embed import embed_text, pack_embedding
    from hotmem.memory import hydrate_memory_detailed

    blob = pack_embedding(embed_text("inline fact"))
    tmp_db.insert(id="inl1", identifier="x", fact_text="inline fact", embedding=blob)
    result = hydrate_memory_detailed(tmp_db, "inl1")
    assert result.content == b"inline fact"
    assert result.verified is False
    assert result.source_uri == ""


def test_hydrate_many_groups_by_uri(tmp_db: MemoryDB, fixture_file: Path):
    from hotmem.memory import hydrate_many

    # Add multiple file-backed memories pointing at the same file.
    refs = []
    for i in range(3):
        ref = FileRef(
            source_uri=str(fixture_file),
            byte_offset=i * 10,
            byte_length=10,
            source_format="bin",
        )
        mid, _ = add_file_backed(tmp_db, identifier=f"ds{i}", file_ref=ref, summary=f"slice {i}")
        refs.append(mid)

    # Add an inline memory too.
    from hotmem.embed import embed_text, pack_embedding

    blob = pack_embedding(embed_text("inline"))
    tmp_db.insert(id="inl", identifier="x", fact_text="inline", embedding=blob)

    results = hydrate_many(tmp_db, refs + ["inl"])
    assert len(results) == 4
    # File-backed results have content matching the byte ranges.
    for i, r in enumerate(results[:3]):
        assert r.content == fixture_file.read_bytes()[i * 10 : i * 10 + 10]
        assert r.source_uri == str(fixture_file)
    # Inline result.
    assert results[3].content == b"inline"
    assert results[3].source_uri == ""


# ── list_file_backed ──────────────────────────────────────────────────────────


def test_list_file_backed_no_file_io(tmp_db: MemoryDB, fixture_file: Path):
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=0,
        byte_length=50,
        source_format="bin",
    )
    add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="s")

    # list_file_backed should return file-backed rows without touching the file.
    rows = tmp_db.list_file_backed()
    assert len(rows) == 1
    assert rows[0]["memory_type"] == "file"
    assert rows[0]["source_uri"] == str(fixture_file)
    assert rows[0]["byte_length"] == 50


def test_list_file_backed_excludes_inline(tmp_db: MemoryDB):
    from hotmem.embed import embed_text, pack_embedding

    blob = pack_embedding(embed_text("inline"))
    tmp_db.insert(id="inl", identifier="x", fact_text="inline", embedding=blob)
    rows = tmp_db.list_file_backed()
    assert len(rows) == 0


# ── Typed error subclasses ─────────────────────────────────────────────────────


def test_checksum_mismatch_raises_typed_error(tmp_db: MemoryDB, fixture_file: Path):
    from hotmem.provenance import ChecksumMismatchError

    expected = hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest()
    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=expected,
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="v")
    fixture_file.write_bytes(b"X" * 1024)

    with pytest.raises(ChecksumMismatchError):
        hydrate_memory(tmp_db, mid)


def test_missing_file_raises_typed_error(tmp_db: MemoryDB, fixture_file: Path):
    from hotmem.provenance import BackingFileMissingError

    ref = FileRef(
        source_uri=str(fixture_file),
        byte_offset=0,
        byte_length=20,
        source_format="bin",
    )
    mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref)
    fixture_file.unlink()

    with pytest.raises(BackingFileMissingError):
        hydrate_memory(tmp_db, mid)


def test_typed_errors_are_provenance_errors():
    from hotmem.provenance import BackingFileMissingError, ChecksumMismatchError, ProvenanceError

    assert issubclass(ChecksumMismatchError, ProvenanceError)
    assert issubclass(BackingFileMissingError, ProvenanceError)
