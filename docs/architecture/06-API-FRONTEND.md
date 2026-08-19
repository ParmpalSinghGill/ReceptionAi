# 06 — API & Frontend

> Conforms to `00-CORE-CONTRACT.md` (including all seven §11 amendments).
> Consumes: `01-DATA-MODEL.md` (tables, RLS, repositories), `02-PLUGIN-CONNECTOR-SDK.md`
> §7 (the render document), `03-AI-ENGINE.md` (streaming internals),
> `04-VOICE-CHANNELS.md` (channel adapters, Redis Streams), `05-PLATFORM-SECURITY-OPS.md`
> Part A (auth, RBAC, rate limits).
>
> This document owns two things and nothing else: **the HTTP/SSE/WS surface**
> that sits on top of those subsystems, and **`apps/web`**, the Next.js admin
> portal. It does not redesign anything decided upstream.

**Status:** Complete, reconciled.

---

## Table of Contents

**Part A — REST API**
1. [Resource model & route table](#1-resource-model--route-table)
2. [Conventions](#2-conventions)
3. [Streaming: SSE and WebSocket](#3-streaming-sse-and-websocket)
4. [OpenAPI 3.1 generation and drift control](#4-openapi-31-generation-and-drift-control)
5. [Two routers in full: services and bookings](#5-two-routers-in-full-services-and-bookings)
6. [Outbound webhooks](#6-outbound-webhooks)

**Part B — Admin Portal**
7. [App Router structure](#7-app-router-structure)
8. [Auth integration and RBAC-driven UI](#8-auth-integration-and-rbac-driven-ui)
9. [The manifest-driven form renderer](#9-the-manifest-driven-form-renderer)
10. [Page inventory](#10-page-inventory)
11. [Data layer](#11-data-layer)
12. [Charts](#12-charts)
13. [Real-time UI](#13-real-time-ui)

**Part C**
14. [Design system & accessibility](#14-design-system--accessibility)
15. [Frontend testing](#15-frontend-testing)
16. [Inconsistencies found in upstream documents](#16-inconsistencies-found-in-upstream-documents)

---

## 0. What this document is defending

Three claims, and everything below exists to make one of them true.

**1. The tenant is never addressable.** Contract §10 says the tenant is derived
from auth context, never a path or query parameter. That is not a convention
here — it is structural. `TenantContext` (01 §15.1) has no constructor path
from a request parameter, `TenantContextMiddleware` (05 §A3.3) hard-rejects
`X-Tenant-Id` with `400 tenant_header_not_permitted`, and the React Query cache
key derivation in §11.2 makes a cross-tenant cache hit impossible rather than
unlikely. A cache leak across tenants is a security bug, not a rendering bug.

**2. A new vertical needs no new screens.** 02 §7 compiles a manifest into a
`RenderDocument`; §9 below is the renderer that consumes it. The test is
mechanical: adding the restaurant vertical (roadmap Phase 4) must not touch
`apps/web`. Every place the portal could have grown vertical-specific knowledge
— field types, validation, navigation, vocabulary — is a place this document
routes back through the manifest instead.

**3. The generated spec is the spec.** There is no hand-written OpenAPI file
that drifts from the code. §4 generates it from FastAPI + Pydantic v2 and gates
it in CI with `oasdiff` (05 §F23.8 already asserts this test exists), so a
breaking change to an integrator is a deliberate, reviewed act.

---

# PART A — REST API

## 1. Resource model & route table

### 1.1 Shape of the surface

Every route is `/api/v1/<plural-resource>` per contract §10. There are exactly
four kinds of route and no others:

| Kind | Pattern | Example |
|---|---|---|
| Collection | `GET`/`POST /api/v1/things` | `GET /api/v1/services` |
| Item | `GET`/`PATCH`/`DELETE /api/v1/things/{id}` | `PATCH /api/v1/services/{id}` |
| Sub-collection | `.../things/{id}/subthings` | `GET /api/v1/conversations/{id}/turns` |
| Action | `POST .../things/{id}/<verb>` | `POST /api/v1/bookings/{id}/cancel` |

**Judgment call — actions are RPC-shaped, deliberately.** `POST
/api/v1/bookings/{id}/cancel` is not REST-pure; the purist form is `PATCH
{status: "cancelled"}`. We use the verb form for state transitions that are not
mere field writes, because cancelling a booking runs a Temporal workflow
signal, applies a cancellation-window policy, may trigger a refund, and emits
`booking.appointment.cancelled`. Modelling that as a field assignment hides a
workflow behind a PATCH and makes the RBAC boundary wrong — `bookings:update`
and `bookings:cancel` are separate permissions in 05 §A2, and a single PATCH
route cannot cleanly require one or the other depending on the body. Verb
routes make the permission boundary match the route boundary.

`PATCH` is reserved for genuine field edits and always requires `If-Match`
(§2.6). There is no `PUT` anywhere in the API: every tenant-owned table has
columns the client does not own (`version`, `created_at`, trigger-maintained
`occupies`), so a full-replacement verb would be a lie.

### 1.2 Route table

Permissions are the `Perm` enum from `packages/core/authz/permissions.py`
(05 §A2). Every row carries one; routes with no permission do not exist —
`assert_all_routes_guarded` (05 §A2.4) fails startup otherwise. `EC` is the
rate-limit `EndpointClass` (05 §B8).

#### Auth & session — `PUBLIC_ROUTES`, no permission

These are fixed by 05 §A1 and reproduced here only for completeness. 06 adds
none of them except `/api/v1/auth/session`.

| Method | Path | EC | Notes |
|---|---|---|---|
| POST | `/api/v1/auth/login` | `auth` | → access token + refresh cookie |
| POST | `/api/v1/auth/refresh` | `auth` | rotation; reuse revokes family |
| POST | `/api/v1/auth/logout` | `auth` | revokes session |
| GET | `/api/v1/auth/oidc/{provider}/start` | `auth` | 302; `provider ∈ google, microsoft, github` |
| GET | `/api/v1/auth/oidc/{provider}/callback` | `auth` | 302 → `{web_origin}/auth/complete` |
| POST | `/api/v1/auth/password/forgot` | `auth` | |
| POST | `/api/v1/auth/password/reset` | `auth` | |
| GET | `/.well-known/jwks.json` | — | Ed25519 public keys |
| **GET** | **`/api/v1/auth/session`** | `read` | **new in 06.** Returns the hydrated principal for the portal shell: user, active tenant, roles, permission set, `mts` switchable tenants, `aal`. Guarded by `requires()` with no permission argument — see §2.9. |
| **POST** | **`/api/v1/auth/switch-tenant`** | `auth` | **new in 06.** Body `{tenant_id}`; `tenant_id` must be in the caller's `mts` claim. Mints a **new session** (01 §3.6: switching never mutates a session row). Returns a fresh access token + sets a fresh refresh cookie. |
| **POST** | **`/api/v1/auth/step-up`** | `auth` | **new in 06.** Completes an MFA challenge and returns an `aal=2` token so a `STEP_UP_REQUIRED` permission becomes reachable (05 §A1.6). |

#### Tenant & organisation

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/tenant` | `TENANT_READ` | `read` |
| PATCH | `/api/v1/tenant` | `TENANT_UPDATE` | `write` |
| DELETE | `/api/v1/tenant` | `TENANT_DELETE` ⚡ | `write` |
| GET | `/api/v1/locations` | `TENANT_READ` | `read` |
| POST | `/api/v1/locations` | `TENANT_UPDATE` | `write` |
| GET/PATCH/DELETE | `/api/v1/locations/{id}` | `TENANT_READ` / `TENANT_UPDATE` | `read`/`write` |
| GET | `/api/v1/settings` | `TENANT_READ` | `read` |
| PATCH | `/api/v1/settings` | `TENANT_UPDATE` | `write` |

⚡ = in `STEP_UP_REQUIRED`; needs `aal >= 2` and `auth_age <= 900s`.

`/api/v1/tenant` is **singular and id-less**. This is the one deliberate
violation of the plural-resource rule in contract §10, and it exists to make
the rule's intent visible: there is exactly one tenant reachable from a token,
so `/api/v1/tenants/{id}` would imply a choice the API does not offer.
`/api/v1/settings` maps to `tenant_settings` filtered to the caller's namespace
grants; it is a document-shaped resource, not a collection, because the portal
edits namespaces wholesale.

#### Users, roles, memberships

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/members` | `MEMBERS_READ` | `read` |
| POST | `/api/v1/members/invitations` | `MEMBERS_INVITE` | `write` |
| GET | `/api/v1/members/invitations` | `MEMBERS_READ` | `read` |
| DELETE | `/api/v1/members/invitations/{id}` | `MEMBERS_INVITE` | `write` |
| PATCH | `/api/v1/members/{user_id}` | `MEMBERS_UPDATE_ROLE` ⚡ | `write` |
| DELETE | `/api/v1/members/{user_id}` | `MEMBERS_REMOVE` | `write` |
| GET | `/api/v1/roles` | `MEMBERS_READ` | `read` |
| POST | `/api/v1/roles` | `ROLES_MANAGE` ⚡ | `write` |
| PATCH/DELETE | `/api/v1/roles/{id}` | `ROLES_MANAGE` ⚡ | `write` |
| GET | `/api/v1/permissions` | `MEMBERS_READ` | `read` |

The resource is `members`, not `users`. `users` is a global table (01 §3.2)
with rows that outlive any single tenant membership; what a tenant administers
is a `user_tenant_roles` row. Naming the route `/api/v1/users` would invite a
handler that reads `users` directly and returns a user who is not a member.
`GET /api/v1/permissions` returns the platform catalogue so the role editor can
render checkboxes without shipping a hardcoded copy of `Perm`.

#### Verticals & manifests

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/verticals` | `AGENTS_READ` | `read` |
| GET | `/api/v1/verticals/installed` | `AGENTS_READ` | `read` |
| POST | `/api/v1/verticals/installed` | `AGENTS_PUBLISH` | `write` |
| GET | `/api/v1/verticals/installed/{id}` | `AGENTS_READ` | `read` |
| PATCH | `/api/v1/verticals/installed/{id}/overlay` | `AGENTS_UPDATE` | `write` |
| POST | `/api/v1/verticals/installed/{id}/validate` | `AGENTS_UPDATE` | `write` |
| POST | `/api/v1/verticals/installed/{id}/publish` | `AGENTS_PUBLISH` | `write` |
| GET | `/api/v1/verticals/installed/{id}/versions` | `AGENTS_READ` | `read` |
| **GET** | **`/api/v1/forms/{form_key}`** | `AGENTS_READ` | `read` |
| GET | `/api/v1/navigation` | `TENANT_READ` | `read` |

`GET /api/v1/forms/{form_key}` is defined in 02 §7.1 and is the single most
load-bearing route in this document — §9 is its client. `GET
/api/v1/navigation` returns the compiled `UiSchema.admin_nav` (02 §2) with
items the caller lacks permission for already removed server-side; see §8.4 for
why filtering happens on the server.

`POST .../publish` runs the tenant smoke-eval suite before going live (03
§12.3) and returns `422` with a per-case diff when a case fails. That is the
"failing cases block the publish with a diff shown in the admin UI" surface 03
asserts exists but never specifies:

```jsonc
// 422 problem+json, extended
{
  "type": "https://errors.receptionai.com/manifest-eval-failed",
  "title": "Manifest evaluation failed",
  "status": 422,
  "failed_cases": [
    { "case_key": "refuses_medical_advice",
      "judge": "no_hallucinated_facts",
      "score": 0.41, "threshold": 0.80,
      "baseline_score": 0.94,
      "diff": { "before": "I can't advise on dosage…", "after": "Take 400mg…" } }
  ]
}
```

#### Booking domain

| Method | Path | Perm | EC |
|---|---|---|---|
| GET/POST | `/api/v1/resource-types` | `BOOKINGS_READ` / `BOOKINGS_UPDATE` | `read`/`write` |
| GET/PATCH/DELETE | `/api/v1/resource-types/{id}` | ″ | ″ |
| GET/POST | `/api/v1/resources` | `BOOKINGS_READ` / `BOOKINGS_UPDATE` | `read`/`write` |
| GET/PATCH/DELETE | `/api/v1/resources/{id}` | ″ | ″ |
| GET/POST | `/api/v1/services` | `BOOKINGS_READ` / `BOOKINGS_UPDATE` | `read`/`write` |
| GET/PATCH/DELETE | `/api/v1/services/{id}` | ″ | ″ |
| GET/PUT | `/api/v1/services/{id}/requirements` | ″ | ″ |
| GET/POST | `/api/v1/availability-rules` | `BOOKINGS_READ` / `BOOKINGS_UPDATE` | `read`/`write` |
| GET/PATCH/DELETE | `/api/v1/availability-rules/{id}` | ″ | ″ |
| GET/POST | `/api/v1/availability-exceptions` | ″ | ″ |
| DELETE | `/api/v1/availability-exceptions/{id}` | `BOOKINGS_UPDATE` | `write` |
| GET/POST | `/api/v1/holidays` | ″ | ″ |
| DELETE | `/api/v1/holidays/{id}` | `BOOKINGS_UPDATE` | `write` |
| **POST** | **`/api/v1/availability/search`** | `BOOKINGS_READ` | `search` |
| GET | `/api/v1/bookings` | `BOOKINGS_READ` | `read` |
| POST | `/api/v1/bookings` | `BOOKINGS_CREATE` | `write` |
| GET | `/api/v1/bookings/{id}` | `BOOKINGS_READ` | `read` |
| PATCH | `/api/v1/bookings/{id}` | `BOOKINGS_UPDATE` | `write` |
| POST | `/api/v1/bookings/{id}/confirm` | `BOOKINGS_UPDATE` | `write` |
| POST | `/api/v1/bookings/{id}/reschedule` | `BOOKINGS_UPDATE` | `write` |
| POST | `/api/v1/bookings/{id}/cancel` | `BOOKINGS_CANCEL` | `write` |
| POST | `/api/v1/bookings/{id}/check-in` | `BOOKINGS_UPDATE` | `write` |
| POST | `/api/v1/bookings/{id}/no-show` | `BOOKINGS_UPDATE` | `write` |
| POST | `/api/v1/holds` | `BOOKINGS_CREATE` | `write` |
| DELETE | `/api/v1/holds/{id}` | `BOOKINGS_CREATE` | `write` |
| GET/POST | `/api/v1/waitlist-entries` | `BOOKINGS_READ` / `BOOKINGS_CREATE` | `read`/`write` |
| POST | `/api/v1/waitlist-entries/{id}/offer` | `BOOKINGS_UPDATE` | `write` |
| GET/POST/DELETE | `/api/v1/recurrence-rules[/{id}]` | `BOOKINGS_READ` / `BOOKINGS_UPDATE` | `read`/`write` |

**`POST /api/v1/availability/search`, not `GET`.** Availability search takes a
service, a date range, a party size, an optional resource filter, and an
optional `attribute_predicate` — a JSONB predicate document (01 §5.5). That
does not fit a query string without inventing an encoding, and encoding it as
JSON-in-a-query-param produces URLs that blow past proxy limits and land in
access logs carrying customer-adjacent data. `POST` for a read is a deliberate
trade: we lose HTTP caching (which we could not use anyway — availability is
uncacheable by construction) and gain a typed body. Rate-limit class is
`search` (cost 2), not `read`.

`PUT /api/v1/services/{id}/requirements` is the one `PUT`-shaped route: the
requirement set for a service genuinely is replace-all, because a partial edit
of "what this service needs" has no coherent meaning when quantities must sum
correctly. It is spelled `PUT` on a sub-collection, never on an item.

**There is no `/api/v1/customers`.** 01 has no `customers` table; customer
identity is denormalized onto `bookings` (`customer_name`,
`customer_phone_e164`, `customer_email`) with `customer_id` as an optional,
non-FK link. See §16.1 — 04 §11.2 describes a `Customer` / `CustomerIdentity`
model that 01 does not implement, and 06 declines to expose an API for a table
that does not exist.

#### Conversations

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/conversations` | `CONVERSATIONS_READ` | `read` |
| GET | `/api/v1/conversations/{id}` | `CONVERSATIONS_READ` | `read` |
| GET | `/api/v1/conversations/{id}/turns` | `CONVERSATIONS_READ` | `read` |
| GET | `/api/v1/conversations/{id}/transcript` | `CONVERSATIONS_READ` | `read` |
| GET | `/api/v1/conversations/{id}/recording` | `CONVERSATIONS_READ` | `read` |
| GET | `/api/v1/conversations/{id}/outcomes` | `CONVERSATIONS_READ` | `read` |
| POST | `/api/v1/conversations/{id}/messages` | — (widget token) | `ai` |
| GET | `/api/v1/conversations/{id}/stream` | — (widget token) | `read` |
| DELETE | `/api/v1/conversations/{id}` | `CONVERSATIONS_DELETE` | `write` |
| POST | `/api/v1/conversations/export` | `CONVERSATIONS_EXPORT` ⚡ | `export` |
| GET | `/api/v1/conversations/live` | `CONVERSATIONS_READ` | `read` |
| GET | `/api/v1/conversations/{id}/monitor` (SSE) | `CONVERSATIONS_READ` | `read` |
| POST | `/api/v1/conversations/{id}/takeover` | `CONVERSATIONS_BARGE_IN` | `write` |
| POST | `/api/v1/conversations/{id}/whisper` | `CONVERSATIONS_BARGE_IN` | `write` |
| POST | `/api/v1/conversations/{id}/transfer` | `CONVERSATIONS_BARGE_IN` | `write` |

`GET .../recording` returns `302` to a short-TTL presigned URL rather than
proxying bytes. 01 §6.4 says `storage_uri` is "never presigned; minted at read
time with short TTL" — that minting happens here. The redirect target carries a
120-second expiry and the response sets `Cache-Control: private, no-store` so
the redirect itself is never cached. Serving audio through FastAPI would tie an
async worker to a multi-megabyte transfer for the length of a download; the
`StoragePort` signs and the object store serves.

`GET .../turns` is the only place `CONVERSATIONS_READ_PII` changes the response
body rather than the route's reachability: without it, `turns.content` and
`transcripts.full_text` come back masked per 05 §B6, and the response carries
`"pii_masked": true` so the UI can show a badge rather than silently displaying
`[REDACTED]` as if it were the text.

#### Knowledge

| Method | Path | Perm | EC |
|---|---|---|---|
| GET/POST | `/api/v1/knowledge/sources` | `KNOWLEDGE_READ` / `KNOWLEDGE_WRITE` | `read`/`write` |
| GET/PATCH/DELETE | `/api/v1/knowledge/sources/{id}` | ″ | ″ |
| POST | `/api/v1/knowledge/sources/{id}/sync` | `KNOWLEDGE_WRITE` | `ingest` |
| GET | `/api/v1/knowledge/documents` | `KNOWLEDGE_READ` | `read` |
| GET | `/api/v1/knowledge/documents/{id}` | `KNOWLEDGE_READ` | `read` |
| PATCH | `/api/v1/knowledge/documents/{id}` | `KNOWLEDGE_WRITE` | `write` |
| GET | `/api/v1/knowledge/documents/{id}/chunks` | `KNOWLEDGE_READ` | `read` |
| POST | `/api/v1/knowledge/uploads` | `KNOWLEDGE_WRITE` | `ingest` |
| GET | `/api/v1/knowledge/jobs` | `KNOWLEDGE_READ` | `read` |
| GET | `/api/v1/knowledge/jobs/{id}` | `KNOWLEDGE_READ` | `read` |
| GET | `/api/v1/knowledge/jobs/{id}/events` (SSE) | `KNOWLEDGE_READ` | `read` |
| POST | `/api/v1/knowledge/jobs/{id}/cancel` | `KNOWLEDGE_WRITE` | `write` |
| POST | `/api/v1/knowledge/search` | `KNOWLEDGE_READ` | `search` |
| GET | `/api/v1/knowledge/review-queue` | `KNOWLEDGE_READ` | `read` |
| POST | `/api/v1/knowledge/review-queue/{chunk_id}/resolve` | `KNOWLEDGE_PUBLISH` | `write` |

`POST /api/v1/knowledge/uploads` is a **two-phase** upload. Phase one posts
metadata and receives a presigned `PUT` URL plus an `upload_id`; the browser
uploads directly to object storage; phase two is implicit — the storage
notification enqueues the ingestion job. Routing a 25 MB PDF (the
`FieldConstraints.max_file_size_bytes` ceiling in 02) through the API process
would occupy an async worker for the whole transfer and put tenant documents in
the API pod's memory for no benefit.

```jsonc
// POST /api/v1/knowledge/uploads → 201
{
  "upload_id": "0192f4c3-...",
  "source_id": "0192f4c1-...",
  "url": "https://s3.../tenants/0192.../uploads/0192f4c3?X-Amz-Signature=...",
  "method": "PUT",
  "headers": { "content-type": "application/pdf",
               "x-amz-server-side-encryption": "aws:kms" },
  "expires_at": "2026-08-05T12:41:00Z",
  "max_bytes": 26214400
}
```

`review-queue` is the surface for 03 §2342's flagged-chunk review — content
screening surfaces suspect chunks "to the tenant admin for review rather than
blocking", and this is where that review happens.

#### Connectors, channels, secrets

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/connectors/catalogue` | `CONNECTORS_READ` | `read` |
| GET/POST | `/api/v1/connectors` | `CONNECTORS_READ` / `CONNECTORS_MANAGE` | `read`/`write` |
| GET/PATCH/DELETE | `/api/v1/connectors/{id}` | ″ | ″ |
| POST | `/api/v1/connectors/{id}/test` | `CONNECTORS_MANAGE` | `write` |
| GET | `/api/v1/connectors/{id}/oauth/start` | `CONNECTORS_MANAGE` | `write` |
| GET | `/api/v1/connectors/{id}/oauth/callback` | `CONNECTORS_MANAGE` | `write` |
| GET | `/api/v1/connectors/{id}/sync-state` | `CONNECTORS_READ` | `read` |
| POST | `/api/v1/connectors/{id}/resync` | `CONNECTORS_MANAGE` | `write` |
| GET/POST/DELETE | `/api/v1/channels[/{id}]` | `CHANNELS_READ` / `CHANNELS_MANAGE` | `read`/`write` |
| PUT | `/api/v1/secrets/{key}` | `SECRETS_WRITE` ⚡ | `write` |
| DELETE | `/api/v1/secrets/{key}` | `SECRETS_WRITE` ⚡ | `write` |
| GET | `/api/v1/secrets` | `CONNECTORS_READ` | `read` |

`GET /api/v1/secrets` lists **keys and metadata only** — never values. There is
deliberately no `secrets:read` permission in 05 §A2, and `PUT
/api/v1/secrets/{key}` is write-only with a `204` response carrying no body. A
secret that has been written can be replaced or deleted, never read back. The
portal renders a masked placeholder and a "replace" affordance, never a
populated input.

#### Workflows

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/workflows` | `AGENTS_READ` | `read` |
| GET | `/api/v1/workflows/{id}/versions` | `AGENTS_READ` | `read` |
| POST | `/api/v1/workflows/{id}/versions` | `AGENTS_PUBLISH` | `write` |
| GET | `/api/v1/workflow-runs` | `AGENTS_READ` | `read` |
| GET | `/api/v1/workflow-runs/{id}` | `AGENTS_READ` | `read` |
| POST | `/api/v1/workflow-runs/{id}/cancel` | `AGENTS_UPDATE` | `write` |
| POST | `/api/v1/workflow-runs/{id}/signal` | `AGENTS_UPDATE` | `write` |

`workflow_runs` is a projection of Temporal state (01 §9.3), so these routes
read Postgres and write to Temporal. Cancel and signal go to Temporal directly
with `workflow_id = {tenant_id}:{domain}:{entity_id}` (contract §3); the
projection catches up asynchronously, so the response is `202 Accepted` with
the run's current projected state, not `200` with a mutated one.

#### AI & voice configuration

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/ai/config` | `AGENTS_READ` | `read` |
| PATCH | `/api/v1/ai/config` | `AGENTS_UPDATE` | `write` |
| GET | `/api/v1/ai/agents` | `AGENTS_READ` | `read` |
| PATCH | `/api/v1/ai/agents/{agent_key}` | `AGENTS_UPDATE` | `write` |
| GET | `/api/v1/ai/model-classes` | `AGENTS_READ` | `read` |
| POST | `/api/v1/ai/playground` (SSE) | `AGENTS_UPDATE` | `ai` |
| GET | `/api/v1/ai/evals` | `AGENTS_READ` | `read` |
| POST | `/api/v1/ai/evals/run` | `AGENTS_UPDATE` | `ai` |
| GET | `/api/v1/voice/config` | `CHANNELS_READ` | `read` |
| PATCH | `/api/v1/voice/config` | `CHANNELS_MANAGE` | `write` |
| GET | `/api/v1/voice/voices` | `CHANNELS_READ` | `read` |
| POST | `/api/v1/voice/preview` | `CHANNELS_MANAGE` | `ai` |
| GET | `/api/v1/voice/phone-numbers` | `CHANNELS_READ` | `read` |

`GET /api/v1/ai/model-classes` returns the four classes from 03 §9.2 —
`router`, `fast`, `balanced`, `deep` — with the currently-bound provider/model
shown **read-only**. 03 is explicit that manifests declare a model class, never
a model name; the portal's "model" control therefore selects a class, and the
binding underneath is platform-operated. A dropdown of `claude-opus-4` /
`gpt-4o` in the tenant portal would be a second source of truth for something
03 deliberately made single-sourced.

`POST /api/v1/ai/playground` is the test console 03 asserts is needed but never
specifies. It runs a turn against a **draft** manifest overlay in a sandbox
graph with tools stubbed by default (`?tools=stub|live`), streams via SSE using
the same event vocabulary as §3.2, and never writes a `conversations` row.

#### Analytics

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/analytics/overview` | `USAGE_READ` | `read` |
| GET | `/api/v1/analytics/conversations` | `USAGE_READ` | `read` |
| GET | `/api/v1/analytics/bookings` | `USAGE_READ` | `read` |
| GET | `/api/v1/analytics/costs` | `USAGE_READ` | `read` |
| GET | `/api/v1/analytics/retrieval-quality` | `KNOWLEDGE_READ` | `read` |
| GET | `/api/v1/analytics/latency` | `USAGE_READ` | `read` |
| POST | `/api/v1/analytics/export` | `CONVERSATIONS_EXPORT` ⚡ | `export` |

Every analytics route takes `from`, `to`, `granularity ∈ hour|day|week|month`,
and optional `location_id`. All return the same envelope so one chart component
serves every endpoint:

```jsonc
{
  "series": [
    { "key": "booked",   "label": "Booked",   "unit": "count",
      "points": [{ "t": "2026-08-01T00:00:00Z", "v": 42 }] },
    { "key": "abandoned","label": "Abandoned","unit": "count",
      "points": [{ "t": "2026-08-01T00:00:00Z", "v": 7 }] }
  ],
  "totals": { "booked": 1204, "abandoned": 189 },
  "granularity": "day",
  "timezone": "Europe/London",
  "generated_at": "2026-08-05T12:00:00Z",
  "partial": false
}
```

`timezone` is the tenant/location IANA zone. Contract §6 forbids storing
business-local time, so bucketing to local days happens in the query
(`date_trunc('day', occurred_at AT TIME ZONE :tz)`) and the response tells the
client which zone it used — otherwise a browser in a different zone re-buckets
and produces a chart that disagrees with the same data on a colleague's screen.
`partial: true` marks a trailing bucket that is still filling, so the UI can
render it dashed rather than as a cliff.

#### Billing

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/billing/subscription` | `BILLING_READ` | `read` |
| GET | `/api/v1/billing/plans` | `BILLING_READ` | `read` |
| POST | `/api/v1/billing/checkout-session` | `BILLING_MANAGE` ⚡ | `write` |
| POST | `/api/v1/billing/portal-session` | `BILLING_MANAGE` ⚡ | `write` |
| GET | `/api/v1/billing/usage` | `USAGE_READ` | `read` |
| GET | `/api/v1/billing/invoices` | `BILLING_READ` | `read` |
| GET | `/api/v1/billing/entitlements` | `BILLING_READ` | `read` |

Card data never reaches our API: `checkout-session` and `portal-session` return
a Stripe-hosted URL the browser is redirected to. `entitlements` returns the
`entitlements_snapshot` from `subscriptions` (01 §2.6) — the copy-on-write
snapshot, not the live `plans.entitlements` — so a grandfathered tenant sees
what they actually bought.

#### API keys, audit, webhooks, DSR

| Method | Path | Perm | EC |
|---|---|---|---|
| GET | `/api/v1/api-keys` | `APIKEYS_READ` | `read` |
| POST | `/api/v1/api-keys` | `APIKEYS_MANAGE` ⚡ | `write` |
| DELETE | `/api/v1/api-keys/{id}` | `APIKEYS_MANAGE` ⚡ | `write` |
| POST | `/api/v1/audit/events` | `AUDIT_READ` | `read` |
| GET/POST | `/api/v1/webhook-subscriptions` | `WEBHOOKS_MANAGE` | `read`/`write` |
| PATCH/DELETE | `/api/v1/webhook-subscriptions/{id}` | `WEBHOOKS_MANAGE` | `write` |
| GET | `/api/v1/webhook-deliveries` | `WEBHOOKS_MANAGE` | `read` |
| POST | `/api/v1/webhook-deliveries/{id}/replay` | `WEBHOOKS_MANAGE` | `write` |
| GET/POST | `/api/v1/dsr-requests` | `DSR_MANAGE` ⚡ | `read`/`write` |

`POST /api/v1/audit/events` is a **POST that reads** — it is verbatim from 05
§B8's canonical example and kept unchanged. The audit filter grammar (actor,
action prefix, entity, severity, date range, PII-access-only) is too rich for a
query string and 05 already froze the shape. `POST /api/v1/api-keys` returns
the full key **exactly once**; every later read shows `key_prefix` only.

### 1.3 Route count and the default-deny gate

That is 138 routes. `assert_all_routes_guarded` (05 §A2.4) walks all of them at
startup, and `PUBLIC_ROUTES` gains exactly the entries in the auth block above.
Adding a route without `Depends(requires(...))` crashes the process before it
serves a request — which is the point: the most common authz bug is a missing
check on a *new* endpoint, and this document adds 138 of them.

---

## 2. Conventions

### 2.1 Versioning

Path-prefix `/api/v1`, per contract §10. There is no header negotiation and no
`?version=`.

**Within v1, only additive change is permitted.** Adding a response field,
adding an optional request field, adding an enum value to a *response*, adding
a route. Removing a field, tightening a type, adding a required request field,
or adding a value to a *request* enum a client must handle are all breaking and
require `/api/v2`. This is enforced mechanically, not by review — see §4.3.

**Judgment call — no sunset headers in v1.** RFC 8594 `Sunset`/`Deprecation`
headers are the right mechanism, but they are meaningless until there is a v2
to migrate to and a real deprecation policy behind them. Emitting them now
trains integrators to ignore them. When v2 exists, deprecated v1 routes gain
`Deprecation: <date>`, `Sunset: <date>`, and `Link: <...>; rel="successor-version"`,
and the OpenAPI operation gains `deprecated: true`.

### 2.2 Cursor pagination

**Cursor, not offset, and the partitioned tables are why.**

Five tables are partitioned monthly by `created_at` with PK `(tenant_id,
created_at, id)`: `conversations`, `turns`, `audit_log`, `usage_events`,
`ai_checkpoints` (01 §17.1). Three things follow.

1. **`OFFSET` does not prune partitions.** `ORDER BY created_at DESC OFFSET
   10000 LIMIT 50` must produce and discard 10,000 rows, and the planner cannot
   know which partitions those rows live in without scanning them. A keyset
   predicate `WHERE (created_at, id) < (:c, :i)` is a partition-pruning
   constraint: Postgres eliminates every partition whose range is entirely
   above the cursor before planning. On a tenant with two years of `turns`,
   this is the difference between touching one partition and touching
   twenty-four.
2. **Offset gives wrong answers on append-heavy tables.** 05 §B8's audit
   example already states this: rows landing mid-scan shift the window, so page
   2 duplicates rows from page 1 and skips others. `conversations`, `turns`,
   `audit_log` and `usage_events` are all append-heavy by definition.
3. **The sort key is already the PK.** `(created_at, id)` is a total order (ids
   are UUIDv7, unique within a millisecond by construction) and it is the
   leading edge of the primary key index. A keyset scan is an index-only range
   scan; there is no cheaper access path available.

Offset pagination survives in exactly one place: `TenantRepository.list()` (01
§15.4) keeps its `offset` parameter for non-partitioned reference tables where
a portal grid wants "jump to page 7" — `services`, `resources`, `roles`. Those
tables are small, bounded, and rarely appended to. **No route in §1.2 exposes
`offset`.** Where a grid needs page numbers it fetches the whole (bounded) set.

#### Cursor format

```python
# packages/core/api/pagination.py
from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Annotated, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")

_CURSOR_VERSION = 1


class CursorError(ValueError):
    """A cursor that did not decode. Always a 400, never a 500."""


def encode_cursor(created_at: datetime, entity_id: UUID, *, sort: str) -> str:
    """Opaque, URL-safe, and carrying its own sort key.

    `sort` is embedded so that a cursor taken from one ordering cannot be
    replayed against another. Without it, paging a list by `starts_at` and then
    switching the sort to `created_at` silently produces a keyset predicate
    against the wrong column and returns a plausible but wrong page — the worst
    failure mode available, because nothing errors.

    Base64url of compact JSON rather than a signed token: a cursor is not a
    capability. It carries no authority — RLS scopes the query regardless of
    what the cursor says, and a forged cursor can only move the caller's own
    window within their own tenant. Signing it would buy nothing and add a key
    to rotate.
    """
    payload = {
        "v": _CURSOR_VERSION,
        "t": created_at.astimezone(UTC).isoformat(),
        "i": str(entity_id),
        "s": sort,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, expect_sort: str) -> tuple[datetime, UUID]:
    padding = "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("malformed cursor") from exc

    if payload.get("v") != _CURSOR_VERSION:
        raise CursorError(f"unsupported cursor version {payload.get('v')!r}")
    if payload.get("s") != expect_sort:
        raise CursorError(
            f"cursor was issued for sort {payload.get('s')!r}, "
            f"but this request sorts by {expect_sort!r}"
        )
    try:
        return datetime.fromisoformat(payload["t"]), UUID(payload["i"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("malformed cursor payload") from exc


class CursorPage(BaseModel, Generic[T]):
    """The shape 05 §B8 references and never defines. Defined here."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False

    model_config = {"json_schema_extra": {"examples": [
        {"items": [], "next_cursor": None, "has_more": False}
    ]}}


Limit = Annotated[int, Field(default=50, ge=1, le=200)]


def page_from(
    rows: list[T],
    limit: int,
    *,
    sort: str,
    key: "Callable[[T], tuple[datetime, UUID]]",
) -> CursorPage[T]:
    """Build a page from `limit + 1` fetched rows.

    Fetching one extra row is how `has_more` is answered without a COUNT. A
    COUNT(*) over a partitioned append-heavy table is the single most expensive
    query the API could issue, it is wrong the moment it returns, and no UI
    element in `apps/web` displays a total row count — the grids show
    "Load more", never "page 3 of 91". See §11.4.
    """
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        created_at, entity_id = key(items[-1])
        next_cursor = encode_cursor(created_at, entity_id, sort=sort)
    return CursorPage[T](items=items, next_cursor=next_cursor, has_more=has_more)
```

The keyset predicate, applied in the repository:

```python
# packages/persistence/keyset.py
from sqlalchemy import Select, tuple_


def apply_keyset(
    stmt: Select,
    model,
    cursor: tuple[datetime, UUID] | None,
    *,
    descending: bool = True,
    ts_column: str = "created_at",
):
    """Row-value keyset predicate.

    `tuple_(a, b) < tuple_(x, y)` compiles to Postgres's row-value comparison
    `(a, b) < (x, y)`, which is a single index range scan against the composite
    PK. The hand-written equivalent — `a < x OR (a = x AND b < y)` — is an OR
    the planner frequently refuses to turn into one index scan, producing a
    BitmapOr over two scans instead. Same rows, materially worse plan, and the
    difference grows with partition count.
    """
    ts = getattr(model, ts_column)
    pk = model.id
    order = (ts.desc(), pk.desc()) if descending else (ts.asc(), pk.asc())
    if cursor is not None:
        anchor = tuple_(ts, pk)
        target = tuple_(*cursor)
        stmt = stmt.where(anchor < target if descending else anchor > target)
    return stmt.order_by(*order)
```

**Cursors have no TTL and are not invalidated by writes.** A cursor is a
position in a total order, not a snapshot. Rows inserted above the cursor
between page fetches are simply not seen — which is correct for a
reverse-chronological feed and is precisely the property offset lacks.

### 2.3 Filtering and sorting grammar

Deliberately small. Every filter is `?field=value` or `?field[op]=value`, and
the set of `(field, op)` pairs a route accepts is declared in its Pydantic
query model, so the OpenAPI spec enumerates them and an unknown parameter is a
`422` rather than a silently ignored filter.

| Form | Meaning | Example |
|---|---|---|
| `?f=v` | equals | `?status=confirmed` |
| `?f=v1,v2` | in | `?status=confirmed,pending` |
| `?f[gte]=v` | ≥ | `?starts_at[gte]=2026-08-01T00:00:00Z` |
| `?f[lte]=v` | ≤ | `?starts_at[lte]=2026-08-31T23:59:59Z` |
| `?f[not]=v` | ≠ | `?channel[not]=api` |
| `?q=text` | route-defined full-text search | `?q=root+canal` |
| `?sort=f` / `?sort=-f` | ascending / descending | `?sort=-created_at` |

**There is no generic `OR`, no nesting, and no expression language.** This
mirrors 02 §1.9's decision to give the manifest a closed predicate vocabulary
with "deliberately no `expr` escape hatch". A query DSL rich enough to express
arbitrary boolean logic is a query planner we did not write, an injection
surface, and a promise that every field is efficiently filterable — which is
false, because `01`'s indexes all lead with `tenant_id` and then a specific
column. Filters that the index catalogue (01 §12) cannot serve are not offered.
When a tenant needs a genuinely complex query, the answer is
`POST /api/v1/conversations/export` and their own tooling.

`?sort` accepts only fields the route declares. Sorting on an unindexed column
is a `422` naming the sortable fields, not a slow query.

```python
# apps/api/schemas/query.py
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field, field_validator


class BookingQuery(BaseModel):
    """Declared filters for GET /api/v1/bookings.

    Every field here appears in the OpenAPI spec as a named query parameter,
    which is the point: `?customer_ssn=...` is a 422 with a list of what IS
    accepted, not a filter that is quietly dropped.
    """

    status: str | None = None
    location_id: UUID | None = None
    service_id: UUID | None = None
    resource_id: UUID | None = None
    starts_at__gte: datetime | None = Field(default=None, alias="starts_at[gte]")
    starts_at__lte: datetime | None = Field(default=None, alias="starts_at[lte]")
    channel: str | None = None
    q: str | None = Field(default=None, max_length=120)
    sort: Literal["-created_at", "created_at", "-starts_at", "starts_at"] = "-starts_at"
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("status", "channel")
    @classmethod
    def _csv_is_bounded(cls, v: str | None) -> str | None:
        # An unbounded IN list is a way to make one request cost 5000 index
        # probes. Ten is more than any real UI filter uses.
        if v is not None and len(v.split(",")) > 10:
            raise ValueError("at most 10 comma-separated values")
        return v


BookingQueryDep = Annotated[BookingQuery, Query()]
```

`extra: "forbid"` is what turns a typo'd filter into a `422`. Without it,
`?statu=confirmed` returns every booking and looks like a data bug.

### 2.4 Sparse fieldsets

`?fields=id,reference,status,starts_at` on any item or collection route.
Omitted → the full representation.

```python
# apps/api/serialization/sparse.py
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

_ALWAYS = frozenset({"id"})


def project(model: BaseModel, fields: str | None) -> dict[str, Any] | BaseModel:
    """Apply ?fields= to an already-serialized, already-authorized model.

    Two properties this MUST have, in order:

    1. It runs AFTER the response model is built, never as a SELECT column
       list. Narrowing the SQL projection sounds like the optimization, but the
       response model is where PII masking (05 §B6) and permission-dependent
       field suppression live. A field the caller may not see must be absent
       because authorization removed it, not because they happened not to ask
       for it — and a `?fields=` that could pull a suppressed column back would
       be an authorization bypass with a query parameter.

    2. It is subtractive only. It can never surface a field the full
       representation would not have returned, because it operates on that
       representation's keys.
    """
    if fields is None:
        return model
    requested = {f.strip() for f in fields.split(",") if f.strip()}
    dumped = model.model_dump(mode="json")
    unknown = requested - set(dumped) - _ALWAYS
    if unknown:
        raise HTTPException(
            422,
            {"error": "unknown_fields", "unknown": sorted(unknown),
             "available": sorted(dumped)},
        )
    return {k: v for k, v in dumped.items() if k in requested | _ALWAYS}
```

`id` is always included: a client that projects it away receives objects it
cannot key, and every consumer immediately re-adds it.

Sparse fieldsets exist for two concrete cases in `apps/web`: the conversation
list, which does not need `turns` counts or cost columns, and the resource
picker in the booking form, which needs `id, name, resource_type_id` out of a
thirty-column row. Both are hot paths on tenant-facing screens.

### 2.5 Idempotency keys

05 defines idempotency for usage events and outbound provider calls but not for
inbound HTTP writes. 04 §10.5 states `Idempotency-Key` is "honored for 24 h" on
`POST /conversations/{id}/messages`. 06 generalises that to every unsafe method
and pins the semantics.

**Contract.** `Idempotency-Key` is **required** on `POST /api/v1/bookings`,
`POST /api/v1/holds`, `POST /api/v1/conversations/{id}/messages`, `POST
/api/v1/knowledge/uploads`, and `POST /api/v1/billing/checkout-session`;
**optional and honored** on every other `POST`/`PATCH`/`DELETE`. Retention is
24 hours. The key is scoped to `(tenant_id, method, path_template, key)` — the
same key on a different route is a different operation.

```python
# apps/api/middleware/idempotency.py
from __future__ import annotations

import hashlib
import json
from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from packages.core.api.problems import problem_response

_REQUIRED: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/v1/bookings"),
    ("POST", "/api/v1/holds"),
    ("POST", "/api/v1/conversations/{conversation_id}/messages"),
    ("POST", "/api/v1/knowledge/uploads"),
    ("POST", "/api/v1/billing/checkout-session"),
})
_TTL_SECONDS = 24 * 3600
_LOCK_TTL_SECONDS = 90


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Replay protection for unsafe methods.

    Ordering: runs AFTER TenantContextMiddleware, because the Redis key is
    tenant-scoped and there is no tenant before auth resolves. Runs BEFORE
    RateLimitMiddleware would be wrong — a client hammering retries should
    still be rate-limited — so the full order is:

        RequestId -> TenantContext -> RateLimit -> Idempotency -> route
    """

    def __init__(self, app, *, redis) -> None:
        super().__init__(app)
        self._redis = redis

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"POST", "PATCH", "DELETE"}:
            return await call_next(request)

        template = request.scope.get("route").path if request.scope.get("route") else request.url.path
        key = request.headers.get("Idempotency-Key")

        if key is None:
            if (request.method, template) in _REQUIRED:
                return problem_response(
                    request, status=400, code="idempotency_key_required",
                    title="Idempotency-Key header is required",
                    detail=f"{request.method} {template} requires an Idempotency-Key "
                           "header so a retried request cannot double-book.",
                )
            return await call_next(request)

        if len(key) > 255:
            return problem_response(
                request, status=400, code="idempotency_key_invalid",
                title="Idempotency-Key too long", detail="Maximum 255 characters.",
            )

        body = await request.body()
        fingerprint = hashlib.sha256(body).hexdigest()
        tenant_id = request.state.principal.tenant_id
        redis_key = f"idem:{tenant_id}:{request.method}:{template}:{key}"

        # SET NX is the whole concurrency story: exactly one concurrent request
        # with a given key proceeds; the rest see the sentinel and get 409.
        claimed = await self._redis.set(
            redis_key, json.dumps({"state": "in_flight", "fp": fingerprint}),
            nx=True, ex=_LOCK_TTL_SECONDS,
        )

        if not claimed:
            stored = json.loads(await self._redis.get(redis_key) or "{}")
            if stored.get("fp") != fingerprint:
                # Same key, different body. This is a client bug — reusing a
                # key for a different operation — and returning the FIRST
                # response would be actively wrong: the caller would believe
                # their second, different booking succeeded.
                return problem_response(
                    request, status=422, code="idempotency_key_reused",
                    title="Idempotency-Key reused with a different payload",
                    detail="This key was already used for a request with a "
                           "different body. Use a new key.",
                )
            if stored.get("state") == "in_flight":
                return problem_response(
                    request, status=409, code="idempotency_in_flight",
                    title="A request with this Idempotency-Key is in flight",
                    detail="Retry after the original request completes.",
                    headers={"Retry-After": "1"},
                )
            replay = Response(
                content=stored["body"],
                status_code=stored["status"],
                media_type=stored.get("media_type", "application/json"),
            )
            replay.headers["Idempotency-Replayed"] = "true"
            return replay

        response = await call_next(request)

        # Only terminal outcomes are cached. A 5xx must be retryable with the
        # same key — caching it would make a transient database blip permanent
        # for 24 hours, which is the opposite of what idempotency is for.
        if 200 <= response.status_code < 500 and response.status_code != 429:
            payload = b"".join([chunk async for chunk in response.body_iterator])
            await self._redis.set(
                redis_key,
                json.dumps({
                    "state": "done", "fp": fingerprint,
                    "status": response.status_code,
                    "body": payload.decode(),
                    "media_type": response.media_type or "application/json",
                }),
                ex=_TTL_SECONDS,
            )
            response = Response(
                content=payload, status_code=response.status_code,
                headers=dict(response.headers), media_type=response.media_type,
            )
        else:
            await self._redis.delete(redis_key)
        return response
```

**Redis is the cache, the database is the truth.** `bookings` already has
`uq_bookings__tenant_idempotency` (01 §5.9) — a unique index on `(tenant_id,
idempotency_key) WHERE idempotency_key IS NOT NULL`. The service layer writes
the header value into that column. If Redis is flushed or evicts the key, the
retry reaches the database, hits `23505`, and the repository translates it to
`ResourceConflictError(kind="duplicate")`; the booking service catches that
specific case and returns the existing booking with `200` instead of `201`.
Redis fails open into a slower correct path, never into a double booking.

### 2.6 Optimistic concurrency

Every table 01 gives a `version` column — `tenants`, `locations`,
`tenant_settings`, `roles`, `resource_types`, `resources`, `services`,
`availability_rules`, `bookings`, `subscriptions`, `tenant_connectors`,
`tenant_vertical_installs`, `users` — is exposed with an `ETag`, and `PATCH`
against it **requires** `If-Match`.

```python
# apps/api/deps/concurrency.py
from typing import Annotated

from fastapi import Depends, Header

from packages.core.api.problems import ProblemException


def etag_for(version: int) -> str:
    """Strong ETag. The version column is exact and monotonic, so there is no
    reason to use a weak validator — `W/` would tell caches that two
    representations are merely equivalent, when in fact identical versions ARE
    byte-identical representations."""
    return f'"{version}"'


async def if_match_version(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int:
    """Parse If-Match into an expected version, or refuse the write.

    Requiring the header rather than treating its absence as "force" is the
    entire point. A last-write-wins PATCH on `services` silently reverts a
    colleague's price change; on `bookings` it silently reverts a reschedule.
    Both are invisible until a customer complains. 428 is the correct status
    (RFC 6585): the request is not malformed, it is unconditional where a
    condition is mandatory.
    """
    if if_match is None:
        raise ProblemException(
            status=428, code="precondition_required",
            title="If-Match header is required",
            detail="Send If-Match with the ETag from the GET you edited, so a "
                   "concurrent change cannot be silently overwritten.",
        )
    if if_match.strip() == "*":
        raise ProblemException(
            status=428, code="precondition_required",
            title="If-Match: * is not accepted",
            detail="An explicit version is required; '*' would defeat the check.",
        )
    token = if_match.strip().lstrip("W/").strip('"')
    try:
        return int(token)
    except ValueError:
        raise ProblemException(
            status=400, code="invalid_if_match",
            title="Malformed If-Match", detail=f"Expected a version ETag, got {if_match!r}.",
        ) from None


ExpectedVersion = Annotated[int, Depends(if_match_version)]
```

`update_versioned(entity_id, expected_version, **values)` (01 §15.4) raises
`ConcurrencyConflictError`, which the exception handler maps to `412 Precondition
Failed` carrying the current representation, so the client can diff rather than
re-fetch:

```jsonc
{
  "type": "https://errors.receptionai.com/version-conflict",
  "title": "The record changed since you loaded it",
  "status": 412,
  "detail": "Expected version 7, current version is 9.",
  "expected_version": 7,
  "current_version": 9,
  "current": { "id": "0192...", "name": "Cleaning", "price": "95.0000", "version": 9 }
}
```

`*` is rejected rather than treated as force. There is no force-write header;
if a tenant genuinely wants to overwrite, they re-GET and re-PATCH, which is
one extra round trip and a decision made with the current value visible.

### 2.7 The error envelope

**RFC 9457 `application/problem+json`, and every error in the platform is
normalised into it — including the three shapes 05 currently emits
differently.** See §16.2: 05's middleware `_deny`, its `HTTPException` details,
and its 429 body are three different envelopes. This is the reconciliation.

```python
# packages/core/api/problems.py
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_BASE = "https://errors.receptionai.com"
MEDIA_TYPE = "application/problem+json"


class ProblemException(Exception):
    """The only exception the API raises for a client-visible error.

    Carrying `code` separately from `type` is deliberate. `type` is a URI a
    human follows to documentation; `code` is the stable snake_case string
    clients switch on, and it is exactly the vocabulary 05 §A3.4 froze
    (`invalid_token`, `token_expired`, `forbidden`, `rate_limited`, ...).
    Those codes are load-bearing — 05 says "clients switch on them" — so this
    document does not get to rename them into URIs.
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = headers or {}
        self.extensions = extensions
        super().__init__(title)


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{_BASE}/{code.replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "instance": str(request.url.path),
        "request_id": getattr(request.state, "request_id", None),
    }
    if detail is not None:
        body["detail"] = detail
    body.update({k: v for k, v in extensions.items() if v is not None})
    return JSONResponse(
        status_code=status, content=body, media_type=MEDIA_TYPE,
        headers=headers or {},
    )


def install_problem_handlers(app: FastAPI) -> None:
    """Register handlers so that NOTHING escapes as a bare FastAPI error.

    FastAPI's default wraps `HTTPException.detail` in `{"detail": ...}`. When
    `requires()` (05 §A2.4) raises `HTTPException(403, {"error": "forbidden",
    "missing_permissions": [...]})`, the wire result is
    `{"detail": {"error": "forbidden", ...}}` — a third envelope shape. The
    handler below unwraps that specific case so a 403 from RBAC and a 403 from
    anywhere else are byte-compatible.
    """

    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException) -> JSONResponse:
        return problem_response(
            request, status=exc.status, code=exc.code, title=exc.title,
            detail=exc.detail, headers=exc.headers, **exc.extensions,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            code = detail.pop("error")
            return problem_response(
                request, status=exc.status_code, code=code,
                title=_TITLES.get(code, code.replace("_", " ").capitalize()),
                headers=dict(exc.headers or {}), **detail,
            )
        return problem_response(
            request, status=exc.status_code, code=_STATUS_CODES.get(exc.status_code, "error"),
            title=str(detail), headers=dict(exc.headers or {}),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request, status=422, code="validation_failed",
            title="Request validation failed",
            detail="One or more fields are invalid.",
            errors=[
                {
                    "field": ".".join(str(p) for p in e["loc"][1:]),
                    "code": e["type"],
                    "message": e["msg"],
                }
                for e in exc.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 05 §A3.4 asserts error bodies never contain "traceback" or
        # "sqlalchemy" (tests/contract/test_openapi_conformance.py). This
        # handler is why that holds: the exception is logged with its trace and
        # the response carries only a correlation id.
        request.app.state.logger.exception(
            "unhandled", extra={"request_id": getattr(request.state, "request_id", None)}
        )
        return problem_response(
            request, status=500, code="internal_error",
            title="Internal server error",
            detail="The request could not be completed. Quote the request_id "
                   "when contacting support.",
        )


_TITLES = {
    "missing_credentials": "Authentication required",
    "invalid_token": "Invalid credentials",
    "token_expired": "Access token expired",
    "token_revoked": "Session revoked",
    "token_stale": "Permissions changed; re-authenticate",
    "unknown_tenant": "Unknown tenant",
    "tenant_suspended": "Tenant suspended",
    "tenant_deleted": "Tenant deleted",
    "tenant_header_not_permitted": "Tenant headers are not accepted",
    "forbidden": "Insufficient permissions",
    "step_up_required": "Additional authentication required",
    "reauthentication_required": "Re-authentication required",
    "rate_limited": "Rate limit exceeded",
    "authentication_unavailable": "Authentication temporarily unavailable",
    "slot_unavailable": "The requested time is no longer available",
    "version_conflict": "The record changed since you loaded it",
    "precondition_required": "If-Match header is required",
}
_STATUS_CODES = {400: "bad_request", 401: "unauthenticated", 403: "forbidden",
                 404: "not_found", 405: "method_not_allowed", 409: "conflict",
                 410: "gone", 415: "unsupported_media_type", 500: "internal_error"}
```

A worked example, the one every booking client must handle:

```jsonc
// 409, POST /api/v1/bookings
{
  "type": "https://errors.receptionai.com/slot-unavailable",
  "title": "The requested time is no longer available",
  "status": 409,
  "code": "slot_unavailable",
  "detail": "Dr. Chen is already booked from 14:00 to 14:30 on 2026-08-12.",
  "instance": "/api/v1/bookings",
  "request_id": "0192f4c3-8a1e-7c2d-9f01-3b6e5d4a7c88",
  "conflict_kind": "overlap",
  "resource_id": "0192f3a0-...",
  "conflicting_range": ["2026-08-12T14:00:00Z", "2026-08-12T14:30:00Z"],
  "alternatives": [
    { "starts_at": "2026-08-12T14:30:00Z", "ends_at": "2026-08-12T15:00:00Z",
      "resource_ids": ["0192f3a0-..."] },
    { "starts_at": "2026-08-12T15:00:00Z", "ends_at": "2026-08-12T15:30:00Z",
      "resource_ids": ["0192f3a0-..."] }
  ]
}
```

`alternatives` is the difference between an error and a recovery. The voice
agent that just lost a race can offer the next two slots in the same turn
instead of re-running availability search and adding a second of latency to a
conversation with a live human on the line.

**404 over 403 for cross-tenant ids** — 05 §A3.4 is explicit, and RLS makes it
automatic: a cross-tenant id returns zero rows, which is indistinguishable from
a nonexistent id. That is not a compromise; it is the elimination of an
enumeration oracle.

### 2.8 Rate-limit class declaration

05 §B8 says `classify_endpoint(request)` reads the class "from route metadata,
not a regex", but does not say how the metadata gets there. 06 defines it,
parallel to `__is_authz_check__`:

```python
# apps/api/ratelimit/declare.py
from typing import Callable, TypeVar

from apps.api.ratelimit.rules import EndpointClass

F = TypeVar("F", bound=Callable)


def endpoint_class(ec: EndpointClass) -> Callable[[F], F]:
    """Attach a rate-limit class to a route handler.

    Read by `classify_endpoint` off `route.endpoint`. A decorator rather than a
    dependency because the class must be resolvable BEFORE the dependency tree
    runs — RateLimitMiddleware sits above the router and never enters it.
    """
    def decorate(fn: F) -> F:
        fn.__endpoint_class__ = ec   # type: ignore[attr-defined]
        return fn
    return decorate


def classify_endpoint(request) -> EndpointClass:
    route = request.scope.get("route")
    declared = getattr(getattr(route, "endpoint", None), "__endpoint_class__", None)
    if declared is not None:
        return declared
    # Fallback by method, so a forgotten decorator is never unlimited.
    return EndpointClass.READ if request.method in {"GET", "HEAD"} else EndpointClass.WRITE
```

The fallback matters: a missing decorator degrades an AI endpoint to `WRITE`
(cost 1 instead of 5) rather than to unlimited. A CI check asserts every route
in `EndpointClass.AI | INGEST | SEARCH | EXPORT | VOICE` territory carries an
explicit declaration — those are the four classes where the fallback is
materially wrong.

### 2.9 Authenticated-but-unpermissioned routes

`GET /api/v1/auth/session` needs authentication but no specific permission —
every authenticated principal may read their own session. `requires()` with no
arguments satisfies `assert_all_routes_guarded` while asserting nothing:

```python
# apps/api/routers/session.py
@router.get("/api/v1/auth/session", response_model=SessionEnvelope)
@endpoint_class(EndpointClass.READ)
async def read_session(
    session: DbSession,
    principal: Principal = Depends(requires()),   # authenticated; no perm needed
) -> SessionEnvelope:
    """Hydrate the portal shell.

    Returns the principal plus the display data the shell needs, in one call —
    a portal that fetched tenant, memberships and permissions separately would
    render its navigation three times on every cold load.
    """
    tenants = TenantRepo(session)
    tenant = await tenants.get_current()
    return SessionEnvelope(
        user_id=principal.user_id,
        active_tenant_id=principal.tenant_id,
        tenant_name=tenant.display_name,
        plan=principal.tenant_plan,
        locale=tenant.default_locale,
        timezone=tenant.default_timezone,
        currency=tenant.default_currency,
        vertical_key=tenant.vertical_key,
        manifest_version=tenant.manifest_version,
        roles=list(principal.roles),
        permissions=sorted(principal.permissions),
        aal=principal.aal,
        # The `mts` claim, resolved to names for the tenant switcher. The claim
        # itself carries only ids, and a switcher showing raw UUIDs is useless.
        tenants=await tenants.list_memberships_for(principal.user_id),
        onboarding_complete=tenant.onboarding_complete,
    )
```

This is preferable to adding the route to `PUBLIC_ROUTES`, which would be a
lie: the route is not public, it just has no permission gate beyond identity.

### 2.10 Response headers, every response

| Header | Value |
|---|---|
| `X-Request-Id` | set by `RequestIdMiddleware` (05 §A3.3) |
| `RateLimit-Limit`, `RateLimit-Remaining` | 05 §B8 |
| `ETag` | on item `GET`s of versioned resources |
| `Cache-Control` | `private, no-store` on everything tenant-scoped |
| `Vary` | `Authorization` |

`private, no-store` on every tenant response is not caution, it is correctness:
a shared cache keyed on URL would serve tenant A's `/api/v1/services` to tenant
B, because the URL carries no tenant (contract §10). `Vary: Authorization` is
the belt to that braces.

### 2.11 CSRF

05 leaves this open. The token model makes it nearly a non-issue and 06 closes
the remainder.

Access tokens live in JS memory and travel in `Authorization: Bearer`, so they
are not ambient credentials and classic CSRF cannot reach a guarded route. The
one ambient credential is the refresh cookie, which is `HttpOnly; Secure;
SameSite=Lax` and path-scoped to `/api/v1/auth`. `SameSite=Lax` already blocks
cross-site `POST`, and `/api/v1/auth/refresh` is a `POST`.

**06 adds a double-submit token to the refresh and logout endpoints anyway.**
On login, alongside the refresh cookie, the server sets a non-`HttpOnly`
`rai_csrf` cookie containing a random 32-byte value; `/api/v1/auth/refresh` and
`/api/v1/auth/logout` require a matching `X-CSRF-Token` header. The reason is
not `SameSite` distrust in principle — it is that `SameSite=Lax` has known
gaps: it is a browser-version-dependent default, some embedded webviews do not
enforce it, and a same-site subdomain XSS (`status.receptionai.com`) is
same-site for cookie purposes. A forced-refresh CSRF is low severity on its own
but it rotates the victim's refresh family, and reuse-detection then revokes
their session — a denial-of-service any web page could trigger. Twelve lines of
double-submit removes it.

---

## 3. Streaming: SSE and WebSocket

### 3.1 Which transport, where

| Surface | Transport | Why |
|---|---|---|
| Assistant token stream (widget, playground) | **SSE** | Contract §1: "REST + SSE for streaming". One-directional, resumable, survives proxies. |
| Web-chat live channel (widget) | **WebSocket** | The client must send while receiving — typing indicators, read receipts, barge-in — which 04 §9.1 names as the sole justification for WS. |
| Realtime voice media | **WebSocket** | `apps/media`, `ws /v1/media/{nonce}`. Owned by 04 §5; not re-specified here. |
| Portal live monitoring, ingestion progress, active-call indicator | **SSE** | Read-only fan-out to an admin. No client→server channel needed. |

**The default is SSE and WebSocket must justify itself**, per 04 §9.1. Web chat
is the one place it does.

### 3.2 SSE event vocabulary

04 fixed the framing mechanics — `id` is the Redis Stream entry id, `event` is
the entry's `type` field, `data` is the entry's `data` field, keepalive is
`ping` every 15 s, idle terminate at 900 s. 03 defines `StreamEvent` and
`_normalize` by name only. **06 defines the vocabulary and every payload.**

`data` is always a JSON object. Every event carries `seq` (monotonic within a
conversation) and `turn_id`.

| `event` | When | `data` |
|---|---|---|
| `turn.started` | assistant begins a turn | `{seq, turn_id, agent_key, role: "assistant"}` |
| `token` | text delta | `{seq, turn_id, text}` |
| `tool.started` | tool invocation begins | `{seq, turn_id, tool_call_id, tool_key, label}` |
| `tool.finished` | tool returns | `{seq, turn_id, tool_call_id, status: "ok"\|"error", duration_ms, summary?}` |
| `block` | a semantic block (04 §8.3) | `{seq, turn_id, block: {...}}` |
| `turn.finished` | turn complete | `{seq, turn_id, finish_reason, usage?}` |
| `handoff` | agent switch (multi-agent) | `{seq, turn_id, from_agent, to_agent, reason}` |
| `status` | lifecycle | `{seq, status: "thinking"\|"typing"\|"idle"}` |
| `error` | recoverable failure | `{seq, turn_id?, code, title, detail?, retryable}` |
| `ping` | keepalive, 15 s | `""` (04, verbatim) |

Three things this vocabulary deliberately does **not** carry.

**No `usage` on `token`.** 03's internal `StreamDelta` has a `usage` kind
carrying token counts, and `TurnCostEvent` (03 §9.5) carries `cost_usd`. Neither
reaches a widget. The widget's SSE stream is authenticated by a **widget token**
(04 §9.1) held by an end customer's browser — an anonymous member of the
public. Streaming per-turn cost to them leaks unit economics. `turn.finished`
carries `usage` **only** when the stream is authenticated by a staff principal
with `USAGE_READ`; the compiler that builds stream entries checks the audience
and omits the field otherwise.

**No raw tool arguments.** `tool.started` carries `tool_key` and a
human-readable `label` from the manifest, never `partial_args`. Tool arguments
contain whatever the model extracted from the conversation, which for a medical
intake is PHI, and they are internal-schema-shaped. The label is the
manifest's, so it is already tenant-authored and vertical-appropriate.

**No internal agent node names beyond `agent_key`.** `handoff` names manifest
agent keys, which are tenant-visible by construction (they authored them), not
LangGraph node ids.

The compiler:

```python
# packages/ai/streaming/events.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from packages.core.ports.llm import StreamDelta

Audience = Literal["widget", "staff"]


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """The wire event 03 §4.5 references and never defines.

    Sits between LangGraph's `astream_events` output and the Redis stream. Two
    responsibilities and no others: name the event, and decide what the
    audience may see.
    """

    type: str
    data: dict[str, Any]

    def to_stream_fields(self) -> dict[bytes, bytes]:
        """The exact XADD field layout 04 §9.1's SSE endpoint reads back:
        `fields[b"type"]` becomes the SSE `event:` and `fields[b"data"]`
        becomes `data:`."""
        return {
            b"type": self.type.encode(),
            b"data": json.dumps(self.data, separators=(",", ":")).encode(),
        }


def normalize(
    delta: StreamDelta,
    *,
    seq: int,
    turn_id: str,
    audience: Audience,
    tool_labels: dict[str, str],
) -> StreamEvent | None:
    """`_normalize` from 03 §4.5, specified.

    Returns None for deltas that have no wire representation — `tool_call_delta`
    carries `partial_args`, which never leaves the process (see above).
    """
    match delta.kind:
        case "text":
            return StreamEvent("token", {"seq": seq, "turn_id": turn_id,
                                         "text": delta.text or ""})
        case "tool_call_start":
            key = delta.tool_call.name if delta.tool_call else "unknown"
            return StreamEvent("tool.started", {
                "seq": seq, "turn_id": turn_id,
                "tool_call_id": delta.tool_call.id if delta.tool_call else None,
                "tool_key": key,
                "label": tool_labels.get(key, "Working…"),
            })
        case "tool_call_delta":
            return None                      # partial_args never goes on the wire
        case "tool_call_end":
            return StreamEvent("tool.finished", {
                "seq": seq, "turn_id": turn_id,
                "tool_call_id": delta.tool_call.id if delta.tool_call else None,
                "status": "ok",
            })
        case "usage":
            if audience != "staff":
                return None                  # unit economics are not public
            u = delta.usage
            return StreamEvent("turn.finished", {
                "seq": seq, "turn_id": turn_id, "finish_reason": "stop",
                "usage": {"input_tokens": u.input_tokens,
                          "output_tokens": u.output_tokens} if u else None,
            })
        case "done":
            return StreamEvent("turn.finished", {
                "seq": seq, "turn_id": turn_id, "finish_reason": "stop"})
    return None
```

### 3.3 Resume semantics

04's endpoint already implements `Last-Event-ID` against the Redis Stream
`conv:{tenant_id}:{conversation_id}` with `MAXLEN ~ 1000`. Four consequences
the client must handle, none of which 04 states:

1. **Duplicate delivery is possible.** The browser may have rendered an event
   whose id it had not yet committed when the connection dropped. Every event
   carries `seq`; the client keeps `lastSeq` and drops anything `<= lastSeq`.
   `seq` is the deduplication key, `Last-Event-ID` is the resume key, and they
   are different things.

2. **Resume can fail.** If the stream was trimmed past the cursor — a turn
   longer than 1000 events, or a reconnect after the conversation expired — the
   entries are gone. The server detects this (`XRANGE` from the cursor returns
   nothing while `XLEN > 0` and the first id exceeds the cursor) and emits a
   terminal `error` with `code: "stream_gap"`, `retryable: false`. The client
   then re-fetches `GET /api/v1/conversations/{id}/turns` and re-renders from
   the durable record rather than showing a half-message forever.

3. **Reconnect backoff is the client's job.** `EventSource` reconnects
   automatically with a browser-chosen interval; that is fine for blips and
   wrong for an outage, because ten thousand widgets reconnecting in lockstep
   is a thundering herd. The server sends `retry: 5000` as the first frame of
   every stream, and `useEventStream` (§13.1) applies its own exponential
   backoff with full jitter on top for anything beyond three consecutive
   failures.

4. **A stream is not a subscription to the future.** `XREAD BLOCK 15000`
   returns after 15 s of silence and 04's loop emits `ping`; after 900 s of
   genuine idleness the generator returns and the connection closes normally.
   The client treats a clean close as "conversation went quiet", not as an
   error, and does not reconnect unless the user acts.

```
Client                                 API                    Redis Stream
  │ GET /conversations/{id}/stream       │                          │
  │ Last-Event-ID: 1723459200123-4       │                          │
  ├─────────────────────────────────────►│  XREAD {key: 172...-4}   │
  │                                      ├─────────────────────────►│
  │ retry: 5000                          │                          │
  │◄─────────────────────────────────────┤                          │
  │ id: 1723459200123-5                  │◄─────────────────────────┤
  │ event: token                         │                          │
  │ data: {"seq":18,"turn_id":"…","text":" appoint"}                │
  │◄─────────────────────────────────────┤                          │
  │                                      │  (15s silence)           │
  │ event: ping                          │                          │
  │ data:                                │                          │
  │◄─────────────────────────────────────┤                          │
```

### 3.4 The web-chat WebSocket

04 leaves this entirely undefined. 06 owns it.

**Endpoint:** `ws /api/v1/channels/web/socket?ticket={ticket}`.

**Auth: a ticket, following the 04 §5.4 nonce precedent.** Browsers cannot set
an `Authorization` header on `WebSocket` or `EventSource`, which 04 does not
acknowledge. The options are a subprotocol header hack, a cookie, or a
single-use ticket. We use the ticket because 04 already established it for
media sockets and because a cookie would be an ambient credential on a
cross-origin widget — exactly what the widget-token design avoids.

```
POST /api/v1/channels/web/tickets       (widget token in Authorization: Bearer)
→ 201 { "ticket": "wt_...", "expires_at": "...", "ws_url": "wss://.../socket?ticket=wt_..." }
```

The ticket is a 32-byte random value in Redis under
`wsticket:{ticket}` → `{tenant_id, conversation_id, aud: "widget"}`, TTL 60 s,
**deleted on redemption**. It appears in the URL, which means it appears in
proxy logs; a 60-second single-use credential in a log is acceptable in a way
that a 10-minute reusable JWT is not. Rejection closes with `4401`, matching
04's media socket.

The same ticket endpoint serves `EventSource`, which has the identical header
limitation: `GET /api/v1/conversations/{id}/stream?ticket=...` is accepted as
an alternative to `Authorization`.

**Envelope.** Every frame both ways:

```typescript
// apps/web/lib/chat/protocol.ts
export interface Envelope<T extends string = string, D = unknown> {
  v: 1;
  type: T;
  id: string;            // uuidv7, client- or server-minted
  ts: string;            // RFC 3339
  data: D;
}
```

Client → server:

| `type` | `data` | Notes |
|---|---|---|
| `message.send` | `{text, attachments?, reply_to_payload_id?}` | `id` doubles as the `Idempotency-Key`. `reply_to_payload_id` is 04 §8.5's opaque choice payload — the round-trip 04 requires. |
| `typing` | `{active: boolean}` | Throttled client-side to 1 per 3 s. |
| `read` | `{up_to_turn_id}` | Read receipt. |
| `cancel` | `{turn_id}` | User interrupts a streaming answer — the chat analogue of barge-in. |
| `pong` | `{}` | Reply to server `ping`. |

Server → client: every SSE event from §3.2, wrapped in the same envelope
(`type` is the SSE event name), plus:

| `type` | `data` |
|---|---|
| `ready` | `{conversation_id, resume_from_seq, capabilities}` — `capabilities` is the `ChannelCapabilities` block from 04 §9.1, so the widget renders exactly what the channel supports |
| `message.ack` | `{client_id, turn_id, seq}` |
| `receipt` | `{turn_id, state: "delivered"\|"read"}` |
| `ping` | `{}` — server→client every 20 s |
| `closing` | `{reason, reconnect_after_ms}` — sent before a deploy drains the socket |

**Heartbeats, both directions.** The server sends `ping` every 20 s and closes
a socket that has not sent any frame (including `pong`) in 60 s. The client
closes and reconnects if it has seen no server frame in 45 s. A one-sided
heartbeat detects a dead peer only in one direction; a half-open TCP connection
where the server's writes vanish into a black hole is exactly the case a
client-side timer catches and a server-side one does not.

**Reconnection.** `ready.resume_from_seq` is the last `seq` the *server*
recorded as delivered. The client compares it with its own `lastSeq` and
back-fills the gap from the Redis stream via the SSE endpoint with
`Last-Event-ID`, rather than the WS replaying — one replay mechanism, not two.
Backoff is exponential with full jitter, base 500 ms, cap 30 s.

**Close codes:**

| Code | Meaning |
|---|---|
| `1000` | normal |
| `1001` | server going away (deploy) — client reconnects immediately |
| `4401` | invalid or expired ticket (04's code) |
| `4403` | conversation not authorized for this token |
| `4408` | heartbeat timeout |
| `4429` | rate limited; `reason` carries `retry_after_ms` |

### 3.5 Portal monitoring streams

`GET /api/v1/conversations/{id}/monitor` is the staff-side SSE stream. Same
event vocabulary as §3.2 with `audience: "staff"`, so `usage` is present.

**Judgment call — Redis pub/sub, not Streams, for the live conversation
*list*.** 04 explicitly rejects pub/sub for conversation token streaming, with
a sound rationale: a subscriber that disconnects loses messages, and a user
switching tabs must not lose part of an answer. That rationale does not
transfer to the live-call list. Its events (`conversation.started`,
`conversation.ended`, `active_count`) are **state announcements, not a
narrative**: the current truth is always re-derivable with one query against
`conversations WHERE status IN ('active','idle')`, so a dropped message costs
one stale row for up to a few seconds and is repaired on reconnect by a full
refetch. Persisting them in a Stream would mean retention, trimming, and
per-admin cursors for data whose value expires in seconds.

Channel: `portal:{tenant_id}:conversations`. On connect, the endpoint emits a
`snapshot` event (the current live list) and *then* subscribes, so there is no
window where an event fires between query and subscribe.

`GET /api/v1/knowledge/jobs/{id}/events` follows the same pattern on
`portal:{tenant_id}:job:{job_id}`, emitting `progress` events
`{documents_processed, documents_discovered, chunks_created, status}` sourced
from the `ingestion_jobs` columns 01 §7.5 already defines.

### 3.6 Live takeover

04 defines caller-interrupts-bot barge-in and agent-initiated warm transfer but
never connects them to `conversations:barge_in`, which 05 defines as "live
takeover". 06 defines the three staff actions, all built on primitives 04
already has:

| Route | Effect |
|---|---|
| `POST /api/v1/conversations/{id}/whisper` | Body `{text}`. Injects a system turn visible only to the agent, steering the next response. No audio path; works on voice and chat. |
| `POST /api/v1/conversations/{id}/takeover` | Suppresses the agent and routes turns to the staff member. On voice, 04's warm-transfer conference (`warm-{call_id}`) is joined with the AI participant muted. On chat, subsequent `message.send` frames come from the staff principal and are stamped `booked_by`-style provenance on the turn. |
| `POST /api/v1/conversations/{id}/transfer` | Body `{destination, mode: "warm"\|"cold"}`. Calls 04 §5.7's `warm_transfer` / `cold_transfer` directly. |

All three require `CONVERSATIONS_BARGE_IN` (owner/admin/manager/agent, not
viewer) and write an audit record. `destination` is hashed in the audit
`metadata` per 04 §5.7's `destination_hash` rule — the human's number is PII.

---

## 4. OpenAPI 3.1 generation and drift control

### 4.1 Generation

FastAPI emits OpenAPI 3.1 natively from Pydantic v2 (both target JSON Schema
2020-12, so there is no dialect translation and `anyOf`/`null` round-trips
correctly). The app factory customises three things:

```python
# apps/api/openapi.py
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def build_openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title="ReceptionAi API",
        version="1.0.0",
        openapi_version="3.1.0",
        summary="Multi-tenant AI receptionist platform.",
        description=(
            "The authenticated principal determines the tenant. There is no "
            "tenant path parameter, no tenant query parameter, and any "
            "`X-Tenant-*` header is rejected with 400 "
            "`tenant_header_not_permitted`."
        ),
        routes=app.routes,
        servers=[{"url": "https://api.receptionai.com", "description": "Production"}],
    )

    schema["components"]["securitySchemes"] = {
        "bearerAuth": {
            "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
            "description": "Ed25519-signed access token, 10 minute TTL "
                           "(05 §A1.1). Also accepts an API key of the form "
                           "`rk_live_<prefix>_<secret>` in the same header.",
        }
    }
    schema["security"] = [{"bearerAuth": []}]

    # Every operation advertises the problem+json envelope. Doing this in the
    # generator rather than per-route means a new route cannot forget it, and
    # generated clients get typed error handling for free.
    problem_ref = {"$ref": "#/components/schemas/Problem"}
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = operation.setdefault("responses", {})
            for status, title in _UNIVERSAL_ERRORS.items():
                responses.setdefault(status, {
                    "description": title,
                    "content": {"application/problem+json": {"schema": problem_ref}},
                })

    schema["components"]["schemas"]["Problem"] = _PROBLEM_SCHEMA
    app.openapi_schema = schema
    return schema


_UNIVERSAL_ERRORS = {
    "401": "Unauthenticated", "403": "Forbidden",
    "422": "Validation failed", "429": "Rate limited",
    "500": "Internal error",
}

_PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type", "title", "status", "code"],
    "properties": {
        "type": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "code": {"type": "string",
                 "description": "Stable machine-readable code. Switch on this, "
                                "not on `type` or `title`."},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "request_id": {"type": "string", "format": "uuid"},
    },
    "additionalProperties": True,
}
```

### 4.2 A real excerpt

```yaml
# openapi.json (rendered as YAML for readability) — committed at repo root
openapi: 3.1.0
info:
  title: ReceptionAi API
  version: 1.0.0
  summary: Multi-tenant AI receptionist platform.
paths:
  /api/v1/services:
    get:
      tags: [services]
      summary: List services
      operationId: list_services
      parameters:
        - name: location_id
          in: query
          required: false
          schema: { anyOf: [{ type: string, format: uuid }, { type: "null" }] }
        - name: is_active
          in: query
          required: false
          schema: { anyOf: [{ type: boolean }, { type: "null" }] }
        - name: cursor
          in: query
          required: false
          schema: { anyOf: [{ type: string }, { type: "null" }] }
        - name: limit
          in: query
          required: false
          schema: { type: integer, minimum: 1, maximum: 200, default: 50 }
      responses:
        "200":
          description: Successful Response
          content:
            application/json:
              schema: { $ref: "#/components/schemas/CursorPage_ServiceOut_" }
        "401": { $ref: "#/components/responses/Problem401" }
        "403": { $ref: "#/components/responses/Problem403" }
        "422": { $ref: "#/components/responses/Problem422" }
        "429": { $ref: "#/components/responses/Problem429" }
      security: [{ bearerAuth: [] }]
      x-required-permissions: [bookings:read]
      x-endpoint-class: read

  /api/v1/bookings:
    post:
      tags: [bookings]
      summary: Create a booking
      operationId: create_booking
      parameters:
        - name: Idempotency-Key
          in: header
          required: true
          schema: { type: string, maxLength: 255 }
          description: >
            Required. A retry with the same key returns the original response
            and never creates a second booking.
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: "#/components/schemas/CreateBookingRequest" }
      responses:
        "201":
          description: Booking created
          headers:
            ETag: { schema: { type: string }, description: Version for If-Match }
            Location: { schema: { type: string, format: uri } }
          content:
            application/json:
              schema: { $ref: "#/components/schemas/BookingOut" }
        "409":
          description: The requested time is no longer available
          content:
            application/problem+json:
              schema: { $ref: "#/components/schemas/SlotUnavailableProblem" }
      security: [{ bearerAuth: [] }]
      x-required-permissions: [bookings:create]
      x-endpoint-class: write

components:
  schemas:
    CreateBookingRequest:
      type: object
      required: [service_id, starts_at, customer_name]
      properties:
        service_id: { type: string, format: uuid }
        location_id: { anyOf: [{ type: string, format: uuid }, { type: "null" }] }
        starts_at: { type: string, format: date-time }
        duration_units: { anyOf: [{ type: integer, minimum: 1 }, { type: "null" }] }
        party_size: { type: integer, minimum: 1, default: 1 }
        resource_ids:
          type: array
          items: { type: string, format: uuid }
          description: Optional explicit allocation; otherwise the engine allocates.
        hold_id:
          anyOf: [{ type: string, format: uuid }, { type: "null" }]
          description: Convert an existing hold. Mutually exclusive with resource_ids.
        customer_name: { type: string, maxLength: 200 }
        customer_phone_e164:
          anyOf: [{ type: string, pattern: "^\\+[1-9]\\d{6,14}$" }, { type: "null" }]
        customer_email: { anyOf: [{ type: string, format: email }, { type: "null" }] }
        intake_data:
          type: object
          additionalProperties: true
          description: Validated against the vertical manifest's intake form.
        resolved_hash:
          anyOf: [{ type: string }, { type: "null" }]
          description: >
            From RenderDocument.meta.resolved_hash. If the manifest changed
            since the form was rendered, returns 409 with the new document
            (02 §7.4 rule 2).
      additionalProperties: false

    SlotUnavailableProblem:
      allOf:
        - { $ref: "#/components/schemas/Problem" }
        - type: object
          properties:
            conflict_kind: { type: string, enum: [overlap, capacity, hold_expired] }
            resource_id: { type: string, format: uuid }
            conflicting_range:
              type: array
              items: { type: string, format: date-time }
              minItems: 2
              maxItems: 2
            alternatives:
              type: array
              items: { $ref: "#/components/schemas/AvailabilitySlot" }
```

`x-required-permissions` and `x-endpoint-class` are emitted from the same
metadata the runtime reads — the `requires()` marker and the `endpoint_class`
decorator — so the documentation of a route's authorization cannot disagree
with its enforcement. They are extracted, never hand-written.

### 4.3 Keeping the spec honest

`openapi.json` is **committed at the repo root** and CI fails on any
divergence:

```yaml
# .github/workflows/api-contract.yml (excerpt)
      - name: Regenerate spec
        run: uv run python -m apps.api.tools.dump_openapi > /tmp/openapi.json

      - name: Fail if the committed spec is stale
        run: |
          if ! diff -u openapi.json /tmp/openapi.json > /tmp/spec.diff; then
            echo "::error::openapi.json is out of date. Run: make openapi"
            cat /tmp/spec.diff
            exit 1
          fi

      - name: Fail on breaking changes against main
        run: |
          git show origin/main:openapi.json > /tmp/base.json
          oasdiff breaking /tmp/base.json openapi.json \
            --fail-on ERR --format githubactions
```

Three gates, each catching a different failure:

1. **Stale-spec diff.** Catches "the code changed and nobody regenerated". The
   spec is a build artifact that happens to be committed, and committing it is
   what makes gate 2 possible — `oasdiff` needs a base to compare against, and
   generating the base from `main`'s *code* would require checking out and
   booting the old app.
2. **`oasdiff breaking`.** This is 05 §F23.8's
   `test_openapi_schema_is_backward_compatible`, run as a CI step rather than a
   pytest so the diff renders as GitHub annotations on the changed lines. It
   catches removed fields, tightened types, new required request fields, and
   removed enum values.
3. **Schemathesis** against a running instance, seeded from the spec.
   Property-based fuzzing that asserts no endpoint returns 500 on generated
   input and that responses validate against their declared schemas —
   05 §A3.4's "no endpoint may return 500 on Schemathesis-generated input".

A fourth gate lives in `apps/web`: the TypeScript client is generated from the
committed spec by `openapi-typescript`, and `tsc --noEmit` in CI means a
breaking API change that slips past `oasdiff` still fails the frontend build.
The frontend never hand-writes a response type.

```json
// package.json (apps/web)
{
  "scripts": {
    "api:types": "openapi-typescript ../../openapi.json -o lib/api/schema.d.ts",
    "api:check": "npm run api:types && git diff --exit-code lib/api/schema.d.ts"
  }
}
```

---

## 5. Two routers in full: services and bookings

### 5.1 The dependency chain

Every route resolves the same four layers, in this order:

```
  HTTP request
      │
      ▼
  RequestIdMiddleware ................. X-Request-Id
      │
      ▼
  TenantContextMiddleware (05 §A3.3) .. L1 IDENTITY
      │   validates Ed25519 JWT or rk_ API key
      │   rejects X-Tenant-* with 400
      │   sets request.state.principal + current_principal ContextVar
      ▼
  RateLimitMiddleware (05 §B8) ........ per-tenant / per-user / per-IP
      │
      ▼
  IdempotencyMiddleware (§2.5) ........ replay protection
      │
      ▼
  Depends(requires(Perm.X)) ........... L2 RBAC — never touches the DB
      │
      ▼
  Depends(get_session) ................ L3 RLS
      │   tenant_session(router, principal)
      │   SELECT set_config('app.tenant_id', :tid, true)
      │   → every subsequent query is tenant-scoped by Postgres
      ▼
  handler → service → repository
```

Two notes on names. 01 §15 and 05 §A3.6 give two different DB dependencies —
`db_session`/`DbSession` and `get_session`/`DbSession`. They are the same
thing under two names; see §16.3. This document uses **`DbSession`**, which
both agree on, and the sole definition lives in `apps/api/deps/db.py` per 05.

The handler never sees a `tenant_id` parameter, because `TenantContext` (01
§15.1) has no path from a request parameter to construction. That is contract
§10 enforced by type signature rather than by review.

### 5.2 The simple one: services

```python
# apps/api/routers/services.py
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from apps.api.deps.authz import requires
from apps.api.deps.concurrency import ExpectedVersion, etag_for
from apps.api.deps.db import DbSession
from apps.api.ratelimit.declare import endpoint_class
from apps.api.ratelimit.rules import EndpointClass
from apps.api.schemas.services import (
    CreateServiceRequest,
    ServiceOut,
    ServiceQuery,
    UpdateServiceRequest,
)
from apps.api.serialization.sparse import project
from packages.core.api.pagination import CursorPage, CursorError, decode_cursor, page_from
from packages.core.api.problems import ProblemException
from packages.core.auth.principal import Principal
from packages.core.authz.permissions import Perm
from packages.persistence.errors import ConcurrencyConflictError, EntityNotFoundError
from packages.persistence.repositories.services import ServiceRepository

router = APIRouter(prefix="/api/v1/services", tags=["services"])


@router.get("", response_model=CursorPage[ServiceOut])
@endpoint_class(EndpointClass.READ)
async def list_services(
    session: DbSession,
    q: Annotated[ServiceQuery, Query()],
    fields: str | None = None,
    _: Principal = Depends(requires(Perm.BOOKINGS_READ)),
) -> CursorPage[ServiceOut]:
    """List services.

    No `tenant_id` filter appears anywhere in this handler or in the
    repository. RLS supplies it. 05 §B8 is explicit that adding a manual filter
    "would mask an RLS regression instead of exposing it in tests" — if the
    policy were ever dropped, a hand-written WHERE would keep the tests green
    while the database was wide open.
    """
    try:
        cursor = decode_cursor(q.cursor, expect_sort=q.sort) if q.cursor else None
    except CursorError as exc:
        raise ProblemException(
            status=400, code="invalid_cursor", title="Malformed cursor",
            detail=str(exc),
        ) from exc

    repo = ServiceRepository(session)
    rows = await repo.list_page(
        location_id=q.location_id,
        is_active=q.is_active,
        category=q.category,
        search=q.q,
        cursor=cursor,
        sort=q.sort,
        limit=q.limit + 1,          # +1 answers has_more without a COUNT
    )
    page = page_from(
        [ServiceOut.model_validate(r) for r in rows],
        q.limit, sort=q.sort, key=lambda s: (s.created_at, s.id),
    )
    if fields:
        page.items = [project(item, fields) for item in page.items]  # type: ignore[misc]
    return page


@router.get("/{service_id}", response_model=ServiceOut)
@endpoint_class(EndpointClass.READ)
async def get_service(
    service_id: UUID,
    session: DbSession,
    response: Response,
    fields: str | None = None,
    _: Principal = Depends(requires(Perm.BOOKINGS_READ)),
) -> ServiceOut:
    repo = ServiceRepository(session)
    service = await repo.get(service_id)
    if service is None:
        # 404 not 403: a cross-tenant id returns zero rows under RLS and is
        # indistinguishable from a nonexistent one. 05 §A3.4 requires this —
        # a 403 here would be an enumeration oracle.
        raise ProblemException(
            status=404, code="not_found", title="Service not found",
            detail=f"No service with id {service_id}.",
        )
    out = ServiceOut.model_validate(service)
    response.headers["ETag"] = etag_for(out.version)
    return project(out, fields)   # type: ignore[return-value]


@router.post("", response_model=ServiceOut, status_code=status.HTTP_201_CREATED)
@endpoint_class(EndpointClass.WRITE)
async def create_service(
    payload: CreateServiceRequest,
    session: DbSession,
    response: Response,
    principal: Principal = Depends(requires(Perm.BOOKINGS_UPDATE)),
) -> ServiceOut:
    repo = ServiceRepository(session)
    service = await repo.create(
        **payload.model_dump(exclude_unset=True),
        # tenant_id is NOT passed. TenantSession's before_flush listener
        # (01 §15.3) asserts it matches the session's tenant, and the model
        # default reads it from the bound context. Passing it here would create
        # a second source of truth that CrossTenantWriteError exists to catch.
        created_by_user_id=principal.user_id,
    )
    await session.flush()
    out = ServiceOut.model_validate(service)
    response.headers["ETag"] = etag_for(out.version)
    response.headers["Location"] = f"/api/v1/services/{out.id}"
    return out


@router.patch("/{service_id}", response_model=ServiceOut)
@endpoint_class(EndpointClass.WRITE)
async def update_service(
    service_id: UUID,
    payload: UpdateServiceRequest,
    session: DbSession,
    response: Response,
    expected_version: ExpectedVersion,
    _: Principal = Depends(requires(Perm.BOOKINGS_UPDATE)),
) -> ServiceOut:
    """Partial update, guarded by If-Match.

    `exclude_unset=True` is what makes this a genuine PATCH: a field the client
    did not send is absent from the dict, so it is not written. Without it,
    Pydantic defaults would flow through and a client editing only `price`
    would silently reset `description` to None.
    """
    repo = ServiceRepository(session)
    try:
        service = await repo.update_versioned(
            service_id, expected_version, **payload.model_dump(exclude_unset=True)
        )
    except EntityNotFoundError:
        raise ProblemException(
            status=404, code="not_found", title="Service not found",
            detail=f"No service with id {service_id}.",
        ) from None
    except ConcurrencyConflictError as exc:
        current = await repo.get(service_id)
        raise ProblemException(
            status=412, code="version_conflict",
            title="The record changed since you loaded it",
            detail=f"Expected version {expected_version}, current is {exc.current_version}.",
            expected_version=expected_version,
            current_version=exc.current_version,
            current=ServiceOut.model_validate(current).model_dump(mode="json"),
        ) from exc

    out = ServiceOut.model_validate(service)
    response.headers["ETag"] = etag_for(out.version)
    return out


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
@endpoint_class(EndpointClass.WRITE)
async def delete_service(
    service_id: UUID,
    session: DbSession,
    expected_version: ExpectedVersion,
    _: Principal = Depends(requires(Perm.BOOKINGS_UPDATE)),
) -> None:
    """Soft delete.

    `services` has `deleted_at`, and `_base_select()` (01 §15.4) filters it
    automatically, so a deleted service vanishes from every read path without
    a single call site changing. Hard deletion is impossible here by design:
    `bookings.service_id` references it, and historical bookings must keep
    resolving their service after it is retired from the menu.
    """
    repo = ServiceRepository(session)
    try:
        await repo.soft_delete_versioned(service_id, expected_version)
    except EntityNotFoundError:
        raise ProblemException(
            status=404, code="not_found", title="Service not found",
        ) from None
    except ConcurrencyConflictError as exc:
        raise ProblemException(
            status=412, code="version_conflict",
            title="The record changed since you loaded it",
            current_version=exc.current_version,
        ) from exc
```

The response model, showing the money and timezone rules from contract §6:

```python
# apps/api/schemas/services.py
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

PriceModel = Literal["flat", "per_unit", "per_person", "per_unit_per_person", "free", "quote"]


class ServiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    location_id: UUID | None
    code: str
    name: str
    description: str | None
    category: str | None
    duration_minutes: int | None
    duration_units: int | None
    is_duration_variable: bool
    buffer_before_minutes: int
    buffer_after_minutes: int
    min_party_size: int | None
    max_party_size: int | None
    lead_time_minutes: int
    booking_horizon_days: int
    cancellation_window_minutes: int
    requires_approval: bool
    allow_waitlist: bool
    allow_double_booking: bool
    price: Decimal | None
    currency: str | None
    price_model: PriceModel
    deposit_amount: Decimal | None
    color: str | None
    sort_order: int
    is_active: bool
    is_bookable_online: bool
    created_at: datetime
    updated_at: datetime
    version: int

    @field_serializer("price", "deposit_amount")
    def _money_as_string(self, value: Decimal | None) -> str | None:
        """Contract §6: money is NUMERIC(12,4), never a float.

        JSON numbers are IEEE 754 doubles in every JavaScript runtime, so
        emitting `95.10` as a number means the browser holds 95.09999999999999
        and a sum of a hundred line items drifts by cents. Serializing to a
        string forces the client to use a decimal library — and `apps/web` does
        exactly that. This mirrors 03 §9.5's `TurnCostEvent.cost_usd`, which is
        already a string for the same reason.
        """
        return None if value is None else f"{value:.4f}"


class CreateServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    location_id: UUID | None = None
    description: str | None = None
    category: str | None = None
    duration_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None
    price: Decimal | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    price_model: PriceModel = "flat"
    duration_units: Annotated[int, Field(ge=1)] | None = None
    is_duration_variable: bool = False
    buffer_before_minutes: Annotated[int, Field(ge=0, le=480)] = 0
    buffer_after_minutes: Annotated[int, Field(ge=0, le=480)] = 0
    min_party_size: Annotated[int, Field(ge=1)] | None = None
    max_party_size: Annotated[int, Field(ge=1)] | None = None
    lead_time_minutes: Annotated[int, Field(ge=0)] = 0
    booking_horizon_days: Annotated[int, Field(ge=1, le=730)] = 90
    cancellation_window_minutes: Annotated[int, Field(ge=0)] = 1440
    requires_approval: bool = False
    allow_waitlist: bool = False
    allow_double_booking: bool = False
    deposit_amount: Decimal | None = None
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None
    sort_order: int = 0
    is_active: bool = True
    is_bookable_online: bool = True

    @model_validator(mode="after")
    def _party_size_is_ordered(self) -> "CreateServiceRequest":
        if (
            self.min_party_size is not None
            and self.max_party_size is not None
            and self.min_party_size > self.max_party_size
        ):
            raise ValueError("min_party_size must not exceed max_party_size")
        if self.price is not None and self.currency is None:
            # A price without a currency is not a price. The DB has a CHECK for
            # this; catching it here gives a field-level 422 instead of a 23514
            # translated into a generic "business rule" message.
            raise ValueError("currency is required when price is set")
        return self


class UpdateServiceRequest(BaseModel):
    """Every field optional. `exclude_unset=True` at the call site distinguishes
    'not sent' from 'sent as null', which is the whole difference between PATCH
    and PUT.

    Deliberately narrower than CreateServiceRequest: `code` is absent because
    it is referenced by manifests and external systems, so renaming it silently
    breaks tool arguments the agent was given. Changing a code is a delete plus
    a create, made visible.
    """
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    description: str | None = None
    category: str | None = None
    duration_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None
    duration_units: Annotated[int, Field(ge=1)] | None = None
    buffer_before_minutes: Annotated[int, Field(ge=0, le=480)] | None = None
    buffer_after_minutes: Annotated[int, Field(ge=0, le=480)] | None = None
    min_party_size: Annotated[int, Field(ge=1)] | None = None
    max_party_size: Annotated[int, Field(ge=1)] | None = None
    lead_time_minutes: Annotated[int, Field(ge=0)] | None = None
    booking_horizon_days: Annotated[int, Field(ge=1, le=730)] | None = None
    cancellation_window_minutes: Annotated[int, Field(ge=0)] | None = None
    requires_approval: bool | None = None
    allow_waitlist: bool | None = None
    allow_double_booking: bool | None = None
    price: Decimal | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    price_model: PriceModel | None = None
    deposit_amount: Decimal | None = None
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    is_bookable_online: bool | None = None
```

### 5.3 The hard one: bookings

Availability search, holds, idempotent creation, and conflict handling with
alternatives.

```python
# apps/api/routers/bookings.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status

from apps.api.deps.authz import requires
from apps.api.deps.concurrency import ExpectedVersion, etag_for
from apps.api.deps.db import DbSession
from apps.api.ratelimit.declare import endpoint_class
from apps.api.ratelimit.rules import EndpointClass
from apps.api.schemas.bookings import (
    AvailabilityRequest,
    AvailabilityResponse,
    BookingOut,
    BookingQuery,
    CancelBookingRequest,
    CreateBookingRequest,
    CreateHoldRequest,
    HoldOut,
    RescheduleRequest,
)
from packages.booking.availability import AvailabilityService
from packages.booking.errors import (
    HoldExpiredError,
    ManifestChangedError,
    PolicyViolation,
    SlotUnavailableError,
)
from packages.booking.service import BookingService
from packages.core.api.pagination import CursorPage, CursorError, decode_cursor, page_from
from packages.core.api.problems import ProblemException
from packages.core.auth.principal import Principal
from packages.core.authz.permissions import Perm
from packages.persistence.errors import (
    ConcurrencyConflictError,
    EntityNotFoundError,
    ResourceConflictError,
)
from packages.persistence.repositories.bookings import BookingRepository

router = APIRouter(prefix="/api/v1", tags=["bookings"])

_MAX_SEARCH_SPAN = timedelta(days=62)


# ---------------------------------------------------------------------------
# Availability search
# ---------------------------------------------------------------------------

@router.post("/availability/search", response_model=AvailabilityResponse)
@endpoint_class(EndpointClass.SEARCH)
async def search_availability(
    payload: AvailabilityRequest,
    session: DbSession,
    _: Principal = Depends(requires(Perm.BOOKINGS_READ)),
) -> AvailabilityResponse:
    """Find bookable slots.

    POST for a read (§2.3): the body carries an `attribute_predicate` JSONB
    document (01 §5.5) that has no sane query-string encoding, and putting
    customer-adjacent search terms in a URL puts them in every access log
    between here and the client.

    Search is READ-ONLY and holds nothing. A slot returned here can be taken
    before the caller acts on it — which is exactly why POST /holds exists and
    why POST /bookings must handle 409 (see below). Any design where search
    implicitly reserves capacity produces a system whose availability collapses
    under browsing traffic.
    """
    if payload.date_to - payload.date_from > _MAX_SEARCH_SPAN:
        raise ProblemException(
            status=422, code="search_span_too_large",
            title="Date range too large",
            detail=f"At most {_MAX_SEARCH_SPAN.days} days per search; "
                   f"requested {(payload.date_to - payload.date_from).days}.",
        )

    svc = AvailabilityService(session)
    try:
        result = await svc.search(
            service_id=payload.service_id,
            location_id=payload.location_id,
            date_from=payload.date_from,
            date_to=payload.date_to,
            party_size=payload.party_size,
            duration_units=payload.duration_units,
            resource_ids=payload.resource_ids,
            resource_type_id=payload.resource_type_id,
            attribute_predicate=payload.attribute_predicate,
            part_of_day=payload.part_of_day,
            limit=payload.limit,
        )
    except EntityNotFoundError:
        raise ProblemException(
            status=404, code="not_found", title="Service not found",
            detail=f"No service with id {payload.service_id}.",
        ) from None
    except PolicyViolation as exc:
        # e.g. requested range falls entirely outside booking_horizon_days.
        raise ProblemException(
            status=422, code="policy_violation", title="Search violates a booking policy",
            detail=exc.message, policy=exc.policy_key,
        ) from exc

    return AvailabilityResponse(
        slots=result.slots,
        timezone=result.timezone,           # the LOCATION's IANA zone, contract §6
        service_id=payload.service_id,
        searched_from=payload.date_from,
        searched_to=payload.date_to,
        truncated=result.truncated,
    )


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------

@router.post("/holds", response_model=HoldOut, status_code=status.HTTP_201_CREATED)
@endpoint_class(EndpointClass.WRITE)
async def create_hold(
    payload: CreateHoldRequest,
    session: DbSession,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Principal = Depends(requires(Perm.BOOKINGS_CREATE)),
) -> HoldOut:
    """Reserve capacity for a bounded window while details are collected.

    The TTL is the tenant's `booking.hold_ttl_seconds` setting, clamped to
    [60, 1800]. A hold longer than 30 minutes is capacity destruction dressed
    up as courtesy: on a busy Saturday it removes a table from the pool for
    half an evening because someone opened a booking form and walked away.

    01 §13.3's three-layer TTL means the API does not sweep anything. The
    read-time predicate `expires_at > now()` in `booking_check_conflicts` is
    authoritative; the Temporal timer releases promptly; `ops_expire_stale_holds`
    is janitorial. This handler participates in none of them.
    """
    svc = BookingService(session, principal=principal)
    try:
        hold = await svc.create_hold(
            service_id=payload.service_id,
            location_id=payload.location_id,
            starts_at=payload.starts_at,
            duration_units=payload.duration_units,
            party_size=payload.party_size,
            resource_ids=payload.resource_ids,
            conversation_id=payload.conversation_id,
            idempotency_key=idempotency_key,
        )
    except SlotUnavailableError as exc:
        raise _slot_conflict(exc) from exc
    except ResourceConflictError as exc:
        # 23P01 from ex_booking_holds__no_overlap — another hold beat us.
        raise _slot_conflict(SlotUnavailableError.from_repo(exc)) from exc

    await session.flush()
    out = HoldOut.model_validate(hold)
    response.headers["Location"] = f"/api/v1/holds/{out.id}"
    # A client that ignores the body still learns the deadline from a header.
    response.headers["X-Hold-Expires-At"] = out.expires_at.isoformat()
    return out


@router.delete("/holds/{hold_id}", status_code=status.HTTP_204_NO_CONTENT)
@endpoint_class(EndpointClass.WRITE)
async def release_hold(
    hold_id: UUID,
    session: DbSession,
    principal: Principal = Depends(requires(Perm.BOOKINGS_CREATE)),
) -> None:
    """Release early. Idempotent: releasing an already-released or expired hold
    is a 204, not a 404 — the caller's intent ('this hold should not block
    anything') is already satisfied, and a client abandoning a form should
    never see an error for tidying up."""
    svc = BookingService(session, principal=principal)
    await svc.release_hold(hold_id, reason="released_by_client")


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

@router.get("/bookings", response_model=CursorPage[BookingOut])
@endpoint_class(EndpointClass.READ)
async def list_bookings(
    session: DbSession,
    q: Annotated[BookingQuery, Query()],
    _: Principal = Depends(requires(Perm.BOOKINGS_READ)),
) -> CursorPage[BookingOut]:
    try:
        cursor = decode_cursor(q.cursor, expect_sort=q.sort) if q.cursor else None
    except CursorError as exc:
        raise ProblemException(
            status=400, code="invalid_cursor", title="Malformed cursor", detail=str(exc)
        ) from exc

    repo = BookingRepository(session)
    rows = await repo.list_page(
        status=q.status.split(",") if q.status else None,
        location_id=q.location_id,
        service_id=q.service_id,
        resource_id=q.resource_id,
        starts_at_gte=q.starts_at__gte,
        starts_at_lte=q.starts_at__lte,
        channel=q.channel.split(",") if q.channel else None,
        search=q.q,
        cursor=cursor, sort=q.sort, limit=q.limit + 1,
    )
    sort_key = (
        (lambda b: (b.starts_at, b.id)) if "starts_at" in q.sort
        else (lambda b: (b.created_at, b.id))
    )
    return page_from(
        [BookingOut.model_validate(r) for r in rows], q.limit, sort=q.sort, key=sort_key
    )


@router.post("/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
@endpoint_class(EndpointClass.WRITE)
async def create_booking(
    payload: CreateBookingRequest,
    session: DbSession,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Principal = Depends(requires(Perm.BOOKINGS_CREATE)),
) -> BookingOut:
    """Create a booking, optionally by converting a hold.

    Concurrency is settled in Postgres, not here. `booking_items` carries
    `ex_booking_items__no_overlap` (01 §5.10) — a GiST EXCLUDE over
    `(tenant_id =, resource_id =, occupies &&)` where `is_blocking AND NOT
    is_capacity_shared`. Two simultaneous requests for the same slot: one
    commits, the other gets SQLSTATE 23P01. There is no application lock, no
    check-then-insert race, and no way for both to win.

    Application-level "check availability then insert" is the classic wrong
    answer: the window between SELECT and INSERT is where every double-booking
    bug in every booking system lives. The constraint closes it in the storage
    engine, which is the only place it can be closed.

    Three conflict shapes are distinguished for the caller:
      * hold_expired  — 409, the hold's TTL passed at conversion (lock_not_available)
      * overlap       — 409, a resource is already occupied (23P01)
      * duplicate     — 200, the idempotency key already produced a booking (23505)
    """
    svc = BookingService(session, principal=principal)
    try:
        booking, created = await svc.create(
            service_id=payload.service_id,
            location_id=payload.location_id,
            starts_at=payload.starts_at,
            duration_units=payload.duration_units,
            party_size=payload.party_size,
            resource_ids=payload.resource_ids,
            hold_id=payload.hold_id,
            customer_name=payload.customer_name,
            customer_phone_e164=payload.customer_phone_e164,
            customer_email=payload.customer_email,
            intake_data=payload.intake_data,
            notes=payload.notes,
            channel=payload.channel or "api",
            booked_by="staff" if principal.user_id else "api",
            idempotency_key=idempotency_key,
            resolved_hash=payload.resolved_hash,
        )
    except ManifestChangedError as exc:
        # 02 §7.4 rule 2: the manifest changed between render and submit. The
        # client re-renders rather than writing data shaped for a manifest that
        # no longer exists.
        raise ProblemException(
            status=409, code="manifest_changed",
            title="The form definition changed",
            detail="Re-render the form and resubmit.",
            expected_hash=payload.resolved_hash,
            current_hash=exc.current_hash,
            render_document=exc.render_document,
        ) from exc
    except HoldExpiredError as exc:
        raise ProblemException(
            status=409, code="hold_expired", title="The hold expired",
            detail="The reservation window elapsed before the booking was "
                   "submitted. Search again and take a new hold.",
            hold_id=str(payload.hold_id),
            expired_at=exc.expired_at.isoformat(),
            conflict_kind="hold_expired",
        ) from exc
    except SlotUnavailableError as exc:
        raise _slot_conflict(exc) from exc
    except ResourceConflictError as exc:
        if exc.kind == "duplicate":
            # uq_bookings__tenant_idempotency. Redis lost the key (eviction,
            # flush, failover) but the database did not. Return the original
            # booking with 200, not 201 — the caller's first request already
            # created it, and reporting 201 twice would be a lie.
            existing = await BookingRepository(session).get_by_idempotency_key(
                idempotency_key
            )
            out = BookingOut.model_validate(existing)
            response.status_code = status.HTTP_200_OK
            response.headers["ETag"] = etag_for(out.version)
            response.headers["Idempotency-Replayed"] = "true"
            return out
        if exc.kind == "overlap":
            raise _slot_conflict(SlotUnavailableError.from_repo(exc)) from exc
        raise ProblemException(
            status=409, code="conflict", title="Conflict", detail=exc.message,
            conflict_kind=exc.kind,
        ) from exc
    except PolicyViolation as exc:
        # Lead time, booking horizon, party size vs capacity, required intake
        # fields. 422 not 409: the request is well-formed and the slot may be
        # free, but a manifest policy forbids it. Retrying identically will
        # always fail, which is what separates 422 from 409.
        raise ProblemException(
            status=422, code="policy_violation", title="Booking policy violation",
            detail=exc.message, policy=exc.policy_key, field=exc.field,
        ) from exc

    await session.flush()
    out = BookingOut.model_validate(booking)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    response.headers["ETag"] = etag_for(out.version)
    response.headers["Location"] = f"/api/v1/bookings/{out.id}"
    return out


@router.post("/bookings/{booking_id}/reschedule", response_model=BookingOut)
@endpoint_class(EndpointClass.WRITE)
async def reschedule_booking(
    booking_id: UUID,
    payload: RescheduleRequest,
    session: DbSession,
    response: Response,
    expected_version: ExpectedVersion,
    principal: Principal = Depends(requires(Perm.BOOKINGS_UPDATE)),
) -> BookingOut:
    """Move a booking.

    Not a PATCH on `service_period`, for three reasons that together make the
    field-assignment framing wrong:

      * `blocking_period` is GENERATED from `service_period` plus buffers, and
        `booking_items.occupies` is trigger-maintained from it
        (`cascade_booking_period_to_items`). The client owns none of that.
      * The new slot must clear the same exclusion constraint as a fresh
        booking, so this is a conflict-bearing operation that can 409.
      * The Temporal workflow needs a `reschedule` signal to cancel pending
        reminders and re-arm them. A PATCH would leave the old reminders
        scheduled.
    """
    svc = BookingService(session, principal=principal)
    try:
        booking = await svc.reschedule(
            booking_id,
            expected_version=expected_version,
            starts_at=payload.starts_at,
            duration_units=payload.duration_units,
            resource_ids=payload.resource_ids,
            reason=payload.reason,
            override_policy=(
                payload.override_policy
                and Perm.BOOKINGS_OVERRIDE in principal.permissions
            ),
        )
    except EntityNotFoundError:
        raise ProblemException(
            status=404, code="not_found", title="Booking not found"
        ) from None
    except ConcurrencyConflictError as exc:
        raise ProblemException(
            status=412, code="version_conflict",
            title="The record changed since you loaded it",
            current_version=exc.current_version,
        ) from exc
    except SlotUnavailableError as exc:
        raise _slot_conflict(exc) from exc
    except PolicyViolation as exc:
        raise ProblemException(
            status=422, code="policy_violation",
            title="Reschedule violates a booking policy",
            detail=exc.message, policy=exc.policy_key,
            # Tell an owner they COULD override; do not tell a viewer, because
            # advertising a capability they lack is a support ticket.
            overridable=Perm.BOOKINGS_OVERRIDE in principal.permissions,
        ) from exc

    out = BookingOut.model_validate(booking)
    response.headers["ETag"] = etag_for(out.version)
    return out


@router.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
@endpoint_class(EndpointClass.WRITE)
async def cancel_booking(
    booking_id: UUID,
    payload: CancelBookingRequest,
    session: DbSession,
    response: Response,
    expected_version: ExpectedVersion,
    principal: Principal = Depends(requires(Perm.BOOKINGS_CANCEL)),
) -> BookingOut:
    """Cancel, applying the manifest's cancellation window.

    Separate permission from BOOKINGS_UPDATE (05 §A2) because cancelling can
    trigger a refund and free capacity someone else is waiting for. A role that
    may correct a typo in a booking is not automatically a role that may
    destroy it.
    """
    svc = BookingService(session, principal=principal)
    try:
        booking = await svc.cancel(
            booking_id,
            expected_version=expected_version,
            reason=payload.reason,
            cancelled_by="staff",
            waive_fee=(
                payload.waive_fee and Perm.BOOKINGS_OVERRIDE in principal.permissions
            ),
            notify_customer=payload.notify_customer,
        )
    except EntityNotFoundError:
        raise ProblemException(status=404, code="not_found", title="Booking not found") from None
    except ConcurrencyConflictError as exc:
        raise ProblemException(
            status=412, code="version_conflict",
            title="The record changed since you loaded it",
            current_version=exc.current_version,
        ) from exc
    except PolicyViolation as exc:
        raise ProblemException(
            status=422, code="cancellation_window_closed",
            title="Cancellation window has closed",
            detail=exc.message, policy=exc.policy_key,
            fee_amount=exc.context.get("fee_amount"),
            overridable=Perm.BOOKINGS_OVERRIDE in principal.permissions,
        ) from exc

    out = BookingOut.model_validate(booking)
    response.headers["ETag"] = etag_for(out.version)
    return out


# ---------------------------------------------------------------------------

def _slot_conflict(exc: SlotUnavailableError) -> ProblemException:
    """Build the 409 that every booking client must handle.

    `alternatives` is the load-bearing part. A voice agent that just lost a race
    can offer the next two slots inside the same turn instead of re-running
    availability search and adding a second of latency to a call with a human
    waiting on the line. The service layer computes them from the same
    availability pass that detected the conflict, so they cost nothing extra.
    """
    return ProblemException(
        status=409,
        code="slot_unavailable",
        title="The requested time is no longer available",
        detail=exc.message,
        conflict_kind=exc.kind,
        resource_id=str(exc.resource_id) if exc.resource_id else None,
        conflicting_range=(
            [exc.conflict_range[0].isoformat(), exc.conflict_range[1].isoformat()]
            if exc.conflict_range else None
        ),
        alternatives=[a.model_dump(mode="json") for a in exc.alternatives],
    )
```

The availability request and slot models:

```python
# apps/api/schemas/bookings.py (excerpt)
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AvailabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: UUID
    location_id: UUID | None = None
    date_from: date
    date_to: date
    party_size: Annotated[int, Field(ge=1, le=500)] = 1
    duration_units: Annotated[int, Field(ge=1)] | None = None
    resource_ids: list[UUID] = Field(default_factory=list, max_length=50)
    resource_type_id: UUID | None = None
    attribute_predicate: dict[str, Any] | None = Field(
        default=None,
        description="Predicate over resource attributes, using the closed "
                    "operator vocabulary of 02 §1.9. No expression escape hatch.",
    )
    part_of_day: Literal["morning", "afternoon", "evening", "any"] = "any"
    limit: Annotated[int, Field(ge=1, le=200)] = 50

    @model_validator(mode="after")
    def _range_is_ordered(self):
        if self.date_to < self.date_from:
            raise ValueError("date_to must be on or after date_from")
        return self


class AvailabilitySlot(BaseModel):
    starts_at: datetime          # UTC, always. Contract §6.
    ends_at: datetime
    resource_ids: list[UUID]
    remaining_capacity: int | None = None
    price: str | None = None     # decimal string, never a float
    currency: str | None = None


class AvailabilityResponse(BaseModel):
    slots: list[AvailabilitySlot]
    timezone: str                # IANA zone for rendering; derived, never stored
    service_id: UUID
    searched_from: date
    searched_to: date
    truncated: bool = Field(
        description="True when `limit` cut the result. The UI must show 'more "
                    "available' rather than implying the day is nearly full."
    )
```

---

## 6. Outbound webhooks

Tenants subscribe to platform events. This is the read-side counterpart of
contract §4's escape hatch: custom logic runs on their infrastructure.

### 6.1 Subscription model

```python
# apps/api/schemas/webhooks.py
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

EventType = Literal[
    "booking.appointment.created", "booking.appointment.confirmed",
    "booking.appointment.rescheduled", "booking.appointment.cancelled",
    "booking.appointment.completed", "booking.appointment.no_show",
    "booking.hold.created", "booking.hold.expired",
    "booking.waitlist.offered", "booking.waitlist.converted",
    "conversation.session.started", "conversation.session.ended",
    "conversation.session.escalated", "conversation.outcome.classified",
    "knowledge.ingestion.succeeded", "knowledge.ingestion.failed",
    "connector.sync.failed", "connector.credential.invalidated",
    "billing.usage.threshold_reached", "billing.subscription.updated",
]


class CreateWebhookSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    event_types: Annotated[list[EventType], Field(min_length=1, max_length=40)]
    description: str | None = Field(default=None, max_length=500)
    active: bool = True

    @field_validator("url")
    @classmethod
    def _https_and_public(cls, v: HttpUrl) -> HttpUrl:
        """Webhook URLs go through the same SSRF guard as connector egress.

        Contract §8 requires egress allow-listing and SSRF guarding. A webhook
        subscription is outbound egress a tenant controls, so it is exactly the
        surface that guard exists for: a subscription pointed at
        169.254.169.254 turns our delivery worker into a cloud-metadata proxy.
        `packages/connectors/egress_guard.py` (05 §B4) is reused verbatim
        rather than reimplemented — one guard, one set of bypass tests.
        """
        from packages.connectors.egress_guard import assert_public_https
        assert_public_https(str(v))
        return v


class WebhookSubscriptionOut(BaseModel):
    id: UUID
    url: HttpUrl
    event_types: list[EventType]
    description: str | None
    active: bool
    # The signing secret is returned ONCE, at creation, then never again.
    secret: str | None = None
    secret_prefix: str
    created_at: datetime
    last_delivery_at: datetime | None
    consecutive_failures: int
    disabled_at: datetime | None
    disabled_reason: str | None
```

Storage reuses `tenant_secrets` for the signing key (envelope-encrypted per
contract §8) and `outbox_events` as the source of truth for what to deliver.
This is deliberate: contract §6 already mandates the transactional outbox for
outbound integration events, so a webhook delivery is a *reader* of the outbox,
not a parallel event path. A booking that commits has its event in the outbox
in the same transaction; there is no window where the booking exists and the
event does not.

### 6.2 Payload and signature

```
POST https://tenant.example.com/hooks/receptionai
Content-Type: application/json
User-Agent: ReceptionAi-Webhooks/1.0
X-ReceptionAi-Event-Id: 0192f4c3-8a1e-7c2d-9f01-3b6e5d4a7c88
X-ReceptionAi-Event-Type: booking.appointment.confirmed
X-ReceptionAi-Delivery-Id: 0192f4c4-1b2c-7d3e-8a09-1f2e3d4c5b6a
X-ReceptionAi-Attempt: 1
X-ReceptionAi-Timestamp: 1786032000
X-ReceptionAi-Signature: v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
```

```jsonc
{
  "event_id": "0192f4c3-8a1e-7c2d-9f01-3b6e5d4a7c88",
  "type": "booking.appointment.confirmed",
  "version": 1,
  "occurred_at": "2026-08-05T12:00:00Z",
  "correlation_id": "0192f4c0-...",
  "causation_id": "0192f4bf-...",
  "tenant_id": "0192f3a0-...",
  "data": {
    "booking_id": "0192f4c2-...",
    "reference": "BK-7F3K2Q",
    "status": "confirmed",
    "service_id": "0192f3b1-...",
    "starts_at": "2026-08-12T14:00:00Z",
    "ends_at": "2026-08-12T14:30:00Z",
    "location_id": "0192f3a5-...",
    "resource_ids": ["0192f3a0-..."],
    "party_size": 1,
    "customer_name": "J. Okonkwo",
    "customer_phone_e164": "+447700900123",
    "price_total": "85.0000",
    "currency": "GBP"
  }
}
```

The envelope is contract §6's event shape verbatim — `event_id`, `tenant_id`,
`occurred_at`, `type`, `version`, `correlation_id`, `causation_id`, `payload`
(named `data` on the wire, which is the one rename, for JSON-consumer
familiarity). `tenant_id` appears in the body even though the receiver knows
which tenant they are, because a tenant with several ReceptionAi accounts
pointing at one endpoint needs to disambiguate.

**PII in payloads is opt-in per subscription.** `customer_name` and
`customer_phone_e164` appear only when the subscription sets
`include_pii: true`, which requires the tenant to have accepted a data-processing
acknowledgement. Default is off, and the fields are absent rather than null so
a receiver cannot mistake redaction for a missing value.

```python
# packages/webhooks/signing.py
import hashlib
import hmac
import time

SIGNATURE_VERSION = "v1"
_TOLERANCE_SECONDS = 300


def sign(payload: bytes, secret: bytes, *, timestamp: int) -> str:
    """HMAC-SHA256 over `{timestamp}.{body}`.

    The timestamp is INSIDE the signed material, not merely a header. A
    signature over the body alone is infinitely replayable: an attacker who
    captures one delivery can resend it forever and it verifies. Binding the
    timestamp into the MAC means a replay must either reuse the original
    timestamp — which the receiver rejects as stale — or alter it, which breaks
    the signature. This is the Stripe/Slack construction, and it is standard
    because the naive version is broken.
    """
    mac = hmac.new(secret, f"{timestamp}.".encode() + payload, hashlib.sha256)
    return f"{SIGNATURE_VERSION}={mac.hexdigest()}"


def verify(payload: bytes, secret: bytes, header: str, timestamp: int) -> bool:
    """Reference implementation shipped in the tenant-facing docs."""
    if abs(time.time() - timestamp) > _TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(sign(payload, secret, timestamp=timestamp), header)
```

Rotation: `POST /api/v1/webhook-subscriptions/{id}/rotate-secret` returns a new
secret and keeps the old one valid for 24 hours. During overlap, deliveries
carry **two** signature values in `X-ReceptionAi-Signature`, comma-separated —
`v1=<new>,v1=<old>` — so a receiver that verifies either passes. Without an
overlap window, rotation is a guaranteed outage for every tenant.

### 6.3 Retry and backoff

Delivery is a Temporal workflow on the `notifications` task queue (contract §3
partitions queues by workload class, and a webhook storm must not starve
booking confirmations).

| Attempt | Delay after previous |
|---|---|
| 1 | immediate |
| 2 | 10 s |
| 3 | 60 s |
| 4 | 5 min |
| 5 | 30 min |
| 6 | 2 h |
| 7 | 6 h |
| 8 | 24 h |

Eight attempts over roughly 33 hours, each with full jitter (`delay *
random()`), which spreads a fleet of retries that would otherwise re-converge
after a shared outage.

```python
# apps/worker/workflows/webhook_delivery.py
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="WebhookDeliveryWorkflow")
class WebhookDeliveryWorkflow:
    """One workflow per delivery.

    workflow_id = f"{tenant_id}:webhook:{delivery_id}" per contract §3 — which
    gives idempotent start for free: the outbox dispatcher can re-enqueue the
    same delivery after a crash and Temporal rejects the duplicate rather than
    delivering twice.

    Why a workflow rather than a retrying activity: the backoff spans 33 hours.
    An activity retry policy holds a worker slot and its heartbeat for that
    entire window. A workflow sleeping on a durable timer holds nothing —
    the schedule survives worker restarts, deploys, and the whole cluster
    rolling.
    """

    @workflow.run
    async def run(self, delivery_id: str, tenant_id: str) -> str:
        backoff = [0, 10, 60, 300, 1800, 7200, 21600, 86400]
        for attempt, delay in enumerate(backoff, start=1):
            if delay:
                await workflow.sleep(timedelta(seconds=delay))
            result = await workflow.execute_activity(
                "deliver_webhook",
                args=[delivery_id, tenant_id, attempt],
                start_to_close_timeout=timedelta(seconds=30),
                # No activity-level retry: the workflow IS the retry policy, and
                # two nested retry mechanisms produce a schedule nobody can
                # reason about or explain to a tenant.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if result["outcome"] == "delivered":
                return "delivered"
            if result["outcome"] == "permanent_failure":
                # 400/401/403/410 from the receiver. Retrying a 410 Gone for 33
                # hours is pure waste — the endpoint is telling us to stop.
                await workflow.execute_activity(
                    "record_permanent_failure", args=[delivery_id, tenant_id],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                return "permanent_failure"

        await workflow.execute_activity(
            "record_exhausted", args=[delivery_id, tenant_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return "exhausted"
```

**Auto-disable.** After 100 consecutive failures across deliveries, or 7
consecutive days with zero successes, the subscription is disabled, `disabled_at`
and `disabled_reason` are set, and the tenant is notified by email and an
in-portal banner. Delivering into a black hole indefinitely burns our egress
budget and makes the delivery log useless for the tenant who *is* watching.

**Ordering is not guaranteed.** Deliveries are independent workflows, so a
`cancelled` event can arrive before the `confirmed` it supersedes. Every payload
carries `occurred_at` and the entity's `version`, and the tenant-facing docs
state plainly that receivers must be prepared to reorder. Promising ordering
would mean a per-tenant serial queue, where one slow receiver stalls every
subsequent event for that tenant — a far worse failure than out-of-order
delivery.

### 6.4 Delivery log and replay

```
GET  /api/v1/webhook-deliveries?subscription_id=&status=&event_type=&cursor=
POST /api/v1/webhook-deliveries/{id}/replay
```

Every attempt is recorded: request headers (signature redacted), response
status, response body truncated to 2 KB, duration, attempt number, error.
Retention 30 days.

```jsonc
// GET /api/v1/webhook-deliveries → CursorPage<WebhookDelivery>
{
  "items": [{
    "id": "0192f4c4-...",
    "subscription_id": "0192f4b0-...",
    "event_id": "0192f4c3-...",
    "event_type": "booking.appointment.confirmed",
    "status": "failed",
    "attempts": 3,
    "next_attempt_at": "2026-08-05T12:06:00Z",
    "last_attempt": {
      "at": "2026-08-05T12:01:00Z",
      "duration_ms": 30001,
      "response_status": null,
      "error": "timeout after 30000ms"
    }
  }],
  "next_cursor": "eyJ2IjoxLCJ0IjoiMjAyNi0wOC0wNVQxMjowMTowMFoi...",
  "has_more": true
}
```

Replay re-sends the **stored payload** with a fresh timestamp, fresh signature,
a new `X-ReceptionAi-Delivery-Id`, and `X-ReceptionAi-Attempt: 1`. It keeps the
original `event_id`, which is how a receiver deduplicates: the delivery is new,
the event is not. Replay is rate-limited to 100 per hour per tenant — it is a
recovery tool, not a load generator, and an unbounded replay button is a way
for a tenant to DDoS their own endpoint.

---

# PART B — ADMIN PORTAL

## 7. App Router structure

### 7.1 The route tree

```
apps/web/
├── app/
│   ├── layout.tsx                       # RSC. <html>, fonts, ThemeProvider, Toaster
│   ├── globals.css                      # Tailwind + design tokens (§14.2)
│   ├── error.tsx                        # client boundary; reports to Sentry
│   ├── not-found.tsx
│   │
│   ├── (auth)/                          # unauthenticated group — no shell, no nav
│   │   ├── layout.tsx                   # RSC. Centered card, no tenant context
│   │   ├── login/page.tsx               # client: email+password, OIDC buttons
│   │   ├── complete/page.tsx            # client: OIDC landing → exchange → redirect
│   │   ├── mfa/page.tsx                 # client: TOTP / WebAuthn challenge
│   │   ├── step-up/page.tsx             # client: re-auth for aal=2 routes
│   │   ├── select-tenant/page.tsx       # client: the `mts` picker
│   │   ├── forgot/page.tsx
│   │   └── reset/page.tsx
│   │
│   ├── (onboarding)/
│   │   ├── layout.tsx                   # RSC. Progress rail, no main nav
│   │   └── setup/
│   │       ├── page.tsx                 # redirect → first incomplete step
│   │       └── [step]/page.tsx          # client: manifest-driven wizard (§10.1)
│   │
│   ├── (app)/                           # THE AUTHENTICATED SHELL
│   │   ├── layout.tsx                   # RSC. Session fetch, TenantProvider, nav
│   │   ├── page.tsx                     # dashboard (§10.2)
│   │   │
│   │   ├── conversations/
│   │   │   ├── layout.tsx               # RSC. Filter rail
│   │   │   ├── page.tsx                 # list + filters
│   │   │   ├── live/page.tsx            # live monitoring (§13.2)
│   │   │   └── [id]/
│   │   │       ├── page.tsx             # transcript viewer (§10.11)
│   │   │       └── recording/page.tsx   # call playback (§10.12)
│   │   │
│   │   ├── bookings/
│   │   │   ├── page.tsx                 # calendar + list toggle
│   │   │   ├── new/page.tsx             # manual booking (dynamic intake form)
│   │   │   └── [id]/page.tsx            # detail, reschedule, cancel
│   │   │
│   │   ├── catalogue/
│   │   │   ├── layout.tsx               # RSC. Tabs: services | resources | types
│   │   │   ├── services/page.tsx
│   │   │   ├── services/[id]/page.tsx
│   │   │   ├── resources/page.tsx
│   │   │   ├── resources/[id]/page.tsx
│   │   │   └── resource-types/page.tsx
│   │   │
│   │   ├── schedule/
│   │   │   ├── hours/page.tsx           # availability rules (§10.4)
│   │   │   ├── holidays/page.tsx
│   │   │   └── exceptions/page.tsx
│   │   │
│   │   ├── policies/page.tsx            # booking policies (§10.7)
│   │   │
│   │   ├── knowledge/
│   │   │   ├── page.tsx                 # sources list + upload
│   │   │   ├── sources/[id]/page.tsx    # documents, ingestion status
│   │   │   ├── documents/[id]/page.tsx  # chunk inspector
│   │   │   └── review/page.tsx          # flagged-chunk queue
│   │   │
│   │   ├── agent/
│   │   │   ├── layout.tsx               # RSC. Tabs: config | agents | voice | test
│   │   │   ├── page.tsx                 # AI config (§10.8)
│   │   │   ├── agents/[key]/page.tsx    # per-agent prompt + tools
│   │   │   ├── voice/page.tsx           # voice config (§10.9)
│   │   │   └── playground/page.tsx      # streaming test console
│   │   │
│   │   ├── integrations/
│   │   │   ├── page.tsx                 # catalogue + installed
│   │   │   ├── [id]/page.tsx            # connector config + sync state
│   │   │   └── channels/page.tsx
│   │   │
│   │   ├── analytics/
│   │   │   ├── layout.tsx               # RSC. Shared date-range control
│   │   │   ├── page.tsx                 # overview (§12)
│   │   │   ├── conversations/page.tsx
│   │   │   ├── bookings/page.tsx
│   │   │   ├── costs/page.tsx
│   │   │   └── quality/page.tsx         # retrieval quality + evals
│   │   │
│   │   ├── settings/
│   │   │   ├── layout.tsx               # RSC. Settings sub-nav
│   │   │   ├── business/page.tsx        # business profile (§10.3)
│   │   │   ├── locations/page.tsx
│   │   │   ├── members/page.tsx         # users & roles (§10.15)
│   │   │   ├── roles/page.tsx
│   │   │   ├── api-keys/page.tsx
│   │   │   ├── webhooks/page.tsx
│   │   │   ├── audit/page.tsx           # audit log (§10.16)
│   │   │   └── billing/page.tsx         # billing (§10.17)
│   │   │
│   │   └── v/[navKey]/page.tsx          # MANIFEST-DEFINED NAV ITEMS (§7.3)
│   │
│   └── api/
│       ├── auth/session/route.ts        # BFF: cookie ↔ access token
│       ├── auth/refresh/route.ts        # BFF: proxies refresh, rotates cookie
│       ├── auth/logout/route.ts
│       └── health/route.ts
│
├── components/
│   ├── forms/                           # THE DYNAMIC RENDERER (§9)
│   ├── ui/                              # shadcn/ui primitives
│   ├── charts/                          # Recharts wrappers (§12)
│   ├── conversations/
│   ├── booking/
│   └── shell/                           # nav, tenant switcher, command palette
│
├── lib/
│   ├── api/                             # generated client + fetch wrapper
│   ├── auth/                            # session, permissions, guards
│   ├── query/                           # React Query setup + key factory (§11)
│   ├── forms/                           # predicate evaluator, validation (§9)
│   └── realtime/                        # SSE/WS hooks (§13)
│
└── e2e/                                 # Playwright (§15.3)
```

### 7.2 Server vs client boundaries

The rule, stated once and applied everywhere: **a component is a Server
Component unless it needs state, effects, event handlers, or a browser API.**
`"use client"` appears in the tree at the smallest possible number of places
and never on a layout that wraps a large subtree.

| Concern | Where | Why |
|---|---|---|
| HTML shell, fonts, theme class | `app/layout.tsx` (RSC) | Static. Nothing interactive. |
| Session fetch + tenant resolution | `app/(app)/layout.tsx` (RSC) | Runs on the server, so the token never has to be in client JS before first paint, and an unauthenticated user is redirected before any HTML ships. |
| Providers (`QueryClientProvider`, `TenantProvider`) | `components/shell/Providers.tsx` (`"use client"`) | React Context is client-only. This is a thin leaf that takes serialized props from the RSC layout. |
| Navigation, tenant switcher, command palette | client | Event handlers. |
| Every data grid, form, chart | client | React Query, form state, Recharts (which needs `ResizeObserver`). |
| Filter rails, static tabs | RSC where possible | A tab strip that only renders `<Link>`s does not need to be a client component. |

**Judgment call — data fetching is client-side React Query, not RSC `fetch`.**
This is the significant architectural decision in Part B and it deserves the
argument.

RSC data fetching is genuinely better for a mostly-static, read-heavy site. The
admin portal is neither. It is a long-lived application session where:

- The same data appears on many screens and must stay consistent (`services`
  feeds the catalogue page, the booking form's service picker, the analytics
  filter, and the policy editor). React Query's shared cache does that with one
  fetch; four RSC pages each `fetch` independently.
- Optimistic updates are pervasive (§11.3). RSC's model is server action →
  revalidate → re-render, which is a full round trip before the UI moves. On a
  toggle switch that is a visible lag.
- Streaming (SSE/WS) is inherently client-side and must write into the same
  cache the rest of the UI reads. A live conversation updating an RSC-rendered
  list requires a full route refresh per event.
- **The token model forbids it.** 05 §A1 puts the access token in memory,
  explicitly "never `localStorage`", with a 10-minute TTL and rotation on the
  refresh cookie. An RSC `fetch` to the API needs that token *on the server*,
  which means either forwarding the refresh cookie to a Node process that
  exchanges it on every render — turning every navigation into a token
  operation and creating a second place refresh-rotation races — or storing the
  access token where the server can read it, which is the cookie-storage
  pattern 05 rejected.

So the server renders the shell, the layout resolves the session once, and
everything below is client-side React Query against `/api/v1` directly. The
four `app/api/*` routes are a minimal BFF for the cookie↔token exchange only —
never a proxy for data.

### 7.3 Where the tenant context lives

Once, in the authenticated layout, on the server:

```tsx
// app/(app)/layout.tsx  — Server Component
import { redirect } from "next/navigation";
import { cookies } from "next/headers";

import { AppShell } from "@/components/shell/AppShell";
import { Providers } from "@/components/shell/Providers";
import { getServerSession } from "@/lib/auth/server";

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Reads the HttpOnly refresh cookie, exchanges it once, and returns the
  // hydrated principal. Runs on every navigation into the group, before any
  // HTML is streamed — an unauthenticated visitor never receives portal markup.
  const session = await getServerSession(cookies());

  if (!session) redirect("/login");
  if (session.tenants.length > 1 && !session.activeTenantId) {
    redirect("/select-tenant");
  }
  if (!session.onboardingComplete) redirect("/setup");

  // Manifest-driven navigation, already permission-filtered server-side (§8.4).
  const navigation = await getNavigation(session);

  return (
    <Providers session={session}>
      <AppShell navigation={navigation}>{children}</AppShell>
    </Providers>
  );
}
```

```tsx
// components/shell/Providers.tsx
"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { ReactNode, createContext, useContext, useMemo, useRef } from "react";

import { makeQueryClient } from "@/lib/query/client";
import type { Session } from "@/lib/auth/types";

interface TenantContextValue {
  tenantId: string;
  tenantName: string;
  plan: string;
  locale: string;
  timezone: string;
  currency: string;
  verticalKey: string;
  manifestVersion: string;
  permissions: ReadonlySet<string>;
  roles: readonly string[];
  aal: number;
  userId: string;
}

const TenantContext = createContext<TenantContextValue | null>(null);

export function Providers({
  session,
  children,
}: {
  session: Session;
  children: ReactNode;
}) {
  // One QueryClient per TENANT, not per app. Switching tenants constructs a
  // new client and the old cache is garbage — see §11.2. This is the primary
  // structural defense against a cross-tenant cache read, and it is why the
  // client is keyed here rather than created at module scope.
  const clientRef = useRef<{ tenantId: string; client: ReturnType<typeof makeQueryClient> } | null>(
    null,
  );
  if (clientRef.current?.tenantId !== session.activeTenantId) {
    clientRef.current = {
      tenantId: session.activeTenantId,
      client: makeQueryClient(session.activeTenantId),
    };
  }

  const tenant = useMemo<TenantContextValue>(
    () => ({
      tenantId: session.activeTenantId,
      tenantName: session.tenantName,
      plan: session.plan,
      locale: session.locale,
      timezone: session.timezone,
      currency: session.currency,
      verticalKey: session.verticalKey,
      manifestVersion: session.manifestVersion,
      permissions: new Set(session.permissions),
      roles: session.roles,
      aal: session.aal,
      userId: session.userId,
    }),
    [session],
  );

  return (
    <QueryClientProvider client={clientRef.current.client}>
      <TenantContext.Provider value={tenant}>{children}</TenantContext.Provider>
    </QueryClientProvider>
  );
}

export function useTenant(): TenantContextValue {
  const ctx = useContext(TenantContext);
  if (!ctx) {
    // Not a nicety. A component rendering outside the provider would fall back
    // to *some* default tenant, and that default would be wrong for somebody.
    // Throwing turns a data-leak class of bug into a render error caught by
    // the first test that mounts the component.
    throw new Error("useTenant must be used inside the authenticated shell");
  }
  return ctx;
}
```

**The tenant id is never sent to the API.** `useTenant().tenantId` exists for
exactly three purposes: React Query cache keys (§11.2), display, and the
switcher. Every request derives the tenant from the bearer token, per contract
§10. A lint rule enforces it:

```js
// eslint.config.js (excerpt)
{
  rules: {
    "no-restricted-syntax": ["error", {
      selector: "Property[key.name=/^tenant_?[Ii]d$/][parent.parent.callee.property.name=/^(get|post|patch|put|delete)$/]",
      message: "Never send tenant_id to the API. The token carries it (contract §10); the server rejects X-Tenant-* with 400.",
    }],
  },
}
```

### 7.4 Manifest-defined routes

`app/(app)/v/[navKey]/page.tsx` is what makes "a new vertical needs no new
screens" true for *navigation*, not just forms. `GET /api/v1/navigation`
returns `UiSchema.admin_nav` (02 §2): each `NavItem` has a `view` of
`entity_list | entity_detail | form | calendar | dashboard | settings` and a
`ref`. One route resolves all of them:

```tsx
// app/(app)/v/[navKey]/page.tsx
"use client";

import { notFound } from "next/navigation";
import { use } from "react";

import { CalendarView } from "@/components/views/CalendarView";
import { DashboardView } from "@/components/views/DashboardView";
import { EntityDetailView } from "@/components/views/EntityDetailView";
import { EntityListView } from "@/components/views/EntityListView";
import { FormView } from "@/components/views/FormView";
import { SettingsView } from "@/components/views/SettingsView";
import { useNavItem } from "@/lib/query/navigation";

const VIEWS = {
  entity_list: EntityListView,
  entity_detail: EntityDetailView,
  form: FormView,
  calendar: CalendarView,
  dashboard: DashboardView,
  settings: SettingsView,
} as const;

export default function ManifestView({ params }: { params: Promise<{ navKey: string }> }) {
  const { navKey } = use(params);
  const item = useNavItem(navKey);

  if (item.isPending) return <ViewSkeleton />;
  if (!item.data) notFound();

  const View = VIEWS[item.data.view];
  if (!View) {
    // A manifest schema MINOR that adds a view kind must not white-screen a
    // portal that has not shipped it yet. Degrade to a readable message with
    // a link to the equivalent first-party screen. Same philosophy as the
    // unknown-widget fallback in §9.6.
    return <UnsupportedView view={item.data.view} navKey={navKey} />;
  }
  return <View navKey={navKey} refKey={item.data.ref} />;
}
```

A dental clinic's manifest nav item `{key: "recall_list", view: "entity_list",
ref: "recall_due"}` and a hotel's `{key: "housekeeping", view: "entity_list",
ref: "rooms_dirty"}` both render through `EntityListView` with no code change.
The `ref` resolves to a server-defined saved query, so the *data* is manifest
configuration too, not just the chrome.

### 7.5 Route groups and why the shell is split three ways

`(auth)`, `(onboarding)`, and `(app)` are separate groups because each has a
different notion of "who is here":

- `(auth)` has **no** principal. Fetching a session in its layout would be a
  guaranteed 401 on every login page render.
- `(onboarding)` has a principal but **no complete tenant** — no vertical
  installed, no locations, no services. Rendering the main nav would show a
  dozen links to empty screens, and half the nav comes from a manifest that has
  not been chosen yet.
- `(app)` has both, and its layout can therefore assert both rather than
  defensively handling null everywhere below.

Splitting them means the `(app)` layout's `session` is non-null by
construction, and every component beneath it can rely on `useTenant()` without
a guard.

---

## 8. Auth integration and RBAC-driven UI

### 8.1 The token dance

05 §A1 puts the access token in memory with a 10-minute TTL and the refresh
token in an `HttpOnly` cookie path-scoped to `/api/v1/auth`. That path scope is
the constraint that shapes everything here: the browser sends the refresh
cookie only to `/api/v1/auth/*`, which is exactly right and means the Next.js
server can read it only if it is on the same origin.

**Deployment assumption, stated because it is load-bearing:** `apps/web` and
`apps/api` are served from the same origin, with `/api/v1/*` reverse-proxied to
FastAPI. If they were on different origins, the refresh cookie would be
third-party, and third-party cookies are being removed from browsers. Same
origin is not a convenience here; it is the only configuration in which 05's
cookie design works in 2026.

```ts
// lib/auth/token-store.ts
type Listener = (token: string | null) => void;

/**
 * The access token lives here and nowhere else.
 *
 * Not localStorage, not sessionStorage, not a cookie — 05 §A1 is explicit
 * ("Memory only — never localStorage"). Any XSS can read localStorage; the
 * same XSS can read this variable too, but only while the page is alive,
 * whereas localStorage survives the tab closing and hands an attacker a token
 * that outlives their injection.
 *
 * Module-scoped rather than React state because the fetch wrapper is called
 * from outside React (React Query's queryFn, the SSE reconnect loop) and must
 * not depend on a render having happened.
 */
let accessToken: string | null = null;
let expiresAt = 0;
let refreshInFlight: Promise<string | null> | null = null;
const listeners = new Set<Listener>();

export function setAccessToken(token: string | null, expiresInSeconds = 0): void {
  accessToken = token;
  expiresAt = token ? Date.now() + expiresInSeconds * 1000 : 0;
  listeners.forEach((l) => l(token));
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function onTokenChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Refresh 60 seconds before expiry so an in-flight request never races it. */
export function needsRefresh(): boolean {
  return !accessToken || Date.now() > expiresAt - 60_000;
}

export async function refreshAccessToken(): Promise<string | null> {
  // Single-flight. Without this, a page that mounts eight queries at once
  // fires eight concurrent refreshes; 05 §A1.4 revokes the whole family on
  // refresh-token reuse, so the 2nd through 8th would look like an attack and
  // log the user out. This is not an optimization — the naive version is a
  // guaranteed logout on every cold page load.
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const res = await fetch("/api/auth/refresh", {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": readCsrfCookie() },
      });
      if (!res.ok) {
        setAccessToken(null);
        return null;
      }
      const body = (await res.json()) as { access_token: string; expires_in: number };
      setAccessToken(body.access_token, body.expires_in);
      return body.access_token;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

function readCsrfCookie(): string {
  return (
    document.cookie
      .split("; ")
      .find((c) => c.startsWith("rai_csrf="))
      ?.split("=")[1] ?? ""
  );
}
```

The fetch wrapper, which is the only thing in the app that talks to `/api/v1`:

```ts
// lib/api/client.ts
import {
  getAccessToken,
  needsRefresh,
  refreshAccessToken,
} from "@/lib/auth/token-store";

export class ApiProblem extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly title: string,
    readonly detail: string | undefined,
    readonly requestId: string | undefined,
    readonly extensions: Record<string, unknown>,
  ) {
    super(title);
    this.name = "ApiProblem";
  }

  static async from(res: Response): Promise<ApiProblem> {
    let body: Record<string, unknown> = {};
    try {
      body = await res.json();
    } catch {
      /* a proxy returned HTML; fall through to the generic shape below */
    }
    const { type, title, status, code, detail, instance, request_id, ...rest } = body;
    return new ApiProblem(
      res.status,
      (code as string) ?? "unknown_error",
      (title as string) ?? res.statusText,
      detail as string | undefined,
      request_id as string | undefined,
      rest,
    );
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit & { idempotencyKey?: string; ifMatch?: string } = {},
): Promise<T> {
  if (needsRefresh()) await refreshAccessToken();

  const send = async (): Promise<Response> => {
    const headers = new Headers(init.headers);
    const token = getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
    if (init.ifMatch) headers.set("If-Match", init.ifMatch);
    // NEVER a tenant header. The API returns 400 tenant_header_not_permitted
    // (05 §A3.4) and it would be a contract §10 violation regardless.
    return fetch(path, { ...init, headers, credentials: "same-origin" });
  };

  let res = await send();

  // One retry on 401. The token may have been revoked mid-flight (permission
  // change bumps prm_v → token_stale) rather than merely expired, and a single
  // refresh distinguishes "stale token, recoverable" from "session gone".
  if (res.status === 401) {
    const refreshed = await refreshAccessToken();
    if (refreshed) res = await send();
  }

  if (!res.ok) {
    const problem = await ApiProblem.from(res);
    if (problem.status === 401) {
      window.dispatchEvent(new CustomEvent("auth:expired"));
    }
    if (problem.code === "step_up_required") {
      window.dispatchEvent(
        new CustomEvent("auth:step-up", { detail: { returnTo: location.pathname } }),
      );
    }
    throw problem;
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
```

Two events rather than direct navigation: the fetch wrapper is called from
React Query's `queryFn`, from streaming reconnect loops, and from tests. Making
it call `router.push` couples a transport utility to a router instance it does
not have. A listener in `AppShell` turns `auth:expired` into a redirect and
`auth:step-up` into a modal.

### 8.2 The BFF refresh route

```ts
// app/api/auth/refresh/route.ts
import { NextRequest, NextResponse } from "next/server";

const API = process.env.API_INTERNAL_URL!;

export async function POST(req: NextRequest) {
  // Double-submit CSRF check (§2.11) before anything touches the upstream.
  const cookieToken = req.cookies.get("rai_csrf")?.value;
  const headerToken = req.headers.get("X-CSRF-Token");
  if (!cookieToken || cookieToken !== headerToken) {
    return NextResponse.json(
      { code: "csrf_mismatch", title: "CSRF token mismatch" },
      { status: 403 },
    );
  }

  const upstream = await fetch(`${API}/api/v1/auth/refresh`, {
    method: "POST",
    headers: {
      // Forward only the refresh cookie. Forwarding the whole Cookie header
      // would hand the API every unrelated cookie on the origin, including any
      // set by a marketing script — a needless widening of what the API sees.
      cookie: `rai_refresh=${req.cookies.get("rai_refresh")?.value ?? ""}`,
      "user-agent": req.headers.get("user-agent") ?? "",
      "x-forwarded-for": req.headers.get("x-forwarded-for") ?? "",
    },
  });

  if (!upstream.ok) {
    const res = NextResponse.json(
      { code: "refresh_failed", title: "Session expired" },
      { status: 401 },
    );
    res.cookies.delete("rai_refresh");
    res.cookies.delete("rai_csrf");
    return res;
  }

  const body = (await upstream.json()) as {
    access_token: string;
    refresh_token: string;
    expires_in: number;
  };

  // The access token goes in the JSON body, to be held in memory. Only the
  // rotated refresh token becomes a cookie. Putting the access token in a
  // cookie would make it an ambient credential and reopen CSRF on every
  // guarded route — precisely what 05's memory-only rule prevents.
  const res = NextResponse.json({
    access_token: body.access_token,
    expires_in: body.expires_in,
  });
  res.cookies.set("rai_refresh", body.refresh_token, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/api/v1/auth",
    maxAge: 30 * 24 * 3600,
  });
  return res;
}
```

### 8.3 Protected routes

Three layers, mirroring 05 §A0's three-layer authorization model — and for the
same reason: any one of them failing must not be a breach.

**Layer 1, middleware — cheap, wrong-by-default-safe.** Redirects an
unauthenticated visitor before the app runs. It does not validate the token
(the Edge runtime has no business verifying Ed25519 signatures against a JWKS
it must fetch); it checks for the presence of a refresh cookie.

```ts
// middleware.ts
import { NextRequest, NextResponse } from "next/server";

const PUBLIC = [/^\/login/, /^\/auth\//, /^\/forgot/, /^\/reset/, /^\/api\/health/];

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (PUBLIC.some((p) => p.test(pathname))) return NextResponse.next();

  if (!req.cookies.has("rai_refresh")) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|woff2)$).*)"],
};
```

**Layer 2, the RSC layout** — `getServerSession` in `app/(app)/layout.tsx`
actually exchanges the cookie and gets a validated principal. A forged or
expired cookie fails here.

**Layer 3, the API** — every request is authorized independently. This is the
only layer that matters for security. Layers 1 and 2 exist so that an
unauthorized user sees a login page instead of a portal full of empty panels
and 403 toasts.

The corollary, stated explicitly because it is the failure mode of every
RBAC-driven UI: **hiding a control is UX, not authorization.** Every action a
hidden button would have performed is independently rejected by `requires()` on
the server. §15 has a Playwright test that drives a `viewer` session directly
to a mutation endpoint and asserts a 403.

### 8.4 RBAC-driven UI

```tsx
// lib/auth/permissions.ts
"use client";

import { useTenant } from "@/components/shell/Providers";

/** Mirrors packages/core/authz/permissions.py. Generated, not hand-written —
 *  see the codegen note below. */
export type Perm =
  | "tenant:read" | "tenant:update" | "tenant:delete"
  | "members:read" | "members:invite" | "members:update_role" | "members:remove"
  | "roles:manage"
  | "billing:read" | "billing:manage" | "usage:read"
  | "conversations:read" | "conversations:read_pii" | "conversations:export"
  | "conversations:delete" | "conversations:barge_in"
  | "bookings:read" | "bookings:create" | "bookings:update"
  | "bookings:cancel" | "bookings:override"
  | "agents:read" | "agents:update" | "agents:publish"
  | "knowledge:read" | "knowledge:write" | "knowledge:publish"
  | "connectors:read" | "connectors:manage"
  | "channels:read" | "channels:manage" | "secrets:write"
  | "apikeys:read" | "apikeys:manage" | "audit:read"
  | "webhooks:manage" | "dsr:manage";

/** 05 §A2's STEP_UP_REQUIRED, mirrored so the UI can pre-empt the 403. */
const STEP_UP: ReadonlySet<Perm> = new Set([
  "tenant:delete", "billing:manage", "apikeys:manage", "secrets:write",
  "conversations:export", "roles:manage", "dsr:manage", "members:update_role",
]);

export function usePermissions() {
  const { permissions, aal } = useTenant();

  const can = (...required: Perm[]) => required.every((p) => permissions.has(p));
  const canAny = (...required: Perm[]) => required.some((p) => permissions.has(p));

  /**
   * Distinguishes "you cannot" from "you must re-authenticate first".
   *
   * This distinction is the difference between a disabled button with no
   * explanation and one that says "Confirm your identity to continue". A UI
   * that treats aal<2 as a flat denial makes step-up undiscoverable — the user
   * has the permission and the button is greyed out with no path forward.
   */
  const gate = (...required: Perm[]): "allowed" | "denied" | "step_up" => {
    if (!can(...required)) return "denied";
    if (required.some((p) => STEP_UP.has(p)) && aal < 2) return "step_up";
    return "allowed";
  };

  return { can, canAny, gate, permissions, aal };
}
```

`Perm` is generated from the Python enum at build time, so a permission added
in 05 that the UI does not know about is a TypeScript error rather than a
silently-always-false check:

```python
# tools/gen_permissions_ts.py — runs in `make codegen`, verified in CI
from packages.core.authz.permissions import STEP_UP_REQUIRED, Perm

lines = ["// GENERATED by tools/gen_permissions_ts.py. Do not edit.",
         "export type Perm ="]
lines += [f'  | "{p.value}"' for p in Perm]
lines.append(";")
lines.append("export const STEP_UP: ReadonlySet<Perm> = new Set([")
lines += [f'  "{p.value}",' for p in sorted(STEP_UP_REQUIRED)]
lines.append("]);")
print("\n".join(lines))
```

The gate component:

```tsx
// components/auth/Can.tsx
"use client";

import { ReactNode } from "react";

import { Perm, usePermissions } from "@/lib/auth/permissions";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export function Can({
  perm,
  mode = "hide",
  fallback = null,
  children,
}: {
  perm: Perm | Perm[];
  mode?: "hide" | "disable";
  fallback?: ReactNode;
  children: ReactNode;
}) {
  const { gate } = usePermissions();
  const required = Array.isArray(perm) ? perm : [perm];
  const verdict = gate(...required);

  if (verdict === "allowed") return <>{children}</>;

  if (verdict === "step_up") {
    return <StepUpWrapper required={required}>{children}</StepUpWrapper>;
  }

  if (mode === "hide") return <>{fallback}</>;

  // `disable` mode: the control stays visible but inert, with a reason.
  //
  // Which mode to use is a real decision, not a preference:
  //   hide    — when the feature is irrelevant to the role (a viewer has no
  //             mental model of "publish", so a disabled Publish button is
  //             noise).
  //   disable — when the user knows the feature exists and its absence would
  //             read as a bug (the Cancel button on a booking detail page).
  //
  // The disabled control must never be a bare <button disabled>: disabled
  // elements are not focusable and screen readers skip them, so the reason is
  // unreachable by keyboard. aria-disabled + a focusable wrapper keeps it
  // announced. See §14.4.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="inline-block cursor-not-allowed opacity-50">
          <span aria-disabled="true" className="pointer-events-none">
            {children}
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        Your role does not permit this. Required: {required.join(", ")}.
      </TooltipContent>
    </Tooltip>
  );
}

function StepUpWrapper({ required, children }: { required: Perm[]; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="outline"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent("auth:step-up", {
                detail: { returnTo: location.pathname, required },
              }),
            )
          }
        >
          Confirm identity to continue
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        This action needs a second authentication factor.
      </TooltipContent>
    </Tooltip>
  );
}
```

### 8.5 What a viewer sees versus an owner

Concrete, because "RBAC-driven UI" is meaningless as an abstraction. Using
05 §A2's role grid:

| Screen | `viewer` | `agent` | `manager` | `admin` | `owner` |
|---|---|---|---|---|---|
| **Nav** | Dashboard, Conversations, Bookings, Analytics | + nothing new | + Catalogue, Schedule, Policies, Knowledge | + Agent, Integrations, Settings | + Billing |
| **Dashboard** | read-only tiles; no "Add booking" | + "Add booking" | + capacity tiles | + cost tile | + margin tile |
| **Conversations list** | list, filters | + live monitor | + live monitor | + export ⚡ | + delete |
| **Transcript** | masked PII, "Masked" badge with the reason | unmasked if `read_pii` | unmasked | unmasked | unmasked |
| **Live conversation** | not in nav | Take over / Whisper / Transfer | same | same | same |
| **Booking detail** | fields read-only, no action bar | Reschedule, Cancel | + Override policy | + Override | + Override |
| **Services** | not in nav | not in nav | full CRUD | full CRUD | full CRUD |
| **Knowledge** | not in nav | not in nav | upload, edit | + publish, review queue | same |
| **Agent config** | not in nav | not in nav | not in nav | edit + publish | same |
| **Members** | not in nav | not in nav | invite (agent/viewer only) | + change roles ⚡ | + remove owner |
| **API keys** | not in nav | not in nav | not in nav | create ⚡ | create ⚡ |
| **Billing** | not in nav | not in nav | not in nav | view usage | manage ⚡ |
| **Audit log** | not in nav | not in nav | not in nav | read | read |

⚡ = renders the step-up affordance when `aal < 2`.

The masked-PII badge is worth calling out. A `viewer` opening a transcript sees
the text with contact details replaced, plus an explicit badge:

```tsx
{transcript.pii_masked && (
  <Alert variant="info" className="mb-4">
    <ShieldIcon aria-hidden />
    <AlertTitle>Personal details are hidden</AlertTitle>
    <AlertDescription>
      Names, phone numbers and email addresses are masked for your role.
      An administrator can grant <code>conversations:read_pii</code>.
    </AlertDescription>
  </Alert>
)}
```

Without the badge, `[REDACTED]` in a transcript reads as a transcription
failure and generates a support ticket about "the AI not hearing the customer".

### 8.6 Permission changes mid-session

05 §A1 puts `prm_v` (permission-set version) in the access token and returns
`401 token_stale` when it diverges. The portal handles it as a *refresh*, not a
logout:

```ts
// In apiFetch's error path
if (problem.code === "token_stale") {
  await refreshAccessToken();            // mints a token with the new prm_v
  window.dispatchEvent(new CustomEvent("auth:permissions-changed"));
}
```

`AppShell` listens and re-fetches `GET /api/v1/auth/session`, which updates
`TenantContext.permissions` and re-renders every `<Can>` in the tree. An admin
demoted to viewer loses their action buttons within one request round trip
without being thrown out of the application — and the server was already
rejecting their writes from the moment `prm_v` bumped.

---

## 9. The manifest-driven form renderer

This is the component the "no code changes for a new vertical" promise rests
on. 02 §7 compiles manifest + tenant overlay + live option sources into a
`RenderDocument`; everything below consumes it. **The portal never sees a
manifest.**

02 §7.3 sketches the top-level component. This section is the complete
implementation: types, predicate evaluator, validation mirror, focus
management, and the degradation path.

### 9.1 Types

Generated from the Pydantic response model, never hand-written — the same
codegen path as every other API type (§4.3).

```ts
// lib/forms/types.ts — GENERATED from the RenderDocument response model
export interface RenderDocument {
  render_version: "1";
  form_key: string;
  title: string;
  description?: string;
  purpose: "intake" | "admin" | "confirmation" | "feedback";
  submit_label: string;
  sections: RenderSection[];
  fields: RenderField[];
  rules: RenderRule[];
  meta: {
    manifest_version: string;
    vertical_key: string;
    resolved_hash: string;
    theme: { accent?: string; density: "comfortable" | "compact" };
  };
}

export interface RenderSection {
  key: string;
  title: string;
  collapsible: boolean;
  field_keys: string[];
}

export type WidgetName =
  | "input" | "textarea" | "radio" | "checkbox" | "switch" | "combobox"
  | "date_picker" | "time_picker" | "slider" | "rating" | "file_drop"
  | "signature_pad" | "address";

export interface RenderField {
  key: string;
  widget: WidgetName;
  input_type:
    | "text" | "email" | "tel" | "number" | "date" | "time"
    | "datetime-local" | "checkbox" | "file" | "custom";
  label: string;
  help?: string;
  placeholder?: string;
  required: boolean;
  disabled: boolean;
  default_value: unknown;
  options?: Array<{ value: string; label: string; description?: string }>;
  options_endpoint?: string;
  layout: { col_span: number; order: number };
  mask?: string;
  autocomplete?: string;
  constraints: {
    min_length?: number;
    max_length?: number;
    pattern?: string;
    minimum?: number;
    maximum?: number;
    min_date?: string;   // ABSOLUTE ISO — resolved server-side (02 §7.2)
    max_date?: string;
    accept?: string[];
    max_file_size_bytes?: number;
  };
  pii: "none" | "contact" | "identity" | "financial" | "health" | "government_id";
  sensitive: boolean;
}

export interface RenderRule {
  id: string;
  kind: "visible_when" | "required_when" | "validate";
  target_field: string;
  predicate: SerializedPredicate;
  message?: string;
  severity?: "error" | "warning" | "info";
  server_only: boolean;
}

/** 02 §1.9's closed vocabulary, verbatim. There is deliberately no `expr`. */
export type PredicateOp =
  | "eq" | "ne" | "gt" | "gte" | "lt" | "lte" | "in" | "not_in"
  | "contains" | "starts_with" | "matches" | "is_empty" | "is_not_empty"
  | "before" | "after" | "within" | "older_than" | "younger_than";

export type SerializedPredicate =
  | { all: SerializedPredicate[] }
  | { any: SerializedPredicate[] }
  | { not: SerializedPredicate }
  | { field: string; op: PredicateOp; value?: unknown }
  | { checks: Array<{ check: string; [k: string]: unknown }> };

export type FormValues = Record<string, unknown>;

export interface FieldError {
  message: string;
  severity: "error" | "warning" | "info";
  ruleId?: string;
}
```

### 9.2 The predicate evaluator

Every operator in 02 §1.9, and nothing else. Anything unrecognised evaluates to
`true` for visibility and `false` for requiredness — the safe direction in each
case, argued below.

```ts
// lib/forms/predicate.ts
import type { PredicateOp, SerializedPredicate, FormValues } from "./types";

/**
 * Evaluate a manifest predicate against current form values.
 *
 * This mirrors `packages/verticals/predicate.py`. The two implementations MUST
 * agree, and §15.1 keeps them agreeing with a shared fixture file that both
 * test suites read — a Python test and a Vitest test over the same table of
 * (predicate, values, expected) triples. Divergence between them is the single
 * most likely defect in the entire form system, because it presents as "the
 * field was hidden but the server says it is required", with nothing in either
 * codebase looking wrong on its own.
 */
export function evaluatePredicate(
  predicate: SerializedPredicate | undefined,
  values: FormValues,
  opts: { onUnknown: boolean },
): boolean {
  if (!predicate) return true;

  if ("all" in predicate) {
    return predicate.all.every((p) => evaluatePredicate(p, values, opts));
  }
  if ("any" in predicate) {
    return predicate.any.some((p) => evaluatePredicate(p, values, opts));
  }
  if ("not" in predicate) {
    return !evaluatePredicate(predicate.not, values, opts);
  }
  if ("checks" in predicate) {
    // Named server-side checks (email_deliverable, no_duplicate_active_booking).
    // The compiler marks these server_only, so they should never reach here;
    // if one does, it is a compiler bug and we must not guess an answer.
    return opts.onUnknown;
  }
  if ("field" in predicate) {
    return evaluateLeaf(predicate.field, predicate.op, predicate.value, values, opts);
  }
  return opts.onUnknown;
}

function evaluateLeaf(
  field: string,
  op: PredicateOp,
  expected: unknown,
  values: FormValues,
  opts: { onUnknown: boolean },
): boolean {
  const actual = resolvePath(field, values);

  switch (op) {
    case "is_empty":
      return isEmpty(actual);
    case "is_not_empty":
      return !isEmpty(actual);
    case "eq":
      return looseEq(actual, expected);
    case "ne":
      return !looseEq(actual, expected);
    case "gt":
      return compare(actual, expected) > 0;
    case "gte":
      return compare(actual, expected) >= 0;
    case "lt":
      return compare(actual, expected) < 0;
    case "lte":
      return compare(actual, expected) <= 0;
    case "in":
      return Array.isArray(expected) && expected.some((e) => looseEq(actual, e));
    case "not_in":
      return Array.isArray(expected) && !expected.some((e) => looseEq(actual, e));
    case "contains":
      if (Array.isArray(actual)) return actual.some((a) => looseEq(a, expected));
      return String(actual ?? "").includes(String(expected));
    case "starts_with":
      return String(actual ?? "").startsWith(String(expected));
    case "matches":
      // The manifest validator already rejected nested unbounded quantifiers
      // (02 §1.9's ReDoS guard), so compiling here is safe. A malformed regex
      // that slipped through must not take down the form.
      try {
        return new RegExp(String(expected)).test(String(actual ?? ""));
      } catch {
        return opts.onUnknown;
      }
    case "before":
      return toTime(actual) !== null && toTime(expected) !== null
        ? toTime(actual)! < toTime(expected)!
        : opts.onUnknown;
    case "after":
      return toTime(actual) !== null && toTime(expected) !== null
        ? toTime(actual)! > toTime(expected)!
        : opts.onUnknown;
    case "within": {
      // value is an ISO 8601 duration, e.g. "P30D": actual is within
      // [now - d, now + d].
      const ms = parseDuration(String(expected));
      const t = toTime(actual);
      if (ms === null || t === null) return opts.onUnknown;
      return Math.abs(t - Date.now()) <= ms;
    }
    case "older_than": {
      const ms = parseDuration(String(expected));
      const t = toTime(actual);
      if (ms === null || t === null) return opts.onUnknown;
      return Date.now() - t > ms;
    }
    case "younger_than": {
      const ms = parseDuration(String(expected));
      const t = toTime(actual);
      if (ms === null || t === null) return opts.onUnknown;
      return Date.now() - t < ms;
    }
    default: {
      // A manifest schema MINOR added an operator this build predates. Never
      // throw: one unknown operator must not white-screen a whole intake form.
      const _exhaustive: never = op;
      void _exhaustive;
      return opts.onUnknown;
    }
  }
}

function resolvePath(path: string, values: FormValues): unknown {
  return path.split(".").reduce<unknown>(
    (acc, part) =>
      acc && typeof acc === "object" ? (acc as Record<string, unknown>)[part] : undefined,
    values,
  );
}

function isEmpty(v: unknown): boolean {
  if (v === null || v === undefined) return true;
  if (typeof v === "string") return v.trim() === "";
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

/**
 * Loose equality across the string/number boundary ONLY.
 *
 * A select's value is always a string ("2"), while a manifest predicate may
 * compare it against a number (2) because YAML parsed it as one. Refusing to
 * bridge that would make `{field: party_size, op: gte, value: 5}` silently
 * false forever — a conditional field that never appears, with nothing in the
 * console. JS's `==` would also equate `"" == 0` and `null == undefined`,
 * which is why this is explicit rather than `==`.
 */
function looseEq(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || a === undefined || b === null || b === undefined) return false;
  if (typeof a === "number" && typeof b === "string") return a === Number(b);
  if (typeof a === "string" && typeof b === "number") return Number(a) === b;
  if (typeof a === "boolean" && typeof b === "string") return String(a) === b;
  return false;
}

function compare(a: unknown, b: unknown): number {
  const ta = toTime(a);
  const tb = toTime(b);
  if (ta !== null && tb !== null) return ta - tb;
  const na = Number(a);
  const nb = Number(b);
  if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  return String(a ?? "").localeCompare(String(b ?? ""));
}

function toTime(v: unknown): number | null {
  if (v instanceof Date) return v.getTime();
  if (typeof v !== "string") return null;
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : t;
}

/** ISO 8601 duration → milliseconds. Years and months use calendar averages,
 *  which is correct for "older_than P18Y" within a day. Exactness there is not
 *  achievable client-side anyway: the server resolved the authoritative
 *  boundary into constraints.min_date/max_date against the tenant timezone. */
const DURATION = /^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/;

export function parseDuration(iso: string): number | null {
  const m = DURATION.exec(iso);
  if (!m) return null;
  const [, y, mo, w, d, h, mi, s] = m.map((x) => (x ? Number(x) : 0));
  return (
    y * 31_557_600_000 + mo * 2_629_800_000 + w * 604_800_000 +
    d * 86_400_000 + h * 3_600_000 + mi * 60_000 + s * 1_000
  );
}
```

**Why the unknown-operator direction differs by rule kind.** `visible_when`
with an unevaluable predicate resolves to **visible**: showing a field the user
did not need is a small annoyance, while hiding a field the server requires
produces an unsubmittable form with no visible cause. `required_when` resolves
to **not required**: the server re-evaluates every rule on submit (02 §7.4 rule
1), so a missed client-side requirement surfaces as a server error attached to
the right field, whereas a false requirement blocks a legitimate submit with an
error the user cannot clear.

### 9.3 Validation mirror

```ts
// lib/forms/validation.ts
import { evaluatePredicate } from "./predicate";
import type { FieldError, FormValues, RenderDocument, RenderField } from "./types";

/**
 * Client-side validation that mirrors the server's rules.
 *
 * 02 §7.4 rule 1: "The client is a convenience, the server is the authority."
 * Everything here is re-run on submit. Its job is latency — telling the user
 * about a bad phone number while their cursor is still in the field — not
 * correctness.
 *
 * Rules with server_only are NEVER evaluated. The compiler marks a rule
 * server_only when it needs a network call (email_deliverable,
 * no_duplicate_active_booking). Guessing at them produces a form that says
 * "valid" and then fails on submit, which is worse than saying nothing.
 */
export function validate(
  doc: RenderDocument,
  values: FormValues,
  visibility: Record<string, boolean>,
): Record<string, FieldError[]> {
  const errors: Record<string, FieldError[]> = {};
  const push = (key: string, err: FieldError) => {
    (errors[key] ??= []).push(err);
  };

  const requiredByRule = new Set<string>();
  for (const rule of doc.rules) {
    if (rule.server_only || rule.kind !== "required_when") continue;
    if (evaluatePredicate(rule.predicate, values, { onUnknown: false })) {
      requiredByRule.add(rule.target_field);
    }
  }

  for (const field of doc.fields) {
    // A hidden field is not validated and its value is not submitted (§9.4).
    // Validating an invisible field yields an error the user cannot see, on a
    // control they cannot reach — a permanently unsubmittable form.
    if (visibility[field.key] === false) continue;

    const value = values[field.key];
    const isRequired = field.required || requiredByRule.has(field.key);

    if (isRequired && isBlank(value)) {
      push(field.key, { message: `${field.label} is required.`, severity: "error" });
      continue; // Do not pile format errors on an empty required field.
    }
    if (isBlank(value)) continue;

    checkConstraints(field, value, push);
  }

  for (const rule of doc.rules) {
    if (rule.server_only || rule.kind !== "validate") continue;
    if (visibility[rule.target_field] === false) continue;
    // A `validate` predicate asserts what must be TRUE; failing it is an error.
    if (!evaluatePredicate(rule.predicate, values, { onUnknown: true })) {
      push(rule.target_field, {
        message: rule.message ?? "This value is not valid.",
        severity: rule.severity ?? "error",
        ruleId: rule.id,
      });
    }
  }

  return errors;
}

function checkConstraints(
  field: RenderField,
  value: unknown,
  push: (key: string, err: FieldError) => void,
): void {
  const c = field.constraints;
  const err = (message: string) => push(field.key, { message, severity: "error" });

  if (typeof value === "string") {
    if (c.min_length !== undefined && value.length < c.min_length) {
      err(`${field.label} must be at least ${c.min_length} characters.`);
    }
    if (c.max_length !== undefined && value.length > c.max_length) {
      err(`${field.label} must be at most ${c.max_length} characters.`);
    }
    if (c.pattern) {
      try {
        if (!new RegExp(c.pattern).test(value)) {
          err(`${field.label} is not in the expected format.`);
        }
      } catch {
        /* invalid pattern: the server validated it; do not block the user */
      }
    }
    if (field.input_type === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) {
      // Deliberately permissive. The RFC 5322 grammar is not usefully
      // expressible in a regex, and every attempt rejects addresses that work.
      // "Has an @ and a dot after it" catches typos; the server's
      // email_deliverable check (server_only) is the real answer.
      err("Enter a valid email address.");
    }
    if (field.input_type === "tel" && !/^\+?[0-9\s().-]{6,20}$/.test(value)) {
      err("Enter a valid phone number.");
    }
  }

  if (field.input_type === "number") {
    const n = typeof value === "number" ? value : Number(value);
    if (Number.isNaN(n)) {
      err(`${field.label} must be a number.`);
    } else {
      if (c.minimum !== undefined && n < c.minimum) {
        err(`${field.label} must be at least ${c.minimum}.`);
      }
      if (c.maximum !== undefined && n > c.maximum) {
        err(`${field.label} must be at most ${c.maximum}.`);
      }
    }
  }

  if (c.min_date || c.max_date) {
    // These arrived ABSOLUTE. 02 §7.2 resolves relative tokens ("today-P18Y")
    // server-side against the tenant's location timezone, precisely so a
    // browser in Auckland and a server in Virginia agree on what "today" is.
    // Re-deriving them here would reintroduce the bug the server fixed.
    const t = Date.parse(String(value));
    if (!Number.isNaN(t)) {
      if (c.min_date && t < Date.parse(c.min_date)) {
        err(`${field.label} must be on or after ${formatDate(c.min_date)}.`);
      }
      if (c.max_date && t > Date.parse(c.max_date)) {
        err(`${field.label} must be on or before ${formatDate(c.max_date)}.`);
      }
    }
  }

  if (value instanceof File) {
    if (c.max_file_size_bytes && value.size > c.max_file_size_bytes) {
      err(`File must be smaller than ${formatBytes(c.max_file_size_bytes)}.`);
    }
    if (c.accept?.length && !c.accept.some((a) => matchesAccept(value, a))) {
      err(`File must be one of: ${c.accept.join(", ")}.`);
    }
  }
}

function isBlank(v: unknown): boolean {
  return v === null || v === undefined || v === "" ||
    (Array.isArray(v) && v.length === 0);
}

function matchesAccept(file: File, accept: string): boolean {
  if (accept.startsWith(".")) return file.name.toLowerCase().endsWith(accept.toLowerCase());
  if (accept.endsWith("/*")) return file.type.startsWith(accept.slice(0, -1));
  return file.type === accept;
}
```

### 9.4 The renderer

```tsx
// components/forms/DynamicForm.tsx
"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { evaluatePredicate } from "@/lib/forms/predicate";
import { validate } from "@/lib/forms/validation";
import type {
  FieldError, FormValues, RenderDocument, RenderField, WidgetName,
} from "@/lib/forms/types";

import { FieldShell } from "./FieldShell";
import { FormSection } from "./FormSection";
import { UnsupportedWidget } from "./widgets/UnsupportedWidget";
import {
  AddressWidget, CheckboxWidget, ComboboxWidget, DatePickerWidget,
  FileDropWidget, RadioWidget, RatingWidget, SignatureWidget, SliderWidget,
  SwitchWidget, TextWidget, TextareaWidget, TimePickerWidget,
} from "./widgets";

export interface WidgetProps {
  field: RenderField;
  value: unknown;
  errors: FieldError[];
  disabled: boolean;
  describedBy: string;
  labelId: string;
  inputId: string;
  onChange: (value: unknown) => void;
  onBlur: () => void;
}

/**
 * The closed widget set from 02 §7.1.
 *
 * Closed is the point: new manifest FIELD TYPES map onto existing widgets by
 * default (02 §7.2's _DEFAULT_WIDGET), so adding `"nhs_number"` as a field type
 * needs a backend line, not a frontend release. A frontend change is required
 * only for a genuinely new INTERACTION, which is rare and deserves review.
 */
const WIDGETS: Record<WidgetName, React.FC<WidgetProps>> = {
  input: TextWidget,
  textarea: TextareaWidget,
  radio: RadioWidget,
  checkbox: CheckboxWidget,
  switch: SwitchWidget,
  combobox: ComboboxWidget,
  date_picker: DatePickerWidget,
  time_picker: TimePickerWidget,
  slider: SliderWidget,
  rating: RatingWidget,
  file_drop: FileDropWidget,
  signature_pad: SignatureWidget,
  address: AddressWidget,
};

export interface SubmitResult {
  ok: boolean;
  fieldErrors?: Record<string, string[]>;
  newDocument?: RenderDocument;   // 409 manifest_changed
}

export function DynamicForm({
  doc,
  initialValues,
  onSubmit,
  submitting = false,
  readOnly = false,
}: {
  doc: RenderDocument;
  initialValues?: FormValues;
  onSubmit: (values: FormValues, resolvedHash: string) => Promise<SubmitResult>;
  submitting?: boolean;
  readOnly?: boolean;
}) {
  const [values, setValues] = useState<FormValues>(() => seed(doc, initialValues));
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [serverErrors, setServerErrors] = useState<Record<string, string[]>>({});
  const [attempted, setAttempted] = useState(false);
  const formId = useId();
  const summaryRef = useRef<HTMLDivElement>(null);
  const fieldRefs = useRef(new Map<string, HTMLElement>());

  const visibility = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const field of doc.fields) map[field.key] = true;
    for (const rule of doc.rules) {
      if (rule.kind !== "visible_when" || rule.server_only) continue;
      // Unknown predicate → visible. See §9.2.
      map[rule.target_field] = evaluatePredicate(rule.predicate, values, {
        onUnknown: true,
      });
    }
    return map;
  }, [doc.rules, doc.fields, values]);

  const clientErrors = useMemo(
    () => validate(doc, values, visibility),
    [doc, values, visibility],
  );

  const errorsFor = useCallback(
    (key: string): FieldError[] => {
      const server = (serverErrors[key] ?? []).map<FieldError>((message) => ({
        message,
        severity: "error",
      }));
      // Server errors always show. Client errors show only after the field has
      // been touched or a submit was attempted — validating an untouched form
      // paints every required field red before the user has typed a character,
      // which reads as "you have already failed".
      const client = touched[key] || attempted ? clientErrors[key] ?? [] : [];
      return [...server, ...client];
    },
    [serverErrors, clientErrors, touched, attempted],
  );

  const blocking = useMemo(
    () =>
      Object.entries(clientErrors).some(
        ([key, errs]) =>
          visibility[key] !== false && errs.some((e) => e.severity === "error"),
      ),
    [clientErrors, visibility],
  );

  const setValue = useCallback((key: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    // Clear the server error for this field the moment the user edits it: a
    // stale server error next to a changed value is actively misleading.
    setServerErrors((prev) => (key in prev ? omit(prev, key) : prev));
  }, []);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setAttempted(true);

    if (blocking) {
      // Focus management (§14.4): move focus to the error summary, not to the
      // first bad field. The summary announces HOW MANY problems there are and
      // lets the user choose where to go. Jumping straight to field one hides
      // the scale of the problem from a screen-reader user.
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }

    // Hidden fields are excluded from the payload, not sent as null. A
    // conditional field that the user never saw has no value — sending null
    // asserts "the user answered nothing", which for a nullable clinical field
    // is a different and wrong claim.
    const payload = Object.fromEntries(
      Object.entries(values).filter(([key]) => visibility[key] !== false),
    );

    const result = await onSubmit(payload, doc.meta.resolved_hash);
    if (!result.ok && result.fieldErrors) {
      setServerErrors(result.fieldErrors);
      requestAnimationFrame(() => summaryRef.current?.focus());
    }
  };

  // 02 §7.4 rule 2: the manifest changed between render and submit. The parent
  // swaps `doc`; we re-seed while preserving values whose fields survived, so
  // a user does not lose ten minutes of typing to a manifest publish.
  useEffect(() => {
    setValues((prev) => {
      const next = seed(doc, undefined);
      for (const field of doc.fields) {
        if (field.key in prev) next[field.key] = prev[field.key];
      }
      return next;
    });
  }, [doc.meta.resolved_hash]); // eslint-disable-line react-hooks/exhaustive-deps

  const groups = doc.sections.length
    ? doc.sections.map((section) => ({
        section,
        fields: section.field_keys
          .map((k) => doc.fields.find((f) => f.key === k))
          .filter((f): f is RenderField => Boolean(f)),
      }))
    : [{ section: null, fields: doc.fields }];

  const visibleErrorCount = attempted
    ? Object.entries(clientErrors).filter(
        ([k, e]) => visibility[k] !== false && e.some((x) => x.severity === "error"),
      ).length + Object.keys(serverErrors).length
    : Object.keys(serverErrors).length;

  return (
    <form
      onSubmit={handleSubmit}
      noValidate                  /* we own validation; native bubbles are unstyleable and unlocalized */
      aria-labelledby={`${formId}-title`}
      data-density={doc.meta.theme.density}
      className="space-y-8"
    >
      <div>
        <h1 id={`${formId}-title`} className="text-2xl font-semibold tracking-tight">
          {doc.title}
        </h1>
        {doc.description && (
          <p className="mt-2 text-muted-foreground">{doc.description}</p>
        )}
      </div>

      <ErrorSummary
        ref={summaryRef}
        count={visibleErrorCount}
        errors={collectSummary(doc, clientErrors, serverErrors, visibility, attempted)}
        onJump={(key) => fieldRefs.current.get(key)?.focus()}
      />

      {groups.map(({ section, fields }) => {
        const anyVisible = fields.some((f) => visibility[f.key] !== false);
        if (!anyVisible) return null;   // an entirely-hidden section renders nothing
        return (
          <FormSection key={section?.key ?? "_default"} section={section}>
            <div className="grid grid-cols-12 gap-x-4 gap-y-6">
              {fields
                .filter((f) => visibility[f.key] !== false)
                .map((field) => {
                  const Widget = WIDGETS[field.widget] ?? UnsupportedWidget;
                  const inputId = `${formId}-${field.key}`;
                  const errs = errorsFor(field.key);
                  return (
                    <div
                      key={field.key}
                      className="col-span-12"
                      style={{ ["--col-span" as string]: field.layout.col_span }}
                      data-col-span={field.layout.col_span}
                    >
                      <FieldShell
                        field={field}
                        inputId={inputId}
                        errors={errs}
                        registerRef={(el) => {
                          if (el) fieldRefs.current.set(field.key, el);
                          else fieldRefs.current.delete(field.key);
                        }}
                      >
                        {({ describedBy, labelId }) => (
                          <Widget
                            field={field}
                            value={values[field.key]}
                            errors={errs}
                            disabled={readOnly || field.disabled || submitting}
                            inputId={inputId}
                            labelId={labelId}
                            describedBy={describedBy}
                            onChange={(v) => setValue(field.key, v)}
                            onBlur={() =>
                              setTouched((t) => ({ ...t, [field.key]: true }))
                            }
                          />
                        )}
                      </FieldShell>
                    </div>
                  );
                })}
            </div>
          </FormSection>
        );
      })}

      {!readOnly && (
        <div className="flex items-center gap-3 border-t pt-6">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Saving…" : doc.submit_label}
          </Button>
          {/* The submit button is NOT disabled on validation errors. A disabled
              submit gives a keyboard or screen-reader user no way to discover
              WHY they cannot proceed — they tab to it, it is inert, and nothing
              is announced. Submitting and surfacing the summary is the
              accessible pattern (WCAG 3.3.1). */}
          <p aria-live="polite" className="text-sm text-muted-foreground">
            {attempted && blocking ? `${visibleErrorCount} problem(s) to fix.` : ""}
          </p>
        </div>
      )}
    </form>
  );
}

function seed(doc: RenderDocument, initial?: FormValues): FormValues {
  const out: FormValues = {};
  for (const field of doc.fields) {
    out[field.key] =
      initial?.[field.key] ?? field.default_value ?? defaultFor(field);
  }
  return out;
}

function defaultFor(field: RenderField): unknown {
  if (field.widget === "checkbox" || field.widget === "switch") return false;
  if (field.widget === "combobox" && field.input_type === "custom") return "";
  return "";
}

function omit<T extends object>(obj: T, key: string): T {
  const { [key as keyof T]: _drop, ...rest } = obj;
  return rest as T;
}
```

`--col-span` plus `data-col-span` rather than an inline `gridColumn`: Tailwind's
JIT cannot generate `col-span-{dynamic}`, and inline styles cannot be
responsive. A CSS rule collapses everything to full width below `md`, which is
what a 4-column manifest layout must do on a phone:

```css
/* globals.css */
@media (min-width: 768px) {
  [data-col-span] { grid-column: span var(--col-span) / span var(--col-span); }
}
```

### 9.5 The field shell — labels, help, errors, PII

```tsx
// components/forms/FieldShell.tsx
"use client";

import { ReactNode, useId } from "react";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import type { FieldError, RenderField } from "@/lib/forms/types";

export function FieldShell({
  field,
  inputId,
  errors,
  registerRef,
  children,
}: {
  field: RenderField;
  inputId: string;
  errors: FieldError[];
  registerRef: (el: HTMLElement | null) => void;
  children: (ids: { describedBy: string; labelId: string }) => ReactNode;
}) {
  const helpId = useId();
  const errorId = useId();
  const labelId = `${inputId}-label`;

  const hasError = errors.some((e) => e.severity === "error");
  // Order matters: aria-describedby is announced in the order given, and the
  // error must come before the hint so a screen-reader user hears the problem
  // first rather than after a sentence of guidance.
  const describedBy =
    [errors.length ? errorId : null, field.help ? helpId : null]
      .filter(Boolean)
      .join(" ");

  return (
    <div ref={registerRef as never} className="space-y-2" tabIndex={-1}>
      <div className="flex items-baseline justify-between gap-2">
        <Label
          id={labelId}
          htmlFor={inputId}
          className={cn(hasError && "text-destructive")}
        >
          {field.label}
          {field.required && (
            <>
              <span aria-hidden="true" className="ml-0.5 text-destructive">*</span>
              <span className="sr-only"> (required)</span>
            </>
          )}
        </Label>
        {field.sensitive && (
          <span
            className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
            title="This value is masked on screen and excluded from analytics, error reports and autosaved drafts."
          >
            Sensitive
          </span>
        )}
      </div>

      {children({ describedBy, labelId })}

      {field.help && (
        <p id={helpId} className="text-sm text-muted-foreground">
          {field.help}
        </p>
      )}

      {errors.length > 0 && (
        <ul id={errorId} className="space-y-1">
          {errors.map((error, i) => (
            <li
              key={i}
              className={cn(
                "text-sm",
                error.severity === "error" && "text-destructive",
                error.severity === "warning" && "text-amber-600 dark:text-amber-500",
                error.severity === "info" && "text-muted-foreground",
              )}
            >
              {error.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

02 §7.4 rule 4 requires `sensitive: true` fields to be excluded from
client-side analytics, error reports and autosave drafts, "enforced by a lint
rule on the widget components, not by convention". Here is that enforcement:

```ts
// eslint-rules/no-sensitive-leak.ts
/**
 * Bans passing a RenderField's value into any of the known sinks without a
 * `field.sensitive` guard. The sinks are analytics, Sentry, and the autosave
 * store — the three places a form value can escape the component tree.
 */
const SINKS = new Set(["track", "captureException", "setContext", "saveDraft", "addBreadcrumb"]);

export const noSensitiveLeak = {
  meta: { type: "problem", messages: {
    leak: "Value from a RenderField reaches {{sink}}() without a `field.sensitive` guard. 02 §7.4 rule 4 forbids this.",
  }},
  create(context) {
    return {
      CallExpression(node) {
        const callee = node.callee;
        const name = callee.type === "MemberExpression" ? callee.property.name : callee.name;
        if (!SINKS.has(name)) return;
        const source = context.getSourceCode().getText(node);
        if (/\bvalues?\b|\bfieldValue\b/.test(source) && !/sensitive/.test(source)) {
          context.report({ node, messageId: "leak", data: { sink: name } });
        }
      },
    };
  },
};
```

And the autosave store honours it structurally, so the lint rule is a second
line rather than the only one:

```ts
// lib/forms/autosave.ts
export function draftPayload(doc: RenderDocument, values: FormValues): FormValues {
  const sensitive = new Set(doc.fields.filter((f) => f.sensitive).map((f) => f.key));
  // A drafted card number or diagnosis in localStorage survives logout and is
  // readable by any XSS on the origin. Dropping the keys entirely — rather than
  // masking them — means there is nothing to leak.
  return Object.fromEntries(
    Object.entries(values).filter(([k]) => !sensitive.has(k)),
  );
}
```

### 9.6 Degradation on an unknown widget

```tsx
// components/forms/widgets/UnsupportedWidget.tsx
"use client";

import { AlertTriangle } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { WidgetProps } from "../DynamicForm";

/**
 * Rendered when `field.widget` is not in WIDGETS.
 *
 * This happens exactly once in the system's life cycle: a manifest schema
 * MINOR adds a widget, the backend ships, and a portal build from before that
 * change renders a form containing it. 02 §7.1 makes WidgetName a closed set
 * specifically to make this rare — but "rare" is not "never", and the two apps
 * deploy independently.
 *
 * The three wrong answers, and why:
 *
 *   1. Throw. One unrecognised widget white-screens an entire intake form and
 *      the tenant cannot take a booking. Catastrophic, and the blast radius is
 *      wildly out of proportion to the cause.
 *   2. Render nothing. The field silently vanishes. If it is required, the
 *      server rejects the submit with an error pointing at a field that is not
 *      on screen — an unsubmittable form with no visible cause, which is the
 *      single worst outcome available.
 *   3. Render a generic input and say nothing. The user types free text into
 *      what should have been a constrained control and the server rejects it
 *      with a format error they cannot act on.
 *
 * What we do instead: render a text control (so the field is fillable and its
 * value reaches the server), label it honestly, and report it. `input_type`
 * still arrives from the server even for an unknown widget, so the control is
 * usually close to right — an unknown widget over `input_type: "date"` still
 * gets a native date picker.
 */
export function UnsupportedWidget({
  field, value, disabled, inputId, describedBy, onChange, onBlur,
}: WidgetProps) {
  if (process.env.NODE_ENV !== "production") {
    console.warn(
      `[DynamicForm] Unknown widget "${field.widget}" for field "${field.key}". ` +
        `Falling back to a text control. This portal build predates the ` +
        `manifest schema that produced this form.`,
    );
  }

  // Sampled telemetry, not per-render: a form with three unknown widgets
  // re-rendering on every keystroke would generate thousands of events.
  useReportOnce("form.unknown_widget", {
    widget: field.widget,
    field_key: field.key,
    // No value, no label — the label may be tenant-authored PII-adjacent text.
  });

  const Control = field.input_type === "text" && !field.constraints.max_length
    ? Textarea
    : Input;

  return (
    <div className="space-y-1.5">
      <Control
        id={inputId}
        type={field.input_type === "custom" ? "text" : field.input_type}
        value={typeof value === "string" ? value : String(value ?? "")}
        placeholder={field.placeholder}
        disabled={disabled}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
      />
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>
          This field normally uses a control this version of the portal does not
          have yet. You can still enter a value; refresh later for the full
          experience.
        </span>
      </p>
    </div>
  );
}
```

The same philosophy applies one level up in `field.input_type` (an unknown
value falls back to `"text"`) and one level up again in the view resolver
(§7.4's `UnsupportedView`). The rule is uniform: **degrade to something usable
and honest, never to a blank screen or a lie.**

### 9.7 A representative widget

```tsx
// components/forms/widgets/ComboboxWidget.tsx
"use client";

import { useQuery } from "@tanstack/react-query";
import { Check, ChevronsUpDown } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { apiFetch } from "@/lib/api/client";
import { useTenant } from "@/components/shell/Providers";
import { cn } from "@/lib/utils";
import type { WidgetProps } from "../DynamicForm";

type Option = { value: string; label: string; description?: string };

export function ComboboxWidget({
  field, value, disabled, inputId, describedBy, onChange, onBlur, errors,
}: WidgetProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { tenantId } = useTenant();

  const multiple = Array.isArray(value);
  const selected = useMemo(
    () => (multiple ? (value as string[]) : value ? [String(value)] : []),
    [value, multiple],
  );

  // 02 §7.4 rule 5: option sets above 200 entries arrive as an endpoint rather
  // than inline. Both paths land in the same `options` array below, so the rest
  // of this component does not care which it was.
  const remote = useQuery({
    // Tenant-scoped key: the option list for `staff` is tenant data (§11.2).
    queryKey: [tenantId, "form-options", field.options_endpoint, search],
    queryFn: () =>
      apiFetch<{ items: Option[] }>(
        `${field.options_endpoint}?q=${encodeURIComponent(search)}&limit=50`,
      ),
    enabled: Boolean(field.options_endpoint) && open,
    staleTime: 60_000,
    placeholderData: (prev) => prev,   // no flicker while typing
  });

  const options: Option[] = field.options ?? remote.data?.items ?? [];
  const labelOf = (v: string) => options.find((o) => o.value === v)?.label ?? v;
  const hasError = errors.some((e) => e.severity === "error");

  const toggle = (optionValue: string) => {
    if (multiple) {
      const next = selected.includes(optionValue)
        ? selected.filter((v) => v !== optionValue)
        : [...selected, optionValue];
      onChange(next);
    } else {
      onChange(optionValue === value ? "" : optionValue);
      setOpen(false);
    }
    onBlur();
  };

  return (
    <Popover open={open} onOpenChange={(o) => { setOpen(o); if (!o) onBlur(); }}>
      <PopoverTrigger asChild>
        <Button
          id={inputId}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-describedby={describedBy}
          aria-invalid={hasError || undefined}
          disabled={disabled}
          className={cn(
            "w-full justify-between font-normal",
            !selected.length && "text-muted-foreground",
            hasError && "border-destructive",
          )}
        >
          <span className="truncate">
            {selected.length === 0
              ? field.placeholder ?? "Select…"
              : selected.length === 1
                ? labelOf(selected[0])
                : `${selected.length} selected`}
          </span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" aria-hidden />
        </Button>
      </PopoverTrigger>

      <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
        <Command shouldFilter={!field.options_endpoint}>
          <CommandInput
            placeholder="Search…"
            value={search}
            onValueChange={setSearch}
          />
          <CommandList>
            <CommandEmpty>
              {remote.isFetching ? "Searching…" : "No matches."}
            </CommandEmpty>
            <CommandGroup>
              {options.map((option) => (
                <CommandItem
                  key={option.value}
                  value={option.label}
                  onSelect={() => toggle(option.value)}
                >
                  <Check
                    className={cn(
                      "mr-2 h-4 w-4",
                      selected.includes(option.value) ? "opacity-100" : "opacity-0",
                    )}
                    aria-hidden
                  />
                  <span className="flex flex-col">
                    <span>{option.label}</span>
                    {option.description && (
                      <span className="text-xs text-muted-foreground">
                        {option.description}
                      </span>
                    )}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
```

`shouldFilter={!field.options_endpoint}` is the whole inline-vs-remote
distinction: with inline options, `cmdk` filters locally and typing is
instant; with a remote endpoint, the server filters and local filtering would
hide results the server just returned.

### 9.8 Submitting, and the 409 re-render

```tsx
// components/forms/FormPage.tsx
"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { DynamicForm, SubmitResult } from "@/components/forms/DynamicForm";
import { ApiProblem, apiFetch } from "@/lib/api/client";
import { useTenant } from "@/components/shell/Providers";
import { toast } from "@/components/ui/use-toast";
import type { FormValues, RenderDocument } from "@/lib/forms/types";

export function FormPage({
  formKey,
  context = "admin",
  submitTo,
  initialValues,
}: {
  formKey: string;
  context?: "admin" | "intake";
  submitTo: string;
  initialValues?: FormValues;
}) {
  const { tenantId, locale } = useTenant();
  const [override, setOverride] = useState<RenderDocument | null>(null);

  const query = useQuery({
    queryKey: [tenantId, "form", formKey, context, locale],
    queryFn: () =>
      apiFetch<RenderDocument>(
        `/api/v1/forms/${formKey}?context=${context}&locale=${locale}`,
      ),
    // The compiled document changes only on a manifest publish, and the portal
    // is notified of those (§13.3). Five minutes is a safe floor.
    staleTime: 5 * 60_000,
  });

  const mutation = useMutation({
    mutationFn: async ({ values, hash }: { values: FormValues; hash: string }) =>
      apiFetch<{ id: string }>(submitTo, {
        method: "POST",
        body: JSON.stringify({ ...values, resolved_hash: hash }),
        idempotencyKey: crypto.randomUUID(),
      }),
  });

  const doc = override ?? query.data;
  if (query.isPending) return <FormSkeleton />;
  if (query.isError) return <FormLoadError error={query.error} />;
  if (!doc) return null;

  const handleSubmit = async (
    values: FormValues,
    resolvedHash: string,
  ): Promise<SubmitResult> => {
    try {
      await mutation.mutateAsync({ values, hash: resolvedHash });
      toast({ title: "Saved" });
      return { ok: true };
    } catch (error) {
      if (!(error instanceof ApiProblem)) throw error;

      if (error.code === "manifest_changed") {
        // 02 §7.4 rule 2. The response carries the NEW render document, so we
        // re-render immediately rather than round-tripping for it. DynamicForm
        // preserves values for fields that survived the change, so the user
        // keeps their typing.
        const next = error.extensions.render_document as RenderDocument;
        setOverride(next);
        toast({
          title: "This form was updated",
          description:
            "An administrator changed the form definition. Your answers were "
            + "kept where the fields still exist. Please review and resubmit.",
          variant: "warning",
        });
        return { ok: false, newDocument: next };
      }

      if (error.code === "validation_failed" || error.code === "policy_violation") {
        // The server's field-level errors are authoritative and may name rules
        // the client never evaluated (server_only). Mapping them onto fields is
        // what closes the loop 02 §7.4 rule 1 opened.
        const problems = (error.extensions.errors ?? []) as Array<{
          field: string;
          message: string;
        }>;
        const byField: Record<string, string[]> = {};
        for (const p of problems) (byField[p.field] ??= []).push(p.message);
        if (!problems.length && error.extensions.field) {
          byField[error.extensions.field as string] = [error.detail ?? error.title];
        }
        return { ok: false, fieldErrors: byField };
      }

      toast({ title: error.title, description: error.detail, variant: "destructive" });
      return { ok: false };
    }
  };

  return (
    <DynamicForm
      doc={doc}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      submitting={mutation.isPending}
    />
  );
}
```

### 9.9 What this buys, restated as a test

Adding the restaurant vertical (roadmap Phase 4's checkpoint) touches:

- `packages/verticals/manifests/restaurant.yaml` — new file
- a connector configuration row
- **zero files under `apps/web/`**

That is testable, and §15.2 tests it: an MSW handler serves a `RenderDocument`
compiled from the restaurant manifest, and the same `DynamicForm` under test
for medical renders a party-size slider, a seating-preference combobox, and a
high-chair switch with no code change. If that test needs a new widget, the
manifest schema was extended in a way 02 §7.2 says should have mapped onto an
existing one, and that is the conversation to have — in `packages/verticals`,
not in `apps/web`.

---

## 10. Page inventory

Seventeen screens. Each entry names the route, the permission, the data it
reads, and a real component sketch where the screen has non-obvious mechanics.
Screens that are a `DataTable` over one endpoint get a short entry — the
interesting ones get code.

### 10.1 Onboarding wizard — `/setup/[step]`

The only screen that is entirely manifest-driven end to end, including its own
step list.

```tsx
// app/(onboarding)/setup/[step]/page.tsx
"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { use } from "react";

import { DynamicForm } from "@/components/forms/DynamicForm";
import { StepRail } from "@/components/onboarding/StepRail";
import { VerticalPicker } from "@/components/onboarding/VerticalPicker";
import { apiFetch } from "@/lib/api/client";
import { useTenant } from "@/components/shell/Providers";
import type { RenderDocument } from "@/lib/forms/types";

interface OnboardingState {
  steps: Array<{
    key: string;
    title: string;
    status: "complete" | "current" | "pending";
    form_key: string | null;      // null = a bespoke step, e.g. vertical choice
    optional: boolean;
  }>;
  current: string;
  vertical_key: string | null;
}

export default function SetupStep({ params }: { params: Promise<{ step: string }> }) {
  const { step } = use(params);
  const { tenantId } = useTenant();
  const router = useRouter();

  // The STEP LIST comes from the server, derived from the installed manifest.
  // A hotel's onboarding asks about room types and rate plans; a dental
  // clinic's asks about providers and insurance. Hardcoding a step array here
  // would be the first place "no new screens per vertical" broke — and it
  // would break invisibly, because the forms would still be dynamic.
  const state = useQuery({
    queryKey: [tenantId, "onboarding"],
    queryFn: () => apiFetch<OnboardingState>("/api/v1/onboarding"),
  });

  const form = useQuery({
    queryKey: [tenantId, "form", state.data?.steps.find((s) => s.key === step)?.form_key],
    queryFn: () =>
      apiFetch<RenderDocument>(
        `/api/v1/forms/${state.data!.steps.find((s) => s.key === step)!.form_key}?context=admin`,
      ),
    enabled: Boolean(state.data?.steps.find((s) => s.key === step)?.form_key),
  });

  const advance = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      apiFetch<{ next: string | null }>(`/api/v1/onboarding/${step}`, {
        method: "POST",
        body: JSON.stringify(values),
        idempotencyKey: `onboarding:${step}`,
      }),
    onSuccess: (result) => {
      // Server decides what is next. A branch — "do you take walk-ins?" gating
      // a whole queue-management step — lives in the manifest, not in a
      // client-side switch that would need editing per vertical.
      router.push(result.next ? `/setup/${result.next}` : "/");
    },
  });

  if (state.isPending) return <StepSkeleton />;

  const current = state.data!.steps.find((s) => s.key === step);
  if (!current) return null;

  return (
    <div className="mx-auto grid max-w-5xl gap-8 md:grid-cols-[220px_1fr]">
      <StepRail steps={state.data!.steps} current={step} />
      <main>
        {current.form_key === null && step === "vertical" ? (
          <VerticalPicker
            onSelect={(key) => advance.mutate({ vertical_key: key })}
          />
        ) : form.data ? (
          <DynamicForm
            doc={form.data}
            submitting={advance.isPending}
            onSubmit={async (values) => {
              await advance.mutateAsync(values);
              return { ok: true };
            }}
          />
        ) : (
          <FormSkeleton />
        )}
        {current.optional && (
          <button
            className="mt-4 text-sm text-muted-foreground underline"
            onClick={() => advance.mutate({})}
          >
            Skip for now
          </button>
        )}
      </main>
    </div>
  );
}
```

Progress persists server-side (`tenant_settings` namespace `onboarding`), so a
tenant who closes the tab resumes where they left off — and so does a colleague
who picks it up on a different machine, which a `localStorage` wizard cannot do.

### 10.2 Dashboard — `/`

Permission: none beyond authentication; each tile is individually gated.

Five tiles, one live strip, one activity feed. Layout in priority order for the
role: an `agent` sees today's schedule first, an `owner` sees revenue and cost
first. Tile order is a function of `roles`, not a fixed grid.

```tsx
// app/(app)/page.tsx
"use client";

import { ActiveCallsStrip } from "@/components/dashboard/ActiveCallsStrip";
import { KpiTile } from "@/components/dashboard/KpiTile";
import { RecentActivity } from "@/components/dashboard/RecentActivity";
import { TodaySchedule } from "@/components/dashboard/TodaySchedule";
import { Can } from "@/components/auth/Can";
import { useDashboard } from "@/lib/query/dashboard";

export default function Dashboard() {
  const { data, isPending } = useDashboard();

  return (
    <div className="space-y-6">
      {/* Only rendered when a call is live. A permanently-present "0 active
          calls" panel trains people to ignore that region of the screen, so
          when it does matter it is invisible. */}
      <ActiveCallsStrip />

      <section aria-labelledby="kpis">
        <h2 id="kpis" className="sr-only">Key metrics</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <KpiTile
            label="Conversations today"
            value={data?.conversations_today}
            delta={data?.conversations_delta}
            loading={isPending}
            sparkline={data?.conversations_spark}
          />
          <KpiTile
            label="Bookings today"
            value={data?.bookings_today}
            delta={data?.bookings_delta}
            loading={isPending}
            sparkline={data?.bookings_spark}
          />
          <KpiTile
            label="Containment rate"
            value={data?.containment_rate}
            format="percent"
            delta={data?.containment_delta}
            loading={isPending}
            hint="Conversations resolved without transferring to a human."
          />
          <Can perm="usage:read" fallback={
            <KpiTile label="Avg. handling time" value={data?.aht_seconds} format="duration" loading={isPending} />
          }>
            <KpiTile
              label="Cost today"
              value={data?.cost_today}
              format="currency"
              delta={data?.cost_delta}
              deltaGoodDirection="down"   // a rising cost is not a win
              loading={isPending}
            />
          </Can>
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <TodaySchedule />
        <RecentActivity />
      </div>
    </div>
  );
}
```

`deltaGoodDirection` exists because a green up-arrow on a rising cost tile is
actively misleading, and every dashboard that omits it eventually ships one.

### 10.3 Business profile — `/settings/business`

Permission: `tenant:read` / `tenant:update`.

Split into three cards: identity (`legal_name`, `display_name`, `slug`),
defaults (`default_timezone`, `default_locale`, `default_currency`), and
compliance (`compliance_profile`, `data_region` — both **read-only** with a
"contact support to change" note, because `data_region` implies a data
migration and `compliance_profile` implies a contract).

Timezone deserves a note. It is a searchable combobox over the IANA database
with the current local time shown next to each zone, because "Europe/London"
means nothing to a receptionist and "Europe/London — 14:32" means everything.
Contract §6 forbids storing local time; this field is the *source* of the zone
every render-time conversion uses, so getting it wrong misaligns every screen
in the product.

### 10.4 Hours & holidays — `/schedule/hours`, `/schedule/holidays`

Permission: `bookings:read` / `bookings:update`.

`availability_rules` (01 §5.6) are stored as weekday arrays plus `TIME` in
**local wall-clock**, with `crosses_midnight` generated. The editor is a weekly
grid, not a form:

```tsx
// components/schedule/WeeklyHoursEditor.tsx
"use client";

import { useMemo } from "react";

import { TimeRangeRow } from "./TimeRangeRow";
import type { AvailabilityRule } from "@/lib/api/types";

const DAYS = [
  { iso: 1, label: "Monday" }, { iso: 2, label: "Tuesday" },
  { iso: 3, label: "Wednesday" }, { iso: 4, label: "Thursday" },
  { iso: 5, label: "Friday" }, { iso: 6, label: "Saturday" },
  { iso: 7, label: "Sunday" },      // ISO 1=Mon..7=Sun, matching 01 §5.6
];

export function WeeklyHoursEditor({
  rules, onChange, timezone, readOnly,
}: {
  rules: AvailabilityRule[];
  onChange: (next: AvailabilityRule[]) => void;
  timezone: string;
  readOnly: boolean;
}) {
  const byDay = useMemo(() => {
    const map = new Map<number, AvailabilityRule[]>(DAYS.map((d) => [d.iso, []]));
    for (const rule of rules) {
      for (const weekday of rule.weekdays) map.get(weekday)?.push(rule);
    }
    for (const list of map.values()) list.sort((a, b) => a.start_time.localeCompare(b.start_time));
    return map;
  }, [rules]);

  return (
    <div className="space-y-1">
      <p className="mb-4 text-sm text-muted-foreground">
        Times are local to this location ({timezone}). They are stored as
        wall-clock times, so they follow daylight saving automatically — 09:00
        stays 09:00 across the change.
      </p>

      {DAYS.map((day) => {
        const ranges = byDay.get(day.iso) ?? [];
        return (
          <div
            key={day.iso}
            className="grid grid-cols-[130px_1fr_auto] items-start gap-3 border-b py-3 last:border-0"
          >
            <span className="pt-2 font-medium">{day.label}</span>

            <div className="space-y-2">
              {ranges.length === 0 ? (
                <span className="inline-block pt-2 text-sm text-muted-foreground">
                  Closed
                </span>
              ) : (
                ranges.map((rule) => (
                  <TimeRangeRow
                    key={rule.id}
                    rule={rule}
                    readOnly={readOnly}
                    onChange={(next) =>
                      onChange(rules.map((r) => (r.id === next.id ? next : r)))
                    }
                    onRemove={() =>
                      onChange(dropDay(rules, rule.id, day.iso))
                    }
                  />
                ))
              )}
              {/* Overlap within a day is a data error the server would accept
                  and the availability engine would double-count. Catch it here
                  where the user can see which two rows conflict. */}
              {overlaps(ranges) && (
                <p role="alert" className="text-sm text-destructive">
                  These times overlap. Merge them into one range.
                </p>
              )}
            </div>

            {!readOnly && (
              <button
                type="button"
                className="pt-2 text-sm text-primary underline"
                onClick={() => onChange([...rules, blankRule(day.iso)])}
              >
                Add hours
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Removing a day from a multi-day rule splits it rather than deleting it —
 *  a rule covering Mon–Fri must not vanish because someone closed Wednesday. */
function dropDay(rules: AvailabilityRule[], ruleId: string, weekday: number) {
  return rules.flatMap((r) => {
    if (r.id !== ruleId) return [r];
    const remaining = r.weekdays.filter((w) => w !== weekday);
    return remaining.length ? [{ ...r, weekdays: remaining }] : [];
  });
}
```

Holidays are a separate calendar view over `holidays` with an "import national
holidays" action driven by `calendar_code`. Both pages show a **preview strip**:
"Next 7 days: Mon 9–5, Tue 9–5, Wed closed (Christmas Day)…" computed by the
server from the same availability engine the agent uses. A tenant's most common
support question is "why did the AI say we're closed", and the preview answers
it before they ask.

### 10.5 Services — `/catalogue/services`

Permission: `bookings:read` / `bookings:update`.

A `DataTable` with inline `is_active` toggles (optimistic, §11.3) and a drawer
editor. The editor is **half static, half dynamic**: the generic booking fields
(duration, buffers, price, lead time) are a hand-built form because they map to
real columns with real semantics, while `intake_schema` is a `DynamicForm` over
the vertical's intake form. Price uses a decimal-safe input:

```tsx
// components/ui/MoneyInput.tsx
"use client";

import { Input } from "@/components/ui/input";

/**
 * Money is a STRING throughout the client (§5.2 / contract §6).
 *
 * The naive `<Input type="number" value={price} />` parses to a JS double, and
 * a price of 95.10 becomes 95.09999999999999 the moment it round-trips. This
 * component never calls Number() on the value — it validates the shape with a
 * regex and hands the string straight back to the API, which stores it in a
 * NUMERIC(12,4). Arithmetic, where the UI genuinely needs it (a line-item
 * total), goes through dinero.js on minor units.
 */
export function MoneyInput({
  value, currency, onChange, ...rest
}: {
  value: string;
  currency: string;
  onChange: (next: string) => void;
} & Omit<React.ComponentProps<typeof Input>, "value" | "onChange" | "type">) {
  return (
    <div className="relative">
      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
        {currencySymbol(currency)}
      </span>
      <Input
        {...rest}
        type="text"
        inputMode="decimal"
        className="pl-7 tabular-nums"
        value={value}
        onChange={(e) => {
          const next = e.target.value;
          if (next === "" || /^\d{0,9}(\.\d{0,4})?$/.test(next)) onChange(next);
        }}
      />
    </div>
  );
}
```

### 10.6 Resources — `/catalogue/resources`

Permission: `bookings:read` / `bookings:update`.

The screen that proves the generic booking model in the UI. `resource_types`
carries `attribute_schema JSONB` (01 §5.2), so the attribute editor for a
resource is — again — a `DynamicForm`, this time over a schema compiled from
the resource type rather than a manifest form. A dental chair's attributes
(`has_xray`, `room_number`) and a hotel room's (`bed_type`, `sea_view`,
`floor`) render through identical code.

Grouping is by `resource_type`, with `capacity` and `overbook_allowance` shown
inline because they are the two fields that silently change booking behaviour
and are worth making impossible to overlook.

### 10.7 Booking policies — `/policies`

Permission: `bookings:read` / `bookings:update`.

Reads the resolved manifest's `policies` block through the overlay editor. Two
columns: the platform/vertical default on the left, the tenant's override on
the right, with unchanged rows collapsed by default. 02 §4's overlay allow-list
means most of the manifest is not patchable — those rows render as read-only
with a lock icon and a tooltip naming the reason, rather than being hidden,
because a tenant who cannot find a setting files a ticket while a tenant who
can see it is locked understands immediately.

Editing writes to `PATCH /api/v1/verticals/installed/{id}/overlay` and shows a
**preview diff** from `POST .../validate` before `POST .../publish`.

### 10.8 AI configuration — `/agent`, `/agent/agents/[key]`

Permission: `agents:read` / `agents:update` / `agents:publish`.

Three surfaces:

**Agent list** — the manifest's `agents`, each with role, model class, granted
tools, and whether it is reachable in realtime mode. That last column is
derived from `derive_realtime_compatibility` (contract §11 A6) and is **never
editable** — the amendment is explicit that compatibility is computed, never
declared. The UI shows *why* an agent is realtime-incompatible ("needs mid-turn
handoff to `billing_specialist`"), which is the actionable part.

**Per-agent editor** — prompt (a plain textarea with a variable-reference
helper, not a rich-text editor: prompts are text and a WYSIWYG would smuggle
markup into a model context), model **class** picker (`router` / `fast` /
`balanced` / `deep`, per 03 §9.2 — never a model name), and tool grants as a
checklist restricted to the manifest's declared tools.

**Publish flow** — the important one:

```tsx
// components/agent/PublishDialog.tsx
"use client";

import { useMutation } from "@tanstack/react-query";

import { ApiProblem, apiFetch } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EvalFailureTable } from "./EvalFailureTable";

interface EvalFailure {
  case_key: string;
  judge: string;
  score: number;
  threshold: number;
  baseline_score: number;
  diff: { before: string; after: string };
}

export function PublishDialog({
  installId, open, onOpenChange,
}: {
  installId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const publish = useMutation({
    mutationFn: () =>
      apiFetch<{ version: string }>(
        `/api/v1/verticals/installed/${installId}/publish`,
        { method: "POST", idempotencyKey: crypto.randomUUID() },
      ),
  });

  // 03 §12.3: the tenant's smoke suite runs against the new version in a
  // sandbox before it goes live, and failing cases BLOCK the publish with a
  // diff. That is what 422 carries here.
  const failures =
    publish.error instanceof ApiProblem && publish.error.status === 422
      ? (publish.error.extensions.failed_cases as EvalFailure[])
      : null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Publish agent changes</DialogTitle>
        </DialogHeader>

        {failures ? (
          <>
            <p className="text-sm text-destructive">
              {failures.length} evaluation case(s) regressed. Publishing is
              blocked until these pass or the cases are updated.
            </p>
            <EvalFailureTable failures={failures} />
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            Your evaluation suite will run against the new version in a sandbox
            before it goes live. This usually takes under a minute.
          </p>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => publish.mutate()} disabled={publish.isPending}>
            {publish.isPending ? "Running evaluations…" : "Run evaluations and publish"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

**Playground** — `/agent/playground`. A chat panel against
`POST /api/v1/ai/playground` (SSE) running the *draft* overlay, with a toggle
for stubbed vs live tools and a right rail showing per-turn latency, model
class actually used, `fallback_depth`, retrieved chunks, and tool calls. It is
the only place a tenant can see `TurnCostEvent`-shaped detail per turn, and it
is gated on `agents:update` so a viewer cannot burn tokens.

### 10.9 Voice configuration — `/agent/voice`

Permission: `channels:read` / `channels:manage`.

Voice selection with an inline preview button (`POST /api/v1/voice/preview`
returns audio; the player is a plain `<audio>` with `preload="none"`), greeting
text, recording policy (04 §5.8's four values: `disabled`, `always`,
`announce`, `opt_in`) with the legal implications spelled out next to each,
transfer destinations, and phone numbers.

The recording policy selector carries a jurisdiction warning that is *not*
legal advice but is specific enough to be useful: selecting `always` in a
two-party-consent jurisdiction shows a warning naming the tenant's
`data_region`. Getting this wrong is a legal exposure, not a UX defect.

Turn-detection tuning (`barge_in_ms` and friends from 04 §3) is deliberately
**not** exposed. Those numbers are latency-budget-critical and 04 §7 tunes them
per vertical; a slider that lets a tenant set barge-in to 800 ms would let them
make their own product feel broken and then report it as a bug.

### 10.10 Knowledge management — `/knowledge`

Permission: `knowledge:read` / `knowledge:write` / `knowledge:publish`.

Sources list, each with health, document count, last sync, and the **resolved
text search config** — 03 §10.3.1 requires the admin UI to show "which config a
source actually resolved to, so a tenant can see they are on `'simple'`".

Upload is the two-phase flow from §1.2, with live progress:

```tsx
// components/knowledge/UploadDropzone.tsx
"use client";

import { useMutation } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { apiFetch } from "@/lib/api/client";
import { cn } from "@/lib/utils";

interface Upload {
  file: File;
  progress: number;
  state: "signing" | "uploading" | "queued" | "failed";
  jobId?: string;
  error?: string;
}

export function UploadDropzone({ sourceId }: { sourceId: string }) {
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [dragging, setDragging] = useState(false);

  const patch = (file: File, next: Partial<Upload>) =>
    setUploads((prev) => prev.map((u) => (u.file === file ? { ...u, ...next } : u)));

  const upload = useMutation({
    mutationFn: async (file: File) => {
      patch(file, { state: "signing" });
      const signed = await apiFetch<{
        upload_id: string; url: string; headers: Record<string, string>;
      }>("/api/v1/knowledge/uploads", {
        method: "POST",
        body: JSON.stringify({
          source_id: sourceId,
          filename: file.name,
          content_type: file.type,
          size_bytes: file.size,
        }),
        idempotencyKey: `upload:${sourceId}:${file.name}:${file.size}:${file.lastModified}`,
      });

      // Direct to object storage. XHR rather than fetch purely for
      // upload progress — fetch still has no upload-side ReadableStream in
      // Safari, and a 25 MB PDF with no progress bar reads as a hang.
      patch(file, { state: "uploading", progress: 0 });
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", signed.url);
        for (const [k, v] of Object.entries(signed.headers)) xhr.setRequestHeader(k, v);
        xhr.upload.onprogress = (e) =>
          e.lengthComputable && patch(file, { progress: e.loaded / e.total });
        xhr.onload = () =>
          xhr.status < 300 ? resolve() : reject(new Error(`Upload failed (${xhr.status})`));
        xhr.onerror = () => reject(new Error("Network error during upload"));
        xhr.send(file);
      });

      patch(file, { state: "queued", progress: 1 });
      return signed.upload_id;
    },
    onError: (error, file) =>
      patch(file, { state: "failed", error: (error as Error).message }),
  });

  const accept = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      const next = Array.from(files).map<Upload>((file) => ({
        file, progress: 0, state: "signing",
      }));
      setUploads((prev) => [...prev, ...next]);
      next.forEach((u) => upload.mutate(u.file));
    },
    [upload],
  );

  return (
    <div>
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload documents. Press Enter to browse, or drop files here."
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); accept(e.dataTransfer.files); }}
        onKeyDown={(e) => {
          // A drop zone that only works with a mouse excludes every keyboard
          // user from a primary action (WCAG 2.1.1). The hidden input is the
          // keyboard path, and it is not optional.
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            document.getElementById("kb-file-input")?.click();
          }
        }}
        className={cn(
          "rounded-lg border-2 border-dashed p-10 text-center transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          dragging ? "border-primary bg-primary/5" : "border-muted-foreground/25",
        )}
      >
        <p className="font-medium">Drop files or press Enter to browse</p>
        <p className="mt-1 text-sm text-muted-foreground">
          PDF, DOCX, TXT, MD, HTML · up to 25 MB each
        </p>
        <input
          id="kb-file-input"
          type="file"
          multiple
          className="sr-only"
          accept=".pdf,.docx,.txt,.md,.html"
          onChange={(e) => accept(e.target.files)}
        />
      </div>

      <ul className="mt-4 space-y-2" aria-live="polite">
        {uploads.map((u) => (
          <li key={`${u.file.name}-${u.file.lastModified}`} className="flex items-center gap-3">
            <span className="min-w-0 flex-1 truncate text-sm">{u.file.name}</span>
            {u.state === "uploading" && (
              <progress
                value={u.progress}
                max={1}
                aria-label={`Uploading ${u.file.name}`}
                className="w-32"
              />
            )}
            <span className="text-xs text-muted-foreground">{describe(u)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

Ingestion status uses the `ingestion_status` enum from 01 §7.5 —
`queued | running | succeeded | failed | cancelled` — streamed over SSE
(§3.5) so a 400-page manual shows real progress instead of a spinner. Failures
show `ingestion_jobs.errors` (capped at 100 in the schema) with per-document
detail, because "ingestion failed" without saying which of 300 documents failed
is unactionable.

The **review queue** (`/knowledge/review`) lists chunks flagged by 03's content
screening, showing the chunk text, the flag reason, and the document it came
from, with approve/exclude actions. This is the only place a human sees
untrusted knowledge content in the portal, so the renderer escapes it as plain
text — never `dangerouslySetInnerHTML`, never markdown. A prompt-injection
payload in an uploaded PDF must render as visible characters, which is
precisely what makes the review useful.

### 10.11 Conversation history & transcript viewer — `/conversations`, `/conversations/[id]`

Permission: `conversations:read`, plus `conversations:read_pii` for unmasked.

List: cursor-paginated (§2.2), filters for channel, status, outcome, date
range, and escalation. Free-text search hits `transcripts.search_vector`.

The transcript viewer:

```tsx
// components/conversations/TranscriptViewer.tsx
"use client";

import { useRef } from "react";

import { Badge } from "@/components/ui/badge";
import { MaskedPiiNotice } from "./MaskedPiiNotice";
import { ToolCallCard } from "./ToolCallCard";
import { TurnBubble } from "./TurnBubble";
import { useConversationTurns } from "@/lib/query/conversations";
import type { Conversation } from "@/lib/api/types";

export function TranscriptViewer({
  conversation, onSeek,
}: {
  conversation: Conversation;
  onSeek?: (ms: number) => void;   // wired to the audio player when a recording exists
}) {
  const turns = useConversationTurns(conversation.id);
  const listRef = useRef<HTMLOListElement>(null);

  return (
    <div className="flex flex-col gap-4">
      {turns.data?.pii_masked && <MaskedPiiNotice />}

      <ol ref={listRef} className="space-y-4">
        {turns.data?.items.map((turn) => {
          // turn_role from 01 §1.7: system | user | assistant | tool | event
          if (turn.role === "tool") {
            return (
              <li key={turn.id}>
                <ToolCallCard
                  name={turn.tool_name}
                  args={turn.tool_calls}
                  result={turn.tool_result}
                  error={turn.tool_error}
                  durationMs={turn.tool_duration_ms}
                />
              </li>
            );
          }
          if (turn.role === "event") {
            return (
              <li key={turn.id} className="flex items-center gap-2 text-xs text-muted-foreground">
                <span className="h-px flex-1 bg-border" />
                {turn.content}
                <span className="h-px flex-1 bg-border" />
              </li>
            );
          }
          return (
            <li key={turn.id}>
              <TurnBubble
                role={turn.role}
                content={turn.content}
                at={turn.created_at}
                // Voice turns carry audio offsets; clicking seeks the player.
                onSeek={
                  turn.audio_start_ms != null && onSeek
                    ? () => onSeek(turn.audio_start_ms!)
                    : undefined
                }
                meta={
                  <>
                    {turn.latency_ms != null && (
                      <Badge variant="outline" className="tabular-nums">
                        {turn.latency_ms} ms
                      </Badge>
                    )}
                    {turn.is_interruption && <Badge variant="secondary">Interrupted</Badge>}
                    {turn.agent_node && <Badge variant="outline">{turn.agent_node}</Badge>}
                  </>
                }
              />
            </li>
          );
        })}
      </ol>

      {turns.hasNextPage && (
        <button onClick={() => turns.fetchNextPage()} className="text-sm underline">
          Load earlier turns
        </button>
      )}
    </div>
  );
}
```

`content` renders as **plain text**, never markdown or HTML. A transcript
contains whatever a caller said and whatever the knowledge base returned; both
are untrusted (contract §8), and rendering them as markup in an admin's browser
is stored XSS with extra steps.

Right rail: outcome (`conversation_outcomes.kind`), sentiment, CSAT, extracted
data, linked booking, cost breakdown (gated on `usage:read`), and a "flag for
review" action.

### 10.12 Call playback — `/conversations/[id]/recording`

Permission: `conversations:read`; the audio URL is minted per request.

```tsx
// components/conversations/CallPlayer.tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api/client";
import { WaveformCanvas } from "./WaveformCanvas";

export function CallPlayer({
  conversationId, words, onTimeUpdate,
}: {
  conversationId: string;
  words: Array<{ word: string; start_ms: number; end_ms: number; speaker: number }>;
  onTimeUpdate?: (ms: number) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [current, setCurrent] = useState(0);

  // The URL is a short-TTL presigned link (§1.2). It is fetched on demand and
  // never persisted in React Query's cache: caching a 120-second credential in
  // a store designed to survive navigations produces a link that is expired
  // exactly when the user comes back to it, and the failure looks like a
  // broken recording rather than an expired URL.
  useEffect(() => {
    let cancelled = false;
    apiFetch<{ url: string }>(
      `/api/v1/conversations/${conversationId}/recording`,
      { redirect: "manual" },
    ).then((r) => !cancelled && setSrc(r.url));
    return () => { cancelled = true; };
  }, [conversationId]);

  const seek = useCallback((ms: number) => {
    if (audioRef.current) audioRef.current.currentTime = ms / 1000;
  }, []);

  return (
    <div className="space-y-3">
      {/* Dual-channel (04 §5.8 records caller and agent on separate channels),
          so the waveform is two stacked tracks. A mixed waveform cannot show
          overlap, which is the one thing a two-track view is for: seeing
          exactly where the caller talked over the agent. */}
      <WaveformCanvas
        src={src}
        currentMs={current}
        words={words}
        onSeek={seek}
      />

      <audio
        ref={audioRef}
        src={src ?? undefined}
        controls
        preload="metadata"
        className="w-full"
        onTimeUpdate={(e) => {
          const ms = e.currentTarget.currentTime * 1000;
          setCurrent(ms);
          onTimeUpdate?.(ms);
        }}
      >
        {/* Native <audio> rather than a custom player: it is keyboard
            accessible, screen-reader labelled, and respects OS media keys and
            playback-speed preferences for free. A bespoke player would have to
            reimplement all of that and would get some of it wrong. */}
        Your browser does not support audio playback.
        <a href={src ?? "#"}>Download the recording</a>
      </audio>

      <div className="flex gap-2">
        {[0.75, 1, 1.25, 1.5, 2].map((rate) => (
          <button
            key={rate}
            onClick={() => audioRef.current && (audioRef.current.playbackRate = rate)}
            className="rounded border px-2 py-0.5 text-xs tabular-nums"
          >
            {rate}×
          </button>
        ))}
      </div>
    </div>
  );
}
```

Word-level highlighting comes from `transcripts.words JSONB` (01 §6.3). The
transcript and the player are bidirectionally linked: clicking a turn seeks the
audio, and playing the audio highlights the current word. That pairing is what
makes a 12-minute call reviewable in 30 seconds.

### 10.13 Integrations — `/integrations`

Permission: `connectors:read` / `connectors:manage`.

Catalogue of `connector_definitions`, installed list with `sync_health`
(`healthy | degraded | failing | disabled` from 01 §1.7), and a detail page
per instance. Configuration forms are — again — `DynamicForm`, this time over
the connector's `config_schema`.

The detail page surfaces `sync_state` honestly: last run, records seen/applied/
skipped/failed, `conflicts_pending`, `consecutive_failures`, and `last_error`
verbatim. A connector that is failing must say so on the screen a tenant
actually opens, not only in a log.

Egress configuration (`allowed_egress_hosts`) is editable with the SSRF guard's
rules stated inline, and a host that fails `assert_public_https` is rejected in
the form with the specific reason ("169.254.169.254 is a link-local address").

### 10.14 Analytics — `/analytics/*`

Permission: `usage:read`; retrieval quality also needs `knowledge:read`.

See §12 for the charting rules. Five pages: overview, conversations, bookings,
costs, and quality. Every page shares a date-range control in the RSC layout and
respects the `timezone` in the response envelope.

The quality page is the one 03 asks for and nobody would otherwise build:
`lexical_contributed` rate per source (with an explanatory note for CJK/Thai
tenants where a persistently-false value is expected, not an alert), shadow-eval
scores per manifest version, and the router degradation indicator from 03 §11.3.

### 10.15 Users & roles — `/settings/members`, `/settings/roles`

Permission: `members:read` / `members:invite` / `members:update_role` ⚡ /
`roles:manage` ⚡.

Members table: name, email, role, locations, last active, status. The role
selector enforces 01 §3.3's `rank` rule client-side (you may assign only at or
below your own rank) and 05's footnotes (a manager may invite only at `agent`
or `viewer`; an admin may not modify an owner). The server enforces it too —
this is UX.

The owner invariant gets special treatment. 01 has a deferred constraint trigger
`trg_tenant_has_owner`, so removing the last owner fails at commit with a
database error. The UI pre-empts it: the last owner's row has its remove action
disabled with "A tenant must always have at least one owner. Promote someone
else first."

Roles page: custom roles composed from the `permissions` catalogue, grouped by
resource, with the five system roles shown read-only as reference. Each
permission has a plain-language description from `permissions.description` —
`conversations:read_pii` reads as "See customer names, phone numbers and email
addresses in transcripts", because nobody outside the engineering team knows
what the slug means.

### 10.16 Audit log — `/settings/audit`

Permission: `audit:read`.

Cursor-paginated over `POST /api/v1/audit/events` (05 §B8's canonical keyset
example). Filters: actor, action prefix, entity type, outcome, severity, date
range, and a `is_phi_access` toggle that exists because a HIPAA tenant's
compliance officer needs exactly that view and nothing else.

Each row expands to show the `changes JSONB` as a field-level diff. Rows are
immutable — the table has UPDATE/DELETE revoked — so there are no actions, only
export.

### 10.17 Billing — `/settings/billing`

Permission: `billing:read`; management needs `billing:manage` ⚡.

Current plan with `entitlements_snapshot` (the grandfathered copy, §1.2), usage
against included quantities per meter with a projection to period end, invoice
history, and payment method via a Stripe portal redirect. No card data ever
touches our DOM.

The usage meters are the screen's real content:

```tsx
// components/billing/MeterBar.tsx
"use client";

/**
 * Usage against an included allowance.
 *
 * Colour alone must not carry the "over quota" signal (WCAG 1.4.1): the bar
 * also gets a distinct pattern and the label states the overage in words. A
 * red bar is invisible to a deuteranope and to anyone glancing at a phone in
 * sunlight.
 */
export function MeterBar({
  label, used, included, unit, projected,
}: {
  label: string;
  used: number;
  included: number | null;
  unit: string;
  projected?: number;
}) {
  const pct = included ? Math.min(used / included, 1) : 0;
  const over = included !== null && used > included;
  const projectedOver = included !== null && (projected ?? 0) > included;

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-muted-foreground">
          {format(used)} {included !== null ? `of ${format(included)} ` : ""}{unit}
          {over && <strong className="ml-1 text-destructive">· over allowance</strong>}
          {!over && projectedOver && (
            <span className="ml-1 text-amber-600 dark:text-amber-500">
              · projected to exceed
            </span>
          )}
        </span>
      </div>
      <div
        role="meter"
        aria-valuenow={used}
        aria-valuemin={0}
        aria-valuemax={included ?? used}
        aria-label={`${label}: ${format(used)} ${unit} used`}
        className="h-2 overflow-hidden rounded-full bg-muted"
      >
        <div
          className={over ? "h-full bg-destructive" : "h-full bg-primary"}
          style={{
            width: `${pct * 100}%`,
            backgroundImage: over
              ? "repeating-linear-gradient(45deg,transparent,transparent 4px,rgba(255,255,255,.35) 4px,rgba(255,255,255,.35) 8px)"
              : undefined,
          }}
        />
      </div>
    </div>
  );
}
```

---

## 11. Data layer

### 11.1 Query client

```ts
// lib/query/client.ts
import { QueryClient } from "@tanstack/react-query";

import { ApiProblem } from "@/lib/api/client";

export function makeQueryClient(tenantId: string): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 30 seconds: long enough that navigating between screens does not
        // refetch everything, short enough that a colleague's change appears
        // without a manual reload.
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          if (error instanceof ApiProblem) {
            // Never retry a decided answer. Retrying a 403 cannot change it
            // and burns rate-limit budget; retrying a 409 on a booking would
            // race the same lost slot three more times.
            if (error.status < 500 && error.status !== 429) return false;
            if (error.status === 429) return failureCount < 2;
          }
          return failureCount < 3;
        },
        retryDelay: (attempt) =>
          Math.min(1000 * 2 ** attempt, 30_000) * (0.5 + Math.random()),
      },
      mutations: {
        // Mutations are never retried automatically. Every unsafe route either
        // requires an Idempotency-Key or is not safely repeatable, and a
        // library-level retry would fire without one on the routes where it is
        // optional. Retries are the caller's explicit decision.
        retry: false,
      },
    },
  });
}
```

### 11.2 Tenant-scoped cache keys

**A cache leak across tenants is a security bug.** Three independent
mechanisms, because one is not enough for a class of bug this severe.

**Mechanism 1 — a new `QueryClient` per tenant.** §7.3's `Providers` keys the
client on `session.activeTenantId`. Switching tenants constructs a new client;
the old cache is unreachable and garbage-collected. Even a query key with no
tenant prefix cannot leak, because there is no shared store to leak through.

**Mechanism 2 — every key is tenant-prefixed, by construction.** Not by
convention:

```ts
// lib/query/keys.ts
import { useTenant } from "@/components/shell/Providers";

/**
 * Tenant-scoped query key factory.
 *
 * Every key begins with the tenant id. This is belt-and-braces over the
 * per-tenant QueryClient (mechanism 1), and it is what makes a leak visible in
 * devtools: a key without a UUID prefix is immediately wrong to anyone looking
 * at the query list.
 *
 * The factory is a HOOK, not a module-level object, so a key literally cannot
 * be constructed without a tenant in scope. A plain exported
 * `keys.services.list()` would be callable from anywhere, including a module
 * that has no idea which tenant is active — and that is exactly the call site
 * that would produce a cross-tenant key.
 */
export function useKeys() {
  const { tenantId } = useTenant();

  return {
    all: [tenantId] as const,

    session: () => [tenantId, "session"] as const,
    navigation: () => [tenantId, "navigation"] as const,

    services: {
      all: () => [tenantId, "services"] as const,
      list: (filters: object = {}) => [tenantId, "services", "list", filters] as const,
      detail: (id: string) => [tenantId, "services", "detail", id] as const,
    },
    bookings: {
      all: () => [tenantId, "bookings"] as const,
      list: (filters: object = {}) => [tenantId, "bookings", "list", filters] as const,
      detail: (id: string) => [tenantId, "bookings", "detail", id] as const,
      availability: (q: object) => [tenantId, "bookings", "availability", q] as const,
    },
    conversations: {
      all: () => [tenantId, "conversations"] as const,
      list: (filters: object = {}) => [tenantId, "conversations", "list", filters] as const,
      detail: (id: string) => [tenantId, "conversations", "detail", id] as const,
      turns: (id: string) => [tenantId, "conversations", id, "turns"] as const,
      live: () => [tenantId, "conversations", "live"] as const,
    },
    knowledge: {
      sources: () => [tenantId, "knowledge", "sources"] as const,
      job: (id: string) => [tenantId, "knowledge", "job", id] as const,
    },
    forms: {
      doc: (formKey: string, context: string, locale: string) =>
        [tenantId, "form", formKey, context, locale] as const,
    },
    analytics: (endpoint: string, params: object) =>
      [tenantId, "analytics", endpoint, params] as const,
  } as const;
}
```

**Mechanism 3 — a lint rule and a runtime assertion.** The lint rule bans
`useQuery` with a `queryKey` that is not derived from `useKeys()`:

```js
// eslint.config.js (excerpt)
{
  rules: {
    "no-restricted-syntax": ["error", {
      selector:
        "CallExpression[callee.name=/^use(Query|InfiniteQuery|SuspenseQuery)$/] > ObjectExpression > Property[key.name='queryKey'] > ArrayExpression:first-child > Literal:first-child",
      message:
        "Query keys must start with the tenant id. Use `useKeys()` from lib/query/keys — a literal first element means this cache entry is shared across tenants.",
    }],
  },
}
```

And in development, a query-cache subscriber that fails loudly:

```ts
// lib/query/guard.ts
import type { QueryClient } from "@tanstack/react-query";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Development-only assertion that every cache entry is tenant-scoped.
 *
 * Throwing in development is deliberate. A silent cross-tenant cache entry is
 * the kind of bug that ships, sits in production for six months, and is found
 * by a customer who saw another customer's booking list. Making it a hard
 * crash on the developer's machine, at the moment the query is registered, is
 * the only reliable time to catch it.
 */
export function installCacheGuard(client: QueryClient, tenantId: string): () => void {
  if (process.env.NODE_ENV === "production") return () => {};

  return client.getQueryCache().subscribe((event) => {
    if (event.type !== "added") return;
    const [first] = event.query.queryKey as unknown[];
    if (typeof first !== "string" || !UUID.test(first)) {
      throw new Error(
        `Query key is not tenant-scoped: ${JSON.stringify(event.query.queryKey)}. ` +
          `Every key must start with the tenant id. Use useKeys().`,
      );
    }
    if (first !== tenantId) {
      throw new Error(
        `CROSS-TENANT CACHE KEY: key is scoped to ${first}, active tenant is ${tenantId}.`,
      );
    }
  });
}
```

**On tenant switch, the old cache is discarded, not invalidated.**
`queryClient.clear()` would be a possible alternative to a new client, but it
races: an in-flight request started under tenant A can resolve after `clear()`
and write tenant A's data into tenant B's fresh cache. Constructing a new
`QueryClient` means the resolving promise writes into an orphaned cache nobody
reads. This is the entire reason mechanism 1 exists in that specific form.

### 11.3 Optimistic updates

Used for exactly three interaction shapes, and no others: toggles, inline field
edits, and reordering. Everything else — anything that can 409, anything that
runs a workflow, anything involving money — waits for the server.

```ts
// lib/query/mutations/services.ts
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiProblem, apiFetch } from "@/lib/api/client";
import { useKeys } from "@/lib/query/keys";
import { toast } from "@/components/ui/use-toast";
import type { CursorPage, Service } from "@/lib/api/types";

export function useToggleService() {
  const keys = useKeys();
  const qc = useQueryClient();

  return useMutation({
    mutationFn: ({ id, isActive, version }: { id: string; isActive: boolean; version: number }) =>
      apiFetch<Service>(`/api/v1/services/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: isActive }),
        ifMatch: `"${version}"`,      // §2.6 — PATCH without If-Match is a 428
      }),

    onMutate: async ({ id, isActive }) => {
      // Cancel in-flight refetches: one landing mid-mutation would overwrite
      // the optimistic value with stale server data and the toggle would visibly
      // flip back and then forward again.
      await qc.cancelQueries({ queryKey: keys.services.all() });

      const previous = qc.getQueriesData<CursorPage<Service>>({
        queryKey: keys.services.all(),
      });

      qc.setQueriesData<CursorPage<Service>>(
        { queryKey: keys.services.all() },
        (page) =>
          page && {
            ...page,
            items: page.items.map((s) =>
              s.id === id ? { ...s, is_active: isActive } : s,
            ),
          },
      );

      return { previous };
    },

    onError: (error, _vars, context) => {
      // Restore EVERY snapshot, not just the list we happened to be looking
      // at. `services` appears in the catalogue list, the booking form's
      // picker, and the policy editor; rolling back one leaves the others
      // showing a value the server rejected.
      context?.previous.forEach(([key, data]) => qc.setQueryData(key, data));

      if (error instanceof ApiProblem && error.status === 412) {
        toast({
          title: "Someone else changed this service",
          description: "Your change was not applied. The latest version is shown.",
          variant: "warning",
        });
      } else {
        toast({
          title: "Could not update the service",
          description: error instanceof ApiProblem ? error.detail : undefined,
          variant: "destructive",
        });
      }
    },

    // Always refetch, success or failure. On success it picks up the new
    // `version` for the next If-Match; on failure it resyncs after the
    // rollback. Skipping this on success leaves a stale version in the cache
    // and the NEXT edit 412s for no visible reason.
    onSettled: () => qc.invalidateQueries({ queryKey: keys.services.all() }),
  });
}
```

**Where optimism is wrong, stated explicitly** — this list is the useful part:

- **Creating a booking.** It can 409 on the exclusion constraint. An optimistic
  row that vanishes two seconds later, after the user has already told the
  customer "you're booked", is worse than a two-second spinner.
- **Cancelling a booking.** It runs a workflow, may charge a fee, and may
  notify the customer. Showing "cancelled" before the server agrees can lead a
  receptionist to tell a customer their fee was waived when it was not.
- **Publishing a manifest.** It runs an eval suite that can block the publish.
- **Anything gated on `aal >= 2`.** The 403 arrives after the optimistic
  update, so the UI shows success and then a step-up prompt.

### 11.4 List pagination

```ts
// lib/query/conversations.ts
import { useInfiniteQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/client";
import { useKeys } from "@/lib/query/keys";
import type { Conversation, CursorPage } from "@/lib/api/types";

export function useConversations(filters: Record<string, string | undefined>) {
  const keys = useKeys();

  return useInfiniteQuery({
    queryKey: keys.conversations.list(filters),
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams(
        Object.entries(filters).filter(([, v]) => v) as [string, string][],
      );
      if (pageParam) params.set("cursor", pageParam);
      params.set("limit", "50");
      return apiFetch<CursorPage<Conversation>>(`/api/v1/conversations?${params}`);
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // No getPreviousPageParam. The API's cursors are forward-only (§2.2), and
    // the UI is "Load more", never numbered pages — which is also why nothing
    // asks for a total count.
    staleTime: 15_000,
  });
}
```

`useInfiniteQuery` maps onto the cursor API exactly: `next_cursor` becomes
`pageParam`, and `has_more` is redundant with `hasNextPage`. The UI never
displays "page 3 of 91" because the API cannot cheaply produce 91 (§2.2).

### 11.5 Streaming into the cache

The subtle part: a streaming response must land in the same cache the rest of
the UI reads, or a live conversation and its transcript view disagree.

```ts
// lib/realtime/useConversationStream.ts
"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { useKeys } from "@/lib/query/keys";
import { useEventStream } from "./useEventStream";

interface StreamState {
  streamingText: string;
  status: "idle" | "thinking" | "typing";
  activeTool: { key: string; label: string } | null;
  error: string | null;
}

export function useConversationStream(conversationId: string, enabled: boolean) {
  const qc = useQueryClient();
  const keys = useKeys();
  const [state, setState] = useState<StreamState>({
    streamingText: "", status: "idle", activeTool: null, error: null,
  });
  const lastSeq = useRef(-1);

  const onEvent = useCallback(
    (type: string, data: Record<string, unknown>) => {
      // §3.3 consequence 1: resume can redeliver. `seq` is the dedup key.
      const seq = typeof data.seq === "number" ? data.seq : -1;
      if (seq >= 0 && seq <= lastSeq.current) return;
      lastSeq.current = seq;

      switch (type) {
        case "turn.started":
          setState((s) => ({ ...s, streamingText: "", status: "typing" }));
          break;
        case "token":
          setState((s) => ({ ...s, streamingText: s.streamingText + String(data.text ?? "") }));
          break;
        case "tool.started":
          setState((s) => ({
            ...s,
            status: "thinking",
            activeTool: { key: String(data.tool_key), label: String(data.label) },
          }));
          break;
        case "tool.finished":
          setState((s) => ({ ...s, activeTool: null, status: "typing" }));
          break;
        case "turn.finished":
          // The durable turn now exists in Postgres. Invalidate rather than
          // hand-inserting the accumulated text: the server's row carries
          // fields the stream never sent (cost, token counts, safety_flags,
          // the canonical id), and synthesising a partial row here would put a
          // subtly different object in the cache than the one a page refresh
          // produces. Divergence between "what you see live" and "what you see
          // on reload" is the bug this avoids.
          setState((s) => ({ ...s, streamingText: "", status: "idle", activeTool: null }));
          qc.invalidateQueries({ queryKey: keys.conversations.turns(conversationId) });
          qc.invalidateQueries({ queryKey: keys.conversations.detail(conversationId) });
          break;
        case "error":
          setState((s) => ({
            ...s,
            status: "idle",
            error: String(data.detail ?? data.title ?? "Something went wrong"),
          }));
          if (data.code === "stream_gap") {
            // §3.3 consequence 2: the Redis stream was trimmed past our
            // cursor. Re-render from the durable record.
            qc.invalidateQueries({ queryKey: keys.conversations.turns(conversationId) });
          }
          break;
      }
    },
    [qc, keys, conversationId],
  );

  const { connected, reconnecting } = useEventStream(
    enabled ? `/api/v1/conversations/${conversationId}/stream` : null,
    onEvent,
  );

  useEffect(() => {
    lastSeq.current = -1;
  }, [conversationId]);

  return { ...state, connected, reconnecting };
}
```

The rule this encodes: **streaming updates ephemeral UI state; the cache is
updated by invalidation at turn boundaries.** Writing every token into React
Query would re-render every subscriber of the turns query on every token, and
would put a partially-formed turn object into a cache that other screens read.

---

## 12. Charts

Recharts, per contract §1's frontend stack. This section fixes the palette, the
mark specs, and the rules — so every chart in the portal reads as one system
rather than as fourteen developers' individual taste.

### 12.1 The palette

Eight categorical slots, **assigned in fixed order and never cycled**. Both
modes are selected — the dark column is the same hues re-stepped for the dark
surface, not an automatic filter flip.

| Slot | Hue | Light | Dark |
|---|---|---|---|
| 1 | blue | `#2a78d6` | `#3987e5` |
| 2 | orange | `#eb6834` | `#d95926` |
| 3 | aqua | `#1baf7a` | `#199e70` |
| 4 | yellow | `#eda100` | `#c98500` |
| 5 | magenta | `#e87ba4` | `#d55181` |
| 6 | green | `#008300` | `#008300` |
| 7 | violet | `#4a3aa7` | `#9085e9` |
| 8 | red | `#e34948` | `#e66767` |

This ordering was validated, not chosen by eye. On the default adjacent
pairlist (lines, stacked bars, grouped bars) the six slots the portal actually
uses clear every gate in both modes: worst adjacent CVD ΔE 9.1 light / 8.4 dark
against a ≥8 target, worst adjacent normal-vision ΔE 19.6 light / 19.3 dark
against a ≥15 floor.

Three light-mode slots — aqua (2.74:1), yellow (2.11:1) and magenta (2.62:1) —
sit below 3:1 contrast on the light surface. That is not dismissable: those
charts **must** carry visible direct labels or a table view. §12.3 does exactly
that, which is why the relief rule is satisfied rather than waived.

**Scatter, bubble and small-multiple charts cap at three series.** Those forms
put every pair on screen simultaneously, and with all 28 pairs in play no
ordering of eight hues clears the floors. The first three slots do validate
all-pairs in both modes (worst CVD ΔE 9.2 light, normal-vision 24.0). Past
three, fold into "Other" or facet. The fourth slot would put yellow beside
orange, and that pair fails all-pairs at normal-vision ΔE 13.7.

```css
/* app/globals.css — chart tokens */
:root {
  --chart-surface: #fcfcfb;
  --chart-grid: #e8e7e3;
  --chart-axis: #52514e;
  --chart-1: #2a78d6;
  --chart-2: #eb6834;
  --chart-3: #1baf7a;
  --chart-4: #eda100;
  --chart-5: #e87ba4;
  --chart-6: #008300;
  --chart-7: #4a3aa7;
  --chart-8: #e34948;

  /* Status colours are RESERVED. They never become "series 4", because a
     chart where series 4 happens to be red and a chart where red means
     "failing" cannot coexist in one product without teaching people to
     misread both. */
  --status-good: #008300;
  --status-warning: #eda100;
  --status-serious: #eb6834;
  --status-critical: #e34948;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --chart-surface: #1a1a19;
    --chart-grid: #2f2f2d;
    --chart-axis: #c3c2b7;
    --chart-1: #3987e5;
    --chart-2: #d95926;
    --chart-3: #199e70;
    --chart-4: #c98500;
    --chart-5: #d55181;
    --chart-6: #008300;
    --chart-7: #9085e9;
    --chart-8: #e66767;
  }
}

:root[data-theme="dark"] {
  --chart-surface: #1a1a19;
  --chart-grid: #2f2f2d;
  --chart-axis: #c3c2b7;
  --chart-1: #3987e5;
  --chart-2: #d95926;
  --chart-3: #199e70;
  --chart-4: #c98500;
  --chart-5: #d55181;
  --chart-6: #008300;
  --chart-7: #9085e9;
  --chart-8: #e66767;
}
```

Both dark scopes are declared: the media query covers the OS setting, the
`data-theme` scope covers the in-app toggle, and the `:not()` guard lets an
explicit light stamp beat OS-dark.

### 12.2 Colour follows the entity, never the rank

The single most common charting bug in an application with filters:

```ts
// lib/charts/series.ts
const SLOTS = [
  "var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)",
  "var(--chart-5)", "var(--chart-6)", "var(--chart-7)", "var(--chart-8)",
] as const;

/**
 * Stable colour assignment keyed on the SERIES KEY, not its index.
 *
 * The bug this prevents: a user filters out "web_chat", every remaining series
 * shifts up an index, and "voice_inbound" changes from orange to blue. The
 * data did not change; the picture did. Users read that as a data change and
 * lose trust in every chart in the product.
 *
 * The order is registration order within a chart's declared key list, which is
 * itself stable (it comes from the endpoint's series order, not from sorting by
 * value). A series that appears in one time range and not another keeps its
 * colour across both.
 */
export function makeColorScale(allKeys: readonly string[]) {
  const assignment = new Map<string, string>();
  allKeys.forEach((key, i) => {
    if (i < SLOTS.length) assignment.set(key, SLOTS[i]);
  });
  return (key: string) => assignment.get(key) ?? "var(--chart-axis)";
}
```

### 12.3 The chart components

```tsx
// components/charts/TimeSeriesChart.tsx
"use client";

import { useMemo } from "react";
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

import { ChartTooltip } from "./ChartTooltip";
import { ChartTable } from "./ChartTable";
import { makeColorScale } from "@/lib/charts/series";
import { formatAxisTick, formatValue } from "@/lib/charts/format";
import type { AnalyticsSeries } from "@/lib/api/types";

export function TimeSeriesChart({
  series, granularity, timezone, unit = "count", title, description, height = 280,
}: {
  series: AnalyticsSeries[];
  granularity: "hour" | "day" | "week" | "month";
  timezone: string;
  unit?: "count" | "percent" | "currency" | "duration";
  title: string;
  description?: string;
  height?: number;
}) {
  const colorOf = useMemo(
    () => makeColorScale(series.map((s) => s.key)),
    [series],
  );

  // Recharts wants row-shaped data; the API returns column-shaped series.
  const rows = useMemo(() => {
    const byTime = new Map<string, Record<string, number | string>>();
    for (const s of series) {
      for (const point of s.points) {
        const row = byTime.get(point.t) ?? { t: point.t };
        row[s.key] = point.v;
        byTime.set(point.t, row);
      }
    }
    return [...byTime.values()].sort((a, b) =>
      String(a.t).localeCompare(String(b.t)),
    );
  }, [series]);

  return (
    <figure className="space-y-2">
      <figcaption>
        <h3 className="text-sm font-medium">{title}</h3>
        {description && (
          <p className="text-xs text-muted-foreground">{description}</p>
        )}
      </figcaption>

      <div style={{ height }} role="img" aria-label={describeChart(title, series, unit)}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
            {/* Horizontal grid only. Vertical gridlines on a time axis add
                ink without adding a comparison anyone makes — you read a time
                series across, not up. */}
            <CartesianGrid
              stroke="var(--chart-grid)"
              strokeDasharray="0"
              vertical={false}
            />
            <XAxis
              dataKey="t"
              tickFormatter={(t) => formatAxisTick(t, granularity, timezone)}
              stroke="var(--chart-axis)"
              tickLine={false}
              axisLine={false}
              minTickGap={32}
              tick={{ fontSize: 12 }}
            />
            <YAxis
              stroke="var(--chart-axis)"
              tickLine={false}
              axisLine={false}
              width={52}
              tick={{ fontSize: 12 }}
              tickFormatter={(v) => formatValue(v, unit, { compact: true })}
              // Always from zero for counts. A truncated y-axis on a count
              // makes a 3% change look like a collapse, and someone will
              // screenshot it into a board deck.
              domain={unit === "count" ? [0, "auto"] : ["auto", "auto"]}
            />
            <Tooltip
              content={<ChartTooltip unit={unit} timezone={timezone} granularity={granularity} />}
              cursor={{ stroke: "var(--chart-axis)", strokeWidth: 1, strokeOpacity: 0.4 }}
            />
            {series.length > 1 && (
              <Legend
                verticalAlign="top"
                align="left"
                height={28}
                iconType="plainline"
                wrapperStyle={{ fontSize: 12, paddingBottom: 8 }}
              />
            )}
            {series.map((s) => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.label}
                stroke={colorOf(s.key)}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--chart-surface)" }}
                // The trailing bucket is still filling (§1.2's `partial`).
                // Dashing it says "incomplete" without inventing a data point.
                strokeDasharray={s.partial_tail ? "0" : undefined}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* The table is not a fallback — it is the relief the contrast WARN
          obligates (§12.1), and it is how a screen-reader user reads the data
          at all. <details> keeps it out of the way for sighted users without
          hiding it from assistive tech. */}
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">
          View as table
        </summary>
        <ChartTable series={series} granularity={granularity} timezone={timezone} unit={unit} />
      </details>
    </figure>
  );
}

function describeChart(title: string, series: AnalyticsSeries[], unit: string): string {
  const parts = series.map(
    (s) => `${s.label}: ${formatValue(s.points.at(-1)?.v ?? 0, unit)} most recent`,
  );
  return `${title}. ${series.length} series. ${parts.join("; ")}. Full data in the table below.`;
}
```

The rules embedded above, stated plainly:

- **No dual axes, ever.** Two measures of different scale become two charts or
  an indexed comparison. A dual-axis chart lets the author position the
  crossover point anywhere they like, which makes it an argument rather than a
  measurement.
- **No 3D, no gradients under lines, no drop shadows, no pie charts above three
  slices.** Recharts makes all of these one prop away, which is precisely why
  the ban is written down.
- **`isAnimationActive={false}`.** Chart entry animation delays the reading of
  the data by 800 ms every single time and is meaningless on a re-render.
- **Direct labels where ≤ 4 series.** The line's own colour plus a label at its
  right end beats a legend the eye must ping-pong to.
- **`role="img"` with a real `aria-label`.** An SVG of `<path>` elements is
  nothing to a screen reader. The label carries the headline; the table carries
  the data.

### 12.4 Stat tiles and sparklines

A single number does not need a chart. `KpiTile` renders a hero number, a
delta, and an optional sparkline — no axes, no grid, no legend:

```tsx
// components/dashboard/KpiTile.tsx
"use client";

import { ArrowDown, ArrowUp, Minus } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Sparkline } from "@/components/charts/Sparkline";
import { formatValue } from "@/lib/charts/format";
import { cn } from "@/lib/utils";

export function KpiTile({
  label, value, delta, format = "count", loading, sparkline, hint,
  deltaGoodDirection = "up",
}: {
  label: string;
  value: number | undefined;
  delta?: number;
  format?: "count" | "percent" | "currency" | "duration";
  loading?: boolean;
  sparkline?: number[];
  hint?: string;
  deltaGoodDirection?: "up" | "down";
}) {
  const good =
    delta === undefined || delta === 0
      ? null
      : deltaGoodDirection === "up"
        ? delta > 0
        : delta < 0;
  const Icon = delta === undefined || delta === 0 ? Minus : delta > 0 ? ArrowUp : ArrowDown;

  return (
    <Card className="p-4">
      <p className="text-sm text-muted-foreground" title={hint}>{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">
        {loading ? <span className="inline-block h-8 w-20 animate-pulse rounded bg-muted" />
                 : formatValue(value ?? 0, format)}
      </p>
      {delta !== undefined && (
        <p
          className={cn(
            "mt-1 flex items-center gap-1 text-xs tabular-nums",
            good === null && "text-muted-foreground",
            good === true && "text-[var(--status-good)]",
            good === false && "text-[var(--status-serious)]",
          )}
        >
          {/* The arrow is the redundant encoding: direction is never carried
              by colour alone (WCAG 1.4.1). */}
          <Icon className="h-3 w-3" aria-hidden />
          {Math.abs(delta * 100).toFixed(1)}%
          <span className="text-muted-foreground">vs. previous period</span>
        </p>
      )}
      {sparkline && sparkline.length > 1 && (
        <Sparkline values={sparkline} className="mt-3 h-8 w-full" />
      )}
    </Card>
  );
}
```

### 12.5 Which form for which analytics question

| Question | Form | Why not the obvious alternative |
|---|---|---|
| Volume over time | line | A bar per hour over 30 days is 720 bars. |
| Volume by channel over time | stacked area | Composition *and* total in one read. |
| Outcome mix | horizontal bar, sorted | Not a pie: seven outcome kinds, and people compare lengths far better than angles. |
| Booking funnel | ordinal bar, one hue ramp | Not categorical — the stages are ordered, so the ramp must be too. |
| Latency distribution | histogram + p50/p95 markers | A mean latency line hides the tail, and the tail is the product problem (04 §7.2). |
| Cost per conversation | line + reference band | The band is the target range; a bare line has no notion of "fine". |
| Busiest hours | heatmap, sequential blue | Two categorical dimensions (weekday × hour) with one magnitude. |
| Containment rate | stat tile + sparkline | One number. A full chart for one number is chartjunk. |

---

## 13. Real-time UI

### 13.1 The SSE hook

```ts
// lib/realtime/useEventStream.ts
"use client";

import { useEffect, useRef, useState } from "react";

import { getAccessToken, refreshAccessToken } from "@/lib/auth/token-store";

type Handler = (type: string, data: Record<string, unknown>) => void;

/**
 * SSE with resume, backoff and a liveness timer.
 *
 * NOT the native EventSource. Three reasons, each disqualifying on its own:
 *
 *  1. EventSource cannot set an Authorization header, and 05 §A1 puts the
 *     access token there. The ticket workaround (§3.4) exists for the widget,
 *     which is a public browser context; inside the portal we already have a
 *     bearer token and should use it.
 *  2. EventSource's reconnect interval is browser-chosen and not jitterable.
 *     Ten thousand portals reconnecting in lockstep after a deploy is a
 *     thundering herd we can trivially avoid.
 *  3. A 401 mid-stream needs a token refresh and a resume from Last-Event-ID.
 *     EventSource reconnects on its own with the same dead credential, forever.
 */
export function useEventStream(url: string | null, onEvent: Handler) {
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const lastEventId = useRef<string | null>(null);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    if (!url) return;

    const abort = new AbortController();
    let attempt = 0;
    let stopped = false;

    const run = async () => {
      while (!stopped) {
        try {
          const headers: Record<string, string> = {
            Accept: "text/event-stream",
            Authorization: `Bearer ${getAccessToken() ?? ""}`,
          };
          if (lastEventId.current) headers["Last-Event-ID"] = lastEventId.current;

          const res = await fetch(url, { headers, signal: abort.signal });

          if (res.status === 401) {
            const token = await refreshAccessToken();
            if (!token) return;              // session genuinely gone
            continue;                        // retry immediately with the new token
          }
          if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);

          setConnected(true);
          setReconnecting(false);
          attempt = 0;

          await pump(res.body, (id, type, data) => {
            if (id) lastEventId.current = id;
            if (type === "ping") return;     // keepalive; not an application event
            handlerRef.current(type, data);
          });

          // Clean close. 04's publisher returns after 900 s idle; that is the
          // conversation going quiet, not a failure. Do not reconnect.
          if (!stopped) { setConnected(false); return; }
        } catch (error) {
          if (abort.signal.aborted || stopped) return;
          setConnected(false);
          setReconnecting(true);
          attempt += 1;
          // Exponential with FULL jitter. Half-jitter still leaves a visible
          // herd; full jitter spreads reconnects uniformly across the window.
          const delay = Math.min(500 * 2 ** attempt, 30_000) * Math.random();
          await new Promise((r) => setTimeout(r, delay));
        }
      }
    };

    void run();
    return () => {
      stopped = true;
      abort.abort();
      setConnected(false);
    };
  }, [url]);

  return { connected, reconnecting };
}

/** Minimal SSE frame parser. Handles multi-line data and the id/event fields. */
async function pump(
  body: ReadableStream<Uint8Array>,
  emit: (id: string | null, type: string, data: Record<string, unknown>) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });

    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      let id: string | null = null;
      let type = "message";
      const dataLines: string[] = [];

      for (const line of frame.split("\n")) {
        if (line.startsWith(":")) continue;              // comment / keepalive
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        const val = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "id") id = val;
        else if (field === "event") type = val;
        else if (field === "data") dataLines.push(val);
      }

      const raw = dataLines.join("\n");
      let data: Record<string, unknown> = {};
      if (raw) {
        try { data = JSON.parse(raw); } catch { data = { text: raw }; }
      }
      emit(id, type, data);
    }
  }
}
```

### 13.2 Live conversation monitoring

```tsx
// app/(app)/conversations/live/page.tsx
"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { LiveConversationCard } from "@/components/conversations/LiveConversationCard";
import { useEventStream } from "@/lib/realtime/useEventStream";
import { useKeys } from "@/lib/query/keys";
import { useLiveConversations } from "@/lib/query/conversations";

export default function LiveMonitorPage() {
  const qc = useQueryClient();
  const keys = useKeys();
  const live = useLiveConversations();

  const onEvent = useCallback(
    (type: string, data: Record<string, unknown>) => {
      switch (type) {
        case "snapshot":
          // §3.5: the endpoint emits the current list before subscribing, so
          // there is no window where an event fires between query and
          // subscribe. Seeding the cache from it avoids a second HTTP fetch.
          qc.setQueryData(keys.conversations.live(), data.conversations);
          break;
        case "conversation.started":
        case "conversation.ended":
        case "conversation.escalated":
          // Pub/sub can drop a message (§3.5). Invalidating rather than
          // patching means a dropped event costs one stale row for a few
          // seconds and self-heals, instead of leaving the list permanently
          // wrong in a way nothing corrects.
          qc.invalidateQueries({ queryKey: keys.conversations.live() });
          break;
      }
    },
    [qc, keys],
  );

  const { connected, reconnecting } = useEventStream(
    "/api/v1/conversations/live/events",
    onEvent,
  );

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Live conversations</h1>
          <p className="text-sm text-muted-foreground">
            {live.data?.length ?? 0} active right now
          </p>
        </div>
        <ConnectionBadge connected={connected} reconnecting={reconnecting} />
      </header>

      {/* aria-live="polite" so a screen reader hears conversations arriving
          and leaving. NOT "assertive": a busy clinic would interrupt the user
          mid-sentence every few seconds, which is worse than silence. */}
      <div
        aria-live="polite"
        aria-relevant="additions removals"
        className="grid gap-4 md:grid-cols-2 xl:grid-cols-3"
      >
        {live.data?.map((c) => <LiveConversationCard key={c.id} conversation={c} />)}
      </div>

      {live.data?.length === 0 && (
        <p className="rounded-lg border border-dashed p-10 text-center text-muted-foreground">
          No conversations in progress.
        </p>
      )}
    </div>
  );
}

function ConnectionBadge({ connected, reconnecting }: { connected: boolean; reconnecting: boolean }) {
  // A live view that has silently stopped being live is worse than no live
  // view, because it reads as "nothing is happening" rather than "I am
  // disconnected". The badge is not decoration.
  return (
    <span
      className="flex items-center gap-2 text-xs"
      role="status"
      aria-live="polite"
    >
      <span
        className={
          connected ? "h-2 w-2 rounded-full bg-[var(--status-good)]"
          : reconnecting ? "h-2 w-2 animate-pulse rounded-full bg-[var(--status-warning)]"
          : "h-2 w-2 rounded-full bg-[var(--status-critical)]"
        }
        aria-hidden
      />
      {connected ? "Live" : reconnecting ? "Reconnecting…" : "Disconnected"}
    </span>
  );
}
```

The card itself streams the conversation's own turns and offers the three
takeover actions from §3.6:

```tsx
// components/conversations/LiveConversationCard.tsx
"use client";

import { useState } from "react";

import { Can } from "@/components/auth/Can";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ChannelIcon } from "./ChannelIcon";
import { LiveDuration } from "./LiveDuration";
import { TakeoverDialog } from "./TakeoverDialog";
import { useConversationStream } from "@/lib/realtime/useConversationStream";
import type { Conversation } from "@/lib/api/types";

export function LiveConversationCard({ conversation }: { conversation: Conversation }) {
  const [expanded, setExpanded] = useState(false);
  const [takeover, setTakeover] = useState(false);

  // Only the expanded card subscribes. Twelve simultaneous SSE connections
  // would exhaust the browser's per-origin HTTP/1.1 connection budget — and
  // even over HTTP/2 it is twelve idle server tasks for twelve panels nobody
  // is reading.
  const stream = useConversationStream(conversation.id, expanded);

  return (
    <Card className="space-y-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ChannelIcon channel={conversation.channel} />
            <span className="truncate font-medium">
              {conversation.contact_name ?? conversation.contact_identifier ?? "Unknown caller"}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            <LiveDuration since={conversation.started_at} /> ·{" "}
            {conversation.turn_count} turns
            {conversation.escalated && (
              <span className="ml-1 text-[var(--status-serious)]">· escalated</span>
            )}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={() => setExpanded((e) => !e)}>
          {expanded ? "Collapse" : "Listen in"}
        </Button>
      </div>

      {expanded && (
        <>
          <div className="max-h-48 overflow-y-auto rounded bg-muted/50 p-2 text-sm">
            {stream.streamingText ? (
              <p>
                {stream.streamingText}
                <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-current align-middle" />
              </p>
            ) : stream.activeTool ? (
              <p className="text-muted-foreground">{stream.activeTool.label}…</p>
            ) : (
              <p className="text-muted-foreground">Listening…</p>
            )}
          </div>

          <Can perm="conversations:barge_in">
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => setTakeover(true)}>
                Take over
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setTakeover(true)}>
                Whisper
              </Button>
            </div>
          </Can>
        </>
      )}

      {takeover && (
        <TakeoverDialog
          conversationId={conversation.id}
          channel={conversation.channel}
          onClose={() => setTakeover(false)}
        />
      )}
    </Card>
  );
}
```

### 13.3 Active-call indicator

Always visible in the app shell when a voice call is in progress.

**Judgment call — the data source is Postgres and Redis, not Prometheus.**
04 §1.3.1 defines `receptionai_voice_active_sessions` as an HPA scaling signal,
and it is tempting to reuse it here. It is the wrong source for a UI: Prometheus
is scrape-interval-lagged (15–30 s), it aggregates across pods with lossy
resolution, and it carries no conversation ids — so it can tell you *how many*
calls exist but never *which*, and a user clicking "3 active calls" needs the
which. The indicator counts `conversations WHERE status IN ('active','idle')
AND channel IN ('voice_inbound','voice_outbound')` and stays current over the
`portal:{tenant_id}:conversations` channel.

```tsx
// components/shell/ActiveCallIndicator.tsx
"use client";

import Link from "next/link";
import { PhoneCall } from "lucide-react";

import { useLiveConversations } from "@/lib/query/conversations";

export function ActiveCallIndicator() {
  const live = useLiveConversations();
  const calls = live.data?.filter((c) => c.channel.startsWith("voice_")) ?? [];

  // Renders nothing at zero. A permanent "0 calls" chip is banner blindness
  // by construction — the region gets tuned out and is then invisible at the
  // one moment it matters.
  if (calls.length === 0) return null;

  return (
    <Link
      href="/conversations/live"
      className="flex items-center gap-2 rounded-full bg-[var(--status-good)]/10 px-3 py-1 text-sm text-[var(--status-good)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="relative flex h-2 w-2" aria-hidden>
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60 motion-reduce:animate-none" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-current" />
      </span>
      <PhoneCall className="h-3.5 w-3.5" aria-hidden />
      <span>
        {calls.length} call{calls.length === 1 ? "" : "s"} in progress
      </span>
    </Link>
  );
}
```

`motion-reduce:animate-none` on the ping is not decoration: a permanently
pulsing element is a vestibular trigger and, for some users, makes the rest of
the page unreadable (WCAG 2.3.3).

### 13.4 Manifest-publish invalidation

03 §4.4 already broadcasts `ai:graph:invalidate` on the Redis pub/sub bus when a
manifest is published. The portal needs the same signal for a different reason:
a `RenderDocument` cached for five minutes (§9.8) is stale the moment an admin
publishes, and the next form submit would 409 with `manifest_changed`.

The API bridges the existing channel to the portal's channel rather than
inventing a second event path, and the shell listens:

```ts
// In AppShell's portal event stream
case "manifest.published":
  qc.invalidateQueries({ queryKey: [tenantId, "form"] });
  qc.invalidateQueries({ queryKey: keys.navigation() });
  toast({
    title: "Configuration updated",
    description: "Forms and navigation have been refreshed.",
  });
  break;
```

This turns the 409 path from the common case into the genuine race it should
be: two admins editing simultaneously, rather than every admin whose tab was
open when someone published.

---

# PART C

## 14. Design system & accessibility

### 14.1 shadcn/ui usage

shadcn/ui is **copied into the repo, not installed**. That is its whole model
and it is the right one here: the components land in `components/ui/` as source
we own, so the accessibility fixes this section demands (`aria-disabled` over
`disabled`, focus-visible rings, `aria-live` regions) are edits to our files
rather than wrappers fighting a dependency.

The discipline that keeps that from becoming a mess:

- `components/ui/*` holds **unmodified-in-structure** primitives. Behaviour
  changes are allowed; API changes are not, so an upstream update is a
  reviewable diff rather than a rewrite.
- Anything with product knowledge lives elsewhere. `components/forms/`,
  `components/charts/`, `components/conversations/`. A `Button` does not know
  what a booking is.
- Radix primitives underneath are the reason this is viable at all: focus
  trapping, roving tabindex, escape handling, and `aria-*` wiring in dialogs,
  popovers, comboboxes and menus are solved by people who specialise in it. We
  do not hand-roll a modal.

### 14.2 Tokens

Semantic, never literal. `bg-destructive`, never `bg-red-500`.

```css
/* app/globals.css */
@layer base {
  :root {
    --background: 0 0% 100%;
    --foreground: 240 10% 4%;
    --card: 0 0% 100%;
    --card-foreground: 240 10% 4%;
    --primary: 213 68% 50%;          /* aligned to --chart-1 */
    --primary-foreground: 0 0% 100%;
    --secondary: 240 5% 96%;
    --muted: 240 5% 96%;
    --muted-foreground: 240 4% 32%;  /* 4.6:1 on --background */
    --accent: 240 5% 96%;
    --destructive: 0 72% 51%;
    --destructive-foreground: 0 0% 100%;
    --border: 240 6% 90%;
    --input: 240 6% 90%;
    --ring: 213 68% 50%;
    --radius: 0.5rem;
  }

  .dark {
    --background: 60 2% 10%;         /* #1a1a19, the chart surface */
    --foreground: 0 0% 100%;
    --card: 60 2% 13%;
    --card-foreground: 0 0% 100%;
    --primary: 213 76% 56%;
    --primary-foreground: 60 2% 10%;
    --secondary: 60 2% 18%;
    --muted: 60 2% 18%;
    --muted-foreground: 55 8% 74%;   /* #c3c2b7, 9.2:1 on --background */
    --accent: 60 2% 18%;
    --destructive: 0 70% 65%;
    --destructive-foreground: 60 2% 10%;
    --border: 60 2% 22%;
    --input: 60 2% 22%;
    --ring: 213 76% 56%;
  }
}
```

`--muted-foreground` is the token people get wrong. The tempting value is a
light grey that looks elegant and measures 3.1:1 — which fails WCAG 1.4.3 for
body text. Both values above clear 4.5:1 against their background, and they
were measured, not eyeballed. A CI check asserts it so a future palette tweak
cannot quietly break it:

```ts
// tests/tokens.contrast.test.ts
import { describe, expect, it } from "vitest";
import { contrastRatio, hslToRgb } from "@/lib/color";

const PAIRS = [
  ["--muted-foreground", "--background", 4.5],
  ["--foreground", "--background", 4.5],
  ["--primary-foreground", "--primary", 4.5],
  ["--destructive-foreground", "--destructive", 4.5],
  ["--border", "--background", 1.5],   // non-text: 3:1 for controls, 1.5 for dividers
] as const;

describe.each(["light", "dark"] as const)("%s theme token contrast", (mode) => {
  it.each(PAIRS)("%s on %s is at least %s:1", (fg, bg, min) => {
    expect(contrastRatio(hslToRgb(token(mode, fg)), hslToRgb(token(mode, bg))))
      .toBeGreaterThanOrEqual(min);
  });
});
```

### 14.3 Dark mode

Class-based, `next-themes`, with `defaultTheme="system"` and a three-way toggle
(light / dark / system). No flash of the wrong theme: the theme class is set by
a blocking inline script before paint, which is the one place a render-blocking
script is correct.

Dark mode is **selected, not derived**. Chart colours are re-stepped for the
dark surface (§12.1) rather than filtered, and `--background` in dark is
`#1a1a19` rather than pure black — a true-black background maximises halation
around light text on OLED and is measurably harder to read for long sessions.

### 14.4 Accessibility requirements — WCAG 2.2 AA

Not a checklist to audit against later. These are the specific requirements
this product's specific screens generate.

**Keyboard navigation.**

| Requirement | Where it bites |
|---|---|
| Every action reachable without a mouse | The knowledge drop zone (§10.10) — a drop-only target excludes every keyboard user from a primary task. Solved with a focusable role + hidden input. |
| Visible focus, ≥3:1 against adjacent colour (2.4.11 Focus Appearance) | `--ring` is `--primary`, which clears 3:1 in both themes. `focus-visible` only — a persistent ring on mouse click is noise. |
| Focus never trapped except in a modal | Radix Dialog handles trap + restore. |
| No keyboard shortcut without a modifier (2.1.4) | The command palette is `⌘K`/`Ctrl+K`, never bare `k` — a single-letter shortcut fires while a speech-input user dictates. |
| Skip link | First focusable element in `AppShell`, visually hidden until focused. |
| Dragging has a non-drag alternative (2.5.7) | Service reordering has explicit up/down buttons alongside drag. |
| Targets ≥24×24 CSS px (2.5.8) | The inline table toggles and the transcript seek affordances are the ones that fail by default. |

**Focus management in the form renderer.** The one that most implementations
get wrong, and where §9 spends real effort:

1. **On submit with errors, focus moves to the error summary**, not to the
   first invalid field. The summary is `tabIndex={-1}`, `role="alert"`, and
   announces "3 problems to fix" with a list of jump links. Jumping straight to
   field one hides the scale of the problem from a screen-reader user, who then
   fixes one error, resubmits, and discovers a second — three times.

2. **The submit button is never `disabled` on validation errors.** A disabled
   submit is unfocusable and announces nothing, so a keyboard user tabs to it,
   nothing happens, and there is no path to discovering why. Submitting and
   surfacing the summary is the accessible pattern (WCAG 3.3.1). §9.4 does
   this.

3. **Conditional fields appearing must not steal focus.** A `visible_when`
   revealing three fields while the user is typing must not move the caret.
   Focus stays put; the new fields are announced by an `aria-live="polite"`
   region that says "3 additional fields added".

4. **Fields removed by a `visible_when` while focused** move focus to the
   preceding visible field rather than letting it fall to `<body>` — where a
   screen reader loses its place entirely and the user restarts from the top of
   the page.

```tsx
// components/forms/ErrorSummary.tsx
"use client";

import { forwardRef } from "react";

export const ErrorSummary = forwardRef<
  HTMLDivElement,
  { count: number; errors: Array<{ key: string; label: string; message: string }>;
    onJump: (key: string) => void }
>(function ErrorSummary({ count, errors, onJump }, ref) {
  if (count === 0) return null;

  return (
    <div
      ref={ref}
      // tabIndex -1 makes it programmatically focusable without adding it to
      // the tab order. role="alert" makes the announcement immediate — this is
      // the one place "assertive" is correct, because the user just pressed
      // submit and is waiting for exactly this answer.
      tabIndex={-1}
      role="alert"
      className="rounded-lg border border-destructive bg-destructive/5 p-4 focus:outline-none focus:ring-2 focus:ring-destructive"
    >
      <h2 className="font-medium text-destructive">
        There {count === 1 ? "is 1 problem" : `are ${count} problems`} to fix
      </h2>
      <ul className="mt-2 space-y-1 text-sm">
        {errors.map((e) => (
          <li key={e.key}>
            <button
              type="button"
              className="text-destructive underline underline-offset-2"
              onClick={() => onJump(e.key)}
            >
              {e.label}: {e.message}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
});
```

**Screen-reader announcements for streaming content.** The hardest requirement
in the product, because naive `aria-live` on a token stream is catastrophic.

The failure: put `aria-live="polite"` on the element accumulating tokens, and
every token mutation queues an announcement. A screen reader reads "The", then
"The next", then "The next available", then "The next available appointment" —
a 40-word answer becomes several hundred spoken fragments and the user hears
nothing usable.

The fix is to **separate the visual stream from the announced stream**:

```tsx
// components/conversations/StreamingMessage.tsx
"use client";

import { useEffect, useRef, useState } from "react";

export function StreamingMessage({
  text, done, status,
}: {
  text: string;
  done: boolean;
  status: "idle" | "thinking" | "typing";
}) {
  const [announced, setAnnounced] = useState("");
  const flushed = useRef(0);

  useEffect(() => {
    if (done) {
      // On completion, announce whatever remains unannounced.
      if (text.length > flushed.current) {
        setAnnounced(text.slice(flushed.current));
        flushed.current = text.length;
      }
      return;
    }

    // Mid-stream, announce at SENTENCE boundaries only. A sentence is the
    // smallest unit a listener can act on; anything finer is noise, anything
    // coarser makes a long answer feel frozen.
    const pending = text.slice(flushed.current);
    const lastBoundary = Math.max(
      pending.lastIndexOf(". "), pending.lastIndexOf("? "), pending.lastIndexOf("! "),
      pending.lastIndexOf(".\n"), pending.lastIndexOf("\n\n"),
    );
    if (lastBoundary > 0) {
      const chunk = pending.slice(0, lastBoundary + 1);
      setAnnounced(chunk);
      flushed.current += chunk.length;
    }
  }, [text, done]);

  useEffect(() => {
    if (done) flushed.current = 0;
  }, [done]);

  return (
    <>
      {/* Visual: updates every token, hidden from assistive tech so it cannot
          generate its own announcements. */}
      <p aria-hidden="true">
        {text}
        {!done && (
          <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-current align-middle motion-reduce:animate-none" />
        )}
      </p>

      {/* Announced: sentence-at-a-time, polite so it never interrupts the
          user's own navigation. atomic=false so only the added text is read,
          not the whole accumulated paragraph on every update. */}
      <div className="sr-only" aria-live="polite" aria-atomic="false">
        {announced}
      </div>

      {/* Status is a separate live region. Merging it into the text region
          would interleave "Checking the calendar" into the middle of a
          sentence being read out. */}
      <div className="sr-only" role="status" aria-live="polite">
        {status === "thinking" ? "Assistant is checking information"
          : status === "typing" ? "Assistant is responding"
          : ""}
      </div>
    </>
  );
}
```

**Colour is never the only channel** (1.4.1). Every place it would be:

| Signal | Colour | Plus |
|---|---|---|
| Booking status | badge tint | text label |
| Delta direction | green/amber | arrow icon (§12.4) |
| Over-quota meter | red fill | diagonal pattern + "over allowance" text (§10.17) |
| Connector health | dot colour | text + icon |
| Chart series | line colour | legend + direct labels + table view (§12.3) |
| Form field error | red border | error text + `aria-invalid` + `aria-describedby` |
| Live/disconnected | dot colour | text (§13.2) |

**Motion.** `prefers-reduced-motion` disables the live-call ping, the streaming
caret, chart animation (already off), and all transitions above 200 ms. Applied
globally rather than per-component so nothing is missed:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

**Other WCAG 2.2 specifics that apply here:**

- **3.3.7 Redundant Entry** — the onboarding wizard never re-asks for
  information a previous step collected. Where a value is genuinely needed
  twice (business phone as the fallback transfer number), it is pre-filled.
- **3.3.8 Accessible Authentication** — no cognitive-function test in the login
  path. The password field allows paste (password managers), and TOTP entry
  allows paste of the whole code.
- **2.4.11 Focus Not Obscured** — the sticky app header uses
  `scroll-margin-top` on focusable content so a keyboard user tabbing down a
  long form never lands behind it.
- **1.4.10 Reflow** — every screen works at 320 px wide with no horizontal
  scroll. Wide content (the audit table, the transcript, charts) scrolls inside
  its own `overflow-x: auto` container; the page body never does.
- **1.4.12 Text Spacing** — no fixed heights on text containers, so a user
  stylesheet increasing line height does not clip content.

---

## 15. Frontend testing

Three layers, each answering a question the others cannot.

### 15.1 Component tests — Vitest + Testing Library

Behaviour, not implementation. No shallow rendering, no snapshot tests of
markup (a snapshot diff tells you something changed, never whether it broke).

**The form renderer is where the density is**, because it is the component
whose failure is silent:

```ts
// components/forms/__tests__/DynamicForm.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DynamicForm } from "../DynamicForm";
import { medicalIntake, restaurantBooking } from "@/test/fixtures/render-documents";

describe("DynamicForm", () => {
  it("renders a medical intake with no vertical-specific code", () => {
    render(<DynamicForm doc={medicalIntake} onSubmit={vi.fn()} />);
    expect(screen.getByLabelText(/date of birth/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/insurance provider/i)).toBeInTheDocument();
  });

  it("renders a restaurant booking through the SAME component", () => {
    // THE test for "a new vertical needs no new screens". If this ever needs a
    // new widget, the manifest schema grew in a way 02 §7.2 says should have
    // mapped onto an existing widget — and that conversation belongs in
    // packages/verticals, not here.
    render(<DynamicForm doc={restaurantBooking} onSubmit={vi.fn()} />);
    expect(screen.getByLabelText(/party size/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/high chair/i)).toBeInTheDocument();
  });

  it("hides a field whose visible_when predicate is false", async () => {
    render(<DynamicForm doc={medicalIntake} onSubmit={vi.fn()} />);
    expect(screen.queryByLabelText(/policy number/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByLabelText(/has insurance/i));
    expect(await screen.findByLabelText(/policy number/i)).toBeInTheDocument();
  });

  it("excludes hidden fields from the submitted payload", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(<DynamicForm doc={medicalIntake} onSubmit={onSubmit} />);

    await userEvent.type(screen.getByLabelText(/full name/i), "J. Okonkwo");
    await userEvent.type(screen.getByLabelText(/date of birth/i), "1985-04-12");
    await userEvent.click(screen.getByRole("button", { name: /submit/i }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    // Not `policy_number: null` — absent. A field the user never saw has no
    // value, and asserting "answered nothing" is a different claim.
    expect(onSubmit.mock.calls[0][0]).not.toHaveProperty("policy_number");
  });

  it("never evaluates server_only rules on the client", async () => {
    const doc = withRule(medicalIntake, {
      id: "email.deliverable", kind: "validate", target_field: "email",
      predicate: { checks: [{ check: "email_deliverable" }] },
      message: "This email cannot receive mail.", server_only: true,
    });
    render(<DynamicForm doc={doc} onSubmit={vi.fn()} />);
    await userEvent.type(screen.getByLabelText(/email/i), "nobody@example.invalid");
    await userEvent.tab();
    // Guessing at a server_only rule produces a form that says "valid" and
    // then fails on submit — worse than saying nothing (02 §7.4 rule 1).
    expect(screen.queryByText(/cannot receive mail/i)).not.toBeInTheDocument();
  });

  it("falls back to a usable control for an unknown widget", () => {
    const doc = withField(medicalIntake, {
      key: "future_thing", widget: "quantum_selector" as never,
      input_type: "text", label: "Future field", required: false,
    });
    render(<DynamicForm doc={doc} onSubmit={vi.fn()} />);
    // Not a crash, not an empty space, not a silent drop.
    expect(screen.getByLabelText(/future field/i)).toBeInTheDocument();
    expect(screen.getByText(/does not have yet/i)).toBeInTheDocument();
  });

  it("moves focus to the error summary on a blocked submit", async () => {
    render(<DynamicForm doc={medicalIntake} onSubmit={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /submit/i }));
    const summary = await screen.findByRole("alert");
    await waitFor(() => expect(summary).toHaveFocus());
  });

  it("keeps the submit button enabled when there are errors", async () => {
    render(<DynamicForm doc={medicalIntake} onSubmit={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /submit/i }));
    // A disabled submit is unfocusable and announces nothing (§14.4).
    expect(screen.getByRole("button", { name: /submit/i })).toBeEnabled();
  });

  it("preserves surviving values when the render document changes", async () => {
    const { rerender } = render(<DynamicForm doc={medicalIntake} onSubmit={vi.fn()} />);
    await userEvent.type(screen.getByLabelText(/full name/i), "J. Okonkwo");
    rerender(<DynamicForm doc={{ ...medicalIntake, meta: { ...medicalIntake.meta, resolved_hash: "new" } }} onSubmit={vi.fn()} />);
    expect(screen.getByLabelText(/full name/i)).toHaveValue("J. Okonkwo");
  });
});
```

**The predicate evaluator is tested against a shared fixture file that the
Python suite also reads.** This is the single most important test in the
frontend, because a client/server divergence presents as "the field was hidden
but the server says it is required" with nothing in either codebase looking
wrong:

```ts
// lib/forms/__tests__/predicate.parity.test.ts
import fixtures from "../../../../../tests/fixtures/predicates.json";
import { describe, expect, it } from "vitest";

import { evaluatePredicate } from "../predicate";

describe("predicate parity with packages/verticals/predicate.py", () => {
  it.each(fixtures.cases)("$name", ({ predicate, values, expected }) => {
    expect(evaluatePredicate(predicate, values, { onUnknown: true })).toBe(expected);
  });
});
```

```python
# tests/verticals/test_predicate_parity.py — reads the SAME file
import json
from pathlib import Path

import pytest

from packages.verticals.predicate import evaluate

FIXTURES = json.loads(Path("tests/fixtures/predicates.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_predicate_matches_typescript(case):
    """One fixture file, two implementations, one expected answer.

    Adding a case to the file forces both implementations to agree, and adding
    an operator to 02 §1.9 without a fixture case fails a coverage assertion
    below — which is how the two stay in sync as the vocabulary grows.
    """
    assert evaluate(case["predicate"], case["values"]) is case["expected"]


def test_every_operator_has_a_fixture():
    from packages.verticals.models import PredicateOp
    covered = {c["predicate"].get("op") for c in FIXTURES["cases"]}
    missing = set(PredicateOp.__args__) - covered
    assert not missing, f"operators with no parity fixture: {sorted(missing)}"
```

Also at this layer: `Can` gating for each role, `MoneyInput` decimal safety,
`KpiTile` delta direction, and every widget's `aria-describedby` wiring. Each
component test file runs `axe` on its rendered output:

```ts
// test/setup.ts
import { axe, toHaveNoViolations } from "jest-axe";
expect.extend(toHaveNoViolations);

export async function expectNoA11yViolations(container: HTMLElement) {
  expect(await axe(container)).toHaveNoViolations();
}
```

### 15.2 Integration tests — MSW

MSW intercepts at the network layer, so the code under test is the real fetch
wrapper, the real React Query configuration, and the real cache. Handlers are
generated from the committed `openapi.json` (§4.3), so a mock cannot drift from
the contract — the most common failure of a hand-written mock layer is that it
returns a shape the API stopped returning six months ago.

```ts
// test/msw/handlers.ts
import { HttpResponse, http } from "msw";

import { problem } from "./problem";

export const handlers = [
  http.get("/api/v1/services", ({ request }) => {
    const cursor = new URL(request.url).searchParams.get("cursor");
    return HttpResponse.json(
      cursor
        ? { items: servicesPage2, next_cursor: null, has_more: false }
        : { items: servicesPage1, next_cursor: "eyJ2IjoxfQ", has_more: true },
    );
  }),

  http.patch("/api/v1/services/:id", async ({ request, params }) => {
    // If-Match is REQUIRED (§2.6). The mock enforces it, so a component that
    // forgets it fails here rather than in production with a 428.
    const ifMatch = request.headers.get("If-Match");
    if (!ifMatch) {
      return problem(428, "precondition_required", "If-Match header is required");
    }
    if (ifMatch !== '"3"') {
      return problem(412, "version_conflict", "The record changed since you loaded it", {
        expected_version: Number(ifMatch.replaceAll('"', "")),
        current_version: 4,
        current: { ...serviceFixture, version: 4, name: "Changed elsewhere" },
      });
    }
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...serviceFixture, ...body, version: 4 });
  }),

  http.post("/api/v1/bookings", async ({ request }) => {
    if (!request.headers.get("Idempotency-Key")) {
      return problem(400, "idempotency_key_required", "Idempotency-Key header is required");
    }
    const body = (await request.json()) as { starts_at: string };
    if (body.starts_at === CONTESTED_SLOT) {
      return problem(409, "slot_unavailable", "The requested time is no longer available", {
        conflict_kind: "overlap",
        alternatives: [
          { starts_at: "2026-08-12T14:30:00Z", ends_at: "2026-08-12T15:00:00Z", resource_ids: [RESOURCE] },
        ],
      });
    }
    return HttpResponse.json(bookingFixture, { status: 201, headers: { ETag: '"1"' } });
  }),
];
```

The scenarios this layer covers, chosen because each is a real bug class:

| Scenario | What it proves |
|---|---|
| Optimistic toggle rolls back on 412 | Every snapshot restores, not just the visible list (§11.3) |
| 409 `slot_unavailable` renders alternatives | The recovery path exists, not just the error |
| 409 `manifest_changed` re-renders with the new doc | 02 §7.4 rule 2 is implemented, and typed values survive |
| 401 mid-flight triggers exactly one refresh | Single-flight holds under eight concurrent queries |
| Refresh failure clears the session once | No redirect loop |
| Cursor pagination appends without duplicates | `getNextPageParam` maps to `next_cursor` correctly |
| Switching tenants empties the cache | **The security test.** Below. |
| SSE reconnect resumes from `Last-Event-ID` | §3.3, including `seq` deduplication |
| `stream_gap` falls back to the durable record | §3.3 consequence 2 |
| Rate-limit 429 surfaces `Retry-After` | The upsell path when `scope === "tenant"` |

The cache-isolation test, written as a security test because that is what it is:

```ts
// test/integration/tenant-isolation.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderWithSession } from "@/test/render";
import { server } from "@/test/msw/server";

describe("tenant cache isolation", () => {
  it("never serves tenant A's data to tenant B", async () => {
    const { rerender } = renderWithSession(<ServicesPage />, { tenantId: TENANT_A });
    expect(await screen.findByText("Root Canal")).toBeInTheDocument();

    // Tenant B's handler returns a completely different catalogue.
    server.use(servicesHandlerFor(TENANT_B));
    rerender(<ServicesPage />, { tenantId: TENANT_B });

    // Not "eventually replaced" — never present. A flash of the previous
    // tenant's data IS the breach, even if it self-corrects in 200 ms:
    // a screenshot, a screen reader, or a slow render makes it real.
    expect(screen.queryByText("Root Canal")).not.toBeInTheDocument();
    expect(await screen.findByText("Table for Two")).toBeInTheDocument();
  });

  it("throws in development when a query key is not tenant-scoped", () => {
    expect(() =>
      renderWithSession(<BadlyKeyedComponent />, { tenantId: TENANT_A }),
    ).toThrow(/not tenant-scoped/);
  });

  it("never sends a tenant identifier to the API", async () => {
    const seen: Request[] = [];
    server.events.on("request:start", ({ request }) => seen.push(request));

    renderWithSession(<ServicesPage />, { tenantId: TENANT_A });
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));

    for (const request of seen) {
      const url = new URL(request.url);
      expect(url.searchParams.has("tenant_id")).toBe(false);
      expect(url.pathname).not.toContain(TENANT_A);
      expect(request.headers.get("X-Tenant-Id")).toBeNull();
    }
  });
});
```

### 15.3 E2E — Playwright

Real browser, real Next.js build, real FastAPI, real Postgres with RLS enabled,
real Redis, and a fake Temporal. Nothing is mocked below the network boundary,
because the bugs this layer exists to catch — a middleware ordering mistake, a
cookie path typo, an RLS policy gap — are invisible to a mocked stack.

**Critical flows, enumerated:**

1. **Login → tenant select → dashboard.** Including the OIDC path against a
   local mock IdP, and the `mts` picker when a user belongs to two tenants.
2. **Session refresh mid-session.** Freeze the clock past 10 minutes, act, and
   assert the user is not logged out and exactly one refresh occurred.
3. **Refresh-token reuse revokes the family.** Replay a consumed refresh token
   and assert the session is dead — 05 §A1.4's reuse detection, from the
   client's side.
4. **Onboarding, end to end.** Pick a vertical, complete every manifest-driven
   step, and land on a dashboard with a working agent.
5. **Create a booking, availability search through to confirmation.** Including
   the hold, and asserting the hold is released when the form is abandoned.
6. **Double-booking is impossible.** Two browser contexts race the same slot;
   exactly one gets 201, the other gets a 409 rendering alternatives, and
   clicking an alternative succeeds. This is the test that proves 01 §13's
   exclusion constraint reaches the user correctly.
7. **Reschedule and cancel**, including the cancellation-window policy block
   and the `bookings:override` path for an owner.
8. **Dynamic form for a second vertical.** Install the restaurant manifest and
   assert its intake form renders with zero frontend deploys — roadmap Phase
   4's checkpoint, mechanised.
9. **Knowledge upload → ingestion → searchable.** Upload a PDF, watch SSE
   progress through `queued → running → succeeded`, then find its content in
   the playground.
10. **Conversation transcript + recording playback.** Including word-level seek
    and the masked-PII view for a `viewer`.
11. **Live monitoring and takeover.** A scripted conversation appears in the
    live list, an agent takes over, and the turn provenance records the staff
    member.
12. **RBAC matrix.** Every role logs in and the nav, action bars, and gated
    controls match §8.5's table.
13. **A `viewer` calling a mutation endpoint directly gets 403.** The proof
    that hidden controls are UX and the server is the authority.
14. **Step-up.** An admin with `aal=1` clicks "Create API key", completes MFA,
    and lands back on the action.
15. **Optimistic rollback.** Toggle a service with a stale version and assert
    the UI reverts and explains.
16. **Keyboard-only traversal of the booking form.** No mouse events at all.
17. **Dark mode across every route.** Screenshot comparison plus a token
    contrast assertion.
18. **Axe scan on every route**, light and dark, at 320 px and 1440 px.

```ts
// e2e/booking-race.spec.ts
import { expect, test } from "@playwright/test";

import { loginAs, seedTenant } from "./helpers";

test("two staff racing the same slot: exactly one wins, the loser recovers", async ({
  browser,
}) => {
  const { serviceId, slot } = await seedTenant("dental");

  const [a, b] = await Promise.all([browser.newContext(), browser.newContext()]);
  const [pa, pb] = await Promise.all([a.newPage(), b.newPage()]);
  await Promise.all([loginAs(pa, "manager@a.test"), loginAs(pb, "manager2@a.test")]);

  for (const page of [pa, pb]) {
    await page.goto(`/bookings/new?service=${serviceId}`);
    await page.getByRole("button", { name: slot.label }).click();
    await page.getByLabel("Full name").fill("Race Test");
    await page.getByLabel("Phone").fill("+447700900123");
  }

  // Both submit within the same event-loop tick. The winner is decided by
  // Postgres's exclusion constraint (01 §5.10), not by anything in the app.
  const [ra, rb] = await Promise.all([
    pa.getByRole("button", { name: "Confirm booking" }).click().then(() => pa),
    pb.getByRole("button", { name: "Confirm booking" }).click().then(() => pb),
  ]);

  const outcomes = await Promise.all(
    [ra, rb].map(async (page) => ({
      page,
      confirmed: await page.getByText(/booking confirmed/i).isVisible().catch(() => false),
    })),
  );

  expect(outcomes.filter((o) => o.confirmed)).toHaveLength(1);

  const loser = outcomes.find((o) => !o.confirmed)!.page;
  await expect(loser.getByText(/no longer available/i)).toBeVisible();
  // The recovery path is the point: an error with no next step is a dead end
  // for a receptionist with a customer on the phone.
  const alternative = loser.getByRole("button", { name: /14:30/ });
  await expect(alternative).toBeVisible();
  await alternative.click();
  await loser.getByRole("button", { name: "Confirm booking" }).click();
  await expect(loser.getByText(/booking confirmed/i)).toBeVisible();
});
```

```ts
// e2e/rbac.spec.ts
import { expect, test } from "@playwright/test";

import { apiRequestAs, loginAs } from "./helpers";

const HIDDEN_FOR_VIEWER = ["/catalogue/services", "/agent", "/settings/members"];

test.describe("viewer", () => {
  test("sees no management navigation", async ({ page }) => {
    await loginAs(page, "viewer@a.test");
    for (const href of HIDDEN_FOR_VIEWER) {
      await expect(page.getByRole("link", { name: new RegExp(href.split("/").pop()!, "i") }))
        .toHaveCount(0);
    }
  });

  test("cannot mutate even by calling the API directly", async ({ request }) => {
    // Hiding a control is UX. THIS is the authorization test.
    const res = await apiRequestAs(request, "viewer@a.test", {
      method: "POST",
      url: "/api/v1/services",
      data: { code: "sneaky", name: "Sneaky" },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    expect(res.status()).toBe(403);
    const body = await res.json();
    expect(body.code).toBe("forbidden");
    expect(body.missing_permissions).toContain("bookings:update");
  });

  test("sees masked PII with an explanation, not bare REDACTED", async ({ page }) => {
    await loginAs(page, "viewer@a.test");
    await page.goto("/conversations");
    await page.getByRole("link", { name: /root canal enquiry/i }).click();
    await expect(page.getByText(/personal details are hidden/i)).toBeVisible();
    await expect(page.getByText("+447700900123")).toHaveCount(0);
  });
});
```

### 15.4 What is deliberately not tested

- **Visual regression on every component.** Chromatic-style snapshots on a
  design system this size generate more false positives than caught bugs. Six
  screenshot tests exist, on the six screens where layout is load-bearing:
  dashboard, transcript viewer, booking form, weekly hours grid, analytics
  overview, and the dynamic form with all thirteen widgets.
- **Unit tests for shadcn primitives.** They are Radix underneath; testing that
  a Dialog traps focus is testing Radix.
- **Mocking React Query.** Tests use a real `QueryClient` with `retry: false`.
  A mocked cache tests the mock.

### 15.5 CI wiring

```yaml
# .github/workflows/web.yml (excerpt)
  web:
    steps:
      - run: npm ci
      - run: npm run api:check          # generated types match openapi.json (§4.3)
      - run: npm run lint               # includes the tenant-key and PII-leak rules
      - run: npx tsc --noEmit
      - run: npm run test:unit -- --coverage
      - run: npm run test:integration
      - run: npm run build
      - run: npx playwright test
      - run: npm run test:a11y          # axe across every route, both themes

    # Coverage thresholds are per-directory, not global. A global 80% lets the
    # form renderer sit at 40% while a barrel file of re-exports sits at 100%.
    # These four directories are where a silent bug is most expensive.
```

| Path | Line coverage floor |
|---|---|
| `lib/forms/**` | 95% |
| `components/forms/**` | 90% |
| `lib/query/**` | 90% |
| `lib/auth/**` | 95% |
| everything else | 70% |

---

## 16. Inconsistencies found in upstream documents

Flagged rather than silently worked around, per the reconciliation discipline
the other documents follow. Each names the documents involved and what 06 did.

### 16.1 `Customer` / `CustomerIdentity` has no table — **04 vs 01**

04 §11.2 specifies a cross-channel identity model:

```
Customer                       (the human, tenant-scoped)
  └─ CustomerIdentity[]        (one per channel handle)
       ├─ channel, identifier_hash, identifier_enc, verified, link_method, ...
```

with a per-tenant pepper, HMAC lookup keys, and reversible linking. 01's
Appendix A table inventory contains **neither table**. Customer identity is
denormalized onto `bookings` (`customer_name`, `customer_phone_e164`,
`customer_email`), `waitlist_entries`, and `conversations` (`customer_id`,
`contact_identifier`, `contact_hash`), where `customer_id` is explicitly *not*
a foreign key.

The `contact_hash BYTEA` column on `conversations` looks like a partial
implementation of 04's `identifier_hash`, but there is no table for it to
point at and no pepper column on `tenants`.

**What 06 did:** declined to expose `/api/v1/customers`. An API for a table
that does not exist would be the worst outcome — it would look implemented.
The booking and conversation resources carry denormalized customer fields, as
the schema actually does.

**Recommended resolution (owner: 01):** either add `customers` and
`customer_identities` to 01 §5 with the pepper on `tenants`, or amend 04 §11 to
state that cross-channel identity resolution is deferred and `contact_hash` is
currently unjoined. The second is cheaper and probably correct for Phase 1 —
04 §11.1 itself notes this is "regulated activity", which is an argument for
deferring it deliberately rather than half-building it.

### 16.2 Three different error envelopes — **within 05**

05 emits errors in three incompatible shapes:

1. `TenantContextMiddleware._deny` → `{"error", "detail", "request_id"}`
2. `requires()` raising `HTTPException(403, {"error": "forbidden", ...})` →
   FastAPI wraps it as `{"detail": {"error": "forbidden", ...}}`
3. `RateLimitMiddleware` 429 → `{"error", "scope", "retry_after_ms", "upgrade_url"}`

A client switching on `body.error` works for 1 and 3 and silently fails for 2,
where the code is nested one level deeper. 05 does not mention RFC 9457.

**What 06 did:** §2.7 normalises all three into `application/problem+json` with
a handler that unwraps FastAPI's `detail` dict. The `error` values become the
`code` field verbatim, because 05 §A3.4 says clients switch on those strings —
renaming them would be a breaking change to a contract 05 already froze.

### 16.3 Two names for the DB session dependency — **01 vs 05**

01 §15 (`apps/api/dependencies.py`) defines `tenant_context()` and
`db_session()` with `DbSession = Annotated[TenantSession, Depends(db_session)]`.
05 §A3.6 (`apps/api/deps/db.py`) defines `get_session()` with
`DbSession = Annotated[AsyncSession, Depends(get_session)]`. Different modules,
different function names, different annotated types, same alias.

They also differ substantively: 01's reads `principal.shard_key` and
`principal.impersonator_user_id`, while 05's `Principal` dataclass has neither
field — it has `acting_as` and no shard key at all. And 05's `tenant_session`
sets a third GUC, `app.scope_all_conversations`, that 01's `unit_of_work` does
not.

**What 06 did:** used the alias `DbSession`, which both agree on, and cited 05
§A3.6 as the definition site since it is the more recent and richer version.
Router code in §5 is written so either implementation satisfies it.

**Recommended resolution (owner: 01 or 05):** one module. 05's is closer to
complete; 01's `TenantContext.shard_key` needs to reach `Principal` for the
contract §2 escape hatch (`ConnectionRouter` per-tenant shard routing) to
function at all, so the merged version needs `shard_key` added to `Principal`.

### 16.4 `Principal(plan=...)` does not compile — **within 05**

05 §A3.2's dataclass field is `tenant_plan: str`. 05 §A1.7's
`authenticate_api_key` constructs `Principal(..., plan=row.tenant_plan)` and
passes `roles=[]` where the field is `tuple[str, ...]`. That is a `TypeError`
at runtime.

**What 06 did:** used `tenant_plan` throughout. Noted here because it is a real
defect in code 05 presents as complete, and mypy would catch it.

### 16.5 Appendix A counts 39 tenant-owned tables and lists 40 — **within 01**

The heading says "39 tables"; the list enumerates 40, ending with
`notifications`. The stated total of 53 is consistent with 40, so the heading
is the typo.

### 16.6 Redis pub/sub versus Streams — **contract §1 vs 04 §9.1**

Contract §1 says Redis covers "cache, rate limits, **pub/sub** for realtime
fan-out". 04 §9.1 explicitly rejects pub/sub for conversation streaming in
favour of Streams, with a sound rationale (pub/sub drops messages to
disconnected subscribers; `Last-Event-ID` needs a cursor).

Not a contradiction so much as an under-specification in the contract — both
are Redis, which is what §1's table is actually deciding. But a reader
implementing the portal from §1 alone would build the wrong thing.

**What 06 did:** §3.5 uses Streams for conversation token streams (following
04) and pub/sub for portal state announcements (live list, ingestion progress,
manifest publish), with the split argued explicitly: Streams for narratives
where a gap is data loss, pub/sub for state announcements where the truth is
re-derivable by a query.

### 16.7 Two undefined names that 06 had to define

Not contradictions — genuine gaps where a document references a symbol it never
specifies, and 06 is the natural owner.

| Symbol | Referenced in | Defined in 06 |
|---|---|---|
| `CursorPage[T]`, `decode_cursor`, `page_from` | 05 §B8 | §2.2 |
| `StreamEvent`, `_normalize` | 03 §4.5 | §3.2 |
| SSE event vocabulary and payloads | 04 §9.1 (framing only) | §3.2 |
| Web-chat WebSocket protocol | 04 §9.1 (asserted, unspecified) | §3.4 |
| Widget-token minting endpoint | 04 §9.1 (token described, no endpoint) | §3.4 |
| `classify_endpoint` route metadata | 05 §B8 | §2.8 |
| Inbound `Idempotency-Key` semantics | 04 §10.5 (one route) | §2.5 (generalised) |
| Staff takeover / barge-in surface | 05 §A2 (permission only) | §3.6 |
| Recording playback | 01 §6.4 (storage only) | §1.2, §10.12 |
| CSRF strategy | 05 (gap) | §2.11 |
| `settings.key_env`, `settings.web_origin` | 05 §A1.7, §B8 | referenced; owner is 05 §E22.2 |

### 16.8 Browsers cannot set `Authorization` on `EventSource` or `WebSocket`

04 §9.1 authenticates the web-chat SSE stream with
`request.headers.get("Authorization")`. No browser API for `EventSource` or
`WebSocket` can set that header. 04 does not acknowledge this, so an
implementation following it literally cannot work from a browser.

**What 06 did:** §3.4 defines a single-use ticket, following the precedent 04
§5.4 already set for the media socket, and accepts it as an alternative to
`Authorization` on both the SSE and WS endpoints. The portal's own SSE
connections use a `fetch`-based reader (§13.1) that *can* set the header, so
the ticket is only needed for the public widget.

---

## 17. Deliberately deferred

Recorded so their absence is a decision rather than an oversight.

| Item | Why not now |
|---|---|
| `/api/v2` and the deprecation-header machinery | Meaningless without a v2. §2.1 states the trigger. |
| GraphQL or a batch endpoint | The portal's fan-out is 3–6 requests per screen over HTTP/2. Solve it when a screen needs twenty. |
| Offline support / service worker | An admin portal for a business with a phone system is online by definition. |
| i18n of the portal chrome | Manifest content is already server-localised (02 §7.4 rule 3). Chrome translation is additive and needs no structural change. |
| Real-time collaborative editing | Optimistic concurrency (§2.6) is the right primitive at this scale. CRDTs are a product. |
| The visual workflow builder | Roadmap Phase 7. `/api/v1/workflows` reads and runs; it does not author. |
| Mobile apps | The portal is responsive to 320 px. A native app is a separate product decision. |
| Per-user dashboard customisation | Role-ordered tiles (§10.2) cover the real need without a layout persistence model. |

---

## Appendix A — Route inventory

138 routes. Full table in §1.2, grouped by domain.

| Domain | Routes | Primary permissions |
|---|---|---|
| Auth & session | 11 | public + `requires()` |
| Tenant & org | 8 | `tenant:*` |
| Members & roles | 10 | `members:*`, `roles:manage` |
| Verticals & forms | 10 | `agents:*` |
| Booking | 34 | `bookings:*` |
| Conversations | 14 | `conversations:*` |
| Knowledge | 15 | `knowledge:*` |
| Connectors & channels | 12 | `connectors:*`, `channels:*`, `secrets:write` |
| Workflows | 7 | `agents:*` |
| AI & voice config | 13 | `agents:*`, `channels:*` |
| Analytics | 7 | `usage:read`, `knowledge:read` |
| Billing | 7 | `billing:*`, `usage:read` |
| Keys, audit, webhooks, DSR | 10 | `apikeys:*`, `audit:read`, `webhooks:manage`, `dsr:manage` |

## Appendix B — Portal screen inventory

| Route | Section | Permission | Manifest-driven |
|---|---|---|---|
| `/setup/[step]` | §10.1 | authenticated | fully — steps and forms |
| `/` | §10.2 | authenticated | tile order by role |
| `/settings/business` | §10.3 | `tenant:read` | no |
| `/schedule/hours`, `/holidays` | §10.4 | `bookings:read` | no |
| `/catalogue/services` | §10.5 | `bookings:read` | intake schema |
| `/catalogue/resources` | §10.6 | `bookings:read` | attribute schema |
| `/policies` | §10.7 | `bookings:read` | fully |
| `/agent`, `/agent/agents/[key]` | §10.8 | `agents:read` | fully |
| `/agent/voice` | §10.9 | `channels:read` | no |
| `/knowledge/*` | §10.10 | `knowledge:read` | no |
| `/conversations`, `/[id]` | §10.11 | `conversations:read` | no |
| `/conversations/[id]/recording` | §10.12 | `conversations:read` | no |
| `/integrations/*` | §10.13 | `connectors:read` | connector config schema |
| `/analytics/*` | §10.14 | `usage:read` | no |
| `/settings/members`, `/roles` | §10.15 | `members:read` | no |
| `/settings/audit` | §10.16 | `audit:read` | no |
| `/settings/billing` | §10.17 | `billing:read` | no |
| `/conversations/live` | §13.2 | `conversations:read` | no |
| `/v/[navKey]` | §7.4 | per nav item | fully |

---

## Appendix C — What this document owns

So a future reader knows where to change something rather than adding a second
source of truth.

| Concern | Owner | This document's relationship |
|---|---|---|
| Tenancy, RLS, ports, event shape | 00 §2, §5, §6 | consumes, never redefines |
| Tables, columns, constraints, repositories | 01 | consumes; §1.2's resources map 1:1 onto its tables |
| Manifest schema, `RenderDocument`, compilation | 02 §7 | **consumes**; §9 is its only client |
| `StreamDelta`, graph runtime, model classes | 03 | consumes; §3.2 defines the wire form it lacked |
| Channel adapters, Redis Streams, media WS | 04 | consumes; §3.4 defines the web-chat WS it lacked |
| Auth, RBAC, rate limits, `Principal` | 05 Part A, §B8 | consumes; §2.7 normalises its three error shapes |
| **HTTP route surface** | **06 §1** | owns |
| **API conventions** — pagination, idempotency, concurrency, errors | **06 §2** | owns |
| **SSE vocabulary, WS protocol, resume semantics** | **06 §3** | owns |
| **OpenAPI generation and drift gates** | **06 §4** | owns |
| **Outbound webhooks** | **06 §6** | owns |
| **`apps/web` in its entirety** | **06 Part B, C** | owns |

Anything in Part A that contradicts 00–05 is a defect in this document, except
where §16 explicitly flags the upstream inconsistency and states the
resolution.
