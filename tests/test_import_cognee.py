"""Tests for importing current Cognee COGX exports without Cognee installed."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import BaseModel

from hotmem.cli import main
from hotmem.db import MemoryDB
from hotmem.importers import IMPORTERS
from hotmem.importers.cognee import read_cognee_export


def _cogx_record(kind: str, external_id: str, body: dict) -> dict:
    text = body.get("content") or body.get("fact_text") or ""
    record = {
        "external_system": "cognee",
        "external_id": external_id,
        "scope": {"user_id": "ada"},
        "created_at": "2026-11-01T12:00:00Z",
        "metadata": {"project": "migration-check"},
        "content_type": "text/plain",
        "content_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "checksum": "",
        "tags": ["from-cognee"],
        "contributors": [],
        "subject": [],
        "kind": kind,
        **body,
    }
    without_checksum = {key: value for key, value in record.items() if key != "checksum"}
    payload = json.dumps(without_checksum, ensure_ascii=False, separators=(",", ":"))
    record["checksum"] = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    return record


def _write_cogx_export(path: Path) -> Path:
    path.mkdir()
    memory = _cogx_record(
        "memory",
        "memory-ada-1",
        {"content": "Ada prefers concise release notes.", "categories": ["preference"]},
    )
    fact = _cogx_record(
        "fact",
        "fact-ada-1",
        {
            "subject_ref": "HotMem",
            "predicate": "stores",
            "object_ref": "portable memories",
            "fact_text": "HotMem stores portable memories.",
            "provenance": [],
        },
    )
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "cogx_version": "0.2",
                "source_system": "cognee",
                "counts": {"memory": 1, "fact": 1},
            }
        ),
        encoding="utf-8",
    )
    (path / "memories.jsonl").write_text(
        json.dumps(memory, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (path / "facts.jsonl").write_text(
        json.dumps(fact, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    # The importer must not read optional Cognee permissions, which can carry
    # user account data and password hashes.
    (path / "permissions.json").write_text(
        '{"owner":{"hashed_password":"fixture-secret"}}', encoding="utf-8"
    )
    return path


def test_cognee_export_round_trips_through_cli(tmp_path: Path):
    source = _write_cogx_export(tmp_path / "cognee-export")
    assert IMPORTERS["cognee"] is read_cognee_export

    records = list(read_cognee_export(source))
    assert [record["fact_text"] for record in records] == [
        "HotMem stores portable memories.",
        "Ada prefers concise release notes.",
    ]
    assert all(record["identifier"] == "ada" for record in records)
    assert all(record["source"] == "cognee" for record in records)
    assert records[1]["tags"] == ["from-cognee", "preference"]
    assert "fixture-secret" not in json.dumps(records)

    target = tmp_path / "hotmem.sqlite"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["import", "--from", "cognee", "--db", str(source), "--target", str(target)],
    )
    assert result.exit_code == 0, result.output
    assert "imported=2" in result.output

    repeated = runner.invoke(
        main,
        ["import", "--from", "cognee", "--db", str(source), "--target", str(target)],
    )
    assert repeated.exit_code == 0, repeated.output
    assert "imported=0" in repeated.output

    db = MemoryDB(target, read_only=True)
    try:
        assert db.count() == 2
        memory = next(row for row in db.all_rows() if "Ada prefers" in row["fact_text"])
        assert memory["source"] == "cognee"
        assert json.loads(memory["tags"]) == ["from-cognee", "preference"]
        metadata = json.loads(memory["metadata_json"])
        assert metadata["cognee"]["external_id"] == "memory-ada-1"
    finally:
        db.close()


def test_cognee_import_accepts_archive_and_rejects_path_traversal(tmp_path: Path):
    source = _write_cogx_export(tmp_path / "export")
    archive_path = tmp_path / "export.cogx.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for item in source.iterdir():
            archive.add(item, arcname=item.name)
    assert len(list(read_cognee_export(archive_path))) == 2

    malicious_path = tmp_path / "malicious.cogx.tar.gz"
    with tarfile.open(malicious_path, "w:gz") as archive:
        payload = b"outside"
        member = tarfile.TarInfo("../outside.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(ValueError, match="unsafe path"):
        list(read_cognee_export(malicious_path))
    assert not (tmp_path / "outside.txt").exists()


def test_cognee_checksum_accepts_pydantic_float_serialization(tmp_path: Path):
    class Record(BaseModel):
        external_id: str = "fact-1"
        checksum: str | None = None
        kind: str = "fact"
        fact_text: str = "Small confidence value"
        metadata: dict = {"confidence": 1e-7}

    record = Record()
    payload = record.model_dump_json(exclude={"checksum"}, exclude_none=True)
    record.checksum = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    source = tmp_path / "export"
    source.mkdir()
    (source / "manifest.json").write_text(
        json.dumps({"cogx_version": "0.2", "source_system": "cognee", "counts": {"fact": 1}})
    )
    (source / "facts.jsonl").write_text(record.model_dump_json(exclude_none=True) + "\n")

    assert list(read_cognee_export(source))[0]["fact_text"] == "Small confidence value"
