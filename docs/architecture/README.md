# ReceptionAi — Architecture Documentation

Multi-tenant AI Receptionist SaaS. One platform, any business vertical, no code
changes to onboard a new one.

## Read in this order

| # | Document | Owner concern |
|---|---|---|
| 00 | [Core Contract](00-CORE-CONTRACT.md) | **Frozen decisions.** Binding on everything below. |
| 01 | [Data Model](01-DATA-MODEL.md) | Schema, RLS, the generic booking model, migrations |
| 02 | [Plugin & Connector SDK](02-PLUGIN-CONNECTOR-SDK.md) | Verticals-as-data, connector interface |
| 03 | [AI Engine](03-AI-ENGINE.md) | Dynamic LangGraph, tools, RAG, prompts, evals |
| 04 | [Voice & Channels](04-VOICE-CHANNELS.md) | Realtime audio, turn-taking, chat channels |
| 05 | [Platform, Security & Ops](05-PLATFORM-SECURITY-OPS.md) | Auth, security, billing, observability, DevOps |
| 06 | [API & Frontend](06-API-FRONTEND.md) | REST contract, admin portal |
| 07 | [Roadmap](07-ROADMAP.md) | Build order and honest effort estimates |

If a document below contradicts **00-CORE-CONTRACT.md**, the contract wins and
the document is wrong.

## The three ideas that make this work

**1. Verticals are data, not code.** A medical clinic and a hotel differ by a
JSON manifest — vocabulary, agents, prompts, tool grants, booking policies —
validated against a versioned schema. Adding a vertical means authoring a
manifest, not deploying code. Tenant-supplied code never runs in our process;
genuine custom logic runs on the tenant's side behind a webhook or MCP tool.

**2. One booking model for every domain.** A dental appointment, a table for
four, a hotel room for three nights, and a rented excavator are the same shape:
a time range against a resource with capacity, subject to rules. One schema,
one availability engine, no vertical-specific columns. Section 5 of the data
model proves this with worked examples.

**3. Everything external is a port.** LLMs, STT, TTS, calendars, CRMs,
channels, storage, secrets — all reached through interfaces, selected per
tenant from configuration. Swapping Deepgram for AssemblyAI is a config change.
No vendor SDK is imported outside its adapter package, and CI enforces that.

## Non-negotiables

- **Tenant isolation at the database.** Postgres RLS, `FORCE`d, on every
  tenant-owned table. The app role cannot bypass it. Isolation is structural,
  not a code-review convention.
- **Durable booking state.** Temporal workflows, not fire-and-forget tasks.
  Bookings touch money and people's time; "the worker crashed" is not an
  acceptable outcome.
- **Voice latency is a product feature.** ~1.9s median from end-of-speech to
  first spoken word. Every architectural choice in the voice path is subordinate
  to that number, and it is budgeted stage by stage.
- **Knowledge-base content is untrusted input.** It is delimited, and it can
  never grant a tool. Prompt injection is treated as a live threat, not a
  theoretical one.

## Status

Phase 1 (architecture and contracts). No application code yet — deliberately.
Contracts first, then implementation against them, so subsystems interlock
instead of drifting.

**Phase 1 is complete.** 51,500 lines, six documents, ten contract amendments,
every cross-document conflict resolved or explicitly recorded as open.

| Document | Lines | State |
|---|---|---|
| 00 Core Contract | 419 | Frozen + 10 amendments (§11) |
| 01 Data Model | 10,155 | Complete, reconciled |
| 02 Plugin & Connector SDK | 13,447 | Complete, reconciled |
| 03 AI Engine | 3,642 | Complete, reconciled |
| 04 Voice & Channels | 5,098 | Complete, reconciled |
| 05 Platform, Security & Ops | 9,703 | Complete, reconciled |
| 06 API & Frontend | 8,804 | Complete, reconciled |

### What the cross-document audit caught

Each document verified itself as internally consistent. Building 06 *on top of*
the other five is what surfaced the assumptions each had made about the others —
the argument for contracts before implementation, in one paragraph:

- **Web chat could not authenticate.** SSE auth read an `Authorization` header
  no browser API can set on `EventSource`/`WebSocket`. The primary chat channel
  would have failed the moment anyone built it.
- **A designed feature had no table.** Cross-channel identity resolution existed
  in 04 with nothing in 01 to persist it, so 06 correctly declined to expose the
  endpoint. Now `customers` / `customer_identities` in 01 §3.9.
- **`authenticate_api_key` raised `TypeError` on every call** — `plan=` against a
  `tenant_plan` field.
- **Shard routing failed open.** An unresolvable `shard_key` fell back to the
  shared cluster — silently placing tenants who *paid for isolation* on shared
  infrastructure, with RLS still correct and no alert firing (A8).
- **A metric two documents depended on was exported by neither** (A7).
- Plus three incompatible error envelopes, two names for one DB dependency, and
  a retrieval query targeting a column that did not exist.

### Verified mechanically, not asserted

- **No placeholders.** Zero TODO/FIXME/TBD/"omitted for brevity" across 41k
  lines. Every table, interface, and adapter is written out.
- **No vendor SDK leaks.** No provider import appears outside
  `providers/` · `connectors/` · `channels/`. Contract §9 holds.
- **RLS is loop-applied**, not per-table hand-written (01 §11): a new table not
  registered in `tenant_tables` fails a CI assertion rather than shipping
  unprotected.
- **All cross-document references resolve** to files that exist.

### Known open items

- **03 §14 Q1** — fine-tuned router cold-start volume is unmeasured.
- **02 §18** — `resource_group` composition (combining two 2-tops into a 4-top)
  is not expressible; connector capability drift has no automated detection.
- **03 §11** — the retrieval stage's 95 ms p50 budget was costed before a
  provenance join was added. Believed immaterial, not re-derived.
- **04 §7.2** — composed-mode p95 is 3,680 ms against a ~1.9 s median product
  claim. The median holds; the tail needs the §7.4 work.

These are recorded because a plan that hides its unknowns is not a plan.
