/** Request/response types for the HotMem HTTP API (#109, #110, #111). */

export interface HotMemClientOptions {
  timeoutMs?: number;
  token?: string;
  sessionId?: string;
  harnessId?: string;
  source?: string;
  namespace?: string;
  expectedStoreId?: string;
  envelopeVersion?: string;
}

export interface AddOptions {
  source?: string;
  importance?: number;
  metadata?: Record<string, unknown>;
  ttlSeconds?: number;
  tags?: string[];
  harnessId?: string;
  sessionId?: string;
  namespace?: string;
  envelopeVersion?: string;
  idempotencyKey?: string;
}

export interface AddRequest {
  identifier: string;
  fact: string;
  source: string;
  importance: number;
  metadata: Record<string, unknown>;
  ttl_seconds?: number;
  tags?: string[];
  harness_id?: string;
  session_id?: string;
  envelope_version?: string;
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
  created_at?: string;
  tags?: string[];
  provenance?: Record<string, unknown>;
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
  store_id?: string;
  store_state?: string;
  runtime_generation?: string;
  embedding?: { model: string; dim: number };
}

export interface HydrateRequest {
  file?: string;
}

export interface HydrateResponse {
  loaded: number;
  skipped_dupes: number;
  /** Records that parsed but failed validation and were not inserted (#67). */
  invalid?: number;
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
