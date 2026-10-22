# Shared Daemon, MCP Relay, and LSD Management Contract (#109, #110, #111, #112)

## 1. Architecture Overview

In **daemon mode**, HotMem runs as an explicitly started, single-owner local service (`hotmem serve --mount <dir>` or `hotmem serve --db <path>`) that owns:
- One SQLite store (`MemoryDB`) with an OS-backed exclusive ownership lock (`.<db_name>.daemon.owner.lock`).
- Warm runtime resources (`Embedder`, optional `Reranker`, optional `VectorIndex`).
- The shared `HotMemRuntime` operation and policy boundary (`src/hotmem/runtime.py`, `src/hotmem/policy.py`).

All daemon-mode clients—MCP (`hotmem mcp --url ...`), CLI (`hotmem <cmd> --url ...`), Python SDK (`HotMemClient` / `AsyncHotMemClient`), TypeScript SDK (`HotMemClient`), and the detachable LSD management client (#112)—route through the same `HotMemRuntime` boundary rather than opening independent or temporary SQLite databases.

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ MCP Host (stdio) │   │ CLI / Py / TS SDK│   │ Detachable LSD UI    │
│ hotmem mcp --url │   │ --url / Client   │   │ (management client)  │
└────────┬─────────┘   └────────┬─────────┘   └──────────┬───────────┘
         │                      │                        │
         └──────────────────────┼────────────────────────┘
                                ▼
              Loopback HTTP Boundary (127.0.0.1:8711)
        ┌───────────────────────────────────────────────┐
        │                 HotMemRuntime                 │
        │  • Policy & Credential Scope (#110)           │
        │  • Session & Source Attribution (#111)        │
        │  • Atomic Operation Ledger & Retries (#100)   │
        │  • Concurrent WAL Reads + Bounded Write Slot  │
        │  • Exclusive Store Lock (StoreLock)           │
        └───────────────────────────────────────────────┘
```

---

## 2. Explicit Store Selection & Ownership

- `hotmem serve` requires `--mount <dir>` or `--db <path>` (or `HOTMEM_MOUNT` / `HOTMEM_DB`). Silent fallback to a temporary SQLite database is disabled.
- `hotmem mcp` requires `--url <daemon_url>` (thin stdio-to-daemon relay mode) or `--mount <dir>` / `--db <path>` (embedded single-process compatibility mode).
- While a daemon process owns a store, competing `hotmem serve`, embedded `hotmem mcp`, or offline mutating CLI commands (`hydrate --db`, `promote --db`, `delta apply --db`, `handoff hydrate --db`) fail immediately with actionable lock owner diagnostics (`pid`, `store_id`, `port`).
- Graceful shutdown releases the lock and unlinks the lock file. If a daemon process terminates abruptly (`SIGKILL`), the OS kernel releases the descriptor lock immediately and the next daemon startup recovers the stale lock metadata cleanly.

---

## 3. Reproducible Daemon Start -> Configure -> Connect -> Reconnect -> Stop Runbook

### Step 1: Create a Mount and Policy File (`policy.json`)

```python
from pathlib import Path
from hotmem.policy import CallerCredential, PolicyConfig, ShareApproval

mount = Path("./incident-mount")
mount.mkdir(parents=True, exist_ok=True)

policy = PolicyConfig(
    store_id="store-incident-01",
    require_auth=True,
    credentials=[
        CallerCredential.from_token(
            token="tok-caller-a",
            caller_id="caller-a",
            store_id="store-incident-01",
            namespaces=("ops/shared", "ops/private"),
            operations=("read", "write", "export", "hydrate", "promote"),
        ),
        CallerCredential.from_token(
            token="tok-caller-b",
            caller_id="caller-b",
            store_id="store-incident-01",
            namespaces=("ops/b-scratch",),
            operations=("read", "write"),
            share_approvals=(
                ShareApproval(
                    approval_id="share-demo-17",
                    namespace="ops/shared",
                    identifiers=("ops/demo-17",),
                    audience=("caller-b",),
                    operations=("read",),
                ),
            ),
        ),
    ],
)
policy.save(mount / "policy.json")
```

### Step 2: Start the Shared Daemon

```bash
hotmem serve --mount ./incident-mount --port 8711 --store-id store-incident-01
```

### Step 3: Verify Daemon Status & Empty Store State

```bash
hotmem status --url http://127.0.0.1:8711 --token tok-caller-a --store-id store-incident-01
```

### Step 4: Register MCP Host in Daemon Relay Mode

Configure your MCP host (e.g. Claude Desktop or Cursor) to launch `hotmem mcp` in thin relay mode:

```json
{
  "mcpServers": {
    "hotmem": {
      "command": "hotmem",
      "args": [
        "mcp",
        "--url", "http://127.0.0.1:8711",
        "--token", "tok-caller-a",
        "--session", "sess-a-investigation",
        "--source", "mcp://claude-a",
        "--namespace", "ops/shared",
        "--store-id", "store-incident-01"
      ]
    }
  }
}
```

Closing or restarting the MCP host closes only the `stdio` relay; the daemon and other connected clients continue running without interruption.

---

## 4. Exposed Daemon Route Inventory

Every route on the daemon is registered in `DAEMON_ROUTE_INVENTORY` (`src/hotmem/server.py`) and enforced by `HotMemRuntime`:

| Method | Path | Required Operation | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/v1/health` | `read` | Scoped health, `store_id`, `store_state` (`verified_empty` / `ready`), `runtime_generation` |
| `POST` | `/v1/add` | `write` | Add inline fact or file-backed reference with session attribution & optional `X-HotMem-Idempotency-Key` |
| `POST` | `/v1/search` | `read` | Hybrid search filtered to caller's permitted namespaces and share approvals prior to ranking |
| `GET` | `/v1/memories` | `read` | Chronological list by identifier within caller's permitted scope |
| `GET` | `/v1/memory/{memory_id}` | `read` | Metadata & provenance lookup by memory ID |
| `POST` | `/v1/memory/{memory_id}/hydrate` | `read` | Raw or profiled (`agent`/`compact`/`audit`/`full`) payload hydration |
| `POST` | `/v1/memory/hydrate-batch` | `read` | Batch profiled hydration with per-item scope checks |
| `POST` | `/v1/hydrate` | `hydrate` | Import Snapshot v2, interchange package, or legacy JSONL |
| `POST` | `/v1/snapshot` | `export` | Scoped export to Snapshot v2, interchange package, or legacy JSONL |
| `POST` | `/v1/handoff/prepare` | `export` | Prepare a `hotmem-handoff-v1` session handoff package |
| `POST` | `/v1/handoff/verify` | `read` | Verify handoff package integrity |
| `GET` | `/v1/handoff/inspect` | `read` | Inspect handoff package metadata & coverage |
| `POST` | `/v1/handoff/hydrate` | `hydrate` | Hydrate verified handoff package into store |
| `GET` | `/v1/files` | `read` | List file-backed references visible to caller |
| `GET` | `/v1/bundles` | `read` | List indexed bundles |
| `POST` | `/v1/discover` | `write` | Scan directory root for bundles |
| `GET` | `/v1/hygiene` | `read` | Run store hygiene check |
| `GET` | `/v1/events` | `read` | Scoped activity stream with `store_id`, `runtime_generation`, `policy_revision` (strips raw `fact_text` & `embedding_b64`) |
| `POST` | `/v1/memory/{memory_id}/promote` | `promote` | Lifecycle state transition (`HOT` -> `READY` -> `PROMOTED` -> `ARCHIVED`) |
| `POST` | `/v1/memory/{memory_id}/candidate` | `promote` | Flag/unflag promotion candidate |
| `GET` | `/v1/promotion/candidates` | `read` | List promotion candidates in caller scope |
| `GET` | `/v1/promotion/states` | `read` | Return promotion state machine model |
| `GET` | `/v1/promotion/by-state` | `read` | List memories by promotion state in caller scope |
| `POST` | `/v1/vector-index/rebuild` | `write` | Rebuild derived vector index |
| `GET` | `/v1/vector-index/status` | `read` | Derived vector index status |
| `DELETE` | `/v1/vector-index` | `write` | Clear derived vector index |

---

## 5. MMVT 6 Recorded 2-Client Direct vs. Loopback HTTP Baseline

Reproduce with:

```bash
uv run python bench/daemon/run_mmvt6_baseline.py
```

Measured on macOS (`darwin`), `hotmem-hash-v1` embedder, 2 callers (`caller-a`, `caller-b`), 25 reads/client after warmup (50 samples per mode), process max RSS `69.09 MB`:

| Mode | Samples | Payload Bytes | Client Wall p50 (ms) | Client Wall p95 (ms) | Server `trace_ms` p50 | Server `trace_ms` p95 | Approx Transport Delta p50 (ms) | CPU Time (s) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct (`HotMemRuntime`) | 50 | 2,168 | 0.390 | 0.918 | 0.380 | 0.900 | — | 0.0238 |
| Pooled Loopback HTTP (`HotMemClient`) | 50 | 2,038 | 1.894 | 2.768 | 0.520 | 0.850 | 1.325 | 0.0925 |
