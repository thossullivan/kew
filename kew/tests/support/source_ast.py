"""Read a module's syntax tree, so a rule about it survives a reformat.

A test that matched a function's source text broke on a reindent, a wrapped
call, or a renamed local, and passed on a rule broken in a shape its substring
did not cover (#2491, #2492). These helpers answer the questions those tests
asked — which definition is this, what does it call, which keyword does it
pass, which identifiers and string values does it hold, on which line does a
call statement start — from the parsed tree, so the answer depends on what the
code does and not on how it is laid out.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def module_source(module: ModuleType) -> str:
    """The text of the file *module* was imported from."""
    assert module.__file__ is not None, module.__name__
    return Path(module.__file__).read_text(encoding="utf-8")


@cache
def _parse(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)


def module_tree(module: ModuleType) -> ast.Module:
    """The parsed source file *module* was imported from."""
    path = module.__file__
    assert path and path.endswith(".py"), f"{module.__name__} has no source to parse"
    return _parse(path)


def _walk_qualname(module: ModuleType, qualname: str) -> Definition:
    scope: list[ast.stmt] = module_tree(module).body
    node: Definition | None = None
    for part in qualname.split("."):
        assert part != "<locals>", f"{qualname} is a nested local"
        node = next(
            (
                child
                for child in scope
                if isinstance(child, Definition) and child.name == part
            ),
            None,
        )
        assert node is not None, f"no definition {part!r} in {module.__name__}"
        scope = node.body
    assert node is not None
    return node


def definition(obj: Any, qualname: str | None = None) -> Definition:
    """The ``def`` or ``class`` node *obj* was defined by, found by qualname.

    ``definition(fn)`` finds *fn* by its own ``__qualname__``;
    ``definition(module, "Class.method")`` finds a dotted name in *module*.
    Walks the module tree by qualname segment rather than by line, so a
    decorator, a reformatted signature or a moved neighbour cannot shift it.
    """
    if qualname is not None:
        assert isinstance(obj, ModuleType), f"{obj!r} is not a module"
        return _walk_qualname(obj, qualname)
    target = inspect.unwrap(obj)
    module = inspect.getmodule(target)
    assert module is not None, f"{obj!r} has no defining module"
    return _walk_qualname(module, target.__qualname__)


def dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a name or attribute chain, ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def callee(call: ast.Call) -> str:
    """The called name, without its receiver: ``_c.f(x)`` and ``f(x)`` are ``f``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def calls(node: ast.AST, name: str | None = None) -> list[ast.Call]:
    """Every call under *node* in source order, or every call to *name* when given."""
    found = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and (name is None or callee(child) == name)
    ]
    return sorted(found, key=lambda call: (call.lineno, call.col_offset))


def called(node: ast.AST) -> list[str]:
    """The dotted callee of every call under *node*, in source order."""
    return [name for call in calls(node) if (name := dotted(call.func)) is not None]


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    """The value passed as keyword *name*, or None when it is not passed."""
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def keyword_text(call: ast.Call, name: str) -> str | None:
    """The unparsed value *call* passes as keyword *name*, if it passes one."""
    value = keyword(call, name)
    return None if value is None else ast.unparse(value)


def self_calls(method: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The ``self.<name>(...)`` methods *method* calls directly."""
    return {
        name.removeprefix("self.")
        for name in called(method)
        if name.startswith("self.") and name.count(".") == 1
    }


def reachable(cls: ast.ClassDef, start: str) -> set[str]:
    """The methods of *cls* that *start* can reach through ``self`` calls."""
    methods = {
        node.name: node
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    seen: set[str] = set()
    pending = [start]
    while pending:
        name = pending.pop()
        if name in seen or name not in methods:
            continue
        seen.add(name)
        pending.extend(self_calls(methods[name]))
    return seen


@dataclass(frozen=True)
class CallSite:
    """A call to a given callee, and the branches it sits in."""

    line: int
    """The line the callee is named on — where a line trace sees the call start."""
    guards: tuple[str, ...]
    """The unparsed tests of every ``if`` the call sits in, outermost first."""


def call_sites(function: Any, callee: str) -> list[CallSite]:
    """Every call in *function* to something ending in *callee*.

    *callee* matches the last dotted segments, so ``"_launch_container"`` finds
    ``dispatch_core._launch_container(...)``.
    """
    sites: list[CallSite] = []

    def visit(node: ast.AST, guards: tuple[str, ...]) -> None:
        if isinstance(node, ast.Call):
            name = dotted(node.func) or ""
            if name == callee or name.endswith("." + callee):
                sites.append(CallSite(node.func.end_lineno or node.lineno, guards))
        for field, value in ast.iter_fields(node):
            inner = guards
            if isinstance(node, ast.If) and field == "body":
                inner = (*guards, ast.unparse(node.test))
            elif isinstance(node, ast.If) and field == "orelse":
                inner = (*guards, f"not ({ast.unparse(node.test)})")
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    visit(child, inner)

    visit(definition(function), ())
    return sites


def identifiers(node: ast.AST) -> set[str]:
    """Every name *node* spells in code: variables, attributes, keywords, imports.

    Docstrings and comments are not code, so a name only mentioned in prose is
    not here.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.keyword) and child.arg is not None:
            names.add(child.arg)
        elif isinstance(child, ast.alias):
            names.add(child.asname or child.name.rsplit(".", 1)[-1])
        elif isinstance(child, ast.arg):
            names.add(child.arg)
    return names


def template(node: ast.AST) -> str | None:
    """A string literal's value; an f-string with each field as ``{expr}``.

    The parser has already joined implicitly concatenated pieces, so a message
    rewrapped across lines reads the same.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            "{" + ast.unparse(part.value) + "}"
            if isinstance(part, ast.FormattedValue)
            else str(template(part))
            for part in node.values
        )
    return None


def texts(node: ast.AST) -> list[str]:
    """Every string literal and f-string template under *node*."""
    return [text for child in ast.walk(node) if (text := template(child)) is not None]


def prose(module: ModuleType) -> Iterator[str]:
    """The module's docstrings, string literals and comment blocks, one each,
    with whitespace collapsed — so a sentence rewrapped across lines still
    reads as one sentence. Consecutive comment lines form one block."""
    block: list[str] = []
    readline = io.StringIO(module_source(module)).readline
    for token in tokenize.generate_tokens(readline):
        if token.type == tokenize.COMMENT:
            block.append(token.string.lstrip("#"))
            continue
        if token.type in (tokenize.NL, tokenize.NEWLINE) and block:
            continue
        if block:
            yield " ".join(" ".join(block).split())
            block = []
        if token.type == tokenize.STRING:
            yield " ".join(token.string.split())
    if block:
        yield " ".join(" ".join(block).split())
