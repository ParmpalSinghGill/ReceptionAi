"""``packages.verticals`` — manifest schema, validator, first-party manifests.

Contract §4: *"a vertical is a declarative manifest validated against a
versioned JSON Schema. It is never tenant-supplied Python."* This package owns
the schema and the validation; ``packages/ai`` owns compiling a validated
manifest into a runnable graph.

The split matters. Validation must run in the admin UI at save time, where a
human is looking at the error (03 §2). Compilation runs on a live call, where
nobody is. Keeping them in separate packages keeps the authoring path from
depending on the runtime one.
"""

from __future__ import annotations

from packages.verticals.manifest import (
    MANIFEST_SCHEMA_VERSION,
    AgentSpec,
    GraphSpec,
    NodeKind,
    ToolGrant,
    VerticalManifest,
    load_manifest,
    load_manifest_file,
)

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "AgentSpec",
    "GraphSpec",
    "NodeKind",
    "ToolGrant",
    "VerticalManifest",
    "load_manifest",
    "load_manifest_file",
]
