"""HotMem Hermes Agent memory provider plugin.

Implements the Hermes Agent MemoryProvider ABC so HotMem runs as a
``memory.provider: hotmem`` backend alongside the built-in MEMORY.md/USER.md.

Hermes lifecycle hooks wired here:
    - prefetch(query)            -> ranked recall before each LLM turn
    - sync_turn(user, assistant) -> persist turns (async, non-blocking)
    - system_prompt_block()       -> static header
    - shutdown()                 -> close the client

Sync-layer hooks (on_memory_write, on_pre_compress, on_session_end) live in
``hotmem_hermes.sync`` and are composed by ``register()``.

Reference:
    https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from hotmem.client import AsyncHotMemClient

try:
    # When loaded inside a Hermes install, use the real ABC.
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - exercised only outside Hermes

    class MemoryProvider:  # type: ignore[no-redef]
        """Local fallback mirroring the Hermes MemoryProvider contract."""

        name: str = ""

        def is_available(self) -> bool:
            raise NotImplementedError

        def initialize(self, session_id: str, **kwargs: Any) -> None:
            raise NotImplementedError

        def get_tool_schemas(self) -> list[dict[str, Any]]:
            raise NotImplementedError

        def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> Any:
            raise NotImplementedError

        def get_config_schema(self) -> list[dict[str, Any]]:
            return []

        def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
            pass

        def system_prompt_block(self) -> str:
            return ""

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def sync_turn(
            self,
            user_content: str,
            assistant_content: str,
            *,
            session_id: str = "",
            messages: list[Any] | None = None,
        ) -> None:
            pass

        def shutdown(self) -> None:
            pass


_SEARCH_TOOL: dict[str, Any] = {
    "name": "hotmem_search",
    "description": "Search HotMem and return ranked, LLM-ready memories.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_chars": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
    },
}

_STORE_TOOL: dict[str, Any] = {
    "name": "hotmem_store",
    "description": "Store a fact in HotMem memory.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "identifier": {"type": "string"},
            "fact": {"type": "string"},
            "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "ttl_seconds": {"type": "integer", "minimum": 1},
        },
        "required": ["identifier", "fact"],
    },
}


class HotMemMemoryProvider(MemoryProvider):
    """Hermes memory provider backed by a local HotMem sidecar."""

    def __init__(self) -> None:
        self._client: AsyncHotMemClient | None = None
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._base_url: str = "http://127.0.0.1:8711"
        self._swap_path: str | None = None
        self._sync_thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return "hotmem"

    def is_available(self) -> bool:
        """Activate when a HotMem URL is configured. No network calls."""
        url = os.environ.get("HOTMEM_URL")
        if url:
            return True
        if self._hermes_home:
            return (Path(self._hermes_home) / "hotmem.json").exists()
        return False

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home", "")
        self._session_id = session_id
        self._hermes_home = hermes_home

        config = self._load_config(hermes_home)
        self._base_url = os.environ.get("HOTMEM_URL") or config.get(
            "hotmem_url", "http://127.0.0.1:8711"
        )
        self._swap_path = config.get("swap_path") or (
            str(Path(hermes_home) / "hotmem-swap.jsonl") if hermes_home else None
        )
        self._client = AsyncHotMemClient(self._base_url)

    def _load_config(self, hermes_home: str) -> dict[str, Any]:
        if not hermes_home:
            return {}
        path = Path(hermes_home) / "hotmem.json"
        if path.exists():
            return json.loads(path.read_text())
        return {}

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "hotmem_url",
                "description": "HotMem sidecar URL",
                "default": "http://127.0.0.1:8711",
            },
            {
                "key": "swap_path",
                "description": "Profile-scoped swap file for snapshot/hydrate",
                "default": "",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        Path(hermes_home).mkdir(parents=True, exist_ok=True)
        path = Path(hermes_home) / "hotmem.json"
        path.write_text(json.dumps(values, indent=2))

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [_SEARCH_TOOL, _STORE_TOOL]

    async def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert self._client is not None
        if tool_name == "hotmem_search":
            memories = await self._client.search(
                args["query"],
                top_k=args.get("top_k", 5),
                max_chars=args.get("max_chars"),
            )
            return {"memories": memories, "count": len(memories)}
        if tool_name == "hotmem_store":
            result = await self._client.add(
                args["identifier"],
                args["fact"],
                importance=args.get("importance", 0.5),
                ttl_seconds=args.get("ttl_seconds"),
            )
            return result
        raise ValueError(f"unknown tool: {tool_name}")

    def system_prompt_block(self) -> str:
        return "HotMem memory provider active — recall injected before each turn."

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        assert self._client is not None
        memories = await self._client.search(query, top_k=5)
        if not memories:
            return ""
        lines = [f"- {m['content']}" for m in memories]
        return "HotMem recall:\n" + "\n".join(lines)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[Any] | None = None,
    ) -> None:
        """Persist a turn asynchronously (non-blocking, per threading contract)."""
        sid = session_id or self._session_id or "hermes"

        def _sync() -> None:
            import asyncio

            async def _go() -> None:
                assert self._client is not None
                await self._client.add(
                    sid,
                    f"User: {user_content}",
                    source="hermes:turn",
                    importance=0.4,
                    metadata={"role": "user", "session": sid},
                )
                await self._client.add(
                    sid,
                    f"Assistant: {assistant_content}",
                    source="hermes:turn",
                    importance=0.3,
                    metadata={"role": "assistant", "session": sid},
                )

            with suppress(Exception):
                asyncio.run(_go())

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_sync, daemon=True)
        self._sync_thread.start()

    def shutdown(self) -> None:
        import asyncio

        if self._client is not None:
            with suppress(Exception):
                asyncio.run(self._client.close())
            self._client = None


def register(ctx: Any) -> None:
    """Plugin entry point called by Hermes memory plugin discovery."""
    provider = HotMemMemoryProvider()
    from hotmem_hermes.sync import attach_sync

    attach_sync(provider)
    ctx.register_memory_provider(provider)
