# HotMem Examples

Copy-paste-runnable examples showing HotMem with each framework and runtime.

## Index

| Example | What it shows | Language | Run |
| --- | --- | --- | --- |
| [langchain_agent](langchain_agent) | HotMem retriever + LLM agent loop | Python | `python agent.py` |
| [crewai_crew](crewai_crew) | Shared HotMem memory across a CrewAI crew | Python | `python crew.py` |
| [autogen_group_chat](autogen_group_chat) | Group chat with HotMem memory plugin | Python | `python chat.py` |
| [pydanticai_agent](pydanticai_agent) | Typed Pydantic AI agent with HotMem deps | Python | `python agent.py` |
| [fastapi_backend](fastapi_backend) | Standalone FastAPI app using HotMem as a library | Python | `uvicorn app:app` |
| [mcp_claude_desktop](mcp_claude_desktop) | `hotmem mcp` wired into Claude Desktop | Config | paste config + restart |
| [typescript](typescript) | TS client add/search against `hotmem serve` | TypeScript | `npx tsx agent.ts` |

## Prerequisites

Most Python examples target a running sidecar:

```sh
pip install -e ".[dev,mcp]"          # HotMem itself
pip install -e adapters/<name>        # the adapter for the example
hotmem serve                          # http://127.0.0.1:8711
```

Framework-specific deps (e.g. `langchain`, `crewai`, `openai`) are listed in
each example's README — they are NOT bundled with HotMem.

## Notes

- Examples are illustrative and not exercised by the test suite (they pull
  heavy, optional framework deps and may need API keys). They are linted by
  `ruff check examples/` in CI to catch syntax/import-order errors.
- The [fastapi_backend](fastapi_backend) example uses HotMem **as a library**
  (in-process `MemoryDB`) — no sidecar required.
