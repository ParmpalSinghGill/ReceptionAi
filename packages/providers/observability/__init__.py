"""Observability adapters — the vendor side of ``TracingPort``.

One of the three packages permitted to import a vendor SDK (contract §5/§9).
``langfuse`` is imported *only* by :mod:`.langfuse_adapter`, and only inside a
function, so a clean clone without the optional dependency imports this package
without error.
"""

from __future__ import annotations

from packages.providers.observability.langfuse_adapter import (
    LangfuseTracingAdapter,
    build_langfuse_tracer,
)

__all__ = ["LangfuseTracingAdapter", "build_langfuse_tracer"]
