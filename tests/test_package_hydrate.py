"""hotmem-interchange-v1 verify + transactional restore tests (#69).

The #69 acceptance core: verification before writes, all-or-nothing restore,
target unchanged on every failure mode, embedding reuse/re-embed/predictable
invalids, idempotent re-hydrate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hotmem.db import MemoryDB
from hotmem.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_text, pack_embedding
from hotmem.interchange.canonical import canonical_line, compute_content_hash
from hotmem.interchange.hydrate import PackageError, hydrate_package, verify_package
from hotmem.interchange.package import MANIFEST_NAME, PAYLOAD_GZ, PAYLOAD_PLAIN, write_package


def _make_db(tmp_path: Path, n: int = 3) -> MemoryDB:
    db = MemoryDB(tmp_path / "src.sqlite")
    for i in range(n):
        fact = f"fact number {i} about acme operations"
        db.insert(
            id=f"m{i}",
            identifier=f"ident-{i}",
            fact_text=fact,
            embedding=pack_embedding(embed_text(fact)),
            content_hash=compute_content_hash(f"ident-{i}", fact),
            namespace="ns",
            tier="hot",
            tags='["t"]',
        )
    return db


def _fingerprint(db: MemoryDB) -> tuple:
    """Full observable target state: every row, every column, embedding bytes."""
    rows = db.all_rows(include_embedding=True)
    return len(rows), tuple(
        (r["id"], r["content_hash"], bytes(r["embedding"] or b""))
        for r in sorted(rows, key=lambda r: r["id"])
    )


@pytest.fixture
def src_db(tmp_path: Path) -> MemoryDB:
    return _make_db(tmp_path)


@pytest.fixture
def pkg(tmp_path: Path, src_db: MemoryDB) -> Path:
    out = tmp_path / "pkg"
    write_package(src_db, out)
    return out


def _corrupt(path: Path, offset: int = -64) -> None:
    data = bytearray(path.read_bytes())
    data[offset] ^= 0xFF
    path.write_bytes(bytes(data))


def _truncate(path: Path, cut: int = 7) -> None:
    data = path.read_bytes()
    path.write_bytes(data[:-cut])


# ── verification ────────────────────────────────────────────────────────────


def test_verify_ok_plain_and_gz(tmp_path: Path, src_db: MemoryDB):
    plain = tmp_path / "p"
    gz = tmp_path / "g"
    write_package(src_db, plain)
    write_package(src_db, gz, gz=True)
    vp = verify_package(plain)
    vg = verify_package(gz)
    assert vp.record_count == vg.record_count == 3
    vp.cleanup()
    vg.cleanup()


def test_verify_rejects_bitflip_in_payload(pkg: Path):
    _corrupt(pkg / PAYLOAD_PLAIN)
    with pytest.raises(PackageError, match="digest_mismatch"):
        verify_package(pkg)


def test_verify_rejects_truncated_payload(pkg: Path):
    _truncate(pkg / PAYLOAD_PLAIN)
    with pytest.raises(PackageError):
        verify_package(pkg)


def test_verify_rejects_missing_payload(pkg: Path):
    (pkg / PAYLOAD_PLAIN).unlink()
    with pytest.raises(PackageError, match="missing_file"):
        verify_package(pkg)


def test_verify_rejects_size_mismatch(pkg: Path):
    (pkg / PAYLOAD_PLAIN).write_text("{}\n")  # replace with tiny valid file
    with pytest.raises(PackageError):
        verify_package(pkg)


def test_verify_rejects_record_count_mismatch(pkg: Path):
    # Append one more valid line; update size/digest so the failure is
    # precisely the stale record_count.
    line = canonical_line(
        {
            "schema_version": 1,
            "id": "extra",
            "identifier": "x",
            "fact_text": "y",
            "content_hash": compute_content_hash("x", "y"),
            "memory_type": "fact",
        }
    )
    payload = (pkg / PAYLOAD_PLAIN).read_bytes() + line.encode()
    (pkg / PAYLOAD_PLAIN).write_bytes(payload)
    manifest = json.loads((pkg / MANIFEST_NAME).read_text())
    manifest["files"][PAYLOAD_PLAIN] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (pkg / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="record_count_mismatch"):
        verify_package(pkg)


def test_verify_rejects_path_escape_manifest(tmp_path: Path, src_db: MemoryDB):
    out = tmp_path / "pkg"
    write_package(src_db, out)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    manifest["files"]["../outside.bin"] = {
        "size": outside.stat().st_size,
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="path_escape"):
        verify_package(out)
    # The outside file was never read: content intact and unreadable secret
    # unchanged (proven by successful verification failing BEFORE reads of
    # unconfined paths).


def test_verify_rejects_symlinked_payload(pkg: Path, tmp_path: Path):
    (tmp_path / "evil.jsonl").write_text("x" * 10)
    (pkg / PAYLOAD_PLAIN).unlink()
    (pkg / PAYLOAD_PLAIN).symlink_to(tmp_path / "evil.jsonl")
    with pytest.raises(PackageError):
        verify_package(pkg)


def test_verify_rejects_future_schema(pkg: Path):
    manifest = json.loads((pkg / MANIFEST_NAME).read_text())
    manifest["schema_version"] = 99
    (pkg / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="unsupported_schema"):
        verify_package(pkg)


def test_verify_rejects_wrong_format(pkg: Path):
    manifest = json.loads((pkg / MANIFEST_NAME).read_text())
    manifest["format"] = "hotmem-snapshot-v2"
    (pkg / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="unsupported_format"):
        verify_package(pkg)


def test_verify_gz_rejects_corrupt_compression(tmp_path: Path, src_db: MemoryDB):
    out = tmp_path / "gz"
    write_package(src_db, out, gz=True)
    raw = bytearray((out / PAYLOAD_GZ).read_bytes())
    raw[10] ^= 0xFF
    (out / PAYLOAD_GZ).write_bytes(bytes(raw))
    with pytest.raises(PackageError):
        verify_package(out)


# ── transactional restore ──────────────────────────────────────────────────


def test_restore_into_clean_instance(tmp_path: Path, pkg: Path):
    target = MemoryDB(tmp_path / "clean.sqlite")
    result = hydrate_package(target, pkg)
    assert result.loaded == 3
    assert result.skipped_dupes == 0
    assert result.invalid == 0
    rows = target.all_rows()
    assert {r["namespace"] for r in rows} == {"ns"}
    assert {r["tier"] for r in rows} == {"hot"}
    target.close()


def test_restore_is_idempotent(tmp_path: Path, pkg: Path):
    target = MemoryDB(tmp_path / "clean.sqlite")
    hydrate_package(target, pkg)
    fp = _fingerprint(target)
    second = hydrate_package(target, pkg)
    assert second.loaded == 0
    assert second.skipped_dupes == 3
    assert _fingerprint(target) == fp  # zero new records, zero changes
    target.close()


def test_restore_gz_matches_plain(tmp_path: Path, src_db: MemoryDB):
    plain = tmp_path / "p"
    gz = tmp_path / "g"
    write_package(src_db, plain)
    write_package(src_db, gz, gz=True)
    t1 = MemoryDB(tmp_path / "t1.sqlite")
    t2 = MemoryDB(tmp_path / "t2.sqlite")
    hydrate_package(t1, plain)
    hydrate_package(t2, gz)
    assert _fingerprint(t1) == _fingerprint(t2)
    t1.close()
    t2.close()


def test_restore_reuses_compatible_embeddings_zero_embed_calls(
    tmp_path: Path, pkg: Path, monkeypatch: pytest.MonkeyPatch
):
    """#69 acceptance: compatible embeddings reused, none re-computed."""
    calls: list[str] = []
    original = embed_text

    def spy(text: str):
        calls.append(text)
        return original(text)

    monkeypatch.setattr("hotmem.interchange.compat.embed_text", spy)
    target = MemoryDB(tmp_path / "clean.sqlite")
    result = hydrate_package(target, pkg)
    assert result.loaded == 3
    assert calls == []  # zero embedding calls
    target.close()


def test_restore_reembeds_incompatible_embeddings(tmp_path: Path, src_db: MemoryDB):
    out = tmp_path / "pkg"
    write_package(src_db, out)
    # Rewrite payload with a foreign model so compat rejects the stored blob.
    lines = []
    for line in (out / PAYLOAD_PLAIN).read_text().splitlines():
        rec = json.loads(line)
        rec["embedding_model"] = "foreign-v9"
        lines.append(canonical_line(rec).rstrip("\n"))
    payload = ("\n".join(lines) + "\n").encode()
    (out / PAYLOAD_PLAIN).write_bytes(payload)
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    manifest["files"][PAYLOAD_PLAIN] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest))

    target = MemoryDB(tmp_path / "clean.sqlite")
    result = hydrate_package(target, out)
    assert result.loaded == 3
    rows = target.all_rows(include_embedding=True)
    assert all(r["embedding_model"] == EMBEDDING_MODEL for r in rows)
    assert all(len(bytes(r["embedding"] or b"")) == EMBEDDING_DIM * 4 for r in rows)
    target.close()


def test_restore_reports_invalid_records_predictably(tmp_path: Path, src_db: MemoryDB):
    out = tmp_path / "pkg"
    write_package(src_db, out)
    extra = canonical_line(
        {
            "schema_version": 1,
            "id": "no-text",
            "identifier": "z",
            "fact_text": "",
            "memory_type": "fact",
            "content_hash": compute_content_hash("z", ""),
        }
    ).rstrip("\n")
    payload = (out / PAYLOAD_PLAIN).read_text() + extra + "\n"
    payload_bytes = payload.encode()
    (out / PAYLOAD_PLAIN).write_bytes(payload_bytes)
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    manifest["record_count"] += 1
    manifest["files"][PAYLOAD_PLAIN] = {
        "size": len(payload_bytes),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
    manifest["logical_id"] = hashlib.sha256(
        "".join(
            sorted([json.loads(line)["content_hash"] for line in payload.splitlines()])
        ).encode()
    ).hexdigest()
    (out / MANIFEST_NAME).write_text(json.dumps(manifest))

    target = MemoryDB(tmp_path / "clean.sqlite")
    result = hydrate_package(target, out)
    assert result.loaded == 3
    assert result.invalid == 1
    assert target.count() == 3  # invalid record never stored
    target.close()


def test_failed_restore_leaves_target_unchanged(tmp_path: Path, pkg: Path):
    """#69 acceptance: integrity failures halt hydration; target unchanged."""
    target = MemoryDB(tmp_path / "clean.sqlite")
    seed_fact = "preexisting memory in target"
    target.insert(
        id="seed",
        identifier="seed",
        fact_text=seed_fact,
        embedding=pack_embedding(embed_text(seed_fact)),
        content_hash=compute_content_hash("seed", seed_fact),
    )
    before = _fingerprint(target)

    import shutil

    def variant(name: str, break_it) -> Path:
        backup = pkg.with_name(f"pkg-{name}")
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(pkg, backup)
        break_it(backup)
        return backup

    for name, break_it in [
        ("bitflip", lambda b: _corrupt(b / PAYLOAD_PLAIN)),
        ("truncate", lambda b: _truncate(b / PAYLOAD_PLAIN)),
        ("missing", lambda b: (b / PAYLOAD_PLAIN).unlink()),
    ]:
        backup = variant(name, break_it)
        with pytest.raises(PackageError):
            hydrate_package(target, backup)
        assert _fingerprint(target) == before, f"{name} must not touch the target"

    target.close()


def test_midstream_corruption_rolls_back_everything(tmp_path: Path, src_db: MemoryDB):
    """Digest-verified but structurally broken line: full rollback (§7)."""
    out = tmp_path / "pkg"
    write_package(src_db, out)
    lines = (out / PAYLOAD_PLAIN).read_text().splitlines()
    broken = lines[:1] + ["{not json at all"] + lines[2:]  # replace line 2, keep count
    payload = ("\n".join(broken) + "\n").encode()
    (out / PAYLOAD_PLAIN).write_bytes(payload)
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    manifest["files"][PAYLOAD_PLAIN] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest))

    target = MemoryDB(tmp_path / "clean.sqlite")
    before = _fingerprint(target)
    with pytest.raises(json.JSONDecodeError):
        hydrate_package(target, out)
    assert _fingerprint(target) == before  # zero rows from the good prefix
    target.close()
