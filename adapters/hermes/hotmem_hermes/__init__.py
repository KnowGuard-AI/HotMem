"""HotMem Hermes Agent adapter.

Implements the Hermes Agent Memory Provider Plugin interface so HotMem
becomes a first-class ``memory.provider: hotmem`` backend.

See: https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin
"""

from hotmem_hermes.provider import HotMemMemoryProvider

__all__ = ["HotMemMemoryProvider"]
__version__ = "0.1.0"
