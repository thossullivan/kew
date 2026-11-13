"""#2174 gate — every repo-wide scan test carries the ``policy`` mark.

A governed remediation round runs only the tests covering the files it changed
(#2115). A gate that *scans* the repository — the silence gate, the id sweep,
the docs truthfulness checks — is covered by no changed file, so targeted
selection never picks it. #2140 patched that with a hand list of paths in
``[commands] test_policy_paths``; the list named the silence gate and nothing
else, so PR #2154 shipped an unguarded ``int(item_id)`` through a green
targeted round and failed ``tests/test_id_sweep_gate.py`` on the merge verify.

The set is now the ``policy`` mark the gates carry, and this module keeps the
mark honest from the other side: it finds the repo-wide scans by their shape
and fails when one is unmarked. A scan test is one that

  * iterates a path derived from a ``__file__``, named either as the call
    receiver (``ROOT.rglob("*.py")``, ``ROOT.walk()``) or as an argument
    (``os.walk(ROOT)``, ``glob.glob(str(ROOT / "**"))``), or
  * calls a module-level helper that does, or
  * hands a module-level ``__file__``-derived path to a helper that iterates.

The shape catches the gates; it also catches a few tests that merely walk a
fixture tree. Those are listed in ``_EXEMPT`` with a reason, as are the real
gates too slow or too environment-bound to run every remediation round.
Exempting is safe by construction: an omission from ``_EXEMPT`` fails this
gate, while an omission from a hand list of gates fails nothing until a
remediation ships past it.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.policy

TESTS_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = TESTS_ROOT.parent
REPO_ROOT = PACKAGE_ROOT.parent
POLICY_MARK = "policy"

# Directory walks, by the shape that names the walked root. A path method names
# it as the receiver (``ROOT.rglob("*.py")``, ``ROOT.walk()`` on 3.12+); the
# module-level forms name it as an argument (``os.walk(ROOT)``,
# ``glob.glob(str(ROOT / "**"))``).
_ITER_METHODS = {"rglob", "glob", "iterdir", "walk"}
_ITER_FUNCTIONS = {"os.walk", "os.scandir", "os.listdir", "glob.glob", "glob.iglob"}

# Same method name, no directory: ``ast.walk`` walks a syntax tree, and this
# gate and most of the others call it. Matched on the unparsed callee, so it
# stays out while ``Path.walk`` — a real directory walk — stays in.
_TREE_WALKS = {"ast.walk"}

# The last name any of the shapes above can end in — the cheap pre-filter in
# ``_is_iteration_call``. Derived from the sets themselves, so a shape added
# there cannot be filtered out here.
_ITER_NAMES = (
    _ITER_METHODS
    | {function.rsplit(".", 1)[-1] for function in _ITER_FUNCTIONS}
    | {walk.rsplit(".", 1)[-1] for walk in _TREE_WALKS}
)


# The same names as a source-text filter: a callee is spelled in the source it
# was parsed from, so a module naming none of them cannot hold a call any
# shape above matches, and is not worth reading as a tree. Derived from the
# sets, so a shape added to them widens this with it.
_ITER_NAME_TOKENS = re.compile("|".join(sorted(re.escape(n) for n in _ITER_NAMES)))


class _Facts(NamedTuple):
    """What one walk of a node answers: see :func:`_facts`."""

    names: frozenset[str]
    assignments: tuple[tuple[ast.expr, tuple[str, ...]], ...]
    iterations: tuple[ast.Call, ...]


# path -> (mtime, tree) and path -> (mtime, scan test names). Both sweeps below
# read every test module in the repo, so they share the parse and the analysis.
_PARSED: dict[Path, tuple[int, ast.Module]] = {}
_SCANNED: dict[Path, tuple[int, tuple[str, ...]]] = {}

# The analysis of ONE module, memoised per node. The shapes below ask the same
# question of the same node over and over — every function in a module reads
# that module's whole assignment chain, three times — and `ast.walk` and
# `ast.unparse` are the entire cost of this gate (#2340: 21 s, 300k walks of
# the same few nodes). An answer about a node cannot change while the tree is
# alive, so each one is computed once and the memos are dropped when the next
# module is analysed, which keeps them the size of a single module.
_FACTS: dict[ast.AST, _Facts] = {}
_FILE_DUNDER: dict[ast.AST, bool] = {}

# Scan-shaped tests that stay out of the policy set, each with the reason it
# is not a gate every remediation must run. Kept live by
# ``test_every_exemption_still_names_a_scan_shaped_test``.
_EXEMPT: dict[str, str] = {
    f"{module}::{test}": reason
    for module, test, reason in (
        (
            "test_agent_image_scan_gate.py",
            "test_packaged_inputs_equal_the_repository_source",
            "builds and scans the agent image; a remediation round has no"
            " docker daemon",
        ),
        (
            "test_docker_daemon_lock.py",
            "test_multiple_waiters_all_acquire",
            "walks a tmp lock directory, not the repository",
        ),
        (
            "test_docker_daemon_lock.py",
            "test_unsuccessful_release_waits_for_verified_drain",
            "walks a tmp lock directory, not the repository",
        ),
        (
            "test_docker_daemon_lock.py",
            "test_release_coordination_inside_workspace_is_not_a_producer",
            "walks a tmp lock directory, not the repository",
        ),
        (
            "test_docker_daemon_lock.py",
            "test_following_job_acquire_is_not_a_producer",
            "walks a tmp lock directory, not the repository",
        ),
        (
            "test_docker_daemon_lock.py",
            "test_script_verb_recognizes_the_native_interpreter_in_real_ps",
            "walks a tmp lock directory, not the repository",
        ),
        (
            "test_docs_truthfulness.py",
            "test_docs_build_renders_explicit_ids_and_fails_on_a_broken_anchor",
            "runs a full mkdocs build; the docs workflow gate owns it",
        ),
        (
            "test_wheel_contents.py",
            "test_wheel_carries_license_and_notice",
            "builds a wheel; the release gate owns it",
        ),
        (
            "test_wheel_contents.py",
            "test_wheel_preserves_docker_input_executable_bits",
            "builds a wheel; the release gate owns it",
        ),
        (
            "test_wheel_contents.py",
            "test_installed_wheel_cli_runs_the_packaged_builder",
            "builds and installs a wheel; the release gate owns it",
        ),
        (
            "test_wheel_contents.py",
            "test_runtime_file_list_matches_source_tree",
            "builds a wheel; the release gate owns it",
        ),
        (
            "test_wheel_contents.py",
            "test_wheel_contains_the_default_provisioning_recipe",
            "builds a wheel; the release gate owns it",
        ),
        (
            "test_wheel_contents.py",
            "test_wheel_contains_the_scanner_pin_the_recipe_reads",
            "builds a wheel; the release gate owns it",
        ),
    )
}


def _forget_module_analysis() -> None:
    """Drop the per-node memos: the module they describe is done with."""
    _FACTS.clear()
    _FILE_DUNDER.clear()


def _facts(node: ast.AST) -> _Facts:
    """The three things the shapes above ask of a node, from one walk of it.

    Each of them used to walk the node separately, and every function in a
    module was asked all three while the module itself was asked them again:
    three walks per function became one, memoised (#2340).
    """
    known = _FACTS.get(node)
    if known is None:
        names: set[str] = set()
        assignments: list[tuple[ast.expr, tuple[str, ...]]] = []
        iterations: list[ast.Call] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Assign):
                assignments.append(
                    (
                        child.value,
                        tuple(t.id for t in child.targets if isinstance(t, ast.Name)),
                    )
                )
            elif _is_iteration_call(child):
                iterations.append(child)
        known = _Facts(frozenset(names), tuple(assignments), tuple(iterations))
        _FACTS[node] = known
    return known


def _referenced_names(node: ast.AST) -> frozenset[str]:
    return _facts(node).names


def _carries_a_file_dunder(node: ast.AST) -> bool:
    """Does this expression spell ``__file__`` anywhere inside it?"""
    carries = _FILE_DUNDER.get(node)
    if carries is None:
        carries = "__file__" in ast.unparse(node)
        _FILE_DUNDER[node] = carries
    return carries


def _mentions_repo_root(node: ast.AST, rooted_names: set[str]) -> bool:
    """Is this expression derived from a ``__file__`` (so, from the checkout)?

    Matched on the identifiers the expression references, not on a substring of
    its source: this also reads iteration *arguments*, and a short rooted name
    is a substring of half the string literals in the tree.
    """
    if _carries_a_file_dunder(node):
        return True
    return bool(_referenced_names(node) & rooted_names)


def _rooted_names(owner: ast.AST, seed: set[str]) -> set[str]:
    """Names bound to a checkout path, following assignment chains."""
    names = set(seed)
    assignments = _facts(owner).assignments
    for _ in range(3):  # a fixpoint in practice: SRC = ROOT / "src" / "kew"
        before = len(names)
        for value, targets in assignments:
            if _mentions_repo_root(value, names):
                names.update(targets)
        if len(names) == before:  # a pass that binds nothing ends the chain
            break
    return names


def _is_iteration_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    # The cheap half of the question first: a call can only be one of the
    # shapes above if the last name in its callee is one of ``_ITER_NAMES``,
    # and almost no call in a test module is. Unparsing the callee of every
    # call in the suite was a third of this gate's cost (#2340).
    last = node.func.attr if isinstance(node.func, ast.Attribute) else None
    if last is None and isinstance(node.func, ast.Name):
        last = node.func.id
    if last is not None and last not in _ITER_NAMES:
        return False
    callee = ast.unparse(node.func)
    if callee in _TREE_WALKS:
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr in _ITER_METHODS:
        return True
    return callee in _ITER_FUNCTIONS


def _iteration_calls(node: ast.AST) -> tuple[ast.Call, ...]:
    """Every directory walk inside ``node``, in either call shape."""
    return _facts(node).iterations


def _iteration_targets(call: ast.Call) -> list[ast.expr]:
    """Expressions that could name the directory ``call`` walks.

    The receiver carries it for ``ROOT.rglob(...)``; an argument carries it for
    ``os.walk(ROOT)`` and ``glob.glob(str(ROOT / "**"))``. Reading only the
    receiver missed both module-level forms this gate advertises (#2174).
    """
    targets: list[ast.expr] = list(call.args)
    targets.extend(keyword.value for keyword in call.keywords)
    if isinstance(call.func, ast.Attribute):
        targets.append(call.func.value)
    return targets


def _walks_the_checkout(
    function: ast.FunctionDef | ast.AsyncFunctionDef, rooted: set[str]
) -> bool:
    """Does this function iterate a directory under the checkout itself?"""
    calls = _iteration_calls(function)
    if not calls:
        # Nothing here iterates anything, so no name this function binds can
        # make it a checkout walk: don't follow its assignment chain at all.
        # Most functions in the suite are this one, and following the chain
        # for each of them was the bulk of this gate's remaining cost (#2340).
        return False
    names = _rooted_names(function, rooted)
    return any(
        _mentions_repo_root(target, names)
        for call in calls
        for target in _iteration_targets(call)
    )


def _scanning_helpers(tree: ast.Module, rooted: set[str]) -> tuple[set[str], set[str]]:
    """Module-level helpers that walk the checkout, and that walk anything.

    The second set is wider on purpose: a helper taking the root as a parameter
    (``scan_tree(root)``) walks whatever it is handed, so only its caller shows
    that the target is the checkout.
    """
    direct = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and _walks_the_checkout(node, rooted)
    }
    any_walk = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and _iteration_calls(node)
    }
    for _ in range(2):  # helpers that delegate to helpers
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and _referenced_names(node) & direct:
                direct.add(node.name)
    return direct, any_walk


def _parse(path: Path, source: str | None = None) -> ast.Module:
    """Parse ``path`` once per revision — both sweeps read every test module."""
    stamp = path.stat().st_mtime_ns
    cached = _PARSED.get(path)
    if cached is None or cached[0] != stamp:
        text = path.read_text(encoding="utf-8") if source is None else source
        cached = (stamp, ast.parse(text))
        _PARSED[path] = cached
    return cached[1]


def could_iterate_a_directory(source: str) -> bool:
    """Could any call in this source be one of the walks above?

    ``_is_iteration_call`` only ever says yes to a callee whose last name is
    one of ``_ITER_NAMES``, and that name is spelled in the source the call was
    parsed from — so a module naming none of them holds no scan test whatever
    its shape. Two thirds of this suite's test modules are in that class, and
    parsing them was most of what this gate spent (#2340). A callee assembled
    at runtime escapes the detector itself, with or without this filter;
    ``test_the_source_filter_cannot_hide_a_shape_the_detector_reads`` pins the
    two to each other.
    """
    return _ITER_NAME_TOKENS.search(source) is not None


def repo_wide_scan_tests(path: Path) -> list[str]:
    """Names of the tests in ``path`` that scan the repository tree."""
    stamp = path.stat().st_mtime_ns
    remembered = _SCANNED.get(path)
    if remembered is not None and remembered[0] == stamp:
        return list(remembered[1])
    source = path.read_text(encoding="utf-8")
    found = (
        _find_scan_tests(_parse(path, source))
        if could_iterate_a_directory(source)
        else []
    )
    _SCANNED[path] = (stamp, tuple(found))
    return found


def _find_scan_tests(tree: ast.Module) -> list[str]:
    _forget_module_analysis()
    rooted = _rooted_names(tree, set())
    direct, any_walk = _scanning_helpers(tree, rooted)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        used = _referenced_names(node)
        if (
            _walks_the_checkout(node, rooted)
            or used & direct
            or (used & any_walk and used & rooted)
        ):
            found.append(node.name)
    return found


def _mark_names(decorator: ast.expr) -> str | None:
    """The mark name in ``@pytest.mark.x`` / ``pytest.mark.x(...)``, else None."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute) and "pytest.mark" in ast.unparse(target.value):
        return target.attr
    return None


def _module_is_marked(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        if f"pytest.mark.{POLICY_MARK}" in ast.unparse(node.value):
            return True
    return False


def policy_marked_tests(path: Path) -> set[str]:
    """Test names in ``path`` carrying the policy mark, by module/class/decorator."""
    tree = _parse(path)
    marked: set[str] = set()
    if _module_is_marked(tree):
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        }
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _module_is_marked(
            ast.Module(body=node.body, type_ignores=[])
        ):
            marked.update(
                child.name
                for child in ast.walk(node)
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            )
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            _mark_names(decorator) == POLICY_MARK for decorator in node.decorator_list
        ):
            marked.add(node.name)
    return marked


def _test_modules() -> list[Path]:
    return sorted(TESTS_ROOT.rglob("test_*.py"))


def test_every_repo_wide_scan_test_carries_the_policy_mark() -> None:
    """A scan gate that no changed file selects must be selected by the mark."""
    unmarked: list[str] = []
    for path in _test_modules():
        scans = repo_wide_scan_tests(path)
        if not scans:  # the marks of a module with no scan gate decide nothing
            continue
        relative = path.relative_to(TESTS_ROOT).as_posix()
        marked = policy_marked_tests(path)
        unmarked.extend(
            f"{relative}::{name}"
            for name in scans
            if name not in marked and f"{relative}::{name}" not in _EXEMPT
        )
    assert not unmarked, (
        "These tests scan the repository tree, so no targeted remediation round"
        f" selects them. Mark each with @pytest.mark.{POLICY_MARK} so"
        " `pytest -m policy` runs it, or add it to _EXEMPT with the reason it is"
        " not a gate every round must run:\n  " + "\n  ".join(sorted(unmarked))
    )


def test_every_exemption_still_names_a_scan_shaped_test() -> None:
    """A stale exemption hides a gate — drop it when the test moves or goes."""
    live: set[str] = set()
    for path in _test_modules():
        relative = path.relative_to(TESTS_ROOT).as_posix()
        live.update(f"{relative}::{name}" for name in repo_wide_scan_tests(path))
    stale = sorted(key for key in _EXEMPT if key not in live)
    assert not stale, (
        "_EXEMPT names tests that no longer scan the repository tree (renamed,"
        " deleted, or rewritten). Remove each stale entry:\n  " + "\n  ".join(stale)
    )
    assert all(reason.strip() for reason in _EXEMPT.values()), (
        "every _EXEMPT entry needs a reason"
    )


def test_the_gate_detects_a_scan_and_reads_every_mark_form(tmp_path: Path) -> None:
    """The detector's own proof: shape found, all three mark forms honoured."""
    module = tmp_path / "test_sample.py"
    module.write_text(
        "import pytest\n"
        "from pathlib import Path\n"
        'SRC = Path(__file__).resolve().parents[1] / "src"\n'
        "def scan_tree(root):\n"
        '    return sorted(root.rglob("*.py"))\n'
        "def test_unmarked_scan():\n"
        '    assert list(SRC.rglob("*.py"))\n'
        "def test_helper_scan():\n"
        "    assert scan_tree(SRC)\n"
        "@pytest.mark.policy\n"
        "def test_decorated_scan():\n"
        '    assert list(SRC.rglob("*.py"))\n'
        "def test_not_a_scan(tmp_path):\n"
        '    assert not list(tmp_path.rglob("*.py"))\n',
        encoding="utf-8",
    )
    assert repo_wide_scan_tests(module) == [
        "test_unmarked_scan",
        "test_helper_scan",
        "test_decorated_scan",
    ]
    assert policy_marked_tests(module) == {"test_decorated_scan"}

    marked_module = tmp_path / "test_marked.py"
    marked_module.write_text(
        "import pytest\npytestmark = pytest.mark.policy\ndef test_one():\n    pass\n",
        encoding="utf-8",
    )
    assert policy_marked_tests(marked_module) == {"test_one"}

    listed = tmp_path / "test_listed.py"
    listed.write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.serial, pytest.mark.policy]\n"
        "def test_one():\n    pass\n",
        encoding="utf-8",
    )
    assert policy_marked_tests(listed) == {"test_one"}


def test_the_gate_detects_the_module_level_walk_shapes(tmp_path: Path) -> None:
    """#2174 regression — the root can be an argument, not the receiver.

    ``os.walk(ROOT)`` and ``glob.glob(str(ROOT / "**"))`` are two of the shapes
    this module advertises, and both name the checkout in an argument. Reading
    only the call receiver detected neither, so a repo-wide gate written that
    way stayed unmarked and this gate stayed green. ``ast.walk`` walks a tree,
    not a directory, and must stay out however rooted its argument is.
    """
    module = tmp_path / "test_module_level.py"
    module.write_text(
        "import ast\n"
        "import glob\n"
        "import os\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def test_os_walk_scan():\n"
        "    assert any(files for _, _, files in os.walk(ROOT))\n"
        "def test_os_walk_keyword_scan():\n"
        "    assert any(files for _, _, files in os.walk(top=ROOT))\n"
        "def test_glob_glob_scan():\n"
        '    assert glob.glob(str(ROOT / "**" / "*.py"), recursive=True)\n'
        "def test_os_walk_of_a_tmp_tree(tmp_path):\n"
        "    assert not list(os.walk(tmp_path))\n"
        "def test_ast_walk_is_not_a_directory_walk():\n"
        '    tree = ast.parse((ROOT / "pyproject.toml").read_text())\n'
        "    assert list(ast.walk(tree))\n",
        encoding="utf-8",
    )
    assert repo_wide_scan_tests(module) == [
        "test_os_walk_scan",
        "test_os_walk_keyword_scan",
        "test_glob_glob_scan",
    ]


def test_the_gate_detects_a_pathlib_path_walk(tmp_path: Path) -> None:
    """``Path.walk()`` walks a directory; ``ast.walk`` shares only the name.

    Reading the receiver of a path method, added for #2174, came with a method
    set that left ``walk`` out to keep ``ast.walk`` — a syntax-tree walk this
    gate and most of the others call — from matching. That also dropped
    ``Path.walk`` (3.12+), so a repo-wide gate written as
    ``for _dir, _subdirs, files in ROOT.walk():`` stayed unmarked and this gate
    stayed green. The callee tells the two apart; the method name cannot.
    """
    module = tmp_path / "test_path_walk.py"
    module.write_text(
        "import ast\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def test_path_walk_scan():\n"
        "    assert [n for _d, _s, files in ROOT.walk() for n in files]\n"
        "def test_path_walk_of_a_tmp_tree(tmp_path):\n"
        "    assert not list(tmp_path.walk())\n"
        "def test_ast_walk_of_a_rooted_tree():\n"
        '    tree = ast.parse((ROOT / "pyproject.toml").read_text())\n'
        "    assert list(ast.walk(tree))\n",
        encoding="utf-8",
    )
    assert repo_wide_scan_tests(module) == ["test_path_walk_scan"]


def test_the_source_filter_cannot_hide_a_shape_the_detector_reads(
    tmp_path: Path,
) -> None:
    """Every shape the detector reads keeps its module worth reading (#2340).

    Two thirds of the suite's modules are never parsed, because a call the
    shapes above match spells one of their names in the source. That is only
    safe while the filter covers every shape: a shape added to the tables with
    a new name would be filtered out before the detector ever saw it, and the
    gate would go quietly green on the class it was widened for. So each one
    is written out here and must survive both halves.
    """
    shapes = {
        "rglob": 'ROOT.rglob("*.py")',
        "glob_method": 'ROOT.glob("*.py")',
        "iterdir": "ROOT.iterdir()",
        "path_walk": "[files for _d, _s, files in ROOT.walk()]",
        "os_walk": "[files for _d, _s, files in os.walk(ROOT)]",
        "os_scandir": "list(os.scandir(ROOT))",
        "os_listdir": "os.listdir(ROOT)",
        "glob_glob": 'glob.glob(str(ROOT / "*.py"))',
        "glob_iglob": 'list(glob.iglob(str(ROOT / "*.py")))',
    }
    # Every spelling in every shape table is one the filter keeps.
    for spelling in _ITER_METHODS | _ITER_FUNCTIONS | _TREE_WALKS:
        assert _ITER_NAME_TOKENS.search(spelling), spelling
    for shape, call in shapes.items():
        module = tmp_path / f"test_{shape}.py"
        module.write_text(
            "import glob\n"
            "import os\n"
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parents[1]\n"
            "def test_scan():\n"
            f"    assert {call} is not None\n",
            encoding="utf-8",
        )
        source = module.read_text(encoding="utf-8")
        assert could_iterate_a_directory(source), f"{shape} is filtered out"
        assert repo_wide_scan_tests(module) == ["test_scan"], shape

    # The other side: a module naming none of them holds no scan test, which
    # is the only reason it is safe not to read it.
    plain = tmp_path / "test_plain.py"
    plain.write_text(
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def test_reads_one_file():\n"
        '    assert (ROOT / "pyproject.toml").read_text()\n',
        encoding="utf-8",
    )
    assert not could_iterate_a_directory(plain.read_text(encoding="utf-8"))
    assert repo_wide_scan_tests(plain) == []


def test_this_repo_names_the_policy_mark_and_registers_it() -> None:
    """The brief's mark and the runner's marker registry are the same name."""
    with (REPO_ROOT / "kew.toml").open("rb") as handle:
        declared = tomllib.load(handle)["commands"]["test_policy_mark"]
    assert declared == POLICY_MARK

    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as handle:
        markers = tomllib.load(handle)["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.split(":")[0] == POLICY_MARK for marker in markers), (
        "an unregistered mark is a --strict-markers error, and silently selects"
        " nothing without it"
    )


def test_the_id_sweep_gate_is_selected_by_the_policy_mark() -> None:
    """Proof for #2174 — the gate PR #2154 slipped past is in the mark's set.

    PR #2154 added an ``int(item_id)`` behind a ``provider == "github"`` guard
    in ``cli/epic.py``; its targeted round was green because no changed file
    selected ``tests/test_id_sweep_gate.py``. That gate now carries the mark,
    so the same remediation runs it and fails on its own change.
    """
    modules = [
        "tests/test_id_sweep_gate.py",
        "tests/test_silence_on_failure.py",
        "tests/test_policy_mark_gate.py",
    ]
    selected = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-m",
            POLICY_MARK,
            *modules,
        ],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert selected.returncode == 0, selected.stdout + selected.stderr
    collected = selected.stdout
    for node in (
        "tests/test_id_sweep_gate.py::test_no_unguarded_int_coercion_on_item_ids",
        "tests/test_silence_on_failure.py"
        "::test_silence_on_failure_no_new_silent_clean_defaults",
        # Every gate in the silence module, not just its tree scan: the retired
        # path list ran the whole module, so a mark that carries only the scan
        # would drop the ledger and citation checks from the mandatory set and
        # fail them on the merge verify instead.
        "tests/test_silence_on_failure.py"
        "::test_silence_baseline_ledger_is_sorted_and_free_of_duplicates",
        "tests/test_silence_on_failure.py"
        "::test_fail_loud_selector_is_the_house_example",
        "tests/test_policy_mark_gate.py"
        "::test_every_repo_wide_scan_test_carries_the_policy_mark",
    ):
        assert node in collected, (
            f"`pytest -m {POLICY_MARK}` does not select {node} — the governed"
            " remediation brief would skip it again"
        )
    unmarked = (
        "tests/test_silence_on_failure.py"
        "::test_silence_gate_fails_when_the_source_tree_is_missing"
    )
    assert unmarked not in collected, (
        "the mark must select the gates, not every test in their modules"
    )
