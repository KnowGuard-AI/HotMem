import type {
  AddOptions,
  AddResponse,
  HealthResponse,
  HotMemClientOptions,
  HydrateResponse,
  Memory,
  SearchOptions,
  SnapshotResponse,
} from "./types.js";
import { HotMemError } from "./types.js";

const DEFAULT_BASE_URL = "http://127.0.0.1:8711";
const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Typed TypeScript client for the HotMem daemon/sidecar HTTP API (#109, #110, #111).
 *
 * Uses the global `fetch` — zero dependencies. Works in Node.js 18+,
 * Deno, Bun, and edge runtimes (Cloudflare Workers).
 */
export class HotMemClient {
  readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly contextHeaders: Record<string, string>;

  constructor(
    baseUrl: string = DEFAULT_BASE_URL,
    optionsOrTimeout: number | HotMemClientOptions = DEFAULT_TIMEOUT_MS,
  ) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.contextHeaders = {};
    if (typeof optionsOrTimeout === "number") {
      this.timeoutMs = optionsOrTimeout;
    } else {
      this.timeoutMs = optionsOrTimeout.timeoutMs ?? DEFAULT_TIMEOUT_MS;
      if (optionsOrTimeout.token) {
        this.contextHeaders["authorization"] = `Bearer ${optionsOrTimeout.token}`;
      }
      if (optionsOrTimeout.expectedStoreId) {
        this.contextHeaders["x-hotmem-store-id"] = optionsOrTimeout.expectedStoreId;
      }
      if (optionsOrTimeout.sessionId) {
        this.contextHeaders["x-hotmem-session-id"] = optionsOrTimeout.sessionId;
      }
      if (optionsOrTimeout.source) {
        this.contextHeaders["x-hotmem-source"] = optionsOrTimeout.source;
      }
      if (optionsOrTimeout.namespace !== undefined && optionsOrTimeout.namespace !== "") {
        this.contextHeaders["x-hotmem-namespace"] = optionsOrTimeout.namespace;
      }
    }
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
    const extraHeaders: Record<string, string> | undefined = options.idempotencyKey
      ? { "x-hotmem-idempotency-key": options.idempotencyKey }
      : undefined;
    return this.request<AddResponse>("POST", "/v1/add", payload, extraHeaders);
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

  /** Fetch metadata for one memory by ID without reading backing files. */
  async getMemory(memoryId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      "GET",
      `/v1/memory/${encodeURIComponent(memoryId)}`,
    );
  }

  /** Trigger swap file hydration. */
  async hydrate(file?: string): Promise<HydrateResponse> {
    const payload: Record<string, unknown> = {};
    if (file !== undefined) {
      payload.file = file;
    }
    return this.request<HydrateResponse>("POST", "/v1/hydrate", payload);
  }

  /** Trigger database snapshot to swap file, v2 directory, or interchange package. */
  async snapshot(
    file?: string,
    options?: { package?: boolean; gz?: boolean; copyAttachments?: boolean },
  ): Promise<SnapshotResponse> {
    const payload: Record<string, unknown> = {};
    if (file !== undefined) {
      payload.file = file;
    }
    if (options?.package !== undefined) {
      payload.package = options.package;
    }
    if (options?.gz !== undefined) {
      payload.gz = options.gz;
    }
    if (options?.copyAttachments !== undefined) {
      payload.copy_attachments = options.copyAttachments;
    }
    return this.request<SnapshotResponse>("POST", "/v1/snapshot", payload);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: Record<string, unknown>,
    extraHeaders?: Record<string, string>,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      "content-type": "application/json",
      ...this.contextHeaders,
      ...(extraHeaders ?? {}),
    };
    const init: RequestInit = {
      method,
      headers,
      signal: AbortSignal.timeout(this.timeoutMs),
    };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }

    let resp: Response;
    try {
      resp = await fetch(url, init);
    } catch (err) {
      throw new HotMemError(
        `request failed: ${err instanceof Error ? err.message : String(err)}`,
        0,
        err,
      );
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
      let message = `HTTP ${resp.status}`;
      if (typeof json === "object" && json) {
        const obj = json as Record<string, unknown>;
        if ("detail" in obj) {
          message = String(obj.detail);
        } else if ("error" in obj && "message" in obj) {
          message = `${String(obj.error)}: ${String(obj.message)}`;
        } else if ("message" in obj) {
          message = String(obj.message);
        }
      }
      throw new HotMemError(message, resp.status, json);
    }

    return json as T;
  }
}
