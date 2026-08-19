"""The ``VerticalManifest`` schema — contract §4, 03 §2.

A vertical is **data**. This module is the schema that data is validated
against, expressed in Pydantic v2 because contract §1 makes Pydantic the typed
boundary at every seam and because a Pydantic model *is* a JSON Schema (it
emits one via ``model_json_schema()``), so there is one definition rather than
a Python class and a hand-maintained ``.json`` alongside it that drift.

**Why validation is strict (``extra="forbid"``) everywhere.** A manifest is
tenant-authored. A typo'd key that is silently ignored produces an agent that
does not do what its author read on the screen — ``handoff:`` instead of
``handoffs:`` yields an agent that can never transfer, with no error anywhere.
Rejecting the unknown key at save time puts the error in front of the person
who can fix it.

The graph half of this schema is 03 §2 verbatim in structure. What this module
adds around it is the rest of the manifest contract §4 enumerates —
``vocabulary``, ``policies``, ``greeting`` — restricted to the Phase 1 slice
that the engine actually consumes. Fields no subsystem reads yet (``entities``,
``workflows``, ``forms``) are deliberately absent rather than declared and
unused: an unread field is a promise the platform has not made.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

from packages.core.errors import ValidationError as DomainValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "AgentSpec",
    "GraphSpec",
    "NodeKind",
    "ToolGrant",
    "VerticalManifest",
    "VocabularySpec",
    "load_manifest",
    "load_manifest_file",
]

#: The schema version a manifest declares. Bumped when a change is not
#: backward-compatible; a manifest declaring an unknown version is refused
#: rather than best-effort parsed.
MANIFEST_SCHEMA_VERSION: Final = "1"


class NodeKind(StrEnum):
    """The execution primitives a manifest may compose — 03 §2.

    A closed enum on purpose: a manifest can only instantiate factories the
    platform ships. A new kind is a platform release, not a tenant edit. This
    is contract §4's "never tenant-supplied Python" expressed as a type.
    """

    SUPERVISOR = "supervisor"
    LLM_AGENT = "llm_agent"
    TERMINAL = "terminal"


class ToolGrant(BaseModel):
    """One agent's permission to call one registered tool — 03 §2.

    **This is the entire authorization surface for tools** (contract §8): the
    model asking for a tool is not authorization, the grant is. Amendment A6
    confirms this holds even in realtime voice, where the provider decides when
    to call but never executes.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    #: Argument values pinned by the manifest, which the model may not choose.
    bound_args: dict[str, Any] = Field(default_factory=dict)
    #: A JSON-Schema fragment intersected with the tool's own schema. Can only
    #: narrow, never widen — checked at compile time by the node factory.
    arg_constraints: dict[str, Any] | None = None
    #: Human-in-the-loop before the side effect. Drives LangGraph's
    #: `interrupt_before`, derived from the manifest rather than hardcoded.
    requires_confirmation: bool = False
    #: Shown to the end user on a `tool.started` event. Tenant-authored, so it
    #: is already vertical-appropriate — 06 §3.2 forbids sending the raw tool
    #: key or its arguments to a widget audience.
    label: str = "Working…"


class AgentSpec(BaseModel):
    """One node in the graph — 03 §2."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    kind: NodeKind
    display_name: str = Field(min_length=1, max_length=80)
    role_prompt: str = Field(max_length=8_000)

    tools: list[ToolGrant] = Field(default_factory=list)
    handoffs: list[str] = Field(default_factory=list)
    knowledge_collections: list[str] = Field(default_factory=list)

    #: A model *class*, not a model name. The LLM router resolves it per tenant,
    #: which is what makes swapping providers a config change (contract §5).
    model_class: Literal["router", "fast", "balanced", "deep"] = "balanced"
    temperature: float = Field(default=0.2, ge=0.0, le=1.5)
    max_consecutive_turns: int = Field(default=6, ge=1, le=20)

    #: SUPERVISOR only: the ordered specialists it may dispatch to.
    members: list[str] = Field(default_factory=list)
    #: TERMINAL only: the Outcome written to the Conversation (contract §7).
    outcome: Literal["resolved", "booked", "transferred", "abandoned"] | None = None


class GraphSpec(BaseModel):
    """The agent graph — 03 §2, including its integrity validator."""

    model_config = ConfigDict(extra="forbid")

    entrypoint: str
    agents: list[AgentSpec] = Field(min_length=1)
    max_steps: int = Field(default=24, ge=4, le=60)
    max_tool_calls: int = Field(default=16, ge=1, le=40)
    wall_clock_budget_ms: int = Field(default=45_000, ge=5_000, le=120_000)

    @model_validator(mode="after")
    def _validate_graph_integrity(self) -> GraphSpec:
        """03 §2's validator, verbatim in intent.

        Runs at manifest-save time, where a human is looking at the error. The
        compiler re-runs it as a defence-in-depth assertion, but the authoring
        path is where the message is useful — a bad manifest should fail in the
        admin UI, not at 2am on an inbound call.
        """
        keys = {a.key for a in self.agents}
        if len(keys) != len(self.agents):
            raise ValueError("duplicate agent keys")
        if self.entrypoint not in keys:
            raise ValueError(f"entrypoint {self.entrypoint!r} is not a declared agent")

        terminals = [a for a in self.agents if a.kind is NodeKind.TERMINAL]
        if not terminals:
            raise ValueError("graph must declare at least one terminal agent")
        for terminal in terminals:
            if terminal.outcome is None:
                raise ValueError(
                    f"terminal agent {terminal.key!r} must declare an outcome"
                )
            if terminal.tools or terminal.handoffs:
                raise ValueError(
                    f"terminal agent {terminal.key!r} may not have tools or handoffs"
                )

        for agent in self.agents:
            for target in agent.handoffs:
                if target not in keys:
                    raise ValueError(
                        f"agent {agent.key!r} hands off to unknown {target!r}"
                    )
            if agent.kind is NodeKind.SUPERVISOR:
                if not agent.members:
                    raise ValueError(f"supervisor {agent.key!r} has no members")
                for member in agent.members:
                    if member not in keys:
                        raise ValueError(
                            f"supervisor {agent.key!r} lists unknown member {member!r}"
                        )
            elif agent.members:
                raise ValueError(
                    f"non-supervisor {agent.key!r} may not declare members"
                )
            if agent.kind is not NodeKind.TERMINAL and agent.outcome is not None:
                raise ValueError(
                    f"non-terminal agent {agent.key!r} may not declare an outcome"
                )

        # Reachability. An unreachable agent is an authoring bug that would
        # otherwise silently never fire in production.
        adjacency = {a.key: set(a.handoffs) | set(a.members) for a in self.agents}
        seen: set[str] = set()
        stack = [self.entrypoint]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency[current] - seen)
        if unreachable := keys - seen:
            raise ValueError(f"unreachable agents: {sorted(unreachable)}")
        if not any(t.key in seen for t in terminals):
            raise ValueError("no terminal agent reachable from entrypoint")
        return self

    def agent(self, key: str) -> AgentSpec:
        for candidate in self.agents:
            if candidate.key == key:
                return candidate
        raise KeyError(key)


class VocabularySpec(BaseModel):
    """Domain terms surfaced in prompts and UI — contract §4.

    This is what makes one manifest a dental practice and another a restaurant
    without a line of code differing. ``appointment`` versus ``reservation``,
    ``patient`` versus ``guest``: the words the agent uses come from here.
    """

    model_config = ConfigDict(extra="forbid")

    business_kind: str = Field(min_length=1, max_length=64)
    customer_term: str = "customer"
    customer_term_plural: str = "customers"
    appointment_term: str = "appointment"
    appointment_term_plural: str = "appointments"
    staff_term: str = "team member"
    resource_term: str = "room"
    service_term: str = "service"
    #: Free-form terms a prompt template may interpolate.
    extra: dict[str, str] = Field(default_factory=dict)


class PolicySpec(BaseModel):
    """Booking, cancellation, and required-information rules — contract §4.

    Rendered into the vertical prompt layer (03 §7.1 layer 3). Semi-trusted:
    authored by a paying customer, so not adversarial by default, but it cannot
    override the platform frame.
    """

    model_config = ConfigDict(extra="forbid")

    cancellation_notice_hours: int = Field(default=24, ge=0, le=720)
    requires_deposit: bool = False
    #: How many days ahead to look for an appointment the caller already has,
    #: before making another one for the same phone number.
    #:
    #: A dental practice and a restaurant want very different answers: seeing
    #: the same patient twice in three days is nearly always a mistaken repeat
    #: booking, while a restaurant may legitimately take three reservations in a
    #: week from one number. Zero disables the check entirely, which is the
    #: right setting for a business where repeat bookings are the norm.
    #:
    #: This is a prompt for a QUESTION, never a block — see
    #: `BookingService.existing_for_phone`. Someone with a broken tooth on
    #: Tuesday and a hygienist visit on Thursday is normal, and refusing them
    #: would be worse than the duplicate it prevents.
    duplicate_check_days: int = Field(default=3, ge=0, le=90)
    #: Facts the agent must collect before a booking is valid.
    required_customer_fields: list[str] = Field(default_factory=list)
    #: Statements appended verbatim to the vertical prompt layer.
    statements: list[str] = Field(default_factory=list)
    #: Phrases that must trigger immediate human escalation regardless of what
    #: the model concludes. A safety floor the model cannot reason past.
    escalation_triggers: list[str] = Field(default_factory=list)


class VerticalManifest(BaseModel):
    """A complete vertical — contract §4.

    Versioned by ``version``, which is half of the compiled-graph cache key
    (03 §4.4). Publishing a new version therefore cannot mutate an in-flight
    conversation's graph: a call that started on ``1.0.0`` finishes on
    ``1.0.0`` even if ``1.1.0`` ships mid-call.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = MANIFEST_SCHEMA_VERSION
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = ""

    vocabulary: VocabularySpec
    policies: PolicySpec = Field(default_factory=PolicySpec)
    graph: GraphSpec

    #: The first thing the customer sees or hears. Returned verbatim by
    #: `POST /api/v1/public/demo/sessions` (PHASE1-API-CONTRACT).
    greeting: str = Field(min_length=1, max_length=500)

    #: Tenant-substitutable placeholders. `{{ business_name }}` in a role
    #: prompt is filled from tenant settings; anything not listed here is left
    #: literal rather than resolved, so a prompt cannot reach for an arbitrary
    #: attribute of our configuration by naming it.
    template_variables: list[str] = Field(
        default_factory=lambda: ["business_name", "location_name", "timezone"]
    )

    @model_validator(mode="after")
    def _validate_schema_version(self) -> VerticalManifest:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"manifest schema_version {self.schema_version!r} is not "
                f"{MANIFEST_SCHEMA_VERSION!r}; refusing to best-effort parse a "
                f"schema this build does not know"
            )
        return self

    @property
    def cache_version(self) -> str:
        """The string that identifies this manifest in the graph cache.

        Includes the key as well as the version so two verticals at ``1.0.0``
        cannot collide in a cache keyed by ``(tenant_id, manifest_version)``.
        """
        return f"{self.key}@{self.version}"


def load_manifest(payload: dict[str, Any]) -> VerticalManifest:
    """Validate a decoded manifest document.

    Raises:
        packages.core.errors.ValidationError: with a flattened, per-field
            message. Pydantic's own ``ValidationError`` is re-raised as the
            domain error so the API layer renders it as RFC 9457 through the
            single handler it already has, rather than needing a second one
            that knows about Pydantic.
    """
    try:
        return VerticalManifest.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise DomainValidationError(
            f"Invalid vertical manifest: {details}", field="manifest"
        ) from exc


def load_manifest_file(path: str | Path) -> VerticalManifest:
    """Read and validate a manifest from disk."""
    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DomainValidationError(
            f"No manifest at {resolved}", field="manifest"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DomainValidationError(
            f"Manifest at {resolved} is not valid JSON: {exc}", field="manifest"
        ) from exc
    return load_manifest(payload)
