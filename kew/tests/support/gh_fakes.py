"""Fake `gh` seams: the epic-plan recorder, the conformance forge, and the
placeholder forge a test installs so no ``gh`` call leaves the process."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.pr_batch import is_pr_batch, pr_batch_result


class FakeGh:
    """Records gh invocations; returns sequential issue URLs on create."""

    def __init__(self, fail_on_create_number: int | None = None) -> None:
        self.calls: list[list[str]] = []
        self._creates = 0
        self._fail_on = fail_on_create_number

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "create"]:
            self._creates += 1
            if self._fail_on is not None and self._creates == self._fail_on:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            n = 100 + self._creates
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"https://github.com/o/r/issues/{n}\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "create"]]


class FakeForge:
    """A stateful GitHub stand-in for every ``gh`` argv kew sends.

    The conformance scenarios (#2487) drive the real epic tick — graph read,
    facts read, merge pass, label and comment writes — and stop only here, at
    the ``gh`` argv (reached through :class:`GhProcessBoundary`). Issues and
    PRs are plain ``gh --json``-shaped dicts. ``gh pr merge`` is the one call
    with an effect outside this object: it fast-forwards *origin*'s base branch
    to the PR head in a real bare repository, so a dependent cut afterwards
    carries the merged commit in its real git history.

    Every argv is kept in :attr:`calls`; an argv the fake does not model is
    answered with a failure and kept in :attr:`unmodelled`, never with an
    empty success.
    """

    def __init__(self, *, origin: Path, repo: str = "o/r", base: str = "main") -> None:
        self.origin = origin
        self.repo = repo
        self.base = base
        self.issues: dict[int, dict[str, Any]] = {}
        self.prs: dict[int, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        #: Argvs the fake answered with a failure because it does not model
        #: them. A caller may swallow a failed ``gh``; a scenario asserts this
        #: stays empty, so an unmodelled read cannot pass as a quiet "no".
        self.unmodelled: list[list[str]] = []
        #: Collaborator logins and the repository permission each holds.
        self.permissions: dict[str, str] = {}
        #: The check runs every PR head reports: one green ``test`` job.
        self.check_runs: list[dict[str, Any]] = [
            {"id": 1, "name": "test", "status": "completed", "conclusion": "success"}
        ]
        self._next_pr = 70

    def open_issue(
        self, number: int, *, epic: str = "", depends_on: tuple[int, ...] = ()
    ) -> None:
        """An open issue; *epic* labels it a member, *depends_on* its edges.

        Membership and edges are what the GitHub convention adapter reads: the
        ``epic:<name>`` label and a ``kew:depends`` comment in the body.
        """
        body = f"Issue {number}."
        if depends_on:
            body += f"\n\n<!-- kew:depends {' '.join(map(str, depends_on))} -->"
        self.issues[number] = {
            "number": number,
            "title": f"node {number}",
            "url": f"https://github.com/{self.repo}/issues/{number}",
            "body": body,
            "state": "OPEN",
            "stateReason": None,
            "labels": [{"name": f"epic:{epic}"}] if epic else [],
            "closedByPullRequestsReferences": [],
            "comments": [],
        }

    def open_pr(self, *, issue: int, branch: str, head: str) -> dict[str, Any]:
        number = self._next_pr
        self._next_pr += 1
        pr: dict[str, Any] = {
            "number": number,
            "url": f"https://github.com/{self.repo}/pull/{number}",
            "state": "OPEN",
            "headRefName": branch,
            "baseRefName": self.base,
            "reviewDecision": None,
            "mergeStateStatus": "CLEAN",
            "headRefOid": head,
            "commits": [],
            "latestReviews": [],
            "author": {"login": "kew-agent"},
            "body": f"Closes #{issue}",
            "closes": issue,
        }
        self.prs[number] = pr
        return pr

    def approve(self, number: int, *, login: str, at: str) -> None:
        pr = self.prs[number]
        pr["reviewDecision"] = "APPROVED"
        pr["latestReviews"] = [
            {
                "state": "APPROVED",
                "author": {"login": login, "is_bot": False, "__typename": "User"},
                "submittedAt": at,
            }
        ]

    def merges(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "pr", "merge"]]

    def __call__(
        self, cmd: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if is_pr_batch(cmd):
            return pr_batch_result(cmd, list(self.prs.values()))
        if cmd[1:3] == ["api", "graphql"] and "blockedBy(first: 1)" in str(cmd):
            # A forge without native issue linkage: the probe's fields are
            # undefined, so kew reads the epic by its label convention.
            error = {
                "message": "Field 'blockedBy' doesn't exist on type 'Issue'",
                "extensions": {"code": "undefinedField", "fieldName": "blockedBy"},
            }
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"errors": [error]}), ""
            )
        if cmd[1:3] == ["api", "rate_limit"]:
            # The dispatch connectivity probe reads only the exit status.
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        if cmd[1:3] == ["repo", "view"]:
            # Only ever asked as `-q .nameWithOwner`: jq prints the raw string.
            return subprocess.CompletedProcess(cmd, 0, f"{self.repo}\n", "")
        if any(arg.endswith("/check-runs?filter=all") for arg in cmd):
            # `gh --jq` streams one JSON object per line, not one document.
            ndjson = "".join(json.dumps(run) + "\n" for run in self.check_runs)
            return subprocess.CompletedProcess(cmd, 0, ndjson, "")
        try:
            payload = self._answer(cmd)
        except LookupError as exc:
            self.unmodelled.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", f"FakeForge: {exc}")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    def _answer(self, cmd: list[str]) -> Any:
        verb = cmd[1:3]
        if verb == ["issue", "list"]:
            label = _flag(cmd, "--label")
            return [i for i in self.issues.values() if label in self._names(i)]
        if verb == ["issue", "view"]:
            return self.issues[int(cmd[3])]
        if verb == ["issue", "close"]:
            self._close_issue(int(cmd[3]))
            return {}
        if verb == ["issue", "edit"]:
            self._edit_labels(int(cmd[3]), cmd[4:])
            return {}
        if verb == ["issue", "comment"]:
            self.issues[int(cmd[3])]["comments"].append({"body": _flag(cmd, "--body")})
            return {}
        if verb == ["pr", "view"]:
            return self.prs[int(cmd[3])]
        if verb == ["pr", "list"]:
            return list(self.prs.values())
        if verb == ["pr", "merge"]:
            self._merge(int(cmd[3]))
            return {}
        if cmd[1:4] == ["api", "--method", "GET"]:
            return {"body": self._body_for(cmd)}
        if cmd[1] == "api" and cmd[2].endswith("/permission"):
            login = cmd[2].split("/")[-2]
            return {
                "permission": self.permissions[login],
                "user": {"login": login, "type": "User"},
            }
        raise LookupError(f"does not model {cmd[:4]}")

    def _body_for(self, cmd: list[str]) -> str:
        for pr in self.prs.values():
            if any(arg.endswith(f"/pulls/{pr['number']}") for arg in cmd):
                return str(pr["body"])
        raise LookupError(f"has no PR for {cmd}")

    def labels(self, number: int) -> set[str]:
        return self._names(self.issues[number])

    @staticmethod
    def _names(issue: dict[str, Any]) -> set[str]:
        return {label["name"] for label in issue["labels"]}

    def _edit_labels(self, number: int, args: list[str]) -> None:
        names = self.labels(number)
        for flag, value in zip(args, args[1:], strict=False):
            if flag == "--add-label":
                names |= set(value.split(","))
            elif flag == "--remove-label":
                names -= set(value.split(","))
        self.issues[number]["labels"] = [{"name": n} for n in sorted(names)]

    def _close_issue(self, number: int) -> None:
        self.issues[number]["state"] = "CLOSED"
        self.issues[number]["stateReason"] = "COMPLETED"

    def _merge(self, number: int) -> None:
        pr = self.prs[number]
        head = str(pr["headRefOid"])
        expected = _flag(self.calls[-1], "--match-head-commit")
        if expected and expected != head:
            raise LookupError(f"head moved: PR #{number} is at {head}")
        # A fast-forward only: the scenario's base never moves under a PR, so a
        # non-ancestor base here is a scenario bug, and git says so.
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", self.base, head],
            cwd=self.origin,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{self.base}", head],
            cwd=self.origin,
            check=True,
        )
        pr["state"] = "MERGED"
        pr["mergeStateStatus"] = "UNKNOWN"
        # `Closes #N` closes the linked issue on GitHub's side of a merge.
        self._close_issue(int(pr["closes"]))


class GhProcessBoundary:
    """``subprocess`` as ``kew.subprocess_utils`` sees it, with ``gh`` faked.

    Every kew subprocess goes through ``subprocess_utils.run``, which resolves
    the allowlisted executable and calls ``subprocess.run``. Installed as that
    module's ``subprocess``, this answers a resolved ``gh`` argv from *forge*
    and hands every other argv — ``git`` above all — to the real
    ``subprocess.run``. So the code under test shells out exactly as it does in
    production, and only the forge on the far side of ``gh`` is fake.
    """

    def __init__(
        self,
        forge: Callable[..., subprocess.CompletedProcess[str]],
        *,
        tool_paths: dict[str, str],
    ) -> None:
        self._forge = forge
        self._tool_paths = tool_paths

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        exe = str(cmd[0])
        if Path(exe).name != "gh" and exe != self._tool_paths.get("gh"):
            return subprocess.run(cmd, **kwargs)
        result = self._forge(["gh", *map(str, cmd[1:])])
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result


class PlaceholderForge:
    """GitHub as it answers the placeholder repos these tests name (``o/r``).

    Every ``gh`` argv is kept in :attr:`calls` and answered as GitHub answers
    a repository that does not exist: ``HTTP 404``. The tests that install it
    already passed against a failing ``gh`` — the 404s the operator's got for
    ``o/r`` (#3069), and the launch error of a host with none — so it keeps
    what they check while nothing leaves the process.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "gh: Not Found (HTTP 404)")


def install_placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> PlaceholderForge:
    """Answer every ``gh`` kew shells out to from a :class:`PlaceholderForge`.

    Installed at the process boundary (``subprocess_utils``' ``subprocess``),
    so the publishers and readers under test run exactly as in production and
    every other argv, ``git`` above all, still reaches the real process.
    """
    from kew import subprocess_utils

    forge = PlaceholderForge()
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    return forge


def _flag(cmd: list[str], name: str) -> str:
    """The value after *name* in *cmd*, or ``""`` when the flag is absent."""
    for flag, value in zip(cmd, cmd[1:], strict=False):
        if flag == name:
            return value
    return ""
