"""CLI `hotmem delta produce|apply` tests (#73)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from hotmem.cli import main
from hotmem.db import MemoryDB
from hotmem.embed import embed_text, pack_embedding
from hotmem.interchange.canonical import compute_content_hash
from hotmem.interchange.hydrate import hydrate_package
from hotmem.interchange.package import write_package


def test_delta_produce_and_apply_via_cli(tmp_path: Path):
    source = MemoryDB(tmp_path / "src.sqlite")
    for i in range(2):
        fact = f"cli delta fact {i}"
        source.insert(
            id=f"c{i}",
            identifier=f"cli-{i}",
            fact_text=fact,
            embedding=pack_embedding(embed_text(fact)),
            embedding_model="hotmem-hash-v1",
            content_hash=compute_content_hash(f"cli-{i}", fact),
        )
    base = tmp_path / "base"
    write_package(source, base)

    receiver = MemoryDB(tmp_path / "receiver.sqlite")
    hydrate_package(receiver, base)

    # Mutate source: one add.
    fact_new = "cli delta fact new"
    source.insert(
        id="c9",
        identifier="cli-9",
        fact_text=fact_new,
        embedding=pack_embedding(embed_text(fact_new)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("cli-9", fact_new),
    )

    runner = CliRunner()

    produce = runner.invoke(
        main,
        [
            "delta",
            "produce",
            "--base",
            str(base),
            "--db",
            str(tmp_path / "src.sqlite"),
            "--out",
            str(tmp_path / "d1"),
        ],
    )
    assert produce.exit_code == 0, produce.output

    apply_run = runner.invoke(
        main,
        [
            "delta",
            "apply",
            "--delta",
            str(tmp_path / "d1"),
            "--db",
            str(tmp_path / "receiver.sqlite"),
        ],
    )
    assert apply_run.exit_code == 0, apply_run.output
    assert receiver.count() == 3

    replay = runner.invoke(
        main,
        [
            "delta",
            "apply",
            "--delta",
            str(tmp_path / "d1"),
            "--db",
            str(tmp_path / "receiver.sqlite"),
        ],
    )
    assert replay.exit_code == 0
    assert "applied=0" in replay.output
    assert "skipped=1" in replay.output
    receiver.close()
    source.close()


def test_delta_apply_conflict_exits_nonzero(tmp_path: Path):
    source = MemoryDB(tmp_path / "src.sqlite")
    fact = "conflict cli fact"
    source.insert(
        id="k0",
        identifier="k0",
        fact_text=fact,
        embedding=pack_embedding(embed_text(fact)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("k0", fact),
    )
    base = tmp_path / "base"
    write_package(source, base)

    # Source rewrites k0 (promotion); receiver diverges on the same record.
    divergent = MemoryDB(tmp_path / "divergent.sqlite")
    hydrate_package(divergent, base)
    divergent.update_promotion_state("k0", "ARCHIVED")
    source.update_promotion_state("k0", "WARM")
    delta_dir = tmp_path / "d1"
    produce = CliRunner().invoke(
        main,
        [
            "delta",
            "produce",
            "--base",
            str(base),
            "--db",
            str(tmp_path / "src.sqlite"),
            "--out",
            str(delta_dir),
        ],
    )
    assert produce.exit_code == 0, produce.output

    result = CliRunner().invoke(
        main,
        ["delta", "apply", "--delta", str(delta_dir), "--db", str(tmp_path / "divergent.sqlite")],
    )
    assert result.exit_code != 0
    assert "state_divergence" in result.output
    assert divergent.count() == 1  # unchanged
    divergent.close()
    source.close()


def test_delta_produce_rejects_unverified_base(tmp_path: Path):
    source = MemoryDB(tmp_path / "src.sqlite")
    fact = "unverified base fact"
    source.insert(
        id="u0",
        identifier="u0",
        fact_text=fact,
        embedding=pack_embedding(embed_text(fact)),
        embedding_model="hotmem-hash-v1",
        content_hash=compute_content_hash("u0", fact),
    )
    base = tmp_path / "base"
    write_package(source, base)
    payload = base / "memories.jsonl"
    payload.write_bytes(payload.read_bytes()[:-5])  # truncate

    result = CliRunner().invoke(
        main,
        [
            "delta",
            "produce",
            "--base",
            str(base),
            "--db",
            str(tmp_path / "src.sqlite"),
            "--out",
            str(tmp_path / "d"),
        ],
    )
    assert result.exit_code != 0
    assert "verification failed" in result.output
    source.close()
