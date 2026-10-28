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
from hotmem.provenance import (
    STREAM_VERIFY_THRESHOLD,
    ChecksumMismatchError,
    ProvenanceError,
    verify_range,
)
from hotmem.storage.local import LocalFilesystemAdapter


@pytest.mark.parametrize("uri", ["file://remote-host/share/notes.txt", "file:///bad%FF.txt"])
def test_http_rejects_invalid_local_file_uris(app_client, uri):
    """Unsupported/malformed URIs are actionable client errors, never HTTP 500."""
    response = app_client.post(
        "/v1/add",
        json={"identifier": "bad-uri", "file_uri": uri, "byte_offset": 0, "byte_length": 1},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_scheme"


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


def test_verify_range_streams_large_ranges(tmp_path: Path):
    """#88: ranges above STREAM_VERIFY_THRESHOLD hash while streaming —
    same digest, identical mismatch/truncation errors, O(chunk) memory;
    small ranges keep the simple single-read path."""
    from spy import SpyAdapter

    adapter = SpyAdapter(LocalFilesystemAdapter())
    data = bytes(range(256)) * ((STREAM_VERIFY_THRESHOLD + 4096) // 256)
    big = tmp_path / "big.bin"
    big.write_bytes(data)

    ok = hashlib.sha256(data[100 : 100 + STREAM_VERIFY_THRESHOLD + 1]).hexdigest()
    verify_range(adapter, str(big), 100, STREAM_VERIFY_THRESHOLD + 1, ok)
    assert adapter.read_range_chunked_calls == 1
    assert adapter.read_range_calls == 0

    # At exactly the threshold: still the simple single-read path (boundary).
    ok_at = hashlib.sha256(data[100 : 100 + STREAM_VERIFY_THRESHOLD]).hexdigest()
    verify_range(adapter, str(big), 100, STREAM_VERIFY_THRESHOLD, ok_at)
    assert adapter.read_range_calls == 1

    ok_small = hashlib.sha256(data[100:132]).hexdigest()
    verify_range(adapter, str(big), 100, 32, ok_small)
    assert adapter.read_range_calls == 2

    with pytest.raises(ChecksumMismatchError):
        verify_range(adapter, str(big), 100, STREAM_VERIFY_THRESHOLD, "0" * 64)
    with pytest.raises(ProvenanceError) as exc:
        verify_range(adapter, str(big), 0, len(data) + 1, "0" * 64)
    assert exc.value.reason == "truncated"


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


def test_verified_hydrate_reads_range_once(tmp_db: MemoryDB, fixture_file: Path):
    """#87: verified hydration reads the range exactly once — the checksum is
    computed over the already-read bytes instead of a second read."""
    from spy import SpyAdapter

    spy = SpyAdapter(LocalFilesystemAdapter())
    import hotmem.memory as mem_mod

    orig = mem_mod.get_adapter
    mem_mod.get_adapter = lambda uri: spy
    try:
        expected = hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest()
        ref = FileRef(
            source_uri=str(fixture_file),
            byte_offset=10,
            byte_length=20,
            source_format="bin",
            source_checksum=expected,
        )
        mid, _ = add_file_backed(tmp_db, identifier="ds", file_ref=ref, summary="v")

        content = hydrate_memory(tmp_db, mid)
        assert content == fixture_file.read_bytes()[10:30]
        assert spy.read_range_calls == 1, "verified hydration must not re-read the range"
    finally:
        mem_mod.get_adapter = orig


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


# ── #100: namespace-scoped file-backed identity ──────────────────────────────


def test_file_backed_identity_is_namespace_scoped(tmp_db: MemoryDB, fixture_file: Path):
    """Identical references in two namespaces are two records; a same-scope
    repeat is an idempotent hit that preserves the original id."""
    from hotmem.db import MemoryRecord
    from hotmem.memory import compute_file_content_hash

    ref = FileRef(source_uri=str(fixture_file), byte_offset=10, byte_length=20, source_format="bin")
    a_id, a_hash = add_file_backed(tmp_db, identifier="ds", file_ref=ref, namespace="ns-a")
    b_id, b_hash = add_file_backed(tmp_db, identifier="ds", file_ref=ref, namespace="ns-b")

    assert a_id != b_id
    assert a_hash == b_hash == compute_file_content_hash("ds", ref)
    assert tmp_db.count() == 2

    repeat_id, repeat_hash = add_file_backed(
        tmp_db, identifier="ds", file_ref=ref, namespace="ns-a"
    )
    assert (repeat_id, repeat_hash) == (a_id, a_hash)
    assert tmp_db.count() == 2
    assert isinstance(tmp_db.get_memory(a_id), dict)
    assert isinstance(tmp_db.get_memory(b_id), dict)
    assert not isinstance(tmp_db.get_memory(a_id), MemoryRecord)


def test_file_identity_normalizes_reference_and_excludes_checksum(
    tmp_db: MemoryDB, fixture_file: Path
):
    """A syntactically equivalent reference and a checksum change keep identity."""
    import hashlib

    from hotmem.memory import compute_file_content_hash

    plain = FileRef(
        source_uri=str(fixture_file), byte_offset=10, byte_length=20, source_format="bin"
    )
    dotted = FileRef(
        source_uri=str(fixture_file.parent / "." / fixture_file.name),
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest(),
    )

    assert compute_file_content_hash("ds", plain) == compute_file_content_hash("ds", dotted)

    first_id, first_hash = add_file_backed(tmp_db, identifier="ds", file_ref=plain, namespace="ns")
    second_id, second_hash = add_file_backed(
        tmp_db, identifier="ds", file_ref=dotted, namespace="ns"
    )
    assert (second_id, second_hash) == (first_id, first_hash)
    assert tmp_db.count() == 1


def test_file_identity_recognizes_legacy_hash(tmp_db: MemoryDB, fixture_file: Path):
    """A pre-#100 hash is still recognized so an upgrade does not duplicate."""
    from hotmem.db import MemoryRecord
    from hotmem.memory import legacy_file_content_hashes

    ref = FileRef(source_uri=str(fixture_file), byte_offset=10, byte_length=20, source_format="bin")
    legacy_hash = legacy_file_content_hashes("ds", ref)[0]
    tmp_db.insert_many_ignore(
        [
            MemoryRecord(
                id="legacy-file",
                identifier="ds",
                fact_text="",
                embedding=b"",
                content_hash=legacy_hash,
                namespace="ns",
                memory_type="file",
                source_uri=ref.source_uri,
                source_format="bin",
                byte_offset=10,
                byte_length=20,
            )
        ]
    )

    memory_id, content_hash = add_file_backed(tmp_db, identifier="ds", file_ref=ref, namespace="ns")
    assert memory_id == "legacy-file"
    assert content_hash == legacy_hash
    assert tmp_db.count() == 1


def test_http_file_identity_is_namespace_scoped(app_client: TestClient):
    """#100: file-backed writes through HTTP are namespace-scoped and idempotent."""
    fixture_path: Path = app_client.fixture_path  # type: ignore[attr-defined]
    body = {
        "identifier": "dataset",
        "file_uri": fixture_path.name,
        "byte_offset": 10,
        "byte_length": 20,
        "source_format": "bin",
    }

    a = app_client.post("/v1/add", json=body, headers={"X-HotMem-Namespace": "ns-a"})
    b = app_client.post("/v1/add", json=body, headers={"X-HotMem-Namespace": "ns-b"})
    assert a.status_code == b.status_code == 200
    assert a.json()["content_hash"] == b.json()["content_hash"]
    assert a.json()["memory_id"] != b.json()["memory_id"]

    repeat = app_client.post("/v1/add", json=body, headers={"X-HotMem-Namespace": "ns-a"})
    assert repeat.status_code == 200
    assert repeat.json()["memory_id"] == a.json()["memory_id"]
    assert repeat.json()["content_hash"] == a.json()["content_hash"]
    assert repeat.json()["deduplicated"] is True

    a_row = app_client.get(f"/v1/memory/{a.json()['memory_id']}").json()
    b_row = app_client.get(f"/v1/memory/{b.json()['memory_id']}").json()
    assert a_row["provenance"]["namespace"] == "ns-a"
    assert b_row["provenance"]["namespace"] == "ns-b"
    assert a_row["source_uri"] == fixture_path.name
    assert b_row["source_uri"] == fixture_path.name


def test_normalize_source_uri_handles_windows_remote_and_base_dir(
    tmp_db: MemoryDB, fixture_file: Path
):
    """#100: syntactic URI normalization handles file://, Windows drives, and base_dir."""
    from hotmem.interchange.canonical import compute_file_reference_hash, normalize_source_uri

    assert normalize_source_uri("file://C:/path%20with%20space.txt") == "C:/path with space.txt"
    assert normalize_source_uri("file:///c:/path%20with%20space.txt") == "C:/path with space.txt"
    assert normalize_source_uri("C:\\path with space.txt") == "C:/path with space.txt"
    assert (
        normalize_source_uri("file://remote-host/share/a%23b.txt")
        == "file://remote-host/share/a%23b.txt"
    )
    assert normalize_source_uri("file:////tmp/a.bin") == "/tmp/a.bin"
    assert normalize_source_uri("//tmp/a.bin") == "/tmp/a.bin"

    base_dir = str(fixture_file.parent)
    rel_ref = FileRef(
        source_uri=fixture_file.name, byte_offset=10, byte_length=20, source_format="bin"
    )
    abs_ref = FileRef(
        source_uri=str(fixture_file), byte_offset=10, byte_length=20, source_format="bin"
    )
    uri_ref = FileRef(
        source_uri=fixture_file.as_uri(), byte_offset=10, byte_length=20, source_format="bin"
    )

    first_id, first_hash = add_file_backed(
        tmp_db, identifier="ds", file_ref=rel_ref, base_dir=base_dir, namespace="ns"
    )
    second_id, second_hash = add_file_backed(
        tmp_db, identifier="ds", file_ref=abs_ref, base_dir=base_dir, namespace="ns"
    )
    third_id, third_hash = add_file_backed(
        tmp_db, identifier="ds", file_ref=uri_ref, base_dir=base_dir, namespace="ns"
    )
    assert (second_id, second_hash) == (first_id, first_hash)
    assert (third_id, third_hash) == (first_id, first_hash)
    assert first_hash == compute_file_reference_hash("ds", fixture_file.name, 10, 20)
    assert tmp_db.count() == 1


def test_export_host_path_redaction_recomputes_file_content_hash(tmp_path: Path):
    """#100: when export sanitizes an absolute source_uri, content_hash matches the portable URI."""
    import json

    from hotmem.interchange.canonical import compute_file_reference_hash
    from hotmem.policy import CallerCredential, PolicyConfig
    from hotmem.runtime import HotMemRuntime

    mount = tmp_path / "mount"
    mount.mkdir()
    backing = mount / "data.bin"
    backing.write_bytes(b"0123456789abcdef")

    db = MemoryDB(mount / "hotmem.sqlite")
    # Insert with base_dir=None so stored content_hash uses the absolute host path.
    add_file_backed(
        db,
        identifier="ds",
        file_ref=FileRef(
            source_uri=str(backing), byte_offset=0, byte_length=8, source_format="bin"
        ),
        namespace="team-a",
    )
    db.close()

    policy = PolicyConfig(
        store_id="store-100",
        require_auth=True,
        redact_host_paths=True,
        credentials=[
            CallerCredential.from_token(
                token="tok-export",
                caller_id="exporter",
                store_id="store-100",
                namespaces=("team-a",),
                operations=("read", "export"),
            )
        ],
    )
    rt = HotMemRuntime(db_path=mount / "hotmem.sqlite", base_dir=mount, policy=policy)
    rt.start()
    try:
        ctx = rt.resolve_caller(
            token="tok-export",
            requested_store_id="store-100",
            session_id="sess-1",
            harness_id="pytest",
            namespace="team-a",
        )
        out_jsonl = mount / "export.jsonl"
        rt.snapshot(ctx, target=str(out_jsonl))
        exported = json.loads(out_jsonl.read_text(encoding="utf-8").strip())
        assert exported["source_uri"] == "data.bin"
        assert exported["content_hash"] == compute_file_reference_hash("ds", "data.bin", 0, 8)
    finally:
        rt.stop()
