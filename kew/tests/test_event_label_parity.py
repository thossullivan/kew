"""The console words every event type kew records, and every reason it blocks on.

``frontend/src/lib/factory/names.ts`` (#2541) maps a run event's type to the
words an operator reads, and falls back to the raw code for a type it has not
met. That fallback is right for a type kew has not shipped; it is a gap for one
it has. The first cut of ``EVENT_LABELS`` was transcribed from the fixtures: it
labelled 6 of the 29 types the live ledger held, leaving 78% of run events as
raw codes — including the commonest, ``dispatch_owner_recorded`` — while
labelling names kew never writes at all.

The anchor here is the backend's own source, not a hand-copied list. The event
names are read off the syntax tree of every module under ``src/kew``: each
audit write (``audit.emit``, ``append_audit_event``, ``_chained_insert*``), each
helper that forwards an ``event`` argument to one, and the members of
:class:`~kew.evidence.EvidenceKind`. A write whose name the scan cannot resolve
fails the test rather than being skipped, so a new emit site cannot hide behind
a variable.

The scan does not try to decide which events carry a ``run_id``: a writer may
attach one on some paths and not others, and the run page shows whichever do.
So every recorded type is labelled — a superset of the run events — and every
label must name a type kew records.

``REASON_LABELS`` is anchored the same way to the Flight contract's evidence
dimensions (``DIM_*`` in ``kew/flight/evidence.py``), which is what the run
page's safest action reads as ``blocked by``.

``ERROR_CATEGORY_LABELS`` and ``REFUSAL_REASON_LABELS`` are anchored to every
code literal the source writes into an ``error_category`` or a ``reason``
(:class:`_CodeScan`). Work's merge verify printed ``host_job_deadline_exceeded``
as recorded, and a refusal reason with no label prints as its code (#2953).
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

import kew
from kew.evidence import EvidenceKind
from kew.flight import evidence as flight_evidence
from kew.report_provenance import PUBLISHED_ERROR_CATEGORIES

pytestmark = pytest.mark.policy

_NAMES_TS = "frontend/src/lib/factory/names.ts"
# The console's committed fixture payloads (`npm run dev:fixtures`); its README
# is the file that locates the directory.
_FIXTURES_README = "frontend/fixtures/README.md"
_SRC = Path(kew.__file__).resolve().parent

# `audit.emit(store, event, ...)`: the event is the second positional argument
# or `event=`.
_EMIT = ("audit.py", "emit")
# `store.append_audit_event(event=...)` and `append_audit_event_once`.
_APPEND_PREFIX = "append_audit_event"
# `_chained_insert(values)` and its in-transaction twins: the event is the
# second element of the values tuple.
_INSERT_PREFIX = "_chained_insert"
# The writer itself: functions that hand a caller's row on unchanged, so the
# names they write are read at their callers instead.
_WRITER = frozenset(
    {
        ("run_store_audit.py", "_chained_insert"),
        ("run_store_audit.py", "_chained_insert_once"),
        ("run_store_audit.py", "_chained_insert_in_transaction"),
        # emit()'s own dispatch: its kwargs are the dict emit() built around
        # its own `event` parameter.
        ("audit.py", "_append_with_optional_dedup"),
    }
)

# A blocked-by reason the run page derives for itself rather than reading off a
# Flight dimension: no attempt identity at all (`flight-recorder.ts`).
_FRONTEND_REASONS = frozenset({"identity"})

# A `key: 'words',` or `'dotted.key': 'words',` entry in a names.ts table.
_LABEL_RE = re.compile(
    r"^\s{2}(?:([a-z][a-z0-9_]*)|'([a-z][a-z0-9_.]*)'):\s*'[^'\n]+',?\s*$",
    re.MULTILINE,
)


def _frontend_file(relative: str) -> Path:
    """Locate a frontend source file by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} not present in this checkout")


def _labels(table: str) -> set[str]:
    """The keys of one `export const <table>` object in names.ts."""
    text = _frontend_file(_NAMES_TS).read_text(encoding="utf-8")
    _, sep, after = text.partition(
        f"export const {table}: Readonly<Record<string, string>> = {{"
    )
    assert sep, f"{table} table not found in {_NAMES_TS}"
    body, sep, _ = after.partition("\n};")
    assert sep, f"{table} table is not closed in {_NAMES_TS}"
    lines = [line for line in body.splitlines() if line.strip()]
    keys = [plain or dotted for plain, dotted in _LABEL_RE.findall(body)]
    # Every line is one entry: a line the pattern does not read is a parse gap
    # that would otherwise pass as "not labelled", or worse, as "labelled".
    assert len(keys) == len(lines), f"unparsed lines in {table}: {lines}"
    assert len(keys) == len(set(keys)), f"duplicate keys in {table}"
    return set(keys)


def _module_sets(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Module-level sets of strings: `NAME = frozenset({"a", "b"})`, a set,
    tuple or list literal, or `frozenset`/`set` of one."""
    out: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"frozenset", "set"}
            and len(value.args) == 1
        ):
            value = value.args[0]
        if isinstance(target, ast.Name) and isinstance(
            value, ast.Set | ast.Tuple | ast.List
        ):
            members = [
                elt.value
                for elt in value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            if members and len(members) == len(value.elts):
                out[target.id] = frozenset(members)
    return out


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "text"` / `NAME: Final = "text"` assignments."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            out[target.id] = value.value
    return out


_Fn = ast.FunctionDef | ast.AsyncFunctionDef
# A function, named by the module that defines it.
_Ref = tuple[Path, str]
_Locals = dict[str, list[ast.expr]]


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _event_param(fn: _Fn) -> int | None | bool:
    """Where `fn` takes `event`: its positional index, None if keyword-only,
    False if it has no such parameter. `self`/`cls` are not counted."""
    names = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    if names and names[0] in {"self", "cls"}:
        names = names[1:]
    if "event" in names:
        return names.index("event")
    return None if "event" in (a.arg for a in fn.args.kwonlyargs) else False


def _param_places(fn: _Fn) -> dict[str, int | None]:
    """Each parameter of `fn` and its positional place (None: keyword-only).
    `self`/`cls` are not counted."""
    names = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    if names and names[0] in {"self", "cls"}:
        names = names[1:]
    places: dict[str, int | None] = {a.arg: None for a in fn.args.kwonlyargs}
    places.update({name: i for i, name in enumerate(names)})
    return places


def _param_defaults(fn: _Fn) -> dict[str, ast.expr]:
    """Each parameter of `fn` that has a default, and that default."""
    positional = [*fn.args.posonlyargs, *fn.args.args]
    out = {
        a.arg: d
        for a, d in zip(
            positional[len(positional) - len(fn.args.defaults) :],
            fn.args.defaults,
            strict=True,
        )
    }
    out.update(
        {
            a.arg: d
            for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True)
            if d is not None
        }
    )
    return out


def _locals(fn: _Fn) -> _Locals:
    """Every value each local name is assigned in `fn`'s body."""
    out: _Locals = defaultdict(list)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id].append(node.value)
    return out


def _write_aliases(local: _Locals) -> frozenset[str]:
    """Locals bound to a store's write by name.

    `append = getattr(store, "append_audit_event", None)`, then `append(...)`.
    """
    return frozenset(
        name
        for name, values in local.items()
        for value in values
        if isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "getattr"
        and len(value.args) >= 2
        and isinstance(value.args[1], ast.Constant)
        and str(value.args[1].value).startswith(_APPEND_PREFIX)
    )


def _module_path(dotted: str, root: Path = _SRC) -> Path | None:
    """`kew.a.b` -> its source file under `root` (src/kew), or None outside kew."""
    head, _, rest = dotted.partition(".")
    if head != "kew":
        return None
    base = root.joinpath(*rest.split(".")) if rest else root
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(
    tree: ast.Module, root: Path = _SRC
) -> tuple[dict[str, _Ref], dict[str, Path]]:
    """`from kew.x import f` bindings, and module aliases (`from kew import x`,
    `import kew.x`), anywhere in one module."""
    names: dict[str, _Ref] = {}
    modules: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            source = _module_path(node.module, root)
            for alias in node.names:
                local = alias.asname or alias.name
                sub = _module_path(f"{node.module}.{alias.name}", root)
                if sub is not None:
                    modules[local] = sub
                elif source is not None:
                    names[local] = (source, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                path = _module_path(alias.name, root)
                if path is not None:
                    modules[alias.asname or alias.name] = path
    return names, modules


class _Scan:
    """Every event name the source writes, and every write it cannot read."""

    def __init__(self) -> None:
        self.trees = {
            path: ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(_SRC.rglob("*.py"))
        }
        self.imports = {path: _imports(tree) for path, tree in self.trees.items()}
        self.defs: dict[_Ref, list[_Fn]] = defaultdict(list)
        for path, tree in self.trees.items():
            for fn in ast.walk(tree):
                if isinstance(fn, _Fn):
                    self.defs[(path, fn.name)].append(fn)
        self.by_name: dict[str, set[Path]] = defaultdict(set)
        self.fns: dict[_Ref, _Fn] = {}
        for path, name in self.defs:
            self.by_name[name].add(path)
        constants: dict[str, set[str]] = defaultdict(set)
        for tree in self.trees.values():
            for name, value in _module_constants(tree).items():
                constants[name].add(value)
        # An imported constant is resolved by name across the package — only
        # where that name means one string everywhere.
        self.constants = {k: next(iter(v)) for k, v in constants.items() if len(v) == 1}
        self.emit: _Ref = (_SRC / _EMIT[0], _EMIT[1])
        self.writer = {(_SRC / module, name) for module, name in _WRITER}
        self.helpers: dict[_Ref, int | None] = {}
        self._find_helpers()
        self.events: dict[str, set[str]] = defaultdict(set)
        self.unresolved: list[str] = []
        for (path, _), fns in self.defs.items():
            for fn in fns:
                self._scan_function(path, fn)
        for member in EvidenceKind:
            self.events[member.value].add("kew/evidence.py:EvidenceKind")

    def _target(self, call: ast.Call, path: Path) -> _Ref | None:
        """The kew function a call reaches, where the source says which."""
        names, modules = self.imports[path]
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in names:
                return names[func.id]
            return (path, func.id) if (path, func.id) in self.defs else None
        if not isinstance(func, ast.Attribute):
            return None
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in {"self", "cls"}:
            return (path, func.attr) if (path, func.attr) in self.defs else None
        if isinstance(owner, ast.Name) and owner.id in modules:
            return (modules[owner.id], func.attr)
        homes = self.by_name.get(func.attr, set())
        return (next(iter(homes)), func.attr) if len(homes) == 1 else None

    def _find_helpers(self) -> None:
        """Functions that take an `event` parameter and pass it to a write."""
        candidates = [
            (ref, fn)
            for ref, fns in self.defs.items()
            for fn in fns
            if ref != self.emit and _event_param(fn) is not False
        ]
        # To a fixed point: a helper may forward to another helper.
        while True:
            found = {
                ref: _event_param(fn)
                for ref, fn in candidates
                if ref not in self.helpers and self._forwards_event(ref[0], fn)
            }
            if not found:
                return
            self.helpers.update(found)

    def _forwards_event(self, path: Path, fn: _Fn) -> bool:
        local = _locals(fn)
        return any(
            isinstance(arg, ast.Name) and arg.id == "event" and "event" not in local
            for arg, _ in self._writes(path, fn, local)
        )

    def _writes(
        self, path: Path, fn: _Fn, local: _Locals
    ) -> list[tuple[ast.expr | None, ast.Call]]:
        """Each audit write in `fn`, with the expression it names its event by
        (None where the call names none the scan can see)."""
        aliases = _write_aliases(local)
        out: list[tuple[ast.expr | None, ast.Call]] = []
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            name = _call_name(call)
            if name is None:
                continue
            target = self._target(call, path)
            if target == self.emit or target in self.helpers:
                index = 1 if target == self.emit else self.helpers[target]
                out.append((self._keyword_or_positional(call, index), call))
            elif name.startswith(_APPEND_PREFIX) or name in aliases:
                out.append((self._keyword_or_positional(call, None), call))
            elif name.startswith(_INSERT_PREFIX):
                out.append((self._row_event(call, path, local), call))
        return out

    @staticmethod
    def _keyword_or_positional(call: ast.Call, index: int | None) -> ast.expr | None:
        for kw in call.keywords:
            if kw.arg == "event":
                return kw.value
        return (
            call.args[index] if index is not None and len(call.args) > index else None
        )

    def _row_event(self, call: ast.Call, path: Path, local: _Locals) -> ast.expr | None:
        """The event column of a `_chained_insert*` row: a tuple literal, a
        local bound to one, or what a kew function building it returns."""
        if not call.args:
            return None
        row = call.args[0]
        if isinstance(row, ast.Name) and len(local.get(row.id, [])) == 1:
            row = local[row.id][0]
        if isinstance(row, ast.Call):
            target = self._target(row, path)
            builders = self.defs.get(target, []) if target else []
            returns = [
                node.value
                for fn in builders
                for node in ast.walk(fn)
                if isinstance(node, ast.Return) and node.value is not None
            ]
            if len(returns) == 1:
                row = returns[0]
        if isinstance(row, ast.Tuple) and len(row.elts) > 1:
            return row.elts[1]
        return None

    def _resolve(self, node: ast.expr, local: _Locals) -> set[str] | None:
        """The strings an event expression can be, or None if unreadable."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            body, orelse = (
                self._resolve(node.body, local),
                self._resolve(node.orelse, local),
            )
            return None if body is None or orelse is None else body | orelse
        if isinstance(node, ast.Attribute) and node.attr in EvidenceKind.__members__:
            return {EvidenceKind[node.attr].value}
        if isinstance(node, ast.Name):
            if node.id in local:
                out: set[str] = set()
                for value in local[node.id]:
                    got = self._resolve(value, {})
                    if got is None:
                        return None
                    out |= got
                return out
            if node.id in self.constants:
                return {self.constants[node.id]}
        return None

    def _scan_function(self, path: Path, fn: _Fn) -> None:
        ref = (path, fn.name)
        if ref in self.writer:
            return
        where = path.relative_to(_SRC.parent)
        local = _locals(fn)
        # A helper (or emit itself) handing its own `event` parameter on: its
        # callers carry the name, and they are scanned as calls to it.
        forwards = ref in self.helpers or ref == self.emit
        for arg, call in self._writes(path, fn, local):
            if (
                forwards
                and isinstance(arg, ast.Name)
                and arg.id == "event"
                and "event" not in local
            ):
                continue
            values = None if arg is None else self._resolve(arg, local)
            if values is None:
                shown = "<no event argument>" if arg is None else ast.unparse(arg)
                self.unresolved.append(f"{where}:{call.lineno}: {shown}")
                continue
            for value in values:
                self.events[value].add(f"{where}:{call.lineno}")


@pytest.fixture(scope="module")
def scan() -> _Scan:
    return _Scan()


def test_the_writer_exemptions_still_name_real_functions(scan: _Scan) -> None:
    """A renamed writer would leave its exemption guarding nothing."""
    assert scan.emit in scan.defs
    assert {ref for ref in scan.writer if ref not in scan.defs} == set()


def test_every_audit_write_names_its_event_readably(scan: _Scan) -> None:
    """A write the scan cannot read would ship its event type unlabelled."""
    assert scan.unresolved == []


def test_every_recorded_event_type_has_a_label(scan: _Scan) -> None:
    labelled = _labels("EVENT_LABELS")
    missing = {
        event: sorted(sites)
        for event, sites in scan.events.items()
        if event not in labelled
    }
    assert missing == {}, "add these to EVENT_LABELS in names.ts"


def test_every_event_label_names_a_type_kew_records(scan: _Scan) -> None:
    assert len(scan.events) > 50, "the scan found implausibly few event types"
    assert _labels("EVENT_LABELS") - set(scan.events) == set()


def _fixture_event_names(node: object) -> set[str]:
    """Every string under an ``"event"`` key, at any depth of one payload."""
    if isinstance(node, dict):
        names = {node["event"]} if isinstance(node.get("event"), str) else set()
        for key, value in node.items():
            if key != "event":
                names |= _fixture_event_names(value)
        return names
    if isinstance(node, list):
        return set().union(*map(_fixture_event_names, node))
    return set()


def test_every_fixture_event_names_a_type_kew_records(scan: _Scan) -> None:
    """A fixture event kew never writes draws a raw code live data never shows.

    The run page's fixture once carried ``agent_turn``, ``artifact_sealed`` and
    ``dispatch_started``: the gallery photographed codes no operator meets.
    """
    fixtures = _frontend_file(_FIXTURES_README).parent
    seen: dict[str, set[str]] = defaultdict(set)
    for path in sorted(fixtures.rglob("*.json")):
        for name in _fixture_event_names(json.loads(path.read_text("utf-8"))):
            seen[name].add(path.relative_to(fixtures).as_posix())
    assert len(seen) > 5, "the fixtures carried implausibly few event names"
    unknown = {
        name: sorted(files) for name, files in seen.items() if name not in scan.events
    }
    assert unknown == {}, "fixture events kew never records"


def test_every_flight_dimension_has_a_reason_label() -> None:
    dimensions = {
        value
        for name, value in vars(flight_evidence).items()
        if name.startswith("DIM_") and isinstance(value, str)
    }
    assert len(dimensions) >= 6
    assert _labels("REASON_LABELS") == dimensions | _FRONTEND_REASONS


# An error category or refusal reason kew records is a code: lower snake case.
# A reason is also where free text lands, so only this shape is read as a code.
_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class _CodeScan:
    """Every code literal the source writes into one field (`error_category`,
    `reason`), wherever the source says so: a keyword argument, a positional
    argument a kew function takes by that name, a dict or subscript key, or an
    assignment to a name or attribute so called (a class attribute included).

    A kew function that hands one of its own parameters on to the field
    forwards it: `_record_prelaunch_refusal(ctx, category)` writes
    `error_category=category`, so each call to it writes the field through
    `category`, by keyword or by place, however many hops deep.

    A carrier (`"DispatchOutcome.category"`) is a class attribute the source
    later copies into the field (`error_category=outcome.category`): the
    class's keyword, a `replace(..., category=)` and an assignment to
    `.category` all write the field too.

    A literal reaches the site directly, through a module constant, or through
    a local the function binds to literals, or as the default of a parameter a
    function forwards (`def release(*, reason="claim_released")` writes the
    default whenever a caller leaves it out). A call to a kew function reaches
    the field with whatever that function returns, through chains of returns,
    and a local any name binds by unpacking one's tuple holds its element
    (`exc_category, status = _classify(exc)`). A local checked against a
    module-level set (`if reason not in _REASONS`) holds any of its members,
    and an `or` holds either side. Building a class passes its arguments to its
    `__init__`, so an exception that keeps `self.reason = code` carries the
    code it was built with. A value computed at run time (an exception's own
    category) is not a literal and is read where it is set.
    """

    def __init__(
        self, field: str, root: Path = _SRC, carriers: tuple[str, ...] = ()
    ) -> None:
        self.field = field
        self.root = root
        self.carriers = [tuple(c.split(".")) for c in carriers]
        trees = {
            path: ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))
        }
        self.imports = {path: _imports(tree, root) for path, tree in trees.items()}
        self.consts = {path: _module_constants(tree) for path, tree in trees.items()}
        everywhere: dict[str, set[str]] = defaultdict(set)
        for consts in self.consts.values():
            for name, value in consts.items():
                everywhere[name].add(value)
        self.unique = {k: next(iter(v)) for k, v in everywhere.items() if len(v) == 1}
        self.sets = {path: _module_sets(tree) for path, tree in trees.items()}
        every_set: dict[str, set[frozenset[str]]] = defaultdict(set)
        for sets in self.sets.values():
            for name, members in sets.items():
                every_set[name].add(members)
        self.unique_sets = {
            k: next(iter(v)) for k, v in every_set.items() if len(v) == 1
        }
        self.by_name: dict[str, set[Path]] = defaultdict(set)
        self.fns: dict[_Ref, _Fn] = {}
        for path, tree in trees.items():
            for fn in ast.walk(tree):
                if isinstance(fn, _Fn):
                    self.fns[(path, fn.name)] = fn
                    self.by_name[fn.name].add(path)
        # Building an exception passes its arguments to the class's `__init__`:
        # `Refused(message, code)` writes `reason` where `__init__` assigns
        # `self.reason = reason`, so the class is a function by its own name.
        for path, tree in trees.items():
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef) or (path, cls.name) in self.fns:
                    continue
                for init in cls.body:
                    if isinstance(init, _Fn) and init.name == "__init__":
                        self.fns[(path, cls.name)] = init
                        self.by_name[cls.name].add(path)
        self._bound: dict[int, _Locals] = {}
        # The parameters each kew function hands on to `field`: name -> place
        # (None: keyword-only).
        self.forwards: dict[_Ref, dict[str, int | None]] = defaultdict(dict)
        for ref, fn in self.fns.items():
            if (index := _param_places(fn).get(self.field, False)) is not False:
                self.forwards[ref][self.field] = index
        self._forwarding(trees)
        self.codes: dict[str, set[str]] = defaultdict(set)
        for ref, params in self.forwards.items():
            defaults = _param_defaults(self.fns[ref])
            for param in params.keys() & defaults.keys():
                self._record(ref[0], defaults[param], {})
        for path, tree in trees.items():
            self._scan(path, tree, {})
            for fn in ast.walk(tree):
                if isinstance(fn, _Fn):
                    self._scan(path, fn, self._bindings(fn))

    def _bindings(self, fn: _Fn) -> _Locals:
        """Every value each local name can hold in `fn`: what `_locals` reads,
        plus a name a tuple unpacking binds from a call (`category, reason =
        _reap_category(...)` holds element 0 of what it returns, whatever the
        name), and a name checked against a set (`if reason not in _REASONS`
        holds any of the set's members)."""
        if id(fn) in self._bound:
            return self._bound[id(fn)]
        out = _locals(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                for target in node.targets:
                    if not isinstance(target, ast.Tuple):
                        continue
                    for i, elt in enumerate(target.elts):
                        if isinstance(elt, ast.Name):
                            element = ast.Subscript(
                                value=node.value, slice=ast.Constant(i), ctx=ast.Load()
                            )
                            out[elt.id].append(ast.copy_location(element, node))
            elif (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In | ast.NotIn)
                and isinstance(node.comparators[0], ast.Name)
            ):
                out[node.left.id].append(node.comparators[0])
        self._bound[id(fn)] = out
        return out

    def _forwarding(self, trees: dict[Path, ast.Module]) -> None:
        """Grow `forwards` until no function hands on a parameter it lacks."""
        changed = True
        while changed:
            changed = False
            for (path, name), fn in self.fns.items():
                places = _param_places(fn)
                known = self.forwards[(path, name)]
                for node in ast.walk(fn):
                    for site in self._sites(node, path):
                        if (
                            isinstance(site, ast.Name)
                            and site.id in places
                            and site.id not in known
                        ):
                            known[site.id] = places[site.id]
                            changed = True

    def _callee(self, call: ast.Call, path: Path) -> _Ref | None:
        """The kew function a call names, when the source says which."""
        names, modules = self.imports[path]
        func, ref = call.func, None
        if isinstance(func, ast.Name):
            ref = names.get(func.id, (path, func.id))
        elif isinstance(func, ast.Attribute):
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in modules:
                ref = (modules[owner.id], func.attr)
            elif isinstance(owner, ast.Name) and owner.id in {"self", "cls"}:
                ref = (path, func.attr)
            elif len(homes := self.by_name.get(func.attr, set())) == 1:
                ref = (next(iter(homes)), func.attr)
        return ref

    def _forwarded(self, call: ast.Call, path: Path) -> list[ast.expr]:
        """The arguments a call to a kew function passes to a parameter it
        forwards to `field`, by keyword or by place."""
        ref = self._callee(call, path)
        params = self.forwards.get(ref, {}) if ref is not None else {}
        out = [
            kw.value
            for kw in call.keywords
            if kw.arg in params and kw.arg != self.field
        ]
        for index in params.values():
            if index is not None and len(call.args) > index:
                out.append(call.args[index])
        return out

    def _carried(self, call: ast.Call) -> list[ast.expr]:
        """`DispatchOutcome(category=...)` or `replace(outcome, category=...)`."""
        name = _call_name(call)
        attrs = {attr for owner, attr in self.carriers if name in {owner, "replace"}}
        return [kw.value for kw in call.keywords if kw.arg in attrs]

    def _named(self, target: ast.expr) -> bool:
        if isinstance(target, ast.Name):
            return target.id == self.field
        if isinstance(target, ast.Attribute):
            return target.attr == self.field or any(
                target.attr == attr for _, attr in self.carriers
            )
        if isinstance(target, ast.Subscript):
            key = target.slice
            return isinstance(key, ast.Constant) and key.value == self.field
        return False

    def _sites(self, node: ast.AST, path: Path) -> list[ast.expr]:
        if isinstance(node, ast.Call):
            values = [kw.value for kw in node.keywords if kw.arg == self.field]
            return [*values, *self._forwarded(node, path), *self._carried(node)]
        if isinstance(node, ast.Dict):
            return [
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == self.field
            ]
        if isinstance(node, ast.Assign) and any(map(self._named, node.targets)):
            return [node.value]
        if isinstance(node, ast.AnnAssign) and node.value and self._named(node.target):
            return [node.value]
        return []

    def _resolve(
        self,
        node: ast.expr,
        path: Path,
        local: _Locals,
        seen: frozenset[_Ref] = frozenset(),
    ) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return self._resolve(node.body, path, local, seen) | self._resolve(
                node.orelse, path, local, seen
            )
        if isinstance(node, ast.BoolOp):
            # `exc.reason or "transport_admission_refused"`.
            return set().union(
                *(self._resolve(v, path, local, seen) for v in node.values)
            )
        if isinstance(node, ast.Call):
            return self._returns(node, path, None, seen)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Call)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
        ):
            return self._returns(node.value, path, node.slice.value, seen)
        if not isinstance(node, ast.Name):
            return set()
        if node.id in local:
            return set().union(
                *(self._resolve(v, path, {}, seen) for v in local[node.id])
            )
        own = self.consts[path].get(node.id)
        found = own if own is not None else self.unique.get(node.id)
        if found is not None:
            return {found}
        members = self.sets[path].get(node.id, self.unique_sets.get(node.id))
        return set() if members is None else set(members)

    def _returns(
        self, call: ast.Call, path: Path, index: int | None, seen: frozenset[_Ref]
    ) -> set[str]:
        """What a call to a kew function returns (element `index` of the tuple
        it returns, when one is named), through chains of returns: `return
        _host_restart_category(...), HOST_RESTART_REASON` is followed into the
        function it calls."""
        ref = self._callee(call, path)
        if ref is None or ref not in self.fns or ref in seen:
            return set()
        fn, home, seen = self.fns[ref], ref[0], seen | {ref}
        local = self._bindings(fn)
        out: set[str] = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = node.value
            if index is not None and isinstance(value, ast.Tuple):
                if len(value.elts) <= index:
                    continue
                value = value.elts[index]
            elif index is not None:
                # `return _other(...)`: element `index` of what it returns.
                if isinstance(value, ast.Call):
                    out |= self._returns(value, home, index, seen)
                continue
            out |= self._resolve(value, home, local, seen)
        return out

    def _unpacked(self, node: ast.AST, path: Path) -> tuple[Path, _Fn, int] | None:
        """`layer, error_category = helper(...)`: the kew function a tuple
        unpacking binds `field` from, and the place `field` takes in it."""
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            return None
        for target in node.targets:
            if not isinstance(target, ast.Tuple):
                continue
            index = next(
                (i for i, elt in enumerate(target.elts) if self._named(elt)), None
            )
            ref = self._callee(node.value, path)
            if index is not None and ref is not None and ref in self.fns:
                return ref[0], self.fns[ref], index
        return None

    def _returned(self, fn: _Fn, index: int) -> list[ast.expr]:
        """The element at `index` of every tuple `fn` returns."""
        return [
            node.value.elts[index]
            for node in ast.walk(fn)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Tuple)
            and len(node.value.elts) > index
        ]

    def _record(self, path: Path, site: ast.expr, local: _Locals) -> None:
        where = path.relative_to(self.root.parent).as_posix()
        for value in self._resolve(site, path, local):
            if _CODE_RE.match(value):
                self.codes[value].add(f"{where}:{site.lineno}")

    def _scan(self, path: Path, scope: ast.AST, local: _Locals) -> None:
        for node in ast.walk(scope):
            for site in self._sites(node, path):
                self._record(path, site, local)
            if (unpacked := self._unpacked(node, path)) is not None:
                home, fn, index = unpacked
                for site in self._returned(fn, index):
                    self._record(home, site, self._bindings(fn))


def _unlabelled(scan: _CodeScan, table: str) -> dict[str, list[str]]:
    labelled = _labels(table)
    return {
        code: sorted(sites)
        for code, sites in sorted(scan.codes.items())
        if code not in labelled
    }


# A dispatch's outcome carries its category to the run row and the `--json`
# summary: `error_category=outcome.category`.
_CATEGORY_CARRIERS = ("DispatchOutcome.category",)


@pytest.fixture(scope="module")
def categories() -> _CodeScan:
    scan = _CodeScan("error_category", carriers=_CATEGORY_CARRIERS)
    # The categories kew publishes by name (`kew report provenance`) include
    # ones the source computes rather than spells at a write site.
    for code in PUBLISHED_ERROR_CATEGORIES:
        scan.codes[code].add("kew/report_provenance.py:PUBLISHED_ERROR_CATEGORIES")
    return scan


# A Switchyard probe's reason is a verdict on the endpoint, not a refusal kew
# records: a refused probe reaches only an exception's message and `kew health`.
_PROBE_VERDICT_HOME = "kew/switchyard.py"


@pytest.fixture(scope="module")
def reasons() -> _CodeScan:
    scan = _CodeScan("reason")
    probe = [
        code
        for code, sites in scan.codes.items()
        if all(site.startswith(f"{_PROBE_VERDICT_HOME}:") for site in sites)
    ]
    assert probe, f"no probe verdict is written in {_PROBE_VERDICT_HOME} any more"
    for code in probe:
        del scan.codes[code]
    return scan


def test_every_recorded_error_category_has_a_label(categories: _CodeScan) -> None:
    """Merge verify printed ``host_job_deadline_exceeded`` as recorded: the
    console words a category only from ERROR_CATEGORY_LABELS."""
    assert len(categories.codes) > 30, "the scan found implausibly few categories"
    assert {"host_job_deadline_exceeded", "host_interrupted"} <= set(categories.codes)
    assert _unlabelled(categories, "ERROR_CATEGORY_LABELS") == {}, (
        "add these to ERROR_CATEGORY_LABELS in names.ts"
    )


def test_every_recorded_refusal_reason_has_a_label(reasons: _CodeScan) -> None:
    """A reason REFUSAL_REASON_LABELS has not met prints as its code (#2953)."""
    assert len(reasons.codes) > 50, "the scan found implausibly few reasons"
    assert {"issued_key_evidence_missing", "credential_scan_missing"} <= set(
        reasons.codes
    )
    assert _unlabelled(reasons, "REFUSAL_REASON_LABELS") == {}, (
        "add these to REFUSAL_REASON_LABELS in names.ts"
    )


_UNLABELLED_SOURCE = """
from kew import refusals
from kew.refusals import refuse

PARKED = "made_up_constant_category"


class Lost(Exception):
    error_category = "made_up_class_category"


def record(store, result, kind):
    category = "made_up_local_category" if kind else PARKED
    store.finish(error_category=category)
    result.error_category = "made_up_attribute_category"
    refuse("made_up_positional_reason")
    refusals.refuse("made_up_module_reason")
    store.emit({"reason": "made_up_payload_reason", "detail": "free text here"})
    store.emit(reason="Not a code, a sentence")
    layer, error_category = _refused(kind)
    store.finish(error_category=error_category)


def _refused(kind):
    if kind:
        return "stream", "made_up_unpacked_category"
    return None, None


def _record_row(store, *, error_category):
    store.finish(error_category=error_category)


def release(store, *, reason="made_up_default_reason"):
    store.emit(reason=reason)


def _park(store, category="made_up_default_parameter_category"):
    _record_row(store, error_category=category)


def _record_refusal(store, category, *, why=""):
    _record_row(store, error_category=category)
    refuse(why)


def _refuse_early(store, code):
    _record_refusal(store, code)


def refusals_through_parameters(store):
    _record_refusal(store, "made_up_forwarded_category")
    _record_refusal(store, category="made_up_forwarded_keyword_category")
    _refuse_early(store, "made_up_two_hop_category")
    _record_refusal(store, "made_up_forwarded_category", why="made_up_forwarded_reason")


class DispatchOutcome:
    def __init__(self, category=""):
        self.category = category if category else "made_up_default_category"


def outcomes(store, outcome):
    stranded = replace(outcome, category="made_up_replaced_category")
    outcome.category = "made_up_assigned_category"
    store.finish(error_category=DispatchOutcome(category="made_up_built_category").category)
    return stranded


def _classify(exc):
    if exc:
        return "made_up_unpacked_local_category", "refused"
    return _classify_further(exc)


def _classify_further(exc):
    return "made_up_wrapped_tuple_category", "failure"


def _reap_category(run):
    return _host_category(run), "made_up_other_element"


def _host_category(run):
    return "made_up_chained_return_category" if run else _plain_category(run)


def _plain_category(run):
    return "made_up_returned_category"


def unpacked_into_any_name(store, exc, run):
    exc_category, status = _classify(exc)
    store.finish(error_category=exc_category)
    found, why = _reap_category(run)
    store.finish(error_category=found)


_ADAPTIVE_REASONS = frozenset(
    {"made_up_set_member_reason", "made_up_other_member_reason"}
)


class Refused(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.reason = code


def admission_reason(config):
    return config.reason.strip()


def admit(config):
    reason = admission_reason(config)
    if reason not in _ADAPTIVE_REASONS:
        reason = "made_up_guard_fallback_reason"
    raise Refused(f"refused: {reason}", reason=reason)


def carried_by_an_exception(store, exc):
    if exc:
        raise Refused("made_up_message_not_a_reason", "made_up_exception_reason")
    store.emit(reason=exc.reason or "made_up_or_fallback_reason")
"""


def test_an_unlabelled_code_fails_the_parity_check(tmp_path: Path) -> None:
    """Negative control: each way the source writes a code is read, so a new
    code shipped through any of them, unlabelled, fails the checks above."""
    package = tmp_path / "kew"
    package.mkdir()
    (package / "refusals.py").write_text(
        "def refuse(reason, *, detail=''):\n    raise ValueError(reason)\n",
        encoding="utf-8",
    )
    (package / "writer.py").write_text(_UNLABELLED_SOURCE, encoding="utf-8")
    direct = {
        "made_up_constant_category",
        "made_up_class_category",
        "made_up_local_category",
        "made_up_attribute_category",
        "made_up_unpacked_category",
        "made_up_forwarded_category",
        "made_up_forwarded_keyword_category",
        "made_up_two_hop_category",
        "made_up_default_parameter_category",
        # A local any name binds by unpacking a kew function's tuple, through
        # a `return _other(...)` and a chain of returns into another function.
        "made_up_unpacked_local_category",
        "made_up_wrapped_tuple_category",
        "made_up_chained_return_category",
        "made_up_returned_category",
    }
    carried = {
        "made_up_default_category",
        "made_up_replaced_category",
        "made_up_assigned_category",
        "made_up_built_category",
    }
    assert (
        set(_unlabelled(_CodeScan("error_category", package), "ERROR_CATEGORY_LABELS"))
        == direct
    )
    assert (
        set(
            _unlabelled(
                _CodeScan("error_category", package, _CATEGORY_CARRIERS),
                "ERROR_CATEGORY_LABELS",
            )
        )
        == direct | carried
    )
    assert set(_unlabelled(_CodeScan("reason", package), "REFUSAL_REASON_LABELS")) == {
        "made_up_positional_reason",
        "made_up_module_reason",
        "made_up_payload_reason",
        "made_up_forwarded_reason",
        "made_up_default_reason",
        # A local checked against a module-level set holds any of its members.
        "made_up_set_member_reason",
        "made_up_other_member_reason",
        "made_up_guard_fallback_reason",
        # Carried into `reason` by an exception's own attribute: built by
        # place, and read back with an `or` fallback.
        "made_up_exception_reason",
        "made_up_or_fallback_reason",
    }
