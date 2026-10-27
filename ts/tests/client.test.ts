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

  it("propagates daemon auth, store, session, harness, source, namespace, and idempotency headers (#109, #111)", async () => {
    const calls = mockFetch(() => ({
      status: 200,
      body: { memory_id: "m-daemon", content_hash: "h-daemon", trace_ms: 0.4 },
    }));
    const c = new HotMemClient("http://127.0.0.1:8711", {
      token: "tok-a",
      expectedStoreId: "store-incident-01",
      sessionId: "sess-a-1",
      harnessId: "codex",
      source: "agent://ts-sdk",
      namespace: "ops/shared",
      envelopeVersion: "hotmem-session-v1",
    });
    await c.add("ops/demo-17", "rollback decision", {
      idempotencyKey: "op-key-1",
      tags: ["model:gpt-5", "project:hotmem", "org:knowguard"],
    });
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers["authorization"]).toBe("Bearer tok-a");
    expect(headers["x-hotmem-store-id"]).toBe("store-incident-01");
    expect(headers["x-hotmem-session-id"]).toBe("sess-a-1");
    expect(headers["x-hotmem-harness-id"]).toBe("codex");
    expect(headers["x-hotmem-source"]).toBe("agent://ts-sdk");
    expect(headers["x-hotmem-namespace"]).toBe("ops/shared");
    expect(headers["x-hotmem-envelope-version"]).toBe("hotmem-session-v1");
    expect(headers["x-hotmem-idempotency-key"]).toBe("op-key-1");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.tags).toEqual(["model:gpt-5", "project:hotmem", "org:knowguard"]);
  });

  it("supports per-write model tag changes, empty tag lists, and per-call context overrides (#111)", async () => {
    const calls = mockFetch(() => ({
      status: 200,
      body: { memory_id: "m-step", content_hash: "h-step", trace_ms: 0.3 },
    }));
    const c = new HotMemClient("http://127.0.0.1:8711", {
      token: "tok-a",
      sessionId: "sess-ts-1",
      harnessId: "claude-code",
      source: "agent://claude",
    });
    await c.add("ops/step", "first write", {
      tags: ["model:claude-sonnet-4", "project:hotmem"],
    });
    await c.add("ops/step", "second write with changed model and override context", {
      tags: ["model:claude-opus-4", "project:hotmem"],
      harnessId: "claude-desktop",
      sessionId: "sess-ts-2",
      source: "agent://claude-desktop",
    });
    await c.add("ops/step", "third write with empty tags", {
      tags: [],
    });

    const firstBody = JSON.parse(calls[0].init.body as string);
    expect(firstBody.tags).toEqual(["model:claude-sonnet-4", "project:hotmem"]);
    const firstHeaders = calls[0].init.headers as Record<string, string>;
    expect(firstHeaders["x-hotmem-harness-id"]).toBe("claude-code");
    expect(firstHeaders["x-hotmem-session-id"]).toBe("sess-ts-1");

    const secondBody = JSON.parse(calls[1].init.body as string);
    expect(secondBody.tags).toEqual(["model:claude-opus-4", "project:hotmem"]);
    expect(secondBody.harness_id).toBe("claude-desktop");
    expect(secondBody.session_id).toBe("sess-ts-2");
    const secondHeaders = calls[1].init.headers as Record<string, string>;
    expect(secondHeaders["x-hotmem-harness-id"]).toBe("claude-desktop");
    expect(secondHeaders["x-hotmem-session-id"]).toBe("sess-ts-2");
    expect(secondHeaders["x-hotmem-source"]).toBe("agent://claude-desktop");

    const thirdBody = JSON.parse(calls[2].init.body as string);
    expect(thirdBody.tags).toEqual([]);
    const thirdHeaders = calls[2].init.headers as Record<string, string>;
    expect(thirdHeaders["x-hotmem-harness-id"]).toBe("claude-code");
    expect(thirdHeaders["x-hotmem-session-id"]).toBe("sess-ts-1");
  });

  it("formats structured daemon policy errors into HotMemError", async () => {
    mockFetch(() => ({
      status: 403,
      body: { error: "scope_denied", message: "caller cannot read namespace ops/private" },
    }));
    const c = new HotMemClient("http://127.0.0.1:8711", { token: "tok-b" });
    await expect(c.getMemory("priv-1")).rejects.toMatchObject({
      name: "HotMemError",
      status: 403,
      message: "scope_denied: caller cannot read namespace ops/private",
    });
  });
});
