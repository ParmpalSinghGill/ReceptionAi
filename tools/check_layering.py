#!/usr/bin/env python3
"""Enforce the dependency rules in Core Contract §9.

Three rules, all structural rather than conventional:

  1. `packages.core` imports nothing from any other first-party package.
     It is the domain model and the port definitions. If core can import
     persistence, then "ports" are decoration and the hexagon is a diagram.

  2. Vendor SDKs are imported only inside adapter packages
     (providers/ | connectors/ | channels/). This is what makes
     "swap Deepgram for AssemblyAI" a config change instead of a refactor.

  3. `apps` may import `packages`; `packages` may never import `apps`.

Run:  python tools/check_layering.py
Exit: 0 clean, 1 on any violation (CI gate).

This parses the AST rather than grepping, so imports inside docstrings,
comments, or strings cannot produce a false positive. A layering check that
cries wolf gets deleted within a month, and then the boundary is gone.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent

# Vendor SDKs that may only appear inside adapter packages. Keyed on the
# top-level module name as it appears in an import statement.
VENDOR_SDKS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "groq",
        "ollama",
        "cohere",
        "mistralai",
        "deepgram",
        "elevenlabs",
        "cartesia",
        "assemblyai",
        "azure",
        "twilio",
        "livekit",
        "stripe",
        "boto3",
        "botocore",
        "googleapiclient",
        "msal",
        "simple_salesforce",
        "hubspot",
        # LLM observability. On this list for the same reason as every other
        # entry: the moment `packages/ai` imports it directly, swapping the
        # tracing backend stops being a config change. TracingPort is the seam.
        "langfuse",
    }
)

# Packages permitted to import a vendor SDK.
ADAPTER_PACKAGES: frozenset[str] = frozenset(
    {
        "packages.providers",
        "packages.connectors",
        "packages.channels",
    }
)

FIRST_PARTY_ROOTS: frozenset[str] = frozenset({"packages", "apps"})


class Violation(NamedTuple):
    path: Path
    line: int
    rule: str
    detail: str


def _module_of(path: Path) -> str:
    """`packages/core/ports/llm.py` -> `packages.core.ports.llm`."""
    return ".".join(path.relative_to(REPO).with_suffix("").parts)


def _imported_names(tree: ast.Module) -> list[tuple[str, int]]:
    """Every imported module path, with its line number.

    `from x.y import z` yields `x.y`; a relative import yields "" and is
    skipped, since a relative import cannot cross a package boundary.
    """
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: cannot escape its own package
                continue
            if node.module:
                out.append((node.module, node.lineno))
    return out


def check_file(path: Path) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "syntax", str(exc))]

    module = _module_of(path)
    in_core = module.startswith("packages.core")
    in_adapter = any(module.startswith(p) for p in ADAPTER_PACKAGES)
    in_packages = module.startswith("packages.")

    violations: list[Violation] = []

    for name, lineno in _imported_names(tree):
        root = name.split(".")[0]
        top_two = ".".join(name.split(".")[:2])

        # Rule 1 — core is a leaf.
        if in_core and root in FIRST_PARTY_ROOTS and top_two != "packages.core":
            violations.append(
                Violation(
                    path,
                    lineno,
                    "core-is-a-leaf",
                    f"packages.core must not import {name!r} (contract §9)",
                )
            )

        # Rule 2 — vendor SDKs stay in adapters.
        if root in VENDOR_SDKS and not in_adapter:
            violations.append(
                Violation(
                    path,
                    lineno,
                    "vendor-sdk-outside-adapter",
                    f"{name!r} may only be imported inside "
                    f"{' | '.join(sorted(ADAPTER_PACKAGES))} (contract §5)",
                )
            )

        # Rule 3 — packages never depend on apps.
        if in_packages and root == "apps":
            violations.append(
                Violation(
                    path,
                    lineno,
                    "package-imports-app",
                    f"packages must not import {name!r}; dependencies point inward",
                )
            )

    return violations


def main() -> int:
    roots = [REPO / "packages", REPO / "apps"]
    files = [
        p
        for root in roots
        if root.exists()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]

    all_violations = [v for f in files for v in check_file(f)]

    if not all_violations:
        print(f"layering OK — {len(files)} files, 3 rules, 0 violations")
        return 0

    print(f"LAYERING VIOLATIONS ({len(all_violations)}):\n", file=sys.stderr)
    for v in all_violations:
        rel = v.path.relative_to(REPO)
        print(f"  {rel}:{v.line}  [{v.rule}]\n      {v.detail}", file=sys.stderr)
    print(
        "\nThese rules are structural, not stylistic. If a rule genuinely "
        "blocks correct work, change the contract deliberately — do not "
        "add an exception here.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
