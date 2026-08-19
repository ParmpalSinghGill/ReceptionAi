"""Hexagonal boundaries — contract §5.

Every external dependency is reached through one of these ports. Adapters are
registered in a DI container and selected **per tenant** from configuration. No
subsystem may import a vendor SDK outside ``packages/providers``,
``packages/connectors``, or ``packages/channels``.

Phase 1 defines three of the thirteen, plus ``TracingPort`` — which is not in
§5's table because LLM observability was added later (it is the seam that keeps
the Langfuse SDK out of ``packages/ai``). The remaining ten (``STTPort``,
``TTSPort``, ``RealtimeVoicePort``, ``EmbeddingPort``, ``VectorStorePort``,
``RerankerPort``, ``ConnectorPort``, ``ChannelPort``, ``StoragePort``,
``BillingPort``, ``NotificationPort``) are defined by the phases that first need
an adapter behind them. Writing a port with no implementation and no caller
produces a signature invented in the abstract, which the first real adapter then
has to break.
"""

from __future__ import annotations

from packages.core.ports.connection_router import (
    DEFAULT_SHARD,
    ConnectionRouterPort,
    UnresolvableShardError,
)
from packages.core.ports.llm import (
    ChatResult,
    LLMPort,
    Message,
    ProviderCapabilities,
    StreamDelta,
    ToolCall,
    ToolSchema,
    Usage,
)
from packages.core.ports.secrets import SecretMeta, SecretRef, SecretsPort, SecretValue
from packages.core.ports.tracing import (
    Capture,
    GenerationUsage,
    NoOpTracer,
    SpanHandle,
    ToolStatus,
    TracingPolicy,
    TracingPort,
    TurnContext,
)

__all__ = [
    "DEFAULT_SHARD",
    "Capture",
    "ChatResult",
    "ConnectionRouterPort",
    "GenerationUsage",
    "LLMPort",
    "Message",
    "NoOpTracer",
    "ProviderCapabilities",
    "SecretMeta",
    "SecretRef",
    "SecretValue",
    "SecretsPort",
    "SpanHandle",
    "StreamDelta",
    "ToolCall",
    "ToolSchema",
    "ToolStatus",
    "TracingPolicy",
    "TracingPort",
    "TurnContext",
    "UnresolvableShardError",
    "Usage",
]
