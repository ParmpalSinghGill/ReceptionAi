"""Observability — OTel setup, structured logging, metrics, LLM tracing.

Contract §9 gives this package to observability concerns shared across the
platform. It imports ``packages.core`` and nothing else first-party, and it
never imports a vendor SDK: the Langfuse client lives in
``packages/providers/observability`` behind ``TracingPort``, which is what lets
the AI layer be instrumented without the AI layer knowing what a Langfuse trace
is.

Import the helpers from :mod:`packages.observability.tracing`; the names are
re-exported here for convenience.
"""

from __future__ import annotations

from packages.observability.pricing import estimate_cost_usd
from packages.observability.redaction import mask_content, scrub, summarize
from packages.observability.tracing import (
    get_tracer,
    graph_node,
    llm_generation,
    record_tool_outcome,
    set_tracer,
    tool_call,
    turn_trace,
    usage_from_port,
)

__all__ = [
    "estimate_cost_usd",
    "get_tracer",
    "graph_node",
    "llm_generation",
    "mask_content",
    "record_tool_outcome",
    "scrub",
    "set_tracer",
    "summarize",
    "tool_call",
    "turn_trace",
    "usage_from_port",
]
