"""Load the RLS registry arrays out of every migration that declares one.

Alembic revision files are loaded by path, not by import — ``0001_...`` is not
even a legal module name — so this reaches into the files the way Alembic does.

The indirection is deliberate: the test must read the arrays *the migrations
actually execute*, not a copy. A duplicated list in the test file would drift
from the migration silently, and a coverage test that checks a stale copy of the
registry is worse than no coverage test, because it reports green.

**Every migration is scanned, not a hardcoded pair.** A migration that adds
tenant-owned tables declares its own ``TENANT_OWNED_TABLES``, and discovering
them by globbing means the next one is covered without anyone remembering to
edit this file — which is the same failure the registry itself exists to
prevent, one level up.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"_rls_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - paths are fixed
        raise RuntimeError(f"cannot load migration at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect() -> list[str]:
    tables: list[str] = []
    for path in sorted(_VERSIONS_DIR.glob("[0-9]*.py")):
        module = _load(path)
        declared = getattr(module, "TENANT_OWNED_TABLES", None)
        if declared:
            tables.extend(declared)
    return tables


#: The union of every ``DO $$`` loop array. Single source of truth.
TENANT_OWNED_TABLES: list[str] = _collect()

__all__ = ["TENANT_OWNED_TABLES"]
