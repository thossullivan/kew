"""Which test files a change selects, and why (#3189).

A pure function of (base tree, candidate tree, selector version, ``GATE_RE``):
every input is read from git objects, never from the working tree, so the same
two commits give a byte-identical explanation and digest on any checkout. The
merge gate paths come from the candidate's committed ``kew.toml``, on top of
the defaults. Nothing here changes what a verify runs — it only explains a
selection.

The graph is the stdlib ``ast`` reading of ``src/kew`` and ``tests/`` in BOTH
trees, unioned, so an import the candidate removed still selects the files
that used to depend on it. Function-local imports count, and importing
``kew.a.b`` counts as importing ``kew.a`` and ``kew`` too, since Python runs
every package ``__init__`` on the way down.

A test-side file also selects on the repo files it reads outside ``src/kew``
(#3211): the string constants and ``Path`` / ``os.path.join`` chains that name
docs, frontend sources, fixtures and the like, and so does a kew module that
reads one by a literal path. A path the selector cannot name runs its reader
on every change, never nothing: one built from a variable after a repo-root
anchor, an upward search or a step up, one under a root a support module
exports, a ``..`` the selector cannot follow beside a directory read under a
variable, which it may climb out of, and a script the test runs, whose reads
no parse sees.

Whatever the selector cannot trace answers the full suite: a changed path that
is neither a module, a test-side file nor a path some test could read, a file
that does not parse, a ``kew.toml`` it cannot read. A selection that misses a
test the change can break must never be possible silently.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import posixpath
import re
import subprocess
import tomllib
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TypeGuard

from kew import subprocess_utils
from kew.config import EpicConfig

#: Bumped whenever a rule or the graph reading changes what is selected.
SELECTOR_VERSION = 3

#: At or above this share of test files, selecting is not worth it.
FULL_SUITE_SHARE = 0.70

CONFTEST_RULE = "tests/conftest.py imports it"
CONFTEST_READ_RULE = "tests/conftest.py reads it"
SHARE_RULE = "the selection reached 70% of test files"
#: Why a test runs when a ``..`` may climb out of a directory it reads.
CLIMB_REASON = "always: a path climbs out of a directory it reads with .."
#: The rules that read the import graph; every other rule fires on paths alone.
GRAPH_RULES = frozenset({CONFTEST_RULE, CONFTEST_READ_RULE, SHARE_RULE})

_DYNAMIC_ROOTS = frozenset({"subprocess", "runpy", "importlib"})
_KEW_STRING = re.compile(r"\Akew(\.[A-Za-z_][\w.]*)?(\s|\Z)")
#: A string holding Python that imports kew, as ``python -c "import kew.x"``.
_KEW_CODE = re.compile(r"(?:\A|[\s;])(?:import|from)\s+kew\b")
_KEW_MODULE = re.compile(r"\Akew(\.[A-Za-z_]\w*)*\Z")
_PATCHERS = frozenset({"patch", "setattr", "object"})
#: Calls that import the module their first argument names.
_IMPORT_CALLS = frozenset({"import_module", "__import__", "importorskip"})
#: Calls that load a Python file by path, and which argument is the path.
_LOADERS = {"spec_from_file_location": 1, "SourceFileLoader": 1, "run_path": 0}
#: A test-side file holding none of these has no dynamic call, mark or patch.
_EXPRESSION_MARKERS = (
    b"subprocess",
    b"runpy",
    b"importlib",
    b"policy",
    b'"kew',
    b"'kew",
    b"__import__",
    b"importorskip",
    b"import_module",
    b"SourceFileLoader",
    b"os.system",
    b"os.popen",
    b"create_subprocess",
    b"pexpect",
)
#: Top-level directories a test may read as files, and what a read of one
#: names. Files at the root and under ``www/`` stay untraced: scripts a test
#: runs read them, and the selector cannot see a script's reads.
_READ_AREAS = frozenset({"docs", "frontend", "docker", "scripts"})
#: A string that could be a path: no spaces, no scheme, no markup. ``+``,
#: ``(`` and ``)`` spell SvelteKit routes such as ``(factory)/+page.svelte``.
_PATH_LIKE = re.compile(r"\A[\w.@*?\[\]()+-]+(?:/[\w.@*?\[\]()+-]*)*\Z")
#: Programs that run a script file, and the flags that make them run inline code.
_SCRIPT_RUNNERS = frozenset(
    {"bash", "sh", "zsh", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno", "tsx"}
    | {"make"}
)
_INLINE_FLAGS = frozenset({"-c", "-e", "-lc", "-ec", "-xc", "--eval"})
#: Shells, whose ``-c`` may follow other options: ``bash --norc -eo pipefail -c``.
_SHELLS = frozenset({"bash", "sh", "zsh"})
#: Shell long options that take the next word as their argument.
_SHELL_ARG_OPTIONS = frozenset({"--rcfile", "--init-file"})
#: A path segment that climbs: ``../x``, ``a/../b``, ``..``.
_CLIMB = re.compile(r"(?:\A|/)\.\.(?:/|\Z)")
#: Runners whose next word names a package script rather than a file.
_PACKAGE_RUNNERS = frozenset({"npm", "npx", "pnpm", "yarn", "bun", "make"})
#: A command word naming a script: whatever it reads, the selector cannot see.
_SCRIPT_FILE = re.compile(r"\.(?:sh|bash|mjs|cjs|js|ts|py)\Z")
#: Calls outside ``subprocess`` that run a command.
_COMMAND_CALLS = frozenset(
    {"system", "popen", "create_subprocess_exec", "create_subprocess_shell", "spawn"}
)
_GLOB = re.compile(r"[*?\[]")
#: Calls that build a path from their arguments, or from their receiver.
_PATH_FUNCTIONS = ("path.join", "path.abspath", "path.realpath", "path.dirname")
_PATH_CONSTRUCTORS = frozenset({"Path", "PurePath", "PurePosixPath", "str"})
_PATH_METHODS = frozenset({"joinpath", "resolve", "absolute"})
#: Calls that read a whole directory.
_WALKS = frozenset({"rglob", "glob", "iterdir", "walk", "listdir", "scandir"})
#: A module under ``src/kew`` holding none of these makes no import call.
_SRC_MARKERS = (b"__import__", b"importorskip", b"import_module")

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

Graph = dict[str, set[str]]


class SelectionError(RuntimeError):
    """The selection could not be read from git."""


# ── reading git ─────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess_utils.run(["git", *args], cwd=repo, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit {exc.returncode}"
        raise SelectionError(f"git {' '.join(args)}: {detail}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SelectionError(f"git {' '.join(args)} could not run: {exc}") from exc
    return done.stdout or ""


def cat_blobs(repo: Path, oids: Sequence[str]) -> dict[str, bytes]:
    """Every blob in *oids*, read in one ``git cat-file --batch``."""
    if not oids:
        return {}
    proc = subprocess_utils.popen_bytes(["git", "cat-file", "--batch"], cwd=repo)
    out, err = proc.communicate("".join(f"{oid}\n" for oid in oids).encode())
    if proc.returncode != 0:
        raise SelectionError(f"git cat-file --batch: {err.decode().strip()}")
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = out.index(b"\n", pos)
        size = int(out[pos:end].split()[2])
        blobs[oid] = out[end + 1 : end + 1 + size]
        pos = end + 2 + size
    return blobs


def _tree_id(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{rev}^{{tree}}").strip()


def _changed_paths(repo: Path, base: str, candidate: str) -> tuple[str, ...]:
    out = _git(repo, "diff", "--name-only", "-z", "--no-renames", base, candidate)
    return tuple(sorted(p for p in out.split("\0") if p))


def _list_tree(repo: Path, rev: str) -> dict[str, str]:
    """``path -> blob oid`` for every file in *rev*."""
    out = _git(repo, "ls-tree", "-r", "-z", "--full-tree", rev)
    files: dict[str, str] = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        _mode, kind, oid = meta.split()
        if kind == "blob":
            files[path] = oid
    return files


def _project_prefix(files: Iterable[str]) -> str:
    """Where ``src/kew`` sits in the repository: ``""`` or ``"kew/"``."""
    inits = [p for p in files if p.endswith("src/kew/__init__.py")]
    if not inits:
        raise SelectionError("the tree has no src/kew package")
    return min(inits, key=len).removesuffix("src/kew/__init__.py")


# ── one tree's facts ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileFacts:
    """What one Python file says about the import graph, read once per blob."""

    imports: tuple[str, ...] = ()
    #: The imports made at module level, outside any function.
    eager: tuple[str, ...] = ()
    dynamic: bool = False
    policy: bool = False
    tests: int = 0
    #: Path suffixes of the Python files this file loads by path.
    loads: tuple[str, ...] = ()
    #: False when the file is not valid Python: its facts are unknown.
    parsed: bool = True
    #: Repo paths this file reads, relative to the repo or the project root.
    reads: tuple[str, ...] = ()
    #: Repo paths this file reads, relative to its own directory.
    local_reads: tuple[str, ...] = ()
    #: It builds a path from a variable right after a repo-root anchor.
    dynamic_read: bool = False
    #: It runs a script file, whose reads the selector cannot see.
    runs_script: bool = False
    #: A string in it climbs with ``..`` and no path it reads resolves it.
    climbs: bool = False
    #: It reads a directory under a variable, which a ``..`` could leave.
    open_dir: bool = False


@dataclass
class Snapshot:
    """One tree, as the selector reads it."""

    prefix: str
    files: dict[str, str]
    #: ``node -> facts``: kew modules by dotted name, other files by path.
    facts: dict[str, FileFacts] = field(default_factory=dict)
    module_paths: dict[str, str] = field(default_factory=dict)
    #: ``test-side file -> the files it loads by path``.
    loaded: dict[str, set[str]] = field(default_factory=dict)
    #: Test-side files that load a file by a path the selector cannot find.
    unresolved: set[str] = field(default_factory=set)
    #: Test modules another file imports: read as files others call into.
    imported: set[str] = field(default_factory=set)


class FactCache:
    """Parsed facts by blob oid, so a replay parses each blob once."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._facts: dict[tuple[str, str, bool, bool], FileFacts] = {}
        self._project_dir: str | None = None

    def facts(
        self, items: Sequence[tuple[str, str, str, bool, bool]]
    ) -> list[FileFacts]:
        """Facts for ``(oid, path, package, test_side, exports)``, parsing only
        new blobs."""
        keys = [(oid, pkg, side, exports) for oid, _, pkg, side, exports in items]
        missing = sorted({key[0] for key in keys if key not in self._facts})
        blobs = cat_blobs(self.repo, missing)
        out = []
        for (oid, path, package, test_side, exports), key in zip(
            items, keys, strict=True
        ):
            if key not in self._facts:
                source = blobs.get(oid, b"")
                self._facts[key] = _read_facts(
                    source, path, package, test_side, exports
                )
            out.append(self._facts[key])
        return out

    def blob(self, oid: str) -> bytes:
        return cat_blobs(self.repo, [oid])[oid]

    def project_dir(self) -> str:
        """The project root's path inside the repository, ``""`` or ``"x/"``."""
        if self._project_dir is None:
            self._project_dir = _git(self.repo, "rev-parse", "--show-prefix").strip()
        return self._project_dir


def _is_module(node: str) -> bool:
    """A kew module (dotted name), not a file under ``tests/`` (a path)."""
    return "/" not in node


def _module_name(rel: str) -> str:
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(node: str, path: str, prefix: str) -> str:
    """The package a relative import in *path* is resolved against."""
    if not _is_module(node):
        return ".".join(PurePosixPath(path[len(prefix) :]).parent.parts)
    if path.endswith("/__init__.py"):
        return node
    return node.rpartition(".")[0]


def snapshot(repo: Path, rev: str, cache: FactCache) -> Snapshot:
    """Read *rev*'s ``src/kew`` and ``tests/`` Python files into facts, and the
    Python files the tests load by path."""
    files = _list_tree(repo, rev)
    prefix = _project_prefix(files)
    snap = Snapshot(prefix=prefix, files=files)
    items = []
    for path in sorted(files):
        node = _node_for(path, prefix)
        if node is not None:
            items.append((node, path, _package_of(node, path, prefix)))
    _add_facts(snap, items, cache)
    pending = _load_targets(snap)
    while pending:
        _add_facts(snap, [(p, p, "") for p in sorted(pending)], cache)
        pending = _load_targets(snap)
    # A test module's helpers read a directory with whatever another file
    # passes them, not only with what its own calls pass.
    snap.imported = _imported_test_modules(snap)
    _add_facts(snap, [i for i in items if i[0] in snap.imported], cache)
    return snap


def _add_facts(
    snap: Snapshot, items: Sequence[tuple[str, str, str]], cache: FactCache
) -> None:
    rows = [
        (snap.files[path], path, pkg, not _is_module(n), _reads_as_export(path, snap))
        for n, path, pkg in items
    ]
    for (node, path, _pkg), facts in zip(items, cache.facts(rows), strict=True):
        snap.facts[node] = facts
        if _is_module(node):
            snap.module_paths[node] = path


def _reads_as_export(path: str, snap: Snapshot) -> bool:
    return _exports(path) or path in snap.imported


def _imported_test_modules(snap: Snapshot) -> set[str]:
    """The test modules some other test-side file imports."""
    found: set[str] = set()
    for node, facts in snap.facts.items():
        if _is_module(node):
            continue
        for name in facts.imports:
            found.update(n for n in _resolve_local(name, node, snap) if n != node)
    return {node for node in found if not _exports(node)}


def _load_targets(snap: Snapshot) -> set[str]:
    """Resolve each load by path; returns the loaded files not yet read."""
    new: set[str] = set()
    by_name: dict[str, list[str]] = {}
    for path in snap.files:
        if path.endswith(".py"):
            by_name.setdefault(PurePosixPath(path).name, []).append(path)
    for node, facts in snap.facts.items():
        for suffix in facts.loads:
            new.update(_resolve_load(snap, node, suffix, by_name))
        if _is_script(node, snap.prefix):
            new.update(_script_siblings(node, facts, snap))
    return {p for p in new if p not in snap.facts}


def _is_script(node: str, prefix: str) -> bool:
    """A file read only because a test loads it by path."""
    return not _is_module(node) and _node_for(node, prefix) is None


def _resolve_load(
    snap: Snapshot, node: str, suffix: str, by_name: Mapping[str, list[str]]
) -> set[str]:
    """Record what *node* loads as *suffix*; returns the scripts among it."""
    candidates = by_name.get(PurePosixPath(suffix).name, ())
    found = {p for p in candidates if p == suffix or p.endswith(f"/{suffix}")}
    if not found:
        snap.unresolved.add(node)
    snap.loaded.setdefault(node, set()).update(found)
    return {p for p in found if _node_for(p, snap.prefix) is None}


def _script_siblings(node: str, facts: FileFacts, snap: Snapshot) -> set[str]:
    """The files beside a loaded script that its plain imports name."""
    parent = PurePosixPath(node).parent
    found = set()
    for name in facts.imports:
        rel = name.replace(".", "/")
        for path in (f"{parent}/{rel}.py", f"{parent}/{rel}/__init__.py"):
            if path in snap.files:
                found.add(path)
    return found


def _node_for(path: str, prefix: str) -> str | None:
    if not path.endswith(".py") or not path.startswith(prefix):
        return None
    rel = path[len(prefix) :]
    if rel.startswith("src/kew/"):
        return _module_name(rel.removeprefix("src/"))
    if rel.startswith("tests/"):
        return path
    return None


# ── reading one file ────────────────────────────────────────────────────────


def _read_facts(
    source: bytes, path: str, package: str, test_side: bool, exports: bool
) -> FileFacts:
    """One file's facts; *test_side* is True for anything outside ``src/kew``,
    *exports* for a file other files call into."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return FileFacts(parsed=False)
    imports = _names(_statements(tree.body, functions=True), package)
    eager = _names(_statements(tree.body, functions=False), package)
    calls = _Calls()
    if any(m in source for m in (_EXPRESSION_MARKERS if test_side else _SRC_MARKERS)):
        calls = _read_calls(tree, test_side)
    paths = _Paths()
    if test_side or b"__file__" in source:
        paths = _read_paths(tree, src=not test_side, exports=exports)
    return FileFacts(
        imports=tuple(sorted(imports | calls.imports)),
        eager=tuple(sorted(eager)),
        dynamic=calls.dynamic,
        policy=calls.policy,
        tests=_count_tests(tree) if test_side else 0,
        loads=tuple(sorted(calls.loads)),
        reads=tuple(sorted(paths.free)),
        local_reads=tuple(sorted(paths.local)),
        dynamic_read=paths.dynamic,
        runs_script=calls.runs_script,
        climbs=paths.climbs,
        open_dir=paths.open_dir,
    )


def _exports(path: str) -> bool:
    """A file other files call into for paths: anything but a test module."""
    name = PurePosixPath(path).name
    return not (name.startswith("test_") or name.endswith("_test.py"))


def _names(statements: Iterable[ast.AST], package: str) -> set[str]:
    return {name for st in statements for name in _imported_names(st, package)}


def _statements(body: Iterable[ast.AST], *, functions: bool) -> Iterator[ast.AST]:
    """Every statement, nested ones too: where an import sits. *functions*
    False stops at function bodies, leaving what importing the module runs."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not functions and isinstance(node, _FUNCTIONS):
            continue
        for name in ("body", "orelse", "finalbody", "handlers", "cases"):
            nested = getattr(node, name, None)
            if isinstance(nested, list):
                stack.extend(nested)


def _imported_names(node: ast.AST, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    base = _absolute(node.module, node.level, package)
    if base is None:
        return []
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _absolute(module: str | None, level: int, package: str) -> str | None:
    if level == 0:
        return module
    parts = package.split(".") if package else []
    if level - 1 > len(parts) or not parts:
        return None
    anchor = ".".join(parts[: len(parts) - (level - 1)])
    return f"{anchor}.{module}" if module else anchor


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@dataclass
class _Calls:
    """What a file's calls add: imports by name, loads by path, dynamic use."""

    imports: set[str] = field(default_factory=set)
    loads: set[str] = field(default_factory=set)
    dynamic: bool = False
    policy: bool = False
    runs_script: bool = False
    #: A call runs a command the selector cannot spell out.
    unknown_command: bool = False


@dataclass(frozen=True)
class _FileContext:
    """What a call is read against: the file's bindings and its strings."""

    #: Local names bound to ``subprocess``/``runpy``/``importlib`` or theirs.
    roots: Mapping[str, str]
    #: Local names from-imported under another name: ``im -> import_module``.
    real: Mapping[str, str]
    #: Module-level ``NAME = value`` assignments.
    assigns: Mapping[str, ast.expr]
    #: Every string in the file that names a kew module.
    kew_modules: frozenset[str]
    #: Some string in the file is Python code that imports kew.
    kew_code: bool


def _file_context(tree: ast.Module, strings: Sequence[str]) -> _FileContext:
    roots: dict[str, str] = {}
    real: dict[str, str] = {}
    for node in _statements(tree.body, functions=True):
        for root, local in _bindings(node):
            if root in _DYNAMIC_ROOTS:
                roots[local] = root
        if isinstance(node, ast.ImportFrom):
            real.update((a.asname, a.name) for a in node.names if a.asname)
    return _FileContext(
        roots=roots,
        real=real,
        assigns=_module_assigns(tree),
        kew_modules=frozenset(s for s in strings if _KEW_MODULE.match(s)),
        kew_code=any(_KEW_CODE.search(s) for s in strings),
    )


def _module_assigns(tree: ast.Module) -> dict[str, ast.expr]:
    assigns: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assigns[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assigns[node.target.id] = node.value
    return assigns


def _bindings(node: ast.AST) -> list[tuple[str, str]]:
    """``(imported root module, local name)`` for each name an import binds."""
    if isinstance(node, ast.Import):
        return [
            (a.name.split(".")[0], a.asname or a.name.split(".")[0]) for a in node.names
        ]
    if isinstance(node, ast.ImportFrom) and node.module:
        root = node.module.split(".")[0]
        return [(root, a.asname or a.name) for a in node.names]
    return []


def _read_calls(tree: ast.Module, test_side: bool) -> _Calls:
    found = _Calls()
    calls: list[ast.Call] = []
    strings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
        elif test_side and _is_policy_mark(node):
            found.policy = True
    ctx = _file_context(tree, strings)
    for call in calls:
        _read_call(call, ctx, found, test_side)
    if found.unknown_command and not found.runs_script:
        found.runs_script = _names_any_script(tree)
    return found


def _read_call(
    call: ast.Call, ctx: _FileContext, found: _Calls, test_side: bool
) -> None:
    name = _dotted(call.func)
    tail = ctx.real.get(name, name.rpartition(".")[2])
    if tail in _IMPORT_CALLS:
        _read_import_call(call, ctx, found, test_side)
    elif tail in _LOADERS and test_side:
        _read_loader(call, _LOADERS[tail], ctx, found)
    if test_side:
        found.dynamic = found.dynamic or _is_dynamic_call(call, ctx)
        found.imports.update(_patched_names(call))
        _read_command(call, ctx, found)


def _read_import_call(
    call: ast.Call, ctx: _FileContext, found: _Calls, test_side: bool
) -> None:
    """``import_module(x)``: a literal is an import; anything else may import
    any kew module the file names, or any at all."""
    arg = call.args[0] if call.args else None
    literal = _string(arg)
    if literal is not None:
        found.imports.add(literal)
        return
    prefix = _constant_prefix(arg)
    if prefix is not None and prefix.startswith("kew"):
        found.imports.add(_wildcard(prefix))
    else:
        found.imports.update(ctx.kew_modules or {"kew.*"})
    # A test whose import the selector cannot name runs on every change.
    found.dynamic = found.dynamic or test_side


def _constant_prefix(node: ast.AST | None) -> str | None:
    """The literal start of ``f"kew.{x}"`` or ``"kew." + x``."""
    if isinstance(node, ast.JoinedStr) and node.values:
        return _string(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return (_string(node.left) or "").split("%")[0] or None
    return None


def _wildcard(prefix: str) -> str:
    """``"kew.runners."`` -> ``"kew.runners.*"``: every module under it."""
    package = prefix.rpartition(".")[0] if "." in prefix else ""
    return f"{package or 'kew'}.*"


def _read_loader(call: ast.Call, index: int, ctx: _FileContext, found: _Calls) -> None:
    arg = call.args[index] if len(call.args) > index else None
    target = _path_suffix(arg, ctx.assigns)
    if target is None:
        found.dynamic = True
    else:
        found.loads.add(target)


def _path_suffix(node: ast.AST | None, assigns: Mapping[str, ast.expr]) -> str | None:
    """``ROOT / "scripts" / "x.py"`` -> ``"scripts/x.py"``, through module-level
    names; None when the path is not spelled out in literals."""
    parts: list[str] = []
    for _ in range(8):
        node = _unwrap(node, assigns)
        if not _is_division(node) or _string(node.right) is None:
            break
        parts.append(str(_string(node.right)))
        node = node.left
    literal = _string(node)
    if literal is not None:
        parts.append(literal)
    rel = "/".join(reversed(parts)).strip("/")
    return rel if rel.endswith(".py") else None


def _unwrap(node: ast.AST | None, assigns: Mapping[str, ast.expr]) -> ast.AST | None:
    """``str(X)`` -> ``X``, and a module-level name -> its value."""
    if isinstance(node, ast.Call) and _dotted(node.func) == "str" and node.args:
        node = node.args[0]
    if isinstance(node, ast.Name) and node.id in assigns:
        node = assigns[node.id]
    return node


def _is_division(node: ast.AST | None) -> TypeGuard[ast.BinOp]:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)


def _is_dynamic_call(call: ast.Call, ctx: _FileContext) -> bool:
    """A subprocess, runpy or importlib call that runs or loads kew."""
    name = _dotted(call.func)
    if ctx.roots.get(name.split(".")[0]) is None:
        return False
    if name.endswith("reload"):
        return True
    strings = [s for n in ast.walk(call) if (s := _string(n)) is not None]
    if any(_KEW_STRING.match(s) or _KEW_CODE.search(s) for s in strings):
        return True
    # ``[sys.executable, "-c", SCRIPT]``: the code is a name, and some string
    # in the file is Python that imports kew.
    return ctx.kew_code and any(
        not isinstance(n, ast.Constant) for n in _flat_args(call)
    )


def _read_command(call: ast.Call, ctx: _FileContext, found: _Calls) -> None:
    """``subprocess.run(["bash", "scripts/x.sh"])``: a call that runs a script."""
    name = _dotted(call.func)
    root = ctx.roots.get(name.split(".")[0])
    if root != "subprocess" and name.rpartition(".")[2] not in _COMMAND_CALLS:
        return
    args = [*call.args[:1], *(k.value for k in call.keywords if k.arg == "args")]
    if not args:
        return
    words = _command_words(args[0], ctx.assigns)
    if words is None:
        found.unknown_command = True
    else:
        found.runs_script = found.runs_script or _names_script(words)


def _command_words(
    node: ast.AST | None, assigns: Mapping[str, ast.expr]
) -> list[str | None] | None:
    """A command as its words, None for each the selector cannot read; None
    when the command itself is not spelled out."""
    node = _unwrap(node, assigns)
    if isinstance(node, ast.List | ast.Tuple):
        return [_last_literal(e, assigns, 0) for e in node.elts]
    text = _last_literal(node, assigns, 0)
    return None if text is None else list(text.split())


def _last_literal(
    node: ast.AST | None, assigns: Mapping[str, ast.expr], depth: int
) -> str | None:
    """The literal a command word ends in: ``ROOT / "scripts" / "x.sh"`` ends
    in ``x.sh``."""
    node = _unwrap(node, assigns)
    if depth > 6 or node is None:
        return None
    if _is_division(node):
        return _last_literal(node.right, assigns, depth + 1)
    if isinstance(node, ast.JoinedStr) and node.values:
        return _last_literal(node.values[-1], assigns, depth + 1)
    return _string(node)


def _names_script(words: Sequence[str | None]) -> bool:
    """A command that runs a script file: a script runner not given inline
    code, or a word naming a script."""
    first = words[0] if words else None
    if first is not None and PurePosixPath(first).name in _SCRIPT_RUNNERS:
        return not _runs_inline(PurePosixPath(first).name, words[1:])
    return any(w is not None and _SCRIPT_FILE.search(w) for w in words)


def _runs_inline(runner: str, rest: Sequence[str | None]) -> bool:
    """*runner* is given inline code, not a script: its first word is an
    inline flag, or, for a shell, its options read past end in one
    (``bash --noprofile --norc -eo pipefail -c X``)."""
    if runner not in _SHELLS:
        return bool(rest) and rest[0] in _INLINE_FLAGS
    skip = False
    for word in rest:
        if skip:
            skip = False
            continue
        kind = _shell_word(word)
        if kind in ("inline", "operand"):
            return kind == "inline"
        skip = kind == "takes_arg"
    return False


def _shell_word(word: str | None) -> str:
    """One word after a shell: an ``operand`` (a script, stdin, ``--`` or a
    word the selector cannot read), an ``inline`` code option (``-c``,
    ``-ec``), an option that ``takes_arg`` (``-o pipefail``, ``-eo pipefail``,
    ``--rcfile f``), or a plain ``option``."""
    if word is None or word in ("-", "--") or not word.startswith(("-", "+")):
        return "operand"
    if word.startswith("--"):
        return "takes_arg" if word in _SHELL_ARG_OPTIONS else "option"
    if word[0] == "-" and "c" in word[1:]:
        return "inline"
    return "takes_arg" if word[-1] in "oO" else "option"


def _names_any_script(tree: ast.Module) -> bool:
    """A script runner anywhere in the file, ``["bash", script]`` or
    ``"bash scripts/x.sh"``, for a file whose command calls take a command
    built elsewhere."""
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple) and node.elts:
            words = [_string(e) for e in node.elts]
        elif (text := _string(node)) is not None:
            words = [*text.split()[:2]]
            if len(words) < 2 or not _SCRIPT_FILE.search(str(words[1])):
                continue
        else:
            continue
        if _runs_a_file(words):
            return True
    return False


def _runs_a_file(words: Sequence[str | None]) -> bool:
    """``[runner, script]``: a runner whose next word is a script file, a
    package script or a name the selector cannot read."""
    first = words[0] if words else None
    if first is None or PurePosixPath(first).name not in _SCRIPT_RUNNERS:
        return False
    nxt = words[1] if len(words) > 1 else None
    if nxt is None or PurePosixPath(first).name in _PACKAGE_RUNNERS:
        return nxt not in _INLINE_FLAGS
    return bool(_SCRIPT_FILE.search(nxt))


def _flat_args(call: ast.Call) -> Iterator[ast.expr]:
    for arg in [*call.args, *(k.value for k in call.keywords)]:
        if isinstance(arg, ast.List | ast.Tuple):
            yield from arg.elts
        else:
            yield arg


def _patched_names(call: ast.Call) -> list[str]:
    """``patch("kew.x.y")``: a string target is an import the test makes."""
    if _dotted(call.func).rpartition(".")[2] not in _PATCHERS or not call.args:
        return []
    value = _string(call.args[0])
    return [value] if value is not None and value.startswith("kew.") else []


def _is_policy_mark(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and _dotted(node).endswith("mark.policy")


def _count_tests(tree: ast.Module) -> int:
    """The static count of tests pytest would collect from this module."""
    total = 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            total += sum(_cases(item) for item in node.body)
        else:
            total += _cases(node)
    return total


def _cases(node: ast.stmt) -> int:
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return 0
    if not node.name.startswith("test"):
        return 0
    cases = 1
    for decorator in node.decorator_list:
        cases *= _parametrize_cases(decorator)
    return cases


def _parametrize_cases(decorator: ast.expr) -> int:
    if not isinstance(decorator, ast.Call):
        return 1
    if not _dotted(decorator.func).endswith("parametrize"):
        return 1
    if len(decorator.args) < 2:
        return 1
    values = decorator.args[1]
    if isinstance(values, ast.List | ast.Tuple | ast.Set):
        return max(len(values.elts), 1)
    return 1


# ── the files a test reads ──────────────────────────────────────────────────

#: A segment that ends what a path spells out: a glob, read as its directory.
_STOP = "*"
#: The name a loop over a walked directory binds: one entry, any entry.
_EACH = "\0each"
_PLACEHOLDER = re.compile(r"%[sr]|\{[^{}]*\}")
#: Calls that keep a path's directory and swap its last segment.
_RENAMES = frozenset({"with_name", "with_suffix", "with_stem"})
#: How deep a path expression, through the names it reads, the selector follows.
_MAX_DEPTH = 40
#: How many names, parameter to argument, a path segment is followed through.
_MAX_PIN_HOPS = 8
#: How many argument choices one path is read under; past it, a variable.
_MAX_VARIANTS = 256
#: How far above ``__file__`` an upward search is followed: past the repo.
_MAX_UPS = 12
#: The attribute a loop over ``x.parents`` binds: one ancestor, any one.
_ANCESTOR = "\0ancestor"
#: Calls a loop's iterable is wrapped in, whose first argument is iterated.
_SEQUENCES = frozenset({"sorted", "list", "set", "tuple", "reversed"})


@dataclass
class _Paths:
    """The repo paths one file reads, as its literals spell them."""

    free: set[str] = field(default_factory=set)
    local: set[str] = field(default_factory=set)
    dynamic: bool = False
    #: A lone string climbs with ``..``: what it climbs out of is unknown.
    climbs: bool = False
    #: A read of a directory with a variable after it.
    open_dir: bool = False


@dataclass(frozen=True)
class _Chain:
    """A path expression: where it starts and the segments after it.

    *ups* counts ``.parent`` steps from ``__file__``, None when the chain does
    not start there; *anchored* is True when it starts at ``__file__`` or at a
    name a test-side module exports. A None segment is not a literal.
    *unplaced* marks a root whose place the selector does not know, such as a
    name a ``tests/support`` module exports; *dynamic* a chain that could name
    any path at all; *ancestor* one that starts at some directory above
    ``__file__``, as an upward search (``for p in Path(__file__).parents``).
    """

    ups: int | None
    anchored: bool
    parts: tuple[str | None, ...] = ()
    unplaced: bool = False
    dynamic: bool = False
    ancestor: bool = False

    def extend(self, parts: Iterable[str | None]) -> _Chain:
        return dataclasses.replace(self, parts=(*self.parts, *parts))

    def up(self, steps: int) -> _Chain | None:
        """*steps* ``.parent`` steps: a literal segment drops, or the chain
        climbs from ``__file__``. Above a variable or a glob, no place is known."""
        if len(self.literal()) != len(self.parts):
            return dataclasses.replace(self, dynamic=True) if self.anchored else None
        parts, ups = list(self.parts), self.ups
        for _ in range(steps):
            if parts and parts[-1] != "..":
                parts.pop()
            elif not parts and ups is not None:
                ups += 1
            else:
                parts.append("..")
        return dataclasses.replace(self, ups=ups, parts=tuple(parts))

    def literal(self) -> tuple[str, ...]:
        """The segments spelled out before the first variable or glob."""
        out = []
        for part in self.parts:
            if part is None or part == _STOP:
                break
            out.append(part)
        return tuple(out)

    def from_variable(self, walks: bool = False) -> bool:
        """A variable right after the anchor, a walk or a path under a root
        whose place is unknown, or a variable under a step up: the path could
        be anything."""
        if not self.anchored:
            return False
        if self.dynamic or (self.unplaced and (self.parts or walks)):
            return True
        # ``__file__`` + "/../" + rel: the steps up name no place either.
        parts = [p for p in self.parts if p != ".."][:1] or [""]
        return bool(self.parts) and parts[0] is None


@dataclass(eq=False)
class _Scope:
    """The names one function body, or the module, reads: each with the
    values its assignments give it and the scope each value is read in. An
    empty tuple is a name bound to something the selector cannot read."""

    names: dict[str, tuple[tuple[ast.expr, _Scope], ...]] = field(default_factory=dict)
    #: Parameters without a default: a fixture of that name may give a value.
    params: set[str] = field(default_factory=set)


@dataclass
class _Names:
    """Every scope in one file, and what its functions return."""

    module: _Scope
    #: ``id(function node) -> its scope``.
    functions: dict[int, _Scope] = field(default_factory=dict)
    #: ``function name -> the values its returns and yields give``.
    returns: dict[str, list[tuple[ast.expr, _Scope]]] = field(default_factory=dict)
    #: ``name -> every value an assignment anywhere gives it``.
    values: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: ``(id(function scope), parameter) -> the argument each call passes``.
    pins: dict[tuple[int, str], list[tuple[ast.expr, _Scope]]] = field(
        default_factory=dict
    )


@dataclass
class _PathContext:
    """What a path chain is read against, and the nodes chains consumed."""

    assigns: _Scope
    #: Names imported from a test-side module: roots such as ``REPO_ROOT``.
    roots: frozenset[str]
    returns: Mapping[str, list[tuple[ast.expr, _Scope]]] = field(default_factory=dict)
    consumed: set[int] = field(default_factory=set)
    #: A name's chain once read: ``(id(scope), name) -> chain``.
    memo: dict[tuple[int, str], _Chain | None] = field(default_factory=dict)
    #: A helper's parameters, as the calls in the file pass them.
    pins: Mapping[tuple[int, str], list[tuple[ast.expr, _Scope]]] = field(
        default_factory=dict
    )
    #: The argument each parameter takes in the chain being read.
    pinned: Mapping[tuple[int, str], tuple[ast.expr, _Scope]] = field(
        default_factory=dict
    )
    #: Parameters with pins a chain read without one.
    touched: list[tuple[int, str]] = field(default_factory=list)
    #: Strings a pinned argument passed into a path that was recorded.
    resolved: set[int] = field(default_factory=set)


def _read_paths(tree: ast.Module, src: bool = False, exports: bool = False) -> _Paths:
    """The repo paths *tree* names: path chains first, then lone strings.
    *src* reads a module under ``src/kew``: only paths off its ``__file__``.
    *exports* is a file others call into, whose functions may hand a caller a
    directory to read anything under."""
    names = _read_names(tree)
    roots = frozenset() if src else _test_side_names(tree)
    ctx = _PathContext(names.module, roots, names.returns)
    if not (src or exports):
        # Another file may call an exported helper with any argument.
        ctx.pins = _pin_parameters(tree, names)
    for name in _only_bases(tree, names.values):
        ctx.consumed.update(id(value) for value in names.values[name])
    found = _Paths()
    nodes = list(_scoped_nodes(tree, names))
    _read_chains(nodes, ctx, found, src)
    if not src:
        _read_strings((node for node, _scope in nodes), ctx, found)
        if exports:
            _read_exports(names, ctx, found)
    return found


def _read_chains(
    nodes: Iterable[tuple[ast.AST, _Scope]], ctx: _PathContext, found: _Paths, src: bool
) -> None:
    """Every path expression no longer one consumed, each in its own scope."""
    for node, scope in nodes:
        if id(node) not in ctx.consumed and _starts_chain(node):
            _record_chain(node, dataclasses.replace(ctx, assigns=scope), found, src)


def _read_strings(nodes: Iterable[ast.AST], ctx: _PathContext, found: _Paths) -> None:
    """The lone strings no chain consumed that name a readable path."""
    for node in nodes:
        text = _string(node)
        if text is not None and id(node) not in ctx.consumed:
            rel = _free_rel(_Chain(None, False, tuple(_pieces(node))).literal())
            if _names_readable(rel):
                found.free.add(rel)
            if id(node) not in ctx.resolved and _CLIMB.search(text):
                found.climbs = True


def _read_exports(names: _Names, ctx: _PathContext, found: _Paths) -> None:
    """A function that hands its caller a root or a directory: the caller
    may read anything under it."""
    for name, bindings in names.returns.items():
        returned = _bound_chain((0, name), bindings, ctx, 0)
        if returned is not None and _hands_out_a_directory(returned):
            _record(dataclasses.replace(returned, dynamic=True), False, found)


def _hands_out_a_directory(chain: _Chain) -> bool:
    """A path a function returns that a caller may extend: a root or a
    directory, not a file the literals name."""
    if not chain.anchored:
        return False
    literal = chain.literal()
    return len(literal) != len(chain.parts) or not literal or "." not in literal[-1]


def _scoped_nodes(tree: ast.Module, names: _Names) -> Iterator[tuple[ast.AST, _Scope]]:
    """Every node, parents before children, with the scope it is read in."""
    stack: list[tuple[ast.AST, _Scope]] = [(tree, names.module)]
    while stack:
        node, scope = stack.pop()
        yield node, scope
        inner = names.functions.get(id(node), scope)
        stack.extend(
            (child, inner) for child in reversed(list(ast.iter_child_nodes(node)))
        )


def _read_names(tree: ast.Module) -> _Names:
    """The module's names, each function's own, and what each function
    returns; a parameter named after a function in the file (a fixture) takes
    that function's value."""
    names = _Names(_Scope())
    _bind_block(names.module, tree.body, names)
    stack: list[tuple[ast.AST, _Scope]] = [(tree, names.module)]
    while stack:
        node, scope = stack.pop()
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (*_FUNCTIONS, ast.Lambda)):
                inner = _function_scope(child, scope, names)
                names.functions[id(child)] = inner
            stack.append((child, inner))
    for scope in names.functions.values():
        for param in scope.params:
            if param in names.returns and not scope.names.get(param):
                scope.names[param] = tuple(names.returns[param])
    return names


def _pin_parameters(
    tree: ast.Module, names: _Names
) -> dict[tuple[int, str], list[tuple[ast.expr, _Scope]]]:
    """Each parameter of a helper the file only calls, by name and without
    ``*args``: the argument every call passes it, in the caller's scope. A
    helper used any other way, decorated, or defined twice takes no pins."""
    calls, loose = _call_sites(tree, names)
    defs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTIONS):
            defs.setdefault(node.name, []).append(node)
    pins: dict[tuple[int, str], list[tuple[ast.expr, _Scope]]] = {}
    for name, fns in defs.items():
        sites = calls.get(name, [])
        if len(fns) == 1 and sites and name not in loose and not fns[0].decorator_list:
            _pin_helper(fns[0], sites, names, pins)
    return pins


def _call_sites(
    tree: ast.Module, names: _Names
) -> tuple[dict[str, list[tuple[ast.Call, _Scope]]], set[str]]:
    """Each call by a bare name, with the scope it is made in, and the names
    the file uses any other way."""
    calls: dict[str, list[tuple[ast.Call, _Scope]]] = {}
    for node, scope in _scoped_nodes(tree, names):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.setdefault(node.func.id, []).append((node, scope))
    callees = {id(call.func) for sites in calls.values() for call, _ in sites}
    loose = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in callees
    }
    return calls, loose


def _pin_helper(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    sites: Sequence[tuple[ast.Call, _Scope]],
    names: _Names,
    pins: dict[tuple[int, str], list[tuple[ast.expr, _Scope]]],
) -> None:
    scope = names.functions[id(fn)]
    positional = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    for param in [*positional, *(a.arg for a in fn.args.kwonlyargs)]:
        index = positional.index(param) if param in positional else None
        values = [(_argument(call, index, param), s) for call, s in sites]
        if all(value is not None for value, _ in values):
            pins[(id(scope), param)] = [(v, s) for v, s in values if v is not None]


def _argument(call: ast.Call, index: int | None, param: str) -> ast.expr | None:
    """What *call* passes *param*; None when the selector cannot say."""
    if any(isinstance(a, ast.Starred) for a in call.args):
        return None
    if any(k.arg is None for k in call.keywords):
        return None
    named = [k.value for k in call.keywords if k.arg == param]
    if named:
        return named[0]
    return call.args[index] if index is not None and index < len(call.args) else None


def _function_scope(
    fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    enclosing: _Scope,
    names: _Names,
) -> _Scope:
    scope = _Scope(dict(enclosing.names))
    for name, default in _parameters(fn.args):
        scope.names[name] = () if default is None else ((default, enclosing),)
        if default is None:
            scope.params.add(name)
    if isinstance(fn, _FUNCTIONS):
        _bind_block(scope, fn.body, names)
        for value in _returned(fn):
            names.returns.setdefault(fn.name, []).append((value, scope))
    return scope


def _parameters(args: ast.arguments) -> list[tuple[str, ast.expr | None]]:
    """Each parameter's name and default, None when it has none."""
    positional = [*args.posonlyargs, *args.args]
    defaults: dict[str, ast.expr | None] = dict(
        zip([a.arg for a in positional][::-1], args.defaults[::-1], strict=False)
    )
    defaults.update(
        (a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    extra = [a for a in (args.vararg, args.kwarg) if a is not None]
    every = [*positional, *args.kwonlyargs, *extra]
    return [(arg.arg, defaults.get(arg.arg)) for arg in every]


def _returned(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.expr]:
    """The values *fn* returns or yields, outside its nested functions."""
    for node in _own_nodes(fn.body):
        if isinstance(node, ast.Return | ast.Yield) and node.value is not None:
            yield node.value


def _own_nodes(body: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Every node in *body*, not descending into a nested function or class."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_FUNCTIONS, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _bind_block(scope: _Scope, body: Sequence[ast.stmt], names: _Names) -> None:
    """Bind each name an assignment in *body* gives a value: a name assigned
    there is local to *scope* and hides the enclosing one."""
    local: dict[str, list[tuple[ast.expr, _Scope]]] = {}
    for node in _own_nodes(body):
        for name, value in _assigned(node):
            local.setdefault(name, [])
            if value is not None:
                local[name].append((value, scope))
                names.values.setdefault(name, []).append(value)
    scope.names.update((name, tuple(values)) for name, values in local.items())


def _assigned(node: ast.AST) -> list[tuple[str, ast.expr | None]]:
    """``(name, value)`` for each name *node* binds; None when no value the
    selector reads is bound."""
    if isinstance(node, ast.Assign):
        return [
            pair for target in node.targets for pair in _targets(target, node.value)
        ]
    if isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
        value = None if isinstance(node, ast.AugAssign) else node.value
        return _targets(node.target, value)
    if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        return _loop_assigned(node.target, node.iter)
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return _targets(node.optional_vars, None)
    return []


def _loop_assigned(
    target: ast.expr, iterable: ast.expr
) -> list[tuple[str, ast.expr | None]]:
    """What a ``for`` loop or a comprehension binds its target to."""
    values = _loop_values(iterable)
    if not values or not isinstance(target, ast.Name):
        return _targets(target, None)
    return [(target.id, value) for value in values]


def _targets(
    target: ast.AST, value: ast.expr | None
) -> list[tuple[str, ast.expr | None]]:
    if isinstance(target, ast.Name):
        return [(target.id, value)]
    if isinstance(target, ast.Tuple | ast.List | ast.Starred):
        return [(n.id, None) for n in ast.walk(target) if isinstance(n, ast.Name)]
    return []


def _loop_values(iterable: ast.expr) -> list[ast.expr] | None:
    """What a loop over *iterable* binds: each element of a literal sequence,
    one entry of a walked directory, or one directory above a path for
    ``x.parents``; None when the selector cannot say."""
    node = iterable
    while isinstance(node, ast.Call) and _dotted(node.func) in _SEQUENCES and node.args:
        node = node.args[0]
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        # ``(start, *start.parents)``: the start, then any directory above it.
        return [v for e in node.elts if (v := _element(e)) is not None]
    ancestor = _ancestor_of(node)
    if ancestor is not None:
        return [ancestor]
    walked = _walked(node)
    if not isinstance(walked, ast.expr):
        return None
    each = ast.Name(id=_EACH, ctx=ast.Load())
    return [ast.BinOp(left=walked, op=ast.Div(), right=each)]


def _element(node: ast.expr) -> ast.expr | None:
    """One element of a literal sequence a loop walks; ``*x.parents`` is any
    directory above ``x``, any other starred value one the selector skips."""
    if not isinstance(node, ast.Starred):
        return node
    return _ancestor_of(node.value)


def _ancestor_of(node: ast.expr) -> ast.expr | None:
    """``x.parents`` as one directory above ``x``, any one; None for anything
    else."""
    if isinstance(node, ast.Attribute) and node.attr == "parents":
        return ast.Attribute(value=node.value, attr=_ANCESTOR, ctx=ast.Load())
    return None


def _starts_chain(node: ast.AST) -> bool:
    """An expression that may spell a path: ``/``, ``+``, ``%``, a call, an
    f-string."""
    if isinstance(node, ast.Call | ast.JoinedStr):
        return True
    return _is_division(node) or _is_concatenation(node) or _is_formatting(node)


def _test_side_names(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for node in _statements(tree.body, functions=True):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or module.split(".")[0] in {"support", "tests"}:
                names.update(a.asname or a.name for a in node.names)
    return frozenset(names)


def _only_bases(tree: ast.Module, values: Mapping[str, list[ast.expr]]) -> set[str]:
    """Names used only as the start of a longer path: their own value is not
    a read, the longer paths are."""
    uses: dict[str, int] = {}
    bases: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            uses[node.id] = uses.get(node.id, 0) + 1
        base = _chain_base(node)
        if isinstance(base, ast.Name):
            bases[base.id] = bases.get(base.id, 0) + 1
    return {n for n in values if bases.get(n) and bases[n] == uses.get(n)}


def _chain_base(node: ast.AST) -> ast.AST | None:
    """What *node* extends into a longer path, when it extends one."""
    if _is_division(node):
        return node.left
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr == "joinpath":
        return node.func.value
    if _dotted(node.func).endswith("path.join") and node.args:
        return node.args[0]
    return None


def _record_chain(node: ast.AST, ctx: _PathContext, found: _Paths, src: bool) -> None:
    """What one path expression reads. A helper's parameter in it is read as
    each argument the file's calls pass it, one path per choice; past
    ``_MAX_VARIANTS`` choices, it stays a variable."""
    walks = _walked(node)
    target = walks if walks is not None else node
    queue: list[dict[tuple[int, str], tuple[ast.expr, _Scope]]] = [{}]
    read = 0
    while queue:
        pinned = queue.pop()
        read += 1
        memo = ctx.memo if not pinned else {}
        probe = dataclasses.replace(
            ctx, pinned=pinned, touched=[], resolved=set(), memo=memo
        )
        chain = _chain(target, probe, 0)
        choices = ctx.pins.get(probe.touched[0], []) if probe.touched else []
        if choices and read + len(queue) + len(choices) <= _MAX_VARIANTS:
            key = probe.touched[0]
            queue.extend({**pinned, key: choice} for choice in choices)
        elif chain is not None and _record(chain, walks is not None, found, src):
            ctx.resolved.update(probe.resolved)


def _record(chain: _Chain, walks: bool, found: _Paths, src: bool = False) -> bool:
    """What one chain reads; *walks* when the whole directory is read. A
    module under ``src/kew`` counts only the literal paths off its own
    ``__file__``. True when the chain named a read."""
    if src and (chain.ups is None or chain.from_variable(walks)):
        return False
    if chain.from_variable(walks):
        # Its reader runs on every change: the literal start names nothing more.
        found.dynamic = True
        return True
    recorded = _record_place(chain, walks, found)
    # A test-side directory read a caller's ``..`` could climb out of.
    found.open_dir = found.open_dir or (recorded and not src and None in chain.parts)
    return recorded


def _record_place(chain: _Chain, walks: bool, found: _Paths) -> bool:
    """The read of a chain that names a place: off ``__file__``, off each
    directory above it for an upward search, or from the project root."""
    literal = chain.literal()
    if chain.ancestor:
        ups: Iterable[int] = range(1, _MAX_UPS + 1)
        return any([_record_local(n, literal, walks, found) for n in ups])
    if chain.ups is not None:
        return _record_local(chain.ups, literal, walks, found)
    free = _free_rel(literal)
    if free and (chain.anchored or _names_readable(free)):
        found.free.add(free)
        return True
    return False


def _record_local(ups: int, parts: Sequence[str], walks: bool, found: _Paths) -> bool:
    """A path *ups* steps up from ``__file__``, relative to its directory."""
    if ups == 0:
        # ``__file__`` / ".." / rel: the first ".." is the file's directory.
        if not parts or parts[0] != "..":
            return False
        ups, parts = 1, parts[1:]
    rel = "/".join(parts)
    if rel or walks:
        found.local.add("/".join([*[".."] * (ups - 1), rel]).strip("/") or ".")
        return True
    return False


def _free_rel(parts: Sequence[str]) -> str:
    """A path a test spells without an anchor, as the repo path it names:
    ``../docs/a.md`` from the project root is ``docs/a.md``."""
    rel = posixpath.normpath("/".join(parts)) if parts else ""
    while rel.startswith("../"):
        rel = rel[3:]
    return "" if rel in (".", "..") else rel


def _walked(node: ast.AST) -> ast.AST | None:
    """The directory ``x.rglob(...)`` or ``os.walk(x)`` reads all of."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in _WALKS:
        return None
    if _dotted(node.func) in {"os.walk", "os.listdir", "os.scandir"}:
        return node.args[0] if node.args else None
    return node.func.value


def _chain(node: ast.AST | None, ctx: _PathContext, depth: int) -> _Chain | None:
    """*node* as a path chain, through the names in scope; None when it does
    not start anywhere the selector can name. An expression nested past
    what it follows could be any path."""
    if node is None:
        return None
    if depth > _MAX_DEPTH:
        return _Chain(None, True, dynamic=True)
    ctx.consumed.add(id(node))
    if isinstance(node, ast.Name):
        return _named_chain(node.id, ctx, depth)
    if _string(node) is not None:
        return _Chain(None, False, tuple(_pieces(node)))
    if _is_division(node):
        left = _chain(node.left, ctx, depth + 1) or _Chain(None, False)
        return left.extend(_pieces(node.right, ctx))
    if isinstance(node, ast.JoinedStr):
        return _fstring_chain(node, ctx, depth)
    if isinstance(node, ast.BinOp):
        return _text_chain(node, ctx, depth)
    return _step_chain(node, ctx, depth)


def _text_chain(node: ast.BinOp, ctx: _PathContext, depth: int) -> _Chain | None:
    """``root + "/docs/a.md"`` and ``"%s/docs/a.md" % root``."""
    if _is_concatenation(node):
        start = _anchored_chain(node.left, ctx, depth, _string(node.right))
        return start and start.extend(_text_pieces([node.right], ctx))
    if _is_formatting(node):
        right = node.right
        args = list(right.elts) if isinstance(right, ast.Tuple) else [right]
        return _format_chain(str(_string(node.left)), args, ctx, depth)
    return None


def _is_concatenation(node: ast.AST | None) -> TypeGuard[ast.BinOp]:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _is_formatting(node: ast.AST | None) -> TypeGuard[ast.BinOp]:
    """``"%s/docs/a.md" % ROOT``."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and _string(node.left) is not None
    )


def _anchored_chain(
    node: ast.AST, ctx: _PathContext, depth: int, then: str | None = "/"
) -> _Chain | None:
    """*node* as a chain only when it starts at ``__file__`` or a root: a
    string joined onto anything else stays a lone string. A root whose place
    is unknown counts only when the text *then* joined on starts a path."""
    probe = dataclasses.replace(ctx, consumed=set())
    chain = _chain(node, probe, depth + 1)
    if chain is None or not chain.anchored:
        return None
    if chain.unplaced and not (then or "").startswith("/"):
        return None
    ctx.consumed.update(probe.consumed)
    return chain


def _fstring_chain(node: ast.JoinedStr, ctx: _PathContext, depth: int) -> _Chain | None:
    """``f"{Path(__file__).parent}/../docs/a.md"``: the chain in the first
    field, then the text after it."""
    first = node.values[0] if node.values else None
    if not isinstance(first, ast.FormattedValue):
        return None
    then = _string(node.values[1]) if len(node.values) > 1 else None
    base = _anchored_chain(first.value, ctx, depth, then)
    if base is None:
        return None
    ctx.consumed.update(id(value) for value in node.values)
    return base.extend(_text_pieces(node.values[1:], ctx))


def _format_chain(
    template: str, args: Sequence[ast.expr], ctx: _PathContext, depth: int
) -> _Chain | None:
    """``"%s/docs/a.md" % ROOT`` and ``"{}/docs/a.md".format(ROOT)``: the root
    in the first placeholder, then the text after it. A root anywhere else
    names no place the selector can follow."""
    first = _PLACEHOLDER.match(template)
    rest = template[first.end() :] if first else ""
    rooted = [c for arg in args if (c := _anchored_chain(arg, ctx, depth, rest))]
    if not rooted:
        return None
    base = _anchored_chain(args[0], ctx, depth, rest) if first else None
    if first is None or base is None:
        return dataclasses.replace(rooted[0], dynamic=True)
    later = _PLACEHOLDER.search(rest)
    if later is None:
        return base.extend(_text_pieces([ast.Constant(rest)], ctx))
    each = ast.Name(id=_EACH, ctx=ast.Load())
    return base.extend(_text_pieces([ast.Constant(rest[: later.start()]), each], ctx))


def _text_pieces(values: Sequence[ast.AST], ctx: _PathContext) -> list[str | None]:
    """Text appended to a path as segments, up to the first value that is not
    a literal; a leading ``/`` only separates it from the path before."""
    text = ""
    for value in values:
        inner = _segment(value, ctx)
        if isinstance(inner, ast.FormattedValue):
            inner = _segment(inner.value, ctx)
        piece = _string(inner)
        if piece is None:
            text = text.rpartition("/")[0]
            return [*_pieces(ast.Constant(text.lstrip("/") or ".")), None]
        text += piece
    return _pieces(ast.Constant(text.lstrip("/") or "."))


def _step_chain(node: ast.AST, ctx: _PathContext, depth: int) -> _Chain | None:
    """``x.parent``, ``x.parents[n]`` and the path calls."""
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _up(_chain(node.value, ctx, depth + 1), 1)
    if isinstance(node, ast.Attribute) and node.attr == _ANCESTOR:
        return _ancestor_chain(_chain(node.value, ctx, depth + 1))
    if isinstance(node, ast.Subscript):
        return _parents_chain(node, ctx, depth)
    if isinstance(node, ast.Call):
        return _call_chain(node, ctx, depth)
    return None


def _ancestor_chain(base: _Chain | None) -> _Chain | None:
    """One directory above *base*, as a loop over ``base.parents`` binds it:
    an upward search when *base* is ``__file__`` or a step up from it; above
    any other anchored path, no place is known."""
    if base is None or not base.anchored:
        return None
    if not base.parts and (base.ups is not None or base.ancestor):
        return _Chain(None, True, ancestor=True)
    return dataclasses.replace(base, dynamic=True)


def _parents_chain(node: ast.Subscript, ctx: _PathContext, depth: int) -> _Chain | None:
    """``x.parents[2]``: three steps up from ``x``. An index the selector
    cannot read leaves any place above ``x``."""
    index = node.slice
    if not isinstance(node.value, ast.Attribute) or node.value.attr != "parents":
        return None
    base = _chain(node.value.value, ctx, depth + 1)
    if not isinstance(index, ast.Constant) or not isinstance(index.value, int):
        return (
            dataclasses.replace(base, dynamic=True) if base and base.anchored else None
        )
    return _up(base, index.value + 1)


def _named_chain(name: str, ctx: _PathContext, depth: int) -> _Chain | None:
    if name == "__file__":
        return _Chain(0, True)
    bindings = ctx.assigns.names.get(name)
    if bindings:
        return _bound_chain((id(ctx.assigns), name), bindings, ctx, depth)
    if bindings is None and name in ctx.roots:
        return _Chain(None, True, unplaced=True)
    return None


def _bound_chain(
    key: tuple[int, str],
    bindings: Iterable[tuple[ast.expr, _Scope]],
    ctx: _PathContext,
    depth: int,
) -> _Chain | None:
    """The chain a name's values give, read once per scope."""
    if key not in ctx.memo:
        ctx.memo[key] = None
        chains = [
            _chain(value, dataclasses.replace(ctx, assigns=scope), depth + 1)
            for value, scope in bindings
        ]
        ctx.memo[key] = _merge(chains)
    return ctx.memo[key]


def _merge(chains: Iterable[_Chain | None]) -> _Chain | None:
    """One chain for a name assigned several values: the segments they share
    then a variable, or any path at all when they start in different places."""
    found = list(dict.fromkeys(c for c in chains if c is not None))
    if len(found) <= 1:
        return found[0] if found else None
    first = found[0]
    place = (first.ups, first.unplaced, first.ancestor)
    if any((c.ups, c.unplaced, c.ancestor) != place for c in found):
        return _Chain(None, True, dynamic=True) if _any_anchored(found) else None
    return dataclasses.replace(
        first,
        anchored=any(c.anchored for c in found),
        parts=(*_shared_parts(found), None),
        dynamic=any(c.dynamic for c in found),
    )


def _shared_parts(chains: Sequence[_Chain]) -> list[str | None]:
    """The segments every chain starts with."""
    shared: list[str | None] = []
    for segments in zip(*(c.parts for c in chains), strict=False):
        if len(set(segments)) != 1:
            break
        shared.append(segments[0])
    return shared


def _any_anchored(chains: Iterable[_Chain]) -> bool:
    return any(c.anchored for c in chains)


def _up(chain: _Chain | None, steps: int) -> _Chain | None:
    return None if chain is None else chain.up(steps)


def _call_chain(call: ast.Call, ctx: _PathContext, depth: int) -> _Chain | None:
    """``Path(a, b)``, ``a.joinpath(b)``, ``os.path.join(a, b)``,
    ``a.resolve()``, ``str(a)``, ``os.path.dirname(a)``, ``a.with_suffix(s)``,
    ``"{}/b".format(a)``, and a call to a function that returns a path."""
    func = call.func
    args = _path_call_args(call)
    if args is None and isinstance(func, ast.Attribute):
        return _method_chain(call, func, ctx, depth)
    if args is None:
        return _returned_chain(func, ctx, depth)
    base = _chain(args[0], ctx, depth + 1) if args else None
    if base is None:
        return None
    if _dotted(func).endswith("dirname"):
        return base.up(1)
    return base.extend(p for arg in args[1:] for p in _pieces(arg, ctx))


def _path_call_args(call: ast.Call) -> list[ast.expr] | None:
    """The path a path call builds on, then its segments; None for any other
    call."""
    name = _dotted(call.func)
    tail = name.rpartition(".")[2]
    if name.endswith(_PATH_FUNCTIONS) or tail in _PATH_CONSTRUCTORS:
        return list(call.args)
    if isinstance(call.func, ast.Attribute) and tail in _PATH_METHODS:
        return [call.func.value, *call.args]
    return None


def _method_chain(
    call: ast.Call, func: ast.Attribute, ctx: _PathContext, depth: int
) -> _Chain | None:
    """``a.with_suffix(s)`` and ``"{}/b".format(a)``."""
    if func.attr in _RENAMES:
        return _renamed(_chain(func.value, ctx, depth + 1), func.attr, call)
    template = _string(func.value)
    if func.attr != "format" or template is None:
        return None
    return _format_chain(template, call.args, ctx, depth)


def _returned_chain(func: ast.expr, ctx: _PathContext, depth: int) -> _Chain | None:
    """``repo_root()``: what a function of the file returns."""
    if not isinstance(func, ast.Name) or func.id not in ctx.returns:
        return None
    return _bound_chain((0, func.id), ctx.returns[func.id], ctx, depth)


def _renamed(base: _Chain | None, how: str, call: ast.Call) -> _Chain | None:
    """``x.with_suffix(".md")``: *x*'s directory, then the renamed segment, or
    a variable in that directory when the new name is not a literal."""
    parent = _up(base, 1)
    if base is None or parent is None:
        return None
    literal = _string(call.args[0]) if call.args else None
    last = base.parts[-1] if base.parts and not parent.dynamic else None
    name = None if literal is None else _new_name(last, how, literal)
    return parent.extend([None] if name is None else _split(name))


def _new_name(last: str | None, how: str, literal: str) -> str | None:
    """The segment ``with_name``, ``with_suffix`` or ``with_stem`` gives; None
    when the selector cannot spell it, and the caller reads a variable."""
    if how == "with_name":
        return literal
    if last is None or last in ("..", _STOP) or "/" in literal:
        return None
    current = PurePosixPath(last)
    if how == "with_stem":
        return f"{literal}{current.suffix}" if literal else None
    valid = literal == "" or (literal.startswith(".") and literal != ".")
    return f"{current.stem}{literal}" if valid else None


def _pieces(node: ast.AST, ctx: _PathContext | None = None) -> list[str | None]:
    """One segment expression as path segments: a literal split on ``/``, an
    f-string's literal start then a variable, anything else a variable."""
    if ctx is not None:
        node = _segment(node, ctx)
    text, tail = _string(node), []
    if text is None and isinstance(node, ast.JoinedStr) and node.values:
        text, tail = (_string(node.values[0]) or "").rpartition("/")[0], [None]
    if text is None or not _PATH_LIKE.match(text or "."):
        return [None]
    return [*_split(text), *tail]


def _segment(node: ast.AST, ctx: _PathContext) -> ast.AST:
    """A segment consumed by its chain, through a name with one value, or a
    parameter to the argument its call passes, and on through the caller's."""
    ctx.consumed.add(id(node))
    scope, pinned = ctx.assigns, False
    for _ in range(_MAX_PIN_HOPS):
        if not isinstance(node, ast.Name):
            break
        key = (id(scope), node.id)
        bindings = scope.names.get(node.id, ())
        if key in ctx.pinned:
            (node, scope), pinned = ctx.pinned[key], True
        elif key in ctx.pins:
            ctx.touched.append(key)
            break
        elif len(bindings) == 1:
            node, scope = bindings[0]
        else:
            break
    if pinned:
        ctx.resolved.update(_leading_strings(node))
    return node


def _leading_strings(node: ast.AST) -> list[int]:
    """The strings a pinned argument spells before its first variable."""
    if isinstance(node, ast.JoinedStr) and node.values:
        node = node.values[0]
    return [id(node)] if _string(node) is not None else []


def _split(text: str) -> list[str | None]:
    """A literal path's segments, up to a glob."""
    parts: list[str | None] = []
    for part in text.split("/"):
        if _GLOB.search(part):
            return [*parts, _STOP]
        if part not in ("", "."):
            parts.append(part)
    return parts


def _names_readable(rel: str) -> bool:
    """A string that names a path a test may read outside ``src/kew``."""
    parts = rel.split("/")
    if not rel or ".." in parts:
        return False
    return parts[0] in _READ_AREAS or "tests/fixtures" in rel


# ── the graph ───────────────────────────────────────────────────────────────


def resolve_kew(name: str, modules: Mapping[str, str]) -> list[str]:
    """The kew modules importing *name* runs: it and every parent package.
    ``kew.x.*`` is every module under ``kew.x``."""
    if name.endswith(".*"):
        base = name.removesuffix(".*")
        under = sorted(m for m in modules if m.startswith(f"{base}."))
        return [*under, *resolve_kew(base, modules)]
    parts = name.split(".")
    while parts and ".".join(parts) not in modules:
        parts.pop()
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _resolve_local(name: str, importer: str, snap: Snapshot) -> list[str]:
    """A non-kew import from a test-side file, as a file the snapshot read."""
    rel = name.replace(".", "/")
    roots = [
        f"{snap.prefix}tests/",
        f"{PurePosixPath(importer).parent}/",
        snap.prefix,
    ]
    for root in roots:
        for path in (f"{root}{rel}.py", f"{root}{rel}/__init__.py"):
            if path in snap.facts:
                return [path]
    return []


def _test_side_parents(path: str, snap: Snapshot) -> list[str]:
    """The ``conftest.py`` files pytest loads for *path*, below ``tests/``, and
    the package ``__init__.py`` files importing it runs, ``tests/``'s own too.

    The root ``conftest.py`` is not an edge: a change it reaches runs the full
    suite.
    """
    root = PurePosixPath(f"{snap.prefix}tests")
    found = []
    for parent in PurePosixPath(path).parents:
        if parent != root and root not in parent.parents:
            break
        conftest = f"{parent}/conftest.py"
        if parent != root and conftest in snap.facts:
            found.append(conftest)
        init = f"{parent}/__init__.py"
        if init in snap.facts and init != path:
            found.append(init)
        if parent == root:
            break
    return found


def edges(snap: Snapshot, *, eager: bool = False) -> Graph:
    """The forward import graph of one tree; *eager* leaves out the imports
    made inside a function."""
    graph: Graph = {}
    for node, facts in snap.facts.items():
        targets: set[str] = set()
        for name in facts.eager if eager else facts.imports:
            if name == "kew" or name.startswith("kew."):
                targets.update(resolve_kew(name, snap.module_paths))
            elif not _is_module(node):
                targets.update(_resolve_local(name, node, snap))
        if not _is_module(node):
            targets.update(snap.loaded.get(node, ()))
            targets.update(_test_side_parents(node, snap))
        targets.discard(node)
        graph[node] = targets
    return graph


def union(*graphs: Graph) -> Graph:
    merged: Graph = {}
    for graph in graphs:
        for node, targets in graph.items():
            merged.setdefault(node, set()).update(targets)
    return merged


def reverse(graph: Graph) -> Graph:
    rev: Graph = {node: set() for node in graph}
    for node, targets in graph.items():
        for target in targets:
            rev.setdefault(target, set()).add(node)
    return rev


def toward_changed(graph: Graph, changed: Iterable[str]) -> dict[str, str | None]:
    """Each affected node's next hop on a shortest chain to a changed node."""
    rev = reverse(graph)
    hop: dict[str, str | None] = {node: None for node in sorted(changed)}
    queue = deque(hop)
    while queue:
        node = queue.popleft()
        for importer in sorted(rev.get(node, ())):
            if importer not in hop:
                hop[importer] = node
                queue.append(importer)
    return hop


def forward_closure(graph: Graph, starts: Iterable[str]) -> set[str]:
    seen = set(starts)
    stack = list(seen)
    while stack:
        for target in graph.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def chain(hop: Mapping[str, str | None], start: str) -> list[str]:
    path = [start]
    while (nxt := hop[path[-1]]) is not None:
        path.append(nxt)
    return path


# ── the selection ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Selection:
    """A selection and why: ``full_rule`` set means the full suite runs."""

    base_tree: str
    candidate_tree: str
    total_files: int
    total_tests: int
    gate_paths: tuple[str, ...] = ()
    gate_re: str | None = None
    selected: tuple[tuple[str, str], ...] = ()
    selected_tests: int = 0
    full_rule: str | None = None
    full_path: str | None = None

    @property
    def share(self) -> float:
        """The selected share of collected tests; a full suite is all of them."""
        if self.full_rule is not None or not self.total_tests:
            return 1.0
        return self.selected_tests / self.total_tests

    @property
    def before_graph(self) -> bool:
        """A rule forced the full suite before the import graph was read."""
        return self.full_rule is not None and self.full_rule not in GRAPH_RULES

    def lines(self) -> list[str]:
        gate_digest = hashlib.sha256("\n".join(self.gate_paths).encode()).hexdigest()
        head = [
            f"selector v{SELECTOR_VERSION}",
            f"base tree {self.base_tree}",
            f"candidate tree {self.candidate_tree}",
            f"merge gate paths: {len(self.gate_paths)} patterns, "
            f"sha256:{gate_digest[:16]}",
            f"GATE_RE: {'unset' if self.gate_re is None else self.gate_re}",
        ]
        if self.full_rule is not None:
            body = [
                f"result: full suite ({self.total_files} test files)",
                f"rule: {self.full_rule}: {self.full_path}",
            ]
        else:
            body = [
                f"result: {len(self.selected)} of {self.total_files} test files, "
                f"{self.selected_tests} of {self.total_tests} tests",
                *(f"{path}  {why}" for path, why in self.selected),
            ]
        return [*head, *body]

    def digest(self) -> str:
        text = "\n".join(self.lines()) + "\n"
        return hashlib.sha256(text.encode()).hexdigest()

    def render(self) -> str:
        return "\n".join([*self.lines(), f"digest sha256:{self.digest()}"]) + "\n"


@dataclass(frozen=True)
class Inputs:
    """Everything a selection reads, already out of git."""

    base: Snapshot
    candidate: Snapshot
    changed: tuple[str, ...]
    base_tree: str
    candidate_tree: str
    #: The default merge gate paths, plus the candidate ``kew.toml``'s own.
    gate_paths: tuple[str, ...] = ()
    #: Why the candidate's ``kew.toml`` could not be read, when it could not.
    config_error: str | None = None


@dataclass(frozen=True)
class GraphView:
    """What a what-if rewrites: the unioned graph and the changed nodes."""

    inputs: Inputs
    graph: Graph
    eager: Graph
    changed: frozenset[str]


#: Rewrites a graph and its changed nodes into a what-if's.
Transform = Callable[[GraphView], tuple[Graph, set[str]]]


def _is_test_file(path: str, prefix: str) -> bool:
    if not path.startswith(f"{prefix}tests/"):
        return False
    rel = path[len(prefix) :]
    name = PurePosixPath(rel).name
    in_tests = rel.startswith("tests/") and not rel.startswith("tests/support/")
    return in_tests and (name.startswith("test_") or name.endswith("_test.py"))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        pattern = f"{pattern}**"
    return PurePosixPath(path).full_match(pattern)


def _is_readable(path: str, prefix: str) -> bool:
    """A path the file-read rule answers for: a file under docs, frontend,
    docker, scripts or ``tests/fixtures``. A file at the root or under
    ``www/`` is not one: a script a test runs may read it unseen."""
    for rel in (path, path[len(prefix) :] if path.startswith(prefix) else ""):
        parts = rel.split("/")
        if len(parts) > 1 and parts[0] in _READ_AREAS:
            return True
    return path.startswith(f"{prefix}tests/fixtures/")


def _full_rules(
    prefix: str,
    gate_paths: Sequence[str],
    gate_re: re.Pattern[str] | None,
    reads: bool = True,
) -> list[tuple[str, Callable[[str], bool]]]:
    """Each path rule that forces the full suite, in the order it is named;
    *reads* False is the selector before the file-read rule (#3211)."""

    def untraced(path: str) -> bool:
        return not (reads and _is_readable(path, prefix))

    return [
        ("tests/conftest.py changed", lambda p: p == f"{prefix}tests/conftest.py"),
        ("tests/support changed", lambda p: p.startswith(f"{prefix}tests/support/")),
        ("uv.lock changed", lambda p: PurePosixPath(p).name == "uv.lock"),
        (
            "a merge gate path changed",
            lambda p: any(_matches(p, pattern) for pattern in gate_paths),
        ),
        ("GATE_RE matched", lambda p: bool(gate_re and gate_re.search(p))),
        (
            "a non-.py file under src/ changed",
            lambda p: p.startswith(f"{prefix}src/") and not p.endswith(".py"),
        ),
        (
            "a non-.py file under tests/ changed",
            lambda p: (
                p.startswith(f"{prefix}tests/")
                and not p.endswith(".py")
                and untraced(p)
            ),
        ),
        # A path outside every area the file-read rule answers for: nothing
        # says which test reads it.
        (
            "an untraced path changed",
            lambda p: _node_for(p, prefix) is None and untraced(p),
        ),
    ]


def _path_rule(
    inputs: Inputs, gate_re: re.Pattern[str] | None, reads: bool
) -> tuple[str, str] | None:
    rules = _full_rules(inputs.candidate.prefix, inputs.gate_paths, gate_re, reads)
    for rule, fires in rules:
        for path in inputs.changed:
            if fires(path):
                return rule, path
    return None


def _unparsed(inputs: Inputs) -> str | None:
    """A file either tree could not parse: its imports are unknown."""
    bad = {
        snap.module_paths.get(node, node)
        for snap in (inputs.base, inputs.candidate)
        for node, facts in snap.facts.items()
        if not facts.parsed
    }
    return min(bad) if bad else None


def _pre_graph_rule(
    inputs: Inputs, gate_re: re.Pattern[str] | None, reads: bool
) -> tuple[str, str] | None:
    if inputs.config_error is not None:
        return "kew.toml could not be read", inputs.config_error
    fired = _path_rule(inputs, gate_re, reads)
    if fired is not None:
        return fired
    bad = _unparsed(inputs)
    if bad is not None:
        return "a Python file could not be parsed", bad
    return None


def _changed_nodes(inputs: Inputs) -> set[str]:
    nodes: set[str] = set()
    for snap in (inputs.base, inputs.candidate):
        by_path = {snap.module_paths.get(n, n): n for n in snap.facts}
        nodes.update(by_path[p] for p in inputs.changed if p in by_path)
    return nodes


def _unimported_helper(inputs: Inputs, graph: Graph, changed: set[str]) -> str | None:
    """A changed file under ``tests/`` that pytest does not collect and nothing
    imports: whatever reads it, the graph cannot say."""
    prefix = inputs.candidate.prefix
    imported = set().union(*graph.values())
    orphans = sorted(
        node
        for node in changed
        if not _is_module(node)
        and not _is_test_file(node, prefix)
        and node not in imported
    )
    return orphans[0] if orphans else None


def _conftest_rule(
    inputs: Inputs, graph: Graph, changed: set[str], reads: bool
) -> tuple[str, str] | None:
    """A changed node ``tests/conftest.py`` reaches — through ``tests/support``
    and its import calls too — or a changed file it reads, or any file the
    file-read rule answers for when it reads a path it builds or runs a
    script: every test loads it."""
    conftest = f"{inputs.candidate.prefix}tests/conftest.py"
    reach = forward_closure(graph, {conftest})
    hit = sorted((reach - {conftest}) & changed)
    if hit:
        node = hit[0]
        nodes = (inputs.base.facts, inputs.candidate.facts)
        imported = any(node in facts for facts in nodes)
        return (CONFTEST_RULE if imported else CONFTEST_READ_RULE), node
    prefix = inputs.candidate.prefix
    readable = sorted(p for p in inputs.changed if _is_readable(p, prefix))
    if reads and readable and _reads_blind_any(reach, inputs):
        return CONFTEST_READ_RULE, readable[0]
    return None


def _reads_blind_any(nodes: set[str], inputs: Inputs) -> bool:
    """One of *nodes* reads a path the selector cannot name, or one climbs
    with ``..`` and one reads a directory under a variable."""
    if any(_reads_blind(n, inputs) for n in nodes):
        return True
    facts = [
        f
        for snap in (inputs.base, inputs.candidate)
        for n in nodes
        if (f := snap.facts.get(n)) is not None
    ]
    return any(f.climbs for f in facts) and any(f.open_dir for f in facts)


def _reads_blind(node: str, inputs: Inputs) -> bool:
    """*node* reads a path the selector cannot name, in either tree."""
    for snap in (inputs.base, inputs.candidate):
        facts = snap.facts.get(node)
        if facts is not None and (facts.dynamic_read or facts.runs_script):
            return True
    return False


def _reads_of(path: str, facts: FileFacts, prefix: str) -> Iterator[str]:
    """The repo-relative paths the file at *path* reads; ``""`` is the
    repository."""
    for rel in facts.reads:
        yield rel
        if prefix:
            yield f"{prefix}{rel}"
    parent = PurePosixPath(path).parent
    for rel in facts.local_reads:
        joined = posixpath.normpath(f"{parent}/{rel}")
        if joined != ".." and not joined.startswith("../"):
            yield "" if joined == "." else joined


def _read_hits(inputs: Inputs) -> dict[tuple[str, str], str]:
    """``(reader, changed path) -> what it reads``, in both trees, for each
    changed path a test-side file or a kew module reads."""
    prefix = inputs.candidate.prefix
    changed = [p for p in inputs.changed if _is_readable(p, prefix)]
    hits: dict[tuple[str, str], str] = {}
    for snap in (inputs.base, inputs.candidate) if changed else ():
        for node, facts in snap.facts.items():
            path = snap.module_paths.get(node, node)
            for read in _reads_of(path, facts, snap.prefix):
                for path in changed:
                    _hit(hits, (node, path), read)
    return hits


def _hit(hits: dict[tuple[str, str], str], key: tuple[str, str], read: str) -> None:
    """Record that ``key[0]`` reads ``key[1]`` through *read*, keeping the
    narrowest read that covers it."""
    path = key[1]
    if read and path != read and not path.startswith(f"{read}/"):
        return
    shown = path if path == read else f"{read}/" if read else "./"
    if len(shown) > len(hits.get(key, "")):
        hits[key] = shown


@dataclass(frozen=True)
class _Reach:
    """Which test-side files reach each kind of node the always rules name."""

    kew: Mapping[str, str | None]
    dynamic: Mapping[str, str | None]
    dynamic_read: Mapping[str, str | None]
    runs_script: Mapping[str, str | None]
    #: Files reaching both a string that climbs with ``..`` and a directory
    #: read with a variable after it: the climb may leave that directory.
    climbs: frozenset[str] = frozenset()


def _reach(cand: Snapshot, graph: Graph, reads: bool) -> _Reach:
    facts = list(cand.facts.items()) if reads else []
    climbs = _reaching(graph, facts, "climbs").keys()
    return _Reach(
        kew=toward_changed(graph, (n for n in graph if _is_module(n))),
        dynamic=toward_changed(graph, _dynamic_nodes(cand)),
        dynamic_read=_reaching(graph, facts, "dynamic_read"),
        runs_script=_reaching(graph, facts, "runs_script"),
        climbs=frozenset(climbs & _reaching(graph, facts, "open_dir").keys()),
    )


def _reaching(
    graph: Graph, facts: Sequence[tuple[str, FileFacts]], kind: str
) -> dict[str, str | None]:
    """The files reaching a file whose fact *kind* is set."""
    return toward_changed(graph, (n for n, f in facts if getattr(f, kind)))


def _always_reason(
    path: str, inputs: Inputs, reach: _Reach, read: str | None
) -> str | None:
    facts = inputs.candidate.facts[path]
    rules = (
        (path in inputs.changed, "always: changed by the candidate"),
        (read is not None, read),
        (facts.policy, "always: marked policy"),
        (path in reach.dynamic, "always: runs or loads kew dynamically"),
        (path in reach.dynamic_read, "always: builds a repo path from a variable"),
        (path in reach.climbs, CLIMB_REASON),
        (path in reach.runs_script, "always: runs a script"),
        (path not in reach.kew, "always: imports no kew module"),
    )
    return next((why for fires, why in rules if fires), None)


def _explain(
    path: str,
    hop: Mapping[str, str | None],
    prefix: str,
    hits: Mapping[tuple[str, str], str],
) -> tuple[bool, str]:
    """The chain behind *path*, and whether it ends in a file it reads."""
    nodes = chain(hop, path)
    read = hits.get((nodes[-2], nodes[-1])) if len(nodes) > 1 else None
    shown = [n.removeprefix(prefix) for n in nodes]
    if read is None:
        return False, " → ".join(shown) + " [changed]"
    return True, f"{' → '.join(shown[:-1])} ← reads {read} [changed {nodes[-1]}]"


def _dynamic_nodes(snap: Snapshot) -> Iterator[str]:
    """Test-side files that run or load kew in a way no import names."""
    for node, facts in snap.facts.items():
        if not _is_module(node) and (facts.dynamic or node in snap.unresolved):
            yield node


def _pick(
    inputs: Inputs,
    graph: Graph,
    hop: Mapping[str, str | None],
    hits: Mapping[tuple[str, str], str],
    reads: bool,
) -> list[tuple[str, str]]:
    cand = inputs.candidate
    prefix = cand.prefix
    reach = _reach(cand, graph, reads)
    picked = []
    for path in sorted(p for p in cand.facts if _is_test_file(p, prefix)):
        is_read, traced = (
            _explain(path, hop, prefix, hits) if path in hop else (False, None)
        )
        why = _always_reason(path, inputs, reach, traced if is_read else None)
        why = why or traced
        if why is not None:
            picked.append((path[len(prefix) :], why))
    return picked


def select(
    inputs: Inputs,
    *,
    gate_re: re.Pattern[str] | None = None,
    transform: Transform | None = None,
    reads: bool = True,
) -> Selection:
    """The selection for *inputs*; *transform* rewrites the graph (what-ifs),
    and *reads* False leaves out the file-read rule, as selector v2 did."""
    cand = inputs.candidate
    tests = {p: f for p, f in cand.facts.items() if _is_test_file(p, cand.prefix)}
    base = Selection(
        base_tree=inputs.base_tree,
        candidate_tree=inputs.candidate_tree,
        total_files=len(tests),
        total_tests=sum(f.tests for f in tests.values()),
        gate_paths=inputs.gate_paths,
        gate_re=None if gate_re is None else gate_re.pattern,
    )
    fired = _pre_graph_rule(inputs, gate_re, reads) or _graph_rules(
        inputs, transform, reads
    )
    if isinstance(fired, tuple):
        return _full(base, *fired)
    if tests and len(fired) >= FULL_SUITE_SHARE * len(tests):
        return _full(base, SHARE_RULE, f"{len(fired)} of {len(tests)} test files")
    count = sum(tests[f"{cand.prefix}{p}"].tests for p, _ in fired)
    return dataclasses.replace(base, selected=tuple(fired), selected_tests=count)


def _graph_rules(
    inputs: Inputs, transform: Transform | None, reads: bool
) -> tuple[str, str] | list[tuple[str, str]]:
    """The rule the import graph fires, or else the files it selects."""
    cand = inputs.candidate
    graph = union(edges(inputs.base), edges(cand))
    changed = _changed_nodes(inputs)
    # A changed file a test reads is a node that reader points at.
    hits = _read_hits(inputs) if reads else {}
    for reader, path in hits:
        graph[reader].add(path)
    orphan = _unimported_helper(inputs, graph, changed)
    if orphan is not None:
        return "a tests/ file nothing imports changed", orphan
    changed.update(path for _reader, path in hits)
    if transform is not None:
        eager = union(edges(inputs.base, eager=True), edges(cand, eager=True))
        graph, changed = transform(GraphView(inputs, graph, eager, frozenset(changed)))
    hit = _conftest_rule(inputs, graph, changed, reads)
    if hit is not None:
        return hit[0], hit[1].removeprefix(cand.prefix)
    return _pick(inputs, graph, toward_changed(graph, changed), hits, reads)


def _full(base: Selection, rule: str, path: str) -> Selection:
    return dataclasses.replace(base, full_rule=rule, full_path=path)


def _gate_paths(
    snap: Snapshot, toml_path: str, cache: FactCache
) -> tuple[tuple[str, ...], str | None]:
    """The default merge gate paths plus the ``kew.toml`` committed in *snap*:
    configured paths add to the defaults, never replace them."""
    defaults: list[str] = EpicConfig.model_fields["merge_gate_paths"].default
    oid = snap.files.get(toml_path)
    if oid is None:
        return tuple(defaults), None
    try:
        raw = tomllib.loads(cache.blob(oid).decode())
        extra = _configured_gate_paths(raw)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
        return tuple(defaults), f"{toml_path}: {exc}"
    return tuple(dict.fromkeys([*defaults, *extra])), None


def _configured_gate_paths(raw: dict[str, Any]) -> list[str]:
    epic = raw.get("epic", {})
    value = epic.get("merge_gate_paths", []) if isinstance(epic, dict) else None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("[epic] merge_gate_paths is not a list of strings")
    cleaned = [v.strip() for v in value]
    if not all(cleaned):
        raise ValueError("[epic] merge_gate_paths has a blank entry")
    return cleaned


def read_inputs(repo: Path, base: str, candidate: str, cache: FactCache) -> Inputs:
    """Both trees, the diff between them, and the candidate's committed
    merge gate paths, out of git objects only."""
    cand = snapshot(repo, candidate, cache)
    gate_paths, config_error = _gate_paths(
        cand, f"{cache.project_dir()}kew.toml", cache
    )
    return Inputs(
        base=snapshot(repo, base, cache),
        candidate=cand,
        changed=_changed_paths(repo, base, candidate),
        base_tree=_tree_id(repo, base),
        candidate_tree=_tree_id(repo, candidate),
        gate_paths=gate_paths,
        config_error=config_error,
    )


def resolve_base(repo: Path, ref: str, base: str = "main") -> str:
    """What *ref* is compared against: its merge base with ``origin/<base>``
    (else ``<base>``), or its first parent when *ref* is already merged there,
    so a merged commit replays its own diff."""
    sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    for name in (f"origin/{base}", base):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        except SelectionError:
            continue
        merge_base = _git(repo, "merge-base", name, sha).strip()
        if merge_base != sha:
            return merge_base
        return _git(repo, "rev-parse", "--verify", f"{sha}^1").strip()
    raise SelectionError(f"neither origin/{base} nor {base} resolves")


def gate_pattern() -> re.Pattern[str] | None:
    """The operator's ``GATE_RE``, when the environment sets one."""
    raw = os.environ.get("GATE_RE")
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        raise SelectionError(f"GATE_RE is not a regular expression: {exc}") from exc


def explain(
    repo: Path, ref: str, *, gate_re: re.Pattern[str] | None = None
) -> Selection:
    """The selection *ref* makes against its merge base with main."""
    base = resolve_base(repo, ref)
    inputs = read_inputs(repo, base, ref, FactCache(repo))
    return select(inputs, gate_re=gate_re)
