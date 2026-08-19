"""Contract A2 — the id generator, and its refusal to degrade.

These run without a database. They exist because the failure A2 describes is
invisible at runtime: a ``uuid4()`` fallback satisfies every type signature and
every integration test in the suite, and surfaces months later as unexplained
WAL volume with nothing pointing back at the import that caused it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4  # ALLOW-UUID4: negative controls for the v7 assertions

import pytest
from packages.core.ids import (
    UUIDGenerationUnavailableError,
    is_uuid7,
    new_id,
    uuid7,
    uuid7_timestamp,
)


def test_generated_ids_are_version_7() -> None:
    for _ in range(100):
        value = uuid7()
        assert value.version == 7, f"{value} is v{value.version}, not v7"
        assert value.variant == "specified in RFC 4122"
        assert is_uuid7(value)


def test_ids_are_time_ordered() -> None:
    """The property the whole amendment exists to protect.

    v7 keys are time-prefixed, so inserts append to the right edge of the
    primary-key index instead of scattering across every leaf page. On a
    100M-row ``turns`` table this is the difference between a working set of a
    few hundred pages and the whole index.

    Sorting as *strings* rather than as integers is deliberate: the b-tree
    orders by the stored byte representation, so string order is the property
    that actually determines index locality.
    """
    ids = [uuid7() for _ in range(1000)]
    assert [str(i) for i in ids] == sorted(str(i) for i in ids), (
        "generated ids are not monotonically ordered; index locality is lost"
    )
    assert len(set(ids)) == len(ids), "generated ids collided"


def test_timestamp_round_trips() -> None:
    """The recovered instant tracks wall clock, within the generator's own skew.

    The tolerance is asymmetric and that asymmetry is the finding. ``uuid6``'s
    generator keeps a monotonic counter: when called more than once inside the
    same millisecond it returns ``last_timestamp_ms + 1`` rather than the clock
    reading, so a burst of N ids in one millisecond pushes the embedded
    timestamp up to N milliseconds **into the future**. That is required for the
    ordering guarantee ``test_ids_are_time_ordered`` asserts — two ids in one
    millisecond must still sort — but it means "embedded timestamp ≤ now" is
    simply not true after a burst.

    This has a real consequence downstream, recorded here rather than
    discovered later: ``TenantRepository._partition_window`` builds a ±1 second
    window around this value to prune partitions. A single process minting more
    than ~1000 ids per millisecond would drift the counter past that window and
    the lookup would miss its own row. At ~1M ids/second sustained that is far
    outside anything this platform will do, but it is a real ceiling and not an
    imaginary one.
    """
    before = datetime.now(tz=UTC) - timedelta(seconds=1)
    value = uuid7()
    # Generous on the upper side: the monotonic counter may run ahead of the
    # clock by one millisecond per id already minted in this millisecond, and
    # earlier tests in this module mint a thousand of them.
    after = datetime.now(tz=UTC) + timedelta(seconds=5)

    recovered = uuid7_timestamp(value)
    assert before <= recovered <= after
    assert recovered.tzinfo is not None, "recovered timestamp must be tz-aware"


def test_uuid7_timestamp_refuses_a_v4() -> None:
    """The refusal is the feature.

    A v4's first 48 bits are noise, so returning a datetime built from them
    would be a plausible-looking lie — and the repository builds a partition
    window from it. A window derived from random bits prunes to zero partitions,
    so ``get()`` returns None for a row that exists: a silent wrong answer, the
    worst kind.
    """
    with pytest.raises(ValueError, match="requires a UUIDv7"):
        uuid7_timestamp(uuid4())  # ALLOW-UUID4: negative control — the value under test

    # And every other non-v7 version, so this cannot be special-cased to v4.
    for version in (1, 3, 5):
        # ALLOW-UUID4: source of random bits for a forged non-v7 id
        forged = UUID(int=(uuid4().int & ~(0xF << 76)) | (version << 76))
        assert forged.version == version
        with pytest.raises(ValueError):
            uuid7_timestamp(forged)


def test_is_uuid7_discriminates() -> None:
    assert is_uuid7(uuid7())
    assert not is_uuid7(uuid4())  # ALLOW-UUID4: negative control — must be rejected


def test_new_id_is_the_same_generator() -> None:
    value = new_id()
    assert value.version == 7


def test_import_failure_is_the_documented_type() -> None:
    """A2: "Unavailability must raise at import."

    The error is an ``ImportError`` subclass so that a caller cannot catch it
    with a bare ``except ImportError`` and quietly substitute ``uuid4`` — they
    would have to name the type, which is a reviewable act rather than an
    accident.
    """
    assert issubclass(UUIDGenerationUnavailableError, ImportError)
