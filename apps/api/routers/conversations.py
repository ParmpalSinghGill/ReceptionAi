"""Conversations — PHASE1-API-CONTRACT's two endpoints, both callers.

    POST /api/v1/conversations/{id}/messages
    GET  /api/v1/conversations/{id}/stream?ticket=<ticket>
    GET  /api/v1/conversations/{id}

The contract is explicit that these serve **both** the admin client and the
public demo widget: *"`ticket` for the demo widget; admin clients authenticate
by cookie and omit it."* So the credential is resolved once, by
:func:`~apps.api.deps.resolve_conversation_access`, and both callers reach the
same handler through the same authorization check.

**Why one handler rather than two.** A route that accepts either credential is
the shape an authorization bug hides in — that is a real risk and the reason
this was originally built as a separate `/public/demo/` surface. But the
contract froze these paths and two frontends are coded against them, so the
answer is to make the shared path *safe* rather than to route around it:

* The credential is resolved in **one function with one exit**, not branched
  inside each handler.
* A ticket never yields a general principal. It yields
  :data:`~apps.api.deps.WIDGET_PERMISSIONS` — write and read, nothing else —
  with ``scope_all_conversations=False``, so it cannot reach `/me`, cannot list
  conversations, and satisfies no step-up route.
* The ticket is **bound to one conversation**. A valid ticket presented against
  a different id is a 403, tested in ``tests/api/test_tenant_isolation.py``.
* ``requires()`` still runs on top for both callers, so
  ``assert_all_routes_guarded`` sees these routes as guarded and the permission
  check is the same code either way.

**The widget's credential lifecycle**, which is subtler than it looks and which
an earlier revision of this file got wrong:

    POST /public/demo/sessions              -> ticket (single-use, 20s)
    GET  /conversations/{id}/stream?ticket= -> REDEEMS the ticket,
                                               mints a conversation session,
                                               emits it as the first event
                                               and sets it as a cookie
    POST /conversations/{id}/messages       -> authenticates with that session
                                               (cookie or X-Conversation-Session)

The ticket is exchanged, not merely spent. That matters because the widget
opens the stream **first** and posts a message later, when the user has
finished typing — so a ticket consumed by opening the stream leaves nothing for
the message, and the demo cannot hold a conversation. The bug survived 148
passing tests because every one of them posted before opening the stream, which
is the natural order for curl and the opposite of the order a browser uses.

The ticket keeps every property it had: single-use, 20 seconds, one
conversation. What changed is that redeeming it now *buys something* rather
than merely opening a socket.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import structlog
from apps.api.deps import (
    SESSION_COOKIE,
    SESSION_HEADER,
    AppStateDep,
    ConversationAccessDep,
    CurrentPrincipal,
    ReadOnlyDbSession,
    requires,
)
from apps.api.problems import ProblemException
from apps.api.schemas import (
    ConversationDetail,
    ConversationSummary,
    MessageAccepted,
    MessageRequest,
    TurnDetail,
    rfc3339,
)
from apps.api.security.permissions import Perm
from apps.api.sse import SSE_HEADERS, SSEEvent, parse_last_event_id, sse_stream
from apps.api.state import AppState
from apps.api.tickets import ConversationSession
from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from packages.ai.runner import TurnEvent
from packages.core.api.pagination import (
    CursorError,
    CursorPage,
    decode_cursor,
    page_from,
)
from packages.core.errors import EntityNotFoundError
from packages.core.tenancy import TenantContext
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationStatus,
    TurnRole,
)
from packages.persistence.repositories.conversations import (
    ConversationRepository,
    ConversationRow,
)

__all__ = ["router"]

log = structlog.get_logger(__name__)

#: How long a stream with no pending turn stays open before closing. Long
#: enough that a person reading the page and then typing is never dropped;
#: short enough that an abandoned tab releases its worker slot. `sse_stream`
#: heartbeats throughout, so proxies do not reap the idle connection.
_IDLE_STREAM_TIMEOUT_S = 300.0

#: Poll interval for the parked message. Redis GETDEL is cheap and this only
#: runs while a stream is genuinely idle; 250ms is imperceptible against the
#: ~2s the model takes to answer.
_IDLE_POLL_INTERVAL_S = 0.25

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _sse_headers() -> dict[str, str]:
    # Content-Type comes from `StreamingResponse(media_type=...)`; passing it
    # here as well produces a duplicate header on some servers.
    return {k: v for k, v in SSE_HEADERS.items() if k.lower() != "content-type"}


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(requires(Perm.CONVERSATIONS_WRITE))],
    summary="Send a user turn (admin token, conversation session, or ticket)",
)
async def post_message(
    conversation_id: UUID,
    body: MessageRequest,
    state: AppStateDep,
    access: ConversationAccessDep,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> MessageAccepted:
    """Accept a turn. Returns immediately; tokens arrive on the stream.

    A widget authenticates here with the **conversation session** it received
    when the stream opened — cookie or ``X-Conversation-Session`` — not with
    the ticket, which by then is spent. A ticket is still accepted for the
    message-first ordering (post, then open the stream), which is what an
    integration test or a curl session naturally does; in that case it is
    peeked, never consumed, so the subsequent stream can still redeem it.

    202 rather than 200 is the honest status: the work is accepted and
    unfinished, and the body carries the ``turn_id`` the client correlates
    events against.
    """
    ctx = access.context
    try:
        turn_id = await state.conversations.record_user_turn(
            ctx=ctx, conversation_id=conversation_id, content=body.content
        )
    except EntityNotFoundError as exc:
        # 404, not 403 — and identical to what a cross-tenant id produces,
        # because RLS made that row invisible to this session.
        raise ProblemException(
            status=404, code="not_found", detail="Conversation not found."
        ) from exc

    try:
        await state.stash_pending(conversation_id, turn_id, body.content)
    except RuntimeError as exc:
        raise ProblemException(
            status=503,
            code="service_unavailable",
            detail="Streaming is unavailable because Redis is unreachable.",
        ) from exc

    if key := (idempotency_key or body.idempotency_key):
        response.headers["Idempotency-Key"] = key

    return MessageAccepted(
        turn_id=turn_id, accepted_at=rfc3339(datetime.now(tz=UTC)) or ""
    )


@router.get(
    "/{conversation_id}/stream",
    dependencies=[Depends(requires(Perm.CONVERSATIONS_READ))],
    summary="SSE token stream (admin token or demo ticket; exchanges the ticket)",
)
async def stream_conversation(
    request: Request,
    conversation_id: UUID,
    state: AppStateDep,
    access: ConversationAccessDep,
    ticket: str | None = Query(default=None, max_length=128),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Stream the assistant's reply, exchanging a ticket for a session.

    **The ticket is redeemed here, atomically and once** — and exchanged, in
    the same step, for a conversation session that outlives this connection.
    That exchange is the fix for a lifetime bug worth stating plainly, because
    it is invisible if you test in the wrong order:

    The widget opens the stream *first* and posts a message later, when the
    user has finished typing. If opening the stream merely spent the ticket,
    there would be no credential left for the message and the demo could not
    hold a conversation. Testing message-first — which is what curl does
    naturally — never exercises that, which is how it shipped.

    So the redeemed ticket becomes a :class:`~apps.api.tickets.ConversationSession`,
    delivered two ways for one reason: an ``HttpOnly`` cookie (JS cannot
    exfiltrate it) *and* the stream's first ``session`` event (a cross-origin
    widget on a customer's site never receives a ``SameSite=Lax`` cookie). The
    widget uses whichever its embedding gives it.

    A reconnect still re-mints: ``EventSource`` replays the original URL,
    ticket included, and the spent ticket 401s — which per the contract means
    *"re-mint"*, not *"auth failed"*. The reconnecting stream may equally
    present the session it already holds, in which case no re-mint is needed at
    all; both paths are tested.
    """
    session = None

    if access.is_widget and ticket:
        claim = await state.tickets.redeem(ticket)
        if claim is None:
            # Either the 20s TTL elapsed between the peek and here, or a
            # reconnect replayed a spent ticket, or another connection won the
            # race. If the caller ALSO holds a valid session for this
            # conversation, `resolve_conversation_access` already accepted it —
            # so reaching this branch with a session in hand is not a failure.
            if not _has_session(request):
                raise ProblemException(
                    status=401,
                    code="invalid_token",
                    detail=(
                        "This stream ticket is expired or has already been "
                        "used. Tickets are single-use: request a new one from "
                        "POST /api/v1/public/demo/sessions."
                    ),
                )
        else:
            if claim.conversation_id != conversation_id:
                raise ProblemException(
                    status=403,
                    code="forbidden",
                    detail="This ticket is not valid for that conversation.",
                )
            session = await state.tickets.open_session(claim)

    # 06 §3.2: usage rides on `turn_complete` only for a staff principal
    # holding `usage:read`. A widget is an anonymous member of the public, and
    # per-turn token counts leak unit economics.
    show_usage = (
        not access.is_widget
        and Perm.USAGE_READ.value in access.principal.permissions
    )

    events = _conversation_events(
        request=request,
        state=state,
        ctx=access.context,
        conversation_id=conversation_id,
        show_usage=show_usage,
        is_widget=access.is_widget,
        session=session,
    )

    response = StreamingResponse(
        sse_stream(
            events,
            heartbeat_seconds=state.settings.sse_heartbeat_seconds,
            idle_timeout_seconds=state.settings.sse_idle_timeout_seconds,
            start_id=parse_last_event_id(last_event_id),
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )

    if session is not None:
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            max_age=session.expires_in,
            # JS cannot read it, so an XSS on the embedding page cannot steal
            # the conversation credential.
            httponly=True,
            # Lax rather than None: the widget's cross-origin case is served by
            # the `session` event instead, and `SameSite=None` would require
            # `Secure` and would send this cookie on every cross-site request
            # to us — a wider surface than the widget needs.
            samesite="lax",
            secure=not state.settings.is_local,
            path="/api/v1/conversations",
        )

    return response


def _has_session(request: Request) -> bool:
    """Whether the caller presented a conversation session credential.

    Used only to decide whether a spent ticket is fatal. The credential itself
    was already validated by ``resolve_conversation_access`` — this is a
    presence check, not a second authorization.
    """
    return bool(
        request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE)
    )


#: Ceiling on the free-text search term. A pattern longer than this is not a
#: search anyone typed, and an unbounded one is an ILIKE scan someone pasted.
_MAX_SEARCH_LEN = 200

#: The sort this list is served in, embedded in every cursor it issues. Newest
#: first, because a receptionist opens this page to see what just happened —
#: not to browse history from the beginning.
_SORT = "started_at:desc"


def _outcome_of(row: ConversationRow) -> str | None:
    """How the conversation went — read, not guessed.

    ``conversations.outcome`` (migration 0004) is now the answer, written by the
    terminal agent that ended the conversation and by the idle sweeper that
    closes the ones nobody said goodbye to. When it is set, it is returned
    verbatim: the graph knew, and second-guessing a recorded value with a
    derivation is how the two drift apart.

    **The derivation below survives only for rows that predate the column.**
    Migration 0004 backfilled what could be established from a record — an
    escalated conversation was transferred, a conversation named by a booking
    was booked — and deliberately left the rest null rather than inferring an
    outcome from turn content. So this fallback exists for those unclassifiable
    historical rows, and it is written to be obviously worse than the stored
    value: it cannot distinguish ``booked`` from ``rescheduled``, and its
    ``answered`` is not a word the database knows.

    It returns ``None`` rather than a guess wherever the old code would have
    invented one, because the UI now distinguishes the three cases the old
    null-means-everything conflation could not: still running, ended with a
    known outcome, and ended with an outcome nobody can honestly reconstruct.
    """
    conversation = row.conversation

    # The recorded answer, whenever there is one.
    if conversation.outcome is not None:
        return str(conversation.outcome)

    # ── Historical rows only, from here down ──────────────────────────────
    if conversation.status == ConversationStatus.ACTIVE:
        # Still running: it has no outcome yet, and inventing one would make a
        # live call look finished.
        return None
    if conversation.escalated:
        return "transferred"
    if conversation.status == ConversationStatus.FAILED:
        return "failed"
    if conversation.status == ConversationStatus.ABANDONED:
        return "abandoned"
    # Ended before the column existed, with nothing recorded that says how. A
    # conversation with turns was at least answered; one without never became a
    # conversation at all. Beyond that this cannot honestly say, and does not
    # try — the UI renders null as "unknown".
    return "answered" if conversation.turn_count > 1 else "abandoned"


def _to_summary(row: ConversationRow, *, unmask: bool) -> ConversationSummary:
    """One repository row as the wire shape.

    ``unmask`` is the viewer asymmetry from 05 §A2.3, applied exactly as
    ``bookings.py`` applies it to ``customer_name``/``customer_phone``: a
    principal without ``conversations:read_pii`` sees that a conversation
    happened, on which channel, how long it ran and how it went — and sees
    ``null`` where the caller's name, number and opening line would be.

    **The masking is here, not in the frontend.** A field the UI declines to
    render has already crossed the wire, is in the browser's memory, and is in
    the response any curl or devtools Network tab can read. Hiding it client-
    side is a suggestion; returning null is a control.
    """
    conversation = row.conversation

    duration_seconds = (
        conversation.duration_ms // 1000 if conversation.duration_ms is not None else None
    )

    return ConversationSummary(
        id=conversation.id,
        channel=str(conversation.channel),
        status=str(conversation.status),
        started_at=rfc3339(conversation.started_at) or "",
        ended_at=rfc3339(conversation.ended_at),
        last_activity_at=rfc3339(row.last_turn_at),
        duration_seconds=duration_seconds,
        turn_count=conversation.turn_count,
        outcome=_outcome_of(row),
        escalated=conversation.escalated,
        # Containment is the inverse of escalation: the assistant "contained"
        # the conversation when no person had to take it over.
        contained=not conversation.escalated,
        agent_code=conversation.agent_code,
        opening_line=row.opening_line if unmask else None,
        contact_identifier=conversation.contact_identifier if unmask else None,
        contact_name=conversation.contact_name if unmask else None,
    )


@router.get(
    "",
    response_model=CursorPage[ConversationSummary],
    dependencies=[Depends(requires(Perm.CONVERSATIONS_READ))],
    summary="Conversations, newest first, cursor-paginated",
)
async def list_conversations(
    principal: CurrentPrincipal,
    session: ReadOnlyDbSession,
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Rows per page. The response carries `next_cursor` for the rest.",
    ),
    cursor: str | None = Query(
        default=None,
        max_length=512,
        description=(
            "Opaque cursor from a previous response's `next_cursor`. Offset "
            "paging is not offered — these tables are partitioned by month."
        ),
    ),
    channel: str | None = Query(
        default=None,
        description="Only this channel, e.g. `web_chat` or `voice_inbound`.",
    ),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Only this status: `active`, `ended`, `failed`, `abandoned`.",
    ),
    from_at: datetime | None = Query(
        default=None,
        alias="from",
        description="Only conversations started at or after this instant (RFC 3339).",
    ),
    to_at: datetime | None = Query(
        default=None,
        alias="to",
        description="Only conversations started strictly before this instant.",
    ),
    q: str | None = Query(
        default=None,
        max_length=_MAX_SEARCH_LEN,
        description=(
            "Free-text search over turn content. A conversation matches when "
            "any of its turns does."
        ),
    ),
) -> CursorPage[ConversationSummary]:
    """The list the portal's `/conversations` page renders.

    **The tenant is not a parameter.** It arrives in the bearer token's ``tid``
    claim, through ``tenant_context`` into ``SET LOCAL app.tenant_id`` (contract
    §10). There is no ``tenant_id`` to pass and none to forget, so a caller
    cannot name another tenant's conversations — the vocabulary does not exist
    on this route. A ``?tenant_id=`` in the query string is inert, which
    ``tests/api/test_conversations_list.py`` asserts rather than assumes.

    **Cursor, not offset.** ``conversations`` is partitioned by month on
    ``created_at``. ``OFFSET 2000`` carries no predicate a partition can be
    pruned by, so a deep page reads the tenant's entire history; and on a table
    being appended to while it is read, an offset window shifts under the reader
    and returns rows twice while skipping others. See
    :meth:`ConversationRepository.page`.

    **Newest first**, non-negotiable for this screen: the question it is opened
    to answer is "what just happened", and a receptionist looking for the call
    they took ninety seconds ago should not have to page to find it.
    """
    decoded = None
    if cursor:
        try:
            decoded = decode_cursor(cursor, expect_sort=_SORT)
        except CursorError as exc:
            # 400 and not 500: a cursor comes from a query string, so it can be
            # truncated by a mail client or replayed from a stale bookmark.
            raise ProblemException(
                status=400,
                code="validation_failed",
                detail=f"Invalid cursor: {exc}",
            ) from exc

    channel_filter = _parse_enum(
        channel, ConversationChannel, field="channel"
    )
    status_value = _parse_enum(
        status_filter, ConversationStatus, field="status"
    )

    if from_at is not None and to_at is not None and to_at < from_at:
        raise ProblemException(
            status=422,
            code="validation_failed",
            detail="`to` falls before `from`.",
        )

    search = q.strip() if q else None

    repo = ConversationRepository(session)
    page = await repo.page(
        limit=limit,
        cursor=decoded,
        channel=channel_filter,
        status=status_value,
        started_from=_as_utc(from_at),
        started_to=_as_utc(to_at),
        search=search or None,
    )

    unmask = Perm.CONVERSATIONS_READ_PII.value in principal.permissions

    # `page_from` trims the sentinel row the repository fetched and turns it
    # into `has_more` + `next_cursor`. The cursor key must be the SAME pair the
    # query ordered by, or the next page silently starts in the wrong place.
    summaries = page_from(
        page.rows,
        limit,
        sort=_SORT,
        key=lambda row: (row.conversation.started_at, row.conversation.id),
    )

    return CursorPage[ConversationSummary](
        items=[_to_summary(row, unmask=unmask) for row in summaries.items],
        next_cursor=summaries.next_cursor,
        has_more=summaries.has_more,
    )


def _parse_enum(value: str | None, enum_type, *, field: str):
    """A filter value as its enum member, or a 422 naming the alternatives.

    FastAPI would give a 422 for an enum-typed parameter on its own, but the
    body would not be an RFC 9457 problem document, and the detail would not
    list what the caller could have said instead.
    """
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ProblemException(
            status=422,
            code="validation_failed",
            detail=f"Unknown `{field}` {value!r}. Expected one of: {allowed}.",
        ) from exc


def _as_utc(value: datetime | None) -> datetime | None:
    """A filter bound in UTC.

    A caller may send ``2026-08-20T00:00:00`` with no offset. Comparing a naive
    datetime against a ``timestamptz`` column raises in the driver, so a missing
    offset is read as UTC rather than being allowed to 500.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    dependencies=[Depends(requires(Perm.CONVERSATIONS_READ))],
    summary="A conversation with its turns",
)
async def get_conversation(
    conversation_id: UUID,
    state: AppStateDep,
    access: ConversationAccessDep,
) -> ConversationDetail:
    """Read a conversation.

    PII masking is by permission, not by role: a principal without
    ``conversations:read_pii`` gets user turns redacted (05 §A2.3, B6.3). A
    widget never holds that permission, so a ticket-holder replaying their own
    conversation sees their own words masked — slightly odd, and the safe
    direction to be wrong in.
    """
    unmask = Perm.CONVERSATIONS_READ_PII.value in access.principal.permissions
    show_usage = (
        not access.is_widget
        and Perm.USAGE_READ.value in access.principal.permissions
    )

    try:
        conversation, turns = await state.conversations.get_with_turns(
            ctx=access.context, conversation_id=conversation_id
        )
    except EntityNotFoundError as exc:
        raise ProblemException(
            status=404, code="not_found", detail="Conversation not found."
        ) from exc

    return ConversationDetail(
        id=conversation.id,
        channel=str(conversation.channel),
        status=str(conversation.status),
        started_at=rfc3339(conversation.started_at) or "",
        ended_at=rfc3339(conversation.ended_at),
        turn_count=conversation.turn_count,
        agent_code=conversation.agent_code,
        agent_version=conversation.agent_version,
        escalated=conversation.escalated,
        turns=[
            TurnDetail(
                id=turn.id,
                sequence=turn.sequence,
                role=str(turn.role),
                content=_content_for(turn, unmask=unmask),
                agent_node=turn.agent_node,
                created_at=rfc3339(turn.created_at) or "",
                latency_ms=turn.latency_ms,
                finish_reason=turn.finish_reason,
                interrupted=bool(turn.is_interruption),
                usage=(
                    {
                        "prompt_tokens": turn.input_tokens,
                        "completion_tokens": turn.output_tokens,
                        "cached_tokens": turn.cached_tokens,
                    }
                    if show_usage and turn.input_tokens is not None
                    else None
                ),
            )
            for turn in turns
        ],
    )


def _content_for(turn, *, unmask: bool) -> str | None:
    """Mask a turn's text unless the principal may read PII.

    Only ``user`` turns are masked. An assistant turn is our own output, and a
    receptionist's reply routinely *contains* what the customer told it — but
    masking it would make the transcript unreadable for the viewer role this
    exists to serve, and the correct control for assistant-side PII is
    redaction at the retention boundary (contract A3), not at read time. A real
    limitation, stated rather than papered over.
    """
    if turn.content is None:
        return None
    if unmask or turn.role != TurnRole.USER:
        return turn.content
    return "[redacted — requires conversations:read_pii]"


async def _conversation_events(
    *,
    request: Request,
    state: AppState,
    ctx: TenantContext,
    conversation_id: UUID,
    show_usage: bool,
    is_widget: bool,
    session: ConversationSession | None = None,
) -> AsyncIterator[SSEEvent]:
    """Runner events, framed for the caller's audience."""
    # First frame when a ticket was just exchanged. Emitted on the stream as
    # well as set as a cookie because the widget is embedded cross-origin on a
    # customer's website, where a `SameSite=Lax` cookie is never sent — the
    # event is the only delivery that works there.
    #
    # It carries a credential, so it goes ONLY to the caller that just redeemed
    # the ticket for this conversation, and only over the connection that
    # redemption opened. It is never re-emitted on a later reconnect.
    if session is not None:
        yield SSEEvent(
            event="session",
            data={
                "session": session.token,
                "expires_in": session.expires_in,
                "conversation_id": str(session.conversation_id),
            },
        )

    pending = await state.take_pending(conversation_id)
    # ONE stream serves the WHOLE conversation, not one turn.
    #
    # A browser opens a single `EventSource` and keeps it for the life of the
    # chat, posting each message over a separate `fetch`. So this generator has
    # to outlive an individual turn: it waits for a message, streams the reply,
    # then goes back to waiting. Ending after the first reply closed the socket
    # while the user was still typing their second question — the reply never
    # arrived, because nothing was listening when it was produced.
    #
    # Two earlier revisions of this function each got one half of that right:
    # the first returned immediately when no message was parked (a tight
    # reconnect loop that exhausted the rate limit), and the second waited for
    # the first message but still exited after answering it.
    idle_deadline = time.monotonic() + _IDLE_STREAM_TIMEOUT_S
    announced_idle = False

    try:
        while True:
            if pending is None:
                if await request.is_disconnected():
                    return
                if time.monotonic() > idle_deadline:
                    # Release the worker slot held by an abandoned tab. This
                    # close IS a real drop, and the client's ordinary reconnect
                    # is the right response — with its conversation session it
                    # reconnects without re-minting a ticket.
                    log.info(
                        "conversation.idle_stream_expired",
                        conversation_id=str(conversation_id),
                    )
                    return
                if not announced_idle:
                    # Once per idle period, not once per poll: the client needs
                    # to know the stream is live, not a 4Hz heartbeat of it.
                    yield SSEEvent(event="status", data={"status": "idle"})
                    announced_idle = True
                await asyncio.sleep(_IDLE_POLL_INTERVAL_S)
                pending = await state.take_pending(conversation_id)
                continue

            turn_id, user_text = pending
            pending = None
            announced_idle = False

            async for event in state.conversations.stream_turn(
                ctx=ctx,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_text=user_text,
            ):
                if await request.is_disconnected():
                    # Belt and braces. Starlette also cancels the generator on
                    # disconnect, and `sse_stream`'s finally closes this one,
                    # which cancels the graph task. Checking here ends the loop
                    # a beat sooner on a slow client.
                    log.info(
                        "conversation.client_disconnected",
                        conversation_id=str(conversation_id),
                    )
                    return
                framed = _frame(
                    event, turn_id, show_usage=show_usage, is_widget=is_widget
                )
                if framed is not None:
                    yield framed

            # The turn is done; the conversation is not. Reset the idle clock
            # from NOW so the timeout measures silence rather than total
            # session length — a chat that has been active for an hour is
            # exactly the one not to hang up on.
            idle_deadline = time.monotonic() + _IDLE_STREAM_TIMEOUT_S
    except asyncio.CancelledError:
        log.info("conversation.stream_cancelled", conversation_id=str(conversation_id))
        raise


def _frame(
    event: TurnEvent, turn_id: UUID, *, show_usage: bool, is_widget: bool
) -> SSEEvent | None:
    """Map a runner event onto the frozen wire vocabulary.

    The contract's frames are ``token``, ``tool_call``, ``turn_complete`` and
    ``error``; a staff client additionally gets ``handoff`` and
    ``turn.started``, because a console showing which specialist is handling a
    call is the point of a console. A widget gets neither — a visitor does not
    need to know the practice runs a triage specialist.
    """
    match event.type:
        case "token":
            # The contract's field is `delta`, not `text`.
            return SSEEvent(
                event="token",
                data={"turn_id": str(turn_id), "delta": event.data.get("text", "")},
            )
        case "tool.started":
            return SSEEvent(
                event="tool_call",
                data={
                    "turn_id": str(turn_id),
                    "tool": event.data.get("tool_key"),
                    "label": event.data.get("label"),
                    "status": "running",
                },
            )
        case "tool.finished":
            return SSEEvent(
                event="tool_call",
                data={
                    "turn_id": str(turn_id),
                    "tool": event.data.get("tool_key"),
                    "status": event.data.get("status", "ok"),
                },
            )
        case "turn.finished":
            data = {
                "turn_id": str(turn_id),
                "finish_reason": event.data.get("finish_reason", "stop"),
                "latency_ms": event.data.get("latency_ms", 0),
            }
            if show_usage and (usage := event.data.get("usage")):
                data["usage"] = usage
            return SSEEvent(event="turn_complete", data=data)
        case "error":
            return SSEEvent(event="error", data={"turn_id": str(turn_id), **event.data})
        case "handoff" if not is_widget:
            return SSEEvent(
                event="handoff", data={"turn_id": str(turn_id), **event.data}
            )
        case "handoff":
            # The widget gets the caller-safe half only: a phrase to show while
            # the specialist starts, never `to_agent`. Rendered as a `tool_call`
            # so it reuses the activity line the widget already has, rather than
            # adding a second progress mechanism that could disagree with it.
            if notice := event.data.get("notice"):
                return SSEEvent(
                    event="tool_call",
                    data={
                        "turn_id": str(turn_id),
                        "tool": "handoff",
                        "label": notice,
                        "status": "running",
                    },
                )
            return None
        case "turn.started" if not is_widget:
            return SSEEvent(
                event="turn.started", data={"turn_id": str(turn_id), **event.data}
            )
    return None
