"""Which browser-check modules a change touches (#3201).

The pre-review gate runs the ui-check modules a head touches on the merge
candidate. Two rules pick them, the same two ``frontend/scripts/ui-check-lib.mjs``
applies to a scoped run: a module the head adds or changes runs, and so does
every module whose ``ui-check-owns:`` lines own a file the head changed. A
module's name says what it checks, not which files render it, so ownership is
read from those lines rather than guessed from names.

The modules are read as text and never imported: they are candidate code, and
this runs on the host. The caller hands in their text; nothing here touches a
checkout's filesystem, where a committed symlink would point outside it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import PurePosixPath

#: Where the surface modules live, relative to the repository root.
UI_CHECKS_DIR = "frontend/scripts/ui-checks"
_FRONTEND = "frontend/"
#: The console's rendered source: a change here is a change to some surface.
_SURFACE_SOURCE = "frontend/src/"
#: A unit test beside the source it tests renders nothing.
_TEST_FILE = re.compile(r"\.(?:test|spec)\.[^/]+$")

#: ``// ui-check-owns: <glob>`` (or the same inside a JSDoc block), a glob
#: relative to ``frontend/``. Mirrors ``OWNS_LINE`` in ``ui-check-lib.mjs``.
OWNS_LINE = re.compile(r"^\s*(?://|\*)\s*ui-check-owns:\s*(\S+)\s*$", re.MULTILINE)


def owned_globs(source: str) -> list[str]:
    """The globs one module's source says it owns."""
    return OWNS_LINE.findall(source)


def glob_matches(glob: str, path: str) -> bool:
    """Whether *path* matches *glob*: ``**`` crosses directories, ``*`` does not."""
    return _glob_pattern(glob).fullmatch(path) is not None


@lru_cache(maxsize=1024)
def _glob_pattern(glob: str) -> re.Pattern[str]:
    """*glob* as a regular expression, built once: a selection matches every
    file of the tree against every ownership line."""
    pattern = ""
    i = 0
    while i < len(glob):
        char = glob[i]
        if glob.startswith("**/", i):
            pattern += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            pattern += ".*"
            i += 2
        else:
            pattern += {"*": "[^/]*", "?": "[^/]"}.get(char, re.escape(char))
            i += 1
    return re.compile(pattern)


#: Module name (without ``.mjs``) to its source text, as read from the head.
ModuleSources = Mapping[str, str]


def touched_modules(
    changed_paths: Iterable[str], *, sources: ModuleSources
) -> list[str]:
    """The module names (without ``.mjs``) a change to *changed_paths* touches.

    *changed_paths* are repository-rooted, as ``git diff --name-only`` prints
    them; *sources* are the head's modules (the pre-review gate reads them out
    of git objects, never through a checkout's filesystem). Sorted and
    de-duplicated; a module the head deleted is not named.
    """
    owners = _ownership(sources)
    picked: set[str] = set()
    for raw in changed_paths:
        picked.update(_modules_for(raw, owners))
    return sorted(picked)


def uncovered_surfaces(
    changed_paths: Iterable[str], *, sources: ModuleSources
) -> list[str]:
    """The changed console source files no ui-check module covers.

    A file under ``frontend/src/`` (a unit test aside) is part of what some
    surface renders. When no module's ``ui-check-owns:`` lines own it, a scoped
    run checks nothing about it, so its passing says nothing about that change.
    Sorted and de-duplicated, repository-rooted.
    """
    owners = _ownership(sources)
    return sorted(
        {
            raw
            for raw in changed_paths
            if raw.startswith(_SURFACE_SOURCE)
            and not _TEST_FILE.search(raw)
            and not _modules_for(raw, owners)
        }
    )


def _ownership(sources: ModuleSources) -> dict[str, list[str]]:
    """Each module by name, with the globs it owns."""
    return {name: owned_globs(source) for name, source in sorted(sources.items())}


def _modules_for(raw: str, owners: dict[str, list[str]]) -> list[str]:
    """The modules one changed path touches: itself, or the modules owning it."""
    path = PurePosixPath(raw)
    if str(path.parent) == UI_CHECKS_DIR and path.suffix == ".mjs":
        return [path.stem] if path.stem in owners else []
    if not raw.startswith(_FRONTEND):
        return []
    rel = raw.removeprefix(_FRONTEND)
    return [
        name
        for name, globs in owners.items()
        if any(glob_matches(glob, rel) for glob in globs)
    ]
