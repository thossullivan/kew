"""Test-quality scan — the counts `tests/test_test_quality_ratchet.py` ratchets (#2493).

Reads every ``test_*.py`` under ``kew/tests/`` with ``ast`` (never imports or
runs them) and reports, per file, how many of five weak-test shapes it holds:

``interaction_only``
    Test functions whose only checks are on a mock's calls
    (``m.assert_called_once_with(...)``, ``assert m.call_count == 2``) — they
    prove the code made a call, never that anything came of it.
``self_patch``
    Functions (tests or fixtures) that patch a function or class DEFINED in a
    ``kew`` module the file imports — ``patch("kew.cli.run")`` where ``run``
    is a ``def`` in ``kew/cli.py``, or ``setattr(kew.cli, "run", ...)`` through
    an imported name or a dotted ``kew.<module>`` chain. The test then replaces
    part of the code it is meant to be testing.
``source_text``
    Test functions that read source text (``inspect.getsource``, or a
    ``read_text``/``open`` of a ``.py`` path or a module's ``__file__``) — they
    pin spelling, not behaviour.
``skip_xfail``
    ``pytest.mark.skip``/``skipif``/``xfail`` markers and ``pytest.skip``/
    ``xfail``/``importorskip`` calls, through any import alias
    (``from pytest import mark as pm`` then ``@pm.skip``). ``getsource`` counts
    under an alias too.
``test_imports``
    Import statements that pull from another ``test_*.py`` module, which
    couples one test file's pass/fail to another's internals.

The baseline lives OUTSIDE ``tests/`` — ``kew/ratchets/test_quality.json`` —
so a change that adds a weak test cannot widen its own exceptions in the same
tree; ``kew/ratchets/`` is a merge-gate path and an always-guarded loop path.

The ratchet is two-way. A count above the baseline fails, naming the file and
the count. A count BELOW the baseline also fails, asking for the baseline to be
lowered, so a fix is banked and the debt can only shrink. The same goes for
file names: a new file named like a review round (``test_round*``,
``test_w42_round*``) or a PR/issue number is refused; the old ones are listed
in the baseline's ``legacy_names`` until they are renamed or deleted. An
acceptance contract, ``tests/acceptance/test_issue_<N>.py``, is the one
exception: its number names the item it was generated for.

Usage (from ``kew/``)::

    uv run python scripts/test_quality_scan.py            # current counts, JSON
    uv run python scripts/test_quality_scan.py --check    # the ratchet verdict
    uv run python scripts/test_quality_scan.py --lower    # bank decreases only
    uv run python scripts/test_quality_scan.py --initial  # first ledger only

``--lower`` writes ``ratchets/test_quality.json`` taking, per count, the LOWER
of the baseline and the tree, and drops legacy names that no longer exist. It
never raises a count or admits a new name: that edit is a human's, by hand.

``--initial`` writes the FIRST ledger: the tree's counts as they stand when the
ratchet lands, with every round- or PR-named file already present grandfathered
into ``legacy_names``. It refuses to run while a ledger exists, so it cannot be
used to widen one — re-recording means deleting the ledger, a visible edit to a
merge-gate path.

The scan fails when it cannot run — a missing tree or an unparseable file is
an error, never an empty (clean) result.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = PACKAGE_ROOT / "tests"
SOURCE_ROOT = PACKAGE_ROOT / "src"
BASELINE_PATH = PACKAGE_ROOT / "ratchets" / "test_quality.json"

METRICS = (
    "interaction_only",
    "self_patch",
    "source_text",
    "skip_xfail",
    "test_imports",
)

#: Names a review round or a PR/issue number into a test file. A file says what
#: behaviour it covers; "round 14" says only when someone was asked to write it.
_REFUSED_NAME = re.compile(
    r"^test_(?:.*_)?(?:w\d+_)?round\d"  # test_round13_x, test_w42_round14, ..._round2_
    r"|(?:^|_)(?:pr|issue|gh)?\d{3,}(?=[_.]|$)",  # test_issue_2493, test_2493_x
    re.IGNORECASE,  # test_Round99_x, test_PR2493_fix
)

_MOCK_ASSERTS = frozenset(
    {
        "assert_called",
        "assert_called_once",
        "assert_called_with",
        "assert_called_once_with",
        "assert_any_call",
        "assert_has_calls",
        "assert_not_called",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_any_await",
        "assert_has_awaits",
        "assert_not_awaited",
    }
)
_MOCK_ATTRS = frozenset(
    {
        "called",
        "call_count",
        "call_args",
        "call_args_list",
        "mock_calls",
        "method_calls",
        "await_count",
        "await_args",
        "await_args_list",
        "awaited",
    }
)
_SKIP_ATTRS = frozenset({"skip", "skipif", "xfail", "importorskip"})
_GETSOURCE = frozenset({"getsource", "getsourcelines", "getsourcefile"})
_READS = frozenset({"read_text", "read_bytes", "open"})


class ScanCannotRunError(RuntimeError):
    """The scan could not be performed — never reported as "no findings"."""


@dataclass
class FileCounts:
    """The five counts for one test file (zero when the shape is absent)."""

    interaction_only: int = 0
    self_patch: int = 0
    source_text: int = 0
    skip_xfail: int = 0
    test_imports: int = 0

    def nonzero(self) -> dict[str, int]:
        return {m: getattr(self, m) for m in METRICS if getattr(self, m)}


@dataclass
class Baseline:
    """The committed ledger: per-file counts and grandfathered names."""

    files: dict[str, dict[str, int]] = field(default_factory=dict)
    legacy_names: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------


def _terminal_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


@dataclass(frozen=True)
class _Bindings:
    """Names a test file binds to ``pytest``, ``pytest.mark``, a skip/xfail
    function, or ``inspect.getsource`` — under any alias."""

    pytest: frozenset[str]
    mark: frozenset[str]
    skips: frozenset[str]
    getsource: frozenset[str]


def _bound_from(nodes: list[ast.AST], module: str, names: frozenset[str]) -> set[str]:
    """Local names that ``from <module> import <name> [as x]`` binds to *names*."""
    return {
        alias.asname or alias.name
        for node in nodes
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
        if alias.name in names
    }


def _bindings(nodes: list[ast.AST]) -> _Bindings:
    pytest = {"pytest"} | {
        alias.asname
        for node in nodes
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "pytest" and alias.asname
    }
    return _Bindings(
        pytest=frozenset(pytest),
        mark=frozenset(_bound_from(nodes, "pytest", frozenset({"mark"}))),
        skips=frozenset(_bound_from(nodes, "pytest", _SKIP_ATTRS)),
        getsource=_GETSOURCE | _bound_from(nodes, "inspect", _GETSOURCE),
    )


def _is_pytest(node: ast.expr, bound: _Bindings) -> bool:
    """``pytest`` or ``pytest.mark``, through any import alias."""
    if isinstance(node, ast.Name):
        return node.id in bound.pytest or node.id in bound.mark
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "mark"
        and isinstance(node.value, ast.Name)
        and node.value.id in bound.pytest
    )


def _is_skip(node: ast.AST, bound: _Bindings) -> bool:
    """A skip/xfail marker or call: ``pytest.mark.skip``, ``pm.skip``, ``skip``."""
    if isinstance(node, ast.Attribute):
        return node.attr in _SKIP_ATTRS and _is_pytest(node.value, bound)
    return isinstance(node, ast.Name) and node.id in bound.skips


@cache
def _module_file(module: str, src_root: Path) -> Path | None:
    base = src_root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


@cache
def _defined_names(module_file: Path) -> frozenset[str]:
    """Top-level ``def``/``class`` names of a source module."""
    try:
        tree = ast.parse(module_file.read_text(), filename=str(module_file))
    except (OSError, SyntaxError) as exc:
        raise ScanCannotRunError(f"cannot read {module_file}: {exc}") from exc
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def _imported_kew_modules(
    nodes: list[ast.AST], src_root: Path
) -> tuple[set[str], dict[str, str]]:
    """The ``kew`` modules a test file imports, and the names bound to them."""
    modules: set[str] = set()
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "kew" or alias.name.startswith("kew."):
                    modules.add(alias.name)
                    if alias.asname:
                        aliases[alias.asname] = alias.name
                    else:  # ``import kew.ids`` binds ``kew``
                        aliases.setdefault("kew", "kew")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if not (node.module == "kew" or node.module.startswith("kew.")):
                continue
            for alias in node.names:
                child = f"{node.module}.{alias.name}"
                if _module_file(child, src_root) is not None:
                    modules.add(child)
                    aliases[alias.asname or alias.name] = child
                else:
                    modules.add(node.module)
    return modules, aliases


def _defines(module: str, attr: str, modules: set[str], src_root: Path) -> bool:
    if module not in modules:
        return False
    path = _module_file(module, src_root)
    return path is not None and attr in _defined_names(path)


def _string_target_is_self(target: str, modules: set[str], src_root: Path) -> bool:
    """``"kew.a.b.func"`` / ``"kew.a.b.Cls.meth"`` patches a def in ``kew.a.b``."""
    parts = target.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:cut])
        if _module_file(module, src_root) is not None:
            return _defines(module, parts[cut], modules, src_root)
    return False


def _dotted_module(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """``kew.ids`` / ``graph.tick`` as the ``kew`` module it names, if any."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        parent = _dotted_module(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _is_self_patch(
    call: ast.Call, modules: set[str], aliases: dict[str, str], src_root: Path
) -> bool:
    func = call.func
    name = _terminal_name(func)
    is_patch = name == "patch"
    is_object = (
        name == "object"
        and isinstance(func, ast.Attribute)
        and _terminal_name(func.value) == "patch"
    )
    is_setattr = name == "setattr" and isinstance(func, ast.Attribute)
    if not (is_patch or is_object or is_setattr) or not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        if is_object:
            return False
        return _string_target_is_self(first.value, modules, src_root)
    if (is_object or is_setattr) and len(call.args) >= 2:
        second = call.args[1]
        module = _dotted_module(first, aliases)
        if (
            module is not None
            and isinstance(second, ast.Constant)
            and isinstance(second.value, str)
        ):
            # A dotted reference names its module as surely as an import does.
            named = modules if isinstance(first, ast.Name) else modules | {module}
            return _defines(module, second.value, named, src_root)
    return False


def _is_interaction_check(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Attribute) and sub.attr in _MOCK_ATTRS
        for sub in ast.walk(node)
    )


def _interaction_only(nodes: list[ast.AST]) -> bool:
    """Checks on a mock's calls, and no check of anything else.

    Any other ``assert``, an ``assert*`` helper call, or ``pytest.raises`` /
    ``warns`` is a check of an effect, so the test is not interaction-only.
    """
    interactions = state = 0
    for node in nodes:
        if isinstance(node, ast.Assert):
            if _is_interaction_check(node.test):
                interactions += 1
            else:
                state += 1
        elif isinstance(node, ast.Call):
            name = _terminal_name(node.func)
            if name in _MOCK_ASSERTS:
                interactions += 1
            elif "assert" in name.lower() or name in {"raises", "warns"}:
                state += 1
    return interactions > 0 and state == 0


def _source_ish_names(tree: ast.Module) -> set[str]:
    """Module-level names bound to a ``.py`` path or a module's ``__file__``."""
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        if _mentions_source(list(ast.walk(node.value)), set()):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _mentions_source(nodes: list[ast.AST], source_names: set[str]) -> bool:
    return any(
        (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and sub.value.endswith(".py")
        )
        or (isinstance(sub, ast.Attribute) and sub.attr == "__file__")
        or (isinstance(sub, ast.Name) and sub.id in source_names)
        for sub in nodes
    )


def _reads_source(
    nodes: list[ast.AST], source_names: set[str], getsource: frozenset[str]
) -> bool:
    reads = False
    for node in nodes:
        if isinstance(node, ast.Call):
            name = _terminal_name(node.func)
            if name in getsource:
                return True
            reads = reads or name in _READS
    return reads and _mentions_source(nodes, source_names)


_Unit = ast.FunctionDef | ast.AsyncFunctionDef


def _units(tree: ast.Module) -> list[_Unit]:
    """Module-level functions and methods of module-level classes."""
    units: list[_Unit] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append(node)
        elif isinstance(node, ast.ClassDef):
            units.extend(
                n
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return units


def _index(
    tree: ast.Module,
) -> tuple[list[ast.AST], dict[_Unit, list[ast.AST]], dict[_Unit, list[ast.AST]]]:
    """One pass over the tree: every node, and each unit's nodes with and
    without its decorators. ``ast.walk`` per question made the scan 3x slower."""
    units = set(_units(tree))
    everything: list[ast.AST] = []
    whole: dict[_Unit, list[ast.AST]] = {u: [] for u in units}
    body: dict[_Unit, list[ast.AST]] = {u: [] for u in units}
    stack: list[tuple[ast.AST, _Unit | None, bool]] = [(tree, None, False)]
    while stack:
        node, unit, in_body = stack.pop()
        everything.append(node)
        if unit is not None:
            whole[unit].append(node)
            if in_body:
                body[unit].append(node)
        if node in units:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            decorators = set(map(id, node.decorator_list))
            stack.extend(
                (child, node, id(child) not in decorators)
                for child in ast.iter_child_nodes(node)
            )
        else:
            stack.extend((child, unit, in_body) for child in ast.iter_child_nodes(node))
    return everything, whole, body


def _imports_a_test_module(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        dotted = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        dotted = [node.module or ""]
        if not node.module or node.level:
            dotted += [alias.name for alias in node.names]
    else:
        return False
    return any(part.startswith("test_") for name in dotted for part in name.split("."))


# --- the scan ----------------------------------------------------------------


def scan_source(text: str, relpath: str, src_root: Path = SOURCE_ROOT) -> FileCounts:
    """Count the five shapes in one test module's source text."""
    try:
        tree = ast.parse(text, filename=relpath)
    except SyntaxError as exc:
        raise ScanCannotRunError(f"cannot parse {relpath}: {exc}") from exc
    everything, whole, body = _index(tree)
    modules, aliases = _imported_kew_modules(everything, src_root)
    source_names = _source_ish_names(tree)
    bound = _bindings(everything)
    counts = FileCounts()
    for unit, nodes in whole.items():
        if unit.name.startswith("test"):
            counts.interaction_only += _interaction_only(body[unit])
            counts.source_text += _reads_source(nodes, source_names, bound.getsource)
        counts.self_patch += any(
            isinstance(node, ast.Call)
            and _is_self_patch(node, modules, aliases, src_root)
            for node in nodes
        )
    for node in everything:
        if _is_skip(node, bound):
            counts.skip_xfail += 1
        elif _imports_a_test_module(node):
            counts.test_imports += 1
    return counts


def _refuse_symlinks(tests_root: Path, relative_to: Path) -> None:
    """A symlink under the tree would hide files from the walk, so it is an error.

    ``Path.rglob`` does not descend into a symlinked directory, and a symlinked
    file reads content kept outside the tree; either lets a test escape the scan.
    """
    for path in tests_root.rglob("*"):
        if path.is_symlink():
            relpath = path.relative_to(relative_to).as_posix()
            raise ScanCannotRunError(f"symlink under the tests tree: {relpath}")


def scan_tree(
    tests_root: Path = TESTS_ROOT,
    relative_to: Path = PACKAGE_ROOT,
    src_root: Path = SOURCE_ROOT,
) -> dict[str, FileCounts]:
    """Counts for every ``test_*.py`` under *tests_root*, keyed by relative path."""
    if not tests_root.is_dir():
        raise ScanCannotRunError(f"tests tree not found: {tests_root}")
    _refuse_symlinks(tests_root, relative_to)
    found: dict[str, FileCounts] = {}
    for path in sorted(tests_root.rglob("test_*.py")):
        relpath = path.relative_to(relative_to).as_posix()
        try:
            text = path.read_text()
        except OSError as exc:
            raise ScanCannotRunError(f"cannot read {relpath}: {exc}") from exc
        found[relpath] = scan_source(text, relpath, src_root)
    if not found:
        raise ScanCannotRunError(f"no test_*.py files under {tests_root}")
    _list_contract_dir(tests_root / "acceptance", relative_to, found)
    return found


def _list_contract_dir(
    contracts: Path, relative_to: Path, found: dict[str, FileCounts]
) -> None:
    """Key every file in the contract directory, not only ``test_*.py``.

    A helper module a contract imports, or any other file there, would
    otherwise sit outside the scan; keyed, the name rule refuses it.
    """
    if not contracts.is_dir():
        return
    for path in sorted(contracts.rglob("*")):
        if path.is_dir() or "__pycache__" in path.relative_to(contracts).parts:
            continue
        found.setdefault(path.relative_to(relative_to).as_posix(), FileCounts())


#: An acceptance contract (#2488) is generated and keyed by its item: in
#: ``tests/acceptance/test_issue_<N>.py`` the number is the contract's identity,
#: not a review round. That directory holds contracts only, so that exact shape
#: is the one name it accepts; every count still applies to it.
_CONTRACT_DIR = "tests/acceptance"
_CONTRACT_NAME = re.compile(r"test_issue_[1-9][0-9]*")  # ASCII, no leading zero


def _in_contract_dir(relpath: str) -> bool:
    return Path(relpath).as_posix().startswith(f"{_CONTRACT_DIR}/")


def refused_name(relpath: str) -> bool:
    """A file named for a review round or a PR/issue number, or any file in the
    contract directory that is not a contract."""
    path = Path(relpath)
    if _in_contract_dir(relpath):
        in_place = path.parent.as_posix() == _CONTRACT_DIR
        return not (
            in_place and path.suffix == ".py" and _CONTRACT_NAME.fullmatch(path.stem)
        )
    return _REFUSED_NAME.search(path.stem) is not None


# --- the baseline and the ratchet --------------------------------------------


def load_baseline(path: Path = BASELINE_PATH) -> Baseline:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ScanCannotRunError(f"cannot read baseline {path}: {exc}") from exc
    return Baseline(files=raw["files"], legacy_names=raw["legacy_names"])


def render_baseline(baseline: Baseline) -> str:
    files = {
        key: {m: baseline.files[key][m] for m in METRICS if baseline.files[key].get(m)}
        for key in sorted(baseline.files)
    }
    payload = {
        "description": (
            "Test-quality debt ledger (#2493) - NOT an approval list. Counts may "
            "only fall; scripts/test_quality_scan.py --lower banks a fall. See "
            "the scan's docstring for what each count means."
        ),
        "legacy_names": sorted(baseline.legacy_names),
        "files": {key: counts for key, counts in files.items() if counts},
    }
    return json.dumps(payload, indent=2) + "\n"


def ratchet_problems(counts: dict[str, FileCounts], baseline: Baseline) -> list[str]:
    """Every way *counts* differs from *baseline*, worded for the person fixing it.

    Empty means the tree matches the ledger exactly.
    """
    rises: list[str] = []
    falls: list[str] = []
    for key in sorted(set(counts) | set(baseline.files)):
        have = counts.get(key, FileCounts())
        recorded = baseline.files.get(key, {})
        for metric in METRICS:
            now, was = getattr(have, metric), recorded.get(metric, 0)
            if now > was:
                rises.append(f"{key}: {metric} {now} (baseline {was})")
            elif now < was:
                falls.append(f"{key}: {metric} {now} (baseline {was})")

    legacy = set(baseline.legacy_names)
    refused = sorted(k for k in counts if refused_name(k) and k not in legacy)
    strays = [k for k in refused if _in_contract_dir(k)]
    new_names = [k for k in refused if not _in_contract_dir(k)]
    gone_names = sorted(legacy - set(counts))

    problems: list[str] = []
    if rises:
        problems.append(
            "Test-quality counts rose above the baseline — fix the test (assert "
            "an effect, not a call; do not patch the code under test; do not "
            "read source text; do not import from another test module). The "
            "baseline in ratchets/test_quality.json is not raised to make room:"
            "\n  " + "\n  ".join(rises)
        )
    if strays:
        problems.append(
            "Not an acceptance contract, whatever the name — only acceptance "
            f"contracts (test_issue_N.py) belong in {_CONTRACT_DIR}; move these "
            "out:\n  " + "\n  ".join(strays)
        )
    if new_names:
        problems.append(
            "New test file(s) named for a review round or a PR/issue number — "
            "name the file for the behaviour it covers:\n  " + "\n  ".join(new_names)
        )
    if falls:
        problems.append(
            "Test-quality counts fell below the baseline — good. Please lower the "
            "baseline to bank it (`uv run python scripts/test_quality_scan.py "
            "--lower`):\n  " + "\n  ".join(falls)
        )
    if gone_names:
        problems.append(
            "Legacy names no longer in the tree — please lower the baseline by "
            "dropping them from legacy_names (`--lower` does it):\n  "
            + "\n  ".join(gone_names)
        )
    return problems


def lowered(counts: dict[str, FileCounts], baseline: Baseline) -> Baseline:
    """The baseline with every fall banked and no rise admitted."""
    files: dict[str, dict[str, int]] = {}
    for key, recorded in baseline.files.items():
        have = counts.get(key, FileCounts())
        files[key] = {m: min(recorded.get(m, 0), getattr(have, m)) for m in METRICS}
    legacy = [name for name in baseline.legacy_names if name in counts]
    return Baseline(files=files, legacy_names=legacy)


def initial(counts: dict[str, FileCounts]) -> Baseline:
    """The first ledger: the tree as it stands, its odd names grandfathered.

    The contract directory is never grandfathered: it holds generated contracts
    only, so anything else there is an error, not history.
    """
    strays = sorted(
        key for key in counts if _in_contract_dir(key) and refused_name(key)
    )
    if strays:
        raise ScanCannotRunError(
            f"not an acceptance contract, in {_CONTRACT_DIR}: {', '.join(strays)}"
        )
    files = {key: vars(have).copy() for key, have in counts.items() if have.nonzero()}
    legacy = sorted(key for key in counts if refused_name(key))
    return Baseline(files=files, legacy_names=legacy)


def write_initial(counts: dict[str, FileCounts], path: Path = BASELINE_PATH) -> None:
    """Write :func:`initial` to *path* — never over an existing ledger."""
    if path.exists():
        raise ScanCannotRunError(
            f"{path} already exists — --initial only writes the first ledger; "
            "use --lower to bank a fall"
        )
    path.write_text(render_baseline(initial(counts)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print the verdict")
    mode.add_argument("--lower", action="store_true", help="bank decreases only")
    mode.add_argument(
        "--initial", action="store_true", help="write the first ledger only"
    )
    args = parser.parse_args(argv)

    counts = scan_tree()
    if args.initial:
        write_initial(counts)
        return 0
    if args.lower:
        BASELINE_PATH.write_text(render_baseline(lowered(counts, load_baseline())))
        return 0
    if args.check:
        problems = ratchet_problems(counts, load_baseline())
        print("\n\n".join(problems) or "test-quality ratchet: clean")
        return 1 if problems else 0
    print(
        json.dumps({k: c.nonzero() for k, c in counts.items() if c.nonzero()}, indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
