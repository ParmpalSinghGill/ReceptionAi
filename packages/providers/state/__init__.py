"""Adapters for :class:`~packages.core.ports.conversation_state.ConversationStatePort`.

Short-lived conversation state that must be visible to every API replica: the
slot handles the booking tools mint and redeem, and the server-side queue of
alternatives a caller walks when they decline one.
"""

from __future__ import annotations

from packages.providers.state.redis_conversation_state import (
    RedisConversationState,
)

__all__ = ["RedisConversationState"]
