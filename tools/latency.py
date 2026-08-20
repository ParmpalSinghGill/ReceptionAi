#!/usr/bin/env python3
"""Per-message latency, read out of Langfuse.

    python tools/latency.py                 # last 20 turns, newest first
    python tools/latency.py --limit 50      # more history
    python tools/latency.py --breakdown     # where the time went, per turn
    python tools/latency.py --summary       # percentiles across the window

Langfuse records latency on every trace and every span, so the question "why
was that reply slow" is answerable after the fact rather than only while
someone is watching. This is the command-line view of the same data the
Tracing page shows; the UI is better for one trace, this is better for a
pattern across many.

Reads credentials from `.env` — the same LANGFUSE_* values the application
authenticates with.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    """Read `.env` without a dependency. Only simple KEY=value lines."""
    env: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    return {**env, **os.environ}


def _get(path: str, env: dict[str, str]) -> dict:
    host = env.get("LANGFUSE_HOST", "http://localhost:53110")
    auth = base64.b64encode(
        f"{env.get('LANGFUSE_PUBLIC_KEY','')}:{env.get('LANGFUSE_SECRET_KEY','')}".encode()
    ).decode()
    req = urllib.request.Request(
        f"{host}{path}", headers={"Authorization": f"Basic {auth}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _clock(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return iso[11:19]


def _first_line(value) -> str:
    """A trace's input, short enough for a table."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value)
    # Masked capture stores a shape, not the words. Say so rather than printing
    # `{"redacted": true, "chars": 43}` in a column meant for a sentence.
    if '"redacted"' in text:
        return "(masked — set LANGFUSE_CAPTURE=full to see text)"
    return text.strip('"').replace("\\n", " ")[:46]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--breakdown", action="store_true", help="per-span timings for each turn"
    )
    parser.add_argument(
        "--summary", action="store_true", help="percentiles across the window"
    )
    args = parser.parse_args()
    env = _load_env()

    try:
        traces = _get(f"/api/public/traces?limit={args.limit}", env)["data"]
    except urllib.error.URLError as exc:
        print(f"cannot reach Langfuse — is the stack up? ({exc})")
        return 2

    turns = [t for t in traces if t.get("latency")]
    if not turns:
        print("no traces yet — hold a conversation first")
        return 0

    if args.summary:
        lat = sorted(t["latency"] for t in turns)
        n = len(lat)
        print(f"turns: {n}")
        print(f"  min    {lat[0]:6.2f}s")
        print(f"  median {lat[n // 2]:6.2f}s")
        print(f"  p90    {lat[min(n - 1, int(n * 0.9))]:6.2f}s")
        print(f"  max    {lat[-1]:6.2f}s")
        return 0

    print(f"{'when':10} {'latency':>8}  {'message'}")
    print("-" * 72)
    for t in turns:
        print(
            f"{_clock(t.get('timestamp','')):10} "
            f"{t['latency']:7.2f}s  {_first_line(t.get('input'))}"
        )

        if not args.breakdown:
            continue

        spans = _get(
            f"/api/public/observations?traceId={t['id']}&limit=40", env
        )["data"]
        timed = [o for o in spans if o.get("startTime") and o.get("endTime")]
        if not timed:
            continue
        start = min(
            datetime.fromisoformat(o["startTime"].replace("Z", "+00:00"))
            for o in timed
        )
        for span in sorted(timed, key=lambda o: o["startTime"]):
            began = datetime.fromisoformat(span["startTime"].replace("Z", "+00:00"))
            ended = datetime.fromisoformat(span["endTime"].replace("Z", "+00:00"))
            secs = (ended - began).total_seconds()
            offset = (began - start).total_seconds()
            # A bar makes a five-call turn legible at a glance: the question is
            # almost always "which of these is the big one", not "how long
            # exactly was the third".
            print(
                f"           {offset:5.2f}s {secs:5.2f}s "
                f"{'#' * max(1, int(secs * 10)):24} {span['name'][:32]}"
            )
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
