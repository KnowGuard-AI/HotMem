#!/usr/bin/env python3
"""Check a Claude Code `stream-json` transcript of the handoff continuation.

Usage: check_claude_continuation.py <stream.jsonl> <package-dir>

Passes only when the transcript itself shows that Claude called
`mcp__hotmem__search_memories`, that the tool result carried the hydrated
resume brief, and that the final answer quotes a next action, source id, and
durable memory that exist in the package. Tool use is read from the recorded
tool_use / tool_result events, never inferred from the model's prose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SEARCH_TOOL = "mcp__hotmem__search_memories"


def fail(message: str, stream: Path) -> None:
    raise SystemExit(f"continuation check FAILED: {message}\n  transcript: {stream}")


def load_events(stream: Path) -> list[dict]:
    try:
        lines = stream.read_text(encoding="utf-8").splitlines()
    except OSError as err:
        fail(f"cannot read transcript ({err})", stream)
    events = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as err:
            fail(f"line {number} is not JSON ({err.msg}): {line[:120]!r}", stream)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            fail(f"line {number} is not a stream-json event object", stream)
        events.append(event)
    if not events:
        fail("transcript is empty; the Claude CLI produced no output", stream)
    return events


def content_blocks(event: dict) -> list[dict]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def mcp_status(events: list[dict]) -> str:
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            servers = event.get("mcp_servers")
            return f"mcp_servers at init: {json.dumps(servers)}"
    return "no system/init event was recorded"


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    stream, package = Path(sys.argv[1]), Path(sys.argv[2])

    brief = json.loads((package / "resume-brief.json").read_text(encoding="utf-8"))
    next_actions = {
        item["text"]: item["source_entry_id"] for item in brief["sections"]["next_actions"]
    }
    memories = [
        json.loads(line)
        for line in (package / "memories.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    brief_identifier = f"handoff/{manifest['source']['label']}/resume-brief"

    events = load_events(stream)

    # 1. The CLI must have finished with exactly one successful result event.
    results = [event for event in events if event["type"] == "result"]
    if len(results) != 1:
        fail(
            f"expected exactly one result event, found {len(results)} "
            f"(event types: {sorted({event['type'] for event in events})})",
            stream,
        )
    result = results[0]
    if result.get("is_error") or result.get("subtype") != "success":
        detail = {
            key: result.get(key)
            for key in ("subtype", "errors", "result", "permission_denials")
            if result.get(key)
        }
        fail(f"Claude CLI reported an error: {json.dumps(detail)[:600]}", stream)

    # 2. Tool-use evidence comes from recorded events, not from the answer text.
    calls: dict[str, str] = {}
    outcomes: dict[str, dict] = {}
    for event in events:
        for block in content_blocks(event):
            if event["type"] == "assistant" and block.get("type") == "tool_use":
                calls[str(block.get("id"))] = str(block.get("name"))
            elif event["type"] == "user" and block.get("type") == "tool_result":
                outcomes[str(block.get("tool_use_id"))] = block
    other_tools = sorted({name for name in calls.values() if name != SEARCH_TOOL})
    if other_tools:
        fail(f"Claude called tools outside the read-only search scope: {other_tools}", stream)
    if not calls:
        fail(
            f"no {SEARCH_TOOL} tool_use event was recorded; {mcp_status(events)}; "
            f"permission_denials: {json.dumps(result.get('permission_denials'))}",
            stream,
        )
    brief_calls = [
        call_id
        for call_id in calls
        if call_id in outcomes
        and not outcomes[call_id].get("is_error")
        and brief_identifier in json.dumps(outcomes[call_id].get("content"))
    ]
    if not brief_calls:
        fail(
            f"{len(calls)} {SEARCH_TOOL} call(s) were recorded, but none has a successful "
            f"tool_result containing {brief_identifier}",
            stream,
        )

    # 3. The final answer must be the requested JSON object, grounded in the package.
    text = result.get("result")
    if not isinstance(text, str):
        fail(f"result event has no text result (got {type(text).__name__})", stream)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        fail(f"final answer contains no JSON object: {text[:200]!r}", stream)
    try:
        answer = json.loads(text[start : end + 1])
    except json.JSONDecodeError as err:
        fail(f"final answer is not valid JSON ({err.msg}): {text[:200]!r}", stream)
    if not isinstance(answer, dict):
        fail("final answer is not a JSON object", stream)

    if answer.get("retrieved") is not True:
        fail("answer does not report retrieved=true", stream)
    if not isinstance(answer.get("goal"), str) or not answer["goal"].strip():
        fail("answer has no goal", stream)
    action = answer.get("next_action")
    if action not in next_actions:
        fail(
            f"next_action {action!r} is not a next action captured in the brief "
            f"(expected one of {sorted(next_actions)})",
            stream,
        )
    sources = answer.get("source_entry_ids")
    if not isinstance(sources, list) or next_actions[action] not in sources:
        fail(
            f"source_entry_ids {sources!r} does not cite {next_actions[action]}, "
            "the source of the quoted next action",
            stream,
        )
    cited = answer.get("memory_identifiers")
    if not isinstance(cited, list):
        fail(f"memory_identifiers is not a list: {cited!r}", stream)
    known = [
        value
        for value in cited
        if isinstance(value, str)
        and any(
            value == memory["identifier"] or (len(value) >= 12 and memory["id"].startswith(value))
            for memory in memories
        )
    ]
    if not known:
        fail(
            f"memory_identifiers {cited!r} names no durable memory from the package "
            f"(expected one of {[memory['identifier'] for memory in memories]})",
            stream,
        )

    print(
        f"Claude continuation: ok — {len(brief_calls)} recorded {SEARCH_TOOL} call(s) "
        f"returned {brief_identifier}"
    )
    print(f"  captured next action: {action} [source: {next_actions[action]}]")
    print(f"  durable memories cited: {', '.join(known)}")
    print(f"  transcript: {stream}")


if __name__ == "__main__":
    main()
