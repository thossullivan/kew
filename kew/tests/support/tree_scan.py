"""The tree a whole-tree text scan reads, and what it reads of each file.

The scans that read every tracked file's name and text share these helpers.
By default the tree is this checkout. ``KEW_TREE_SCAN_ROOT`` points them at
another git checkout instead: ``scripts/make-public-snapshot.sh`` sets it to
run this checkout's scans against the snapshot it exported, and
:func:`scan_root` is the one place it is read. The script also sets
``KEW_TREE_SCAN_EXPECT_FILES`` to the number of files the archive wrote, read
in :func:`expected_file_count`.

Under the override the scans must read a real snapshot, completely: the root
must be a git checkout of its own (``.git`` a directory, not a ``gitdir:``
file pointing at another repository), must not be this checkout, and must hold
no path this checkout's ``.gitattributes`` marks ``export-ignore``; and every
tracked path must be on disk. Each of those fails rather than skips: a scan
that quietly read no tree, part of one, or the wrong one would pass a snapshot
it never saw.

Known limit: a stray override that names a real snapshot makes an ordinary run
scan that snapshot instead of this checkout. ``tests/conftest.py`` prints the
scanned root in the report header whenever the override is set, and nothing
sets the variable except the snapshot script.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

ENV = "KEW_TREE_SCAN_ROOT"
EXPECT_ENV = "KEW_TREE_SCAN_EXPECT_FILES"
CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


def override_active() -> bool:
    return bool(os.environ.get(ENV, ""))


def override_header() -> str | None:
    """The report-header line ``tests/conftest.py`` prints under the override."""
    if not override_active():
        return None
    return (
        f"{ENV}={os.environ[ENV]}: the whole-tree scans read that tree, "
        "not this checkout"
    )


def _checkout_export_ignored() -> list[str]:
    """The paths this checkout's ``.gitattributes`` marks ``export-ignore``."""
    attributes = CHECKOUT_ROOT / ".gitattributes"
    if not attributes.is_file():
        return []
    paths = []
    for raw in attributes.read_text(encoding="utf-8").splitlines():
        fields = raw.split()
        if fields and not fields[0].startswith("#") and "export-ignore" in fields[1:]:
            paths.append(fields[0])
    return paths


def scan_root() -> Path:
    """The checkout the scans read: this one, or the snapshot ``ENV`` names."""
    override = os.environ.get(ENV, "")
    if not override:
        return CHECKOUT_ROOT
    root = Path(override).resolve()
    if root == CHECKOUT_ROOT:
        raise AssertionError(f"{ENV}={override} is this checkout, not a snapshot")
    if not (root / ".git").is_dir():
        raise AssertionError(
            f"{ENV}={override} is not a git checkout of its own: .git must be a "
            "directory (a gitdir: file reads another repository's index)"
        )
    held = [path for path in _checkout_export_ignored() if os.path.lexists(root / path)]
    if held:
        raise AssertionError(
            f"{ENV}={override} still holds export-ignored paths, so it is not a "
            f"snapshot: {held}"
        )
    return root


def expected_file_count() -> int | None:
    """How many tracked paths the scanned snapshot must hold, when the
    snapshot script says (``EXPECT_ENV``); None otherwise."""
    value = os.environ.get(EXPECT_ENV, "")
    if not value:
        return None
    if not value.isdigit():
        raise AssertionError(f"{EXPECT_ENV}={value!r} is not a file count")
    return int(value)


def tracked_paths() -> list[str]:
    """Every path the scanned checkout tracks, repo-root-relative."""
    root = scan_root()
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if override_active():
            raise AssertionError(f"git ls-files failed in {root}: {result.stderr}")
        pytest.skip("not a git checkout; nothing to scan")  # pragma: no cover
    return sorted(path for path in result.stdout.split("\0") if path)


def scan_subjects(text: str) -> list[str]:
    """The text, and the text with its escaped line breaks and tabs made real.

    A JSON or source string stores a line break as a backslash and a letter.
    The letter is a word character, so a name wrapped across that break would
    pass a pattern if only the raw text were read.
    """
    unescaped = re.sub(r"\\[nrt]", "\n", text)
    return [text] if unescaped == text else [text, unescaped]


def path_text(rel: str) -> str | None:
    """What ships of one tracked path: a symlink's link text (the link, not
    its target, is what ``git archive`` writes; a dangling link is read too),
    a text file's contents, or None for a file that is not text.

    A tracked path that is neither on disk: under the override, an
    AssertionError naming it (the snapshot is incomplete). In this checkout,
    None: an unstaged deletion on a developer's machine must not fail the
    policy run.
    """
    path = scan_root() / rel
    if path.is_symlink():
        return os.readlink(path)
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None  # an image or a font has no prose to scan
    if override_active():
        raise AssertionError(f"{rel} is tracked but not on disk in {scan_root()}")
    return None


def tree_scan_subjects(exempt: set[str]) -> dict[str, list[str]]:
    """What a tree scan reads of each tracked path: its name and what ships of
    it (:func:`path_text`), read through :func:`scan_subjects`."""
    scanned: dict[str, list[str]] = {}
    for rel in tracked_paths():
        if rel in exempt:
            continue
        subjects = [rel]
        text = path_text(rel)
        if text is not None:
            subjects.extend(scan_subjects(text))
        scanned[rel] = subjects
    return scanned


def exported_paths() -> list[str]:
    """The tracked paths ``git archive`` keeps: every one ``.gitattributes``
    does not mark ``export-ignore``, on itself or on a directory above it
    (``git archive`` drops a marked directory whole)."""
    root = scan_root()
    tracked = tracked_paths()
    parents = {str(parent) for rel in tracked for parent in PurePosixPath(rel).parents}
    candidates = sorted(set(tracked) | (parents - {"."}))
    result = subprocess.run(
        ["git", "-C", str(root), "check-attr", "--stdin", "-z", "export-ignore"],
        input="\0".join(candidates) + "\0",
        capture_output=True,
        text=True,
        check=True,
    )
    fields = result.stdout.split("\0")
    ignored = {
        fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "set"
    }
    return [
        rel
        for rel in tracked
        if rel not in ignored
        and not any(str(parent) in ignored for parent in PurePosixPath(rel).parents)
    ]
