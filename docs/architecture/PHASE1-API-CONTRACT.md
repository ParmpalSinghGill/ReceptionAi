# Phase 1 API Contract (frozen for the walking skeleton)

The exact surface `apps/api` exposes and both frontends consume. Frozen so the
backend and frontend can be built in parallel without drift. A narrow slice of
06-API-FRONTEND.md — not a replacement for it.

---

## Auth (Phase 1 scope)

Two paths:

1. **Authenticated admin** — `Authorization: Bearer <jwt>` from `POST
   /api/v1/auth/login`. Tenant comes from the token's `tid` claim, never a
   header or path (contract §10).
2. **Public demo widget** — no login. `POST /api/v1/public/demo/sessions`
   returns a short-lived, single-use **stream ticket** scoped to one
   conversation. This is the mechanism from 04 §9.1 as corrected: browsers
   cannot set `Authorization` on `EventSource`, and the session token must
   never appear in a URL.

---

## Endpoints

### `POST /api/v1/public/demo/sessions`
Starts an anonymous demo conversation against the seeded demo tenant.

```jsonc
// → 201
{
  "conversation_id": "019fd1ef-...",   // UUIDv7
  "ticket": "dt_9f3a...",              // single-use, 20s TTL, one conversation
  "vertical": "medical",
  "greeting": "Thanks for calling Northgate Dental — how can I help?"
}
```
Rate limited per IP. No auth. Never returns tenant internals.

### `POST /api/v1/conversations/{id}/messages`
Send a user turn. Returns immediately; tokens arrive on the stream.

```jsonc
// ← body
{ "content": "I need to see someone about my tooth", "idempotency_key": "uuid" }
// → 202
{ "turn_id": "019fd1ef-...", "accepted_at": "2026-08-05T18:22:41.221Z" }
```

### `GET /api/v1/conversations/{id}/stream?ticket=<ticket>`
SSE. `ticket` for the demo widget; admin clients authenticate by cookie and
omit it.

```
event: token
data: {"turn_id":"...","delta":"Sorry you're"}

event: tool_call
data: {"turn_id":"...","tool":"check_availability","status":"running"}

event: turn_complete
data: {"turn_id":"...","finish_reason":"stop","latency_ms":842,
       "usage":{"prompt_tokens":612,"completion_tokens":48}}

event: error
data: {"code":"PROVIDER_UNAVAILABLE","message":"...","retryable":true}

: heartbeat
```

- Heartbeat comment every 15s so proxies don't reap the connection.
- `Last-Event-ID` resumes; every event carries a monotonic `id:`.
- **A 401 on reconnect means "re-mint the ticket", not "auth failed"** — the
  ticket is single-use and `EventSource` reuses the original URL (04 §9.1).

### `GET /api/v1/conversations/{id}`
Full conversation with turns. Admin auth required.

### `GET /api/v1/me`
Current principal, permissions, tenant summary. Drives RBAC-conditional UI.

### `GET /healthz` · `GET /readyz`
Liveness / readiness. `readyz` checks DB and Redis.

---

## Errors — RFC 9457, always

```jsonc
{
  "type": "https://receptionai.dev/problems/tenant-quota-exceeded",
  "title": "Monthly conversation quota exceeded",
  "status": 429,
  "code": "TENANT_QUOTA_EXCEEDED",     // the frozen vocabulary (amendment A9)
  "detail": "...",
  "instance": "/api/v1/conversations",
  "request_id": "01JQ..."
}
```
`Content-Type: application/problem+json`. Middleware denials use this shape too.

---

## Conventions

- All ids UUIDv7 strings. All timestamps RFC 3339 UTC with `Z`.
- Money is a decimal **string** plus an ISO-4217 currency — never a float.
- Cursor pagination: `?limit=50&cursor=<opaque>` → `{"items":[...],
  "next_cursor":"..."|null}`. Offset paging is not offered; the conversation
  tables are partitioned and offset cannot prune.
- `POST` accepts `Idempotency-Key`.
- Every response carries `X-Request-Id`, echoed into logs and traces.

---

## Phase 1 non-goals

Bookings, knowledge upload, voice, billing, workflow editor. The skeleton
proves: tenant isolation → manifest-compiled graph → streaming reply.
