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
