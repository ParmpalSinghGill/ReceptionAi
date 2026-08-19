"""The tracing seam: optionality, layering, and the capture ceiling.

Three properties are asserted here, all of which are easy to break by accident
and none of which a passing feature test would notice:

1. **Langfuse is optional.** No key configured must mean a working platform,
   not a degraded one. This is the property that lets the stack run on a clean
   clone.
2. **`packages/ai` never imports the vendor SDK.** `tools/check_layering.py`
   enforces it in CI; asserted here too so it fails in a unit run rather than
   only in a lint job someone can skip.
3. **The capture ceiling holds.** A tenant row cannot talk the platform into
   recording more than the deployment allows.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

from packages.core.ports.tracing import (
    Capture,
    GenerationUsage,
    NoOpTracer,
    TracingPolicy,
    TracingPort,
    TurnContext,
)
from packages.observability.pricing import estimate_cost_usd
from packages.observability.tracing import get_tracer, resolve_policy, set_tracer

REPO = Path(__file__).resolve().parents[2]


# ── Optionality ───────────────────────────────────────────────────────────


def test_no_keys_yields_a_working_noop_tracer() -> None:
    from packages.providers.observability import build_langfuse_tracer

    tracer = build_langfuse_tracer(public_key=None, secret_key=None, host=None)
    assert isinstance(tracer, NoOpTracer)
    assert tracer.enabled is False


def test_disabled_yields_a_noop_even_with_keys() -> None:
    from packages.providers.observability import build_langfuse_tracer

    tracer = build_langfuse_tracer(
        public_key="pk", secret_key="sk", host="http://x", enabled=False
    )
    assert isinstance(tracer, NoOpTracer)


def test_set_tracer_none_restores_the_noop() -> None:
    """The reset path, which is also how a test isolates itself.

    Deliberately not asserting on the tracer's *initial* value: the module
    global is process-wide, so any test that installed one would make that
    assertion order-dependent. What matters is that resetting works.
    """
    set_tracer(None)
    assert isinstance(get_tracer(), NoOpTracer)
    assert get_tracer().enabled is False


def test_the_noop_satisfies_the_port() -> None:
    assert isinstance(NoOpTracer(), TracingPort)


def test_a_full_turn_runs_against_the_noop_without_raising() -> None:
    """The shape every instrumented call site uses, end to end."""
    tracer = NoOpTracer()
    context = TurnContext(
        tenant_id=uuid4(), conversation_id=uuid4(), turn_id=uuid4()
    )
    with tracer.start_turn(context) as turn:
        turn.set_attribute("tenant.id", str(context.tenant_id))
        with tracer.start_node("ai.node.triage", agent_key="triage") as node:
            node.set_attribute("ai.node.kind", "llm_agent")
            with tracer.start_generation(
                "llm.chat", model="deepseek-chat", provider="deepseek"
            ) as gen:
                gen.set_usage(GenerationUsage(prompt_tokens=10, completion_tokens=5))
            with tracer.start_tool("ai.tool.book", agent_key="scheduler") as tool:
                tool.set_error("business_error")
    tracer.flush()
    tracer.shutdown()


# ── Layering ──────────────────────────────────────────────────────────────


def test_the_ai_package_never_imports_langfuse() -> None:
    """The seam, asserted structurally rather than by convention.

    If `packages/ai` imports the SDK, `TracingPort` is decoration and swapping
    the backend stops being a config change.
    """
    offenders: list[str] = []
    for path in (REPO / "packages" / "ai").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] == "langfuse" for n in names):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, f"packages/ai imports langfuse at {offenders}"


def test_core_does_not_import_the_observability_package() -> None:
    """`packages.core` is a leaf (contract §9). The port must stand alone."""
    tree = ast.parse(
        (REPO / "packages" / "core" / "ports" / "tracing.py").read_text("utf-8")
    )
    for node in ast.walk(tree):
        module = ""
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        assert not module.startswith("packages.observability"), module
        assert not module.startswith("packages.providers"), module


# ── The capture ceiling ───────────────────────────────────────────────────


def test_a_tenant_cannot_exceed_the_deployment_ceiling() -> None:
    """The control that makes `LANGFUSE_CAPTURE=masked` actually binding.

    Without this, a tenant_settings row — restorable from any old backup —
    would be enough to turn on full prompt capture in an environment whose
    operators had deliberately disabled it.
    """
    assert resolve_policy("full", ceiling="masked").capture is Capture.MASKED
    assert resolve_policy("full", ceiling="none").capture is Capture.NONE
    assert resolve_policy("masked", ceiling="none").capture is Capture.NONE


def test_a_tenant_may_restrict_below_the_ceiling() -> None:
    assert resolve_policy("none", ceiling="full").capture is Capture.NONE
    assert resolve_policy("masked", ceiling="full").capture is Capture.MASKED


def test_opting_in_requires_both_sides() -> None:
    assert resolve_policy("full", ceiling="full").capture is Capture.FULL


def test_an_unset_or_invalid_tenant_value_masks() -> None:
    assert resolve_policy(None, ceiling="full").capture is Capture.MASKED
    assert resolve_policy("", ceiling="full").capture is Capture.MASKED
    assert resolve_policy("yes-please", ceiling="full").capture is Capture.MASKED


def test_the_policy_default_is_masked() -> None:
    assert TracingPolicy().capture is Capture.MASKED
    context = TurnContext(
        tenant_id=uuid4(), conversation_id=uuid4(), turn_id=uuid4()
    )
    assert context.policy.capture is Capture.MASKED


# ── Cost ──────────────────────────────────────────────────────────────────


def test_an_unknown_model_costs_none_not_zero() -> None:
    """`None` means unknown. `0.0` would be a false claim that it was free."""
    usage = GenerationUsage(prompt_tokens=1000, completion_tokens=500)
    assert estimate_cost_usd("some-model-nobody-priced", usage) is None


def test_cached_tokens_are_not_billed_twice() -> None:
    """Providers report cached tokens INSIDE the prompt total.

    Pricing the full prompt count at the fresh rate and then adding the cached
    count again would overstate every cached turn — precisely the turns prompt
    caching exists to make cheaper.
    """
    all_fresh = GenerationUsage(prompt_tokens=1000, completion_tokens=0)
    mostly_cached = GenerationUsage(
        prompt_tokens=1000, completion_tokens=0, cached_prompt_tokens=900
    )
    assert estimate_cost_usd("deepseek-chat", mostly_cached) < estimate_cost_usd(
        "deepseek-chat", all_fresh
    )


def test_pricing_matches_a_hand_computed_figure() -> None:
    """1M fresh input tokens on deepseek-chat is the table's input rate."""
    usage = GenerationUsage(prompt_tokens=1_000_000, completion_tokens=0)
    assert estimate_cost_usd("deepseek-chat", usage) == 0.27


def test_a_dated_model_variant_resolves_by_prefix() -> None:
    usage = GenerationUsage(prompt_tokens=1_000_000, completion_tokens=0)
    assert estimate_cost_usd("deepseek-chat-2026-01-01", usage) == 0.27
