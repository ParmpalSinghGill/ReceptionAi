"""Invariant I4 — the booking engine names no vertical. 02 §5.6.

``packages/booking/`` may only speak resource, service, requirement, window,
slot, booking and hold. The moment one vertical's word appears there, every other
vertical becomes a special case and "verticals are data" is a slogan.

**The realistic failure is not malice.** It is someone adding
``if service.code == "emergency":`` to the solver at 6pm to ship a demo. Nobody
reviews that line twice; six months later there are nine of them and the engine
only works for dentists. This test makes that a build failure at the moment it is
written, which is the only moment it is cheap to fix.

**It inspects the parsed AST, not raw text.** Only identifiers and string
literals are examined; comments and docstrings are invisible to it *by
construction*. A docstring reading "e.g. a table, a hotel room" is legitimate
documentation explaining the abstraction, and a check that failed on it would be
deleted within a month — and then the boundary would be gone.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOKING_PACKAGE = REPO_ROOT / "packages" / "booking"

#: Vertical vocabulary, lowercase; matching is case-folded. Grouped by the
#: vertical that would introduce each, because that grouping is what makes an
#: addition reviewable — a new group is a new vertical leaking in.
FORBIDDEN_TERMS: frozenset[str] = frozenset(
    {
        # medical / dental
        "patient", "doctor", "physician", "clinician", "clinic", "appointment",
        "practice", "dentist", "dental", "surgery", "hygienist", "operatory",
        "copay", "insurance", "telehealth", "prescription", "triage",
        # food service
        "restaurant", "table", "diner", "cover", "seating", "menu",
        "party_size", "turn_time", "waiter", "kitchen",
        # lodging
        "hotel", "room", "roomnight", "room_night", "guest", "reservation",
        "checkin", "check_in", "checkout", "check_out", "folio", "rate_plan",
        "occupancy", "suite", "concierge",
        # personal services
        "salon", "stylist", "therapist", "treatment", "massage", "haircut",
    }
)

#: Exact identifiers that contain a forbidden substring but are legitimate.
#: Matched **whole**, after case-folding, so adding one here widens the exemption
#: by exactly one name rather than by a substring.
ALLOWED_IDENTIFIERS: frozenset[str] = frozenset(
    {
        # 'reservation' is the industry-neutral word for an external system's
        # booking handle, and it appears in ConnectorPort payloads we do not
        # control.
        "reservation_id",
        "reservation_ref",
        "external_reservation_id",
        # Not the furniture.
        "timetable",
        "table_name",
        "tablespace",
    }
)

#: Files exempt from the check, each with the reason. There should be very few,
#: and ideally none inside `packages/booking` — the translation seam that is
#: allowed to speak both languages lives in `packages/verticals` precisely so
#: that this package stays clean.
EXCLUDED_PATHS: dict[str, str] = {}


def _iter_source_files() -> list[pathlib.Path]:
    """Production sources only.

    Tests are excluded deliberately: a booking-engine test *should* assert that a
    90-minute dinner slot solves correctly, because that is how the generality is
    demonstrated. The invariant constrains production code, not the tests that
    prove it.
    """
    return [
        path
        for path in sorted(BOOKING_PACKAGE.rglob("*.py"))
        if "tests" not in path.parts
        and not path.name.startswith("test_")
        and str(path.relative_to(REPO_ROOT)) not in EXCLUDED_PATHS
    ]


def _split_identifier(name: str) -> set[str]:
    """Break snake_case AND camelCase into lowercase words.

    Both, because ``party_size`` and ``partySize`` are the same leak and a check
    that caught only one would be trivially routed around without anyone
    intending to.
    """
    spaced = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
    return {part for part in spaced.lower().split("_") if part}


def _offending_terms(text: str, *, whole_identifier: str | None = None) -> set[str]:
    if whole_identifier and whole_identifier.lower() in ALLOWED_IDENTIFIERS:
        return set()
    hits = _split_identifier(text) & FORBIDDEN_TERMS
    # Also catch multi-word forms written as one token ("roomnight").
    collapsed = text.lower().replace("_", "")
    hits |= {t for t in FORBIDDEN_TERMS if "_" not in t and t == collapsed}
    return hits


class _VocabularyVisitor(ast.NodeVisitor):
    """Collects vertical vocabulary from identifiers and string literals."""

    def __init__(self, source: str) -> None:
        self.offences: list[tuple[int, str, str]] = []
        self._lines = source.splitlines()

    def _line(self, lineno: int) -> str:
        index = lineno - 1
        return self._lines[index] if 0 <= index < len(self._lines) else ""

    def _suppressed(self, lineno: int) -> bool:
        """``# noqa: I4`` on the line is the intended pressure valve.

        A genuine false positive needs no ceremony to silence — the alternative
        is that someone deletes the whole check the first time it is wrong.
        """
        return "noqa: I4" in self._line(lineno)

    def _record(
        self, node: ast.AST, text: str, context: str, whole: str | None = None
    ) -> None:
        lineno = getattr(node, "lineno", 0)
        if self._suppressed(lineno):
            return
        for term in _offending_terms(text, whole_identifier=whole):
            self.offences.append((lineno, term, context))

    def visit_Name(self, node: ast.Name) -> None:
        self._record(node, node.id, f"name {node.id!r}", whole=node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._record(node, node.attr, f"attribute .{node.attr}", whole=node.attr)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._record(node, node.arg, f"parameter {node.arg!r}", whole=node.arg)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Only string *values*. A docstring is an ast.Expr whose value is a
        # Constant, and those are stripped before visiting — see `_visit_body`.
        if isinstance(node.value, str):
            self._record(node, node.value, f"string {node.value[:40]!r}")
        self.generic_visit(node)

    def _visit_def(self, node: ast.AST) -> None:
        name = getattr(node, "name", "")
        self._record(node, name, f"definition {name!r}", whole=name)
        for child in self._visit_body(node):
            self.visit(child)
        for decorator in getattr(node, "decorator_list", []):
            self.visit(decorator)

    visit_FunctionDef = _visit_def  # noqa: N815 - ast API
    visit_AsyncFunctionDef = _visit_def  # noqa: N815
    visit_ClassDef = _visit_def  # noqa: N815

    @staticmethod
    def _visit_body(node: ast.AST) -> list[ast.stmt]:
        """A node's body with its docstring removed.

        The docstring is where the abstraction gets *explained*, and explaining
        that a resource might be a table or a hotel room is exactly what the
        documentation should do.
        """
        body = list(getattr(node, "body", []))
        if ast.get_docstring(node):  # type: ignore[arg-type]
            body = body[1:]
        return body


def test_booking_package_contains_no_vertical_vocabulary() -> None:
    """The engine may only speak Resource / Service / Requirement / Window / Slot.

    When this fires, work through these in order before reaching for the
    allow-list:

    1. Can it be a resource ``attribute`` plus a ``requires[].attributes``
       filter? (This covers the large majority of apparent gaps — "seats at least
       four" is an attribute predicate, not a restaurant feature.)
    2. Can it be a new ``allocation_strategy``?
    3. Can it be a ``capacity`` or ``buffers`` refinement?

    Only if all three fail is it a real model gap, and that is an ADR and a
    Core Contract §4 review — not a local fix. **Editing this term list to make
    the failure disappear is never the answer.**
    """
    offenders: list[str] = []

    for path in _iter_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        visitor = _VocabularyVisitor(source)
        # The module docstring is stripped for the same reason a function's is.
        body = tree.body[1:] if ast.get_docstring(tree) else tree.body
        for node in body:
            visitor.visit(node)

        relative = path.relative_to(REPO_ROOT)
        offenders.extend(
            f"  {relative}:{lineno}: {term!r} in {context}"
            for lineno, term, context in visitor.offences
        )

    assert not offenders, (
        "Vertical vocabulary leaked into the generic booking engine:\n"
        + "\n".join(sorted(offenders))
        + "\n\nThe booking engine may only speak Resource / Service / "
        "Requirement / Window / Slot. If you need a concept it cannot express, "
        "that is a MODEL change, not a local fix. To document a genuine false "
        "positive, add the whole identifier to ALLOWED_IDENTIFIERS with a "
        "comment, or suppress one line with `# noqa: I4`."
    )


def test_booking_does_not_import_the_verticals_package() -> None:
    """The structural half of the same invariant.

    Even with clean naming, importing the manifest models would let vertical
    semantics in through the type system — the engine would start receiving
    objects whose *shape* encodes a vertical, and every subsequent method would
    quietly depend on it.

    The engine receives plain domain objects. Manifests are translated into those
    at the seam, which lives in ``packages/verticals`` precisely so this package
    stays clean.
    """
    offenders: list[str] = []
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{relative}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if "verticals" in alias.name
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module and "verticals" in node.module:
                    offenders.append(
                        f"{relative}:{node.lineno}: from {node.module}"
                    )

    assert not offenders, (
        "packages/booking imports the verticals package:\n"
        + "\n".join(offenders)
        + "\n\nThe engine must receive plain domain objects."
    )


def test_booking_does_not_import_persistence_or_the_ai_layer() -> None:
    """The engine is handed its I/O; it does not reach for it.

    Contract §9 would permit these imports, so this is a stricter rule than the
    layering check enforces globally — and it is worth having, because it is what
    keeps the solver testable with an in-memory double and keeps a DST assertion
    from needing a database.
    """
    forbidden = ("packages.persistence", "packages.ai", "sqlalchemy")
    offenders: list[str] = []
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{relative}:{node.lineno}: {name}"
                for name in names
                if any(name.startswith(prefix) for prefix in forbidden)
            )

    assert not offenders, (
        "packages/booking reached for its own I/O:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "term", ["patient", "table", "room_night", "hotel", "dentist"]
)
def test_the_detector_actually_detects(term: str) -> None:
    """The check catches what it claims to.

    A vocabulary test that silently matches nothing is the most dangerous kind of
    green: it reports that the boundary holds while checking nothing at all. This
    runs the detector against a synthetic offender for each vertical group.
    """
    source = f"def solve():\n    {term}_count = 1\n    return {term}_count\n"
    tree = ast.parse(source)
    visitor = _VocabularyVisitor(source)
    for node in tree.body:
        visitor.visit(node)
    assert visitor.offences, f"the detector missed {term!r}"


def test_the_suppression_comment_works() -> None:
    """``# noqa: I4`` is the pressure valve, and it must actually valve.

    If suppression did not work, the first genuine false positive would be
    unfixable without editing the term list — which is the one thing this check
    must never encourage.
    """
    source = "def solve():\n    patient_count = 1  # noqa: I4\n    return 1\n"
    tree = ast.parse(source)
    visitor = _VocabularyVisitor(source)
    for node in tree.body:
        visitor.visit(node)
    assert not visitor.offences
