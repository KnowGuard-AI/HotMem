"""CLI `hotmem import --from okf` tests (#68).

The importer must emit reviewable, byte-stable JSONL before hydration, and
the output must hydrate through the existing JSONL path into a usable store.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from hotmem.cli import main

WIKI_PAGE = """---
type: Playbook
title: Deploy checklist
description: Steps to deploy the service safely.
tags: [ops, deploy]
status: stable
generated: { by: human:sre, at: 2026-09-01T10:00:00Z }
verified: { by: human:sre, at: 2026-09-02T09:00:00Z }
---

# Steps

1. Freeze deploys per [the runbook](/ops/runbook.md).
2. Verify the dashboard (https://example.com/dash).
"""


def _make_wiki(root: Path) -> Path:
    wiki = root / "company-wiki"
    (wiki / "ops").mkdir(parents=True)
    (wiki / "index.md").write_text('---\nokf_version: "0.2"\n---\n# Index\n')
    (wiki / "ops" / "deploy.md").write_text(WIKI_PAGE, encoding="utf-8")
    (wiki / "ops" / "runbook.md").write_text(
        "---\ntype: Runbook\n---\nStandard deploy runbook.\n", encoding="utf-8"
    )
    return wiki


def test_import_okf_emits_reviewable_jsonl_and_hydrates(tmp_path: Path):
    wiki = _make_wiki(tmp_path)
    out = tmp_path / "wiki.jsonl"
    target = tmp_path / "brain.sqlite"

    result = CliRunner().invoke(
        main,
        ["import", "--from", "okf", "--db", str(wiki), "--out", str(out), "--target", str(target)],
    )
    assert result.exit_code == 0, result.output

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # ops/deploy + ops/runbook; index.md reserved

    records = [json.loads(line) for line in lines]
    # Canonical serialization: sorted keys, compact separators.
    for line, rec in zip(lines, records, strict=True):
        assert line == json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    deploy = next(r for r in records if r["identifier"] == "ops/deploy")
    assert deploy["fact_summary"] == "Steps to deploy the service safely."
    assert deploy["tags"] == ["ops", "deploy"]
    assert deploy["provenance"]["verified"] == [{"by": "human:sre", "at": "2026-09-02T09:00:00Z"}]
    assert deploy["metadata"]["okf"]["trust_tier"] == "human-reviewed"
    assert deploy["metadata"]["okf"]["links"] == ["ops/runbook"]

    # Hydrated through the existing JSONL path into a usable store.
    from hotmem.db import MemoryDB

    db = MemoryDB(target)
    assert db.count() == 2
    row = db.all_rows()[0]
    assert row["source_uri"] in ("ops/deploy.md", "ops/runbook.md")
    assert len(row["source_checksum"]) == 64
    db.close()


def test_import_okf_output_is_byte_stable_across_runs(tmp_path: Path):
    """#68 acceptance: the same bundle yields identical --out bytes twice."""
    wiki = _make_wiki(tmp_path)
    outs = []
    for i in range(2):
        out = tmp_path / f"out{i}.jsonl"
        result = CliRunner().invoke(
            main, ["import", "--from", "okf", "--db", str(wiki), "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_import_okf_default_source_and_namespace(tmp_path: Path):
    """Namespace defaults to the bundle directory name; records re-embed on hydrate."""
    wiki = _make_wiki(tmp_path)
    out = tmp_path / "wiki.jsonl"
    result = CliRunner().invoke(
        main, ["import", "--from", "okf", "--db", str(wiki), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(r["namespace"] == "company-wiki" for r in records)
    assert all("embedding" not in r for r in records)
    assert all(r["source"] == "okf" for r in records)


def test_import_okf_rejects_missing_bundle(tmp_path: Path):
    result = CliRunner().invoke(
        main, ["import", "--from", "okf", "--db", str(tmp_path / "missing")]
    )
    assert result.exit_code != 0
