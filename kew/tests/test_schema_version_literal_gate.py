"""#2297 gate — no test pins a stale ``CURRENT_SCHEMA_VERSION``.

A ``run_store`` migration bumps one constant and leaves behind every test that
spelled the old value as a literal. The migration skill makes hunting them a
manual checklist step (``grep -rn "== <N-1>"``), and the step keeps getting
missed: the v62/v63 bump in this PR updated every such literal except
``tests/test_round14_liveness.py``, which asserted a post-migration DB was at
v61 and so failed at head — a test the changed source modules do not name, so
no targeted round would have run it either.

The literal is deliberate (it forces a schema bump to be an explicit edit, not
a tautology against the constant), so this gate keeps the literals rather than
replacing them: every equality chain that reads the ``schema_version`` table or
names ``CURRENT_SCHEMA_VERSION`` must compare against the current value.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from kew.run_store import CURRENT_SCHEMA_VERSION

TESTS = pathlib.Path(__file__).resolve().parent

# The two spellings that mean "the run_store schema version of a migrated DB":
# a read of the table itself, and the constant. Other ``schema_version``
# attributes in the tree — a route profile's, an ingest payload's, a config
# capability's — are unrelated version numbers and are left alone, as is
# ``_read_db_schema_version(db) == 18``, which round-trips a hand-written value
# rather than asserting a migration outcome.
_IDENTITY = ("FROM schema_version", "CURRENT_SCHEMA_VERSION")


def _pinned_versions(source: str) -> list[tuple[int, int]]:
    """``(lineno, literal)`` for every schema-version literal in ``source``.

    Only all-``==`` chains, and only literals in a comparison position: the
    ``0`` in ``.fetchone()[0]`` is a subscript on the left-hand expression, not
    a claim about the schema.
    """
    out: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not all(isinstance(op, ast.Eq) for op in node.ops):
            continue
        chain = [node.left, *node.comparators]
        if not any(
            token in ast.unparse(element) for element in chain for token in _IDENTITY
        ):
            continue
        out.extend(
            (node.lineno, element.value)
            for element in chain
            if isinstance(element, ast.Constant)
            and isinstance(element.value, int)
            and not isinstance(element.value, bool)
        )
    return out


@pytest.mark.policy
def test_no_test_pins_a_stale_schema_version() -> None:
    stale = [
        f"{path.relative_to(TESTS).as_posix()}:{lineno}:"
        f" asserts schema v{version}, current is v{CURRENT_SCHEMA_VERSION}"
        for path in sorted(TESTS.rglob("test_*.py"))
        for lineno, version in _pinned_versions(path.read_text())
        if version != CURRENT_SCHEMA_VERSION
    ]
    assert stale == [], stale


@pytest.mark.policy
def test_gate_detects_a_stale_literal() -> None:
    """The detector must see the shape that actually shipped broken.

    The stale value is derived from the constant rather than spelled, so these
    fixtures cannot themselves become the stale literal the sweep above hunts.
    """
    stale = CURRENT_SCHEMA_VERSION - 2
    shipped = (
        "with SqliteRunStore(path) as store:\n"
        "    assert (\n"
        '        store._conn.execute("SELECT version FROM schema_version")'
        ".fetchone()[0]\n"
        f"        == {stale}\n"
        "    )\n"
    )
    # The Compare's lineno is its left-hand expression's, not the assert's.
    assert _pinned_versions(shipped) == [(3, stale)]
    chained = f"assert ver == CURRENT_SCHEMA_VERSION == {stale}"
    assert _pinned_versions(chained) == [(1, stale)]
    # Unrelated version numbers stay out, as does a hand-written round-trip.
    assert _pinned_versions("assert route.profile.schema_version == 1") == []
    assert _pinned_versions("assert _read_db_schema_version(db) == 18") == []
