"""Stream tickets — PHASE1-API-CONTRACT, the mechanism from 04 §9.1 corrected.

The problem this solves, stated exactly. The demo widget needs an
``EventSource`` connection, and:

* **A browser cannot set ``Authorization`` on ``EventSource``.** The API is
  fixed; there is no header option. So a bearer token cannot authenticate the
  stream.
* **The session token must never appear in a URL.** URLs land in access logs,
  in ``Referer`` headers, in browser history, and in any proxy between here
  and the client. A long-lived credential in a query string is a credential
  you have published.

So: a **short-lived, single-use ticket**, scoped to one conversation, that is
worthless the moment it is used.

Three properties, each defended below:

1. **Single-use, atomically.** ``GETDEL`` — one round-trip that reads and
   deletes. A ``GET`` followed by a ``DELETE`` is two round-trips with a race:
   two connections arriving together both read the ticket, both proceed, and
   the "single-use" property is gone precisely under the conditions (a
   reconnect storm) where it matters.
2. **20-second TTL.** Long enough for the widget to receive the response and
   open the stream; short enough that a ticket captured from a log is expired
   before anyone reads the log.
3. **Bound to one conversation.** A ticket for conversation A cannot open the
   stream for conversation B, so a leaked ticket exposes one anonymous demo
   chat and nothing else.

**A 401 on reconnect means "re-mint", not "auth failed"** (04 §9.1). This is
the consequence the frontends must handle: the ticket is single-use, and
``EventSource`` reconnects by replaying *the original URL*, ticket included. So
the second connection always presents a spent ticket. That is correct
behaviour, not a bug, and the ``WWW-Authenticate`` error parameter distinguishes
it.

---

**Why a ticket is not enough on its own — the exchange.**

A single-use ticket has a lifetime problem the contract does not spell out. The
widget's real sequence is:

    POST /public/demo/sessions   -> ticket
    GET  /conversations/{id}/stream?ticket=...   (opens, then waits)
    ...user types...
    POST /conversations/{id}/messages            (seconds or minutes later)

The stream is opened *first* and stays open. If opening it spends the ticket —
which it must, or the URL an ``EventSource`` replays on reconnect would be
indefinitely reusable — then by the time the user finishes typing there is no
credential left. The message 401s and the demo cannot hold a conversation.

A peek-instead-of-redeem on the message path does not help: once the stream has
redeemed the ticket, a peek fails too. The ticket is simply the wrong shape for
a credential that has to outlive the exchange.

So the ticket is **exchanged**, exactly once, for a
:class:`ConversationSession`: a conversation-scoped credential with a ~30
minute TTL, minted by the stream and returned to the client as the stream's
first event. The ticket keeps every property it had — single-use, 20 seconds,
one conversation — and what it now buys is a credential rather than a
connection.

The session credential is deliberately *not* a second ticket: it is reusable
within its conversation (the user sends many messages), and it is scoped to
exactly that conversation, so it is worth no more than the ticket that produced
it. It is not a login, cannot reach ``/me``, and expires on its own.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

__all__ = [
    "SESSION_PREFIX",
    "TICKET_PREFIX",
    "ConversationSession",
    "StreamTicketService",
    "TicketClaim",
]

log = structlog.get_logger(__name__)

#: Prefixed so a ticket is recognizable in a log or a Redis dump, and so the
#: key space cannot collide with the rate limiter's.
TICKET_PREFIX = "dt_"

#: Distinct prefix for the exchanged credential. Not cosmetic: it makes the two
#: unmistakable in a log, and it lets `redeem` reject a session token presented
#: where a ticket belongs (and vice versa) on a string comparison, before any
#: Redis round-trip.
SESSION_PREFIX = "ds_"

_KEY = "ticket:stream:{token}"
_SESSION_KEY = "ticket:session:{token}"

#: How long an exchanged credential lives. Long enough for a real conversation
#: — someone reads the greeting, thinks, types, waits for a reply, types again
#: — and short enough that an abandoned one expires the same afternoon. Renewed
#: on each use, so an *active* conversation never expires under the user.
DEFAULT_SESSION_TTL_SECONDS = 1_800


@dataclass(frozen=True, slots=True)
class TicketClaim:
    """What a redeemed ticket authorizes: exactly one conversation."""

    tenant_id: UUID
    conversation_id: UUID


@dataclass(frozen=True, slots=True)
class ConversationSession:
    """A reusable, conversation-scoped credential exchanged for a ticket."""

    token: str
    tenant_id: UUID
    conversation_id: UUID
    expires_in: int


class StreamTicketService:
    """Mints and redeems single-use stream tickets."""

    def __init__(self, redis: Redis | None, *, ttl_seconds: int = 20) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def mint(self, *, tenant_id: UUID, conversation_id: UUID) -> str:
        """Issue a ticket for one conversation.

        The token is 256 bits from ``secrets``. Not a signed JWT: a signed
        token cannot be *revoked* on use without server state, and server state
        is the thing that makes single-use work. Since we need the Redis entry
        anyway, an opaque random string is strictly better — it carries no
        payload to leak and no signature to attack.
        """
        token = TICKET_PREFIX + secrets.token_urlsafe(32)
        if self._redis is None:
            raise RuntimeError(
                "Stream tickets require Redis. Refusing to mint an "
                "unverifiable ticket — a ticket that cannot be redeemed "
                "atomically is not single-use."
            )
        await self._redis.set(
            _KEY.format(token=token),
            f"{tenant_id}:{conversation_id}",
            ex=self._ttl,
        )
        return token

    async def peek(self, token: str) -> TicketClaim | None:
        """Validate a ticket **without consuming it**.

        This exists because of a sequencing problem the contract implies but
        does not spell out. The widget's flow is:

            POST /messages   (send the turn)
            GET  /stream     (read the reply)

        Both carry the same ticket. If ``POST /messages`` consumed it, the
        ``GET /stream`` that follows would always find it spent — the widget
        could never read the answer to its own question, and every single
        conversation would need two tickets for one turn.

        So the split is: **``/messages`` peeks, ``/stream`` redeems.** The
        single-use property lives where it matters — on the long-lived
        connection, which is the one an ``EventSource`` replays on reconnect
        and therefore the one that must not be re-openable.

        The peek is still a real authorization check: an expired, unknown or
        already-redeemed ticket returns ``None`` here exactly as it does in
        :meth:`redeem`, and the 20-second TTL still bounds it. What a peek
        cannot do is open a stream.
        """
        if not token or not token.startswith(TICKET_PREFIX):
            return None
        if self._redis is None:
            return None

        try:
            raw = await self._redis.get(_KEY.format(token=token))
        except RedisError:
            # Fail CLOSED, as `redeem` does: this is an isolation control, and
            # admitting an unverifiable ticket during a Redis blip would let
            # whoever asked post into a conversation.
            log.warning("ticket.peek_unavailable")
            return None

        return self._decode(raw)

    async def redeem(self, token: str) -> TicketClaim | None:
        """Consume a ticket. Returns its claim, or ``None`` if invalid.

        ``GETDEL`` is the whole security property — see the module docstring.
        Returns ``None`` for every failure mode (unknown, expired, already
        used, malformed) without distinguishing them: telling a caller *why*
        their ticket failed lets them probe for valid ones, and the client's
        remedy is identical in all four cases.
        """
        if not token or not token.startswith(TICKET_PREFIX):
            return None
        if self._redis is None:
            return None

        try:
            raw = await self._redis.getdel(_KEY.format(token=token))
        except RedisError:
            # Fail CLOSED. Unlike the rate limiter, this is an isolation
            # control: admitting an unverifiable ticket during a Redis blip
            # would open an authenticated stream to whoever asked.
            log.warning("ticket.redeem_unavailable")
            return None

        return self._decode(raw)

    @staticmethod
    def _decode(raw: bytes | str | None) -> TicketClaim | None:
        """Parse a stored ticket payload. Shared by peek and redeem."""
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        tenant_str, _, conversation_str = value.partition(":")
        try:
            return TicketClaim(
                tenant_id=UUID(tenant_str), conversation_id=UUID(conversation_str)
            )
        except ValueError:
            log.warning("ticket.malformed_payload")
            return None

    async def revoke(self, token: str) -> None:
        """Drop a ticket without redeeming it. For logout and for tests."""
        if self._redis is not None and token.startswith(TICKET_PREFIX):
            await self._redis.delete(_KEY.format(token=token))

    # ── The exchange ──────────────────────────────────────────────────────

    async def open_session(
        self, claim: TicketClaim, *, ttl_seconds: int | None = None
    ) -> ConversationSession:
        """Mint the credential a redeemed ticket buys.

        Called by the stream handler immediately after :meth:`redeem`, so the
        ticket is exchanged exactly once and the client receives something that
        outlives the connection it was exchanged on.

        The claim is passed in rather than the raw ticket: the caller has
        already redeemed, and taking a :class:`TicketClaim` makes it impossible
        to call this without having done so. A signature that cannot express
        "mint a session without spending a ticket" is the enforcement.
        """
        if self._redis is None:
            raise RuntimeError(
                "A conversation session requires Redis. Refusing to mint an "
                "unverifiable credential."
            )

        ttl = ttl_seconds or DEFAULT_SESSION_TTL_SECONDS
        token = SESSION_PREFIX + secrets.token_urlsafe(32)
        await self._redis.set(
            _SESSION_KEY.format(token=token),
            f"{claim.tenant_id}:{claim.conversation_id}",
            ex=ttl,
        )
        log.info(
            "ticket.exchanged",
            conversation_id=str(claim.conversation_id),
            ttl_seconds=ttl,
        )
        return ConversationSession(
            token=token,
            tenant_id=claim.tenant_id,
            conversation_id=claim.conversation_id,
            expires_in=ttl,
        )

    async def resolve_session(
        self, token: str, *, renew: bool = True
    ) -> TicketClaim | None:
        """Validate a conversation session credential.

        **Reusable, unlike a ticket** — that is the entire point of the
        exchange. A user sends many messages in one conversation, and a
        single-use credential here would mean one message per session.

        ``renew`` slides the TTL on each use, so an active conversation never
        expires mid-sentence while an abandoned one still ages out. The renewal
        is a separate ``EXPIRE`` rather than a re-``SET``: it cannot alter the
        stored claim, so a renewal bug can shorten or extend a lifetime but can
        never repoint a credential at another conversation.
        """
        if not token or not token.startswith(SESSION_PREFIX):
            return None
        if self._redis is None:
            return None

        key = _SESSION_KEY.format(token=token)
        try:
            raw = await self._redis.get(key)
            if raw is not None and renew:
                await self._redis.expire(key, DEFAULT_SESSION_TTL_SECONDS)
        except RedisError:
            # Fail CLOSED, as the ticket paths do: this is an isolation
            # control, and admitting an unverifiable credential during a Redis
            # blip would let whoever asked post into a conversation.
            log.warning("ticket.session_resolve_unavailable")
            return None

        return self._decode(raw)

    async def revoke_session(self, token: str) -> None:
        """Drop a conversation session. For an explicit end-of-chat and tests."""
        if self._redis is not None and token.startswith(SESSION_PREFIX):
            await self._redis.delete(_SESSION_KEY.format(token=token))
