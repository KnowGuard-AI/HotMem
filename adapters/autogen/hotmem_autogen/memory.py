"""AutoGen memory plugin backed by HotMem.

Compatible with AutoGen 0.4+ memory conventions: a memory store exposes
add_context()/update_context() to inject recalled context and save messages.
"""

from __future__ import annotations

from typing import Any

from hotmem.client import HotMemClient


class HotMemMemoryPlugin:
    """Memory plugin for AutoGen agents backed by HotMem.

    Usage: attach to a ConversableAgent and register its hooks so recall
    is injected before each turn and turns are persisted after.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8711",
        identifier: str = "autogen",
        top_k: int = 5,
        client: HotMemClient | None = None,
    ) -> None:
        self._client = client or HotMemClient(base_url)
        self.identifier = identifier
        self.top_k = top_k

    @property
    def client(self) -> HotMemClient:
        return self._client

    def add_context(self, query: str) -> str:
        """Return recalled memories as a context string for the agent."""
        memories = self._client.search(query, top_k=self.top_k)
        if not memories:
            return ""
        lines = [f"- {m['content']}" for m in memories]
        return "Relevant memories:\n" + "\n".join(lines)

    def update_context(self, query: str) -> str:
        """Alias for add_context."""
        return self.add_context(query)

    def save(
        self,
        content: str,
        *,
        identifier: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a fact to HotMem."""
        return self._client.add(
            identifier=identifier or self.identifier,
            fact=content,
            source="autogen",
            importance=importance,
            metadata=metadata or {},
        )

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Search memories."""
        return self._client.search(query, top_k=top_k)
