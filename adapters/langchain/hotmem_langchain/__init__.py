"""HotMem LangChain adapter.

Exposes HotMem as a LangChain BaseChatMessageHistory and BaseRetriever.
"""

from hotmem_langchain.message_history import HotMemChatMessageHistory
from hotmem_langchain.retriever import HotMemRetriever

__all__ = ["HotMemChatMessageHistory", "HotMemRetriever"]
__version__ = "0.1.0"
