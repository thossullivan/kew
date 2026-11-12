"""Provider adapters for the DependencySource port, plus their factory."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.adapters.github_native import GitHubNativeSource
from kew.graph.adapters.linear_native import LinearSource
from kew.graph.port import DependencySource
from kew.linear.client import LinearClient

if TYPE_CHECKING:
    from kew.config import DispatchConfig


def make_github_source(
    *, repo: str, epic_prefix: str = "epic:"
) -> GitHubNativeSource | GitHubConventionSource:
    """The GitHub adapter this API supports: native relations, else convention.

    The capability probe runs once per driver run (cached by repo) and its
    verdict — including *why* it fell back — is printed by `kew epic status`,
    so a repository reading its epic from the label instead of its sub-issues
    says so rather than looking identical to one that has no sub-issues.
    """
    return GitHubNativeSource(repo=repo, epic_prefix=epic_prefix, probe_on_read=True)


def make_dependency_source(
    config: DispatchConfig,
    *,
    env: Mapping[str, str] | None = None,
    epic_prefix: str = "epic:",
) -> DependencySource:
    """Build the DependencySource the config's tracker selects.

    GitHub needs ``project.repo`` (callers detect it first); Linear needs the
    LINEAR_API_KEY environment variable and no subprocess tooling at all.
    """
    environ = os.environ if env is None else env
    if config.tracker.type == "linear":
        api_key = environ.get("LINEAR_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "tracker.type is 'linear' but the LINEAR_API_KEY environment "
                "variable is not set."
            )
        return LinearSource(
            LinearClient(api_key),
            in_review_state=config.tracker.linear.in_review_state,
        )
    return make_github_source(repo=config.project.repo, epic_prefix=epic_prefix)
