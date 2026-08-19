"""Resolve a tenant by public slug, before any tenant context exists.

The gap this closes. ``tenants``' RLS policy is ``id = app_current_tenant()``
(01 §11.5), which is correct and deliberate: a tenant may see itself and
nothing else. But it makes slug → id resolution impossible for *every* role,
because knowing the id is the precondition for reading the row that contains
it. Anything needing to start from a public identifier is therefore locked out:

* the public demo widget, which knows only ``northgate-dental``;
* a hosted chat page served at ``/c/{slug}``;
* an inbound webhook that carries a tenant slug and no credential.

This is exactly the shape of problem 01 §11.6 already solves for login, and it
is solved the same way: a ``SECURITY DEFINER`` function with a deliberately
narrow return, rather than a weakened policy. A policy change would widen
``tenants`` for every query in the system; a function widens exactly one
lookup, and its signature is the audit surface.

**What it returns is the whole security argument.** ``id``, ``slug``,
``display_name``, ``status``, ``default_timezone``, ``default_locale``. Public
identity, and nothing else — no ``kms_key_id``, no ``shard_key``, no
``metadata``, no ``compliance_profile``, no ``suspension_reason``. Those are
the fields that make a tenant row sensitive, and none of them is reachable
through this. The blast radius of the function being called by something that
should not have is "learned a business's display name from its slug", which is
information the business publishes on its own website.

It also filters ``deleted_at IS NULL`` and returns nothing for a deleted
tenant, so a slug does not resolve after erasure.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


#: Every table migration 0001 created. Reassigned to ``receptionai_owner``
#: below — see :func:`_fix_table_ownership`.
_TABLES: tuple[str, ...] = (
    "tenants",
    "locations",
    "tenant_settings",
    "users",
    "permissions",
    "roles",
    "role_permissions",
    "user_tenant_roles",
    "sessions",
    "conversations",
    "turns",
)


def _fix_table_ownership() -> None:
    """Hand the tables to ``receptionai_owner``, which 0001 intended but missed.

    Migration 0001 builds its whole privilege model around
    ``receptionai_owner``: it sets ``ALTER DEFAULT PRIVILEGES FOR ROLE
    receptionai_owner``, it assigns every dynamically-created *partition* to
    that role so those defaults apply, and 01 §16.1 describes it as the table
    owner. But it never reassigns the parent tables, so they stayed owned by
    ``receptionai_migrator`` — the deploy identity, which should own nothing at
    runtime.

    Two consequences, both silent until now:

    1. **The default privileges never fire.** They are keyed on objects created
       *by* ``receptionai_owner``, and every table was created by the migrator.
       0001 papers over this with explicit ``GRANT ... ON ALL TABLES``, which
       is why nothing has failed yet — but the next migration to add a table
       and rely on the defaults would produce a table the app cannot read.
    2. **No role can bypass RLS for a legitimate ``SECURITY DEFINER``
       function.** ``row_security = off`` requires table ownership or
       ``BYPASSRLS``; with the tables owned by the migrator, a function owned
       by ``receptionai_owner`` cannot use it, which is what blocked
       ``public_resolve_tenant_by_slug`` below.

    Reassigning is safe and idempotent: ownership does not change any grant,
    any policy, or any row. It makes the schema match what 0001 already
    documents.
    """
    for table in _TABLES:
        # Identifiers cannot be bound; these are module-level constants.
        op.execute(f"ALTER TABLE {table} OWNER TO receptionai_owner")

    # The partitions inherit nothing here — they were already assigned by
    # `ops_create_monthly_partitions` — but future ones are created by that
    # SECURITY DEFINER function, which now genuinely owns its parents.


def _add_owner_lookup_policy() -> None:
    """Admit ``receptionai_owner`` to ``tenants`` for SELECT.

    **Why a policy and not `row_security = off`.** The obvious way to write a
    SECURITY DEFINER lookup is to disable row security inside it. That cannot
    work here, and the reason is worth stating because it is easy to spend an
    hour on: `FORCE ROW LEVEL SECURITY` means *the owner is subject to policies
    too*, and PostgreSQL refuses `row_security = off` for a role that is not
    exempt — which, under FORCE, includes the owner. FORCE is contract §2.2 and
    is not negotiable, so the answer is to give the definer role a policy.

    **Why this does not widen anything.** `receptionai_owner` is `NOLOGIN`
    (migration 0001). Nothing connects as it. The only way to execute under it
    is to be inside a SECURITY DEFINER function it owns, and the only such
    function that touches `tenants` is `public_resolve_tenant_by_slug`, whose
    `RETURNS TABLE` exposes six public columns. So the reachable surface of
    this policy is exactly that function's signature.

    The policy is SELECT-only and names the role explicitly. It grants no
    INSERT, UPDATE or DELETE, so it cannot be used to create or modify a
    tenant even from inside a definer function.
    """
    op.execute(
        """
        CREATE POLICY rls_tenants__owner_lookup ON tenants
            FOR SELECT
            TO receptionai_owner
            USING (deleted_at IS NULL)
        """
    )

    # The same treatment for the two tables 01 §11.6's login functions read,
    # and for the same reason: those functions are SECURITY DEFINER precisely
    # because they must read *before* a tenant context exists, but under FORCE
    # the definer is still policy-filtered — so without a policy admitting it,
    # `auth_resolve_user_by_email` returns zero rows for a user that exists and
    # login reports "email or password is incorrect". A silent wrong answer, on
    # the authentication path.
    #
    # `users` is global (01 §3.2), so a row here is not tenant data; the
    # function's own RETURNS TABLE is what bounds it to authentication
    # material. `user_tenant_roles` is tenant-owned, and the policy is
    # SELECT-only for the same NOLOGIN role, reachable only through
    # `auth_list_memberships`, whose return is (tenant_id, slug, status,
    # role_code, location_id) for ONE user id the caller already holds.
    op.execute(
        """
        CREATE POLICY rls_users__owner_auth_lookup ON users
            FOR SELECT
            TO receptionai_owner
            USING (deleted_at IS NULL)
        """
    )
    op.execute(
        """
        CREATE POLICY rls_user_tenant_roles__owner_auth_lookup ON user_tenant_roles
            FOR SELECT
            TO receptionai_owner
            USING (true)
        """
    )
    op.execute(
        """
        CREATE POLICY rls_roles__owner_auth_lookup ON roles
            FOR SELECT
            TO receptionai_owner
            USING (true)
        """
    )


def _add_refresh_functions() -> None:
    """Refresh-token rotation, which has the same bootstrapping problem as login.

    ``sessions``' RLS policy is ``user_id = app.user_id`` (01 §11.5) — correct,
    because a user must see their own sessions across every tenant they belong
    to. But refresh presents *only an opaque token*: we do not know the user
    until we have found the row, and we cannot find the row without knowing the
    user. Exactly the circularity ``auth_resolve_user_by_email`` exists to
    break, so it is broken the same way.

    ``auth_find_session_by_token`` takes a **digest**, never a token: the
    caller hashes first, so a plaintext refresh token never appears in a query,
    a query log, or ``pg_stat_statements``. The row is addressed by 256 bits of
    entropy, which is not enumerable.

    ``auth_revoke_session_family`` is separate rather than a flag on the
    lookup, because revoking a family is the *theft response* (05 §A1.3) and
    deserves to be a call someone can grep for.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_find_session_by_token(p_digest BYTEA)
        RETURNS TABLE (
            id UUID, user_id UUID, tenant_id UUID, family_id UUID,
            revoked_at TIMESTAMPTZ, expires_at TIMESTAMPTZ
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            RETURN QUERY
            SELECT s.id, s.user_id, s.tenant_id, s.family_id,
                   s.revoked_at, s.expires_at
              FROM sessions s
             WHERE s.refresh_token_hash = p_digest;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_revoke_session_family(
            p_family_id UUID, p_reason TEXT
        )
        RETURNS INTEGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            affected INTEGER;
        BEGIN
            UPDATE sessions
               SET revoked_at = now(), revoked_reason = p_reason
             WHERE family_id = p_family_id AND revoked_at IS NULL;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_revoke_session(
            p_digest BYTEA, p_reason TEXT
        )
        RETURNS INTEGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            affected INTEGER;
        BEGIN
            UPDATE sessions
               SET revoked_at = now(), revoked_reason = p_reason
             WHERE refresh_token_hash = p_digest AND revoked_at IS NULL;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END;
        $$;
        """
    )

    # The definer needs a policy to reach the table at all, for the same FORCE
    # reason as `_add_owner_lookup_policy`. Rows are addressed by a 256-bit
    # digest and the functions return no token material.
    op.execute(
        """
        CREATE POLICY rls_sessions__owner_auth ON sessions
            FOR ALL
            TO receptionai_owner
            USING (true)
            WITH CHECK (true)
        """
    )

    for signature in (
        "auth_find_session_by_token(BYTEA)",
        "auth_revoke_session_family(UUID, TEXT)",
        "auth_revoke_session(BYTEA, TEXT)",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO receptionai_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {signature} "
            f"TO receptionai_app, receptionai_auth"
        )


def _reassign_auth_functions() -> None:
    """Move 0001's login functions to ``receptionai_owner``.

    They were created by, and are therefore owned by, ``receptionai_migrator``
    — which after :func:`_fix_table_ownership` owns no tables and matches no
    policy, so a SECURITY DEFINER function running as it reads nothing. The
    definer must be the role the policies above name.
    """
    for signature in (
        "auth_resolve_user_by_email(TEXT)",
        "auth_list_memberships(UUID)",
        "ops_create_monthly_partitions(DATE)",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO receptionai_owner")


def _grant_auth_functions_to_app() -> None:
    """Let the app role execute the login lookups — 01 §11.6, Phase 1 shape.

    Migration 0001 grants ``auth_resolve_user_by_email`` and
    ``auth_list_memberships`` to ``receptionai_auth`` only, on the design that
    the login path connects with a *separate, narrower credential* than the
    rest of the API. That design is right and is worth keeping: the blast
    radius of a compromised auth credential is then "can enumerate password
    hashes", not "can read every tenant".

    Phase 1 does not have that second credential. The local stack provisions
    one login role, ``ConnectionRouter`` builds one engine per shard, and
    ``apps/api`` has one DSN — so the login route runs as ``receptionai_app``
    and gets "permission denied for function auth_resolve_user_by_email".

    Two ways to close that, and this is the less bad one:

    * **Grant the functions to the app role** (chosen). The app role gains the
      ability to look up a password *hash* by email and to list a user's
      memberships. It already has SELECT on ``users``, narrowed by RLS to the
      current tenant's members — so what it actually gains is the pre-tenant
      lookup, which is real but small, and bounded by the functions' returns.
    * **Add a second engine and DSN for auth.** Correct, and what production
      should do — but it is a change to ``ConnectionRouter``, to settings, to
      the compose file, and to every deployment, for a property Phase 1 has no
      way to test.

    So: grant now, and treat splitting the credential as the deployment task it
    is. Recorded here rather than left as a surprise, because the grant is a
    real widening of the app role and someone hardening this later needs to
    know it was deliberate.
    """
    for signature in (
        "auth_resolve_user_by_email(TEXT)",
        "auth_list_memberships(UUID)",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO receptionai_app")


def upgrade() -> None:
    _fix_table_ownership()
    _add_owner_lookup_policy()
    _reassign_auth_functions()
    _grant_auth_functions_to_app()
    _add_refresh_functions()

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public_resolve_tenant_by_slug(p_slug TEXT)
        RETURNS TABLE (
            id UUID,
            slug TEXT,
            display_name TEXT,
            status tenant_status,
            default_timezone TEXT,
            default_locale TEXT
        )
        -- plpgsql, not sql. A LANGUAGE sql body is parsed AND
        -- permission-checked at CREATE time, as the migrator — which is
        -- subject to `tenants`' FORCE'd policy and matches none of its named
        -- roles, so the CREATE itself fails with "query would be affected by
        -- row-level security policy". plpgsql defers the query to execution,
        -- where the definer's own policy applies. Every SECURITY DEFINER
        -- function in migration 0001 that touches an RLS table is plpgsql for
        -- the same reason.
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        -- Mandatory on every SECURITY DEFINER function: without it a caller
        -- can prepend a schema containing a malicious `tenants` of their own
        -- and hijack the definer's privileges.
        SET search_path = public, pg_temp
        -- Runs as `receptionai_owner`, which the policy added below admits to
        -- `tenants` for SELECT. See `_add_owner_lookup_policy` for why that is
        -- a policy rather than `row_security = off`.
        AS $$
        BEGIN
            RETURN QUERY
            SELECT t.id, t.slug, t.display_name, t.status,
                   t.default_timezone, t.default_locale
              FROM tenants t
             WHERE t.slug = lower(btrim(p_slug))
               AND t.deleted_at IS NULL;
        END;
        $$;
        """
    )
    # Owned by receptionai_owner, matching every other SECURITY DEFINER
    # function in the schema (01 §11.4). The migrator creates it and hands it
    # over, so the definer is the role the rest of the object graph belongs to
    # rather than the deploy identity — which should own nothing at runtime.
    op.execute(
        "ALTER FUNCTION public_resolve_tenant_by_slug(TEXT) "
        "OWNER TO receptionai_owner"
    )

    # Default-deny, then grant deliberately — the same discipline 01 §16.1
    # applies to every function: a table is granted by default privileges, a
    # function never is, because a function is a privilege-escalation surface.
    op.execute(
        "REVOKE ALL ON FUNCTION public_resolve_tenant_by_slug(TEXT) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public_resolve_tenant_by_slug(TEXT) "
        "TO receptionai_app, receptionai_auth"
    )

    op.execute(
        "COMMENT ON FUNCTION public_resolve_tenant_by_slug(TEXT) IS "
        "'Resolves a public tenant slug to its public identity. Returns no "
        "secret, no shard_key, no compliance metadata — see migration 0002. "
        "Used by the public demo widget and any route that starts from a slug "
        "rather than a credential.'"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public_resolve_tenant_by_slug(TEXT)")
    op.execute("DROP POLICY IF EXISTS rls_tenants__owner_lookup ON tenants")
    op.execute("DROP POLICY IF EXISTS rls_users__owner_auth_lookup ON users")
    op.execute(
        "DROP POLICY IF EXISTS rls_user_tenant_roles__owner_auth_lookup "
        "ON user_tenant_roles"
    )
    op.execute("DROP POLICY IF EXISTS rls_roles__owner_auth_lookup ON roles")
    op.execute("DROP POLICY IF EXISTS rls_sessions__owner_auth ON sessions")
    op.execute("DROP FUNCTION IF EXISTS auth_find_session_by_token(BYTEA)")
    op.execute("DROP FUNCTION IF EXISTS auth_revoke_session_family(UUID, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS auth_revoke_session(BYTEA, TEXT)")
    for signature in (
        "auth_resolve_user_by_email(TEXT)",
        "auth_list_memberships(UUID)",
    ):
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {signature} FROM receptionai_app"
        )
    # Table ownership is deliberately NOT reverted: 0001's privilege model
    # always intended `receptionai_owner` to own them (see
    # `_fix_table_ownership`), and handing them back to the deploy identity
    # would re-break the default privileges rather than restore anything.
