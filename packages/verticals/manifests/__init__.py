"""First-party vertical manifests, shipped with the platform.

These are *data files*, not code (contract §4). They are loaded by
``tools/seed_demo.py`` at install time and by
:func:`packages.verticals.manifest.load_manifest_file` in tests.

They live inside the package rather than in a top-level ``manifests/``
directory so they travel with the wheel: a container image that shipped the
code but not the manifests would start, pass its health check, and fail on the
first conversation.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["MANIFEST_DIR", "manifest_path"]

MANIFEST_DIR = Path(__file__).parent


def manifest_path(key: str) -> Path:
    """The on-disk path of a first-party manifest by vertical key."""
    return MANIFEST_DIR / f"{key}.json"
