"""CrewAI memory backend backed by HotMem."""

from __future__ import annotations

from typing import Any

from hotmem.client import HotMemClient


class HotMemMemory:
    """Memory store compatible with CrewAI's memory interface.

    CrewAI memories expose save() and load()/search(). This adapter maps
    them to HotMem add() and search().
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8711",
        client: HotMemClient | None = None,
    ) -> None:
        self._client = client or HotMemClient(base_url)

    @property
    def client(self) -> HotMemClient:
        return self._client

    def save(
        self,
        content: str,
        *,
        identifier: str = "crewai",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save a memory entry to HotMem."""
        return self._client.add(
            identifier=identifier,
            fact=content,
            source="crewai",
            importance=importance,
            metadata=metadata or {},
        )

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Search memories and return ranked results."""
        return self._client.search(query, top_k=top_k)

    def load(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Alias for search(), matching CrewAI's load convention."""
        return self.search(query, top_k=top_k)

    def clear(self) -> None:
        """Clear memories. HotMem v0.1 has no delete; this is a no-op."""
        pass
