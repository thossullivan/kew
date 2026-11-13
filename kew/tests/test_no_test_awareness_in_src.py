"""Production code never asks whether it is under test (#2480, #2482).

Special-casing the harness in shipped code is a known way an agent makes tests
pass without making the code right (ImpossibleBench): branch on
``PYTEST_CURRENT_TEST`` or on ``"pytest" in sys.modules`` and the suite checks a
path no user ever runs. ``src/kew`` is clean today; this keeps it clean.

Every module under ``src/kew`` is parsed with :mod:`ast` and refused on:

* any read of ``PYTEST_CURRENT_TEST``;
* a ``"pytest"``/``"_pytest"``/``"unittest"`` lookup in ``sys.modules``;
* an import of ``pytest`` or ``_pytest`` (static or through ``importlib``);
* an environment read of a name with a ``TEST``/``TESTS``/``TESTING`` word,
  ``TEST_MODE`` included.

Building a pytest command line (``merge_verify.py``, ``config.py``) writes
string literals and environment *values*; none of that is a read, so none of it
matches. A real exception is listed in :data:`EXCEPTIONS` with its reason.
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

_HARNESS_MODULES = frozenset({"pytest", "_pytest", "unittest"})
_IMPORT_REFUSED = frozenset({"pytest", "_pytest"})
_TEST_WORD = re.compile(r"(?:^|_)(?:TEST|TESTS|TESTING)(?:_|$)")
_ENV_READERS = frozenset({"get", "getenv", "getenvb", "pop", "setdefault"})

#: ``(module path relative to src/kew, finding)`` → why it is not test-awareness.
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("verify_executed.py", "imports _pytest.junitxml"): (
        "junit_key() maps a node id to junit's (classname, name) with pytest's "
        "own mangle_test_address, to read a verify run's report; it never asks "
        "whether kew itself is under test"
    ),
    ("held_out_report_plugin.py", "imports pytest"): (
        "never imported by kew: the verify ships its text to the host, where it "
        "runs as the held-out run's entry point and calls pytest.main with "
        "itself as the first plugin; it never asks whether kew is under test"
    ),
}


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def _is_sys_modules(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "modules") or (
        isinstance(node, ast.Name) and node.id == "modules"
    )


def _is_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute) and node.attr in ("environ", "environb")
    ) or (isinstance(node, ast.Name) and node.id in ("environ", "environb"))


def _str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _env_name_refused(name: str) -> bool:
    return name == "PYTEST_CURRENT_TEST" or bool(_TEST_WORD.search(name))


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[tuple[int, str]] = []

    def _add(self, node: ast.AST, finding: str) -> None:
        self.findings.append((getattr(node, "lineno", 0), finding))

    def visit_Constant(self, node: ast.Constant) -> None:
        # The variable has no use but to detect a test run, so any mention of
        # it is a read in waiting, whatever expression carries it.
        if isinstance(node.value, str) and "PYTEST_CURRENT_TEST" in node.value:
            self._add(node, "reads PYTEST_CURRENT_TEST")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _root(alias.name) in _IMPORT_REFUSED:
                self._add(node, f"imports {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0 and _root(node.module) in _IMPORT_REFUSED:
            self._add(node, f"imports {node.module}")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        key = _str(node.left)
        for op, right in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.In | ast.NotIn) or key is None:
                continue
            if _is_sys_modules(right) and _root(key) in _HARNESS_MODULES:
                self._add(node, f"tests {key!r} in sys.modules")
            elif _is_environ(right) and _env_name_refused(key):
                self._add(node, f"reads environment {key}")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = _str(node.slice)
        if key is not None and isinstance(node.ctx, ast.Load):
            if _is_sys_modules(node.value) and _root(key) in _HARNESS_MODULES:
                self._add(node, f"looks up {key!r} in sys.modules")
            elif _is_environ(node.value) and _env_name_refused(key):
                self._add(node, f"reads environment {key}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        key = _str(node.args[0]) if node.args else None
        if key is not None:
            finding = _call_finding(node.func, key)
            if finding:
                self._add(node, finding)
        self.generic_visit(node)


def _call_finding(func: ast.expr, key: str) -> str | None:
    """What a call with a string first argument reveals, if anything."""
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    owner = func.value if isinstance(func, ast.Attribute) else None
    if name in ("import_module", "__import__") and _root(key) in _IMPORT_REFUSED:
        return f"imports {key}"
    if owner is not None and _is_sys_modules(owner) and name in _ENV_READERS:
        harness = _root(key) in _HARNESS_MODULES
        return f"looks up {key!r} in sys.modules" if harness else None
    reads_env = name in ("getenv", "getenvb") or (
        owner is not None and _is_environ(owner) and name in _ENV_READERS
    )
    if reads_env and _env_name_refused(key):
        return f"reads environment {key}"
    return None


def findings_in(source: str) -> list[tuple[int, str]]:
    """Every test-awareness construct in ``source``, as (line, finding)."""
    return findings_in_tree(ast.parse(source))


def findings_in_tree(tree: ast.Module) -> list[tuple[int, str]]:
    """Every test-awareness construct in a parsed module, as (line, finding)."""
    finder = _Finder()
    finder.visit(tree)
    return sorted(finder.findings)


@pytest.mark.policy
def test_src_kew_never_detects_a_test_run() -> None:
    modules = sorted(SRC.rglob("*.py"))
    assert len(modules) > 50, f"found only {len(modules)} modules under {SRC}"
    offences = []
    used = set()
    for path in modules:
        relative = path.relative_to(SRC).as_posix()
        for line, finding in findings_in_tree(parse_file(str(path))):
            if (relative, finding) in EXCEPTIONS:
                used.add((relative, finding))
                continue
            offences.append(f"src/kew/{relative}:{line}: {finding}")
    assert not offences, (
        "production code must not special-case the test harness (#2480):\n"
        + "\n".join(offences)
    )
    assert used == set(EXCEPTIONS), f"stale exceptions: {set(EXCEPTIONS) - used}"


@pytest.mark.parametrize(
    "source",
    [
        'import os\nif os.environ.get("PYTEST_CURRENT_TEST"):\n    pass\n',
        'import os\nflag = os.environ["PYTEST_CURRENT_TEST"]\n',
        'import os\nflag = "PYTEST_CURRENT_TEST" in os.environ\n',
        'from os import getenv\nflag = getenv("PYTEST_CURRENT_TEST")\n',
        'import os\nNAME = "PYTEST_CURRENT_TEST"\nflag = os.environ.get(NAME)\n',
        'import sys\nif "pytest" in sys.modules:\n    pass\n',
        'import sys\nflag = "unittest" not in sys.modules\n',
        'import sys\nflag = sys.modules.get("_pytest")\n',
        'import sys\nflag = sys.modules["pytest"]\n',
        'from sys import modules\nflag = "pytest" in modules\n',
        "import pytest\n",
        "import _pytest.config\n",
        "from _pytest import nodes\n",
        "def f():\n    from pytest import MonkeyPatch\n",
        'import importlib\nimportlib.import_module("pytest")\n',
        'import os\nflag = os.getenv("TESTING")\n',
        'import os\nflag = os.environ.get("KEW_TEST_MODE", "")\n',
        'import os\nflag = os.environ["TEST"]\n',
        'import os\nflag = "APP_TESTS" in os.environ\n',
    ],
)
def test_a_synthetic_module_that_detects_a_test_run_is_refused(source: str) -> None:
    assert findings_in(source), source


@pytest.mark.parametrize(
    "source",
    [
        # Building a pytest command line or its environment is not a read.
        'env = {}\nenv["PYTEST_ADDOPTS"] = "-p no:cacheprovider"\n',
        'argv = ["uv", "run", "pytest", "-m", "not serial"]\n',
        'import os\nworkers = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")\n',
        "import sys\nmodule = sys.modules[__name__]\n",
        'import os\nflag = os.environ.get("KEW_ATTESTATION")\n',
        'import os\nflag = os.environ.get("LATEST_TAG")\n',
        'CHECK = "import site; site.main(); import pytest, kew"\n',
    ],
)
def test_ordinary_code_is_not_refused(source: str) -> None:
    assert findings_in(source) == []
