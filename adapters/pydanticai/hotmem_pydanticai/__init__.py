"""HotMem Pydantic AI adapter.

Exposes HotMem as a Pydantic AI dependency and tool provider.
"""

from hotmem_pydanticai.provider import HotMemDeps, recall_system_prompt

__all__ = ["HotMemDeps", "recall_system_prompt"]
__version__ = "0.1.0"
