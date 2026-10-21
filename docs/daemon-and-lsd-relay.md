# Owned memory: daemon, JSONL cassette, and LSD relay

Status: owner-approved product direction and next experiment, recorded on
2026-10-21. This document does not announce implemented daemon, access-control,
UI, or remote-sharing capabilities. The holder for this discussion and its
showcase work is [#112](https://github.com/KnowGuard-AI/HotMem/issues/112).
The [Vision and Canon](vision-and-canon.md) remains the product constitution;
this note preserves the operational decisions, proposals, and open questions
from this discussion in one place.

## Decisions and product thesis

1. **JSONL is the memory cassette.** HotMem is the recorder/player. SQLite is
   the live runtime store; indexes and caches help operate it. The portable
   JSONL representation, with its manifest and any required attachments, must
   preserve the knowledge that matters when changing machines or harnesses.
   A JSONL record that references an external file does not itself contain that
   file. Complete portability must account for those dependencies explicitly.
2. **The daemon is the product foundation.** It must have dependable lifecycle,
   durable writes, recovery, concurrent clients, enforced access boundaries,
   bounded resource use, and measured performance. Feature completeness means
   satisfying a finite operational contract with observed evidence.
3. **LSD is the detachable player and relay interface.** It connects to HotMem
   to show and control authorised activity. Closing it must leave the daemon
   running. A small management surface and detachable view use the same backend.
4. **Prove the enterprise collaboration workflow first.** Preserve this
   discussion in docs and link it from #112; avoid another large roadmap or
   platform-by-platform implementation programme before the first useful test.

HotMem is a formal KnowGuard product. KnowB is a shadow brand, not HotMem's
product owner or a required runtime service. The KnowB connector was not exposed
in this session; no external brand material was retrieved. LSD's role here is
the owner's stated direction, not a verified claim about an existing LSD build.
The thesis is enterprise and real collaborative work: continuity of decisions,
operations, ownership, and access across people and agents. Artifact generation
and developer productivity are not the product thesis. Grok remains excluded
from the integration/showcase programme.

## Ownership includes derived knowledge

The cassette analogy asks whether every player should retain copies, traces,
fingerprints, embeddings, or a semantic model of a person's memories. HotMem's
direction is to keep the durable brain under its owner's control and give each
harness only the context approved for its task.

Raw memories, summaries, embeddings, relationship links, inferred structures,
and access history all need that boundary. Sanitising a summary does not
automatically make it safe: combinations of permitted facts can reveal more
than an individual fact. An inferred or meta-memory needs source provenance,
reviewable status, and access restrictions at least as strong as its sources.
Relationships stay explicit linked records, not a new graph database.

Disclosure policy belongs on the HotMem service boundary, before a result,
export, log, or UI stream leaves the protected store. Agent instructions alone
cannot enforce it. Raw processing must stay in a trusted local environment
when it cannot be disclosed; any external embedding or inference step also
counts as disclosure. The goal includes approved clones, remote copies, and
sharing with teammates or friends under the owner's control.

The practical limit is explicit: a cloud agent can retain context returned to
it, and HotMem cannot prevent a provider seeing information typed directly into
that provider. Revocation stops future access; it cannot erase already released
copies. The promise is controlled, minimal disclosure, not guaranteed absence
of traces in another system. The existing redacted handoff path is not a
universal runtime privacy filter.

## What the current code establishes

These observations describe the checkout reviewed on 2026-10-21; they are code
inspection, not a fresh runtime test:

| Existing path | Observation | Consequence for this work |
| --- | --- | --- |
| [`mcp_server.py`](https://github.com/KnowGuard-AI/HotMem/blob/3994821/src/hotmem/mcp_server.py) | MCP uses stdio and opens `MemoryDB` directly. | MCP does not inherently make an HTTP round trip; a host-managed process is not yet the proposed shared daemon. |
| [`server.py`](https://github.com/KnowGuard-AI/HotMem/blob/3994821/src/hotmem/server.py) | FastAPI opens the store and runs HTTP handlers; server state is module-global. | Reuse existing operations, but make service ownership/lifetime explicit before relying on multiple runtimes or clients. |
| [`client.py`](https://github.com/KnowGuard-AI/HotMem/blob/3994821/src/hotmem/client.py) | Python clients reuse `httpx.Client` / `AsyncClient`. | Preserve connection reuse; do not claim that every SDK call opens a new connection. |
| [`cli.py`](https://github.com/KnowGuard-AI/HotMem/blob/3994821/src/hotmem/cli.py) | Several commands operate on a database directly; MCP can use a temporary store without explicit configuration. | Shared-daemon mode needs one explicit endpoint/store and actionable failures, not silent fallback. |
| [`search.py`](https://github.com/KnowGuard-AI/HotMem/blob/3994821/src/hotmem/search.py) | Hybrid ranking returns memory text using relevance and stored importance. | Scoped disclosure and access-driven scoring are not demonstrated by this path. |
| Snapshot/handoff | Portable formats, verification, and redacted consented handoff exist. | Preserve their contracts; export privacy and transport security are different guarantees. |

The full vision is not complete merely because storage and retrieval exist.
Client identity, runtime authorisation, shared-service lifecycle, the LSD UI,
and remote collaboration still require implementation and observed tests.
Full-fidelity portable state also remains tied to #94–#100; do not describe the
current SQLite store as entirely disposable before those criteria are met.

## Unified backend and operation routing

The proposed daemon owns one configured brain/store and its operation context.
Extract shared memory operations from transport handlers, retaining the existing
storage, search, snapshot, hydration, and handoff implementations underneath.
Transport code translates requests; it must not maintain independent policy,
ranking, or persistence rules.

```text
agents / MCP relay     CLI / SDK     management UI / detachable LSD
           \              |                     /
                 configured daemon endpoint
                            |
           identity -> scope -> validation -> operation
                            |
             SQLite runtime + owned backing files
                            |
              JSONL cassette / verified package
```

The first port-driven candidate is the existing loopback HTTP service, with a
thin stdio MCP relay for clients that require stdio. A native HTTP MCP endpoint
can use the same service if the tested clients need it. These are proposals,
not commands or interfaces available today. Keep a single application boundary;
there is no demonstrated need for additional microservices.

In daemon mode, all authorised memory reads, writes, relationship changes,
score updates, imports, exports, and hydration go through that boundary.
Background work uses the same policy and operations. CLI/SDK clients select
the endpoint and verify the intended store. An explicitly supported offline
maintenance mode would need exclusive ownership rather than bypassing the live
daemon. Preserve existing embedded use through a documented compatibility path;
it must not be described as having daemon policy enforcement automatically.

Daemon authority does not make the machine a sandbox. A harness able to read
the SQLite file, backing files, keys, or process memory can bypass an API.
Protect those resources with OS permissions and, where required, separate
identities or isolation. Two clients sharing an OS user is a functional test,
not evidence against a malicious process running with the owner's privileges.

## Latency and resilience: concrete options, measured in order

Loopback requests do not incur an internet round trip. They still have protocol,
serialisation, scheduling, and copying costs; a stdio-to-daemon relay adds a hop
relative to today's direct MCP path. FastAPI is not, by itself, evidence of the
dominant cost. Separate transport time from queueing, embedding, retrieval,
ranking, persistence, and response construction before choosing another stack.

Start with the smallest comparison: the same small synthetic store, active
embedder, bounded query, and two clients using current direct operations versus
the existing loopback HTTP path with reused connections. Record cold startup
separately from repeated warm calls, sample count, payload size, elapsed time,
and operation timings where already available. This baseline answers whether
transport is worth optimising; it does not prove the proposed daemon exists.

Priorities for the implementation, justified by that comparison:

- Keep the store and active model/index resources warm in the daemon; reuse
  connections and avoid per-request model loading or database creation.
- Return bounded, policy-approved context rather than shipping the whole brain
  to a harness. Resolve file-backed payloads lazily and avoid unnecessary copies.
- Keep slow extraction, embedding, snapshots, and imports from blocking health
  checks or unrelated bounded reads; use bounded work/concurrency and explicit
  cancellation. Do not acknowledge a durable write before it is committed.
- Add batching only if observed request chatter warrants it. Any result cache
  must be scoped to identity/policy/store revision and invalidated on changes;
  it must never serve data after a permission is revoked.
- If measured loopback overhead matters, compare a Unix-domain socket or Windows
  named pipe against the same operation contract. Do not introduce a new wire
  protocol, native rewrite, or mandatory broker based on assumed speed.
- Remote access should send the approved result over a reused secure connection,
  with bounded timeouts and visible offline state. Avoid per-call whole-store
  transfer; remote replicas need the existing identity/replay contracts first.

Resilience requires explicit start/status/stop/reconnect behaviour, a stable
endpoint, a store lock/ownership rule, bounded contention, graceful shutdown,
and recovery after a process failure. Service startup is an explicit owner/
service-manager operation, not a process spawned by a memory tool call. A lost
response makes a write's outcome
ambiguous: do not blindly retry a mutation without identity/deduplication
semantics. A disconnected agent or closed UI must not stop the service.
Resource and latency budgets must be recorded from representative runs before
claiming performance acceptance. No measurements have been collected for this
proposal. Do not add broad CI, telemetry services, or a benchmark programme to
block the first two-client proof.

## Security and the detachable LSD surface

The daemon must identify clients and enforce their permitted brain, namespace,
operations, and disclosure scope. Privileged management requires distinct
authority from an agent's memory access. Loopback, a private overlay, a browser
origin check, and a client-supplied actor name are not authentication. Scope
must apply to search, direct IDs, metadata, file hydration, export, and activity
streams as well as ordinary reads/writes.

Keep payloads and sensitive identifiers out of routine logs and streams. Shared
caches, errors, counts, and export diagnostics must respect the same scope.
Credential storage, permissions, and retention are part of the first supported
deployment's boundary; checksum integrity does not establish encryption or
signing. Browser management must address authentication and cross-origin
requests before privileged actions become available.

The default management UI should show daemon health, the selected cassette,
connected clients, permissions, pending privileged actions, and recent scoped
activity. The detachable LSD view uses that same backend and can later expose:

- individual agent streams and contextual chat with the permitted stream;
- combined discovery/insights over explicitly authorised streams;
- actionable alerts for denied access, failures, or suspicious activity;
- memory use and any explained effect on relevance scoring.

Report only activity observed at HotMem. A memory connection does not reveal
every action inside a harness. Shared views and contextual chat must not merge
private content into another user's context. Discovery and inferred insights
must retain sources and policy. Access frequency is evidence of use, not truth;
the scoring rule and distinction between one retry and a new use remain open.
That scoring work is not required for the first relay test.

## One enterprise workflow, then stop and assess

Use a synthetic operations incident. Client A can access a private incident
note containing a canary value; client B can access only a shared, approved
runbook/decision. Both connect to the same daemon, with a simple relay view.

| Small test | Expected result / stop condition |
| --- | --- |
| A writes an approved shared decision; B retrieves it. | One service/store, visible provenance, no manual copying or harness-owned shadow store. |
| B tries the private note through search, direct lookup, export, and a forbidden mutation. | The service denies access; protected content is absent from responses and B's logs/UI stream. Check every exposed route in this deployment. |
| Disconnect/reconnect one client and close/reopen the detachable view. | The other client and daemon continue; the view resumes permitted activity and shows real disconnection state. |
| Stop/restart the daemon and inspect an acknowledged write. | The write survives; clients reconnect explicitly; safe retry does not create a duplicate. Include one bounded abrupt-stop recovery check. |
| Export JSONL/verified package and restore into a separate clean store. | The permitted decision and provenance survive; omissions/dependencies are visible and repeat hydration is safe under the supported contract. |
| Repeat a small bounded workload with both clients. | Record latency/resource observations; distinguish transport from operation costs and report remaining uncertainty. |

This is the next experiment, not proof of universal enterprise hardening. A
port/storage-only test does not pass authorisation acceptance, and an HTTP
script does not prove two real harnesses work. Start with two test clients to
isolate the backend; then use two actual configured harnesses for the claimed
showcase. If policy, lifecycle, or the relay is absent, report that failure/gap
and implement the smallest missing slice before claiming success.

Once this slice passes, assess the next concrete uncertainty. Do not wait for
every platform, remote provider, insight feature, or full UI before delivering
the useful local result.

## Making each brain visible

| Aspect | Concrete collaborative behaviour | Status in this discussion |
| --- | --- | --- |
| Digital brain | An owner changes harnesses while keeping the JSONL cassette and approved working context. | Existing portability foundation; daemon/disclosure proof pending. |
| Company/ops brain | Different agents collaborate on shared decisions while private material stays scoped. | First two-client experiment above. |
| Remote brain | An authorised remote device/team member reaches a selected brain or deliberate replica. | Follow-on after local identity/policy proof; sync is not assumed. |
| Overmind | An authorised aggregate view across selected brains and agent streams, with source-linked insights. | Product direction; no automatic global privileges or graph database. |

Cloning, sharing, and remote synchronization are different operations. A
selective share should not grant an entire cassette; a clone is an explicit
copy with independent consequences. Revocation and ownership must be visible.

## Flats study: relevant patterns, bounded adoption

The supplied LinkedIn post points to
[`gosuda/flats`](https://github.com/gosuda/flats). Review date: 2026-10-21;
source revision:
[`d35a4f49bde41cf701623f91afdd6c8a86b1cfcb`](https://github.com/gosuda/flats/tree/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb).
This was a documentation/source review, not an installed or tested deployment.

Its [README](https://github.com/gosuda/flats/blob/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb/README.md)
describes a resident local host with CLI/API/MCP/console access. Approval applies
to particular lifecycle transitions; not every live collaborative edit waits
for publication. Its console trust model explicitly distinguishes CSRF checks
from authentication.

The [agent guide](https://github.com/gosuda/flats/blob/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb/docs/agents.md)
describes a stdio relay to the selected host and read-only compatibility/setup
discovery. The [networking guide](https://github.com/gosuda/flats/blob/d35a4f49bde41cf701623f91afdd6c8a86b1cfcb/docs/networking.md)
confirms per-flat NetBird peers, private access policies, and separately enabled
public exposure. Network reachability and publication are separate controls.

Our interpretation for HotMem:

- A stable owner-run service, thin clients, and detachable management fit the
  daemon/relay direction without requiring a Go rewrite or single-binary pledge.
- Approval should bind to the exact selected memory share/export and audience.
  Routine permitted memory use should not require clicking through every call.
- A future private overlay could connect approved team devices to a selected
  brain. It complements application authorisation; it cannot replace it.
- A shared incident report or decision brief can be a useful artifact view over
  authorised memories. HotMem should not become a website/artifact hosting
  platform to demonstrate that workflow.

No Flats/NetBird dependency, installation, public tunnel, or private-network
deployment is required by this note or the first local test.

## Issue ownership and remaining choices

[#112](https://github.com/KnowGuard-AI/HotMem/issues/112) holds today's additional
daemon/relay/showcase discussion. Link implementation evidence back there and
keep this doc authoritative for these decisions. Existing
[#109](https://github.com/KnowGuard-AI/HotMem/issues/109) (host setup),
[#110](https://github.com/KnowGuard-AI/HotMem/issues/110) (export privacy), and
[#111](https://github.com/KnowGuard-AI/HotMem/issues/111) (session context)
retain their acceptance criteria. Portable-state work remains in
[#94](https://github.com/KnowGuard-AI/HotMem/issues/94),
[#95](https://github.com/KnowGuard-AI/HotMem/issues/95),
[#96](https://github.com/KnowGuard-AI/HotMem/issues/96),
[#97](https://github.com/KnowGuard-AI/HotMem/issues/97),
[#98](https://github.com/KnowGuard-AI/HotMem/issues/98),
[#99](https://github.com/KnowGuard-AI/HotMem/issues/99), and
[#100](https://github.com/KnowGuard-AI/HotMem/issues/100).
This discussion does not mark any implementation work complete.

The owner wants v0.2.6 aligned on the secure daemon and a minimal management/
detachable-relay experience. This extends #112's earlier host-setup scope;
the first local slice is the implementation focus. The broader features remain
recorded product direction rather than automatic release blockers. The
[Hermes showcase #70](https://github.com/KnowGuard-AI/HotMem/issues/70),
[OpenClaw boundary #71](https://github.com/KnowGuard-AI/HotMem/issues/71) and
[showcase #72](https://github.com/KnowGuard-AI/HotMem/issues/72),
[walk #105](https://github.com/KnowGuard-AI/HotMem/issues/105),
[deep map #106](https://github.com/KnowGuard-AI/HotMem/issues/106), and
[platform roadmap #113 with children #114–#117](https://github.com/KnowGuard-AI/HotMem/issues/113)
remain linked follow-ons rather than prerequisites for that experiment.

Still to settle through implementation/evidence: the initial client credential
and scope model; the first supported service-manager/platform combination;
measured latency/resource budgets; the smallest detachable UI packaging; and
the next remote collaboration boundary. Preserve these uncertainties rather
than silently substituting authentication, UI, or performance claims.
