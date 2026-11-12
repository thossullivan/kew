"""Every audit row kew writes goes through ``chained_insert_statement`` (#3332).

From schema v77 a row's ``chain_format`` decides its hash, and the
``audit_chain_format_no_downgrade`` trigger refuses a legacy-format row once
the ledger holds a format-2 one. An ``INSERT INTO audit_events`` written by
hand never names ``chain_format``, so it takes the legacy default and fails on
every ledger that has moved on: the write probe once did, and with it every
gcp host lease (``record_lapsed_leases``).

Every module under ``src/kew`` is parsed with :mod:`ast`; a string literal
(f-string parts included) that inserts into ``audit_events`` is refused
outside the functions in :data:`EXCEPTIONS`, each with its reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# The shared AST reader (tests/support/source_ast.py): this lint parses every
# src module, and the read lives in one audited helper, not in the test.
from support.source_ast import _parse as parse_file

SRC = Path(__file__).resolve().parent.parent / "src" / "kew"

_AUDIT_INSERT = re.compile(
    r"\b(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+[\"`\[]?audit_events\b",
    re.IGNORECASE,
)

#: ``(module path relative to src/kew, enclosing function)`` → why it may.
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("run_store_audit.py", "chained_insert_statement"): (
        "the one builder: it names chain_format whenever the column exists, "
        "and every writer takes its INSERT from here"
    ),
}


class _AuditInserts(ast.NodeVisitor):
    """Each audit-insert string literal, keyed by its enclosing function."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: list[tuple[str, int]] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _AUDIT_INSERT.search(node.value):
            where = self.scope[-1] if self.scope else "<module>"
            self.found.append((where, node.lineno))


def _audit_inserts() -> dict[tuple[str, str], list[int]]:
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        visitor = _AuditInserts()
        visitor.visit(parse_file(str(path)))
        rel = path.relative_to(SRC).as_posix()
        for where, line in visitor.found:
            found.setdefault((rel, where), []).append(line)
    return found


@pytest.mark.policy
def test_every_audit_insert_is_built_by_chained_insert_statement():
    found = _audit_inserts()
    stray = {key: lines for key, lines in found.items() if key not in EXCEPTIONS}
    assert not stray, (
        "an INSERT INTO audit_events built by hand takes the legacy chain "
        "format and is refused on a v77 ledger; build it with "
        f"chained_insert_statement (kew.run_store_audit): {stray}"
    )


@pytest.mark.policy
def test_every_listed_exception_still_writes_an_audit_insert():
    """An exception whose insert moved away is a stale hole in the rule."""
    assert set(_audit_inserts()) >= set(EXCEPTIONS)


def test_the_rule_matches_every_spelling_of_an_audit_insert():
    for sql in (
        "INSERT INTO audit_events (timestamp, event) VALUES (?, ?)",
        "insert  or ignore into audit_events(timestamp) values (?)",
        'REPLACE INTO "audit_events" (id) VALUES (?)',
    ):
        assert _AUDIT_INSERT.search(sql), sql
    for sql in (
        "SELECT * FROM audit_events",
        "INSERT INTO audit_events_archive (id) VALUES (?)",
        "UPDATE audit_events SET item_id = ?",
    ):
        assert not _AUDIT_INSERT.search(sql), sql
