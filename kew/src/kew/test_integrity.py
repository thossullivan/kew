"""Test-diff integrity report for a ``base...head`` diff (#2483, epic #2477).

Every gate runs the head branch's own tests, so a PR that deletes asserts,
skips a test or edits an expected value to match a regression passes all of
them. This module measures what a diff does to its tests and names the signals
that need a stated reason.

Pure: it reads a unified diff text and the branch's per-commit file lists and
returns a frozen :class:`TestIntegrityReport`. It runs no git. The diff it is
handed must be the merge-base diff (``git diff base...head``), never
``git diff HEAD`` — a working-tree diff is not the change under review.

A line that only moves inside one file (removed and re-added verbatim) is not
evidence of anything, so identical removed/added text in the same file cancels
before any signal is counted.
"""

from __future__ import annotations

import ast
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar

#: What counts as a test file: the paths the test-integrity guard treats as
#: tests. Path-aware globs — ``*`` does not cross ``/``, ``**`` does.
#: The frontend's tests (#3187): vitest files and the browser checks. pytest
#: never collects them, so an edit to one is named by its path.
FRONTEND_TEST_PATH_GLOBS: tuple[str, ...] = (
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/scripts/ui-checks/**",
)
TEST_PATH_GLOBS: tuple[str, ...] = (
    "tests/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    *FRONTEND_TEST_PATH_GLOBS,
)

#: Flag names, one per signal that needs a stated reason.
FLAG_ASSERTS_REMOVED = "asserts_removed"
FLAG_TESTS_REMOVED = "tests_removed"
FLAG_SKIP_ADDED = "skip_added"
FLAG_CHANGED_EXPECTED = "changed_expected"
FLAG_TEST_ONLY_FIX_ROUND = "test_only_fix_round"
FLAG_RAISES_LOOSENED = "raises_loosened"
FLAG_MOCK_ANY_ADDED = "mock_any_added"
FLAG_UNDECLARED_TEST_EDITS = "undeclared_test_edits"

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ASSERT = re.compile(r"^\s*(?:assert\b|self\.assert\w*\()")
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w*)")
_SKIP = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\("
    r"|unittest\.skip\w*\b|@skip(?:If|Unless)?\b"
)
_LITERAL = r"-?\d[\w.+-]*|'[^']*'|\"[^\"]*\"|True|False|None"
_ASSERT_EQ = re.compile(
    rf"^\s*assert\s+(?P<lhs>.+?)\s*==\s*(?P<rhs>{_LITERAL})\s*(?:,.*)?$"
)
_RAISES = re.compile(r"pytest\.raises\(")
_RAISES_BROAD = re.compile(r"pytest\.raises\(\s*(?:Exception|BaseException)\s*[,)]")
_MATCH_KW = re.compile(r"\bmatch\s*=")
_MOCK_ANY = re.compile(r"\b(?:mock\.)?ANY\b")
_IMPORT = re.compile(r"^\s*(?:from|import)\s")

_logger = logging.getLogger(__name__)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path-aware glob (``**/`` optional dirs, ``*`` one segment)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


_TEST_PATH_RES = tuple(_glob_to_regex(g) for g in TEST_PATH_GLOBS)


def is_test_path(path: str) -> bool:
    """True when ``path`` is a test file under :data:`TEST_PATH_GLOBS`."""
    return any(rx.match(path) for rx in _TEST_PATH_RES)


_FRONTEND_TEST_PATH_RES = tuple(_glob_to_regex(g) for g in FRONTEND_TEST_PATH_GLOBS)


def is_frontend_test_path(path: str) -> bool:
    """True when ``path`` is a frontend test under :data:`FRONTEND_TEST_PATH_GLOBS`."""
    return any(rx.match(path) for rx in _FRONTEND_TEST_PATH_RES)


def _is_src_path(path: str) -> bool:
    return path.startswith("src/") or "/src/" in path


def _is_production_path(path: str) -> bool:
    """A ``src/`` path that is not a frontend test (``frontend/src/x.test.ts``).

    Only the frontend globs are excluded: ``**/test_*.py`` also matches a
    production module such as ``src/kew/test_integrity.py``.
    """
    return _is_src_path(path) and not is_frontend_test_path(path)


@dataclass(frozen=True)
class Evidence:
    """One line (or commit) behind a signal.

    ``line`` is the head line number for an added line, the base line number
    for a removed one, and ``None`` for a commit-level signal.
    """

    path: str
    line: int | None
    text: str
    commit: str | None = None


@dataclass(frozen=True)
class DiffLine:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class FileDiff:
    path: str
    removed: tuple[DiffLine, ...]
    added: tuple[DiffLine, ...]


@dataclass(frozen=True)
class TestIntegrityReport:
    """What a diff did to its tests; ``flags`` empty means clean."""

    __test__: ClassVar[bool] = False  # not a pytest test class

    #: Every test-file path the diff touches, in diff order.
    test_files: tuple[str, ...] = ()
    asserts_removed: tuple[Evidence, ...] = ()
    asserts_added: tuple[Evidence, ...] = ()
    tests_removed: tuple[Evidence, ...] = ()
    tests_added: tuple[Evidence, ...] = ()
    skips_added: tuple[Evidence, ...] = ()
    changed_expected: tuple[Evidence, ...] = ()
    test_only_fix_rounds: tuple[Evidence, ...] = ()
    raises_loosened: tuple[Evidence, ...] = ()
    mock_any_added: tuple[Evidence, ...] = ()
    #: Existing test functions the diff edits that the PR body's **Behavior
    #: changes** section does not name — the held-out gate fails the merge on
    #: any of them that fails against the candidate's src (#2830).
    undeclared_test_edits: tuple[Evidence, ...] = ()
    #: The node ids behind ``tests_removed``, read off the base file's syntax
    #: tree with their class ancestry — the ids the held-out gate reports them
    #: under (#2872). Empty when the base was not read; not a flag's evidence.
    removed_test_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = field(default=())

    def evidence_for(self, flag: str) -> tuple[Evidence, ...]:
        """The evidence behind one flag name."""
        return {
            FLAG_ASSERTS_REMOVED: self.asserts_removed,
            FLAG_TESTS_REMOVED: self.tests_removed,
            FLAG_SKIP_ADDED: self.skips_added,
            FLAG_CHANGED_EXPECTED: self.changed_expected,
            FLAG_TEST_ONLY_FIX_ROUND: self.test_only_fix_rounds,
            FLAG_RAISES_LOOSENED: self.raises_loosened,
            FLAG_MOCK_ANY_ADDED: self.mock_any_added,
            FLAG_UNDECLARED_TEST_EDITS: self.undeclared_test_edits,
        }[flag]


# --- diff parsing -----------------------------------------------------------


def _header_path(line: str) -> str | None:
    raw = line[4:].split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


class _Parser:
    def __init__(self) -> None:
        self.files: list[FileDiff] = []
        self.old_path: str | None = None
        self.path: str | None = None
        self.removed: list[DiffLine] = []
        self.added: list[DiffLine] = []
        self.old_no = self.new_no = 0
        self.old_left = self.new_left = 0

    def flush(self) -> None:
        if self.path is not None:
            self.files.append(
                FileDiff(self.path, tuple(self.removed), tuple(self.added))
            )
        self.path = self.old_path = None
        self.removed, self.added = [], []

    def header(self, line: str) -> None:
        if line.startswith("diff --git "):
            self.flush()
        elif line.startswith("--- "):
            self.old_path = _header_path(line)
        elif line.startswith("+++ "):
            self.path = _header_path(line) or self.old_path
        elif m := _HUNK.match(line):
            self.old_no, self.new_no = int(m.group(1)), int(m.group(3))
            self.old_left = int(m.group(2) or "1")
            self.new_left = int(m.group(4) or "1")

    def body(self, line: str) -> None:
        tag, text = line[:1], line[1:]
        path = self.path or ""
        if tag in ("-", " "):
            if tag == "-":
                self.removed.append(DiffLine(path, self.old_no, text))
            self.old_no += 1
            self.old_left -= 1
        if tag in ("+", " "):
            if tag == "+":
                self.added.append(DiffLine(path, self.new_no, text))
            self.new_no += 1
            self.new_left -= 1

    def feed(self, line: str) -> None:
        in_hunk = self.old_left > 0 or self.new_left > 0
        if in_hunk and line[:1] in ("-", "+", " "):
            self.body(line)
        elif in_hunk and line == "":
            self.body(" ")  # a context line whose trailing space was stripped
        elif not line.startswith("\\"):
            self.old_left = self.new_left = 0
            self.header(line)


def parse_diff(diff: str) -> tuple[FileDiff, ...]:
    """Split a unified diff into per-file removed/added lines."""
    parser = _Parser()
    for line in diff.splitlines():
        parser.feed(line)
    parser.flush()
    return tuple(parser.files)


def _cancel_moves(fd: FileDiff) -> FileDiff:
    """Drop removed/added pairs whose stripped text is identical."""
    removed_texts = Counter(d.text.strip() for d in fd.removed)
    added_texts = Counter(d.text.strip() for d in fd.added)
    common = removed_texts & added_texts
    return FileDiff(
        fd.path, _without(fd.removed, Counter(common)), _without(fd.added, common)
    )


def _without(lines: tuple[DiffLine, ...], drop: Counter[str]) -> tuple[DiffLine, ...]:
    kept: list[DiffLine] = []
    for d in lines:
        key = d.text.strip()
        if drop[key] > 0:
            drop[key] -= 1
        else:
            kept.append(d)
    return tuple(kept)


# --- signals ----------------------------------------------------------------


def _ev(d: DiffLine) -> Evidence:
    return Evidence(d.path, d.line, d.text.strip())


def _matching(lines: Iterable[DiffLine], rx: re.Pattern[str]) -> list[Evidence]:
    return [_ev(d) for d in lines if rx.search(d.text)]


def _test_defs(lines: Iterable[DiffLine]) -> dict[str, list[Evidence]]:
    found: dict[str, list[Evidence]] = {}
    for d in lines:
        if m := _TEST_DEF.match(d.text):
            found.setdefault(m.group(1), []).append(_ev(d))
    return found


def _only_in(
    ours: dict[str, list[Evidence]], theirs: dict[str, list[Evidence]]
) -> list[Evidence]:
    return [e for name, evs in ours.items() if name not in theirs for e in evs]


def _tests_removed_added(
    removed: Sequence[DiffLine], added: Sequence[DiffLine]
) -> tuple[list[Evidence], list[Evidence]]:
    gone, new = _test_defs(removed), _test_defs(added)
    return _only_in(gone, new), _only_in(new, gone)


def _changed_expected(fd: FileDiff) -> list[Evidence]:
    before: dict[str, DiffLine] = {}
    for d in fd.removed:
        if m := _ASSERT_EQ.match(d.text):
            before.setdefault(m.group("lhs").strip(), d)
    out: list[Evidence] = []
    for d in fd.added:
        m = _ASSERT_EQ.match(d.text)
        old = before.get(m.group("lhs").strip()) if m else None
        if m and old is not None:
            old_rhs = _ASSERT_EQ.match(old.text)
            if old_rhs and old_rhs.group("rhs") != m.group("rhs"):
                text = f"{old.text.strip()}  ->  {d.text.strip()}"
                out.append(Evidence(d.path, d.line, text))
    return out


def _raises_loosened(fd: FileDiff) -> list[Evidence]:
    def strict(lines: tuple[DiffLine, ...]) -> list[DiffLine]:
        return [d for d in lines if _RAISES.search(d.text) and _MATCH_KW.search(d.text)]

    out: list[Evidence] = []
    lost_match = strict(fd.removed)
    if len(lost_match) > len(strict(fd.added)):
        out.extend(_ev(d) for d in lost_match)
    out.extend(_matching(fd.added, _RAISES_BROAD))
    return out


def _mock_any(fd: FileDiff) -> list[Evidence]:
    return [
        _ev(d)
        for d in fd.added
        if _MOCK_ANY.search(d.text) and not _IMPORT.match(d.text)
    ]


def _test_only_fix_rounds(
    commits: Sequence[tuple[str, Sequence[str]]],
) -> list[Evidence]:
    out: list[Evidence] = []
    for sha, paths in list(commits)[1:]:
        tests = [p for p in paths if is_test_path(p)]
        if tests and not any(_is_production_path(p) for p in paths):
            out.extend(Evidence(p, None, sha, commit=sha) for p in tests)
    return out


def _flags(report: TestIntegrityReport) -> tuple[str, ...]:
    flags: list[str] = []
    if len(report.asserts_removed) > len(report.asserts_added):
        flags.append(FLAG_ASSERTS_REMOVED)
    if report.tests_removed:
        flags.append(FLAG_TESTS_REMOVED)
    named = (
        (FLAG_SKIP_ADDED, report.skips_added),
        (FLAG_CHANGED_EXPECTED, report.changed_expected),
        (FLAG_TEST_ONLY_FIX_ROUND, report.test_only_fix_rounds),
        (FLAG_RAISES_LOOSENED, report.raises_loosened),
        (FLAG_MOCK_ANY_ADDED, report.mock_any_added),
        (FLAG_UNDECLARED_TEST_EDITS, report.undeclared_test_edits),
    )
    flags.extend(name for name, evidence in named if evidence)
    return tuple(flags)


def _per_file(
    files: Sequence[FileDiff], signal: Callable[[FileDiff], list[Evidence]]
) -> tuple[Evidence, ...]:
    return tuple(e for fd in files for e in signal(fd))


def build_report(
    diff: str, commits: Sequence[tuple[str, Sequence[str]]] = ()
) -> TestIntegrityReport:
    """Measure what a ``base...head`` diff does to its tests.

    ``commits`` is the branch's commits oldest first, each as ``(sha, paths)``.
    Every commit after the first is a fix round; one that touches a test file
    and no ``src/`` file is flagged ``test_only_fix_round``.
    """
    files = [_cancel_moves(fd) for fd in parse_diff(diff) if is_test_path(fd.path)]
    removed = [d for fd in files for d in fd.removed]
    added = [d for fd in files for d in fd.added]
    tests_gone, tests_new = _tests_removed_added(removed, added)
    report = TestIntegrityReport(
        test_files=tuple(dict.fromkeys(fd.path for fd in files)),
        asserts_removed=tuple(_matching(removed, _ASSERT)),
        asserts_added=tuple(_matching(added, _ASSERT)),
        tests_removed=tuple(tests_gone),
        tests_added=tuple(tests_new),
        skips_added=tuple(_matching(added, _SKIP)),
        changed_expected=_per_file(files, _changed_expected),
        test_only_fix_rounds=tuple(_test_only_fix_rounds(commits)),
        raises_loosened=_per_file(files, _raises_loosened),
        mock_any_added=_per_file(files, _mock_any),
    )
    return replace(report, flags=_flags(report))


# --- edited existing tests (#2830) ------------------------------------------

_FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


def _test_functions(
    body: Iterable[ast.stmt], prefix: str = ""
) -> Iterable[tuple[str, _FunctionDef]]:
    """Every pytest-collected test function in *body*, by ``Class::name``."""
    for node in body:
        if isinstance(node, _FunctionDef) and node.name.startswith("test"):
            yield prefix + node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            yield from _test_functions(node.body, f"{prefix}{node.name}::")


def _parsed_tests(source: str) -> dict[str, _FunctionDef]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        _logger.warning("test-integrity: unparseable test file, no edit named: %s", exc)
        return {}
    return dict(_test_functions(tree.body))


def edited_tests(base_source: str, head_source: str) -> tuple[tuple[str, int], ...]:
    """The test functions present on both sides whose code changed.

    Each as ``(qualified name, head line of its def)``. Code is compared by its
    syntax tree, decorators included, so a reformat or a comment edit is not an
    edit; a test added or removed is not an *existing* test edited.
    """
    base, head = _parsed_tests(base_source), _parsed_tests(head_source)
    return tuple(
        (name, node.lineno)
        for name, node in head.items()
        if name in base and ast.dump(base[name]) != ast.dump(node)
    )


def removed_test_names(report: TestIntegrityReport, path: str) -> frozenset[str]:
    """The bare names of the test functions *report* flags removed from *path*."""
    return frozenset(
        m.group(1)
        for e in report.tests_removed
        if e.path == path and (m := _TEST_DEF.match(e.text))
    )


def removed_tests(
    base_source: str, head_source: str | None, names: frozenset[str]
) -> tuple[str, ...]:
    """The base test functions named in *names* that *head_source* lacks.

    Each by its qualified name — ``Class::name`` inside a test class — the
    name pytest reports it under. A head of None means the file was deleted.
    """
    head = _parsed_tests(head_source) if head_source is not None else {}
    return tuple(
        name
        for name in _parsed_tests(base_source)
        if name not in head and name.rsplit("::", 1)[-1] in names
    )


def is_declared(node_id: str, declared: frozenset[str]) -> bool:
    """True when *declared* names *node_id* or one of its parametrized cases."""
    return node_id in declared or any(d.startswith(f"{node_id}[") for d in declared)


def with_undeclared_test_edits(
    report: TestIntegrityReport, undeclared: Sequence[Evidence]
) -> TestIntegrityReport:
    """*report* carrying *undeclared* edits, its flags recomputed."""
    out = replace(report, undeclared_test_edits=tuple(undeclared))
    return replace(out, flags=_flags(out))
