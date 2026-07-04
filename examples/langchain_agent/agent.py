"""LangChain agent with HotMem memory.

A 3-turn loop: recall relevant memories, call the LLM, store any new facts.
Uses HotMemRetriever (hybrid search) + HotMemChatMessageHistory (turn log).

Run: hotmem serve  then  python agent.py
"""

from __future__ import annotations

import os

from hotmem_langchain import HotMemRetriever

from hotmem.client import HotMemClient

HOTMEM_URL = os.environ.get("HOTMEM_URL", "http://127.0.0.1:8711")
IDENTIFIER = "user-42"


def get_llm() -> object:
    """Return a chat LLM. Falls back to a deterministic stub when no API key."""
    if os.environ.get("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o-mini", temperature=0)

    from langchain_core.language_models import FakeListChatModel

    return FakeListChatModel(
        responses=[
            "Got it — I'll remember you prefer dark mode.",
            "Based on what I recall, you prefer dark mode. Want me to keep it that way?",
            "Done. Your preference is saved.",
        ]
    )


def main() -> None:
    client = HotMemClient(HOTMEM_URL)
    retriever = HotMemRetriever(base_url=HOTMEM_URL, top_k=3)
    llm = get_llm()

    turns = [
        "Please remember I prefer dark mode.",
        "What UI theme do I like?",
        "Thanks, keep it that way.",
    ]

    for user_text in turns:
        docs = retriever.invoke(user_text)
        context = "\n".join(f"- {d.page_content}" for d in docs) or "(no memories yet)"
        prompt = f"Memories:\n{context}\n\nUser: {user_text}\nAssistant:"
        reply = llm.invoke(prompt)
        answer = reply.content if hasattr(reply, "content") else str(reply)
        print(f"User: {user_text}")
        print(f"Assistant: {answer}\n")

        # Persist the user's stated fact (heuristic: short declarative turns).
        if "remember" in user_text.lower() or "prefer" in user_text.lower():
            client.add(IDENTIFIER, user_text, source="langchain-example")


if __name__ == "__main__":
    main()
