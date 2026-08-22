"""Adapter selection — contract §5's "adapters are selected from configuration".

One rule governs this module: **a missing API key falls back to the stub with a
loud log, and never crashes the process.**

That is not laxity, and the asymmetry with the rest of the platform is
deliberate. Contract §2's isolation controls fail *closed* — an unresolvable
shard refuses the session (A8), an unset tenant refuses the query. Provider
selection is an *availability* control, and 05 §B8 draws the same line for rate
limits: fail open on availability, closed on isolation. A platform that refuses
to boot without a DeepSeek key is a platform where the demo, the test suite,
and every offline developer are blocked by a third party's billing page.

"Loud" is doing real work here. The failure mode this must avoid is a
production pod silently serving canned dental responses because a secret did
not mount. So the fallback logs at ``error`` with an explicit remediation, and
:func:`build_llm_adapter` takes ``allow_stub_fallback`` which the settings model
sets to ``False`` outside local — turning the same condition into a startup
crash where a crash is the correct behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from packages.core.errors import ConfigurationError
from packages.core.ports.llm import LLMPort
from packages.core.ports.secrets import SecretValue
from packages.providers.llm.deepseek_adapter import DeepSeekLLMAdapter
from packages.providers.llm.openai_compatible import OpenAICompatibleLLMAdapter
from packages.providers.llm.stub_adapter import StubLLMAdapter

__all__ = ["LLMProviderSettings", "build_llm_adapter"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LLMProviderSettings:
    """Everything adapter selection needs, and nothing about HTTP or FastAPI.

    A plain frozen dataclass rather than the app's Pydantic settings object:
    ``packages/`` must not import from ``apps/`` (contract §9), and this keeps
    the factory callable from a Temporal worker, a CLI seed script, and a test
    without any of them constructing a web-app configuration.
    """

    provider: str = "deepseek"
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    #: Milliseconds between stub chunks. Non-zero locally so the UI exercises
    #: incremental rendering; zero in tests so the suite stays fast.
    stub_token_delay_ms: int = 0


def build_llm_adapter(
    settings: LLMProviderSettings,
    *,
    allow_stub_fallback: bool = True,
) -> LLMPort:
    """Resolve configuration to a live ``LLMPort``.

    Args:
        allow_stub_fallback: ``True`` locally and in tests. ``False`` in
            staging and production, where a missing key must crash the pod
            visibly rather than degrade into canned replies that look like a
            working product to every dashboard.

    Raises:
        ConfigurationError: when a key is absent and fallback is disallowed,
            or when the provider name is unknown. An unknown provider is always
            fatal — falling back there would mask a typo in a ConfigMap
            (``deepsek``) as "the stub is fine", and the typo would survive
            into production.
    """
    provider = (settings.provider or "").strip().lower()

    if provider in ("stub", "none", ""):
        log.info("llm.adapter.selected", provider="stub", reason="configured")
        return StubLLMAdapter(token_delay_ms=settings.stub_token_delay_ms)

    if provider not in _BUILDERS:
        raise ConfigurationError(
            f"Unknown LLM provider {settings.provider!r}. Known: "
            f"{sorted(_BUILDERS)} plus 'stub'. Not falling back — an unknown "
            f"provider name is a typo in configuration, and masking it as "
            f"'the stub is fine' is how the typo reaches production."
        )

    key = (settings.api_key or "").strip()
    if not key:
        if not allow_stub_fallback:
            raise ConfigurationError(
                f"No API key for provider {provider!r} and stub fallback is "
                f"disabled. Set the provider key, or set LLM_PROVIDER=stub "
                f"deliberately. Refusing to start rather than serving canned "
                f"responses that every dashboard would report as healthy."
            )
        log.error(
            "llm.adapter.stub_fallback",
            configured_provider=provider,
            reason="no_api_key",
            remediation=(
                f"Set the {provider.upper()}_API_KEY environment variable. "
                f"Until then every reply is a canned stub response."
            ),
        )
        return StubLLMAdapter(token_delay_ms=settings.stub_token_delay_ms)

    adapter = _BUILDERS[provider](settings, SecretValue(key))
    log.info(
        "llm.adapter.selected",
        provider=provider,
        model=getattr(adapter, "model", "unknown"),
    )
    return adapter


def _build_deepseek(
    settings: LLMProviderSettings, key: SecretValue
) -> OpenAICompatibleLLMAdapter:
    return DeepSeekLLMAdapter(
        api_key=key,
        model=settings.model or "deepseek-v4-flash",
        base_url=settings.base_url or "https://api.deepseek.com/v1",
    )


def _build_openai(
    settings: LLMProviderSettings, key: SecretValue
) -> OpenAICompatibleLLMAdapter:
    """OpenAI proper, through the same base.

    Included because it costs nothing — the wire format is identical — and
    because having a second entry proves the base is genuinely shared rather
    than a DeepSeek adapter with an abstract class drawn around it.
    """
    from packages.core.ports.llm import ProviderCapabilities

    return OpenAICompatibleLLMAdapter(
        provider="openai",
        base_url=settings.base_url or "https://api.openai.com/v1",
        api_key=key,
        model=settings.model or "gpt-4o-mini",
        capabilities=ProviderCapabilities(
            supports_tools=True,
            supports_parallel_tools=True,
            supports_streaming_tools=True,
            supports_json_schema=True,
            supports_prompt_caching=True,
            context_window=128_000,
            max_output_tokens=16_384,
        ),
    )


_BUILDERS = {
    "deepseek": _build_deepseek,
    "openai": _build_openai,
}
