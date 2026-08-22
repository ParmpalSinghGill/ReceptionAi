"""``DeepSeekLLMAdapter`` — contract §10's ``<Vendor><Domain>Adapter`` naming.

DeepSeek speaks the OpenAI Chat Completions wire format, so this class is a
specialization of :class:`~packages.providers.llm.openai_compatible.OpenAICompatibleLLMAdapter`
and not a second implementation. What differs is genuinely small, and stating
it explicitly is the point of the subclass:

* ``base_url`` is ``https://api.deepseek.com/v1``.
* Four models across two generations, and they are not interchangeable.
  ``deepseek-v4-flash`` is the default and ``deepseek-v4-pro`` backs the
  ``deep`` class; the V3-era ``deepseek-chat`` and ``deepseek-reasoner`` stay
  supported so a pinned deployment keeps working. ``deepseek-reasoner`` emits
  a reasoning trace and, per DeepSeek's own documentation, **does not support
  tool calling**, so its capability record says so rather than letting a
  manifest grant tools to an agent that will have them silently dropped.
* Prompt caching is automatic and server-side; hits come back as
  ``prompt_cache_hit_tokens``, which the base adapter already reads into
  :attr:`~packages.core.ports.llm.Usage.cached_prompt_tokens`.
* Context window is per model — 1M on V4, 64k on the V3 pair — and is the
  ceiling the budget manager plans against, so it is read from the capability
  record rather than assumed.
"""

from __future__ import annotations

from typing import Final

import httpx
from packages.core.errors import ConfigurationError
from packages.core.ports.llm import ProviderCapabilities
from packages.core.ports.secrets import SecretValue
from packages.providers.llm.openai_compatible import OpenAICompatibleLLMAdapter

__all__ = ["DEEPSEEK_MODELS", "DeepSeekLLMAdapter"]

DEFAULT_BASE_URL: Final = "https://api.deepseek.com/v1"

#: Capability records per model. Not a single record with the "safest" values:
#: 03 §9.3's fallback router must know that routing a tool-granted agent to
#: ``deepseek-reasoner`` is invalid, and a merged record that claimed
#: ``supports_tools=False`` for both would needlessly disable tools on the
#: model that has them.
DEEPSEEK_MODELS: Final[dict[str, ProviderCapabilities]] = {
    # V4 generation. Checked 2026-08-22 against
    # https://api-docs.deepseek.com/quick_start/pricing — both carry a 1M
    # context window, a 384k output ceiling, and tool calling.
    #
    # The context window is 16x deepseek-chat's, which the budget manager
    # plans against; the output ceiling is larger than any turn this platform
    # produces, so it constrains nothing here.
    "deepseek-v4-flash": ProviderCapabilities(
        supports_tools=True,
        supports_parallel_tools=True,
        supports_streaming_tools=True,
        supports_json_schema=True,
        supports_prompt_caching=True,  # automatic, server-side
        context_window=1_000_000,
        max_output_tokens=384_000,
    ),
    "deepseek-v4-pro": ProviderCapabilities(
        supports_tools=True,
        supports_parallel_tools=True,
        supports_streaming_tools=True,
        supports_json_schema=True,
        supports_prompt_caching=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
    ),
    "deepseek-chat": ProviderCapabilities(
        supports_tools=True,
        supports_parallel_tools=True,
        supports_streaming_tools=True,
        supports_json_schema=True,
        supports_prompt_caching=True,  # automatic, server-side
        context_window=65_536,
        max_output_tokens=8_192,
    ),
    "deepseek-reasoner": ProviderCapabilities(
        # Documented limitation, recorded rather than discovered at runtime:
        # a request carrying `tools` to this model is rejected, and the base
        # adapter raises ConfigurationError before spending the round-trip.
        supports_tools=False,
        supports_parallel_tools=False,
        supports_streaming_tools=False,
        supports_json_schema=False,
        supports_prompt_caching=True,
        context_window=65_536,
        max_output_tokens=8_192,
    ),
}

#: 03 §2's `model_class` is what a manifest declares — an agent asks for
#: "fast" or "deep", never for a model name, so a provider swap is a config
#: change. This is the DeepSeek half of that mapping.
MODEL_CLASSES: Final[dict[str, str]] = {
    "router": "deepseek-v4-flash",
    "fast": "deepseek-v4-flash",
    "balanced": "deepseek-v4-flash",
    # `deep` is the one class where the extra capability is worth 3x the
    # output rate; the rest of the ladder routes to flash, which is cheaper
    # per output token than deepseek-chat was and carries 16x the context.
    "deep": "deepseek-v4-pro",
}


class DeepSeekLLMAdapter(OpenAICompatibleLLMAdapter):
    """``LLMPort`` against DeepSeek."""

    def __init__(
        self,
        *,
        api_key: SecretValue | str,
        model: str = "deepseek-v4-flash",
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        capabilities = DEEPSEEK_MODELS.get(model)
        if capabilities is None:
            raise ConfigurationError(
                f"Unknown DeepSeek model {model!r}. Known: "
                f"{sorted(DEEPSEEK_MODELS)}. An unknown model would get an "
                f"invented capability record, and the fallback router would "
                f"then make routing decisions from a guess."
            )
        super().__init__(
            provider="deepseek",
            base_url=base_url,
            api_key=api_key,
            model=model,
            capabilities=capabilities,
            client=client,
            max_attempts=max_attempts,
        )

    @staticmethod
    def model_for_class(model_class: str) -> str:
        """Resolve a manifest ``model_class`` to a concrete model name."""
        return MODEL_CLASSES.get(model_class, "deepseek-v4-flash")
