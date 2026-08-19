# ReceptionAi — Data Model (Persistence Layer)

> Conforms to `00-CORE-CONTRACT.md` (FROZEN). Where this document appears to
> extend the contract, it is an elaboration, never an override.

**Status:** Phase 1 design, implementable as written
**Owner:** Data Architecture
**Target:** PostgreSQL 16 + `pgvector` 0.7+, SQLAlchemy 2.x async, Alembic

---

## 0. Reading Guide

| Section | Content |
|---|---|
| 1 | Global conventions, extensions, enums, helper functions |
| 2 | Tenancy DDL |
| 3 | Identity & access DDL, incl. customers / cross-channel identity |
| 4 | Vertical plugin DDL |
| 5 | Generic booking engine DDL (the hard part) |
| 6 | Conversations DDL |
| 7 | Knowledge / RAG DDL, incl. per-tenant text-search config |
| 8 | Connectors DDL |
| 9 | Workflows DDL + `ai_checkpoints` (physical design) |
| 10 | Operations DDL (audit, outbox, usage, cost, notifications) |
| 11 | RLS: the universal pattern, applied to every tenant table |
| 12 | Index catalogue with query-pattern justification |
| 13 | Constraints: double-booking prevention and hold TTL semantics |
| 14 | The generic booking model, proven with three worked examples |
| 15 | SQLAlchemy 2.x async patterns (session factory, repository, ConnectionRouter) |
| 16 | Migration strategy (Alembic, RLS, zero downtime, role split) |
| 17 | Partitioning & retention |
| 18 | Open questions deliberately deferred |

---

## 1. Global Conventions

### 1.1 Extensions

```sql
-- Run once per database, by the privileged migration role.
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid, digest()
CREATE EXTENSION IF NOT EXISTS btree_gist;    -- REQUIRED: lets GiST indexes mix
                                              -- scalar equality (tenant_id, resource_id)
                                              -- with range overlap (&&) in one
                                              -- EXCLUDE constraint.
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- trigram search for admin lookups
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

`btree_gist` is non-optional. Without it, `EXCLUDE USING gist (tenant_id WITH =, ...)`
fails because the default GiST opclass set has no equality operator for `uuid`.

### 1.2 UUIDv7

The contract mandates UUIDv7 primary keys. PostgreSQL 16 ships `gen_random_uuid()`
(v4) only; `uuidv7()` is a PG18 builtin. We therefore define our own generator so
that (a) database-side defaults work for rows inserted by migrations, backfills,
and `INSERT ... SELECT`, and (b) application-side generation (the normal path)
produces byte-identical values.

**Why UUIDv7 matters here and not just aesthetically:** our hottest tables
(`turns`, `usage_events`, `audit_log`) are append-only and indexed by
`(tenant_id, created_at)`. Random v4 keys scatter inserts across every leaf page
of the primary-key index, inflating WAL and cache churn. v7 keys are
time-prefixed, so inserts append to the right edge of the index. On a 100M-row
`turns` table this is the difference between a working set of a few hundred
pages and the whole index.

```sql
-- Timestamp-ordered UUIDv7 per RFC 9562.
-- Layout: 48 bits unix_ts_ms | 4 bits version(7) | 12 bits rand_a
--         | 2 bits variant(0b10) | 62 bits rand_b
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL SAFE
AS $$
DECLARE
    unix_ts_ms  bytea;
    uuid_bytes  bytea;
BEGIN
    unix_ts_ms := substring(
        int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint)
        FROM 3 FOR 6
    );

    -- 10 random bytes fill rand_a + rand_b; version/variant nibbles are then
    -- overwritten in place.
    uuid_bytes := unix_ts_ms || gen_random_bytes(10);

    -- byte 6: set version to 7 (high nibble)
    uuid_bytes := set_byte(uuid_bytes, 6,
        (b'0111'::int << 4) | (get_byte(uuid_bytes, 6) & 15));

    -- byte 8: set variant to RFC 4122 (0b10xxxxxx)
    uuid_bytes := set_byte(uuid_bytes, 8,
        (b'10'::int << 6) | (get_byte(uuid_bytes, 8) & 63));

    RETURN encode(uuid_bytes, 'hex')::uuid;
END;
$$;

-- Extract the embedded timestamp. Used by retention jobs and partition
-- routing sanity checks, and lets us answer "when was this row created"
-- without a column on small lookup tables.
CREATE OR REPLACE FUNCTION uuid_v7_timestamp(u uuid)
RETURNS timestamptz
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT to_timestamp(
        ('x' || substring(replace(u::text, '-', '') FROM 1 FOR 12))::bit(48)::bigint
        / 1000.0
    );
$$;
```

> **Note on `IMMUTABLE`:** `uuid_v7_timestamp` is genuinely immutable (pure
> function of its input), so it is safe in index expressions and partition
> constraints. `uuid_generate_v7` is `VOLATILE` and must never appear in an
> index or a `CHECK`.

### 1.3 Timestamp and money conventions

- Every timestamp column is `TIMESTAMPTZ`. The database session runs with
  `timezone = 'UTC'`; no code ever relies on the server's local zone.
- Business-local rendering is derived from `locations.timezone` (IANA string),
  never stored. `CHECK (timezone IS NULL OR now() AT TIME ZONE timezone IS NOT NULL)`
  gives us validation of the IANA name at write time — an invalid zone raises.
- Money is always a pair: `NUMERIC(12,4)` amount plus `CHAR(3)` ISO-4217 code.
  4 decimal places because per-token LLM costs are sub-cent
  (e.g. `0.0003 USD`) and rounding at 2 dp destroys cost attribution.
- Micro-costs that would underflow even 4 dp (per-token prices) are stored in
  `cost_records` as `NUMERIC(20,10)` unit rates with a `NUMERIC(12,4)` rolled-up
  total. See §10.4.

### 1.4 Standard column set

Every tenant-owned table carries:

```
id           UUID PRIMARY KEY DEFAULT uuid_generate_v7()
tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE
created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
```

Mutable business entities additionally carry:

```
version      INTEGER NOT NULL DEFAULT 1     -- optimistic concurrency
deleted_at   TIMESTAMPTZ                    -- soft delete, NULL = live
```

**Why soft delete only on some tables:** soft deletes complicate every unique
constraint (you need partial uniques on `WHERE deleted_at IS NULL`) and every
join. We apply them only where audit or referential history genuinely requires
it: `resources`, `services`, `users`, `tenant_connectors`, `knowledge_sources`.
High-volume append-only tables (`turns`, `usage_events`) are never soft-deleted;
they are dropped by partition.

### 1.5 `updated_at` maintenance

```sql
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
```

Applied per table as:

```sql
CREATE TRIGGER trg_<table>_updated_at
    BEFORE UPDATE ON <table>
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

We do **not** bump `version` in this trigger. `version` is incremented
explicitly by the application (`UPDATE ... SET version = version + 1 WHERE
version = :expected`) so that a lost update surfaces as `rowcount = 0` rather
than being silently papered over.

### 1.6 Tenant-context helper

Every RLS policy calls this rather than `current_setting` directly:

```sql
-- STABLE (not IMMUTABLE): the value is constant within a statement but varies
-- across transactions. STABLE lets the planner treat it as a constant for the
-- duration of a query, which is what makes RLS predicates index-usable.
CREATE OR REPLACE FUNCTION app_current_tenant()
RETURNS uuid
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
$$;
```

`current_setting(..., true)` (the `missing_ok` form) returns NULL instead of
raising when the GUC is unset. Combined with `NULLIF` on the empty string, an
unset context yields `NULL`, and `tenant_id = NULL` is never true — so a session
that forgot to set context sees **zero rows** rather than erroring or, far
worse, seeing everything. Fail-closed by construction.

A parallel helper exists for the platform-admin path:

```sql
CREATE OR REPLACE FUNCTION app_is_platform_admin()
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT coalesce(current_setting('app.platform_admin', true), 'off') = 'on';
$$;
```

This is deliberately **not** used in tenant-table policies (see §11.4). It gates
only the small set of platform-catalogue tables.

### 1.7 Enum strategy

We use PostgreSQL `ENUM` types for closed vocabularies that the *platform*
owns and that appear in indexed predicates (`status`, `channel`, `role`).
We use `TEXT` + `CHECK` for vocabularies a vertical manifest might extend.

**Why enums at all**, given they are painful to alter? Three reasons: 4 bytes
instead of a variable-length text (matters on 100M-row `turns`), planner
selectivity estimates are far better on a bounded domain, and an invalid value
is rejected at write time rather than discovered in a dashboard. PG 12+ supports
`ALTER TYPE ... ADD VALUE` without a table rewrite, which removes the historical
objection. The remaining constraint — `ADD VALUE` cannot run inside a
transaction block that then uses the value — is handled in §16.4.

```sql
CREATE TYPE tenant_status         AS ENUM ('trial','active','past_due','suspended','cancelled');
CREATE TYPE subscription_status   AS ENUM ('trialing','active','past_due','paused','cancelled','expired');
CREATE TYPE user_status           AS ENUM ('invited','active','disabled','locked');
CREATE TYPE mfa_factor_type       AS ENUM ('totp','webauthn','sms','recovery_code');
CREATE TYPE api_key_status        AS ENUM ('active','revoked','expired');

CREATE TYPE resource_kind         AS ENUM ('human','space','asset','virtual');
CREATE TYPE booking_status        AS ENUM ('draft','pending','confirmed','checked_in',
                                           'in_progress','completed','cancelled','no_show');
CREATE TYPE hold_status           AS ENUM ('active','converted','released','expired');
CREATE TYPE waitlist_status       AS ENUM ('waiting','offered','converted','expired','cancelled');
CREATE TYPE availability_effect   AS ENUM ('open','closed');

CREATE TYPE conversation_channel  AS ENUM ('voice_inbound','voice_outbound','web_chat','sms',
                                           'whatsapp','telegram','email','api');
CREATE TYPE conversation_status   AS ENUM ('active','ended','failed','abandoned');
CREATE TYPE turn_role            AS ENUM ('system','user','assistant','tool','event');
CREATE TYPE outcome_kind         AS ENUM ('resolved','booked','rescheduled','cancelled',
                                          'transferred','abandoned','failed','spam');

CREATE TYPE ingestion_status      AS ENUM ('queued','running','succeeded','failed','cancelled');
CREATE TYPE document_status       AS ENUM ('pending','indexed','superseded','failed','excluded');

CREATE TYPE connector_auth_kind   AS ENUM ('none','api_key','basic','oauth2','jwt','mtls','hmac');
CREATE TYPE sync_direction        AS ENUM ('inbound','outbound','bidirectional');
CREATE TYPE sync_health           AS ENUM ('healthy','degraded','failing','disabled');

CREATE TYPE workflow_run_status   AS ENUM ('pending','running','completed','failed',
                                           'cancelled','terminated','timed_out','compensating');

CREATE TYPE notification_channel  AS ENUM ('email','sms','push','webhook','voice');
CREATE TYPE notification_status   AS ENUM ('pending','sending','sent','delivered','bounced',
                                           'failed','suppressed');

CREATE TYPE pii_class             AS ENUM ('none','low','medium','high','phi','pci');
CREATE TYPE outbox_status         AS ENUM ('pending','dispatched','failed','dead');
```

### 1.8 Naming

Per contract §10, tables are plural snake_case. Constraint naming is mechanical
so that migrations can be generated and diffed reliably:

| Object | Pattern | Example |
|---|---|---|
| Primary key | `pk_<table>` | `pk_bookings` |
| Foreign key | `fk_<table>__<ref_table>` | `fk_bookings__resources` |
| Unique | `uq_<table>__<cols>` | `uq_tenants__slug` |
| Check | `ck_<table>__<rule>` | `ck_bookings__time_order` |
| Exclusion | `ex_<table>__<rule>` | `ex_bookings__no_overlap` |
| Index | `ix_<table>__<cols>` | `ix_bookings__tenant_start` |
| RLS policy | `rls_<table>__<verb>` | `rls_bookings__all` |

---

## 2. Tenancy

### 2.1 `tenants`

The root aggregate. This is one of only two tables in the system that is **not**
tenant-scoped in the usual sense — its own `id` *is* the tenant scope.

```sql
CREATE TABLE tenants (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    slug                TEXT NOT NULL,
    legal_name          TEXT NOT NULL,
    display_name        TEXT NOT NULL,

    status              tenant_status NOT NULL DEFAULT 'trial',

    -- Contract §2 escape hatch. NULL = lives in the shared cluster.
    -- A non-null value names a logical shard the ConnectionRouter resolves to
    -- a dedicated engine. Adding a shard is a data change, never a schema change.
    --
    -- Setting this is a COMPLIANCE COMMITMENT, not a performance tweak: it is
    -- set only for tenants whose contract or regulator makes the shared
    -- cluster unacceptable. Per contract A8, a non-null value the router
    -- cannot resolve to a configuration refuses the session rather than
    -- falling back to shared (§15.2).
    shard_key           TEXT,

    -- Default timezone/locale/currency. Locations may override the timezone.
    default_timezone    TEXT NOT NULL DEFAULT 'UTC',
    default_locale      TEXT NOT NULL DEFAULT 'en-US',
    default_currency    CHAR(3) NOT NULL DEFAULT 'USD',

    -- Default PostgreSQL text-search configuration for this tenant's content.
    -- Drives stemming and stop-word removal in every tsvector we build for
    -- them (§7.4.1). REGCONFIG rather than TEXT so an invalid config name is
    -- rejected at write time and so the value can be passed straight to
    -- to_tsvector() without a cast or a lookup table.
    -- 'simple' = no stemming (language-neutral, lower recall); 'english',
    -- 'spanish', 'german', … = stemmed. Overridable per knowledge source.
    default_text_search_config REGCONFIG NOT NULL DEFAULT 'simple',

    -- Data-residency and compliance posture, used by the router and by
    -- provider selection (e.g. force EU LLM endpoints).
    data_region         TEXT NOT NULL DEFAULT 'us-east-1',
    compliance_profile  TEXT NOT NULL DEFAULT 'standard',

    -- KMS key that wraps this tenant's data-encryption keys. See §2.4.
    kms_key_id          TEXT,

    -- Free-form platform metadata. NOT tenant-configurable; see tenant_settings
    -- for that. Used for internal flags, onboarding state, sales attribution.
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    trial_ends_at       TIMESTAMPTZ,
    suspended_at        TIMESTAMPTZ,
    suspension_reason   TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_tenants__slug_format
        CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$'),
    CONSTRAINT ck_tenants__currency_format
        CHECK (default_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_tenants__timezone_valid
        CHECK (now() AT TIME ZONE default_timezone IS NOT NULL),
    CONSTRAINT ck_tenants__compliance_profile
        CHECK (compliance_profile IN ('standard','hipaa','gdpr_strict','pci')),
    CONSTRAINT ck_tenants__suspension_coherent
        CHECK ((status = 'suspended') = (suspended_at IS NOT NULL))
);

-- Slug is the public URL identity; must be unique among live tenants only,
-- so a deleted tenant does not permanently burn a name.
CREATE UNIQUE INDEX uq_tenants__slug
    ON tenants (slug) WHERE deleted_at IS NULL;

CREATE INDEX ix_tenants__shard_key
    ON tenants (shard_key) WHERE shard_key IS NOT NULL;

CREATE INDEX ix_tenants__status
    ON tenants (status) WHERE deleted_at IS NULL;

CREATE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Why `ck_tenants__timezone_valid` works:** `now() AT TIME ZONE <bad name>`
raises `invalid_parameter_value`, which aborts the statement. Using it inside a
CHECK turns an invalid IANA name into a write-time rejection. It is technically
a non-immutable expression in a CHECK, which PostgreSQL permits with a warning
in this form; the risk (a tzdata update invalidating an existing row) is
acceptable and is caught by a nightly validation query rather than at read time.

### 2.2 `locations`

A tenant may have many physical or logical sites: clinics, restaurant branches,
hotel properties. Locations own the operating calendar and the timezone.

```sql
CREATE TABLE locations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    code                TEXT NOT NULL,          -- tenant-scoped short code, e.g. 'downtown'
    name                TEXT NOT NULL,

    timezone            TEXT NOT NULL,          -- IANA; authoritative for all local rendering
    currency            CHAR(3),                -- NULL → inherit tenant default
    locale              TEXT,

    address_line1       TEXT,
    address_line2       TEXT,
    city                TEXT,
    region              TEXT,
    postal_code         TEXT,
    country_code        CHAR(2),

    latitude            NUMERIC(9,6),
    longitude           NUMERIC(9,6),

    phone_e164          TEXT,
    email               TEXT,

    -- Location-level operational config: lead times, buffers, cutoffs.
    -- Merged under tenant_settings at read time (§2.3).
    settings            JSONB NOT NULL DEFAULT '{}'::jsonb,

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_locations__timezone_valid
        CHECK (now() AT TIME ZONE timezone IS NOT NULL),
    CONSTRAINT ck_locations__currency_format
        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_locations__country_format
        CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
    CONSTRAINT ck_locations__phone_e164
        CHECK (phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$'),
    CONSTRAINT ck_locations__geo_paired
        CHECK ((latitude IS NULL) = (longitude IS NULL))
);

CREATE UNIQUE INDEX uq_locations__tenant_code
    ON locations (tenant_id, code) WHERE deleted_at IS NULL;

CREATE INDEX ix_locations__tenant_active
    ON locations (tenant_id, is_active) WHERE deleted_at IS NULL;

CREATE TRIGGER trg_locations_updated_at
    BEFORE UPDATE ON locations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Composite-key note.** Child tables that reference a location do **not** simply
`REFERENCES locations(id)`. They reference `(tenant_id, id)` against a redundant
unique key. This makes it structurally impossible for a row in tenant A to point
at a location in tenant B — an FK violation rather than an RLS-invisible dangling
reference. We add the supporting unique constraint here and repeat the pattern
for every parent that is referenced across tenant-owned tables:

```sql
ALTER TABLE locations
    ADD CONSTRAINT uq_locations__tenant_id_id UNIQUE (tenant_id, id);
ALTER TABLE tenants
    ADD CONSTRAINT uq_tenants__id_id UNIQUE (id);  -- already PK; explicit for symmetry
```

> **Why composite FKs everywhere.** RLS filters *reads*. It does not stop an
> `INSERT` that names a foreign `id` the current tenant cannot see — the FK
> check runs with RLS bypassed for referential integrity, so a cross-tenant
> reference would be *accepted* and then invisible. The composite FK closes
> that hole at the storage layer. This costs one extra unique index per
> referenced parent; that is a price worth paying for "impossible by
> construction" rather than "unlikely by convention" (contract §2.7).

### 2.3 `tenant_settings`

Namespaced, versioned, typed configuration. One row per `(tenant_id, location_id, namespace, key)`.

```sql
CREATE TABLE tenant_settings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- NULL location_id = tenant-wide default. A non-null row overrides it.
    location_id         UUID,

    namespace           TEXT NOT NULL,   -- 'booking' | 'voice' | 'ai' | 'branding' | ...
    key                 TEXT NOT NULL,
    value               JSONB NOT NULL,

    -- The JSON Schema fragment this value was validated against, so we can
    -- re-validate after a platform upgrade and report drift.
    schema_version      TEXT NOT NULL DEFAULT '1',

    is_secret_ref       BOOLEAN NOT NULL DEFAULT FALSE, -- value is {"secret_id": "..."}
    description         TEXT,

    updated_by_user_id  UUID,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_tenant_settings__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_tenant_settings__namespace
        CHECK (namespace ~ '^[a-z][a-z0-9_]{0,31}$'),
    CONSTRAINT ck_tenant_settings__key
        CHECK (key ~ '^[a-z][a-z0-9_.]{0,63}$')
);

-- Two partial uniques rather than one, because NULL never equals NULL in a
-- unique index: without this split, a tenant could insert two tenant-wide rows
-- for the same key.
CREATE UNIQUE INDEX uq_tenant_settings__tenant_ns_key_global
    ON tenant_settings (tenant_id, namespace, key)
    WHERE location_id IS NULL;

CREATE UNIQUE INDEX uq_tenant_settings__tenant_loc_ns_key
    ON tenant_settings (tenant_id, location_id, namespace, key)
    WHERE location_id IS NOT NULL;

-- Settings are read as a whole namespace on nearly every request path
-- ("give me all booking settings for this location"), so the namespace is in
-- the index, not just the key.
CREATE INDEX ix_tenant_settings__tenant_namespace
    ON tenant_settings (tenant_id, namespace, location_id);

CREATE TRIGGER trg_tenant_settings_updated_at
    BEFORE UPDATE ON tenant_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Resolution order** (implemented in `packages/persistence`, not in SQL):
`location row` → `tenant row` → `vertical manifest default` → `platform default`.
The first non-null wins. Kept out of SQL because the manifest layer is a
JSONB document the database should not have to interpret.

### 2.4 `tenant_secrets`

Envelope encryption per contract §8. **Plaintext is never persisted, and never
passes through a column.** The table stores ciphertext plus the wrapped DEK.

```sql
CREATE TABLE tenant_secrets (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    scope               TEXT NOT NULL,    -- 'provider' | 'connector' | 'channel' | 'webhook'
    name                TEXT NOT NULL,    -- 'openai.api_key', 'salesforce.refresh_token'

    -- Envelope: KMS wraps the DEK; the DEK (AES-256-GCM) encrypts the payload.
    -- We store the wrapped DEK, not the DEK.
    kms_key_id          TEXT NOT NULL,
    wrapped_dek         BYTEA NOT NULL,
    ciphertext          BYTEA NOT NULL,
    nonce               BYTEA NOT NULL,
    auth_tag            BYTEA NOT NULL,
    cipher_alg          TEXT NOT NULL DEFAULT 'AES-256-GCM',

    -- Additional authenticated data binds the ciphertext to its identity, so a
    -- row copied into another tenant's slot fails to decrypt rather than
    -- silently working. AAD = tenant_id || scope || name || key_version.
    key_version         INTEGER NOT NULL DEFAULT 1,

    -- Non-secret fingerprint for UI display ("sk-...4f2a") and for detecting
    -- an unchanged re-save without decrypting.
    last_four           TEXT,
    digest_sha256       BYTEA,

    expires_at          TIMESTAMPTZ,
    rotated_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ,

    created_by_user_id  UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,

    CONSTRAINT ck_tenant_secrets__scope
        CHECK (scope IN ('provider','connector','channel','webhook','signing','other')),
    CONSTRAINT ck_tenant_secrets__name
        CHECK (name ~ '^[a-z][a-z0-9_.-]{0,127}$'),
    CONSTRAINT ck_tenant_secrets__nonce_len
        CHECK (octet_length(nonce) BETWEEN 12 AND 16),
    CONSTRAINT ck_tenant_secrets__tag_len
        CHECK (octet_length(auth_tag) = 16),
    CONSTRAINT ck_tenant_secrets__last_four
        CHECK (last_four IS NULL OR length(last_four) <= 8)
);

CREATE UNIQUE INDEX uq_tenant_secrets__tenant_scope_name
    ON tenant_secrets (tenant_id, scope, name) WHERE deleted_at IS NULL;

-- Rotation sweeper: "which secrets expire in the next 7 days, across tenants"
-- runs as the privileged role outside RLS.
CREATE INDEX ix_tenant_secrets__expires
    ON tenant_secrets (expires_at) WHERE expires_at IS NOT NULL AND deleted_at IS NULL;

CREATE TRIGGER trg_tenant_secrets_updated_at
    BEFORE UPDATE ON tenant_secrets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Belt and braces: revoke column-level SELECT on the ciphertext from any role
-- that only needs to enumerate secret names for the admin UI.
-- (Applied to the reporting role in §16.5.)
COMMENT ON COLUMN tenant_secrets.ciphertext IS
    'pii_class=high; envelope-encrypted; never logged, never returned by API';
```

**Why store `digest_sha256`:** when a tenant re-saves an unchanged API key from
the admin UI, we do not want to burn a KMS wrap call or bump `key_version`.
Comparing the SHA-256 of the incoming plaintext (computed in the app, never
stored as plaintext) against `digest_sha256` detects a no-op. The digest is a
one-way function of a high-entropy secret, so it is not itself a disclosure risk
for API keys; for low-entropy secrets (a password) the application salts with
`tenant_id` before digesting.

### 2.5 `tenant_provider_config`

Which adapter implements each port, per tenant, per environment. This is the
table the DI container reads to satisfy contract §5's "adapters are selected
per tenant from configuration."

```sql
CREATE TABLE tenant_provider_config (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    port                TEXT NOT NULL,      -- 'LLMPort' | 'STTPort' | 'TTSPort' | ...
    provider            TEXT NOT NULL,      -- 'openai' | 'anthropic' | 'deepgram' | ...
    model               TEXT,               -- 'gpt-4o-mini', 'nova-2', NULL where n/a

    -- Ordered failover. Lower value tried first. Two rows may share a port;
    -- exactly one must be priority 0 (the primary).
    priority            SMALLINT NOT NULL DEFAULT 0,

    -- Adapter-specific knobs validated against the adapter's Pydantic schema
    -- before write: temperature, voice_id, endpointing_ms, region, ...
    config              JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Reference into tenant_secrets rather than an inline key.
    secret_id           UUID,

    is_enabled          BOOLEAN NOT NULL DEFAULT TRUE,

    -- Guardrails enforced by the adapter wrapper, not the vendor.
    timeout_ms          INTEGER NOT NULL DEFAULT 30000,
    max_retries         SMALLINT NOT NULL DEFAULT 2,
    monthly_budget      NUMERIC(12,4),
    budget_currency     CHAR(3),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_tenant_provider_config__secrets
        FOREIGN KEY (tenant_id, secret_id)
        REFERENCES tenant_secrets (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT ck_tenant_provider_config__port
        CHECK (port IN ('LLMPort','STTPort','TTSPort','RealtimeVoicePort','EmbeddingPort',
                        'VectorStorePort','RerankerPort','ConnectorPort','ChannelPort',
                        'StoragePort','SecretsPort','BillingPort','NotificationPort')),
    CONSTRAINT ck_tenant_provider_config__timeout
        CHECK (timeout_ms BETWEEN 100 AND 600000),
    CONSTRAINT ck_tenant_provider_config__retries
        CHECK (max_retries BETWEEN 0 AND 10),
    CONSTRAINT ck_tenant_provider_config__priority
        CHECK (priority BETWEEN 0 AND 9),
    CONSTRAINT ck_tenant_provider_config__budget_paired
        CHECK ((monthly_budget IS NULL) = (budget_currency IS NULL))
);

ALTER TABLE tenant_secrets
    ADD CONSTRAINT uq_tenant_secrets__tenant_id_id UNIQUE (tenant_id, id);

-- One row per (port, priority): the failover chain is a total order.
CREATE UNIQUE INDEX uq_tenant_provider_config__tenant_port_priority
    ON tenant_provider_config (tenant_id, port, priority);

-- Exactly one primary per port.
CREATE UNIQUE INDEX uq_tenant_provider_config__tenant_port_primary
    ON tenant_provider_config (tenant_id, port) WHERE priority = 0;

-- Hot path: "resolve the enabled adapter chain for LLMPort for this tenant",
-- executed on every conversation turn. Covered by the index; no heap access
-- needed for the ordering decision.
CREATE INDEX ix_tenant_provider_config__tenant_port_enabled
    ON tenant_provider_config (tenant_id, port, priority)
    INCLUDE (provider, model, secret_id)
    WHERE is_enabled;

CREATE TRIGGER trg_tenant_provider_config_updated_at
    BEFORE UPDATE ON tenant_provider_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 2.6 `plans` and `subscriptions`

`plans` is a **platform catalogue** table — not tenant-owned, readable by all,
writable only by the privileged role.

```sql
CREATE TABLE plans (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    code                TEXT NOT NULL,      -- 'starter' | 'growth' | 'enterprise'
    name                TEXT NOT NULL,
    description         TEXT,

    billing_interval    TEXT NOT NULL,      -- 'month' | 'year'
    base_price          NUMERIC(12,4) NOT NULL,
    currency            CHAR(3) NOT NULL,

    -- Hard entitlements checked by the service layer. Structure:
    -- {"max_locations": 5, "max_resources": 50, "included_minutes": 2000,
    --  "features": ["voice","whatsapp","custom_connectors"]}
    entitlements        JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Overage rates keyed by meter name, applied after included quantity.
    -- {"voice_minutes": {"rate": "0.0900", "included": 2000}, ...}
    metered_rates       JSONB NOT NULL DEFAULT '{}'::jsonb,

    external_price_id   TEXT,               -- Stripe price id
    is_public           BOOLEAN NOT NULL DEFAULT TRUE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order          SMALLINT NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_plans__interval CHECK (billing_interval IN ('month','year')),
    CONSTRAINT ck_plans__price_nonneg CHECK (base_price >= 0),
    CONSTRAINT ck_plans__currency CHECK (currency ~ '^[A-Z]{3}$')
);

CREATE UNIQUE INDEX uq_plans__code_currency_interval
    ON plans (code, currency, billing_interval) WHERE is_active;

CREATE TRIGGER trg_plans_updated_at
    BEFORE UPDATE ON plans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

```sql
CREATE TABLE subscriptions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id             UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,

    status              subscription_status NOT NULL DEFAULT 'trialing',

    -- The billing period this subscription is currently in. Half-open
    -- [start, end) so consecutive periods never overlap at the boundary.
    current_period      TSTZRANGE NOT NULL,

    quantity            INTEGER NOT NULL DEFAULT 1,   -- seats, if seat-priced

    -- Snapshot of entitlements at purchase time. A plan change updates the
    -- plan_id AND this snapshot; grandfathered tenants keep old terms without
    -- us versioning the whole plans table.
    entitlements_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,

    external_customer_id     TEXT,
    external_subscription_id TEXT,

    trial_ends_at       TIMESTAMPTZ,
    cancel_at           TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_subscriptions__quantity CHECK (quantity > 0),
    CONSTRAINT ck_subscriptions__period_bounds
        CHECK (lower(current_period) IS NOT NULL AND upper(current_period) IS NOT NULL),
    CONSTRAINT ck_subscriptions__cancel_coherent
        CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL)
);

-- A tenant has at most one live subscription. Enforced as a partial unique
-- rather than a CHECK because it is a cross-row invariant.
CREATE UNIQUE INDEX uq_subscriptions__tenant_live
    ON subscriptions (tenant_id)
    WHERE status IN ('trialing','active','past_due','paused');

CREATE INDEX ix_subscriptions__tenant_status
    ON subscriptions (tenant_id, status);

-- Billing sweeper (privileged role, cross-tenant): "which periods close today".
CREATE INDEX ix_subscriptions__period_end
    ON subscriptions (upper(current_period))
    WHERE status IN ('trialing','active','past_due');

CREATE UNIQUE INDEX uq_subscriptions__external_sub
    ON subscriptions (external_subscription_id)
    WHERE external_subscription_id IS NOT NULL;

CREATE TRIGGER trg_subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Why `TSTZRANGE` for the billing period** rather than two columns: it makes
"which subscription covered 2026-03-15" a single `@>` containment test that a
GiST index can answer, and it makes the half-open semantics explicit rather than
a convention everyone has to remember. The same reasoning drives the booking
engine's use of ranges in §5.

---

## 3. Identity & Access

### 3.1 Design: users are global, membership is tenant-scoped

A person may work for two tenants (an agency managing three restaurants; a
contractor covering two clinics). Modelling `users` as tenant-owned forces
duplicate accounts with duplicate credentials and duplicate MFA enrolment — a
security and UX regression.

So: `users` is a **global** table keyed by email. `user_tenant_roles` is the
tenant-owned join that grants access. RLS on `users` is therefore *not* a
`tenant_id` predicate; it is an existence check against the membership table
(§11.5). This is the one deliberate departure from "every table has `tenant_id`",
and it is a departure the contract anticipates: contract §2.1 says every
*tenant-owned* table has `tenant_id`. `users` is not tenant-owned.

### 3.2 `users`

```sql
CREATE TABLE users (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    email                   TEXT NOT NULL,
    email_normalized        TEXT GENERATED ALWAYS AS (lower(btrim(email))) STORED,
    email_verified_at       TIMESTAMPTZ,

    -- NULL for SSO-only users who never set a local password.
    password_hash           TEXT,
    password_algo           TEXT,               -- 'argon2id'
    password_updated_at     TIMESTAMPTZ,

    full_name               TEXT,
    display_name            TEXT,
    avatar_url              TEXT,
    phone_e164              TEXT,
    phone_verified_at       TIMESTAMPTZ,

    locale                  TEXT NOT NULL DEFAULT 'en-US',
    timezone                TEXT NOT NULL DEFAULT 'UTC',

    status                  user_status NOT NULL DEFAULT 'invited',

    -- Platform staff. Bypasses nothing in the DB; only unlocks admin routes,
    -- which then set app.platform_admin explicitly and audibly.
    is_platform_staff       BOOLEAN NOT NULL DEFAULT FALSE,

    mfa_enforced            BOOLEAN NOT NULL DEFAULT FALSE,
    failed_login_count      SMALLINT NOT NULL DEFAULT 0,
    locked_until            TIMESTAMPTZ,
    last_login_at           TIMESTAMPTZ,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ,
    version                 INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_users__email_shape
        CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
    CONSTRAINT ck_users__phone_e164
        CHECK (phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$'),
    CONSTRAINT ck_users__password_paired
        CHECK ((password_hash IS NULL) = (password_algo IS NULL)),
    CONSTRAINT ck_users__timezone_valid
        CHECK (now() AT TIME ZONE timezone IS NOT NULL)
);

-- Uniqueness on the normalized form: 'Bob@X.com' and 'bob@x.com' are one
-- account. Generated column rather than a functional index so the normalized
-- value is also queryable and joinable.
CREATE UNIQUE INDEX uq_users__email_normalized
    ON users (email_normalized) WHERE deleted_at IS NULL;

-- Admin user search ("find bob") without a leading-wildcard seq scan.
CREATE INDEX ix_users__email_trgm
    ON users USING gin (email_normalized gin_trgm_ops);

CREATE INDEX ix_users__locked
    ON users (locked_until) WHERE locked_until IS NOT NULL;

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN users.password_hash IS 'pii_class=high; never logged';
COMMENT ON COLUMN users.email          IS 'pii_class=medium; masked in traces';
COMMENT ON COLUMN users.phone_e164     IS 'pii_class=medium; masked in traces';
```

### 3.3 `permissions`, `roles`, `role_permissions`

Permissions are a **platform catalogue**: a fixed vocabulary shipped with the
release, not tenant data. Roles are *either* platform-defined templates
(`tenant_id IS NULL`) *or* tenant-authored custom roles.

```sql
CREATE TABLE permissions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    -- '<resource>:<action>' e.g. 'booking:create', 'knowledge:publish'
    code            TEXT NOT NULL,
    resource        TEXT NOT NULL,
    action          TEXT NOT NULL,
    description     TEXT NOT NULL,

    -- Permissions that only make sense with a feature entitlement.
    requires_feature TEXT,

    -- Dangerous permissions require a fresh MFA assertion at use time.
    is_sensitive    BOOLEAN NOT NULL DEFAULT FALSE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_permissions__code_shape
        CHECK (code ~ '^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$'),
    CONSTRAINT ck_permissions__code_matches_parts
        CHECK (code = resource || ':' || action)
);

CREATE UNIQUE INDEX uq_permissions__code ON permissions (code);
CREATE INDEX ix_permissions__resource ON permissions (resource);
```

```sql
CREATE TABLE roles (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    -- NULL = platform-defined template role, visible to every tenant.
    -- Non-null = a role this tenant authored.
    tenant_id           UUID REFERENCES tenants(id) ON DELETE CASCADE,

    code                TEXT NOT NULL,      -- 'owner','admin','manager','agent','viewer'
    name                TEXT NOT NULL,
    description         TEXT,

    is_system           BOOLEAN NOT NULL DEFAULT FALSE,  -- undeletable, uneditable

    -- Ordering for "can this role manage that role" checks. Higher = more
    -- powerful. A user may only assign roles at or below their own rank.
    rank                SMALLINT NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_roles__code_shape CHECK (code ~ '^[a-z][a-z0-9_]{0,31}$'),
    CONSTRAINT ck_roles__system_is_global CHECK (NOT is_system OR tenant_id IS NULL),
    CONSTRAINT ck_roles__rank CHECK (rank BETWEEN 0 AND 100)
);

CREATE UNIQUE INDEX uq_roles__global_code
    ON roles (code) WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX uq_roles__tenant_code
    ON roles (tenant_id, code) WHERE tenant_id IS NOT NULL;

-- Needed as the target of the composite FK from role_permissions, which must
-- tolerate a NULL tenant_id on template roles. See note below.
CREATE UNIQUE INDEX uq_roles__id_tenant ON roles (id, tenant_id);

CREATE TRIGGER trg_roles_updated_at
    BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

```sql
CREATE TABLE role_permissions (
    role_id         UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id   UUID NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,

    -- Denormalized from roles.tenant_id so RLS on this table is a plain
    -- predicate rather than a subquery join. NULL for template roles.
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,

    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    CONSTRAINT pk_role_permissions PRIMARY KEY (role_id, permission_id),
    CONSTRAINT fk_role_permissions__role_tenant
        FOREIGN KEY (role_id, tenant_id) REFERENCES roles (id, tenant_id) ON DELETE CASCADE
);

-- Reverse lookup: "who has knowledge:publish" for an audit report.
CREATE INDEX ix_role_permissions__permission ON role_permissions (permission_id, role_id);
CREATE INDEX ix_role_permissions__tenant ON role_permissions (tenant_id) WHERE tenant_id IS NOT NULL;
```

> **On the denormalized `tenant_id`:** RLS predicates that require a subquery
> (`EXISTS (SELECT 1 FROM roles r WHERE r.id = role_permissions.role_id AND ...)`)
> are evaluated per row and defeat index-only scans. Carrying `tenant_id` and
> binding it with a composite FK gives the same integrity guarantee with a
> predicate the planner can push into an index scan. This trade — denormalize
> `tenant_id` onto join tables — recurs throughout the schema and is deliberate.

### 3.4 `user_tenant_roles`

The membership table. This is what makes a user "in" a tenant at all.

```sql
CREATE TABLE user_tenant_roles (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id             UUID NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,

    -- Optional narrowing: a manager scoped to one location only.
    -- NULL = all locations in the tenant.
    location_id         UUID,

    -- Denormalized for fast "what can this user do here" without a join.
    -- Refreshed by a trigger on role_permissions; see below.
    is_owner            BOOLEAN NOT NULL DEFAULT FALSE,

    invited_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    invited_at          TIMESTAMPTZ,
    accepted_at         TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,        -- time-boxed contractor access

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at          TIMESTAMPTZ,

    CONSTRAINT fk_user_tenant_roles__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE
);

-- A user holds a given role at a given scope at most once (while live).
CREATE UNIQUE INDEX uq_user_tenant_roles__tenant_user_role_global
    ON user_tenant_roles (tenant_id, user_id, role_id)
    WHERE location_id IS NULL AND revoked_at IS NULL;

CREATE UNIQUE INDEX uq_user_tenant_roles__tenant_user_role_loc
    ON user_tenant_roles (tenant_id, user_id, role_id, location_id)
    WHERE location_id IS NOT NULL AND revoked_at IS NULL;

-- Hot path A: authorization on every request — "roles of user U in tenant T".
CREATE INDEX ix_user_tenant_roles__tenant_user
    ON user_tenant_roles (tenant_id, user_id)
    INCLUDE (role_id, location_id)
    WHERE revoked_at IS NULL;

-- Hot path B: login — "which tenants can user U enter". Leads with user_id
-- because at login time there is no tenant context yet. This index is the
-- deliberate exception to contract §2.6's tenant-first rule, and it is safe
-- precisely because it is the index that *establishes* tenant context.
CREATE INDEX ix_user_tenant_roles__user_tenant
    ON user_tenant_roles (user_id, tenant_id)
    WHERE revoked_at IS NULL;

-- Expiry sweeper.
CREATE INDEX ix_user_tenant_roles__expires
    ON user_tenant_roles (expires_at)
    WHERE expires_at IS NOT NULL AND revoked_at IS NULL;

CREATE TRIGGER trg_user_tenant_roles_updated_at
    BEFORE UPDATE ON user_tenant_roles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**The `ix_user_tenant_roles__user_tenant` exception, stated plainly.** Contract
§2.6 requires every index on a tenant-owned table to lead with `tenant_id`. The
purpose of that rule is to guarantee that RLS-filtered queries — which always
carry `tenant_id = app_current_tenant()` — can use an index. Here we need the
opposite access path exactly once, during login, when `app.tenant_id` is by
definition not yet known. That query runs as a dedicated `auth` role against a
narrow security-definer function (§11.5), not as the general runtime role. It is
the only such index in the schema, and it is called out here so no future reader
mistakes it for drift.

### 3.5 `api_keys`

Machine credentials for tenant-side integrations hitting `/api/v1/*`.

```sql
CREATE TABLE api_keys (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    name                TEXT NOT NULL,

    -- Presented key is 'rk_<prefix>_<secret>'. We store the prefix in clear
    -- (for lookup and for showing the user which key is which) and only a
    -- hash of the secret half.
    key_prefix          TEXT NOT NULL,
    key_hash            BYTEA NOT NULL,      -- SHA-256 of the secret half
    key_algo            TEXT NOT NULL DEFAULT 'sha256',

    -- Explicit grant list, intersected with the creating user's own
    -- permissions at issue time. A key can never exceed its creator.
    scopes              TEXT[] NOT NULL DEFAULT '{}',

    -- Optional narrowing.
    location_id         UUID,

    -- CIDR allow-list. Empty array = any source.
    allowed_ips         INET[] NOT NULL DEFAULT '{}',

    status              api_key_status NOT NULL DEFAULT 'active',

    rate_limit_rpm      INTEGER,             -- NULL = tenant default

    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    last_used_at        TIMESTAMPTZ,
    last_used_ip        INET,
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    revoked_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_api_keys__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_api_keys__prefix_shape CHECK (key_prefix ~ '^rk_[a-z0-9]{8,16}$'),
    CONSTRAINT ck_api_keys__hash_len CHECK (octet_length(key_hash) = 32),
    CONSTRAINT ck_api_keys__revoked_coherent
        CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CONSTRAINT ck_api_keys__rate_limit CHECK (rate_limit_rpm IS NULL OR rate_limit_rpm > 0)
);

-- Authentication lookup is by prefix ALONE (globally unique), because at
-- authentication time we do not yet know the tenant. Same rationale as the
-- login index in §3.4: this is the query that establishes context.
CREATE UNIQUE INDEX uq_api_keys__key_prefix ON api_keys (key_prefix);

CREATE INDEX ix_api_keys__tenant_status
    ON api_keys (tenant_id, status) WHERE status = 'active';

CREATE INDEX ix_api_keys__expires
    ON api_keys (expires_at) WHERE expires_at IS NOT NULL AND status = 'active';

CREATE TRIGGER trg_api_keys_updated_at
    BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN api_keys.key_hash IS 'pii_class=high; SHA-256 of secret half; never returned';
```

**Why SHA-256 and not Argon2 for API keys** (unlike passwords): the secret half
is 256 bits of CSPRNG output, so there is no dictionary to attack — key
stretching buys nothing and costs ~50 ms on the hottest authentication path in
the system. Passwords are low-entropy and human-chosen, so they get Argon2id.
This asymmetry is intentional, not an oversight.

### 3.6 `sessions`

Interactive browser sessions for the Next.js admin portal.

```sql
CREATE TABLE sessions (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- The tenant this session is currently "acting as". A user with three
    -- memberships switches tenants by minting a new session, not by mutating
    -- this — so a stolen cookie cannot be pivoted to another tenant.
    tenant_id               UUID REFERENCES tenants(id) ON DELETE CASCADE,

    -- Opaque refresh token, hashed. The short-lived access JWT is never stored.
    refresh_token_hash      BYTEA NOT NULL,

    -- Rotation chain: each refresh mints a new row pointing at its parent.
    -- Reuse of a rotated token is a theft signal and revokes the whole family.
    parent_session_id       UUID REFERENCES sessions(id) ON DELETE SET NULL,
    family_id               UUID NOT NULL,

    ip_address              INET,
    user_agent              TEXT,
    device_fingerprint      TEXT,

    -- Which factors were satisfied, and when. Sensitive permissions require a
    -- recent mfa_at (step-up auth).
    mfa_at                  TIMESTAMPTZ,
    auth_method             TEXT NOT NULL DEFAULT 'password',

    issued_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at              TIMESTAMPTZ NOT NULL,
    revoked_at              TIMESTAMPTZ,
    revoked_reason          TEXT,

    CONSTRAINT ck_sessions__hash_len CHECK (octet_length(refresh_token_hash) = 32),
    CONSTRAINT ck_sessions__expiry_future CHECK (expires_at > issued_at),
    CONSTRAINT ck_sessions__auth_method
        CHECK (auth_method IN ('password','oauth','saml','magic_link','impersonation'))
);

CREATE UNIQUE INDEX uq_sessions__refresh_token_hash ON sessions (refresh_token_hash);

CREATE INDEX ix_sessions__user_live
    ON sessions (user_id, expires_at DESC) WHERE revoked_at IS NULL;

-- Revoke-the-family on token reuse.
CREATE INDEX ix_sessions__family ON sessions (family_id) WHERE revoked_at IS NULL;

-- Reaper. Partial index keeps it small: only unexpired rows matter.
CREATE INDEX ix_sessions__expires ON sessions (expires_at) WHERE revoked_at IS NULL;

CREATE INDEX ix_sessions__tenant_user
    ON sessions (tenant_id, user_id) WHERE tenant_id IS NOT NULL AND revoked_at IS NULL;
```

**Why no `updated_at` trigger on `sessions`:** `last_seen_at` is written on
nearly every request. A trigger would double the write amplification for no
benefit. `last_seen_at` is updated by the app, throttled to at most once per
60 seconds per session, to keep this off the hot path.

### 3.7 `mfa_factors`

```sql
CREATE TABLE mfa_factors (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    factor_type         mfa_factor_type NOT NULL,
    label               TEXT,                       -- "iPhone", "YubiKey 5C"

    -- TOTP: encrypted shared secret. WebAuthn: credential public key + id.
    -- Recovery codes: one row per code, hash only.
    secret_ciphertext   BYTEA,
    secret_nonce        BYTEA,
    wrapped_dek         BYTEA,
    kms_key_id          TEXT,

    credential_id       BYTEA,                      -- WebAuthn credential id
    public_key          BYTEA,                      -- WebAuthn COSE key
    sign_count          BIGINT NOT NULL DEFAULT 0,  -- WebAuthn clone detection
    aaguid              UUID,
    transports          TEXT[],

    code_hash           BYTEA,                      -- recovery code (SHA-256)
    consumed_at         TIMESTAMPTZ,                -- recovery code single-use

    phone_e164          TEXT,                       -- SMS factor

    is_verified         BOOLEAN NOT NULL DEFAULT FALSE,
    last_used_at        TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at          TIMESTAMPTZ,

    -- Each factor type requires its own payload and forbids the others'.
    CONSTRAINT ck_mfa_factors__totp_shape CHECK (
        factor_type <> 'totp'
        OR (secret_ciphertext IS NOT NULL AND secret_nonce IS NOT NULL
            AND wrapped_dek IS NOT NULL)
    ),
    CONSTRAINT ck_mfa_factors__webauthn_shape CHECK (
        factor_type <> 'webauthn'
        OR (credential_id IS NOT NULL AND public_key IS NOT NULL)
    ),
    CONSTRAINT ck_mfa_factors__sms_shape CHECK (
        factor_type <> 'sms'
        OR (phone_e164 IS NOT NULL AND phone_e164 ~ '^\+[1-9]\d{6,14}$')
    ),
    CONSTRAINT ck_mfa_factors__recovery_shape CHECK (
        factor_type <> 'recovery_code' OR code_hash IS NOT NULL
    )
);

CREATE INDEX ix_mfa_factors__user_type
    ON mfa_factors (user_id, factor_type) WHERE revoked_at IS NULL;

-- WebAuthn assertion lookup is by credential_id, globally.
CREATE UNIQUE INDEX uq_mfa_factors__credential_id
    ON mfa_factors (credential_id) WHERE credential_id IS NOT NULL;

-- A user has at most one verified TOTP factor.
CREATE UNIQUE INDEX uq_mfa_factors__user_totp
    ON mfa_factors (user_id)
    WHERE factor_type = 'totp' AND is_verified AND revoked_at IS NULL;

CREATE TRIGGER trg_mfa_factors_updated_at
    BEFORE UPDATE ON mfa_factors
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 3.8 `oauth_identities`

External identity links: Google/Microsoft social login, and per-tenant SAML/OIDC.

```sql
CREATE TABLE oauth_identities (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    provider            TEXT NOT NULL,      -- 'google','microsoft','okta','saml:<tenant_slug>'
    provider_user_id    TEXT NOT NULL,      -- 'sub' claim; stable per provider

    -- For tenant-scoped enterprise IdPs, which tenant's connection produced
    -- this identity. NULL for global social providers.
    tenant_id           UUID REFERENCES tenants(id) ON DELETE CASCADE,

    email               TEXT,
    email_verified      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Raw claims for debugging IdP mapping issues. PII-bearing; retained 90d.
    raw_claims          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Provider refresh token, envelope-encrypted, when we need offline access
    -- (e.g. Google Calendar for a staff member).
    refresh_ciphertext  BYTEA,
    refresh_nonce       BYTEA,
    wrapped_dek         BYTEA,
    kms_key_id          TEXT,
    scopes_granted      TEXT[] NOT NULL DEFAULT '{}',

    linked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at       TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    unlinked_at         TIMESTAMPTZ,

    CONSTRAINT ck_oauth_identities__provider_shape
        CHECK (provider ~ '^[a-z][a-z0-9_]*(:[a-z0-9-]+)?$')
);

-- One account per (provider, subject). This is the login lookup key.
CREATE UNIQUE INDEX uq_oauth_identities__provider_subject
    ON oauth_identities (provider, provider_user_id) WHERE unlinked_at IS NULL;

CREATE INDEX ix_oauth_identities__user
    ON oauth_identities (user_id) WHERE unlinked_at IS NULL;

CREATE INDEX ix_oauth_identities__tenant
    ON oauth_identities (tenant_id, provider) WHERE tenant_id IS NOT NULL;

CREATE TRIGGER trg_oauth_identities_updated_at
    BEFORE UPDATE ON oauth_identities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN oauth_identities.raw_claims IS
    'pii_class=medium; retained 90d then nulled by retention job';
```

### 3.9 `customers` and `customer_identities`

**Adopted from `04-VOICE-CHANNELS.md` §11.2.1's handoff specification.** That
document owns the *linking semantics* (§11.3) and the *privacy constraints*
(§11.4); this document is the authority on the schema from here. Where the DDL
below differs from the version reproduced there, this version governs — the
differences are the composite-FK correction that document itself requested, plus
the conventions every other table in this schema follows (§1.4 column set,
`updated_at` trigger, partial uniques on soft delete).

**Why these are in Identity and not Booking.** A customer is a *person the
tenant knows*, resolved across channels — the same category of thing as `users`,
differing only in that a user authenticates into our portal while a customer is
recognised by a channel handle. Placing it here also keeps the distinction
sharp: `bookings.customer_name` remains a denormalized snapshot of who booked
(§5.9), while `customers` is the resolved identity. Those are genuinely
different facts and conflating them was the reason §18 deferred this table.

**Why it is no longer deferred.** §18 originally deferred `customers` pending a
design for merge/dedupe/identity-resolution semantics. `04` §11.3 supplies
exactly that — link only on platform-verified identifiers, never on spoken
names — so the blocking question is answered and the entry is retired.

#### The hash/encrypt split, and why it must not be collapsed

`04` §11.2 flags this as the detail most likely to be "simplified" during
implementation. It is load-bearing, so the reason is recorded in the DDL itself:

- `identifier_hash` is `HMAC-SHA256(tenant_pepper, normalized_identifier)`. It is
  the **lookup key** — indexed, constant-time to compare, and useless to an
  attacker who does not hold the tenant's pepper.
- `identifier_enc` is the envelope-encrypted actual value. It is needed to
  *place a call or send a message*, and is never indexed or searched.

Collapsing these into one indexed plaintext column would turn the index into a
plaintext directory of every customer phone number the platform holds — readable
by anything with database read access, including a backup, a replica, or a
mis-scoped analytics grant. Storing only the hash would make the row unusable
for outbound contact.

The pepper is **per tenant**, so the same phone number produces different hashes
in different tenants. This is what makes cross-tenant correlation impossible
even for an actor with full read access — the property Contract §2.7 demands.
The pepper lives in `tenant_secrets` under `scope='signing'`, and is never
stored beside the hashes it produces.

```sql
CREATE TABLE customers (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- ── Identity, encrypted ────────────────────────────────────────────────
    -- Envelope-encrypted exactly as tenant_secrets (§2.4): KMS wraps a DEK,
    -- the DEK encrypts the payload. A display name is PII (Contract §8) and a
    -- cross-channel profile makes it more sensitive, not less.
    display_name_enc    BYTEA,
    display_name_nonce  BYTEA,
    wrapped_dek         BYTEA,
    kms_key_id          TEXT,
    cipher_alg          TEXT NOT NULL DEFAULT 'AES-256-GCM',
    key_version         INTEGER NOT NULL DEFAULT 1,

    -- Non-secret display affordance for the admin UI ("R••• K••"), computed at
    -- write time. Lets a staff member disambiguate two customers without the
    -- API decrypting anything.
    display_hint        TEXT,

    locale              TEXT,
    timezone            TEXT,

    -- ── Privacy controls (04 §11.4) ────────────────────────────────────────
    -- When true, this customer's identities must not be used to link channels.
    -- Enforced at QUERY time, not only at write time, so an opt-out takes
    -- effect for rows that were already linked. See the note below on why this
    -- is not an RLS policy.
    linking_opt_out     BOOLEAN NOT NULL DEFAULT FALSE,
    linking_opt_out_at  TIMESTAMPTZ,
    -- Recorded lawful basis for holding a cross-channel profile at all.
    consent_basis       TEXT,
    consent_recorded_at TIMESTAMPTZ,

    -- Drives the 04 §11.4(8) dormancy check and the retention sweep (§17.3).
    last_seen_at        TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_customers__enc_paired
        CHECK ((display_name_enc IS NULL)
               = (display_name_nonce IS NULL AND wrapped_dek IS NULL)),
    CONSTRAINT ck_customers__optout_coherent
        CHECK (linking_opt_out = (linking_opt_out_at IS NOT NULL)),
    CONSTRAINT ck_customers__timezone_valid
        CHECK (timezone IS NULL OR now() AT TIME ZONE timezone IS NOT NULL)
);

-- REQUIRED by every child's composite FK (§2.2). Without it a child row could
-- name a customer belonging to another tenant: the FK check runs with RLS
-- bypassed, so the reference would be accepted and then invisible.
ALTER TABLE customers
    ADD CONSTRAINT uq_customers__tenant_id_id UNIQUE (tenant_id, id);

-- Admin list and the dormancy sweep: "customers not seen since X".
CREATE INDEX ix_customers__tenant_last_seen
    ON customers (tenant_id, last_seen_at DESC) WHERE deleted_at IS NULL;

-- Opt-out enforcement is a hot read on the resolution path; partial index
-- keeps it proportional to the (small) set of opted-out customers.
CREATE INDEX ix_customers__tenant_optout
    ON customers (tenant_id) WHERE linking_opt_out AND deleted_at IS NULL;

CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN customers.display_name_enc IS
    'pii_class=high; envelope-encrypted; never logged, never in an LLM prompt '
    'unless the tenant opted in for that field (Contract §8)';
```

```sql
CREATE TABLE customer_identities (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id         UUID NOT NULL,

    -- Reuses the platform's channel vocabulary rather than a parallel TEXT
    -- domain, so a new channel is added in one place. 'voice_inbound' and
    -- 'voice_outbound' both normalize to E.164 and are interchangeable for
    -- linking purposes; the resolver treats them as one identifier space.
    channel             conversation_channel NOT NULL,

    -- ── The hash/encrypt split (see the note above; do not collapse) ───────
    -- Lookup key: HMAC-SHA256(tenant_pepper, normalized_identifier).
    -- Indexed. Constant-time comparison. Useless without the tenant pepper.
    identifier_hash     BYTEA NOT NULL,
    -- The usable value: envelope-encrypted. Needed to place a call or send a
    -- message. NEVER indexed, never searched, never logged.
    identifier_enc      BYTEA NOT NULL,
    identifier_nonce    BYTEA NOT NULL,
    wrapped_dek         BYTEA NOT NULL,
    kms_key_id          TEXT NOT NULL,
    cipher_alg          TEXT NOT NULL DEFAULT 'AES-256-GCM',
    key_version         INTEGER NOT NULL DEFAULT 1,

    -- Non-secret suffix for the admin UI ("···0142"). Short enough not to
    -- reconstitute the identifier, long enough for a human to recognise.
    identifier_hint     TEXT,

    -- ── Link provenance (04 §11.3) ─────────────────────────────────────────
    -- Was ownership of this handle proven by the platform? Only verified
    -- identities may be auto-linked; this is the audit evidence for that rule.
    verified            BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at         TIMESTAMPTZ,
    -- Which basis produced the link. 'deterministic' = same platform-verified
    -- identifier on another channel. 'verified_otp' = we sent a code.
    -- 'agent_confirmed' = a human confirmed in-conversation. 'manual' = staff.
    link_method         TEXT NOT NULL,
    -- The channel we linked FROM, for deterministic links. Null for the first
    -- identity of a customer.
    source_channel      conversation_channel,
    linked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    linked_by           TEXT,

    -- ── Unlink (04 §11.4) ──────────────────────────────────────────────────
    -- Unlinking is reversible-by-audit, not by resurrection: the row is
    -- retired rather than deleted so the fact that a link existed survives,
    -- which is what makes the action auditable.
    unlinked_at         TIMESTAMPTZ,
    unlink_reason       TEXT,
    unlinked_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    last_seen_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The composite FK 04 §11.2.1(1) asked for, replacing its original
    -- single-column REFERENCES customers(id).
    CONSTRAINT fk_customer_identities__customers
        FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,

    CONSTRAINT ck_customer_identities__hash_len
        CHECK (octet_length(identifier_hash) = 32),
    CONSTRAINT ck_customer_identities__nonce_len
        CHECK (octet_length(identifier_nonce) BETWEEN 12 AND 16),
    CONSTRAINT ck_customer_identities__link_method
        CHECK (link_method IN ('deterministic','verified_otp',
                               'agent_confirmed','manual')),
    -- Only a verified identity may be auto-linked deterministically. This
    -- encodes 04 §11.3's controlling rule as a storage constraint rather than
    -- leaving it to the resolver to remember.
    CONSTRAINT ck_customer_identities__deterministic_requires_verified
        CHECK (link_method <> 'deterministic' OR verified),
    CONSTRAINT ck_customer_identities__verified_coherent
        CHECK (verified = (verified_at IS NOT NULL)),
    CONSTRAINT ck_customer_identities__unlink_coherent
        CHECK ((unlinked_at IS NULL) OR (unlink_reason IS NOT NULL)),
    CONSTRAINT ck_customer_identities__hint_short
        CHECK (identifier_hint IS NULL OR length(identifier_hint) <= 12)
);

-- One live identity per (channel, identifier) within a tenant. Partial on
-- unlinked_at so retiring a link frees the handle for re-linking later —
-- the same partial-unique pattern used for soft deletes throughout (§13.5).
CREATE UNIQUE INDEX uq_customer_identities__tenant_channel_hash
    ON customer_identities (tenant_id, channel, identifier_hash)
    WHERE unlinked_at IS NULL;

-- ════════════════════════════════════════════════════════════════════════════
-- THE RESOLUTION LOOKUP — the hot path, run on every inbound contact.
-- IdentityResolver.resolve() probes (tenant_id, identifier_hash) first for the
-- same channel, then across the phone-channel set. Leading with tenant_id
-- satisfies Contract §2.6 and confines the probe to one tenant's hash space,
-- which is also what makes the per-tenant pepper effective.
CREATE INDEX ix_customer_identities__tenant_hash
    ON customer_identities (tenant_id, identifier_hash)
    INCLUDE (customer_id, channel, verified)
    WHERE unlinked_at IS NULL;

-- "Every channel handle for this customer" — the profile view and the
-- erasure cascade.
CREATE INDEX ix_customer_identities__tenant_customer
    ON customer_identities (tenant_id, customer_id)
    WHERE unlinked_at IS NULL;

CREATE TRIGGER trg_customer_identities_updated_at
    BEFORE UPDATE ON customer_identities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN customer_identities.identifier_hash IS
    'pii_class=high; HMAC-SHA256 under a PER-TENANT pepper from tenant_secrets. '
    'Lookup key only. Do NOT collapse with identifier_enc into a plaintext '
    'indexed column — see 01 §3.9 and 04 §11.2.';
COMMENT ON COLUMN customer_identities.identifier_enc IS
    'pii_class=high; envelope-encrypted E.164 or handle. Never indexed, never '
    'logged. Decrypted only to place an outbound contact.';
```

#### Wiring the existing `customer_id` columns

Three tables already carry a bare `customer_id UUID` with no referential
integrity — written before this table existed. They now get composite FKs:

```sql
ALTER TABLE bookings
    ADD CONSTRAINT fk_bookings__customers
    FOREIGN KEY (tenant_id, customer_id)
    REFERENCES customers (tenant_id, id) ON DELETE SET NULL;

ALTER TABLE waitlist_entries
    ADD CONSTRAINT fk_waitlist_entries__customers
    FOREIGN KEY (tenant_id, customer_id)
    REFERENCES customers (tenant_id, id) ON DELETE SET NULL;
```

`ON DELETE SET NULL` rather than `CASCADE` on both: erasing a customer must not
delete their bookings. A booking is a business and financial record with its own
retention obligations, and it already carries the denormalized
`customer_name` / `customer_phone_e164` snapshot it needs to remain meaningful.
Erasure nulls the *link* and redacts the snapshot columns; it does not destroy
the appointment history. This is the same reasoning that made those columns
denormalized in the first place (§5.9).

`conversations.customer_id` is handled separately in §6.1 because
`conversations` is partitioned — see the note there.

#### `linking_opt_out` is a query-time rule, not an RLS policy

`04` §11.4(5) requires opt-out to affect **already-linked** rows, not merely
prevent new links. The coordinator asked whether this is expressible against the
RLS model. It is, but it should **not** be an RLS policy, for two reasons:

1. **RLS is the wrong tool for a business rule.** RLS answers "may this tenant
   see this row". Opt-out answers "may the resolver use this row to infer a
   link". Encoding the latter as a visibility predicate would make an
   opted-out customer's identities *invisible to the tenant's own staff*,
   breaking the admin UI, the unlink flow, and erasure — all of which must still
   see the rows in order to act on them.

2. **A permissive policy can only widen access.** Adding a second policy to
   `customer_identities` would be OR-ed with the tenant policy (§11.2), so it
   could not restrict anything. Making it restrictive would apply to every
   query, including the ones that must not be filtered.

So it is enforced in the resolver's query, which is the single chokepoint
through which linking happens. The predicate is a join to `customers`:

```sql
-- IdentityResolver.find_by_hash(). The opt-out check is part of the lookup,
-- so an opted-out customer's identity is never returned as a link candidate —
-- including for links that already exist.
SELECT ci.customer_id, ci.channel, ci.verified
  FROM customer_identities ci
  JOIN customers c
    ON c.tenant_id = ci.tenant_id
   AND c.id = ci.customer_id
 WHERE ci.tenant_id = $1
   AND ci.identifier_hash = $2
   AND ci.channel = ANY($3::conversation_channel[])
   AND ci.unlinked_at IS NULL
   AND c.deleted_at IS NULL
   AND NOT c.linking_opt_out;     -- ← the opt-out, applied at read time
```

The join is cheap: `ix_customer_identities__tenant_hash` is an index-only probe
returning at most a handful of rows, and the `customers` lookup is a primary-key
hit per row. A CI test asserts that every code path reaching
`customer_identities` for resolution purposes goes through this repository
method — the same discipline that keeps `booking_check_conflicts` from being
reimplemented inline (§15.5).

#### Blob deletion on unlink and erasure — routed to `05`

`04` §11.2.1 correctly notes that erasure cascades through conversations,
transcripts, and recordings, and that **blob deletion via `StoragePort` cannot
be expressed in DDL**. Splitting ownership:

- **This document owns the row-level cascade.** `ON DELETE CASCADE` from
  `customers` to `customer_identities`; `ON DELETE SET NULL` to `bookings`,
  `waitlist_entries`, and `conversations`; and the erasure function in §17.4,
  which is extended below to include customer-linked conversations.
- **`05-PLATFORM-SECURITY-OPS.md` owns the blob reaper.** The schema's part is
  to make the objects *findable and markable*: `call_recordings.retain_until`
  and `deleted_from_storage_at` (§6.4) already exist for exactly this, and the
  erasure path sets `retain_until` in the past so the reaper collects them. The
  reaper itself — walking `StoragePort`, verifying deletion, handling a
  provider that fails a delete — is an operational concern, not a schema one.

Coordinator: **route the `StoragePort` reaper requirement to `05`.** Its
contract with this document is narrow and already satisfied: it consumes
`call_recordings` rows where `retain_until < now() AND deleted_from_storage_at
IS NULL`, and writes back `deleted_from_storage_at`. Nothing further is needed
here.

The tenant-scoped erasure entry point:

```sql
-- Erase a customer and everything that identifies them, in one transaction.
-- Conversations are NOT deleted here: they are business records with their own
-- retention, and ops_erase_tenant_conversation (§17.4) handles those
-- individually when a full conversation erasure is requested. What this does
-- is sever the identity link and destroy the identifiers themselves.
CREATE OR REPLACE FUNCTION ops_erase_customer(
    p_tenant_id UUID, p_customer_id UUID
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    n_conv INTEGER;
BEGIN
    -- 1. Mark any recordings of this customer's conversations for blob
    --    deletion. The 05-owned reaper does the actual StoragePort delete.
    UPDATE call_recordings cr
       SET retain_until = now() - INTERVAL '1 second'
     WHERE cr.tenant_id = p_tenant_id
       AND cr.conversation_id IN (
           SELECT c.id FROM conversations c
            WHERE c.tenant_id = p_tenant_id AND c.customer_id = p_customer_id
       );

    -- 2. Sever the link from conversations and redact the denormalized
    --    contact fields. The conversation row survives as an operational
    --    record; the person it was with does not remain identifiable.
    UPDATE conversations c
       SET customer_id        = NULL,
           contact_identifier = NULL,
           contact_name       = NULL,
           contact_hash       = NULL,
           redacted_at        = COALESCE(c.redacted_at, now())
     WHERE c.tenant_id = p_tenant_id AND c.customer_id = p_customer_id;
    GET DIAGNOSTICS n_conv = ROW_COUNT;

    -- 3. Redact the denormalized snapshots on business records, keeping the
    --    records themselves (financial/retention obligations).
    UPDATE bookings b
       SET customer_id         = NULL,
           customer_name       = NULL,
           customer_phone_e164 = NULL,
           customer_email      = NULL
     WHERE b.tenant_id = p_tenant_id AND b.customer_id = p_customer_id;

    UPDATE waitlist_entries w
       SET customer_id         = NULL,
           customer_name       = NULL,
           customer_phone_e164 = NULL,
           customer_email      = NULL
     WHERE w.tenant_id = p_tenant_id AND w.customer_id = p_customer_id;

    -- 4. Destroy the identifiers. Hard delete, not unlink: an unlinked row
    --    still holds identifier_enc, and erasure means the value is gone.
    --    Cascades from the customer delete below, but stated explicitly
    --    because relying on a cascade for an erasure guarantee is fragile.
    DELETE FROM customer_identities
     WHERE tenant_id = p_tenant_id AND customer_id = p_customer_id;

    -- 5. The customer itself.
    DELETE FROM customers
     WHERE tenant_id = p_tenant_id AND id = p_customer_id;

    RETURN n_conv;
END;
$$;

REVOKE ALL ON FUNCTION ops_erase_customer(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_erase_customer(UUID, UUID) TO receptionai_ops;
```

Note step 4 is a **hard delete**, unlike an unlink. Unlinking retires a row so
the audit trail survives; erasure must destroy `identifier_enc`, and a retired
row still contains it. The audit record that an erasure occurred lives in
`audit_log`, which is exempt from erasure for the reason given in §17.4.

#### Partitioning and retention

**Not partitioned.** Customer records are long-lived business data, not
execution state, and they fail every test that made the five partitioned tables
worth partitioning: volume is bounded by a tenant's actual customer count (tens
of thousands, not tens of millions), queries are by identity hash rather than by
time window, retention is dormancy-based rather than age-based, and — decisively
— both tables are FK *targets*, so a partition key would have to propagate into
every composite FK referencing them. Same reasoning that kept `bookings`
unpartitioned (§17.1).

**Retention is dormancy-based, not age-based**, and this is where I depart from
the handoff spec. `04` §11.2.1 proposes applying "the tenant's retention policy
to `customers` on the same schedule as transcripts." That is the right
instinct — an identity outliving every conversation it was resolved from serves
no purpose — but the wrong trigger: a customer who has called every month for
three years would be erased on a 12-month transcript schedule, breaking exactly
the recognition the feature exists to provide.

So: erase a customer when they have been **dormant** for the retention window,
measured from `last_seen_at`, not from `created_at`.

```
retention.customers_dormancy_days   default 730   floor 90   ceiling 2555
```

| Data | Default | Floor | Ceiling | Driver |
|---|---|---|---|---|
| `customers` / `customer_identities` | 24 months **dormant** | 90 days | 7 years | Recognition value decays with time since last contact; GDPR storage limitation |

The sweep, run daily per tenant, calls `ops_erase_customer` for each dormant
row so that dormancy erasure and requested erasure follow exactly the same path
— there is no second, subtly-different deletion routine to drift:

```sql
CREATE OR REPLACE FUNCTION ops_purge_dormant_customers(
    p_tenant_id UUID, p_dormancy_days INTEGER, p_limit INTEGER DEFAULT 500
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    rec    RECORD;
    n      INTEGER := 0;
    cutoff TIMESTAMPTZ := now() - make_interval(days => p_dormancy_days);
BEGIN
    FOR rec IN
        SELECT id FROM customers
         WHERE tenant_id = p_tenant_id
           AND deleted_at IS NULL
           -- A customer never seen since creation is dormant from creation.
           AND COALESCE(last_seen_at, created_at) < cutoff
         ORDER BY COALESCE(last_seen_at, created_at)
         LIMIT p_limit
    LOOP
        PERFORM ops_erase_customer(p_tenant_id, rec.id);
        n := n + 1;
    END LOOP;

    RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION ops_purge_dormant_customers(UUID, INTEGER, INTEGER)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_purge_dormant_customers(UUID, INTEGER, INTEGER)
    TO receptionai_ops;
```

`last_seen_at` is maintained by the resolver on every successful resolution —
one narrow `UPDATE` per inbound contact, which is why `customers` carries no
`fillfactor` tuning concern: the update touches no indexed column except
`last_seen_at`, so it is a HOT update in practice.

**API consequence, for `06`:** these tables make `/api/v1/customers` exposable.
Per `04` §11.2.1 it should stay **read-and-unlink only** — customers are created
by `IdentityResolver` during a conversation, never by direct `POST`, because a
hand-created customer bypasses the verified-identifier rule that
`ck_customer_identities__deterministic_requires_verified` enforces at the
storage layer.

---

## 4. Vertical Plugins

Contract §4: verticals are declarative manifests, never tenant Python. The
persistence job is therefore (a) hold the manifest as validated JSONB, (b)
version it immutably, and (c) record which tenant installed which version with
what overrides.

### 4.1 `vertical_definitions`

Platform catalogue — the identity of a vertical, independent of its versions.

```sql
CREATE TABLE vertical_definitions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    code                TEXT NOT NULL,      -- 'medical','restaurant','hotel','salon','legal'
    name                TEXT NOT NULL,
    description         TEXT,
    category            TEXT,

    -- Who authored it. First-party verticals ship with the platform; a
    -- partner-authored vertical is still data, still validated, still no code.
    publisher           TEXT NOT NULL DEFAULT 'receptionai',
    publisher_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,

    -- Which manifest JSON Schema major version this vertical targets.
    manifest_schema_major SMALLINT NOT NULL DEFAULT 1,

    icon_url            TEXT,
    is_public           BOOLEAN NOT NULL DEFAULT TRUE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_vertical_definitions__code
        CHECK (code ~ '^[a-z][a-z0-9_]{1,47}$'),
    CONSTRAINT ck_vertical_definitions__private_has_owner
        CHECK (is_public OR publisher_tenant_id IS NOT NULL)
);

CREATE UNIQUE INDEX uq_vertical_definitions__code ON vertical_definitions (code);
CREATE INDEX ix_vertical_definitions__public
    ON vertical_definitions (is_public, category) WHERE is_active;
CREATE INDEX ix_vertical_definitions__publisher_tenant
    ON vertical_definitions (publisher_tenant_id) WHERE publisher_tenant_id IS NOT NULL;

CREATE TRIGGER trg_vertical_definitions_updated_at
    BEFORE UPDATE ON vertical_definitions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 4.2 `vertical_versions`

**Immutable once published.** A published manifest is a contract with every
tenant running it; mutating it in place would retroactively change behaviour for
live bookings. Enforced by a trigger, not by convention.

```sql
CREATE TABLE vertical_versions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    vertical_id         UUID NOT NULL REFERENCES vertical_definitions(id) ON DELETE CASCADE,

    version             TEXT NOT NULL,      -- semver: '2.1.0'
    version_major       SMALLINT NOT NULL,
    version_minor       SMALLINT NOT NULL,
    version_patch       SMALLINT NOT NULL,

    -- The whole VerticalManifest. Validated against the JSON Schema named by
    -- manifest_schema_version BEFORE insert, in the application layer, and
    -- shape-checked again here.
    manifest            JSONB NOT NULL,
    manifest_schema_version TEXT NOT NULL,

    -- Content hash of the canonicalized manifest. Lets us detect an identical
    -- republish and gives every install a verifiable fingerprint.
    manifest_sha256     BYTEA NOT NULL,

    -- Denormalized from the manifest for indexable queries. Populated by
    -- trigger so they cannot drift from the document.
    declared_tools      TEXT[] NOT NULL DEFAULT '{}',
    declared_workflows  TEXT[] NOT NULL DEFAULT '{}',
    declared_entities   TEXT[] NOT NULL DEFAULT '{}',
    required_features   TEXT[] NOT NULL DEFAULT '{}',

    changelog           TEXT,
    is_prerelease       BOOLEAN NOT NULL DEFAULT FALSE,
    published_at        TIMESTAMPTZ,
    deprecated_at       TIMESTAMPTZ,
    -- Tenants on a version past this date are force-migrated.
    end_of_life_at      TIMESTAMPTZ,

    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_vertical_versions__semver
        CHECK (version ~ '^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$'),
    CONSTRAINT ck_vertical_versions__sha_len
        CHECK (octet_length(manifest_sha256) = 32),
    -- Structural gate: the four mandatory top-level manifest keys must exist.
    -- Deep validation is the JSON Schema validator's job; this stops garbage
    -- reaching the table even if a migration or backfill bypasses the app.
    CONSTRAINT ck_vertical_versions__manifest_shape CHECK (
        jsonb_typeof(manifest) = 'object'
        AND manifest ? 'vocabulary'
        AND manifest ? 'entities'
        AND manifest ? 'agents'
        AND manifest ? 'policies'
    ),
    CONSTRAINT ck_vertical_versions__eol_after_deprecation
        CHECK (end_of_life_at IS NULL OR deprecated_at IS NULL
               OR end_of_life_at >= deprecated_at)
);

CREATE UNIQUE INDEX uq_vertical_versions__vertical_version
    ON vertical_versions (vertical_id, version);

CREATE UNIQUE INDEX uq_vertical_versions__vertical_sha
    ON vertical_versions (vertical_id, manifest_sha256);

-- "Latest stable version of vertical X" — the install/upgrade path.
CREATE INDEX ix_vertical_versions__latest
    ON vertical_versions (vertical_id, version_major DESC, version_minor DESC,
                          version_patch DESC)
    WHERE published_at IS NOT NULL AND NOT is_prerelease;

-- "Which verticals grant tool T" — used by the security review of tool grants.
CREATE INDEX ix_vertical_versions__tools
    ON vertical_versions USING gin (declared_tools);

-- Ad-hoc manifest introspection from the admin console.
CREATE INDEX ix_vertical_versions__manifest
    ON vertical_versions USING gin (manifest jsonb_path_ops);
```

**Immutability trigger.** Once `published_at` is set, only the lifecycle columns
may change:

```sql
CREATE OR REPLACE FUNCTION guard_vertical_version_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.published_at IS NOT NULL THEN
        IF NEW.manifest IS DISTINCT FROM OLD.manifest
           OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.vertical_id IS DISTINCT FROM OLD.vertical_id THEN
            RAISE EXCEPTION
                'vertical_versions %: published manifests are immutable', OLD.id
                USING ERRCODE = 'raise_exception';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_vertical_versions_immutable
    BEFORE UPDATE ON vertical_versions
    FOR EACH ROW EXECUTE FUNCTION guard_vertical_version_immutable();
```

**Denormalization trigger**, so the arrays cannot drift from the document:

```sql
CREATE OR REPLACE FUNCTION sync_vertical_version_arrays()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.declared_tools := COALESCE(
        ARRAY(SELECT jsonb_array_elements_text(NEW.manifest -> 'tools' -> 'names')), '{}');
    NEW.declared_workflows := COALESCE(
        ARRAY(SELECT jsonb_array_elements_text(NEW.manifest -> 'workflows' -> 'names')), '{}');
    NEW.declared_entities := COALESCE(
        ARRAY(SELECT jsonb_object_keys(NEW.manifest -> 'entities')), '{}');
    NEW.required_features := COALESCE(
        ARRAY(SELECT jsonb_array_elements_text(NEW.manifest -> 'requires' -> 'features')), '{}');

    NEW.version_major := split_part(split_part(NEW.version, '-', 1), '.', 1)::smallint;
    NEW.version_minor := split_part(split_part(NEW.version, '-', 1), '.', 2)::smallint;
    NEW.version_patch := split_part(split_part(NEW.version, '-', 1), '.', 3)::smallint;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_vertical_versions_sync_arrays
    BEFORE INSERT OR UPDATE OF manifest, version ON vertical_versions
    FOR EACH ROW EXECUTE FUNCTION sync_vertical_version_arrays();
```

### 4.3 `tenant_vertical_installs`

```sql
CREATE TABLE tenant_vertical_installs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    vertical_id         UUID NOT NULL REFERENCES vertical_definitions(id) ON DELETE RESTRICT,
    vertical_version_id UUID NOT NULL REFERENCES vertical_versions(id) ON DELETE RESTRICT,

    -- NULL = applies to every location. A multi-brand group can run 'restaurant'
    -- at one location and 'hotel' at another.
    location_id         UUID,

    -- Tenant-authored overlay, merged over the manifest at load time.
    -- Restricted by the manifest's own `overridable` allow-list — a tenant
    -- cannot override tool grants or policies flagged as locked.
    overrides           JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Resolved manifest = manifest ⊕ overrides, cached here so the runtime
    -- does not re-merge on every request. Invalidated on either input changing.
    resolved_manifest       JSONB,
    resolved_at             TIMESTAMPTZ,
    resolved_sha256         BYTEA,

    -- 'auto_patch' | 'auto_minor' | 'manual'
    upgrade_policy      TEXT NOT NULL DEFAULT 'auto_patch',

    is_enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    -- The vertical whose vocabulary the agent uses when nothing else narrows it.
    is_primary          BOOLEAN NOT NULL DEFAULT FALSE,

    installed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    installed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    uninstalled_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_tenant_vertical_installs__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_tenant_vertical_installs__upgrade_policy
        CHECK (upgrade_policy IN ('auto_patch','auto_minor','manual')),
    CONSTRAINT ck_tenant_vertical_installs__overrides_object
        CHECK (jsonb_typeof(overrides) = 'object'),
    CONSTRAINT ck_tenant_vertical_installs__resolved_paired
        CHECK ((resolved_manifest IS NULL) = (resolved_at IS NULL))
);

-- One install of a given vertical per scope.
CREATE UNIQUE INDEX uq_tenant_vertical_installs__tenant_vertical_global
    ON tenant_vertical_installs (tenant_id, vertical_id)
    WHERE location_id IS NULL AND uninstalled_at IS NULL;

CREATE UNIQUE INDEX uq_tenant_vertical_installs__tenant_vertical_loc
    ON tenant_vertical_installs (tenant_id, vertical_id, location_id)
    WHERE location_id IS NOT NULL AND uninstalled_at IS NULL;

-- Exactly one primary vertical per (tenant, location scope).
CREATE UNIQUE INDEX uq_tenant_vertical_installs__primary_global
    ON tenant_vertical_installs (tenant_id)
    WHERE is_primary AND location_id IS NULL AND uninstalled_at IS NULL;

CREATE UNIQUE INDEX uq_tenant_vertical_installs__primary_loc
    ON tenant_vertical_installs (tenant_id, location_id)
    WHERE is_primary AND location_id IS NOT NULL AND uninstalled_at IS NULL;

-- Hot path: load every enabled install for a tenant at session start.
CREATE INDEX ix_tenant_vertical_installs__tenant_enabled
    ON tenant_vertical_installs (tenant_id, location_id)
    INCLUDE (vertical_version_id, is_primary)
    WHERE is_enabled AND uninstalled_at IS NULL;

-- Upgrade sweeper (privileged): "who is on an EOL version".
CREATE INDEX ix_tenant_vertical_installs__version
    ON tenant_vertical_installs (vertical_version_id) WHERE uninstalled_at IS NULL;

CREATE TRIGGER trg_tenant_vertical_installs_updated_at
    BEFORE UPDATE ON tenant_vertical_installs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

---

## 5. The Generic Booking Engine

This is the load-bearing part of the schema. §14 proves it works; this section
defines it.

### 5.1 The abstraction, in one paragraph

Every bookable thing in every vertical is the same shape: **a customer consumes
some capacity of one or more resources for a contiguous interval of time.** A
doctor's appointment consumes 1 unit of one physician for 30 minutes. A
restaurant table consumes 1 unit of one table (which has a seat capacity) for a
90-minute dining window. A hotel room consumes 1 unit of one room for three
nights. Equipment rental consumes 2 units of a fungible pool of 10 scooters for
an afternoon. The differences between these are entirely expressible as
(a) resource *kind*, (b) capacity semantics, (c) granularity of the time axis,
(d) which requirements a service places on resources — all data, not columns.

### 5.2 `resource_types`

The *class* of resource. Populated from a vertical manifest's `entities` block
on install, but tenant-editable afterwards.

```sql
CREATE TABLE resource_types (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    code                TEXT NOT NULL,      -- 'physician','dining_table','guest_room','scooter'
    name                TEXT NOT NULL,      -- display label from the vocabulary block
    kind                resource_kind NOT NULL,

    -- Which vertical install introduced this type. Lets an uninstall know what
    -- it may safely remove, and lets the agent map vocabulary correctly.
    vertical_install_id UUID,

    -- ── The three switches that make one schema cover every vertical ────────
    --
    -- 1. Is capacity per-resource shared among concurrent bookings?
    --    FALSE (exclusive): a physician or a hotel room serves one booking at a
    --      time. Overlap is forbidden outright.
    --    TRUE  (shared):    a yoga studio holds 20; a shuttle seats 12. Overlap
    --      is allowed until summed quantity exceeds capacity.
    is_capacity_shared  BOOLEAN NOT NULL DEFAULT FALSE,

    -- 2. What is the natural unit of the time axis?
    --    'minute' → appointments, tables. 'night' → hotels. 'day' → equipment
    --    rental. This drives UI, pricing, and how the engine snaps boundaries.
    --    It does NOT change storage: everything is a tstzrange either way.
    time_unit           TEXT NOT NULL DEFAULT 'minute',

    -- 3. Do bookings on this type snap to a slot grid, or float freely?
    --    A clinic has 15-minute slots. A restaurant floats (7:13 pm is fine).
    --    NULL = free-floating.
    slot_granularity_minutes SMALLINT,

    -- Attribute schema for instances of this type, as JSON Schema. The admin UI
    -- renders a form from it; the engine validates resources.attributes against
    -- it. THIS is where 'seats', 'bed_type', 'room_floor', 'specialty' live —
    -- not as columns.
    attribute_schema    JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Which attributes may be used as booking filters (indexed via GIN).
    filterable_attributes TEXT[] NOT NULL DEFAULT '{}',

    sort_order          SMALLINT NOT NULL DEFAULT 0,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_resource_types__installs
        FOREIGN KEY (tenant_id, vertical_install_id)
        REFERENCES tenant_vertical_installs (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT ck_resource_types__code CHECK (code ~ '^[a-z][a-z0-9_]{1,47}$'),
    CONSTRAINT ck_resource_types__time_unit
        CHECK (time_unit IN ('minute','hour','day','night')),
    CONSTRAINT ck_resource_types__granularity
        CHECK (slot_granularity_minutes IS NULL
               OR slot_granularity_minutes BETWEEN 1 AND 1440),
    CONSTRAINT ck_resource_types__attr_schema_object
        CHECK (jsonb_typeof(attribute_schema) = 'object')
);

ALTER TABLE tenant_vertical_installs
    ADD CONSTRAINT uq_tenant_vertical_installs__tenant_id_id UNIQUE (tenant_id, id);

CREATE UNIQUE INDEX uq_resource_types__tenant_code
    ON resource_types (tenant_id, code);

CREATE INDEX ix_resource_types__tenant_active
    ON resource_types (tenant_id, kind) WHERE is_active;

CREATE TRIGGER trg_resource_types_updated_at
    BEFORE UPDATE ON resource_types
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 5.3 `resources`

The bookable instances. Dr. Chen, Table 12, Room 401, Scooter #7.

```sql
CREATE TABLE resources (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    location_id         UUID NOT NULL,
    resource_type_id    UUID NOT NULL,

    code                TEXT NOT NULL,      -- 'dr-chen','T12','401','scooter-07'
    name                TEXT NOT NULL,
    description         TEXT,

    -- ── Capacity ───────────────────────────────────────────────────────────
    -- How many units of this resource exist / can be consumed at once.
    --   Physician:      1  (exclusive; capacity is trivially 1)
    --   Table for four: 1  (exclusive as a table; SEATS are an attribute, and
    --                       seat-count matching is a service requirement, not
    --                       a capacity subdivision — see §14.2)
    --   Yoga studio:   20  (shared)
    --   Scooter pool:  10  (shared, fungible)
    capacity            INTEGER NOT NULL DEFAULT 1,

    -- Overbooking allowance, as a count of units beyond capacity. Airlines and
    -- restaurants do this deliberately. Default 0 = never overbook.
    overbook_allowance  INTEGER NOT NULL DEFAULT 0,

    -- ── Type-specific attributes, validated against
    --    resource_types.attribute_schema ──────────────────────────────────────
    --   physician:    {"specialty":"cardiology","npi":"...","languages":["en","es"]}
    --   dining_table: {"seats":4,"section":"patio","combinable_with":["T13"]}
    --   guest_room:   {"bed_type":"king","floor":4,"view":"ocean","smoking":false}
    --   scooter:      {"model":"X1","range_km":45}
    attributes          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Free-form operational tags used for filtering and grouping.
    tags                TEXT[] NOT NULL DEFAULT '{}',

    -- Timezone override. Almost always NULL (inherits location). Non-null for
    -- a remote/telehealth resource operating on its own clock.
    timezone            TEXT,

    -- If this resource is a staff member with a portal login.
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,

    -- External system identity, for calendar/PMS sync.
    external_ref        TEXT,
    external_system     TEXT,

    -- Bookability. is_active=false hides it entirely; is_bookable=false keeps
    -- it visible (e.g. on a floor plan) but not reservable.
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    is_bookable         BOOLEAN NOT NULL DEFAULT TRUE,

    sort_order          SMALLINT NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_resources__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_resources__resource_types
        FOREIGN KEY (tenant_id, resource_type_id)
        REFERENCES resource_types (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_resources__capacity_positive CHECK (capacity > 0),
    CONSTRAINT ck_resources__overbook_nonneg CHECK (overbook_allowance >= 0),
    CONSTRAINT ck_resources__attributes_object CHECK (jsonb_typeof(attributes) = 'object'),
    CONSTRAINT ck_resources__timezone_valid
        CHECK (timezone IS NULL OR now() AT TIME ZONE timezone IS NOT NULL)
);

ALTER TABLE resource_types
    ADD CONSTRAINT uq_resource_types__tenant_id_id UNIQUE (tenant_id, id);
ALTER TABLE resources
    ADD CONSTRAINT uq_resources__tenant_id_id UNIQUE (tenant_id, id);

CREATE UNIQUE INDEX uq_resources__tenant_location_code
    ON resources (tenant_id, location_id, code) WHERE deleted_at IS NULL;

-- Primary availability query: "bookable resources of type T at location L".
CREATE INDEX ix_resources__tenant_type_location
    ON resources (tenant_id, resource_type_id, location_id)
    INCLUDE (capacity, overbook_allowance)
    WHERE is_active AND is_bookable AND deleted_at IS NULL;

-- Attribute filtering: "rooms with a king bed and an ocean view",
-- "tables seating >= 4". jsonb_path_ops is ~3x smaller and faster than the
-- default opclass for pure containment (@>), which is all we need.
CREATE INDEX ix_resources__attributes
    ON resources USING gin (attributes jsonb_path_ops)
    WHERE deleted_at IS NULL;

CREATE INDEX ix_resources__tags
    ON resources USING gin (tags) WHERE deleted_at IS NULL;

CREATE INDEX ix_resources__external
    ON resources (tenant_id, external_system, external_ref)
    WHERE external_ref IS NOT NULL AND deleted_at IS NULL;

CREATE INDEX ix_resources__user
    ON resources (tenant_id, user_id) WHERE user_id IS NOT NULL AND deleted_at IS NULL;

CREATE TRIGGER trg_resources_updated_at
    BEFORE UPDATE ON resources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Attribute validation.** `resources.attributes` is validated against
`resource_types.attribute_schema` in the application (Pydantic model generated
from the JSON Schema) before write. We deliberately do **not** attempt JSON
Schema validation in a Postgres trigger: there is no first-class validator in
core Postgres, a PL/pgSQL reimplementation would be a second, divergent source
of truth, and the failure mode (a bad row) is far less costly than the failure
mode of two validators disagreeing. The CHECK constraints above catch structural
garbage; semantic validation is the app's job at a single chokepoint (§15.3).

### 5.4 `services`

What a customer books: "30-minute consultation", "dinner reservation",
"deluxe king, 1 night", "half-day scooter rental".

```sql
CREATE TABLE services (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- NULL = offered at every location.
    location_id         UUID,

    code                TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT,
    category            TEXT,

    vertical_install_id UUID,

    -- ── Duration model ─────────────────────────────────────────────────────
    -- duration_minutes is the canonical service length. For night-based
    -- services it is NULL and duration_units + resource_type.time_unit govern.
    duration_minutes    INTEGER,
    -- Number of time_units consumed. Hotels: nights. Rentals: days.
    -- NULL = variable, determined at booking time (a 3-night vs 5-night stay).
    duration_units      INTEGER,
    is_duration_variable BOOLEAN NOT NULL DEFAULT FALSE,
    min_duration_units  INTEGER,
    max_duration_units  INTEGER,

    -- Padding around the booking that blocks the resource but is not billed
    -- and is not shown to the customer. Room turnover, table reset, exam-room
    -- cleaning. THIS is why bookings carry both a service range and a
    -- blocking range (§5.9).
    buffer_before_minutes INTEGER NOT NULL DEFAULT 0,
    buffer_after_minutes  INTEGER NOT NULL DEFAULT 0,

    -- ── Party / quantity model ─────────────────────────────────────────────
    -- How many "people" or "units" a single booking of this service may carry.
    -- Restaurant: party size 1..12. Clinic: 1. Rental: 1..10 scooters.
    min_party_size      INTEGER NOT NULL DEFAULT 1,
    max_party_size      INTEGER NOT NULL DEFAULT 1,

    -- ── Booking policy ─────────────────────────────────────────────────────
    lead_time_minutes       INTEGER NOT NULL DEFAULT 0,      -- earliest bookable
    booking_horizon_days    INTEGER NOT NULL DEFAULT 90,     -- furthest bookable
    cancellation_window_minutes INTEGER NOT NULL DEFAULT 1440,
    requires_approval       BOOLEAN NOT NULL DEFAULT FALSE,
    allow_waitlist          BOOLEAN NOT NULL DEFAULT TRUE,
    allow_double_booking    BOOLEAN NOT NULL DEFAULT FALSE,  -- see §13.4

    -- ── Pricing ────────────────────────────────────────────────────────────
    price               NUMERIC(12,4),
    currency            CHAR(3),
    price_model         TEXT NOT NULL DEFAULT 'flat',
    deposit_amount      NUMERIC(12,4),
    -- Per-unit pricing for night/day services; total = unit_price * units.
    unit_price          NUMERIC(12,4),

    -- Fields the agent must collect before confirming. Rendered from the
    -- manifest's `validation` block; e.g.
    -- [{"key":"reason_for_visit","type":"string","required":true,
    --   "pii_class":"phi","prompt":"What brings you in today?"}]
    intake_schema       JSONB NOT NULL DEFAULT '[]'::jsonb,

    color               TEXT,               -- calendar rendering
    sort_order          SMALLINT NOT NULL DEFAULT 0,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    is_bookable_online  BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_services__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_services__installs
        FOREIGN KEY (tenant_id, vertical_install_id)
        REFERENCES tenant_vertical_installs (tenant_id, id) ON DELETE SET NULL,

    CONSTRAINT ck_services__code CHECK (code ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    CONSTRAINT ck_services__duration_present
        CHECK (duration_minutes IS NOT NULL OR duration_units IS NOT NULL
               OR is_duration_variable),
    CONSTRAINT ck_services__duration_positive
        CHECK ((duration_minutes IS NULL OR duration_minutes > 0)
               AND (duration_units IS NULL OR duration_units > 0)),
    CONSTRAINT ck_services__variable_bounds CHECK (
        NOT is_duration_variable
        OR (min_duration_units IS NOT NULL AND max_duration_units IS NOT NULL
            AND min_duration_units >= 1 AND max_duration_units >= min_duration_units)
    ),
    CONSTRAINT ck_services__buffers_nonneg
        CHECK (buffer_before_minutes >= 0 AND buffer_after_minutes >= 0),
    CONSTRAINT ck_services__party_bounds
        CHECK (min_party_size >= 1 AND max_party_size >= min_party_size),
    CONSTRAINT ck_services__horizon CHECK (booking_horizon_days BETWEEN 0 AND 3650),
    CONSTRAINT ck_services__price_model
        CHECK (price_model IN ('flat','per_unit','per_person','per_unit_per_person','free','quote')),
    CONSTRAINT ck_services__price_currency_paired
        CHECK ((price IS NULL AND unit_price IS NULL) OR currency IS NOT NULL),
    CONSTRAINT ck_services__price_nonneg
        CHECK ((price IS NULL OR price >= 0) AND (unit_price IS NULL OR unit_price >= 0)
               AND (deposit_amount IS NULL OR deposit_amount >= 0)),
    CONSTRAINT ck_services__intake_array CHECK (jsonb_typeof(intake_schema) = 'array')
);

ALTER TABLE services
    ADD CONSTRAINT uq_services__tenant_id_id UNIQUE (tenant_id, id);

CREATE UNIQUE INDEX uq_services__tenant_code_global
    ON services (tenant_id, code) WHERE location_id IS NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_services__tenant_code_loc
    ON services (tenant_id, location_id, code)
    WHERE location_id IS NOT NULL AND deleted_at IS NULL;

-- Service catalogue for a location — rendered on every booking flow entry.
CREATE INDEX ix_services__tenant_location_active
    ON services (tenant_id, location_id, sort_order)
    WHERE is_active AND deleted_at IS NULL;

CREATE INDEX ix_services__tenant_category
    ON services (tenant_id, category) WHERE is_active AND deleted_at IS NULL;

CREATE TRIGGER trg_services_updated_at
    BEFORE UPDATE ON services
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 5.5 `service_resource_requirements`

**The join that makes the model generic.** A service declares *what kinds of
resources it needs, how many, and with what attributes* — it never names a
specific resource, and it never hard-codes a vertical.

```sql
CREATE TABLE service_resource_requirements (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_id          UUID NOT NULL,

    -- What kind of resource is needed.
    resource_type_id    UUID NOT NULL,

    -- Ordinal for multi-resource services: a surgery needs a surgeon (role
    -- 'primary'), an anaesthetist ('assist'), and an operating theatre ('space').
    role                TEXT NOT NULL DEFAULT 'primary',

    -- How many DISTINCT resources of this type are needed.
    quantity            INTEGER NOT NULL DEFAULT 1,

    -- How many CAPACITY UNITS of each selected resource are consumed. For an
    -- exclusive resource this is 1. For a shared 20-person studio, a class
    -- booking of 5 people consumes 5. For a scooter pool, party_size scooters.
    -- NULL = "derive from booking.party_size" (the restaurant/rental case).
    units_per_resource  INTEGER,

    -- Attribute predicate the resource must satisfy, evaluated as a JSONB
    -- containment (@>) plus optional numeric comparisons. Structure:
    --   {"match": {"specialty": "cardiology"},
    --    "gte":   {"seats": "$party_size"},
    --    "in":    {"bed_type": ["king","queen"]}}
    -- '$party_size' and '$duration_units' are the two supported bind variables,
    -- substituted from the booking request.
    attribute_predicate JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- If false, the engine may confirm a booking without this requirement
    -- satisfied (a "nice to have" room preference).
    is_required         BOOLEAN NOT NULL DEFAULT TRUE,

    -- Selection strategy when several resources qualify.
    -- 'least_utilised' spreads load across staff; 'first_available' fills a
    -- floor front-to-back; 'best_fit' minimises wasted capacity (seat a party
    -- of 2 at a 2-top, not a 6-top); 'round_robin' rotates fairly.
    allocation_strategy TEXT NOT NULL DEFAULT 'best_fit',

    -- Does this requirement consume the resource for the booking's blocking
    -- range (true) or only its service range (false)? An operating theatre is
    -- blocked through cleanup; a consulting surgeon is not.
    respects_buffers    BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_srr__services
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_srr__resource_types
        FOREIGN KEY (tenant_id, resource_type_id)
        REFERENCES resource_types (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_srr__quantity CHECK (quantity BETWEEN 1 AND 100),
    CONSTRAINT ck_srr__units CHECK (units_per_resource IS NULL OR units_per_resource >= 1),
    CONSTRAINT ck_srr__role CHECK (role ~ '^[a-z][a-z0-9_]{0,31}$'),
    CONSTRAINT ck_srr__strategy
        CHECK (allocation_strategy IN ('first_available','least_utilised',
                                       'best_fit','round_robin','explicit')),
    CONSTRAINT ck_srr__predicate_object
        CHECK (jsonb_typeof(attribute_predicate) = 'object')
);

CREATE UNIQUE INDEX uq_srr__service_type_role
    ON service_resource_requirements (tenant_id, service_id, resource_type_id, role);

-- Loaded in full whenever a service's availability is computed.
CREATE INDEX ix_srr__tenant_service
    ON service_resource_requirements (tenant_id, service_id);

-- Reverse: "which services would break if I delete this resource type".
CREATE INDEX ix_srr__tenant_resource_type
    ON service_resource_requirements (tenant_id, resource_type_id);

CREATE TRIGGER trg_srr_updated_at
    BEFORE UPDATE ON service_resource_requirements
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 5.6 `availability_rules`

Recurring open/closed windows. Expressed in **local wall-clock time** plus a
weekday mask, because "we open at 9 am" must survive a DST transition. The
engine materialises these into UTC `tstzrange` candidate windows at query time
using the location timezone.

```sql
CREATE TABLE availability_rules (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Scope: exactly one of these three levels is targeted.
    --   resource_id set   → this resource's own schedule
    --   resource_type_id  → default schedule for all resources of a type
    --   location_id only  → the location's opening hours
    location_id         UUID NOT NULL,
    resource_type_id    UUID,
    resource_id         UUID,

    -- A rule may also be service-specific: "new patient slots only on Tuesdays".
    service_id          UUID,

    effect              availability_effect NOT NULL DEFAULT 'open',

    -- Weekday mask, ISO-8601 numbering (1=Monday … 7=Sunday).
    -- Array rather than seven booleans: indexable with GIN, trivially
    -- expressible in a manifest, and reads naturally.
    weekdays            SMALLINT[] NOT NULL,

    -- Local wall-clock window. end_time < start_time means it crosses midnight
    -- (a bar open 18:00–02:00), which the materialiser handles explicitly.
    start_time          TIME NOT NULL,
    end_time            TIME NOT NULL,
    crosses_midnight    BOOLEAN GENERATED ALWAYS AS (end_time <= start_time) STORED,

    -- Calendar window over which the rule applies. NULL upper = indefinite.
    -- DATERANGE not TSTZRANGE: these are wall-clock calendar dates, not instants.
    effective_period    DATERANGE NOT NULL DEFAULT daterange(CURRENT_DATE, NULL, '[)'),

    -- Higher priority wins when rules conflict. A 'closed' rule at equal
    -- priority always beats an 'open' rule (see the materialiser contract).
    priority            SMALLINT NOT NULL DEFAULT 0,

    -- Capacity override for this window: a restaurant that seats 40 normally
    -- but 25 on the patio in winter.
    capacity_override   INTEGER,

    label               TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_availability_rules__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_availability_rules__resources
        FOREIGN KEY (tenant_id, resource_id)
        REFERENCES resources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_availability_rules__resource_types
        FOREIGN KEY (tenant_id, resource_type_id)
        REFERENCES resource_types (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_availability_rules__services
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id) ON DELETE CASCADE,

    CONSTRAINT ck_availability_rules__weekdays_valid
        CHECK (weekdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]
               AND array_length(weekdays, 1) BETWEEN 1 AND 7),
    -- Scope is a strict hierarchy: a resource rule must not also name a type.
    CONSTRAINT ck_availability_rules__scope_exclusive
        CHECK (NOT (resource_id IS NOT NULL AND resource_type_id IS NOT NULL)),
    CONSTRAINT ck_availability_rules__capacity
        CHECK (capacity_override IS NULL OR capacity_override >= 0),
    CONSTRAINT ck_availability_rules__priority CHECK (priority BETWEEN 0 AND 1000)
);

-- The materialiser's query: "all active rules for this location, in this date
-- window, ordered by priority". Written to be an index-only scan.
CREATE INDEX ix_availability_rules__tenant_location_period
    ON availability_rules USING gist (
        tenant_id,
        location_id,
        effective_period
    ) WHERE is_active;

CREATE INDEX ix_availability_rules__tenant_resource
    ON availability_rules (tenant_id, resource_id, priority DESC)
    WHERE resource_id IS NOT NULL AND is_active;

CREATE INDEX ix_availability_rules__tenant_type
    ON availability_rules (tenant_id, resource_type_id, priority DESC)
    WHERE resource_type_id IS NOT NULL AND is_active;

CREATE INDEX ix_availability_rules__tenant_service
    ON availability_rules (tenant_id, service_id)
    WHERE service_id IS NOT NULL AND is_active;

CREATE INDEX ix_availability_rules__weekdays
    ON availability_rules USING gin (weekdays) WHERE is_active;

CREATE TRIGGER trg_availability_rules_updated_at
    BEFORE UPDATE ON availability_rules
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Why not store materialised slots.** The obvious alternative — a `slots` table
with one row per bookable interval per resource — is how many booking systems
start and why they later rewrite. For 200 resources × 90 days × 15-minute slots
that is 17 million rows per tenant, all of which must be regenerated whenever a
rule, a holiday, or an exception changes, and all of which are wrong the moment
the horizon rolls forward. We compute availability on demand from rules,
exceptions, holidays and existing bookings, and cache the *result* in Redis
keyed by `(tenant, resource, date)` with invalidation on booking write. The
database stores intent; the cache stores derived facts.

### 5.7 `availability_exceptions`

One-off overrides: a doctor's vacation, a private buyout, a maintenance window,
an extra Saturday clinic.

```sql
CREATE TABLE availability_exceptions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    location_id         UUID NOT NULL,
    resource_type_id    UUID,
    resource_id         UUID,
    service_id          UUID,

    effect              availability_effect NOT NULL DEFAULT 'closed',

    -- Absolute UTC interval. Unlike rules, an exception is a concrete instant
    -- range: "Dr Chen is away from 2026-07-01T00:00Z to 2026-07-15T00:00Z".
    -- Half-open [) so back-to-back exceptions do not collide.
    period              TSTZRANGE NOT NULL,

    capacity_override   INTEGER,

    reason              TEXT,
    -- Where it came from: manual entry, an external calendar sync, or a
    -- holiday expansion. Sync-sourced rows are replaced wholesale per sync.
    source              TEXT NOT NULL DEFAULT 'manual',
    external_ref        TEXT,
    connector_id        UUID,

    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_availability_exceptions__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_availability_exceptions__resources
        FOREIGN KEY (tenant_id, resource_id)
        REFERENCES resources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_availability_exceptions__resource_types
        FOREIGN KEY (tenant_id, resource_type_id)
        REFERENCES resource_types (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_availability_exceptions__services
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id) ON DELETE CASCADE,

    CONSTRAINT ck_availability_exceptions__period_bounded
        CHECK (lower(period) IS NOT NULL AND upper(period) IS NOT NULL
               AND NOT isempty(period)),
    CONSTRAINT ck_availability_exceptions__scope_exclusive
        CHECK (NOT (resource_id IS NOT NULL AND resource_type_id IS NOT NULL)),
    CONSTRAINT ck_availability_exceptions__source
        CHECK (source IN ('manual','calendar_sync','holiday','system','pms_sync'))
);

-- The availability query overlaps a date window against exceptions. GiST on
-- the range, with tenant_id and location_id leading so RLS and scope filter
-- inside the same index. btree_gist makes the mixed opclass legal.
CREATE INDEX ix_availability_exceptions__tenant_location_period
    ON availability_exceptions USING gist (tenant_id, location_id, period);

CREATE INDEX ix_availability_exceptions__tenant_resource_period
    ON availability_exceptions USING gist (tenant_id, resource_id, period)
    WHERE resource_id IS NOT NULL;

-- Idempotent re-sync from an external calendar: upsert on external identity.
CREATE UNIQUE INDEX uq_availability_exceptions__external
    ON availability_exceptions (tenant_id, connector_id, external_ref)
    WHERE external_ref IS NOT NULL;

CREATE TRIGGER trg_availability_exceptions_updated_at
    BEFORE UPDATE ON availability_exceptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 5.8 `holidays`

A named calendar of closed days, shared across locations, expanded into
exceptions by a scheduled job. Kept separate from `availability_exceptions`
because holidays are *declarative and recurring* ("US federal holidays,
observed") while exceptions are concrete instants.

```sql
CREATE TABLE holidays (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- NULL = applies to every location in the tenant.
    location_id         UUID,

    name                TEXT NOT NULL,
    -- The calendar date in LOCAL terms. Expansion to UTC uses the location tz.
    holiday_date        DATE NOT NULL,

    -- 'closed' shuts the location; 'open' overrides a default closure
    -- (a clinic that opens on a public holiday).
    effect              availability_effect NOT NULL DEFAULT 'closed',

    -- Partial-day holiday: NULL/NULL means the whole day.
    start_time          TIME,
    end_time            TIME,

    -- Recurrence: 'none' for a one-off, 'annual' to repeat on the same
    -- month/day every year without inserting a row per year.
    recurrence          TEXT NOT NULL DEFAULT 'none',

    -- Provenance for imported national calendars.
    calendar_code       TEXT,               -- 'US','GB','DE-BY'
    source              TEXT NOT NULL DEFAULT 'manual',

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_holidays__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_holidays__recurrence CHECK (recurrence IN ('none','annual')),
    CONSTRAINT ck_holidays__times_paired
        CHECK ((start_time IS NULL) = (end_time IS NULL)),
    CONSTRAINT ck_holidays__source CHECK (source IN ('manual','imported','vertical'))
);

CREATE UNIQUE INDEX uq_holidays__tenant_loc_date_name_global
    ON holidays (tenant_id, holiday_date, name) WHERE location_id IS NULL;
CREATE UNIQUE INDEX uq_holidays__tenant_loc_date_name
    ON holidays (tenant_id, location_id, holiday_date, name)
    WHERE location_id IS NOT NULL;

-- Expansion job: "holidays for this tenant/location in the next N months".
CREATE INDEX ix_holidays__tenant_date
    ON holidays (tenant_id, holiday_date) WHERE is_active;

-- Annual recurrence lookup by month/day, ignoring the stored year.
CREATE INDEX ix_holidays__annual
    ON holidays (tenant_id, (extract(month FROM holiday_date)::smallint),
                 (extract(day FROM holiday_date)::smallint))
    WHERE is_active AND recurrence = 'annual';

CREATE TRIGGER trg_holidays_updated_at
    BEFORE UPDATE ON holidays
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 5.9 `bookings`

The customer-facing aggregate: one reservation, whatever the vertical calls it.

```sql
CREATE TABLE bookings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    location_id         UUID NOT NULL,
    service_id          UUID NOT NULL,

    -- Human-facing reference read aloud on the phone: 'BK-7F3K2Q'.
    reference           TEXT NOT NULL,

    status              booking_status NOT NULL DEFAULT 'pending',

    -- ── The two time ranges ────────────────────────────────────────────────
    -- service_period: what the customer experiences and is billed for.
    -- blocking_period: what the resource is actually unavailable for, i.e.
    --   service_period widened by the service's buffers. Generated, never
    --   hand-maintained, so the two can never disagree.
    -- Both half-open [) so a 10:00–10:30 and a 10:30–11:00 booking do not
    -- overlap. This is the single most important detail in the whole engine:
    -- with inclusive ranges every back-to-back booking would collide.
    service_period      TSTZRANGE NOT NULL,
    buffer_before_minutes INTEGER NOT NULL DEFAULT 0,
    buffer_after_minutes  INTEGER NOT NULL DEFAULT 0,
    blocking_period     TSTZRANGE GENERATED ALWAYS AS (
        tstzrange(
            lower(service_period) - make_interval(mins => buffer_before_minutes),
            upper(service_period) + make_interval(mins => buffer_after_minutes),
            '[)'
        )
    ) STORED,

    -- Denormalized scalars for cheap range-free filtering and sorting. Kept in
    -- lockstep with service_period by a generated expression.
    starts_at           TIMESTAMPTZ GENERATED ALWAYS AS (lower(service_period)) STORED,
    ends_at             TIMESTAMPTZ GENERATED ALWAYS AS (upper(service_period)) STORED,

    -- The local calendar date the booking "belongs to", for day-view queries
    -- and daily aggregates. Stored because deriving it needs the location tz,
    -- which is not available to a generated column.
    local_date          DATE NOT NULL,

    -- ── Party / quantity ───────────────────────────────────────────────────
    party_size          INTEGER NOT NULL DEFAULT 1,
    -- Number of service time_units consumed: nights, days, sessions.
    duration_units      INTEGER NOT NULL DEFAULT 1,

    -- ── Customer ───────────────────────────────────────────────────────────
    -- Deliberately denormalized rather than a customers FK. A receptionist
    -- takes bookings from people who are not registered customers, and the
    -- identity we captured at booking time must not mutate when a customer
    -- record is later edited. A customer_id link is optional and additive.
    customer_id         UUID,
    customer_name       TEXT,
    customer_phone_e164 TEXT,
    customer_email      TEXT,
    customer_locale     TEXT,

    -- Answers to services.intake_schema. Field-level PII classes come from
    -- that schema, so masking is driven by data, not by column names.
    intake_data         JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes               TEXT,
    internal_notes      TEXT,

    -- ── Provenance ─────────────────────────────────────────────────────────
    -- Which conversation produced this booking. FK is deliberately omitted:
    -- conversations is partitioned and may be pruned before bookings are.
    conversation_id     UUID,
    channel             conversation_channel,
    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    booked_by           TEXT NOT NULL DEFAULT 'agent',

    -- ── Money ──────────────────────────────────────────────────────────────
    price_total         NUMERIC(12,4),
    deposit_paid        NUMERIC(12,4),
    currency            CHAR(3),
    payment_status      TEXT NOT NULL DEFAULT 'none',
    external_payment_ref TEXT,

    -- ── Recurrence ─────────────────────────────────────────────────────────
    recurrence_rule_id  UUID,
    -- For a materialised occurrence of a series: which instant in the series
    -- this row represents. Lets us detect and store a single moved occurrence.
    recurrence_instance_start TIMESTAMPTZ,
    is_recurrence_exception BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Lifecycle ──────────────────────────────────────────────────────────
    -- Temporal workflow driving hold → confirm → remind → complete.
    workflow_id         TEXT,
    workflow_run_id     TEXT,

    confirmed_at        TIMESTAMPTZ,
    checked_in_at       TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    cancellation_reason TEXT,
    cancelled_by        TEXT,
    no_show_at          TIMESTAMPTZ,

    -- Reschedule chain: a new row points at the row it replaced.
    rescheduled_from_id UUID,

    -- External system identity (PMS, EMR, OpenTable).
    external_ref        TEXT,
    external_system     TEXT,

    -- Idempotency: an agent retrying a create must not double-book.
    idempotency_key     TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_bookings__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_bookings__services
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_bookings__rescheduled_from
        FOREIGN KEY (tenant_id, rescheduled_from_id)
        REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,

    CONSTRAINT ck_bookings__reference_shape CHECK (reference ~ '^[A-Z0-9-]{4,24}$'),
    CONSTRAINT ck_bookings__period_bounded
        CHECK (lower(service_period) IS NOT NULL AND upper(service_period) IS NOT NULL
               AND NOT isempty(service_period)),
    CONSTRAINT ck_bookings__period_order
        CHECK (upper(service_period) > lower(service_period)),
    CONSTRAINT ck_bookings__buffers_nonneg
        CHECK (buffer_before_minutes >= 0 AND buffer_after_minutes >= 0),
    CONSTRAINT ck_bookings__party_positive CHECK (party_size >= 1),
    CONSTRAINT ck_bookings__units_positive CHECK (duration_units >= 1),
    CONSTRAINT ck_bookings__money_paired
        CHECK ((price_total IS NULL AND deposit_paid IS NULL) OR currency IS NOT NULL),
    CONSTRAINT ck_bookings__money_nonneg
        CHECK ((price_total IS NULL OR price_total >= 0)
               AND (deposit_paid IS NULL OR deposit_paid >= 0)),
    CONSTRAINT ck_bookings__payment_status
        CHECK (payment_status IN ('none','pending','authorized','paid','partially_refunded',
                                  'refunded','failed')),
    CONSTRAINT ck_bookings__booked_by
        CHECK (booked_by IN ('agent','staff','customer','api','import','sync')),
    CONSTRAINT ck_bookings__phone_e164
        CHECK (customer_phone_e164 IS NULL OR customer_phone_e164 ~ '^\+[1-9]\d{6,14}$'),
    -- Terminal-state coherence: a status implies its timestamp and vice versa.
    CONSTRAINT ck_bookings__cancelled_coherent
        CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
    CONSTRAINT ck_bookings__completed_coherent
        CHECK (status <> 'completed' OR completed_at IS NOT NULL),
    CONSTRAINT ck_bookings__noshow_coherent
        CHECK ((status = 'no_show') = (no_show_at IS NOT NULL)),
    CONSTRAINT ck_bookings__confirmed_coherent
        CHECK (status NOT IN ('confirmed','checked_in','in_progress','completed')
               OR confirmed_at IS NOT NULL),
    CONSTRAINT ck_bookings__intake_object CHECK (jsonb_typeof(intake_data) = 'object')
);

ALTER TABLE bookings
    ADD CONSTRAINT uq_bookings__tenant_id_id UNIQUE (tenant_id, id);

-- Customer-quoted reference must be unique within the tenant.
CREATE UNIQUE INDEX uq_bookings__tenant_reference ON bookings (tenant_id, reference);

-- Idempotent create.
CREATE UNIQUE INDEX uq_bookings__tenant_idempotency
    ON bookings (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX uq_bookings__external
    ON bookings (tenant_id, external_system, external_ref)
    WHERE external_ref IS NOT NULL;

CREATE TRIGGER trg_bookings_updated_at
    BEFORE UPDATE ON bookings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Why `blocking_period` is a `GENERATED ... STORED` column.** The exclusion
constraint in §5.10 must index the *buffered* interval. If buffers were applied
in application code when writing a separate column, a bug or a direct SQL fix
could desynchronise them and silently punch a hole in double-booking protection.
A generated column makes the widened range a function of the stored inputs,
enforced by the storage engine. `STORED` (not `VIRTUAL`, which PG16 lacks
anyway) is required for it to be indexable.

> **CORRECTION — the DDL above does not compile as written.**
>
> PostgreSQL rejects it with `ERROR: generation expression is not immutable`.
> `make_interval` and `tstzrange` are both IMMUTABLE, but the operator between
> them — `timestamptz + interval`, i.e. `timestamptz_pl_interval` — is
> **STABLE**. An `interval` may carry months and days, whose length in real time
> depends on the session `TimeZone` (adding `1 day` across a DST boundary is 23
> or 25 hours), so the planner cannot know at DDL time that this particular
> interval is minutes-only. The whole expression inherits STABLE and a generated
> column refuses it. §14.1's worked example therefore cannot be executed as
> printed either — see the correction there.
>
> **The shipped form** (migration `0003_booking_engine.py`) substitutes an
> IMMUTABLE helper:
>
> ```sql
> CREATE FUNCTION booking_shift_minutes(p_at TIMESTAMPTZ, p_minutes INTEGER)
> RETURNS TIMESTAMPTZ LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
>     SELECT to_timestamp(extract(epoch FROM p_at) + p_minutes * 60)
> $$;
>
> blocking_period TSTZRANGE GENERATED ALWAYS AS (
>     tstzrange(
>         booking_shift_minutes(lower(service_period), -buffer_before_minutes),
>         booking_shift_minutes(upper(service_period),  buffer_after_minutes),
>         '[)'
>     )
> ) STORED
> ```
>
> The substitution is **exact, not approximate**. A `timestamptz` is an absolute
> instant and its epoch-second representation is timezone-independent by
> definition, so shifting by a fixed number of seconds is a pure function of its
> inputs — which is what IMMUTABLE means. It produces byte-identical results to
> `+ make_interval(mins => n)` for every input, because a minutes-only interval
> has no months or days to resolve and so there was never any timezone
> dependence to lose. The equivalence is asserted directly by
> `tests/booking/test_double_booking.py::test_blocking_period_is_generated_and_cannot_be_written`,
> which compares the stored column against the `make_interval` expression on a
> live row.
>
> Everything else about this section stands: the column is still `GENERATED ...
> STORED`, still a function of `service_period` and the two buffer columns, and
> still the value the exclusion constraint indexes.

**Why buffers are copied onto the booking rather than read from `services`.**
A service's buffer policy can change; existing bookings must keep the geometry
they were created with, or an admin editing "cleanup time" from 10 to 20 minutes
would retroactively make historical bookings overlap and break the exclusion
constraint on the next unrelated write. Copy-on-write of policy values into the
transaction record is the standard fix and we apply it consistently
(cf. `subscriptions.entitlements_snapshot`).

### 5.10 `booking_items` — where double-booking is prevented

A booking consumes one row per allocated resource. This is the table the
exclusion constraint lives on, because the constraint is fundamentally about a
*resource's* timeline, not a booking's.

```sql
CREATE TABLE booking_items (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    booking_id          UUID NOT NULL,
    resource_id         UUID NOT NULL,

    -- Which requirement this allocation satisfies ('primary','assist','space').
    role                TEXT NOT NULL DEFAULT 'primary',
    requirement_id      UUID,

    -- Capacity units consumed on this resource for this interval.
    --   Exclusive resource: 1.
    --   Shared studio, class of 5: 5.
    --   Scooter pool, party of 3: 3.
    quantity            INTEGER NOT NULL DEFAULT 1,

    -- ── The occupancy interval ─────────────────────────────────────────────
    -- Copied from the parent booking (service or blocking range, depending on
    -- the requirement's respects_buffers flag) by trigger, so the exclusion
    -- constraint can be a single-table check with no joins. Denormalization
    -- here is not an optimisation — an EXCLUDE constraint cannot reference
    -- another table, so this is the only way to enforce it declaratively.
    occupies            TSTZRANGE NOT NULL,

    -- Mirrors bookings.status so the constraint can ignore cancelled rows.
    -- Maintained by trigger; never written directly by the application.
    is_blocking         BOOLEAN NOT NULL DEFAULT TRUE,

    -- Denormalized from resources for the shared-capacity path.
    is_capacity_shared  BOOLEAN NOT NULL DEFAULT FALSE,

    -- Pricing attribution when a booking spans differently-priced resources.
    price_component     NUMERIC(12,4),
    currency            CHAR(3),

    -- External calendar event id created for this specific allocation.
    external_event_ref  TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_booking_items__bookings
        FOREIGN KEY (tenant_id, booking_id)
        REFERENCES bookings (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_booking_items__resources
        FOREIGN KEY (tenant_id, resource_id)
        REFERENCES resources (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_booking_items__quantity CHECK (quantity >= 1),
    CONSTRAINT ck_booking_items__occupies_bounded
        CHECK (lower(occupies) IS NOT NULL AND upper(occupies) IS NOT NULL
               AND NOT isempty(occupies)),
    CONSTRAINT ck_booking_items__money_paired
        CHECK (price_component IS NULL OR currency IS NOT NULL)
);

-- ════════════════════════════════════════════════════════════════════════════
-- THE DOUBLE-BOOKING CONSTRAINT
-- ════════════════════════════════════════════════════════════════════════════
-- Read it as: "no two blocking allocations of the same exclusive resource,
-- within the same tenant, may have overlapping occupancy intervals."
--
-- tenant_id WITH =   : same tenant (needs btree_gist)
-- resource_id WITH = : same resource (needs btree_gist)
-- occupies WITH &&   : overlapping ranges
--
-- All three must be TRUE simultaneously for the constraint to fire; that is
-- exactly the definition of a conflict. The WHERE clause makes it a partial
-- constraint so cancelled bookings and shared-capacity resources are exempt.
ALTER TABLE booking_items
    ADD CONSTRAINT ex_booking_items__no_overlap
    EXCLUDE USING gist (
        tenant_id   WITH =,
        resource_id WITH =,
        occupies    WITH &&
    )
    WHERE (is_blocking AND NOT is_capacity_shared)
    DEFERRABLE INITIALLY IMMEDIATE;

-- Fast lookup of a booking's allocations.
CREATE INDEX ix_booking_items__tenant_booking
    ON booking_items (tenant_id, booking_id);

-- Resource timeline: "what is on Dr Chen's calendar this week". The GiST index
-- backing the EXCLUDE constraint only covers blocking, non-shared rows, so we
-- need a second one for the general case (including cancelled, for history).
CREATE INDEX ix_booking_items__tenant_resource_occupies
    ON booking_items USING gist (tenant_id, resource_id, occupies);

-- Shared-capacity summation: "how many units of resource R are consumed over
-- range X". Separate partial index keeps it small.
CREATE INDEX ix_booking_items__shared_capacity
    ON booking_items USING gist (tenant_id, resource_id, occupies)
    WHERE is_capacity_shared AND is_blocking;

CREATE TRIGGER trg_booking_items_updated_at
    BEFORE UPDATE ON booking_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Why `DEFERRABLE INITIALLY IMMEDIATE` and not `INITIALLY DEFERRED`.** Immediate
checking gives us the failure at the offending `INSERT`, with the correct row in
the error context, which is what the booking service needs in order to retry
allocation against a different resource. `DEFERRABLE` is still declared so a
rare bulk operation (importing a customer's existing schedule, or a "swap these
two rooms" move) can `SET CONSTRAINTS ex_booking_items__no_overlap DEFERRED`
inside one transaction and pass through a transiently-overlapping intermediate
state. Without `DEFERRABLE`, an A→B/B→A swap is impossible without a temporary
third value.

**Trigger that maintains `occupies`, `is_blocking`, `is_capacity_shared`.** These
three columns are the constraint's entire input, so the application is never
allowed to set them:

```sql
CREATE OR REPLACE FUNCTION sync_booking_item_occupancy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    b       bookings%ROWTYPE;
    r_shared BOOLEAN;
    respects BOOLEAN;
BEGIN
    SELECT * INTO b FROM bookings
     WHERE id = NEW.booking_id AND tenant_id = NEW.tenant_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'booking_items: parent booking % not found', NEW.booking_id;
    END IF;

    SELECT rt.is_capacity_shared INTO r_shared
      FROM resources res
      JOIN resource_types rt
        ON rt.id = res.resource_type_id AND rt.tenant_id = res.tenant_id
     WHERE res.id = NEW.resource_id AND res.tenant_id = NEW.tenant_id;

    NEW.is_capacity_shared := COALESCE(r_shared, FALSE);

    SELECT COALESCE(srr.respects_buffers, TRUE) INTO respects
      FROM service_resource_requirements srr
     WHERE srr.id = NEW.requirement_id AND srr.tenant_id = NEW.tenant_id;

    NEW.occupies := CASE
        WHEN COALESCE(respects, TRUE) THEN b.blocking_period
        ELSE b.service_period
    END;

    -- A booking only blocks its resources while it is in a live state.
    NEW.is_blocking := b.status IN
        ('draft','pending','confirmed','checked_in','in_progress','completed');

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_booking_items_sync_occupancy
    BEFORE INSERT OR UPDATE OF booking_id, resource_id, requirement_id
    ON booking_items
    FOR EACH ROW EXECUTE FUNCTION sync_booking_item_occupancy();
```

**Cascade when the parent booking moves or is cancelled.** A reschedule changes
`bookings.service_period`; every child item's `occupies` must follow, and the
exclusion constraint must re-evaluate at that moment:

```sql
CREATE OR REPLACE FUNCTION cascade_booking_period_to_items()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.service_period IS DISTINCT FROM OLD.service_period
       OR NEW.blocking_period IS DISTINCT FROM OLD.blocking_period
       OR NEW.status IS DISTINCT FROM OLD.status THEN

        UPDATE booking_items bi
           SET occupies = CASE
                   WHEN COALESCE(srr.respects_buffers, TRUE) THEN NEW.blocking_period
                   ELSE NEW.service_period
               END,
               is_blocking = NEW.status IN
                   ('draft','pending','confirmed','checked_in','in_progress','completed'),
               updated_at = now()
          FROM (SELECT 1) AS _
          LEFT JOIN service_resource_requirements srr ON FALSE
         WHERE bi.booking_id = NEW.id AND bi.tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;
```

That LEFT JOIN form is awkward; the correlated version below is the one we
actually ship — clearer, and the row counts here are single digits:

```sql
CREATE OR REPLACE FUNCTION cascade_booking_period_to_items()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.service_period IS DISTINCT FROM OLD.service_period
       OR NEW.buffer_before_minutes IS DISTINCT FROM OLD.buffer_before_minutes
       OR NEW.buffer_after_minutes  IS DISTINCT FROM OLD.buffer_after_minutes
       OR NEW.status IS DISTINCT FROM OLD.status THEN

        UPDATE booking_items bi
           SET occupies = CASE
                   WHEN COALESCE(
                            (SELECT srr.respects_buffers
                               FROM service_resource_requirements srr
                              WHERE srr.id = bi.requirement_id
                                AND srr.tenant_id = bi.tenant_id),
                            TRUE)
                   THEN NEW.blocking_period
                   ELSE NEW.service_period
               END,
               is_blocking = NEW.status IN
                   ('draft','pending','confirmed','checked_in','in_progress','completed'),
               updated_at = now()
         WHERE bi.booking_id = NEW.id
           AND bi.tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_bookings_cascade_period
    AFTER UPDATE ON bookings
    FOR EACH ROW EXECUTE FUNCTION cascade_booking_period_to_items();
```

**Shared-capacity enforcement.** The EXCLUDE constraint deliberately skips
shared resources — "no overlap at all" is the wrong rule for a 20-person studio.
The right rule ("summed quantity over any instant must not exceed capacity +
overbook_allowance") is not expressible as an exclusion constraint, because it
is an aggregate over a moving window. We enforce it with a constraint trigger
that runs at statement end and takes a row lock on the resource first, so
concurrent inserts serialise:

```sql
CREATE OR REPLACE FUNCTION check_shared_capacity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    cap        INTEGER;
    over       INTEGER;
    peak       INTEGER;
BEGIN
    IF NOT NEW.is_capacity_shared OR NOT NEW.is_blocking THEN
        RETURN NULL;
    END IF;

    -- Serialise concurrent bookings of the same shared resource. This is a
    -- row lock, not a table lock: bookings of *different* resources proceed
    -- in parallel.
    SELECT r.capacity, r.overbook_allowance INTO cap, over
      FROM resources r
     WHERE r.id = NEW.resource_id AND r.tenant_id = NEW.tenant_id
       FOR UPDATE;

    -- Peak concurrent consumption is found by evaluating the running sum at
    -- every interval start point that overlaps the new item: the maximum of a
    -- step function can only occur at a step boundary.
    WITH overlapping AS (
        SELECT bi.quantity, bi.occupies
          FROM booking_items bi
         WHERE bi.tenant_id = NEW.tenant_id
           AND bi.resource_id = NEW.resource_id
           AND bi.is_blocking
           AND bi.is_capacity_shared
           AND bi.occupies && NEW.occupies
    ),
    boundaries AS (
        SELECT DISTINCT lower(occupies) AS t FROM overlapping
    )
    SELECT COALESCE(MAX(load), 0) INTO peak
      FROM (
        SELECT b.t, SUM(o.quantity) AS load
          FROM boundaries b
          JOIN overlapping o ON o.occupies @> b.t
         GROUP BY b.t
      ) s;

    IF peak > cap + over THEN
        RAISE EXCEPTION
            'capacity exceeded on resource %: peak % > capacity % (+% overbook)',
            NEW.resource_id, peak, cap, over
            USING ERRCODE = 'exclusion_violation';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_booking_items_shared_capacity
    AFTER INSERT OR UPDATE ON booking_items
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION check_shared_capacity();
```

Note the trigger raises `exclusion_violation` (SQLSTATE `23P01`) deliberately,
so the application's conflict handler catches one error class for both the
exclusive and shared paths.

### 5.11 `booking_holds`

A hold is a **short-lived, TTL-bounded reservation of capacity** taken while the
agent is still talking to the caller. It must block competing bookings, and it
must vanish on its own if the caller hangs up.

```sql
CREATE TABLE booking_holds (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    location_id         UUID NOT NULL,
    service_id          UUID NOT NULL,
    resource_id         UUID NOT NULL,

    status              hold_status NOT NULL DEFAULT 'active',

    -- Same geometry as booking_items.occupies, and deliberately the same
    -- column name and semantics so the two participate in one constraint (§13.2).
    occupies            TSTZRANGE NOT NULL,
    quantity            INTEGER NOT NULL DEFAULT 1,
    is_capacity_shared  BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── TTL ────────────────────────────────────────────────────────────────
    -- The moment the hold stops blocking. Enforced three ways (§13.3):
    --   1. the partial index predicate on the exclusion constraint,
    --   2. a Temporal timer that releases it,
    --   3. a sweeper that expires stragglers.
    expires_at          TIMESTAMPTZ NOT NULL,

    -- Derived, indexed flag: only active, unexpired holds block. Cannot be a
    -- generated column because now() is not immutable — maintained by the
    -- sweeper and by the constraint predicate, which re-checks expires_at.
    -- See §13.3 for why the predicate alone is sufficient for correctness.

    -- Who holds it.
    conversation_id     UUID,
    session_token       TEXT,
    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,

    -- Temporal correlation, so a hold can be released by the workflow that
    -- created it even after a worker restart.
    workflow_id         TEXT,

    -- Once converted, which booking claimed it.
    booking_id          UUID,
    converted_at        TIMESTAMPTZ,
    released_at         TIMESTAMPTZ,
    release_reason      TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_booking_holds__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_booking_holds__services
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_booking_holds__resources
        FOREIGN KEY (tenant_id, resource_id)
        REFERENCES resources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_booking_holds__bookings
        FOREIGN KEY (tenant_id, booking_id)
        REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,

    CONSTRAINT ck_booking_holds__occupies_bounded
        CHECK (lower(occupies) IS NOT NULL AND upper(occupies) IS NOT NULL
               AND NOT isempty(occupies)),
    CONSTRAINT ck_booking_holds__quantity CHECK (quantity >= 1),
    CONSTRAINT ck_booking_holds__converted_coherent
        CHECK ((status = 'converted') = (booking_id IS NOT NULL AND converted_at IS NOT NULL)),
    CONSTRAINT ck_booking_holds__released_coherent
        CHECK (status <> 'released' OR released_at IS NOT NULL)
);

-- Holds do not overlap each other on an exclusive resource.
ALTER TABLE booking_holds
    ADD CONSTRAINT ex_booking_holds__no_overlap
    EXCLUDE USING gist (
        tenant_id   WITH =,
        resource_id WITH =,
        occupies    WITH &&
    )
    WHERE (status = 'active' AND NOT is_capacity_shared);

-- Sweeper: "expire everything past its TTL". Tiny partial index, so the
-- sweeper's cost is proportional to live holds, not to history.
CREATE INDEX ix_booking_holds__expiry_sweep
    ON booking_holds (expires_at) WHERE status = 'active';

CREATE INDEX ix_booking_holds__tenant_resource_active
    ON booking_holds USING gist (tenant_id, resource_id, occupies)
    WHERE status = 'active';

CREATE INDEX ix_booking_holds__conversation
    ON booking_holds (tenant_id, conversation_id)
    WHERE conversation_id IS NOT NULL;

CREATE UNIQUE INDEX uq_booking_holds__booking
    ON booking_holds (tenant_id, booking_id) WHERE booking_id IS NOT NULL;

CREATE TRIGGER trg_booking_holds_updated_at
    BEFORE UPDATE ON booking_holds
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

The hold-vs-booking interaction — the part that actually needs care — is §13.2
and §13.3.

### 5.12 `waitlist_entries`

```sql
CREATE TABLE waitlist_entries (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    location_id         UUID NOT NULL,
    service_id          UUID NOT NULL,

    status              waitlist_status NOT NULL DEFAULT 'waiting',

    -- What the customer will accept. A range rather than an instant, because a
    -- waitlist is inherently "anything in this window works".
    desired_period      TSTZRANGE NOT NULL,
    -- Narrower preference inside desired_period, used for ranking offers.
    preferred_start     TIMESTAMPTZ,

    party_size          INTEGER NOT NULL DEFAULT 1,
    duration_units      INTEGER NOT NULL DEFAULT 1,

    -- Optional pinning: "only Dr Chen" / "only a patio table".
    resource_id         UUID,
    resource_type_id    UUID,
    attribute_predicate JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Ordering. Lower number = earlier offer. Default is FIFO by created_at;
    -- priority lets staff bump a VIP or a clinically urgent case.
    priority            SMALLINT NOT NULL DEFAULT 100,

    customer_id         UUID,
    customer_name       TEXT,
    customer_phone_e164 TEXT,
    customer_email      TEXT,
    notify_channels     notification_channel[] NOT NULL DEFAULT '{sms}',

    conversation_id     UUID,

    -- Offer lifecycle: when a slot opens we offer it, with its own short TTL.
    offered_booking_id  UUID,
    offered_at          TIMESTAMPTZ,
    offer_expires_at    TIMESTAMPTZ,
    offer_attempts      SMALLINT NOT NULL DEFAULT 0,

    converted_booking_id UUID,
    converted_at        TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,        -- entry itself goes stale
    cancelled_at        TIMESTAMPTZ,

    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_waitlist_entries__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_waitlist_entries__services
        FOREIGN KEY (tenant_id, service_id)
        REFERENCES services (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_waitlist_entries__resources
        FOREIGN KEY (tenant_id, resource_id)
        REFERENCES resources (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT fk_waitlist_entries__resource_types
        FOREIGN KEY (tenant_id, resource_type_id)
        REFERENCES resource_types (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT fk_waitlist_entries__converted
        FOREIGN KEY (tenant_id, converted_booking_id)
        REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,

    CONSTRAINT ck_waitlist_entries__period_bounded
        CHECK (lower(desired_period) IS NOT NULL AND upper(desired_period) IS NOT NULL),
    CONSTRAINT ck_waitlist_entries__preferred_inside
        CHECK (preferred_start IS NULL OR desired_period @> preferred_start),
    CONSTRAINT ck_waitlist_entries__party CHECK (party_size >= 1),
    CONSTRAINT ck_waitlist_entries__priority CHECK (priority BETWEEN 0 AND 1000),
    CONSTRAINT ck_waitlist_entries__converted_coherent
        CHECK ((status = 'converted') = (converted_booking_id IS NOT NULL)),
    CONSTRAINT ck_waitlist_entries__contactable
        CHECK (customer_phone_e164 IS NOT NULL OR customer_email IS NOT NULL)
);

-- The matcher's query, run whenever a booking is cancelled: "who is waiting
-- for a slot that overlaps the one that just freed up, best candidate first".
-- GiST on the desired range, with tenant and service leading.
CREATE INDEX ix_waitlist_entries__match
    ON waitlist_entries USING gist (tenant_id, service_id, desired_period)
    WHERE status = 'waiting';

-- Offer ranking within the matched set.
CREATE INDEX ix_waitlist_entries__tenant_priority
    ON waitlist_entries (tenant_id, service_id, priority, created_at)
    WHERE status = 'waiting';

-- Offer-timeout sweeper.
CREATE INDEX ix_waitlist_entries__offer_expiry
    ON waitlist_entries (offer_expires_at) WHERE status = 'offered';

CREATE INDEX ix_waitlist_entries__tenant_customer
    ON waitlist_entries (tenant_id, customer_phone_e164)
    WHERE customer_phone_e164 IS NOT NULL;

CREATE TRIGGER trg_waitlist_entries_updated_at
    BEFORE UPDATE ON waitlist_entries
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 5.13 `recurrence_rules`

RFC 5545 RRULE semantics, stored as structured columns rather than an opaque
string, so the expander can query them and an admin UI can round-trip them.

```sql
CREATE TABLE recurrence_rules (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- What recurs. A recurrence rule may drive a series of bookings
    -- (weekly physio) or a repeating availability block. Exactly one owner.
    owner_kind          TEXT NOT NULL DEFAULT 'booking',

    freq                TEXT NOT NULL,      -- DAILY | WEEKLY | MONTHLY | YEARLY
    interval_n          SMALLINT NOT NULL DEFAULT 1,

    by_weekday          SMALLINT[],         -- ISO 1..7
    by_month_day        SMALLINT[],         -- 1..31, negative = from end
    by_month            SMALLINT[],         -- 1..12
    by_set_pos          SMALLINT[],         -- "the 2nd Tuesday" = weekday{2}, setpos{2}

    -- Anchor in LOCAL wall-clock terms plus the tz, so a weekly 09:00 series
    -- stays at 09:00 across a DST boundary. This is the single most common
    -- recurrence bug and the reason we do not store a UTC anchor.
    dtstart_local       TIMESTAMP NOT NULL,
    timezone            TEXT NOT NULL,

    -- Termination: exactly one of count / until, or neither (infinite).
    count_n             INTEGER,
    until_local         TIMESTAMP,

    -- Materialisation horizon: how far ahead the expander has already created
    -- concrete bookings. Infinite series are expanded in a rolling window.
    expanded_through    TIMESTAMPTZ,

    -- Dates removed from the series (RFC 5545 EXDATE), as local timestamps.
    exdates             TIMESTAMP[] NOT NULL DEFAULT '{}',
    -- Extra dates added to the series (RDATE).
    rdates              TIMESTAMP[] NOT NULL DEFAULT '{}',

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT ck_recurrence_rules__owner_kind
        CHECK (owner_kind IN ('booking','availability','holiday')),
    CONSTRAINT ck_recurrence_rules__freq
        CHECK (freq IN ('DAILY','WEEKLY','MONTHLY','YEARLY')),
    CONSTRAINT ck_recurrence_rules__interval CHECK (interval_n BETWEEN 1 AND 366),
    CONSTRAINT ck_recurrence_rules__termination
        CHECK (NOT (count_n IS NOT NULL AND until_local IS NOT NULL)),
    CONSTRAINT ck_recurrence_rules__count CHECK (count_n IS NULL OR count_n BETWEEN 1 AND 1000),
    CONSTRAINT ck_recurrence_rules__timezone_valid
        CHECK (now() AT TIME ZONE timezone IS NOT NULL),
    CONSTRAINT ck_recurrence_rules__weekday_range
        CHECK (by_weekday IS NULL OR by_weekday <@ ARRAY[1,2,3,4,5,6,7]::smallint[]),
    CONSTRAINT ck_recurrence_rules__month_range
        CHECK (by_month IS NULL OR by_month <@ ARRAY[1,2,3,4,5,6,7,8,9,10,11,12]::smallint[])
);

ALTER TABLE recurrence_rules
    ADD CONSTRAINT uq_recurrence_rules__tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE bookings
    ADD CONSTRAINT fk_bookings__recurrence_rules
    FOREIGN KEY (tenant_id, recurrence_rule_id)
    REFERENCES recurrence_rules (tenant_id, id) ON DELETE SET NULL;

-- One occurrence per series per instant: the expander is idempotent.
CREATE UNIQUE INDEX uq_bookings__recurrence_instance
    ON bookings (tenant_id, recurrence_rule_id, recurrence_instance_start)
    WHERE recurrence_rule_id IS NOT NULL;

-- Expander sweep: "series whose horizon needs extending".
CREATE INDEX ix_recurrence_rules__expansion
    ON recurrence_rules (expanded_through)
    WHERE is_active AND owner_kind = 'booking';

CREATE INDEX ix_recurrence_rules__tenant_active
    ON recurrence_rules (tenant_id, owner_kind) WHERE is_active;

CREATE TRIGGER trg_recurrence_rules_updated_at
    BEFORE UPDATE ON recurrence_rules
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Why materialise occurrences into `bookings` at all**, rather than expanding on
read? Because each occurrence has independent state — one can be cancelled,
rescheduled, checked in, or paid — and because the exclusion constraint can only
protect rows that exist. A virtual occurrence cannot participate in
double-booking prevention. We expand on a rolling 180-day horizon (configurable
per tenant) driven by a Temporal schedule, with `uq_bookings__recurrence_instance`
making re-expansion idempotent.

---

## 6. Conversations

Contract §7 fixes the vocabulary: `Conversation → Turn[] → Transcript? →
Outcome`. Voice and chat converge; agents are channel-agnostic.

`conversations` and `turns` are the two highest-volume tables in the system and
are both **partitioned by month** (§17).

### 6.1 `conversations`

```sql
CREATE TABLE conversations (
    id                  UUID NOT NULL DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL,
    location_id         UUID,

    channel             conversation_channel NOT NULL,
    status              conversation_status NOT NULL DEFAULT 'active',

    -- Channel-native identity: a Twilio CallSid, a WhatsApp wa_id, a web
    -- session id. Lets an inbound webhook find its conversation without a
    -- lookup table.
    external_ref        TEXT,
    channel_metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- ── Participants ───────────────────────────────────────────────────────
    -- The end user. PII-classified; masked in logs and prompts unless the
    -- tenant opted in (contract §8).
    contact_identifier  TEXT,               -- E.164, email, or channel handle
    contact_name        TEXT,

    -- The resolved cross-channel identity (§3.9), when there is one.
    -- DELIBERATELY NULLABLE, per 04 §11.2.1(2): an anonymous web visitor has
    -- no resolved customer, and making this NOT NULL would force the resolver
    -- to fabricate an identity record for someone who never identified
    -- themselves — creating the profile the privacy model exists to avoid.
    -- NULL is the correct representation of "we do not know who this is".
    -- No inline FK: see the note below on why it is enforced by trigger.
    customer_id         UUID,
    -- Hash for analytics joins that must not carry the raw identifier.
    contact_hash        BYTEA,

    -- Which agent configuration ran. Snapshot the version, because manifests
    -- change and we must be able to explain a transcript months later.
    vertical_install_id UUID,
    agent_code          TEXT,
    agent_version       TEXT,

    -- ── Timing ─────────────────────────────────────────────────────────────
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at         TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,
    duration_ms         INTEGER,

    -- ── Aggregates, maintained incrementally ───────────────────────────────
    -- Rolled up here so the conversation list view needs no aggregate over
    -- turns (which is a partitioned, 100M-row table).
    turn_count          INTEGER NOT NULL DEFAULT 0,
    total_input_tokens  BIGINT NOT NULL DEFAULT 0,
    total_output_tokens BIGINT NOT NULL DEFAULT 0,
    total_cost          NUMERIC(12,4) NOT NULL DEFAULT 0,
    cost_currency       CHAR(3) NOT NULL DEFAULT 'USD',

    -- Quality signals surfaced in the dashboard.
    first_response_ms   INTEGER,
    p95_response_ms     INTEGER,
    interruption_count  SMALLINT NOT NULL DEFAULT 0,
    escalated           BOOLEAN NOT NULL DEFAULT FALSE,
    escalated_at        TIMESTAMPTZ,
    escalation_reason   TEXT,

    -- Language actually used, detected or configured.
    language            TEXT,

    -- Redaction state: whether PII has been scrubbed by the retention job.
    redacted_at         TIMESTAMPTZ,

    -- Correlation for tracing across the whole request tree.
    correlation_id      UUID,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_conversations PRIMARY KEY (tenant_id, created_at, id),
    CONSTRAINT ck_conversations__duration
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT ck_conversations__end_after_start
        CHECK (ended_at IS NULL OR ended_at >= started_at),
    CONSTRAINT ck_conversations__ended_coherent
        CHECK (status <> 'ended' OR ended_at IS NOT NULL),
    CONSTRAINT ck_conversations__escalation_coherent
        CHECK (escalated = (escalated_at IS NOT NULL)),
    CONSTRAINT ck_conversations__cost_nonneg CHECK (total_cost >= 0)
) PARTITION BY RANGE (created_at);
```

**Why the primary key is `(tenant_id, created_at, id)`.** PostgreSQL requires
the partition key to be part of every unique constraint on a partitioned table.
`created_at` must therefore appear in the PK. Leading with `tenant_id` keeps the
contract's tenant-first rule intact, and it makes the PK index itself useful for
the dominant query ("this tenant's conversations, newest first"). The cost is
that a bare `WHERE id = ?` lookup must scan all partitions — so the application
always carries `(tenant_id, created_at)` alongside a conversation id, which the
UUIDv7 timestamp makes free: `created_at` is recoverable from `id` via
`uuid_v7_timestamp(id)` and the repository injects it automatically (§15.4).

```sql
-- Indexes are declared on the parent; PostgreSQL creates them on every
-- partition, existing and future.

-- Conversation list, the admin portal's landing query.
CREATE INDEX ix_conversations__tenant_started
    ON conversations (tenant_id, started_at DESC);

-- Filtered list views: by channel, by status.
CREATE INDEX ix_conversations__tenant_channel_started
    ON conversations (tenant_id, channel, started_at DESC);

CREATE INDEX ix_conversations__tenant_status
    ON conversations (tenant_id, status, started_at DESC)
    WHERE status = 'active';

-- Inbound webhook resolution: "find the conversation for CallSid X".
CREATE UNIQUE INDEX uq_conversations__tenant_external
    ON conversations (tenant_id, channel, external_ref, created_at)
    WHERE external_ref IS NOT NULL;

-- Customer history: "every call from this number".
CREATE INDEX ix_conversations__tenant_contact
    ON conversations (tenant_id, contact_identifier, started_at DESC)
    WHERE contact_identifier IS NOT NULL;

-- Cross-channel history: "every conversation with this customer, any channel".
-- This is the query the agent runs at conversation start to recall context,
-- and the one the erasure path uses to find what to redact.
CREATE INDEX ix_conversations__tenant_customer
    ON conversations (tenant_id, customer_id, started_at DESC)
    WHERE customer_id IS NOT NULL;

CREATE INDEX ix_conversations__tenant_location
    ON conversations (tenant_id, location_id, started_at DESC)
    WHERE location_id IS NOT NULL;

-- Escalation review queue.
CREATE INDEX ix_conversations__escalated
    ON conversations (tenant_id, escalated_at DESC) WHERE escalated;

CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN conversations.contact_identifier IS
    'pii_class=medium; masked in logs and LLM prompts unless tenant opted in';
COMMENT ON COLUMN conversations.contact_name IS 'pii_class=medium';
COMMENT ON COLUMN conversations.customer_id IS
    'Nullable by design (04 §11.2.1): NULL means "not identified", which is the '
    'correct state for an anonymous visitor. Integrity enforced by '
    'trg_conversations_customer_fk, not a declarative FK — see 01 §6.1.';
```

**Why `conversations.customer_id` has no declarative foreign key.** `04`
§11.2.1(2) asks for a composite FK so a conversation cannot be attributed to
another tenant's customer. The intent is right and is enforced — but not with
`FOREIGN KEY`, because `conversations` is partitioned and PostgreSQL does not
support a foreign key **from** a partitioned table whose own partitions are
created dynamically without that FK being re-established per partition. Declaring
it on the parent works, but every monthly partition then inherits an FK check on
a table that will outlive many of those partitions, and `ops_create_monthly_
partitions()` would have to stay in lockstep with it.

The same constraint already forced `turns` to drop its FK to `conversations`
(§6.2). Here, unlike there, the guarantee is a **cross-tenant** one, so dropping
it silently is not acceptable. We enforce it with a constraint trigger instead:

```sql
CREATE OR REPLACE FUNCTION check_conversation_customer_tenant()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.customer_id IS NULL THEN
        RETURN NULL;                    -- anonymous: nothing to check
    END IF;

    -- The tenant_id equality is the whole point: a customer row belonging to
    -- another tenant is invisible under RLS, so without this check the
    -- reference would be accepted and then dangle.
    IF NOT EXISTS (
        SELECT 1 FROM customers c
         WHERE c.id = NEW.customer_id
           AND c.tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION
            'conversations.customer_id % does not exist in tenant %',
            NEW.customer_id, NEW.tenant_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_conversations_customer_fk
    AFTER INSERT OR UPDATE OF customer_id, tenant_id ON conversations
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION check_conversation_customer_tenant();
```

It raises `foreign_key_violation` (SQLSTATE `23503`) deliberately, so the
repository's error translator (§15.4) maps it to the same domain error as a real
FK without special-casing. What it does **not** provide is `ON DELETE SET NULL`;
`ops_erase_customer` (§3.9) nulls the column explicitly, which is why that
function performs the update rather than relying on a cascade.

### 6.2 `turns`

One row per message, tool call, or system event. The single largest table.

```sql
CREATE TABLE turns (
    id                  UUID NOT NULL DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL,
    conversation_id     UUID NOT NULL,

    -- Monotonic ordering within a conversation. Explicit, not derived from
    -- created_at, because two turns can share a millisecond and because a
    -- retried tool call must slot deterministically.
    sequence            INTEGER NOT NULL,

    role                turn_role NOT NULL,

    -- Rendered text. NULL for a pure tool-call turn with no assistant prose.
    content             TEXT,
    -- Structured content for multimodal or channel-specific payloads
    -- (images, buttons, cards, DTMF).
    content_parts       JSONB,

    -- Tool interaction, matching the OpenAI/Anthropic shape:
    -- [{"id":"call_1","name":"check_availability","arguments":{...}}]
    tool_calls          JSONB,
    tool_call_id        TEXT,               -- for role='tool', which call this answers
    tool_name           TEXT,
    tool_result         JSONB,
    tool_error          TEXT,
    tool_duration_ms    INTEGER,

    -- ── Model accounting (contract §7: role, content, tool_calls, latency,
    --    tokens, cost) ─────────────────────────────────────────────────────
    model_provider      TEXT,
    model_name          TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cached_tokens       INTEGER,
    reasoning_tokens    INTEGER,
    cost                NUMERIC(12,6),
    cost_currency       CHAR(3),

    latency_ms          INTEGER,
    time_to_first_token_ms INTEGER,

    finish_reason       TEXT,

    -- Which agent node in the LangGraph produced this turn.
    agent_node          TEXT,

    -- Voice specifics.
    audio_start_ms      INTEGER,            -- offset into the recording
    audio_end_ms        INTEGER,
    is_interruption     BOOLEAN NOT NULL DEFAULT FALSE,

    -- Safety and redaction.
    pii_redacted        BOOLEAN NOT NULL DEFAULT FALSE,
    safety_flags        TEXT[],

    correlation_id      UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_turns PRIMARY KEY (tenant_id, created_at, id),
    CONSTRAINT ck_turns__sequence_nonneg CHECK (sequence >= 0),
    CONSTRAINT ck_turns__tokens_nonneg CHECK (
        COALESCE(input_tokens, 0) >= 0 AND COALESCE(output_tokens, 0) >= 0
        AND COALESCE(cached_tokens, 0) >= 0
    ),
    CONSTRAINT ck_turns__cost_nonneg CHECK (cost IS NULL OR cost >= 0),
    CONSTRAINT ck_turns__cost_currency_paired
        CHECK ((cost IS NULL) = (cost_currency IS NULL)),
    -- A tool-result turn must say which call it answers.
    CONSTRAINT ck_turns__tool_role_shape
        CHECK (role <> 'tool' OR tool_call_id IS NOT NULL),
    CONSTRAINT ck_turns__audio_order
        CHECK (audio_end_ms IS NULL OR audio_start_ms IS NULL
               OR audio_end_ms >= audio_start_ms),
    CONSTRAINT ck_turns__content_present
        CHECK (content IS NOT NULL OR content_parts IS NOT NULL
               OR tool_calls IS NOT NULL OR tool_result IS NOT NULL
               OR role = 'event')
) PARTITION BY RANGE (created_at);

-- The dominant query by an enormous margin: "replay this conversation in
-- order". Every agent invocation runs it to rebuild context.
CREATE UNIQUE INDEX uq_turns__conversation_sequence
    ON turns (tenant_id, conversation_id, sequence, created_at);

-- Cost and token reporting rolled up by day.
CREATE INDEX ix_turns__tenant_created
    ON turns (tenant_id, created_at DESC);

-- Tool analytics: "how often does check_availability fail, and how slow is it".
CREATE INDEX ix_turns__tenant_tool
    ON turns (tenant_id, tool_name, created_at DESC)
    WHERE tool_name IS NOT NULL;

-- Model mix and per-model spend.
CREATE INDEX ix_turns__tenant_model
    ON turns (tenant_id, model_provider, model_name, created_at DESC)
    WHERE model_name IS NOT NULL;

-- Safety review queue.
CREATE INDEX ix_turns__safety
    ON turns USING gin (safety_flags) WHERE safety_flags IS NOT NULL;

COMMENT ON COLUMN turns.content IS
    'pii_class=high; customer utterances; redacted per tenant retention policy';
```

**No FK from `turns` to `conversations`.** Both are partitioned by `created_at`,
and a turn's `created_at` is always ≥ its conversation's — so the FK would have
to be `(tenant_id, conversation_created_at, conversation_id)`, forcing us to
carry the parent's partition key on every child row. The referential guarantee
is worth less than the write cost on the hottest table in the system, and the
relationship is enforced in the repository, which never creates a turn outside a
conversation aggregate. This is a considered exception, not an oversight.

### 6.3 `transcripts`

Voice only. One row per conversation, holding word-level timing.

```sql
CREATE TABLE transcripts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL,
    conversation_created_at TIMESTAMPTZ NOT NULL,

    -- Full flat text, for search and for feeding a summariser.
    full_text           TEXT,

    -- Word-level detail:
    -- [{"w":"hello","s":120,"e":410,"c":0.98,"spk":"caller"}, ...]
    -- Stored as one JSONB document rather than a row per word: a 10-minute
    -- call is ~1500 words, and we always read the whole thing or none of it.
    -- 1500 rows per call × millions of calls is a table we would immediately
    -- regret; one TOASTed JSONB is a single I/O.
    words               JSONB,

    -- Diarization summary: [{"spk":"caller","ms":142000},{"spk":"agent","ms":98000}]
    speakers            JSONB,

    provider            TEXT,
    model               TEXT,
    language            TEXT,
    confidence_avg      NUMERIC(4,3),

    is_final            BOOLEAN NOT NULL DEFAULT TRUE,
    redacted_at         TIMESTAMPTZ,

    -- The config this row's search_vector was built with. Resolved from
    -- transcripts.language when known, else the tenant default. Stored so a
    -- config change is detectable and the row can be re-vectorised.
    text_search_config  REGCONFIG NOT NULL DEFAULT 'simple',

    -- Full-text search vector over the transcript. NOT a generated column —
    -- see §7.4.1 for why a per-row dynamic regconfig cannot be generated.
    search_vector       TSVECTOR,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_transcripts__confidence
        CHECK (confidence_avg IS NULL OR confidence_avg BETWEEN 0 AND 1)
);

-- Maintained by trigger, because to_tsvector(<column>, ...) is STABLE, not
-- IMMUTABLE, and a GENERATED column requires an IMMUTABLE expression (§7.4.1).
CREATE TRIGGER trg_transcripts_tsvector
    BEFORE INSERT OR UPDATE OF full_text, text_search_config ON transcripts
    FOR EACH ROW EXECUTE FUNCTION set_search_vector('full_text');

CREATE UNIQUE INDEX uq_transcripts__conversation
    ON transcripts (tenant_id, conversation_id);

-- "Find every call where someone mentioned 'refund'".
CREATE INDEX ix_transcripts__search
    ON transcripts USING gin (search_vector);

CREATE INDEX ix_transcripts__tenant_created
    ON transcripts (tenant_id, created_at DESC);

CREATE TRIGGER trg_transcripts_updated_at
    BEFORE UPDATE ON transcripts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN transcripts.full_text IS 'pii_class=high';
COMMENT ON COLUMN transcripts.words IS 'pii_class=high';
```

**On the tsvector config.** `transcripts.text_search_config` is resolved at write
time from `transcripts.language` (which the STT provider reports) when a matching
config exists, falling back to `tenants.default_text_search_config` and finally
to `'simple'`. The mechanism, the reason this is a trigger rather than a
`GENERATED` column, and the recall cost of `'simple'` are all in §7.4.1 — the
same machinery serves both tables, and neither should diverge from it.

Transcript search matters less than chunk search (it is a support and QA tool,
not a live retrieval path), so if the two ever need to diverge, this is the one
that can tolerate `'simple'`.

### 6.4 `call_recordings`

Audio lives in blob storage; only the pointer and metadata are in Postgres.

```sql
CREATE TABLE call_recordings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL,
    conversation_created_at TIMESTAMPTZ NOT NULL,

    -- StoragePort URI: 's3://bucket/tenant/<id>/conv/<id>/rec.ogg'. Never a
    -- presigned URL — those are minted at read time with a short TTL.
    storage_uri         TEXT NOT NULL,
    storage_provider    TEXT NOT NULL DEFAULT 's3',
    -- Which tenant KMS key encrypted the object at rest.
    kms_key_id          TEXT,

    format              TEXT NOT NULL DEFAULT 'ogg_opus',
    sample_rate_hz      INTEGER,
    channels            SMALLINT NOT NULL DEFAULT 1,
    duration_ms         INTEGER,
    size_bytes          BIGINT,
    sha256              BYTEA,

    -- Separate legs for a two-channel recording (caller / agent), which makes
    -- diarization trivial and lets us delete one side for privacy.
    track               TEXT NOT NULL DEFAULT 'mixed',

    -- Consent and legal basis, required in two-party-consent jurisdictions.
    consent_obtained    BOOLEAN NOT NULL DEFAULT FALSE,
    consent_method      TEXT,
    legal_basis         TEXT,

    -- Retention: set at creation from tenant policy; the reaper reads it.
    retain_until        TIMESTAMPTZ,
    deleted_from_storage_at TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_call_recordings__track
        CHECK (track IN ('mixed','inbound','outbound')),
    CONSTRAINT ck_call_recordings__duration CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT ck_call_recordings__size CHECK (size_bytes IS NULL OR size_bytes >= 0),
    CONSTRAINT ck_call_recordings__sha CHECK (sha256 IS NULL OR octet_length(sha256) = 32),
    CONSTRAINT ck_call_recordings__channels CHECK (channels BETWEEN 1 AND 8)
);

CREATE UNIQUE INDEX uq_call_recordings__conversation_track
    ON call_recordings (tenant_id, conversation_id, track);

-- Retention reaper: "objects past their retain_until that still exist".
CREATE INDEX ix_call_recordings__retention
    ON call_recordings (retain_until)
    WHERE retain_until IS NOT NULL AND deleted_from_storage_at IS NULL;

CREATE INDEX ix_call_recordings__tenant_created
    ON call_recordings (tenant_id, created_at DESC);

CREATE TRIGGER trg_call_recordings_updated_at
    BEFORE UPDATE ON call_recordings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN call_recordings.storage_uri IS
    'pii_class=high; audio contains raw customer voice';
```

### 6.5 `conversation_outcomes`

Contract §7's `Outcome`. Separate from `conversations` because a single
conversation can produce several outcomes (booked *and* transferred), and
because outcome classification is often asynchronous — a post-call LLM pass.

```sql
CREATE TABLE conversation_outcomes (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL,
    conversation_created_at TIMESTAMPTZ NOT NULL,

    kind                outcome_kind NOT NULL,

    -- Vertical-specific sub-classification from the manifest's vocabulary,
    -- e.g. 'appointment_scheduled', 'prescription_refill', 'reservation_modified'.
    subtype             TEXT,

    -- What it produced.
    booking_id          UUID,
    waitlist_entry_id   UUID,
    transferred_to      TEXT,

    -- Post-hoc analysis.
    summary             TEXT,
    -- Structured extraction the agent or a post-call pass produced.
    extracted_data      JSONB NOT NULL DEFAULT '{}'::jsonb,
    intents             TEXT[] NOT NULL DEFAULT '{}',
    sentiment           NUMERIC(3,2),       -- -1.00 .. 1.00
    csat_score          SMALLINT,           -- 1..5 if collected
    resolution_confidence NUMERIC(3,2),

    -- Who decided. 'agent' at runtime, 'classifier' post-hoc, 'human' on review.
    determined_by       TEXT NOT NULL DEFAULT 'agent',
    reviewed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at         TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_conversation_outcomes__bookings
        FOREIGN KEY (tenant_id, booking_id)
        REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT ck_conversation_outcomes__sentiment
        CHECK (sentiment IS NULL OR sentiment BETWEEN -1 AND 1),
    CONSTRAINT ck_conversation_outcomes__csat
        CHECK (csat_score IS NULL OR csat_score BETWEEN 1 AND 5),
    CONSTRAINT ck_conversation_outcomes__confidence
        CHECK (resolution_confidence IS NULL OR resolution_confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_conversation_outcomes__determined_by
        CHECK (determined_by IN ('agent','classifier','human','system')),
    CONSTRAINT ck_conversation_outcomes__booked_has_booking
        CHECK (kind <> 'booked' OR booking_id IS NOT NULL)
);

-- One outcome of each kind per conversation; a re-classification updates.
CREATE UNIQUE INDEX uq_conversation_outcomes__conversation_kind
    ON conversation_outcomes (tenant_id, conversation_id, kind);

-- The core analytics query: "resolution mix over the last 30 days".
CREATE INDEX ix_conversation_outcomes__tenant_kind_created
    ON conversation_outcomes (tenant_id, kind, created_at DESC);

CREATE INDEX ix_conversation_outcomes__tenant_booking
    ON conversation_outcomes (tenant_id, booking_id) WHERE booking_id IS NOT NULL;

CREATE INDEX ix_conversation_outcomes__intents
    ON conversation_outcomes USING gin (intents);

-- Human review queue: low-confidence, unreviewed.
CREATE INDEX ix_conversation_outcomes__review_queue
    ON conversation_outcomes (tenant_id, resolution_confidence)
    WHERE reviewed_at IS NULL AND resolution_confidence IS NOT NULL;

CREATE TRIGGER trg_conversation_outcomes_updated_at
    BEFORE UPDATE ON conversation_outcomes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

---

## 7. Knowledge (RAG)

Four-level hierarchy: a **source** (a website, a Drive folder, an uploaded PDF)
produces **documents**; each document has immutable **versions**; each version is
split into **chunks** carrying embeddings. Versioning at the document level is
what makes atomic reindexing possible — `VectorStorePort.delete_by_version`
in contract §5 is precisely this.

### 7.1 `knowledge_sources`

```sql
CREATE TABLE knowledge_sources (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    location_id         UUID,

    name                TEXT NOT NULL,
    description         TEXT,

    -- 'upload' | 'web_crawl' | 'sitemap' | 'gdrive' | 'notion' | 'confluence'
    -- | 'api' | 'connector' | 'manual'
    source_type         TEXT NOT NULL,

    -- Type-specific config: crawl depth, include/exclude globs, folder ids,
    -- CSS selectors. Validated against a per-type Pydantic schema.
    config              JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- For connector-backed sources.
    connector_id        UUID,
    -- Credentials live in tenant_secrets, referenced not embedded.
    secret_id           UUID,

    -- ── Retrieval behaviour ────────────────────────────────────────────────
    -- Chunks from this source can be restricted to certain agents or
    -- verticals, so a "pricing" source is not retrieved during a clinical call.
    scope_tags          TEXT[] NOT NULL DEFAULT '{}',
    -- Boost applied to similarity scores from this source. A tenant's official
    -- policy doc should outrank a scraped forum page.
    priority_weight     NUMERIC(4,3) NOT NULL DEFAULT 1.000,

    -- Contract §8: knowledge-base content is UNTRUSTED INPUT. This flag records
    -- whether the source is tenant-authored (lower risk) or externally
    -- scraped (higher risk). The prompt assembler wraps high-risk content in
    -- stricter delimiters and never lets it carry instructions.
    trust_level         TEXT NOT NULL DEFAULT 'untrusted',

    -- ── Sync ───────────────────────────────────────────────────────────────
    sync_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    sync_cron           TEXT,
    last_sync_at        TIMESTAMPTZ,
    next_sync_at        TIMESTAMPTZ,
    last_sync_status    ingestion_status,
    last_error          TEXT,

    -- Rolled-up counts for the admin UI.
    document_count      INTEGER NOT NULL DEFAULT 0,
    chunk_count         INTEGER NOT NULL DEFAULT 0,
    total_bytes         BIGINT NOT NULL DEFAULT 0,

    -- Chunking parameters. Kept per source, because a legal PDF and a FAQ page
    -- want different strategies.
    chunk_strategy      TEXT NOT NULL DEFAULT 'recursive',
    chunk_size_tokens   INTEGER NOT NULL DEFAULT 512,
    chunk_overlap_tokens INTEGER NOT NULL DEFAULT 64,

    -- Embedding config, snapshotted so a model change is a deliberate reindex.
    embedding_provider  TEXT,
    embedding_model     TEXT,
    embedding_dimensions SMALLINT,

    -- Lexical-search language for this source's content (§7.4.1). Per-source
    -- rather than only per-tenant because a single tenant plausibly holds an
    -- English policy manual and a Spanish patient-facing FAQ, and stemming
    -- one with the other's rules degrades both. NULL = inherit
    -- tenants.default_text_search_config.
    text_search_config  REGCONFIG,

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_knowledge_sources__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_knowledge_sources__secrets
        FOREIGN KEY (tenant_id, secret_id)
        REFERENCES tenant_secrets (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT ck_knowledge_sources__source_type
        CHECK (source_type IN ('upload','web_crawl','sitemap','gdrive','notion',
                               'confluence','api','connector','manual','faq')),
    CONSTRAINT ck_knowledge_sources__trust
        CHECK (trust_level IN ('trusted','untrusted')),
    CONSTRAINT ck_knowledge_sources__chunk_size
        CHECK (chunk_size_tokens BETWEEN 64 AND 8192),
    CONSTRAINT ck_knowledge_sources__overlap
        CHECK (chunk_overlap_tokens >= 0 AND chunk_overlap_tokens < chunk_size_tokens),
    CONSTRAINT ck_knowledge_sources__weight
        CHECK (priority_weight BETWEEN 0 AND 10),
    CONSTRAINT ck_knowledge_sources__strategy
        CHECK (chunk_strategy IN ('recursive','semantic','markdown_header',
                                  'fixed','sentence','page'))
);

ALTER TABLE knowledge_sources
    ADD CONSTRAINT uq_knowledge_sources__tenant_id_id UNIQUE (tenant_id, id);

CREATE UNIQUE INDEX uq_knowledge_sources__tenant_name
    ON knowledge_sources (tenant_id, name) WHERE deleted_at IS NULL;

CREATE INDEX ix_knowledge_sources__tenant_active
    ON knowledge_sources (tenant_id, source_type) WHERE is_active AND deleted_at IS NULL;

-- Sync scheduler sweep (privileged, cross-tenant).
CREATE INDEX ix_knowledge_sources__next_sync
    ON knowledge_sources (next_sync_at)
    WHERE sync_enabled AND is_active AND deleted_at IS NULL;

CREATE INDEX ix_knowledge_sources__scope_tags
    ON knowledge_sources USING gin (scope_tags);

CREATE TRIGGER trg_knowledge_sources_updated_at
    BEFORE UPDATE ON knowledge_sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 7.2 `documents`

The stable identity of a piece of content. Content itself lives in versions.

```sql
CREATE TABLE documents (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id           UUID NOT NULL,

    -- Stable external identity within the source: a URL, a Drive file id, an
    -- uploaded filename. This is the upsert key for re-crawls.
    external_id         TEXT NOT NULL,

    title               TEXT,
    uri                 TEXT,
    mime_type           TEXT,
    language            TEXT,

    -- Which version is live for retrieval. Exactly one at a time.
    current_version_id  UUID,

    status              document_status NOT NULL DEFAULT 'pending',

    -- Arbitrary metadata used as retrieval filters: department, effective
    -- date, product line, audience.
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Manual overrides.
    is_excluded         BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason    TEXT,
    priority_weight     NUMERIC(4,3),       -- overrides source weight

    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when a re-crawl no longer finds the document; its chunks are then
    -- retired without losing history.
    disappeared_at      TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_documents__sources
        FOREIGN KEY (tenant_id, source_id)
        REFERENCES knowledge_sources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_documents__weight
        CHECK (priority_weight IS NULL OR priority_weight BETWEEN 0 AND 10)
);

ALTER TABLE documents
    ADD CONSTRAINT uq_documents__tenant_id_id UNIQUE (tenant_id, id);

-- Re-crawl upsert key.
CREATE UNIQUE INDEX uq_documents__source_external
    ON documents (tenant_id, source_id, external_id);

CREATE INDEX ix_documents__tenant_source_status
    ON documents (tenant_id, source_id, status);

CREATE INDEX ix_documents__metadata
    ON documents USING gin (metadata jsonb_path_ops);

-- Stale-document sweep after a crawl: rows whose last_seen_at predates the run.
CREATE INDEX ix_documents__last_seen
    ON documents (tenant_id, source_id, last_seen_at)
    WHERE disappeared_at IS NULL;

CREATE TRIGGER trg_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 7.3 `document_versions`

Immutable content snapshots. This is the unit of atomic reindex.

```sql
CREATE TABLE document_versions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id         UUID NOT NULL,

    version_number      INTEGER NOT NULL,

    -- Content hash: if unchanged, we skip re-chunking and re-embedding
    -- entirely. On a nightly 10k-page crawl this is the difference between
    -- a $200 embedding bill and a $2 one.
    content_sha256      BYTEA NOT NULL,

    -- Extracted plain text. Large documents are TOASTed; very large ones
    -- (>1 MB) are offloaded to blob storage and content_uri is set instead.
    content             TEXT,
    content_uri         TEXT,
    content_bytes       BIGINT,
    token_count         INTEGER,

    -- Extraction provenance: which parser, which version, what it struggled with.
    extractor           TEXT,
    extractor_version   TEXT,
    extraction_warnings JSONB,

    -- Indexing lifecycle. A version is only retrievable once 'indexed'.
    status              document_status NOT NULL DEFAULT 'pending',
    chunk_count         INTEGER NOT NULL DEFAULT 0,
    indexed_at          TIMESTAMPTZ,
    superseded_at       TIMESTAMPTZ,

    -- Embedding config snapshot for this version's chunks. A model change
    -- creates a NEW version rather than mutating chunks in place, so retrieval
    -- never mixes embedding spaces.
    embedding_provider  TEXT,
    embedding_model     TEXT,
    embedding_dimensions SMALLINT,

    ingestion_job_id    UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_document_versions__documents
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES documents (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_document_versions__sha CHECK (octet_length(content_sha256) = 32),
    CONSTRAINT ck_document_versions__version_positive CHECK (version_number >= 1),
    CONSTRAINT ck_document_versions__content_present
        CHECK (content IS NOT NULL OR content_uri IS NOT NULL),
    CONSTRAINT ck_document_versions__indexed_coherent
        CHECK (status <> 'indexed' OR indexed_at IS NOT NULL)
);

ALTER TABLE document_versions
    ADD CONSTRAINT uq_document_versions__tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE documents
    ADD CONSTRAINT fk_documents__current_version
    FOREIGN KEY (tenant_id, current_version_id)
    REFERENCES document_versions (tenant_id, id) ON DELETE SET NULL;

CREATE UNIQUE INDEX uq_document_versions__document_number
    ON document_versions (tenant_id, document_id, version_number);

-- Skip-if-unchanged check during a crawl.
CREATE INDEX ix_document_versions__content_hash
    ON document_versions (tenant_id, document_id, content_sha256);

-- Reindex worker: "versions awaiting chunking".
CREATE INDEX ix_document_versions__pending
    ON document_versions (tenant_id, status, created_at)
    WHERE status IN ('pending','failed');

CREATE INDEX ix_document_versions__job
    ON document_versions (tenant_id, ingestion_job_id)
    WHERE ingestion_job_id IS NOT NULL;
```

### 7.4 `chunks` — with pgvector

```sql
-- Dimensionality decision: 1536.
--   text-embedding-3-small  = 1536 native
--   text-embedding-3-large  = 3072, but supports Matryoshka truncation to 1536
--   voyage-3               = 1024, zero-padded to 1536
-- Fixing ONE column dimension lets a single HNSW index serve every tenant.
-- The alternative (a column per model, or a table per dimension) multiplies
-- index count and makes cross-model migration a schema change. Normalising to
-- 1536 with truncation/padding costs a few points of recall on the padded
-- models and buys a vastly simpler operational story. Revisit if a model we
-- want cannot be projected into 1536 without unacceptable loss.
CREATE TABLE chunks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Denormalized ancestry so retrieval filters need no joins. The vector
    -- search is the expensive part; every join we can avoid inside the filter
    -- is real latency saved on every single agent turn.
    source_id           UUID NOT NULL,
    document_id         UUID NOT NULL,
    document_version_id UUID NOT NULL,

    chunk_index         INTEGER NOT NULL,

    content             TEXT NOT NULL,
    -- Extra context prepended at retrieval time but NOT embedded: document
    -- title, section heading breadcrumb. Improves LLM grounding without
    -- polluting the vector.
    context_header      TEXT,
    token_count         INTEGER,

    -- ── The vector ─────────────────────────────────────────────────────────
    embedding           vector(1536),
    embedding_model     TEXT,
    embedded_at         TIMESTAMPTZ,

    -- ── Hybrid search: lexical half ────────────────────────────────────────
    -- BM25-ish keyword matching catches exact terms (part numbers, drug names,
    -- policy codes) that embeddings routinely miss. Fusing the two with RRF
    -- beats either alone; this is why the column exists rather than relying
    -- on the vector alone.
    --
    -- text_search_config drives stemming. Resolved at chunk-write time from
    -- knowledge_sources.text_search_config (which itself defaults from the
    -- tenant). Stored per row so a source's language can change without
    -- invalidating chunks written under the old config, and so a re-vectorise
    -- sweep can find exactly the rows that need it. See §7.4.1.
    text_search_config  REGCONFIG NOT NULL DEFAULT 'simple',
    -- NOT a generated column: to_tsvector(<column>, ...) is STABLE, and a
    -- GENERATED column requires an IMMUTABLE expression. Trigger-maintained.
    search_vector       TSVECTOR,

    -- Retrieval filters, denormalized from source and document.
    scope_tags          TEXT[] NOT NULL DEFAULT '{}',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority_weight     NUMERIC(4,3) NOT NULL DEFAULT 1.000,

    -- Only chunks of the current version are retrievable. Maintained when a
    -- version is superseded; the partial index below makes this the cheap path.
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,

    -- Provenance for citations rendered back to the caller.
    page_number         INTEGER,
    char_start          INTEGER,
    char_end            INTEGER,
    heading_path        TEXT[],

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_chunks__document_versions
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_versions (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_chunks__documents
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES documents (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_chunks__sources
        FOREIGN KEY (tenant_id, source_id)
        REFERENCES knowledge_sources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_chunks__index_nonneg CHECK (chunk_index >= 0),
    CONSTRAINT ck_chunks__content_nonempty CHECK (length(content) > 0),
    CONSTRAINT ck_chunks__char_order
        CHECK (char_end IS NULL OR char_start IS NULL OR char_end >= char_start),
    CONSTRAINT ck_chunks__weight CHECK (priority_weight BETWEEN 0 AND 10)
);

CREATE UNIQUE INDEX uq_chunks__version_index
    ON chunks (tenant_id, document_version_id, chunk_index);

CREATE TRIGGER trg_chunks_tsvector
    BEFORE INSERT OR UPDATE OF content, text_search_config ON chunks
    FOR EACH ROW EXECUTE FUNCTION set_search_vector('content');
```

### 7.4.1 Per-tenant text-search configuration

The lexical half of hybrid search must stem in the right language. `'english'`
maps *booking*, *booked*, and *bookings* to one lexeme; run over German or
Turkish text it produces nonsense, and over Chinese it produces nothing useful
at all. A single hardcoded config blocks every non-English market.

**Why the original `'simple'` was a hedge, and what it costs.** `'simple'`
lowercases and splits on non-word characters, and does nothing else — no
stemming, no stop-word removal. It is language-neutral, which is why it was the
safe default for a multi-language product before this column existed. The cost
is real and measurable: a query for *"booking"* does not match a chunk
containing only *"booked"*, so lexical recall drops noticeably on natural-prose
knowledge bases. It only performs well on the exact-token cases (part numbers,
drug names, policy codes) that motivated the column in the first place. Keeping
`'simple'` as a permanent default would have meant shipping a hybrid search
whose lexical half underperforms for every tenant, English or not.

**The constraint that shapes the solution.** The obvious implementation —

```sql
-- DOES NOT WORK
search_vector TSVECTOR GENERATED ALWAYS AS (
    to_tsvector(text_search_config, COALESCE(content, ''))
) STORED
```

— is rejected by PostgreSQL: `ERROR: generation expression is not immutable`.
The two-argument `to_tsvector(regconfig, text)` is `IMMUTABLE`, but a `GENERATED`
column may only reference *other columns of the same row through an immutable
expression tree*, and PostgreSQL conservatively treats a `regconfig`-typed
**column** argument as non-immutable because the config it names is a mutable
catalogue object: an `ALTER TEXT SEARCH CONFIGURATION` would silently invalidate
every stored value and every index built on it. That is a correct refusal, not
an arbitrary one.

Three options, and why we chose the third:

| Option | Verdict |
|---|---|
| Keep `'simple'` everywhere | Rejected. Blocks non-English markets and degrades English recall for no benefit. |
| Per-language partial indexes over a generated column per config | Rejected. Needs one column and one GIN index per supported language; adding Portuguese becomes a migration that rewrites the largest table in the system. |
| **`REGCONFIG` column + trigger-maintained `TSVECTOR`** | **Chosen.** One column, one index, adding a language is a data change. |

```sql
-- Shared by transcripts and chunks. The source column name is passed as a
-- trigger argument so one function serves both tables.
CREATE OR REPLACE FUNCTION set_search_vector()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    src_col TEXT := TG_ARGV[0];
    src_val TEXT;
    cfg     REGCONFIG;
BEGIN
    EXECUTE format('SELECT ($1).%I::text', src_col) INTO src_val USING NEW;
    cfg := NEW.text_search_config;

    NEW.search_vector := to_tsvector(cfg, COALESCE(src_val, ''));
    RETURN NEW;
END;
$$;
```

**What this costs relative to a generated column.** A PL/pgSQL trigger with a
dynamic `EXECUTE` per row is roughly 10–20 µs slower than an inline generated
expression. On `chunks` that is paid once per chunk at ingestion — a batch
operation already dominated by embedding API latency measured in hundreds of
milliseconds — so it is unmeasurable in practice. It is *not* on any read path.
Had this been a per-turn write we would have chosen differently.

**Resolution order**, applied in the ingestion pipeline, not in SQL:
`knowledge_sources.text_search_config` → `tenants.default_text_search_config` →
`'simple'`. The resolved value is written to `chunks.text_search_config` on the
row, so the vector and the config that built it always travel together.

The index is unchanged — one GIN index serves every language, because the
lexemes of different configs simply occupy disjoint regions of the same index:

```sql
CREATE INDEX ix_chunks__search_vector
    ON chunks USING gin (search_vector) WHERE is_current;
```

**Querying must use the matching config.** A `to_tsquery('english', …)` against
a vector built with `'german'` silently returns nothing. The retrieval layer
therefore passes the config explicitly and filters on it:

```sql
-- Lexical half of hybrid retrieval, config-aware.
SELECT c.id, c.content,
       ts_rank_cd(c.search_vector, q.query) AS lexical_score
  FROM chunks c
  CROSS JOIN LATERAL (
      SELECT websearch_to_tsquery(c.text_search_config, $2) AS query
  ) q
 WHERE c.tenant_id = $1
   AND c.is_current
   AND c.search_vector @@ q.query
 ORDER BY lexical_score DESC
 LIMIT $3;
```

**Changing a source's language** sets `knowledge_sources.text_search_config` and
enqueues a re-vectorise job. Because `chunks.text_search_config` is stored per
row, the job's predicate is exact and cheap — and note it does **not** require
re-embedding, since the vector half is language-agnostic:

```sql
-- Re-vectorise only the rows whose config drifted from their source's.
UPDATE chunks c
   SET text_search_config = ks.text_search_config
  FROM knowledge_sources ks
 WHERE ks.tenant_id = c.tenant_id
   AND ks.id = c.source_id
   AND c.text_search_config IS DISTINCT FROM ks.text_search_config
   AND c.tenant_id = $1
   AND c.id IN (SELECT id FROM chunks
                 WHERE tenant_id = $1 LIMIT 5000);
-- The BEFORE UPDATE trigger rebuilds search_vector for each row touched.
-- Run in batches until zero rows affected.
```

> **Doing this now is the whole point.** Adding a `REGCONFIG` column to an empty
> `chunks` table is one migration. Adding it after production data exists means
> rebuilding a GIN index over the largest content table in the system while it
> serves live retrieval traffic. The same applies to `transcripts`.

> **Cross-document note.** `03-AI-ENGINE.md` §7 illustrates hybrid retrieval
> against a `chunks.tsv` column generated with a hardcoded `'english'`. That
> column does not exist; the retrieval target is `chunks.search_vector` as
> defined here, and the config comes from `chunks.text_search_config` rather
> than a literal. The query shape in that document is otherwise correct — RRF
> fusion over a vector half and a lexical half — and only the config argument
> and column name change. This resolves that document's open question #2.

**Supported configs** are whatever the server ships (`\dF` lists ~29, including
`english`, `spanish`, `french`, `german`, `portuguese`, `italian`, `dutch`,
`russian`, `swedish`, `turkish`, plus `simple`). Languages Postgres has no
snowball stemmer for — Chinese, Japanese, Korean, Thai — fall back to `'simple'`
and lean on the vector half of hybrid search, which handles them well. That
fallback is a documented product limitation, not a silent degradation: the
admin UI shows which config a source resolved to.

**The HNSW index.**

```sql
-- ════════════════════════════════════════════════════════════════════════════
-- VECTOR INDEX
-- ════════════════════════════════════════════════════════════════════════════
-- Operator class: vector_cosine_ops. Every embedding model we support emits
-- L2-normalised vectors, for which cosine and inner product are rank-
-- equivalent; cosine is chosen because it is the documented default for these
-- models and because it is robust if a future provider does NOT normalise.
--
-- m = 16            : edges per node. pgvector's default; the recall/size knee
--                     for 1536-d text embeddings. m=32 buys ~1-2% recall for
--                     ~2x index size and ~2x build time — not worth it here.
-- ef_construction=64: pgvector's default is 64. We raise to 128 because build
--                     happens once and query recall improves measurably.
--
-- Query-time ef_search is set per statement (see below), not baked in.
CREATE INDEX ix_chunks__embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE is_current AND embedding IS NOT NULL;
```

> **The multi-tenant HNSW problem, stated honestly.** HNSW is a single global
> graph. It does not partition by `tenant_id`. A query with
> `WHERE tenant_id = $1 ORDER BY embedding <=> $2 LIMIT 10` performs a
> *post-filter*: pgvector walks the graph, and rows failing the filter are
> discarded. With many tenants sharing the index, the `ef_search` candidate pool
> can be exhausted by other tenants' rows before 10 of *this* tenant's survive,
> and recall collapses.
>
> Three mitigations, applied together:
>
> 1. **Raise `ef_search` proportionally.** Set `hnsw.ef_search` per query to
>    roughly `k × 10 × (1 / tenant_share)` clamped to [40, 1000]. The repository
>    computes this from a cached per-tenant chunk-count ratio.
> 2. **Use pgvector's iterative scan** (0.8+): `SET LOCAL hnsw.iterative_scan =
>    'relaxed_order'` makes the scan *continue* fetching candidates until `LIMIT`
>    is satisfied or `hnsw.max_scan_tuples` is hit, instead of stopping at
>    `ef_search`. This turns a recall cliff into a latency gradient, which is
>    the trade we want.
> 3. **Partial indexes for whale tenants.** A tenant holding >5% of all chunks
>    gets its own partial HNSW index:
>    `CREATE INDEX ... ON chunks USING hnsw (embedding vector_cosine_ops)
>     WHERE tenant_id = '<uuid>' AND is_current;`
>    The planner picks it automatically for that tenant's queries, giving a
>    private graph with no cross-tenant dilution. This is created by an
>    operational playbook, not by migration, and is exactly the signal that the
>    tenant should be moved to a `VectorStorePort` backend of its own.
>
> This is also the concrete trigger for contract §1's "pgvector initially;
> `VectorStorePort` abstracts it". When mitigation 3 is needed for more than a
> handful of tenants, we move to a dedicated vector store. The port means that
> is an adapter swap, not a rewrite.

```sql
-- Lexical half of hybrid search.
CREATE INDEX ix_chunks__search_vector
    ON chunks USING gin (search_vector) WHERE is_current;

-- Metadata pre-filtering ("only chunks tagged 'billing'").
CREATE INDEX ix_chunks__scope_tags
    ON chunks USING gin (scope_tags) WHERE is_current;

CREATE INDEX ix_chunks__metadata
    ON chunks USING gin (metadata jsonb_path_ops) WHERE is_current;

-- delete_by_version: retire an entire version's chunks in one statement.
CREATE INDEX ix_chunks__tenant_version
    ON chunks (tenant_id, document_version_id);

CREATE INDEX ix_chunks__tenant_document
    ON chunks (tenant_id, document_id) WHERE is_current;

-- Backfill worker: chunks awaiting embedding.
CREATE INDEX ix_chunks__unembedded
    ON chunks (tenant_id, created_at) WHERE embedding IS NULL;
```

**Storage note.** `vector(1536)` is 6 KB per row (4 bytes × 1536 + header). At
the default `EXTENDED` storage this gets TOASTed and compressed, which *hurts* —
every index scan then needs a TOAST fetch. Set the column to `PLAIN` storage so
vectors stay inline:

```sql
ALTER TABLE chunks ALTER COLUMN embedding SET STORAGE PLAIN;
```

This forces a page size consideration: 6 KB vectors mean roughly one chunk row
per 8 KB page. That is the accepted cost of inline vectors and is why `content`
(the large text) stays `EXTENDED` and gets TOASTed instead.

### 7.5 `ingestion_jobs`

```sql
CREATE TABLE ingestion_jobs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id           UUID NOT NULL,

    -- 'full_sync' | 'incremental' | 'single_document' | 'reindex' | 'delete'
    job_type            TEXT NOT NULL,
    status              ingestion_status NOT NULL DEFAULT 'queued',

    -- Temporal correlation. Contract §3: workflow_id is
    -- '{tenant_id}:ingestion:{source_id}'.
    workflow_id         TEXT,
    workflow_run_id     TEXT,
    task_queue          TEXT NOT NULL DEFAULT 'ingestion',

    -- Progress, for a live UI.
    documents_discovered INTEGER NOT NULL DEFAULT 0,
    documents_processed INTEGER NOT NULL DEFAULT 0,
    documents_skipped   INTEGER NOT NULL DEFAULT 0,
    documents_failed    INTEGER NOT NULL DEFAULT 0,
    chunks_created      INTEGER NOT NULL DEFAULT 0,
    chunks_deleted      INTEGER NOT NULL DEFAULT 0,
    tokens_embedded     BIGINT NOT NULL DEFAULT 0,

    -- Cost attribution for the embedding spend this job incurred.
    cost                NUMERIC(12,4) NOT NULL DEFAULT 0,
    cost_currency       CHAR(3) NOT NULL DEFAULT 'USD',

    -- Per-document failures, capped at 100 entries to bound row size.
    errors              JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_error          TEXT,

    -- Idempotency for a retried trigger.
    idempotency_key     TEXT,

    triggered_by        TEXT NOT NULL DEFAULT 'schedule',
    triggered_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    queued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_ingestion_jobs__sources
        FOREIGN KEY (tenant_id, source_id)
        REFERENCES knowledge_sources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_ingestion_jobs__job_type
        CHECK (job_type IN ('full_sync','incremental','single_document','reindex','delete')),
    CONSTRAINT ck_ingestion_jobs__triggered_by
        CHECK (triggered_by IN ('schedule','manual','webhook','api','system')),
    CONSTRAINT ck_ingestion_jobs__errors_array
        CHECK (jsonb_typeof(errors) = 'array'),
    CONSTRAINT ck_ingestion_jobs__finished_coherent
        CHECK (status NOT IN ('succeeded','failed','cancelled') OR finished_at IS NOT NULL),
    CONSTRAINT ck_ingestion_jobs__counts_nonneg
        CHECK (documents_processed >= 0 AND chunks_created >= 0 AND tokens_embedded >= 0)
);

ALTER TABLE ingestion_jobs
    ADD CONSTRAINT uq_ingestion_jobs__tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE document_versions
    ADD CONSTRAINT fk_document_versions__jobs
    FOREIGN KEY (tenant_id, ingestion_job_id)
    REFERENCES ingestion_jobs (tenant_id, id) ON DELETE SET NULL;

CREATE UNIQUE INDEX uq_ingestion_jobs__idempotency
    ON ingestion_jobs (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Job list for a source, newest first.
CREATE INDEX ix_ingestion_jobs__tenant_source_created
    ON ingestion_jobs (tenant_id, source_id, created_at DESC);

-- "Is anything running for this tenant" — enforces a per-tenant concurrency cap.
CREATE INDEX ix_ingestion_jobs__tenant_active
    ON ingestion_jobs (tenant_id, status) WHERE status IN ('queued','running');

-- Stuck-job detector (privileged sweep).
CREATE INDEX ix_ingestion_jobs__running_since
    ON ingestion_jobs (started_at) WHERE status = 'running';

CREATE TRIGGER trg_ingestion_jobs_updated_at
    BEFORE UPDATE ON ingestion_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

---

## 8. Connectors

Contract §4's escape hatch and §5's `ConnectorPort`. A connector reaches a
tenant's own system: their calendar, CRM, PMS, EMR, or an arbitrary
webhook/MCP endpoint. Egress must be allow-listed and SSRF-guarded (§8 of the
contract), and the allow-list lives here.

### 8.1 `connector_definitions`

Platform catalogue of connector *types*. First-party (Google Calendar,
Salesforce, Toast) and generic (REST, GraphQL, SQL, MCP, webhook).

```sql
CREATE TABLE connector_definitions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    code                TEXT NOT NULL,      -- 'google_calendar','salesforce','mcp','rest'
    name                TEXT NOT NULL,
    description         TEXT,
    category            TEXT NOT NULL,      -- 'calendar','crm','pms','emr','pos','generic'

    vendor              TEXT,
    icon_url            TEXT,
    docs_url            TEXT,

    -- Which adapter class implements it. A REGISTERED name resolved through
    -- the DI container — never an importable path supplied by a tenant.
    adapter_key         TEXT NOT NULL,

    auth_kind           connector_auth_kind NOT NULL DEFAULT 'api_key',

    -- OAuth2 metadata for the ones that need it.
    oauth_authorize_url TEXT,
    oauth_token_url     TEXT,
    oauth_scopes        TEXT[] NOT NULL DEFAULT '{}',
    oauth_pkce_required BOOLEAN NOT NULL DEFAULT TRUE,

    -- JSON Schema for tenant_connectors.config. The admin UI renders a form
    -- from this; the API validates against it.
    config_schema       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- JSON Schema for the credential payload before encryption.
    credential_schema   JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Capability declaration, so the booking engine and agent know what this
    -- connector can do without probing:
    -- {"read_events":true,"write_events":true,"webhooks":true,
    --  "free_busy":true,"push_sync":false}
    capabilities        JSONB NOT NULL DEFAULT '{}'::jsonb,

    supported_directions sync_direction[] NOT NULL DEFAULT '{inbound}',

    -- Default egress allow-list for this connector type. A tenant's list is
    -- the intersection of this and their own.
    default_egress_hosts TEXT[] NOT NULL DEFAULT '{}',

    -- Rate limits the vendor imposes, used to size our own throttle.
    default_rate_limit_rpm INTEGER,

    is_beta             BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    min_plan_code       TEXT,               -- entitlement gate

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_connector_definitions__code
        CHECK (code ~ '^[a-z][a-z0-9_]{1,47}$'),
    CONSTRAINT ck_connector_definitions__oauth_shape CHECK (
        auth_kind <> 'oauth2'
        OR (oauth_authorize_url IS NOT NULL AND oauth_token_url IS NOT NULL)
    ),
    CONSTRAINT ck_connector_definitions__category
        CHECK (category IN ('calendar','crm','pms','emr','pos','ecommerce','payments',
                            'messaging','storage','generic','custom'))
);

CREATE UNIQUE INDEX uq_connector_definitions__code ON connector_definitions (code);
CREATE INDEX ix_connector_definitions__category
    ON connector_definitions (category) WHERE is_active;

CREATE TRIGGER trg_connector_definitions_updated_at
    BEFORE UPDATE ON connector_definitions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 8.2 `tenant_connectors`

A tenant's configured instance of a connector definition.

```sql
CREATE TABLE tenant_connectors (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    connector_definition_id UUID NOT NULL REFERENCES connector_definitions(id) ON DELETE RESTRICT,

    location_id         UUID,               -- NULL = tenant-wide

    name                TEXT NOT NULL,      -- "Front desk Google Calendar"

    -- Instance config, validated against connector_definitions.config_schema.
    -- Base URLs, calendar ids, object mappings, field maps.
    config              JSONB NOT NULL DEFAULT '{}'::jsonb,

    direction           sync_direction NOT NULL DEFAULT 'bidirectional',

    -- ── Egress control (contract §8) ───────────────────────────────────────
    -- Hostnames this connector instance may reach. The HTTP client resolves
    -- every request against this list AND against an IP-range denylist
    -- (no 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fc00::/7)
    -- AFTER DNS resolution, to defeat DNS-rebinding SSRF.
    allowed_egress_hosts TEXT[] NOT NULL DEFAULT '{}',
    -- Explicit opt-in for private ranges, for on-prem VPN-connected systems.
    allow_private_ranges BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Resilience knobs enforced by the adapter wrapper ───────────────────
    timeout_ms          INTEGER NOT NULL DEFAULT 15000,
    max_retries         SMALLINT NOT NULL DEFAULT 3,
    rate_limit_rpm      INTEGER,
    circuit_breaker_threshold SMALLINT NOT NULL DEFAULT 5,
    circuit_open_until  TIMESTAMPTZ,

    -- ── Sync scheduling ────────────────────────────────────────────────────
    sync_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    sync_cron           TEXT,
    sync_interval_seconds INTEGER,
    -- Webhook-driven connectors register a callback; we store the shared
    -- secret reference used to verify inbound signatures.
    webhook_secret_id   UUID,
    webhook_path_token  TEXT,               -- unguessable path segment

    health              sync_health NOT NULL DEFAULT 'healthy',
    last_error          TEXT,
    last_error_at       TIMESTAMPTZ,
    consecutive_failures SMALLINT NOT NULL DEFAULT 0,

    is_enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    version             INTEGER NOT NULL DEFAULT 1,

    CONSTRAINT fk_tenant_connectors__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_tenant_connectors__webhook_secret
        FOREIGN KEY (tenant_id, webhook_secret_id)
        REFERENCES tenant_secrets (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT ck_tenant_connectors__timeout
        CHECK (timeout_ms BETWEEN 100 AND 300000),
    CONSTRAINT ck_tenant_connectors__retries CHECK (max_retries BETWEEN 0 AND 10),
    CONSTRAINT ck_tenant_connectors__interval
        CHECK (sync_interval_seconds IS NULL OR sync_interval_seconds >= 60),
    CONSTRAINT ck_tenant_connectors__schedule_exclusive
        CHECK (NOT (sync_cron IS NOT NULL AND sync_interval_seconds IS NOT NULL)),
    -- Egress must be explicitly configured; an empty list is a hard fail, not
    -- an implicit "allow all".
    CONSTRAINT ck_tenant_connectors__egress_present
        CHECK (NOT is_enabled OR array_length(allowed_egress_hosts, 1) >= 1)
);

ALTER TABLE tenant_connectors
    ADD CONSTRAINT uq_tenant_connectors__tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE availability_exceptions
    ADD CONSTRAINT fk_availability_exceptions__connectors
    FOREIGN KEY (tenant_id, connector_id)
    REFERENCES tenant_connectors (tenant_id, id) ON DELETE SET NULL;

ALTER TABLE knowledge_sources
    ADD CONSTRAINT fk_knowledge_sources__connectors
    FOREIGN KEY (tenant_id, connector_id)
    REFERENCES tenant_connectors (tenant_id, id) ON DELETE SET NULL;

CREATE UNIQUE INDEX uq_tenant_connectors__tenant_name
    ON tenant_connectors (tenant_id, name) WHERE deleted_at IS NULL;

-- Inbound webhook routing: the path token is globally unique and unguessable,
-- so a webhook POST resolves its tenant without any auth header.
CREATE UNIQUE INDEX uq_tenant_connectors__webhook_token
    ON tenant_connectors (webhook_path_token) WHERE webhook_path_token IS NOT NULL;

CREATE INDEX ix_tenant_connectors__tenant_enabled
    ON tenant_connectors (tenant_id, connector_definition_id)
    WHERE is_enabled AND deleted_at IS NULL;

-- Health dashboard + alerting sweep.
CREATE INDEX ix_tenant_connectors__health
    ON tenant_connectors (tenant_id, health)
    WHERE health <> 'healthy' AND deleted_at IS NULL;

-- Scheduler sweep (privileged, cross-tenant).
CREATE INDEX ix_tenant_connectors__sync_due
    ON tenant_connectors (id)
    WHERE sync_enabled AND is_enabled AND deleted_at IS NULL;

CREATE TRIGGER trg_tenant_connectors_updated_at
    BEFORE UPDATE ON tenant_connectors
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 8.3 `connector_credentials`

Separate from `tenant_secrets` because OAuth credentials have a lifecycle that
generic secrets do not: access/refresh token pairs, expiry, scope drift, and
per-user (not just per-tenant) grants.

```sql
CREATE TABLE connector_credentials (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_connector_id UUID NOT NULL,

    -- A connector may hold several credentials: one per staff member whose
    -- calendar we sync. NULL = the service-level credential.
    on_behalf_of_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    -- Or per resource, when a calendar belongs to a room rather than a person.
    resource_id         UUID,

    auth_kind           connector_auth_kind NOT NULL,

    -- Envelope encryption, same scheme as tenant_secrets. Access token and
    -- refresh token are encrypted separately so a short-lived access token can
    -- be rotated without touching the refresh token's ciphertext.
    kms_key_id          TEXT NOT NULL,
    wrapped_dek         BYTEA NOT NULL,
    access_ciphertext   BYTEA,
    access_nonce        BYTEA,
    access_tag          BYTEA,
    refresh_ciphertext  BYTEA,
    refresh_nonce       BYTEA,
    refresh_tag         BYTEA,
    cipher_alg          TEXT NOT NULL DEFAULT 'AES-256-GCM',
    key_version         INTEGER NOT NULL DEFAULT 1,

    -- Non-secret metadata.
    external_account_id TEXT,               -- the remote account this maps to
    external_account_label TEXT,            -- "ops@clinic.com"
    scopes_granted      TEXT[] NOT NULL DEFAULT '{}',
    token_type          TEXT DEFAULT 'Bearer',

    access_expires_at   TIMESTAMPTZ,
    refresh_expires_at  TIMESTAMPTZ,
    -- Refresh coordination: a mutex so two workers do not both refresh and
    -- invalidate each other's token (common with single-use refresh tokens).
    refresh_locked_until TIMESTAMPTZ,
    last_refreshed_at   TIMESTAMPTZ,
    refresh_failure_count SMALLINT NOT NULL DEFAULT 0,

    is_valid            BOOLEAN NOT NULL DEFAULT TRUE,
    invalidated_at      TIMESTAMPTZ,
    invalidation_reason TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_connector_credentials__connectors
        FOREIGN KEY (tenant_id, tenant_connector_id)
        REFERENCES tenant_connectors (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_connector_credentials__resources
        FOREIGN KEY (tenant_id, resource_id)
        REFERENCES resources (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_connector_credentials__oauth_shape CHECK (
        auth_kind <> 'oauth2' OR access_ciphertext IS NOT NULL
    ),
    CONSTRAINT ck_connector_credentials__owner_exclusive
        CHECK (NOT (on_behalf_of_user_id IS NOT NULL AND resource_id IS NOT NULL)),
    CONSTRAINT ck_connector_credentials__invalid_coherent
        CHECK (is_valid = (invalidated_at IS NULL))
);

-- One credential per (connector, owner-scope).
CREATE UNIQUE INDEX uq_connector_credentials__connector_service
    ON connector_credentials (tenant_id, tenant_connector_id)
    WHERE on_behalf_of_user_id IS NULL AND resource_id IS NULL;

CREATE UNIQUE INDEX uq_connector_credentials__connector_user
    ON connector_credentials (tenant_id, tenant_connector_id, on_behalf_of_user_id)
    WHERE on_behalf_of_user_id IS NOT NULL;

CREATE UNIQUE INDEX uq_connector_credentials__connector_resource
    ON connector_credentials (tenant_id, tenant_connector_id, resource_id)
    WHERE resource_id IS NOT NULL;

-- Refresh sweeper: "tokens expiring in the next 10 minutes".
CREATE INDEX ix_connector_credentials__access_expiry
    ON connector_credentials (access_expires_at)
    WHERE is_valid AND access_expires_at IS NOT NULL;

-- Re-consent alerting: refresh tokens that failed repeatedly.
CREATE INDEX ix_connector_credentials__failing
    ON connector_credentials (tenant_id, refresh_failure_count)
    WHERE refresh_failure_count > 0;

CREATE TRIGGER trg_connector_credentials_updated_at
    BEFORE UPDATE ON connector_credentials
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN connector_credentials.access_ciphertext IS
    'pii_class=high; envelope-encrypted OAuth access token; never logged';
COMMENT ON COLUMN connector_credentials.refresh_ciphertext IS
    'pii_class=high; envelope-encrypted OAuth refresh token; never logged';
```

### 8.4 `sync_state`

Cursor/watermark bookkeeping per connector per synced entity stream. Kept apart
from `tenant_connectors` because one connector syncs several streams
(calendar events, contacts, availability) each with its own cursor.

```sql
CREATE TABLE sync_state (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_connector_id UUID NOT NULL,

    -- Which stream. 'calendar.events' | 'crm.contacts' | 'pms.reservations'
    stream              TEXT NOT NULL,
    -- Optional sub-scope: a specific calendar id, a specific resource.
    scope_ref           TEXT,

    direction           sync_direction NOT NULL DEFAULT 'inbound',

    -- ── Cursors. Which one is used depends on the remote API's model. ──────
    -- Opaque continuation token (Google syncToken, Microsoft deltaLink).
    cursor_token        TEXT,
    -- High-water mark for timestamp-based APIs.
    watermark_at        TIMESTAMPTZ,
    -- Monotonic sequence for change-log APIs.
    watermark_sequence  BIGINT,
    -- ETag / version for conditional GETs.
    etag                TEXT,

    -- Full-resync bookkeeping. Remote APIs invalidate sync tokens periodically;
    -- when they do we must re-baseline.
    last_full_sync_at   TIMESTAMPTZ,
    full_resync_required BOOLEAN NOT NULL DEFAULT FALSE,
    full_resync_reason  TEXT,

    -- ── Run statistics ─────────────────────────────────────────────────────
    last_run_at         TIMESTAMPTZ,
    last_success_at     TIMESTAMPTZ,
    last_run_duration_ms INTEGER,
    records_seen        BIGINT NOT NULL DEFAULT 0,
    records_applied     BIGINT NOT NULL DEFAULT 0,
    records_skipped     BIGINT NOT NULL DEFAULT 0,
    records_failed      BIGINT NOT NULL DEFAULT 0,

    -- Conflict policy for bidirectional streams.
    conflict_policy     TEXT NOT NULL DEFAULT 'remote_wins',
    conflicts_pending   INTEGER NOT NULL DEFAULT 0,

    health              sync_health NOT NULL DEFAULT 'healthy',
    consecutive_failures SMALLINT NOT NULL DEFAULT 0,
    last_error          TEXT,
    last_error_at       TIMESTAMPTZ,

    -- Lease, so exactly one worker syncs a stream at a time. Held for the
    -- duration of a run; a crashed worker's lease simply expires.
    lease_owner         TEXT,
    lease_expires_at    TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_sync_state__connectors
        FOREIGN KEY (tenant_id, tenant_connector_id)
        REFERENCES tenant_connectors (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_sync_state__stream CHECK (stream ~ '^[a-z][a-z0-9_.]{1,63}$'),
    CONSTRAINT ck_sync_state__conflict_policy
        CHECK (conflict_policy IN ('remote_wins','local_wins','newest_wins','manual')),
    CONSTRAINT ck_sync_state__lease_paired
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE UNIQUE INDEX uq_sync_state__connector_stream_scope
    ON sync_state (tenant_id, tenant_connector_id, stream, COALESCE(scope_ref, ''));

-- Worker claim query: "streams whose lease is free and are due to run".
CREATE INDEX ix_sync_state__claimable
    ON sync_state (lease_expires_at NULLS FIRST, last_run_at NULLS FIRST)
    WHERE health <> 'disabled';

CREATE INDEX ix_sync_state__tenant_connector
    ON sync_state (tenant_id, tenant_connector_id);

CREATE INDEX ix_sync_state__unhealthy
    ON sync_state (tenant_id, health) WHERE health IN ('degraded','failing');

CREATE TRIGGER trg_sync_state_updated_at
    BEFORE UPDATE ON sync_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

---

## 9. Workflows

Temporal owns execution state (contract §3). These tables are the **projection**
Temporal state into our own database: they exist so that (a) the admin UI can
list and filter runs without querying Temporal's Visibility API on every page
load, (b) we can join workflow runs to bookings and conversations, and (c) the
tenant's own definitions of *which* registered workflow to run and with what
parameters are versioned as data.

**They are not the source of truth for execution.** If this projection and
Temporal disagree, Temporal wins, and a reconciler corrects the projection.

### 9.1 `workflow_definitions`

```sql
CREATE TABLE workflow_definitions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),

    -- NULL = platform-provided workflow available to every tenant.
    tenant_id           UUID REFERENCES tenants(id) ON DELETE CASCADE,

    code                TEXT NOT NULL,      -- 'booking_lifecycle','reminder_cascade'
    name                TEXT NOT NULL,
    description         TEXT,

    -- The REGISTERED Temporal workflow type this definition binds to.
    -- Contract §10: '<Domain><Action>Workflow'. Resolved through a registry;
    -- a tenant can never name an arbitrary class.
    workflow_type       TEXT NOT NULL,

    -- Contract §3: task queues partitioned per workload class.
    task_queue          TEXT NOT NULL,

    -- What starts it.
    trigger_kind        TEXT NOT NULL DEFAULT 'event',
    -- For event triggers: which event type. 'booking.appointment.confirmed'
    trigger_event_type  TEXT,
    -- For schedule triggers: cron in the tenant's timezone.
    trigger_cron        TEXT,

    category            TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_workflow_definitions__code CHECK (code ~ '^[a-z][a-z0-9_]{1,63}$'),
    CONSTRAINT ck_workflow_definitions__task_queue
        CHECK (task_queue IN ('bookings','notifications','ingestion','connectors','default')),
    CONSTRAINT ck_workflow_definitions__trigger_kind
        CHECK (trigger_kind IN ('event','schedule','manual','api','signal')),
    CONSTRAINT ck_workflow_definitions__trigger_shape CHECK (
        (trigger_kind = 'event'    AND trigger_event_type IS NOT NULL) OR
        (trigger_kind = 'schedule' AND trigger_cron IS NOT NULL) OR
        (trigger_kind NOT IN ('event','schedule'))
    ),
    CONSTRAINT ck_workflow_definitions__event_type_shape
        CHECK (trigger_event_type IS NULL
               OR trigger_event_type ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$')
);

CREATE UNIQUE INDEX uq_workflow_definitions__global_code
    ON workflow_definitions (code) WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX uq_workflow_definitions__tenant_code
    ON workflow_definitions (tenant_id, code) WHERE tenant_id IS NOT NULL;

ALTER TABLE workflow_definitions
    ADD CONSTRAINT uq_workflow_definitions__id_tenant UNIQUE (id, tenant_id);

-- Event dispatch: "which workflows fire on booking.appointment.confirmed".
CREATE INDEX ix_workflow_definitions__trigger_event
    ON workflow_definitions (trigger_event_type, tenant_id)
    WHERE is_active AND trigger_event_type IS NOT NULL;

CREATE INDEX ix_workflow_definitions__tenant_active
    ON workflow_definitions (tenant_id) WHERE is_active;

CREATE TRIGGER trg_workflow_definitions_updated_at
    BEFORE UPDATE ON workflow_definitions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 9.2 `workflow_versions`

Immutable, like vertical versions, and for the same reason: a long-running
workflow started under version 3 must keep version 3's parameters even after
version 4 ships. Temporal handles code determinism via versioning APIs; this
table handles *configuration* determinism.

```sql
CREATE TABLE workflow_versions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    workflow_definition_id UUID NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    tenant_id           UUID,

    version             INTEGER NOT NULL,

    -- Declarative parameters passed to the workflow at start. Validated
    -- against param_schema. E.g. reminder offsets, escalation thresholds,
    -- retry policies, which notification templates to use.
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    param_schema        JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Timer configuration extracted for indexability: how far ahead this
    -- workflow schedules timers, which bounds our Temporal retention needs.
    max_duration_seconds INTEGER,

    -- Temporal retry policy for the workflow itself.
    retry_policy        JSONB NOT NULL DEFAULT
        '{"initial_interval_seconds":1,"backoff_coefficient":2.0,"maximum_attempts":3}'::jsonb,

    -- Activity timeouts, per activity name.
    activity_timeouts   JSONB NOT NULL DEFAULT '{}'::jsonb,

    definition_sha256   BYTEA NOT NULL,

    is_published        BOOLEAN NOT NULL DEFAULT FALSE,
    published_at        TIMESTAMPTZ,
    deprecated_at       TIMESTAMPTZ,

    created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_workflow_versions__definitions
        FOREIGN KEY (workflow_definition_id, tenant_id)
        REFERENCES workflow_definitions (id, tenant_id) ON DELETE CASCADE,
    CONSTRAINT ck_workflow_versions__version_positive CHECK (version >= 1),
    CONSTRAINT ck_workflow_versions__sha CHECK (octet_length(definition_sha256) = 32),
    CONSTRAINT ck_workflow_versions__published_coherent
        CHECK (is_published = (published_at IS NOT NULL)),
    CONSTRAINT ck_workflow_versions__max_duration
        CHECK (max_duration_seconds IS NULL OR max_duration_seconds BETWEEN 1 AND 31536000)
);

ALTER TABLE workflow_versions
    ADD CONSTRAINT uq_workflow_versions__id_tenant UNIQUE (id, tenant_id);

CREATE UNIQUE INDEX uq_workflow_versions__definition_version
    ON workflow_versions (workflow_definition_id, version);

-- "Latest published version to start new runs with".
CREATE INDEX ix_workflow_versions__latest
    ON workflow_versions (workflow_definition_id, version DESC)
    WHERE is_published AND deprecated_at IS NULL;

CREATE INDEX ix_workflow_versions__tenant
    ON workflow_versions (tenant_id) WHERE tenant_id IS NOT NULL;

CREATE OR REPLACE FUNCTION guard_workflow_version_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.is_published AND (
           NEW.parameters IS DISTINCT FROM OLD.parameters
        OR NEW.retry_policy IS DISTINCT FROM OLD.retry_policy
        OR NEW.activity_timeouts IS DISTINCT FROM OLD.activity_timeouts
        OR NEW.version IS DISTINCT FROM OLD.version
    ) THEN
        RAISE EXCEPTION 'workflow_versions %: published versions are immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_workflow_versions_immutable
    BEFORE UPDATE ON workflow_versions
    FOR EACH ROW EXECUTE FUNCTION guard_workflow_version_immutable();
```

### 9.3 `workflow_runs`

```sql
CREATE TABLE workflow_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    workflow_definition_id UUID,
    workflow_version_id UUID,

    -- ── Temporal identity ──────────────────────────────────────────────────
    -- Contract §3: workflow_id is '{tenant_id}:{domain}:{entity_id}'.
    -- That namespacing gives us idempotency for free: starting the same
    -- logical workflow twice is rejected by Temporal, not by us.
    workflow_id         TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    workflow_type       TEXT NOT NULL,
    task_queue          TEXT NOT NULL,
    temporal_namespace  TEXT NOT NULL DEFAULT 'default',

    status              workflow_run_status NOT NULL DEFAULT 'pending',

    -- ── What it operates on ────────────────────────────────────────────────
    -- Polymorphic subject reference. Not an FK, because the subject may be in
    -- a partitioned table (conversations) or may be deleted before the run's
    -- projection is pruned.
    subject_kind        TEXT,               -- 'booking' | 'conversation' | 'connector' | ...
    subject_id          UUID,

    booking_id          UUID,               -- FK'd when subject is a booking
    conversation_id     UUID,

    -- Input snapshot, PII-scrubbed. Full input lives in Temporal history.
    input_summary       JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary      JSONB,

    -- ── Lifecycle ──────────────────────────────────────────────────────────
    started_at          TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    duration_ms         BIGINT,

    attempt             INTEGER NOT NULL DEFAULT 1,
    -- Continue-As-New chain: a long-running booking workflow resets its
    -- history periodically; each reset is a new run_id in the same chain.
    first_run_id        TEXT,
    continued_from_run_id TEXT,

    error_type          TEXT,
    error_message       TEXT,
    -- Non-retryable failures need human attention; indexed for the ops queue.
    is_terminal_failure BOOLEAN NOT NULL DEFAULT FALSE,

    -- Next scheduled timer, projected so the UI can show "reminder in 3 days"
    -- without a Temporal describe call.
    next_timer_at       TIMESTAMPTZ,

    -- Reconciliation bookkeeping.
    last_synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    correlation_id      UUID,
    causation_id        UUID,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_workflow_runs__definitions
        FOREIGN KEY (workflow_definition_id)
        REFERENCES workflow_definitions (id) ON DELETE SET NULL,
    CONSTRAINT fk_workflow_runs__versions
        FOREIGN KEY (workflow_version_id)
        REFERENCES workflow_versions (id) ON DELETE SET NULL,
    CONSTRAINT fk_workflow_runs__bookings
        FOREIGN KEY (tenant_id, booking_id)
        REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT ck_workflow_runs__closed_coherent
        CHECK (status NOT IN ('completed','failed','cancelled','terminated','timed_out')
               OR closed_at IS NOT NULL),
    CONSTRAINT ck_workflow_runs__subject_paired
        CHECK ((subject_kind IS NULL) = (subject_id IS NULL)),
    -- The contract's workflow_id namespacing, enforced.
    CONSTRAINT ck_workflow_runs__workflow_id_namespaced
        CHECK (workflow_id LIKE tenant_id::text || ':%')
);

-- Temporal's (workflow_id, run_id) is the natural key.
CREATE UNIQUE INDEX uq_workflow_runs__workflow_run
    ON workflow_runs (workflow_id, run_id);

-- "Is there already a live run for this booking" — the idempotency check
-- before starting one.
CREATE UNIQUE INDEX uq_workflow_runs__live_workflow_id
    ON workflow_runs (tenant_id, workflow_id)
    WHERE status IN ('pending','running','compensating');

-- Run list in the admin UI.
CREATE INDEX ix_workflow_runs__tenant_status_started
    ON workflow_runs (tenant_id, status, started_at DESC);

CREATE INDEX ix_workflow_runs__tenant_type
    ON workflow_runs (tenant_id, workflow_type, started_at DESC);

-- "Show me the workflow driving this booking".
CREATE INDEX ix_workflow_runs__tenant_booking
    ON workflow_runs (tenant_id, booking_id) WHERE booking_id IS NOT NULL;

CREATE INDEX ix_workflow_runs__tenant_subject
    ON workflow_runs (tenant_id, subject_kind, subject_id)
    WHERE subject_id IS NOT NULL;

-- Ops queue: terminal failures needing attention.
CREATE INDEX ix_workflow_runs__terminal_failures
    ON workflow_runs (tenant_id, closed_at DESC) WHERE is_terminal_failure;

-- Reconciler: runs whose projection is stale.
CREATE INDEX ix_workflow_runs__stale_projection
    ON workflow_runs (last_synced_at)
    WHERE status IN ('pending','running','compensating');

CREATE TRIGGER trg_workflow_runs_updated_at
    BEFORE UPDATE ON workflow_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### 9.4 `ai_checkpoints`

LangGraph's durable graph state. `03-AI-ENGINE.md` §5 owns the checkpointer
*protocol* — serialisation format, `schema_version` gating, resume semantics.
**This document owns the table's physical design**: partitioning, retention,
and storage tuning. The DDL below is authoritative and supersedes the
illustrative version in that document; the two differ only in physical
properties, not in columns or RLS semantics.

It is declared here rather than in §6 (Conversations) because it is execution
state, like `workflow_runs` — a projection of a running graph, not a durable
business record. The durable record of what was said lives in `turns`.

```sql
CREATE TABLE ai_checkpoints (
    id                    UUID NOT NULL DEFAULT uuid_generate_v7(),
    tenant_id             UUID NOT NULL,

    -- LangGraph thread identity. thread_id is the conversation_id as text;
    -- checkpoint_ns namespaces sub-graphs within one thread.
    thread_id             TEXT NOT NULL,
    checkpoint_ns         TEXT NOT NULL DEFAULT '',
    checkpoint_id         TEXT NOT NULL,
    parent_checkpoint_id  TEXT,

    -- Gates replay: a checkpoint written by an older state schema is rejected
    -- rather than deserialised into a graph that no longer matches it.
    schema_version        INTEGER NOT NULL,

    type                  TEXT NOT NULL,
    checkpoint            BYTEA NOT NULL,
    metadata_type         TEXT NOT NULL,
    metadata              BYTEA NOT NULL,

    -- Denormalized so retention and GC need no join back to conversations.
    conversation_id       UUID,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_ai_checkpoints PRIMARY KEY (tenant_id, created_at, id),
    CONSTRAINT ck_ai_checkpoints__schema_version CHECK (schema_version >= 1),
    CONSTRAINT ck_ai_checkpoints__payload_nonempty
        CHECK (octet_length(checkpoint) > 0)
) PARTITION BY RANGE (created_at);

-- LangGraph's natural key. created_at is in it because PostgreSQL requires the
-- partition key in every unique constraint on a partitioned table — the same
-- constraint that shaped the conversations and turns PKs (§6.1).
CREATE UNIQUE INDEX uq_ai_checkpoints__thread_ns_ckpt
    ON ai_checkpoints (tenant_id, thread_id, checkpoint_ns, checkpoint_id, created_at);

-- The only hot read: "latest checkpoint for this thread", on every graph
-- resume. DESC on checkpoint_id because LangGraph ids sort lexicographically
-- in creation order.
CREATE INDEX ix_ai_checkpoints__thread_latest
    ON ai_checkpoints (tenant_id, thread_id, checkpoint_ns, checkpoint_id DESC);

-- Targeted erasure alongside a conversation (§17.4 stage 3).
CREATE INDEX ix_ai_checkpoints__conversation
    ON ai_checkpoints (tenant_id, conversation_id)
    WHERE conversation_id IS NOT NULL;

-- Checkpoint payloads are large BYTEA and are always read or written whole.
-- EXTENDED (compress, then TOAST out-of-line) is correct here and is the
-- default — stated explicitly because chunks.embedding deliberately does the
-- opposite (§7.4), and the difference should not look accidental.
ALTER TABLE ai_checkpoints ALTER COLUMN checkpoint SET STORAGE EXTENDED;
ALTER TABLE ai_checkpoints ALTER COLUMN metadata   SET STORAGE EXTENDED;

-- Insert-only, high-churn, short-lived. Same profile as turns.
ALTER TABLE ai_checkpoints SET (
    autovacuum_vacuum_insert_scale_factor = 0.0,
    autovacuum_vacuum_insert_threshold    = 100000,
    autovacuum_analyze_scale_factor       = 0.0,
    autovacuum_analyze_threshold          = 10000
);

ALTER TABLE ai_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_checkpoints FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_ai_checkpoints__all ON ai_checkpoints
    FOR ALL
    TO receptionai_app, receptionai_reporting, receptionai_ops
    USING      (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

COMMENT ON COLUMN ai_checkpoints.checkpoint IS
    'pii_class=high; serialized graph state containing customer utterances '
    'and collected_data; retention 14d (§17.3)';
```

**Two decisions the AI-engine document left open, made here.**

**1. Partition on `created_at`, monthly — same as the other four.** Alternatives
considered and rejected: partitioning on `thread_id` hash would help nothing
(the hot query already carries `tenant_id, thread_id` and hits one index), and
would make retention a `DELETE` over every partition rather than a `DROP`.
Retention is the reason we partition; it must drive the key.

Monthly rather than daily is the harder call, because at a 14-day retention a
monthly partition is never more than ~50% dead space and we drop only ~1
partition per month. Daily partitions would track retention far more tightly.
We still choose monthly, for consistency with the other four tables — one
`ops_create_monthly_partitions()` job, one mental model — and accept carrying
up to ~6 weeks of checkpoints when retention is 14 days. If checkpoint volume
turns out to dominate storage, switching this one table to **weekly** partitions
is a contained change (§18); daily is over-engineering at any plausible volume.

**2. Retention is 14 days, not 30, and far shorter than conversation retention.**
A checkpoint exists to resume a live or recently-dropped graph. The voice
resume window is 10 minutes (`RESUME_WINDOW_SECONDS` in `03-AI-ENGINE.md` §5.3);
a chat thread might legitimately resume the next day. Beyond that a checkpoint
is pure liability: it is the highest-density PII in the system — full
`collected_data`, verbatim utterances — in an opaque BYTEA that no redaction
sweep can partially scrub, because it is a serialised blob rather than columns.

So: **14 days**, floor 1 day, ceiling 30 days. This deliberately overrides the
30-day figure in `03-AI-ENGINE.md` §5.2. 14 days covers every resume case with
two orders of magnitude of margin, halves the storage, and halves the window in
which a checkpoint could be exposed. The durable record — what was said, what
was booked — lives in `turns` and `bookings` with proper column-level PII
classification and a redaction pipeline that can actually reach into it.

A checkpoint is never the system of record for anything. If it were, 14 days
would be indefensible; because it is not, 30 days was simply longer than needed.

> **Recommendation on checkpoint frequency.** The AI-engine document flags
> "every step vs. turn boundaries" as a measurement question. From the storage
> side the answer is not close, and it is worth stating rather than measuring.
>
> A voice turn runs perhaps 5–10 graph steps (route → retrieve → plan → tool →
> observe → respond). Checkpointing every step therefore writes 5–10 BYTEA rows
> per turn, each holding a **complete copy** of the graph state — checkpoints
> are snapshots, not deltas, so the payload does not shrink for intermediate
> steps. With state in the tens of KB and a 20-turn call, per-step
> checkpointing is on the order of 100–200 writes and single-digit MB **per
> call**, against ~20 writes and a few hundred KB for turn-boundary
> checkpointing.
>
> What per-step buys is resuming mid-turn — recovering a graph that died between
> `tool` and `observe`. That is worth very little here: a dropped call cannot
> resume mid-turn anyway (the caller is gone and the audio stream is closed),
> and a worker crash mid-turn is better handled by replaying the turn from its
> last boundary, which is idempotent by construction because tool calls carry
> idempotency keys.
>
> **Recommendation: checkpoint at turn boundaries by default**, with per-step
> checkpointing available as a per-tenant debug flag for reproducing agent
> misbehaviour. That is roughly an order of magnitude fewer rows and bytes, and
> it makes `ai_checkpoints` comparable in volume to `turns` rather than 5–10×
> larger than it. The decision belongs to the AI engine's runtime; this document
> records that the storage cost of the alternative is what makes it the wrong
> default.

---

## 10. Operations

### 10.1 `audit_log`

Contract §8: *"Every state-changing action writes an append-only audit record."*
Append-only is enforced by revoking `UPDATE`/`DELETE` from every runtime role
(§16.5) and by an RLS policy with no `USING` clause for those commands.
Partitioned monthly (§17).

```sql
CREATE TABLE audit_log (
    id                  UUID NOT NULL DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL,

    -- ── Who ────────────────────────────────────────────────────────────────
    actor_kind          TEXT NOT NULL,      -- 'user'|'api_key'|'agent'|'system'|'workflow'
    actor_user_id       UUID,
    actor_api_key_id    UUID,
    actor_label         TEXT,               -- denormalized for readability
    -- Platform-staff impersonation must be visible in the tenant's own log.
    impersonator_user_id UUID,

    -- ── What ───────────────────────────────────────────────────────────────
    -- Contract §6 naming: 'domain.entity.action'
    action              TEXT NOT NULL,      -- 'booking.appointment.cancelled'
    entity_type         TEXT NOT NULL,      -- 'bookings'
    entity_id           UUID,
    entity_label        TEXT,               -- 'BK-7F3K2Q', for the UI

    -- Field-level diff, PII already masked according to the column's
    -- pii_class. Structure: {"status":{"old":"confirmed","new":"cancelled"}}
    changes             JSONB,
    -- Additional context that is not a field diff.
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    outcome             TEXT NOT NULL DEFAULT 'success',
    error_message       TEXT,

    -- ── Where / how ────────────────────────────────────────────────────────
    ip_address          INET,
    user_agent          TEXT,
    request_method      TEXT,
    request_path        TEXT,
    session_id          UUID,

    -- ── Correlation (contract §6) ──────────────────────────────────────────
    correlation_id      UUID,
    causation_id        UUID,
    trace_id            TEXT,               -- OTel trace id, hex
    span_id             TEXT,

    -- Compliance classification, so a HIPAA tenant's PHI-access records can
    -- be exported and retained separately.
    is_phi_access       BOOLEAN NOT NULL DEFAULT FALSE,
    severity            TEXT NOT NULL DEFAULT 'info',

    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_audit_log PRIMARY KEY (tenant_id, created_at, id),
    CONSTRAINT ck_audit_log__actor_kind
        CHECK (actor_kind IN ('user','api_key','agent','system','workflow','connector')),
    CONSTRAINT ck_audit_log__action_shape
        CHECK (action ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'),
    CONSTRAINT ck_audit_log__outcome
        CHECK (outcome IN ('success','failure','denied','partial')),
    CONSTRAINT ck_audit_log__severity
        CHECK (severity IN ('debug','info','notice','warning','critical')),
    CONSTRAINT ck_audit_log__actor_ref CHECK (
        (actor_kind <> 'user'    OR actor_user_id IS NOT NULL) AND
        (actor_kind <> 'api_key' OR actor_api_key_id IS NOT NULL)
    )
) PARTITION BY RANGE (created_at);

-- Entity history: "everything that happened to booking X". The single most
-- common audit query.
CREATE INDEX ix_audit_log__tenant_entity
    ON audit_log (tenant_id, entity_type, entity_id, occurred_at DESC);

-- Actor history: "everything user U did". Second most common; used for
-- offboarding review and incident response.
CREATE INDEX ix_audit_log__tenant_actor
    ON audit_log (tenant_id, actor_user_id, occurred_at DESC)
    WHERE actor_user_id IS NOT NULL;

-- Action-type filtering in the audit viewer.
CREATE INDEX ix_audit_log__tenant_action
    ON audit_log (tenant_id, action, occurred_at DESC);

-- Compliance export: all PHI access in a period.
CREATE INDEX ix_audit_log__phi
    ON audit_log (tenant_id, occurred_at DESC) WHERE is_phi_access;

-- Security review: denials and failures.
CREATE INDEX ix_audit_log__failures
    ON audit_log (tenant_id, occurred_at DESC) WHERE outcome IN ('failure','denied');

-- Cross-system tracing.
CREATE INDEX ix_audit_log__correlation
    ON audit_log (tenant_id, correlation_id) WHERE correlation_id IS NOT NULL;

-- Impersonation review — the query a nervous compliance officer runs.
CREATE INDEX ix_audit_log__impersonation
    ON audit_log (tenant_id, occurred_at DESC) WHERE impersonator_user_id IS NOT NULL;

COMMENT ON COLUMN audit_log.changes IS
    'pii_class=medium; field values already masked per column pii_class before write';
```

**Why we do not use table triggers to write audit rows.** A generic
`AFTER INSERT OR UPDATE OR DELETE` audit trigger looks attractive and is a trap
here: it cannot know the *intent* (was this cancellation a customer request, a
no-show, or a system sweep?), it cannot know the actor when the change comes
from a background worker, and it doubles write amplification on the booking hot
path. Audit records are written explicitly by the service layer, in the same
transaction as the change, through a single `AuditWriter` chokepoint that
enforces the schema. The database enforces *append-only*; the application
enforces *completeness*, and a CI test asserts that every mutating repository
method has a corresponding audit call.

### 10.2 `outbox_events`

Contract §6: *"Outbound integration events use the transactional outbox
pattern."* The event is written in the **same transaction** as the state change,
so it cannot be lost, and a separate relay publishes it.

```sql
CREATE TABLE outbox_events (
    -- Contract §6 field names, verbatim.
    event_id            UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    type                TEXT NOT NULL,          -- 'booking.appointment.confirmed'
    version             SMALLINT NOT NULL DEFAULT 1,
    correlation_id      UUID,
    causation_id        UUID,
    payload             JSONB NOT NULL,

    -- ── Ordering ───────────────────────────────────────────────────────────
    -- Events about the same aggregate must be delivered in order. The relay
    -- processes one partition_key at a time; different keys go in parallel.
    aggregate_type      TEXT NOT NULL,
    aggregate_id        UUID NOT NULL,
    partition_key       TEXT GENERATED ALWAYS AS
                        (aggregate_type || ':' || aggregate_id::text) STORED,
    -- Monotonic per-aggregate sequence, for consumer-side gap detection.
    aggregate_sequence  BIGINT,

    -- ── Delivery ───────────────────────────────────────────────────────────
    status              outbox_status NOT NULL DEFAULT 'pending',
    -- Where it goes. NULL = the default internal bus. Non-null = a specific
    -- tenant webhook subscription.
    destination         TEXT,

    attempts            SMALLINT NOT NULL DEFAULT 0,
    max_attempts        SMALLINT NOT NULL DEFAULT 8,
    -- Exponential backoff with jitter; the relay only picks up rows whose
    -- next_attempt_at has passed.
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error          TEXT,
    last_attempt_at     TIMESTAMPTZ,
    dispatched_at       TIMESTAMPTZ,

    -- Relay lease, so concurrent relay instances do not double-publish.
    locked_by           TEXT,
    locked_until        TIMESTAMPTZ,

    -- Idempotency key handed to the consumer.
    idempotency_key     TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_outbox_events__type_shape
        CHECK (type ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'),
    CONSTRAINT ck_outbox_events__payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_outbox_events__attempts CHECK (attempts >= 0 AND attempts <= max_attempts),
    CONSTRAINT ck_outbox_events__dispatched_coherent
        CHECK ((status = 'dispatched') = (dispatched_at IS NOT NULL)),
    CONSTRAINT ck_outbox_events__lock_paired
        CHECK ((locked_by IS NULL) = (locked_until IS NULL))
);

-- ════════════════════════════════════════════════════════════════════════════
-- THE RELAY QUERY
-- ════════════════════════════════════════════════════════════════════════════
-- SELECT * FROM outbox_events
--  WHERE status = 'pending' AND next_attempt_at <= now()
--    AND (locked_until IS NULL OR locked_until < now())
--  ORDER BY partition_key, occurred_at
--  FOR UPDATE SKIP LOCKED
--  LIMIT 100;
--
-- SKIP LOCKED is what makes multiple relay instances safe without a
-- distributed lock: each grabs a disjoint set of rows.
--
-- The index is partial on status='pending'. This matters enormously: a
-- dispatched row is dead weight, and after a month there are millions of them.
-- A partial index stays proportional to the BACKLOG, not to history, so the
-- relay's index scan cost does not grow over time.
CREATE INDEX ix_outbox_events__relay
    ON outbox_events (next_attempt_at, partition_key)
    WHERE status = 'pending';

-- Per-aggregate ordering lookup.
CREATE INDEX ix_outbox_events__aggregate
    ON outbox_events (tenant_id, aggregate_type, aggregate_id, occurred_at);

-- Dead-letter queue review.
CREATE INDEX ix_outbox_events__dead
    ON outbox_events (tenant_id, occurred_at DESC) WHERE status = 'dead';

CREATE INDEX ix_outbox_events__tenant_type
    ON outbox_events (tenant_id, type, occurred_at DESC);

CREATE UNIQUE INDEX uq_outbox_events__idempotency
    ON outbox_events (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
```

**Why `outbox_events` is not partitioned** even though it is high-volume: the
working set is the pending backlog, which is small and self-limiting, and a
`DELETE FROM outbox_events WHERE status='dispatched' AND dispatched_at < now() -
interval '7 days'` run every hour keeps the table bounded at a few million rows.
Partitioning would complicate `FOR UPDATE SKIP LOCKED` across partitions for no
gain. If retention ever needs to exceed 30 days, dispatched events are copied to
a cold archive table and deleted here.

### 10.3 `usage_events`

Raw metering. Every billable action emits one row. Partitioned monthly (§17).

```sql
CREATE TABLE usage_events (
    id                  UUID NOT NULL DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL,
    location_id         UUID,

    -- The meter. Matches the keys in plans.metered_rates.
    -- 'voice_minutes' | 'llm_input_tokens' | 'llm_output_tokens' |
    -- 'stt_seconds' | 'tts_characters' | 'embedding_tokens' |
    -- 'sms_messages' | 'bookings_created' | 'storage_gb_hours'
    meter               TEXT NOT NULL,

    -- Quantity in the meter's own unit. NUMERIC not INTEGER because
    -- voice_minutes is fractional and storage_gb_hours is very fractional.
    quantity            NUMERIC(18,6) NOT NULL,
    unit                TEXT NOT NULL,

    -- ── Attribution ────────────────────────────────────────────────────────
    -- What generated it, so a tenant can see "this call cost $0.42".
    conversation_id     UUID,
    booking_id          UUID,
    ingestion_job_id    UUID,
    workflow_run_id     UUID,
    turn_id             UUID,

    -- Which provider/model, for cost reconciliation against vendor invoices.
    provider            TEXT,
    model               TEXT,
    port                TEXT,

    -- ── Billing state ──────────────────────────────────────────────────────
    subscription_id     UUID,
    -- Which billing period this belongs to. Set at write time from the
    -- subscription's current_period, so a late-arriving event still lands in
    -- the right period.
    billing_period_start TIMESTAMPTZ,
    -- Set once the event has been rolled into an invoice line. NULL = unbilled.
    billed_at           TIMESTAMPTZ,
    invoice_ref         TEXT,

    -- Idempotency: a retried activity must not double-meter. This is the
    -- single most important correctness property of this table.
    idempotency_key     TEXT NOT NULL,

    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_usage_events PRIMARY KEY (tenant_id, created_at, id),
    CONSTRAINT ck_usage_events__quantity_nonneg CHECK (quantity >= 0),
    CONSTRAINT ck_usage_events__meter_shape CHECK (meter ~ '^[a-z][a-z0-9_]{1,47}$')
) PARTITION BY RANGE (created_at);

-- Idempotency, enforced per partition. Because idempotency keys embed a
-- timestamp-derived component, a retry always lands in the same partition,
-- so a partition-local unique index is sufficient. The key format is
-- '<meter>:<source_kind>:<source_id>:<seq>'.
CREATE UNIQUE INDEX uq_usage_events__idempotency
    ON usage_events (tenant_id, idempotency_key, created_at);

-- ════════════════════════════════════════════════════════════════════════════
-- The billing rollup, run once per period per tenant:
--   SELECT meter, SUM(quantity) FROM usage_events
--    WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
--      AND billed_at IS NULL
--    GROUP BY meter;
-- Leading (tenant_id, meter, created_at) makes this an index-only scan when
-- quantity is INCLUDEd.
CREATE INDEX ix_usage_events__tenant_meter_created
    ON usage_events (tenant_id, meter, created_at DESC)
    INCLUDE (quantity);

-- Unbilled sweep across a period.
CREATE INDEX ix_usage_events__unbilled
    ON usage_events (tenant_id, billing_period_start)
    WHERE billed_at IS NULL;

-- Per-conversation cost drill-down in the UI.
CREATE INDEX ix_usage_events__conversation
    ON usage_events (tenant_id, conversation_id)
    WHERE conversation_id IS NOT NULL;

-- Provider reconciliation against vendor invoices.
CREATE INDEX ix_usage_events__provider
    ON usage_events (tenant_id, provider, model, created_at DESC)
    WHERE provider IS NOT NULL;
```

### 10.4 `cost_records`

Usage is what we **bill**; cost is what we **pay**. Keeping them separate is not
pedantry: gross margin per tenant is the number that decides whether the product
is viable, and conflating "2,000 output tokens" with "$0.03 of Anthropic spend"
makes that number unrecoverable.

```sql
CREATE TABLE cost_records (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Links back to the metered event, when there is one. Not an FK, because
    -- usage_events is partitioned and pruned on a different schedule.
    usage_event_id      UUID,
    usage_event_created_at TIMESTAMPTZ,

    -- What we paid for.
    port                TEXT NOT NULL,      -- 'LLMPort','STTPort',...
    provider            TEXT NOT NULL,
    model               TEXT,
    operation           TEXT,               -- 'chat','embed','transcribe','synthesize'

    -- ── The money ──────────────────────────────────────────────────────────
    -- unit_rate has 10 decimal places because per-token prices are on the
    -- order of 0.0000030 USD. Rounding these at 4dp before multiplying by a
    -- million tokens loses ~30% of the figure.
    quantity            NUMERIC(18,6) NOT NULL,
    unit                TEXT NOT NULL,
    unit_rate           NUMERIC(20,10) NOT NULL,
    -- Rolled up to the contract's canonical money precision.
    amount              NUMERIC(12,4) NOT NULL,
    currency            CHAR(3) NOT NULL,

    -- Which rate card produced unit_rate, so a price change is auditable.
    rate_card_version   TEXT,

    -- Attribution.
    conversation_id     UUID,
    turn_id             UUID,
    booking_id          UUID,
    ingestion_job_id    UUID,
    workflow_run_id     UUID,

    -- Was this cost recovered? Filled by the margin job.
    billed_amount       NUMERIC(12,4),
    margin              NUMERIC(12,4) GENERATED ALWAYS AS
                        (COALESCE(billed_amount, 0) - amount) STORED,

    -- Reconciliation against the vendor's own invoice.
    vendor_invoice_ref  TEXT,
    reconciled_at       TIMESTAMPTZ,
    reconciliation_delta NUMERIC(12,4),

    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_cost_records__amount_nonneg CHECK (amount >= 0),
    CONSTRAINT ck_cost_records__quantity_nonneg CHECK (quantity >= 0),
    CONSTRAINT ck_cost_records__rate_nonneg CHECK (unit_rate >= 0),
    CONSTRAINT ck_cost_records__currency CHECK (currency ~ '^[A-Z]{3}$')
);

-- Margin dashboard: cost by provider over a period.
CREATE INDEX ix_cost_records__tenant_provider_occurred
    ON cost_records (tenant_id, provider, occurred_at DESC)
    INCLUDE (amount, currency);

-- Per-conversation cost, the drill-down from the conversation view.
CREATE INDEX ix_cost_records__conversation
    ON cost_records (tenant_id, conversation_id) WHERE conversation_id IS NOT NULL;

-- Platform-wide (privileged) spend by provider, for vendor reconciliation.
CREATE INDEX ix_cost_records__provider_occurred
    ON cost_records (provider, occurred_at DESC) INCLUDE (amount, currency);

-- Unreconciled records awaiting an invoice match.
CREATE INDEX ix_cost_records__unreconciled
    ON cost_records (provider, occurred_at) WHERE reconciled_at IS NULL;

CREATE INDEX ix_cost_records__tenant_occurred
    ON cost_records (tenant_id, occurred_at DESC);
```

### 10.5 `notifications`

Outbound messages to customers and staff, via `NotificationPort`.

```sql
CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    location_id         UUID,

    channel             notification_channel NOT NULL,
    status              notification_status NOT NULL DEFAULT 'pending',

    -- ── Recipient ──────────────────────────────────────────────────────────
    recipient_kind      TEXT NOT NULL DEFAULT 'customer',
    recipient_user_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    -- The actual destination. E.164, email, device token, or webhook URL.
    recipient_address   TEXT NOT NULL,
    recipient_name      TEXT,
    recipient_locale    TEXT,
    recipient_timezone  TEXT,

    -- ── Content ────────────────────────────────────────────────────────────
    template_code       TEXT,
    -- Variables merged into the template. PII-bearing.
    template_data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Rendered output, stored for support ("what exactly did we send them?").
    subject             TEXT,
    body_text           TEXT,
    body_html           TEXT,

    -- ── Cause ──────────────────────────────────────────────────────────────
    booking_id          UUID,
    conversation_id     UUID,
    waitlist_entry_id   UUID,
    workflow_run_id     UUID,
    trigger_event_type  TEXT,

    -- ── Scheduling ─────────────────────────────────────────────────────────
    scheduled_for       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Quiet-hours enforcement: a reminder computed for 03:00 local is pushed
    -- to the next allowed window rather than sent.
    quiet_hours_deferred BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at          TIMESTAMPTZ,        -- pointless to send after this

    -- ── Delivery ───────────────────────────────────────────────────────────
    provider            TEXT,
    provider_message_id TEXT,
    attempts            SMALLINT NOT NULL DEFAULT 0,
    max_attempts        SMALLINT NOT NULL DEFAULT 3,
    next_attempt_at     TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    opened_at           TIMESTAMPTZ,
    clicked_at          TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    failure_reason      TEXT,
    failure_code        TEXT,

    -- ── Compliance ─────────────────────────────────────────────────────────
    -- Suppression: unsubscribed, bounced hard, or on a do-not-contact list.
    -- Checked before send; a suppressed notification is recorded, not sent,
    -- so we can prove we honoured the opt-out.
    suppression_reason  TEXT,
    consent_basis       TEXT,

    cost                NUMERIC(12,6),
    cost_currency       CHAR(3),

    idempotency_key     TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_notifications__locations
        FOREIGN KEY (tenant_id, location_id)
        REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_notifications__bookings
        FOREIGN KEY (tenant_id, booking_id)
        REFERENCES bookings (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_notifications__waitlist
        FOREIGN KEY (tenant_id, waitlist_entry_id)
        REFERENCES waitlist_entries (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_notifications__recipient_kind
        CHECK (recipient_kind IN ('customer','staff','admin','system')),
    CONSTRAINT ck_notifications__attempts CHECK (attempts >= 0 AND attempts <= max_attempts),
    CONSTRAINT ck_notifications__sent_coherent
        CHECK (status NOT IN ('sent','delivered') OR sent_at IS NOT NULL),
    CONSTRAINT ck_notifications__suppressed_coherent
        CHECK ((status = 'suppressed') = (suppression_reason IS NOT NULL)),
    CONSTRAINT ck_notifications__cost_paired
        CHECK ((cost IS NULL) = (cost_currency IS NULL))
);

ALTER TABLE waitlist_entries
    ADD CONSTRAINT uq_waitlist_entries__tenant_id_id UNIQUE (tenant_id, id);

-- ════════════════════════════════════════════════════════════════════════════
-- The sender's claim query, structurally identical to the outbox relay:
--   ... WHERE status='pending' AND scheduled_for <= now()
--       ORDER BY scheduled_for FOR UPDATE SKIP LOCKED LIMIT 200
-- Partial index keeps it proportional to the queue, not to history.
CREATE INDEX ix_notifications__send_queue
    ON notifications (scheduled_for)
    WHERE status = 'pending';

-- Retry queue.
CREATE INDEX ix_notifications__retry_queue
    ON notifications (next_attempt_at)
    WHERE status = 'failed' AND next_attempt_at IS NOT NULL;

-- Provider webhook callback resolution ("message SM123 was delivered").
CREATE UNIQUE INDEX uq_notifications__provider_message
    ON notifications (provider, provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE UNIQUE INDEX uq_notifications__idempotency
    ON notifications (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- "Every message we sent about this booking" — the support query.
CREATE INDEX ix_notifications__tenant_booking
    ON notifications (tenant_id, booking_id) WHERE booking_id IS NOT NULL;

-- Deliverability dashboard.
CREATE INDEX ix_notifications__tenant_channel_status
    ON notifications (tenant_id, channel, status, created_at DESC);

-- Per-recipient history, for rate limiting and for proving opt-out compliance.
CREATE INDEX ix_notifications__tenant_recipient
    ON notifications (tenant_id, recipient_address, created_at DESC);

CREATE TRIGGER trg_notifications_updated_at
    BEFORE UPDATE ON notifications
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN notifications.recipient_address IS 'pii_class=medium';
COMMENT ON COLUMN notifications.body_text IS 'pii_class=medium';
COMMENT ON COLUMN notifications.template_data IS 'pii_class=medium';
```

---

## 11. Row-Level Security

Contract §2 rules 2–5 and 7. This section is the mechanical realisation.

### 11.1 The role split

```sql
-- Owns every object. Runs migrations. Never used by the application.
CREATE ROLE receptionai_owner NOLOGIN;

-- The application's runtime role. NOT the owner. NOT BYPASSRLS. NOT superuser.
CREATE ROLE receptionai_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- Authentication-only role, used exclusively by the login/API-key resolution
-- path, which must read across tenants BEFORE a tenant context exists.
-- It has SELECT on exactly four tables and EXECUTE on two functions.
CREATE ROLE receptionai_auth LOGIN PASSWORD :'auth_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- Read-only analytics, RLS-enforced, no access to secret ciphertext.
CREATE ROLE receptionai_reporting LOGIN PASSWORD :'reporting_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- Background sweepers that legitimately operate cross-tenant (expiry, billing,
-- retention). Still NOT BYPASSRLS — it works through explicitly-granted
-- SECURITY DEFINER functions, never by seeing all rows.
CREATE ROLE receptionai_ops LOGIN PASSWORD :'ops_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
```

**`NOBYPASSRLS` is the whole point.** The contract says the runtime role must
not have it. Note also that even a role *with* `BYPASSRLS` is defeated by
`FORCE ROW LEVEL SECURITY` only for the table owner — so we do both: the app
role lacks `BYPASSRLS`, *and* every table is `FORCE`d so that even if someone
accidentally runs the app as the owner, policies still apply.

### 11.2 The universal policy pattern

Every tenant-owned table gets exactly this, with the table name substituted:

```sql
-- ┌──────────────────────────────────────────────────────────────────────────┐
-- │ THE PATTERN — applied verbatim to every tenant-owned table               │
-- └──────────────────────────────────────────────────────────────────────────┘
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <table> FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_<table>__all ON <table>
    FOR ALL
    TO receptionai_app, receptionai_reporting, receptionai_ops
    USING      (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
```

Four things about this that are deliberate:

1. **`USING` *and* `WITH CHECK`.** `USING` filters what you can see and which
   rows `UPDATE`/`DELETE` can touch. `WITH CHECK` validates rows you *write*.
   Omitting `WITH CHECK` would let a tenant `INSERT` a row stamped with another
   tenant's id — it would immediately become invisible to them, but it would be
   *there*, corrupting the other tenant's data. Both clauses, always.

2. **`FOR ALL` rather than four separate policies.** Multiple permissive
   policies on one table are OR-ed, which is a footgun: adding a policy can only
   ever *widen* access. One `FOR ALL` policy per table per role-set means the
   predicate is stated once and cannot be accidentally loosened by an addition.

3. **`TO <explicit roles>`.** A policy without a `TO` clause applies to
   `PUBLIC`, which includes roles we may add later for unrelated reasons.
   Naming the roles means a new role gets *no* access until someone deliberately
   grants it.

4. **`app_current_tenant()` returns NULL when unset**, and `tenant_id = NULL` is
   never true. A code path that forgets `SET LOCAL app.tenant_id` sees an empty
   table. Fail-closed.

### 11.3 Applied to every tenant-owned table

```sql
DO $$
DECLARE
    t TEXT;
    tenant_tables TEXT[] := ARRAY[
        -- Tenancy
        'locations', 'tenant_settings', 'tenant_secrets', 'tenant_provider_config',
        'subscriptions',
        -- Identity (tenant-scoped members only; users/sessions handled in §11.5)
        'user_tenant_roles', 'api_keys', 'customers', 'customer_identities',
        -- Verticals
        'tenant_vertical_installs',
        -- Booking
        'resource_types', 'resources', 'services', 'service_resource_requirements',
        'availability_rules', 'availability_exceptions', 'holidays',
        'bookings', 'booking_items', 'booking_holds', 'waitlist_entries',
        'recurrence_rules',
        -- Conversations
        'conversations', 'turns', 'transcripts', 'call_recordings',
        'conversation_outcomes',
        -- Knowledge
        'knowledge_sources', 'documents', 'document_versions', 'chunks',
        'ingestion_jobs',
        -- Connectors
        'tenant_connectors', 'connector_credentials', 'sync_state',
        -- Workflows
        'workflow_runs', 'ai_checkpoints',
        -- Ops
        'audit_log', 'outbox_events', 'usage_events', 'cost_records', 'notifications'
    ];
BEGIN
    FOREACH t IN ARRAY tenant_tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format($f$
            CREATE POLICY %I ON %I
                FOR ALL
                TO receptionai_app, receptionai_reporting, receptionai_ops
                USING      (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant())
        $f$, 'rls_' || t || '__all', t);
    END LOOP;
END;
$$;
```

**Partitioned tables.** RLS on a partitioned parent (`conversations`, `turns`,
`audit_log`, `usage_events`) propagates to every partition automatically,
including partitions created later. We do **not** define policies on individual
partitions; doing so would create a second, divergent policy set.

**`audit_log` is append-only at the database level.** The generic policy above
would permit `UPDATE` and `DELETE`. Replace it with a split pair:

```sql
DROP POLICY rls_audit_log__all ON audit_log;

CREATE POLICY rls_audit_log__select ON audit_log
    FOR SELECT
    TO receptionai_app, receptionai_reporting, receptionai_ops
    USING (tenant_id = app_current_tenant());

CREATE POLICY rls_audit_log__insert ON audit_log
    FOR INSERT
    TO receptionai_app, receptionai_ops
    WITH CHECK (tenant_id = app_current_tenant());

-- No UPDATE or DELETE policy exists. With RLS enabled and no permissive policy
-- for a command, that command matches zero rows — an UPDATE silently affects
-- nothing. Because "silently" is unacceptable for an audit trail, we ALSO
-- revoke the privileges outright so an attempt is a hard error:
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log
    FROM receptionai_app, receptionai_reporting, receptionai_ops;
```

**`tenant_secrets` gets a narrower grant.** Reporting must never read ciphertext:

```sql
DROP POLICY rls_tenant_secrets__all ON tenant_secrets;

CREATE POLICY rls_tenant_secrets__app ON tenant_secrets
    FOR ALL
    TO receptionai_app
    USING      (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- Reporting may see that a secret exists, never its contents.
CREATE POLICY rls_tenant_secrets__reporting ON tenant_secrets
    FOR SELECT
    TO receptionai_reporting
    USING (tenant_id = app_current_tenant());

REVOKE ALL ON tenant_secrets FROM receptionai_reporting;
GRANT SELECT (id, tenant_id, scope, name, last_four, expires_at, rotated_at,
              last_used_at, created_at, updated_at)
    ON tenant_secrets TO receptionai_reporting;
```

The same column-level treatment is applied to `connector_credentials`:

```sql
REVOKE ALL ON connector_credentials FROM receptionai_reporting;
GRANT SELECT (id, tenant_id, tenant_connector_id, auth_kind, external_account_id,
              external_account_label, scopes_granted, access_expires_at,
              refresh_expires_at, is_valid, created_at, updated_at)
    ON connector_credentials TO receptionai_reporting;
```

### 11.4 Why platform-admin access does *not* go through an RLS escape hatch

A tempting pattern is:

```sql
-- DO NOT DO THIS
USING (tenant_id = app_current_tenant() OR app_is_platform_admin())
```

We reject it. Once that `OR` exists in every policy, the security of the entire
system rests on one boolean GUC never being set incorrectly — by a bug, by a
connection-pool leak of session state, or by an injection into `SET`. The
contract says cross-tenant access is impossible *by construction*.

Instead: platform staff supporting a tenant go through an **impersonation flow**
that (a) requires an explicit reason and a fresh MFA assertion, (b) writes an
`audit_log` row with `impersonator_user_id` set, into the *tenant's own* log,
(c) mints a time-boxed session scoped to that single tenant, and (d) then uses
the ordinary `SET LOCAL app.tenant_id` path. Support access is therefore
indistinguishable, at the database layer, from ordinary tenant access — which
means the same policy protects it.

Genuinely cross-tenant operations (billing rollups, retention sweeps, hold
expiry) run as `receptionai_ops` through `SECURITY DEFINER` functions that are
narrow, auditable, and individually granted:

```sql
-- Example: the hold-expiry sweeper. Cross-tenant by necessity, but it can do
-- exactly one thing, and it cannot read a single business column.
CREATE OR REPLACE FUNCTION ops_expire_stale_holds(p_limit INTEGER DEFAULT 1000)
RETURNS INTEGER
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    WITH expired AS (
        UPDATE booking_holds
           SET status = 'expired',
               released_at = now(),
               release_reason = 'ttl_expired',
               updated_at = now()
         WHERE id IN (
             SELECT id FROM booking_holds
              WHERE status = 'active' AND expires_at <= now()
              ORDER BY expires_at
              LIMIT p_limit
              FOR UPDATE SKIP LOCKED
         )
        RETURNING 1
    )
    SELECT count(*)::INTEGER FROM expired;
$$;

REVOKE ALL ON FUNCTION ops_expire_stale_holds(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_expire_stale_holds(INTEGER) TO receptionai_ops;
```

`SET search_path = public, pg_temp` on every `SECURITY DEFINER` function is
mandatory: without it, a caller can prepend a schema containing a malicious
`booking_holds` and hijack the definer's privileges.

### 11.5 Non-tenant-scoped tables

`users`, `sessions`, `mfa_factors`, `oauth_identities`, `roles`,
`role_permissions`, `permissions` are not tenant-owned. They still need RLS,
just with a different predicate.

```sql
-- ── users ──────────────────────────────────────────────────────────────────
-- A tenant may see a user only if that user is a member of the current tenant.
-- The subquery is an index lookup on ix_user_tenant_roles__tenant_user, and the
-- planner caches app_current_tenant() as a constant (it is STABLE).
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_users__member_visible ON users
    FOR SELECT
    TO receptionai_app, receptionai_reporting
    USING (
        EXISTS (
            SELECT 1 FROM user_tenant_roles utr
             WHERE utr.user_id = users.id
               AND utr.tenant_id = app_current_tenant()
               AND utr.revoked_at IS NULL
        )
    );

-- A user may update only their own profile row.
CREATE POLICY rls_users__self_update ON users
    FOR UPDATE
    TO receptionai_app
    USING (id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- Invitations create users. Only the app role, and only rows that are
-- immediately made members in the same transaction (asserted by the service
-- layer and verified by a deferred constraint trigger).
CREATE POLICY rls_users__insert ON users
    FOR INSERT
    TO receptionai_app
    WITH CHECK (true);

-- ── sessions ───────────────────────────────────────────────────────────────
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_sessions__own ON sessions
    FOR ALL
    TO receptionai_app
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ── mfa_factors ────────────────────────────────────────────────────────────
ALTER TABLE mfa_factors ENABLE ROW LEVEL SECURITY;
ALTER TABLE mfa_factors FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_mfa_factors__own ON mfa_factors
    FOR ALL
    TO receptionai_app
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ── oauth_identities ───────────────────────────────────────────────────────
ALTER TABLE oauth_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_identities FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_oauth_identities__own ON oauth_identities
    FOR ALL
    TO receptionai_app
    USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ── roles / role_permissions ───────────────────────────────────────────────
-- Template roles (tenant_id IS NULL) are readable by all; custom roles only
-- by their owning tenant. Writes only to custom roles of the current tenant.
ALTER TABLE roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE roles FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_roles__read ON roles
    FOR SELECT
    TO receptionai_app, receptionai_reporting
    USING (tenant_id IS NULL OR tenant_id = app_current_tenant());

CREATE POLICY rls_roles__write ON roles
    FOR ALL
    TO receptionai_app
    USING      (tenant_id = app_current_tenant() AND NOT is_system)
    WITH CHECK (tenant_id = app_current_tenant() AND NOT is_system);

ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_permissions FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_role_permissions__read ON role_permissions
    FOR SELECT
    TO receptionai_app, receptionai_reporting
    USING (tenant_id IS NULL OR tenant_id = app_current_tenant());

CREATE POLICY rls_role_permissions__write ON role_permissions
    FOR ALL
    TO receptionai_app
    USING      (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
```

**Platform catalogue tables** (`permissions`, `plans`, `vertical_definitions`,
`vertical_versions`, `connector_definitions`, `workflow_definitions`,
`workflow_versions`) are read-only reference data. They get no RLS; they get
privilege control instead:

```sql
GRANT SELECT ON permissions, plans, vertical_definitions, vertical_versions,
                connector_definitions
    TO receptionai_app, receptionai_reporting, receptionai_ops;

REVOKE INSERT, UPDATE, DELETE ON permissions, plans, vertical_definitions,
                                 vertical_versions, connector_definitions
    FROM receptionai_app, receptionai_reporting, receptionai_ops;
```

`workflow_definitions` and `workflow_versions` are the exception among
catalogues — they have a nullable `tenant_id` because tenants may author their
own — so they get the split-policy treatment like `roles`:

```sql
ALTER TABLE workflow_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_definitions FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_workflow_definitions__read ON workflow_definitions
    FOR SELECT
    TO receptionai_app, receptionai_reporting, receptionai_ops
    USING (tenant_id IS NULL OR tenant_id = app_current_tenant());

CREATE POLICY rls_workflow_definitions__write ON workflow_definitions
    FOR ALL
    TO receptionai_app
    USING      (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE workflow_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_versions FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_workflow_versions__read ON workflow_versions
    FOR SELECT
    TO receptionai_app, receptionai_reporting, receptionai_ops
    USING (tenant_id IS NULL OR tenant_id = app_current_tenant());

CREATE POLICY rls_workflow_versions__write ON workflow_versions
    FOR ALL
    TO receptionai_app
    USING      (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
```

`tenants` itself:

```sql
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;

CREATE POLICY rls_tenants__self ON tenants
    FOR ALL
    TO receptionai_app, receptionai_reporting, receptionai_ops
    USING      (id = app_current_tenant())
    WITH CHECK (id = app_current_tenant());
```

Tenant *creation* happens through a `SECURITY DEFINER` provisioning function
granted only to a dedicated signup role — a tenant cannot create a tenant.

### 11.6 The `auth` role's narrow window

Login and API-key resolution must read before a tenant context exists. Rather
than weakening any policy, `receptionai_auth` gets `EXECUTE` on two
`SECURITY DEFINER` functions and `SELECT` on nothing:

```sql
CREATE OR REPLACE FUNCTION auth_resolve_user_by_email(p_email TEXT)
RETURNS TABLE (
    user_id UUID, email TEXT, password_hash TEXT, password_algo TEXT,
    status user_status, mfa_enforced BOOLEAN, locked_until TIMESTAMPTZ,
    is_platform_staff BOOLEAN
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT u.id, u.email, u.password_hash, u.password_algo, u.status,
           u.mfa_enforced, u.locked_until, u.is_platform_staff
      FROM users u
     WHERE u.email_normalized = lower(btrim(p_email))
       AND u.deleted_at IS NULL;
$$;

CREATE OR REPLACE FUNCTION auth_resolve_api_key(p_prefix TEXT)
RETURNS TABLE (
    api_key_id UUID, tenant_id UUID, key_hash BYTEA, scopes TEXT[],
    location_id UUID, allowed_ips INET[], status api_key_status,
    expires_at TIMESTAMPTZ, rate_limit_rpm INTEGER
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT k.id, k.tenant_id, k.key_hash, k.scopes, k.location_id,
           k.allowed_ips, k.status, k.expires_at, k.rate_limit_rpm
      FROM api_keys k
     WHERE k.key_prefix = p_prefix
       AND k.status = 'active';
$$;

CREATE OR REPLACE FUNCTION auth_list_memberships(p_user_id UUID)
RETURNS TABLE (tenant_id UUID, tenant_slug TEXT, tenant_status tenant_status,
               role_code TEXT, location_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT utr.tenant_id, t.slug, t.status, r.code, utr.location_id
      FROM user_tenant_roles utr
      JOIN tenants t ON t.id = utr.tenant_id AND t.deleted_at IS NULL
      JOIN roles   r ON r.id = utr.role_id
     WHERE utr.user_id = p_user_id
       AND utr.revoked_at IS NULL
       AND (utr.expires_at IS NULL OR utr.expires_at > now());
$$;

REVOKE ALL ON FUNCTION auth_resolve_user_by_email(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_resolve_api_key(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_list_memberships(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_resolve_user_by_email(TEXT) TO receptionai_auth;
GRANT EXECUTE ON FUNCTION auth_resolve_api_key(TEXT)       TO receptionai_auth;
GRANT EXECUTE ON FUNCTION auth_list_memberships(UUID)      TO receptionai_auth;
```

Note what these return: authentication material and nothing else. No booking
data, no conversation content, no secrets. The blast radius of a compromised
`auth` credential is "can enumerate password hashes", not "can read every
tenant".

### 11.7 Proving RLS works — the test that must exist

RLS is the kind of control that silently stops working. This test runs in CI
against a real Postgres and fails the build if any table is missing coverage:

```sql
-- Every table with a tenant_id column must have RLS enabled AND forced.
-- Run as an assertion in CI; zero rows = pass.
SELECT c.relname AS table_missing_rls,
       c.relrowsecurity AS rls_enabled,
       c.relforcerowsecurity AS rls_forced
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relkind IN ('r', 'p')
   AND EXISTS (
       SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0
          AND NOT a.attisdropped
   )
   AND NOT (c.relrowsecurity AND c.relforcerowsecurity);

-- Every index on a tenant table must lead with tenant_id (contract §2.6).
-- Known exceptions are listed explicitly so adding one is a deliberate act.
SELECT i.indexrelid::regclass AS offending_index,
       i.indrelid::regclass  AS on_table
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
 WHERE n.nspname = 'public'
   AND c.relkind IN ('r','p')
   AND i.indkey[0] <> a.attnum
   AND i.indexrelid::regclass::text NOT IN (
        'ix_user_tenant_roles__user_tenant',   -- §3.4: pre-context login path
        'uq_api_keys__key_prefix',             -- §3.5: pre-context auth path
        'ix_booking_holds__expiry_sweep',      -- §5.11: ops sweeper
        'ix_outbox_events__relay',             -- §10.2: cross-tenant relay
        'ix_notifications__send_queue',        -- §10.5: cross-tenant sender
        'ix_notifications__retry_queue',
        'uq_notifications__provider_message',
        'ix_subscriptions__period_end',        -- §2.6: billing sweeper
        'ix_tenant_secrets__expires',          -- §2.4: rotation sweeper
        'ix_sessions__expires', 'ix_sessions__user_live', 'ix_sessions__family',
        'uq_sessions__refresh_token_hash',
        'ix_knowledge_sources__next_sync',
        'ix_connector_credentials__access_expiry',
        'ix_sync_state__claimable',
        'ix_workflow_runs__stale_projection',
        'uq_workflow_runs__workflow_run',
        'ix_cost_records__provider_occurred',
        'ix_cost_records__unreconciled',
        'ix_recurrence_rules__expansion',
        'ix_ingestion_jobs__running_since',
        'ix_call_recordings__retention',
        'ix_user_tenant_roles__expires',
        'ix_api_keys__expires',
        'ix_tenant_connectors__sync_due',
        'uq_tenant_connectors__webhook_token'
   );
```

Plus a Python integration test that opens two sessions as `receptionai_app`,
sets a different `app.tenant_id` in each, and asserts that tenant A cannot
`SELECT`, `UPDATE`, or `INSERT`-with-B's-id on every single tenant table. That
test is generated from the same table list as §11.3, so a new table without RLS
fails the build.

---

## 12. Index Catalogue

Every index above is repeated here with the query it serves. An index without a
named query pattern is deleted; write amplification is not free.

### 12.1 Contract rule and its exceptions

Contract §2.6: *every index on a tenant-owned table leads with `tenant_id`.*

The rule exists because RLS injects `tenant_id = app_current_tenant()` into
every query. If the index does not lead with `tenant_id`, the planner must
either scan a wider index range and filter, or fall back to a sequential scan
whose cost grows with *total* rows across all tenants — meaning one large tenant
degrades every other tenant's latency.

Exceptions are only permitted for queries that run **before** tenant context
exists, or that are **inherently cross-tenant background sweeps** run by
`receptionai_ops`. Every one is enumerated in the CI assertion in §11.7.

### 12.2 Tenancy

| Index | Query it serves |
|---|---|
| `uq_tenants__slug` | Resolve `acme.receptionai.com` → tenant at request entry |
| `ix_tenants__shard_key` | `ConnectionRouter` shard lookup on session open |
| `ix_tenants__status` | Suspension sweep; admin tenant list |
| `uq_locations__tenant_code` | Resolve a location by its short code in an API path |
| `ix_locations__tenant_active` | Location picker; availability fan-out across sites |
| `uq_tenant_settings__*` (2) | Upsert a setting; guarantee one row per key per scope |
| `ix_tenant_settings__tenant_namespace` | Load an entire namespace (`booking.*`) per request |
| `uq_tenant_secrets__tenant_scope_name` | `SecretsPort.get('provider','openai.api_key')` |
| `ix_tenant_secrets__expires` | Rotation sweeper (cross-tenant, `ops`) |
| `uq_tenant_provider_config__tenant_port_priority` | Enforce a total failover order |
| `uq_tenant_provider_config__tenant_port_primary` | Enforce exactly one primary per port |
| `ix_tenant_provider_config__tenant_port_enabled` | **Hot:** resolve the LLM adapter chain on every turn. `INCLUDE (provider, model, secret_id)` makes it index-only |
| `uq_subscriptions__tenant_live` | Enforce one live subscription per tenant |
| `ix_subscriptions__tenant_status` | Entitlement check on gated routes |
| `ix_subscriptions__period_end` | Billing period-close sweep (cross-tenant, `ops`) |

### 12.3 Identity

| Index | Query it serves |
|---|---|
| `uq_users__email_normalized` | Login by email; enforce one account per address |
| `ix_users__email_trgm` | Admin user search with a leading wildcard (`%bob%`) |
| `ix_users__locked` | Unlock sweeper |
| `uq_permissions__code` | Permission resolution by code |
| `uq_roles__global_code` / `uq_roles__tenant_code` | Role lookup; name uniqueness per scope |
| `ix_role_permissions__permission` | "Who can `knowledge:publish`" audit report |
| `ix_user_tenant_roles__tenant_user` | **Hot:** authorize every request. `INCLUDE (role_id, location_id)` → index-only |
| `ix_user_tenant_roles__user_tenant` | Login tenant picker (pre-context; §3.4 exception) |
| `ix_user_tenant_roles__expires` | Contractor-access expiry sweep |
| `uq_api_keys__key_prefix` | **Hot:** API-key auth (pre-context; §3.5 exception) |
| `ix_api_keys__tenant_status` | Key list in the admin UI |
| `uq_sessions__refresh_token_hash` | Refresh-token exchange |
| `ix_sessions__family` | Revoke a whole token family on reuse detection |
| `ix_sessions__expires` | Session reaper |
| `uq_mfa_factors__credential_id` | WebAuthn assertion lookup |
| `uq_oauth_identities__provider_subject` | SSO login by `(provider, sub)` |

### 12.4 Verticals

| Index | Query it serves |
|---|---|
| `uq_vertical_definitions__code` | Resolve `'medical'` → definition |
| `ix_vertical_versions__latest` | "Newest stable version" on install/upgrade |
| `ix_vertical_versions__tools` (GIN) | Security review: which manifests grant tool T |
| `ix_vertical_versions__manifest` (GIN, `jsonb_path_ops`) | Ad-hoc manifest introspection |
| `ix_tenant_vertical_installs__tenant_enabled` | **Hot:** load installs at conversation start. `INCLUDE (vertical_version_id, is_primary)` |
| `uq_tenant_vertical_installs__primary_*` | Enforce one primary vertical per scope |
| `ix_tenant_vertical_installs__version` | EOL migration sweep |

### 12.5 Booking

| Index | Query it serves |
|---|---|
| `ix_resources__tenant_type_location` | **Hot:** candidate resources for an availability query. `INCLUDE (capacity, overbook_allowance)` avoids a heap fetch during allocation |
| `ix_resources__attributes` (GIN, `jsonb_path_ops`) | Attribute filters: seats ≥ 4, bed_type = king, specialty = cardiology |
| `ix_resources__tags` (GIN) | Tag-based grouping and filtering |
| `ix_resources__external` | PMS/EMR sync upsert by external id |
| `ix_services__tenant_location_active` | Service catalogue render on booking-flow entry |
| `ix_srr__tenant_service` | **Hot:** load requirements when computing availability |
| `ix_srr__tenant_resource_type` | Impact analysis before deleting a resource type |
| `ix_availability_rules__tenant_location_period` (GiST) | Materialise open windows for a date range |
| `ix_availability_rules__tenant_resource` | Per-resource schedule override lookup |
| `ix_availability_exceptions__tenant_location_period` (GiST) | Overlap a query window against closures |
| `ix_availability_exceptions__tenant_resource_period` (GiST) | Per-resource vacation/blocking overlap |
| `uq_availability_exceptions__external` | Idempotent calendar re-sync |
| `ix_holidays__tenant_date` | Holiday expansion into exceptions |
| `ix_holidays__annual` | Annual recurrence match by month/day |
| `uq_bookings__tenant_reference` | Caller quotes `BK-7F3K2Q` on the phone |
| `uq_bookings__tenant_idempotency` | Prevent a retried agent tool call from double-booking |
| `ix_bookings__tenant_local_date_status` | **Hot:** day view — "today's bookings at this location" |
| `ix_bookings__tenant_service_period` (GiST) | Range overlap for availability and reporting |
| `ix_bookings__tenant_customer_phone` | "Find my reservation" by caller ID |
| `ix_bookings__upcoming` | Reminder scheduling; upcoming list |
| `uq_bookings__recurrence_instance` | Idempotent recurrence expansion |
| **`ex_booking_items__no_overlap` (GiST EXCLUDE)** | **The double-booking constraint.** Doubles as the index for resource-timeline overlap on live, exclusive bookings |
| `ix_booking_items__tenant_resource_occupies` (GiST) | Full resource timeline incl. cancelled, for history views |
| `ix_booking_items__shared_capacity` (GiST) | Capacity summation for shared resources |
| `ex_booking_holds__no_overlap` (GiST EXCLUDE) | Hold-vs-hold conflict |
| `ix_booking_holds__expiry_sweep` | TTL sweeper (cross-tenant, `ops`) |
| `ix_waitlist_entries__match` (GiST) | **Hot on cancellation:** who is waiting for this freed slot |
| `ix_waitlist_entries__tenant_priority` | Offer ranking within the matched set |
| `ix_recurrence_rules__expansion` | Rolling-horizon expansion sweep |

Two `bookings` indexes referenced above are declared here (they belong to §5.9
but are grouped with their justification):

```sql
-- Day view: "everything at this location today", the single most-run booking
-- query in the product. local_date is a plain DATE so this is a cheap btree.
CREATE INDEX ix_bookings__tenant_local_date_status
    ON bookings (tenant_id, location_id, local_date, status)
    INCLUDE (service_id, reference, starts_at, party_size);

-- Range overlap for availability checks and reporting.
CREATE INDEX ix_bookings__tenant_service_period
    ON bookings USING gist (tenant_id, service_period);

-- Caller-ID lookup: "find my reservation".
CREATE INDEX ix_bookings__tenant_customer_phone
    ON bookings (tenant_id, customer_phone_e164, starts_at DESC)
    WHERE customer_phone_e164 IS NOT NULL;

-- Reminder scheduling and the "upcoming" list. Partial on live statuses keeps
-- it proportional to the future, not to all history.
CREATE INDEX ix_bookings__upcoming
    ON bookings (tenant_id, starts_at)
    WHERE status IN ('pending','confirmed');

-- Conversation → booking drill-down.
CREATE INDEX ix_bookings__tenant_conversation
    ON bookings (tenant_id, conversation_id) WHERE conversation_id IS NOT NULL;

-- Customer history.
CREATE INDEX ix_bookings__tenant_customer
    ON bookings (tenant_id, customer_id, starts_at DESC) WHERE customer_id IS NOT NULL;

-- Workflow correlation.
CREATE INDEX ix_bookings__workflow
    ON bookings (tenant_id, workflow_id) WHERE workflow_id IS NOT NULL;
```

### 12.6 Conversations

| Index | Query it serves |
|---|---|
| `pk_conversations` `(tenant_id, created_at, id)` | Partition-pruned point lookup; also serves "newest first" |
| `ix_conversations__tenant_started` | Conversation list — the portal landing page |
| `ix_conversations__tenant_channel_started` | Filtered list by channel |
| `uq_conversations__tenant_external` | **Hot:** inbound webhook resolves CallSid → conversation |
| `ix_conversations__tenant_contact` | "Every call from this number" |
| `ix_conversations__escalated` | Escalation review queue |
| `uq_turns__conversation_sequence` | **Hottest query in the system:** replay a conversation in order, on every agent invocation |
| `ix_turns__tenant_created` | Token/cost rollups by day |
| `ix_turns__tenant_tool` | Tool reliability and latency analytics |
| `ix_turns__tenant_model` | Model mix and per-model spend |
| `ix_transcripts__search` (GIN) | Full-text search across call transcripts |
| `ix_call_recordings__retention` | Retention reaper for audio objects |
| `uq_conversation_outcomes__conversation_kind` | Idempotent outcome classification |
| `ix_conversation_outcomes__tenant_kind_created` | Resolution-mix dashboard |
| `ix_conversation_outcomes__review_queue` | Low-confidence human review queue |

### 12.7 Knowledge

| Index | Query it serves |
|---|---|
| `ix_knowledge_sources__next_sync` | Sync scheduler sweep (cross-tenant, `ops`) |
| `uq_documents__source_external` | Re-crawl upsert by URL / file id |
| `ix_documents__metadata` (GIN) | Metadata-filtered retrieval |
| `ix_documents__last_seen` | Stale-document detection after a crawl |
| `ix_document_versions__content_hash` | Skip-if-unchanged: the single biggest embedding-cost saver |
| `ix_document_versions__pending` | Reindex worker queue |
| **`ix_chunks__embedding_hnsw`** | **Hot:** vector similarity on every RAG turn |
| `ix_chunks__search_vector` (GIN) | Lexical half of hybrid retrieval; one config-aware index serves every language (§7.4.1) |
| `ix_chunks__scope_tags` (GIN) | Pre-filter by agent scope |
| `ix_chunks__tenant_version` | `VectorStorePort.delete_by_version` — atomic reindex |
| `ix_chunks__unembedded` | Embedding backfill worker |
| `ix_ingestion_jobs__tenant_active` | Per-tenant ingestion concurrency cap |

### 12.8 Connectors, Workflows, Ops

| Index | Query it serves |
|---|---|
| `uq_tenant_connectors__webhook_token` | Inbound webhook routes to a tenant with no auth header |
| `ix_tenant_connectors__health` | Connector health dashboard and alerting |
| `ix_connector_credentials__access_expiry` | OAuth refresh sweeper (cross-tenant, `ops`) |
| `uq_sync_state__connector_stream_scope` | Cursor upsert per stream |
| `ix_sync_state__claimable` | Worker claim with `FOR UPDATE SKIP LOCKED` |
| `ix_workflow_definitions__trigger_event` | Event dispatch: which workflows fire on this event type |
| `uq_workflow_runs__workflow_run` | Temporal's natural key; reconciler upsert |
| `uq_workflow_runs__live_workflow_id` | Idempotency: is a run already live for this entity |
| `ix_workflow_runs__terminal_failures` | Ops queue for non-retryable failures |
| `ix_ai_checkpoints__thread_latest` | **Hot:** latest checkpoint for a thread, on every graph resume |
| `uq_ai_checkpoints__thread_ns_ckpt` | LangGraph natural key; checkpointer upsert |
| `ix_ai_checkpoints__conversation` | Targeted checkpoint erasure alongside a conversation |
| `ix_audit_log__tenant_entity` | "Everything that happened to booking X" |
| `ix_audit_log__tenant_actor` | "Everything user U did" — offboarding and IR |
| `ix_audit_log__phi` | HIPAA access-report export |
| **`ix_outbox_events__relay`** | **Hot:** the relay claim query. Partial on `status='pending'` so cost tracks the backlog, not history |
| `ix_usage_events__tenant_meter_created` | Billing rollup. `INCLUDE (quantity)` → index-only aggregate |
| `ix_usage_events__unbilled` | Unbilled sweep at period close |
| `ix_cost_records__tenant_provider_occurred` | Margin dashboard. `INCLUDE (amount, currency)` |
| `ix_notifications__send_queue` | Sender claim query; partial on pending |
| `uq_notifications__provider_message` | Delivery-webhook callback resolution |

### 12.9 Indexes deliberately NOT created

- **No index on `bookings.status` alone.** Status has ~8 values and terrible
  selectivity; every useful status query also filters `tenant_id` and a date,
  and those composites already exist.
- **No index on `turns.role`.** Five values, always queried alongside
  `conversation_id`, which is already covered.
- **No GIN on `bookings.intake_data`.** It is read by primary key, never
  searched. A GIN index here would cost writes on the booking hot path for a
  query nobody runs. If intake search is ever needed, it goes through the
  knowledge/search layer.
- **No trigram index on `resources.name`.** Resource counts per tenant are in
  the hundreds; a sequential scan within an already-`tenant_id`-filtered set is
  faster than an index probe.
- **No covering index for the conversation list beyond `started_at`.** The list
  view fetches 25 rows; a heap fetch for 25 rows is not worth widening the index.

---

## 13. Constraints, Double-Booking, and Hold TTL Semantics

### 13.1 The exclusion constraint, in detail

```sql
ALTER TABLE booking_items
    ADD CONSTRAINT ex_booking_items__no_overlap
    EXCLUDE USING gist (
        tenant_id   WITH =,
        resource_id WITH =,
        occupies    WITH &&
    )
    WHERE (is_blocking AND NOT is_capacity_shared)
    DEFERRABLE INITIALLY IMMEDIATE;
```

**What it means operationally.** PostgreSQL builds a GiST index over
`(tenant_id, resource_id, occupies)`. On every insert or update of a qualifying
row, it probes that index for any existing row where all three operators hold
simultaneously. If one is found, the statement fails with SQLSTATE `23P01`
(`exclusion_violation`).

**Why this and not application-level checking.** The naive approach is:

```sql
SELECT 1 FROM booking_items WHERE resource_id = $1 AND occupies && $2;  -- check
INSERT INTO booking_items ...;                                          -- then act
```

Under `READ COMMITTED` — the default, and what we run — two concurrent
transactions both see no conflict, both insert, and both commit. Two people are
booked into the same chair. Fixing this in application code requires either
`SERIALIZABLE` isolation on the entire booking path (with its retry storms) or
advisory locks keyed by resource (correct, but a hand-rolled lock manager whose
every future caller must remember to use it). The exclusion constraint pushes
the guarantee into the storage engine, where **no code path can forget it** —
including a psql session, a data-fix script, or a bulk import.

**Why `&&` and not `OVERLAPS`.** `&&` is the range overlap operator with GiST
index support. `OVERLAPS` is a SQL construct over four scalar endpoints; it is
not indexable and cannot appear in an `EXCLUDE`.

**Why half-open `[)` ranges are non-negotiable.** With inclusive `[]` ranges,
`[10:00, 10:30]` and `[10:30, 11:00]` overlap at the instant `10:30`, so every
back-to-back booking would be rejected. Every range in this schema is
constructed `[)`. The `tstzrange(a, b, '[)')` in `bookings.blocking_period`'s
generated expression makes this explicit rather than relying on the default.

### 13.2 Holds and bookings must not conflict with each other

The exclusion constraints in §5.10 and §5.11 each cover their own table.
`booking_items` cannot conflict with `booking_holds` via a constraint, because
PostgreSQL exclusion constraints are single-table.

Three options were considered:

| Option | Verdict |
|---|---|
| One `occupancy` table holding both holds and confirmed items, discriminated by a `kind` column, with one EXCLUDE over it | Correct, but couples two very different lifecycles and write patterns; hold churn (thousands/hour, all short-lived) would bloat the same index that protects confirmed bookings |
| A materialised view with an EXCLUDE | Not possible; exclusion constraints require a real table |
| **Separate tables + explicit conflict check under a resource lock** | **Chosen** |

The chosen design: converting a hold into a booking, and creating a booking
without a hold, both go through one function that takes a **transaction-scoped
advisory lock keyed by `(tenant_id, resource_id)`** and then checks both tables.

```sql
CREATE OR REPLACE FUNCTION booking_check_conflicts(
    p_tenant_id     UUID,
    p_resource_id   UUID,
    p_occupies      TSTZRANGE,
    p_ignore_hold_id UUID DEFAULT NULL,
    p_ignore_booking_id UUID DEFAULT NULL
)
RETURNS TABLE (conflict_kind TEXT, conflict_id UUID, conflict_range TSTZRANGE)
LANGUAGE plpgsql
AS $$
DECLARE
    lock_key BIGINT;
BEGIN
    -- Serialise all conflict evaluation for this (tenant, resource). The key
    -- is a 64-bit hash of both ids; collisions are harmless (they only cause
    -- two unrelated resources to serialise briefly), and hashtextextended is
    -- stable across sessions.
    lock_key := hashtextextended(p_tenant_id::text || ':' || p_resource_id::text, 0);
    PERFORM pg_advisory_xact_lock(lock_key);

    RETURN QUERY
    SELECT 'booking'::TEXT, bi.booking_id, bi.occupies
      FROM booking_items bi
     WHERE bi.tenant_id = p_tenant_id
       AND bi.resource_id = p_resource_id
       AND bi.is_blocking
       AND NOT bi.is_capacity_shared
       AND bi.occupies && p_occupies
       AND (p_ignore_booking_id IS NULL OR bi.booking_id <> p_ignore_booking_id)

    UNION ALL

    SELECT 'hold'::TEXT, h.id, h.occupies
      FROM booking_holds h
     WHERE h.tenant_id = p_tenant_id
       AND h.resource_id = p_resource_id
       AND h.status = 'active'
       -- ── The TTL check, evaluated at read time. A hold whose expires_at has
       --    passed does not block, even if the sweeper has not run yet.
       AND h.expires_at > now()
       AND NOT h.is_capacity_shared
       AND h.occupies && p_occupies
       AND (p_ignore_hold_id IS NULL OR h.id <> p_ignore_hold_id);
END;
$$;
```

**The advisory lock is what makes this safe**, and it is deliberately narrow:
it is scoped to one `(tenant, resource)` pair and released automatically at
transaction end (`pg_advisory_xact_lock`, not `pg_advisory_lock` — the session
variant leaks locks on a crashed connection). Bookings for different resources
never contend. Concurrency is per-resource, which is exactly the granularity the
business problem has.

The exclusion constraints remain in place as the **backstop**. The advisory lock
prevents the hold-vs-booking race; the exclusion constraint guarantees that even
if someone bypasses `booking_check_conflicts` entirely, booking-vs-booking is
still impossible. Defence in depth: the lock provides *coordination*, the
constraint provides *correctness*.

### 13.3 Hold TTL semantics — three layers

A hold's TTL must be enforced even if every background process is down. It is:

**Layer 1 — Read-time predicate (authoritative).** Every conflict check
includes `AND h.expires_at > now()`. An expired hold **stops blocking the moment
its TTL passes**, with no sweeper involvement. This is what makes the system
correct rather than merely tidy: a dead worker cannot cause a resource to be
permanently unbookable.

**Layer 2 — Temporal timer (timely).** The booking workflow that created the
hold sets a durable timer for `expires_at`. When it fires, the workflow releases
the hold explicitly, records the reason, and — if the caller abandoned — may
offer the slot to the waitlist. This is the *fast, well-behaved* path and the
one that actually runs 99.9% of the time.

**Layer 3 — Sweeper (janitorial).** `ops_expire_stale_holds()` from §11.4 runs
every 30 seconds and flips stragglers to `status = 'expired'`. Its purpose is
**not correctness** — Layer 1 already gives that. It exists so that (a) the
`ex_booking_holds__no_overlap` constraint's partial index stays small, since its
predicate is `status = 'active'` and cannot reference `now()`, and (b) admin
views show accurate state.

> **Why `expires_at` cannot be in the exclusion constraint's `WHERE` clause.**
> A partial index predicate must be `IMMUTABLE`. `now()` is `STABLE`. So
> `WHERE (status = 'active' AND expires_at > now())` is rejected by PostgreSQL.
> This is the reason the design needs three layers rather than one: the
> constraint can only know about `status`, so an expired-but-unswept hold
> *would* still block a competing hold at the constraint level. That is
> acceptable — and self-correcting within 30 seconds — because holds are
> short-lived and because the *booking* path (Layer 1) already ignores them.

**The hold → booking conversion, atomically.**

```sql
CREATE OR REPLACE FUNCTION booking_convert_hold(
    p_tenant_id   UUID,
    p_hold_id     UUID,
    p_booking_id  UUID
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    h booking_holds%ROWTYPE;
BEGIN
    -- Lock the hold row. If another transaction is converting or releasing it,
    -- we block here rather than racing.
    SELECT * INTO h
      FROM booking_holds
     WHERE id = p_hold_id AND tenant_id = p_tenant_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'hold % not found', p_hold_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF h.status <> 'active' THEN
        RAISE EXCEPTION 'hold % is %, not active', p_hold_id, h.status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF h.expires_at <= now() THEN
        -- The TTL passed while the caller was talking. Fail loudly; the agent
        -- must re-check availability and re-offer, not silently double-book.
        UPDATE booking_holds
           SET status = 'expired', released_at = now(),
               release_reason = 'ttl_expired_at_conversion'
         WHERE id = p_hold_id;
        RAISE EXCEPTION 'hold % expired at %', p_hold_id, h.expires_at
            USING ERRCODE = 'lock_not_available';
    END IF;

    -- Ignore THIS hold when checking conflicts — it is the one being converted.
    IF EXISTS (
        SELECT 1 FROM booking_check_conflicts(
            p_tenant_id, h.resource_id, h.occupies, p_hold_id, NULL)
    ) THEN
        RAISE EXCEPTION 'conflict converting hold %', p_hold_id
            USING ERRCODE = 'exclusion_violation';
    END IF;

    -- Release the hold in the SAME transaction that inserts the booking_item.
    -- Order matters: releasing first means the hold no longer blocks, and the
    -- exclusion constraint on booking_items then guards the insert.
    UPDATE booking_holds
       SET status = 'converted', booking_id = p_booking_id,
           converted_at = now(), updated_at = now()
     WHERE id = p_hold_id;

    INSERT INTO booking_items (tenant_id, booking_id, resource_id, quantity, occupies)
    VALUES (p_tenant_id, p_booking_id, h.resource_id, h.quantity, h.occupies);
    -- occupies/is_blocking/is_capacity_shared are then overwritten by
    -- trg_booking_items_sync_occupancy from the parent booking. The value
    -- passed here only satisfies NOT NULL.
END;
$$;
```

Because the whole function runs in one transaction, either the hold becomes a
booking or nothing changes. There is no window in which the slot is
unprotected — the hold's release and the item's insert commit together.

### 13.4 `allow_double_booking` — the deliberate override

Some verticals genuinely overbook: a clinic that triple-books 10-minute slots
knowing a third of patients no-show; a restaurant that seats two parties at a
communal table. `services.allow_double_booking` handles this **without weakening
the constraint**, by routing those bookings to a resource whose type has
`is_capacity_shared = true` and whose `capacity` / `overbook_allowance` express
the policy numerically. The exclusion constraint's `WHERE NOT is_capacity_shared`
predicate then correctly exempts them, and the shared-capacity trigger (§5.10)
enforces the real limit.

This is the payoff of putting the switch on `resource_types` rather than
special-casing verticals: "overbooking" is not a new code path, it is the shared
capacity path with `overbook_allowance > 0`.

### 13.5 Full constraint inventory

**Foreign keys.** Every FK between tenant-owned tables is composite
`(tenant_id, x_id) → (tenant_id, id)`, per §2.2. Delete behaviour:

| Behaviour | Used for | Reason |
|---|---|---|
| `ON DELETE CASCADE` | children of `tenants`, `locations`, `bookings`→`booking_items`, `document_versions`→`chunks` | The child has no meaning without the parent |
| `ON DELETE RESTRICT` | `bookings`→`services`, `bookings`→`locations`, `booking_items`→`resources`, `subscriptions`→`plans` | Deleting a service with live bookings must fail loudly; the admin soft-deletes instead |
| `ON DELETE SET NULL` | `*_by_user_id`, `secret_id`, `connector_id` | The reference is provenance; losing it must not destroy the record |

**CHECK constraints** fall into five recurring families:

1. **Format** — regex on slugs, codes, E.164 phones, ISO currency, semver,
   event-type names. Catches typos at the boundary rather than in a dashboard.
2. **Range** — non-negative money, quantity ≥ 1, sentiment ∈ [-1,1], timeouts
   within sane bounds.
3. **Pairing** — `(latitude IS NULL) = (longitude IS NULL)`,
   `(cost IS NULL) = (cost_currency IS NULL)`, `(monthly_budget IS NULL) =
   (budget_currency IS NULL)`. Half-populated pairs are always a bug.
4. **State coherence** — `(status = 'cancelled') = (cancelled_at IS NOT NULL)`.
   Prevents the classic "status says cancelled but the timestamp is null"
   inconsistency that makes reports lie.
5. **Shape** — `jsonb_typeof(x) = 'object'`, mandatory manifest keys, per-factor
   MFA payload requirements. Structural gates on JSONB, with deep validation in
   the application.

**Exclusion constraints** — two, both documented above.

**Unique constraints** — note the pervasive use of *partial* uniques
(`WHERE deleted_at IS NULL`, `WHERE status = 'active'`). This is what makes soft
delete and lifecycle states coexist with uniqueness: a cancelled booking does
not reserve its reference forever, and a deleted location frees its code.

---

## 14. The Generic Booking Model, Proven

The claim to defend: **one schema, no vertical-specific columns, handles a
medical appointment, a restaurant table for 4 at 7 pm, and a hotel room for 3
nights.** Below, each is written out as literal `INSERT` statements against the
tables in §5. Nothing is elided.

The only tables involved are the same seven every time: `resource_types`,
`resources`, `services`, `service_resource_requirements`, `availability_rules`,
`bookings`, `booking_items`. What differs is **data**.

### 14.0 The four dials

| Dial | Where | Medical | Restaurant | Hotel |
|---|---|---|---|---|
| Time unit | `resource_types.time_unit` | `minute` | `minute` | `night` |
| Slot grid | `resource_types.slot_granularity_minutes` | `15` | `NULL` (free-floating) | `NULL` (day boundaries) |
| Capacity | `resource_types.is_capacity_shared` + `resources.capacity` | exclusive, 1 | exclusive, 1 | exclusive, 1 |
| Matching | `service_resource_requirements.attribute_predicate` | `specialty` | `seats >= party_size` | `bed_type ∈ {…}` |

Everything else — the exclusion constraint, the availability materialiser, the
allocation strategy, the hold/confirm workflow — is identical code.

---

### 14.1 Worked example A — Medical appointment

*"30-minute cardiology follow-up with Dr Chen, Tuesday 2026-03-10 at 14:00
America/New_York, 15-minute room-cleaning buffer after."*

**Setup.**

```sql
-- Tenant, location
INSERT INTO tenants (id, slug, legal_name, display_name, status, default_timezone,
                     default_currency, compliance_profile)
VALUES ('01890000-0000-7000-8000-000000000001', 'northside-cardio',
        'Northside Cardiology PC', 'Northside Cardiology', 'active',
        'America/New_York', 'USD', 'hipaa');

INSERT INTO locations (id, tenant_id, code, name, timezone, country_code)
VALUES ('01890000-0000-7000-8000-0000000000a1',
        '01890000-0000-7000-8000-000000000001',
        'main', 'Main Clinic', 'America/New_York', 'US');

-- Resource types: a physician (human) and an exam room (space).
INSERT INTO resource_types
    (id, tenant_id, code, name, kind, is_capacity_shared, time_unit,
     slot_granularity_minutes, attribute_schema, filterable_attributes)
VALUES
('01890000-0000-7000-8000-0000000000b1',
 '01890000-0000-7000-8000-000000000001',
 'physician', 'Physician', 'human', FALSE, 'minute', 15,
 '{"type":"object","properties":{
     "specialty":{"type":"string"},
     "npi":{"type":"string"},
     "languages":{"type":"array","items":{"type":"string"}},
     "accepts_new_patients":{"type":"boolean"}}}'::jsonb,
 ARRAY['specialty','languages','accepts_new_patients']),

('01890000-0000-7000-8000-0000000000b2',
 '01890000-0000-7000-8000-000000000001',
 'exam_room', 'Exam Room', 'space', FALSE, 'minute', 15,
 '{"type":"object","properties":{
     "equipment":{"type":"array","items":{"type":"string"}},
     "wheelchair_accessible":{"type":"boolean"}}}'::jsonb,
 ARRAY['equipment','wheelchair_accessible']);

-- Resources: Dr Chen, and Room 3 which has an ECG machine.
INSERT INTO resources
    (id, tenant_id, location_id, resource_type_id, code, name, capacity, attributes)
VALUES
('01890000-0000-7000-8000-0000000000c1',
 '01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000a1',
 '01890000-0000-7000-8000-0000000000b1',
 'dr-chen', 'Dr. Alice Chen', 1,
 '{"specialty":"cardiology","npi":"1234567890",
   "languages":["en","zh"],"accepts_new_patients":true}'::jsonb),

('01890000-0000-7000-8000-0000000000c2',
 '01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000a1',
 '01890000-0000-7000-8000-0000000000b2',
 'room-3', 'Exam Room 3', 1,
 '{"equipment":["ecg","ultrasound"],"wheelchair_accessible":true}'::jsonb);

-- Service: 30 minutes, 15-minute cleaning buffer after, one patient.
INSERT INTO services
    (id, tenant_id, location_id, code, name, duration_minutes,
     buffer_before_minutes, buffer_after_minutes,
     min_party_size, max_party_size,
     lead_time_minutes, booking_horizon_days, cancellation_window_minutes,
     price, currency, price_model, intake_schema)
VALUES
('01890000-0000-7000-8000-0000000000d1',
 '01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000a1',
 'cardio-followup', 'Cardiology Follow-up', 30,
 0, 15,
 1, 1,
 120, 90, 1440,
 250.0000, 'USD', 'flat',
 '[{"key":"reason_for_visit","type":"string","required":true,"pii_class":"phi",
    "prompt":"What brings you in today?"},
   {"key":"insurance_member_id","type":"string","required":true,"pii_class":"phi",
    "prompt":"What is your insurance member ID?"}]'::jsonb);

-- Requirements: ONE cardiologist AND ONE ECG-equipped room.
-- Two rows, two roles. This is a multi-resource booking and it needs no
-- special handling anywhere.
INSERT INTO service_resource_requirements
    (tenant_id, service_id, resource_type_id, role, quantity,
     units_per_resource, attribute_predicate, allocation_strategy, respects_buffers)
VALUES
('01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000d1',
 '01890000-0000-7000-8000-0000000000b1',
 'primary', 1, 1,
 '{"match":{"specialty":"cardiology"}}'::jsonb,
 'least_utilised',
 -- The physician is free the moment the visit ends; cleaning is the room's
 -- problem, not the doctor's.
 FALSE),

('01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000d1',
 '01890000-0000-7000-8000-0000000000b2',
 'space', 1, 1,
 '{"match":{"equipment":["ecg"]}}'::jsonb,
 'first_available',
 -- The room IS blocked through cleanup.
 TRUE);

-- Availability: Dr Chen works Mon-Fri 09:00-17:00 local.
INSERT INTO availability_rules
    (tenant_id, location_id, resource_id, effect, weekdays,
     start_time, end_time, effective_period)
VALUES
('01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000a1',
 '01890000-0000-7000-8000-0000000000c1',
 'open', ARRAY[1,2,3,4,5]::smallint[],
 '09:00', '17:00', daterange('2026-01-01', NULL, '[)'));
```

**The booking.** 14:00 America/New_York on 2026-03-10 is 18:00 UTC (EDT, UTC-4).

```sql
INSERT INTO bookings
    (id, tenant_id, location_id, service_id, reference, status,
     service_period, buffer_before_minutes, buffer_after_minutes,
     local_date, party_size, duration_units,
     customer_name, customer_phone_e164, intake_data,
     channel, booked_by, price_total, currency,
     confirmed_at, idempotency_key)
VALUES
('01890000-0000-7000-8000-0000000000e1',
 '01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000a1',
 '01890000-0000-7000-8000-0000000000d1',
 'BK-7F3K2Q', 'confirmed',
 tstzrange('2026-03-10 18:00:00+00', '2026-03-10 18:30:00+00', '[)'),
 0, 15,
 DATE '2026-03-10', 1, 1,
 'Robert Kim', '+12125550147',
 '{"reason_for_visit":"post-stent follow-up","insurance_member_id":"BCBS-448120"}'::jsonb,
 'voice_inbound', 'agent', 250.0000, 'USD',
 now(), 'conv-9f2a:book:1');
```

`blocking_period` is computed automatically:
`[2026-03-10 18:00+00, 2026-03-10 18:45+00)` — 30 minutes of service plus 15
minutes of cleaning.

> **CORRECTION.** The value above is right, but this example cannot be run
> against the `bookings` DDL exactly as §5.9 prints it: that DDL is rejected at
> `CREATE TABLE` with `generation expression is not immutable`, because
> `timestamptz + interval` is STABLE. See the correction note in §5.9 — the
> shipped schema uses an IMMUTABLE `booking_shift_minutes()` helper and produces
> this same `[18:00, 18:45)` result. Nothing else in this worked example changes.

```sql
-- Two allocations. Note the DIFFERENT occupancy ranges, driven entirely by
-- respects_buffers on the requirement row.
INSERT INTO booking_items
    (tenant_id, booking_id, resource_id, role, requirement_id, quantity, occupies)
VALUES
-- Dr Chen: service range only → free at 18:30 UTC (14:30 local).
('01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000e1',
 '01890000-0000-7000-8000-0000000000c1',
 'primary',
 (SELECT id FROM service_resource_requirements
   WHERE service_id = '01890000-0000-7000-8000-0000000000d1' AND role = 'primary'),
 1, tstzrange('2026-03-10 18:00:00+00','2026-03-10 18:30:00+00','[)')),

-- Room 3: blocking range → free at 18:45 UTC (14:45 local).
('01890000-0000-7000-8000-000000000001',
 '01890000-0000-7000-8000-0000000000e1',
 '01890000-0000-7000-8000-0000000000c2',
 'space',
 (SELECT id FROM service_resource_requirements
   WHERE service_id = '01890000-0000-7000-8000-0000000000d1' AND role = 'space'),
 1, tstzrange('2026-03-10 18:00:00+00','2026-03-10 18:45:00+00','[)'));
```

**What the constraint now guarantees.** A second patient booked with Dr Chen at
14:15 raises `23P01` — his `occupies` range `[18:15, 18:45)` overlaps
`[18:00, 18:30)`. A second patient in Room 3 at 14:40 also fails: `[18:40, …)`
overlaps the room's `[18:00, 18:45)`. But a second patient with a *different*
cardiologist in Room 3 at 14:45 succeeds — the room is free at exactly 18:45
because the range is half-open.

**Vertical-specific columns used: zero.** `specialty` lives in
`resources.attributes`. `reason_for_visit` lives in `bookings.intake_data`.
Cleaning time lives in `services.buffer_after_minutes`.

---

### 14.2 Worked example B — Restaurant table for 4 at 7 pm

*"Table for 4, Saturday 2026-03-14 at 19:00 America/New_York, 90-minute dining
window, 15-minute table reset."*

The interesting question this example must answer: **how does a schema whose
capacity unit is "the resource" handle "a party of 4 needs a table that seats at
least 4"?** The answer is that seat count is an *attribute predicate*, not a
capacity subdivision — and the difference matters.

**Setup.**

```sql
INSERT INTO tenants (id, slug, legal_name, display_name, status,
                     default_timezone, default_currency)
VALUES ('01890000-0000-7000-8000-000000000002', 'osteria-blu',
        'Osteria Blu LLC', 'Osteria Blu', 'active', 'America/New_York', 'USD');

INSERT INTO locations (id, tenant_id, code, name, timezone, country_code)
VALUES ('01890000-0000-7000-8000-0000000000a2',
        '01890000-0000-7000-8000-000000000002',
        'main', 'Osteria Blu', 'America/New_York', 'US');

-- ONE resource type. Note: free-floating (slot_granularity NULL) because
-- 19:13 is a perfectly legitimate reservation time in a restaurant.
INSERT INTO resource_types
    (id, tenant_id, code, name, kind, is_capacity_shared, time_unit,
     slot_granularity_minutes, attribute_schema, filterable_attributes)
VALUES
('01890000-0000-7000-8000-0000000000b3',
 '01890000-0000-7000-8000-000000000002',
 'dining_table', 'Dining Table', 'space', FALSE, 'minute', NULL,
 '{"type":"object","properties":{
     "seats":{"type":"integer"},
     "min_party":{"type":"integer"},
     "section":{"type":"string"},
     "combinable_with":{"type":"array","items":{"type":"string"}},
     "accessible":{"type":"boolean"}}}'::jsonb,
 ARRAY['seats','section','accessible']);

-- The floor plan. Capacity is 1 for every table: a table hosts ONE party at a
-- time. Seat count is an ATTRIBUTE.
INSERT INTO resources
    (id, tenant_id, location_id, resource_type_id, code, name, capacity, attributes)
VALUES
('01890000-0000-7000-8000-0000000000c3',
 '01890000-0000-7000-8000-000000000002','01890000-0000-7000-8000-0000000000a2',
 '01890000-0000-7000-8000-0000000000b3','T04','Table 4', 1,
 '{"seats":2,"min_party":1,"section":"window","accessible":true}'::jsonb),

('01890000-0000-7000-8000-0000000000c4',
 '01890000-0000-7000-8000-000000000002','01890000-0000-7000-8000-0000000000a2',
 '01890000-0000-7000-8000-0000000000b3','T12','Table 12', 1,
 '{"seats":4,"min_party":3,"section":"main","accessible":true,
   "combinable_with":["T13"]}'::jsonb),

('01890000-0000-7000-8000-0000000000c5',
 '01890000-0000-7000-8000-000000000002','01890000-0000-7000-8000-0000000000a2',
 '01890000-0000-7000-8000-0000000000b3','T20','Table 20', 1,
 '{"seats":8,"min_party":5,"section":"patio","accessible":false}'::jsonb);

-- Service. Party size 1..12 is the RANGE THE SERVICE ACCEPTS — not a capacity.
INSERT INTO services
    (id, tenant_id, location_id, code, name, duration_minutes,
     buffer_before_minutes, buffer_after_minutes,
     min_party_size, max_party_size,
     lead_time_minutes, booking_horizon_days, cancellation_window_minutes,
     price_model, allow_waitlist, intake_schema)
VALUES
('01890000-0000-7000-8000-0000000000d2',
 '01890000-0000-7000-8000-000000000002','01890000-0000-7000-8000-0000000000a2',
 'dinner', 'Dinner Reservation', 90,
 0, 15,
 1, 12,
 30, 60, 120,
 'free', TRUE,
 '[{"key":"occasion","type":"string","required":false,"pii_class":"none",
    "prompt":"Any special occasion?"},
   {"key":"dietary_notes","type":"string","required":false,"pii_class":"low",
    "prompt":"Any allergies or dietary restrictions?"}]'::jsonb);

-- ════════════════════════════════════════════════════════════════════════════
-- THE KEY ROW. One table, sized for the party, chosen by best fit.
--
--   "gte": {"seats": "$party_size"}  → seats >= 4
--   "lte": {"min_party": "$party_size"} → don't put 2 people at an 8-top
--   allocation_strategy = 'best_fit'  → prefer T12 (4 seats) over T20 (8)
--
-- $party_size is bound from bookings.party_size at allocation time. The engine
-- substitutes it; the predicate itself is vertical-agnostic.
INSERT INTO service_resource_requirements
    (tenant_id, service_id, resource_type_id, role, quantity,
     units_per_resource, attribute_predicate, allocation_strategy, respects_buffers)
VALUES
('01890000-0000-7000-8000-000000000002',
 '01890000-0000-7000-8000-0000000000d2',
 '01890000-0000-7000-8000-0000000000b3',
 'primary', 1, 1,
 '{"gte":{"seats":"$party_size"},"lte":{"min_party":"$party_size"}}'::jsonb,
 'best_fit',
 TRUE);

-- Service hours: dinner Tue-Sun 17:00-22:00 local.
INSERT INTO availability_rules
    (tenant_id, location_id, resource_type_id, service_id, effect, weekdays,
     start_time, end_time, effective_period)
VALUES
('01890000-0000-7000-8000-000000000002',
 '01890000-0000-7000-8000-0000000000a2',
 '01890000-0000-7000-8000-0000000000b3',
 '01890000-0000-7000-8000-0000000000d2',
 'open', ARRAY[2,3,4,5,6,7]::smallint[],
 '17:00', '22:00', daterange('2026-01-01', NULL, '[)'));
```

**The allocation query the engine actually runs.** This is the generic query;
nothing in it mentions restaurants:

```sql
-- Given: tenant, location, service, party_size=4, desired window.
-- Returns candidate resources, best fit first.
WITH req AS (
    SELECT srr.*
      FROM service_resource_requirements srr
     WHERE srr.tenant_id = $1 AND srr.service_id = $2 AND srr.role = 'primary'
),
candidates AS (
    SELECT r.id, r.code, r.attributes, r.capacity
      FROM resources r, req
     WHERE r.tenant_id = $1
       AND r.location_id = $3
       AND r.resource_type_id = req.resource_type_id
       AND r.is_active AND r.is_bookable AND r.deleted_at IS NULL
       -- "gte" predicate: seats >= party_size
       AND (req.attribute_predicate -> 'gte' -> 'seats' IS NULL
            OR (r.attributes ->> 'seats')::int >= $4)
       -- "lte" predicate: min_party <= party_size
       AND (req.attribute_predicate -> 'lte' -> 'min_party' IS NULL
            OR COALESCE((r.attributes ->> 'min_party')::int, 0) <= $4)
)
SELECT c.id, c.code, (c.attributes ->> 'seats')::int AS seats
  FROM candidates c
 WHERE NOT EXISTS (
     SELECT 1 FROM booking_items bi
      WHERE bi.tenant_id = $1
        AND bi.resource_id = c.id
        AND bi.is_blocking
        AND bi.occupies && $5::tstzrange
 )
 AND NOT EXISTS (
     SELECT 1 FROM booking_holds h
      WHERE h.tenant_id = $1
        AND h.resource_id = c.id
        AND h.status = 'active'
        AND h.expires_at > now()
        AND h.occupies && $5::tstzrange
 )
 -- best_fit: smallest table that still fits the party.
 ORDER BY (c.attributes ->> 'seats')::int ASC
 LIMIT 1;
```

For party_size = 4: T04 (2 seats) fails the `gte`; T20 (min_party 5) fails the
`lte`; **T12 is selected**. Exactly the seating decision a host makes.

**The booking.** 19:00 EDT = 23:00 UTC.

```sql
INSERT INTO bookings
    (id, tenant_id, location_id, service_id, reference, status,
     service_period, buffer_before_minutes, buffer_after_minutes,
     local_date, party_size, duration_units,
     customer_name, customer_phone_e164, intake_data,
     channel, booked_by, confirmed_at, idempotency_key)
VALUES
('01890000-0000-7000-8000-0000000000e2',
 '01890000-0000-7000-8000-000000000002',
 '01890000-0000-7000-8000-0000000000a2',
 '01890000-0000-7000-8000-0000000000d2',
 'BK-2M8XPT', 'confirmed',
 tstzrange('2026-03-14 23:00:00+00','2026-03-15 00:30:00+00','[)'),
 0, 15,
 DATE '2026-03-14',      -- the LOCAL date; note the UTC range crosses midnight
 4, 1,
 'Dana Alvarez', '+16465550188',
 '{"occasion":"anniversary","dietary_notes":"one shellfish allergy"}'::jsonb,
 'web_chat', 'agent', now(), 'conv-3c7b:book:1');

INSERT INTO booking_items
    (tenant_id, booking_id, resource_id, role, requirement_id, quantity, occupies)
VALUES
('01890000-0000-7000-8000-000000000002',
 '01890000-0000-7000-8000-0000000000e2',
 '01890000-0000-7000-8000-0000000000c4',   -- T12
 'primary',
 (SELECT id FROM service_resource_requirements
   WHERE service_id = '01890000-0000-7000-8000-0000000000d2'),
 1, tstzrange('2026-03-14 23:00:00+00','2026-03-15 00:45:00+00','[)'));
```

T12 is blocked until 00:45 UTC (20:45 local) — 90 minutes of dining plus 15
minutes of reset. The next seating at T12 can start at exactly 20:45 local.

**Note `local_date = 2026-03-14` while the UTC range crosses into the 15th.**
This is precisely why `local_date` is stored rather than derived: a "Saturday
night" day-view must include this booking, and `date_trunc('day', starts_at)` in
UTC would file it under Sunday.

**Party of 10 needing two combined tables.** No schema change. Either the
restaurant defines a `combined_12_13` resource (a virtual table with
`seats: 8`), or the requirement row is set to `quantity: 2` with
`attribute_predicate: {"gte": {"seats": 4}}` — two tables of at least 4 seats
each. The engine allocates two `booking_items` rows, and the exclusion
constraint independently protects both.

**Vertical-specific columns used: zero.** Seats, section, and combinability live
in `resources.attributes`. Party size is `bookings.party_size`, which the medical
example also has (set to 1). Table reset is `buffer_after_minutes`, the same
column that held the clinic's cleaning time.

---

### 14.3 Worked example C — Hotel room, 3 nights

*"Deluxe king, check-in 2026-06-12, check-out 2026-06-15, 2 adults + 1 child,
America/Denver."*

The interesting question here: **how does a minute-granular `tstzrange` model
"3 nights" without a nights-specific column?** The answer is that a night *is* a
time range — the hotel's check-in/check-out times, applied in the property's
timezone, produce exactly the interval the constraint needs. The only thing that
differs is the *unit in which the duration is expressed and priced*, which is
`resource_types.time_unit` and `services.unit_price`.

**Setup.**

```sql
INSERT INTO tenants (id, slug, legal_name, display_name, status,
                     default_timezone, default_currency)
VALUES ('01890000-0000-7000-8000-000000000003', 'alpine-lodge',
        'Alpine Lodge Holdings Inc', 'Alpine Lodge', 'active',
        'America/Denver', 'USD');

INSERT INTO locations (id, tenant_id, code, name, timezone, country_code, settings)
VALUES ('01890000-0000-7000-8000-0000000000a3',
        '01890000-0000-7000-8000-000000000003',
        'main', 'Alpine Lodge Vail', 'America/Denver', 'US',
        -- Check-in/out times live in settings: they are policy, not schema.
        '{"check_in_time":"16:00","check_out_time":"11:00"}'::jsonb);

-- time_unit = 'night'. THIS is the dial that makes it a hotel.
INSERT INTO resource_types
    (id, tenant_id, code, name, kind, is_capacity_shared, time_unit,
     slot_granularity_minutes, attribute_schema, filterable_attributes)
VALUES
('01890000-0000-7000-8000-0000000000b4',
 '01890000-0000-7000-8000-000000000003',
 'guest_room', 'Guest Room', 'space', FALSE, 'night', NULL,
 '{"type":"object","properties":{
     "room_class":{"type":"string"},
     "bed_type":{"type":"string"},
     "max_occupancy":{"type":"integer"},
     "floor":{"type":"integer"},
     "view":{"type":"string"},
     "smoking":{"type":"boolean"},
     "accessible":{"type":"boolean"}}}'::jsonb,
 ARRAY['room_class','bed_type','max_occupancy','view','accessible']);

INSERT INTO resources
    (id, tenant_id, location_id, resource_type_id, code, name, capacity, attributes)
VALUES
('01890000-0000-7000-8000-0000000000c6',
 '01890000-0000-7000-8000-000000000003','01890000-0000-7000-8000-0000000000a3',
 '01890000-0000-7000-8000-0000000000b4','401','Room 401', 1,
 '{"room_class":"deluxe","bed_type":"king","max_occupancy":3,"floor":4,
   "view":"mountain","smoking":false,"accessible":false}'::jsonb),

('01890000-0000-7000-8000-0000000000c7',
 '01890000-0000-7000-8000-000000000003','01890000-0000-7000-8000-0000000000a3',
 '01890000-0000-7000-8000-0000000000b4','402','Room 402', 1,
 '{"room_class":"deluxe","bed_type":"two_queen","max_occupancy":4,"floor":4,
   "view":"mountain","smoking":false,"accessible":false}'::jsonb),

('01890000-0000-7000-8000-0000000000c8',
 '01890000-0000-7000-8000-000000000003','01890000-0000-7000-8000-0000000000a3',
 '01890000-0000-7000-8000-0000000000b4','101','Room 101', 1,
 '{"room_class":"standard","bed_type":"queen","max_occupancy":2,"floor":1,
   "view":"courtyard","smoking":false,"accessible":true}'::jsonb);

-- ════════════════════════════════════════════════════════════════════════════
-- The service. VARIABLE duration: a stay is 1..14 nights, decided at booking.
-- duration_minutes is NULL; duration_units carries the night count.
-- Housekeeping turnover is the buffer, exactly as cleaning was for the clinic.
INSERT INTO services
    (id, tenant_id, location_id, code, name,
     duration_minutes, duration_units, is_duration_variable,
     min_duration_units, max_duration_units,
     buffer_before_minutes, buffer_after_minutes,
     min_party_size, max_party_size,
     lead_time_minutes, booking_horizon_days, cancellation_window_minutes,
     unit_price, currency, price_model, deposit_amount, intake_schema)
VALUES
('01890000-0000-7000-8000-0000000000d3',
 '01890000-0000-7000-8000-000000000003','01890000-0000-7000-8000-0000000000a3',
 'deluxe-king-stay', 'Deluxe King Room',
 NULL, NULL, TRUE,
 1, 14,
 -- 3 hours of housekeeping between the 11:00 checkout and the 16:00 check-in
 -- would be redundant (the gap is already 5 hours), so the buffer is 0 here.
 -- A same-day-turnaround property would set buffer_after_minutes = 120.
 0, 0,
 1, 4,
 0, 365, 2880,
 389.0000, 'USD', 'per_unit', 200.0000,
 '[{"key":"arrival_time","type":"string","required":false,"pii_class":"none",
    "prompt":"What time do you expect to arrive?"},
   {"key":"loyalty_number","type":"string","required":false,"pii_class":"low",
    "prompt":"Do you have a loyalty number?"}]'::jsonb);

-- Requirement: one room, king bed, occupancy at least the party size.
INSERT INTO service_resource_requirements
    (tenant_id, service_id, resource_type_id, role, quantity,
     units_per_resource, attribute_predicate, allocation_strategy, respects_buffers)
VALUES
('01890000-0000-7000-8000-000000000003',
 '01890000-0000-7000-8000-0000000000d3',
 '01890000-0000-7000-8000-0000000000b4',
 'primary', 1, 1,
 '{"match":{"room_class":"deluxe"},
   "in":{"bed_type":["king"]},
   "gte":{"max_occupancy":"$party_size"}}'::jsonb,
 'least_utilised',
 TRUE);

-- A hotel is open every day. One rule, all seven weekdays, full 24 hours —
-- the same table that expressed clinic hours and dinner service.
INSERT INTO availability_rules
    (tenant_id, location_id, resource_type_id, effect, weekdays,
     start_time, end_time, effective_period)
VALUES
('01890000-0000-7000-8000-000000000003',
 '01890000-0000-7000-8000-0000000000a3',
 '01890000-0000-7000-8000-0000000000b4',
 'open', ARRAY[1,2,3,4,5,6,7]::smallint[],
 '00:00', '23:59:59', daterange('2026-01-01', NULL, '[)'));
```

**The booking.** Check-in 16:00 MDT on 2026-06-12 = 22:00 UTC.
Check-out 11:00 MDT on 2026-06-15 = 17:00 UTC.

```sql
INSERT INTO bookings
    (id, tenant_id, location_id, service_id, reference, status,
     service_period, buffer_before_minutes, buffer_after_minutes,
     local_date, party_size, duration_units,
     customer_name, customer_phone_e164, customer_email, intake_data,
     channel, booked_by,
     price_total, deposit_paid, currency, payment_status,
     confirmed_at, idempotency_key)
VALUES
('01890000-0000-7000-8000-0000000000e3',
 '01890000-0000-7000-8000-000000000003',
 '01890000-0000-7000-8000-0000000000a3',
 '01890000-0000-7000-8000-0000000000d3',
 'BK-9QW4RZ', 'confirmed',
 tstzrange('2026-06-12 22:00:00+00','2026-06-15 17:00:00+00','[)'),
 0, 0,
 DATE '2026-06-12',      -- arrival date, the natural "belongs to" day
 3,                      -- 2 adults + 1 child
 3,                      -- THREE NIGHTS. This is the only place the night
                         -- count appears, and it is a generic integer.
 'Priya Raghavan', '+13035550122', 'priya@example.com',
 '{"arrival_time":"18:30","loyalty_number":"AL-77213"}'::jsonb,
 'voice_inbound', 'agent',
 1167.0000,              -- 389.00 x 3 nights, from unit_price x duration_units
 200.0000, 'USD', 'authorized',
 now(), 'conv-5d1e:book:1');

INSERT INTO booking_items
    (tenant_id, booking_id, resource_id, role, requirement_id, quantity, occupies)
VALUES
('01890000-0000-7000-8000-000000000003',
 '01890000-0000-7000-8000-0000000000e3',
 '01890000-0000-7000-8000-0000000000c6',   -- Room 401
 'primary',
 (SELECT id FROM service_resource_requirements
   WHERE service_id = '01890000-0000-7000-8000-0000000000d3'),
 1, tstzrange('2026-06-12 22:00:00+00','2026-06-15 17:00:00+00','[)'));
```

**What the constraint guarantees here.** Room 401 is occupied for a 67-hour
range. Any other stay overlapping it fails. A guest checking in on 2026-06-15 at
16:00 MDT (22:00 UTC) succeeds: `[2026-06-15 22:00, …)` does not overlap
`[…, 2026-06-15 17:00)`. The five-hour housekeeping window between 11:00 and
16:00 falls naturally out of the check-in/check-out times, with no special code.

**Pricing.** `price_model = 'per_unit'`, `unit_price = 389.00`,
`duration_units = 3` → `price_total = 1167.00`. The same three columns priced
the clinic (`price_model = 'flat'`, `price = 250.00`, `duration_units = 1`) and
the restaurant (`price_model = 'free'`).

**Vertical-specific columns used: zero.** Bed type, view, and occupancy live in
`resources.attributes`. Nights are `duration_units` — the same column that is 1
for an appointment and 1 for a dinner. Check-in/check-out times are location
settings, applied by the engine when constructing the range.

---

### 14.4 Side-by-side: the same seven tables

| Table | Medical | Restaurant | Hotel |
|---|---|---|---|
| `resource_types` | `physician` (human, 15-min grid), `exam_room` (space) | `dining_table` (space, free-floating) | `guest_room` (space, `time_unit='night'`) |
| `resources` | Dr Chen `{specialty}`, Room 3 `{equipment}` | T04/T12/T20 `{seats, min_party}` | 401/402/101 `{bed_type, max_occupancy}` |
| `services` | 30 min, buffer_after 15, party 1..1, flat $250 | 90 min, buffer_after 15, party 1..12, free | variable 1..14 units, party 1..4, per_unit $389 |
| `service_resource_requirements` | 2 rows (physician + room), different `respects_buffers` | 1 row, `gte:{seats:$party_size}`, `best_fit` | 1 row, `in:{bed_type:[king]}`, `least_utilised` |
| `availability_rules` | Mon–Fri 09:00–17:00, per-resource | Tue–Sun 17:00–22:00, per-type + per-service | all 7 days, 00:00–23:59, per-type |
| `bookings` | 30-min range, `duration_units=1` | 90-min range, `party_size=4` | 67-hour range, `duration_units=3` |
| `booking_items` | 2 rows, different `occupies` | 1 row | 1 row |

### 14.5 What would break the model, and why it does not

| Objection | Answer |
|---|---|
| "A hotel books by date, not by instant" | A date, in the property's timezone, with the property's check-in/check-out times, *is* an instant range. Storing dates would lose the 11:00→16:00 turnover window and break the exclusion constraint's precision. |
| "A restaurant needs seat-level capacity" | It does not: a table hosts one party. Communal tables that genuinely share (a chef's counter) set `is_capacity_shared = true` with `capacity = 12`, and the shared-capacity trigger applies. The model already covers both. |
| "A clinic needs the doctor AND the room" | Two `service_resource_requirements` rows, two `booking_items` rows, two independent exclusion checks. Demonstrated in §14.1. |
| "Group classes / equipment pools break exclusivity" | `is_capacity_shared = true`, `capacity = N`, `units_per_resource = NULL` (derive from party size). The EXCLUDE constraint exempts them and the capacity trigger enforces the sum. |
| "Recurring physiotherapy" | `recurrence_rules` + one materialised `bookings` row per occurrence, each independently protected. |
| "Overbooking" | `overbook_allowance > 0` on a shared-capacity resource. §13.4. |
| "A new vertical needs a field we don't have" | It goes in `resources.attributes` (validated against `resource_types.attribute_schema`) or `bookings.intake_data` (validated against `services.intake_schema`), both declared by the vertical manifest. No migration. |
| "Attribute predicates in JSONB will be slow" | The candidate set is already narrowed by `ix_resources__tenant_type_location` to the resources of one type at one location — tens to low hundreds of rows. `ix_resources__attributes` (GIN) handles containment; numeric comparisons run against that small set. The expensive part of availability is the range overlap, which is GiST-indexed. |

**The claim holds.** Three verticals, one schema, zero vertical-specific columns.

---

## 15. SQLAlchemy 2.x Async Patterns

All code lives in `packages/persistence/` per contract §9. It imports from
`packages/core/` (ports, domain types, errors) and from nothing else.

### 15.1 The tenant context

```python
# packages/persistence/context.py
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The identity under which a unit of work executes.

    Immutable and frozen: a request's tenant cannot be reassigned mid-flight.
    Constructed once, at the authentication boundary, from the auth token —
    never from a path or query parameter (contract §10).
    """

    tenant_id: UUID
    user_id: UUID | None = None
    shard_key: str | None = None
    # Set only by the impersonation flow; written into every audit record.
    impersonator_user_id: UUID | None = None
    # Present for API-key auth; narrows what the session may do.
    api_key_id: UUID | None = None
    location_id: UUID | None = None


_tenant_ctx: contextvars.ContextVar[TenantContext | None] = contextvars.ContextVar(
    "receptionai_tenant_ctx", default=None
)


def set_tenant_context(ctx: TenantContext) -> contextvars.Token:
    return _tenant_ctx.set(ctx)


def get_tenant_context() -> TenantContext:
    ctx = _tenant_ctx.get()
    if ctx is None:
        raise MissingTenantContextError(
            "No tenant context bound. Every DB access must run inside "
            "`async with unit_of_work(ctx)`."
        )
    return ctx


def try_get_tenant_context() -> TenantContext | None:
    return _tenant_ctx.get()


class MissingTenantContextError(RuntimeError):
    """Raised when a DB session is opened without a bound tenant."""
```

`contextvars` rather than a thread-local: asyncio tasks inherit context vars at
creation, and each request handler runs in its own task, so a value set at the
top of a request is visible to everything it awaits and to nothing else.

### 15.2 The ConnectionRouter

Contract §2's escape hatch: *"Every subsystem must resolve its DB session
through the router, never a global engine."*

```python
# packages/persistence/router.py
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .context import TenantContext

logger = logging.getLogger(__name__)

_DEFAULT_SHARD: Final = "__default__"


@dataclass(frozen=True, slots=True)
class ShardConfig:
    key: str
    dsn: str
    pool_size: int = 20
    max_overflow: int = 10
    pool_timeout: float = 10.0
    pool_recycle: int = 1800          # recycle before typical LB idle timeouts
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000
    idle_in_txn_timeout_ms: int = 60_000
    application_name: str = "receptionai-api"
    echo: bool = False


class UnresolvableShardError(RuntimeError):
    """A tenant is assigned to a shard this process has no configuration for.

    Per contract amendment A8 this is FATAL to the request, never a fallback.
    See ``ConnectionRouter._engine`` for the reasoning.
    """

    def __init__(self, shard_key: str, known: list[str]) -> None:
        self.shard_key = shard_key
        self.known = known
        super().__init__(
            f"No configuration for shard {shard_key!r}; refusing to open a "
            f"session. This tenant is assigned to dedicated infrastructure and "
            f"MUST NOT be served from the shared cluster. "
            f"Known shards: {known}"
        )


class ConnectionRouter:
    """Resolves a TenantContext to the engine that owns its data.

    Today every tenant resolves to the shared cluster. A tenant with a
    non-null ``tenants.shard_key`` resolves to a dedicated engine. Relocating
    an enterprise or HIPAA tenant is therefore a config + data-migration
    exercise — never a schema change and never a code change in any caller.

    This class is the ONLY place in the codebase that constructs an engine.
    A CI import-linter rule forbids `create_async_engine` anywhere else.

    **Fail-closed on shard resolution (contract A8).** See ``_engine``.
    """

    def __init__(self, shards: dict[str, ShardConfig]) -> None:
        if _DEFAULT_SHARD not in shards:
            raise ValueError("ConnectionRouter requires a '__default__' shard")
        self._configs = shards
        self._engines: dict[str, AsyncEngine] = {}
        self._lock = asyncio.Lock()

    async def engine_for(self, ctx: TenantContext) -> AsyncEngine:
        # shard_key IS NULL means "never assigned to dedicated infrastructure",
        # so the default shard is genuinely correct — not a fallback.
        return await self._engine(ctx.shard_key or _DEFAULT_SHARD)

    async def default_engine(self) -> AsyncEngine:
        """For migrations, health checks, and cross-tenant ops jobs."""
        return await self._engine(_DEFAULT_SHARD)

    async def _engine(self, shard_key: str) -> AsyncEngine:
        engine = self._engines.get(shard_key)
        if engine is not None:
            return engine

        async with self._lock:
            engine = self._engines.get(shard_key)   # double-checked
            if engine is not None:
                return engine

            cfg = self._configs.get(shard_key)
            if cfg is None:
                # ── FAIL CLOSED (contract A8) ──────────────────────────────
                # Do NOT fall back to the default shard. A non-null shard_key
                # exists only for tenants moved to dedicated infrastructure
                # BECAUSE shared was contractually or legally unacceptable.
                # Falling back would place precisely those tenants on the
                # shared cluster, and would do it invisibly: RLS still scopes
                # correctly, every query succeeds, no test fails, no alert
                # fires. It fails *successfully*, and discovery comes at the
                # next audit — the exact event the arrangement existed to
                # survive.
                #
                # The trigger is routine (config not yet propagated to a newly
                # scheduled pod), and at production log volume a warning is not
                # a control. So: refuse the session.
                #
                # This is the fail-open/fail-closed split applied consistently.
                # Availability controls fail OPEN — the rate limiter in `05`
                # deliberately admits traffic when Redis is down, because an
                # outage there must not take the product down. Isolation
                # controls fail CLOSED. Shard routing is an isolation control,
                # so it fails closed, exactly as RLS does when
                # `app.tenant_id` is unset (§1.6).
                raise UnresolvableShardError(
                    shard_key, sorted(self._configs.keys())
                )

            engine = create_async_engine(
                cfg.dsn,
                pool_size=cfg.pool_size,
                max_overflow=cfg.max_overflow,
                pool_timeout=cfg.pool_timeout,
                pool_recycle=cfg.pool_recycle,
                pool_pre_ping=True,
                echo=cfg.echo,
                # ── Why these three server settings are non-negotiable ──────
                # statement_timeout : a runaway query cannot hold a connection
                #   forever and starve the pool.
                # lock_timeout      : a booking transaction waiting on an
                #   advisory lock fails fast and retries rather than piling up.
                # idle_in_transaction_session_timeout : a leaked `SET LOCAL`
                #   transaction cannot pin an old snapshot and block VACUUM,
                #   which is the classic way an RLS app bloats a table.
                connect_args={
                    "server_settings": {
                        "application_name": cfg.application_name,
                        "statement_timeout": str(cfg.statement_timeout_ms),
                        "lock_timeout": str(cfg.lock_timeout_ms),
                        "idle_in_transaction_session_timeout":
                            str(cfg.idle_in_txn_timeout_ms),
                        "timezone": "UTC",
                        # asyncpg prepares statements server-side; with
                        # PgBouncer in transaction mode that breaks. Disabled
                        # here and re-enabled per-deployment if the pooler is
                        # session-mode or absent.
                        "jit": "off",
                    },
                    # Required when PgBouncer sits in front in transaction mode.
                    "statement_cache_size": 0,
                },
            )
            self._engines[shard_key] = engine
            return engine

    def known_shard_keys(self) -> set[str]:
        """Shards this process is configured to serve.

        Used by the readiness gate below so a pod with stale configuration
        fails at deploy time rather than at request time.
        """
        return set(self._configs.keys())

    async def dispose_all(self) -> None:
        for engine in self._engines.values():
            await engine.dispose()
        self._engines.clear()
```

**On the relocation path — no carve-out needed.** The coordinator asked whether
there is a bootstrap or migration case where a non-null `shard_key` legitimately
has no configuration yet. There is not, provided relocation is ordered
correctly, and the ordering is the same additive-first discipline §16.4 already
requires of schema changes:

1. Provision the dedicated database; add its `ShardConfig` to the deployment
   configuration with the tenant still on shared.
2. Roll the config out and **verify every serving pod has it**, via a readiness
   probe that asserts the expected shard keys are present.
3. Migrate the tenant's data; only then set `tenants.shard_key`.

The commitment is made in step 3, after the capability exists — so a non-null
`shard_key` with no config is never a legitimate intermediate state. It means
either a mis-ordered relocation or a pod running stale configuration, and both
are exactly what A8 wants surfaced. Reversing the order (set `shard_key`, then
propagate config) would create a window in which the fallback silently served a
committed tenant from shared, which is the failure A8 exists to prevent.

Two supports make the loud failure operationally tolerable rather than merely
correct:

```python
# Readiness gate. A pod that cannot serve every configured shard does not
# accept traffic — so the failure surfaces at deploy time, against a rolling
# update that can halt, instead of at request time against a real tenant.
async def check_shard_configuration(
    router: ConnectionRouter, expected: set[str]
) -> None:
    missing = expected - set(router.known_shard_keys())
    if missing:
        raise RuntimeError(
            f"Refusing readiness: no configuration for shards {sorted(missing)}. "
            f"Tenants assigned to them would be refused service (contract A8)."
        )
```

`expected` is read at startup from `SELECT DISTINCT shard_key FROM tenants
WHERE shard_key IS NOT NULL` — the database is the authority on which shards
must exist, so a relocation that skipped step 2 fails the *next* deploy rather
than the next request.

And `UnresolvableShardError` maps to **503, not 500**: the tenant's data is
intact and the condition is transient-by-remediation, so retries and status
pages behave correctly. It is alerted at page severity with the tenant id and
shard key, because for these tenants it is a compliance incident in progress,
not a routine error.

### 15.3 The session factory — where `SET LOCAL app.tenant_id` happens

This is the single most important piece of code in the persistence layer.

```python
# packages/persistence/session.py
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .context import TenantContext, get_tenant_context, set_tenant_context
from .router import ConnectionRouter

logger = logging.getLogger(__name__)


class TenantSession(AsyncSession):
    """An AsyncSession that knows which tenant it is bound to.

    The binding is set once, at open, and asserted on every flush. It exists so
    that repositories can defensively re-check tenant_id in Python — contract
    §8's "re-checked in the service layer" — without threading the context
    through every call signature.

    **This is the canonical session type platform-wide (contract A10).**
    `05-PLATFORM-SECURITY-OPS.md` has withdrawn its `get_session()` /
    `AsyncSession` pair in favour of `tenant_context()` / `db_session()` /
    `TenantSession`, so the properties below are a published interface, not
    internal convenience. Removing or renaming one is a breaking change for
    `05`'s audit writer.
    """

    tenant_ctx: TenantContext

    def __init__(self, *args, tenant_ctx: TenantContext, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tenant_ctx = tenant_ctx

    # ── Published accessors (contract A10) ────────────────────────────────
    # Flattened off tenant_ctx so callers in `05` need not import
    # TenantContext to write an audit row.

    @property
    def tenant_id(self) -> UUID:
        return self.tenant_ctx.tenant_id

    @property
    def user_id(self) -> UUID | None:
        """The SUBJECT — whose authority the action is taken under."""
        return self.tenant_ctx.user_id

    @property
    def impersonator_user_id(self) -> UUID | None:
        """The ACTOR — the platform staff member driving an impersonated
        session, or None for ordinary access.

        `05` stores the subject and needs the actor: an audit row must record
        both "acted as Alice" and "was really Bob from support", or
        impersonation is indistinguishable from the real user in the trail.
        §11.4 makes impersonation use the ordinary tenant-scoped path
        precisely so the same RLS policy protects it — which means the actor
        is NOT recoverable from the database session and must be carried here.
        """
        return self.tenant_ctx.impersonator_user_id

    @property
    def shard_key(self) -> str | None:
        """Which shard this session is bound to. None = the shared cluster.

        Exposed for `05`'s audit and telemetry: a record written on dedicated
        infrastructure should say so, and A8 makes an unexpected value here a
        signal worth alerting on rather than a benign detail.
        """
        return self.tenant_ctx.shard_key

    @property
    def is_impersonated(self) -> bool:
        return self.tenant_ctx.impersonator_user_id is not None


@event.listens_for(TenantSession, "before_flush")
def _assert_tenant_on_flush(session: TenantSession, flush_context, instances) -> None:
    """Belt-and-braces: RLS WITH CHECK would reject a wrong tenant_id anyway,
    but it would do so as an opaque database error deep in a flush. Catching it
    here gives a precise, testable failure with the offending object named.
    """
    expected = session.tenant_ctx.tenant_id
    for obj in list(session.new) + list(session.dirty):
        tid = getattr(obj, "tenant_id", None)
        if tid is None:
            continue                     # non-tenant-owned entity
        if tid != expected:
            raise CrossTenantWriteError(
                f"{type(obj).__name__} carries tenant_id={tid} but the session "
                f"is bound to {expected}"
            )


class CrossTenantWriteError(RuntimeError):
    """A write was attempted with a tenant_id other than the session's."""


class SessionFactory:
    """Creates tenant-scoped sessions through the ConnectionRouter."""

    def __init__(self, router: ConnectionRouter) -> None:
        self._router = router
        self._makers: dict[int, async_sessionmaker[TenantSession]] = {}

    async def _maker(self, ctx: TenantContext) -> async_sessionmaker[TenantSession]:
        engine = await self._router.engine_for(ctx)
        key = id(engine)
        maker = self._makers.get(key)
        if maker is None:
            maker = async_sessionmaker(
                bind=engine,
                class_=TenantSession,
                expire_on_commit=False,   # objects stay usable after commit
                autoflush=False,          # explicit flushes; no surprise writes
            )
            self._makers[key] = maker
        return maker

    @asynccontextmanager
    async def unit_of_work(
        self,
        ctx: TenantContext | None = None,
        *,
        readonly: bool = False,
    ) -> AsyncIterator[TenantSession]:
        """Open a transaction with RLS context applied.

        Everything about this is deliberate:

        1. ``SET LOCAL`` — not ``SET``. LOCAL is transaction-scoped, so the GUC
           is reset automatically at COMMIT or ROLLBACK. A plain ``SET`` would
           persist on the pooled connection and leak tenant A's context into
           tenant B's next request. This is THE multi-tenancy bug, and
           ``SET LOCAL`` is the fix.

        2. Bound parameters — never f-strings. Contract §2.4: "never
           interpolated into SQL by hand". ``SET LOCAL`` does not accept a
           bind parameter directly, so we go through ``set_config(name, value,
           is_local => true)``, which does. The tenant id therefore travels as
           a parameter, immune to injection.

        3. The context is set BEFORE any user query runs, inside the same
           transaction, so there is no window in which a query executes
           without RLS scoping.
        """
        ctx = ctx or get_tenant_context()
        maker = await self._maker(ctx)

        token = set_tenant_context(ctx)
        session = maker(tenant_ctx=ctx)
        try:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(ctx.tenant_id)},
                )
                if ctx.user_id is not None:
                    await session.execute(
                        text("SELECT set_config('app.user_id', :uid, true)"),
                        {"uid": str(ctx.user_id)},
                    )
                if readonly:
                    # Cheap insurance for report queries: any accidental write
                    # fails at the database rather than being caught in review.
                    await session.execute(text("SET LOCAL transaction_mode "
                                               "TO DEFAULT"))
                    await session.execute(text("SET TRANSACTION READ ONLY"))

                yield session
                # `session.begin()` commits on clean exit, rolls back on raise.
        finally:
            await session.close()
            reset_tenant_context(token)

    @asynccontextmanager
    async def ops_unit_of_work(self) -> AsyncIterator[TenantSession]:
        """Cross-tenant maintenance work.

        Deliberately does NOT set app.tenant_id, so every RLS-protected table
        returns zero rows. The only useful thing this session can do is call
        the explicitly-granted SECURITY DEFINER ops_* functions (§11.4). If
        someone writes a bare SELECT here expecting rows, they get none — which
        is exactly the feedback we want.
        """
        engine = await self._router.default_engine()
        maker = async_sessionmaker(
            bind=engine, class_=TenantSession, expire_on_commit=False
        )
        sentinel = TenantContext(tenant_id=UUID(int=0))
        session = maker(tenant_ctx=sentinel)
        try:
            async with session.begin():
                yield session
        finally:
            await session.close()


def reset_tenant_context(token) -> None:
    """Restore the previous context var value.

    Always called from a ``finally`` so a raised exception cannot leave a
    tenant bound to the calling task — which, in a worker process serving
    every tenant, would be a cross-tenant leak.
    """
    from .context import _tenant_ctx

    _tenant_ctx.reset(token)
```

**Why `set_config(..., is_local => true)` rather than literal `SET LOCAL`.**
`SET LOCAL app.tenant_id = $1` is a syntax error — `SET` takes a literal, not a
placeholder. The alternatives are string interpolation (rejected by contract
§2.4, and a genuine injection vector if a tenant id ever comes from an
attacker-influenced source) or `set_config()`, which is an ordinary function
call and therefore takes bind parameters. We use `set_config`.

**The `SET LOCAL` vs `SET` distinction, restated because it is the whole ball
game.** Connections are pooled. If tenant A's request issues a session-level
`SET app.tenant_id = 'A'` and the connection returns to the pool without a
reset, tenant B's next request inherits `app.tenant_id = 'A'` and — because RLS
policies are doing exactly what they were told — B reads A's data with no error
anywhere. `SET LOCAL` is scoped to the transaction and is discarded at
`COMMIT`/`ROLLBACK`, closing that hole structurally.

### 15.4 The base repository

```python
# packages/persistence/repository.py
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase

from packages.core.errors import (
    ConcurrencyConflictError,
    EntityNotFoundError,
    ResourceConflictError,
)
from .session import TenantSession

ModelT = TypeVar("ModelT", bound=DeclarativeBase)


def uuid7_timestamp(value: UUID) -> datetime:
    """Recover the creation instant embedded in a UUIDv7.

    Used to derive the partition key for point lookups against partitioned
    tables (conversations, turns, audit_log, usage_events, ai_checkpoints)
    without the caller
    having to carry a timestamp alongside every id.
    """
    ms = int(value.hex[:12], 16)
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


class TenantRepository(Generic[ModelT]):
    """Base repository for tenant-owned entities.

    Every query issued through this class carries an explicit
    ``tenant_id == session.tenant_ctx.tenant_id`` predicate, even though RLS
    already enforces it. That redundancy is intentional and buys three things:

      1. Defence in depth — contract §8 requires the service layer to re-check
         what the database enforces.
      2. Index usability — the planner sees a literal predicate and picks the
         tenant-leading index, rather than relying on the RLS predicate being
         pushed down (which it usually is, but "usually" is not a plan).
      3. Local testability — repository tests can run without RLS enabled and
         still verify scoping.
    """

    model: type[ModelT]
    # Set True on partitioned models so point lookups add a created_at window.
    partitioned: bool = False

    def __init__(self, session: TenantSession) -> None:
        self.session = session

    @property
    def tenant_id(self) -> UUID:
        return self.session.tenant_ctx.tenant_id

    # ── Query construction ────────────────────────────────────────────────

    def _base_select(self) -> Select[tuple[ModelT]]:
        stmt = select(self.model).where(self.model.tenant_id == self.tenant_id)
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))
        return stmt

    def _partition_window(self, entity_id: UUID) -> tuple[datetime, datetime]:
        """A narrow created_at range around a UUIDv7's embedded timestamp.

        Partition pruning needs a predicate on created_at. Rather than forcing
        every caller to pass one, we derive it from the id. The +/- 1 second
        margin absorbs clock skew between the app instance that minted the id
        and the database that stamped ``created_at``.
        """
        ts = uuid7_timestamp(entity_id)
        return ts - timedelta(seconds=1), ts + timedelta(seconds=1)

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get(self, entity_id: UUID) -> ModelT | None:
        stmt = self._base_select().where(self.model.id == entity_id)
        if self.partitioned:
            lo, hi = self._partition_window(entity_id)
            stmt = stmt.where(
                self.model.created_at >= lo, self.model.created_at < hi
            )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_raise(self, entity_id: UUID) -> ModelT:
        entity = await self.get(entity_id)
        if entity is None:
            raise EntityNotFoundError(
                entity_type=self.model.__tablename__, entity_id=entity_id
            )
        return entity

    async def list(
        self,
        *filters: Any,
        limit: int = 50,
        offset: int = 0,
        order_by: Any = None,
    ) -> Sequence[ModelT]:
        stmt = self._base_select().where(*filters)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        elif hasattr(self.model, "created_at"):
            stmt = stmt.order_by(self.model.created_at.desc())
        stmt = stmt.limit(min(limit, 500)).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self, *filters: Any) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.tenant_id == self.tenant_id, *filters)
        )
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one()

    async def exists(self, *filters: Any) -> bool:
        stmt = select(
            select(1)
            .select_from(self.model)
            .where(self.model.tenant_id == self.tenant_id, *filters)
            .exists()
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    # ── Writes ────────────────────────────────────────────────────────────

    async def add(self, entity: ModelT) -> ModelT:
        # Stamp rather than trust: the caller cannot set another tenant's id
        # even by accident, and the before_flush guard is a second net.
        entity.tenant_id = self.tenant_id
        self.session.add(entity)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise self._translate(exc) from exc
        return entity

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        for e in entities:
            e.tenant_id = self.tenant_id
        self.session.add_all(entities)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise self._translate(exc) from exc
        return entities

    async def update_versioned(
        self, entity_id: UUID, expected_version: int, **values: Any
    ) -> int:
        """Optimistic concurrency.

        Returns the new version. Raises ConcurrencyConflictError if another
        writer moved first — surfaced to the caller as a 409, never silently
        overwritten. This is why `version` is bumped here rather than in a
        trigger: a trigger cannot report that the expectation was violated.
        """
        stmt = (
            update(self.model)
            .where(
                self.model.id == entity_id,
                self.model.tenant_id == self.tenant_id,
                self.model.version == expected_version,
            )
            .values(**values, version=self.model.version + 1)
            .returning(self.model.version)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise ConcurrencyConflictError(
                entity_type=self.model.__tablename__,
                entity_id=entity_id,
                expected_version=expected_version,
            )
        return row

    async def soft_delete(self, entity_id: UUID) -> None:
        if not hasattr(self.model, "deleted_at"):
            raise TypeError(f"{self.model.__name__} does not support soft delete")
        stmt = (
            update(self.model)
            .where(
                self.model.id == entity_id,
                self.model.tenant_id == self.tenant_id,
                self.model.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )
        if (await self.session.execute(stmt)).rowcount == 0:
            raise EntityNotFoundError(
                entity_type=self.model.__tablename__, entity_id=entity_id
            )

    async def hard_delete(self, entity_id: UUID) -> None:
        stmt = delete(self.model).where(
            self.model.id == entity_id, self.model.tenant_id == self.tenant_id
        )
        if (await self.session.execute(stmt)).rowcount == 0:
            raise EntityNotFoundError(
                entity_type=self.model.__tablename__, entity_id=entity_id
            )

    # ── Error translation ─────────────────────────────────────────────────

    @staticmethod
    def _translate(exc: IntegrityError) -> Exception:
        """Turn Postgres SQLSTATEs into domain errors.

        23P01 (exclusion_violation) is the one that matters: it is how a
        double-booking attempt surfaces, and the booking service must be able
        to catch it specifically in order to retry against another resource.
        """
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(
            orig, "pgcode", None
        )
        constraint = getattr(
            getattr(orig, "__cause__", None), "constraint_name", None
        )
        if sqlstate == "23P01":
            return ResourceConflictError(
                "The requested time is no longer available",
                constraint=constraint,
                kind="overlap",
            )
        if sqlstate == "23505":
            return ResourceConflictError(
                "A record with these values already exists",
                constraint=constraint,
                kind="duplicate",
            )
        if sqlstate == "23503":
            return ResourceConflictError(
                "Referenced record does not exist or belongs to another tenant",
                constraint=constraint,
                kind="foreign_key",
            )
        if sqlstate == "23514":
            return ResourceConflictError(
                "Value violates a business rule",
                constraint=constraint,
                kind="check",
            )
        return exc
```

### 15.5 A concrete repository: the booking write path

```python
# packages/persistence/repositories/bookings.py
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Range

from packages.core.errors import ResourceConflictError
from ..models import Booking, BookingHold, BookingItem
from ..repository import TenantRepository


class BookingRepository(TenantRepository[Booking]):
    model = Booking

    async def find_conflicts(
        self, resource_id: UUID, occupies: Range[datetime],
        *, ignore_hold_id: UUID | None = None,
        ignore_booking_id: UUID | None = None,
    ) -> list[dict]:
        """Delegate to booking_check_conflicts (§13.2).

        Calling the SQL function rather than reimplementing its query in Python
        matters: the function takes the per-resource advisory lock. If a caller
        wrote the equivalent SELECTs inline, the lock would be missing and the
        hold-vs-booking race would reopen.
        """
        rows = await self.session.execute(
            text(
                "SELECT conflict_kind, conflict_id, conflict_range "
                "FROM booking_check_conflicts(:tid, :rid, :occ, :ih, :ib)"
            ),
            {
                "tid": self.tenant_id,
                "rid": resource_id,
                "occ": occupies,
                "ih": ignore_hold_id,
                "ib": ignore_booking_id,
            },
        )
        return [dict(r._mapping) for r in rows]

    async def create_hold(
        self, *, location_id: UUID, service_id: UUID, resource_id: UUID,
        occupies: Range[datetime], ttl_seconds: int = 300,
        conversation_id: UUID | None = None, quantity: int = 1,
    ) -> BookingHold:
        conflicts = await self.find_conflicts(resource_id, occupies)
        if conflicts:
            raise ResourceConflictError(
                "That time is no longer available",
                kind="overlap",
                details=conflicts,
            )

        hold = BookingHold(
            tenant_id=self.tenant_id,
            location_id=location_id,
            service_id=service_id,
            resource_id=resource_id,
            occupies=occupies,
            quantity=quantity,
            conversation_id=conversation_id,
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds),
        )
        self.session.add(hold)
        try:
            await self.session.flush()
        except Exception as exc:            # noqa: BLE001 — re-raised below
            raise self._translate(exc) from exc
        return hold

    async def confirm_from_hold(
        self, hold_id: UUID, booking: Booking
    ) -> Booking:
        """Convert a hold into a confirmed booking, atomically.

        The whole thing runs in the caller's unit_of_work, so the hold release
        and the booking_items insert commit together or not at all. There is no
        instant at which the slot is unprotected.
        """
        booking.tenant_id = self.tenant_id
        self.session.add(booking)
        await self.session.flush()          # assign booking.id

        try:
            await self.session.execute(
                text("SELECT booking_convert_hold(:tid, :hid, :bid)"),
                {
                    "tid": self.tenant_id,
                    "hid": hold_id,
                    "bid": booking.id,
                },
            )
        except Exception as exc:            # noqa: BLE001
            raise self._translate(exc) from exc

        return booking
```

### 15.6 Wiring it to FastAPI

```python
# apps/api/dependencies.py
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request

from packages.persistence.context import TenantContext
from packages.persistence.session import SessionFactory, TenantSession


async def tenant_context(request: Request) -> TenantContext:
    """Derive the tenant from the authenticated principal.

    Contract §10: "tenant is derived from auth context, never taken from a
    path or query parameter." The auth middleware has already validated the
    bearer token or API key and attached the principal; this function reads
    it and nothing else. There is deliberately no `tenant_id` parameter
    anywhere in the signature — you cannot pass one in if the API does not
    accept one.
    """
    principal = request.state.principal          # set by AuthMiddleware
    return TenantContext(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        shard_key=principal.shard_key,
        api_key_id=principal.api_key_id,
        impersonator_user_id=principal.impersonator_user_id,
    )


async def db_session(
    request: Request,
    ctx: Annotated[TenantContext, Depends(tenant_context)],
) -> AsyncIterator[TenantSession]:
    factory: SessionFactory = request.app.state.session_factory
    async with factory.unit_of_work(ctx) as session:
        yield session


DbSession = Annotated[TenantSession, Depends(db_session)]
Ctx = Annotated[TenantContext, Depends(tenant_context)]
```

```python
# apps/api/routers/bookings.py — the shape every route follows
from fastapi import APIRouter, status

from apps.api.dependencies import DbSession
from packages.persistence.repositories.bookings import BookingRepository

router = APIRouter(prefix="/api/v1/bookings", tags=["bookings"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_booking(payload: CreateBookingRequest, session: DbSession):
    repo = BookingRepository(session)
    booking = await BookingService(session).create(payload)
    return BookingResponse.model_validate(booking)
    # The unit_of_work context manager commits on clean return and rolls back
    # on any exception, so a failed booking leaves no partial hold, no orphan
    # booking_items row, and no outbox event.
```

### 15.7 Temporal activities use the same factory

```python
# apps/worker/activities/booking.py
from temporalio import activity

from packages.persistence.context import TenantContext
from packages.persistence.session import SessionFactory


@activity.defn(name="confirm_booking")
async def confirm_booking(tenant_id: str, booking_id: str, hold_id: str) -> dict:
    """Contract §3: every workflow carries tenant_id in its input.

    An activity therefore reconstructs the TenantContext from its arguments —
    it never inherits an ambient one, because a Temporal worker serves every
    tenant from one process and an inherited context would be a cross-tenant
    leak waiting to happen.
    """
    factory: SessionFactory = activity_deps().session_factory
    ctx = TenantContext(tenant_id=UUID(tenant_id))

    async with factory.unit_of_work(ctx) as session:
        repo = BookingRepository(session)
        booking = await repo.get_or_raise(UUID(booking_id))
        await repo.confirm_from_hold(UUID(hold_id), booking)
        await OutboxWriter(session).emit(
            type="booking.appointment.confirmed",
            aggregate_type="booking",
            aggregate_id=booking.id,
            payload={"booking_id": str(booking.id), "reference": booking.reference},
        )
        return {"status": "confirmed", "reference": booking.reference}
```

The outbox write shares the transaction with the state change — contract §6's
transactional outbox, realised in three lines.

### 15.8 Model definitions — the declarative base

```python
# packages/persistence/models/base.py
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s__%(referred_table_name)s",
    "uq": "uq_%(table_name)s__%(column_0_N_name)s",
    "ck": "ck_%(table_name)s__%(constraint_name)s",
    "ix": "ix_%(table_name)s__%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    """Naming conventions here are what make Alembic autogenerate produce
    stable, reviewable diffs. Without them, dropping and recreating a
    constraint yields a random system-assigned name and every migration
    churns.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONB}


class TenantMixin:
    """Applied to every tenant-owned model. The mixin — rather than repetition
    — is what lets the RLS conformance test enumerate tenant tables by
    introspecting the model registry and assert coverage.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=func.now(),
    )


class VersionedMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

Note `server_default=text("uuid_generate_v7()")` **and** application-side
generation. The application normally supplies the id (so it is known before the
flush, for logging and for the outbox), but the server default guarantees that
a migration, a backfill, or a direct `INSERT` still produces a v7 key.

---

## 16. Migration Strategy

### 16.1 The privileged / runtime role split

Contract §2.5: *"The application's runtime DB role is not the table owner and
does not have BYPASSRLS. Migrations run as a separate privileged role."*

| Role | Owns objects | Runs migrations | RLS applies | Used by |
|---|---|---|---|---|
| `receptionai_owner` | yes | yes | forced, but irrelevant — it never queries business data | Alembic only |
| `receptionai_app` | no | no | yes | API, workers |
| `receptionai_auth` | no | no | yes (sees nothing directly) | login path only |
| `receptionai_reporting` | no | no | yes | analytics, BI |
| `receptionai_ops` | no | no | yes (works via `SECURITY DEFINER`) | sweepers |

**The critical detail: default privileges.** Without them, every migration that
creates a table must remember to `GRANT` on it — and the one that forgets
produces a table the application cannot read, discovered in production. Set them
once:

```sql
-- Run as receptionai_owner, once, before the first migration.
ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO receptionai_app;

ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO receptionai_reporting;

ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public
    GRANT SELECT, UPDATE ON TABLES TO receptionai_ops;

ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO receptionai_app;

-- Functions are NOT granted by default. Each SECURITY DEFINER function is
-- granted explicitly, which forces a conscious decision every time one is
-- added. This is the opposite of the tables policy, and deliberately so:
-- a function is a privilege escalation surface, a table is not.
ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

GRANT USAGE ON SCHEMA public
    TO receptionai_app, receptionai_auth, receptionai_reporting, receptionai_ops;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

### 16.2 Alembic configuration

```python
# migrations/env.py
from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from packages.persistence.models.base import Base
import packages.persistence.models  # noqa: F401 — registers every model

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate from fighting us.

    Partitions are created by an operational job, not by migrations, so they
    must not appear as "unexpected tables to drop". Likewise the pgvector and
    extension-owned objects.
    """
    partition_prefixes = ("conversations_p", "turns_p", "audit_log_p",
                          "usage_events_p", "ai_checkpoints_p")
    if type_ in ("table", "index") and name.startswith(partition_prefixes):
        return False
    if reflected and type_ == "table" and name in {"alembic_version"}:
        return False
    return True


def process_revision_directives(ctx, revision, directives) -> None:
    """Fail loudly on an empty autogenerated migration rather than committing
    a no-op file that later confuses `alembic history`."""
    if getattr(context.config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            print("No schema changes detected; no migration generated.")


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        process_revision_directives=process_revision_directives,
        compare_type=True,
        compare_server_default=True,
        # One transaction for the whole migration: a failure rolls back
        # everything. PostgreSQL has transactional DDL; we use it.
        transaction_per_migration=False,
        render_as_batch=False,
    )

    with context.begin_transaction():
        # Migrations must not be blocked forever by a long-running reader, and
        # must not themselves hold an ACCESS EXCLUSIVE lock indefinitely.
        connection.execute(text("SET lock_timeout = '5s'"))
        connection.execute(text("SET statement_timeout = '0'"))
        # Advisory lock: two deploy pods running Alembic concurrently would
        # otherwise both try to apply the same revision.
        connection.execute(text("SELECT pg_advisory_xact_lock(742193847)"))
        context.run_migrations()


async def run_async_migrations() -> None:
    cfg = context.config.get_section(context.config.config_ini_section, {})
    # NOTE: the PRIVILEGED DSN. Never the app role's.
    cfg["sqlalchemy.url"] = os.environ["MIGRATION_DATABASE_URL"]
    engine = async_engine_from_config(cfg, prefix="sqlalchemy.",
                                      poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported: RLS policy DDL "
                       "and role grants require a live connection.")
asyncio.run(run_async_migrations())
```

### 16.3 RLS in migrations — the template every table migration follows

Alembic autogenerate does **not** detect RLS policies. They must be written by
hand, and the discipline that makes this safe is a shared helper plus a CI check
(§11.7) that fails the build if any tenant table lacks coverage.

```python
# migrations/helpers.py
from alembic import op


TENANT_ROLES = "receptionai_app, receptionai_reporting, receptionai_ops"


def enable_tenant_rls(table: str) -> None:
    """Apply the §11.2 pattern. Called at the END of every migration that
    creates a tenant-owned table."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE  ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY rls_{table}__all ON {table}
            FOR ALL
            TO {TENANT_ROLES}
            USING      (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())
    """)


def disable_tenant_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS rls_{table}__all ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
```

```python
# migrations/versions/0004_add_booking_notes.py
"""add booking_notes table"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from migrations.helpers import enable_tenant_rls, disable_tenant_rls

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.create_table(
        "booking_notes",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v7()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("author_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "booking_id"],
                                ["bookings.tenant_id", "bookings.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_booking_notes__tenant_booking", "booking_notes",
                    ["tenant_id", "booking_id", "created_at"])
    op.execute("""
        CREATE TRIGGER trg_booking_notes_updated_at
            BEFORE UPDATE ON booking_notes
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    enable_tenant_rls("booking_notes")     # ← never omitted


def downgrade() -> None:
    disable_tenant_rls("booking_notes")
    op.drop_table("booking_notes")
```

### 16.4 Zero-downtime column changes

The governing rule: **at every instant during a deploy, both the old and the new
application version must work against the current schema.** That means schema
changes are always additive first, and destructive only after the old code is
gone. Every one of these is a multi-release sequence, not a single migration.

#### Adding a column

```sql
-- Release N: additive, safe. PostgreSQL 11+ stores a non-volatile default in
-- the catalogue rather than rewriting the table, so even a NOT NULL DEFAULT
-- is instant. A VOLATILE default (e.g. gen_random_uuid()) still rewrites —
-- so never do that on a large table.
ALTER TABLE bookings ADD COLUMN source_campaign TEXT;
```

Old code ignores it; new code writes it. No coordination needed.

#### Adding a NOT NULL column to a large table

Never in one step. `ALTER TABLE ... ADD COLUMN x NOT NULL` without a default
fails outright; with a volatile default it rewrites the table under
`ACCESS EXCLUSIVE`, which on a 100M-row table is an outage.

```sql
-- Release N: add nullable.
ALTER TABLE bookings ADD COLUMN channel_ref TEXT;

-- Release N (application): write the column on every new/updated row.

-- Release N+1: backfill in batches, outside a long transaction. Each batch is
-- its own transaction so VACUUM can keep up and no single lock is long-lived.
--   UPDATE bookings SET channel_ref = ... WHERE channel_ref IS NULL
--    AND id IN (SELECT id FROM bookings WHERE channel_ref IS NULL LIMIT 5000);
-- Repeated until zero rows affected.

-- Release N+2: add the constraint NOT VALID (instant, no full scan, no
-- ACCESS EXCLUSIVE beyond a moment), then VALIDATE separately (takes only a
-- SHARE UPDATE EXCLUSIVE lock, so reads and writes continue).
ALTER TABLE bookings
    ADD CONSTRAINT ck_bookings__channel_ref_present
    CHECK (channel_ref IS NOT NULL) NOT VALID;

ALTER TABLE bookings VALIDATE CONSTRAINT ck_bookings__channel_ref_present;

-- Release N+3 (optional): promote to a real NOT NULL. In PG 12+ this is cheap
-- because the planner can use the validated CHECK to skip the scan.
ALTER TABLE bookings ALTER COLUMN channel_ref SET NOT NULL;
ALTER TABLE bookings DROP CONSTRAINT ck_bookings__channel_ref_present;
```

#### Renaming a column

A rename is never a rename. It is add → dual-write → backfill → switch reads →
stop writing old → drop.

```sql
-- Release N
ALTER TABLE resources ADD COLUMN display_name TEXT;
-- app writes BOTH `name` and `display_name`, reads `name`

-- Release N+1: batched backfill of display_name from name.
-- app reads `display_name`, still writes both.

-- Release N+2: app writes only `display_name`.

-- Release N+3
ALTER TABLE resources DROP COLUMN name;
```

Four releases for a rename is the correct cost of never taking the product down.

#### Changing a column type

```sql
-- Widening is usually free: PG rewrites nothing for these.
ALTER TABLE bookings ALTER COLUMN party_size TYPE BIGINT;    -- int4 → int8: rewrite
ALTER TABLE services ALTER COLUMN description TYPE TEXT;     -- varchar → text: free

-- NUMERIC precision increase with the same scale is free (no rewrite):
ALTER TABLE cost_records ALTER COLUMN amount TYPE NUMERIC(14,4);

-- Anything narrowing or scale-changing follows the add/backfill/drop dance.
```

#### Adding an index

**Always `CONCURRENTLY`.** A plain `CREATE INDEX` takes a `SHARE` lock that
blocks all writes to the table for the duration — minutes on a large table.

```python
# migrations/versions/0011_add_booking_campaign_index.py
from alembic import op

revision = "0011"
down_revision = "0010"

# CREATE INDEX CONCURRENTLY cannot run inside a transaction block, and Alembic
# wraps migrations in one by default. This flag is mandatory, and forgetting it
# produces a confusing "CREATE INDEX CONCURRENTLY cannot run inside a
# transaction block" at deploy time.
def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_bookings__tenant_campaign
                ON bookings (tenant_id, source_campaign)
                WHERE source_campaign IS NOT NULL
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_bookings__tenant_campaign")
```

A `CONCURRENTLY` build that fails leaves an `INVALID` index that is not used by
the planner but *does* slow writes. A post-deploy check catches these:

```sql
SELECT c.relname AS invalid_index
  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
 WHERE NOT i.indisvalid;
```

#### Adding an enum value

```python
def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot be used in the same transaction that
    # subsequently references the new value. PG 12+ allows the ADD itself
    # inside a transaction, but any statement using it must come later.
    # We therefore run it in an autocommit block and use it in a LATER
    # migration or a later release.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE booking_status ADD VALUE IF NOT EXISTS 'waitlisted'")
```

There is no `DROP VALUE`. Downgrade is not supported for enum additions, and
`downgrade()` raises rather than pretending otherwise:

```python
def downgrade() -> None:
    raise NotImplementedError(
        "PostgreSQL cannot remove an enum value. Roll forward instead."
    )
```

This is also why the schema uses `TEXT` + `CHECK` for anything a vertical
manifest might extend: adding a value there is an ordinary constraint swap.

#### Adding a foreign key

```sql
-- NOT VALID first: instant, no scan of existing rows, brief lock only.
ALTER TABLE booking_notes
    ADD CONSTRAINT fk_booking_notes__bookings
    FOREIGN KEY (tenant_id, booking_id) REFERENCES bookings (tenant_id, id)
    NOT VALID;

-- Then validate under a lighter lock, with reads and writes continuing.
ALTER TABLE booking_notes VALIDATE CONSTRAINT fk_booking_notes__bookings;
```

#### Adding an exclusion constraint to a populated table

This is the one that has no fully-online path, because `ADD CONSTRAINT EXCLUDE`
always builds its index under `ACCESS EXCLUSIVE`. The workaround:

```sql
-- 1. Build the backing index concurrently (no lock on writes).
CREATE INDEX CONCURRENTLY ix_tmp_no_overlap
    ON booking_items USING gist (tenant_id, resource_id, occupies)
    WHERE (is_blocking AND NOT is_capacity_shared);

-- 2. Adopt it. PostgreSQL will reuse an existing matching index rather than
--    rebuilding, so the ACCESS EXCLUSIVE window is milliseconds, not minutes.
ALTER TABLE booking_items
    ADD CONSTRAINT ex_booking_items__no_overlap
    EXCLUDE USING gist (tenant_id WITH =, resource_id WITH =, occupies WITH &&)
    WHERE (is_blocking AND NOT is_capacity_shared);
```

If step 2 fails because live data already violates the constraint, that is
information we needed: it means double-bookings already exist and must be
reconciled before the guarantee can be asserted.

### 16.5 Grants, revocations, and the ordering trap

A migration that creates a table and immediately revokes on it must run the
revoke **after** the default privileges have applied. Alembic's transactional
DDL makes this ordering deterministic within one migration, but across
migrations the rule is: grants in the same migration as the `CREATE TABLE`.

```python
def upgrade() -> None:
    op.create_table("tenant_secrets", ...)
    enable_tenant_rls("tenant_secrets")
    # Column-level narrowing for reporting, per §11.3.
    op.execute("REVOKE ALL ON tenant_secrets FROM receptionai_reporting")
    op.execute("""
        GRANT SELECT (id, tenant_id, scope, name, last_four, expires_at,
                      rotated_at, last_used_at, created_at, updated_at)
            ON tenant_secrets TO receptionai_reporting
    """)
    op.execute("""
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_log
            FROM receptionai_app, receptionai_reporting, receptionai_ops
    """)
```

### 16.6 Testing migrations

Three gates in CI, all against a real PostgreSQL 16 container:

1. **Round-trip.** `alembic upgrade head` then `alembic downgrade base` then
   `upgrade head` again. Catches non-reversible migrations early. Migrations
   that legitimately cannot downgrade (enum additions) declare it explicitly
   and are skipped by the harness via a marker.

2. **Model/schema parity.** After `upgrade head`, run
   `alembic check` (autogenerate diff) and assert it is empty. A non-empty diff
   means a model changed without a migration, which is the most common way a
   schema drifts.

3. **RLS conformance.** The two SQL assertions in §11.7 plus the two-session
   cross-tenant isolation test, generated from the model registry so a new
   `TenantMixin` model automatically gains coverage.

```python
# tests/persistence/test_rls_conformance.py
import pytest
from sqlalchemy import text

from packages.persistence.models.base import Base, TenantMixin

TENANT_TABLES = sorted(
    m.__tablename__
    for m in Base.registry.mappers
    if issubclass(m.class_, TenantMixin)
)


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_rls_enabled_and_forced(db, table):
    row = (await db.execute(text("""
        SELECT relrowsecurity, relforcerowsecurity
          FROM pg_class WHERE relname = :t AND relkind IN ('r','p')
    """), {"t": table})).one()
    assert row.relrowsecurity, f"{table}: RLS not enabled"
    assert row.relforcerowsecurity, f"{table}: RLS not FORCED"


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_cross_tenant_select_is_empty(app_session_a, app_session_b,
                                            seeded_row, table):
    """Session A seeds a row; session B (different tenant) must not see it."""
    got = (await app_session_b.execute(
        text(f"SELECT count(*) FROM {table} WHERE id = :id"),
        {"id": seeded_row[table]},
    )).scalar_one()
    assert got == 0, f"{table}: tenant B can see tenant A's row"


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_cross_tenant_insert_is_rejected(app_session_b, tenant_a_id, table):
    """WITH CHECK must reject a row stamped with another tenant's id."""
    with pytest.raises(Exception) as exc:
        await app_session_b.execute(
            text(f"INSERT INTO {table} (id, tenant_id) "
                 f"VALUES (uuid_generate_v7(), :tid)"),
            {"tid": tenant_a_id},
        )
    assert "row-level security" in str(exc.value).lower()
```

---

## 17. Partitioning & Retention

### 17.1 What is partitioned, and why exactly these five

| Table | Strategy | Interval | Why |
|---|---|---|---|
| `conversations` | RANGE on `created_at` | monthly | Millions of rows/month; retention is time-based; queries are almost always time-bounded |
| `turns` | RANGE on `created_at` | monthly | 10–50× `conversations` volume; the largest table in the system |
| `audit_log` | RANGE on `created_at` | monthly | Append-only, never updated, retention is regulatory and time-based |
| `usage_events` | RANGE on `created_at` | monthly | Append-only; billing queries are always period-bounded; old periods are archived wholesale |
| `ai_checkpoints` | RANGE on `created_at` | monthly | Highest write volume per conversation; 14-day retention makes `DROP` the only sane reclamation; see §9.4 |

**Why monthly and not daily.** Daily partitions on five tables produce ~1,825
partitions per year. Planning time grows with partition count even with pruning,
and `pg_class` bloat degrades every catalogue lookup. Monthly gives 60
partitions per year across all five tables — comfortable — while still making
retention a `DROP TABLE` rather than a `DELETE`.

`ai_checkpoints` is the one table where that reasoning is genuinely close: at
14-day retention, monthly partitions hold up to ~6 weeks of data before a drop
becomes possible. Weekly partitions would track it far more tightly. We keep it
monthly for operational uniformity — one provisioning job, one retention job,
one mental model — and flag weekly as the contained change to make if checkpoint
storage becomes the binding constraint (§18).

**Why `created_at` and not `tenant_id` (hash).** Tenant-hash partitioning is
tempting for isolation, but it does not help retention (the reason we partition
at all), it does not help pruning (queries filter time, not hash buckets), and
it makes adding a tenant-relocation path harder, not easier. Time partitioning
plus tenant-leading indexes gives us both properties.

**Why these five and not `bookings`.** `bookings` is queried across arbitrary
time ranges (a customer's history, a recurring series, a reschedule chain), is
frequently updated, and has foreign keys pointing at it. Partitioning it would
force the partition key into every FK and into the exclusion constraint's
uniqueness scope. Its volume is also two orders of magnitude below `turns`.
Revisit if a single tenant exceeds ~50M bookings.

### 17.2 Partition creation

```sql
-- Create a month's partition for every partitioned table, idempotently.
CREATE OR REPLACE FUNCTION ops_create_monthly_partitions(p_month DATE)
RETURNS TEXT[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    parent      TEXT;
    parents     TEXT[] := ARRAY['conversations','turns','audit_log',
                                'usage_events','ai_checkpoints'];
    part_name   TEXT;
    range_start DATE := date_trunc('month', p_month)::date;
    range_end   DATE := (date_trunc('month', p_month) + INTERVAL '1 month')::date;
    created     TEXT[] := '{}';
BEGIN
    FOREACH parent IN ARRAY parents LOOP
        part_name := format('%s_p%s', parent, to_char(range_start, 'YYYYMM'));

        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = part_name
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                part_name, parent, range_start, range_end
            );
            -- Partitions inherit the parent's indexes and RLS automatically.
            -- Ownership must be set explicitly so default privileges apply.
            EXECUTE format('ALTER TABLE %I OWNER TO receptionai_owner', part_name);
            created := created || part_name;
        END IF;
    END LOOP;

    RETURN created;
END;
$$;

REVOKE ALL ON FUNCTION ops_create_monthly_partitions(DATE) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_create_monthly_partitions(DATE) TO receptionai_ops;
```

**Run three months ahead, every day.** A Temporal schedule calls
`ops_create_monthly_partitions(current_date + interval '1 month')`,
`+ 2 months`, and `+ 3 months` daily. Running it daily and idempotently means a
missed run is self-healing, and three months of headroom means an outage in the
scheduler has to last a very long time before an insert fails.

**The DEFAULT partition is deliberately absent.** A default partition would
silently accept rows outside every defined range — which is precisely the
failure we want to be loud. An insert with no matching partition raises
`23514`, pages someone, and gets fixed. A default partition would instead
accumulate rows that then make *adding* the correct partition require an
`ACCESS EXCLUSIVE` scan of the default. We accept the loud failure.

Bootstrap:

```sql
-- Create the current and next three months at deploy time.
SELECT ops_create_monthly_partitions(d::date)
  FROM generate_series(date_trunc('month', now()),
                       date_trunc('month', now()) + INTERVAL '3 months',
                       INTERVAL '1 month') AS d;
```

### 17.3 Retention policy

Retention is **per tenant** where regulation permits, with a platform maximum.
The policy lives in `tenant_settings` under the `retention` namespace:

```json
{
  "conversations_days": 730,
  "turns_days": 365,
  "transcripts_days": 365,
  "recordings_days": 90,
  "audit_log_days": 2555,
  "usage_events_days": 1095,
  "ai_checkpoints_days": 14,
  "customers_dormancy_days": 730,
  "pii_redact_after_days": 90
}
```

| Data | Default retention | Floor | Ceiling | Driver |
|---|---|---|---|---|
| `conversations` | 24 months | 30 days | 7 years | Product analytics |
| `turns` | 12 months | 30 days | 7 years | Volume; older turns have little value |
| `transcripts` | 12 months | 30 days | 7 years | Same as turns |
| `call_recordings` | 90 days | 7 days | 7 years | Storage cost and consent scope |
| `audit_log` | 7 years | 1 year | 10 years | SOC 2 / HIPAA |
| `usage_events` | 3 years | 13 months | 7 years | Billing dispute window |
| `ai_checkpoints` | **14 days** | 1 day | 30 days | Resume window only; highest-density PII in the system (§9.4) |
| `cost_records` | 7 years | 3 years | 10 years | Financial records |
| `customers` / `customer_identities` | 24 months **dormant** | 90 days | 7 years | Dormancy-based, not age-based (§3.9) |
| `outbox_events` (dispatched) | 7 days | 1 day | 30 days | Operational only |
| `sessions` (expired) | 30 days | 1 day | 90 days | Incident forensics |

### 17.4 The three-stage retention pipeline

Because partitions hold multiple tenants with different policies, we cannot
simply drop a partition when the shortest policy expires. Three stages:

**Stage 1 — PII redaction (in place, per tenant).** Runs daily. Nulls or hashes
PII-classified columns for conversations older than the tenant's
`pii_redact_after_days`, while keeping the row for analytics. This is what makes
a long analytics retention compatible with a short PII retention.

```sql
CREATE OR REPLACE FUNCTION ops_redact_conversation_pii(
    p_tenant_id UUID, p_older_than TIMESTAMPTZ, p_limit INTEGER DEFAULT 5000
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    n INTEGER;
BEGIN
    WITH target AS (
        SELECT tenant_id, created_at, id
          FROM conversations
         WHERE tenant_id = p_tenant_id
           AND created_at < p_older_than
           AND redacted_at IS NULL
         ORDER BY created_at
         LIMIT p_limit
    ), red_conv AS (
        UPDATE conversations c
           SET contact_identifier = NULL,
               contact_name       = NULL,
               channel_metadata   = '{}'::jsonb,
               redacted_at        = now()
          FROM target t
         WHERE c.tenant_id = t.tenant_id
           AND c.created_at = t.created_at
           AND c.id = t.id
        RETURNING c.id
    ), red_turns AS (
        UPDATE turns tu
           SET content       = NULL,
               content_parts = NULL,
               tool_result   = NULL,
               pii_redacted  = TRUE
          FROM red_conv rc
         WHERE tu.tenant_id = p_tenant_id
           AND tu.conversation_id = rc.id
           AND NOT tu.pii_redacted
        RETURNING 1
    ), red_tx AS (
        UPDATE transcripts tr
           SET full_text = NULL, words = NULL, redacted_at = now()
          FROM red_conv rc
         WHERE tr.tenant_id = p_tenant_id
           AND tr.conversation_id = rc.id
           AND tr.redacted_at IS NULL
        RETURNING 1
    )
    SELECT count(*)::INTEGER INTO n FROM red_conv;

    RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION ops_redact_conversation_pii(UUID, TIMESTAMPTZ, INTEGER)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_redact_conversation_pii(UUID, TIMESTAMPTZ, INTEGER)
    TO receptionai_ops;
```

**Stage 2 — Archival (partition detach).** When a partition is older than the
*longest* tenant policy applying to it, detach it concurrently, dump it to
object storage as Parquet, then drop it.

```sql
CREATE OR REPLACE FUNCTION ops_detach_partition(p_parent TEXT, p_partition TEXT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    -- CONCURRENTLY avoids the ACCESS EXCLUSIVE lock on the parent that a plain
    -- DETACH takes. It cannot run inside a transaction block, so this function
    -- is invoked with autocommit from the ops worker.
    EXECUTE format('ALTER TABLE %I DETACH PARTITION %I CONCURRENTLY',
                   p_parent, p_partition);
END;
$$;

REVOKE ALL ON FUNCTION ops_detach_partition(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_detach_partition(TEXT, TEXT) TO receptionai_ops;
```

**Checkpoints need a sweeper as well as a partition drop.** With 14-day
retention and monthly partitions, a partition is droppable only after ~6 weeks
— so between drops, up to a month of dead checkpoints would sit in the live
partition holding high-density PII well past their policy. The partition drop
reclaims *space*; this sweeper enforces the *policy*:

```sql
CREATE OR REPLACE FUNCTION ops_purge_expired_checkpoints(
    p_tenant_id UUID, p_retain_days INTEGER, p_limit INTEGER DEFAULT 10000
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    n INTEGER;
    cutoff TIMESTAMPTZ := now() - make_interval(days => p_retain_days);
BEGIN
    WITH doomed AS (
        DELETE FROM ai_checkpoints
         WHERE (tenant_id, created_at, id) IN (
             SELECT tenant_id, created_at, id
               FROM ai_checkpoints
              WHERE tenant_id = p_tenant_id
                AND created_at < cutoff
              ORDER BY created_at
              LIMIT p_limit
         )
        RETURNING 1
    )
    SELECT count(*)::INTEGER INTO n FROM doomed;

    RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION ops_purge_expired_checkpoints(UUID, INTEGER, INTEGER)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_purge_expired_checkpoints(UUID, INTEGER, INTEGER)
    TO receptionai_ops;
```

Batched and ordered by `created_at` so each call touches the oldest rows in the
fewest pages, and so a large tenant's purge cannot hold a long transaction.
Run daily per tenant, reading `retention.ai_checkpoints_days` from
`tenant_settings`. This is the one retention path that deletes rather than
redacts, for the reason given in §9.4: a serialised blob has no columns to null.

The archival workflow (Temporal, `ingestion` task queue):

1. Identify partitions where `upper_bound < now() - max_retention_for_any_tenant`.
2. `COPY (SELECT * FROM <partition>) TO PROGRAM 'aws s3 cp - s3://…'` — or,
   in practice, a Python activity streaming with `COPY … TO STDOUT (FORMAT
   binary)` into a Parquet writer, so the archive is queryable by Athena.
3. Verify the object's row count and checksum against the partition.
4. `ops_detach_partition(...)`, then `DROP TABLE <partition>`.
5. Write an `audit_log` entry per affected tenant recording the deletion.

**Why detach-then-drop rather than drop directly:** the detach is reversible up
until the drop. If step 3's verification fails, we re-attach and retry, having
lost nothing.

**Stage 3 — Tenant-scoped deletion (GDPR erasure).** A right-to-erasure request
cannot wait for a partition to age out. It runs immediately, per tenant, as
targeted `DELETE`s within partitions:

```sql
CREATE OR REPLACE FUNCTION ops_erase_tenant_conversation(
    p_tenant_id UUID, p_conversation_id UUID, p_created_at TIMESTAMPTZ
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    DELETE FROM turns
     WHERE tenant_id = p_tenant_id
       AND conversation_id = p_conversation_id
       AND created_at >= p_created_at - INTERVAL '1 day'
       AND created_at <  p_created_at + INTERVAL '30 days';

    DELETE FROM transcripts
     WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;

    -- Recordings are objects in blob storage; mark for deletion and let the
    -- StoragePort reaper remove them, then the row goes.
    UPDATE call_recordings
       SET retain_until = now() - INTERVAL '1 second'
     WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;

    DELETE FROM conversation_outcomes
     WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;

    -- Checkpoints are opaque BYTEA: they cannot be partially redacted the way
    -- turns.content can, so erasure means deletion. Bounded by created_at so
    -- the delete prunes to the partitions that can possibly hold them; the
    -- 14-day retention means there is usually nothing here at all.
    DELETE FROM ai_checkpoints
     WHERE tenant_id = p_tenant_id
       AND conversation_id = p_conversation_id
       AND created_at >= p_created_at - INTERVAL '1 day'
       AND created_at <  p_created_at + INTERVAL '31 days';

    DELETE FROM conversations
     WHERE tenant_id = p_tenant_id
       AND id = p_conversation_id
       AND created_at = p_created_at;
END;
$$;

REVOKE ALL ON FUNCTION ops_erase_tenant_conversation(UUID, UUID, TIMESTAMPTZ)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops_erase_tenant_conversation(UUID, UUID, TIMESTAMPTZ)
    TO receptionai_ops;
```

**`audit_log` is exempt from erasure.** A record that a deletion occurred must
survive the deletion, or the audit trail is worthless. The audit row for an
erasure contains the entity id and the actor, never the erased content.
Legitimate-interest basis under GDPR Art. 17(3)(b)/(e); documented in the DPA.

### 17.5 Autovacuum tuning for the hot tables

Partitioned append-only tables need different autovacuum settings from the
transactional core. The defaults assume a mixed workload and are wrong here in
both directions.

```sql
-- turns / usage_events / audit_log: insert-only. They never accumulate dead
-- tuples, but they DO need frequent ANALYZE so the planner's estimate of
-- created_at's max keeps up — otherwise "last 24 hours" queries plan against
-- a stale histogram and choose a seq scan.
ALTER TABLE turns SET (
    autovacuum_vacuum_scale_factor  = 0.0,
    autovacuum_vacuum_threshold     = 50000,
    autovacuum_analyze_scale_factor = 0.0,
    autovacuum_analyze_threshold    = 10000,
    -- Insert-only tables still need vacuum for freezing and for the
    -- visibility map (without which index-only scans never happen).
    autovacuum_vacuum_insert_scale_factor = 0.0,
    autovacuum_vacuum_insert_threshold    = 100000
);

ALTER TABLE audit_log     SET (autovacuum_vacuum_insert_threshold = 100000,
                               autovacuum_analyze_threshold = 10000,
                               autovacuum_analyze_scale_factor = 0.0);
ALTER TABLE usage_events  SET (autovacuum_vacuum_insert_threshold = 100000,
                               autovacuum_analyze_threshold = 10000,
                               autovacuum_analyze_scale_factor = 0.0);

-- booking_holds: extreme churn. Thousands of short-lived rows per hour, all
-- updated once and then swept. Default scale factors would let it bloat
-- badly between vacuums, and its GiST exclusion index is the thing that
-- suffers most from bloat.
ALTER TABLE booking_holds SET (
    autovacuum_vacuum_scale_factor  = 0.02,
    autovacuum_vacuum_threshold     = 100,
    autovacuum_analyze_scale_factor = 0.02,
    fillfactor                      = 80    -- room for HOT updates in-page
);

-- bookings: moderate churn with frequent status updates. Lower fillfactor
-- enables HOT updates, which do not touch indexes at all — meaningful, given
-- how many indexes bookings carries.
ALTER TABLE bookings SET (fillfactor = 85);

-- outbox_events: written once, updated once, deleted. Same profile as holds.
ALTER TABLE outbox_events SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold    = 500,
    fillfactor                     = 80
);

-- chunks: vectors are large and PLAIN-stored. Bloat here is expensive in
-- absolute bytes, and the HNSW index does not tolerate it well.
ALTER TABLE chunks SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

-- Statistics targets: the columns the planner most needs to estimate well.
ALTER TABLE bookings ALTER COLUMN tenant_id SET STATISTICS 1000;
ALTER TABLE turns    ALTER COLUMN tenant_id SET STATISTICS 1000;
ALTER TABLE chunks   ALTER COLUMN tenant_id SET STATISTICS 1000;
```

**Why `SET STATISTICS 1000` on `tenant_id`.** Tenant distribution is extremely
skewed: a handful of large tenants hold most rows. With the default 100 buckets,
the planner's most-common-values list misses mid-sized tenants entirely and
estimates their row counts from an average — producing plans tuned for a tenant
that does not exist. 1000 buckets captures the long tail and is the single
highest-leverage planner tuning in this schema.

### 17.6 Extended statistics

Correlated columns whose independence assumption produces bad estimates:

```sql
-- tenant_id and location_id are strongly correlated (a location belongs to
-- exactly one tenant), so the planner's independent-selectivity multiplication
-- underestimates by orders of magnitude.
CREATE STATISTICS st_bookings__tenant_location (dependencies, ndistinct)
    ON tenant_id, location_id FROM bookings;

CREATE STATISTICS st_bookings__tenant_service_status (dependencies, ndistinct)
    ON tenant_id, service_id, status FROM bookings;

CREATE STATISTICS st_resources__tenant_type_location (dependencies, ndistinct)
    ON tenant_id, resource_type_id, location_id FROM resources;

CREATE STATISTICS st_turns__tenant_conversation (dependencies, ndistinct)
    ON tenant_id, conversation_id FROM turns;

CREATE STATISTICS st_chunks__tenant_document (dependencies, ndistinct)
    ON tenant_id, document_id, document_version_id FROM chunks;
```

---

## 18. Deliberately Deferred

Recorded so that their absence is understood as a decision, not an omission.

| Question | Deferred because | Trigger to revisit |
|---|---|---|
| Tenant-hash sub-partitioning of `turns` | Monthly time partitions plus tenant-leading indexes are sufficient at target scale | A single partition exceeding ~200M rows |
| Weekly partitions for `ai_checkpoints` | Monthly keeps one provisioning job and one mental model across all five partitioned tables; 14-day retention wastes at most ~4 weeks of space (§17.1) | Checkpoint storage becoming the dominant line item, or a tenant needing sub-7-day retention |
| Per-language partial GIN indexes on `chunks` | One config-aware GIN index serves every language (§7.4.1); lexemes from different configs occupy disjoint index regions | A single tenant's multi-language corpus showing measurable cross-language index bloat |
| Stemmer support for CJK/Thai lexical search | PostgreSQL ships no snowball stemmer for these; they fall back to `'simple'` and rely on the vector half of hybrid search, which handles them well | Selling into a CJK market where lexical recall proves insufficient; would mean a `pgroonga`/`zhparser` extension dependency |
| Materialised availability slots | Rules-on-demand + Redis caching is correct and far cheaper (§5.6) | p95 availability latency > 200 ms after cache tuning |
| Separate read replicas in the router | `ConnectionRouter` already abstracts engine selection; adding a `readonly` engine per shard is a config change | Read load exceeding ~60% of primary capacity |
| A dedicated vector store | pgvector + the three mitigations in §7.4 | More than a handful of tenants needing partial HNSW indexes |
| Event sourcing for bookings | `audit_log` + `outbox_events` give reconstructable history without the read-model complexity | A regulatory requirement for full state reconstruction |
| Multi-currency FX normalisation | Every tenant bills in one currency today; `cost_records` stores the native pair | First tenant operating across currency zones |
| Bi-temporal booking history (valid-time vs transaction-time) | `version` + `audit_log` covers "who changed what when"; full bi-temporality is a large complexity increase | A vertical requiring "what did the schedule look like as of last Tuesday" as a product feature |

---

## Appendix A — Table Inventory

**Tenant-owned (RLS: `ENABLE` + `FORCE`, policy on `tenant_id`) — 42 tables**

`locations`, `tenant_settings`, `tenant_secrets`, `tenant_provider_config`,
`subscriptions`, `user_tenant_roles`, `api_keys`, `customers`,
`customer_identities`, `tenant_vertical_installs`,
`resource_types`, `resources`, `services`, `service_resource_requirements`,
`availability_rules`, `availability_exceptions`, `holidays`, `bookings`,
`booking_items`, `booking_holds`, `waitlist_entries`, `recurrence_rules`,
`conversations`*, `turns`*, `transcripts`, `call_recordings`,
`conversation_outcomes`, `knowledge_sources`, `documents`, `document_versions`,
`chunks`, `ingestion_jobs`, `tenant_connectors`, `connector_credentials`,
`sync_state`, `workflow_runs`, `ai_checkpoints`*, `audit_log`*,
`outbox_events`, `usage_events`*, `cost_records`, `notifications`

*= partitioned monthly by `created_at`

**Tenant-scoped by self (RLS on `id`) — 1 table**

`tenants`

**Nullable-tenant, split policy (template + tenant rows) — 4 tables**

`roles`, `role_permissions`, `workflow_definitions`, `workflow_versions`

**User-scoped (RLS on `app.user_id`) — 4 tables**

`users` (membership-scoped for SELECT), `sessions`, `mfa_factors`,
`oauth_identities`

**Platform catalogue (no RLS; privilege-controlled, read-only to runtime) — 5 tables**

`permissions`, `plans`, `vertical_definitions`, `vertical_versions`,
`connector_definitions`

**Total: 56 tables.**

## Appendix B — Function Inventory

| Function | Kind | Granted to | Purpose |
|---|---|---|---|
| `uuid_generate_v7()` | VOLATILE | PUBLIC | Time-ordered PK default |
| `uuid_v7_timestamp(uuid)` | IMMUTABLE | PUBLIC | Extract creation instant from a v7 key |
| `app_current_tenant()` | STABLE | PUBLIC | RLS predicate source; NULL when unset |
| `app_is_platform_admin()` | STABLE | PUBLIC | Platform-catalogue gate only (§11.4) |
| `set_updated_at()` | trigger | — | `updated_at` maintenance |
| `set_search_vector()` | trigger | — | Config-aware tsvector maintenance (§7.4.1) |
| `guard_vertical_version_immutable()` | trigger | — | Published manifests are immutable |
| `sync_vertical_version_arrays()` | trigger | — | Keep denormalized arrays honest |
| `guard_workflow_version_immutable()` | trigger | — | Published workflow config is immutable |
| `sync_booking_item_occupancy()` | trigger | — | Maintains the exclusion constraint's inputs |
| `cascade_booking_period_to_items()` | trigger | — | Reschedule/cancel propagation |
| `check_shared_capacity()` | constraint trigger | — | Shared-capacity limit (§5.10) |
| `check_conversation_customer_tenant()` | constraint trigger | — | Cross-tenant customer_id guard (§6.1) |
| `booking_check_conflicts(...)` | plain | `app` | Hold+booking conflict check under advisory lock |
| `booking_convert_hold(...)` | plain | `app` | Atomic hold → booking conversion |
| `auth_resolve_user_by_email(text)` | SECURITY DEFINER | `auth` | Pre-context login lookup |
| `auth_resolve_api_key(text)` | SECURITY DEFINER | `auth` | Pre-context API-key lookup |
| `auth_list_memberships(uuid)` | SECURITY DEFINER | `auth` | Tenant picker at login |
| `ops_expire_stale_holds(int)` | SECURITY DEFINER | `ops` | Hold TTL sweeper |
| `ops_create_monthly_partitions(date)` | SECURITY DEFINER | `ops` | Partition provisioning |
| `ops_detach_partition(text,text)` | SECURITY DEFINER | `ops` | Archival detach |
| `ops_purge_expired_checkpoints(...)` | SECURITY DEFINER | `ops` | Checkpoint TTL enforcement (§17.4) |
| `ops_erase_customer(uuid,uuid)` | SECURITY DEFINER | `ops` | Customer erasure + identifier destruction (§3.9) |
| `ops_purge_dormant_customers(...)` | SECURITY DEFINER | `ops` | Dormancy-based customer retention (§3.9) |
| `ops_redact_conversation_pii(...)` | SECURITY DEFINER | `ops` | Stage-1 retention |
| `ops_erase_tenant_conversation(...)` | SECURITY DEFINER | `ops` | GDPR erasure |

Every `SECURITY DEFINER` function carries `SET search_path = public, pg_temp`
and is `REVOKE ALL ... FROM PUBLIC` before its single explicit `GRANT`.
