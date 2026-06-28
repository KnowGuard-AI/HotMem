# hotmem-crewai

CrewAI memory adapter for the [HotMem](https://github.com/KnowGuard-AI/HotMem) memory sidecar.

## Install

```sh
pip install hotmem-crewai
```

## Quickstart

```sh
hotmem serve
```

```python
from hotmem_crewai import HotMemMemory

memory = HotMemMemory()
memory.save("Vendor Acme has NET-30 terms", identifier="vendor:acme", importance=0.8)

results = memory.search("acme payment terms")
# [{ "content": "Vendor Acme has NET-30 terms", "score": 0.91, ... }]
```

## API

| Method | Description |
| --- | --- |
| `save(content, identifier, importance, metadata)` | Store a memory |
| `search(query, top_k)` | Hybrid-ranked search |
| `load(query, top_k)` | Alias for `search()` |

## License

MIT
