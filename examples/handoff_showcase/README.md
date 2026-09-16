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