# crewai_crew

A 2-agent CrewAI crew (Researcher + Writer) sharing a single HotMem store so
the Writer recalls what the Researcher saved.

## Setup

See [../README.md](../README.md#prerequisites) for the common HotMem install +
`hotmem serve` steps. Then install this example's framework deps:

```sh
pip install -e adapters/crewai
pip install crewai
```

## Run

```sh
python crew.py
```
