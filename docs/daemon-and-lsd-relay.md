# HotMem: carry the brain, change the player

An incident is contained. The next shift is coming online. One agent has the
investigation, another has the recovery runbook, and the person taking over
needs to know why the team chose to roll back. They should be able to continue
from that decision, with its evidence and permissions intact.

This is the experience HotMem is being built to deliver: a working brain that
belongs to its owner and can travel between compatible tools. **JSONL is the
memory cassette. HotMem is the recorder and player. LSD is the detachable
interface that keeps the recorder in view.**

For a developer, the job is concrete. Keep memory durable. Give each client the
right view. Make the service survive a disconnected agent, a closed window, and
a restart. Then show two people and their agents continuing useful work.

!!! info "Implementation status — 2026-10-21"

    This guide defines the shared daemon and LSD relay direction for v0.2.7,
    tracked by [#112](https://github.com/KnowGuard-AI/HotMem/issues/112).
    Local storage, retrieval, snapshots, and verified handoff are existing
    foundations. The shared daemon, runtime authorisation, detachable UI, and
    remote collaboration below still need implementation and runtime evidence.
    Examples marked **current runtime** use existing interfaces.

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

LSD carries that view into a detachable player. Keep it beside the incident
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

HotMem is a formal KnowGuard product; KnowB is a shadow brand and no required
runtime service. The [Vision and Canon](vision-and-canon.md) defines the enduring
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
