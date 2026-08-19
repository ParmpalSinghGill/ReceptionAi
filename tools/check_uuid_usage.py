#!/usr/bin/env python3
"""CI gate: no `uuid4`/`uuid1`/`uuid3`/`uuid5` outside an annotated allow-list.

Core Contract §6 and amendment A2; 05-PLATFORM-SECURITY-OPS.md §A1.0.

Why this is a build-breaking gate rather than a code-review convention:

A silent fallback to `uuid4()` satisfies the type signature and every test
while destroying the index locality UUIDv7 was chosen for. Nothing fails
visibly. Inserts scatter across every leaf page of the PK index on `turns`,
`usage_events` and `audit_events`, and the symptom surfaces months later as
unexplained WAL volume — by which point the ids are persisted and the fix is a
migration, not a patch.

The second reason A2 gives: `uuid7_timestamp()` recovers the partition key
from the id itself, which a v4 makes impossible. A v4 id is not merely slower,
it is missing information the system depends on.

Legitimate non-identifier randomness does exist — test fixtures, opaque
correlation values with no index. Each such use must carry an explicit
`# ALLOW-UUID4: <reason>` marker on the same line or the line above, so the
exception is a deliberate, reviewable statement rather than an oversight.

The AST is parsed rather than grepped, so a `uuid4` inside a docstring,
comment or string literal cannot produce a false positive. A checker that
cries wolf gets deleted within a month, and then the rule is gone.

Run:  python3 tools/check_uuid_usage.py
Exit: 0 clean, 1 on any violation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent

FORBIDDEN: frozenset[str] = frozenset({"uuid4", "uuid1", "uuid3", "uuid5"})
ALLOWLIST_MARKER = "# ALLOW-UUID4:"

SCAN_ROOTS = ("packages", "apps", "tools", "migrations", "tests")

# The canonical generator itself. A2 fixes ONE source of ids in Python, and
# this module is it: it references stdlib uuid to detect `uuid.uuid7` on 3.14+
# and deliberately raises rather than falling back.
CANONICAL_IDS_MODULE = "packages/core/ids.py"


class Violation(NamedTuple):
    path: Path
    line: int
    name: str


def _annotated(lines: list[str], lineno: int) -> bool:
    """True if the marker is on this line or the line immediately above.

    Two positions because a long call often wraps, leaving no room for a
    trailing comment on the offending line itself.
    """
    idx = lineno - 1
    if 0 <= idx < len(lines) and ALLOWLIST_MARKER in lines[idx]:
        return True
    prev = idx - 1
    return 0 <= prev < len(lines) and ALLOWLIST_MARKER in lines[prev]


def _called_name(node: ast.Call) -> str | None:
    """`uuid4()` -> 'uuid4';  `uuid.uuid4()` -> 'uuid4'."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def check_file(path: Path) -> list[Violation]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # Layering check owns syntax reporting; stay silent rather than
        # double-reporting the same broken file.
        return []

    lines = text.splitlines()
    violations: list[Violation] = []

    # `ast.walk` yields a Call AND the Name/Attribute inside it as separate
    # nodes, so a single `uuid4()` matches two branches below. Recording the
    # func nodes already accounted for keeps one violation per occurrence —
    # a checker that reports the same line three times trains people to skim
    # its output, which is how a real hit gets missed.
    counted_func_nodes: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node)
            if name in FORBIDDEN:
                counted_func_nodes.add(id(node.func))
                if not _annotated(lines, node.lineno):
                    violations.append(Violation(path, node.lineno, name))

    for node in ast.walk(tree):
        # Bare references never called at the call site — the `default=uuid4`
        # idiom in a SQLAlchemy Column or a dataclass field, which is precisely
        # how a v4 default sneaks onto a persisted column.
        if isinstance(node, ast.Name | ast.Attribute) and id(node) not in counted_func_nodes:
            ref = node.id if isinstance(node, ast.Name) else node.attr
            if ref in FORBIDDEN and not _annotated(lines, node.lineno):
                violations.append(Violation(path, node.lineno, ref))

        # `from uuid import uuid4` — flag the import so the reason is stated
        # once at the top rather than at every use.
        elif isinstance(node, ast.ImportFrom) and node.module == "uuid":
            for alias in node.names:
                if alias.name in FORBIDDEN and not _annotated(lines, node.lineno):
                    violations.append(Violation(path, node.lineno, alias.name))

    # Deduplicate: an import line can also be reached as a Name reference.
    return sorted(set(violations), key=lambda v: (v.line, v.name))


def main() -> int:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if base.exists():
            files.extend(
                p
                for p in base.rglob("*.py")
                if "__pycache__" not in p.parts and ".venv" not in p.parts
            )

    all_violations: list[Violation] = []
    for f in sorted(set(files)):
        rel = f.relative_to(REPO).as_posix()
        if rel == CANONICAL_IDS_MODULE:
            continue
        all_violations.extend(check_file(f))

    if not all_violations:
        print(f"uuid usage OK — {len(files)} files, 0 non-v7 identifier sources")
        return 0

    print(f"FORBIDDEN UUID USAGE ({len(all_violations)}):\n", file=sys.stderr)
    for v in all_violations:
        rel = v.path.relative_to(REPO)
        print(f"  {rel}:{v.line}  {v.name}()", file=sys.stderr)
    print(
        "\nContract §6 / amendment A2: all persisted identifiers are UUIDv7.\n"
        "Use `from packages.core.ids import uuid7` — the ONE canonical source.\n"
        "\n"
        "A v4 satisfies the type signature and every test while destroying the\n"
        "index locality v7 was chosen for, and makes uuid7_timestamp() — which\n"
        "recovers the partition key from the id — impossible.\n"
        "\n"
        "If this really is non-identifier randomness (a test fixture, an opaque\n"
        f"correlation value with no index), annotate it:  {ALLOWLIST_MARKER} <reason>",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
