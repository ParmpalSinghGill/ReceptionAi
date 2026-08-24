#!/usr/bin/env python3
"""Everything about one slow turn, read out of Langfuse.

    python tools/slow_turn.py                      # the slowest recent turn
    python tools/slow_turn.py --limit 50           # search further back
    python tools/slow_turn.py --trace <id>         # one specific trace
    python tools/slow_turn.py --slowest 3          # the three worst
    python tools/slow_turn.py --content            # full prompts and replies
    python tools/slow_turn.py --nudged             # only turns where the guard fired

Answers "where did those 40 seconds go" with the whole turn laid out in order:
every model call and tool call with its own duration, the gaps BETWEEN them
(time the platform spent, not the provider), prompt sizes, token counts, and
optionally the exact text sent and received.

The gap column is the part a latency summary cannot show. A turn whose spans sum
to 12s but whose wall clock is 19s spent 7s somewhere that is not instrumented,
and that is a different bug from a slow provider.

Reads LANGFUSE_* from `.env`, the same values the application authenticates with.
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
from typing import Any

NUDGE_MARK = "This instruction is internal"


def _load_env() -> None:
    """Read `.env` without a dependency; real environment wins."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _api(path: str) -> Any:
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:53110").rstrip("/")
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        sys.exit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set.")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    req = urllib.request.Request(
        f"{host}{path}", headers={"Authorization": f"Basic {auth}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        sys.exit(f"Langfuse {exc.code} on {path}: {exc.reason}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach Langfuse at {host}: {exc.reason}")


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: Any, limit: int | None) -> str:
    """Flatten a message list or dict to readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        out = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                role = item.get("role", "?")
                content = item.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(p.get("text", p)) if isinstance(p, dict) else str(p)
                        for p in content
                    )
                parts.append(f"[{role}] {content}")
            else:
                parts.append(str(item))
        out = "\n".join(parts)
    else:
        out = json.dumps(value, ensure_ascii=False)
    if limit is not None and len(out) > limit:
        return out[:limit] + f"… (+{len(out) - limit} chars)"
    return out


def _usage(obs: dict) -> str:
    u = obs.get("usage") or obs.get("usageDetails") or {}
    if not isinstance(u, dict):
        return ""
    got = {
        k: v
        for k, v in u.items()
        if isinstance(v, int) and v and k not in {"total", "totalTokens"}
    }
    return "  ".join(f"{k}={v}" for k, v in sorted(got.items()))


def report(trace_id: str, *, content: bool, width: int | None) -> None:
    d = _api(f"/api/public/traces/{trace_id}")
    obs = [o for o in d.get("observations", []) if isinstance(o, dict)]
    obs.sort(key=lambda o: o.get("startTime") or "")
    total = d.get("latency") or 0.0
    blob = json.dumps(d)

    print("=" * 78)
    print(f"TRACE {trace_id}")
    print(f"  when      : {d.get('timestamp')}")
    print(f"  wall clock: {total:.2f}s")
    print(f"  guard nudge fired: {'YES' if NUDGE_MARK in blob else 'no'}")
    print(f"  input     : {_text(d.get('input'), 160)!r}")
    print(f"  output    : {_text(d.get('output'), 160)!r}")
    print("=" * 78)

    root_start = _ts(d.get("timestamp"))
    spans = [o for o in obs if o.get("type") in {"GENERATION", "TOOL"}]
    llm = sum(o.get("latency") or 0 for o in spans if o["type"] == "GENERATION")
    tool = sum(o.get("latency") or 0 for o in spans if o["type"] == "TOOL")

    print(f"\n{'#':>2}  {'kind':<11}{'name':<34}{'start':>8}{'dur':>9}{'gap':>8}")
    print("-" * 78)
    prev_end: datetime | None = root_start
    for i, o in enumerate(spans, 1):
        start, end = _ts(o.get("startTime")), _ts(o.get("endTime"))
        dur = o.get("latency") or 0.0
        offset = (start - root_start).total_seconds() if start and root_start else 0.0
        gap = (start - prev_end).total_seconds() if start and prev_end else 0.0
        flag = " <-- NUDGE" if NUDGE_MARK in json.dumps(o.get("input") or "") else ""
        print(
            f"{i:>2}  {o.get('type',''):<11}{str(o.get('name'))[:33]:<34}"
            f"{offset:>7.2f}s{dur:>8.2f}s{gap:>7.2f}s{flag}"
        )
        if end:
            prev_end = end

    tail = (
        (root_start and prev_end and total - (prev_end - root_start).total_seconds())
        or 0.0
    )
    accounted = llm + tool
    print("-" * 78)
    print(f"    model calls   {llm:>7.2f}s  ({100*llm/total if total else 0:.0f}%)")
    print(f"    tool  calls   {tool:>7.2f}s  ({100*tool/total if total else 0:.1f}%)")
    print(f"    unaccounted   {total-accounted:>7.2f}s  <- platform, queueing, gaps")
    print(f"    after last span {tail:>5.2f}s")

    if not content:
        print("\n(run with --content for the full prompts and replies)")
        return

    for i, o in enumerate(spans, 1):
        if o.get("type") != "GENERATION":
            continue
        print("\n" + "=" * 78)
        print(
            f"CALL {i}  {o.get('name')}  model={o.get('model')}  "
            f"{o.get('latency') or 0:.2f}s"
        )
        if u := _usage(o):
            print(f"  usage: {u}")
        raw = json.dumps(o.get("input") or "")
        print(f"  prompt chars: {len(raw)}")
        if NUDGE_MARK in raw:
            print("  *** THE GUARD NUDGE IS IN THIS PROMPT ***")
        print("-" * 78)
        print("INPUT:")
        print(_text(o.get("input"), width))
        print("-" * 78)
        print("OUTPUT:")
        print(_text(o.get("output"), width))

    for i, o in enumerate(spans, 1):
        if o.get("type") != "TOOL":
            continue
        print("\n" + "-" * 78)
        print(f"TOOL {i}  {o.get('name')}  {o.get('latency') or 0:.3f}s")
        print(f"  in : {_text(o.get('input'), width)}")
        print(f"  out: {_text(o.get('output'), width)}")


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", help="a specific trace id")
    ap.add_argument("--limit", type=int, default=40, help="how many to search")
    ap.add_argument("--slowest", type=int, default=1, help="how many to report")
    ap.add_argument("--nudged", action="store_true", help="only guard-nudged turns")
    ap.add_argument("--content", action="store_true", help="full prompts and replies")
    ap.add_argument(
        "--width", type=int, default=4000, help="chars per field (0 = unlimited)"
    )
    args = ap.parse_args()
    width = None if args.width == 0 else args.width

    if args.trace:
        report(args.trace, content=args.content, width=width)
        return

    rows = _api(f"/api/public/traces?limit={args.limit}").get("data", [])
    cand = [t for t in rows if isinstance(t.get("latency"), (int, float))]
    if args.nudged:
        keep = []
        for t in cand:
            if NUDGE_MARK in json.dumps(_api(f"/api/public/traces/{t['id']}")):
                keep.append(t)
        cand = keep
        print(f"turns where the guard fired: {len(cand)}\n")
    if not cand:
        sys.exit("No matching traces found.")
    cand.sort(key=lambda t: t["latency"], reverse=True)

    print(f"slowest {min(args.slowest, len(cand))} of {len(cand)} turns:\n")
    for t in cand[: args.slowest]:
        report(t["id"], content=args.content, width=width)
        print()


if __name__ == "__main__":
    main()
