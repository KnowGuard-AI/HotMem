# langchain_agent

A 3-turn LangChain agent that retrieves from HotMem before each response and
stores new facts afterwards.

## Setup

```sh
pip install -e ".[dev,mcp]"
pip install -e adapters/langchain
pip install langchain langchain-openai          # framework deps

hotmem serve
export OPENAI_API_KEY=sk-...                     # or use a stub LLM
```

## Run

```sh
python agent.py
```
