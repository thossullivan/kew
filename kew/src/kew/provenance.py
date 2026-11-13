"""Runtime provenance for dispatch audit events."""

from __future__ import annotations

import subprocess
from pathlib import Path

from kew import __version__
from kew.subprocess_utils import run


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = run(["git", *args], cwd=root, check=False, timeout=5)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _git_dirty(root: Path) -> bool | None:
    try:
        result = run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=False,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def orchestrator_provenance() -> dict[str, str | bool | None]:
    """Return the kew build identity without making dispatch depend on git.

    Editable installs point ``__file__`` at the source checkout, which is the
    checkout whose commit actually supplied the running orchestrator. A wheel
    has no source repository, so its commit and dirty state are explicitly
    unknown rather than inferred from the target project's worktree.
    """
    root = Path(__file__).resolve().parents[2]
    commit = _git_output(root, "rev-parse", "HEAD")
    dirty: bool | None = None
    if commit is not None:
        dirty = _git_dirty(root)
    return {
        "orchestrator_version": __version__,
        "orchestrator_commit": commit,
        "orchestrator_dirty": dirty,
    }
