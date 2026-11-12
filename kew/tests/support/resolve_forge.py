"""The edges ``kew resolve`` reaches past the repository, for one PR.

``gh pr view`` answers with the PR at the head the forge reports, the remote
ref reads answer with that head and the base, the checkout hands back a real
worktree the base already merged into, and the push (``publish_checkout``)
records what it was asked to publish instead of reaching a remote. Every other
process runs for real.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kew.subprocess_utils import run


class Push:
    """``publish_checkout``: the push of the integration commit, recorded."""

    def __init__(self) -> None:
        self.published: list[Path] = []

    def __call__(self, worktree: Path, **_kwargs: Any) -> object:
        self.published.append(worktree)
        return object()


@contextmanager
def resolve_forge(
    worktree: Path, *, branch: str, pr_head: str, item: int, base_sha: str
) -> Iterator[Push]:
    """``kew resolve``'s forge for a PR on *branch* whose head is *pr_head*,
    with *worktree* the checkout the base *base_sha* merged into cleanly."""
    push = Push()
    meta = {
        "headRefName": branch,
        "headRefOid": pr_head,
        "baseRefName": "main",
        "title": f"node {item}",
        "body": f"Closes #{item}",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
    }

    def remote_head(_remote: str, ref: str, **_kwargs: Any) -> str:
        return pr_head if ref == branch else base_sha

    def gh_or_run(cmd: list[str], **kwargs: Any) -> Any:
        """``gh pr view`` answers with the PR; every other process runs."""
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(meta), "")
        return run(cmd, **kwargs)

    with (
        patch("kew.resolve.subprocess_run", side_effect=gh_or_run),
        patch("kew.resolve._check_github_connectivity", return_value=True),
        patch("kew.resolve.checkout_existing_worktree", return_value=(worktree, None)),
        patch("kew.resolve.remote_branch_sha", side_effect=remote_head),
        patch("kew.resolve.remote_commit_is_ancestor", return_value=False),
        patch("kew.resolve.publish_checkout", side_effect=push),
        patch("kew.resolve.cleanup_worktree"),
    ):
        yield push
