# pydanticai_agent

A typed Pydantic AI agent that injects HotMem recall into its system prompt
and exposes a `remember` tool.

## Setup

See [../README.md](../README.md#prerequisites) for the common HotMem install +
`hotmem serve` steps. Then install this example's framework deps:

```sh
pip install -e adapters/pydanticai
pip install pydantic-ai
```

## Run

```sh
python agent.py
```
