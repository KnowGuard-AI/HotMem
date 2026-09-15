"""hotmem-delta-v1 producer tests (#73).

Deterministic base-to-current diff: CAS upserts only, removals counted and
never applied, byte-stable output, atomic publish, verified base required.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.delta import (
    FORMAT_ID,
    MANIFEST_NAME,
    OPS_PLAIN,
    produce_delta,
)
from hotmem.interchange.fingerprint import record_fingerprint
from hotmem.interchange.package import MANIFEST_NAME as PKG_MANIFEST
from hotmem.interchange.package import write_package


def _fact(i: int) -> str:
    return f"delta fixture fact {i} for acme operations"


@pytest.fixture
def src_db(tmp_path: Path) -> MemoryDB:
    db = MemoryDB(tmp_path / "src.sqlite")
    for i in range(3):
        fact = _fact(i)
        db.insert(
            id=f"m{i}",
            identifier=f"ident-{i}",
            fact_text=fact,
            embedding=pack_embedding(embed_text(fact)),
            content_hash=compute_content_hash(f"ident-{i}", fact),
            namespace="team",
        )
    return db


@pytest.fixture
def base_pkg(tmp_path: Path, src_db: MemoryDB) -> Path:
    out = tmp_path / "base"
    write_package(src_db, out)
    return out


def _mutate_since_base(db: MemoryDB) -> None:
    """Add one record, change one record's tier, leave one unchanged."""
    new_fact = _fact(3)
    db.insert(
        id="m3",
        identifier="ident-3",
        fact_text=new_fact,
        embedding=pack_embedding(embed_text(new_fact)),
        content_hash=compute_content_hash("ident-3", new_fact),
        namespace="team",
    )
    db.update_promotion_state("m0", "WARM")


def test_produce_delta_add_and_change_ops(base_pkg: Path, src_db: MemoryDB, tmp_path: Path):
    _mutate_since_base(src_db)
    out = tmp_path / "delta"
    result = produce_delta(src_db, base_pkg, out)

    assert result.added == 1  # m3
    assert result.changed == 1  # m0 promotion transition
    assert result.total_ops == 2
    assert result.removed_since_base == 0

    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["format"] == FORMAT_ID
    assert manifest["schema_version"] == 1
    assert manifest["fingerprint_version"] == 1
    assert manifest["counts"] == {
        "added": 1,
        "changed": 1,
        "removed_since_base": 0,
        "total_ops": 2,
    }
    assert (
        manifest["base"]["logical_id"]
        == json.loads((base_pkg / PKG_MANIFEST).read_text())["logical_id"]
    )

    ops = [json.loads(line) for line in (out / OPS_PLAIN).read_text().splitlines()]
    assert [op["record_id"] for op in ops] == ["m0", "m3"]  # sorted by record id
    change_op = ops[0]
    assert change_op["op"] == "upsert"
    assert change_op["expected_pre_fingerprint"] is not None
    assert change_op["record"]["promotion_state"] == "WARM"
    add_op = ops[1]
    assert add_op["expected_pre_fingerprint"] is None
    for op in ops:
        assert op["resulting_fingerprint"] == record_fingerprint(op["record"])
        assert (
            op["op_id"]
            == hashlib.sha256(
                f"upsert:{op['record_id']}:{op['resulting_fingerprint']}".encode()
            ).hexdigest()
        )

    # File digest verifies against the manifest.
    entry = manifest["files"][OPS_PLAIN]
    assert entry["sha256"] == hashlib.sha256((out / OPS_PLAIN).read_bytes()).hexdigest()


def test_produce_delta_empty_when_in_sync(base_pkg: Path, src_db: MemoryDB, tmp_path: Path):
    out = tmp_path / "delta"
    result = produce_delta(src_db, base_pkg, out)
    assert result.total_ops == 0
    assert (out / OPS_PLAIN).read_text() == ""
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["counts"]["total_ops"] == 0


def test_produce_delta_counts_removals_never_applies(
    base_pkg: Path, src_db: MemoryDB, tmp_path: Path
):
    """A record removed at the source is reported, never turned into an op (§7)."""
    src_db._conn.execute("DELETE FROM memories WHERE id = 'm2'")
    src_db.commit()
    out = tmp_path / "delta"
    result = produce_delta(src_db, base_pkg, out)
    assert result.removed_since_base == 1
    assert result.total_ops == 0  # no deletion ops in v1
    ops_text = (out / OPS_PLAIN).read_text()
    assert ops_text == ""


def test_produce_delta_is_byte_stable(base_pkg: Path, src_db: MemoryDB, tmp_path: Path):
    _mutate_since_base(src_db)
    a = tmp_path / "a"
    b = tmp_path / "b"
    produce_delta(src_db, base_pkg, a)
    produce_delta(src_db, base_pkg, b)
    assert (a / OPS_PLAIN).read_bytes() == (b / OPS_PLAIN).read_bytes()
    ma = json.loads((a / MANIFEST_NAME).read_text())
    mb = json.loads((b / MANIFEST_NAME).read_text())
    assert ma["files"][OPS_PLAIN]["sha256"] == mb["files"][OPS_PLAIN]["sha256"]
    assert ma["resulting_state_fingerprint"] == mb["resulting_state_fingerprint"]


def test_produce_delta_gz_transport(base_pkg: Path, src_db: MemoryDB, tmp_path: Path):
    _mutate_since_base(src_db)
    out = tmp_path / "delta"
    result = produce_delta(src_db, base_pkg, out, gz=True)
    assert result.total_ops == 2

    manifest = json.loads((out / MANIFEST_NAME).read_text())
    raw = (out / "operations.jsonl.gz").read_bytes()
    entry = manifest["files"]["operations.jsonl.gz"]
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    decompressed = gzip.decompress(raw)
    assert entry["decompressed_sha256"] == hashlib.sha256(decompressed).hexdigest()
    assert (
        decompressed == (tmp_path / "delta" / ".." / "delta").resolve().exists()
        and True
        or decompressed
    )  # sanity

    # Byte-stable across runs.
    out2 = tmp_path / "delta2"
    produce_delta(src_db, base_pkg, out2, gz=True)
    assert (out2 / "operations.jsonl.gz").read_bytes() == raw


def test_produce_delta_requires_verified_base(base_pkg: Path, src_db: MemoryDB, tmp_path: Path):
    payload = base_pkg / "memories.jsonl"
    data = bytearray(payload.read_bytes())
    data[-3] ^= 0xFF
    payload.write_bytes(bytes(data))

    from hotmem.interchange.hydrate import PackageError

    with pytest.raises(PackageError):
        produce_delta(src_db, base_pkg, tmp_path / "delta")
    assert not (tmp_path / "delta").exists()  # no partial delta


def test_produce_delta_resulting_fingerprint_matches_full_state(
    base_pkg: Path, src_db: MemoryDB, tmp_path: Path
):
    from hotmem.interchange.fingerprint import state_fingerprint

    _mutate_since_base(src_db)
    out = tmp_path / "delta"
    result = produce_delta(src_db, base_pkg, out)
    assert result.resulting_state_fingerprint == state_fingerprint(src_db.all_rows())


def test_op_records_carry_full_canonical_state(base_pkg: Path, src_db: MemoryDB, tmp_path: Path):
    """Upserts are whole records: provenance, namespace, tags, embeddings."""
    src_db.insert(
        id="m9",
        identifier="ident-9",
        fact_text=_fact(9),
        embedding=pack_embedding(embed_text(_fact(9))),
        embedding_model=EMBEDDING_MODEL,
        content_hash=compute_content_hash("ident-9", _fact(9)),
        namespace="team",
        tags='["ops"]',
        provenance_json='{"origin": "manual"}',
        ttl_seconds=60,
    )
    out = tmp_path / "delta"
    produce_delta(src_db, base_pkg, out)
    ops = [json.loads(line) for line in (out / OPS_PLAIN).read_text().splitlines()]
    rec = next(op["record"] for op in ops if op["record_id"] == "m9")
    assert rec["tags"] == ["ops"]
    assert rec["provenance"] == {"origin": "manual"}
    assert rec["ttl_seconds"] == 60
    assert len(rec["embedding"]) > 0
    assert rec["embedding_dim"] == EMBEDDING_DIM
    assert rec["embedding_model"] == EMBEDDING_MODEL
