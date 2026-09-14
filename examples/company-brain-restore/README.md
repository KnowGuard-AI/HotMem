# Company-brain restore — a reproducible clone (#69)

The full workflow in five commands: import a knowledge wiki, hydrate a
source instance, package it, restore into a clean instance, and prove the
clone is equivalent. Every command below is deterministic — run it twice,
expect identical bytes and zero re-loaded records.

Uses the vendored public OKF fixture bundle (`tests/fixtures/okf/acme_retail`,
Apache-2.0) as the knowledge wiki.

## 1. Reviewable JSONL from the wiki

```bash
hotmem import --from okf \
  --db tests/fixtures/okf/acme_retail \
  --out ./brain.jsonl
```

`brain.jsonl` is the reviewable intermediate (#68): canonical, sorted,
byte-stable — diff it, lint it, commit it. Every page becomes one record
with provenance (sources, verified timestamps, trust tier) and a SHA-256
source hash.

## 2. Source instance

```bash
hotmem hydrate --file brain.jsonl --db source.sqlite
```

## 3. Package (the clone artifact)

```bash
hotmem snapshot --file clone-pkg --package --gz --db source.sqlite
hotmem verify clone-pkg
```

`clone-pkg/` holds `manifest.json` + `memories.jsonl.gz`: a versioned
manifest (record count, logical identity, digests, embedding compatibility)
and a canonical payload — `hotmem-interchange-v1`. Publish is atomic; the
manifest's `logical_id` is identical for equivalent contents regardless of
compression or export time.

## 4. Clean instance

```bash
hotmem hydrate --file clone-pkg --db clean.sqlite
```

Verification (digests, sizes, record counts, schema, path confinement)
completes BEFORE any write; the restore is transactional — a corrupt or
truncated package leaves `clean.sqlite` untouched.

## 5. Prove equivalence

```bash
hotmem hydrate --file clone-pkg --db clean.sqlite   # repeat
```

The repeat reports `loaded=0` — every record is a content-hash duplicate.
Embeddings were reused (no re-embedding) because the manifest declared the
same model/dimension. Retrieval on the clean instance returns the same
results as the source (locked by `tests/test_e2e_clone.py`).

## Script

`./restore.sh` runs all five steps with assertions (idempotent repeat,
verified package) — CI-friendly evidence in ~10 seconds:

```bash
./restore.sh
```
