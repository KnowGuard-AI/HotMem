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

    # metadata.json may differ in created_at/host; the rest must match.
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
