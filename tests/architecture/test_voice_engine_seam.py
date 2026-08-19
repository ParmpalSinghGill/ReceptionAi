"""The voice seam holds: Pipecat is an implementation detail, not a dependency.

04 §2.2 places the abstraction at ``VoiceEngine`` so that the composed pipeline's
provider stack can be replaced without touching the platform. That claim is only
true if the import graph enforces it, so these tests check the graph rather than
trusting the prose.

Two properties, both structural:

1. ``packages/voice/`` — the seam itself — imports no vendor SDK. If it imports
   Pipecat, then "the engine is swappable" is a diagram, not a fact.
2. ``apps/media/`` imports Pipecat only *lazily*, inside function bodies. The
   app tier owns process lifecycle and metrics; it must be importable (by a
   test, by a metrics scrape) without paying for a vendor SDK at module scope.

``tools/check_layering.py`` covers the repo-wide rules; this file covers the
voice-specific invariant that rule 2 alone does not express.
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: SDKs the voice stack could plausibly reach for. Kept in sync with
#: ``tools/check_layering.py``'s list by intent, narrowed to voice.
VENDOR_ROOTS = frozenset(
    {"pipecat", "deepgram", "elevenlabs", "cartesia", "assemblyai", "livekit"}
)


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(tree: ast.AST) -> list[tuple[str, int, bool]]:
    """Every imported root module: (name, lineno, is_module_scope)."""
    out: list[tuple[str, int, bool]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        # The mixedCase names are `ast.NodeVisitor`'s dispatch protocol, not a
        # style choice — it looks up `visit_<ClassName>`.
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_AsyncFunctionDef(
            self, node: ast.AsyncFunctionDef
        ) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                out.append((alias.name.split(".")[0], node.lineno, self.depth == 0))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module and not node.level:
                out.append((node.module.split(".")[0], node.lineno, self.depth == 0))

    Visitor().visit(tree)
    return out


def test_voice_seam_imports_no_vendor_sdk() -> None:
    """``packages/voice`` is the abstraction; it must not know any provider."""
    offenders: list[str] = []
    for path in _python_files(REPO / "packages" / "voice"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root, lineno, _ in _imports(tree):
            if root in VENDOR_ROOTS:
                offenders.append(f"{path.relative_to(REPO)}:{lineno} imports {root!r}")

    assert not offenders, (
        "packages/voice defines the VoiceEngine seam and must stay vendor-free "
        "— move the provider code into packages/providers/voice/:\n  "
        + "\n  ".join(offenders)
    )


def test_media_app_defers_vendor_imports() -> None:
    """``apps/media`` may use Pipecat, but never at module import time.

    Importing the app must not cost a multi-second SDK import, and the metrics
    module in particular has to be importable in an environment where the voice
    extra is not installed at all.
    """
    offenders: list[str] = []
    for path in _python_files(REPO / "apps" / "media"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root, lineno, module_scope in _imports(tree):
            if root in VENDOR_ROOTS and module_scope:
                offenders.append(f"{path.relative_to(REPO)}:{lineno} imports {root!r}")

    assert not offenders, (
        "apps/media must import vendor SDKs lazily (inside the function that "
        "needs them), so the process is importable without the voice extra:\n  "
        + "\n  ".join(offenders)
    )


def test_ai_and_core_never_import_voice_vendors() -> None:
    """The agent runtime must not learn about audio. Contract §9."""
    offenders: list[str] = []
    for pkg in ("ai", "core"):
        for path in _python_files(REPO / "packages" / pkg):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for root, lineno, _ in _imports(tree):
                if root in VENDOR_ROOTS:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{lineno} imports {root!r}"
                    )

    assert not offenders, (
        "packages/ai and packages/core are channel-agnostic (contract §7); a "
        "voice SDK there means the agent knows what a microphone is:\n  "
        + "\n  ".join(offenders)
    )
