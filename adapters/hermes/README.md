# hotmem-hermes

[Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider plugin for the [HotMem](https://github.com/KnowGuard-AI/HotMem) local-first memory sidecar.

Makes HotMem a first-class `memory.provider: hotmem` backend — Hermes calls into HotMem at every lifecycle point automatically (prefetch before each turn, sync after each turn, mirror built-in memory writes, pre-compress extraction, session-end snapshot).

## Install

```sh
pip install hotmem-hermes
```

## Quickstart

Start the HotMem sidecar and configure Hermes to use it:

```sh
hotmem serve  # start the sidecar on http://127.0.0.1:8711

hermes memory setup        # select "hotmem"
# or manually:
hermes config set memory.provider hotmem
```

HotMem is local-first — no API key required. Configure the sidecar URL in `$HERMES_HOME/hotmem.json` or via the `HOTMEM_URL` env var.

## How it works

HotMem implements the Hermes [Memory Provider Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin) interface:

| Hermes hook | HotMem action |
| --- | --- |
| `prefetch(query)` | Vector + FTS5 recall injected before each LLM turn |
| `sync_turn(user, assistant)` | Persist turns asynchronously (non-blocking) |
| `on_memory_write(action, target, content)` | Mirror `MEMORY.md`/`USER.md` writes to HotMem (biased importance: user 0.9, memory 0.8) |
| `on_pre_compress(messages)` | Extract durable facts before context compression |
| `on_session_end(messages)` | Flush + snapshot to a profile-scoped swap file |
| `hotmem_search` / `hotmem_store` tools | In-process tool routing to the sidecar |

The built-in `MEMORY.md`/`USER.md` continues to work alongside HotMem — the provider is additive.

## Bundled skill

`hotmem-memory` (agentskills.io standard) teaches the agent when to persist learnings proactively. Install from the repo:

```sh
hermes skills install <this-repo>/adapters/hermes/skill/hotmem-memory
```

## CLI

```sh
hermes hotmem status   # sidecar health + memory count
hermes hotmem config   # show active config
```

## License

MIT
