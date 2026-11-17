# Session Handoff Showcase — Codex → HotMem → Claude (#101)

A clean-room, end-to-end walkthrough: a Codex session export becomes a
verified `hotmem-handoff-v1` package, hydrates a fresh HotMem target, and
the resume brief plus durable memories are retrieved through the normal
search path — the exact flow a Claude Desktop session uses.

Clean-room rules (acceptance criterion 1): **no cloud account, no network
access, no provider secret, and no manual database editing.** Everything
below runs locally against the bundled fixture.

## Layout

- `codex_export/` — the `codex-export-v1` source fixture (a documented
  local export format; see `tests/fixtures/codex_export/README.md`).
- `run_showcase.sh` — the scripted walkthrough (prepare → verify →
  inspect → hydrate → search, plus the archive variant).
- `expected_output.md` — what a healthy run prints, with volatile values
  elided.
- `claude_desktop_config.json` — the Claude Desktop MCP wiring for the
  target session (reuses `examples/mcp_claude_desktop`).
- `run_claude_continuation.sh`, `check_claude_continuation.py` — the
  optional scripted Claude Code continuation and its transcript check.

## Run it

```bash
./run_showcase.sh          # creates a throwaway DB + package under /tmp
```

The script exits non-zero the moment any step fails; every step is a
plain `hotmem handoff …` command you can also run by hand.

## The five steps

1. **prepare** — `hotmem handoff prepare --source codex_export
   --out "$WORK/pkg" --mode resume --consent "…showcase demo consent…"`
   Consent is required; nothing is captured implicitly. The fixture
   contains a pasted secret and a hidden prompt: the first is redacted
   deny-by-default (`[REDACTED:api_key]`), the second is never read.
2. **verify** — fail-closed integrity (checksums, confinement, counts,
   content-derived package_id, secret gate).
3. **inspect** — the coverage report: 7 omissions, 1 redaction, all
   linked source ids — the honesty layer.
4. **hydrate** — atomic, idempotent restore into a fresh SQLite target;
   repeat runs are no-ops reporting `already_applied`.
5. **search** — `hotmem search --db "$WORK/target.sqlite" --query
   "resume brief handoff showcase"` retrieves the brief and the durable
   memories through the normal search path, with source ids visible.

## Claude Desktop as the target

Register the hydrated database with the MCP server (see
`claude_desktop_config.json`), then ask Claude:

> "Search HotMem for the handoff resume brief and continue the work."

Claude retrieves `handoff/hotmem-showcase-planning/resume-brief` through
`search_memories` — no handoff-specific retrieval tools needed.

## Scripted Claude Code continuation

To check everything up to the model call, without invoking Claude or using
any quota:

```bash
./run_claude_continuation.sh --local-only
```

This prepares, verifies, and hydrates the local package, writes the MCP config
Claude would receive, then starts that exact MCP server over stdio and calls
`search_memories` to confirm the resume brief comes back. It proves the local
half only; it says so and does not claim a continuation.

To run the same fixture through an actual Claude continuation:

```bash
./run_claude_continuation.sh
```

This repeats the local steps, then starts one non-persistent Claude Code CLI
session against the hydrated database. The HotMem server exposes its full tool
list; the session is restricted to `search_memories` by the CLI allow-list
(no built-in tools, anything else denied without prompting). Claude must
recover the resume brief, quote its next action, cite the source entry, and
identify a durable memory.

The run is recorded as `claude-continuation.stream.jsonl` (the CLI's
`stream-json` transcript) under the printed work directory, and
`check_claude_continuation.py` passes only if that transcript contains:

- a recorded `mcp__hotmem__search_memories` tool call whose tool result
  carries `handoff/hotmem-showcase-planning/resume-brief`, and no call to any
  other tool — tool use is read from the transcript events, not from the
  model's description of what it did;
- a successful CLI result (errors such as an exhausted budget are reported
  with their subtype);
- a final JSON answer whose next action, source id, and memory identifier all
  exist in the prepared package.

The fixture is a **historical capture** of a 2026-09-16 planning session. Its
next action ("Wire the hotmem handoff CLI group…") has long since shipped; the
demo shows that Claude recovers the captured state faithfully with provenance,
not that the action is still open. The prompt tells Claude the same.

Prerequisites are `uv`, Claude Code CLI, and the project's `mcp` extra. The
script uses the Claude CLI's configured authentication and default model. For
API-key billing, its default call cap is `$0.25`; set
`CLAUDE_MAX_BUDGET_USD` to change that cap. This is a model call and may use
account quota. The local package is prepared before Claude is invoked, and the
Claude session receives only the hydrated brief and memories through HotMem.
This demonstrates a portable handoff and continuation, not native transcript
migration.

## What this proves

| Acceptance criterion | Where |
|---|---|
| Clean-room demo, no cloud/secret/manual edits | whole script |
| Kind variety (turns, decision, commitment, unresolved, action, tools, file, memories) | `codex_export/session.jsonl` |
| Explicit selection + consent | step 1 (`--consent`, `--session`) |
| Ordering, stable ids, provenance, omissions | inspect report |
| Deny-by-default redaction, value never stored | the redacted line + coverage |
| Versioned self-describing manifest | `manifest.json` |
| Verify-before-hydrate, fail-closed | step 2 |
| Idempotent hydration; unrelated state preserved | step 4 (repeat) |
| Modes differ; no tool replay | archive variant; `executable: false` |
| Support matrix + limitations | `docs/handoff.md` §4 |
