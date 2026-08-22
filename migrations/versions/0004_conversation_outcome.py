"""``conversations.outcome`` — how a conversation went, stored rather than guessed.

Contract §7 makes ``Outcome`` part of the conversation vocabulary, and the graph
has been producing the answer all along: a terminal agent declares an ``outcome``
in its manifest (``wrap_up`` → ``resolved``), the compiler's overflow terminal
declares ``transferred``, and ``GraphRunner`` reads it off graph state into
``TurnResult.outcome`` on every turn. It was simply never written down. The API
compensated by *deriving* an outcome from ``status`` and ``escalated`` — a
derivation that cannot distinguish ``booked`` from ``rescheduled``, and which
returned ``answered`` (a word the database does not know) for everything that
merely ended.

**Why a column on ``conversations`` and not 01 §6.5's ``conversation_outcomes``
table.** §6.5 exists for a richer thing than this: several outcomes per
conversation, an async post-call classifier, sentiment, CSAT, a reviewing user,
a subtype from the vertical's vocabulary. None of that is produced yet, and a
table whose only ever-populated column is ``kind`` is a join the list view pays
for on every page in exchange for nothing. This column is the *runtime* answer —
one value, written by the agent that ended the conversation — and it is
deliberately narrow enough that §6.5 landing later can populate itself from it
rather than contradict it.

**Why ``outcome_kind`` rather than a new type.** 0001 already created
``outcome_kind`` (01 §1.7) with all eight labels, precisely for this vocabulary,
and it is otherwise unused — no table references it. Minting a second, narrower
enum for the same concept would leave the database with two spellings of
"booked" and would guarantee a cast at the seam where §6.5 arrives. The column
uses the existing type and a CHECK narrows it to the four values the runtime can
actually produce, which is 01 §1.7's own stated pattern (enum for the platform's
closed vocabulary; a CHECK where the *writable* subset is narrower than the
type). The CHECK is the part that fails loudly if someone later writes ``spam``
from a classifier without deciding what the list view should do with it.

**Nullable, and that is the honest representation.** An in-progress conversation
has no outcome yet. So does one that predates this column and cannot be
classified after the fact. Both are genuinely unknown, and ``NULL`` says so —
whereas defaulting to ``'abandoned'`` would put a definite-looking word on 3,200
rows nobody ever looked at.

**The registry needs no change; RLS does.** ``conversations`` is already in
0001's ``TENANT_OWNED_TABLES`` (01 §11.3) with its ``rls_conversations__all``
policy, and a policy covers the *row*, not the column list — a column added to a
table under FORCE RLS is protected the moment it exists.
``tests/rls/test_rls_coverage.py`` re-derives the registry from ``pg_class`` and
would fail if that were not so. Grants are likewise table-level and already held
by ``receptionai_app``.

What this migration *does* add is a narrow ``receptionai_owner`` policy, because
two things here legitimately cross tenants — the backfill below, and the idle
sweeper — and under FORCE RLS neither can see a single row without one. That
matters more than it sounds: see :func:`_create_owner_policy`, where a backfill
written the obvious way updates nothing and reports ``UPDATE 0``.

**Closing conversations.** The runtime has three endings and this migration
supplies the one that has no event behind it. A terminal agent speaking is an
unambiguous end and a WebRTC session closing is another; both are closed in
application code. A web chat, though, simply stops — nobody notifies anyone when
a tab closes — so ``ops_close_idle_conversations`` treats 30 minutes without a
turn as over. Thirty because that is the conversation session TTL: past it the
visitor's credential has lapsed and they *cannot* continue, so the sweep records
a decision the system has already made rather than making one.

**The backfill is deliberately incomplete**, and the incompleteness is the
point. Two rules can be applied honestly to history, and both are records rather
than inferences:

* An escalated conversation was transferred — that is what ``escalated`` means.
* A conversation named by a booking's ``conversation_id`` produced that booking.

Everything else is a guess. A row could be classified by reading its turns for
"see you Tuesday", and it would be right often enough to look convincing and
wrong often enough to poison every number computed from it. Those rows keep
``NULL``, and the UI says "unknown" rather than inventing a plausible answer.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


#: The subset of ``outcome_kind`` the runtime can produce, and therefore the
#: subset this column accepts. Kept in one place because it is asserted three
#: times below (column CHECK, backfill, comment) and in
#: ``packages.persistence.models.enums.ConversationOutcome``.
_WRITABLE = ("resolved", "booked", "transferred", "abandoned")


def upgrade() -> None:
    _add_column()
    _create_owner_policy()
    _create_sweeper()
    _backfill()


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ops_close_idle_conversations(INTEGER, INTEGER)")
    # Belt and braces: upgrade() drops these itself, but a migration that failed
    # part-way through leaves them behind, and a downgrade is exactly when
    # someone is cleaning that up.
    op.execute("DROP FUNCTION IF EXISTS pg_temp_backfill_conversation_outcome()")
    op.execute(
        "DROP POLICY IF EXISTS rls_bookings__outcome_backfill ON bookings"
    )
    op.execute(
        "DROP POLICY IF EXISTS rls_conversations__owner_sweeper ON conversations"
    )
    op.execute("DROP POLICY IF EXISTS rls_turns__owner_sweeper ON turns")
    # A partitioned parent drops the column from every partition with it.
    op.execute(
        "ALTER TABLE conversations "
        "DROP CONSTRAINT IF EXISTS ck_conversations__outcome_writable"
    )
    op.execute("DROP INDEX IF EXISTS ix_conversations__tenant_outcome")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS outcome")


# ── The column ───────────────────────────────────────────────────────────────


def _add_column() -> None:
    """Add ``outcome``, its CHECK, and the index the list view filters on.

    ``ADD COLUMN`` with no default and no ``NOT NULL`` is metadata-only on
    PostgreSQL 11+ — it does not rewrite the table, which matters here because
    ``conversations`` is partitioned and a rewrite would take an ACCESS
    EXCLUSIVE lock on every partition at once. Adding it to the *parent*
    propagates to existing and future partitions.
    """
    op.execute("ALTER TABLE conversations ADD COLUMN outcome outcome_kind")

    # NOT VALID then VALIDATE: the ADD is a catalogue change taking a brief
    # lock, and the scan that proves existing rows conform runs afterwards
    # under a weaker lock. Every existing row is NULL and trivially passes, but
    # doing it in the order that stays online is the habit worth keeping on a
    # table this size.
    op.execute(
        f"""
        ALTER TABLE conversations
            ADD CONSTRAINT ck_conversations__outcome_writable
            CHECK (outcome IS NULL OR outcome::TEXT IN
                   ({", ".join(repr(v) for v in _WRITABLE)}))
            NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE conversations "
        "VALIDATE CONSTRAINT ck_conversations__outcome_writable"
    )

    # "This tenant's booked conversations, newest first" — the list view's
    # outcome filter. Partial on NOT NULL because the null rows are the ones
    # nobody filters *for*: an index entry per unclassified row would be most
    # of the table for no query.
    op.execute(
        "CREATE INDEX ix_conversations__tenant_outcome "
        "ON conversations (tenant_id, outcome, started_at DESC) "
        "WHERE outcome IS NOT NULL"
    )

    op.execute(
        "COMMENT ON COLUMN conversations.outcome IS "
        "'How the conversation went, as declared by the terminal agent that "
        "ended it (contract §7). NULL means not yet known: still running, or a "
        "row predating this column that could not be classified honestly. "
        "Narrowed by ck_conversations__outcome_writable to the four values the "
        "runtime produces; the outcome_kind type carries four more that only a "
        "post-hoc classifier would write.'"
    )


# ── Cross-tenant reach, the way 01 §11.4 permits it ──────────────────────────


def _create_owner_policy() -> None:
    """Let ``receptionai_owner`` see every tenant's conversations.

    **This is not optional plumbing, it is the bug that ate the first draft of
    this migration.** ``conversations`` is ``FORCE ROW LEVEL SECURITY`` and its
    only policy predicates on ``tenant_id = app_current_tenant()``. A migration
    runs as ``receptionai_migrator``, which has ``rolbypassrls = false`` and no
    tenant GUC set — so ``app_current_tenant()`` returns NULL, ``tenant_id =
    NULL`` is never true, and the migrator sees **zero rows of 3,249**.

    A backfill written the obvious way therefore reports success and updates
    nothing. It does not error, it does not warn; ``UPDATE 0`` looks exactly
    like "there was nothing to do". That is precisely the failure mode this
    project's fail-closed RLS design produces on the write path, and it is worth
    the paragraph because the next data migration will hit it too.

    01 §11.4 is explicit that legitimate cross-tenant access goes through an
    explicitly-granted ``SECURITY DEFINER`` function owned by the NOLOGIN owner
    role — never through ``BYPASSRLS`` on a login role. This policy is what lets
    such a function reach the table at all, and it is the same one 0003 already
    creates on ``booking_holds`` for ``ops_expire_stale_holds``.

    ``receptionai_owner`` cannot log in, so the reachable surface is exactly the
    definer functions granted below, not "the owner role can read everything".

    **Two tables, and the second one is not obvious.** The sweeper decides when a
    conversation went quiet by reading its newest turn, so it needs ``turns`` as
    well — and ``turns`` is FORCE RLS with no owner policy either. Without it the
    ``max(created_at)`` subquery returns NULL for every conversation, the
    ``COALESCE`` falls back to ``started_at``, and every swept row gets
    ``duration_ms = 0``. Nothing errors. The list view fills with conversations
    that apparently lasted no time at all, and the median-length tile reads
    "0s" instead of "—", which is a worse lie than the "—" it replaced.

    The ``turns`` policy is ``FOR SELECT`` only: the sweeper reads turns to date
    a conversation and has no business writing them.
    """
    op.execute(
        """
        CREATE POLICY rls_conversations__owner_sweeper ON conversations
            FOR ALL
            TO receptionai_owner
            USING (true)
            WITH CHECK (true)
        """
    )
    op.execute(
        """
        CREATE POLICY rls_turns__owner_sweeper ON turns
            FOR SELECT
            TO receptionai_owner
            USING (true)
        """
    )


def _create_sweeper() -> None:
    """``ops_close_idle_conversations`` — the honest end of a web chat.

    **Why a sweep is needed at all.** A voice call ends when the WebRTC session
    closes and a graph run ends when a terminal agent speaks, but a web chat
    just *stops*: the visitor closes the tab, and nothing anywhere is notified.
    Without a sweep those conversations stay ``active`` forever, which is why
    every row on ``/conversations`` reads "In progress" and the median-length
    tile renders "—".

    **Why 30 minutes and not a number someone liked.** It is
    ``DEFAULT_SESSION_TTL_SECONDS`` from ``apps/api/tickets.py``, the
    conversation session TTL, and the choice is not cosmetic. That credential is
    what authorises posting into a conversation, it is renewed on every use, and
    when it lapses the visitor *cannot continue this conversation* — a new one
    would have to be started. So at 30 idle minutes the conversation is over as
    a matter of mechanism, not of judgement, and closing the row merely records
    something the system has already decided.

    The 5-minute ``_IDLE_STREAM_TIMEOUT_S`` was the other candidate and is
    wrong for this: it closes an idle *SSE stream*, not the conversation. A
    visitor who reads a reply, thinks for six minutes and types again is
    completely ordinary, and would find their conversation had been declared
    over underneath them.

    **Idempotent**, by predicate rather than by bookkeeping: it only touches
    rows that are still ``active``, and the same row cannot be closed twice
    because the first close makes it fail the ``WHERE``. Re-running it
    immediately is a no-op that returns 0.

    **Tenant-scoped**, in the sense that matters: ``p_tenant_id`` narrows it to
    one tenant when given, and every row it writes keeps its own ``tenant_id``.
    It never moves a value between tenants and never reads one tenant's row to
    decide another's. Cross-tenant *reach* is the point of an ops sweeper — the
    alternative is a caller that must already know every tenant id — and it is
    granted to ``receptionai_ops`` only, never to the app role, so a
    cross-tenant write can never land on a request path.

    **``status``, not just ``ended_at``.** A conversation nobody came back to is
    ``abandoned`` when the visitor left mid-exchange and ``ended`` when the
    agent had already declared an outcome. Both satisfy
    ``ck_conversations__ended_coherent``; the distinction is what stops a
    completed booking being counted as a walkout.

    ``duration_ms`` is computed to the last turn, not to ``now()``. The
    conversation ended when the visitor stopped talking, not when this job
    happened to notice — timing it to the sweep would make every abandoned chat
    look 30 minutes long and would make the median a measure of the cron
    schedule.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ops_close_idle_conversations(
            p_idle_minutes INTEGER DEFAULT 30,
            p_limit        INTEGER DEFAULT 5000
        )
        RETURNS INTEGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            affected INTEGER;
        BEGIN
            WITH idle AS (
                SELECT c.tenant_id,
                       c.created_at,
                       c.id,
                       -- The last thing that happened: the newest turn, or the
                       -- conversation's own start when it never got one.
                       GREATEST(
                           c.started_at,
                           COALESCE(
                               (SELECT max(t.created_at)
                                  FROM turns t
                                 WHERE t.tenant_id = c.tenant_id
                                   AND t.conversation_id = c.id
                                   -- Partition pruning: a turn cannot predate
                                   -- its conversation, and the day of slack
                                   -- matches _PARTITION_SKEW in the repository.
                                   AND t.created_at >= c.created_at
                                                       - INTERVAL '1 day'),
                               c.started_at)
                       ) AS last_activity_at
                  FROM conversations c
                 WHERE c.status = 'active'
                 ORDER BY c.started_at
                 LIMIT p_limit
            ),
            stale AS (
                SELECT * FROM idle
                 WHERE last_activity_at
                       <= now() - make_interval(mins => p_idle_minutes)
            )
            UPDATE conversations c
               SET status = CASE
                       -- The agent reached a conclusion; nobody just walked
                       -- out. `ended` is the truthful status even though the
                       -- visitor never said goodbye.
                       WHEN c.outcome IS NOT NULL THEN 'ended'
                       ELSE 'abandoned'
                   END::conversation_status,
                   ended_at    = s.last_activity_at,
                   duration_ms = GREATEST(
                       0,
                       (EXTRACT(EPOCH FROM (s.last_activity_at - c.started_at))
                        * 1000)::BIGINT
                   )::INTEGER,
                   -- An abandoned conversation with no declared outcome IS the
                   -- abandoned outcome. One that reached a terminal agent keeps
                   -- what that agent declared.
                   outcome     = COALESCE(c.outcome, 'abandoned'),
                   updated_at  = now()
              FROM stale s
             WHERE c.tenant_id  = s.tenant_id
               AND c.created_at = s.created_at
               AND c.id         = s.id
               -- Re-checked inside the UPDATE, so two sweepers racing cannot
               -- both close the same row. Makes the function idempotent by
               -- predicate rather than by a bookkeeping column.
               AND c.status     = 'active';

            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END;
        $fn$;
        """
    )

    op.execute(
        "ALTER FUNCTION ops_close_idle_conversations(INTEGER, INTEGER) "
        "OWNER TO receptionai_owner"
    )
    # Default-deny, then grant deliberately: a table is covered by default
    # privileges, a function never is, because a function is a
    # privilege-escalation surface.
    op.execute(
        "REVOKE ALL ON FUNCTION ops_close_idle_conversations(INTEGER, INTEGER) "
        "FROM PUBLIC"
    )
    # An ops job, not an application call — exactly as 0003 reasons about
    # `ops_expire_stale_holds`. Granting it to the app role would put a
    # cross-tenant write on the request path.
    op.execute(
        "GRANT EXECUTE ON FUNCTION ops_close_idle_conversations(INTEGER, INTEGER) "
        "TO receptionai_ops"
    )
    op.execute(
        "COMMENT ON FUNCTION ops_close_idle_conversations(INTEGER, INTEGER) IS "
        "'Close conversations idle longer than p_idle_minutes (default 30, the "
        "conversation session TTL from apps/api/tickets.py — past it the "
        "visitor''s credential has lapsed and they cannot continue). Idempotent: "
        "only touches status = ''active''. ended_at is the last turn, not now(), "
        "so an abandoned chat is not recorded as p_idle_minutes long.'"
    )


# ── Backfill ─────────────────────────────────────────────────────────────────


def _backfill() -> None:
    """Classify what history can be classified honestly, and nothing else.

    **Why this is a temporary ``SECURITY DEFINER`` function and not a ``DO``
    block.** This is the second half of the trap :func:`_create_owner_policy`
    describes, and it caught the first two drafts of this migration. Adding the
    owner policy is necessary but *not sufficient*: a bare ``DO $$ ... $$``
    block executes with the privileges and the RLS identity of the **invoking**
    role, which for a migration is ``receptionai_migrator``. The owner policy
    does not apply to it, ``app_current_tenant()`` is still NULL, and the
    ``UPDATE`` still matches zero rows — reporting ``UPDATE 0``, which is
    indistinguishable from "there was nothing to backfill".

    ``SECURITY DEFINER`` is a property of a **function**, so the backfill has to
    be one. It is created, called once, and dropped in the same migration: it is
    a one-shot data fix, not an ops capability, and leaving a cross-tenant
    rewriter of ``conversations`` installed on the database afterwards would be
    a standing privilege nobody asked for.

    **And a third turn of the same screw**, which cost the most time of the
    three because it fails *partially*. Running as the owner, the function can
    now see and write ``conversations`` — but the ``booked`` rule reads
    ``bookings``, which is also ``FORCE ROW LEVEL SECURITY`` and has no policy
    naming ``receptionai_owner``. So the ``EXISTS`` subquery returns nothing,
    every conversation looks like it produced no booking, and the ``UPDATE``
    reports 0 while the ``SELECT count(*)`` in the same function cheerfully
    reports 3,249. Visible rows and a silently empty join look identical from
    the outside.

    The fix is a ``SELECT``-only owner policy on ``bookings``, created and
    dropped around the call. It is deliberately not permanent and deliberately
    not ``FOR ALL``: this backfill needs to *read* bookings to classify
    conversations, and nothing about that requires the ability to write them.

    Two rules, both of which are records rather than inferences:

    * ``escalated`` is set by the code path that hands a conversation to a
      person. A row carrying it *was* transferred.
    * ``bookings.conversation_id`` is the booking's own record of the
      conversation that produced it. A conversation it names *did* book.

    **A time-window join was considered and rejected.** "This tenant created a
    booking while this conversation was open" is true of every conversation open
    during a busy hour, including the ones that discussed nothing but opening
    times. It would be right often enough to look convincing and wrong often
    enough to poison every number computed from it. Rows that neither rule
    reaches keep ``NULL``, and the dashboard says "unknown".

    Nothing here reads turn content. A conversation whose transcript contains
    "see you Tuesday" may or may not have booked anything, and the transcript
    cannot tell the difference between an appointment made and one merely
    discussed.
    """
    # Read-only, and only for as long as the backfill runs. `bookings` is FORCE
    # RLS with no owner policy, so without this the `booked` rule's EXISTS
    # subquery silently matches nothing — see the docstring.
    op.execute(
        """
        CREATE POLICY rls_bookings__outcome_backfill ON bookings
            FOR SELECT
            TO receptionai_owner
            USING (true)
        """
    )
    op.execute(
        """
        CREATE FUNCTION pg_temp_backfill_conversation_outcome()
        RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            escalated_rows INTEGER;
            booked_rows    INTEGER;
        BEGIN
            UPDATE conversations
               SET outcome = 'transferred'
             WHERE escalated
               AND outcome IS NULL;
            GET DIAGNOSTICS escalated_rows = ROW_COUNT;

            UPDATE conversations c
               SET outcome = 'booked'
             WHERE c.outcome IS NULL
               AND EXISTS (
                   SELECT 1
                     FROM bookings b
                    WHERE b.tenant_id       = c.tenant_id
                      AND b.conversation_id = c.id
               );
            GET DIAGNOSTICS booked_rows = ROW_COUNT;

            -- Reported rather than returned, so the counts land in the
            -- migration's own output. A silent backfill is how the RLS trap
            -- above stayed invisible through two drafts.
            RAISE NOTICE
                'conversations.outcome backfill: % transferred, % booked, '
                '% left unknown (honestly unclassifiable)',
                escalated_rows, booked_rows,
                (SELECT count(*) FROM conversations WHERE outcome IS NULL);
        END;
        $fn$;
        """
    )
    # The function must be OWNED by the role the owner policy names, or
    # SECURITY DEFINER runs it as the migrator and we are back where we started.
    op.execute(
        "ALTER FUNCTION pg_temp_backfill_conversation_outcome() "
        "OWNER TO receptionai_owner"
    )

    op.execute("SELECT pg_temp_backfill_conversation_outcome()")

    # One-shot, both of them. A data fix, not a capability to leave lying
    # around: after this migration the owner can no longer read `bookings` and
    # the cross-tenant rewriter of `conversations` does not exist.
    op.execute("DROP FUNCTION pg_temp_backfill_conversation_outcome()")
    op.execute("DROP POLICY rls_bookings__outcome_backfill ON bookings")
