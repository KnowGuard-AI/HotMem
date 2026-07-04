# mcp_claude_desktop

Wire the HotMem MCP server into Claude Desktop so Claude can add and search
your memories as tools.

## Setup

See [../README.md](../README.md#prerequisites) for the common HotMem install
steps. This example needs the MCP extra:

```sh
pip install -e ".[mcp]"
```

## Configure Claude Desktop

Copy the snippet below into your Claude Desktop config:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "hotmem": {
      "command": "hotmem",
      "args": ["mcp", "--db", "~/hotmem.sqlite"]
    }
  }
}
```

Restart Claude Desktop. The HotMem tools (`add_memory`, `search_memories`,
`memory_health`, `snapshot`, `hydrate`) are now available.

## Try it

Ask Claude:
- "Remember that I prefer dark mode and vim keybindings."
- "What UI preferences have I told you about?"

Claude calls `search_memories` to recall and `add_memory` to persist.
