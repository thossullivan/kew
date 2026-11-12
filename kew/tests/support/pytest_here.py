"""A red-at-base runner for tmp repos that have no uv project."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kew.acceptance_author import BaseOutcome


def run_pytest_here(worktree: Path, node_id: str) -> BaseOutcome:
    """The real pytest, on this interpreter: the tmp repo has no uv project.

    Like the production runner, it writes nothing into the checkout: no
    bytecode and no ``.pytest_cache``, which the author refuses as changes.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node_id],
        cwd=worktree,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode == 0:
        return "passed"
    return "failed" if completed.returncode == 1 else "error"
