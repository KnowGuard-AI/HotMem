# langchain_agent

A 3-turn LangChain agent that retrieves from HotMem before each response and
stores new facts afterwards.

## Setup

See [../README.md](../README.md#prerequisites) for the common HotMem install +
`hotmem serve` steps. Then install this example's framework deps:

```sh
pip install -e adapters/langchain
pip install langchain langchain-openai
export OPENAI_API_KEY=sk-...                     # or use the stub LLM (no key needed)
```

## Run

```sh
python agent.py
```
