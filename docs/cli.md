# CLI

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

## Commands

### `serve`

Start the HotMem sidecar server.

| Flag | Default | Description |
|---|---|---|
| `--port` | 8711 | Port to listen on |
| `--mount` | — | Mount directory path |
| `--db` | — | Explicit database path |
| `--host` | 127.0.0.1 | Host to bind to |

### `hydrate`

Load a swap file into the database.

| Flag | Default | Description |
|---|---|---|
| `--file` | swap.jsonl | Swap file path |
| `--db` | required | Database path |

### `snapshot`

Export database memories to a swap file.

| Flag | Default | Description |
|---|---|---|
| `--file` | swap.jsonl | Output swap file path |
| `--db` | required | Database path |

### `status`

Check if a HotMem server is running.

| Flag | Default | Description |
|---|---|---|
| `--port` | 8711 | Port to check |
| `--host` | 127.0.0.1 | Host to check |

### `openapi`

Export the OpenAPI specification.

| Flag | Default | Description |
|---|---|---|
| `--output` / `-o` | stdout | Output file path |
| `--format` | json | Output format (json or yaml) |
