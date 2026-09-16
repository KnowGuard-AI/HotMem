# Session Handoff (Codex → HotMem → Claude)

Verified continuity with preserved context: move the useful working state
of a source agent session (Codex) through HotMem into a target session
(Claude) — without native transcript cloning, without cloud transport, and
with every unsupported, redacted, or omitted item visible to the operator.

## 1. How it works

1. **Prepare** — `hotmem handoff prepare` reads a documented source export
   (`codex-export-v1`: an `export.json` envelope plus an ordered
   `session.jsonl` log produced by an operator or adapter script), and
   writes one `hotmem-handoff-v1` package atomically:

   ```
   <package>/
     manifest.json        # identity, mode, counts, SHA-256 checksums, coverage
     session.jsonl        # ordered normalized entries
     memories.jsonl       # selected durable memories (hotmem-interchange-v1)
     resume-brief.json     # bounded, linked, target-friendly brief
   ```

2. **Verify** — fail-closed integrity before any target write: manifest
   schema ceiling, path confinement (no traversal, no symlink escapes),
   per-file checksums, record counts, entry ordering and duplicate ids,
   interchange validity of memories, coverage arithmetic, content-derived
   `package_id` recomputation, and the deny-by-default secret gate.

3. **Inspect** — read-only report with handoff/package ids, mode, counts,
   coverage, omissions, and redactions — useful on valid and invalid
   packages alike (`inspect --json` exits 0 with `valid: false` and the
   failure reason; `verify` is the gate).

4. **Hydrate** — atomic, idempotent restore into a HotMem database: the
   selected durable memories plus exactly one resume-brief record. The
   brief is a normal memory record (`handoff/<label>/resume-brief`), so
   Claude retrieves it through the **unmodified** search path.

## 2. Modes

| Mode | Package contents | Use |
|---|---|---|
| `resume` | Bounded turn stream (head/tail windows, omission records for the middle) + full brief + memories | A clean target session continues the work quickly |
| `archive` | Full ordered session stream within global caps + brief + memories | Audit and later inspection |

Modes differ in content and coverage — never inferred at the target.

## 3. Consent and redaction

- Consent is an **explicit, non-empty statement** required at every surface
  (CLI `--consent`, MCP `consent`, HTTP `consent`) and checked *before any
  session content is read*. Nothing is captured implicitly.
- Redaction is deny-by-default and layered: hidden prompts and
  credential-bearing source fields are never read; secret patterns (api
  keys, tokens, bearer headers, PEM blocks, vendor-prefixed credentials)
  are replaced with `[REDACTED:<kind>]`; and an output gate re-scans
  rendered briefs and error strings. Redaction records carry the secret
  *kind* and location — never the value.
- Every unsupported field, dropped item, or bound exceedance becomes a
  machine-readable omission record with a reason and a recoverability
  flag. The manifest's `coverage` block is the honesty layer.

## 4. Support matrix

| Dimension | Supported and tested |
|---|---|
| Source capture path | `codex-export-v1` documented local export format (fixture adapter). **Not** a claim of a native Codex export API |
| Target | HotMem SQLite ≥ 0.2.4 on Python 3.11–3.14; Claude Desktop via the existing MCP example |
| Transport | Local files only |
| Surfaces | CLI (`hotmem handoff …`), MCP (`handoff_*` tools), HTTP (`/v1/handoff/*`) — same core functions |

### Limitations (explicit, never silent)

- Native Codex/Claude transcript restoration is **not** implied; HotMem
  owns the handoff representation, integrity, hydration, and adapter seams.
- No cloud transport, no encryption/authentication, no multi-writer sync.
- Historical tools are inert data — nothing is ever re-executed.
- Until tombstone semantics land (#98), a package record deleted at a
  used target is re-inserted on repeat hydration; the showcase targets a
  clean database.

## 5. CLI

```
hotmem handoff prepare  --source ./codex_export --out ./handoff \
                        --mode resume --consent "…"
hotmem handoff verify   ./handoff
hotmem handoff inspect  ./handoff --json
hotmem handoff hydrate  ./handoff --db ./claude.sqlite
```

Repeat hydration of the same package is a true no-op: zero writes, zero
new events, `already_applied: true`.

## 6. Agent surfaces

- **MCP** — `handoff_prepare`, `handoff_inspect`, `handoff_verify`,
  `handoff_hydrate`. `handoff_prepare` requires the `consent` argument, so
  no host can implicitly capture a session; `handoff_hydrate` targets the
  server's configured database and embedder.
- **HTTP** — `POST /v1/handoff/prepare`, `POST /v1/handoff/verify`,
  `GET /v1/handoff/inspect?package=…`, `POST /v1/handoff/hydrate`.
  Verification failures return 409 with a structured body
  (`error`/`reason`/`file`/`expected`/`actual`); prepare returns 400 when
  consent is absent — before anything is written.

## 7. Showcase walkthrough

See [`examples/handoff_showcase/`](https://github.com/KnowGuard-AI/HotMem/tree/main/examples/handoff_showcase)
for the clean-room runbook and script: no cloud account, no network, no
provider secret, and no manual database editing — prepare, verify,
inspect, hydrate, then retrieve the brief through the normal search path.

The normative contract is
[`docs/okf/handoff-v1.md`](https://github.com/KnowGuard-AI/HotMem/blob/main/docs/okf/handoff-v1.md).