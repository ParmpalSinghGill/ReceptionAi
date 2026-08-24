#!/usr/bin/env python3
"""Is a slow `llm.chat` span one slow request, or several retried ones?

    python tools/probe_llm_latency.py                 # 5 calls, same prompt
    python tools/probe_llm_latency.py -n 20           # more samples
    python tools/probe_llm_latency.py --big           # ~12k prompt, like a real turn
    python tools/probe_llm_latency.py --raw           # bypass the adapter entirely

WHY THIS EXISTS
---------------
A Langfuse `llm.chat` span is opened in `packages/ai/nodes.py` and closed after
`OpenAICompatibleLLMAdapter.chat` returns. Inside that call, `_post_json` retries
up to `max_attempts` (3) with exponential backoff. So ONE span can contain three
HTTP requests and two sleeps, and a trace showing "23.25s, one call" cannot be
read as "the provider took 23 seconds to answer once".

This measures the two layers separately:

  HTTP    — one raw request/response, timed here, no retry, no adapter
  ADAPTER — the same call through the adapter, which may retry inside

If ADAPTER >> HTTP, the span is hiding retries and the platform is the cause.
If they track each other, the provider really is that slow and variable.

Reads DEEPSEEK_API_KEY / LLM_* from `.env`. Sends real requests and spends real
tokens: keep -n small.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_env() -> None:
    env = REPO / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _prompt(big: bool) -> list[dict[str, str]]:
    """A small prompt, or one the size a real booking turn sends (~12k chars)."""
    system = "You are a receptionist at a dental practice. Reply in one sentence."
    if big:
        filler = (
            "Practice policy note. Appointments run to time; a late arrival may "
            "be asked to rebook. Emergency slots are held each weekday afternoon. "
        )
        system = system + "\n\n" + (filler * 90)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Do you have anything on Tuesday morning?"},
    ]


async def _raw_http(messages: list[dict[str, str]], model: str) -> tuple[float, str]:
    """One request, no retry, no adapter. The provider's honest latency."""
    import httpx

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    body = {"model": model, "messages": messages, "temperature": 0.2}
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
    elapsed = time.perf_counter() - started
    if response.status_code >= 400:
        return elapsed, f"HTTP {response.status_code}"
    data = response.json()
    usage = data.get("usage") or {}
    return elapsed, f"ok out={usage.get('completion_tokens', '?')}"


async def _via_adapter(messages: list[dict[str, str]], model: str) -> tuple[float, str]:
    """The same call through the adapter — retries included, as production runs it."""
    from packages.core.ports.llm import Message
    from packages.providers.llm.deepseek_adapter import DeepSeekLLMAdapter

    adapter = DeepSeekLLMAdapter(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""), model=model
    )
    msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
    started = time.perf_counter()
    try:
        result = await adapter.chat(messages=msgs)
    except Exception as exc:
        return time.perf_counter() - started, f"{type(exc).__name__}"
    elapsed = time.perf_counter() - started
    out = (result.usage.completion_tokens if result.usage else None) or "?"
    return elapsed, f"ok out={out}"


def _summarise(label: str, samples: list[float]) -> None:
    if not samples:
        print(f"{label}: no samples")
        return
    s = sorted(samples)
    p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
    print(
        f"{label:<9} n={len(s):<3} min={s[0]:6.2f}s  median={statistics.median(s):6.2f}s"
        f"  p90={p90:6.2f}s  max={s[-1]:6.2f}s  spread={s[-1]/s[0]:5.1f}x"
    )


async def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=5, help="samples per layer")
    ap.add_argument("--big", action="store_true", help="~12k prompt, like a real turn")
    ap.add_argument("--raw", action="store_true", help="raw HTTP only")
    ap.add_argument("--adapter", action="store_true", help="adapter only")
    ap.add_argument("--model", default=None, help="override the model id")
    args = ap.parse_args()

    model = args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    messages = _prompt(args.big)
    chars = sum(len(m["content"]) for m in messages)
    both = not (args.raw or args.adapter)

    print(f"model      : {model}")
    print(f"prompt     : {chars} chars ({'big, like a real turn' if args.big else 'small'})")
    print(f"samples    : {args.n} per layer")
    print("adapter    : max_attempts=3, timeout_ms=15000 (so a 45s ceiling)\n")

    raw: list[float] = []
    adapted: list[float] = []
    print(f"{'#':>3}  {'layer':<9}{'elapsed':>9}  result")
    print("-" * 52)
    for i in range(1, args.n + 1):
        if args.raw or both:
            took, note = await _raw_http(messages, model)
            raw.append(took)
            print(f"{i:>3}  {'HTTP':<9}{took:>8.2f}s  {note}")
        if args.adapter or both:
            took, note = await _via_adapter(messages, model)
            adapted.append(took)
            flag = ""
            if raw and took > max(raw) * 1.8:
                flag = "   <-- far over any raw call: retry suspected"
            print(f"{i:>3}  {'ADAPTER':<9}{took:>8.2f}s  {note}{flag}")

    print("\n" + "=" * 52)
    _summarise("HTTP", raw)
    _summarise("ADAPTER", adapted)

    if raw and adapted:
        rm, am = statistics.median(raw), statistics.median(adapted)
        print(f"\nmedian adapter / median http = {am/rm:.2f}x")
        print(
            "  ~1x  -> the span is one request; slow spans are the provider.\n"
            "  >2x  -> the adapter is retrying inside one span, and a Langfuse\n"
            "          'one call, 23s' is really several requests plus backoff."
        )
    print(
        "\nNote: a Langfuse llm.chat span covers the whole adapter call, retries\n"
        "included. Compare its duration against the HTTP row above, not against\n"
        "an assumption that one span means one request."
    )


if __name__ == "__main__":
    asyncio.run(main())
