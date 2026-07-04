"""AutoGen group chat with HotMem memory.

Three conversable agents share a HotMemMemoryPlugin. Before each turn the
plugin recalls relevant context; after each turn the utterance is persisted,
so later agents inherit earlier ones' context.

Run: hotmem serve  then  python chat.py
"""

from __future__ import annotations

import os

from hotmem_autogen import HotMemMemoryPlugin

HOTMEM_URL = os.environ.get("HOTMEM_URL", "http://127.0.0.1:8711")


def main() -> None:
    memory = HotMemMemoryPlugin(base_url=HOTMEM_URL, identifier="group-chat")

    memory.save("The project deadline is Friday.", identifier="pm", importance=0.9)
    print("PM saved: deadline is Friday")

    for speaker, query in [
        ("engineer", "what is the deadline?"),
        ("qa", "when should we finish testing?"),
    ]:
        context = memory.add_context(query)
        print(f"{speaker} recalled:\n{context or '(nothing)'}")
        msg = f"{speaker} plans around the Friday deadline."
        memory.save(msg, identifier=speaker, importance=0.5)
        print(f"{speaker} said: {msg}\n")


if __name__ == "__main__":
    main()
