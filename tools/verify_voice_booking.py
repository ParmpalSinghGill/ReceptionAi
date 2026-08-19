#!/usr/bin/env python3
"""Book an appointment through the VOICE path, then check the database.

    python tools/verify_voice_booking.py

The sibling ``verify_booking.py`` drives the HTTP demo endpoint. This one drives
:meth:`apps.media.runtime.MediaRuntime.run_turn` — the exact function the Pipecat
pipeline calls once Deepgram has turned speech into text. Everything downstream
of that call is shared with chat, so this proves the claim that matters: **a
spoken booking writes the same row a typed one does.**

What this does NOT prove is the audio itself. STT, VAD, turn-taking and TTS are
verified separately (see the module docstring in
``packages/providers/voice/pipecat_composed.py``) and ultimately by talking to
it. This harness covers the seam between "we have the caller's words" and "the
diary changed", which is where a voice-specific regression would otherwise hide
behind a transcript that reads perfectly.

Requires the stack up (`make dev`). Exits non-zero if the row does not land.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

#: Unique per run. A fixed name meant a run that booked nothing still found the
#: *previous* run's row and reported success — the harness passed while the
#: agent was saying "there's nothing free". A verification tool that cannot fail
#: is worse than no verification tool.
CUSTOMER = f"Voice Verify {uuid4().hex[:8]}"
PHONE = "07700900551"


def _load_env() -> None:
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip().strip('"'))


def booking_row() -> str:
    """Read the row straight from Postgres as the superuser.

    Bypasses the application deliberately: asking the app whether it saved
    something, using the app, proves nothing.
    """
    # The customer name is bound as a psql variable rather than interpolated.
    # It is a module constant here, so injection is not the live risk — but a
    # query built by concatenation is the thing that gets copied somewhere it
    # does matter, and `:'name'` quotes it correctly for free.
    #
    # The SQL goes in on **stdin**, not `-c`: psql does not expand variables in
    # a `-c` command string, so `-c` silently fails with a syntax error at the
    # colon and the harness reports "no row" for a booking that exists.
    sql = (
        "SELECT reference||' | '||status||' | '||"
        "to_char(starts_at AT TIME ZONE 'Asia/Kolkata','Dy DD Mon HH24:MI') "
        "FROM bookings WHERE customer_name = :'name' "
        "ORDER BY created_at DESC LIMIT 1;"
    )
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker", "compose", "exec", "-T", "postgres", "psql",
            "-U", "postgres", "-d", "receptionai",
            "-v", f"name={CUSTOMER}", "-tA",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    return out.stdout.strip() or "(no row)"


async def main() -> int:
    _load_env()

    from apps.api.settings import get_settings
    from apps.media.runtime import build_runtime
    from packages.core.tenancy import TenantContext
    from packages.persistence.models.enums import ConversationChannel
    from sqlalchemy import text

    settings = get_settings()
    runtime = await build_runtime(settings)

    try:
        # Resolve the demo tenant through the sanctioned function, not a SELECT
        # on `tenants`: that table's RLS policy is `id = app_current_tenant()`,
        # so a direct query returns zero rows and reports a seeded tenant as
        # missing. Knowing the id is the precondition for reading its row.
        async with runtime.session_factory.ops_unit_of_work() as ops:
            tenant_id: UUID | None = (
                await ops.execute(
                    text("SELECT id FROM public_resolve_tenant_by_slug(:slug)"),
                    {"slug": settings.demo_tenant_slug},
                )
            ).scalar_one_or_none()
        if tenant_id is None:
            print(f"demo tenant {settings.demo_tenant_slug!r} not seeded")
            return 2
        ctx = TenantContext(tenant_id=tenant_id)

        # A browser call is inbound voice: the channel is recorded so analytics
        # can tell a spoken booking from a typed one.
        started = await runtime.conversations.start(
            ctx=ctx,
            channel=ConversationChannel.VOICE_INBOUND,
            channel_metadata={"source": "verify_voice_booking"},
        )
        conversation_id = started.conversation_id
        print(f"conversation {conversation_id}")
        print("DESK:", started.greeting)

        # Phrased the way somebody speaks, not the way somebody types: relative
        # dates only, no ISO timestamps. An explicit date passes even when the
        # model has no idea what today is.
        script = [
            "Hello, I'd like to book a check-up please. Any day next week.",
            f"That works. I'm {CUSTOMER}, my number is {PHONE}.",
            # A third turn so the harness can still land the booking when the
            # first offer is declined or a slot is taken between offer and
            # confirmation — which is a real race, not a test artefact.
            "Yes, please book whichever of those you have.",
        ]

        for utterance in script:
            print(f"\nCALLER: {utterance}")
            reply = await runtime.run_turn(
                tenant_id=ctx.tenant_id,
                conversation_id=conversation_id,
                user_text=utterance,
            )
            print(f"DESK  : {reply}")
            print(f"ROW   : {booking_row()}")

        row = booking_row()
        if "confirmed" not in row:
            print(f"\nFAIL — no confirmed booking for {CUSTOMER!r}: {row}")
            return 1

        print(f"\nOK — the voice path booked a real row: {row}")
        return 0
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
