# hotmem-autogen

AutoGen memory adapter for the [HotMem](https://github.com/KnowGuard-AI/HotMem) memory sidecar.

## Install

```sh
pip install hotmem-autogen
# with AutoGen itself:
pip install "hotmem-autogen[autogen]"
```

## Quickstart

```sh
hotmem serve
```

```python
from hotmem_autogen import HotMemMemoryPlugin

memory = HotMemMemoryPlugin(identifier="my-agent", top_k=5)
memory.save("Server staging runs on port 2222", importance=0.8)

# Inject recall into agent context
context = memory.add_context("staging server port")
# "Relevant memories:\n- Server staging runs on port 2222"
```

## API

| Method | Description |
| --- | --- |
| `add_context(query)` / `update_context(query)` | Return recalled memories as a context string |
| `save(content, identifier, importance, metadata)` | Persist a fact |
| `search(query, top_k)` | Hybrid-ranked search |

## License

MIT
