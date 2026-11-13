"""Fake answers for the facts reader's one-query PR batch (#2582).

`GitHubFactsReader.prepare` asks GitHub, in a single ``gh api graphql`` call,
for every live node's branch PRs (aliases ``b0``, ``b1``, …) and every PR number
the ledger or a closing reference names (``p0``, ``p1``, …). Tests describe the
forge as a list of ``gh pr view --json``-shaped PR dicts; this module turns that
list into the GraphQL response the reader parses.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from typing import Any


def is_pr_batch(cmd: list[str]) -> bool:
    """Whether *cmd* is the reader's batch query (not some other GraphQL)."""
    query = next((arg for arg in cmd if arg.startswith("query=")), "")
    return cmd[:3] == ["gh", "api", "graphql"] and any(
        selection in query
        for selection in (
            "pullRequests(headRefName:",
            "pullRequest(number:",
            "ref(qualifiedName:",
        )
    )


def batch_variables(cmd: list[str]) -> dict[str, str]:
    """The ``-f``/``-F`` variables the batch passed, by name."""
    variables: dict[str, str] = {}
    for flag, arg in zip(cmd, cmd[1:], strict=False):
        if flag in ("-f", "-F") and not arg.startswith("query="):
            key, _, value = arg.partition("=")
            variables[key] = value
    return variables


def batched_branches(cmd: list[str]) -> list[str]:
    return [
        v for k, v in batch_variables(cmd).items() if k[0] == "b" and k[1:].isdigit()
    ]


def batched_numbers(cmd: list[str]) -> list[int]:
    return [
        int(v)
        for k, v in batch_variables(cmd).items()
        if k[0] == "p" and k[1:].isdigit()
    ]


def _graphql_pr(pr: dict[str, Any]) -> dict[str, Any]:
    node = dict(pr)
    node["commits"] = {"nodes": [{"commit": c} for c in pr.get("commits") or []]}
    node["latestReviews"] = {"nodes": list(pr.get("latestReviews") or [])}
    return node


def pr_batch_payload(
    cmd: list[str],
    prs: Iterable[dict[str, Any]],
    *,
    cost: int = 1,
    branches: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The GraphQL ``data`` GitHub would return for *cmd* over *prs*.

    *branches* are the branches that exist for a ref probe (``r0``, …); by
    default, every branch some PR in *prs* was opened from.
    """
    prs = list(prs)
    existing = (
        {str(pr.get("headRefName")) for pr in prs}
        if branches is None
        else set(branches)
    )
    repository: dict[str, Any] = {}
    for key, value in batch_variables(cmd).items():
        if key[0] == "b" and key[1:].isdigit():
            matches = sorted(
                (pr for pr in prs if pr.get("headRefName") == value),
                key=lambda pr: -int(pr.get("number", 0)),
            )
            repository[key] = {"nodes": [_graphql_pr(pr) for pr in matches[:5]]}
        elif key[0] == "p" and key[1:].isdigit():
            match = next((pr for pr in prs if pr.get("number") == int(value)), None)
            repository[key] = _graphql_pr(match) if match is not None else None
        elif key[0] == "r" and key[1:].isdigit():
            branch = value.removeprefix("refs/heads/")
            repository[key] = {"name": branch} if branch in existing else None
    return {"data": {"rateLimit": {"cost": cost}, "repository": repository}}


def pr_batch_result(
    cmd: list[str],
    prs: Iterable[dict[str, Any]],
    *,
    cost: int = 1,
    branches: Iterable[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = pr_batch_payload(cmd, prs, cost=cost, branches=branches)
    return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
