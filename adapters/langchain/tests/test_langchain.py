"""Tests for hotmem_langchain — back the adapters with a real in-process server."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hotmem.server import create_app


@pytest.fixture
def hotmem_url(tmp_path: Path) -> str:
    db_path = tmp_path / "langchain_test.sqlite"
    app = create_app(db_path=db_path)
    with TestClient(app) as transport:
        yield transport


def _make_client(hotmem_url, base_url: str = "http://testserver"):
    from hotmem.client import HotMemClient

    c = HotMemClient.__new__(HotMemClient)
    c.base_url = base_url
    c._client = hotmem_url
    return c


def test_message_history_round_trip(hotmem_url):
    from hotmem_langchain import HotMemChatMessageHistory

    hist = HotMemChatMessageHistory.__new__(HotMemChatMessageHistory)
    hist.session_id = "sess-1"
    hist._client = _make_client(hotmem_url)

    hist.add_user_message("hello there")
    hist.add_ai_message("hi! how can I help?")

    msgs = hist.messages()
    assert len(msgs) == 2
    contents = {m.content for m in msgs}
    assert contents == {"hello there", "hi! how can I help?"}


def test_retriever_returns_documents(hotmem_url):
    from hotmem_langchain import HotMemRetriever

    client = _make_client(hotmem_url)
    client.add("vendor_x", "Invoice total $5000 detected", importance=0.9)

    retriever = HotMemRetriever(base_url="http://testserver")
    retriever._client = client

    docs = retriever.invoke("invoice")
    assert len(docs) >= 1
    assert "Invoice" in docs[0].page_content
    assert docs[0].metadata["source"] == "hotmem"
    assert "score" in docs[0].metadata
