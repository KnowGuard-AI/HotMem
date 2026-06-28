# hotmem

A typed, zero-dependency TypeScript client for the [HotMem](https://github.com/KnowGuard-AI/HotMem) local-first memory sidecar.

Uses the global `fetch` — works in **Node.js 18+**, **Deno**, **Bun**, and edge runtimes (Cloudflare Workers).

## Install

```sh
npm install hotmem
```

## Quickstart

```sh
hotmem serve  # start the sidecar on http://127.0.0.1:8711
```

```typescript
import { HotMemClient } from "hotmem";

const client = new HotMemClient("http://127.0.0.1:8711");

await client.add("vendor_x", "Invoice total $5000", { importance: 0.8 });
await client.add("vendor_x", "Prefers email over phone", {
  source: "intake-call",
  ttlSeconds: 86400,
});

const memories = await client.search("duplicate invoice risk", { topK: 5 });
// memories[0] => { role: "system", content: "...", score: 0.92, ... }

const health = await client.health();
console.log(`${health.memory_count} memories stored`);
```

## API

| Method | Description |
| --- | --- |
| `add(identifier, fact, options?)` | Store a fact. Options: `source`, `importance` (0-1), `metadata`, `ttlSeconds` |
| `search(query, options?)` | Hybrid-ranked search. Options: `topK` (default 5), `maxChars` |
| `health()` | Server status: memory count, uptime, db path |
| `hydrate(file?)` | Load memories from a JSONL swap file |
| `snapshot(file?)` | Export all memories to a JSONL swap file |

## Errors

Non-2xx responses throw a `HotMemError` with `status` and `body`. Network failures also throw `HotMemError` with `status: 0`.

## License

MIT
