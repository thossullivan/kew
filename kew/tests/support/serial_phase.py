"""Which Textual pilot modules need the serial phase, read from their source.

``tests/conftest.py`` marks every test in a module that drives a Textual app
through its pilot ``serial`` unless that module waits through
``tests/support/tui_wait.py``, whose waits all carry a deadline (#2342).
``tests/test_serial_phase.py`` pins both halves of that rule and refuses a
deadline-free pump in a module that claims the escape. Both read the rule
from here, so there is one answer rather than two regexes to keep in step.

The rule is read off the syntax tree, not the module text, because every
text-level form of it could be satisfied without being earned: a line inside
a docstring is not an import, an import nothing calls waits for nothing, and
the pilot's deadline-free pump is a pump under whatever name the pilot reached
the test under — a ``with`` target (``p``, ``_pilot``), or the fixture or
helper the module opened the pilot in and handed it out from — not only under
``pilot``.

The pilot's entry point and its pump are named by concatenation so this
module's own source matches neither probe: ``tests/test_serial_phase.py``
refuses a pilot call in a helper module, where the marker probe cannot see it.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

#: The helper whose waits earn a pilot module the parallel phase, in the one
#: import form the suite uses. Any other form reads as "no bounded waits",
#: which fails safe: the module stays in the serial phase.
BOUNDED_WAITS_MODULE = "support.tui_wait"
#: The waits themselves. The helper's autouse ``no_ambient_polls`` fixture is
#: deliberately not one — it stops the app's own interval polls from landing
#: mid-test and buys no deadline for the state an assertion is about.
BOUNDED_WAITS = frozenset({"wait_for", "quiesce", "settle"})

_PILOT_ENTRY = "run_" + "test"
_PILOT_PUMP = "pause"
#: The attribute form (a dot before the name), so prose that merely names the
#: pilot entry point does not park a whole module in the serial phase.
_PILOT_CALL = re.compile(r"\." + _PILOT_ENTRY + r"\b")


def drives_a_pilot(source: str) -> bool:
    """Does *source* call the Textual pilot entry point?

    The cheap half of the rule, and the only half every test module in the
    suite is asked: a module with no pilot is never parsed.
    """
    return _PILOT_CALL.search(source) is not None


def _called_names(tree: ast.Module) -> set[str]:
    """Every bare name *tree* calls (``quiesce(pilot)``, not ``x.quiesce()``)."""
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _imported_waits(tree: ast.Module) -> set[str]:
    """The local names *tree* really imports from the bounded-wait helper."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == BOUNDED_WAITS_MODULE
        for alias in node.names
        if alias.name in BOUNDED_WAITS
    }


def waits_with_deadlines(source: str) -> bool:
    """Does *source* actually wait through the bounded-wait helper?

    Both halves, because either one alone is free to write: the import has to
    be a real import statement, and one of the names it binds has to be
    called.
    """
    tree = ast.parse(source)
    return bool(_imported_waits(tree) & _called_names(tree))


def _opens_a_pilot(node: ast.AST, factories: frozenset[str]) -> bool:
    """Does *node* call the pilot entry point, or a *factories* name that does?"""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr == _PILOT_ENTRY or node.func.attr in factories
    return isinstance(node.func, ast.Name) and node.func.id in factories


def _exposed_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names *func* can be reached under: its own, plus any ``name=``.

    ``@pytest.fixture(name="pilot")`` is the name a test asks for the fixture
    by. The keyword is read off any decorator rather than pytest's alone,
    because a name too many only widens what reads as a pilot, which fails
    safe.
    """
    return {func.name} | {
        keyword.value.value
        for decorator in func.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
        if isinstance(keyword.value.value, str)
    }


def _pilot_factories(tree: ast.Module) -> set[str]:
    """Every name in *tree* that hands out a pilot the module opened itself.

    A pilot does not have to arrive through a ``with`` statement of the
    test's own: an in-module fixture or context-manager helper opens it and
    yields it, and the test then holds it under *that* name. One set of names
    covers both, because a fixture is requested by its name and a helper is
    called by it.
    """
    factories: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            _opens_a_pilot(child, frozenset()) for child in ast.walk(node)
        ):
            factories |= _exposed_names(node)
    return factories


def _pilot_bindings(tree: ast.Module) -> set[str]:
    """Every name in *tree* that holds a pilot.

    The ``with`` targets — including those of a helper that yields a pilot
    rather than the entry point itself — plus the factory names a pilot
    reaches a test under with no ``with`` statement in sight.
    """
    factories = _pilot_factories(tree)
    bound = set(factories)
    frozen = frozenset(factories)
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        bound |= {
            item.optional_vars.id
            for item in node.items
            if isinstance(item.optional_vars, ast.Name)
            and _opens_a_pilot(item.context_expr, frozen)
        }
    return bound


def bare_pumps(source: str) -> list[int]:
    """The lines of *source* that pump a pilot with its deadline-free pump.

    Under whatever name the pilot reached the test under: ``as p`` then
    ``p``'s pump is the same "the app got there within N idle points"
    assertion as ``pilot``'s, and the name is not what makes it bounded. A
    pilot handed out by an in-module fixture or helper counts too, so moving
    the ``run_test`` out of the test is not a way out. An attribute chain (a
    Textual timer's own pause, say) is not a pilot and is left alone.
    """
    tree = ast.parse(source)
    bound = _pilot_bindings(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == _PILOT_PUMP
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in bound
    )


@functools.cache
def needs_the_serial_phase(path: str) -> bool:
    """Does the module at *path* have to run alone, after the xdist phase?

    Cached by path for the session: ``conftest`` asks this once per collected
    test, and a module's source cannot change under a running collection.
    """
    source = Path(path).read_text(encoding="utf-8")
    if not drives_a_pilot(source):
        return False
    return not waits_with_deadlines(source)
