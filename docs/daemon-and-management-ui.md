# Shared Daemon, MCP Relay, and Management UI Contract (#109, #110, #111, #112)

## 1. Architecture Overview

In **daemon mode**, HotMem runs as an explicitly started, single-owner local service (`hotmem serve --mount <dir>` or `hotmem serve --db <path>`) that owns:
- One SQLite store (`MemoryDB`) with an OS-backed exclusive ownership lock (`.<db_name>.daemon.owner.lock`).
- Warm runtime resources (`Embedder`, optional `Reranker`, optional `VectorIndex`).
- The shared `HotMemRuntime` operation and policy boundary (`src/hotmem/runtime.py`, `src/hotmem/policy.py`).

All daemon-mode clients—MCP (`hotmem mcp --url ...`), CLI (`hotmem <cmd> --url ...`), Python SDK (`HotMemClient` / `AsyncHotMemClient`), TypeScript SDK (`HotMemClient`), and the detachable management client (#112)—route through the same `HotMemRuntime` boundary rather than opening independent or temporary SQLite databases.

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ MCP Host (stdio) │   │ CLI / Py / TS SDK│   │ Detachable UI    │
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

### Operating invariants (enforced)

- **Writes require session attribution.** When a policy is active, every
  `POST /v1/add` must carry `X-HotMem-Session-Id` (400 `session_required`).
  A body-level `source` may not claim another caller's bound source (403
  `session_impersonation_denied`).
- **Notification of deduplicated writes.** Adding content that already exists
  in the same namespace returns the existing `memory_id` with
  `"deduplicated": true` and records a `memory.deduplicated` event carrying
  the new caller/session/source attribution.
- **Namespace-scoped hydration.** Hydration is fail-closed: the daemon scans
  the artifact's record namespaces and refuses (403 `scope_denied`) before any
  write if the caller cannot write every namespace present. SQLite artifacts
  are rejected through the daemon boundary (403 `unsupported_daemon_route`).
  Integrity is verified first, so tampering returns 409, not 403.
- **Store identity is persistent.** The store id is recorded inside the store
  (`store_meta`) and adopted on restart, so moving a store does not change the
  id clients are pinned to. `--store-id` that contradicts the policy or the
  stored identity fails startup.
- **Caller paths are confined.** In policy mode, `snapshot`, `hydrate`,
  `handoff *`, and `discover` paths must resolve inside the daemon base
  directory (403 `path_not_permitted`).
- **Reads never observe uncommitted writes.** Read operations use per-thread
  read-only WAL connections; the shared write connection is never read
  concurrently mid-transaction. Slow writes and exports do not block
  `/v1/health` or basic reads.
- **Scoped diagnostics are O(rows) under redaction.** `/v1/health` and
  `/v1/vector-index/status` scan visible rows and run disclosure patterns to
  produce scoped counts; this is linear in store size and should be monitored
  on large stores.
- **Scoped diagnostics.** `/v1/hygiene`, `/v1/events`, `/v1/health`, and
  snapshots report only the caller's permitted rows; health exposes
  `store_state` as `verified_empty`, `ready`, or `empty_scope`, and omits
  `db_path` for scoped callers. Store-wide `/v1/bundles` and `/v1/discover`
  require management authority.
- **Offline-only commands.** `delta produce/apply`, `verify`, `inspect`, and
  `import` operate on local files and are intentionally offline (they cannot be
  routed through the HTTP boundary). `handoff prepare/verify/inspect/hydrate`
  all support `--url` (and require the path to be inside the daemon base dir
  when a policy is active).
- **`X-HotMem-Store-Id` is a typo-safety assertion**, not a credential: it lets
  a client fail fast when it points at the wrong daemon. Authorization always
  comes from the bearer token and the credential's `store_id` binding.
- **Revocation is immediate.** Management callers can revoke at runtime:
  `hotmem policy revoke --url ... --caller caller-b` or `--approval <id>`, and
  `hotmem policy reload --url ...` to re-read the policy file.
- **Exports are self-describing.** Scoped snapshot/package exports record their
  `scope`, `exported_count`, `omitted_count`, `redacted_count`, and
  `disclosure` summary in the manifest/metadata and redact host/database paths.
  Snapshot v2 records declare record schema 2; interchange v1 records keep
  `schema_version: 1` and carry the stored row version separately as
  `record_schema_version`.

### Scoped Disclosure & Privacy Policy Contract (#110)

When a policy (`--policy` or `<mount>/policy.json`) is active, `HotMemRuntime`
enforces both **pre-ranking namespace/share scope** and **outbound disclosure
rules** across retrieval (`search`, `list`, `get_memory`, `hydrate`,
`hydrate-batch`, `files`, `promotion/candidates`, `promotion/by-state`,
`events`, `hygiene`, `health`, `vector-index/status`) and exports (`snapshot`):

| Policy Field | Values | Default | Behavior |
| --- | --- | --- | --- |
| `secret_action` | `"allow"` \| `"redact"` \| `"omit"` \| `"fail_closed"` | `"redact"` on shared (`ShareApproval`) access; `"allow"` on direct self-owned namespaces unless explicitly set. Top-level `default_secret_action` applies to shared access. | Controls how secret-shaped (`api_key`, `bearer_token`, `private_key`, `prefixed_credential`, `dsn_inline_password`) and PII (`pii_ssn`, `pii_email`) patterns are handled in text, summaries, hydrated file bytes, and metadata. |
| `private_classification_action` | `"allow"` \| `"omit"` \| `"fail_closed"` | `"omit"` on shared (`ShareApproval`) access; `"allow"` on direct self-owned namespaces unless explicitly set. Top-level `default_private_classification_action` applies to shared access. | Controls how records tagged with `metadata.classification` / `visibility` in `{"private", "secret", "confidential", "restricted"}` (or `metadata.private: true`) are handled. |
| `redact_host_paths` | `true` \| `false` | `true` | Strips absolute host directory prefixes from `source_uri` in API responses, hygiene warnings, events, and exports, and omits `db_path`/`host` from health, vector status, and Snapshot v2 `metadata.json`. |
| `allow_attachments` | `true` \| `false` | `false` (`true` for `is_management`) | Gates `copy_attachments=true` on `/v1/snapshot` (403 `attachments_not_permitted` when disabled). |

- **Pre-ranking candidate selection:** Scope (`ctx.can_access_row`) and
  disclosure transforms (`omit` / `redact` / `fail_closed`) run **before** FTS
  BM25 min/max normalization, hybrid score sorting, second-stage MMR reranking,
  and `top_k` / `max_chars` truncation. When the optional derived `VectorIndex`
  is active and out-of-scope private vectors crowd the ANN oversample window,
  search automatically falls back to the exact scoped SQLite scan so authorized
  callers are never starved.
- **Inbound bulk-import confinement:** `POST /v1/hydrate` and
  `POST /v1/handoff/hydrate` validate every record's target namespace against
  `ctx.can_access_namespace(..., operation="write")` before committing any
  rows. Snapshot v2 and interchange packages are integrity-verified first;
  legacy JSONL and markdown bundles are structurally validated only (they
  carry no checksummed envelope). Unscoped records bind to the caller's
  namespace (pinned header, or the single granted namespace); explicit foreign
  namespaces still fail closed with `403 scope_denied`.
- **Distinct security guarantees (Scoped Snapshot vs. Consented Handoff):**

| Guarantee Dimension | Scoped Snapshot (`POST /v1/snapshot`) | Consented Handoff (`POST /v1/handoff/prepare`) |
| --- | --- | --- |
| **Primary Purpose** | Portable cassette backup/clone of authorized SQLite rows (`.jsonl`, Snapshot v2, `hotmem-interchange-v1`) | Bounded session transfer (`hotmem-handoff-v1`) with `resume-brief.json` and inert history |
| **API Authorization** | Requires `export` + `read` on permitted namespaces / `ShareApproval` | Requires `export` operation and path confined to `base_dir` |
| **Explicit User Consent** | Not required (governed by caller credential & share policy) | **Mandatory** non-empty `consent` verified before reading source lines |
| **Secret / PII Redaction** | Configurable per credential/share (`allow`, `redact`, `omit`, `fail_closed`) including PII patterns; recomputes `content_hash` and embeddings when redacted | Always-on credential redaction (`redact.py`: assignments, bearer tokens, private keys, prefixed keys, inline DSN passwords) + fail-closed `assert_no_secrets` output gate; PII/classification rules are policy-surface only |
| **Tool Execution Safety** | Stores declarative facts and file pointers only | Forces `executable=false` on all historical tool calls |
| **Artifact Integrity** | SHA-256 per-file & overall digests (`hotmem verify`) | SHA-256 per-file, `package_id`, and `handoff_id` (`hotmem handoff verify`) |
| **Encryption & Signing** | Not provided (plaintext JSONL/JSON; use OS/volume or external age/GPG encryption) | Not provided (plaintext JSONL/JSON; checksums prove integrity, not cryptographic authorship) |

#- **Handoff ledger is store-global.** A `package_id` applies once per store.
  A second caller hydrating it in another namespace receives
  `already_applied: true`, `loaded: 0`, `brief_accessible: false`, and no
  brief identifier for the foreign namespace.
- **Non-text hydration fails closed.** Raw hydration of a file-backed payload
  that is not valid UTF-8 returns `403 policy_disclosure_blocked`
  (`reason: non_text_payload`) when `secret_action` is not `allow`; bytes are
  never decoded and re-encoded destructively.
- **PII patterns are opinionated.** `pii_email` matches ordinary addresses
  (support aliases, bot accounts) and `pii_ssn` matches 3-2-4 digit strings, so
  shared rows can be redacted or fail closed on benign data. Set
  `secret_action: "allow"` for a share when those patterns are unacceptable.
- **Artifact validation is point-in-time.** `hydrate` scans namespaces before
  the loader re-reads the artifact; a caller with write access to `base_dir`
  could race a replacement between the two reads. Treat the daemon base
  directory as trusted operator-controlled storage.
- **Event payloads obey the same disclosure action.** `metadata_json`,
  `provenance_json`, `tags`, `identifier`, and `fact_summary` are redacted,
  omitted, or fail closed before an event is returned.

## Store schema migration (v5 -> v6)

The persistent SQLite schema moved from `user_version = 5` to `6`. The
migration runs automatically on the next open and is **fail-closed**:

| Change | Purpose |
| --- | --- |
| `store_meta(key, value)` table | Persists the store id so identity survives moves/copies |
| `operation_ledger` table | Atomic idempotent write outcomes (caller, namespace, key, request hash) |
| `idx_memories_content_hash_unique` redefined as `(namespace, content_hash)` | Namespace-safe logical identity (#100) |

There is no manual migration step and no data rewrite. If the new unique
index cannot be created because the store already contains duplicate
`(namespace, content_hash)` rows, startup aborts with an explicit error and
the original index/transaction is preserved — resolve the duplicates and
restart. Downgrading to a pre-v6 binary is not supported for writes; take a
verified snapshot before downgrading.

**No artifact format bump is required.** Snapshot v2 stays `schema_version: 2`
for records/manifest, and interchange packages stay `schema_version: 1`; the
row's stored schema version travels additively as `record_schema_version`.
Scoped exports add an informational `scope` block (not part of any logical
identity or digest).

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
| `GET` | `/v1/bundles` | `manage` | List indexed bundles |
| `POST` | `/v1/discover` | `manage` | Scan directory root for bundles |
| `GET` | `/v1/hygiene` | `read` | Run store hygiene check |
| `GET` | `/v1/events` | `read` | Scoped activity stream with `store_id`, `runtime_generation`, `policy_revision` (strips raw `fact_text` & `embedding_b64`) |
| `POST` | `/v1/memory/{memory_id}/promote` | `promote` | Lifecycle state transition (`HOT` -> `READY` -> `PROMOTED` -> `ARCHIVED`) |
| `POST` | `/v1/memory/{memory_id}/candidate` | `promote` | Flag/unflag promotion candidate |
| `GET` | `/v1/promotion/candidates` | `read` | List promotion candidates in caller scope |
| `GET` | `/v1/promotion/states` | `read` | Return promotion state machine model |
| `GET` | `/v1/promotion/by-state` | `read` | List memories by promotion state in caller scope |
| `POST` | `/v1/vector-index/rebuild` | `manage` | Rebuild derived vector index |
| `GET` | `/v1/vector-index/status` | `read` | Derived vector index status |
| `DELETE` | `/v1/vector-index` | `manage` | Clear derived vector index |
| `POST` | `/v1/policy/revoke` | `manage` | Immediately revoke a caller credential and/or share approval |
| `POST` | `/v1/policy/reload` | `manage` | Reload the daemon policy file without restart |

---

## 5. MMVT 6 Recorded 2-Client Direct vs. Loopback HTTP Baseline

Reproduce with:

```bash
uv run python bench/daemon/run_mmvt6_baseline.py
```

Measured on macOS (`darwin`), `hotmem-hash-v1` embedder, 2 callers (`caller-a`, `caller-b`), 25 reads/client after warmup (50 samples per mode), process max RSS `69.09 MB`:

| Mode | Samples | Avg Payload Bytes | Client Wall p50 (ms) | Client Wall p95 (ms) | Server `trace_ms` p50 | Server `trace_ms` p95 | Approx Transport Delta p50 (ms) | CPU Time (s) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct (`HotMemRuntime`) | 50 | 2,168 | 0.390 | 0.918 | 0.380 | 0.900 | — | 0.0238 |
| Pooled Loopback HTTP (`HotMemClient`) | 50 | 2,038 | 1.894 | 2.768 | 0.520 | 0.850 | 1.325 | 0.0925 |

---

# Design intent: the cassette and the detachable player

## HotMem: carry the brain, change the player

An incident is contained. The next shift is coming online. One agent has the
investigation, another has the recovery runbook, and the person taking over
needs to know why the team chose to roll back. They should be able to continue
from that decision, with its evidence and permissions intact.

This is the experience HotMem is being built to deliver: a working brain that
belongs to its owner and can travel between compatible tools. **JSONL is the
memory cassette. HotMem is the recorder and player. The management UI is the detachable
interface that keeps the recorder in view.**

For a developer, the job is concrete. Keep memory durable. Give each client the
right view. Make the service survive a disconnected agent, a closed window, and
a restart. Then show two people and their agents continuing useful work.

The sections below preserve the original v0.2.7 design narrative for the shared daemon, memory cassette, and management surface (#112). The runbook above is the implemented operator contract.

## The cassette: knowledge you can carry

A cassette keeps the recording when you change players. HotMem applies that
idea to the decisions, facts, preferences, provenance, and operational context
that make an agent useful beyond a single conversation.

JSONL is the canonical portable record stream. A manifest adds identity,
compatibility information, and integrity checks. Attachments or declared file
dependencies carry material that a record only references. A file URI inside
JSONL is a pointer; a portable package must account for the bytes behind it.
See [Snapshot v2](snapshot-v2.md) and [memory portability](agent-memory-portability.md)
for the existing formats and restore behaviour.

SQLite holds live runtime state. Indexes and caches make that state useful at
speed. They serve the cassette contract: knowledge should remain inspectable
and recoverable when a machine, model, or harness changes. Full-fidelity
round trips still depend on the portable-state work linked at the end of this
guide; the current database cannot simply be discarded on that assumption.

Ownership covers what the system learns about the recording, too. Summaries,
embeddings, relationships, inferred structures, and access history can reveal
as much as the original text. A derived memory needs provenance, a reviewable
status, and access restrictions at least as strong as its sources. Combining
permitted facts can also reveal a sensitive relationship. Relationships remain
explicit linked records, without requiring a graph database.

## Start with the current runtime

You can already run the storage and retrieval foundation locally. In a clean
directory, using an environment with HotMem installed, start the HTTP sidecar:

```bash
hotmem serve --mount ./incident-brain
```

The mount makes the store selection explicit. In a second terminal, save a
synthetic operations decision and retrieve it:

```bash
curl --fail -sS http://127.0.0.1:8711/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"identifier":"ops/demo","fact":"Incident DEMO-17: roll back the worker release before resuming the queue."}'

curl --fail -sS http://127.0.0.1:8711/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"DEMO-17 worker rollback","top_k":1,"max_chars":500}'

curl --fail -sS http://127.0.0.1:8711/v1/health
```

The write returns a memory identifier and content hash. Search returns ranked
memory objects, and health reports the selected store. This gives you a real
starting point for the incident workflow. Use synthetic data on loopback: this
example does not establish separate client permissions or a managed daemon.
The default hash embedder requires no model download; semantic model support
is a separate configuration choice.

Current MCP takes a different route: the stdio server opens `MemoryDB` directly.
It does not call FastAPI. Several CLI operations also open the database directly,
while the Python SDK uses persistent HTTP clients. These paths share useful
building blocks, but do not yet form the single controlled service below.

## The recorder: one daemon, one operation boundary

In the target architecture, the recorder stays running while clients come and
go. It owns the configured brain/store and the resources needed to operate it.
A CLI command, an MCP call, and an action in the management window reach the
same memory operations under the same rules.

```text
agents / MCP relay     CLI / SDK     management UI / detachable view
           \              |                     /
                 configured daemon endpoint
                            |
           identity -> scope -> validation -> operation
                            |
             SQLite runtime + owned backing files
                            |
              JSONL cassette / verified package
```

Extract the shared operations from the transport handlers. Keep existing
storage, retrieval, snapshot, hydration, and handoff implementations underneath.
An adapter translates a request; the service decides what the caller may do
and how that operation changes or exposes memory. This puts persistence,
provenance, policy, and failure handling in one place.

The first port-driven candidate is the existing loopback HTTP service, with a
thin stdio relay for MCP hosts that need it. Native HTTP MCP, if the selected
clients need it, should reach the same operation layer. These are proposed
seams, not extra commands available today. Begin with one service; additional
processes or protocols need an observed reason to exist.

In daemon mode, reads, writes, relationship changes, score updates, imports,
exports, and hydration pass through this boundary. Background work follows the
same rules. Clients select an endpoint and verify the intended brain. A failure
must identify the unavailable transport, process, or store; silently opening a
temporary or different database would break continuity.

Existing embedded use needs a documented compatibility path. Offline
maintenance needs exclusive store ownership while the daemon is stopped or
otherwise safely excluded. Neither route automatically inherits daemon policy.

## Follow a memory from capture to recall

Return to the incident. The investigation agent has a detailed private note;
the next shift needs an approved rollback decision. That decision should enter
HotMem with its source attached, survive a restart, and be retrievable without
giving the second agent the private investigation.

The proposed write path starts by authenticating the client and authorising
the selected source and operation. Where an adapter extracts memory, it reads
only that approved source and performs sensitive processing locally. The
service validates the record and provenance, commits the write, then returns
an acknowledgement and a scoped activity event. A durable write cannot be
acknowledged before it is committed.

Recall starts at the same boundary. Apply the client's scope to candidate
selection, retrieve and rank permitted context, and return a bounded result.
Exports, direct-ID lookups, metadata, file hydration, and activity streams need
the same protection. Hiding a record from search alone does not protect it.

External embedding or inference calls also disclose information. Raw memory
that cannot be shared must remain in a trusted local environment. Routine logs
should describe operations without retaining private payloads or sensitive
identifiers. Caches, errors, counts, and combined views must respect that
boundary too.

Once context reaches a cloud agent, it may enter that provider's history or
logs. Information typed directly into the agent is already there. Revocation
can stop future reads; it cannot erase released copies. HotMem's defensible
promise is owner-controlled, minimal disclosure. Existing redacted handoff is
a useful foundation, not a universal runtime filter.

## The detachable player: keep the recorder in view

The operator needs to know which brain is loaded, which agents are connected,
and whether the handoff is moving. The default management view should answer
those questions directly: service health, cassette/store identity, client
permissions, pending privileged actions, and recent authorised activity.

The detachable management UI carries that view into a detachable player. Keep it beside the incident
channel, open the relevant stream, and return to full management when needed.
Both views use the daemon. Closing either window leaves the recording and
other clients running.

The first useful relay is small: actual connection state and permitted memory
operations. From that base, contextual chat can follow an individual stream;
combined discovery can connect authorised streams; alerts can surface denied
access, failures, or suspicious activity. Source-linked insights can reveal
the brain's structure without granting access to every underlying note.

This view knows what HotMem observes. A memory connection cannot report every
action inside a harness. Shared chat must not import another user's private
stream. Access history can inform relevance, but repeated reads are evidence
of use rather than truth. How retries count and how access changes a score
remain explicit choices; they do not block the first relay proof.

## Make the fast path dependable

The recorder should be ready when a client asks. Keep the configured store,
model, and useful index resources warm; reuse connections and avoid opening
or rebuilding them per operation. Resolve file-backed payloads only when
needed, and return the smallest useful approved context.

A local HTTP call has no internet round trip, but still pays for serialisation,
copying, scheduling, and protocol work. A stdio-to-daemon relay adds a hop to
today's direct MCP path. Measure that cost alongside queueing, embedding,
retrieval, ranking, persistence, and response construction. FastAPI alone does
not tell you which part is slow. Current Python clients already reuse
`httpx.Client` and `AsyncClient`; preserve that behaviour.

The next performance experiment is narrow: one small synthetic store, the same
active embedder and bounded query, and two clients. Compare direct operations
with warm loopback HTTP using reused connections. Separate cold startup from
warm calls; record sample count, payload size, elapsed time, and operation
timings already available. No measurements have been collected for the
proposed architecture yet.

Let the result choose the next optimisation. If request chatter dominates,
consider batching. If loopback transport dominates, compare a Unix-domain
socket or Windows named pipe with the same operation contract. Any result cache
must be keyed by client/policy/store revision and invalidated on relevant
changes, including revocation. Remote requests should reuse secure connections
and transfer approved results. Replicas need the existing identity and replay
contracts before they can maintain a reliable copy.

Responsiveness also depends on scheduling. Slow extraction, imports, snapshots,
and embedding must not hold health checks or unrelated bounded reads hostage.
Use bounded concurrency, queues where justified, and explicit cancellation.
Keep resource consumption observable without adding a telemetry service or
broad benchmark programme to block the first test. A new protocol, broker, or
native rewrite needs evidence that it resolves an actual bottleneck.

Dependability continues through shutdown. Define start, status, stop, reconnect,
store ownership, bounded contention, and recovery. Startup is an explicit
operator or service-manager action, not a process spawned by a memory tool.
A lost response leaves a mutation's outcome uncertain: retries need stable
identity and deduplication semantics. A client disconnect must not stop the
service. Set latency and resource budgets from representative evidence before
declaring performance acceptance.

## Put authority at the right boundary

An agent's permission to retrieve a runbook should not let it approve its own
access to the investigation. Management authority must be distinct from
ordinary memory access. The initial deployment needs an explicit credential
and scope model, protected credential storage, and defined retention.
Loopback, an overlay network, an actor-name parameter, and a browser origin
check are not authentication. Browser management needs both authenticated
authority and protection against cross-origin requests.

The operating system is part of that boundary. A harness that can read the
database, backing files, keys, or process memory can bypass the API. Protect
those resources with permissions and, where required, separate identities or
isolation. Two clients under the owner's OS account can demonstrate functional
authorisation; they do not demonstrate protection from a malicious process
with that owner's privileges. Checksums establish integrity, not encryption
or signing.

## First mission: hand the incident to the next shift

Use a synthetic incident with one private canary note and one approved shared
runbook. Client A can access the private investigation. Client B can use the
shared operations context. Both connect to one daemon, with a small relay view.

| Action | Evidence required |
| --- | --- |
| A records the approved rollback decision; B retrieves it. | Both use the same service/store, with visible provenance and no manual copying. |
| B attempts private search, direct lookup, export, and mutation. | Each exposed route enforces scope; the canary stays absent from B's responses, logs, and relay stream. |
| Disconnect/reconnect A; close/reopen the detachable view. | B keeps working. The daemon stays available and the view reports real connection state. |
| Restart the daemon, including one bounded abrupt-stop check. | Acknowledged writes survive; reconnection is explicit and safe retries do not duplicate logical memory. |
| Export permitted JSONL/package content and restore a separate clean store. | The decision and provenance survive; dependencies/omissions are visible and repeat hydration follows the supported idempotence contract. |
| Repeat a small bounded workload with both clients. | Record latency/resource observations, separating transport from operation cost and stating remaining uncertainty. |

Start with two test clients to isolate the backend. Use two actual configured
agent harnesses before claiming the two-agent showcase works. A working port
does not establish authorisation, and a missing UI cannot count as a completed
relay. Record failures, implement the smallest missing slice, and repeat the
affected check. Stop when this local workflow answers its question; the next
experiment should follow a demonstrated gap.

## Four views of the same working brain

The incident is one entry point. The same foundation should make increasingly
collaborative work visible, with ownership and access carried through each view.

| View | Experience | Delivery boundary |
| --- | --- | --- |
| Digital brain | Change harnesses while carrying the cassette and approved context. | Existing portability foundation; shared-daemon disclosure proof pending. |
| Company/ops brain | Continue shared decisions while private investigation stays scoped. | The first two-client incident workflow. |
| Remote brain | An authorised device or teammate reaches a selected brain or replica. | Follow local identity/policy proof; remote synchronisation is a separate capability. |
| Overmind | Follow authorised activity across selected brains and inspect source-linked insights. | Product direction; aggregate views inherit access boundaries. |

A selective share grants selected knowledge to an audience. A clone creates
an explicit copy with independent consequences. Synchronisation maintains a
relationship between changing stores. Keep these actions distinct and make
their ownership and revocation behaviour visible. The same controls allow
personal sharing with a friend without granting an entire cassette.

## What Flats teaches this design

[`gosuda/flats`](https://github.com/gosuda/flats) is a useful comparison for the
service and sharing experience. Its [README](https://github.com/gosuda/flats/blob/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb/README.md)
describes a resident local host with CLI, HTTP, MCP, and console surfaces.
Approval applies to specific lifecycle transitions; live collaboration is not
uniformly gated by publication. Its console model distinguishes CSRF protection
from authentication.

The [agent guide](https://github.com/gosuda/flats/blob/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb/docs/agents.md)
documents a stdio relay to a selected host and read-only compatibility discovery.
The [networking guide](https://github.com/gosuda/flats/blob/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb/docs/networking.md)
describes per-flat NetBird peers, private access policy, and separately enabled
public exposure. Reachability and publication are separate controls.

For HotMem, the useful pattern is an owner-run service with thin clients,
clear approvals, and deliberately scoped sharing. An approval should identify
the exact memory version/selection and audience; ordinary permitted reads
should remain fluid. A future private overlay could connect a team's approved
devices, while HotMem still enforces application-level access.

A shared incident brief can make this collaboration tangible by revealing
authorised decisions and evidence. Enterprise and real collaborative workflows
are the focus, rather than developer productivity or artifact hosting. This
design introduces no Flats/NetBird dependency, public tunnel, Go rewrite, or
single-binary commitment. The comparison is a source/documentation review dated
2026-10-21 at revision
[`d35a4f49`](https://github.com/gosuda/flats/tree/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb),
not evidence from running Flats.

## Implementation map

HotMem is a formal KnowGuard product. Daemonite is its planned desktop console;
HotMem owns its interface, theme, tray integration, daemon/store operations
and authorization. The approved [desktop direction](daemonite-desktop.md) defines
the v0.3.0 shell and packaging baseline. The [Vision and Canon](vision-and-canon.md) defines the enduring
product commitments. [#112](https://github.com/KnowGuard-AI/HotMem/issues/112)
holds this guide's v0.2.7 daemon/relay direction and implementation evidence.
Grok remains excluded from the integration/showcase programme.

Begin with these existing code boundaries, inspected on 2026-10-21 at
[main revision `c6ed9ae`](https://github.com/KnowGuard-AI/HotMem/tree/c6ed9ae60bf9ee21320ede63c4357d7cb34c1633):

| Component | Starting point |
| --- | --- |
| [`mcp_server.py`](https://github.com/KnowGuard-AI/HotMem/blob/c6ed9ae60bf9ee21320ede63c4357d7cb34c1633/src/hotmem/mcp_server.py) | Direct stdio/database operations to connect to the shared service. |
| [`server.py`](https://github.com/KnowGuard-AI/HotMem/blob/c6ed9ae60bf9ee21320ede63c4357d7cb34c1633/src/hotmem/server.py) | HTTP handlers and module-global state to separate into explicit runtime ownership. |
| [`client.py`](https://github.com/KnowGuard-AI/HotMem/blob/c6ed9ae60bf9ee21320ede63c4357d7cb34c1633/src/hotmem/client.py) | Reused HTTP clients to retain behind the common endpoint contract. |
| [`cli.py`](https://github.com/KnowGuard-AI/HotMem/blob/c6ed9ae60bf9ee21320ede63c4357d7cb34c1633/src/hotmem/cli.py) | Explicit store/endpoint selection and direct-database compatibility paths. |
| [`search.py`](https://github.com/KnowGuard-AI/HotMem/blob/c6ed9ae60bf9ee21320ede63c4357d7cb34c1633/src/hotmem/search.py) | Hybrid retrieval/stored importance; runtime scoping and access-driven scoring are not established here. |

Host setup is tracked in [#109](https://github.com/KnowGuard-AI/HotMem/issues/109),
export privacy in [#110](https://github.com/KnowGuard-AI/HotMem/issues/110), and
session context in [#111](https://github.com/KnowGuard-AI/HotMem/issues/111).
Portable-state acceptance remains in
[#94](https://github.com/KnowGuard-AI/HotMem/issues/94),
[#95](https://github.com/KnowGuard-AI/HotMem/issues/95),
[#96](https://github.com/KnowGuard-AI/HotMem/issues/96),
[#97](https://github.com/KnowGuard-AI/HotMem/issues/97),
[#98](https://github.com/KnowGuard-AI/HotMem/issues/98),
[#99](https://github.com/KnowGuard-AI/HotMem/issues/99), and
[#100](https://github.com/KnowGuard-AI/HotMem/issues/100).

The shared release target is **v0.2.7**: the portable-state foundation, secure
shared service, minimal management/detachable relay, relationship traversal,
and the platform showcase/qualification tracks all use that release number.
The first executable slice remains the local two-client incident workflow;
the other release deliverables follow in independent slices. The
[Hermes showcase #70](https://github.com/KnowGuard-AI/HotMem/issues/70),
[OpenClaw boundary #71](https://github.com/KnowGuard-AI/HotMem/issues/71) and
[showcase #72](https://github.com/KnowGuard-AI/HotMem/issues/72),
[walk #105](https://github.com/KnowGuard-AI/HotMem/issues/105),
[deep map #106](https://github.com/KnowGuard-AI/HotMem/issues/106), and
[platform roadmap #113 with children #114–#117](https://github.com/KnowGuard-AI/HotMem/issues/113)
are included in the v0.2.7 release plan. Their tests and acceptance remain
pending; sharing a release target does not make them prerequisites for the
first local experiment. Broader remote and overmind features remain product
direction unless a linked ticket explicitly defines their delivery.

The published v0.2.6 Windows correction remains the current runtime baseline.
This guide and the v0.2.7 milestone describe upcoming work, not a package
publication or evidence that those features have shipped.

The remaining choices are specific: the first credential/scope model, supported
service-manager/platform combination, measured latency/resource budgets,
detachable UI packaging, and the next remote collaboration boundary. Resolve
them through working slices and recorded results. This guide preserves the
2026-10-21 product decisions; it does not complete those implementation tickets.
