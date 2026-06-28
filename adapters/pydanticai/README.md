# hotmem-pydanticai

Pydantic AI dependency and tool provider for the [HotMem](https://github.com/KnowGuard-AI/HotMem) memory sidecar.

## Install

```sh
pip install hotmem-pydanticai
```

## Quickstart

```sh
hotmem serve
```

```python
from pydantic_ai import Agent
from hotmem_pydanticai import HotMemDeps, recall_system_prompt

agent = Agent("openai:gpt-4o", deps_type=HotMemDeps)
agent.system_prompt(recall_system_prompt)

# optional: a tool to store facts
@agent.tool
async def remember(ctx, fact: str) -> str:
    ctx.deps.client.add(ctx.deps.identifier, fact)
    return "remembered"

deps = HotMemDeps.from_url("http://127.0.0.1:8711")
result = await agent.run("What do you know about my preferences?", deps=deps)
print(result.output)
```

## API

| Object | Description |
| --- | --- |
| `HotMemDeps` | Dependency dataclass — holds a `HotMemClient`, identifier, top_k |
| `HotMemDeps.recall(query)` | Return ranked memories as a context string |
| `HotMemDeps.from_url(base_url)` | Convenience constructor |
| `recall_system_prompt(ctx)` | System prompt function injecting recall |

## License

MIT
