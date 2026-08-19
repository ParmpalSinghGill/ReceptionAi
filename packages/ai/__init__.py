"""``packages.ai`` — the LangGraph runtime, prompt assembly, agent registry.

Contract §4 says a vertical is data. This package is what makes that true at
runtime: it takes a validated
:class:`~packages.verticals.manifest.VerticalManifest` and produces a runnable
graph, with **no agent hardcoded anywhere**. Grepping this package for
"scheduler" or "triage" returns nothing — those live in
``packages/verticals/manifests/medical.json``, and a restaurant manifest
compiles through exactly the same code path.

Layering (contract §9): this package imports ``core`` and ``verticals``, and no
vendor SDK. It reaches a model only through
:class:`~packages.core.ports.llm.LLMPort`, so which provider is behind it is a
configuration decision made in ``packages/providers``.
"""

from __future__ import annotations

from packages.ai.cache import CompiledGraphCache
from packages.ai.compiler import CompiledGraph, GraphCompiler, ManifestCompilationError
from packages.ai.prompts import PromptAssembler, assemble_prompt
from packages.ai.runner import GraphRunner, TurnEvent
from packages.ai.state import ConversationState

__all__ = [
    "CompiledGraph",
    "CompiledGraphCache",
    "ConversationState",
    "GraphCompiler",
    "GraphRunner",
    "ManifestCompilationError",
    "PromptAssembler",
    "TurnEvent",
    "assemble_prompt",
]
