"""CrewAI crew with shared HotMem memory.

Researcher saves a finding; Writer recalls it via the same HotMem store,
demonstrating cross-agent memory continuity without a hand-rolled bus.

Run: hotmem serve  then  python crew.py
"""

from __future__ import annotations

import os

from hotmem_crewai import HotMemMemory

HOTMEM_URL = os.environ.get("HOTMEM_URL", "http://127.0.0.1:8711")


def main() -> None:
    memory = HotMemMemory(base_url=HOTMEM_URL)

    # Researcher agent stores a finding.
    memory.save(
        "Q3 revenue grew 18% driven by enterprise renewals.",
        identifier="researcher",
        importance=0.8,
    )
    print("Researcher saved: Q3 revenue finding")

    # Writer agent recalls relevant context before drafting.
    hits = memory.load("revenue growth", top_k=3)
    context = "\n".join(f"- {h['content']}" for h in hits)
    print(f"Writer recalled:\n{context}")

    summary = (
        "Based on the research team's findings, Q3 saw 18% revenue growth, "
        "primarily from enterprise renewals."
    )
    print(f"Writer draft: {summary}")

    memory.save(summary, identifier="writer", importance=0.6)


if __name__ == "__main__":
    main()
