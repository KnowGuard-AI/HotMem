"""HotMem client - Python SDK for the memory sidecar.

Purpose:
    Provide a simple, typed client for the HotMem HTTP API.
    Designed for direct use in agent applications and SPA backends.

Interface:
    HotMemClient(base_url)
        .add(identifier, fact, ...) -> dict
        .search(query, top_k, max_chars?) -> list[MessageObject]
        .health() -> dict
        .hydrate(file?) -> dict
        .snapshot(file?) -> dict
    AsyncHotMemClient(base_url)
        Async equivalent of HotMemClient.

Deps: httpx
Extension: add retry logic or connection pooling here.
"""

from __future__ import annotations

from typing import Any

import httpx


class HotMemClient:
    """Synchronous client for the HotMem sidecar API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8711") -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def health(self) -> dict[str, Any]:
        """Check server health."""
        resp = self._client.get("/v1/health")
        resp.raise_for_status()
        return resp.json()

    def add(
        self,
        identifier: str,
        fact: str,
        *,
        source: str = "",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Add a fact to memory."""
        payload = {
            "identifier": identifier,
            "fact": fact,
            "source": source,
            "importance": importance,
            "metadata": metadata or {},
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        resp = self._client.post("/v1/add", json=payload)
        resp.raise_for_status()
        return resp.json()

    def search(
        self,
        query: str,
        top_k: int = 5,
        max_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search memories and return LLM-ready message objects."""
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if max_chars is not None:
            payload["max_chars"] = max_chars
        resp = self._client.post("/v1/search", json=payload)
        resp.raise_for_status()
        return resp.json()["memories"]

    def hydrate(self, file: str | None = None) -> dict[str, Any]:
        """Trigger swap file hydration."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        resp = self._client.post("/v1/hydrate", json=payload)
        resp.raise_for_status()
        return resp.json()

    def snapshot(self, file: str | None = None) -> dict[str, Any]:
        """Trigger database snapshot to swap file."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        resp = self._client.post("/v1/snapshot", json=payload)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> HotMemClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncHotMemClient:
    """Asynchronous client for the HotMem sidecar API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8711") -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def health(self) -> dict[str, Any]:
        """Check server health."""
        resp = await self._client.get("/v1/health")
        resp.raise_for_status()
        return resp.json()

    async def add(
        self,
        identifier: str,
        fact: str,
        *,
        source: str = "",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Add a fact to memory."""
        payload = {
            "identifier": identifier,
            "fact": fact,
            "source": source,
            "importance": importance,
            "metadata": metadata or {},
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        resp = await self._client.post("/v1/add", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        query: str,
        top_k: int = 5,
        max_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search memories and return LLM-ready message objects."""
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if max_chars is not None:
            payload["max_chars"] = max_chars
        resp = await self._client.post("/v1/search", json=payload)
        resp.raise_for_status()
        return resp.json()["memories"]

    async def hydrate(self, file: str | None = None) -> dict[str, Any]:
        """Trigger swap file hydration."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        resp = await self._client.post("/v1/hydrate", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def snapshot(self, file: str | None = None) -> dict[str, Any]:
        """Trigger database snapshot to swap file."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        resp = await self._client.post("/v1/snapshot", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHotMemClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
