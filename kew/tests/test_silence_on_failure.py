"""A4 enforcement — no silent clean defaults in ``except`` handlers.

Silence on failure is this project's recurring bug archetype: twelve confirmed
instances (the 2026-08-06 reference-gap audit, exec finding 6, and the
2026-08-07 correction), all the same shape — the system reports a clean
or absent state when it could not determine the state.

The convention (``CONTRIBUTING.md``, "No silent clean defaults") is: **no
except-and-default path may return a clean state without recording why it
defaulted** — a reason on the returned state, a warning line, or an audit fact.
This module is the mechanical half of that convention.

What the scanner flags: an ``except`` handler that returns only clean defaults,
or assigns one for a later return, and records nothing about why. Clean defaults
include ``None``/``False``/``0``/``""``, empty containers, bare ``return``,
zero-argument state constructors, and constants named like ``DEFAULT``.

The scanner does not measure ``contextlib.suppress``, ``except: pass``,
``except: continue``, or ``if err: return <clean>`` without an ``except``.

The house example of the *right* shape is the A2 fail-loud selector,
``kew.cli._select_dispatch_issue``: a failed GitHub query prints "cannot tell if
the queue is empty (this is NOT an empty queue)" and returns a distinct exit
code, instead of the empty queue a clean default would have implied. It is
pinned below so the example cited in the docs cannot rot.

Existing sites are recorded in ``ratchets/silence_on_failure_baseline.txt`` — a
*debt ledger*, not an approval list. The gate is a ratchet: a new site fails, and
a fixed site must be removed from the ledger so the corpus can only shrink.

This gate fails when it cannot run: a missing tree, an unreadable file, or an
unparseable module is an error, never an empty (clean) finding list.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = PACKAGE_ROOT / "src" / "kew"
BASELINE_PATH = PACKAGE_ROOT / "ratchets" / "silence_on_failure_baseline.txt"

# A call whose terminal name contains one of these is treated as recording why
# the handler defaulted (a log/warning line, an audit fact, an emitted event).
_RECORDING_NAME_HINTS = (
    "alert",
    "annotate",
    "audit",
    "console",
    "echo",
    "emit",
    "error",
    "log",
    "note",
    "notify",
    "record",
    "report",
    "warn",
)

_EMPTY_FACTORIES = frozenset({"list", "dict", "set", "tuple", "frozenset"})


class GateCannotRunError(RuntimeError):
    """The scan could not be performed — never reported as "no findings"."""


@dataclass(frozen=True)
class SilentDefault:
    """One `except`-and-default site that records nothing about why."""

    path: str
    qualname: str
    lineno: int
    exception: str

    @property
    def key(self) -> str:
        return f"{self.path}::{self.qualname}"


def _is_clean_default(node: ast.expr | None) -> bool:
    """Is this returned value a bare clean default carrying no reason?"""
    if node is None:  # bare `return` — returns None
        return True
    if isinstance(node, ast.Constant):
        return (
            node.value is None or node.value is False or node.value in (0, 0.0, "", b"")
        )
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, (ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Tuple):
        return not node.elts or all(_is_clean_default(elt) for elt in node.elts)
    if isinstance(node, ast.Name):
        return node.id.isupper() and "DEFAULT" in node.id.strip("_").split("_")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        is_clean_constructor = (
            node.func.id in _EMPTY_FACTORIES or node.func.id[:1].isupper()
        )
        return is_clean_constructor and not node.args and not node.keywords
    return False


def _body_nodes(body: list[ast.stmt]) -> list[ast.AST]:
    """Walk a handler body without descending into nested scopes.

    A nested `def` gets visited on its own, with its own qualname, so its
    statements must not be attributed to the enclosing handler.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _records_why(nodes: list[ast.AST]) -> bool:
    """Does the handler say anything about why it defaulted?"""
    for node in nodes:
        if isinstance(node, ast.Raise):
            return True
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            if name == "write" and "stderr" in ast.unparse(func.value):
                return True
        else:
            continue
        lowered = name.lower()
        if any(hint in lowered for hint in _RECORDING_NAME_HINTS):
            return True
    return False


def _exception_label(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare except"
    return ast.unparse(handler.type)


class _SilentDefaultVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[SilentDefault] = []
        self._scope: list[str] = []

    def _scoped(self, name: str, node: ast.AST) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node.name, node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node.name, node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        body = _body_nodes(node.body)
        returns = [n for n in body if isinstance(n, ast.Return)]
        raises = [n for n in body if isinstance(n, ast.Raise)]
        assignments = [
            n
            for n in body
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            and n.value is not None
            and _is_clean_default(n.value)
        ]
        returns_clean = bool(returns) and all(
            _is_clean_default(returned.value) for returned in returns
        )
        assigns_clean_for_later = not returns and not raises and bool(assignments)
        if (returns_clean or assigns_clean_for_later) and not _records_why(body):
            self.findings.append(
                SilentDefault(
                    path=self.path,
                    qualname="::".join(self._scope) or "<module>",
                    lineno=node.lineno,
                    exception=_exception_label(node),
                )
            )
        self.generic_visit(node)


def scan_source(source: str, *, path: str) -> list[SilentDefault]:
    """Find silent clean defaults in one module's source."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # a module we cannot read is not a clean module
        raise GateCannotRunError(f"cannot parse {path}: {exc}") from exc
    visitor = _SilentDefaultVisitor(path)
    visitor.visit(tree)
    return visitor.findings


def scan_tree(root: Path, *, relative_to: Path) -> list[SilentDefault]:
    """Scan every module under ``root``; refuse to report a clean empty scan."""
    if not root.is_dir():
        raise GateCannotRunError(f"source tree not found: {root}")
    paths = sorted(root.rglob("*.py"))
    if not paths:
        raise GateCannotRunError(f"no Python modules found under {root}")
    findings: list[SilentDefault] = []
    for path in paths:
        rel = path.relative_to(relative_to).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GateCannotRunError(f"cannot read {rel}: {exc}") from exc
        findings.extend(scan_source(source, path=rel))
    return findings


def _counts_by_key(findings: list[SilentDefault]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.key] = counts.get(finding.key, 0) + 1
    return counts


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, int]:
    """Read the debt ledger: ``<file>::<qualname> <count>`` per line."""
    if not path.is_file():
        raise GateCannotRunError(f"baseline ledger missing: {path}")
    baseline: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, count = line.rpartition(" ")
        if not key or not count.isdigit():
            raise GateCannotRunError(
                f"{path.name}:{lineno}: expected '<file>::<qualname> <count>', "
                f"got {raw!r}"
            )
        if key in baseline:
            raise GateCannotRunError(f"{path.name}:{lineno}: duplicate entry {key}")
        baseline[key] = int(count)
    return baseline


# --- the ratchet -------------------------------------------------------------


@pytest.mark.policy
def test_silence_on_failure_no_new_silent_clean_defaults() -> None:
    """New `except`-and-default sites fail; fixed ones must leave the ledger."""
    counts = _counts_by_key(scan_tree(SOURCE_ROOT, relative_to=PACKAGE_ROOT))
    baseline = load_baseline()

    added = sorted(k for k, n in counts.items() if n > baseline.get(k, 0))
    removed = sorted(k for k, n in baseline.items() if n > counts.get(k, 0))

    problems: list[str] = []
    if added:
        problems.append(
            "New silent clean default(s) — an `except` handler returning or "
            "assigning None/False/0/''/empty without recording why.\n"
            "Record the reason (a reason on the returned state, a warning line, "
            "or an audit fact) — see CONTRIBUTING.md, 'No silent clean "
            "defaults'. If the default is genuinely right and says so, add the "
            "site to ratchets/silence_on_failure_baseline.txt with a review "
            "note:\n  " + "\n  ".join(f"{k} {counts[k]}" for k in added)
        )
    if removed:
        problems.append(
            "Ledger entries no longer found — if you fixed or deleted these, "
            "update ratchets/silence_on_failure_baseline.txt to the new count "
            "(drop the line at 0) so the corpus stays honest:\n  "
            + "\n  ".join(f"{k} {counts.get(k, 0)}" for k in removed)
        )
    assert not problems, "\n\n".join(problems)


@pytest.mark.policy
def test_silence_baseline_ledger_is_sorted_and_free_of_duplicates() -> None:
    """The ledger stays diffable — sorted, unique, positive counts.

    In the policy set (#2174): the ledger is a repo-wide artifact no changed
    file selects, so before the mark carried it a remediation that edited the
    ledger learned it was unsorted only on the merge verify.
    """
    baseline = load_baseline()
    assert list(baseline) == sorted(baseline), (
        "ratchets/silence_on_failure_baseline.txt must stay sorted"
    )
    assert all(count > 0 for count in baseline.values()), (
        "a zero-count ledger entry means the site is gone — delete the line"
    )


# --- the house example: A2's fail-loud selector ------------------------------


@pytest.mark.policy
def test_fail_loud_selector_is_the_house_example() -> None:
    """`_select_dispatch_issue` warns and returns a distinct code, not a zero.

    Cited by CONTRIBUTING.md as the shape every except-and-default path
    should have. Pinned so the citation cannot rot.

    In the policy set (#2174) for the same reason as the ratchet above: it reads
    the real checkout, and a remediation that renames or rewrites the cited
    selector does not select this module from its changed files.
    """
    source = (SOURCE_ROOT / "cli" / "__init__.py").read_text(encoding="utf-8")
    findings = scan_source(source, path="src/kew/cli/__init__.py")
    assert not [f for f in findings if f.qualname == "_select_dispatch_issue"], (
        "the cited house example must stay fail-loud"
    )

    selector = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_select_dispatch_issue"
    )
    handler = next(
        node for node in ast.walk(selector) if isinstance(node, ast.ExceptHandler)
    )
    assert _exception_label(handler) == "GhQueryError"
    body = _body_nodes(handler.body)
    assert _records_why(body), "the query failure must be reported, not swallowed"
    returned = [n for n in body if isinstance(n, ast.Return)]
    assert returned and not any(_is_clean_default(r.value) for r in returned), (
        "a failed queue read must not return the value an empty queue would"
    )


# --- the detector itself -----------------------------------------------------


@pytest.mark.parametrize(
    "returned",
    ["None", "", "False", "0", '""', "[]", "{}", "()", "set()", "dict()"],
)
def test_silence_gate_flags_every_clean_default_shape(returned: str) -> None:
    source = f"""
def count_open():
    try:
        return remote_count()
    except Exception:
        return {returned}
"""
    findings = scan_source(source, path="probe.py")
    assert [f.qualname for f in findings] == ["count_open"]
    assert findings[0].exception == "Exception"


@pytest.mark.parametrize(
    "assignment",
    ["all_open = []", "all_open: list[str] = []"],
    ids=["assign", "annotated-assign"],
)
def test_silence_gate_flags_assign_then_return(assignment: str) -> None:
    source = f"""
def list_open():
    try:
        all_open = remote_items()
    except Exception:
        {assignment}
    return all_open
"""
    findings = scan_source(source, path="probe.py")
    assert [finding.qualname for finding in findings] == ["list_open"]


@pytest.mark.parametrize(
    "returned",
    ["None, None", "Status()", "DEFAULT"],
    ids=["clean-tuple", "state-constructor", "default-constant"],
)
def test_silence_gate_flags_extended_clean_default_shapes(returned: str) -> None:
    source = f"""
def load():
    try:
        return remote_value()
    except Exception:
        return {returned}
"""
    findings = scan_source(source, path="probe.py")
    assert [finding.qualname for finding in findings] == ["load"]


def test_silence_gate_does_not_treat_print_as_recording_why() -> None:
    source = """
def load():
    try:
        return remote_value()
    except Exception as exc:
        print(exc)
        return None
"""
    findings = scan_source(source, path="probe.py")
    assert [finding.qualname for finding in findings] == ["load"]


@pytest.mark.parametrize(
    "handler",
    [
        # a warning line
        'log.warning("gh query failed: %s", exc)\n        return 0',
        # an audit fact
        "append_audit_event(store, event)\n        return 0",
        # a re-raise
        "raise QueueUnknownError(exc) from exc",
        # a reason carried on the returned state
        "return QueueCount(value=None, reason=str(exc))",
        # a distinct non-clean sentinel (the A2 selector's shape)
        "return GH_QUERY_FAILURE_EXIT_CODE",
    ],
)
def test_silence_gate_accepts_a_handler_that_records_why(handler: str) -> None:
    source = f"""
def count_open():
    try:
        return remote_count()
    except Exception as exc:
        {handler}
"""
    assert scan_source(source, path="probe.py") == []


def test_silence_gate_reports_a_nested_helper_under_its_own_qualname() -> None:
    """The A2 site's shape: a closure inside a command function."""
    source = """
class Status:
    def render(self):
        def _count(label):
            try:
                return int(run(label).stdout)
            except ValueError:
                return 0
        return _count
"""
    findings = scan_source(source, path="probe.py")
    assert [f.qualname for f in findings] == ["Status::render::_count"]


def test_silence_gate_ignores_a_handler_that_returns_a_real_value() -> None:
    source = """
def load():
    try:
        return read()
    except OSError:
        return fallback_from_cache()
"""
    assert scan_source(source, path="probe.py") == []


# --- a gate that cannot run fails, rather than passing -----------------------


def test_silence_gate_fails_when_the_source_tree_is_missing(tmp_path: Path) -> None:
    with pytest.raises(GateCannotRunError, match="source tree not found"):
        scan_tree(tmp_path / "absent", relative_to=tmp_path)


def test_silence_gate_fails_when_the_tree_holds_no_modules(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(GateCannotRunError, match="no Python modules"):
        scan_tree(tmp_path / "src", relative_to=tmp_path)


def test_silence_gate_fails_when_a_module_cannot_be_parsed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "broken.py").write_text("def f(:\n", encoding="utf-8")
    with pytest.raises(GateCannotRunError, match="cannot parse"):
        scan_tree(tmp_path / "src", relative_to=tmp_path)


def test_silence_gate_fails_when_a_module_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "non_utf8.py").write_bytes(b"\xff")
    with pytest.raises(GateCannotRunError, match=r"cannot read .*non_utf8\.py"):
        scan_tree(tmp_path / "src", relative_to=tmp_path)


def test_silence_gate_fails_when_the_baseline_ledger_is_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(GateCannotRunError, match="baseline ledger missing"):
        load_baseline(tmp_path / "absent.txt")


def test_silence_gate_fails_on_a_malformed_baseline_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "baseline.txt"
    ledger.write_text("src/kew/thing.py::helper\n", encoding="utf-8")
    with pytest.raises(GateCannotRunError, match="expected"):
        load_baseline(ledger)


def test_silence_gate_fails_on_a_duplicated_baseline_entry(tmp_path: Path) -> None:
    ledger = tmp_path / "baseline.txt"
    ledger.write_text(
        "src/kew/thing.py::helper 1\nsrc/kew/thing.py::helper 1\n", encoding="utf-8"
    )
    with pytest.raises(GateCannotRunError, match="duplicate entry"):
        load_baseline(ledger)
