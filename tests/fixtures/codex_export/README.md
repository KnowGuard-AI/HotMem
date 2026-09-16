# Codex export fixture (`codex-export-v1`)

This fixture is the showcase's representative source: a **documented local
export format**, not a claim that Codex exposes a native session-export API.
An operator (or a small adapter script) produces an export like this from a
Codex session log; HotMem consumes it through `hotmem.handoff.codex_source`.
See `docs/okf/handoff-v1.md` §12 for the adapter boundary and support matrix.

## Layout

- `export.json` — the envelope: `format`, `exported_at`, and `session`
  metadata (`id`, `label`, time range, adapter, `codex_version`).
- `session.jsonl` — the ordered source log: one JSON object per line with a
  strictly increasing `seq`.

## Source line types

| Type | Fields | Handoff mapping |
| --- | --- | --- |
| `turn` | `role` (user/assistant), `text`, `timestamp`; `model`, `reasoning_tokens` are unsupported extras | `kind: turn` (extras listed as omissions) |
| `decision` | `text`, `timestamp` | `kind: decision` |
| `commitment` | `text`, `owner`, `timestamp` | `kind: commitment` |
| `unresolved_question` | `text`, `asked_by`, `timestamp` | `kind: unresolved_question` |
| `next_action` | `text`, `priority`, `timestamp` | `kind: next_action` |
| `tool_call` | `tool`, `args_summary`, `timestamp` | `kind: tool_call`, `executable: false` |
| `tool_result` | `tool`, `status`, `result_summary`, `timestamp` | `kind: tool_result`, `executable: false` |
| `file_reference` | `path`, `note`, `timestamp` | `kind: file_reference` |
| `memory_item` | `identifier`, `fact`, `importance`, `tags`, `timestamp` | durable memory (interchange record) |
| `hidden_prompt` | (content never read) | policy omission, `recoverable: false` |

## Fixture inventory (15 lines)

8 turns (4 user / 3 assistant / 1 secret-bearing), 1 decision, 1
commitment, 1 unresolved question, 1 next action, 1 tool call + 1 tool
result, 1 file reference, 2 memory items, 1 hidden prompt. The
secret-bearing line (`HOTMEM_API_KEY=hotmem_sk_…`) exercises deny-by-default
redaction; the `hidden_prompt` line exercises the never-read policy; the
`model`/`reasoning_tokens` extras exercise unsupported-field omission.