"""Pydantic AI agent with HotMem dependency + recall system prompt.

Uses HotMemDeps as the agent's dependency container and recall_system_prompt
to inject ranked memories into the system prompt each turn. A `remember`
tool persists new facts.

Run: hotmem serve  then  python agent.py
"""

from __future__ import annotations

import asyncio
import os

from hotmem_pydanticai import HotMemDeps, recall_system_prompt
from pydantic_ai import Agent, RunContext

HOTMEM_URL = os.environ.get("HOTMEM_URL", "http://127.0.0.1:8711")


def build_agent() -> Agent[HotMemDeps, str]:
    agent: Agent[HotMemDeps, str] = Agent("test", deps_type=HotMemDeps)
    agent.system_prompt(recall_system_prompt)

    @agent.tool
    async def remember(ctx: RunContext[HotMemDeps], fact: str) -> str:
        """Persist a fact the user wants remembered."""
        ctx.deps.client.add(ctx.deps.identifier, fact, source="pydanticai-example")
        return f"Remembered: {fact}"

    return agent


async def main() -> None:
    deps = HotMemDeps.from_url(HOTMEM_URL, identifier="user-7")
    agent = build_agent()

    # Seed a memory so recall has something to surface.
    deps.client.add("user-7", "User prefers concise answers.", source="pydanticai-example")

    for prompt in [
        "What style of answer do I prefer?",
        "Remember that I also like examples.",
    ]:
        result = await agent.run(prompt, deps=deps)
        print(f"User: {prompt}")
        print(f"Agent: {result.output}\n")


if __name__ == "__main__":
    asyncio.run(main())
