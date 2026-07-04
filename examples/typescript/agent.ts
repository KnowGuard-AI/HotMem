/**
 * HotMem TypeScript client example: add facts, search, print health.
 *
 * Run: hotmem serve  then  npx tsx agent.ts
 */

import { HotMemClient } from "hotmem";

const BASE_URL = process.env.HOTMEM_URL ?? "http://127.0.0.1:8711";

async function main(): Promise<void> {
  const client = new HotMemClient(BASE_URL);

  await client.add("vendor_x", "Invoice total $5000", { importance: 0.8 });
  await client.add("vendor_x", "Prefers email over phone", {
    source: "intake-call",
    ttlSeconds: 86400,
  });
  console.log("Added 2 memories");

  const memories = await client.search("invoice risk", { topK: 5 });
  console.log("Search results:");
  for (const m of memories) {
    console.log(`  [${m.score}] ${m.identifier}: ${m.content}`);
  }

  const health = await client.health();
  console.log(`Health: ${health.memory_count} memories, uptime ${health.uptime_s}s`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
