/**
 * Typed client for the Phase 1 public demo surface.
 * Contract: docs/architecture/PHASE1-API-CONTRACT.md
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** POST /api/v1/public/demo/sessions → 201 */
export interface DemoSession {
  conversation_id: string;
  /** Single-use, ~20s TTL, scoped to one conversation. */
  ticket: string;
  vertical: string;
  greeting: string;
}

/** POST /api/v1/conversations/{id}/messages → 202 */
export interface AcceptedTurn {
  turn_id: string;
  accepted_at: string;
}

/** RFC 9457 problem detail — the error shape for every failure. */
export interface ProblemDetail {
  type?: string;
  title: string;
  status: number;
  code?: string;
  detail?: string;
  instance?: string;
  request_id?: string;
}

/**
 * An API failure carrying the parsed problem document, so callers can branch
 * on `code` (the frozen vocabulary) rather than parsing message strings.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  readonly problem: ProblemDetail | undefined;

  constructor(message: string, status: number, problem?: ProblemDetail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = problem?.code;
    this.problem = problem;
  }
}

async function parseProblem(res: Response): Promise<ProblemDetail | undefined> {
  // Per the contract every error is application/problem+json, but a proxy or
  // gateway failure can still return HTML — so this must never throw.
  try {
    const text = await res.text();
    if (!text) return undefined;
    return JSON.parse(text) as ProblemDetail;
  } catch {
    return undefined;
  }
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
  } catch (cause) {
    // fetch only rejects on network-level failure — the API being down, DNS,
    // CORS. Surfaced as status 0 so the UI can say "can't reach" rather than
    // inventing an HTTP status.
    throw new ApiError(
      "Could not reach the ReceptionAi service.",
      0,
      undefined,
    );
  }

  if (!res.ok) {
    const problem = await parseProblem(res);
    throw new ApiError(
      problem?.detail ?? problem?.title ?? `Request failed (${res.status})`,
      res.status,
      problem,
    );
  }

  return (await res.json()) as T;
}

/**
 * Start an anonymous demo conversation. Rate limited per IP; no auth.
 *
 * `mode` tells the server which front door was used, so the conversation is
 * recorded as inbound voice rather than web chat. It defaults to chat, which
 * is what every existing caller of this function gets.
 */
export function createDemoSession(
  mode: "chat" | "voice" = "chat",
): Promise<DemoSession> {
  return request<DemoSession>("/api/v1/public/demo/sessions", {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
}

/**
 * Send a user turn. Returns immediately; tokens arrive on the stream.
 *
 * `sessionToken` is the conversation session minted by the stream — NOT the
 * ticket. The ticket is redeemed the moment the SSE stream opens, so by the
 * time a human has finished typing it is long spent; posting with it 401s.
 *
 * The credential travels in the `X-Conversation-Session` header rather than
 * the cookie the server also sets: that cookie is `SameSite=Lax`, which works
 * on a same-origin localhost test page and then silently fails once the widget
 * is embedded cross-origin on a customer's site — which is its entire purpose.
 * A header is origin-independent, so it behaves identically in both places.
 */
export function sendMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
  sessionToken: string,
): Promise<AcceptedTurn> {
  return request<AcceptedTurn>(
    `/api/v1/conversations/${conversationId}/messages`,
    {
      method: "POST",
      // POST accepts Idempotency-Key (contract §Conventions); the body key is
      // sent too because the contract's example body includes it.
      headers: {
        "Idempotency-Key": idempotencyKey,
        "X-Conversation-Session": sessionToken,
      },
      body: JSON.stringify({ content, idempotency_key: idempotencyKey }),
    },
  );
}

/**
 * URL for the SSE stream.
 *
 * Two credentials open a stream, and which one you hold decides the shape:
 *  - `{ ticket }`  — the first connection. Opening REDEEMS the ticket and the
 *    server answers with a `session` event carrying the longer-lived
 *    credential.
 *  - `{ session }` — every reconnect after that. The session is renewed on use
 *    (30-minute TTL), so no re-mint round-trip is needed.
 *
 * `EventSource` cannot set headers, which is why the session appears in the
 * query string here while message POSTs use the header. That is the same
 * constraint that made the ticket a query parameter in the first place.
 */
export function streamUrl(
  conversationId: string,
  credential: { ticket: string } | { session: string },
): string {
  const base = `${API_BASE}/api/v1/conversations/${conversationId}/stream`;
  const param =
    "ticket" in credential
      ? `ticket=${encodeURIComponent(credential.ticket)}`
      : `session=${encodeURIComponent(credential.session)}`;
  return `${base}?${param}`;
}

/* ----------------------------------------------------------------- events -- */

/**
 * The FIRST frame on every stream, before any token.
 *
 * Opening the stream exchanges the single-use ticket for this conversation
 * session: 30-minute TTL, renewed on each use, scoped to one conversation.
 * Every subsequent message POST must carry it.
 */
export interface SessionEvent {
  session: string;
  expires_in: number;
  conversation_id: string;
}

export interface TokenEvent {
  turn_id: string;
  delta: string;
}

export interface ToolCallEvent {
  turn_id: string;
  tool: string;
  status: string;
}

export interface TurnCompleteEvent {
  turn_id: string;
  finish_reason: string;
  latency_ms?: number;
  usage?: { prompt_tokens: number; completion_tokens: number };
}

export interface StreamErrorEvent {
  code: string;
  message: string;
  retryable: boolean;
}

/** Browser-safe UUID, for idempotency keys. */
export function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}
