"""Canonical UUIDv7 generation — contract §6 and amendment A2.

**There is exactly one source of ids in this codebase and it is this module.**

Amendment A2 fixes the split:

* Application-side generation is the *normal* path. Python mints ids for rows
  the application inserts, from the ``uuid6`` backport, auto-switching to the
  stdlib ``uuid.uuid7`` on 3.14+ behind the same name.
* Database-side ``uuid_generate_v7()`` (01 §1.2) is the *fallback*, existing so
  that column defaults work for rows inserted by migrations, backfills, and
  ``INSERT ... SELECT``.

**Why there is no ``uuid4()`` fallback.** A silent downgrade to v4 satisfies the
type signature and every test in the suite while destroying the index locality
that UUIDv7 was chosen for. On ``turns`` — an append-only table indexed by
``(tenant_id, created_at)`` — random keys scatter inserts across every leaf page
of the primary-key index. That surfaces months later as unexplained WAL volume
and cache churn, with nothing pointing back at the import that caused it. So
unavailability raises **at import**, where it is a five-second fix, rather than
at runtime, where it is not detectable at all.

The second reason it must fail hard: :func:`uuid7_timestamp` recovers the
partition key from the id itself, which is how a point lookup against a
partitioned table (``conversations``, ``turns``, ``audit_log``,
``usage_events``, ``ai_checkpoints``) prunes to a single partition without the
caller carrying a timestamp alongside every id. A v4 makes that impossible: the
first 48 bits are noise, and the derived "timestamp" would be a plausible-looking
datetime somewhere in the next 8000 years, silently pruning to the wrong
partition or to none.

Enforced externally by ``tools/check_uuid_usage.py`` plus a SQL assertion that
any persisted non-v7 id returns zero rows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

__all__ = [
    "UUID_V7_EPOCH",
    "UUIDGenerationUnavailableError",
    "is_uuid7",
    "new_id",
    "uuid7",
    "uuid7_timestamp",
]


class UUIDGenerationUnavailableError(ImportError):
    """No conforming UUIDv7 generator is importable.

    Raised at import time, deliberately. Amendment A2: "Unavailability must
    raise at import." Catching this and substituting ``uuid.uuid4`` is a
    contract violation, not a workaround.
    """


def _resolve_generator() -> Callable[[], UUID]:
    """Bind the one generator, preferring the stdlib when it exists.

    Python 3.14 adds ``uuid.uuid7`` to the standard library. Until then the
    pinned ``uuid6`` backport provides a byte-identical implementation of
    RFC 9562 §5.7. Resolving in this order means the 3.14 upgrade drops the
    third-party dependency without touching a single call site.
    """
    import uuid as _stdlib_uuid

    stdlib_uuid7 = getattr(_stdlib_uuid, "uuid7", None)
    if callable(stdlib_uuid7):
        return stdlib_uuid7  # type: ignore[no-any-return]

    try:
        from uuid6 import uuid7 as _backport_uuid7
    except ImportError as exc:  # pragma: no cover - exercised by the import guard
        raise UUIDGenerationUnavailableError(
            "No UUIDv7 generator available. Install the pinned `uuid6` backport "
            "or run on Python 3.14+ (stdlib `uuid.uuid7`). Contract amendment A2 "
            "forbids falling back to uuid4(): it satisfies the type signature and "
            "every test while destroying the index locality UUIDv7 was chosen for, "
            "and it makes uuid7_timestamp() — which recovers the partition key from "
            "the id — return meaningless values."
        ) from exc

    return _backport_uuid7


_uuid7: Final[Callable[[], UUID]] = _resolve_generator()

#: Unix epoch, the origin of the 48-bit millisecond field in a UUIDv7.
UUID_V7_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def uuid7() -> UUID:
    """Mint a time-ordered UUIDv7. The only id source in the application."""
    return _uuid7()


def new_id() -> UUID:
    """Alias for :func:`uuid7`, read at call sites as "a new primary key"."""
    return _uuid7()


def uuid7_timestamp(value: UUID) -> datetime:
    """Recover the creation instant embedded in a UUIDv7.

    The first 48 bits of a v7 are the Unix timestamp in milliseconds
    (RFC 9562 §5.7). Used by the repository layer to derive a partition window
    for point lookups against partitioned tables, and by retention jobs.

    Raises:
        ValueError: if ``value`` is not version 7. Refusing rather than
            returning a plausible-looking datetime is the point: a v4 would
            yield a random instant, and a partition-pruning predicate built
            from it would silently miss the row.
    """
    if value.version != 7:
        raise ValueError(
            f"uuid7_timestamp() requires a UUIDv7; got version {value.version}. "
            f"The timestamp field of a non-v7 UUID is random data, so any value "
            f"returned here would be a plausible-looking lie (contract A2)."
        )
    milliseconds = value.int >> 80
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=UTC)


def is_uuid7(value: UUID) -> bool:
    """True when ``value`` is a version-7 UUID.

    Used by the persisted-id assertion: every id column in the database must
    satisfy this, and a row that does not means something bypassed both
    generators.
    """
    return value.version == 7
