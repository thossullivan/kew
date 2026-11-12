"""The selection sizing report and its two what-if graphs (#3189).

``kew verify --selection-report`` replays merged commits through
:mod:`kew.verify_selection` three ways: today's graph, and two refactors the
reliability epic may make, modelled from the same ``ast`` facts.

- **thin kew.cli** (F1): ``kew/cli/__init__.py`` assembles nothing, and each
  top-level name it defines lives where only its users import it. A file that
  takes ``X`` from ``kew.cli`` then reaches only what ``X``'s definition uses.
  A test that drives the root ``app`` reaches the root callback and the
  commands its strings name — every command when its strings name none.
  A file that uses ``kew.cli`` as a whole (``import kew.cli``, a bare
  ``from kew import cli`` passed around) keeps today's edges.
- **core cycle cut** (F2): the largest import cycle keeps only its module-level
  edges, as if its function-local imports had been hoisted out of it.

Neither changes what a verify runs; both only measure.
"""

from __future__ import annotations

import ast
import hashlib
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kew import verify_selection as vs

#: The package whose init F1 thins.
THIN = "kew.cli"
_ROOT_APP = "app"
_WHOLE = f"{THIN}:*"
_REGISTERING = frozenset({"command", "callback", "result_callback"})
_WORD = re.compile(r"[a-z][a-z0-9-]*")
_PATCHERS = frozenset({"setattr", "object"})
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


# ── kew/cli/__init__.py as a table of names ────────────────────────────────


@dataclass
class _Table:
    """``kew/cli/__init__.py``, read as a table of its top-level names."""

    #: name -> the top-level names its definition uses.
    refs: dict[str, set[str]] = field(default_factory=dict)
    #: name -> the kew imports its definition makes, function-local ones too.
    imports: dict[str, set[str]] = field(default_factory=dict)
    #: name -> a digest of its definition.
    digests: dict[str, str] = field(default_factory=dict)
    #: The statements that bind no name: ``app.add_typer(...)`` and the like.
    assembly: list[str] = field(default_factory=list)
    #: root command -> the names that implement it.
    commands: dict[str, set[str]] = field(default_factory=dict)
    callbacks: set[str] = field(default_factory=set)
    #: local sub-app -> the names registered onto it.
    registered: dict[str, set[str]] = field(default_factory=dict)
    #: name -> the object's qualified name, ``kew.cli._apps.epic_app``.
    qualified: dict[str, str] = field(default_factory=dict)
    #: The names that hold a Typer app: registered onto, or mounted.
    apps: set[str] = field(default_factory=set)

    def merge(self, other: _Table) -> None:
        for mine, theirs in (
            (self.refs, other.refs),
            (self.imports, other.imports),
            (self.commands, other.commands),
            (self.registered, other.registered),
        ):
            for name, values in theirs.items():
                mine.setdefault(name, set()).update(values)
        self.callbacks |= other.callbacks
        self.apps |= other.apps
        self.qualified = {**other.qualified, **self.qualified}


def _flatten(body: Iterable[ast.stmt]) -> Iterator[ast.stmt]:
    """Top-level statements, with ``if``/``try`` bodies opened up."""
    for stmt in body:
        if isinstance(stmt, ast.If):
            yield from _flatten([*stmt.body, *stmt.orelse])
        elif isinstance(stmt, ast.Try):
            handlers = [s for h in stmt.handlers for s in h.body]
            yield from _flatten([*stmt.body, *handlers, *stmt.orelse, *stmt.finalbody])
        else:
            yield stmt


def _bound(stmt: ast.stmt, package: str) -> dict[str, tuple[str, list[str]]]:
    """``local name -> (qualified name, kew imports)`` for what *stmt* binds."""
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {stmt.name: (f"{package}.{stmt.name}", [])}
    if isinstance(stmt, ast.Import):
        return {
            a.asname or a.name.split(".")[0]: (a.name, [a.name]) for a in stmt.names
        }
    if isinstance(stmt, ast.ImportFrom):
        return _from_bound(stmt, package)
    return {name: (f"{package}.{name}", []) for name in _assigned(stmt)}


def _from_bound(stmt: ast.ImportFrom, package: str) -> dict[str, tuple[str, list[str]]]:
    base = vs._absolute(stmt.module, stmt.level, package) or ""
    return {
        a.asname or a.name: (f"{base}.{a.name}", [base, f"{base}.{a.name}"])
        for a in stmt.names
    }


def _assigned(stmt: ast.AST) -> list[str]:
    """The names an assignment statement binds."""
    targets: list[ast.AST] = []
    if isinstance(stmt, ast.Assign):
        targets = [n for t in stmt.targets for n in ast.walk(t)]
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    return [n.id for n in targets if isinstance(n, ast.Name)]


def _used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _local_imports(node: ast.AST, package: str) -> set[str]:
    return {
        name
        for inner in ast.walk(node)
        if inner is not node
        for name in vs._imported_names(inner, package)
    }


def _table(source: bytes) -> _Table:
    table = _Table()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return table
    for stmt in _flatten(tree.body):
        bound = _bound(stmt, THIN)
        if not bound:
            if not _is_docstring(stmt):
                table.assembly.append(ast.dump(stmt))
            _read_registration(stmt, table)
            continue
        dump = hashlib.sha256(ast.dump(stmt).encode()).hexdigest()
        used = _used(stmt)
        local = _local_imports(stmt, THIN)
        for name, (qualified, imports) in bound.items():
            table.refs.setdefault(name, set()).update(used - {name})
            table.imports.setdefault(name, set()).update([*imports, *local])
            table.digests[name] = table.digests.get(name, "") + dump
            table.qualified[name] = qualified
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            _read_decorators(stmt, table)
    return table


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and vs._string(stmt.value) is not None


def _command_name(call: ast.Call, default: str) -> str:
    named = [k.value for k in call.keywords if k.arg == "name"]
    for node in [*call.args[:1], *named]:
        value = vs._string(node)
        if value is not None:
            return value
    return default.lower().replace("_", "-")


def _register(table: _Table, app: str, kind: str, name: str, names: set[str]) -> None:
    table.apps.add(app)
    if app != _ROOT_APP:
        table.registered.setdefault(app, set()).update(names)
    elif kind == "callback":
        table.callbacks.update(names)
    else:
        table.commands.setdefault(name, set()).update(names)


def _read_decorators(fn: ast.FunctionDef | ast.AsyncFunctionDef, table: _Table) -> None:
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        app = dec.func.value
        if isinstance(app, ast.Name) and dec.func.attr in _REGISTERING:
            kind = "callback" if "callback" in dec.func.attr else "command"
            _register(table, app.id, kind, _command_name(dec, fn.name), {fn.name})


def _read_registration(stmt: ast.stmt, table: _Table) -> None:
    """``app.add_typer(sub, name="x")`` and ``app.command("x")(fn)``."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return
    call = stmt.value
    if not call.args:
        return
    method = _app_method(call.func)
    if method is not None and method[1] == "add_typer":
        name = _command_name(call, _first_name(call.args[0]))
        table.apps.update(_used(call.args[0]))
        _register(table, method[0], "command", name, _used(call.args[0]))
        return
    inner = call.func
    method = _app_method(inner.func) if isinstance(inner, ast.Call) else None
    if isinstance(inner, ast.Call) and method is not None and method[1] == "command":
        default = _first_name(call.args[0]).rpartition(".")[2]
        name = _command_name(inner, default)
        _register(table, method[0], "command", name, _used(call.args[0]))


def _app_method(func: ast.AST) -> tuple[str, str] | None:
    """``app.add_typer`` -> ``("app", "add_typer")``."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None


def _first_name(node: ast.AST) -> str:
    return vs._dotted(node) or "?"


# ── how one file uses kew.cli ───────────────────────────────────────────────


@dataclass(frozen=True)
class _Use:
    """How one file uses ``kew.cli``: by name, as a whole, and what it adds."""

    symbols: frozenset[str] = frozenset()
    whole: bool = False
    words: frozenset[str] = frozenset()
    #: ``(qualified app, what is added to it)``: ``""`` for a command or
    #: callback, a qualified sub-app for ``add_typer``.
    registers: frozenset[tuple[str, str]] = frozenset()


def _docstring(node: ast.AST) -> ast.AST | None:
    """The docstring constant of a module, class or function, if any."""
    if not isinstance(node, (ast.Module, ast.ClassDef, *_DEFS)) or not node.body:
        return None
    first = node.body[0]
    if isinstance(first, ast.Expr) and vs._string(first.value) is not None:
        return first.value
    return None


def _is_kew_cli(node: ast.AST) -> bool:
    """``kew.cli`` spelled as an attribute of a bound ``kew``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "cli"
        and isinstance(node.value, ast.Name)
        and node.value.id == "kew"
    )


@dataclass
class _UseReader:
    """Reads one file's :class:`_Use` in one walk; *module* is its own
    qualified name."""

    module: str
    package: str
    aliases: set[str] = field(default_factory=set)
    via_kew: bool = False
    symbols: set[str] = field(default_factory=set)
    whole: bool = False
    qualified: dict[str, str] = field(default_factory=dict)
    #: Each spelling of the ``kew.cli`` module object: every use of it, and
    #: the uses that name what they take from it.
    uses: Counter[str] = field(default_factory=Counter)
    named: Counter[str] = field(default_factory=Counter)
    words: set[str] = field(default_factory=set)
    registers: set[tuple[str, str]] = field(default_factory=set)

    def read(self, tree: ast.Module) -> _Use:
        for stmt in vs._statements(tree.body, functions=True):
            self._bindings(stmt)
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            doc = _docstring(node)
            if doc is not None:
                docstrings.add(id(doc))
            self._visit(node, docstrings)
        whole = self.whole or any(n > self.named[k] for k, n in self.uses.items())
        return _Use(
            symbols=frozenset(self.symbols),
            whole=whole,
            words=frozenset(self.words),
            registers=frozenset(self.registers),
        )

    def _visit(self, node: ast.AST, docstrings: set[int]) -> None:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in self.aliases:
                self.uses[node.id] += 1
        elif isinstance(node, ast.Attribute):
            self._attribute(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._string_use(node.value, docstring=id(node) in docstrings)
        elif isinstance(node, ast.Call):
            self.registers.update(self._registers(node))
            self._patched(node)

    def _module_spelling(self, node: ast.AST) -> str | None:
        """Which spelling of the ``kew.cli`` module object *node* is."""
        if isinstance(node, ast.Name) and node.id in self.aliases:
            return node.id
        if self.via_kew and _is_kew_cli(node):
            return THIN
        return None

    def _attribute(self, node: ast.Attribute) -> None:
        if self.via_kew and _is_kew_cli(node):
            self.uses[THIN] += 1
        spelling = self._module_spelling(node.value)
        if spelling is not None:
            self.symbols.add(node.attr)
            self.named[spelling] += 1

    def _patched(self, call: ast.Call) -> None:
        """``monkeypatch.setattr(cli, "x", ...)`` / ``patch.object(cli, "x")``."""
        if len(call.args) < 2:
            return
        if vs._dotted(call.func).rpartition(".")[2] not in _PATCHERS:
            return
        spelling = self._module_spelling(call.args[0])
        name = vs._string(call.args[1])
        if spelling is not None and name is not None:
            self.symbols.add(name)
            self.named[spelling] += 1

    def _bindings(self, stmt: ast.AST) -> None:
        if isinstance(stmt, ast.Import):
            self._import(stmt)
        elif isinstance(stmt, ast.ImportFrom):
            self._import_from(stmt)
        for name in _assigned(stmt):
            self.qualified.setdefault(name, f"{self.module}.{name}")

    def _import(self, stmt: ast.Import) -> None:
        for a in stmt.names:
            if a.name == THIN and a.asname:
                self.aliases.add(a.asname)
            elif a.name == THIN or a.name.startswith(f"{THIN}."):
                self.via_kew = True

    def _import_from(self, stmt: ast.ImportFrom) -> None:
        base = vs._absolute(stmt.module, stmt.level, self.package) or ""
        for a in stmt.names:
            local = a.asname or a.name
            self.qualified[local] = f"{base}.{a.name}"
            if f"{base}.{a.name}" == THIN:
                self.aliases.add(local)
            elif base == THIN:
                self.symbols.add(a.name)

    def _string_use(self, value: str, *, docstring: bool) -> None:
        if value == THIN:
            self.whole = True
        elif value.startswith(f"{THIN}.") and vs._KEW_MODULE.match(value):
            self.symbols.add(value.split(".")[2])
        if not docstring:
            self.words.update(w for w in value.split() if _WORD.fullmatch(w))

    def _registers(self, call: ast.Call) -> Iterator[tuple[str, str]]:
        func = call.func
        if isinstance(func, ast.Call):
            func = func.func
        if not isinstance(func, ast.Attribute):
            return
        target = self._qualify(func.value)
        if target is None:
            return
        if func.attr in _REGISTERING:
            yield target, ""
        elif func.attr == "add_typer" and call.args:
            added = self._qualify(call.args[0])
            yield target, added or ""

    def _qualify(self, node: ast.AST) -> str | None:
        dotted = vs._dotted(node)
        if not dotted:
            return None
        head, _, rest = dotted.partition(".")
        if head in self.aliases:
            return f"{THIN}.{rest}" if rest else THIN
        base = self.qualified.get(head)
        if base is None:
            return None
        return f"{base}.{rest}" if rest else base


def _read_use(source: bytes, module: str, package: str) -> _Use:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _Use(whole=True)
    return _UseReader(module=module, package=package).read(tree)


# ── the F1 what-if ──────────────────────────────────────────────────────────


@dataclass
class _Model:
    """One what-if run's view of ``kew.cli``: its table and its users."""

    view: vs.GraphView
    table: _Table
    modules: dict[str, str]
    rev: vs.Graph
    uses: dict[str, _Use] = field(default_factory=dict)
    #: virtual node -> the table names it covers, for change detection.
    covers: dict[str, set[str]] = field(default_factory=dict)
    #: virtual node -> its edges.
    nodes: dict[str, set[str]] = field(default_factory=dict)


class ThinCli:
    """What-if (F1): a thin ``kew.cli``, read from both trees' facts."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._tables: dict[str, _Table] = {}
        self._uses: dict[tuple[str, str, str], _Use] = {}

    def __call__(self, view: vs.GraphView) -> tuple[vs.Graph, set[str]]:
        if THIN not in view.graph:
            return view.graph, set(view.changed)
        snaps = (view.inputs.base, view.inputs.candidate)
        table = _Table()
        for snap in snaps:
            table.merge(self._table(snap))
        modules = {m: p for s in snaps for m, p in s.module_paths.items()}
        model = _Model(
            view=view, table=table, modules=modules, rev=vs.reverse(view.graph)
        )
        graph = {node: set(targets) for node, targets in view.graph.items()}
        graph[THIN] = set()
        users = sorted(n for n in model.rev.get(THIN, ()) if n != THIN)
        self._read(model, [*users, *self._registrar_candidates(model)])
        for node in users:
            graph[node].update(self._targets(model, node))
        graph.update(model.nodes)
        return graph, self._changed(model)

    def _registrar_candidates(self, model: _Model) -> list[str]:
        """Every module that could register a command onto one of the apps."""
        table = model.table
        homes = {table.qualified.get(a, THIN).rpartition(".")[0] for a in table.apps}
        found = {m for h in homes for m in {h, *model.rev.get(h, ())}}
        return sorted(m for m in found if vs._is_module(m))

    # -- reading ---------------------------------------------------------

    def _blobs(self, oids: Iterable[str]) -> dict[str, bytes]:
        return vs.cat_blobs(self.repo, sorted(set(oids)))

    def _table(self, snap: vs.Snapshot) -> _Table:
        path = snap.module_paths.get(THIN)
        if path is None:
            return _Table()
        oid = snap.files[path]
        if oid not in self._tables:
            self._tables[oid] = _table(self._blobs([oid])[oid])
        return self._tables[oid]

    def _keys(self, model: _Model, node: str) -> list[tuple[str, str, str]]:
        """``(blob, node, package)`` for *node* in each tree that holds it."""
        keys = []
        for snap in (model.view.inputs.base, model.view.inputs.candidate):
            path = snap.module_paths.get(node, node)
            oid = snap.files.get(path)
            if oid is not None:
                keys.append((oid, node, vs._package_of(node, path, snap.prefix)))
        return keys

    def _read(self, model: _Model, nodes: Iterable[str]) -> None:
        """Read the uses of *nodes* not read yet, in one ``git cat-file``."""
        keys = [k for n in nodes for k in self._keys(model, n) if k not in self._uses]
        blobs = self._blobs(k[0] for k in keys)
        for oid, node, package in keys:
            self._uses[(oid, node, package)] = _read_use(blobs[oid], node, package)

    def _use(self, model: _Model, node: str) -> _Use:
        """How *node* uses ``kew.cli`` in either tree."""
        if node in model.uses:
            return model.uses[node]
        self._read(model, [node])
        found = [self._uses[key] for key in self._keys(model, node)]
        model.uses[node] = _Use(
            symbols=frozenset().union(*(u.symbols for u in found)),
            whole=any(u.whole for u in found),
            words=frozenset().union(*(u.words for u in found)),
            registers=frozenset().union(*(u.registers for u in found)),
        )
        return model.uses[node]

    # -- the rewrite ---------------------------------------------------------

    def _targets(self, model: _Model, node: str) -> set[str]:
        """What *node* reaches in place of the whole ``kew.cli``."""
        use = self._use(model, node)
        targets = {THIN}
        for symbol in sorted(use.symbols):
            targets.update(self._symbol_targets(model, node, symbol, use))
        if use.whole or not use.symbols:
            targets.add(self._whole(model))
        return targets

    def _symbol_targets(
        self, model: _Model, node: str, symbol: str, use: _Use
    ) -> set[str]:
        submodule = f"{THIN}.{symbol}"
        if submodule in model.modules:
            return {submodule}
        if symbol not in model.table.refs:
            return {self._whole(model)}
        if symbol != _ROOT_APP:
            return {self._virtual(model, symbol, {symbol})}
        commands = sorted(model.table.commands)
        if not vs._is_module(node):
            # A test drives the commands its strings name; one naming none
            # (``--help``, argv built elsewhere) drives every one.
            commands = [c for c in commands if c in use.words] or commands
        root = {_ROOT_APP, *model.table.callbacks}
        found = {self._virtual(model, _ROOT_APP, root)}
        for command in commands:
            names = set(model.table.commands[command])
            found.add(self._virtual(model, command, names))
        return found

    def _whole(self, model: _Model) -> str:
        if _WHOLE not in model.nodes:
            model.nodes[_WHOLE] = set(model.view.graph[THIN])
            model.covers[_WHOLE] = set(model.table.refs)
        return _WHOLE

    def _virtual(self, model: _Model, label: str, names: set[str]) -> str:
        node = f"{THIN}:{label}"
        if node not in model.nodes:
            covered, targets = self._entry(model, names)
            model.covers[node] = covered
            model.nodes[node] = (targets | {THIN}) - {node}
        return node

    def _entry(self, model: _Model, names: set[str]) -> tuple[set[str], set[str]]:
        """The table names *names* use, and the module nodes they reach — with
        what registers commands onto a sub-app among *names*."""
        table = model.table
        covered = _closure(table.refs, names)
        pending = deque(sorted(covered & names & table.apps))
        seen: set[str] = set()
        while pending:
            app = pending.popleft()
            if app in seen:
                continue
            seen.add(app)
            local = table.registered.get(app, set())
            covered |= _closure(table.refs, local)
            pending.extend(sorted(local & table.apps))
        apps = {table.qualified.get(app, f"{THIN}.{app}") for app in seen}
        targets = self._registrar_modules(model, apps)
        for name in covered:
            for imported in table.imports.get(name, ()):
                targets.update(vs.resolve_kew(imported, model.modules))
        return covered, targets

    def _registrar_modules(self, model: _Model, apps: Iterable[str]) -> set[str]:
        """The modules registering onto *apps*, or onto a sub-app they add."""
        pending = deque(sorted(apps))
        modules: set[str] = set()
        seen: set[str] = set()
        while pending:
            qualified = pending.popleft()
            if qualified in seen:
                continue
            seen.add(qualified)
            for module, added in self._registrars(model, qualified):
                modules.add(module)
                if added:
                    pending.append(added)
        return modules

    def _registrars(self, model: _Model, qualified: str) -> list[tuple[str, str]]:
        """``(module, what it adds)`` for each module registering onto *qualified*."""
        home = qualified.rpartition(".")[0]
        candidates = {home, *model.rev.get(home, ())}
        found = []
        for module in sorted(m for m in candidates if vs._is_module(m)):
            if module == THIN:
                continue  # the table already holds kew.cli's own registrations
            for target, added in self._use(model, module).registers:
                if target == qualified:
                    found.append((module, added))
        return found

    def _changed(self, model: _Model) -> set[str]:
        """``kew.cli`` changed: under F1, only the names whose definitions
        changed did — unless a statement that defines no name changed too."""
        changed = set(model.view.changed)
        if THIN not in changed:
            return changed
        base = self._table(model.view.inputs.base)
        cand = self._table(model.view.inputs.candidate)
        if base.assembly != cand.assembly or not base.refs or not cand.refs:
            return changed | set(model.nodes)
        names = set(base.digests) | set(cand.digests)
        edited = {n for n in names if base.digests.get(n) != cand.digests.get(n)}
        changed.discard(THIN)
        changed.update(n for n, covers in model.covers.items() if covers & edited)
        return changed


def _closure(refs: Mapping[str, set[str]], names: Iterable[str]) -> set[str]:
    seen = {n for n in names if n in refs}
    stack = list(seen)
    while stack:
        for used in refs.get(stack.pop(), ()):
            if used in refs and used not in seen:
                seen.add(used)
                stack.append(used)
    return seen


# ── the F2 what-if ──────────────────────────────────────────────────────────


def _postorder(
    root: str, nexts: Callable[[str], Iterable[str]], seen: set[str]
) -> list[str]:
    """The nodes first reached from *root*, in DFS postorder; marks *seen*."""
    order: list[str] = []
    seen.add(root)
    stack = [(root, iter(sorted(nexts(root))))]
    while stack:
        node, targets = stack[-1]
        nxt = next((t for t in targets if t not in seen), None)
        if nxt is None:
            stack.pop()
            order.append(node)
        else:
            seen.add(nxt)
            stack.append((nxt, iter(sorted(nexts(nxt)))))
    return order


def largest_cycle(graph: vs.Graph) -> set[str]:
    """The largest strongly connected set of kew modules (Kosaraju)."""
    kew: vs.Graph = {
        node: {t for t in targets if vs._is_module(t)}
        for node, targets in graph.items()
        if vs._is_module(node)
    }
    seen: set[str] = set()
    order: list[str] = []
    for root in sorted(kew):
        if root not in seen:
            order.extend(_postorder(root, lambda n: kew.get(n, ()), seen))
    rev = vs.reverse(kew)
    seen = set()
    best: set[str] = set()
    for root in reversed(order):
        if root not in seen:
            found = set(_postorder(root, lambda n: rev.get(n, ()), seen))
            best = found if len(found) > len(best) else best
    return best


def cut_core_cycle(view: vs.GraphView) -> tuple[vs.Graph, set[str]]:
    """What-if (F2): the largest import cycle is cut by hoisting its lazy
    imports out of it — inside the cycle, only module-level edges remain."""
    core = largest_cycle(view.graph)
    graph = {
        node: {t for t in targets if t in view.eager.get(node, ()) or t not in core}
        if node in core
        else targets
        for node, targets in view.graph.items()
    }
    return graph, set(view.changed)


def what_ifs(repo: Path) -> tuple[tuple[str, vs.Transform | None], ...]:
    """The three graphs the report compares, in the order it prints them."""
    return (
        ("today", None),
        ("thin kew.cli", ThinCli(repo)),
        ("core cycle cut", cut_core_cycle),
    )


# ── the sizing report ───────────────────────────────────────────────────────


def merged_commits(repo: Path, last: int, branch: str = "main") -> list[str]:
    """The last *last* first-parent commits on *branch*: the merged PRs."""
    for name in (f"origin/{branch}", branch):
        try:
            out = vs._git(
                repo, "rev-list", "--first-parent", f"--max-count={last}", name
            )
        except vs.SelectionError:
            continue
        return [sha for sha in out.split() if sha]
    raise vs.SelectionError(f"neither origin/{branch} nor {branch} resolves")


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    index = q * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _spread(label: str, values: Sequence[float]) -> str:
    return (
        f"{label}: median {_quantile(values, 0.5):.1%}, "
        f"p75 {_quantile(values, 0.75):.1%}"
    )


def sizing_report(
    repo: Path,
    commits: Sequence[str],
    *,
    gate_re: re.Pattern[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Median and p75 selected share, by test count, under each graph: over
    every commit, and over the commits no path rule forced to the full suite."""
    cache = vs.FactCache(repo)
    graphs = what_ifs(repo)
    replay = _Replay(names=[name for name, _ in graphs])
    for sha in commits:
        started = clock()
        inputs = vs.read_inputs(repo, f"{sha}^", sha, cache)
        for name, transform in graphs:
            replay.add(name, vs.select(inputs, gate_re=gate_re, transform=transform))
        before = vs.select(inputs, gate_re=gate_re, reads=False)
        replay.before_reads.append(before.share)
        replay.slowest = max(replay.slowest, clock() - started)
    return replay.render(len(commits))


@dataclass
class _Replay:
    """What the sizing report gathers, per graph, as it replays commits."""

    names: list[str]
    shares: dict[str, list[float]] = field(default_factory=dict)
    free: dict[str, list[float]] = field(default_factory=dict)
    tally: dict[str, Counter[str]] = field(default_factory=dict)
    #: Today's shares without the file-read rule (#3211).
    before_reads: list[float] = field(default_factory=list)
    slowest: float = 0.0

    def add(self, name: str, picked: vs.Selection) -> None:
        self.shares.setdefault(name, []).append(picked.share)
        self.tally.setdefault(name, Counter())[picked.full_rule or "selected"] += 1
        if not picked.before_graph:
            self.free.setdefault(name, []).append(picked.share)

    def render(self, commits: int) -> str:
        lines = [f"selection report: {commits} merged commits, by test count"]
        lines.extend(_spread(n, self.shares[n]) for n in self.names if n in self.shares)
        free = len(self.free.get(self.names[0], ()))
        lines.append(f"no path rule fired: {free} of {commits} commits")
        lines.extend(
            _spread(f"{n}, no path rule", self.free[n])
            for n in self.names
            if n in self.free
        )
        for name in self.names:
            counts = self.tally.get(name, Counter())
            lines.extend(f"{name}, {rule}: {n}" for rule, n in sorted(counts.items()))
        if self.before_reads and self.names[0] in self.shares:
            lines.append(_spread("before the file-read rule", self.before_reads))
            lines.append(
                _spread("after the file-read rule", self.shares[self.names[0]])
            )
        lines.append(
            f"slowest replay: {self.slowest:.2f}s for the three graphs "
            "and the selection before the file-read rule"
        )
        return "\n".join(lines) + "\n"
