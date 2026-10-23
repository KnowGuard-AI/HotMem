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

Or connect as a thin stdio-to-daemon relay to a shared running `hotmem serve` daemon (#109):

```json
{
  "mcpServers": {
    "hotmem": {
      "command": "hotmem",
      "args": [
        "mcp",
        "--url", "http://127.0.0.1:8711",
        "--token", "your-caller-token",
        "--session", "claude-desktop-session",
        "--source", "mcp://claude-desktop",
        "--store-id", "store-incident-01"
      ]
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

## Windows

Install the `[mcp]` extra into a dedicated environment. Configure the actual
absolute executable and mount paths; MCP hosts do not run these arguments
through PowerShell and must not rely on `~`, `%USERPROFILE%`, activation,
or the GUI process's PATH. Replace the example paths with your installation:

```json
{
  "mcpServers": {
    "hotmem": {
      "command": "C:\\Users\\you\\HotMem\\.venv-check\\Scripts\\hotmem.exe",
      "args": ["mcp", "--mount", "C:\\Users\\you\\HotMem data", "--embedder", "hash", "--reranker", "none"]
    }
  }
}
```

Use `hotmem mcp` directly, without a shell wrapper that writes banners to
stdout. Startup diagnostics belong on stderr; stdout is the MCP protocol.
The real stdio regression tests check initialization, tools, writes/search,
restart persistence, JSONL/JSONL.GZ and Snapshot v2 restore, verification
failure, and EOF cleanup. Snapshot v2 over MCP is added by PR #121 and is
not present in the previously published 0.2.6 package. These protocol tests
do not claim that the Claude Desktop GUI itself was exercised.

See [Windows setup and qualification](../../docs/quickstart.md#windows-qualification)
for PowerShell installation, reproduction, and support limits.
