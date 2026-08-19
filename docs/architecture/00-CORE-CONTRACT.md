# ReceptionAi — Core Architectural Contract (FROZEN)

> This document is the single source of truth that every other architecture
> document must conform to. If a subsystem document contradicts this file,
> this file wins. Any change here invalidates downstream documents.

**Status:** Frozen for Phase 1
**Platform:** Multi-tenant AI Receptionist SaaS

---

## 1. Locked Technology Decisions

| Concern | Decision | Rationale |
|---|---|---|
| Language / API | Python 3.12, FastAPI, Pydantic v2, async-first | Native async for streaming voice/chat; Pydantic v2 for typed contracts at every boundary. |
| Persistence | PostgreSQL 16, SQLAlchemy 2.x (async), Alembic | RLS support, JSONB for plugin manifests, `pgvector` for embeddings. |
| Tenancy | Shared database, shared schema, Postgres Row-Level Security | See §2. |
| Cache / bus | Redis 7 (cache, rate limits, pub/sub for realtime fan-out) | Single dependency covers three needs at this stage. |
| Durable execution | **Temporal** (not Celery) | See §3. |
| Vector store | `pgvector` initially; `VectorStorePort` abstracts it | Avoids a second datastore until scale demands it. |
| Frontend | Next.js (App Router), TypeScript, Tailwind, shadcn/ui, React Query | Server components for the admin shell; React Query for tenant-scoped data. |
| Transport | REST + SSE for streaming; WebSocket for realtime voice | SSE is sufficient for token streaming and survives proxies better. |
| Observability | OpenTelemetry → Prometheus / Grafana / OTLP traces | One instrumentation standard, swappable backends. |

---

## 2. Tenancy Model (binding on all subsystems)

**Decision: shared database, shared schema, Postgres Row-Level Security.**

Rules that every table and every query path MUST follow:

1. Every tenant-owned table has a non-null `tenant_id UUID REFERENCES tenants(id)`.
2. Every tenant-owned table has RLS `ENABLED` **and** `FORCED`.
3. Policies filter on `current_setting('app.tenant_id')::uuid`.
4. The application sets `app.tenant_id` via `SET LOCAL` inside the request
   transaction. It is never interpolated into SQL by hand.
5. The application's runtime DB role is **not** the table owner and does **not**
   have `BYPASSRLS`. Migrations run as a separate privileged role.
6. Every index on a tenant-owned table leads with `tenant_id`.
7. Cross-tenant access is impossible by construction, not by convention.

**Why not database-per-tenant:** thousands of tenants means thousands of
connection pools, migration targets, and backup schedules. Operationally
untenable at the target scale.

**Why not schema-per-tenant:** Postgres degrades with tens of thousands of
schemas; migrations become O(tenants) and `search_path` juggling is a
correctness hazard.

**Escape hatch (designed in, not built now):** `tenants.shard_key` and a
`ConnectionRouter` port allow relocating an enterprise/HIPAA tenant to a
dedicated database without a schema change. Every subsystem must resolve its
DB session through the router, never a global engine.

---

## 3. Durable Execution: Temporal

Booking lifecycles are long-running and stateful: hold → confirm → remind →
reschedule → escalate → cancel, spanning hours or weeks, with human approval
steps and timers. Celery models *task retries*, not *durable workflow state*;
implementing this on Celery means rebuilding Temporal's history, timers, and
determinism guarantees badly.

**Binding rules:**
- Workflow code is deterministic; all I/O lives in Activities.
- Every workflow carries `tenant_id` in its input and its `workflow_id` is
  namespaced `{tenant_id}:{domain}:{entity_id}` for isolation and idempotency.
- Task queues are partitioned per workload class (`bookings`, `notifications`,
  `ingestion`) so a slow ingestion job cannot starve booking confirmations.
- Cost accepted: Temporal is a real operational dependency (server + workers).
  Justified by correctness requirements on money-adjacent booking state.

---

## 4. Plugin Model: Verticals Are Data, Not Code

A vertical (medical, restaurant, hotel, …) is a **declarative manifest**
validated against a versioned JSON Schema. It is never tenant-supplied Python.

**Why:** executing tenant code in-process is an RCE surface, a noisy-neighbor
risk, and unschedulable. Declarative manifests are diffable, versionable,
statically validatable, and safe to hot-reload.

A `VerticalManifest` declares:
- `vocabulary` — domain terms surfaced in prompts and UI
- `entities` — resource/service types mapped onto the generic booking engine
- `agents` — agent nodes, their roles, prompts, and tool grants
- `tools` — references to **registered** tool implementations + JSON Schema args
- `workflows` — references to registered Temporal workflow types
- `forms` / `ui_schema` — admin and intake form rendering
- `policies` — booking, cancellation, and required-information rules
- `validation` — field-level rules applied to collected customer data

Escape hatch for genuine custom logic: a tenant registers an **outbound
webhook or MCP tool**, executed out-of-process behind the connector framework
with timeouts, retries, and egress allow-listing. Custom code runs on their
infrastructure, never ours.

---

## 5. Ports (Hexagonal Boundaries)

Every external dependency is reached through one of these ports. Adapters are
registered in a DI container and selected **per tenant** from configuration.
No subsystem may import a vendor SDK outside its adapter package.

| Port | Responsibility |
|---|---|
| `LLMPort` | chat/completion, streaming, tool-calling, token accounting |
| `STTPort` | streaming + batch transcription, partial results |
| `TTSPort` | streaming synthesis, barge-in cancellation |
| `RealtimeVoicePort` | bidirectional realtime sessions (OpenAI Realtime, LiveKit) |
| `EmbeddingPort` | text → vectors, batch-aware |
| `VectorStorePort` | upsert / hybrid search / metadata filter / delete-by-version |
| `RerankerPort` | rerank candidate chunks |
| `ConnectorPort` | tenant systems: calendars, CRMs, SQL, REST, MCP |
| `ChannelPort` | inbound/outbound messaging per channel |
| `StoragePort` | tenant-scoped blob storage |
| `SecretsPort` | tenant secret read/write, envelope-encrypted |
| `BillingPort` | subscriptions, metered usage |
| `NotificationPort` | email / SMS / push |

**Universal adapter requirements:** async, cancellable, timeout-bounded,
retry-with-jitter, circuit-broken, emit OTel spans, emit cost/usage events,
never log secrets or raw PII.

---

## 6. Canonical Identifiers & Events

- All primary keys are UUIDv7 (time-ordered, index-friendly).
- Money is `NUMERIC(12,4)` plus an ISO-4217 currency column. Never floats.
- All timestamps are `TIMESTAMPTZ`, stored UTC. Business-local time is derived
  from the tenant/location IANA timezone at render time, never stored.
- Every event carries: `event_id`, `tenant_id`, `occurred_at`, `type`,
  `version`, `correlation_id`, `causation_id`, `payload`.
- Event type names are `domain.entity.action` (e.g. `booking.appointment.confirmed`).
- Outbound integration events use the transactional outbox pattern.

---

## 7. Conversation Model (shared vocabulary)

Voice and chat converge on one abstraction so agents are channel-agnostic:

```
Conversation
  ├─ id, tenant_id, channel, status, started_at, ended_at
  ├─ Turn[]        # role, content, tool_calls, latency, tokens, cost
  ├─ Transcript?   # voice only: word timings, confidence, speaker
  └─ Outcome       # resolved | booked | transferred | abandoned
```

A `ChannelAdapter` normalizes any inbound message into a `Turn` and renders any
outbound `Turn` back to its channel. **Agents never know which channel they are on.**

---

## 8. Security Baseline (non-negotiable)

- Tenant isolation enforced at the database (RLS), re-checked in the service layer.
- Secrets envelope-encrypted (KMS-wrapped DEK per tenant); plaintext never persisted.
- PII classified at the column level; masked in logs, traces, and LLM prompts
  unless the tenant has explicitly opted in for that field.
- Prompt injection defenses: knowledge-base content is untrusted input; tool
  invocation requires an explicit grant in the vertical manifest; no tool is
  reachable purely because a model asked for it.
- Connector egress is allow-listed per tenant; SSRF-guarded (no link-local,
  loopback, or private ranges unless explicitly permitted).
- Rate limits are per-tenant and per-endpoint-class, enforced at the edge.
- Every state-changing action writes an append-only audit record.

---

## 9. Repository Layout (binding)

```
receptionai/
├── apps/
│   ├── api/              # FastAPI: HTTP, WS, SSE. Thin — no business logic.
│   ├── worker/           # Temporal workers: workflows + activities
│   ├── media/            # realtime voice sessions (see §11 A1)
│   └── web/              # Next.js admin portal
├── packages/
│   ├── core/             # domain model, ports, errors, result types. No I/O.
│   ├── persistence/      # SQLAlchemy models, repositories, RLS session mgmt
│   ├── ai/               # LangGraph runtime, prompt assembly, agent registry
│   ├── voice/            # STT/TTS/realtime pipeline, turn & barge-in logic
│   ├── channels/         # channel adapters (web, WhatsApp, SMS, Telegram, …)
│   ├── connectors/       # connector SDK + first-party connector adapters
│   ├── providers/        # LLM/STT/TTS/embedding adapters
│   ├── knowledge/        # ingestion, chunking, indexing, retrieval
│   ├── booking/          # generic booking engine (availability, holds, rules)
│   ├── verticals/        # manifest schema, validator, first-party manifests
│   ├── billing/          # metering, subscriptions, cost attribution
│   └── observability/    # OTel setup, structured logging, metrics
├── infra/
│   ├── docker/ · k8s/ · terraform/
├── migrations/           # Alembic
└── docs/                 # architecture, guides, SDK docs
```

**Dependency rule (enforced in CI):** `core` imports nothing from other
packages. Everything may import `core`. `api` and `worker` depend on packages,
never the reverse. No package imports a vendor SDK outside `providers/`,
`connectors/`, or `channels/`.

---

## 10. Naming Conventions

- Tables: plural snake_case (`booking_appointments`).
- Ports: `<Domain>Port`. Adapters: `<Vendor><Domain>Adapter` (`DeepgramSTTAdapter`).
- Temporal workflows: `<Domain><Action>Workflow`. Activities: `<verb>_<noun>`.
- API routes: `/api/v1/<plural-resource>`; tenant is derived from auth context,
  **never** taken from a path or query parameter.

---

## 11. Amendments

Changes to the frozen contract after Phase 1 review. Each names the document
that motivated it. The contract still wins on conflict; these ARE the contract.

### A1 — `apps/media` added to §9  [from 04-VOICE-CHANNELS.md]

Realtime voice runs in a dedicated process, not in `apps/api` or `apps/worker`.

Justification, accepted: a slow LLM call in a request handler head-of-line
blocks audio frames on the same event loop; voice scales on concurrent-call
count while the API scales on RPS; and media pods need `maxUnavailable: 0` with
a long termination grace so a deploy never cuts a live call. Colocating with
`apps/api` would force API deploys to wait on in-flight calls.

`apps/media` exports `receptionai_voice_active_sessions`, which
05-PLATFORM-SECURITY-OPS.md §E19.4 uses as the HPA scaling signal (voice
workers sit at 15–25% CPU while completely full, so CPU-based autoscaling never
fires). The metric is a cross-document dependency: it is part of the contract,
not an implementation detail.

Fallback if a deployment target cannot host a fourth app: run it as a separate
*deployment* of `apps/worker` with a distinct task queue. Process isolation is
the requirement; a separate top-level directory is the preferred expression.

### A2 — ID generation split  [from 01-DATA-MODEL.md / 05-PLATFORM-SECURITY-OPS.md]

§6 requires UUIDv7. Two generators exist and their scopes are fixed:

- **Application-side is the normal path.** Python mints ids for rows the
  application inserts, from ONE canonical source: the `uuid6` backport, pinned,
  auto-switching to stdlib `uuid.uuid7()` on 3.14+ behind the same name.
- **Database-side `uuid_generate_v7()` (PL/pgSQL) is the fallback**, existing so
  column defaults work for rows inserted by migrations, backfills, and
  `INSERT ... SELECT`.

A silent fallback to `uuid4()` is forbidden — it satisfies the type signature
and every test while destroying the index locality UUIDv7 was chosen for,
surfacing months later as unexplained WAL volume. Unavailability must raise at
import. Enforced by `tools/check_uuid_usage.py` plus a SQL assertion that any
persisted non-v7 id returns zero rows.

Second reason this must fail hard: `uuid7_timestamp()` recovers the partition
key from the id itself, which a v4 makes impossible.

> *Correction:* an earlier revision of this amendment stated DB-side was
> authoritative for persisted rows. That inverted 01 §1.2. The above is correct.

### A3 — Checkpoint frequency and retention  [from 01 §9.4, adopted by 03 §5.2]

LangGraph checkpoints are written at **turn boundaries**, not per super-step.
Checkpoints are snapshots, not deltas, so per-step writes 5–10 full state copies
per turn to buy mid-turn resume — which neither real failure mode can use (a
dropped call cannot resume mid-turn; a worker crash replays idempotently from
the boundary). Per-step is available behind a per-tenant debug flag.

Retention is **14 days**, deliberately shorter than conversation retention. A
checkpoint is the highest-density PII in the system in a form that *cannot be
partially redacted* — an opaque BYTEA has no columns for a redaction sweep to
null — so deletion is the only lever. Checkpoints are in the GDPR erasure path.

Physical design is owned by 01 §9.4 and supersedes any illustrative DDL
elsewhere.

The gap this leaves is handled at the tool layer, not by checkpoint frequency:
replay-from-boundary is free only when re-executed work is idempotent **and
cheap**. A turn calling an externally-metered API then crashing replays
correctly but bills the vendor twice. `ToolSpec.replay_cache_ttl_s` memoizes
those specific tools under their idempotency key.

### A4 — Text search configuration  [from 01 §7.4, adopted by 03 §10]

`to_tsvector` config is per-tenant (`tenants.default_text_search_config`),
overridable per knowledge source, and stored per row on `chunks` and
`transcripts` so a vector always travels with the config that built it. Type is
`REGCONFIG`, so an invalid name fails at write time.

It is trigger-maintained, not `GENERATED`: `to_tsvector(regconfig, text)` is
IMMUTABLE, but PostgreSQL refuses a generated column referencing a `regconfig`
**column**, because the config names a mutable catalogue object. That refusal is
correct — `ALTER TEXT SEARCH CONFIGURATION` would silently invalidate every
stored value and index.

Known limitation, documented rather than silent: CJK and Thai have no
PostgreSQL stemmer and fall back to `'simple'`, leaning on the vector half.
`RetrievalService` records `lexical_contributed` per query so a tenant whose
lexical arm never fires is visible in the dashboard.

### A5 — Knowledge chunk context headers  [from 03 §10]

`context_header` is **prepended at retrieval, not embedded at ingestion**.
Embedding a document title into every chunk pulls them toward a shared centroid
and flattens within-document ranking, which is the ranking that matters most.

Exception, handled at ingestion: bare tabular rows ("Adult cleaning | £85") are
meaningless without their header, so `repeat_header=True` chunkers inline it
into the chunk text itself.

The header is untrusted content and is sanitized identically to the body — a
heading in an uploaded PDF is exactly as untrusted as the paragraph under it.

### A6 — Realtime mode and agent topology  [RESOLVED — 04 §2.4.3]

**Multi-agent manifests run in realtime mode. The supervisor runs out-of-band.**

The question was whether Mode B (realtime speech-to-speech) forces single-agent
manifests, since the provider owns turn detection and reasoning and there is no
supervisor hop in its budget. Measured answer:

| Supervisor placement | Added cost | Mode B total (med/p95) |
|---|---|---|
| In-band | +325 ms / +770 ms | 1135 / 2500 ms |
| **Out-of-band** | **+0 ms / +45 ms** | **810 / 1775 ms** |
| (Mode A for reference) | — | 1755 / 3680 ms |

The in-band hop is **not** fatal — it still beats Mode A comfortably. It is
simply wasteful, because the supervisor does not need the *final* transcript. It
needs enough to pick a specialist, and that is available from the provider's
live transcription while the caller is still speaking.

**Load-bearing constraint:** the supervisor needs ~325 ms but the provider's
turn-detection window is ~300 ms, so it **must** start during speech, not at
speech-end. Routing on partial transcripts is not an optimization here; it is
the design. An implementation that waits for end-of-speech silently becomes the
in-band variant.

**Tool grants are unaffected.** The provider decides *when* to call and with
*what arguments*, but never executes — every realtime protocol emits a tool-call
event and waits for the client to return a result, and the provider has no
network path to a tenant's systems. Execution round-trips through us
unconditionally, so `grant_for()` remains the sole authorization surface,
running before execution. Only the active agent's granted tools are uploaded.

**What Mode B genuinely gives up: within-turn multi-hop handoff.** The session
generates audio continuously, so swapping instructions under a live response is
unsafe and audibly wrong. Multi-agent becomes multi-agent *across* turns — mild
for a receptionist, where turns are naturally single-intent; severe for
workflows needing two specialists to collaborate before answering.

**Compatibility is derived, never declared.** `derive_realtime_compatibility`
computes it from the manifest at publish time and at mode-switch. A declared
flag would be a second source of truth that can disagree with the first — a
manifest could claim `realtime_compatible: true` while declaring agents needing
mid-turn handoff, and nothing would catch it until a caller hit it. Single-agent
operation therefore remains the automatic outcome for a manifest that fails the
check, reached by validation rather than policy. No schema change.

### A8 — `ConnectionRouter` fails closed on an unresolvable shard  [01 vs 05]

01 §15 logged a warning and fell back to the default shard when a
`tenants.shard_key` had no matching config. 05 raised instead. **05 is correct;
01 changes.**

`shard_key` is non-null *only* for tenants relocated to dedicated infrastructure
**because shared was contractually or legally unacceptable** — the HIPAA/enterprise
escape hatch in §2. A fallback therefore lands precisely those tenants on the
shared cluster, and does so invisibly: RLS still scopes correctly, no test fails,
no alert fires. It fails *successfully*, and discovery comes at the next audit —
which for these tenants is the event the arrangement existed to survive. The
trigger is routine (config not yet propagated to a new pod), and a warning log
is not a control at production log volume.

This is the fail-open/fail-closed split from 05 §B8 applied consistently: open
on availability controls (rate limits), closed on isolation controls. Shard
routing is an isolation control.

**Rule:** `shard_key IS NULL` → default shard (never assigned; the default is
genuinely correct). `shard_key` non-null with no matching config → **raise**;
refuse the session. A tenant who paid for isolation gets an error, not silent
downgrade.

### A9 — Error envelope  [from 06 §16.2, adopted by 05]

All API errors are RFC 9457 `application/problem+json`. 05's frozen `error`
code vocabulary is preserved as the `code` field. Middleware-level denials use
the same shape as handler-level ones — the only difference is mechanical
(middleware runs outside the exception-handler stack, so it calls the builder
directly rather than raising). A test asserts every frozen code has a title; a
missing one would `KeyError` inside the error path, discoverable only by failing
to render an error.

### A10 — DB session dependency  [01 owns]

`tenant_context()` / `db_session()` / `TenantSession` from 01 §15 are canonical.
05's `get_session()` / bare `AsyncSession` is withdrawn. `TenantSession` carries
`shard_key` and exposes `impersonator_user_id` (01 names the actor; 05 stores
the subject).

### A7 — Voice scaling metrics  [from 04 §1.3]

`apps/media` exports `receptionai_voice_active_sessions` (tenant- and
mode-labelled) and `receptionai_voice_session_capacity`. 05 §E19.4's HPA targets
**utilization as a ratio** of the two, not an absolute session count, which
would go stale whenever pod resources change. The gauge is incremented and
decremented by `CallSession.__aenter__/__aexit__` so it cannot drift on an
exception path. Both names are cross-document dependencies and part of the
contract.
