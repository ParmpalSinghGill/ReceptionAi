#!/usr/bin/env python3
"""Drive the urgent-symptom path and check the database.

    python tools/verify_emergency.py            # normal journey
    python tools/verify_emergency.py --bad-phone   # recovery on a bad number
    python tools/verify_emergency.py --red-flag    # must NOT book; must escalate
    python tools/verify_emergency.py --runs 6      # repeat, distinct customers

Companion to `verify_booking.py`, which covers the routine
book → reschedule → cancel path. Both exist because three consecutive fixes
each repaired one path while silently breaking the other, and only running
both caught it.

`--runs` matters more than it looks. Two of the bugs found here appeared
roughly one time in three: a single clean run proved nothing, and the failure
was invisible in the transcript either way. Repetition against distinct
customer names is the only reliable signal.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
ORIGIN = {"Origin": "http://localhost:3002"}
TIMEOUT = 120

#: The spoken time in an offer ("at 4:30 this afternoon", "at 9am tomorrow").
_OFFERED = re.compile(r"\bat (\d{1,2}(?::\d{2})?\s?(?:am|pm))", re.IGNORECASE)

#: The booking reference the agent reads back, e.g. "BK-V5TAYV". Markdown bold
#: around it is common, hence the optional asterisks.
_REFERENCE = re.compile(r"\*{0,2}(BK-[A-Z0-9]{6,})\*{0,2}")


def _request(path, body=None, headers=None, method=None):
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


def booking_row(reference: str) -> str:
    """Read the row directly from Postgres, bypassing the API deliberately.

    Looked up by the reference the agent spoke, not by customer name. Matching
    on the name reported false failures: told "I'm Verify Urgent 21917-1", the
    agent quite sensibly stored "Urgent 21917-1", reading the leading word as
    part of my sentence rather than part of the name. The reference is the
    agent's own output and is unambiguous.
    """
    sql = (
        "SELECT reference||' | '||status||' | '||"
        "to_char(starts_at AT TIME ZONE 'Asia/Kolkata','Dy DD Mon HH24:MI') "
        f"FROM bookings WHERE reference='{reference}' LIMIT 1;"
    )
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres",
         "-d", "receptionai", "-tAc", sql],
        capture_output=True, text=True, timeout=60,
    )
    return out.stdout.strip() or "(no row)"


def run_once(customer: str, mode: str) -> int:
    """One conversation. Returns the number of failed checks."""
    session = json.load(_request("/api/v1/public/demo/sessions", method="POST"))
    cid, ticket = session["conversation_id"], session["ticket"]
    print("DESK:", session["greeting"])

    cred: dict[str, str] = {}
    reply: list[str] = []
    done = threading.Event()

    def stream() -> None:
        event = None
        for raw in _request(f"/api/v1/conversations/{cid}/stream?ticket={ticket}"):
            line = raw.decode().rstrip()
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event == "session":
                    cred["token"] = payload["session"]
                if "delta" in payload:
                    reply.append(payload["delta"])
                if event == "turn_complete":
                    done.set()

    threading.Thread(target=stream, daemon=True).start()
    for _ in range(150):
        if "token" in cred:
            break
        time.sleep(0.1)
    if "token" not in cred:
        print("never received a conversation session credential")
        return 1

    if mode == "red-flag":
        opening = "my face is swollen and I'm having trouble swallowing"
        phone = "9812350321"
    elif mode == "bad-phone":
        opening = "chipped tooth"
        # Four digits is a length no supported country uses, so it is refused
        # everywhere. This was "9123456789" while the rules demanded exactly the
        # practice's own format — but ten digits is an Indian mobile, a US
        # number and a French one, and the policy is now to take down anything
        # plausible somewhere rather than interrogate a caller about dialling
        # codes. A test that asserts a real number is rejected is a test
        # defending a bug.
        phone = "9123"
    else:
        opening = "chipped tooth"
        phone = "9812350321"

    script = [
        opening,
        "since last night",
        f"yes please. {customer}, {phone}",
        "yes, book it",
        "yes, go ahead",
    ]

    failures = 0
    offered: str | None = None
    reference: str | None = None
    transcript: list[str] = []

    for message in script:
        reply.clear()
        done.clear()
        print("\nYOU:", message)
        _request(
            f"/api/v1/conversations/{cid}/messages",
            {"content": message},
            {"X-Conversation-Session": cred["token"]},
            "POST",
        )
        done.wait(TIMEOUT)
        text = "".join(reply)
        transcript.append(text)
        print("DESK:", text)

        if "slot_" in text:
            print("  !! an internal slot handle reached the caller")
            failures += 1
        if "DSML" in text or "<｜" in text:
            print("  !! raw tool-call markup reached the caller")
            failures += 1
        if offered is None and (found := _OFFERED.search(text)):
            offered = found.group(1).lower().replace(" ", "")
        if reference is None and (ref := _REFERENCE.search(text)):
            reference = ref.group(1)

    row = booking_row(reference) if reference else "(no row)"
    print("\nDB →", row)
    joined = " ".join(transcript).lower()

    if mode == "red-flag":
        # A red flag must escalate, not book. Booking someone with facial
        # swelling into a routine slot is the failure this path exists to stop.
        if row != "(no row)":
            print("  !! a red-flag caller was booked instead of escalated")
            failures += 1
    elif mode == "bad-phone":
        if row != "(no row)":
            print("  !! an unusable phone number still produced a booking")
            failures += 1
        # The recovery must name the obstacle. "I wasn't able to complete that"
        # tells the caller nothing and invites them to repeat the same input.
        if not re.search(r"number|digit", joined):
            print("  !! the reply never mentions the phone number, so the "
                  "caller cannot know what to fix")
            failures += 1
    else:
        if "confirmed" not in row:
            print("  !! no confirmed booking")
            failures += 1
        elif offered:
            # The time spoken aloud must be the time in the row. This drifted
            # by 30 and 60 minutes in two separate bugs, both silent.
            hh = row.split()[-1]
            h24, mm = int(hh.split(":")[0]), hh.split(":")[1]
            suffix = "am" if h24 < 12 else "pm"
            h12 = h24 % 12 or 12
            expected = {f"{h12}:{mm}{suffix}", f"{h12}{suffix}" if mm == "00" else ""}
            if offered not in expected:
                print(f"  !! offered {offered}, booked {hh} — the caller was "
                      f"told a time that is not in the diary")
                failures += 1

    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--bad-phone", action="store_true")
    parser.add_argument("--red-flag", action="store_true")
    args = parser.parse_args()

    mode = "bad-phone" if args.bad_phone else "red-flag" if args.red_flag else "book"

    try:
        _request("/healthz")
    except urllib.error.URLError as exc:
        print(f"cannot reach the API at {API} — is `make dev` running? ({exc})")
        return 2

    total = 0
    stamp = int(time.time()) % 100000
    for i in range(args.runs):
        customer = f"Verify Urgent {stamp}-{i + 1}"
        print(f"\n{'=' * 68}\nrun {i + 1}/{args.runs} — {customer} [{mode}]\n{'=' * 68}")
        total += run_once(customer, mode)

    print()
    if total:
        print(f"FAILED — {total} check(s) did not hold across {args.runs} run(s)")
        return 1
    print(f"OK — {args.runs} run(s), mode {mode}, every check held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
