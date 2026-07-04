# pydanticai_agent

A typed Pydantic AI agent that injects HotMem recall into its system prompt
and exposes a `remember` tool.

## Setup

```sh
pip install -e ".[dev,mcp]"
pip install -e adapters/pydanticai
pip install pydantic-ai                           # framework dep

hotmem serve
```

## Run

```sh
python agent.py
```
