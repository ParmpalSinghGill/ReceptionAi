"""``LLMPort`` adapters — contract §5, 03 §9.

Three things live here:

* :mod:`~packages.providers.llm.openai_compatible` — the shared base for every
  provider that speaks the OpenAI Chat Completions wire format. DeepSeek,
  Together, Groq, Fireworks and OpenAI itself differ only by ``base_url``,
  model names and which optional fields they honour, so the request/response
  translation is written once.
* :mod:`~packages.providers.llm.deepseek_adapter` — DeepSeek, a thin
  specialization of the base.
* :mod:`~packages.providers.llm.stub_adapter` — a deterministic, offline
  implementation of the same port. It is not a test double bolted on the side:
  it is the adapter the platform selects when no key is configured, so a
  developer with no credentials still gets a running system rather than a
  crash-loop, and the test suite never touches the network.

Selection is :func:`~packages.providers.llm.factory.build_llm_adapter`, which
falls back to the stub with a loud log rather than raising.
"""

from __future__ import annotations

from packages.providers.llm.deepseek_adapter import DeepSeekLLMAdapter
from packages.providers.llm.factory import LLMProviderSettings, build_llm_adapter
from packages.providers.llm.openai_compatible import OpenAICompatibleLLMAdapter
from packages.providers.llm.stub_adapter import StubLLMAdapter

__all__ = [
    "DeepSeekLLMAdapter",
    "LLMProviderSettings",
    "OpenAICompatibleLLMAdapter",
    "StubLLMAdapter",
    "build_llm_adapter",
]
