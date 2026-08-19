"""First-party tool implementations.

A tool module exports a ``TOOLS`` tuple of
:class:`~packages.ai.tools.registry.ToolSpec`, which
:func:`~packages.ai.tools.registry.default_registry` assembles. Registration is
by *collection*, not by import side effect: a module that registered itself on
import would make the catalogue depend on which modules happened to be imported,
and a tool would silently disappear when an unrelated import was removed.
"""

from __future__ import annotations

__all__: list[str] = []
