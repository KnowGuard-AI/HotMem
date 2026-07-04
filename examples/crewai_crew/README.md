# crewai_crew

A 2-agent CrewAI crew (Researcher + Writer) sharing a single HotMem store so
the Writer recalls what the Researcher saved.

## Setup

```sh
pip install -e ".[dev,mcp]"
pip install -e adapters/crewai
pip install crewai                                # framework dep

hotmem serve
```

## Run

```sh
python crew.py
```
