"""Platform ceilings — 03 §2.2.

Manifest-declared budgets are **clamped, never trusted**. Manifests are
tenant-authored data (contract §4), which means without clamping a manifest is
a denial-of-service primitive: ``max_steps: 10000`` burns the token budget and
holds a voice channel open indefinitely, and the tenant who wrote it pays
nothing extra for the privilege.

The schema's own ``ge``/``le`` bounds already reject an absurd value at save
time. These ceilings are the *second* line, applied at compile time, because a
manifest can also arrive from a database row written before the bound existed,
from a fixture, or from a migration — and the compiler is the one place every
path converges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["CEILINGS", "PlatformCeilings"]


@dataclass(frozen=True, slots=True)
class PlatformCeilings:
    """Absolute upper bounds. A manifest may request less; never more."""

    max_steps: int = 40
    max_tool_calls: int = 24
    wall_clock_budget_ms: int = 90_000
    max_parallel_tools: int = 6
    max_agents: int = 16
    max_role_prompt_chars: int = 8_000
    #: How many times one agent may run consecutively before the guard treats
    #: it as a loop. 03 §4.3's anti-ping-pong bound.
    max_consecutive_same_agent: int = 3


CEILINGS: Final = PlatformCeilings()
