"""Tests for #39 — Snapshot v2 directory format.

Covers the four required unit tests from the issue:
  1. snapshot->hydrate round-trip for inline + file-backed memories; manifest
     checksums verified.
  2. legacy swap.jsonl (plain + stored-embedding) hydrates identically to today.
  3. tampered manifest/checksum fails hydrate loudly.
  4. determinism — identical DB produces byte-identical manifest + memories.

Plus extras: attachments opt-in, path heuristic, v2 hydrate uses stored
embeddings (no re-embedding), file-backed references preserved (no bytes
copied).
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.memory import FileRef, add_file_backed
from hotmem.snapshot import detect_format, hydrate, snapshot
from hotmem.snapshot.format import SnapshotChecksumError
from hotmem.snapshot.reader import MANIFEST_NAME, MEMORIES_NAME, verify_manifest
from hotmem.snapshot.writer import METADATA_NAME, write_snapshot_v2

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def populated_db(tmp_path: Path, fixture_file: Path) -> MemoryDB:
    """A DB with one inline memory and one file-backed memory (with summary)."""
    db = MemoryDB(tmp_path / "src.sqlite")
    # Inline memory.
    blob = pack_embedding(embed_text("an inline fact about acme invoices"))
    db.insert(
        id="inline1",
        identifier="vendor_x",
        fact_text="an inline fact about acme invoices",
        embedding=blob,
        importance=0.8,
        content_hash=hashlib.sha256(b"vendor_x:an inline fact").hexdigest(),
    )
    # File-backed memory pointing at a slice of the fixture file.
    ref = FileRef(
        source_uri=fixture_file.name,
        byte_offset=10,
        byte_length=20,
        source_format="bin",
        source_checksum=hashlib.sha256(fixture_file.read_bytes()[10:30]).hexdigest(),
    )
    add_file_backed(db, "dataset", ref, base_dir=fixture_file.parent, summary="acme q3 data")
    return db


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    return tmp_path / "snapshot"


# ── 1. round-trip inline + file-backed; manifest checksums verified ─────────


def test_roundtrip_inline_and_file_backed(populated_db: MemoryDB, snapshot_dir: Path):
    result = snapshot(populated_db, snapshot_dir)
    assert result.exported == 2
    assert (snapshot_dir / MANIFEST_NAME).is_file()
    assert (snapshot_dir / MEMORIES_NAME).is_file()
    assert (snapshot_dir / METADATA_NAME).is_file()

    # Manifest verifies cleanly.
    manifest = verify_manifest(snapshot_dir)
    assert manifest.memory_count == 2
    assert manifest.inline_count == 1
    assert manifest.file_backed_count == 1
    assert manifest.overall_sha256
    assert MEMORIES_NAME in manifest.files

    # Hydrate into a fresh DB; references preserved, not bytes copied.
    fresh = snapshot_dir.parent / "fresh.sqlite"
    fresh_db = MemoryDB(fresh)
    h = hydrate(fresh_db, snapshot_dir)
    assert h.loaded == 2
    assert fresh_db.count() == 2

    inline = fresh_db.get_memory("inline1")
    assert inline is not None
    assert inline["memory_type"] != "file"  # inline/fact (main's default)
    assert inline["fact_text"] == "an inline fact about acme invoices"

    fb_id = next(r["id"] for r in fresh_db.all_rows() if r["memory_type"] == "file")
    fb = fresh_db.get_memory(fb_id)
    assert fb is not None
    assert fb["memory_type"] == "file"
    assert fb["fact_text"] in (None, "")  # nullable/empty for file-backed
    assert fb["source_uri"] is not None
    assert fb["byte_offset"] == 10
    assert fb["byte_length"] == 20
    assert fb["source_checksum"] is not None
    fresh_db.close()


def test_roundtrip_uses_stored_embeddings_no_reembed(populated_db: MemoryDB, snapshot_dir: Path):
    """v2 snapshot stores base64 embeddings; hydrate uses them (no re-embed)."""
    snapshot(populated_db, snapshot_dir)
    # Inspect the jsonl: every inline record carries a non-null base64 embedding.
    lines = (snapshot_dir / MEMORIES_NAME).read_text().strip().split("\n")
    assert len(lines) == 2
    inline_rec = next(
        json.loads(line) for line in lines if json.loads(line)["memory_type"] != "file"
    )
    assert inline_rec["embedding"] is not None
    # Decodable as base64.
    decoded = base64.b64decode(inline_rec["embedding"])
    assert len(decoded) == EMBEDDING_DIM * 4  # float32 blob

    # Hydrate into a fresh DB; the embedding_model is preserved (stored-embedding path).
    fresh_db = MemoryDB(snapshot_dir.parent / "fresh.sqlite")
    hydrate(fresh_db, snapshot_dir)
    row = fresh_db.get_memory("inline1")
    assert row["embedding_model"] == EMBEDDING_MODEL
    fresh_db.close()


# ── 2. legacy swap.jsonl (plain + stored-embedding) hydrates identically ─────


def test_legacy_plain_swap_jsonl_hydrates(tmp_db: MemoryDB, tmp_path: Path):
    """A plain legacy swap.jsonl (no stored embedding) hydrates and re-embeds."""
    swap = tmp_path / "swap.jsonl"
    records = [
        {"identifier": "vendor_a", "fact_text": "Invoice total $5000"},
        {"identifier": "vendor_b", "fact_text": "Late payment risk"},
    ]
    swap.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    result = hydrate(tmp_db, swap)
    assert result.loaded == 2
    assert tmp_db.count() == 2


def test_legacy_stored_embedding_swap_jsonl_hydrates(tmp_db: MemoryDB, tmp_path: Path):
    """A legacy swap.jsonl carrying stored base64 embeddings is used directly."""
    swap = tmp_path / "swap.jsonl"
    blob = pack_embedding(embed_text("stored embedding fact"))
    rec = {
        "identifier": "vendor_c",
        "fact_text": "stored embedding fact",
        "embedding": base64.b64encode(blob).decode(),
        "embedding_dim": EMBEDDING_DIM,
        "embedding_model": "hotmem-hash-v1",
        "content_hash": hashlib.sha256(b"vendor_c:stored").hexdigest(),
    }
    swap.write_text(json.dumps(rec) + "\n")

    result = hydrate(tmp_db, swap)
    assert result.loaded == 1
    row = tmp_db.get_memory(tmp_db.all_rows()[0]["id"])
    assert row["embedding_model"] == "hotmem-hash-v1"


def test_legacy_snapshot_writes_v2_columns_and_base64(tmp_db: MemoryDB, tmp_path: Path):
    """The legacy writer emits v2 columns + base64 embedding."""
    blob = pack_embedding(embed_text("legacy out"))
    tmp_db.insert(id="L1", identifier="x", fact_text="legacy out", embedding=blob)
    swap = tmp_path / "out.jsonl"
    snapshot(tmp_db, swap)

    rec = json.loads(swap.read_text().strip())
    assert rec["memory_type"] == "fact"  # main's default for inline
    assert rec.get("embedding_b64") is not None  # main uses embedding_b64
    assert base64.b64decode(rec["embedding_b64"]) == blob


def test_legacy_jsonl_gz_roundtrip(tmp_db: MemoryDB, tmp_path: Path):
    """.jsonl.gz legacy snapshot round-trips through gzip."""
    blob = pack_embedding(embed_text("gz fact"))
    tmp_db.insert(id="G1", identifier="z", fact_text="gz fact", embedding=blob)
    gz = tmp_path / "out.jsonl.gz"
    snapshot(tmp_db, gz)
    assert gz.is_file()

    fresh = MemoryDB(tmp_path / "fresh.sqlite")
    result = hydrate(fresh, gz)
    assert result.loaded == 1
    assert fresh.get_memory("G1")["fact_text"] == "gz fact"
    fresh.close()


# ── 3. tampered manifest/checksum fails hydrate loudly ────────────────────────


def test_tampered_memories_jsonl_fails(populated_db: MemoryDB, snapshot_dir: Path):
    snapshot(populated_db, snapshot_dir)
    # Corrupt memories.jsonl after snapshot.
    (snapshot_dir / MEMORIES_NAME).write_text("tampered\n")

    fresh = MemoryDB(snapshot_dir.parent / "fresh.sqlite")
    with pytest.raises(SnapshotChecksumError) as exc:
        hydrate(fresh, snapshot_dir)
    assert exc.value.reason == "mismatch"
    assert exc.value.file == MEMORIES_NAME
    fresh.close()


def test_tampered_overall_sha256_fails(populated_db: MemoryDB, snapshot_dir: Path):
    snapshot(populated_db, snapshot_dir)
    # Rewrite the manifest with a wrong overall_sha256.
    mpath = snapshot_dir / MANIFEST_NAME
    manifest = json.loads(mpath.read_text())
    manifest["overall_sha256"] = "0" * 64
    mpath.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")

    fresh = MemoryDB(snapshot_dir.parent / "fresh.sqlite")
    with pytest.raises(SnapshotChecksumError) as exc:
        hydrate(fresh, snapshot_dir)
    assert exc.value.reason == "mismatch"
    assert "overall" in str(exc.value).lower()
    fresh.close()


def test_missing_manifest_fails(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    fresh = MemoryDB(tmp_path / "fresh.sqlite")
    with pytest.raises(SnapshotChecksumError) as exc:
        hydrate(fresh, empty)
    assert exc.value.reason == "missing_manifest"
    fresh.close()


def test_tampered_manifest_fails_http(app_client: TestClient):
    # Snapshot via the API into a v2 directory, then tamper.
    app_client.post("/v1/add", json={"identifier": "x", "fact": "snap me"})
    snap_dir = app_client.mount_dir / "snap"  # type: ignore[attr-defined]
    resp = app_client.post("/v1/snapshot", json={"path": str(snap_dir)})
    assert resp.status_code == 200
    assert resp.json()["exported"] == 1

    (snap_dir / MEMORIES_NAME).write_text("corrupted\n")
    h = app_client.post("/v1/hydrate", json={"path": str(snap_dir)})
    assert h.status_code == 409
    body = h.json()
    assert body["error"] == "snapshot_checksum_mismatch"


# ── 4. determinism — snapshot_id + memories are deterministic ─────────────────


def test_determinism(populated_db: MemoryDB, tmp_path: Path):
    dir1 = tmp_path / "snap1"
    dir2 = tmp_path / "snap2"
    snapshot(populated_db, dir1)
    snapshot(populated_db, dir2)

    # snapshot_id is content-derived and must be identical.
    m1 = json.loads((dir1 / MANIFEST_NAME).read_text())
    m2 = json.loads((dir2 / MANIFEST_NAME).read_text())
    assert m1["snapshot_id"] == m2["snapshot_id"], "snapshot_id must be identical"

    # memories.jsonl is sorted by id and must be byte-identical.
    mem1 = (dir1 / MEMORIES_NAME).read_bytes()
    mem2 = (dir2 / MEMORIES_NAME).read_bytes()
    assert mem1 == mem2, "memories.jsonl must be byte-identical for identical input"

    # manifest.json must be byte-identical except for created_at (wall-clock).
    m1.pop("created_at", None)
    m2.pop("created_at", None)
    assert m1 == m2, "manifest.json must be identical (minus created_at) for identical input"
    assert m1["overall_sha256"] == m2["overall_sha256"], "overall_sha256 must be deterministic"

    # metadata.json is informational; may differ in created_at/host.
    meta1 = json.loads((dir1 / METADATA_NAME).read_text())
    meta2 = json.loads((dir2 / METADATA_NAME).read_text())
    meta1.pop("created_at", None)
    meta2.pop("created_at", None)
    meta1.pop("host", None)
    meta2.pop("host", None)
    assert meta1 == meta2


# ── Extras: attachments opt-in, path heuristic, references preserved ──────────


def test_attachments_default_off(populated_db: MemoryDB, snapshot_dir: Path):
    snapshot(populated_db, snapshot_dir)
    # No attachments/ contents (directory may or may not exist, but no files).
    att_dir = snapshot_dir / "attachments"
    if att_dir.exists():
        assert not list(att_dir.iterdir())
    # Manifest carries file_references with attachment=None.
    manifest = verify_manifest(snapshot_dir)
    assert len(manifest.file_backed_references) == 1
    assert manifest.file_backed_references[0].attachment is None


def test_attachments_opt_in_copies_small_ranges(
    populated_db: MemoryDB, snapshot_dir: Path, fixture_file: Path
):
    # copy_attachments=True with base_dir pointing at the fixture's dir.
    write_snapshot_v2(
        populated_db,
        snapshot_dir,
        copy_attachments=True,
        base_dir=fixture_file.parent,
    )
    att_dir = snapshot_dir / "attachments"
    assert att_dir.is_dir()
    copies = list(att_dir.iterdir())
    assert len(copies) == 1
    # The attachment content matches the byte range.
    expected_range = fixture_file.read_bytes()[10:30]
    assert copies[0].read_bytes() == expected_range

    # Manifest references the attachment by filename.
    manifest = verify_manifest(snapshot_dir)
    ref = manifest.file_backed_references[0]
    assert ref.attachment == copies[0].name
    # And the attachment file is listed in manifest.files.
    assert f"attachments/{copies[0].name}" in manifest.files


def test_attachments_skips_large_ranges(tmp_path: Path, tmp_db: MemoryDB):
    """File-backed ranges >= attach_threshold are NOT copied (stay referenced)."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"Z" * (20 * 1024))  # 20 KB > 8 KB threshold
    ref = FileRef(
        source_uri=big.name,
        byte_offset=0,
        byte_length=20 * 1024,
        source_format="bin",
        source_checksum=hashlib.sha256(b"Z" * 20 * 1024).hexdigest(),
    )
    add_file_backed(tmp_db, "big", ref, base_dir=tmp_path, summary="big slice")

    snap_dir = tmp_path / "snap"
    write_snapshot_v2(tmp_db, snap_dir, copy_attachments=True, base_dir=tmp_path)

    att_dir = snap_dir / "attachments"
    if att_dir.exists():
        assert not list(att_dir.iterdir()), "large range should not be copied"
    manifest = verify_manifest(snap_dir)
    assert manifest.file_backed_references[0].attachment is None


def test_path_heuristic_legacy_suffix(populated_db: MemoryDB, tmp_path: Path):
    swap = tmp_path / "out.jsonl"
    assert detect_format(swap) == "legacy"
    snapshot(populated_db, swap)
    assert swap.is_file()


def test_path_heuristic_gzip(populated_db: MemoryDB, tmp_path: Path):
    gz = tmp_path / "out.jsonl.gz"
    assert detect_format(gz) == "legacy"
    snapshot(populated_db, gz)
    assert gz.is_file()


def test_path_heuristic_directory(populated_db: MemoryDB, tmp_path: Path):
    d = tmp_path / "snap"
    assert detect_format(d) == "v2"
    snapshot(populated_db, d)
    assert (d / MANIFEST_NAME).is_file()


def test_hydrate_directory_with_only_memories_jsonl(tmp_db: MemoryDB, tmp_path: Path):
    """A directory with memories.jsonl but no manifest -> legacy reader on that file."""
    d = tmp_path / "loose"
    d.mkdir()
    (d / MEMORIES_NAME).write_text(json.dumps({"identifier": "x", "fact_text": "loose"}) + "\n")
    result = hydrate(tmp_db, d)
    assert result.loaded == 1
    assert tmp_db.count() == 1


def test_hydrate_v2_preserves_file_backed_reference_no_bytes(
    populated_db: MemoryDB, snapshot_dir: Path, fixture_file: Path
):
    """Hydrate reconstructs file-backed refs WITHOUT touching the backing file."""
    snapshot(populated_db, snapshot_dir)
    # Move the backing file away so any read would fail; hydrate must still work.
    fixture_file.rename(fixture_file.parent / "renamed.bin")

    fresh = MemoryDB(snapshot_dir.parent / "fresh.sqlite")
    result = hydrate(fresh, snapshot_dir)
    assert result.loaded == 2  # references preserved without reading the file
    fb = next(r for r in fresh.all_rows() if r["memory_type"] == "file")
    assert fb["source_uri"] == fixture_file.name
    fresh.close()


def test_snapshot_v2_via_api(app_client: TestClient):
    """The /v1/snapshot endpoint writes a v2 directory when given a dir path."""
    app_client.post("/v1/add", json={"identifier": "x", "fact": "api snap"})
    snap_dir = app_client.mount_dir / "apisnap"  # type: ignore[attr-defined]
    resp = app_client.post("/v1/snapshot", json={"path": str(snap_dir)})
    assert resp.status_code == 200
    assert resp.json()["exported"] == 1
    assert (snap_dir / MANIFEST_NAME).is_file()

    # Hydrate back via the API.
    fresh = app_client.mount_dir / "fresh.sqlite"  # type: ignore[attr-defined]
    # Use a second client pointed at a fresh DB to verify round-trip.
    from hotmem.server import create_app

    app2 = create_app(db_path=fresh, base_dir=app_client.mount_dir)
    with TestClient(app2) as c2:
        h = c2.post("/v1/hydrate", json={"path": str(snap_dir)})
        assert h.status_code == 200
        assert h.json()["loaded"] == 1
        assert c2.get("/v1/health").json()["memory_count"] == 1


def test_legacy_api_file_alias_still_works(app_client: TestClient):
    """The deprecated 'file' field on /v1/snapshot still writes a legacy JSONL."""
    app_client.post("/v1/add", json={"identifier": "x", "fact": "legacy alias"})
    swap = app_client.mount_dir / "legacy.jsonl"  # type: ignore[attr-defined]
    resp = app_client.post("/v1/snapshot", json={"file": str(swap)})
    assert resp.status_code == 200
    assert resp.json()["exported"] == 1
    assert swap.is_file()


# ── Shared normalization on the v2 path (#67) ──────────────────────────────


def test_roundtrip_preserves_namespace_tier_tags_ttl(tmp_db: MemoryDB, tmp_path: Path):
    import hashlib as _h

    db = tmp_db
    db.insert(
        id="ns1",
        identifier="vendor_x",
        fact_text="namespaced fact",
        embedding=pack_embedding(embed_text("namespaced fact")),
        content_hash=_h.sha256(b"vendor_x:namespaced fact").hexdigest(),
        namespace="team-x",
        tier="warm",
        tags='["ns", "tier"]',
        ttl_seconds=43200,
        fact_summary="namespaced",
        provenance_json=json.dumps({"origin": "test"}),
    )
    out = tmp_path / "snap"
    write_snapshot_v2(db, out)
    record = json.loads((out / "memories.jsonl").read_text().strip())
    assert record["namespace"] == "team-x"
    assert record["tier"] == "warm"
    assert record["tags"] == ["ns", "tier"]
    assert record["ttl_seconds"] == 43200
    assert record["provenance"] == {"origin": "test"}

    result = hydrate(MemoryDB(tmp_path / "fresh.sqlite"), out)
    db2 = MemoryDB(tmp_path / "fresh.sqlite")
    row = db2.all_rows()[0]
    assert row["namespace"] == "team-x"
    assert row["tier"] == "warm"
    assert json.loads(row["tags"]) == ["ns", "tier"]
    assert row["ttl_seconds"] == 43200
    assert json.loads(row["provenance_json"]) == {"origin": "test"}
    db2.close()
    assert result.loaded == 1


def test_v2_hydrate_rejects_incompatible_stored_embedding(
    tmp_db: MemoryDB, snapshot_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """A v2 record with a foreign embedding must be re-embedded, not reused.

    Before #67 the v2 reader base64-decoded blindly; the shared compatibility
    rule now applies here too.
    """
    blob = pack_embedding(embed_text("v2 fact"))
    snapshot_dir.mkdir(parents=True)
    record = {
        "schema_version": 2,
        "id": "bad-model",
        "identifier": "x",
        "fact_text": "v2 fact",
        "memory_type": "fact",
        "embedding": base64.b64encode(blob).decode(),
        "embedding_dim": 64,
        "embedding_model": "foreign-model-v9",
        "source": "snapshot",
        "content_hash": hashlib.sha256(b"x:v2 fact").hexdigest(),
    }
    (snapshot_dir / MEMORIES_NAME).write_text(json.dumps(record) + "\n")
    manifest = {
        "format": "hotmem-snapshot-v2",
        "snapshot_id": "x" * 64,
        "created_at": "2026-01-01T00:00:00Z",
        "files": {MEMORIES_NAME: {"size": None, "sha256": ""}},
    }
    payload = (snapshot_dir / MEMORIES_NAME).read_bytes()
    manifest["files"][MEMORIES_NAME] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (snapshot_dir / MANIFEST_NAME).write_text(json.dumps(manifest))

    fresh = MemoryDB(snapshot_dir.parent / "fresh.sqlite")
    result = hydrate(fresh, snapshot_dir)
    row = fresh.all_rows(include_embedding=True)[0]
    fresh.close()
    assert result.loaded == 1
    assert row["embedding_model"] == EMBEDDING_MODEL
    assert row["embedding"] == blob  # same text -> same deterministic vector


def test_verify_manifest_rejects_path_escape(tmp_path: Path):
    """A manifest listing ../outside must fail verification (confinement)."""
    d = tmp_path / "snap"
    d.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (d / MEMORIES_NAME).write_text("{}\n")
    payload = (d / MEMORIES_NAME).read_bytes()
    manifest = {
        "format": "hotmem-snapshot-v2",
        "snapshot_id": "x" * 64,
        "files": {
            MEMORIES_NAME: {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            "../outside.txt": {
                "size": outside.stat().st_size,
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            },
        },
    }
    (d / MANIFEST_NAME).write_text(json.dumps(manifest))

    from hotmem.snapshot.reader import verify_manifest

    with pytest.raises(SnapshotChecksumError, match="path_escape"):
        verify_manifest(d)


# ── Windows byte-integrity & newline regressions (#118 Gate 1) ───────────────


def test_windows_crlf_stream_regression_and_idempotent_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Gate 1 (#118): writer must emit exact LF-hashed bytes even when default
    text streams translate LF -> CRLF as on Windows; clean restore reuses both
    stored embeddings and a second hydrate is idempotent."""
    import builtins

    real_open = builtins.open

    def windows_default_open(file, mode="r", *args, **kwargs):
        # Emulate Windows TextIOWrapper default: text writes without an explicit
        # newline policy translate '\n' into '\r\n'.
        if "b" not in mode and ("w" in mode or "a" in mode) and kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", windows_default_open)

    from hotmem.swap import add_memory

    src_db = MemoryDB(tmp_path / "src.sqlite")
    add_memory(src_db, "windows-check", "Synthetic training note one.")
    add_memory(src_db, "windows-check", "Synthetic training note two.")

    snap_dir = tmp_path / "windows-snapshot-check"
    res = write_snapshot_v2(src_db, snap_dir)
    src_db.close()
    assert res.exported == 2

    raw_memories = (snap_dir / MEMORIES_NAME).read_bytes()
    assert b"\r\n" not in raw_memories
    assert b"\r\n" not in (snap_dir / MANIFEST_NAME).read_bytes()
    assert b"\r\n" not in (snap_dir / METADATA_NAME).read_bytes()

    manifest = verify_manifest(snap_dir)
    entry = manifest.files[MEMORIES_NAME]
    assert entry.size == len(raw_memories)
    assert entry.sha256 == hashlib.sha256(raw_memories).hexdigest()

    # Clean-store restore: 2 loaded, 2 embeddings reused, 0 rebuilt.
    clean_db = MemoryDB(tmp_path / "clean.sqlite")
    first = hydrate(clean_db, snap_dir)
    assert first.loaded == 2
    assert first.skipped_dupes == 0
    assert first.invalid == 0
    assert first.embedding_reused == 2
    assert first.embedding_rebuilt == 0
    assert first.embedding_missing == 0
    assert first.embedding_failed == 0

    # Second hydrate into the same store is idempotent.
    second = hydrate(clean_db, snap_dir)
    assert second.loaded == 0
    assert second.skipped_dupes == 2
    assert second.invalid == 0
    assert second.embedding_reused == 0
    assert second.embedding_rebuilt == 0
    clean_db.close()


def test_roundtrip_preserves_unicode_and_embedded_newlines(tmp_path: Path):
    """Gate 1 (#118): non-ASCII content and embedded LF/CRLF inside record fields
    survive Snapshot v2 round-trip without altering logical record content."""
    from hotmem.swap import add_memory

    src_db = MemoryDB(tmp_path / "src.sqlite")
    fact_one = "Café résumé — 東京 — line1\nline2\r\nline3"
    fact_two = " Cyrillic: Привет мир\r\nTabbed:\tvalue "
    id1, h1 = add_memory(
        src_db,
        "unicode-check",
        fact_one,
        metadata={"note": "multi\nline\r\nmeta — 雪"},
    )
    id2, h2 = add_memory(src_db, "unicode-check", fact_two)

    snap_dir = tmp_path / "snap-unicode"
    write_snapshot_v2(src_db, snap_dir)
    src_db.close()

    assert b"\r\n" not in (snap_dir / MEMORIES_NAME).read_bytes()
    verify_manifest(snap_dir)

    clean_db = MemoryDB(tmp_path / "clean.sqlite")
    result = hydrate(clean_db, snap_dir)
    assert result.loaded == 2
    assert result.embedding_reused == 2
    assert result.embedding_rebuilt == 0

    r1 = clean_db.get_memory(id1)
    r2 = clean_db.get_memory(id2)
    assert r1 is not None and r2 is not None
    assert r1["fact_text"] == fact_one
    assert r1["content_hash"] == h1
    assert json.loads(r1["metadata_json"]) == {"note": "multi\nline\r\nmeta — 雪"}
    assert r2["fact_text"] == fact_two
    assert r2["content_hash"] == h2
    clean_db.close()


def test_crlf_corrupted_or_truncated_payload_fails_closed_before_mutation(tmp_path: Path):
    """Gate 1 (#118): CRLF-translated (+2 bytes for 2 records) or truncated
    memories.jsonl fails verification before any target DB mutation."""
    from hotmem.swap import add_memory

    src_db = MemoryDB(tmp_path / "src.sqlite")
    add_memory(src_db, "windows-check", "Synthetic training note one.")
    add_memory(src_db, "windows-check", "Synthetic training note two.")
    snap_dir = tmp_path / "snap"
    write_snapshot_v2(src_db, snap_dir)
    src_db.close()

    original_bytes = (snap_dir / MEMORIES_NAME).read_bytes()
    crlf_bytes = original_bytes.replace(b"\n", b"\r\n")
    assert len(crlf_bytes) == len(original_bytes) + 2

    target_db = MemoryDB(tmp_path / "target.sqlite")
    seed_id, _ = add_memory(target_db, "existing", "Pre-existing target record.")

    # 1. CRLF-mutated file (+2 bytes, matching the 0.2.5 Windows defect symptom).
    (snap_dir / MEMORIES_NAME).write_bytes(crlf_bytes)
    with pytest.raises(SnapshotChecksumError) as exc_crlf:
        hydrate(target_db, snap_dir)
    assert exc_crlf.value.reason == "mismatch"
    assert exc_crlf.value.file == MEMORIES_NAME
    assert exc_crlf.value.expected == f"size={len(original_bytes)}"
    assert exc_crlf.value.actual == f"size={len(crlf_bytes)}"
    assert target_db.count() == 1

    # 2. Truncated file.
    (snap_dir / MEMORIES_NAME).write_bytes(original_bytes[:-5])
    with pytest.raises(SnapshotChecksumError) as exc_trunc:
        hydrate(target_db, snap_dir)
    assert exc_trunc.value.reason == "mismatch"
    assert exc_trunc.value.file == MEMORIES_NAME
    assert target_db.count() == 1
    assert target_db.get_memory(seed_id) is not None
    target_db.close()


def test_v2_identity_is_namespace_scoped_and_repeat_is_idempotent(tmp_path: Path):
    """#100: export/restore keeps namespace twins and skips same-scope repeats."""
    from hotmem.interchange.canonical import compute_content_hash

    src = MemoryDB(tmp_path / "src-scoped.sqlite")
    content_hash = compute_content_hash("doc", "same fact")
    for ns, memory_id in (("ns-a", "a"), ("ns-b", "b")):
        src.insert(
            id=memory_id,
            identifier="doc",
            fact_text="same fact",
            embedding=pack_embedding(embed_text("same fact")),
            content_hash=content_hash,
            namespace=ns,
        )
    snap = tmp_path / "snap-scoped"
    write_snapshot_v2(src, snap)
    src.close()

    dst = MemoryDB(tmp_path / "dst-scoped.sqlite")
    first = hydrate(dst, snap)
    assert first.loaded == 2
    assert first.skipped_dupes == 0
    assert {row["namespace"] for row in dst.all_rows()} == {"ns-a", "ns-b"}

    second = hydrate(dst, snap)
    assert second.loaded == 0
    assert second.skipped_dupes == 2
    assert dst.count() == 2
    dst.close()


# -- Maximal inline record v2 round-trip (#99 WP2) --


@pytest.mark.parametrize("stored_schema_version", [1, 2, 3])
def test_maximal_inline_record_roundtrips_all_30_columns_across_clean_runtimes(
    tmp_path: Path, stored_schema_version: int
):
    """#99 WP2: a maximal inline record exported via Snapshot v2, Interchange v1,
    and JSONL hydrates into clean runtimes with every portable field equal and
    compatible stored embeddings reused without recomputation."""
    from hotmem.db import _MEMORY_COLUMNS
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.interchange.fingerprint import record_fingerprint
    from hotmem.interchange.record import MEMORY_FIELD_INVENTORY
    from hotmem.snapshot import verify as verify_snapshot

    fact_text = "Maximal inline Memory Record v2 state fidelity check (#99)."
    identifier = "ops/inline-maximal"
    content_hash = compute_content_hash(identifier, fact_text)
    vec_blob = pack_embedding(embed_text(fact_text))

    src = MemoryDB(tmp_path / f"src_v{stored_schema_version}.sqlite")
    src.insert(
        id="inline-max-001",
        identifier=identifier,
        fact_text=fact_text,
        embedding=vec_blob,
        embedding_dim=EMBEDDING_DIM,
        embedding_model=EMBEDDING_MODEL,
        source="agent://investigator",
        importance=0.85,
        metadata_json=json.dumps(
            {
                "ticket": 99,
                "nested": {"stage": "wp2", "ok": True},
                "annotations": {
                    "schema_version": 1,
                    "confidence": [
                        {
                            "score": 0.95,
                            "source": "human:reviewer",
                            "timestamp": "2026-10-01T12:00:00Z",
                            "rationale": "verified inline state",
                        }
                    ],
                },
            },
            sort_keys=True,
        ),
        content_hash=content_hash,
        ttl_seconds=86400,
        created_at="2026-10-01T12:00:00Z",
        namespace="ops/shared",
        tier="warm",
        memory_type="fact",
        source_uri="",
        source_format="",
        source_checksum="",
        byte_offset=None,
        byte_length=None,
        updated_at="2026-10-02T15:30:00Z",
        snapshot_id="snap-lineage-prev-01",
        promotion_state="READY",
        promotion_candidate=1,
        parent_memory="mem-parent-root",
        related_memories=json.dumps(["mem-rel-a", "mem-rel-b"]),
        tags=json.dumps(["model:gpt-5", "project:hotmem", "org:knowguard"]),
        schema_version=stored_schema_version,
        fact_summary="Short summary of maximal inline record",
        provenance_json=json.dumps(
            {
                "caller_id": "caller-alpha",
                "envelope_version": "hotmem-session-v1",
                "harness_id": "codex",
                "namespace": "ops/shared",
                "session_id": "sess-wp2-01",
                "source": "agent://investigator",
                "store_id": "store-origin",
            },
            sort_keys=True,
        ),
    )
    expected_row = src.get_memory("inline-max-001")
    assert expected_row is not None
    expected_fp = record_fingerprint(expected_row)

    # 1. Snapshot v2 export -> verify -> clean hydrate
    v2_dir = tmp_path / f"snap_v2_{stored_schema_version}"
    snapshot(src, v2_dir)
    verify_info = verify_snapshot(v2_dir)
    assert verify_info["valid"] is True

    # 2. Interchange v1 package export -> verify -> clean hydrate
    pkg_dir = tmp_path / f"pkg_v1_{stored_schema_version}"
    snapshot(src, pkg_dir, package=True)
    assert verify_snapshot(pkg_dir)["valid"] is True

    # 3. Legacy JSONL export -> clean hydrate
    jsonl_path = tmp_path / f"swap_{stored_schema_version}.jsonl"
    snapshot(src, jsonl_path)
    src.close()

    for label, artifact in (("v2", v2_dir), ("package", pkg_dir), ("jsonl", jsonl_path)):
        dst = MemoryDB(tmp_path / f"dst_{label}_{stored_schema_version}.sqlite")
        res = hydrate(dst, artifact)
        assert res.loaded == 1
        assert res.skipped_dupes == 0
        assert res.invalid == 0
        assert res.embedding_reused == 1
        assert res.embedding_rebuilt == 0

        restored = dst.get_memory("inline-max-001")
        assert restored is not None
        for col in _MEMORY_COLUMNS:
            assert col in MEMORY_FIELD_INVENTORY
            assert restored[col] == expected_row[col], (
                f"{label} mismatch on column {col!r}: "
                f"expected {expected_row[col]!r}, got {restored[col]!r}"
            )
        assert record_fingerprint(restored) == expected_fp
        dst.close()


# -- Maximal file-backed record & declared dependencies (#99 WP3) --


@pytest.mark.parametrize("stored_schema_version", [1, 2])
def test_maximal_file_backed_record_and_attachments_roundtrip_into_clean_runtime(
    tmp_path: Path, stored_schema_version: int
):
    """#99 WP3: a maximal file-backed record restores all 30 columns and its
    attached backing bytes into a clean runtime where the original file does
    not exist, while reference-only exports declare the external dependency."""
    from hotmem.db import _MEMORY_COLUMNS
    from hotmem.interchange.fingerprint import record_fingerprint
    from hotmem.memory import FileRef, add_file_backed, hydrate_memory_detailed
    from hotmem.provenance import BackingFileMissingError, ChecksumMismatchError
    from hotmem.snapshot import verify as verify_snapshot

    src_workspace = tmp_path / f"workspace_v{stored_schema_version}"
    src_workspace.mkdir()
    backing = src_workspace / "ledger.csv"
    backing_bytes = b"id,amount,status\n101,2500.00,SETTLED\n102,900.00,PENDING\n"
    backing.write_bytes(backing_bytes)

    slice_offset = 17
    slice_length = 20
    slice_bytes = backing_bytes[slice_offset : slice_offset + slice_length]
    slice_sha = hashlib.sha256(slice_bytes).hexdigest()

    src = MemoryDB(src_workspace / "src.sqlite")
    mid, ch = add_file_backed(
        src,
        identifier="finance/ledger-row-101",
        file_ref=FileRef(
            source_uri="ledger.csv",
            byte_offset=slice_offset,
            byte_length=slice_length,
            source_format="csv",
            source_checksum=slice_sha,
        ),
        base_dir=str(src_workspace),
        summary="Settled ledger entry 101 for $2500.00",
        importance=0.9,
        metadata={"sheet": "Q4", "verified_by": "auditor"},
        source="agent://finance-ingest",
        ttl_seconds=172800,
        created_at="2026-10-03T08:00:00Z",
        namespace="finance/q4",
        tier="cold",
        updated_at="2026-10-04T09:15:00Z",
        snapshot_id="snap-fin-000",
        promotion_state="PROMOTED",
        promotion_candidate=1,
        parent_memory="inline-max-001",
        related_memories=["mem-audit-1", "mem-audit-2"],
        tags=["model:gpt-5", "project:hotmem", "org:knowguard"],
        schema_version=stored_schema_version,
        provenance={
            "caller_id": "caller-fin",
            "envelope_version": "hotmem-session-v1",
            "harness_id": "codex",
            "namespace": "finance/q4",
            "session_id": "sess-wp3-fin",
            "source": "agent://finance-ingest",
            "store_id": "store-fin-src",
        },
    )
    expected_row = src.get_memory(mid)
    assert expected_row is not None
    expected_fp = record_fingerprint(expected_row)

    # Export Snapshot v2 WITH attachments and WITHOUT attachments, plus Interchange v1.
    v2_attached = tmp_path / f"v2_attached_{stored_schema_version}"
    snapshot(src, v2_attached, copy_attachments=True, base_dir=src_workspace)
    assert verify_snapshot(v2_attached)["valid"] is True
    att_manifest = verify_manifest(v2_attached)
    assert len(att_manifest.file_backed_references) == 1
    assert att_manifest.file_backed_references[0].attachment == slice_sha
    assert f"attachments/{slice_sha}" in att_manifest.files

    v2_ref_only = tmp_path / f"v2_ref_only_{stored_schema_version}"
    snapshot(src, v2_ref_only, copy_attachments=False, base_dir=src_workspace)
    ref_manifest = verify_manifest(v2_ref_only)
    assert len(ref_manifest.file_backed_references) == 1
    assert ref_manifest.file_backed_references[0].attachment is None
    assert ref_manifest.file_backed_references[0].source_uri == "ledger.csv"
    assert ref_manifest.file_backed_references[0].source_checksum == slice_sha

    pkg_dir = tmp_path / f"pkg_file_{stored_schema_version}"
    snapshot(src, pkg_dir, package=True)
    src.close()

    # Delete the original backing file to prove clean-runtime independence.
    backing.unlink()

    # 1. Clean runtime hydrating v2_attached restores all 30 columns AND can
    # materialize the verified slice bytes directly from the snapshot attachment.
    clean_attached_db = MemoryDB(tmp_path / f"clean_att_{stored_schema_version}.sqlite")
    res_att = hydrate(clean_attached_db, v2_attached)
    assert res_att.loaded == 1
    assert res_att.embedding_reused == 1
    assert res_att.embedding_rebuilt == 0
    restored_att = clean_attached_db.get_memory(mid)
    assert restored_att is not None
    for col in _MEMORY_COLUMNS:
        assert restored_att[col] == expected_row[col], (
            f"attached v2 column {col!r} mismatch: "
            f"expected {expected_row[col]!r}, got {restored_att[col]!r}"
        )
    assert restored_att["content_hash"] == ch
    assert record_fingerprint(restored_att) == expected_fp

    hydrated_payload = hydrate_memory_detailed(clean_attached_db, mid, base_dir=v2_attached)
    assert hydrated_payload.content == slice_bytes
    assert hydrated_payload.verified is True

    # Re-exporting from clean_attached_db using v2_attached as base_dir preserves the attachment.
    v2_reexport = tmp_path / f"v2_reexport_{stored_schema_version}"
    snapshot(clean_attached_db, v2_reexport, copy_attachments=True, base_dir=v2_attached)
    reexport_manifest = verify_manifest(v2_reexport)
    assert reexport_manifest.file_backed_references[0].attachment == slice_sha

    # Tampering with the attachment in v2_attached is caught on payload hydration too.
    att_file = v2_attached / "attachments" / slice_sha
    att_file.write_bytes(b"X" * slice_length)
    with pytest.raises(ChecksumMismatchError):
        hydrate_memory_detailed(clean_attached_db, mid, base_dir=v2_attached)
    clean_attached_db.close()

    # 2. Clean runtime hydrating v2_ref_only or pkg_dir preserves all 30 columns,
    # and reports BackingFileMissingError when attempting to read external bytes
    # that are absent in the clean environment.
    for label, art in (("v2_ref_only", v2_ref_only), ("pkg", pkg_dir)):
        clean_ref_db = MemoryDB(tmp_path / f"clean_{label}_{stored_schema_version}.sqlite")
        res_ref = hydrate(clean_ref_db, art)
        assert res_ref.loaded == 1
        restored_ref = clean_ref_db.get_memory(mid)
        assert restored_ref is not None
        for col in _MEMORY_COLUMNS:
            assert restored_ref[col] == expected_row[col], (
                f"{label} column {col!r} mismatch: "
                f"expected {expected_row[col]!r}, got {restored_ref[col]!r}"
            )
        with pytest.raises(BackingFileMissingError):
            hydrate_memory_detailed(clean_ref_db, mid, base_dir=tmp_path)
        clean_ref_db.close()


def test_missing_or_mismatched_declared_attachment_fails_before_hydration(
    populated_db: MemoryDB, tmp_path: Path
):
    """#99 WP3: if a manifest declares an attachment in file_backed_references
    or attachments that is missing from files/disk or mismatches source_checksum
    or byte_length, verify_manifest and hydrate fail closed without mutating target DB."""
    snap_dir = tmp_path / "snap_dep"
    write_snapshot_v2(populated_db, snap_dir, copy_attachments=True, base_dir=tmp_path)

    target_db = MemoryDB(tmp_path / "target_dep.sqlite")

    # Case 1: file_backed_references points to an attachment not listed in manifest.files
    manifest_path = snap_dir / MANIFEST_NAME
    orig_manifest_bytes = manifest_path.read_bytes()

    bad_missing = json.loads(orig_manifest_bytes.decode("utf-8"))
    bad_missing["file_backed_references"][0]["attachment"] = "deadbeef" * 8
    manifest_path.write_text(json.dumps(bad_missing), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError, match="missing_file"):
        hydrate(target_db, snap_dir)
    assert target_db.count() == 0

    # Case 2: file_backed_references attachment sha256 disagrees with source_checksum
    bad_checksum = json.loads(orig_manifest_bytes.decode("utf-8"))
    bad_checksum["file_backed_references"][0]["source_checksum"] = "00" * 32
    manifest_path.write_text(json.dumps(bad_checksum), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError, match="mismatch"):
        hydrate(target_db, snap_dir)
    assert target_db.count() == 0

    # Case 3: file_backed_references byte_length disagrees with attachment size
    bad_size = json.loads(orig_manifest_bytes.decode("utf-8"))
    bad_size["file_backed_references"][0]["byte_length"] = 999
    manifest_path.write_text(json.dumps(bad_size), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError, match="mismatch"):
        hydrate(target_db, snap_dir)
    assert target_db.count() == 0

    # Case 4: manifest.files is missing memories.jsonl altogether
    bad_no_memories = json.loads(orig_manifest_bytes.decode("utf-8"))
    bad_no_memories["files"].pop(MEMORIES_NAME, None)
    manifest_path.write_text(json.dumps(bad_no_memories), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError, match="missing_file"):
        hydrate(target_db, snap_dir)
    assert target_db.count() == 0
    target_db.close()


# -- Hydration mapping, validation, rollback & embeddings (#99 WP4) --


def test_hydrate_v2_rejects_malformed_record_values_and_rolls_back_on_midstream_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """#99 WP4: malformed record values in a checksum-valid Snapshot v2 are
    rejected without storing invalid state, and a mid-stream failure after a
    batch flush rolls back the entire transaction so target DB stays untouched."""
    from hotmem.snapshot import reader as v2_reader
    from hotmem.snapshot.format import FileEntry, Manifest, compute_overall, sha256_file

    # 1. Checksum-valid Snapshot v2 containing 1 valid record and 3 malformed records.
    snap_dir = tmp_path / "snap_malformed_records"
    snap_dir.mkdir()
    lines = [
        json.dumps(
            {
                "id": "valid-1",
                "identifier": "ops/valid",
                "fact_text": "Valid record survives.",
                "importance": 0.6,
                "tier": "hot",
                "promotion_state": "HOT",
                "promotion_candidate": 0,
            }
        ),
        json.dumps(
            {
                "id": "bad-state",
                "identifier": "ops/bad-state",
                "fact_text": "Invalid promotion state must be rejected.",
                "promotion_state": "NOT_A_VALID_STATE",
            }
        ),
        json.dumps(
            {
                "id": "bad-imp",
                "identifier": "ops/bad-imp",
                "fact_text": "Out of range importance must be rejected.",
                "importance": 9.99,
            }
        ),
        json.dumps(
            {
                "id": "bad-ttl",
                "identifier": "ops/bad-ttl",
                "fact_text": "Negative TTL must be rejected.",
                "ttl_seconds": -30,
            }
        ),
    ]
    mem_path = snap_dir / MEMORIES_NAME
    mem_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mem_sha = sha256_file(mem_path)
    mem_size = mem_path.stat().st_size
    manifest = Manifest(
        snapshot_id="a" * 64,
        memory_count=len(lines),
        inline_count=len(lines),
        files={MEMORIES_NAME: FileEntry(size=mem_size, sha256=mem_sha)},
        overall_sha256=compute_overall({MEMORIES_NAME: mem_sha}),
    )
    (snap_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    dst = MemoryDB(tmp_path / "dst_malformed.sqlite")
    res = hydrate(dst, snap_dir)
    assert res.loaded == 1
    assert res.invalid == 3
    assert dst.count() == 1
    assert dst.get_memory("valid-1") is not None
    assert dst.get_memory("bad-state") is None
    assert dst.get_memory("bad-imp") is None
    assert dst.get_memory("bad-ttl") is None
    dst.close()

    # 2. Multi-batch rollback: force HYDRATE_BATCH=1 so batch 1 flushes before
    # batch 2 raises an error; verify db.rollback() leaves 0 rows in target DB.
    monkeypatch.setattr(v2_reader, "HYDRATE_BATCH", 1)
    snap_rollback = tmp_path / "snap_rollback"
    snap_rollback.mkdir()
    rb_lines = [
        json.dumps({"id": "rb-1", "identifier": "ops/rb1", "fact_text": "First batch row."}),
        json.dumps({"id": "rb-2", "identifier": "ops/rb2", "fact_text": "Second batch row."}),
    ]
    rb_mem = snap_rollback / MEMORIES_NAME
    rb_mem.write_text("\n".join(rb_lines) + "\n", encoding="utf-8")
    rb_sha = sha256_file(rb_mem)
    rb_manifest = Manifest(
        snapshot_id="b" * 64,
        memory_count=2,
        inline_count=2,
        files={MEMORIES_NAME: FileEntry(size=rb_mem.stat().st_size, sha256=rb_sha)},
        overall_sha256=compute_overall({MEMORIES_NAME: rb_sha}),
    )
    (snap_rollback / MANIFEST_NAME).write_text(
        json.dumps(rb_manifest.to_dict(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    target_rb = MemoryDB(tmp_path / "target_rb.sqlite")
    orig_insert_many = target_rb.insert_many_ignore
    calls = 0

    def fail_on_second_batch(records, *, _commit: bool = True):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated mid-hydration failure after batch 1 flush")
        return orig_insert_many(records, _commit=_commit)

    monkeypatch.setattr(target_rb, "insert_many_ignore", fail_on_second_batch)
    with pytest.raises(RuntimeError, match="simulated mid-hydration failure"):
        hydrate(target_rb, snap_rollback)

    assert calls == 2
    assert target_rb.count() == 0
    target_rb.close()


def test_hydrate_v2_preserves_compatible_embeddings_and_rebuilds_only_incompatible(
    tmp_path: Path,
):
    """#99 WP4: compatible stored embeddings are preserved without calling the
    embedder, while incompatible embeddings are rebuilt and restamped."""
    from hotmem.embed import EmbeddingDescriptor

    desc = EmbeddingDescriptor(
        implementation="custom",
        model="wp4-embed",
        dimension=8,
        revision="r1",
        preprocessing="pp1",
    )

    class CountingEmbedder:
        def __init__(self) -> None:
            self.calls: list[str] = []

        @property
        def descriptor(self) -> EmbeddingDescriptor:
            return desc

        def embed(self, text: str) -> list[float]:
            self.calls.append(text)
            norm = 8**0.5
            return [(1.0 if i % 2 == 0 else -1.0) / norm for i in range(8)]

    embedder = CountingEmbedder()
    unit_blob = pack_embedding(embedder.embed("seed"))
    embedder.calls.clear()

    src = MemoryDB(tmp_path / "src_emb.sqlite")
    # Row 1: already matches CountingEmbedder's descriptor -> must be reused.
    src.insert(
        id="emb-compat",
        identifier="ops/compat",
        fact_text="Compatible vector text.",
        embedding=unit_blob,
        embedding_dim=desc.dimension,
        embedding_model=desc.key,
        content_hash=hashlib.sha256(b"ops/compat:Compatible vector text.").hexdigest(),
    )
    # Row 2: carries foreign model -> must be rebuilt under CountingEmbedder.
    src.insert(
        id="emb-foreign",
        identifier="ops/foreign",
        fact_text="Foreign vector text.",
        embedding=pack_embedding(embed_text("Foreign vector text.")),
        embedding_dim=EMBEDDING_DIM,
        embedding_model="foreign-model-v0",
        content_hash=hashlib.sha256(b"ops/foreign:Foreign vector text.").hexdigest(),
    )

    snap_dir = tmp_path / "snap_emb"
    write_snapshot_v2(src, snap_dir)
    src.close()

    dst = MemoryDB(tmp_path / "dst_emb.sqlite")
    res = hydrate(dst, snap_dir, embedder=embedder)
    assert res.loaded == 2
    assert res.embedding_reused == 1
    assert res.embedding_rebuilt == 1
    assert embedder.calls == ["Foreign vector text."]

    r_compat = dst.get_memory("emb-compat")
    r_foreign = dst.get_memory("emb-foreign")
    assert r_compat is not None and r_foreign is not None
    assert r_compat["embedding"] == unit_blob
    assert r_compat["embedding_model"] == desc.key
    assert r_compat["embedding_dim"] == desc.dimension
    assert r_foreign["embedding"] == unit_blob
    assert r_foreign["embedding_model"] == desc.key
    assert r_foreign["embedding_dim"] == desc.dimension
    dst.close()


# -- Zero-length file references: export/verify/hydrate compatibility (#99 review F1) --


def test_zero_length_file_reference_export_verifies_and_hydrates(
    tmp_path: Path,
):
    """A supported zero-length file-backed memory must not produce a snapshot
    that fails its own manifest verification, and must hydrate with all 30
    portable columns preserved (legacy DBs and ``add_file_backed`` allow 0)."""
    from hotmem.db import _MEMORY_COLUMNS
    from hotmem.memory import FileRef, add_file_backed
    from hotmem.snapshot import verify as verify_snapshot

    ws = tmp_path / "ws_zero"
    ws.mkdir()
    (ws / "empty.csv").write_bytes(b"")

    src = MemoryDB(ws / "src.sqlite")
    mid, _ = add_file_backed(
        src,
        identifier="ops/empty-slice",
        file_ref=FileRef(
            source_uri="empty.csv",
            byte_offset=0,
            byte_length=0,
            source_format="csv",
            source_checksum=hashlib.sha256(b"").hexdigest(),
        ),
        base_dir=str(ws),
        summary="Zero-length slice summary",
        namespace="ops",
        tier="cold",
        promotion_state="ARCHIVED",
        promotion_candidate=0,
        tags=["project:hotmem"],
        provenance={"session_id": "sess-zero"},
    )
    expected_row = src.get_memory(mid)
    assert expected_row is not None and expected_row["byte_length"] == 0

    snap_dir = tmp_path / "snap_zero"
    snapshot(src, snap_dir, copy_attachments=True, base_dir=ws)
    manifest = verify_manifest(snap_dir)
    assert manifest.file_backed_references[0].byte_length == 0
    assert manifest.file_backed_references[0].attachment is None
    assert verify_snapshot(snap_dir)["valid"] is True
    src.close()

    dst = MemoryDB(tmp_path / "dst_zero.sqlite")
    res = hydrate(dst, snap_dir)
    assert res.loaded == 1
    assert res.invalid == 0
    restored = dst.get_memory(mid)
    assert restored is not None
    for col in _MEMORY_COLUMNS:
        assert restored[col] == expected_row[col], (
            f"zero-length ref column {col!r} mismatch: "
            f"expected {expected_row[col]!r}, got {restored[col]!r}"
        )
    dst.close()


def test_legacy_manifest_with_zero_length_reference_still_verifies(tmp_path: Path):
    """Pre-#99 supported packages may declare a zero-length file reference;
    verification must accept it (reject only negative lengths)."""
    from hotmem.snapshot.format import compute_overall, sha256_file

    snap_dir = tmp_path / "legacy_zero_ref"
    snap_dir.mkdir()
    record = {
        "schema_version": 2,
        "id": "legacy-zero",
        "identifier": "legacy/zero",
        "memory_type": "file",
        "fact_text": "",
        "fact_summary": "Legacy zero-length slice",
        "source_uri": "old.csv",
        "byte_offset": 0,
        "byte_length": 0,
        "source_format": "csv",
        "source_checksum": "",
    }
    mem = snap_dir / MEMORIES_NAME
    mem.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    mem_sha = sha256_file(mem)
    manifest = {
        "format": "hotmem-snapshot-v2",
        "schema_version": 2,
        "snapshot_id": "d" * 64,
        "memory_count": 1,
        "file_backed_count": 1,
        "inline_count": 0,
        "files": {MEMORIES_NAME: {"size": mem.stat().st_size, "sha256": mem_sha}},
        "overall_sha256": compute_overall({MEMORIES_NAME: mem_sha}),
        "file_backed_references": [
            {
                "memory_id": "legacy-zero",
                "source_uri": "old.csv",
                "byte_offset": 0,
                "byte_length": 0,
                "source_format": "csv",
                "source_checksum": "",
            }
        ],
    }
    (snap_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    parsed = verify_manifest(snap_dir)
    assert parsed.file_backed_references[0].byte_length == 0

    dst = MemoryDB(tmp_path / "dst_legacy_zero.sqlite")
    res = hydrate(dst, snap_dir)
    assert res.loaded == 1
    assert res.invalid == 0
    restored = dst.get_memory("legacy-zero")
    assert restored is not None and restored["byte_length"] == 0
    dst.close()


def test_hydrate_many_uses_snapshot_attachments_in_clean_runtime(tmp_path: Path):
    """#99 review F2: the bulk hydration path must honor verified Snapshot v2
    attachments exactly like hydrate_memory_detailed when the backing file is
    absent from the clean runtime."""
    from hotmem.db import _MEMORY_COLUMNS
    from hotmem.memory import FileRef, add_file_backed, hydrate_many
    from hotmem.snapshot import verify as verify_snapshot

    ws = tmp_path / "ws_bulk"
    ws.mkdir()
    payload = b"part,price\\nA,10\\nB,20\\n"
    (ws / "parts.csv").write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()

    src = MemoryDB(ws / "src.sqlite")
    mid, _ = add_file_backed(
        src,
        identifier="ops/parts",
        file_ref=FileRef("parts.csv", 0, len(payload), "csv", checksum),
        base_dir=str(ws),
        summary="Parts price table",
        namespace="ops",
        tags=["project:hotmem"],
        provenance={"session_id": "sess-bulk"},
    )
    expected = src.get_memory(mid)
    assert expected is not None

    snap_dir = tmp_path / "snap_bulk"
    snapshot(src, snap_dir, copy_attachments=True, base_dir=ws)
    assert verify_snapshot(snap_dir)["valid"] is True
    src.close()
    (ws / "parts.csv").unlink()

    dst = MemoryDB(tmp_path / "dst_bulk.sqlite")
    assert hydrate(dst, snap_dir).loaded == 1
    restored = dst.get_memory(mid)
    assert restored is not None
    for col in _MEMORY_COLUMNS:
        assert restored[col] == expected[col]

    results = hydrate_many(dst, [mid], base_dir=snap_dir)
    assert len(results) == 1
    assert results[0].content == payload
    assert results[0].verified is True
    dst.close()


def test_snapshot_attachment_lookup_rejects_wrong_length_and_caches_manifest(
    tmp_path: Path,
):
    """#99 review F3: corrupt-length attachments are never returned, and the
    manifest reference index is parsed once per manifest identity."""
    from hotmem.memory import (
        FileRef,
        _snapshot_reference_index,
        add_file_backed,
        hydrate_memory_detailed,
    )
    from hotmem.provenance import BackingFileMissingError
    from hotmem.snapshot import verify as verify_snapshot

    ws = tmp_path / "ws_len"
    ws.mkdir()
    payload = b"sku,qty\\nA,1\\n"
    (ws / "stock.csv").write_bytes(payload)

    src = MemoryDB(ws / "src.sqlite")
    mid, _ = add_file_backed(
        src,
        identifier="ops/stock",
        file_ref=FileRef("stock.csv", 0, len(payload), "csv", None),
        base_dir=str(ws),
        summary="Stock quantity table",
        namespace="ops",
    )
    snap_dir = tmp_path / "snap_len"
    snapshot(src, snap_dir, copy_attachments=True, base_dir=ws)
    assert verify_snapshot(snap_dir)["valid"] is True
    src.close()
    (ws / "stock.csv").unlink()

    dst = MemoryDB(tmp_path / "dst_len.sqlite")
    assert hydrate(dst, snap_dir).loaded == 1

    _snapshot_reference_index.cache_clear()
    first = hydrate_memory_detailed(dst, mid, base_dir=snap_dir)
    second = hydrate_memory_detailed(dst, mid, base_dir=snap_dir)
    assert first.content == payload and second.content == payload
    info = _snapshot_reference_index.cache_info()
    assert info.misses == 1
    assert info.hits >= 1

    att_file = next((snap_dir / "attachments").iterdir())
    att_file.write_bytes(att_file.read_bytes() + b"tampered")
    with pytest.raises(BackingFileMissingError):
        hydrate_memory_detailed(dst, mid, base_dir=snap_dir)
    dst.close()


def test_manifest_falls_back_to_legacy_file_references_when_new_key_empty(
    tmp_path: Path,
):
    """#99 review F4: a transitional manifest with an empty
    ``file_backed_references`` and populated legacy ``file_references`` keeps
    its declared dependencies (the pre-#99 ``or`` fallback)."""
    from hotmem.memory import FileRef, add_file_backed

    ws = tmp_path / "ws_legacy_refs"
    ws.mkdir()
    payload = b"id,name\\n1,alpha\\n"
    (ws / "names.csv").write_bytes(payload)

    src = MemoryDB(ws / "src.sqlite")
    mid, _ = add_file_backed(
        src,
        identifier="ops/names",
        file_ref=FileRef("names.csv", 0, len(payload), "csv", hashlib.sha256(payload).hexdigest()),
        base_dir=str(ws),
        summary="Names table",
        namespace="ops",
    )
    snap_dir = tmp_path / "snap_legacy_refs"
    snapshot(src, snap_dir, copy_attachments=True, base_dir=ws)
    src.close()

    manifest_path = snap_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy_refs = manifest["file_backed_references"]
    assert legacy_refs and legacy_refs[0]["memory_id"] == mid
    manifest["file_backed_references"] = []
    manifest["file_references"] = legacy_refs
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")

    parsed = verify_manifest(snap_dir)
    assert len(parsed.file_backed_references) == 1
    assert parsed.file_backed_references[0].memory_id == mid
    assert parsed.file_backed_references[0].attachment == legacy_refs[0]["attachment"]


def test_snapshot_v2_manifest_carries_mount_identity_and_replay_cursor(
    populated_db: MemoryDB, tmp_path: Path
):
    """#94: Snapshot v2 manifest records mount_id, replica_id, history_id,
    state_fingerprint, and an opaque hmc1 replay_cursor; clean hydrate records
    clone lineage while tampered cursors fail verification before mutation."""
    import shutil

    from hotmem.events import decode_cursor
    from hotmem.mount import compare_mount_lineage

    snap_dir = tmp_path / "snap_cursor_v2"
    snapshot(populated_db, snap_dir)

    raw_manifest = json.loads((snap_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    src_ident = populated_db.mount_identity()
    assert raw_manifest["mount_id"] == src_ident.mount_id
    assert raw_manifest["replica_id"] == src_ident.replica_id
    assert raw_manifest["history_id"] == src_ident.history_id
    assert raw_manifest["state_fingerprint"]
    assert raw_manifest["replay_cursor"].startswith("hmc1.")

    decoded = decode_cursor(raw_manifest["replay_cursor"])
    assert decoded.mount_id == src_ident.mount_id
    assert decoded.replica_id == src_ident.replica_id
    assert decoded.history_id == src_ident.history_id

    # Hydrating into a clean target adopts mount_id and records clone lineage.
    clone_db = MemoryDB(tmp_path / "clone_from_v2.sqlite")
    try:
        hydrate(clone_db, snap_dir)
        cmp_res = compare_mount_lineage(populated_db, clone_db)
        assert cmp_res["relationship"] == "clone"
        assert clone_db.mount_identity().mount_id == src_ident.mount_id
        assert clone_db.mount_identity().replica_id != src_ident.replica_id
        assert clone_db.mount_identity().base_cursor == raw_manifest["replay_cursor"]
    finally:
        clone_db.close()

    # Tampering replay_cursor with a bare sequence or mismatched mount_id fails verification.
    bad_dir = tmp_path / "snap_bad_cursor"
    shutil.copytree(snap_dir, bad_dir)
    bad_manifest = dict(raw_manifest)
    bad_manifest["replay_cursor"] = "1"
    (bad_dir / MANIFEST_NAME).write_text(json.dumps(bad_manifest), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError) as exc_info:
        verify_manifest(bad_dir)
    assert exc_info.value.file == "replay_cursor"

    bad_manifest["mount_id"] = "mnt-wrong-mount"
    bad_manifest["replay_cursor"] = raw_manifest["replay_cursor"]
    (bad_dir / MANIFEST_NAME).write_text(json.dumps(bad_manifest), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError) as exc_info2:
        verify_manifest(bad_dir)
    assert exc_info2.value.file == "replay_cursor"

    # A cursor without its identity fields is rejected.
    no_identity = dict(raw_manifest)
    for key in ("mount_id", "replica_id", "history_id"):
        no_identity.pop(key, None)
    (bad_dir / MANIFEST_NAME).write_text(json.dumps(no_identity), encoding="utf-8")
    with pytest.raises(SnapshotChecksumError) as exc_no_ident:
        verify_manifest(bad_dir)
    assert exc_no_ident.value.file == "replay_cursor"

    # Derived identity fields are recomputed from the payload for #94 exports.
    for field, value in (("state_fingerprint", "f" * 64), ("snapshot_id", "0" * 64)):
        tampered = dict(raw_manifest)
        tampered[field] = value
        (bad_dir / MANIFEST_NAME).write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(SnapshotChecksumError) as exc_derived:
            verify_manifest(bad_dir)
        assert exc_derived.value.file == field


def test_snapshot_fully_trimmed_history_exports_without_cursor(
    populated_db: MemoryDB, tmp_path: Path
):
    """#94: a fully trimmed event history cannot anchor a cursor; export must
    omit it instead of emitting an unusable token."""
    populated_db.append_event(
        event_id="trim-evt",
        event_type="memory.created",
        occurred_at="2026-10-30T00:00:00Z",
    )
    assert populated_db.trim_events_by_count(0) == 1

    snap_dir = tmp_path / "trimmed_snap"
    snapshot(populated_db, snap_dir)
    manifest = json.loads((snap_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert not manifest.get("replay_cursor")
    assert manifest["mount_id"] == populated_db.mount_identity().mount_id

    target = MemoryDB(tmp_path / "trimmed_target.sqlite")
    try:
        result = hydrate(target, snap_dir)
        assert result.loaded == populated_db.count()
        assert target.count() == populated_db.count()
    finally:
        target.close()


def test_hydrate_duplicate_records_remains_a_clone_not_a_fork(tmp_path: Path):
    """#94: skipped duplicate records must not make a clean clone look divergent."""
    from hotmem.interchange.fingerprint import (
        record_fingerprint,
        state_fingerprint,
        state_fingerprint_from_list,
    )
    from hotmem.mount import compare_mount_lineage
    from hotmem.snapshot.format import compute_overall, compute_snapshot_id
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "dup_src.sqlite")
    snap_dir = tmp_path / "dup_snap"
    try:
        add_memory(src, "dup/x", "One duplicated fact")
        snapshot(src, snap_dir)
        manifest = json.loads((snap_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        payload_path = snap_dir / MEMORIES_NAME
        lines = payload_path.read_text(encoding="utf-8").splitlines()
        duplicate = json.loads(lines[0])
        duplicate["id"] = "dup-alternate-id"
        records = [json.loads(lines[0]), duplicate]
        payload = ("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n").encode()
        payload_path.write_bytes(payload)

        manifest["files"][MEMORIES_NAME] = {
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        manifest["overall_sha256"] = compute_overall(
            {name: entry["sha256"] for name, entry in manifest["files"].items()}
        )
        manifest["memory_count"] = len(records)
        manifest["inline_count"] = int(manifest.get("inline_count") or 0) + 1
        manifest["snapshot_id"] = compute_snapshot_id(records)
        manifest["state_fingerprint"] = state_fingerprint_from_list(
            [record_fingerprint(r) for r in records]
        )
        (snap_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
        )

        target = MemoryDB(tmp_path / "dup_target.sqlite")
        try:
            result = hydrate(target, snap_dir)
            assert result.loaded == 1
            assert result.skipped_dupes == 1
            assert target.count() == 1
            assert compare_mount_lineage(src, target)["relationship"] == "clone"
            assert target.mount_identity().base_state_fingerprint == state_fingerprint(
                target.all_rows()
            )
        finally:
            target.close()
    finally:
        src.close()


def test_snapshot_same_mount_bootstrap_clone_records_clone_lineage_and_replays(
    populated_db: MemoryDB, tmp_path: Path
):
    """#94: snapshot hydrate into a bootstrapped same-mount clone records clone
    lineage and accepts cursor replay, instead of being classified replacement."""
    from hotmem.events import _build_memory_created_payload, append_event, replay_with_cursor
    from hotmem.mount import bootstrap_mount, compare_mount_lineage
    from hotmem.swap import add_memory

    for row in populated_db.all_rows(include_embedding=True):
        append_event(
            populated_db,
            event_type="memory.created",
            memory_id=row["id"],
            namespace=row.get("namespace") or "",
            payload=_build_memory_created_payload(row),
        )

    snap_dir = tmp_path / "snap_same_mount"
    snapshot(populated_db, snap_dir)
    src_mount = populated_db.mount_identity().mount_id

    clone_mount = tmp_path / "snap_clone_mount"
    bootstrap_mount(clone_mount, mount_id=src_mount)
    clone = MemoryDB(clone_mount / "hotmem.sqlite")
    try:
        hydrate(clone, snap_dir)
        lineage = compare_mount_lineage(populated_db, clone)
        assert lineage["relationship"] == "clone", lineage
        assert clone.mount_identity().base_source_replica_id == (
            populated_db.mount_identity().replica_id
        )

        add_memory(populated_db, "snap-later", "created after snapshot export")
        new_row = populated_db.all_rows(include_embedding=True)[-1]
        append_event(
            populated_db,
            event_type="memory.created",
            memory_id=new_row["id"],
            namespace=new_row.get("namespace") or "",
            payload=_build_memory_created_payload(new_row),
        )
        res = replay_with_cursor(populated_db, clone)
        assert res.applied == 1
        assert res.completed is True
        assert compare_mount_lineage(populated_db, clone)["relationship"] == "clone"
    finally:
        clone.close()


# ── Semantic snapshot identity (#96) ─────────────────────────────────────────


def test_semantic_snapshot_id_distinguishes_namespace_and_preserves_legacy_v1_and_lineage(
    tmp_path: Path,
):
    """#96 MVT-1: semantic snapshot_id (v2) distinguishes identical content_hash
    across namespaces and portable state while keeping legacy v1 manifests and
    #94 clone/cursor lineage compatible."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.mount import compare_mount_lineage
    from hotmem.snapshot.format import (
        LEGACY_SNAPSHOT_ID_ALGORITHM,
        LEGACY_SNAPSHOT_ID_VERSION,
        SNAPSHOT_ID_ALGORITHM,
        SNAPSHOT_ID_VERSION,
        compute_legacy_snapshot_id,
    )

    ch = compute_content_hash("shared/topic", "Identical fact across namespaces.")
    db_a = MemoryDB(tmp_path / "ns_a.sqlite")
    db_b = MemoryDB(tmp_path / "ns_b.sqlite")
    try:
        for db_inst, ns, state in ((db_a, "ns/alpha", "HOT"), (db_b, "ns/beta", "READY")):
            db_inst.insert(
                id="same-id-1",
                identifier="shared/topic",
                fact_text="Identical fact across namespaces.",
                embedding=pack_embedding(embed_text("Identical fact across namespaces.")),
                embedding_dim=EMBEDDING_DIM,
                embedding_model=EMBEDDING_MODEL,
                source="agent://test",
                importance=0.5,
                metadata_json="{}",
                content_hash=ch,
                created_at="2026-10-30T10:00:00Z",
                namespace=ns,
                tier="hot",
                promotion_state=state,
                promotion_candidate=0,
            )

        snap_a = tmp_path / "snap_a"
        snap_b = tmp_path / "snap_b"
        snapshot(db_a, snap_a)
        snapshot(db_b, snap_b)

        man_a = verify_manifest(snap_a)
        man_b = verify_manifest(snap_b)
        assert man_a.snapshot_id_version == SNAPSHOT_ID_VERSION
        assert man_a.snapshot_id_algorithm == SNAPSHOT_ID_ALGORITHM
        assert man_a.snapshot_id != man_b.snapshot_id
        assert man_a.snapshot_id != compute_legacy_snapshot_id([ch])

        # Fallback base_snapshot_id lineage check works for both v2 semantic ID
        # and legacy v1 content-hash-list ID even without base_source_replica_id.
        clone_v2 = MemoryDB(tmp_path / "clone_v2.sqlite")
        clone_v1 = MemoryDB(tmp_path / "clone_v1.sqlite")
        try:
            hydrate(clone_v2, snap_a)
            clone_v2._conn.execute("DELETE FROM store_meta WHERE key = 'base_source_replica_id'")
            clone_v2.commit()
            assert compare_mount_lineage(db_a, clone_v2)["relationship"] == "clone"

            # Convert snap_a manifest to explicit v1 legacy content-hash-list identity.
            raw_man = json.loads((snap_a / MANIFEST_NAME).read_text(encoding="utf-8"))
            raw_man["snapshot_id"] = compute_legacy_snapshot_id([ch])
            raw_man["snapshot_id_version"] = LEGACY_SNAPSHOT_ID_VERSION
            raw_man["snapshot_id_algorithm"] = LEGACY_SNAPSHOT_ID_ALGORITHM
            (snap_a / MANIFEST_NAME).write_text(
                json.dumps(raw_man, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            verified_v1 = verify_manifest(snap_a)
            assert verified_v1.snapshot_id_version == LEGACY_SNAPSHOT_ID_VERSION

            hydrate(clone_v1, snap_a)
            clone_v1._conn.execute("DELETE FROM store_meta WHERE key = 'base_source_replica_id'")
            clone_v1.commit()
            assert compare_mount_lineage(db_a, clone_v1)["relationship"] == "clone"
        finally:
            clone_v2.close()
            clone_v1.close()
    finally:
        db_a.close()
        db_b.close()


def test_semantic_snapshot_id_golden_vectors():
    """#96 WP5: lock canonical projection JSON and SHA-256 digests for empty,
    scoped-empty, and maximal inline + file-backed snapshot vectors."""
    from hotmem.interchange.canonical import (
        canonical_dumps,
        compute_content_hash,
        compute_file_reference_hash,
    )
    from hotmem.snapshot.format import (
        FileReference,
        compute_semantic_snapshot_id,
        semantic_record_digest,
        semantic_record_projection,
        semantic_snapshot_projection,
        sha256_bytes,
    )

    # 1. Empty unscoped snapshot golden vector.
    empty_proj = semantic_snapshot_projection([])
    assert canonical_dumps(empty_proj) == (
        '{"file_backed_references":[],"records":[],"scope":null,"version":2}'
    )
    expected_empty_id = sha256_bytes(
        b'hotmem-snapshot-id-v2:{"file_backed_references":[],"records":[],"scope":null,"version":2}'
    )
    assert expected_empty_id == "f8a6cf35d0d17fab750458fdd94399434cf4e5816fa10cb166d01ac22946b3dd"
    assert compute_semantic_snapshot_id([]) == expected_empty_id

    # 2. Empty namespace-scoped snapshot golden vector.
    scoped_empty_id = compute_semantic_snapshot_id(
        [],
        scope={
            "namespace": "ops/shared",
            "permitted_namespaces": ["ops/public", "ops/shared", "ops/public"],
            "session_id": "ignored-session-noise",
            "caller_id": "ignored-caller-noise",
        },
    )
    assert scoped_empty_id != expected_empty_id
    assert scoped_empty_id == sha256_bytes(
        b'hotmem-snapshot-id-v2:{"file_backed_references":[],"records":[],'
        b'"scope":{"namespace":"ops/shared","permitted_namespaces":["ops/public","ops/shared"]},'
        b'"version":2}'
    )

    # 3. Pinned inline + file-backed golden records.
    inline_rec = {
        "schema_version": 2,
        "record_schema_version": 1,
        "id": "gold-inl-01",
        "identifier": "ops/runbook",
        "memory_type": "fact",
        "fact_text": "Freeze Friday production deploys after 16:00 UTC.",
        "fact_summary": "Friday deploy freeze policy",
        "embedding": "AAAA",
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
        "source": "agent://codex",
        "importance": 0.85,
        "metadata": {"service": "payments", "tier_hint": 1},
        "content_hash": compute_content_hash(
            "ops/runbook", "Freeze Friday production deploys after 16:00 UTC."
        ),
        "ttl_seconds": 86400,
        "created_at": "2026-10-30T09:00:00Z",
        "updated_at": "2026-10-30T10:00:00Z",
        "namespace": "ops/prod",
        "tier": "warm",
        "tags": ["model:gpt-5", "env:prod"],
        "related_memories": ["gold-rel-01", "gold-rel-02"],
        "parent_memory": "gold-parent-00",
        "snapshot_id": "snap-origin-00",
        "promotion_state": "READY",
        "promotion_candidate": 1,
        "provenance": {"session_id": "sess-gold-1", "actor": "human:sre"},
    }
    file_rec = {
        "schema_version": 2,
        "id": "gold-file-01",
        "identifier": "ops/metrics",
        "memory_type": "file",
        "fact_text": "",
        "fact_summary": "Latency CSV slice",
        "source": "agent://claude",
        "importance": 0.6,
        "metadata": {},
        "content_hash": compute_file_reference_hash("ops/metrics", "data/latency.csv", 0, 128),
        "created_at": "2026-10-30T09:05:00Z",
        "namespace": "ops/prod",
        "tier": "hot",
        "source_uri": "./data/latency.csv",
        "byte_offset": 0,
        "byte_length": 128,
        "source_format": "csv",
        "source_checksum": "ab" * 32,
        "promotion_state": "HOT",
        "promotion_candidate": 0,
    }
    file_ref = FileReference(
        memory_id="gold-file-01",
        source_uri="./data/latency.csv",
        byte_offset=0,
        byte_length=128,
        source_format="csv",
        source_checksum="ab" * 32,
        attachment="ab" * 32,
    )

    inl_proj = semantic_record_projection(inline_rec)
    assert "embedding" not in inl_proj
    assert "embedding_dim" not in inl_proj
    assert "embedding_model" not in inl_proj
    assert inl_proj["schema_version"] == 1
    assert semantic_record_projection(file_rec)["schema_version"] == 2
    assert semantic_record_projection(file_rec)["source_uri"] == "data/latency.csv"

    d_inl = semantic_record_digest(inline_rec)
    d_file = semantic_record_digest(file_rec)
    assert len(d_inl) == 64 and len(d_file) == 64 and d_inl != d_file

    combined_id = compute_semantic_snapshot_id(
        [inline_rec, file_rec],
        file_backed_references=[file_ref],
        scope={"namespace": "ops/prod"},
    )
    expected_envelope = {
        "file_backed_references": [
            {
                "byte_length": 128,
                "byte_offset": 0,
                "memory_id": "gold-file-01",
                "source_checksum": "ab" * 32,
                "source_format": "csv",
                "source_uri": "data/latency.csv",
            }
        ],
        "records": sorted([d_inl, d_file]),
        "scope": {"namespace": "ops/prod", "permitted_namespaces": []},
        "version": 2,
    }
    assert combined_id == sha256_bytes(
        f"hotmem-snapshot-id-v2:{canonical_dumps(expected_envelope)}".encode()
    )


def test_semantic_snapshot_id_invariant_to_record_ref_and_json_key_ordering(tmp_path: Path):
    """#96 acceptance: reordering records, file_backed_references, or JSON object
    keys never changes semantic snapshot_id."""
    from hotmem.snapshot.format import FileReference, compute_semantic_snapshot_id

    rec_1 = {
        "id": "rec-1",
        "identifier": "alpha/one",
        "fact_text": "First portable memory",
        "namespace": "team-a",
        "metadata": {"z_key": 1, "a_key": {"inner_b": "two", "inner_a": "one"}},
        "provenance": {"z_prov": "x", "a_prov": "y"},
        "created_at": "2026-10-30T01:00:00Z",
    }
    rec_1_rekeyed = {
        "created_at": "2026-10-30T01:00:00Z",
        "provenance": {"a_prov": "y", "z_prov": "x"},
        "metadata": {"a_key": {"inner_a": "one", "inner_b": "two"}, "z_key": 1},
        "namespace": "team-a",
        "fact_text": "First portable memory",
        "identifier": "alpha/one",
        "id": "rec-1",
    }
    rec_2 = {
        "id": "rec-2",
        "identifier": "beta/two",
        "memory_type": "file",
        "fact_text": "",
        "fact_summary": "Second portable file memory",
        "source_uri": "dir/../reports/q3.csv",
        "byte_offset": 10,
        "byte_length": 200,
        "source_format": "csv",
        "source_checksum": "11" * 32,
        "namespace": "team-b",
        "created_at": "2026-10-30T02:00:00Z",
    }
    ref_a = FileReference(
        memory_id="rec-2",
        source_uri="reports/q3.csv",
        byte_offset=10,
        byte_length=200,
        source_format="csv",
        source_checksum="11" * 32,
        attachment="11" * 32,
    )
    ref_b = FileReference(
        memory_id="rec-3",
        source_uri="./notes/arch.md",
        byte_offset=0,
        byte_length=64,
        source_format="md",
        source_checksum="22" * 32,
        attachment=None,
    )

    id_forward = compute_semantic_snapshot_id(
        [rec_1, rec_2],
        file_backed_references=[ref_a, ref_b],
        scope={"namespace": "team-a", "permitted_namespaces": ["team-b", "team-a"]},
    )
    id_reversed = compute_semantic_snapshot_id(
        [rec_2, rec_1_rekeyed],
        file_backed_references=[ref_b, ref_a],
        scope={"permitted_namespaces": ["team-a", "team-b", "team-a"], "namespace": "team-a"},
    )
    assert id_forward == id_reversed


def test_semantic_snapshot_id_changes_on_every_preserved_field_and_reference_state():
    """#96 acceptance: every one of the 27 #99 preserve columns, plus file-backed
    reference state and semantic scope, changes semantic snapshot_id when mutated."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.interchange.record import MEMORY_FIELD_INVENTORY
    from hotmem.snapshot.format import (
        FileReference,
        compute_semantic_snapshot_id,
        semantic_record_projection,
    )

    # Verify 1-to-1 coverage of all 27 'preserve' columns in MEMORY_FIELD_INVENTORY.
    preserved_inventory_cols = {
        name for name, spec in MEMORY_FIELD_INVENTORY.items() if spec.disposition == "preserve"
    }
    regenerated_inventory_cols = {
        name for name, spec in MEMORY_FIELD_INVENTORY.items() if spec.disposition == "regenerate"
    }
    assert len(preserved_inventory_cols) == 27
    assert regenerated_inventory_cols == {"embedding", "embedding_dim", "embedding_model"}

    base_record = {
        "id": "mem-base-01",
        "identifier": "ops/incident",
        "fact_text": "Primary database failover completed in 14 seconds.",
        "fact_summary": "DB failover duration",
        "memory_type": "fact",
        "source": "agent://codex",
        "importance": 0.75,
        "metadata": {"ticket": "INC-42", "extra": ["a"]},
        "content_hash": compute_content_hash(
            "ops/incident", "Primary database failover completed in 14 seconds."
        ),
        "ttl_seconds": 3600,
        "created_at": "2026-10-30T12:00:00Z",
        "updated_at": "2026-10-30T12:30:00Z",
        "namespace": "ops/incident",
        "tier": "warm",
        "tags": ["sev:2", "db:primary"],
        "related_memories": ["mem-rel-1", "mem-rel-2"],
        "parent_memory": "mem-parent-1",
        "source_uri": "logs/failover.log",
        "source_format": "log",
        "source_checksum": "aa" * 32,
        "byte_offset": 0,
        "byte_length": 512,
        "snapshot_id": "snap-prev-1",
        "promotion_state": "READY",
        "promotion_candidate": 1,
        "schema_version": 2,
        "record_schema_version": 1,
        "provenance": {"session_id": "s-100", "harness_id": "codex"},
        "embedding": "AAAA",
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
    }
    proj = semantic_record_projection(base_record)
    mapped_proj_cols = {
        "metadata_json" if k == "metadata" else "provenance_json" if k == "provenance" else k
        for k in proj
    }
    assert mapped_proj_cols == preserved_inventory_cols

    base_ref = FileReference(
        memory_id="mem-base-01",
        source_uri="logs/failover.log",
        byte_offset=0,
        byte_length=512,
        source_format="log",
        source_checksum="aa" * 32,
        attachment=None,
    )
    base_id = compute_semantic_snapshot_id(
        [base_record],
        file_backed_references=[base_ref],
        scope={"namespace": "ops/incident"},
    )

    mutations: list[tuple[str, dict[str, object]]] = [
        ("id", {"id": "mem-base-02"}),
        ("identifier", {"identifier": "ops/incident-other"}),
        ("fact_text", {"fact_text": "Primary database failover completed in 19 seconds."}),
        ("fact_summary", {"fact_summary": "Updated summary"}),
        ("memory_type", {"memory_type": "file"}),
        ("source", {"source": "agent://claude"}),
        ("source_empty", {"source": ""}),
        ("importance", {"importance": 0.25}),
        ("metadata", {"metadata": {"ticket": "INC-99", "extra": ["a"]}}),
        ("content_hash", {"content_hash": "ff" * 32}),
        ("ttl_seconds", {"ttl_seconds": 7200}),
        ("ttl_none", {"ttl_seconds": None}),
        ("created_at", {"created_at": "2026-10-30T13:00:00Z"}),
        ("updated_at", {"updated_at": "2026-10-30T14:00:00Z"}),
        ("updated_at_none", {"updated_at": None}),
        ("namespace", {"namespace": "ops/other-ns"}),
        ("namespace_default", {"namespace": ""}),
        ("tier", {"tier": "cold"}),
        ("tags_value", {"tags": ["sev:1", "db:primary"]}),
        ("tags_order", {"tags": ["db:primary", "sev:2"]}),
        ("related_memories_value", {"related_memories": ["mem-rel-1", "mem-rel-9"]}),
        ("related_memories_order", {"related_memories": ["mem-rel-2", "mem-rel-1"]}),
        ("parent_memory", {"parent_memory": "mem-parent-2"}),
        ("source_uri", {"source_uri": "logs/other.log"}),
        ("source_format", {"source_format": "txt"}),
        ("source_checksum", {"source_checksum": "bb" * 32}),
        ("byte_offset", {"byte_offset": 64}),
        ("byte_offset_none", {"byte_offset": None}),
        ("byte_length", {"byte_length": 1024}),
        ("byte_length_none", {"byte_length": None}),
        ("snapshot_id", {"snapshot_id": "snap-prev-2"}),
        ("promotion_state", {"promotion_state": "PROMOTED"}),
        ("promotion_candidate", {"promotion_candidate": 0}),
        ("schema_version", {"record_schema_version": 2}),
        ("provenance", {"provenance": {"session_id": "s-200", "harness_id": "codex"}}),
    ]
    for label, patch in mutations:
        mutated = {**base_record, **patch}
        mut_id = compute_semantic_snapshot_id(
            [mutated],
            file_backed_references=[base_ref],
            scope={"namespace": "ops/incident"},
        )
        assert mut_id != base_id, f"mutation {label!r} did not change semantic snapshot_id"

    # Attachment reference mutation and scope mutation also change snapshot_id.
    attached_ref = FileReference(
        memory_id="mem-base-01",
        source_uri="logs/failover.log",
        byte_offset=0,
        byte_length=512,
        source_format="log",
        source_checksum="aa" * 32,
        attachment="aa" * 32,
    )
    # Attachment bundling is packaging, not portable state: it must not change
    # semantic identity (#96 acceptance, docs/snapshot-v2.md#determinism).
    assert (
        compute_semantic_snapshot_id(
            [base_record],
            file_backed_references=[attached_ref],
            scope={"namespace": "ops/incident"},
        )
        == base_id
    )
    assert (
        compute_semantic_snapshot_id(
            [base_record],
            file_backed_references=[base_ref],
            scope={"namespace": "ops/different-scope"},
        )
        != base_id
    )


def test_semantic_snapshot_id_ignores_regenerate_embeddings_and_packaging_noise(tmp_path: Path):
    """#96 acceptance: regenerate embedding fields and packaging/transport noise
    do not change semantic snapshot_id."""
    from hotmem.snapshot.format import compute_semantic_snapshot_id

    base = {
        "id": "mem-noise-1",
        "identifier": "ops/noise",
        "fact_text": "Packaging noise invariance test.",
        "namespace": "ops",
        "created_at": "2026-10-30T00:00:00Z",
        "embedding": "AAAA",
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
    }
    different_embedding = {
        **base,
        "embedding": "ZZZZ9999==",
        "embedding_dim": 1536,
        "embedding_model": "foreign-embed-v9",
    }
    no_embedding = {
        k: v for k, v in base.items() if k not in ("embedding", "embedding_dim", "embedding_model")
    }

    id_base = compute_semantic_snapshot_id(
        [base],
        scope={
            "namespace": "ops",
            "caller_id": "caller-1",
            "session_id": "sess-1",
            "store_id": "store-1",
            "exported_count": 1,
            "omitted_count": 0,
        },
    )
    assert (
        compute_semantic_snapshot_id([different_embedding], scope={"namespace": "ops"}) == id_base
    )
    assert compute_semantic_snapshot_id([no_embedding], scope={"namespace": "ops"}) == id_base
    assert (
        compute_semantic_snapshot_id(
            [base],
            scope={
                "namespace": "ops",
                "caller_id": "caller-999",
                "session_id": "sess-999",
                "store_id": "store-999",
                "exported_count": 500,
                "omitted_count": 42,
            },
        )
        == id_base
    )


def test_semantic_snapshot_id_unicode_nfc_and_null_default_normalization():
    """#96 WP2/WP5: Unicode NFC/NFD equivalence and omitted/null default
    normalization produce identical semantic snapshot_id digests."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.snapshot.format import compute_semantic_snapshot_id

    nfc_text = "Caf\u00e9 r\u00e9sum\u00e9"
    nfd_text = "Cafe\u0301 re\u0301sume\u0301"
    assert nfc_text != nfd_text

    rec_nfc_explicit_defaults = {
        "id": "uni-1",
        "identifier": "ops/caf\u00e9",
        "fact_text": nfc_text,
        "fact_summary": nfc_text,
        "memory_type": "fact",
        "content_hash": compute_content_hash("ops/caf\u00e9", nfc_text),
        "namespace": "",
        "source": "snapshot",
        "importance": 0.0,
        "metadata": {"caf\u00e9": nfc_text},
        "provenance": None,
        "tags": ["tag:caf\u00e9"],
        "related_memories": [],
        "parent_memory": "",
        "ttl_seconds": None,
        "created_at": "2026-10-30T10:00:00Z",
        "updated_at": None,
        "tier": "hot",
        "promotion_state": "HOT",
        "promotion_candidate": 0,
        "source_uri": "./docs/../ops/caf\u00e9.md",
        "source_format": "",
        "source_checksum": "",
        "byte_offset": None,
        "byte_length": None,
        "snapshot_id": "",
        "schema_version": 2,
        "record_schema_version": 1,
    }
    rec_nfd_omitted_defaults = {
        "id": "uni-1",
        "identifier": "ops/cafe\u0301",
        "fact_text": nfd_text,
        "fact_summary": nfd_text,
        "memory_type": "inline",
        "content_hash": compute_content_hash("ops/cafe\u0301", nfd_text),
        "importance": -0.0,
        "metadata": {"cafe\u0301": nfd_text},
        "provenance": {},
        "tags": ["tag:cafe\u0301"],
        "ttl_seconds": 0,
        "created_at": "2026-10-30T10:00:00Z",
        "updated_at": "",
        "source_uri": "ops/cafe\u0301.md",
        "schema_version": 1,
    }
    assert compute_semantic_snapshot_id([rec_nfc_explicit_defaults]) == (
        compute_semantic_snapshot_id([rec_nfd_omitted_defaults])
    )


def test_semantic_snapshot_id_empty_and_duplicate_logical_records(tmp_path: Path):
    """#96 acceptance: empty snapshots and duplicate logical records have
    deterministic, order-independent snapshot_id behavior."""
    from hotmem.interchange.canonical import compute_content_hash
    from hotmem.snapshot import verify
    from hotmem.snapshot.format import (
        SNAPSHOT_ID_ALGORITHM,
        SNAPSHOT_ID_VERSION,
        compute_semantic_snapshot_id,
    )

    # Empty store snapshot export + verify + hydrate.
    empty_db = MemoryDB(tmp_path / "empty.sqlite")
    empty_snap = tmp_path / "empty_snap"
    try:
        snapshot(empty_db, empty_snap)
        summary = verify(empty_snap)
        assert summary["valid"] is True
        assert summary["record_count"] == 0
        assert summary["snapshot_id_version"] == SNAPSHOT_ID_VERSION
        assert summary["snapshot_id_algorithm"] == SNAPSHOT_ID_ALGORITHM
        assert summary["snapshot_id"] == compute_semantic_snapshot_id([])

        target_empty = MemoryDB(tmp_path / "target_empty.sqlite")
        try:
            res = hydrate(target_empty, empty_snap)
            assert res.loaded == 0
            assert target_empty.count() == 0
        finally:
            target_empty.close()
    finally:
        empty_db.close()

    # Duplicate logical records: same (namespace, content_hash).
    ch = compute_content_hash("dup/key", "Shared logical fact")
    dup_a = {
        "id": "dup-1",
        "identifier": "dup/key",
        "fact_text": "Shared logical fact",
        "content_hash": ch,
        "namespace": "ops",
        "tier": "hot",
        "created_at": "2026-10-30T01:00:00Z",
    }
    dup_b = {
        "id": "dup-2",
        "identifier": "dup/key",
        "fact_text": "Shared logical fact",
        "content_hash": ch,
        "namespace": "ops",
        "tier": "warm",
        "created_at": "2026-10-30T02:00:00Z",
    }

    single_id = compute_semantic_snapshot_id([dup_a])
    exact_dup_id = compute_semantic_snapshot_id([dup_a, dict(dup_a)])
    divergent_dup_ab = compute_semantic_snapshot_id([dup_a, dup_b])
    divergent_dup_ba = compute_semantic_snapshot_id([dup_b, dup_a])

    assert single_id != exact_dup_id
    assert exact_dup_id != divergent_dup_ab
    assert divergent_dup_ab == divergent_dup_ba


def test_manifest_identity_version_validation_and_cursor_compatibility(tmp_path: Path):
    """#96 WP3/WP4/WP5: malformed or unsupported snapshot_id_version /
    snapshot_id_algorithm fails closed, while #94 hmc1 v1 cursors remain
    compatible across legacy v1 and semantic v2 snapshot bases."""
    from hotmem.events import (
        _build_memory_created_payload,
        append_event,
        issue_replay_cursor,
        replay_with_cursor,
    )
    from hotmem.snapshot import verify
    from hotmem.snapshot.format import (
        LEGACY_SNAPSHOT_ID_ALGORITHM,
        LEGACY_SNAPSHOT_ID_VERSION,
        SNAPSHOT_ID_ALGORITHM,
        SNAPSHOT_ID_VERSION,
        compute_legacy_snapshot_id,
    )
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "ver_src.sqlite")
    snap_dir = tmp_path / "ver_snap"
    try:
        add_memory(src, "ver/one", "Versioned snapshot identity test")
        row = src.all_rows(include_embedding=True)[0]
        append_event(
            src,
            event_type="memory.created",
            memory_id=row["id"],
            namespace=row.get("namespace") or "",
            payload=_build_memory_created_payload(row),
        )
        snapshot(src, snap_dir)
        summary = verify(snap_dir)
        assert summary["snapshot_id_version"] == SNAPSHOT_ID_VERSION
        assert summary["snapshot_id_algorithm"] == SNAPSHOT_ID_ALGORITHM

        manifest_path = snap_dir / MANIFEST_NAME
        orig_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Malformed / unsupported version and algorithm cases fail closed.
        bad_cases = [
            {"snapshot_id_version": 0},
            {"snapshot_id_version": 3},
            {"snapshot_id_version": 99},
            {"snapshot_id_version": True},
            {"snapshot_id_version": "2"},
            {"snapshot_id_algorithm": "unknown-alg"},
            {"snapshot_id_algorithm": 123},
            {
                "snapshot_id_version": SNAPSHOT_ID_VERSION,
                "snapshot_id_algorithm": LEGACY_SNAPSHOT_ID_ALGORITHM,
            },
            {
                "snapshot_id_version": LEGACY_SNAPSHOT_ID_VERSION,
                "snapshot_id_algorithm": SNAPSHOT_ID_ALGORITHM,
            },
        ]
        for patch in bad_cases:
            bad_man = {**orig_manifest, **patch}
            manifest_path.write_text(json.dumps(bad_man), encoding="utf-8")
            with pytest.raises(SnapshotChecksumError) as exc_bad:
                verify_manifest(snap_dir)
            assert exc_bad.value.reason == "malformed"

        # Restore valid v2 manifest and hydrate clone_v2; also hydrate clone_v1
        # from a legacy v1 manifest, then replay a new event into both via hmc1 v1 cursors.
        manifest_path.write_text(
            json.dumps(orig_manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        clone_v2 = MemoryDB(tmp_path / "clone_cursor_v2.sqlite")
        clone_v1 = MemoryDB(tmp_path / "clone_cursor_v1.sqlite")
        try:
            hydrate(clone_v2, snap_dir)

            legacy_man = {
                **orig_manifest,
                "snapshot_id": compute_legacy_snapshot_id([row["content_hash"]]),
                "snapshot_id_version": LEGACY_SNAPSHOT_ID_VERSION,
                "snapshot_id_algorithm": LEGACY_SNAPSHOT_ID_ALGORITHM,
            }
            manifest_path.write_text(
                json.dumps(legacy_man, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            summary_v1 = verify(snap_dir)
            assert summary_v1["snapshot_id_version"] == LEGACY_SNAPSHOT_ID_VERSION
            assert summary_v1["snapshot_id_algorithm"] == LEGACY_SNAPSHOT_ID_ALGORITHM
            hydrate(clone_v1, snap_dir)

            # Emit a second event on src and replay into both clones, including an
            # explicit hmc1 v1 cursor carrying the legacy v1 base_snapshot_id.
            add_memory(src, "ver/two", "Event emitted after snapshot export")
            row2 = src.all_rows(include_embedding=True)[-1]
            append_event(
                src,
                event_type="memory.created",
                memory_id=row2["id"],
                namespace=row2.get("namespace") or "",
                payload=_build_memory_created_payload(row2),
            )

            res_v2 = replay_with_cursor(src, clone_v2)
            assert res_v2.applied == 1 and clone_v2.count() == 2

            legacy_cursor = issue_replay_cursor(
                src,
                position="seq",
                seq=1,
                base_snapshot_id=legacy_man["snapshot_id"],
            ).encode()
            res_v1 = replay_with_cursor(src, clone_v1, cursor=legacy_cursor)
            assert res_v1.applied == 1 and clone_v1.count() == 2
        finally:
            clone_v2.close()
            clone_v1.close()
    finally:
        src.close()


def test_semantic_projection_is_total_for_malformed_lifecycle_fields():
    """#96 review: projection must never raise on adversarial lifecycle values."""
    from hotmem.snapshot.format import semantic_record_projection

    base = {"id": "tot-1", "identifier": "ops/total", "fact_text": "Projection totality."}
    for candidate in ("abc", {"a": 1}, [1], 2.5, None, "", False):
        proj = semantic_record_projection({**base, "promotion_candidate": candidate})
        assert proj["promotion_candidate"] in (0, 1)
    assert (
        semantic_record_projection({**base, "promotion_candidate": True})["promotion_candidate"]
        == 1
    )
    assert (
        semantic_record_projection({**base, "promotion_candidate": 1})["promotion_candidate"] == 1
    )


def test_verify_manifest_maps_unexpected_projection_errors_to_malformed(
    tmp_path: Path, monkeypatch
):
    """#96 review: verifier fails closed instead of leaking ValueError/TypeError."""
    import hotmem.snapshot.reader as v2_reader

    db = MemoryDB(tmp_path / "failclosed.sqlite")
    try:
        db.insert(
            id="fc-1",
            identifier="ops/failclosed",
            fact_text="Fail closed.",
            embedding=pack_embedding(embed_text("Fail closed.")),
            embedding_dim=EMBEDDING_DIM,
            embedding_model=EMBEDDING_MODEL,
            source="agent://test",
            importance=0.5,
            metadata_json="{}",
            content_hash=hashlib.sha256(b"ops/failclosed:Fail closed.").hexdigest(),
            created_at="2026-10-30T10:00:00Z",
            namespace="ops",
            tier="hot",
            promotion_state="HOT",
            promotion_candidate=0,
        )
        snap_dir = tmp_path / "failclosed_snap"
        snapshot(db, snap_dir)
    finally:
        db.close()

    def boom(*args, **kwargs):
        raise ValueError("simulated coercion failure")

    monkeypatch.setattr(v2_reader, "semantic_record_digest", boom)
    with pytest.raises(SnapshotChecksumError) as exc:
        verify_manifest(snap_dir)
    assert exc.value.reason == "malformed"
    assert exc.value.file == MEMORIES_NAME


def test_verify_manifest_handles_uncoercible_promotion_candidate(tmp_path: Path):
    """#96 review: a re-sealed manifest with `promotion_candidate: "abc"` must
    verify deterministically and hydrate as an invalid record, never crash."""
    from hotmem.interchange.fingerprint import state_fingerprint_from_list
    from hotmem.snapshot.format import compute_overall, compute_semantic_snapshot_id, sha256_file
    from hotmem.swap import add_memory

    src = MemoryDB(tmp_path / "malformed_candidate.sqlite")
    snap_dir = tmp_path / "malformed_candidate_snap"
    try:
        add_memory(src, "ops/candidate", "Malformed promotion candidate.")
        snapshot(src, snap_dir)
    finally:
        src.close()

    mem_path = snap_dir / MEMORIES_NAME
    record = json.loads(mem_path.read_text(encoding="utf-8").strip())
    record["promotion_candidate"] = "abc"
    mem_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    manifest_path = snap_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_sha = sha256_file(mem_path)
    manifest["files"][MEMORIES_NAME] = {"size": mem_path.stat().st_size, "sha256": new_sha}
    manifest["overall_sha256"] = compute_overall({MEMORIES_NAME: new_sha})
    manifest["snapshot_id"] = compute_semantic_snapshot_id(
        [record],
        file_backed_references=[],
        scope=manifest.get("scope"),
    )
    # The record is invalid, so it contributes no state fingerprint; re-seal the
    # metadata the way a producer of this (invalid) payload would.
    manifest["state_fingerprint"] = state_fingerprint_from_list([])
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    assert verify_manifest(snap_dir).snapshot_id == manifest["snapshot_id"]

    dst = MemoryDB(tmp_path / "malformed_candidate_dst.sqlite")
    try:
        res = hydrate(dst, snap_dir)
        assert res.loaded == 0
        assert res.invalid == 1
        assert dst.count() == 0
    finally:
        dst.close()


def test_semantic_snapshot_id_is_independent_of_attachment_bundling(tmp_path: Path):
    """#96 review: bundling byte ranges (packaging) must not change semantic
    identity; only referenced-byte state does."""
    from hotmem.snapshot.format import compute_semantic_snapshot_id

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = workspace / "slice.bin"
    data.write_bytes(b"0123456789" * 10)

    db = MemoryDB(tmp_path / "bundle.sqlite")
    try:
        add_file_backed(
            db,
            "bundle/slice",
            FileRef(
                source_uri=str(data),
                byte_offset=0,
                byte_length=50,
                source_format="bin",
            ),
            summary="bundled slice",
        )
        plain_dir = tmp_path / "bundle_plain"
        attached_dir = tmp_path / "bundle_attached"
        snapshot(db, plain_dir, base_dir=workspace)
        snapshot(db, attached_dir, copy_attachments=True, base_dir=workspace)
    finally:
        db.close()

    plain = verify_manifest(plain_dir)
    attached = verify_manifest(attached_dir)
    assert attached.file_backed_references[0].attachment is not None
    assert plain.file_backed_references[0].attachment is None
    assert plain.snapshot_id == attached.snapshot_id
    assert plain.snapshot_id == compute_semantic_snapshot_id(
        [json.loads((plain_dir / MEMORIES_NAME).read_text(encoding="utf-8").strip())],
        file_backed_references=plain.file_backed_references,
        scope=plain.scope,
    )
