"""Cursor pagination — the shape 05 §B8 references and 06 §11.4 specifies.

Offset paging is not offered anywhere in this API, and the reason is specific
rather than stylistic: ``conversations`` and ``turns`` are partitioned by month
on ``created_at``. An ``OFFSET n`` carries no predicate the planner can prune a
partition with, so page 40 of a busy tenant's history reads every partition to
throw away the first 2,000 rows. On an append-heavy table it is also *wrong* —
rows inserted between two requests shift the window, so a reader paging
backwards through a live list sees some rows twice and never sees others.

A keyset cursor fixes both: it is a predicate on the same columns the index is
built from, and it names a position in the data rather than a count of rows
skipped, so concurrent inserts cannot move it.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CursorError",
    "CursorPage",
    "Limit",
    "decode_cursor",
    "encode_cursor",
    "page_from",
]

_CURSOR_VERSION = 1


class CursorError(ValueError):
    """A cursor that did not decode. Always a 400, never a 500."""


def encode_cursor(created_at: datetime, entity_id: UUID, *, sort: str) -> str:
    """Opaque, URL-safe, and carrying its own sort key.

    ``sort`` is embedded so that a cursor taken from one ordering cannot be
    replayed against another. Without it, paging a list by ``started_at`` and
    then switching the sort to ``created_at`` silently produces a keyset
    predicate against the wrong column and returns a plausible but wrong page —
    the worst failure mode available, because nothing errors.

    Base64url of compact JSON rather than a signed token: a cursor is not a
    capability. It carries no authority — RLS scopes the query regardless of
    what the cursor says, and a forged cursor can only move the caller's own
    window within their own tenant. Signing it would buy nothing and add a key
    to rotate.
    """
    payload = {
        "v": _CURSOR_VERSION,
        "t": created_at.astimezone(UTC).isoformat(),
        "i": str(entity_id),
        "s": sort,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, expect_sort: str) -> tuple[datetime, UUID]:
    """The inverse, raising :class:`CursorError` on anything unusable.

    Every failure path here is a *client* error. A cursor arrives from a query
    string, so it can be truncated by a mail client, hand-edited, or replayed
    from a bookmark after the sort changed — none of which is an exception the
    server should surface as a 500.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CursorError("malformed cursor") from exc

    if not isinstance(payload, dict):
        raise CursorError("malformed cursor payload")
    if payload.get("v") != _CURSOR_VERSION:
        raise CursorError(f"unsupported cursor version {payload.get('v')!r}")
    if payload.get("s") != expect_sort:
        raise CursorError(
            f"cursor was issued for sort {payload.get('s')!r}, "
            f"but this request sorts by {expect_sort!r}"
        )
    try:
        moment = datetime.fromisoformat(payload["t"])
        entity_id = UUID(payload["i"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("malformed cursor payload") from exc

    # A naive timestamp compared against a timezone-aware column raises inside
    # the driver. Normalising here keeps that failure out of the query layer.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC), entity_id


class CursorPage[ItemT](BaseModel):
    """One page of an unbounded list.

    Note what is absent: a total. A ``COUNT(*)`` over a partitioned,
    append-heavy table is the single most expensive query this API could issue,
    it is wrong the moment it returns, and no screen in ``apps/web`` displays
    one — the lists show "Load more", never "page 3 of 91" (06 §11.4).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"items": [], "next_cursor": None, "has_more": False}]
        },
    )

    items: list[ItemT]
    next_cursor: str | None = None
    has_more: bool = False


#: The page size a list route accepts. Bounded at both ends: 0 would spin a
#: client forever and an unbounded value would let one request pull a tenant's
#: entire history through a single connection.
Limit = Annotated[int, Field(default=50, ge=1, le=200)]


def page_from[ItemT](
    rows: list[ItemT],
    limit: int,
    *,
    sort: str,
    key: Callable[[ItemT], tuple[datetime, UUID]],
) -> CursorPage[ItemT]:
    """Build a page from ``limit + 1`` fetched rows.

    Fetching one extra row is how ``has_more`` is answered without a COUNT: if
    the query returned more than the caller asked for, there is at least one
    more page, and the extra row is discarded rather than shown.
    """
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        created_at, entity_id = key(items[-1])
        next_cursor = encode_cursor(created_at, entity_id, sort=sort)
    return CursorPage[ItemT](items=items, next_cursor=next_cursor, has_more=has_more)
