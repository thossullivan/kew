"""No long-lived kew tree sits under the OS temp directory.

macOS cleans its temp directory daily, under a running suite (#3171). Two
policy checks keep kew's trees under :func:`kew.work_dirs.work_root`:

* a run with ``TMPDIR`` pointed at a sentinel drives ``kew verify --host
  local``, a sandbox clone and a reviewer checkout, and the sentinel stays
  empty while every tree they made sits under the work root;
* a syntax-tree lint refuses a new ``mkdtemp``, ``TemporaryDirectory`` or
  ``gettempdir`` outside ``work_dirs.py``. A call that names its own ``dir=``
  does not default to the temp directory, so it passes; the listed modules
  and functions are call-local uses whose tree never outlives the call.
"""

from __future__ import annotations

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest

# The shared AST reader (tests/support/source_ast.py): the lint parses every
# src module, and the read lives in one audited helper, not in the test.
from support.source_ast import _parse as parse_file
from typer.testing import CliRunner

from kew import work_dirs
from kew.cli import app
from kew.config import DispatchConfig
from kew.dispatch_core import _materialize_checkout
from kew.review_dispatch import materialize_reviewer_worktree

pytestmark = pytest.mark.policy

_SRC = Path(__file__).resolve().parent.parent / "src" / "kew"
_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

#: The names that make a directory in, or name, the OS temp directory.
_REFUSED = frozenset({"mkdtemp", "TemporaryDirectory", "gettempdir"})

#: Whole modules whose uses are call-local.
_ALLOWED_MODULES = frozenset({"work_dirs.py", "resolve.py", "worktree.py"})

#: Single call-local uses, by module and enclosing function.
_ALLOWED_FUNCTIONS = frozenset(
    {
        ("acceptance_author.py", "*"),
        ("drive_merge.py", "_CombinedVerify.__call__"),
        ("project_merge_queue.py", "GitCandidate.build"),
        # A container ID file lives only while its container starts.
        ("sandbox.py", "_new_container_cidfile"),
    }
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [*_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _names_a_directory(node: ast.Call) -> bool:
    """A ``dir`` that is not ``None``, by keyword or as the third positional."""
    given = [kw.value for kw in node.keywords if kw.arg == "dir"]
    if len(node.args) >= 3:
        given.append(node.args[2])
    return any(not _is_none(value) for value in given)


def _tempfile_aliases(tree: ast.Module) -> dict[str, str]:
    """``from tempfile import mkdtemp as m`` binds ``m`` to ``mkdtemp``."""
    return {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "tempfile"
        for alias in node.names
        if alias.name in _REFUSED
    }


def _refused_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        name: str | None = func.attr
    elif isinstance(func, ast.Name):
        name = aliases.get(func.id, func.id)
    else:
        name = None
    if name not in _REFUSED:
        return None
    if name != "gettempdir" and _names_a_directory(node):
        return None
    return name


class _Finder(ast.NodeVisitor):
    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.scope: list[str] = []
        self.found: list[tuple[str, int, str]] = []

    def _scoped(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _scoped
    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        name = _refused_name(node, self.aliases)
        if name is not None:
            self.found.append((".".join(self.scope), node.lineno, name))
        self.generic_visit(node)


def _violations(source: str, module: str) -> list[str]:
    return _tree_violations(ast.parse(source), module)


def _tree_violations(tree: ast.Module, module: str) -> list[str]:
    finder = _Finder(_tempfile_aliases(tree))
    finder.visit(tree)
    return [
        f"{module}:{line} {name}() in {scope or '<module>'}"
        for scope, line, name in finder.found
        if (module, scope) not in _ALLOWED_FUNCTIONS
        and (module, "*") not in _ALLOWED_FUNCTIONS
    ]


def test_no_module_but_work_dirs_makes_a_tree_in_the_os_temp_directory() -> None:
    modules = sorted(_SRC.rglob("*.py"))
    assert len(modules) > 50, f"found only {len(modules)} modules under {_SRC}"
    found: list[str] = []
    for path in modules:
        module = path.relative_to(_SRC).as_posix()
        if module in _ALLOWED_MODULES:
            continue
        found += _tree_violations(parse_file(str(path)), module)
    assert found == [], "route these through kew.work_dirs:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "source",
    [
        "import tempfile\nd = tempfile.mkdtemp(prefix='x-')\n",
        "from tempfile import TemporaryDirectory\nwith TemporaryDirectory(): ...\n",
        "import tempfile\ndef f():\n    return tempfile.gettempdir()\n",
        "import tempfile\nd = tempfile.gettempdir(dir='x')\n",
        "from tempfile import mkdtemp as m\nd = m(prefix='x-')\n",
        "from tempfile import TemporaryDirectory as T\nwith T(): ...\n",
        "from tempfile import gettempdir as g\nd = g()\n",
        "import tempfile\nd = tempfile.mkdtemp(prefix='x-', dir=None)\n",
        "import tempfile\nd = tempfile.mkdtemp('', 'x-', None)\n",
    ],
)
def test_the_lint_refuses_a_default_temp_directory(source: str) -> None:
    """The lint can say "no": each of these is one violation."""
    assert len(_violations(source, "fresh.py")) == 1


def test_the_lint_passes_an_explicit_directory_and_a_listed_use() -> None:
    explicit = "import tempfile\nd = tempfile.mkdtemp(prefix='x-', dir=root)\n"
    aliased = "from tempfile import mkdtemp as m\nd = m('', 'x-', root)\n"
    assert _violations(aliased, "fresh.py") == []
    listed = (
        "import tempfile\nclass GitCandidate:\n    def build(self):\n"
        "        with tempfile.TemporaryDirectory() as d: ...\n"
    )
    assert _violations(explicit, "fresh.py") == []
    assert _violations(listed, "project_merge_queue.py") == []


def _candidate_repo(repo: Path, facts: Path) -> tuple[str, str]:
    """A project whose HEAD is one commit ahead of ``main``; returns both shas.

    The gate's first step, the pinned-tool preflight, is a stand-in that
    records where the job ran and what its ``TMPDIR`` was, then stops.
    """
    _git(repo, "init", "--quiet", "-b", "main", ".")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "scan-agent-image.sh").write_text(
        f'#!/bin/sh\npwd > {facts}/pwd\necho "$TMPDIR" > {facts}/tmpdir\nexit 1\n',
        encoding="utf-8",
    )
    (repo / "kew").mkdir()
    (repo / "kew" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--quiet", "-b", "candidate")
    (repo / "kew" / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "candidate")
    return base, _git(repo, "rev-parse", "HEAD")


def test_verify_a_sandbox_clone_and_a_review_leave_the_os_temp_directory_alone(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path_factory.mktemp("sentinel-tmpdir")
    facts = tmp_path_factory.mktemp("job-facts")
    monkeypatch.setenv("TMPDIR", str(sentinel))
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert Path(tempfile.gettempdir()) == sentinel
    root = work_dirs.configured_root()

    repo = tmp_path
    _candidate_repo(repo, facts)
    (repo / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = {str(facts / "logs")!r}\n'
        '[verify]\nhost = "local"\n',
        encoding="utf-8",
    )
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KEW_PROJECT", str(repo))
    monkeypatch.chdir(repo)
    CliRunner().invoke(app, ["verify", "--host", "local"])
    job_dir = Path((facts / "pwd").read_text().strip())
    job_tmp = Path((facts / "tmpdir").read_text().strip())

    upstream = tmp_path_factory.mktemp("upstream")
    # The review's shas are the upstream's own: its commits are not the first
    # repo's whenever the clock crossed a second between the two.
    base, head = _candidate_repo(upstream, facts)
    _git(upstream, "checkout", "--quiet", "main")
    local = tmp_path_factory.mktemp("clone") / "local"
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    monkeypatch.chdir(local)
    sandbox = _materialize_checkout("5", "agent/issue-5", DispatchConfig())
    review = materialize_reviewer_worktree(upstream, head, base_ref=base)

    made = {"verify job": job_dir, "job TMPDIR": job_tmp}
    made |= {"sandbox clone": sandbox, "reviewer checkout": review}
    assert {what: path.is_relative_to(root) for what, path in made.items()} == {
        what: True for what in made
    }
    assert not any(path.is_relative_to(sentinel) for path in made.values())
    assert sorted(p.name for p in sentinel.iterdir()) == []
