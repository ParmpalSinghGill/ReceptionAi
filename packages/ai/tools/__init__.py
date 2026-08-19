"""Tool registration, grant enforcement, and execution — 03 §6.

Three modules, one boundary:

* :mod:`packages.ai.tools.registry` — the catalogue. A manifest may only grant a
  tool that is registered here.
* :mod:`packages.ai.tools.executor` — the chokepoint. **Nothing in this package
  calls a tool except through** :meth:`ToolExecutor.execute_many`, which runs the
  grant check, the argument validation and the timeout.
* :mod:`packages.ai.tools.builtin` — the first-party implementations.

Re-exported here so the existing call sites (``from packages.ai.tools import
default_registry``) keep working — this was a single module before the executor
needed a home, and moving it must not be a breaking change for ``apps/api``.
"""

from __future__ import annotations

from packages.ai.tools.executor import ToolExecutor, ToolOutcome
from packages.ai.tools.registry import (
    ToolContext,
    ToolExecutionContext,
    ToolImpl,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    default_registry,
    object_schema,
)

__all__ = [
    "ToolContext",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolImpl",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "default_registry",
    "object_schema",
]
