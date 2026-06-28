/** Request/response types for the HotMem HTTP API. */

export interface AddOptions {
  source?: string;
  importance?: number;
  metadata?: Record<string, unknown>;
  ttlSeconds?: number;
}

export interface AddRequest {
  identifier: string;
  fact: string;
  source: string;
  importance: number;
  metadata: Record<string, unknown>;
  ttl_seconds?: number;
}

export interface AddResponse {
  memory_id: string;
  content_hash: string;
  trace_ms: number;
}

export interface SearchOptions {
  topK?: number;
  maxChars?: number;
}

export interface SearchRequest {
  query: string;
  top_k: number;
  max_chars?: number;
}

/** A ranked, LLM-ready message object returned by search. */
export interface Memory {
  role: string;
  content: string;
  memory_id: string;
  identifier: string;
  score: number;
}

export interface SearchResponse {
  memories: Memory[];
  count: number;
  trace_ms: number;
}

export interface HealthResponse {
  status: string;
  memory_count: number;
  db_path: string;
  uptime_s: number;
}

export interface HydrateRequest {
  file?: string;
}

export interface HydrateResponse {
  loaded: number;
  skipped_dupes: number;
}

export interface SnapshotResponse {
  exported: number;
  path: string;
}

export class HotMemError extends Error {
  status: number;
  body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = "HotMemError";
    this.status = status;
    this.body = body;
  }
}
