"""LangChain BaseChatMessageHistory backed by HotMem."""

from __future__ import annotations

from typing import Any

from hotmem.client import HotMemClient
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class HotMemChatMessageHistory(BaseChatMessageHistory):
    """Persist chat messages to HotMem under a shared identifier.

    The session id is used as the HotMem identifier so all messages in a
    conversation are grouped and searchable together.
    """

    def __init__(
        self,
        session_id: str,
        *,
        base_url: str = "http://127.0.0.1:8711",
        client: HotMemClient | None = None,
    ) -> None:
        self.session_id = session_id
        self._client = client or HotMemClient(base_url)

    @property
    def client(self) -> HotMemClient:
        return self._client

    def add_message(self, message: Any) -> None:
        role = getattr(message, "type", None) or "system"
        self._client.add(
            identifier=self.session_id,
            fact=message.content,
            source=f"langchain:{role}",
            importance=0.5,
            metadata={"role": role},
        )

    def add_user_message(self, message: str) -> None:
        self.add_message(HumanMessage(content=message))

    def add_ai_message(self, message: str) -> None:
        self.add_message(AIMessage(content=message))

    def messages(self) -> list[Any]:
        """Return messages for this session.

        Note: HotMem search returns results ranked by relevance, not
        insertion order. Until HotMem exposes timestamps, ordering is
        best-effort by score.
        """
        memories = self._client.search(self.session_id, top_k=100, max_chars=10_000)
        out: list[Any] = []
        for m in memories:
            role = (
                m.get("metadata", {}).get("role", m.get("role", "system"))
                if isinstance(m.get("metadata"), dict)
                else m.get("role", "system")
            )
            content = m["content"]
            if role == "human":
                out.append(HumanMessage(content=content))
            elif role == "ai":
                out.append(AIMessage(content=content))
            else:
                out.append(SystemMessage(content=content))
        return out

    def clear(self) -> None:
        # HotMem v0.1 has no delete; clearing is a no-op.
        # Future versions may support TTL-based expiry per identifier.
        pass
