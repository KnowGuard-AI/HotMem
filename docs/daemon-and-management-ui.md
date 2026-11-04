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
        "--harness", "claude-code",
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

### Explicit Session & Harness Context, Generic Tags, and ID Lifetimes (#111)

- **Versioned session provenance (`hotmem-session-v1`):** Contextual writes record
  `{"envelope_version": "hotmem-session-v1", "caller_id": ..., "harness_id": ..., "session_id": ..., "source": ..., "namespace": ..., "store_id": ...}`
  in `memories.provenance_json`. Unknown envelope versions fail closed (`400 unsupported_envelope_version`); conflicting header vs. payload context fails with `400 context_conflict`.
- **Attribution vs. authentication:** `harness_id`, `session_id`, and `source` are writer attribution. Authentication and namespace/share access derive solely from the bearer token (`CallerCredential`). Credentials may optionally pin `allowed_sessions`, `allowed_sources`, and `allowed_harnesses`; claiming another caller's bound identity fails with `403 session_impersonation_denied`.
- **Canonical generic `tags` (`memories.tags`):** Model, project, and organization labels use canonical record tags (for example `{"tags": ["model:gpt-5", "project:hotmem", "org:knowguard"]}`) across HTTP (`/v1/add`), Python `HotMemClient` / `AsyncHotMemClient`, TypeScript `HotMemClient`, MCP `add_memory` (alongside `metadata`), and CLI `hotmem add --tag ...`. No dedicated model/project/org columns or parallel copies in `metadata` or `provenance` are created. Missing labels are omitted, `[]` is valid, and `model:*` labels can change per write within the same session.
- **Native vs. adapter-scoped session IDs:**
  - When a client or harness exposes a native conversation/session ID, supply that ID per conversation or per write (`X-HotMem-Session-Id`, SDK `session_id`, or MCP `add_memory(session_id=...)`).
  - Standard MCP stdio transport does **not** automatically expose a host's native conversation ID or automatic transcript capture. When `--session` / `HOTMEM_SESSION_ID` is passed to `hotmem mcp --url ...`, its lifetime is scoped to that relay process instance unless overridden per tool call. Do not assign one permanent static session ID across unrelated conversations; see **Native Client Task Synchronization (#125)** for per-hook native session wiring.
- **Writer preservation, retries, and observed activity:**
  - Reader context on `/v1/search`, `/v1/memories`, `/v1/memory/{id}`, and `/v1/memory/{id}/hydrate` never mutates stored writer provenance or tags.
  - Namespace-scoped logical identity `(namespace, content_hash)` preserves the original writer's stored record and tags while recording subsequent same-scope attempts in scoped `memory.deduplicated` events. Identical content in another namespace is a distinct record.
  - Idempotency-key retries (`X-HotMem-Idempotency-Key`) include `tags`, `harness_id`, `source`, `namespace`, and `metadata` in the request fingerprint; changed context or tags under a repeated key fail with `409 idempotency_key_conflict`.
  - Activity (`GET /v1/events`) reports only genuine HotMem-observed, permission-scoped operations (`memory.created`, `memory.deduplicated`, `snapshot.imported`, `handoff.applied`, etc.) and reuses existing `ttl_seconds` and event retention mechanisms without client-presence polling.

### Deterministic Exact-Identifier Recall (#125)

Semantic `search_memories` ranks by relevance and does not define which record
is current. When a caller needs the latest state for a known task list or other
logical stream, use `list_memories` with its exact identifier instead:

```json
{"identifier": "tasks/current", "order": "desc", "limit": 1}
```

The additive MCP tool is available in embedded and daemon-relay modes and uses
the same `GET /v1/memories` operation and authorization/disclosure boundary as
the Python SDK. Embedded reads honor the configured server namespace. Relay
results retain `memories`, `count`, `trace_ms`, and the runtime `disclosure`
envelope; embedded results include `memories`, `count`, and `trace_ms`.

Results are ordered by `created_at` with local insertion order as the tie-breaker
for same-second records. That tie-breaker is intentionally local to the current
store, not a portable sequence. Task-sync events therefore carry their own
durable observation order for reconstruction across snapshot/restore. `order`
defaults to `asc`; `limit` defaults to 100 and must be `1..1000` (invalid values
are rejected). Exact-identifier recall is deterministic in one store, but a
bounded result or `disclosure.omitted_count > 0` is not proof of a complete
history and must not be treated as authoritative.

### Native Client Task Synchronization (#125)

`hotmem task-sync` is an opt-in stdin/stdout hook adapter for persisting native
client task mutations as ordinary HotMem memories and recalling their reduced
current state. It uses the Python SDK against an explicitly configured daemon;
it does not open a private database, start a daemon, or change policy. The
adapter is not a distributed task engine.

#### Claude Code

The supported qualification target is the Claude Code CLI with native Task
tools and command hooks. Live validation for this change used Claude Code
`2.1.292`; Claude Desktop, IDE, web, and mobile variants are not covered by that
result. Task tools are model/configuration dependent: when necessary, set
`CLAUDE_CODE_ENABLE_TODO_TOOLS=1` and verify the tools available in the actual
session. A model still decides when to use them, so hook installation does not
guarantee automatic task creation.

- `PostToolUse` matching `TaskCreate|TaskUpdate` captures successful creates,
  status changes, dependency additions, owner/subject/description/activeForm
  changes, and native `deleted` tombstones. Failed updates and unrelated tools
  are ignored. Arbitrary native task metadata and legacy `TodoWrite` are not
  captured by this adapter version.
- `SessionStart` recalls the exact configured task list and injects a bounded
  reduced state. A command hook is required for startup recall because MCP
  servers are not yet initialized to serve `SessionStart` MCP hooks. Task
  subjects/descriptions are labeled as untrusted data; descriptions are omitted
  from automatic context by default. Use `--include-task-descriptions` only
  when bounded description context is acceptable.
- Claude's `session_id` becomes per-write HotMem session provenance and its
  `tool_use_id` becomes the source event ID. Initial logical task IDs are
  namespaced by client, native session, and native task ID. Transcript paths,
  cwd, permission mode, and arbitrary hook metadata are not persisted.
- To continue a task intentionally in a fresh Claude session, recreate only
  remaining tasks with the injected `task_id` in TaskCreate metadata:
  `{"hotmem_task_list_id": "<task-list-id>", "hotmem_task_id": "<task-id>"}`.
  The adapter validates and uses those namespaced continuation fields; no
  arbitrary task metadata is retained. Do not recreate completed tasks.
- Native task mutations do not expose the active model to this hook. Omit
  `model:*` unless another reliable per-write source exists; supply project/org
  labels only as explicit canonical `--tag` values. Empty tags are valid.

Example settings excerpt (use an absolute `hotmem` path when the client PATH is
not deterministic, and keep credentials out of committed files):

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1",
    "HOTMEM_URL": "http://127.0.0.1:8711",
    "HOTMEM_NAMESPACE": "ops/shared",
    "HOTMEM_STORE_ID": "<expected-store-id>",
    "HOTMEM_TASK_LIST_ID": "incident-demo-17"
  },
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "hotmem task-sync --client claude --tag project:hotmem", "timeout": 15}]}
    ],
    "PostToolUse": [
      {"matcher": "TaskCreate|TaskUpdate", "hooks": [{"type": "command", "command": "hotmem task-sync --client claude --tag project:hotmem", "timeout": 15}]}
    ]
  }
}
```

Set `HOTMEM_TOKEN` in the client process environment rather than storing the
bearer token in shared repository settings. If a user-local settings file must
contain credentials or a task list name, restrict its permissions (`0600`),
never commit it, and rotate the synthetic/development token if exposed.

The task-list ID is an explicit workstream key shared only by intentional
continuation sessions. It is distinct from the native session ID; do not set a
permanent `HOTMEM_SESSION_ID` across unrelated conversations. The adapter has
no static session option and supplies each hook's native session per write, so
it does not conflict with relay startup context bindings.

Each event uses identifier `hotmem/task-sync/<task-list-id>`, a durable
`observed_at` ordering value, and an idempotency key derived from task-list ID,
harness, native session, and native tool-use ID. An exact retry returns the
original memory and writer attribution; because `observed_at` is regenerated,
the adapter recognizes the expected idempotency fingerprint conflict by reading
the existing event before suppressing it. A new native tool-use ID remains a
new observation even when it repeats an earlier state, so A→B→A histories
reconstruct correctly; altered content under a repeated key still fails.

Recall reads the disclosure envelope and processes at most 1000 ordered events.
Reaching that bound, policy omission (`omitted_count > 0`), malformed history,
an update without its create, an oversized context, or an ambiguous native alias
causes recall to fail closed with a visible warning instead of claiming a
complete current state. Context is capped at 8000 characters; larger task lists
must use authorized MCP `list_memories` detail instead. Native deletion is a
task-event tombstone, not HotMem memory deletion or portable replay, and does
not require #98.

Task events carry their observation order in the record, so snapshot/handoff
round trips preserve task state even though the generic list's `rowid`
tie-breaker is local. Hook processes using the same URL, store, namespace and
task-list configuration on one host coordinate timestamps through a private SQLite state file (`~/.hotmem/task-sync.sqlite3`, overridable
with `HOTMEM_TASK_SYNC_STATE`). A duplicate timestamp fails recall closed.
Independent hosts must not write the same task list without coordinated clocks.
Preserve the state file across restarts; task events remain in the HotMem store.

Hook events are stored in the private local state file before delivery. If the
daemon is unavailable, the hook reports the failure and retains the event; the
next hook or `hotmem task-sync --client <client> --task-list-id <id> --url
<url> --flush` retries queued events in order. Use the same URL, store ID,
namespace and credential to replay; bearer tokens are never stored. The state
file contains task text and is subject to local filesystem protection and
retention. Preserve it until queued events have been delivered. Replaying an
acknowledged event is safe via idempotency. Independent adapters must not
concurrently mutate one task-list ID;
use separate task-list IDs for unrelated workstreams. Long task histories are
not compacted by this adapter, so the 1000-event completeness bound is an
operational limit.

**Recall is not native task-store restoration.** A fresh Claude session can
read current and remaining work from injected HotMem context and through
`list_memories`, but its native `TaskList` remains empty unless the user/agent
recreates remaining tasks with the continuation metadata described above. Such
recreation is instruction-driven and is not performed implicitly. Reader
credentials and namespace/share/disclosure checks apply to every recall;
self-reported tags, sessions, or harness labels grant no permissions. Task text
is persisted in HotMem and is subject to that store's retention and disclosure
policy; it can contain sensitive data even after native task deletion.

#### Codex CLI

The qualified task-sync surface is Codex CLI `0.164.0` `exec` with command
hooks and `update_plan`; live validation used `gpt-6.1-sol`. Interactive TUI,
IDE, Desktop, Cloud, and app-server variants are not covered by that result.
Headless exec did not expose `update_plan` by default in the validated
configuration, so enable it explicitly with the public TOML setting
`tools.update_plan.enabled=true`. The `exec --json` `todo_list` projection keeps
only text plus a completed boolean and is not the persistence seam because it
loses `in_progress`; `PostToolUse(update_plan)` retains the complete native
plan and all three statuses.

- `PostToolUse(update_plan)` stores each full plan snapshot, optional
  explanation, source `tool_use_id`, and source `turn_id`. Fresh state replaces
  the previous Codex plan for that task list, including item removal and plan
  clearing. It does not replace Claude tasks if a task list is intentionally
  shared between clients.
- Codex plan items have no native task IDs or dependency fields. The adapter
  generates `codex:*` item keys from exact step text and duplicate occurrence;
  duplicate steps remain distinct and order is preserved. Renaming a step
  creates a new surrogate identity. No dependency semantics are inferred.
- Only an accepted `Plan updated` response is persisted; failed plan responses
  are ignored. Native `session_id`, `turn_id`, and per-write `model` are read
  from the hook. The model becomes a canonical `model:*` tag, replacing or
  removing a configured model tag for that write; project/org labels remain
  explicit canonical tags.
- `SessionStart` injects the bounded reduced current plan and remaining work as
  untrusted data. Descriptions are omitted by default and the same 8000-character
  context limit applies. As with Claude, recalled context does not prove native
  plan storage was restored.

Live qualification used reviewed session-flag configuration. The essential
flags are shown below; use an absolute adapter path and explicit arguments when
client hook environments are not deterministic. Pass the bearer token through
the protected client/process environment (`HOTMEM_TOKEN`), never in the `-c`
argv shown in process listings and logs. Set a short timeout so an unavailable
daemon does not retain the default 600-second hook budget:

```sh
# Set in the client's protected environment, not in a committed file or argv.
export HOTMEM_TOKEN='<task-writer-token>'

codex exec \
  --enable hooks \
  -c 'tools.update_plan.enabled=true' \
  -c 'hooks.SessionStart=[{hooks=[{type="command",command="hotmem task-sync --client codex --task-list-id incident-demo-17 --url http://127.0.0.1:8711 --namespace ops/shared --store-id <expected-store-id> --tag project:hotmem",timeout=15}]}]' \
  -c 'hooks.PostToolUse=[{matcher="update_plan",hooks=[{type="command",command="hotmem task-sync --client codex --task-list-id incident-demo-17 --url http://127.0.0.1:8711 --namespace ops/shared --store-id <expected-store-id> --tag project:hotmem",timeout=15}]}]' \
  'Create and update a synthetic plan'
```

The adapter caps injected task context at 8000 characters. Configure the
client's hook `additionalContextLimit` below its documented spill threshold if
the host writes oversized output to disk; task text is data, not instructions,
and should not contain secrets.

#### Measuring agent overhead

Compare the same synthetic prompt, client version and model with hooks disabled
and enabled against a disposable daemon. Capture whole-process wall time and
model-visible tool names/counts from the client's JSON stream; separately
record callback invocations, persisted events and HotMem operation timings.
One paired run is a smoke measurement, not a stable latency estimate: model
startup, inference and cache variation are included. Keep the recorded product
results and limits in [TECH-20](https://linear.app/knowguardai/issue/TECH-20).
For MCP qualification, verify the real tool-call result, not just discovery or
an attempted invocation; an isolated test may approve only the specific
read-only tool it exercises.

The isolated live test also used `--ignore-user-config`, `--ignore-rules`,
`--ephemeral`, read-only sandboxing, and `--dangerously-bypass-hook-trust` on a
reviewed temporary configuration. The bypass is not a recommended default:
normal installations should review and trust their hook sources rather than
skipping trust enforcement. Project-file hook discovery was not qualified by
the headless test; session flags above are the verified configuration.

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

Host setup is delivered in [#109](https://github.com/KnowGuard-AI/HotMem/issues/109) ([PR #124](https://github.com/KnowGuard-AI/HotMem/pull/124)),
scoped runtime disclosure and export privacy in [#110](https://github.com/KnowGuard-AI/HotMem/issues/110) ([PR #126](https://github.com/KnowGuard-AI/HotMem/pull/126)),
session/harness attribution and canonical `tags` in [#111](https://github.com/KnowGuard-AI/HotMem/issues/111) ([PR #132](https://github.com/KnowGuard-AI/HotMem/pull/132)),
and Codex/Claude native task synchronization and real-client discovery in [#125](https://github.com/KnowGuard-AI/HotMem/issues/125) / TECH-20 ([PR #133](https://github.com/KnowGuard-AI/HotMem/pull/133)).
Portable mount identity and opaque `hmc1` replay cursors are delivered in
[#94](https://github.com/KnowGuard-AI/HotMem/issues/94) ([PR #136](https://github.com/KnowGuard-AI/HotMem/pull/136)) (`mount_id` logical
workspace identity vs. `replica_id` runtime SQLite instance vs. `history_id`
event-log epoch vs. `runtime_generation` process lifespan vs. `store_id` daemon
routing; multi-writer ordering and conflict resolution remain intentionally
undefined). Canonical semantic Snapshot v2 identity (`snapshot_id_version: 2`,
`hotmem-snapshot-id-v2` over all 27 `#99` `preserve` columns, attachment
references, and semantic namespace scope, with explicit v1 legacy compatibility)
is delivered in [#96](https://github.com/KnowGuard-AI/HotMem/issues/96) ([PR #137](https://github.com/KnowGuard-AI/HotMem/pull/137)).
Replay-safe state mutations (`memory.updated`) and tombstones (`memory.deleted`,
`memory.forgotten`) are delivered in [#98](https://github.com/KnowGuard-AI/HotMem/issues/98) ([PR #139](https://github.com/KnowGuard-AI/HotMem/pull/139)),
Memory Record v2 round-trip fidelity in [#99](https://github.com/KnowGuard-AI/HotMem/issues/99) ([PR #135](https://github.com/KnowGuard-AI/HotMem/pull/135)),
and namespace-safe logical identity in [#100](https://github.com/KnowGuard-AI/HotMem/issues/100) ([PR #134](https://github.com/KnowGuard-AI/HotMem/pull/134)).

**Release milestone boundaries:**

- **v0.2.7 ([#112](https://github.com/KnowGuard-AI/HotMem/issues/112) / Linear TECH-18):** Ships the owned-memory core and local collaboration foundation (#94, #96, #98, #99, #100, #109, #110, #111, #125), the minimal detachable management interface (`hotmem status --url` and `hotmem playground --url`), the two-harness incident decision workflow, and the integrated two-client performance/responsiveness baseline.
- **v0.2.8 ([#131](https://github.com/KnowGuard-AI/HotMem/issues/131)):** Holds the showcase, ecosystem, and follow-up portable-clone/traversal scope: [Hermes showcase #70](https://github.com/KnowGuard-AI/HotMem/issues/70), [OpenClaw boundary #71](https://github.com/KnowGuard-AI/HotMem/issues/71) and [showcase #72](https://github.com/KnowGuard-AI/HotMem/issues/72), [portable clone reconciliation #95](https://github.com/KnowGuard-AI/HotMem/issues/95), [public-language audit #97](https://github.com/KnowGuard-AI/HotMem/issues/97), [`memory_walk` #105](https://github.com/KnowGuard-AI/HotMem/issues/105), [`memory_map` #106](https://github.com/KnowGuard-AI/HotMem/issues/106), and [personal-agent platform roadmap #113 with children #114–#117](https://github.com/KnowGuard-AI/HotMem/issues/113).
- **v0.3.0 ([#130](https://github.com/KnowGuard-AI/HotMem/issues/130) & [#129](https://github.com/KnowGuard-AI/HotMem/issues/129)):** Holds the full Daemonite desktop application (Tauri 2/Rust tray shell, interactive memory management, safe swaps, and automated desktop release packaging).

---

## Integrated v0.2.7 Acceptance Evidence (#112 / TECH-18)

### 1. Minimal Management / Detachable Interface (Row 8)

HotMem v0.2.7 provides a zero-dependency CLI/terminal detachable management view through `hotmem status --url ...` (`src/hotmem/ui.py`, `src/hotmem/cli.py`) and `hotmem playground --url ...` (`src/hotmem/playground.py`), backed by policy-enforced `GET /v1/health` and `GET /v1/events` (`src/hotmem/runtime.py`):

- **Service, store, client, permitted activity, and permission state:**
  - `GET /v1/health` (when `policy_enforced=True`) returns `store_id`, `mount_id`, `store_state` (`verified_empty` | `empty_scope` | `ready`), `runtime_generation`, `policy_revision`, `observed_at`, `freshness_model: "poll_and_event_observed"`, `permission_state` (caller identity, `is_management`, permitted `namespaces`, `operations`, `active_share_approvals`, plus non-secret `active_caller_ids`, `revoked_caller_ids`, and `revoked_share_approvals` for management callers), `observed_clients` (`observation_basis: "recorded_events"`), and `recent_activity` (`observation_basis: "recorded_events"`, `latest_seq`, `events`). Unauthenticated `/v1/health` keeps its 5-key golden contract unchanged.
- **Truthful freshness & disconnect/reconnect:**
  - The detachable console explicitly reports poll- and event-observed state (`freshness_model: "poll_and_event_observed"`, `observation_basis: "recorded_events"`) and never claims unobserved live client presence.
  - When the daemon becomes unreachable, `_HttpBackend.status_snapshot()` transitions `connection_state` to `"disconnected"` while preserving `last_connected_at`, `stale_age_s`, `store_id`, `runtime_generation`, and `last_error`. When the daemon returns, the console transitions to `"reconnected"` and surfaces `generation_changed=True` if `runtime_generation` rotated.
- **Authorized operations & close/reopen lifecycle independence:**
  - Operators can run `status`/`refresh`, `events`, `hygiene` (`GET /v1/hygiene`), `search`, `add`, `revoke caller <id>`, `revoke approval <id>` (`POST /v1/policy/revoke`), and `reload` (`POST /v1/policy/reload`) through the daemon.
  - Closing (`quit`) and reopening the console process has zero effect on the running daemon or active agent sessions (`tests/test_daemon_mmvt7_e2e.py::test_mmvt7_real_detachable_management_interface_close_reopen_and_actions`).

### 2. Explicit Two-Harness Incident Scenario (Row 9)

Verified across both automated MCP stdio relay subprocess tests (`tests/test_daemon_mmvt7_e2e.py::test_mmvt7_two_harness_mcp_incident_decision_and_full_route_isolation`) and a live two-harness execution on macOS (`darwin` arm64) against disposable mount `store-incident-112` (`mount_id: mnt-33cc41f17e3e4f33a1ddae273fb39f47`):

- **Client A (Writer):** OpenAI Codex CLI `codex-cli 0.164.0-alpha.11.1` (`codex exec --json`), native conversation `thread_id: 01a1a6f4-ed49-7683-acdd-a529395e4a42`, connected via `hotmem mcp --url ... --token tok-codex-a --harness codex-cli --session sess-codex-inc112-a --source mcp://codex-a --store-id store-incident-112`:
  1. Wrote private investigation canary `ops/incident-112/private-canary` (`memory_id: 67c40aefbb5c417892e5001df67b51d5`, `namespace: ops/private`, `metadata: {"classification": "private"}`, server `trace_ms: 1.33`).
  2. Wrote approved sanitized shared rollback decision `ops/incident-112/rollback-decision` (`memory_id: 0dbe324c798a469383631b0f684adb78`, `namespace: ops/shared`, `tags: ["model:gpt-6.1-sol", "project:hotmem-ops", "org:knowguard"]`, server `trace_ms: 0.65`).
- **Client B (Reader):** Anthropic Claude Code CLI `2.1.292 (Claude Code)` (`claude -p --output-format json`), native conversation `session_id: 35c8fac4-710a-4350-91e0-647f560d3e42`, connected via `hotmem mcp --url ... --token tok-claude-b --harness claude-code --session sess-claude-inc112-b --source mcp://claude-b --store-id store-incident-112` with `ShareApproval(approval_id="share-inc-112-decision", namespace="ops/shared", identifiers=("ops/incident-112/rollback-decision",), audience=("caller-b",), operations=("read",))`:
  1. Recalled `0dbe324c798a469383631b0f684adb78` via `mcp__hotmem__list_memories` and `mcp__hotmem__search_memories`, visibly preserving Writer A's `provenance` (`caller_id: "caller-a"`, `harness_id: "codex-cli"`, `session_id: "sess-codex-inc112-a"`, `source: "mcp://codex-a"`, `namespace: "ops/shared"`, `store_id: "store-incident-112"`) and `tags` (`["model:gpt-6.1-sol", "project:hotmem-ops", "org:knowguard"]`) distinct from Reader B's context.
  2. Verified `0` hits and zero disclosure of `67c40aefbb5c417892e5001df67b51d5` / `PRIVATE-CANARY-INC112` across MCP `search_memories`, `list_memories`, and all 28 HTTP routes (`GET /v1/memory/{id}` -> `404`, `POST /v1/memory/{id}/hydrate` -> `404`, `POST /v1/memory/hydrate-batch` -> `not_found`, `POST /v1/snapshot` -> `403`, `POST /v1/hydrate` -> `403`, `GET /v1/events`, `GET /v1/health`, `GET /v1/hygiene`, `GET /v1/vector-index/status`, `GET /v1/promotion/by-state`, `GET /v1/bundles` -> `403`, `POST /v1/discover` -> `403`, `POST /v1/policy/revoke` -> `403`).
- **Native vs. adapter session identity boundary:**
  - Lifecycle hooks (`hotmem task-sync --adapter claude-code` and `--adapter codex` in #125) receive the host's native `session_id` directly in the hook stdin JSON payload.
  - Standard MCP stdio tool calls do not automatically include the host's internal conversation UUID unless passed in tool arguments (`session_id`) or configured per session on the relay (`hotmem mcp --session <id>`). Setting `--session` per client relay keeps conversation identities distinct and auditable while `allowed_sessions` / `allowed_harnesses` / `allowed_sources` reject spoofing (`403 session_impersonation_denied`).
  - `model:*`, `project:*`, and `org:*` use canonical `tags` only; no dedicated schema columns were added, and empty optional `tags: []` remain valid.

### 3. Integrated Confidence, Portability, and Two-Client Baseline (Row 10)

- **Lifecycle, abrupt-stop (`SIGKILL`), and retry attribution (`test_mmvt7_integrated_incident_lifecycle_retry_and_portable_restore`):**
  - Acknowledged writes (inline shared decision + file-backed runbook + private canary) and `mount_id` survive both graceful daemon restart (`SIGTERM`) and abrupt termination (`SIGKILL`) with automatic stale-lock recovery.
  - Ambiguous retries via `X-HotMem-Idempotency-Key` and same-namespace content hash return the existing `memory_id`, preserve Writer A's original `provenance` and `tags`, and emit `memory.deduplicated` activity without duplicating logical records.
- **Portable export and clean restore (#94, #96, #98, #99, #100, #110, #111):**
  - Scoped `POST /v1/snapshot` exports only permitted records (`exported_count: 2`, `omitted_count: 1` for the private canary, with zero canary bytes on disk), records `snapshot_id_version: 2` (`hotmem-snapshot-id-v2`), `mount_id`, `replay_cursor` (`hmc1.*`), and `file_backed_references` (`source_checksum`).
  - Restoring into a separate clean daemon store (`store-clean-restore-112`) preserves logical identity, writer `provenance`, `tags`, and file-backed hydration; repeat hydration is idempotent (`loaded: 0`, `skipped_dupes: 2`).
- **Repeated two-client latency & resource baseline (`bench/daemon/run_mmvt6_baseline.py`, 2026-11-04 on `a1ac7e8` + #112 branch):**
  - **Setup:** macOS `darwin` arm64, Python `3.12`, SQLite WAL, `hotmem-hash-v1` (384-dim), 20 seeded memories (10 shared in `ops/shared`, 10 private in `ops/private`), 2 concurrent client contexts (`caller-a` and `caller-b`), 3 warmups + 25 measured searches per client (**50 measured reads per mode**):

| Path | Samples | Wall p50 (ms) | Wall p95 (ms) | Wall Mean (ms) | Server `trace_ms` p50 | Server `trace_ms` p95 | Approx. Loopback Transport Delta p50 / p95 (ms) | Avg Payload (bytes) | Process CPU (s) | Daemon RSS (MB) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct `HotMemRuntime` (in-process) | 50 | 0.568 | 0.894 | 0.616 | 0.560 | 0.880 | — | 2,451.5 | 0.0317 | — |
| Pooled Loopback HTTP (`HotMemClient`) | 50 | 1.872 | 2.312 | 1.880 | 0.750 | 1.010 | +1.164 / +1.382 | 2,289.6 | 0.0913 | 70.8 |

  - **Responsiveness under slow write contention (`tests/test_daemon_mmvt6_responsiveness.py`):** While a slow write holds `_write_slot` for `350 ms`, concurrent `GET /v1/health` and `POST /v1/search` reads complete in `< 200 ms`, and write-queue saturation returns bounded `503 write_queue_saturated` without wedging the server.
  - **Relationship to TECH-20 (#125) measurements:** TECH-20 measured single-run CLI hook capture (`claude-code` hook `51.66–60.25 ms`, `codex` hook `52.14 ms`, server `POST /v1/add` `0.81–2.07 ms`) and end-to-end LLM turn wall times (`6.22–15.46 s`, dominated by remote model inference). Those TECH-20 measurements are cited only for hook/turn behavior; the 50-sample table above is the canonical #112 repeated two-client daemon baseline. No synthetic numeric performance gate or telemetry framework is introduced.
