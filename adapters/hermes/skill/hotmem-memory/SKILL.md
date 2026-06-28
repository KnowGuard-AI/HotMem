---
name: hotmem-memory
description: Persist durable learnings to HotMem so they survive across sessions
version: 1.0.0
metadata:
  hermes:
    tags: [memory, hotmem]
    requires_toolsets: []
---
# HotMem Memory Persistence

## When to Use
After learning a durable fact, convention, or correction that should outlive this session.

## Procedure
1. Call `hotmem_store` with `identifier="hermes:learned"`, the fact, and importance (0.7 default, 0.9 for user preferences)
2. For environment facts, use `identifier="hermes:memory"`
3. For user preferences, use `identifier="hermes:user"`

## Pitfalls
- Do not store ephemeral session state (file paths, debug context)
- Do not duplicate what is already in MEMORY.md — HotMem mirrors those automatically
