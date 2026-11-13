"""Whether a head's ``uv.lock`` leaves out a dependency its project declares (#3234).

Never imported by kew. The held-out phase writes this file's text outside the
candidate's checkout and runs it with the interpreter kew found
(``"$held_out_python" -I``), before the frozen runs, with two arguments: the
head's ``pyproject.toml`` and its ``uv.lock``. It prints ``stale`` when the
lock no longer matches what the project declares, and nothing otherwise.

``uv lock --check`` resolves before it compares, so on a host with no network
it cannot say a dependency the head added is missing from the lock — the very
case where a frozen install leaves it out and the base's tests fail to
import. This check reads both files and nothing else: a name the project
declares (``[project]`` dependencies and optional dependencies, or
``[dependency-groups]``) that the lock's entry for the project does not record
among the same kind is stale, and so is a lock with no entry for the project
at all. Names compare
as PEP 503 normalizes them. Anything it cannot read prints nothing: the phase
only adds the lock as a likely cause to a failure it already has.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable

#: A PEP 508 requirement's distribution name, at the start of the string.
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def normalized(name: str) -> str:
    """*name* as PEP 503 normalizes it: lowercase, each run of ``-_.`` one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(requirements: object) -> set[str]:
    """The distribution names a list of PEP 508 strings names."""
    if not isinstance(requirements, list):
        return set()
    names = set()
    for requirement in requirements:
        if not isinstance(requirement, str):
            continue
        match = _NAME.match(requirement.strip())
        if match is not None:
            names.add(normalized(match.group(0)))
    return names


def _tables(value: object) -> list[object]:
    """The values of a TOML table, or none when *value* is not one."""
    return list(value.values()) if isinstance(value, dict) else []


def declared_names(pyproject: dict[str, object]) -> tuple[set[str], set[str]]:
    """The distributions the project declares: those it is built with
    (``[project]`` dependencies, extras included), then its development groups
    (``[dependency-groups]``)."""
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return set(), set()
    built = _requirement_names(project.get("dependencies"))
    for requirements in _tables(project.get("optional-dependencies")):
        built |= _requirement_names(requirements)
    groups: set[str] = set()
    for requirements in _tables(pyproject.get("dependency-groups")):
        groups |= _requirement_names(requirements)
    return built, groups


def _entry_names(entries: Iterable[object]) -> set[str]:
    """The names a lock's lists of ``{ name = ... }`` entries record."""
    names = set()
    for entries_list in entries:
        if not isinstance(entries_list, list):
            continue
        for entry in entries_list:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(normalized(entry["name"]))
    return names


def locked_names(package: dict[str, object]) -> tuple[set[str], set[str]]:
    """The distributions the lock's entry for the project records, in the two
    halves :func:`declared_names` reads: its dependencies and recorded
    ``requires-dist``, extras included, then its development groups and
    recorded ``requires-dev``."""
    metadata = package.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    built = _entry_names(
        [
            package.get("dependencies"),
            metadata.get("requires-dist"),
            *_tables(package.get("optional-dependencies")),
        ]
    )
    groups = _entry_names(
        [
            *_tables(package.get("dev-dependencies")),
            *_tables(metadata.get("requires-dev")),
        ]
    )
    return built, groups


def is_stale(pyproject: dict[str, object], lock: dict[str, object]) -> bool:
    """Whether *lock* leaves out the project, or a dependency it declares."""
    project = pyproject.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str):
        return False  # no project for a lock to match
    packages = lock.get("package")
    root = next(
        (
            package
            for package in (packages if isinstance(packages, list) else [])
            if isinstance(package, dict)
            and normalized(str(package.get("name", ""))) == normalized(name)
        ),
        None,
    )
    if root is None:
        return True
    (built, groups), (locked, locked_groups) = (
        declared_names(pyproject),
        locked_names(root),
    )
    return bool(built - locked or groups - locked_groups)


def main(argv: list[str]) -> int:
    """Print ``stale`` when the lock at ``argv[1]`` is stale for the project
    at ``argv[0]``. Always exits 0: the phase reads only what it prints. An
    interpreter older than 3.11 has no ``tomllib`` and says so on stderr."""
    try:
        import tomllib

        with open(argv[0], "rb") as handle:
            pyproject = tomllib.load(handle)
        with open(argv[1], "rb") as handle:
            lock = tomllib.load(handle)
    # TOMLDecodeError is a ValueError, named without tomllib, which may be absent.
    except (ImportError, OSError, IndexError, ValueError) as exc:
        sys.stderr.write(f"kew lock check: cannot read the project or lock: {exc}\n")
        return 0
    if is_stale(pyproject, lock):
        print("stale")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
