# autogen_group_chat

A 3-agent AutoGen group chat using the HotMem memory plugin so each agent
recalls prior turns before speaking.

## Setup

See [../README.md](../README.md#prerequisites) for the common HotMem install +
`hotmem serve` steps. Then install this example's framework deps:

```sh
pip install -e adapters/autogen
pip install "autogen-agentchat>=0.4"
```

## Run

```sh
python chat.py
```
