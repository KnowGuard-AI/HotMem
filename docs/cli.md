# OKF: CLI Reference

Status: Accepted
Owner: HotMem maintainers
Last updated: 2026-07-06
Scope: Stable command-line interface reference

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

## 5. Open Questions

- Should future file-native health hints appear under `status`, a new
  `inspect`, or both?
