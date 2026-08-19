# ReceptionAi — Build Roadmap & Effort Assessment

This document exists to be honest about scale. The architecture describes a
platform; this describes what it costs to build it, in what order, and where
the risk actually sits.

---

## 1. Honest sizing

The full specification — plugin runtime, connector SDK, realtime voice, generic
booking engine, RAG stack, visual workflow builder, billing, admin portal, full
IaC — is on the order of **18–36 months for 8–15 engineers**. That is not
pessimism; it is what comparable platforms cost. Individually hard pieces:

| Subsystem | Rough effort | Why it's expensive |
|---|---|---|
| Realtime voice pipeline | 3–5 eng-months | Turn-taking is the hardest UX problem here. Latency budgets, barge-in, provider failover mid-call. |
| Generic booking engine | 3–4 eng-months | Availability across resources, timezones, recurrence, holds, and concurrency is deceptively deep. |
| Connector framework + first 5 connectors | 4–6 eng-months | Each third-party API has its own auth, pagination, rate limits, and failure modes. |
| Dynamic LangGraph runtime | 2–3 eng-months | Compilation, caching, checkpointing, tool-grant enforcement. |
| Knowledge/RAG stack | 2–3 eng-months | Ingestion breadth (PDF/DOCX/web/Notion/Drive) is the long pole, not retrieval. |
| Visual workflow builder | 3–4 eng-months | A no-code editor is a product in itself. |
| Admin portal | 4–6 eng-months | Dozens of screens, dynamic manifest-driven forms. |
| Platform (auth, billing, RBAC, audit) | 3–4 eng-months | Table stakes, unglamorous, non-optional. |
| DevOps / IaC / CI-CD | 2–3 eng-months | Voice workloads make autoscaling non-trivial. |

**The strategic consequence:** building all of this before serving a single
customer is the highest-risk path available. The recommended order below is
built to get one real vertical fully working, then make the second nearly free
— which is the actual test of whether the plugin architecture works.

---

## 2. Recommended build order

### Phase 0 — Contracts (current phase)
Architecture documents, frozen core contract, complete schema, all port
interfaces, manifest JSON Schema. **No application code.**

*Rationale: every downstream subsystem depends on these contracts. Writing code
first guarantees rework.*

**Exit criteria:** all seven documents complete and mutually consistent; the
manifest schema validates the three reference verticals; the schema survives
the three booking worked-examples.

---

### Phase 1 — Walking skeleton (~4–6 weeks)
The thinnest end-to-end slice that is genuinely production-shaped.

- Repo scaffold, `core` package, DI container, config
- Postgres + Alembic + **RLS proven by tests that assert cross-tenant reads fail**
- Tenant + user + auth (JWT, one OAuth provider), tenant-resolution middleware
- Manifest loader + validator; one vertical (medical) installed for one tenant
- LangGraph compiled from that manifest, one LLM adapter, no tools yet
- Web chat channel, SSE streaming
- Docker Compose stack that runs locally
- CI: lint, typecheck, unit, integration

**Exit:** a real tenant has a real text conversation with a manifest-defined
agent, isolated by RLS, in a container, green in CI.

*Rationale: this proves the four riskiest structural bets (RLS, manifest-driven
graphs, port abstraction, streaming) at the smallest possible size. Every later
phase adds surface to a proven skeleton rather than hoping it composes.*

---

### Phase 2 — Booking core (~6–8 weeks)
- Generic booking engine: resources, services, availability, holds
- Double-booking prevention (GiST exclusion constraint) proven under concurrent load
- Temporal workflows: book → confirm → remind → reschedule → cancel
- Google Calendar connector (the reference implementation)
- Tools wired into the graph with manifest grant enforcement
- Booking APIs + audit logging

**Exit:** the AI books, moves, and cancels a real appointment on a real Google
Calendar, and concurrent bookings for the same slot cannot both succeed.

---

### Phase 3 — Voice (~8–10 weeks)
- Composed pipeline: Deepgram STT + LLM + ElevenLabs TTS
- Turn-taking: VAD, endpointing, barge-in, TTS cancellation
- Twilio Media Streams; inbound calls; warm transfer to human
- Latency instrumentation against the ~1.9s budget
- Realtime speech-to-speech as the second mode
- Mid-call provider failover

**Exit:** a phone call books an appointment, median end-of-speech-to-first-word
under 2s, measured — and interrupting the agent mid-sentence works cleanly.

*Rationale: voice after booking, because a voice agent with nothing to do is a
demo. Booking is the value; voice is the interface.*

---

### Phase 4 — Knowledge & second vertical (~6 weeks)
- Ingestion (PDF, DOCX, web), chunking, embeddings, hybrid search, reranking
- RAG into prompt assembly with injection defenses
- **Author the restaurant vertical with zero backend code changes**

**Exit:** the restaurant vertical ships as a manifest + connector config only.
If this requires backend changes, the plugin architecture has failed and must be
fixed here — before more verticals amplify the mistake.

*This is the single most important checkpoint in the roadmap.*

---

### Phase 5 — Admin portal (~8–10 weeks)
Next.js portal: onboarding, business profile, hours/holidays, services,
resources, policies, AI config, voice config, knowledge management, integrations,
conversation history, analytics, users/roles, audit views. Manifest-driven
dynamic forms so new verticals need no new screens.

---

### Phase 6 — Commercial readiness (~6–8 weeks)
Billing/metering → Stripe, plans and quotas, usage enforcement, analytics
dashboards, notifications, MFA, SSO, remaining channels (WhatsApp, SMS,
Telegram, Slack, Teams), K8s + Terraform, blue-green deploy, SOC 2 groundwork.

---

### Phase 7 — Scale & breadth (ongoing)
Workflow builder, remaining connectors, more verticals, more providers,
multi-region, HIPAA posture and the per-tenant-DB escape hatch.

---

## 3. Where the real risk is

| Risk | Why it matters | Mitigation |
|---|---|---|
| **Voice turn-taking feels wrong** | Users forgive latency far less than they forgive a wrong answer. Interrupting a bot that keeps talking is the #1 complaint about voice agents. | Phase 3 spends real time here. Test with humans, not scripts. Tune endpointing per vertical. |
| **The manifest can't express a real vertical** | The entire "no code changes" promise collapses. | Phase 4's zero-code restaurant is the honest test. Three complete manifests are authored in Phase 0 specifically to stress the schema early. |
| **Latency budget is blown** | ~1.9s is a marketing promise. Missing it is a product failure, not a perf ticket. | Budgeted per-stage from day one; measured continuously in CI, not discovered in production. |
| **Booking correctness under concurrency** | Double-booking destroys trust instantly and is unrecoverable socially. | DB-level exclusion constraint, not application locking. Load-tested in Phase 2. |
| **RLS gap** | One missing policy is a cross-tenant data breach. | RLS tests are mandatory per table; CI fails if a tenant table lacks a policy. |
| **LLM cost per conversation exceeds price** | Silent margin death; discovered at scale, too late. | Cost attribution from Phase 1. Per-tenant unit economics visible before GA. |
| **Temporal operational burden** | A real dependency with a real learning curve. | Accepted deliberately. Revisit only if the team genuinely cannot operate it. |

---

## 4. Recommended cut for a first release

If the goal is revenue and learning rather than completeness, ship **Phases
1–4 plus a reduced Phase 5**: one vertical, voice + web chat, booking against
Google Calendar, knowledge base, and an admin portal covering only what a
customer must self-serve. That is roughly **6–9 months with 3–5 engineers** and
it is a sellable product.

Deliberately deferred: workflow builder, most connectors, most channels,
multi-region, SSO. None of them block a first paying customer, and each is
cheaper to build once real usage has shown which ones customers actually ask
for.

The architecture is designed so that deferring these costs nothing structurally —
they are additive, not surgical.
