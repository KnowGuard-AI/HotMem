import type {
  AddOptions,
  AddResponse,
  HealthResponse,
  HydrateResponse,
  Memory,
  SearchOptions,
  SnapshotResponse,
} from "./types.js";
import { HotMemError } from "./types.js";

const DEFAULT_BASE_URL = "http://127.0.0.1:8711";
const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Typed TypeScript client for the HotMem HTTP API.
 *
 * Uses the global `fetch` — zero dependencies. Works in Node.js 18+,
 * Deno, Bun, and edge runtimes (Cloudflare Workers).
 *
 * @example
 * const client = new HotMemClient("http://127.0.0.1:8711");
 * await client.add("vendor_x", "Invoice total $5000", { importance: 0.8 });
 * const memories = await client.search("duplicate invoice risk", { topK: 5 });
 */
export class HotMemClient {
  readonly baseUrl: string;
  private readonly timeoutMs: number;

  constructor(baseUrl: string = DEFAULT_BASE_URL, timeoutMs: number = DEFAULT_TIMEOUT_MS) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeoutMs = timeoutMs;
  }

  /** Check server health. */
  async health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("GET", "/v1/health");
  }

  /**
   * Add a fact to memory.
   * @returns The stored memory id and content hash.
   */
  async add(
    identifier: string,
    fact: string,
    options: AddOptions = {},
  ): Promise<AddResponse> {
    const payload: Record<string, unknown> = {
      identifier,
      fact,
      source: options.source ?? "",
      importance: options.importance ?? 0.5,
      metadata: options.metadata ?? {},
    };
    if (options.ttlSeconds !== undefined) {
      payload.ttl_seconds = options.ttlSeconds;
    }
    return this.request<AddResponse>("POST", "/v1/add", payload);
  }

  /**
   * Search memories and return ranked, LLM-ready message objects.
   * @returns An array of memories sorted by relevance.
   */
  async search(query: string, options: SearchOptions = {}): Promise<Memory[]> {
    const payload: Record<string, unknown> = {
      query,
      top_k: options.topK ?? 5,
    };
    if (options.maxChars !== undefined) {
      payload.max_chars = options.maxChars;
    }
    const res = await this.request<{ memories: Memory[] }>("POST", "/v1/search", payload);
    return res.memories;
  }

  /** Trigger swap file hydration. */
  async hydrate(file?: string): Promise<HydrateResponse> {
    const payload: Record<string, unknown> = {};
    if (file !== undefined) {
      payload.file = file;
    }
    return this.request<HydrateResponse>("POST", "/v1/hydrate", payload);
  }

  /** Trigger database snapshot to swap file. */
  async snapshot(file?: string): Promise<SnapshotResponse> {
    const payload: Record<string, unknown> = {};
    if (file !== undefined) {
      payload.file = file;
    }
    return this.request<SnapshotResponse>("POST", "/v1/snapshot", payload);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: Record<string, unknown>,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const init: RequestInit = {
      method,
      headers: { "content-type": "application/json" },
      signal: AbortSignal.timeout(this.timeoutMs),
    };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }

    let resp: Response;
    try {
      resp = await fetch(url, init);
    } catch (err) {
      throw new HotMemError(`request failed: ${err instanceof Error ? err.message : String(err)}`, 0, err);
    }

    let json: unknown = undefined;
    const text = await resp.text();
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        json = text;
      }
    }

    if (!resp.ok) {
      const message = typeof json === "object" && json && "detail" in json
        ? String((json as Record<string, unknown>).detail)
        : `HTTP ${resp.status}`;
      throw new HotMemError(message, resp.status, json);
    }

    return json as T;
  }
}
