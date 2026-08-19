"""Manifest → LangGraph — 03 §4.2.

The structure produced, for any manifest::

    START ──► __guard ──┬──► <entrypoint>
                        ├──► <any agent>
                        ├──► __overflow ──► END
                        └──► END

    <every non-terminal agent> ──► __guard
    <every terminal agent>     ──► END

**No agent is hardcoded.** The node set, the edges, the routing table and the
interrupt points are all derived from the manifest. A dental manifest and a
restaurant manifest differ only in the JSON.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from langgraph.graph import END, START, StateGraph
from packages.ai.limits import CEILINGS
from packages.ai.nodes import NodeFactory
from packages.ai.routing import build_router, guard_node
from packages.ai.state import ConversationState
from packages.core.errors import ConfigurationError
from packages.verticals.manifest import AgentSpec, GraphSpec, NodeKind, VerticalManifest

__all__ = ["CompiledGraph", "GraphCompiler", "ManifestCompilationError"]

log = structlog.get_logger(__name__)

GUARD = "__guard"
OVERFLOW = "__overflow"


class ManifestCompilationError(ConfigurationError):
    """A validated manifest could not be compiled into a runnable graph.

    A :class:`~packages.core.errors.ConfigurationError` (500, ``configuration_error``)
    rather than a ``ValidationError`` (422): by the time compilation runs, the
    manifest has already passed the schema. Reaching here means the platform
    and the stored manifest disagree — a tool was removed in a release, or a
    ceiling was lowered — which is our problem, not the caller's.
    """


@dataclass(frozen=True, slots=True)
class CompiledGraph:
    """A runnable graph plus the clamped budgets to seed a run with."""

    graph: object  # langgraph CompiledStateGraph; typed loosely to avoid the
    # framework's generic parameters leaking into every caller signature.
    spec: GraphSpec
    manifest: VerticalManifest
    manifest_version: str
    max_steps: int
    max_tool_calls: int
    wall_clock_budget_ms: int


class GraphCompiler:
    """Turns manifest data into a runnable LangGraph."""

    def __init__(self, factory: NodeFactory, *, checkpointer: object | None = None) -> None:
        """
        Args:
            checkpointer: A LangGraph checkpointer, or ``None``. Governs
                whether manifest-declared confirmation interrupts are installed
                — see :meth:`compile`. Typed as ``object`` so this module does
                not depend on which checkpointer implementation 03 §5.3 lands.
        """
        self._factory = factory
        self._checkpointer = checkpointer

    def compile(self, manifest: VerticalManifest) -> CompiledGraph:
        spec = manifest.graph
        self._preflight(spec)

        builder = StateGraph(ConversationState)

        for agent in spec.agents:
            match agent.kind:
                case NodeKind.SUPERVISOR:
                    node = self._factory.supervisor(agent, spec)
                case NodeKind.LLM_AGENT:
                    node = self._factory.llm_agent(agent, spec)
                case NodeKind.TERMINAL:
                    node = self._factory.terminal(agent, spec)
                case _:  # pragma: no cover - the enum is closed
                    raise ManifestCompilationError(
                        f"unknown node kind {agent.kind!r}"
                    )
            builder.add_node(agent.key, node)

        builder.add_node(GUARD, guard_node(spec))
        builder.add_node(OVERFLOW, self._factory.terminal(_overflow_spec(spec), spec))

        builder.add_edge(START, GUARD)
        for agent in spec.agents:
            if agent.kind is NodeKind.TERMINAL:
                builder.add_edge(agent.key, END)
            else:
                builder.add_edge(agent.key, GUARD)
        builder.add_edge(OVERFLOW, END)

        builder.add_conditional_edges(
            GUARD,
            build_router(spec, overflow=OVERFLOW),
            # An explicit path map, so LangGraph validates every target at
            # compile time. Without it a routing bug is a runtime KeyError on a
            # live call; with it, it is a compile error on a deploy.
            {a.key: a.key for a in spec.agents} | {OVERFLOW: OVERFLOW, END: END},
        )

        # ── Human-in-the-loop interrupts, and why they are conditional ────
        #
        # `interrupt_before` is how LangGraph expresses "stop and wait for a
        # human" (03 §4.2), and the node list is manifest-derived rather than
        # hardcoded — a vertical wanting manager approval on refunds declares
        # it via `requires_confirmation`.
        #
        # But an interrupt is only *resumable* with a checkpointer: the run
        # halts, and without persisted state there is nothing to resume from.
        # Compiling with interrupts and no checkpointer produces a graph that
        # stops at the first confirmable node and silently returns a state with
        # no assistant message — a conversation that just goes quiet, with no
        # error anywhere. That is precisely the failure this codebase is meant
        # not to have.
        #
        # So the interrupt is installed only when a checkpointer is. Phase 1
        # ships neither (checkpointing is 03 §5, and no tool is executable
        # yet), so the confirmation path is inert rather than half-wired.
        confirmation_nodes = self._confirmation_nodes(spec)
        compiled = builder.compile(
            checkpointer=self._checkpointer,
            interrupt_before=(
                confirmation_nodes if (self._checkpointer and confirmation_nodes) else None
            ),
        )
        if confirmation_nodes and self._checkpointer is None:
            log.info(
                "graph.compiled.confirmations_inert",
                manifest=manifest.cache_version,
                nodes=confirmation_nodes,
                reason=(
                    "no checkpointer configured; interrupt_before would halt "
                    "unresumably, so confirmation is not installed"
                ),
            )

        clamped = CompiledGraph(
            graph=compiled,
            spec=spec,
            manifest=manifest,
            manifest_version=manifest.cache_version,
            max_steps=min(spec.max_steps, CEILINGS.max_steps),
            max_tool_calls=min(spec.max_tool_calls, CEILINGS.max_tool_calls),
            wall_clock_budget_ms=min(
                spec.wall_clock_budget_ms, CEILINGS.wall_clock_budget_ms
            ),
        )
        log.info(
            "graph.compiled",
            manifest=manifest.cache_version,
            agents=len(spec.agents),
            max_steps=clamped.max_steps,
        )
        return clamped

    def _preflight(self, spec: GraphSpec) -> None:
        """Ceilings and tool grants, before a single node is built."""
        if len(spec.agents) > CEILINGS.max_agents:
            raise ManifestCompilationError(
                f"{len(spec.agents)} agents exceeds the platform ceiling of "
                f"{CEILINGS.max_agents}"
            )
        for agent in spec.agents:
            if len(agent.role_prompt) > CEILINGS.max_role_prompt_chars:
                raise ManifestCompilationError(
                    f"role_prompt for {agent.key!r} is "
                    f"{len(agent.role_prompt)} chars, over the "
                    f"{CEILINGS.max_role_prompt_chars} ceiling"
                )
            if unknown := self._factory.unknown_tools(agent):
                raise ManifestCompilationError(
                    f"agent {agent.key!r} grants unregistered tools: "
                    f"{sorted(unknown)}. Refusing to compile rather than "
                    f"silently dropping them — an agent that promises a "
                    f"capability it does not have is worse than one that will "
                    f"not start."
                )
            try:
                self._factory.validate_grants(agent)
            except ValueError as exc:
                raise ManifestCompilationError(str(exc)) from exc

    @staticmethod
    def _confirmation_nodes(spec: GraphSpec) -> list[str]:
        return [
            agent.key
            for agent in spec.agents
            if any(grant.requires_confirmation for grant in agent.tools)
        ]


def _overflow_spec(spec: GraphSpec) -> AgentSpec:
    """The terminal the guard routes to when a budget is exhausted.

    Synthesized rather than required from the manifest: every manifest would
    otherwise have to declare a near-identical overflow agent, and the one that
    forgot would leave a run with nowhere to go on a budget breach. It reuses
    the terminal factory, so its reply is generated by the same path as any
    other closing message and sounds like the rest of the conversation.

    Note the two names. The graph *node* is ``__overflow`` (double-underscore,
    matching ``__guard``), which the manifest's ``key`` pattern deliberately
    cannot express — that is what makes the reserved names unspoofable by a
    tenant. The ``AgentSpec`` therefore carries the legal key
    ``platform_overflow``, and the compiler registers it under the reserved
    node name. Only the node name is ever routed to.
    """
    _ = spec
    return AgentSpec(
        key="platform_overflow",
        kind=NodeKind.TERMINAL,
        display_name="Wrap Up",
        model_class="fast",
        outcome="transferred",
        role_prompt=(
            "This conversation has run longer than expected and needs a person. "
            "In one short sentence, apologise briefly and say you are passing "
            "them to a colleague who will pick it up. Do not ask any further "
            "questions and do not promise a specific time."
        ),
    )
