-- ===========================================================================
-- Extensions and the database-side UUIDv7 generator.
--
-- Extensions require superuser, so they are created here by the bootstrap
-- `postgres` role rather than by an Alembic migration running as the
-- unprivileged migrator. A migration that needs superuser is a migration that
-- cannot run against managed RDS the same way it runs here.
-- ===========================================================================

-- These must match the set migration 0001 requests. `CREATE EXTENSION IF NOT
-- EXISTS` is a no-op when the extension is already present, so pre-creating
-- them here as the superuser is what lets the UNPRIVILEGED migrator role run
-- that migration at all: extension creation needs CREATE on the database,
-- which the migrator deliberately does not have (contract §2.5).
--
-- Verified by running the real migration: a missing entry here surfaces as
-- `InsufficientPrivilegeError: permission denied to create extension`, which
-- is the correct failure — the privilege separation working, not a bug.
CREATE EXTENSION IF NOT EXISTS vector;              -- pgvector, contract §1
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  -- query-level observability
CREATE EXTENSION IF NOT EXISTS pgcrypto;            -- gen_random_bytes, digest
CREATE EXTENSION IF NOT EXISTS btree_gin;           -- composite (tenant_id, …) GIN
CREATE EXTENSION IF NOT EXISTS btree_gist;          -- exclusion constraints on bookings
CREATE EXTENSION IF NOT EXISTS pg_trgm;             -- trigram search on names

-- ---------------------------------------------------------------------------
-- uuid_generate_v7() is NOT defined here, deliberately.
--
-- Migration 0001 (`_create_functions`) owns it, per 01-DATA-MODEL.md §1.2 and
-- contract amendment A2. An earlier revision of this file defined it too, on
-- the reasoning that a fresh database should have the generator before the
-- first migration runs. That was wrong twice over, and both failures were
-- found by actually running the migration rather than by reading it:
--
--   1. It broke the migration outright. A function created here is owned by
--      the bootstrap superuser, and the migration's CREATE OR REPLACE then
--      fails with "must be owner of function uuid_generate_v7" — the
--      unprivileged migrator cannot replace a superuser's function.
--
--   2. It was a second source of truth for an RFC 9562 bit layout. A2 requires
--      the Python and SQL generators to produce byte-identical layouts, and
--      two hand-written PL/pgSQL definitions that must agree with a third
--      Python implementation is exactly the drift A2 warns about. If they
--      diverged, uuid7_timestamp() would recover a different partition key
--      depending on which side minted the row — silently.
--
-- The extensions above are the genuine exception: they need superuser, so the
-- migration cannot create them and pre-creating them here is what allows the
-- unprivileged migrator to run `CREATE EXTENSION IF NOT EXISTS` as a no-op.
-- Nothing else in this file should grow into schema ownership.
-- ---------------------------------------------------------------------------
