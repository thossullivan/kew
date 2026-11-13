"""Which base tests the held-out phase runs (#2816).

The held-out phase runs base's tests against the candidate's ``src``. A test
file the candidate did not touch is byte-identical on both sides, and the main
phase already ran it against that same ``src``, so running it again proves
nothing. Only a test file the candidate modified, renamed or deleted can hide a
regression from the main phase, so only those files' base versions are run.

That holds while everything the test files share is unchanged too. A change to
anything else under the test tree — a ``conftest.py``, ``tests/support/``, a
fixture or data file — or to the pytest configuration changes how every test
runs, so the full base tree is run instead, as before.

A test file is shared too when another test module imports from it: the files
that import it run against its base version only if the full tree runs. Any
base module under the test tree that names a selected file's module, other than
the selected files themselves, makes the selection the full tree — a word
match, so it errs towards running more, never less.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

#: The three answers: run the whole base tree, run the selected files, or run
#: nothing because no test file changed.
FULL = "full"
SELECTED = "selected"
NONE = "none"

#: Why a held-out phase with nothing selected ran nothing.
NO_TEST_FILE_CHANGED = "no test file changed"

#: Files beside the test tree that configure how every test in it runs.
_PROJECT_CONFIG = ("conftest.py", "pytest.ini")


@dataclass(frozen=True)
class HeldOutSelection:
    """The base test files the held-out phase runs, by repository path."""

    #: :data:`FULL`, :data:`SELECTED` or :data:`NONE`.
    mode: str = FULL

    #: The base paths run in :data:`SELECTED` mode; empty otherwise.
    files: tuple[str, ...] = ()


def _is_test_file(path: str, tests: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(f"{tests}/")
        and name.startswith("test_")
        and name.endswith(".py")
    )


def _changes(name_status: str) -> Iterable[tuple[str, list[str]]]:
    """``(status letter, paths)`` per line of ``git diff --name-status -z``."""
    fields = name_status.split("\0")
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index][0]
        count = 2 if status in "RC" else 1
        yield status, fields[index + 1 : index + 1 + count]
        index += 1 + count


def _pytest_config(text: str | None) -> object:
    """The ``[tool.pytest]`` table of a ``pyproject.toml``, or the text itself
    when it does not parse — so an unreadable file always reads as changed."""
    if text is None:
        return None
    try:
        return tomllib.loads(text).get("tool", {}).get("pytest")
    except tomllib.TOMLDecodeError:
        return text


def _shares(paths: list[str], *, project: str, tests: str) -> bool:
    """Whether a change to *paths* touches something every test shares: the
    project's pytest files, or anything under the test tree but a test file."""
    shared = {f"{project}/{name}" for name in _PROJECT_CONFIG}
    return any(
        path in shared
        or (path.startswith(f"{tests}/") and not _is_test_file(path, tests))
        for path in paths
    )


def select_held_out(
    name_status: str,
    *,
    project: str,
    tests: str,
    read_base: Callable[[str], str | None],
    read_head: Callable[[str], str | None],
    base_mentions: Callable[[tuple[str, ...]], Iterable[str] | None],
) -> HeldOutSelection:
    """The held-out selection for one base-to-candidate diff.

    *name_status* is ``git diff --name-status -z -M`` between base and
    candidate; *read_base* and *read_head* read a file at either side, ``None``
    when it is absent there, for the pytest configuration comparison.
    *base_mentions* names the base modules under *tests* whose text names any
    of the given module names as a word, or ``None`` when it cannot tell.
    """
    pyproject = f"{project}/pyproject.toml"
    if _pytest_config(read_base(pyproject)) != _pytest_config(read_head(pyproject)):
        return HeldOutSelection(mode=FULL)
    selected: dict[str, None] = {}
    for status, paths in _changes(name_status):
        if _shares(paths, project=project, tests=tests):
            return HeldOutSelection(mode=FULL)
        if status not in "AC" and _is_test_file(paths[0], tests):
            # The base side of the change: the old path of a rename.
            selected[paths[0]] = None
    if not selected:
        return HeldOutSelection(mode=NONE)
    if _imported_elsewhere(tuple(selected), base_mentions):
        return HeldOutSelection(mode=FULL)
    return HeldOutSelection(mode=SELECTED, files=tuple(selected))


def _imported_elsewhere(
    selected: tuple[str, ...],
    base_mentions: Callable[[tuple[str, ...]], Iterable[str] | None],
) -> bool:
    """Whether a base module beside the *selected* files may import one."""
    modules = tuple(dict.fromkeys(PurePosixPath(path).stem for path in selected))
    mentions = base_mentions(modules)
    if mentions is None:
        return True
    return any(path not in selected for path in mentions)
