"""HotMem client - Python SDK for the memory daemon/sidecar (#109, #110, #111).

Purpose:
    Provide a simple, typed client for the HotMem HTTP API with persistent
    connection pooling (`httpx.Client` / `httpx.AsyncClient`), credential-bound
    authentication, explicit store verification, session/source attribution,
    and idempotent write keys.
"""

from __future__ import annotations

from typing import Any

import httpx


def _build_context_headers(
    *,
    token: str | None = None,
    session_id: str | None = None,
    harness_id: str | None = None,
    source: str | None = None,
    namespace: str | None = None,
    expected_store_id: str | None = None,
    idempotency_key: str | None = None,
    envelope_version: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if expected_store_id:
        headers["X-HotMem-Store-Id"] = expected_store_id
    if session_id:
        headers["X-HotMem-Session-Id"] = session_id
    if harness_id:
        headers["X-HotMem-Harness-Id"] = harness_id
    if source:
        headers["X-HotMem-Source"] = source
    if namespace:
        headers["X-HotMem-Namespace"] = namespace
    if idempotency_key:
        headers["X-HotMem-Idempotency-Key"] = idempotency_key
    if envelope_version:
        headers["X-HotMem-Envelope-Version"] = envelope_version
    return headers


class HotMemClient:
    """Synchronous client for the HotMem daemon/sidecar API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8711",
        *,
        timeout: float = 30.0,
        token: str | None = None,
        session_id: str | None = None,
        harness_id: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        expected_store_id: str | None = None,
        envelope_version: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.harness_id = harness_id
        self.source = source
        self.namespace = namespace
        self.expected_store_id = expected_store_id
        self.envelope_version = envelope_version
        headers = _build_context_headers(
            token=token,
            session_id=session_id,
            harness_id=harness_id,
            source=source,
            namespace=namespace,
            expected_store_id=expected_store_id,
            envelope_version=envelope_version,
        )
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

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
        tags: list[str] | None = None,
        harness_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        envelope_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add an inline fact to memory."""
        payload: dict[str, Any] = {
            "identifier": identifier,
            "fact": fact,
            "source": source,
            "importance": importance,
            "metadata": metadata or {},
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        if tags is not None:
            payload["tags"] = tags
        extra_headers: dict[str, str] = {}
        if idempotency_key is not None:
            extra_headers["X-HotMem-Idempotency-Key"] = idempotency_key
        if harness_id is not None:
            extra_headers["X-HotMem-Harness-Id"] = harness_id
            payload["harness_id"] = harness_id
        if session_id is not None:
            extra_headers["X-HotMem-Session-Id"] = session_id
            payload["session_id"] = session_id
        if namespace is not None:
            extra_headers["X-HotMem-Namespace"] = namespace
        if envelope_version is not None:
            extra_headers["X-HotMem-Envelope-Version"] = envelope_version
            payload["envelope_version"] = envelope_version
        if source and getattr(self, "source", None) and source != getattr(self, "source", None):
            extra_headers["X-HotMem-Source"] = source
        if extra_headers:
            resp = self._client.post("/v1/add", json=payload, headers=extra_headers)
        else:
            resp = self._client.post("/v1/add", json=payload)
        resp.raise_for_status()
        return resp.json()

    def add_file_ref(
        self,
        identifier: str,
        file_uri: str,
        *,
        byte_offset: int = 0,
        byte_length: int = 0,
        source_format: str = "",
        source_checksum: str | None = None,
        summary: str | None = None,
        source: str = "",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        harness_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        envelope_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a file-backed memory by reference (zero bytes copied)."""
        payload: dict[str, Any] = {
            "identifier": identifier,
            "file_uri": file_uri,
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "source_format": source_format,
            "source": source,
            "importance": importance,
            "metadata": metadata or {},
        }
        if source_checksum is not None:
            payload["source_checksum"] = source_checksum
        if summary is not None:
            payload["summary"] = summary
        if tags is not None:
            payload["tags"] = tags
        extra_headers: dict[str, str] = {}
        if idempotency_key is not None:
            extra_headers["X-HotMem-Idempotency-Key"] = idempotency_key
        if harness_id is not None:
            extra_headers["X-HotMem-Harness-Id"] = harness_id
            payload["harness_id"] = harness_id
        if session_id is not None:
            extra_headers["X-HotMem-Session-Id"] = session_id
            payload["session_id"] = session_id
        if namespace is not None:
            extra_headers["X-HotMem-Namespace"] = namespace
        if envelope_version is not None:
            extra_headers["X-HotMem-Envelope-Version"] = envelope_version
            payload["envelope_version"] = envelope_version
        if source and getattr(self, "source", None) and source != getattr(self, "source", None):
            extra_headers["X-HotMem-Source"] = source
        if extra_headers:
            resp = self._client.post("/v1/add", json=payload, headers=extra_headers)
        else:
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

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        """Return memory metadata without touching the backing file."""
        resp = self._client.get(f"/v1/memory/{memory_id}")
        resp.raise_for_status()
        return resp.json()

    def hydrate_memory(self, memory_id: str) -> bytes:
        """Materialize a memory's payload on demand (lazy hydration)."""
        resp = self._client.post(f"/v1/memory/{memory_id}/hydrate")
        resp.raise_for_status()
        return resp.content

    def list(
        self,
        identifier: str,
        *,
        order: str = "asc",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return memories for an identifier in created_at order."""
        resp = self._client.get(
            "/v1/memories",
            params={"identifier": identifier, "order": order, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()["memories"]

    def hydrate(self, file: str | None = None) -> dict[str, Any]:
        """Trigger swap file or v2 directory hydration."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        resp = self._client.post("/v1/hydrate", json=payload)
        resp.raise_for_status()
        return resp.json()

    def snapshot(
        self,
        file: str | None = None,
        *,
        copy_attachments: bool | None = None,
        package: bool | None = None,
        gz: bool | None = None,
    ) -> dict[str, Any]:
        """Trigger database snapshot to swap file, v2 directory, or package."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        if copy_attachments is not None:
            payload["copy_attachments"] = copy_attachments
        if package is not None:
            payload["package"] = package
        if gz is not None:
            payload["gz"] = gz
        resp = self._client.post("/v1/snapshot", json=payload)
        resp.raise_for_status()
        return resp.json()

    def handoff_prepare(
        self,
        source: str,
        output: str,
        *,
        consent: str,
        mode: str = "resume",
        session: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": source,
            "output": output,
            "mode": mode,
            "consent": consent,
        }
        if session is not None:
            payload["session"] = session
        resp = self._client.post("/v1/handoff/prepare", json=payload)
        resp.raise_for_status()
        return resp.json()

    def handoff_verify(self, package: str) -> dict[str, Any]:
        resp = self._client.post("/v1/handoff/verify", json={"package": package})
        resp.raise_for_status()
        return resp.json()

    def handoff_inspect(self, package: str) -> dict[str, Any]:
        resp = self._client.get("/v1/handoff/inspect", params={"package": package})
        resp.raise_for_status()
        return resp.json()

    def handoff_hydrate(self, package: str) -> dict[str, Any]:
        resp = self._client.post("/v1/handoff/hydrate", json={"package": package})
        resp.raise_for_status()
        return resp.json()

    def promote(
        self,
        memory_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to_state": to_state}
        if reason is not None:
            payload["reason"] = reason
        if actor is not None:
            payload["actor"] = actor
        resp = self._client.post(f"/v1/memory/{memory_id}/promote", json=payload)
        resp.raise_for_status()
        return resp.json()

    def candidates(
        self, *, namespace: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if namespace is not None:
            params["namespace"] = namespace
        if state is not None:
            params["state"] = state
        resp = self._client.get("/v1/promotion/candidates", params=params)
        resp.raise_for_status()
        return resp.json()["candidates"]

    def events(
        self,
        *,
        memory_id: str | None = None,
        namespace: str | None = None,
        event_type: str | None = None,
        after_seq: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if memory_id is not None:
            params["memory_id"] = memory_id
        if namespace is not None:
            params["namespace"] = namespace
        if event_type is not None:
            params["event_type"] = event_type
        if after_seq is not None:
            params["after_seq"] = after_seq
        resp = self._client.get("/v1/events", params=params)
        resp.raise_for_status()
        return resp.json()

    def revoke_policy(
        self,
        *,
        caller_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Revoke a caller credential and/or share approval (management only)."""
        payload: dict[str, Any] = {}
        if caller_id is not None:
            payload["caller_id"] = caller_id
        if approval_id is not None:
            payload["approval_id"] = approval_id
        resp = self._client.post("/v1/policy/revoke", json=payload)
        resp.raise_for_status()
        return resp.json()

    def reload_policy(self) -> dict[str, Any]:
        """Reload the daemon policy file (management only)."""
        resp = self._client.post("/v1/policy/reload", json={})
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
    """Asynchronous client for the HotMem daemon/sidecar API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8711",
        *,
        timeout: float = 30.0,
        token: str | None = None,
        session_id: str | None = None,
        harness_id: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        expected_store_id: str | None = None,
        envelope_version: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.harness_id = harness_id
        self.source = source
        self.namespace = namespace
        self.expected_store_id = expected_store_id
        self.envelope_version = envelope_version
        headers = _build_context_headers(
            token=token,
            session_id=session_id,
            harness_id=harness_id,
            source=source,
            namespace=namespace,
            expected_store_id=expected_store_id,
            envelope_version=envelope_version,
        )
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)

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
        tags: list[str] | None = None,
        harness_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        envelope_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a fact to memory."""
        payload: dict[str, Any] = {
            "identifier": identifier,
            "fact": fact,
            "source": source,
            "importance": importance,
            "metadata": metadata or {},
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        if tags is not None:
            payload["tags"] = tags
        extra_headers: dict[str, str] = {}
        if idempotency_key is not None:
            extra_headers["X-HotMem-Idempotency-Key"] = idempotency_key
        if harness_id is not None:
            extra_headers["X-HotMem-Harness-Id"] = harness_id
            payload["harness_id"] = harness_id
        if session_id is not None:
            extra_headers["X-HotMem-Session-Id"] = session_id
            payload["session_id"] = session_id
        if namespace is not None:
            extra_headers["X-HotMem-Namespace"] = namespace
        if envelope_version is not None:
            extra_headers["X-HotMem-Envelope-Version"] = envelope_version
            payload["envelope_version"] = envelope_version
        if source and getattr(self, "source", None) and source != getattr(self, "source", None):
            extra_headers["X-HotMem-Source"] = source
        if extra_headers:
            resp = await self._client.post("/v1/add", json=payload, headers=extra_headers)
        else:
            resp = await self._client.post("/v1/add", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def add_file_ref(
        self,
        identifier: str,
        file_uri: str,
        *,
        byte_offset: int = 0,
        byte_length: int = 0,
        source_format: str = "",
        source_checksum: str | None = None,
        summary: str | None = None,
        source: str = "",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        harness_id: str | None = None,
        session_id: str | None = None,
        namespace: str | None = None,
        envelope_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a file-backed memory by reference (zero bytes copied)."""
        payload: dict[str, Any] = {
            "identifier": identifier,
            "file_uri": file_uri,
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "source_format": source_format,
            "source": source,
            "importance": importance,
            "metadata": metadata or {},
        }
        if source_checksum is not None:
            payload["source_checksum"] = source_checksum
        if summary is not None:
            payload["summary"] = summary
        if tags is not None:
            payload["tags"] = tags
        extra_headers: dict[str, str] = {}
        if idempotency_key is not None:
            extra_headers["X-HotMem-Idempotency-Key"] = idempotency_key
        if harness_id is not None:
            extra_headers["X-HotMem-Harness-Id"] = harness_id
            payload["harness_id"] = harness_id
        if session_id is not None:
            extra_headers["X-HotMem-Session-Id"] = session_id
            payload["session_id"] = session_id
        if namespace is not None:
            extra_headers["X-HotMem-Namespace"] = namespace
        if envelope_version is not None:
            extra_headers["X-HotMem-Envelope-Version"] = envelope_version
            payload["envelope_version"] = envelope_version
        if source and getattr(self, "source", None) and source != getattr(self, "source", None):
            extra_headers["X-HotMem-Source"] = source
        if extra_headers:
            resp = await self._client.post("/v1/add", json=payload, headers=extra_headers)
        else:
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

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        """Return memory metadata without touching the backing file."""
        resp = await self._client.get(f"/v1/memory/{memory_id}")
        resp.raise_for_status()
        return resp.json()

    async def hydrate_memory(self, memory_id: str) -> bytes:
        """Materialize a memory's payload on demand (lazy hydration)."""
        resp = await self._client.post(f"/v1/memory/{memory_id}/hydrate")
        resp.raise_for_status()
        return resp.content

    async def list(
        self,
        identifier: str,
        *,
        order: str = "asc",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return memories for an identifier in created_at order."""
        resp = await self._client.get(
            "/v1/memories",
            params={"identifier": identifier, "order": order, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()["memories"]

    async def hydrate(self, file: str | None = None) -> dict[str, Any]:
        """Trigger swap file or v2 directory hydration."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        resp = await self._client.post("/v1/hydrate", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def snapshot(
        self,
        file: str | None = None,
        *,
        copy_attachments: bool | None = None,
        package: bool | None = None,
        gz: bool | None = None,
    ) -> dict[str, Any]:
        """Trigger database snapshot to swap file, v2 directory, or package."""
        payload: dict[str, Any] = {}
        if file:
            payload["file"] = file
        if copy_attachments is not None:
            payload["copy_attachments"] = copy_attachments
        if package is not None:
            payload["package"] = package
        if gz is not None:
            payload["gz"] = gz
        resp = await self._client.post("/v1/snapshot", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def handoff_prepare(
        self,
        source: str,
        output: str,
        *,
        consent: str,
        mode: str = "resume",
        session: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": source,
            "output": output,
            "mode": mode,
            "consent": consent,
        }
        if session is not None:
            payload["session"] = session
        resp = await self._client.post("/v1/handoff/prepare", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def handoff_verify(self, package: str) -> dict[str, Any]:
        resp = await self._client.post("/v1/handoff/verify", json={"package": package})
        resp.raise_for_status()
        return resp.json()

    async def handoff_inspect(self, package: str) -> dict[str, Any]:
        resp = await self._client.get("/v1/handoff/inspect", params={"package": package})
        resp.raise_for_status()
        return resp.json()

    async def handoff_hydrate(self, package: str) -> dict[str, Any]:
        resp = await self._client.post("/v1/handoff/hydrate", json={"package": package})
        resp.raise_for_status()
        return resp.json()

    async def promote(
        self,
        memory_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to_state": to_state}
        if reason is not None:
            payload["reason"] = reason
        if actor is not None:
            payload["actor"] = actor
        resp = await self._client.post(f"/v1/memory/{memory_id}/promote", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def candidates(
        self, *, namespace: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if namespace is not None:
            params["namespace"] = namespace
        if state is not None:
            params["state"] = state
        resp = await self._client.get("/v1/promotion/candidates", params=params)
        resp.raise_for_status()
        return resp.json()["candidates"]

    async def events(
        self,
        *,
        memory_id: str | None = None,
        namespace: str | None = None,
        event_type: str | None = None,
        after_seq: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if memory_id is not None:
            params["memory_id"] = memory_id
        if namespace is not None:
            params["namespace"] = namespace
        if event_type is not None:
            params["event_type"] = event_type
        if after_seq is not None:
            params["after_seq"] = after_seq
        resp = await self._client.get("/v1/events", params=params)
        resp.raise_for_status()
        return resp.json()

    async def revoke_policy(
        self,
        *,
        caller_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Revoke a caller credential and/or share approval (management only)."""
        payload: dict[str, Any] = {}
        if caller_id is not None:
            payload["caller_id"] = caller_id
        if approval_id is not None:
            payload["approval_id"] = approval_id
        resp = await self._client.post("/v1/policy/revoke", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def reload_policy(self) -> dict[str, Any]:
        """Reload the daemon policy file (management only)."""
        resp = await self._client.post("/v1/policy/reload", json={})
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHotMemClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
