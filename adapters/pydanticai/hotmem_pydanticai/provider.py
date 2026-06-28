"""Pydantic AI dependency and tools backed by HotMem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hotmem.client import HotMemClient


@dataclass
class HotMemDeps:
    """Dependency container exposing HotMem to a Pydantic AI agent.

    Pass as `deps_type=HotMemDeps` to the Agent and inject an instance at
    run time. Tools access the client via `ctx.deps.client`.
    """

    client: HotMemClient
    identifier: str = "pydanticai"
    top_k: int = 5

    @classmethod
    def from_url(cls, base_url: str = "http://127.0.0.1:8711", **kwargs: Any) -> HotMemDeps:
        return cls(client=HotMemClient(base_url), **kwargs)

    def recall(self, query: str) -> str:
        """Return ranked memories as a context string."""
        memories = self.client.search(query, top_k=self.top_k)
        if not memories:
            return ""
        lines = [f"- {m['content']}" for m in memories]
        return "Relevant memories:\n" + "\n".join(lines)


async def recall_system_prompt(ctx: Any) -> str:
    """System prompt function that injects HotMem recall.

    Usage::

        from pydantic_ai import Agent
        from hotmem_pydanticai import HotMemDeps, recall_system_prompt

        agent = Agent('openai:gpt-4o', deps_type=HotMemDeps)
        agent.system_prompt(recall_system_prompt)

        deps = HotMemDeps.from_url()
        result = await agent.run('question', deps=deps)
    """
    deps = ctx.deps
    if not isinstance(deps, HotMemDeps):
        return ""
    return deps.recall(ctx.prompt if hasattr(ctx, "prompt") else "")
