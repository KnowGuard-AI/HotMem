"""LangChain BaseRetriever backed by HotMem search."""

from __future__ import annotations

from typing import Any

from hotmem.client import HotMemClient
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import PrivateAttr


class HotMemRetriever(BaseRetriever):
    """Retrieve LangChain Documents from HotMem hybrid search."""

    base_url: str = "http://127.0.0.1:8711"
    top_k: int = 5
    max_chars: int | None = None
    _client: HotMemClient = PrivateAttr()

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._client = HotMemClient(self.base_url)

    @property
    def client(self) -> HotMemClient:
        return self._client

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        memories = self._client.search(
            query,
            top_k=self.top_k,
            max_chars=self.max_chars,
        )
        return [
            Document(
                page_content=m["content"],
                metadata={
                    "memory_id": m["memory_id"],
                    "identifier": m["identifier"],
                    "score": m["score"],
                    "source": "hotmem",
                },
            )
            for m in memories
        ]
