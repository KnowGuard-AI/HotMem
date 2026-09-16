# CLI Reference

## 1. Purpose

This document records the stable HotMem CLI. Existing commands and flags should
remain compatible as file-native behavior is added.

## 2. Common Commands

```bash
hotmem serve --port 8711 --mount ./data/hotmem
hotmem serve --db ./my.sqlite
hotmem serve --host 0.0.0.0 --port 8711
hotmem hydrate --file swap.jsonl --db ./my.sqlite
hotmem snapshot --file swap.jsonl --db ./my.sqlite
hotmem import --from okf --db ./company-wiki --target ./brain.sqlite --out ./wiki.jsonl
hotmem status
hotmem openapi --output openapi.json
hotmem openapi --output openapi.yaml --format yaml
```

## 3. Compatibility Rules

- `hydrate` and `snapshot` keep JSONL support.
- New snapshot formats must be additive.
- Existing flags should not change meaning.
- Future warnings about DB growth should be informational by default.

## 4. Commands

### 4.1 `serve`

Start the HotMem sidecar server.

| Flag | Default | Description |
|---|---|---|
| `--port` | 8711 | Port to listen on |
| `--mount` | — | Mount directory path |
| `--db` | — | Explicit database path |
| `--host` | 127.0.0.1 | Host to bind to |

### 4.2 `hydrate`

Load a swap file into the database.

| Flag | Default | Description |
|---|---|---|
| `--file` | swap.jsonl | Swap file path |
| `--db` | required | Database path |

### 4.3 `snapshot`

Export database memories to a swap file.

| Flag | Default | Description |
|---|---|---|
| `--file` | swap.jsonl | Output swap file path |
| `--db` | required | Database path |

### 4.4 `status`

Check if a HotMem server is running.

| Flag | Default | Description |
|---|---|---|
| `--port` | 8711 | Port to check |
| `--host` | 127.0.0.1 | Host to check |

### 4.5 `openapi`

Export the OpenAPI specification.

| Flag | Default | Description |
|---|---|---|
| `--output` / `-o` | stdout | Output file path |
| `--format` | json | Output format (json or yaml) |

### 4.6 `import`

Import memories from a foreign memory system or knowledge bundle into HotMem.

| Flag | Default | Description |
|---|---|---|
| `--from` | required | Source type: `mem0` (SQLite history DB) or `okf` (OKF v0.2 bundle directory) |
| `--db` | required | Source database path (mem0) or bundle directory (okf) |
| `--target` | temp DB | HotMem database to hydrate into |
| `--out` | temp file, deleted | Keep the intermediate JSONL for review |

The command always emits the intermediate JSONL before hydration — pass
`--out` to keep a reviewable artifact of exactly what will be loaded.

#### OKF bundles (`--from okf`)

Reads a Google Open Knowledge Format v0.2 bundle — a directory tree of
markdown concept pages with YAML frontmatter — and converts each page into
one deterministic record: identifier is the concept path (sans `.md`),
`fact_text` is the page body, frontmatter trust/lifecycle/provenance
families are preserved (trust tier, status, sources, generated/verified
timestamps), relative and bundle-absolute links are captured, and each
record carries the page's SHA-256 as `source_checksum`. The same bundle
always produces byte-identical JSONL.

Living-wiki conventions that import cleanly:

- An `index.md` per directory listing child pages (reserved; never a record).
- Compact, focused pages that link to each other rather than duplicating
  content — links become record relationships.
- Raw source material under `references/` stays distinct from compiled
  knowledge: it is recorded as provenance and marked in metadata, never
  fetched or inlined.

Safety envelope: per-file size cap (16 MiB), safe YAML only, symlink
escapes rejected, zero network access. Pages with malformed frontmatter
warn and are skipped — they never crash the import.

### 4.7 `handoff`

Verified session handoff: Codex → HotMem → Claude (#101). One canonical
`hotmem-handoff-v1` package; verify-before-hydrate; atomic, idempotent
hydration; explicit consent required.

#### `handoff prepare`

| Flag | Default | Description |
|---|---|---|
| `--source` | required | Source export directory (`codex-export-v1`) |
| `--out` | required | Handoff package output directory |
| `--mode` | `resume` | `resume` (bounded brief) or `archive` (full ordered stream) |
| `--consent` | required | Explicit consent statement; capture is never implicit |
| `--session` | — | Require this session id in the export envelope |

#### `handoff verify`

Fail-closed verification: exits non-zero with the structured reason on any
integrity problem. Read-only.

#### `handoff inspect`

Read-only report: identity, mode, counts, coverage, omissions, and
redactions — or the failure reason for invalid packages. `--json` emits
the full report and exits 0 with `valid: false` so scripts can parse
failures; the human mode fails loudly.

#### `handoff hydrate`

| Flag | Default | Description |
|---|---|---|
| `--db` | required | Target database path |
| `--embedder` | `hash` | Runtime embedder (see §4.1) |

Atomic and idempotent: the selected durable memories plus exactly one
resume-brief record hydrate in one transaction; a repeat run of the same
package is a no-op reporting `already_applied`. The brief is a normal
memory record (`handoff/<label>/resume-brief`), retrievable through the
unmodified search path.

```bash
hotmem handoff prepare --source ./codex_export --out ./handoff \
    --mode resume --consent "I consent to capturing this session"
hotmem handoff verify ./handoff
hotmem handoff inspect ./handoff --json
hotmem handoff hydrate ./handoff --db ./claude.sqlite
```
