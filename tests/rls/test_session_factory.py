"""Isolation through the real production code path.

``test_tenant_isolation.py`` proves the *database* enforces isolation, using raw
connections. This file proves the code that opens sessions in production —
:class:`~packages.persistence.session.SessionFactory`, the
:class:`~packages.persistence.engine.ConnectionRouter`, and the repositories —
actually uses that enforcement correctly.

The distinction matters. A perfectly configured RLS policy protects nothing if
the factory sets ``app.tenant_id`` with a plain ``SET`` instead of a local one,
or opens the session before setting it, or resolves the wrong engine. Those are
Python bugs, and only a Python-level test finds them.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from packages.core.errors import CrossTenantAccessError
from packages.core.ids import uuid7
from packages.core.ports.connection_router import UnresolvableShardError
from packages.core.tenancy import TenantContext
from packages.persistence.engine import ConnectionRouter, ShardConfig
from packages.persistence.models.conversations import Conversation
from packages.persistence.models.enums import ConversationChannel, TurnRole
from packages.persistence.repositories import ConversationRepository, TurnRepository
from packages.persistence.session import SessionFactory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.rls, pytest.mark.integration]


@pytest.fixture
def factory(app_dsn: str) -> SessionFactory:
    """A factory bound to the app role — not the owner, no BYPASSRLS.

    ``pool_size=1, max_overflow=0`` is load-bearing, not tidiness: it forces
    every unit of work onto **one physical connection**. Context leakage between
    tenants is a property of *reused* connections, so a pool large enough to
    hand out a fresh one per test would make the leak untestable — the very
    thing ``SET LOCAL`` exists to prevent would go unobserved. See
    ``test_context_does_not_leak_between_units_of_work``.
    """
    router = ConnectionRouter(
        {
            "__default__": ShardConfig(
                key="__default__", dsn=app_dsn, pool_size=1, max_overflow=0
            )
        }
    )
    return SessionFactory(router)


async def test_unit_of_work_scopes_reads_to_its_tenant(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """Two units of work over the same pool see disjoint data.

    The pool is shared, so if ``set_config``'s ``is_local`` flag were false, the
    second unit of work would inherit the first's ``app.tenant_id`` from the
    recycled connection and read A's rows under B's context — with no error
    anywhere, because the policy would be doing exactly what it was told.
    """
    tenant_a, tenant_b = two_tenants

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        a_rows = await ConversationRepository(session).count()
        assert a_rows == 1

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_b)) as session:
        b_conversations = await ConversationRepository(session).recent()
        assert all(c.tenant_id == tenant_b for c in b_conversations)
        assert len(b_conversations) == 1

    # And back to A, to prove B's context did not stick either.
    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        bound = (
            await session.execute(text("SELECT app_current_tenant()"))
        ).scalar_one()
        assert bound == tenant_a


async def test_context_does_not_leak_between_units_of_work(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """THE multi-tenancy bug, closed — asserted on one pooled connection.

    The ``factory`` fixture pins the pool to a single connection, so the unit of
    work below genuinely reuses the physical connection the previous one
    returned. That is the only configuration in which this proves anything.

    If ``set_config``'s third argument (``is_local``) were ``false``, the GUC
    would be *session*-scoped rather than transaction-scoped: it would survive
    the COMMIT, ride the connection back into the pool, and the next unit of
    work — which happens to be for a different tenant — would open against a
    connection already carrying tenant A's id. RLS would then do exactly what it
    was told and hand B's session A's rows, with no error anywhere.

    Three things are asserted, in increasing order of what they would catch:

    1. Between units of work, on the same connection, the GUC is gone.
    2. A unit of work for B is bound to B, not to whatever ran before it.
    3. B reads B's data. This is the one that fails loudly if the flag flips.
    """
    tenant_a, tenant_b = two_tenants

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        assert (
            await session.execute(text("SELECT app_current_tenant()"))
        ).scalar_one() == tenant_a

    # Borrow the same pooled connection outside any unit of work and look at the
    # GUC directly. `SET LOCAL` was discarded at COMMIT, so it must be unset.
    engine = await factory._router.default_engine()
    async with engine.connect() as raw:
        residue = (
            await raw.execute(text("SELECT current_setting('app.tenant_id', true)"))
        ).scalar_one()
        assert residue in (None, ""), (
            f"app.tenant_id survived COMMIT on a pooled connection: {residue!r}. "
            f"set_config(..., is_local => true) is what makes the GUC "
            f"transaction-scoped; without it, tenant A's context leaks into "
            f"tenant B's next request and RLS obediently serves A's data."
        )

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_b)) as session:
        bound = (
            await session.execute(text("SELECT app_current_tenant()"))
        ).scalar_one()
        assert bound == tenant_b

        rows = (
            await session.execute(text("SELECT tenant_id FROM conversations"))
        ).scalars().all()
        assert rows == [tenant_b], (
            f"tenant B's unit of work saw {rows}; expected only its own rows"
        )


async def test_session_exposes_the_a10_published_interface(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """Amendment A10's five accessors are present and correct.

    ``05``'s audit writer reads these, so they are a cross-document contract,
    not internal convenience. The impersonation case is the one worth asserting:
    01 names the *actor* and 05 names the *subject*, and an audit row that
    recorded only one of them would make an impersonated action indistinguishable
    from the real user's.
    """
    tenant_a, _ = two_tenants
    actor = uuid7()
    subject = uuid7()

    ctx = TenantContext(
        tenant_id=tenant_a,
        user_id=subject,
        impersonator_user_id=actor,
        shard_key=None,
    )
    async with factory.unit_of_work(ctx) as session:
        assert session.tenant_id == tenant_a
        assert session.user_id == subject
        assert session.impersonator_user_id == actor
        assert session.shard_key is None
        assert session.is_impersonated is True

        # app.user_id is set too, which is what drives the self-row policies on
        # `users` and `sessions` (01 §11.5).
        bound_user = (
            await session.execute(text("SELECT current_setting('app.user_id', true)"))
        ).scalar_one()
        assert bound_user == str(subject)

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        assert session.is_impersonated is False
        assert session.impersonator_user_id is None


async def test_flush_guard_rejects_a_cross_tenant_write(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """The ``before_flush`` guard fires before the database has to.

    RLS's ``WITH CHECK`` would reject this anyway, but as an opaque
    ``IntegrityError`` deep inside a flush. Catching it in Python gives a
    precise failure naming the offending object — and, importantly, proves the
    guard is wired to the *synchronous* session where ORM unit-of-work events
    actually fire. Registered on the async facade (as 01 §15.3 shows), it never
    runs at all, and this test would fail with a database error instead.
    """
    tenant_a, tenant_b = two_tenants

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        rogue = Conversation(
            id=uuid7(), tenant_id=tenant_b, channel=ConversationChannel.WEB_CHAT
        )
        session.add(rogue)
        with pytest.raises(CrossTenantAccessError) as exc_info:
            await session.flush()
        assert "Conversation" in str(exc_info.value)
        assert str(tenant_b) in str(exc_info.value)
        session.expunge(rogue)


async def test_repository_stamps_tenant_id_rather_than_trusting_it(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """``add()`` overwrites whatever the caller put in ``tenant_id``.

    Defence in depth in the cheapest possible form: the caller cannot write
    another tenant's id even by accident, so the flush guard and RLS never have
    to be the thing that catches an ordinary mistake.
    """
    tenant_a, tenant_b = two_tenants

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        repo = ConversationRepository(session)
        misattributed = Conversation(
            id=uuid7(), tenant_id=tenant_b, channel=ConversationChannel.SMS
        )
        stored = await repo.add(misattributed)
        assert stored.tenant_id == tenant_a, (
            "add() must stamp the session's tenant, not trust the object's"
        )


async def test_partitioned_point_lookup_and_turn_rollup(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """The UUIDv7 partition window works, and the aggregates stay consistent.

    This is contract A2's payoff made concrete: ``get()`` on a partitioned table
    derives its ``created_at`` window from the id's embedded timestamp, so a
    caller never carries a timestamp alongside an id. With a v4 id the window
    would land somewhere in the next few thousand years and prune to zero
    partitions — the row would exist and the lookup would return None.
    """
    tenant_a, _ = two_tenants

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        conversations = ConversationRepository(session)
        turns = TurnRepository(session)

        conversation = await conversations.start(
            channel=ConversationChannel.VOICE_INBOUND,
            external_ref="CA-test-0001",
            contact_identifier="+15551234567",
        )
        await turns.append(
            conversation=conversation,
            role=TurnRole.USER,
            content="I would like to book an appointment",
            input_tokens=9,
        )
        await turns.append(
            conversation=conversation,
            role=TurnRole.ASSISTANT,
            content="Of course — what day suits you?",
            output_tokens=8,
            model_provider="anthropic",
            model_name="claude-test",
        )

    async with factory.unit_of_work(TenantContext(tenant_id=tenant_a)) as session:
        conversations = ConversationRepository(session)
        fetched = await conversations.get(conversation.id)
        assert fetched is not None, (
            "partitioned point lookup returned nothing; the created_at window "
            "derived from the UUIDv7 did not match the row's partition"
        )
        assert fetched.turn_count == 2
        assert fetched.total_input_tokens == 9
        assert fetched.total_output_tokens == 8

        replayed = await TurnRepository(session).replay(conversation.id)
        assert [t.sequence for t in replayed] == [0, 1], (
            "turns must replay in explicit sequence order"
        )

        by_ref = await conversations.by_external_ref(
            channel=ConversationChannel.VOICE_INBOUND, external_ref="CA-test-0001"
        )
        assert by_ref is not None and by_ref.id == conversation.id


async def test_ops_unit_of_work_sees_nothing(
    factory: SessionFactory, two_tenants: tuple[UUID, UUID]
) -> None:
    """A cross-tenant maintenance session reads zero rows by design.

    It deliberately does not set ``app.tenant_id``, so every RLS-protected table
    returns nothing. The only useful thing it can do is call the explicitly
    granted ``SECURITY DEFINER`` ``ops_*`` functions. Someone who writes a bare
    SELECT here expecting rows gets none — exactly the feedback we want.
    """
    async with factory.ops_unit_of_work() as session:
        for table in ("conversations", "locations", "tenants"):
            count = (
                await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            assert count == 0, f"ops session saw {count} rows in {table}"


async def test_router_fails_closed_on_an_unknown_shard(app_dsn: str) -> None:
    """Amendment A8, at the seam it protects.

    ``shard_key`` is non-null only for tenants relocated to dedicated
    infrastructure *because shared was contractually or legally unacceptable*. A
    fallback would land precisely those tenants on the shared cluster and do it
    invisibly — RLS still scopes correctly, every query succeeds, no test fails.
    It fails *successfully*, and discovery comes at the next audit.

    So: refuse the session. And the NULL case, which is genuinely correct rather
    than a fallback, must still resolve.
    """
    router = ConnectionRouter(
        {"__default__": ShardConfig(key="__default__", dsn=app_dsn)}
    )
    try:
        # NULL shard_key → the default shard. Never assigned, so this is right.
        engine = await router.engine_for(TenantContext(tenant_id=uuid7()))
        assert isinstance(engine, AsyncEngine)

        # A committed tenant whose config has not propagated to this pod.
        ctx = TenantContext(tenant_id=uuid7(), shard_key="eu-hipaa-01")
        with pytest.raises(UnresolvableShardError) as exc_info:
            await router.engine_for(ctx)

        error = exc_info.value
        assert error.shard_key == "eu-hipaa-01"
        assert error.code == "shard_unavailable"
        # 503, not 500: the data is intact and the condition is
        # transient-by-remediation, so retries and status pages behave.
        assert error.http_status == 503
        assert "__default__" in error.known
    finally:
        await router.dispose_all()


async def test_router_requires_a_default_shard(app_dsn: str) -> None:
    """A router with no default shard is a configuration error at construction.

    Caught here rather than at the first request for a NULL-``shard_key``
    tenant — which is to say, at the first request from almost every tenant.
    """
    with pytest.raises(ValueError, match="__default__"):
        ConnectionRouter({"eu-hipaa-01": ShardConfig(key="eu", dsn=app_dsn)})


async def test_readiness_gate_refuses_a_pod_missing_a_shard(app_dsn: str) -> None:
    """The A8 failure surfaces at deploy time, not at request time.

    ``expected`` comes from ``SELECT DISTINCT shard_key FROM tenants WHERE
    shard_key IS NOT NULL`` — the database is the authority on which shards must
    exist, so a relocation that skipped the config rollout fails the *next*
    deploy against a rolling update that can halt, rather than the next request
    against a real tenant.
    """
    from packages.persistence.engine import check_shard_configuration

    router = ConnectionRouter(
        {"__default__": ShardConfig(key="__default__", dsn=app_dsn)}
    )
    try:
        await check_shard_configuration(router, {"__default__"})

        with pytest.raises(RuntimeError, match="eu-hipaa-01"):
            await check_shard_configuration(router, {"__default__", "eu-hipaa-01"})
    finally:
        await router.dispose_all()
