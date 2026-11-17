# Production Hardening Runbook

This page covers SQLite concurrency and recovery, full-store backup and restore,
schema upgrades, and the boundaries provided by daemon authentication and MCP
read-only mode.

## SQLite concurrency and recovery

HotMem stores canonical records in SQLite with WAL enabled. SQLite serializes
writers; each committed batch is atomic. `MemoryDB` repairs the external-content
FTS index once per store and records that repair transactionally, rather than
taking a write lock and rebuilding the index on every process open.

The focused reliability checks are:

- Two independent SQLite connections start writes together and both commit
  their full 100-row batches.
- A writer process exits with an open, uncommitted transaction. Reopening the
  database retains the prior committed row, discards the interrupted row, and
  accepts another write.

These checks cover SQLite transaction recovery and two concurrent local
connections. They do not claim multi-host write coordination; the daemon's
store ownership lock remains the single-owner boundary.

## Full database backup and restore

Use SQLite's online backup API for a full-store backup. It includes the
canonical records, event and replay state, store metadata, and indexes while
the daemon is running. A HotMem snapshot is a portable memory export; it is
not a substitute when the goal is to preserve all internal SQLite state.

Create and verify a backup:

```python
import sqlite3

source = sqlite3.connect("./hotmem.sqlite")
backup = sqlite3.connect("./hotmem-backup.sqlite")
try:
    source.backup(backup)
    result = backup.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"backup integrity check failed: {result}")
    print("schema user_version:", backup.execute("PRAGMA user_version").fetchone()[0])
finally:
    backup.close()
    source.close()
```

Keep the backup on protected storage. SQLite backups and portable snapshots
are not encrypted by HotMem.

To restore, stop HotMem first, preserve the current database, then copy the
verified backup into the configured database path. Reopen the database and
check both `PRAGMA integrity_check` and a representative local search before
reconnecting clients. Do not replace an active database file by copying over
it; a running process may have an open WAL transaction.

For a portable record export instead, use `hotmem snapshot --db PATH --file
./snapshot-v2`, verify it with `hotmem verify ./snapshot-v2`, and restore it
with `hotmem hydrate --file ./snapshot-v2 --db ./restored.sqlite`.

## Schema upgrades

Writable `MemoryDB` startup applies additive SQLite migrations automatically.
The current store schema reports `PRAGMA user_version = 7`. Before upgrading:

1. Make a full SQLite backup with the procedure above.
2. Record the current HotMem version and the database's `user_version`.
3. Test the new HotMem version against a copy of the backup.
4. Check `PRAGMA integrity_check`, confirm the schema version, and run a
   representative local search against the copied store.
5. Stop the old process, start the new version against the production store,
   and verify its status and search behavior.

Migrations run when a writable store opens; there is no downgrade command.
If an upgrade fails validation, stop the process and restore the pre-upgrade
backup rather than opening the migrated store with an older release.

## Daemon credential boundary

`hotmem serve` binds to `127.0.0.1` by default. When using a policy file for
shared or remote clients, set `require_auth: true`, issue high-entropy bearer
tokens, and grant only the namespaces and operations each client needs.
`CallerCredential.from_token` stores a SHA-256 token digest, and token checks
use constant-time comparison. Policy serialization writes the digest, not the
input token. Older policy files that still contain a `token` field are read
for compatibility and hashed in memory; replace those files with a newly saved
policy and protect them with restrictive filesystem permissions.

The daemon API uses HTTP. If binding beyond loopback, place it behind a
transport that provides TLS and network access controls. HotMem application
traces do not include authorization header values. The database, policy file,
snapshots, and backups remain readable by operating-system users who can access
their files.

## MCP read-only mode

`hotmem mcp --read-only` works with embedded `--db` / initialized `--mount`
mode and daemon `--url` relay mode. In embedded mode, the database must already
exist and SQLite opens it with `mode=ro` plus query-only enforcement. The MCP
server does not perform startup hydration. In relay mode, the MCP process hides
and rejects the `add_memory`, `snapshot`, `hydrate`, `handoff_prepare`, and
`handoff_hydrate` tools before dispatch.

The flag is an MCP boundary. In relay mode, it does not change daemon policy
for other clients. The remaining MCP tools can still read memories and inspect
or verify a caller-supplied handoff package path; use OS access controls to
limit which files the MCP process can read.

```bash
hotmem mcp --read-only --db ./hotmem.sqlite
hotmem mcp --read-only --mount ./hotmem-mount
hotmem mcp --read-only --url http://127.0.0.1:8711
```

## Scale run

Run the local SQLite scale benchmark with:

```bash
python bench/store_scale/run.py --output bench/store-scale/results.json
```

It creates temporary synthetic stores at 100,000 and 1,000,000 rows, records
ingest time, default-path search p50/p95, peak resident memory, and database
plus sidecar size, then removes the stores. It uses the default exact-scan
search path and makes no network calls. The source work plan asks for memory
and latency targets but does not state numeric thresholds; the committed
measurements are diagnostics and do not claim a pass/fail target.

Measured on macOS 27.2.1 arm64, Python 3.11.9, SQLite 3.54.1; each row uses
20 mixed exact-match and no-match searches:

| Rows | Ingest | Search p50 | Search p95 | Peak RSS | Database + sidecars |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | 4.919 s | 1,113.293 ms | 1,124.355 ms | 184 MiB | 58 MiB |
| 1,000,000 | 52.788 s | 11,488.974 ms | 11,702.829 ms | 1,580 MiB | 580 MiB |

The exact samples, environment, and workload checks are in
[`bench/store-scale/results.json`](../bench/store-scale/results.json). Peak RSS
and latency reflect this machine and this synthetic default-path workload;
they are not production capacity guarantees.
