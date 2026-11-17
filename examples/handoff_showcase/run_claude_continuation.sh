#!/usr/bin/env bash
# Run the local Codex → HotMem handoff, then continue from it with Claude Code.
# The Claude session can call only HotMem's read-only search_memories tool.
#
#   run_claude_continuation.sh [--local-only] [WORK_DIR]
#
# --local-only (or HOTMEM_HANDOFF_LOCAL_ONLY=1) stops before the Claude call:
# it prepares and hydrates the package, writes the MCP config, and proves the
# brief is retrievable through that exact MCP server. No provider, no quota.
set -euo pipefail

LOCAL_ONLY="${HOTMEM_HANDOFF_LOCAL_ONLY:-0}"
if [[ "${1:-}" == "--local-only" ]]; then
    LOCAL_ONLY=1
    shift
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/hotmem-claude-handoff.XXXXXX)}"
UV_BIN="$(command -v uv || true)"
CLAUDE_BIN="$(command -v claude || true)"
MAX_BUDGET_USD="${CLAUDE_MAX_BUDGET_USD:-0.25}"

if [[ -z "$UV_BIN" ]]; then
    echo "uv is required; install it and retry." >&2
    exit 1
fi
if [[ "$LOCAL_ONLY" != "1" && -z "$CLAUDE_BIN" ]]; then
    echo "Claude Code CLI is required; install it and retry." >&2
    exit 1
fi
if [[ ! "$MAX_BUDGET_USD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "CLAUDE_MAX_BUDGET_USD must be a non-negative decimal." >&2
    exit 1
fi

mkdir -p "$WORK"
echo "work dir: $WORK"

# Produce and hydrate the deterministic package before contacting Claude.
bash "$HERE/run_showcase.sh" "$WORK"

MCP_CONFIG="$WORK/claude-mcp.json"
python3 - "$MCP_CONFIG" "$UV_BIN" "$REPO_ROOT" "$WORK/target.sqlite" <<'PY'
import json
import sys

path, uv, project, database = sys.argv[1:]
config = {
    "mcpServers": {
        "hotmem": {
            "command": uv,
            "args": [
                "run",
                "--project",
                project,
                "--extra",
                "mcp",
                "hotmem",
                "mcp",
                "--db",
                database,
            ],
        }
    }
}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(config, stream, indent=2)
    stream.write("\n")
PY

# Local gate: start the server from the config Claude will receive and call
# search_memories over stdio. Fails here, for free, if the MCP path is broken.
echo
echo "== MCP probe (local, no Claude call) =="
"$UV_BIN" run --project "$REPO_ROOT" --extra mcp python - "$MCP_CONFIG" <<'PY'
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BRIEF = "handoff/hotmem-showcase-planning/resume-brief"
server = json.load(open(sys.argv[1], encoding="utf-8"))["mcpServers"]["hotmem"]


async def probe() -> None:
    params = StdioServerParameters(command=server["command"], args=server["args"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        if "search_memories" not in tools:
            raise SystemExit(f"MCP probe FAILED: search_memories not exposed: {sorted(tools)}")
        result = await session.call_tool(
            "search_memories", {"query": "resume brief handoff showcase"}
        )
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.isError or BRIEF not in text:
            raise SystemExit(f"MCP probe FAILED: search did not return {BRIEF}: {text[:300]}")
    print(f"MCP probe: ok — search_memories over stdio returned {BRIEF}")


asyncio.run(asyncio.wait_for(probe(), timeout=120))
PY

if [[ "$LOCAL_ONLY" == "1" ]]; then
    echo
    echo "local-only: package prepared, hydrated, and retrievable over MCP: $WORK"
    echo "Claude was NOT invoked; the continuation itself is still unverified."
    exit 0
fi

PROMPT="$(cat <<'EOF'
Use the HotMem MCP search_memories tool to retrieve the resume brief for the
Codex-to-HotMem handoff. Search for: resume brief handoff showcase.

The brief is a historical capture of a Codex session from 2026-09-16. Its next
action is what was open when the session was captured and may since have been
completed; report it as captured, without claiming it is still open.

Use only the retrieved brief and its linked memories. Continue from the
captured task context. Return one JSON object with these keys:
  retrieved: boolean
  goal: short string
  next_action: exact text of one listed next action, or empty if unavailable
  source_entry_ids: array of source ids supporting the next action
  memory_identifiers: array of durable-memory identifiers you used

Do not infer missing context or claim native transcript migration. If the
brief is not retrievable, set retrieved to false and leave the other fields
empty.
EOF
)"

# stream-json (which the CLI only emits with --verbose) records every tool_use
# and tool_result event, so the check below reads tool use from the transcript
# instead of trusting the model's own account of what it did.
STREAM="$WORK/claude-continuation.stream.jsonl"
STDERR_LOG="$WORK/claude-continuation.stderr.txt"
echo
echo "== Claude continuation (model call; budget cap \$$MAX_BUDGET_USD) =="
CLAUDE_STATUS=0
"$CLAUDE_BIN" \
    --print \
    --output-format stream-json \
    --verbose \
    --strict-mcp-config \
    --mcp-config "$MCP_CONFIG" \
    --restricted \
    --tools "" \
    --allowedTools=mcp__hotmem__search_memories \
    --permission-mode dontAsk \
    --permission-prompts none \
    --no-session-persistence \
    --max-budget-usd "$MAX_BUDGET_USD" \
    "$PROMPT" < /dev/null > "$STREAM" 2> "$STDERR_LOG" || CLAUDE_STATUS=$?

if [[ ! -s "$STREAM" ]]; then
    echo "Claude CLI exited $CLAUDE_STATUS without output; stderr follows:" >&2
    cat "$STDERR_LOG" >&2
    exit 1
fi
if [[ "$CLAUDE_STATUS" -ne 0 ]]; then
    echo "Claude CLI exited $CLAUDE_STATUS; stderr: $STDERR_LOG" >&2
fi

# The transcript decides the outcome even after a non-zero CLI exit, so the
# diagnostic names the recorded error rather than only the exit code.
python3 "$HERE/check_claude_continuation.py" "$STREAM" "$WORK/pkg"
if [[ "$CLAUDE_STATUS" -ne 0 ]]; then
    echo "continuation check passed but the Claude CLI exited $CLAUDE_STATUS; treating as failure." >&2
    exit 1
fi
