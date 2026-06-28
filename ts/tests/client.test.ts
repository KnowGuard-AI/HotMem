import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HotMemClient, HotMemError } from "../src/index.js";

type FetchCall = { url: string; init: RequestInit };

function mockFetch(handler: (call: FetchCall) => { status: number; body: unknown }) {
  const calls: FetchCall[] = [];
  const fn = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    const { status, body } = handler({ url, init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  });
  globalThis.fetch = fn as unknown as typeof fetch;
  return calls;
}

describe("HotMemClient", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("strips trailing slashes from base url", () => {
    const c = new HotMemClient("http://localhost:8711///");
    expect(c.baseUrl).toBe("http://localhost:8711");
  });

  it("health() GETs /v1/health", async () => {
    const calls = mockFetch(() => ({
      status: 200,
      body: { status: "ok", memory_count: 3, db_path: "/data/h.sqlite", uptime_s: 1.2 },
    }));
    const c = new HotMemClient();
    const res = await c.health();
    expect(res.status).toBe("ok");
    expect(res.memory_count).toBe(3);
    expect(calls[0].url).toBe("http://127.0.0.1:8711/v1/health");
    expect(calls[0].init.method).toBe("GET");
  });

  it("add() POSTs to /v1/add with defaults", async () => {
    const calls = mockFetch(() => ({
      status: 200,
      body: { memory_id: "m1", content_hash: "abc", trace_ms: 1.0 },
    }));
    const c = new HotMemClient();
    const res = await c.add("vendor_x", "Invoice total $5000", { importance: 0.8 });
    expect(res.memory_id).toBe("m1");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body).toEqual({
      identifier: "vendor_x",
      fact: "Invoice total $5000",
      source: "",
      importance: 0.8,
      metadata: {},
    });
  });

  it("add() includes ttl_seconds when provided", async () => {
    const calls = mockFetch(() => ({ status: 200, body: { memory_id: "m" } }));
    const c = new HotMemClient();
    await c.add("v", "f", { ttlSeconds: 3600 });
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.ttl_seconds).toBe(3600);
  });

  it("search() returns ranked memories", async () => {
    mockFetch(() => ({
      status: 200,
      body: {
        memories: [{ role: "system", content: "fact", memory_id: "m1", identifier: "v", score: 0.9 }],
        count: 1,
        trace_ms: 0.5,
      },
    }));
    const c = new HotMemClient();
    const mems = await c.search("risk", { topK: 5, maxChars: 100 });
    expect(mems).toHaveLength(1);
    expect(mems[0].role).toBe("system");
    expect(mems[0].score).toBe(0.9);
  });

  it("hydrate() and snapshot() pass optional file", async () => {
    const calls = mockFetch(({ url }) => {
      if (url.endsWith("/v1/hydrate")) {
        return { status: 200, body: { loaded: 1, skipped_dupes: 0 } };
      }
      return { status: 200, body: { exported: 1, path: "swap.jsonl" } };
    });
    const c = new HotMemClient();
    expect(await c.hydrate("swap.jsonl")).toEqual({ loaded: 1, skipped_dupes: 0 });
    expect(await c.snapshot("swap.jsonl")).toEqual({ exported: 1, path: "swap.jsonl" });
    expect(JSON.parse(calls[0].init.body as string).file).toBe("swap.jsonl");
  });

  it("throws HotMemError on non-2xx with detail", async () => {
    mockFetch(() => ({ status: 400, body: { detail: "bad request" } }));
    const c = new HotMemClient();
    await expect(c.search("x")).rejects.toMatchObject({
      name: "HotMemError",
      status: 400,
      message: "bad request",
    });
  });

  it("throws HotMemError on network failure", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof fetch;
    const c = new HotMemClient();
    await expect(c.health()).rejects.toBeInstanceOf(HotMemError);
  });
});
