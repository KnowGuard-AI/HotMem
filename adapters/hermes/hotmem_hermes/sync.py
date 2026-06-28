"""Hermes <-> HotMem deep memory sync.

Wires the Hermes lifecycle hooks the base provider leaves as no-ops into a
two-way memory bridge:

    on_memory_write(action, target, content) -> mirror MEMORY.md/USER.md to HotMem
    on_pre_compress(messages)               -> extract durable facts before discard
    on_session_end(messages)               -> flush + snapshot to a swap file

All hooks run through the provider's AsyncHotMemClient and are non-blocking.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from typing import Any

# Importance biasing: user-profile facts surface first in recall.
_IMPORTANCE = {"user": 0.9, "memory": 0.8}
_IDENTIFIER = {"user": "hermes:user", "memory": "hermes:memory"}

# Heuristic markers that a turn contains a durable fact worth persisting.
_DURABLE_PATTERNS = re.compile(
    r"\b(actually|remember that|from now on|don'?t|always|never|prefer)\b",
    re.IGNORECASE,
)


class HotMemSync:
    """Composable sync hooks for HotMemMemoryProvider."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self._thread: threading.Thread | None = None

    def _run(self, coro_factory: Any) -> None:
        """Run an async coroutine in a daemon thread (non-blocking)."""

        def _work() -> None:
            with contextlib.suppress(Exception):
                asyncio.run(coro_factory())

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = threading.Thread(target=_work, daemon=True)
        self._provider._sync_thread = self._thread  # noqa: SLF001
        self._thread.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        """Mirror a built-in MEMORY.md/USER.md write to HotMem.

        Biased importance: user-profile facts (0.9) rank above env facts (0.8).
        HotMem dedupes via content-hash, so ``replace`` is just a re-add.
        ``remove`` best-effort sets a short TTL (HotMem has no delete in v0.1).
        """
        identifier = _IDENTIFIER.get(target, f"hermes:{target}")
        importance = _IMPORTANCE.get(target, 0.5)
        client = self._provider._client  # noqa: SLF001

        async def _go() -> None:
            if action == "remove":
                await client.add(
                    identifier,
                    content,
                    importance=importance,
                    ttl_seconds=1,
                    metadata={"action": "removed"},
                )
                return
            await client.add(
                identifier,
                content,
                source="hermes:memory_write",
                importance=importance,
                metadata={"target": target, "action": action},
            )

        self._run(_go)

    def on_pre_compress(self, messages: list[Any], **kwargs: Any) -> None:
        """Extract durable-looking facts from trailing context before compression.

        Heuristic only — no LLM call. Defers richer extraction to a later issue.
        """
        client = self._provider._client  # noqa: SLF001
        trailing = messages[-6:] if len(messages) > 6 else messages

        async def _go() -> None:
            for msg in trailing:
                text = _msg_text(msg)
                if not text or not _DURABLE_PATTERNS.search(text):
                    continue
                await client.add(
                    "hermes:context",
                    text[:1000],
                    source="hermes:pre_compress",
                    importance=0.6,
                    metadata={"phase": "pre_compress"},
                )

        self._run(_go)

    def on_session_end(self, messages: list[Any], **kwargs: Any) -> None:
        """Flush and snapshot to a profile-scoped swap file for instant hydrate."""
        client = self._provider._client  # noqa: SLF001
        swap_path = self._provider._swap_path  # noqa: SLF001

        async def _go() -> None:
            if swap_path:
                await client.snapshot(file=swap_path)

        self._run(_go)


def _msg_text(msg: Any) -> str:
    """Best-effort text extraction from an OpenAI-style message."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        return msg.get("content", "") or ""
    return getattr(msg, "content", "") or ""


def attach_sync(provider: Any) -> HotMemSync:
    """Compose the sync hooks onto a provider instance.

    Called by ``register()`` so the provider gains on_memory_write /
    on_pre_compress / on_session_end without subclassing.
    """
    sync = HotMemSync(provider)
    provider.on_memory_write = sync.on_memory_write  # type: ignore[attr-defined]
    provider.on_pre_compress = sync.on_pre_compress  # type: ignore[attr-defined]
    provider.on_session_end = sync.on_session_end  # type: ignore[attr-defined]
    return sync
