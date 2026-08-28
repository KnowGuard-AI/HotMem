#!/usr/bin/env python3
"""Deterministic corpus generator for the native helper spike (#48).

Purpose:
     Generate the reproducible benchmark corpus used by run_bench.py:
       - binary checksum files (B1: range SHA-256 arms)
       - JSONL event files shaped like real hotmem swap records (B2: scanning arms)
       - loose markdown bundle trees (B3: bundle parse profile)

Determinism:
     Binary content: SHAKE-256 counter-mode (fast, constant-RSS; random.Random
     .randbytes measured pathological on this box — see README methodology).
     Text content: seeded random.Random per artifact.
     Same seed + same profile => byte-identical corpus (verified via manifest
     SHA-256 digests, which are committed in manifest.json).

Usage:
     python gen_corpus.py [--out corpus] [--profile reduced|full]

     reduced: checksum files up to 100MB (~285MB total) — default.
     full:    adds a 200MB checksum file (~485MB total) for the gated 200MB arm.

Writes:
     <out>/manifest.json — sizes, SHA-256 digests, row counts, bad-line offsets.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEED = 48
CHUNK = 8 << 20  # 8 MiB write window — constant RSS regardless of file size.

CHECKSUM_SIZES_REDUCED = [
    ("bin_1kb.bin", 1024),
    ("bin_100kb.bin", 100 * 1024),
    ("bin_1mb.bin", 1 << 20),
    ("bin_10mb.bin", 10 << 20),
    ("bin_100mb.bin", 100 << 20),
]
CHECKSUM_SIZES_FULL = CHECKSUM_SIZES_REDUCED + [("bin_200mb.bin", 200 << 20)]

JSONL_TARGETS = [
    ("events_10mb.jsonl", 10 << 20),
    ("events_50mb.jsonl", 50 << 20),
    ("events_100mb.jsonl", 100 << 20),
]

BUNDLE_COUNTS = [100, 1000, 2500]

_WORDS = (  # noqa: SIM905 — multi-line split is more maintainable here
    "memory agent fact snapshot hydration provenance checksum range offset "
    "bundle markdown metadata identifier namespace tier promotion archive "
    "invoice contract customer meeting decision preference tool result query "
    "search vector embedding index local filesystem sidecar event log append "
    "snapshot manifest attachment reference hydrate compact audit full agent "
    "risk policy schedule deadline owner status priority review deploy model"
).split()

_BASE_TIME = datetime(2026, 7, 1, tzinfo=UTC)


def shake_ctr(seed_label: str, total: int, sink) -> None:
    """Write `total` deterministic bytes to `sink` via SHAKE-256 counter-mode."""
    label = seed_label.encode()
    written = 0
    counter = 0
    while written < total:
        block = hashlib.shake_256(label + b":" + counter.to_bytes(8, "big")).digest(
            min(CHUNK, total - written)
        )
        sink.write(block)
        written += len(block)
        counter += 1


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def gen_checksum_files(out: Path, profile: str) -> list[dict]:
    sizes = CHECKSUM_SIZES_FULL if profile == "full" else CHECKSUM_SIZES_REDUCED
    entries = []
    for name, size in sizes:
        path = out / name
        with open(path, "wb") as f:
            shake_ctr(f"checksum:{name}", size, f)
        digest, actual = _sha256_file(path)
        assert actual == size, f"{name}: wrote {actual}, expected {size}"
        entries.append({"name": name, "size": size, "sha256": digest})
        print(f"  {name}: {size} bytes sha256={digest[:16]}…")
    return entries


def _record(rng: random.Random, idx: int) -> dict:
    """One swap.jsonl-shaped record (same field set as real snapshot rows)."""
    fact = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(10, 22)))
    identifier = rng.choice(["user", "project", "session", "agent", "team"]) + f"-{idx % 97}"
    created = _BASE_TIME + timedelta(seconds=idx * 37 + rng.randint(0, 36))
    raw = rng.randbytes(256)  # 256 bytes -> 344-char b64, same as real embedding blob
    importance = round(rng.uniform(0.1, 0.9), 2)
    return {
        "id": rng.getrandbits(128).to_bytes(16, "big").hex(),
        "identifier": identifier,
        "fact_text": fact,
        "embedding_dim": 64,
        "embedding_model": "hotmem-hash-v1",
        "source": rng.choice(["", "bundle", "import", "api"]),
        "importance": importance,
        "metadata_json": "{}",
        "content_hash": hashlib.sha256(f"{identifier}:{fact}".encode()).hexdigest(),
        "ttl_seconds": None,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "namespace": rng.choice(["", "work", "personal"]),
        "tier": rng.choice(["hot", "warm"]),
        "memory_type": "fact",
        "source_uri": "",
        "source_format": "",
        "source_checksum": "",
        "byte_offset": None,
        "byte_length": None,
        "updated_at": None,
        "snapshot_id": "",
        "promotion_state": rng.choice(["HOT", "READY"]),
        "promotion_candidate": rng.randint(0, 1),
        "parent_memory": "",
        "related_memories": "[]",
        "tags": json.dumps([rng.choice(_WORDS) for _ in range(rng.randint(0, 3))]),
        "schema_version": 1,
        "fact_summary": None,
        "provenance_json": None,
        "embedding_b64": base64.b64encode(raw).decode(),
    }


def gen_jsonl_files(out: Path) -> list[dict]:
    entries = []
    for name, target in JSONL_TARGETS:
        rng = random.Random(f"{SEED}:jsonl:{name}")
        path = out / name
        rows = 0
        bad_line_index = None
        bad_line_offset = None
        written = 0
        bad_written = False
        with open(path, "wb") as f:
            idx = 0
            while written < target:
                # Insert one malformed line at ~60% of the target size.
                if not bad_written and written >= target * 0.6:
                    bad_line_offset = written
                    bad_line_index = rows
                    f.write(b'{"id": "broken", "fact_text": "truncated')
                    f.write(b"\n")
                    written += 38
                    rows += 1
                    bad_written = True
                    continue
                line = json.dumps(_record(rng, idx), separators=(",", ":")).encode()
                f.write(line)
                f.write(b"\n")
                written += len(line) + 1
                rows += 1
                idx += 1
        digest, size = _sha256_file(path)
        assert bad_written, f"{name}: malformed line never inserted"
        entries.append(
            {
                "name": name,
                "size": size,
                "rows": rows,
                "sha256": digest,
                "bad_line_index": bad_line_index,
                "bad_line_offset": bad_line_offset,
            }
        )
        print(f"  {name}: {size} bytes, {rows} rows, bad line #{bad_line_index}")
    return entries


def _bundle_dir(tree: Path, i: int) -> Path:
    return tree / f"bundle_{i:05d}"


def gen_bundle_tree(out: Path, count: int) -> dict:
    tree = out / f"bundles_{count}"
    tree.mkdir(parents=True, exist_ok=True)
    n_files = 0
    for i in range(count):
        rng = random.Random(f"{SEED}:bundle:{count}:{i}")
        b = _bundle_dir(tree, i)
        b.mkdir(exist_ok=True)
        (b / "memory.md").write_text(
            f"# Bundle {i:05d}\n\n"
            + "\n".join(
                f"- {' '.join(rng.choice(_WORDS) for _ in range(rng.randint(6, 14)))}"
                for _ in range(rng.randint(6, 12))
            )
            + "\n",
            encoding="utf-8",
        )
        (b / "metadata.json").write_text(
            json.dumps(
                {
                    "identifier": f"bundle-{i:05d}",
                    "importance": round(rng.uniform(0.2, 0.9), 2),
                    "namespace": rng.choice(["", "work"]),
                    "tags": [rng.choice(_WORDS) for _ in range(2)],
                    "source": "spike-corpus",
                }
            ),
            encoding="utf-8",
        )
        (b / "facts.json").write_text(
            json.dumps(
                [
                    {"fact": " ".join(rng.choice(_WORDS) for _ in range(rng.randint(8, 18)))}
                    for _ in range(3)
                ]
            ),
            encoding="utf-8",
        )
        with open(b / "events.jsonl", "wb") as f:
            for _ in range(5):
                text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(6, 15)))
                line = json.dumps({"event": text}, separators=(",", ":")) + "\n"
                f.write(line.encode())
        att = b / "attachments"
        att.mkdir(exist_ok=True)
        (att / "data.bin").write_bytes(
            hashlib.shake_256(f"{SEED}:att:{count}:{i}".encode()).digest(rng.randint(200, 500))
        )
        (att / "notes.txt").write_text(
            " ".join(rng.choice(_WORDS) for _ in range(20)) + "\n", encoding="utf-8"
        )
        n_files += 7
    # Cheap tree fingerprint: sha256 over sorted (relpath, size, digest) triples.
    h = hashlib.sha256()
    for p in sorted(tree.rglob("*")):
        if p.is_file():
            d, s = _sha256_file(p)
            h.update(f"{p.relative_to(tree)}:{s}:{d}\n".encode())
    print(f"  bundles_{count}: {count} bundles, {n_files} files, digest={h.hexdigest()[:16]}…")
    return {
        "name": f"bundles_{count}",
        "count": count,
        "files": n_files,
        "tree_digest": h.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="corpus", help="output directory (default: corpus)")
    parser.add_argument("--profile", choices=["reduced", "full"], default="reduced")
    parser.add_argument("--bundles", default=",".join(str(c) for c in BUNDLE_COUNTS))
    args = parser.parse_args()

    out = Path(__file__).resolve().parent / args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"Generating spike corpus (seed={SEED}, profile={args.profile}) in {out}")
    manifest = {
        "generator": "gen_corpus.py",
        "seed": SEED,
        "profile": args.profile,
        "created": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checksum_files": gen_checksum_files(out, args.profile),
        "jsonl_files": gen_jsonl_files(out),
        "bundle_trees": [
            gen_bundle_tree(out, int(c)) for c in args.bundles.split(",") if c.strip()
        ],
    }
    manifest_path = out.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
