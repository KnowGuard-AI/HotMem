# hotmem-langchain

LangChain adapter for the [HotMem](https://github.com/KnowGuard-AI/HotMem) memory sidecar.

Provides `HotMemChatMessageHistory` and `HotMemRetriever` backed by HotMem's hybrid vector + FTS5 search.

## Install

```sh
pip install hotmem-langchain
```

## Quickstart

```sh
hotmem serve  # start the sidecar on http://127.0.0.1:8711
```

```python
from hotmem_langchain import HotMemChatMessageHistory, HotMemRetriever

# Chat message history
history = HotMemChatMessageHistory("session-1")
history.add_user_message("User prefers dark mode")
history.add_ai_message("Got it, dark mode enabled.")

# Retriever
retriever = HotMemRetriever(top_k=5)
docs = retriever.invoke("user preferences")
# [Document(page_content="User prefers dark mode", metadata={"score": 0.92, ...})]
```

## Classes

| Class | LangChain base | Purpose |
| --- | --- | --- |
| `HotMemChatMessageHistory` | `BaseChatMessageHistory` | Persist conversation turns |
| `HotMemRetriever` | `BaseRetriever` | Retrieve `Document` objects from search |

## License

MIT
