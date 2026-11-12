#!/usr/bin/env python3
"""Decide what one Release run may do, before it builds anything.

A release is cut by a person: they run the Release workflow with an explicit
``release_version``. Nothing derives a version from commit subjects, and a
push to ``main`` builds and checks the packages but never publishes them.

Two subcommands, each appending ``key=value`` lines to ``$GITHUB_OUTPUT``:

``canonical``
    ``canonical=true`` exactly when ``$GITHUB_REPOSITORY`` is the repository
    the site and its install gate name: ``REPO_SLUG`` in
    ``www/src/lib/data/install.js``, the one place the slug is written. A fork
    or the private development repository gets ``canonical=false`` and its
    Release run never reaches a release step.

``resolve``
    With ``$RELEASE_VERSION`` empty: ``released=false`` and the version the
    tree states, so a push builds and smoke-tests that version. With it set,
    it refuses unless the run is on ``refs/heads/main``, the input is a plain
    ``X.Y.Z`` equal to the version in ``kew/pyproject.toml``,
    ``kew/src/kew/__init__.py`` and ``create-kew/package.json``, and
    ``CHANGELOG.md`` has a non-empty section for it. It then writes that
    section to ``--notes`` and sets ``released=true``.

    The tag ``v<version>`` must not exist, here or on ``origin``, with one
    exception: a re-run (``$GITHUB_RUN_ATTEMPT`` above 1) of the run whose
    first attempt created it at this very commit (``$GITHUB_SHA``). Then
    ``tag=existing`` instead of a refusal, so "Re-run failed jobs" can finish
    a release that failed after its tag. The ``tag-release`` job then accepts
    the tag only when its release carries this run's id.
    ``--skip-tag-check`` runs the cheap checks alone, before the suite.

Standard library only: it runs on the hosted runner's own ``python3`` before
any dependency is installed. Exit 1 with one ``::error::`` line on a refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - fixed git argv, no shell
import sys
import tomllib
from pathlib import Path

SLUG_FILE = Path("www/src/lib/data/install.js")
PYPROJECT = Path("kew/pyproject.toml")
INIT = Path("kew/src/kew/__init__.py")
PACKAGE_JSON = Path("create-kew/package.json")
CHANGELOG = Path("CHANGELOG.md")
MAIN_REF = "refs/heads/main"

_SLUG = re.compile(r"^export const REPO_SLUG = '([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)';$")
_INIT_VERSION = re.compile(r'^__version__ = "([^"]+)"$')
# PyPI and npm spell a pre-release differently (1.0.0rc1 against 1.0.0-rc.1),
# so the one string both packages carry is a plain release.
_PLAIN_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class Refusal(Exception):
    """A release this run must not cut, with the reason a person reads."""


def _output(lines: dict[str, str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    text = "".join(f"{key}={value}\n" for key, value in lines.items())
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text)


def _summary(line: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line)


def canonical_slug(root: Path) -> str:
    """REPO_SLUG from the site's install module, or a refusal."""
    for line in (root / SLUG_FILE).read_text(encoding="utf-8").splitlines():
        match = _SLUG.match(line.strip())
        if match:
            return match.group(1)
    raise Refusal(f"{SLUG_FILE} states no REPO_SLUG")


def tree_versions(root: Path) -> dict[str, str]:
    """Every version field the release must agree with, by file."""
    project = tomllib.loads((root / PYPROJECT).read_text(encoding="utf-8"))
    versions = {str(PYPROJECT): str(project["project"]["version"])}
    init = root / INIT
    if init.is_file():
        for line in init.read_text(encoding="utf-8").splitlines():
            match = _INIT_VERSION.match(line)
            if match:
                versions[str(INIT)] = match.group(1)
                break
    package = json.loads((root / PACKAGE_JSON).read_text(encoding="utf-8"))
    versions[str(PACKAGE_JSON)] = str(package["version"])
    return versions


def changelog_section(text: str, version: str) -> str:
    """The body of the ``## v<version>`` (or ``## <version>``) section."""
    heading = re.compile(
        r"^##[ \t]+\[?v?" + re.escape(version) + r"\]?(?=[ \t]|$)", re.MULTILINE
    )
    match = heading.search(text)
    if match is None:
        return ""
    # The rest of the heading line (a date) is not part of the notes.
    line_end = text.find("\n", match.end())
    if line_end == -1:
        return ""
    rest = text[line_end + 1 :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: following.start()] if following else rest
    return body.strip()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed git argv
        ["git", *args],  # noqa: S607 - git from the runner's PATH
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _tag_commits(root: Path, tag: str) -> dict[str, str]:
    """Where the tag points, by where it was found: ``here`` and ``origin``.

    The commit a tag names, peeled through an annotated tag object. An
    unanswered question about ``origin`` is a refusal: it does not cut a
    release.
    """
    found: dict[str, str] = {}
    local = _git(
        root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"
    )
    if local.returncode == 0:
        found["here"] = local.stdout.strip()
    remote = _git(
        root,
        "ls-remote",
        "--exit-code",
        "--tags",
        "origin",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    )
    # 2 is ls-remote's "no matching ref".
    if remote.returncode not in (0, 2):
        raise Refusal(f"could not list tags on origin: {remote.stderr.strip()}")
    refs = dict(
        reversed(line.split("\t", 1))
        for line in remote.stdout.splitlines()
        if "\t" in line
    )
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if commit:
        found["origin"] = commit
    return found


def _check_tag(root: Path, tag: str, sha: str, attempt: int) -> str:
    """``new`` when no tag exists; ``existing`` when a re-run finds the tag
    its own first attempt made at this commit; otherwise a refusal."""
    found = _tag_commits(root, tag)
    if not found:
        return "new"
    elsewhere = {where: commit for where, commit in found.items() if commit != sha}
    if elsewhere:
        where, commit = next(iter(elsewhere.items()))
        place = "in this checkout" if where == "here" else "on origin"
        raise Refusal(f"tag {tag} already exists {place}, at {commit[:12]}")
    if attempt <= 1:
        raise Refusal(
            f"tag {tag} already exists at this commit, and this is a first "
            "attempt: a new run never reuses a tag. Re-run the failed jobs of "
            "the run that made it, or delete the release and its tag first."
        )
    return "existing"


def resolve(
    root: Path,
    requested: str,
    ref: str,
    notes: Path,
    *,
    sha: str = "",
    attempt: int = 1,
    tag_check: bool = True,
) -> dict[str, str]:
    versions = tree_versions(root)
    if not requested:
        return {"released": "false", "version": versions[str(PYPROJECT)]}
    if ref != MAIN_REF:
        raise Refusal(f"a release runs from {MAIN_REF}, not {ref or 'an unknown ref'}")
    if not _PLAIN_VERSION.match(requested):
        raise Refusal(f"release_version {requested!r} is not a plain X.Y.Z version")
    mismatched = {path: found for path, found in versions.items() if found != requested}
    if mismatched:
        listed = ", ".join(f"{path} says {found}" for path, found in mismatched.items())
        raise Refusal(
            f"release_version {requested} does not match the tree: {listed}. "
            "Bump the versions in a pull request first."
        )
    section = changelog_section(
        (root / CHANGELOG).read_text(encoding="utf-8"), requested
    )
    if not section:
        raise Refusal(f"{CHANGELOG} has no non-empty section for {requested}")
    lines = {"released": "true", "version": requested}
    if tag_check:
        lines["tag"] = _check_tag(root, f"v{requested}", sha, attempt)
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(section + "\n", encoding="utf-8")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("canonical")
    resolver = commands.add_parser("resolve")
    resolver.add_argument("--notes", type=Path, required=True)
    resolver.add_argument("--skip-tag-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "canonical":
            slug = canonical_slug(args.root)
            here = os.environ.get("GITHUB_REPOSITORY", "")
            _output({"canonical": "true" if here == slug else "false"})
            if here != slug:
                _summary(
                    f"{here or 'This repository'} is not {slug}: no release step runs."
                )
            return 0
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        lines = resolve(
            args.root,
            os.environ.get("RELEASE_VERSION", "").strip(),
            os.environ.get("GITHUB_REF", ""),
            args.notes,
            sha=os.environ.get("GITHUB_SHA", ""),
            attempt=int(attempt) if attempt.isdigit() else 1,
            tag_check=not args.skip_tag_check,
        )
    except Refusal as refusal:
        print(f"::error::release refused: {refusal}")
        return 1
    _output(lines)
    if lines["released"] == "false":
        _summary(
            f"No release_version given: kew {lines['version']} is built and "
            "checked, and nothing is tagged or published."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
