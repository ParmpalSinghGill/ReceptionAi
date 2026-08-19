"""The tool registry — 03 §6.1.

Contract §8: *"tool invocation requires an explicit grant in the vertical
manifest; no tool is reachable purely because a model asked for it."* Two things
follow, and they are separate:

* A manifest may only grant a tool that is **registered here**. A grant naming an
  unregistered tool fails compilation (03 §4.2) rather than being silently
  dropped, because an agent that promises a capability it does not have is worse
  than one that will not start.
* A registered tool is only *reachable* by an agent the manifest granted it to.
  The registry is the catalogue; the grant is the authorization. The two are
  enforced in :mod:`packages.ai.tools.executor`.

**Every registered tool's schema must set ``additionalProperties: false``**, and
:meth:`ToolRegistry.register` refuses one that does not. Strictness is mandatory
rather than advisory: a permissive schema lets a model smuggle an unvalidated
field past validation and into a tool implementation, which is the same class of
hole as an unparameterised query.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import jsonschema
from packages.core.ports.llm import ToolSchema

__all__ = [
    "ToolContext",
    "ToolExecutionContext",
    "ToolImpl",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "default_registry",
]


class ToolContext(Protocol):
    """What a tool implementation is told about its caller — 03 §6.1.

    A Protocol rather than a concrete class so a tool can be exercised in a test
    with a plain object, and so ``packages/ai`` does not force its own container
    on the implementations that live outside it.
    """

    tenant_id: UUID
    conversation_id: UUID
    agent_key: str
    correlation_id: str
    deadline_ms: int
    #: Which channel the conversation is on. A tool that behaves differently by
    #: channel needs this — an escalation can bridge a live phone call but has
    #: nobody to bridge to on web chat, and a tool that cannot tell the
    #: difference will claim a handoff that cannot happen.
    channel: str


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """The concrete :class:`ToolContext` the executor builds per call.

    ``tenant_id`` comes from the graph's state, which came from the authenticated
    request — **never from the model's arguments**. That is the last line of
    defence behind RLS (02 §6.3's checkpoint 4e): even if a model produced a
    ``tenant_id``, it never reaches a tool, because a tool is not given the
    arguments' copy, it is given this.
    """

    tenant_id: UUID
    conversation_id: UUID
    agent_key: str
    correlation_id: str
    deadline_ms: int
    channel: str = "web_chat"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returns to the graph — 03 §6.1.

    ``content`` goes to the model; ``collected_data`` goes to state. They are
    separate because a tool often knows a fact the model should not have to
    re-extract from its own prose — a hold token, a booking reference, a resolved
    booking id. Making the model retype a UUID it read in a sentence is how a
    confirm ends up pointing at the wrong row.

    ``structured`` is the machine-readable body the API surfaces alongside the
    prose, so a widget can render a slot picker rather than parsing English.
    """

    content: str
    collected_data: dict[str, Any] = field(default_factory=dict)
    structured: dict[str, Any] = field(default_factory=dict)


#: A tool implementation. Async because every real one is I/O.
ToolImpl = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One registered tool: what it is, and how it runs — 03 §6.1."""

    name: str
    description: str
    #: JSON Schema, draft 2020-12. Validated at registration.
    parameters: dict[str, Any]
    impl: ToolImpl | None = None
    #: Contract §5: timeout-bounded. Capped further by the turn's deadline.
    timeout_ms: int = 8_000
    #: Safe to run in parallel and to retry. A read.
    idempotent: bool = False
    #: Writes to a tenant system. Two parallel `book_appointment` calls against
    #: the same slot is a double-booking, and the model *will* emit them — so
    #: side-effecting tools are serialized by the executor.
    side_effecting: bool = True
    #: Amendment A3: memoize a replayed call under its idempotency key, so a turn
    #: that crashes after calling an externally-metered API does not bill the
    #: vendor twice on replay. Zero means "safe to re-execute".
    replay_cache_ttl_s: int = 0
    #: Argument paths carrying PII, masked before any log or span (contract §8).
    pii_arg_paths: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.impl is not None

    def to_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters=self.parameters
        )


def object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    """Build a strict argument schema.

    ``additionalProperties: false`` is not optional here — see the module
    docstring. Providing this helper rather than letting each tool spell it out
    means a new tool cannot forget it by omission; it would have to be removed
    deliberately, and :meth:`ToolRegistry.register` would then refuse it anyway.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


class ToolRegistry:
    """The catalogue a manifest's grants are validated against."""

    def __init__(self, tools: tuple[ToolSpec, ...] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            self.register(tool)

    def register(self, spec: ToolSpec) -> None:
        """Add a tool, refusing three specific mistakes.

        Each check exists because the failure it prevents is silent:

        1. **Duplicate name.** The second registration would shadow the first,
           and which one wins depends on import order — so a manifest granting
           ``booking.book_appointment`` could reach a different implementation
           after an unrelated refactor.
        2. **Invalid schema.** A malformed schema does not fail at registration,
           it fails at the first validation attempt, mid-call.
        3. **Permissive schema.** Without ``additionalProperties: false`` a model
           can attach a field the schema never described, and it arrives in the
           implementation's ``arguments`` dict unvalidated.
        """
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool registration: {spec.name}")
        jsonschema.Draft202012Validator.check_schema(spec.parameters)
        if spec.parameters.get("additionalProperties") is not False:
            raise ValueError(
                f"tool {spec.name}: parameters must set additionalProperties:false. "
                f"A permissive schema lets a model smuggle an unvalidated field "
                f"into the implementation."
            )
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> dict[str, ToolSchema]:
        """Every registered tool as a ``ToolSchema``, keyed by name.

        What the compiler passes to the node factory. The factory intersects it
        with each agent's grants, so an agent is only ever *shown* the tools its
        manifest granted it — an ungranted tool is not merely refused, it is
        invisible (02 §6.4). The model cannot be talked into requesting a tool it
        has never heard of.
        """
        return {name: tool.to_schema() for name, tool in self._tools.items()}

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def keys(self) -> frozenset[str]:
        """Alias for :meth:`names`, kept for the compiler's existing call site."""
        return self.names()

    def available_keys(self) -> frozenset[str]:
        """Tools that can actually execute in this build."""
        return frozenset(n for n, t in self._tools.items() if t.available)


def default_registry() -> ToolRegistry:
    """The process-wide catalogue: every first-party tool.

    Imported lazily inside the function rather than at module scope because the
    builtin modules import *this* module for :class:`ToolSpec`, and a top-level
    import would be circular. The cost is one import on first call; the benefit
    is that a tool module is a plain module rather than something with an import
    ordering constraint nobody can see.
    """
    from packages.ai.tools.builtin import booking as booking_tools
    from packages.ai.tools.builtin import comms as comms_tools

    return ToolRegistry(booking_tools.TOOLS + comms_tools.TOOLS)
