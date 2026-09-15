"""hotmem-interchange-v1 package writer tests (#69).

Determinism (payload bytes, logical identity across compression/order),
atomic publish, canonical serialization, and full field round-trip.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.package import (
    FORMAT_ID,
    MANIFEST_NAME,
    PAYLOAD_GZ,
    PAYLOAD_PLAIN,
    write_package,
)


@pytest.fixture
def src_db(tmp_path: Path) -> MemoryDB:
    db = MemoryDB(tmp_path / "src.sqlite")
    db.insert(
        id="m1",
        identifier="vendor_x",
        fact_text="acme ships quarterly",
        embedding=pack_embedding(embed_text("acme ships quarterly")),
        content_hash=compute_content_hash("vendor_x", "acme ships quarterly"),
        namespace="team",
        tier="hot",
        tags='["biz"]',
        ttl_seconds=3600,
        fact_summary="acme cadence",
        provenance_json=json.dumps({"origin": "erp"}),
    )
    db.insert(
        id="m2",
        identifier="ops",
        fact_text="deploys happen on tuesdays",
        embedding=pack_embedding(embed_text("deploys happen on tuesdays")),
        content_hash=compute_content_hash("ops", "deploys happen on tuesdays"),
    )
    return db


def test_package_layout_and_manifest(src_db: MemoryDB, tmp_path: Path):
    out = tmp_path / "pkg"
    result = write_package(src_db, out)

    assert (out / MANIFEST_NAME).is_file()
    payload = out / PAYLOAD_PLAIN
    assert payload.is_file()
    manifest = json.loads((out / MANIFEST_NAME).read_text())

    assert manifest["format"] == FORMAT_ID
    assert manifest["schema_version"] == 1
    assert manifest["record_count"] == 2
    assert manifest["source"]["kind"] == "hotmem-dump"
    assert manifest["embedding"]["model"] == "hotmem-hash-v1"
    assert manifest["embedding"]["dim"] == 64
    assert "created_at" in manifest  # informational
    assert "hotmem_version" in manifest

    # File digests verify.
    entry = manifest["files"][PAYLOAD_PLAIN]
    assert entry["size"] == payload.stat().st_size
    assert entry["sha256"] == hashlib.sha256(payload.read_bytes()).hexdigest()

    # logical_id = sha256 of sorted content-hash concatenation.
    lines = payload.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    hashes = sorted(r["content_hash"] for r in records)
    expected = hashlib.sha256("".join(hashes).encode()).hexdigest()
    assert manifest["logical_id"] == expected
    assert result.logical_id == expected
    assert result.exported == 2


def test_package_payload_is_canonical_and_sorted(src_db: MemoryDB, tmp_path: Path):
    out = tmp_path / "pkg"
    write_package(src_db, out)
    payload = out / PAYLOAD_PLAIN
    lines = payload.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["id"] for r in records] == ["m1", "m2"]
    for line, rec in zip(lines, records, strict=True):
        assert line == json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # Full field round-trip in the record.
    m1 = records[0]
    assert m1["namespace"] == "team"
    assert m1["tags"] == ["biz"]
    assert m1["ttl_seconds"] == 3600
    assert m1["provenance"] == {"origin": "erp"}
    assert m1["fact_summary"] == "acme cadence"
    assert len(m1["embedding"]) > 0


def test_package_plain_vs_gz_share_logical_identity(src_db: MemoryDB, tmp_path: Path):
    plain = tmp_path / "pkg_plain"
    gz = tmp_path / "pkg_gz"
    write_package(src_db, plain)
    write_package(src_db, gz, gz=True)

    m_plain = json.loads((plain / MANIFEST_NAME).read_text())
    m_gz = json.loads((gz / MANIFEST_NAME).read_text())
    # Contract §3: compression never changes logical identity.
    assert m_plain["logical_id"] == m_gz["logical_id"]

    # GZ entry carries the decompressed digest; decompressing matches it and
    # the payload bytes.
    entry = m_gz["files"][PAYLOAD_GZ]
    raw = (gz / PAYLOAD_GZ).read_bytes()
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    decompressed = gzip.decompress(raw)
    assert entry["decompressed_sha256"] == hashlib.sha256(decompressed).hexdigest()
    assert decompressed == (plain / PAYLOAD_PLAIN).read_bytes()

    # GZ transport bytes are byte-stable across runs (mtime=0, no filename).
    gz2 = tmp_path / "pkg_gz2"
    write_package(src_db, gz2, gz=True)
    assert (gz2 / PAYLOAD_GZ).read_bytes() == raw


def test_package_payload_bytes_deterministic(src_db: MemoryDB, tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    write_package(src_db, a)
    write_package(src_db, b)
    assert (a / PAYLOAD_PLAIN).read_bytes() == (b / PAYLOAD_PLAIN).read_bytes()
    ma = json.loads((a / MANIFEST_NAME).read_text())
    mb = json.loads((b / MANIFEST_NAME).read_text())
    assert ma["logical_id"] == mb["logical_id"]
    assert ma["files"][PAYLOAD_PLAIN]["sha256"] == mb["files"][PAYLOAD_PLAIN]["sha256"]


def test_package_insert_order_does_not_change_identity(src_db: MemoryDB, tmp_path: Path):
    """Equivalent contents ⇒ same logical_id even with different row ids."""
    other = MemoryDB(tmp_path / "other.sqlite")
    # Insert in reverse order; ids differ but content hashes match.
    other.insert(
        id="zz2",
        identifier="ops",
        fact_text="deploys happen on tuesdays",
        embedding=pack_embedding(embed_text("deploys happen on tuesdays")),
        content_hash=compute_content_hash("ops", "deploys happen on tuesdays"),
    )
    other.insert(
        id="zz1",
        identifier="vendor_x",
        fact_text="acme ships quarterly",
        embedding=pack_embedding(embed_text("acme ships quarterly")),
        content_hash=compute_content_hash("vendor_x", "acme ships quarterly"),
    )
    out = tmp_path / "pkg"
    write_package(other, out)
    m = json.loads((out / MANIFEST_NAME).read_text())

    ref = tmp_path / "ref"
    write_package(src_db, ref)
    m_ref = json.loads((ref / MANIFEST_NAME).read_text())
    assert m["logical_id"] == m_ref["logical_id"]
    other.close()


def test_package_publish_is_atomic(src_db: MemoryDB, tmp_path: Path):
    """A publish over an existing package leaves no staging dirs; failed
    publishes never destroy the previous package."""
    out = tmp_path / "pkg"
    write_package(src_db, out)
    before = (out / PAYLOAD_PLAIN).read_bytes()
    write_package(src_db, out)  # overwrite via atomic swap
    assert (out / PAYLOAD_PLAIN).read_bytes() == before
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".pkg")]
    assert leftovers == []

    # A mid-write failure restores the previous package.
    class BoomDB(MemoryDB):
        def iter_rows(self, **kwargs):
            yield from super().iter_rows(**kwargs)
            raise RuntimeError("disk full")

    boom = BoomDB(tmp_path / "boom.sqlite")
    boom.insert(
        id="x",
        identifier="x",
        fact_text="x",
        embedding=pack_embedding(embed_text("x")),
        content_hash="h" * 64,
    )
    with pytest.raises(RuntimeError, match="disk full"):
        write_package(boom, out)
    assert (out / PAYLOAD_PLAIN).read_bytes() == before
    assert json.loads((out / MANIFEST_NAME).read_text())["record_count"] == 2
    boom.close()


def test_package_empty_db(tmp_db: MemoryDB, tmp_path: Path):
    out = tmp_path / "empty"
    result = write_package(tmp_db, out)
    assert result.exported == 0
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["record_count"] == 0
    assert (out / PAYLOAD_PLAIN).read_text() == ""


def test_package_roundtrip_preserves_promotion_state(src_db: MemoryDB, tmp_path: Path):
    """Promotion state is canonical access state (#73 amendment): an archived
    memory must survive clone -> restore as ARCHIVED, not be silently
    resurrected to HOT."""
    src_db.update_promotion_state("m1", "ARCHIVED")
    out = tmp_path / "pkg"
    write_package(src_db, out)
    records = [json.loads(line) for line in (out / PAYLOAD_PLAIN).read_text().splitlines()]
    record = next(r for r in records if r["id"] == "m1")
    assert record["promotion_state"] == "ARCHIVED"

    target = MemoryDB(tmp_path / "fresh.sqlite")
    from hotmem.interchange.hydrate import hydrate_package

    hydrate_package(target, out)
    row = next(r for r in target.all_rows() if r["id"] == "m1")
    assert row["promotion_state"] == "ARCHIVED"
    target.close()
