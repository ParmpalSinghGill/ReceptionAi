#!/usr/bin/env python3
"""Drive a real booking journey through the demo endpoint and check the database.

    python tools/verify_booking.py

Books, reschedules, then cancels an appointment using ONLY relative dates
("next Tuesday morning"), and prints the `bookings` row after each step.

Why this exists as a committed tool rather than a scratch script: every booking
bug found so far was invisible in the transcript and obvious in the database.
The agent said "your appointment has been moved" while the row sat unchanged;
it said "I've passed you to a colleague" while no tool ran. Reading the reply
proves nothing. This asserts against the row.

Relative dates are deliberate. Explicit ISO times pass even when the model has
no idea what today is — the bug that hid behind a scripted "8:15 exactly" for
two rounds of verification.

Requires the stack up (`make dev`) and exits non-zero if any step fails to
land, so it is usable as a smoke test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
ORIGIN = {"Origin": "http://localhost:3002"}
CUSTOMER = "Verify Harness"
TIMEOUT = 120


def _request(path: str, body: dict | None = None, headers: dict | None = None,
             method: str | None = None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body else None,
        headers={
            **ORIGIN,
            **(headers or {}),
            **({"Content-Type": "application/json"} if body else {}),
        },
        method=method,
    )
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def booking_row() -> str:
    """Read the row straight from Postgres as the superuser.

    Deliberately bypasses the API: asking the application whether it saved
    something, using the same application, proves nothing.
    """
    sql = (
        "SELECT reference||' | '||status||' | '||"
        "to_char(starts_at AT TIME ZONE 'Asia/Kolkata','Dy DD Mon HH24:MI') "
        f"FROM bookings WHERE customer_name='{CUSTOMER}' "
        "ORDER BY created_at DESC LIMIT 1;"
    )
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres",
         "-d", "receptionai", "-tAc", sql],
        capture_output=True, text=True, timeout=60,
    )
    return out.stdout.strip() or "(no row)"


def main() -> int:
    try:
        session = json.load(_request("/api/v1/public/demo/sessions", method="POST"))
    except urllib.error.URLError as exc:
        print(f"cannot reach the API at {API} — is `make dev` running? ({exc})")
        return 2

    conversation_id = session["conversation_id"]
    ticket = session["ticket"]
    print("DESK:", session["greeting"])

    credential: dict[str, str] = {}
    reply: list[str] = []
    complete = threading.Event()

    def stream() -> None:
        event = None
        for raw in _request(
            f"/api/v1/conversations/{conversation_id}/stream?ticket={ticket}"
        ):
            line = raw.decode().rstrip()
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event == "session":
                    credential["token"] = payload["session"]
                if "delta" in payload:
                    reply.append(payload["delta"])
                if event == "turn_complete":
                    complete.set()

    threading.Thread(target=stream, daemon=True).start()
    for _ in range(150):
        if "token" in credential:
            break
        time.sleep(0.1)
    if "token" not in credential:
        print("never received a conversation session credential")
        return 2

    # (message, expected status, expected weekday in the row or None)
    #
    # The weekday matters as much as the status. An earlier version checked only
    # `confirmed`, so a reschedule that never moved the row passed cleanly: the
    # agent said "shall I move it to Thursday?" three times, the row sat on
    # Tuesday, and the harness reported OK. A booking that is confirmed on the
    # wrong day is exactly the failure this file exists to catch.
    script: list[tuple[str, str | None, str | None]] = [
        ("Hello, I'd like a check-up next Tuesday morning please.", None, None),
        # Accept whatever was offered rather than naming a time. The harness
        # previously said "8:15 suits me", which is only sometimes a slot that
        # exists — when it was not, the agent quite correctly asked whether the
        # offered 8:00 would do instead, and the harness read that as a failure
        # to book. Scripting a time the diary may not contain tests the diary,
        # not the agent.
        (
            f"That works. I'm {CUSTOMER}, my number is 9812350550.",
            "confirmed",
            "Tue",
        ),
        ("Actually, I need to move it to Thursday morning instead.", None, None),
        ("Yes, that time is fine — please move it.", "confirmed", "Thu"),
        ("On reflection, please cancel it altogether.", "cancelled", None),
    ]

    failures = 0
    for message, expected, expected_day in script:
        reply.clear()
        complete.clear()
        print("\nCALLER:", message)
        _request(
            f"/api/v1/conversations/{conversation_id}/messages",
            {"content": message},
            {"X-Conversation-Session": credential["token"]},
            "POST",
        )
        complete.wait(TIMEOUT)
        text = "".join(reply)
        if "DSML" in text or "<｜" in text:
            print("  !! raw tool-call markup leaked into the reply")
            failures += 1
        print("DESK:", text)
        if expected:
            row = booking_row()
            print("   DB →", row)
            if expected not in row:
                print(f"  !! expected status {expected!r}; the row disagrees")
                failures += 1
            if expected_day and expected_day not in row:
                print(
                    f"  !! expected the row to be on a {expected_day}; it is "
                    f"not. A booking confirmed on the wrong day is the failure "
                    f"this check exists for."
                )
                failures += 1

    print()
    if failures:
        print(f"FAILED — {failures} check(s) did not hold")
        return 1
    print("OK — book, reschedule and cancel all landed in the database")
    return 0


if __name__ == "__main__":
    sys.exit(main())
