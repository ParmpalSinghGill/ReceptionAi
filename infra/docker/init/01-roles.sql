-- ===========================================================================
-- Role separation — Core Contract §2.5.
--
-- This is the single most important file in local setup. Contract §2 says the
-- application's runtime DB role is NOT the table owner and does NOT have
-- BYPASSRLS. If the app connects as `postgres` (a superuser, implicitly
-- BYPASSRLS), every RLS policy in the system silently does nothing, every
-- isolation test passes, and every cross-tenant bug ships. The separation must
-- exist in local development or it is not being tested anywhere.
--
-- DIVISION OF RESPONSIBILITY with migration 0001 — read this before editing.
--
-- Migration 0001 owns the ROLE MODEL: it creates receptionai_{owner,app,auth,
-- reporting,ops} idempotently, NOLOGIN, and attaches every grant, policy and
-- default privilege to them. That is schema, it is versioned, and it must stay
-- the single source of truth — duplicating the model here would give two
-- definitions that silently drift.
--
-- This file owns only what a LOCAL DEVELOPMENT database additionally needs,
-- which the migration deliberately does not do:
--
--   1. receptionai_migrator — the role that RUNS Alembic. Not part of the
--      application role model at all; it is the deploy identity. It needs
--      CREATEROLE (the migration creates roles) and ownership of the schema.
--   2. LOGIN + a password on the roles a developer actually connects as.
--      The migration creates them NOLOGIN precisely so its definitions can
--      never be used to connect; production provisions credentials out of
--      band. Locally, someone has to.
--
-- Ordering note: this runs BEFORE any migration, so it must not assume the
-- application roles exist. It creates receptionai_app if absent and the
-- migration's `IF NOT EXISTS` guard then leaves it alone — either order works.
--
-- Run automatically by the postgres image's docker-entrypoint-initdb.d hook,
-- once, on an empty data directory. `make reset` (which drops the volume) is
-- the way to re-run it.
-- ===========================================================================

-- Password comes from the environment so this file has no secret literal in it
-- and so compose can hand the same value to the application. \set + :'var'
-- quotes it correctly; string-concatenating a password into DDL is how you get
-- a SQL injection in a bootstrap script.
\set app_password `echo "${RECEPTIONAI_DB_PASSWORD:-localpass}"`

-- --------------------------------------------------------------- migrator ---
-- The deploy identity: the role Alembic connects as.
--
--   CREATEROLE — migration 0001 creates the five application roles. Without
--                this, `alembic upgrade head` fails with "permission denied to
--                create role" on a fresh database. Verified by running it.
--   CREATEDB   — the test harness mints per-worker databases.
--
-- Deliberately NOT SUPERUSER: a migration that needs superuser is a migration
-- doing something the production deploy will not be allowed to do, and it is
-- far better to discover that here than in a change window. (Extensions are
-- the one genuine superuser need, which is why 02-extensions.sql pre-creates
-- them and the migration's CREATE EXTENSION IF NOT EXISTS becomes a no-op.)
CREATE ROLE receptionai_migrator LOGIN PASSWORD :'app_password' CREATEROLE CREATEDB;

-- ------------------------------------------------------------------- app ---
-- The runtime role. Created here only to attach LOGIN and a password, because
-- migration 0001 creates it NOLOGIN and a developer has to be able to connect.
-- If the migration ran first, its IF NOT EXISTS guard leaves this untouched;
-- the ALTER below then applies either way.
--
-- NOBYPASSRLS is the load-bearing clause and is stated explicitly even though
-- it is the default: an explicit NOBYPASSRLS is a claim a reader can verify,
-- whereas an omission is indistinguishable from an oversight.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receptionai_app') THEN
        CREATE ROLE receptionai_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;
ALTER ROLE receptionai_app LOGIN PASSWORD :'app_password' NOBYPASSRLS;

-- ----------------------------------------------- reporting (read-only) ---
-- Analytics and debugging. Part of the migration's role model; same treatment.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receptionai_reporting') THEN
        CREATE ROLE receptionai_reporting NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;
ALTER ROLE receptionai_reporting LOGIN PASSWORD :'app_password' NOBYPASSRLS;

-- ----------------------------------------------------------------- owner ---
-- The table owner in the migration's role model. Created here, ahead of the
-- migration's own idempotent CREATE, for one reason: the migrator must be able
-- to SET ROLE to it.
--
-- Migration 0001 sets objects' ownership to receptionai_owner and calls
-- SECURITY DEFINER functions that assume it. A CREATEROLE role is NOT
-- automatically a member of the roles it creates, so without the membership
-- grant below the migration fails with "must be able to SET ROLE
-- receptionai_owner" — found by running it, not by reading it.
--
-- WITH ADMIN OPTION so the migrator can manage the membership later without
-- needing the superuser back.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receptionai_owner') THEN
        CREATE ROLE receptionai_owner NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT NOLOGIN;
    END IF;
END
$$;
GRANT receptionai_owner TO receptionai_migrator WITH ADMIN OPTION;

GRANT CONNECT ON DATABASE receptionai
    TO receptionai_app, receptionai_reporting, receptionai_migrator;

-- The migrator owns the public schema so objects it creates are owned by it
-- rather than by `postgres`. Without this, Alembic running as the migrator
-- cannot create tables in `public` on PG15+, where PUBLIC lost CREATE by
-- default. Migration 0001 reassigns table ownership to receptionai_owner.
ALTER SCHEMA public OWNER TO receptionai_migrator;

GRANT USAGE ON SCHEMA public TO receptionai_app, receptionai_reporting;

-- receptionai_owner needs CREATE, not merely USAGE: migration 0001's
-- `ops_create_monthly_partitions()` is SECURITY DEFINER owned by
-- receptionai_owner and creates partition tables at runtime, long after the
-- migration finishes. Without this it fails with "permission denied for schema
-- public" the first time a partition is rolled — which would happen in
-- production, on a schedule, not during a deploy anyone was watching.
GRANT USAGE, CREATE ON SCHEMA public TO receptionai_owner;

-- ------------------------------------------------------ default privileges ---
-- Grants for objects the BOOTSTRAP SUPERUSER creates — i.e. the functions in
-- 02-extensions.sql. Everything the migration creates is covered by the
-- ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner statements inside the
-- migration itself, which is where they belong.
--
-- Note these are DML only: the app role can read and write rows and can never
-- issue DDL, so an application-level SQL injection cannot DROP a table or,
-- more subtly, ALTER TABLE ... DISABLE ROW LEVEL SECURITY.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO receptionai_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO receptionai_app, receptionai_reporting;
