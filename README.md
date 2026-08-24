# ReceptionAi

Multi-tenant AI receptionist platform — voice and chat agents that answer calls,
book appointments, and integrate with a tenant's existing systems.

**▶ [Watch the demo video](https://youtu.be/GxYUHvRKpN8)** — a caller books an
appointment by voice and it lands in the practice's diary seconds later.

**Status: working end to end.** A real tenant holds a real streaming
conversation with a manifest-defined agent, isolated by RLS. Voice calls
(browser WebRTC → Deepgram STT/TTS) and the full booking engine (book,
reschedule, cancel, emergency triage) are live; knowledge ingestion and a
second vertical are not built yet — see
[the roadmap](docs/architecture/07-ROADMAP.md).

---

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| Docker + Compose v2 | 24+ / v2.20+ | The whole local stack. `docker compose version` must work. |
| Python | 3.12 | Pinned in `pyproject.toml` (`>=3.12,<3.13`). |
| Node | 20+ | The two Next.js apps. |
| Make | any | Entry points below. |

Roughly 8 GB of RAM for the full stack and ~5 GB of disk for images.

> **On a non-Linux filesystem?** If the repo sits on NTFS or exFAT (common for a
> shared data partition), symlinks are unavailable: `python -m venv` cannot
> create `.venv` in-tree and `pnpm install` may exhaust memory. `make setup`
> detects this and falls back to `~/.receptionai-venv` automatically. Node
> dependencies still need `node-linker=hoisted`, which `.npmrc` already sets.
> Moving the repo to an ext4 partition avoids the whole class of problem.

---

## Getting started

```bash
cp .env.example .env      # local defaults; every value is documented
make setup                # Python venv + dependencies
make dev                  # everything: docker, migrations, seed, all 3 servers
```

`make dev` is the whole environment in one command. It brings up the Docker
stack, migrates, seeds the demo tenant, starts the API and both frontends
detached, and waits until each answers before telling you it is ready:

```
docker stack
  ✓ 12 services healthy
database
  ✓ migrations at head
  ✓ demo tenant seeded
servers
  ✓ api ready
  ✓ marketing ready
  ✓ web ready

Ready.
  Demo chat      http://localhost:3002    ← click "Try the chat instead"
  Admin portal   http://localhost:3003
  API docs       http://localhost:8000/docs
```

Managing it:

```bash
make dev-status           # what is running, on which port
make dev-logs S=api       # tail one server (api | marketing | web)
make dev-down             # stop the servers, leave docker up
make dev-stop             # stop the servers AND docker
make dev-restart          # both, in order
```

Logs and PIDs live in `.run/` (git-ignored). To run **one** server in the
foreground instead — for a debugger, or to watch its output inline —
use `make dev-api`, `make dev-marketing`, or `make dev-web`.

Open **http://localhost:3002** and click *Try the chat instead*. That widget
talks to the real API, which streams from whichever LLM provider is configured
in `.env`. Ask it about opening hours or prices and it answers from the dental
manifest in `packages/verticals/manifests/`.

### Where everything lives

| Service | URL | Notes |
|---|---|---|
| Marketing site + demo chat | http://localhost:3002 | |
| Admin portal | http://localhost:3003 | Demo mode; no password needed |
| API | http://localhost:8000 | `/docs` for the OpenAPI UI |
| Grafana | http://localhost:3001 | Dashboards pre-provisioned, no login |
| Temporal UI | http://localhost:8080 | Workflow history |
| Prometheus | http://localhost:59090 | Targets and alert rules |
| MinIO console | http://localhost:9001 | `minioadmin` / `minioadmin` |
| Postgres | `localhost:55432` | See "Connecting to the database" |
| Redis | `localhost:56379` | |

The unusual Postgres and Redis ports are deliberate — they avoid colliding with
a local install you may already be running.

**Port already in use?** Every port is an environment variable. Change it in
`.env` and run `make up` again; nothing in `docker-compose.yml` needs editing.
If you move the marketing site off 3002, add its new origin to `CORS_ORIGINS`
too, or the browser silently blocks the demo widget's requests.

### Provider keys

The demo answers with a stub adapter and no API key, which is enough to see the
plumbing work. For real replies, set one in `.env`:

```bash
DEEPSEEK_API_KEY=...      # or OPENAI_API_KEY / ANTHROPIC_API_KEY
```

The API logs `llm_provider=` at startup so you can confirm which one it picked.
A missing key falls back to the stub with a loud log line rather than crashing.

### Common tasks

```bash
make help                 # every target, with descriptions
make ps                   # service status
make logs S=postgres      # tail one service
make down                 # stop, keep data
make reset                # stop and DESTROY volumes (re-runs DB init)
```

---

## Running tests

```bash
make test                 # unit + RLS suites
make test-rls             # tenant-isolation proofs only
make test-integration     # needs `make up`
```

The **RLS suite is the one that gates merges.** It spins its own PostgreSQL via
testcontainers, runs the real migration as the real privileged role, and then
connects as `receptionai_app` — a role that is not the table owner and does not
have `BYPASSRLS`. That last detail is the whole point: a test running as the
superuser passes with row-level security entirely absent.

Requires a working Docker socket. `make test-rls` needs no other services up.

---

## Before you push

```bash
make check                # every gate CI runs, in CI's order
```

`make check` runs lint, the contract gates, typecheck, and tests. It is
deliberately identical to CI — a local gate that diverges from CI is worse than
no local gate, because it teaches people to ignore it.

### The contract gates

Three checks enforce architectural rules that would otherwise fail silently:

| Gate | Enforces |
|---|---|
| `tools/check_layering.py` | Contract §9. `core` imports nothing; `apps` depend on `packages`, never the reverse; vendor SDKs only inside `providers/`, `connectors/`, `channels/`. |
| `tools/check_uuid_usage.py` | Contract §6 / A2. No `uuid4` for identifiers. A v4 satisfies every test while destroying the index locality v7 was chosen for, surfacing months later as unexplained WAL volume. |
| `tools/check_image_pinning.py` | §E21.3. Every image pinned by `@sha256:` digest and every GitHub Action by commit SHA. A mutable tag can be re-pushed, so the image CI verified is not provably the image that runs. |

If a `uuid4` is genuinely non-identifier randomness, annotate it:

```python
value = uuid4()  # ALLOW-UUID4: opaque correlation value, no index
```

---

## Connecting to the database

```bash
make shell-db             # as receptionai_app — RLS applies
make shell-db-admin       # as superuser — bypasses RLS, inspection only
```

The app role sees **nothing** until a tenant is set, because policies filter on
`app.tenant_id` and an unset GUC matches no rows:

```sql
SET app.tenant_id = '<tenant-uuid>';
SELECT * FROM conversations;
```

That is the tenancy model working as designed — it fails closed. If a query
returns zero rows unexpectedly, check the GUC before you check the data.

### Roles

Contract §2.5 requires the runtime role to be neither the table owner nor
`BYPASSRLS`, so cross-tenant access is impossible by construction:

| Role | Purpose |
|---|---|
| `receptionai_migrator` | Runs Alembic. The only role that issues DDL. |
| `receptionai_owner` | Owns tables. Created by the migration. |
| `receptionai_app` | **The runtime role.** DML only, no `BYPASSRLS`. |
| `receptionai_reporting` | Read-only analytics. |

Created by `infra/docker/init/01-roles.sql` (local credentials) and migration
`0001` (the role model). These run once, on an empty data directory — use
`make reset` to re-run them.

---

## Migrations

```bash
make migrate                       # upgrade to head
make revision M="add bookings"     # autogenerate
```

Migrations connect as `receptionai_migrator` via `MIGRATION_DATABASE_URL` and
never as the app role. Extensions (`vector`, `pgcrypto`, `btree_gist`,
`pg_trgm`) need superuser, so they are pre-created by
`infra/docker/init/02-extensions.sql`; the migration's
`CREATE EXTENSION IF NOT EXISTS` is then a no-op. This is what lets the
migrator role stay unprivileged.

---

## Layout

```
apps/
  api/         FastAPI — HTTP and SSE. Thin; no business logic.          ✅
  marketing/   Public site + the live demo chat widget                   ✅
  web/         Next.js admin portal                                      ✅
  worker/      Temporal workers: workflows + activities                  ○
  media/       Realtime voice sessions (separate process — contract A1)  ○
packages/
  core/        Domain model, ports, errors. No I/O, imports nothing.     ✅
  persistence/ SQLAlchemy models, repositories, RLS session management   ✅
  ai/          LangGraph runtime compiled from vertical manifests        ✅
  providers/   LLM/STT/TTS adapters — the only place vendor SDKs live    ✅
  verticals/   Manifest schema + the seeded manifests                    ✅
  ui/          Shared design system (tokens + components)                ✅
  voice/ channels/ connectors/ knowledge/ booking/ billing/
  observability/                                                         ○
infra/docker/  Dockerfiles, compose config, DB init, observability config
migrations/    Alembic
tools/         CI enforcement gates + seed script
tests/         unit/ · api/ · integration/ · rls/
```

✅ built · ○ scaffolded, empty

---

## Architecture documentation

Start with the contract — it is the source of truth, and every other document
conforms to it:

| Document | Covers |
|---|---|
| [`docs/architecture/00-CORE-CONTRACT.md`](docs/architecture/00-CORE-CONTRACT.md) | **Read first.** Locked decisions, tenancy, ports, amendments. |
| [`01-DATA-MODEL.md`](docs/architecture/01-DATA-MODEL.md) | Schema, RLS policies, partitioning, id generation |
| [`02-PLUGIN-CONNECTOR-SDK.md`](docs/architecture/02-PLUGIN-CONNECTOR-SDK.md) | Vertical manifests, connector framework |
| [`03-AI-ENGINE.md`](docs/architecture/03-AI-ENGINE.md) | LangGraph runtime, prompts, retrieval |
| [`04-VOICE-CHANNELS.md`](docs/architecture/04-VOICE-CHANNELS.md) | Voice pipeline, realtime mode, channels |
| [`05-PLATFORM-SECURITY-OPS.md`](docs/architecture/05-PLATFORM-SECURITY-OPS.md) | Auth, secrets, PII, Docker, CI/CD, config |
| [`06-API-FRONTEND.md`](docs/architecture/06-API-FRONTEND.md) | REST surface, error envelope, admin UI |
| [`07-ROADMAP.md`](docs/architecture/07-ROADMAP.md) | Phasing |

The contract's §11 amendments **are** the contract — they supersede the
original text where they conflict, and they carry the reasoning.

---

## Design notes worth knowing early

- **Tenancy is enforced at the database.** Shared schema, RLS `ENABLED` and
  `FORCED`, policies on `current_setting('app.tenant_id')`. The service layer
  re-checks, but the database is the control.
- **Temporal, not Celery.** Booking lifecycles span hours to weeks with human
  approval steps and timers. Celery models task retries, not durable workflow
  state.
- **Verticals are data, not code.** A vertical is a declarative manifest
  validated against a JSON Schema, never tenant-supplied Python. Custom logic
  runs out-of-process behind the connector framework.
- **All identifiers are UUIDv7**, generated in Python from one canonical source
  (`packages/core/ids.uuid7`). The database-side generator is a fallback for
  migrations and backfills.
- **Realtime voice runs in `apps/media`**, a separate process, because a slow
  LLM call in a request handler head-of-line blocks audio frames on the same
  event loop.
