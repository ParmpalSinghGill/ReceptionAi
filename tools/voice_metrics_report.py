#!/usr/bin/env python3
"""Summarise voice turn-detection metrics by configuration, ready for a paper.

    ~/.receptionai-venv/bin/python tools/voice_metrics_report.py
    ~/.receptionai-venv/bin/python tools/voice_metrics_report.py --latex > table.tex
    ~/.receptionai-venv/bin/python tools/voice_metrics_report.py --csv summary.csv

Reads ``turns.csv`` written by the media worker (``$VOICE_METRICS_DIR``, default
``var/voice_metrics``) — one row per agent turn, keyed by ``call_id``,
``chat_id`` and ``turn_index`` — and groups it by ``config_label`` (detector,
plus ``+spec`` when speculation was on).

Every latency is in milliseconds from the caller's last word (Silero VAD's last
speech before the turn was committed), so the columns are comparable across
detectors. The headline number for a voice agent is ``first_audio_ms``: last
word to the first sound the caller hears back.

The same file loads straight into pandas for anything not printed here::

    import pandas as pd
    df = pd.read_csv("var/voice_metrics/turns.csv")
    df.groupby("config_label")["first_audio_ms"].describe()

Standard library only, so it runs without pandas installed.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT = Path(os.getenv("VOICE_METRICS_DIR", "var/voice_metrics")) / "turns.csv"

#: (column, label) — the latency columns summarised as median / p90.
LATENCIES: tuple[tuple[str, str], ...] = (
    ("endpoint_ms", "End-of-turn decision"),
    ("stt_final_ms", "Final transcript"),
    ("first_token_ms", "First LLM token"),
    ("first_reply_token_ms", "First reply token"),
    ("generation_end_ms", "Generation complete"),
    ("first_audio_ms", "First audio (voice-to-voice)"),
    ("reply_audio_ms", "Reply audio starts"),
)


def _num(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return None if math.isnan(out) else out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _rate(rows: list[dict[str, str]], column: str) -> float | None:
    vals = [r.get(column) for r in rows if r.get(column) not in (None, "")]
    if not vals:
        return None
    return sum(1 for v in vals if v in ("1", "True", "true")) / len(vals)


def summarise(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row.get("config_label") or "?"].append(row)

    out: list[dict[str, object]] = []
    for label in sorted(groups):
        g = groups[label]
        summary: dict[str, object] = {
            "config": label,
            "calls": len({r["call_id"] for r in g}),
            "turns": len(g),
        }
        for column, _ in LATENCIES:
            values = [v for v in (_num(r.get(column, "")) for r in g) if v is not None]
            summary[f"{column}_n"] = len(values)
            summary[f"{column}_p50"] = statistics.median(values) if values else None
            summary[f"{column}_p90"] = _pct(values, 0.9)
        summary["early_resume_rate"] = _rate(g, "user_resumed_before_reply")
        summary["superseded_rate"] = _rate(g, "superseded")
        summary["barge_in_rate"] = _rate(g, "barge_in")
        spec = [r for r in g if (r.get("spec_result") or "none") != "none"]
        hits = [r for r in spec if r.get("spec_result") == "hit"]
        summary["spec_hit_rate"] = (len(hits) / len(spec)) if spec else None
        saved = [v for v in (_num(r.get("spec_saved_ms", "")) for r in hits) if v is not None]
        summary["spec_saved_p50"] = statistics.median(saved) if saved else None
        out.append(summary)
    return out


def _fmt(value: object, *, pct: bool = False) -> str:
    if value is None:
        return "-"
    if pct:
        return f"{float(value) * 100:.0f}%"  # type: ignore[arg-type]
    if isinstance(value, float):
        return f"{value:.0f}"
    return str(value)


def print_text(summary: list[dict[str, object]]) -> None:
    for s in summary:
        print(f"\n== {s['config']}  ({s['calls']} calls, {s['turns']} turns)")
        print(f"   {'metric':<32}{'n':>5}{'p50 ms':>10}{'p90 ms':>10}")
        for column, label in LATENCIES:
            print(
                f"   {label:<32}{s[f'{column}_n']!s:>5}"
                f"{_fmt(s[f'{column}_p50']):>10}{_fmt(s[f'{column}_p90']):>10}"
            )
        print(
            f"   caller resumed before reply: {_fmt(s['early_resume_rate'], pct=True)}"
            f"   superseded: {_fmt(s['superseded_rate'], pct=True)}"
            f"   barge-in: {_fmt(s['barge_in_rate'], pct=True)}"
        )
        if s["spec_hit_rate"] is not None:
            print(
                f"   speculation hit rate: {_fmt(s['spec_hit_rate'], pct=True)}"
                f"   median saved: {_fmt(s['spec_saved_p50'])} ms"
            )


def print_latex(summary: list[dict[str, object]]) -> None:
    cols = ["endpoint_ms", "first_reply_token_ms", "first_audio_ms"]
    print("\\begin{tabular}{lrrrrrrrr}")
    print("\\hline")
    print(
        "Method & Turns & Endpoint p50 & p90 & First token p50 & p90 & "
        "First audio p50 & p90 & Early resume \\\\"
    )
    print("\\hline")
    for s in summary:
        cells = [str(s["config"]).replace("_", "\\_"), str(s["turns"])]
        for c in cols:
            cells += [_fmt(s[f"{c}_p50"]), _fmt(s[f"{c}_p90"])]
        cells.append(_fmt(s["early_resume_rate"], pct=True).replace("%", "\\%"))
        print(" & ".join(cells) + " \\\\")
    print("\\hline")
    print("\\end{tabular}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT)
    parser.add_argument("--latex", action="store_true", help="print a LaTeX table")
    parser.add_argument("--csv", type=Path, help="write the summary as CSV")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"No metrics yet at {args.path}. Make a voice call first.", file=sys.stderr)
        return 1
    with args.path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print(f"{args.path} has no rows.", file=sys.stderr)
        return 1

    summary = summarise(rows)
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
        print(f"wrote {args.csv}", file=sys.stderr)
    if args.latex:
        print_latex(summary)
    else:
        print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
