"""Test modules a governed remediation round must always run (#2200).

A remediation brief asks for "the tests covering your changes", which leaves
the agent to decide what that means from the finding alone. #2053 edited
`src/kew/sandbox.py` across its fix rounds, never once ran
`tests/test_sandbox.py`, and broke four asserts that only the pre-merge verify
caught. The host takes the judgement call back: it reads the diff and names the
test module carrying each changed module's name. The agent may add to that set,
never remove from it.

A diff names paths from the repository root; a test runner is configured for
the package that owns the tests. The two differ in this very repo, so each
named path carries the directory it is relative to rather than the repo root.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kew import verify_selection

_logger = logging.getLogger(__name__)

_TESTS_DIR = "tests"
_ROOT = PurePosixPath(".")


@dataclass(frozen=True)
class TargetGroup:
    """Test paths the brief names, and the directory they are relative to.

    `uv run pytest` is configured for `kew/` in this repo, so a repo-rooted
    `kew/tests/test_review.py` handed to it there fails collection outright
    (pytest exit 4). The brief names the run directory beside the paths so the
    agent runs the command where the path resolves.
    """

    run_dir: str
    """Checkout-relative directory to run the tests from; `""` is the root."""

    paths: tuple[str, ...]
    """Test paths relative to *run_dir*, sorted and de-duplicated."""


def module_test_targets(
    changed_paths: Iterable[str], *, root: Path
) -> list[TargetGroup]:
    """Return the test modules named after each changed source module.

    Args:
        changed_paths: Diff paths relative to *root*, as git prints them.
        root: The checkout those paths are relative to.

    Returns:
        One group per run directory, sorted by it. Only files that exist are
        named, so a module with no test module of its own adds nothing and the
        brief never asks for a path the agent cannot run.
    """
    found: dict[str, set[str]] = {}
    for raw in changed_paths:
        module = _source_module(raw)
        if module is None:
            continue
        for run_dir, name in _tests_named_for(module, root=root):
            found.setdefault(run_dir, set()).add(name)
    return [
        TargetGroup(
            run_dir=run_dir,
            paths=tuple(sorted(f"{_TESTS_DIR}/{name}" for name in names)),
        )
        for run_dir, names in sorted(found.items())
    ]


def _source_module(raw: str) -> PurePosixPath | None:
    """The changed path as a source module, or None when it is not one."""
    if not raw or raw.startswith("/"):
        return None
    rel = PurePosixPath(raw)
    if rel.suffix != ".py" or ".." in rel.parts:
        return None
    if _TESTS_DIR in rel.parts or rel.name.startswith("test_"):
        return None
    return rel


def _tests_named_for(module: PurePosixPath, *, root: Path) -> Iterator[tuple[str, str]]:
    """(run directory, test filename) pairs for *module*, in every `tests/` above it.

    A diff is rooted at the repository, a `tests/` directory at its package, so
    the two meet somewhere between the changed file and the checkout root —
    `kew/src/kew/sandbox.py` is covered by `kew/tests/test_sandbox.py`, which
    is `tests/test_sandbox.py` run from `kew/`.
    """
    for ancestor in module.parents:
        tests_dir = root / ancestor / _TESTS_DIR
        if not tests_dir.is_dir():
            continue
        run_dir = "" if ancestor == _ROOT else str(ancestor)
        for name in _matching_test_names(tests_dir, module.stem):
            yield run_dir, name


def _matching_test_names(tests_dir: Path, stem: str) -> list[str]:
    """`test_<stem>.py` and `test_<stem>_*.py` files present in *tests_dir*.

    Matched by name rather than by glob: a module stem is a filename, and a
    filename may legitimately contain glob metacharacters.
    """
    exact = f"test_{stem}.py"
    suffixed = f"test_{stem}_"
    names: list[str] = []
    for entry in tests_dir.iterdir():
        name = entry.name
        matches = name == exact or (name.startswith(suffixed) and name.endswith(".py"))
        if matches and entry.is_file():
            names.append(name)
    return names


def remediation_test_targets(
    finding_paths: Iterable[str],
    delta_paths: Iterable[str],
    *,
    root: Path,
    base_branch: str,
) -> list[TargetGroup] | None:
    """The tests a remediation round must run, scoped to what it answers (#3201).

    A round answers its findings, not the whole PR: a 100-module PR with one
    file named in one finding needs that file's tests, and a brief that listed
    all 100 modules' tests sent the agent to run the suite in a sandbox sized
    for a targeted set. The paths are the files the findings name plus the
    remediation's own delta (what the round already changed on top of the
    reviewed head — a base merge's conflicted files, say), expanded through the
    import-graph selector (:mod:`kew.verify_selection`) and floored by the
    name rule :func:`module_test_targets` applies.

    Returns None when no finding names a file in the checkout: the round's
    target is then the PR as a whole, and the caller keeps the whole-diff rule.
    """
    paths = sorted(
        {
            raw
            for raw in (*finding_paths, *delta_paths)
            if raw
            and not raw.startswith("/")
            and ".." not in PurePosixPath(raw).parts
            and (root / raw).is_file()
        }
    )
    if not paths:
        return None
    found: dict[str, set[str]] = {
        group.run_dir: set(group.paths)
        for group in module_test_targets(paths, root=root)
    }
    for run_dir, test_path in _selected_tests(
        paths, root=root, base_branch=base_branch
    ):
        found.setdefault(run_dir, set()).add(test_path)
    return [
        TargetGroup(run_dir=run_dir, paths=tuple(sorted(names)))
        for run_dir, names in sorted(found.items())
    ]


def _selected_tests(
    paths: list[str], *, root: Path, base_branch: str
) -> list[tuple[str, str]]:
    """``(run_dir, test path)`` pairs the import-graph selector picks for *paths*.

    Empty when the selector cannot read this tree (not a ``src/kew`` layout, a
    git error) or answers the full suite: a remediation brief never asks a
    sandboxed round for the whole suite, so the name rule's floor stands alone.
    A tree it cannot read says so in a warning line, so a brief that names only
    the floor is never mistaken for one the import graph scoped.
    """
    try:
        base = verify_selection.resolve_base(root, "HEAD", base_branch)
        inputs = verify_selection.read_inputs(
            root, base, "HEAD", verify_selection.FactCache(root)
        )
        selection = verify_selection.select(
            dataclasses.replace(inputs, changed=tuple(paths))
        )
    except (verify_selection.SelectionError, OSError, ValueError) as exc:
        _logger.warning(
            "review: the import-graph test selector could not read %s (%s) — "
            "the brief names only each changed module's own test module",
            root,
            exc,
        )
        return []
    if selection.full_rule is not None:
        return []
    run_dir = inputs.candidate.prefix.rstrip("/")
    return [(run_dir, test_path) for test_path, _ in selection.selected]
