# autogen_group_chat

A 3-agent AutoGen group chat using the HotMem memory plugin so each agent
recalls prior turns before speaking.

## Setup

```sh
pip install -e ".[dev,mcp]"
pip install -e adapters/autogen
pip install "autogen-agentchat>=0.4"              # framework dep

hotmem serve
```

## Run

```sh
python chat.py
```
