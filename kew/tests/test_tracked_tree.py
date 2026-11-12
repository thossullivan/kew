"""#2209 gate — nothing a `.gitignore` in this tree calls generated is tracked.

A `svelte-kit sync` run from the repo root instead of `frontend/` wrote a
`.svelte-kit/` tree at the root, and it was committed: `frontend/.gitignore`
excludes `.svelte-kit/` one directory down, the root `.gitignore` did not, so
`git add` took eight files of generated output — a `nodes/0.js` hardcoding a
path into untracked `node_modules`, an empty route dictionary, and an `app.js`
importing a `root.js` that was never committed.

The class is not SvelteKit's. Every tool in this repo discovers its config by
walking up from the cwd and writes its output beside it, so running any of them
one directory off produces a second output tree in a directory only the inner
project's `.gitignore` names. The pattern that was supposed to catch it exists
and is correct; it is just anchored in the wrong file.

So the oracle restates no list. It reads the directory patterns out of every
tracked `.gitignore` — each one is that project's own statement that the
directory holds output nobody should commit — and requires that no tracked
path sits under such a directory ANYWHERE in the tree. A project declaring
`build/` generated makes a tracked `build/` generated at the root too.

Deliberately fail-closed: a `.gitignore` that starts excluding a directory this
tree really does track fails here until someone decides which side it is on.

The tree is the one ``tests/support/tree_scan.py`` names: this checkout, or
the snapshot ``scripts/make-public-snapshot.sh`` points the scans at.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import PurePosixPath

import pytest
from support.tree_scan import scan_root, tracked_paths

pytestmark = pytest.mark.policy


def _generated_dir_patterns(tracked: list[str]) -> dict[str, list[str]]:
    """Directory-name patterns any tracked `.gitignore` excludes, to its files.

    Only the "anywhere" directory form — a trailing `/` and no interior slash,
    which gitignore(5) matches at any depth. A path-anchored entry
    (`demo-assets/node_modules/`, `kew/src/kew/serve/web/`) says something
    about one location rather than about a directory NAME, and a file entry
    (`.coverage`, `coverage.xml`) names no directory at all.
    """
    root = scan_root()
    patterns: dict[str, list[str]] = {}
    for rel in tracked:
        if PurePosixPath(rel).name != ".gitignore":
            continue
        for raw in (root / rel).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "!")) or not line.endswith("/"):
                continue
            name = line[:-1]
            if "/" in name:
                continue
            patterns.setdefault(name, []).append(rel)
    return patterns


def test_no_tracked_file_lives_in_a_generated_directory() -> None:
    tracked = tracked_paths()
    patterns = _generated_dir_patterns(tracked)
    # The oracle is only as wide as the patterns it found; an empty read would
    # pass everything, including the tree this gate exists for.
    assert ".svelte-kit" in patterns, patterns

    offenders: list[str] = []
    for rel in tracked:
        parents = PurePosixPath(rel).parent.parts
        for component in parents:
            hits = [
                f"{pattern} ({', '.join(patterns[pattern])})"
                for pattern in sorted(patterns)
                if fnmatch.fnmatch(component, pattern)
            ]
            if hits:
                offenders.append(f"{rel} -> {'; '.join(hits)}")
                break
    assert offenders == [], (
        "tracked files sit under a directory a .gitignore in this tree calls "
        "generated output:\n  " + "\n  ".join(offenders)
    )


def test_the_root_gitignore_covers_every_project_output_directory() -> None:
    """A generated directory one project excludes is excluded at the root too.

    This is the half that was missing: the pattern existed in
    `frontend/.gitignore`, so the tree was invisible to everyone working in
    `frontend/` and untracked-clean there, while the identical tree at the root
    showed up as new files to add. `git check-ignore` is asked rather than the
    file re-parsed, so it is git's own answer about the root path.
    """
    tracked = tracked_paths()
    patterns = _generated_dir_patterns(tracked)
    uncovered: list[str] = []
    for name, sources in sorted(patterns.items()):
        if "*" in name or "?" in name or "[" in name:
            continue  # a glob, not a directory a root entry could spell
        result = subprocess.run(
            ["git", "-C", str(scan_root()), "check-ignore", "-q", f"{name}/"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            uncovered.append(f"{name}/ (excluded by {', '.join(sources)})")
    assert uncovered == [], (
        "a .gitignore deeper in the tree calls these directories generated "
        "output, but the repo root does not exclude them — running the same "
        "tool from the root commits its output:\n  " + "\n  ".join(uncovered)
    )
