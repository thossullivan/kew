"""Real-`gh` allowlist smoke test for the epic driver (NON-NEGOTIABLE #6).

Every other driver test fakes ``deps``/``facts``/the dispatch sink — which
stubs the exact layer that resolves the tool allowlist (``TOOL_PATHS`` in
``kew.subprocess_utils``). That is the #435 blind spot: a driver code path
that forgets to call ``preflight_tools()`` before shelling out is invisible
to those tests, because the fake sink/reader never calls ``resolve_cmd`` at
all, and the global ``TOOL_PATHS`` can be left populated by an earlier test
in the same process.

This test uses REAL ``GitHubConventionSource``, REAL ``GitHubFactsReader``,
and a REAL ``DetachedDispatchSink`` — no fakes for any of them — and mocks
*only* the stdlib boundary (``subprocess.run`` / ``subprocess.Popen``), the
same seam ``kew.subprocess_utils.run``/``popen`` call after resolving the
executable name through the allowlist. ``TOOL_PATHS`` is forced empty before
the tick, so the ONLY way a `gh`/`git`/`kew` call can reach the mocked
stdlib functions with a resolved absolute path is if the driver actually ran
``preflight_tools()`` (for gh/git) and ``DetachedDispatchSink``'s own
runtime registration (for kew) first. If either seam were missing, the real
``resolve_cmd`` would raise ``ValueError`` on an empty ``TOOL_PATHS``, and
the tick would raise instead of completing.

One tick over a two-node graph exercises both allowlist-critical paths:
  * node "1" — a bare TODO issue with no linked PR — is the frontier
    dispatch candidate.
  * node "2" — an OPEN issue with a linked, approved, CI-clean PR — is the
    merge-pass candidate.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew import subprocess_utils
from kew.config import DispatchConfig, EpicConfig
from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.driver.dispatch import DetachedDispatchSink
from kew.graph.driver.facts import GitHubFactsReader
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import advance
from kew.run_store import SqliteRunStore

REPO = "o/r"
EPIC = "dag-1"
PR_NUMBER = 99
HEAD_OID = "deadbeef1234"


class _ArgvScriptedSubprocess:
    """Fake for the stdlib ``subprocess.run``, keyed on the `gh`/`git` argv.

    Records every call (for the allowlist assertions) and returns canned
    ``CompletedProcess`` output for exactly the calls one `advance()` tick
    over the two-node graph needs to make. Raises on anything unscripted so
    an unexpected call fails loudly instead of silently returning empty
    facts.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._merged = False

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        check: bool = True,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        exe = argv[0]
        if exe.endswith("/git"):
            return self._git(argv)
        if exe.endswith("/gh"):
            return self._gh(argv)
        raise AssertionError(f"unscripted executable in resolved argv: {argv!r}")

    def _git(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1] == "ls-remote":
            # No remote branch yet for this issue -> dispatch proceeds.
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unscripted git call: {argv!r}")

    def _gh(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["issue", "list"]:
            return self._issue_list(argv)
        if argv[1:3] == ["issue", "view"]:
            return self._issue_view(argv)
        if argv[1:3] == ["pr", "list"]:
            return self._pr_list(argv)
        if argv[1:3] == ["pr", "view"]:
            return self._pr_view(argv)
        if argv[1:3] == ["pr", "merge"]:
            return self._pr_merge(argv)
        if argv[1:3] == ["issue", "edit"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1] == "api" and any("/check-runs" in part for part in argv):
            # The merge gate reads the head's own check runs (#2033): the
            # tests really ran and passed on this head.
            runs = '{"id": 1, "name": "test", "status": "completed", '
            runs += '"conclusion": "success"}\n'
            return subprocess.CompletedProcess(argv, 0, stdout=runs, stderr="")
        if argv[1] == "api":
            payload = {
                "permission": "write",
                "user": {"login": "alice", "type": "User"},
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )
        raise AssertionError(f"unscripted gh call: {argv!r}")

    def _issue_list(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        # build_graph (via GitHubConventionSource.list_epic_nodes): the two
        # epic-member issues. Neither body carries a `kew:depends` marker,
        # so build_graph resolves them as two independent (edge-free) nodes.
        issues = [
            {
                "number": 1,
                "title": "dispatchable root",
                "url": f"https://github.com/{REPO}/issues/1",
                "state": "OPEN",
                "stateReason": None,
                "labels": [],
                "body": "",
            },
            {
                "number": 2,
                "title": "approved + clean, ready to merge",
                "url": f"https://github.com/{REPO}/issues/2",
                "state": "OPEN",
                "stateReason": None,
                "labels": [],
                "body": "",
            },
        ]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(issues), stderr=""
        )

    def _issue_view(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        issue_num = argv[3]
        if issue_num == "1":
            # No linked PR -> NodeFacts.pr_number is None -> overlay TODO,
            # i.e. the frontier dispatch candidate.
            payload = {
                "number": 1,
                "state": "OPEN",
                "stateReason": None,
                "labels": [],
                "closedByPullRequestsReferences": [],
            }
        elif issue_num == "2":
            # Linked PR #99. Before the merge lands this reads OPEN; after
            # `gh pr merge` runs, a real squash-merge auto-closes the linked
            # issue, so subsequent reads (merge_pass's own _verify_closed
            # re-check) must see CLOSED/COMPLETED without a separate
            # `gh issue close` call.
            payload = {
                "number": 2,
                "state": "CLOSED" if self._merged else "OPEN",
                "stateReason": "COMPLETED" if self._merged else None,
                "labels": [],
                "closedByPullRequestsReferences": [
                    {"number": PR_NUMBER, "url": f"https://github.com/{REPO}/pull/99"}
                ],
            }
        else:
            raise AssertionError(f"unscripted gh issue view for {issue_num!r}")
        stdout = json.dumps(payload)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def _pr_list(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Script the resolver's broad batch and branch-miss fallback."""
        payload: list[dict[str, Any]] = []
        head = argv[argv.index("--head") + 1] if "--head" in argv else None
        if head is None or head == "agent/issue-2":
            payload.append(
                {
                    "number": PR_NUMBER,
                    "url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
                    "state": "MERGED" if self._merged else "OPEN",
                    "reviewDecision": "APPROVED",
                    "mergeStateStatus": "CLEAN",
                    "headRefOid": HEAD_OID,
                    "headRefName": "agent/issue-2",
                    "commits": [{"committedDate": "2026-06-30T00:00:00Z"}],
                    "latestReviews": [
                        {
                            "state": "APPROVED",
                            "submittedAt": "2026-07-01T00:00:00Z",
                            "author": {
                                "login": "alice",
                                "is_bot": False,
                                "__typename": "User",
                            },
                        }
                    ],
                    "author": {"login": "carol"},
                }
            )
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        )

    def _pr_view(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        pr_num = argv[3]
        assert pr_num == str(PR_NUMBER), f"unscripted gh pr view for {pr_num!r}"
        payload = {
            "number": PR_NUMBER,
            "state": "MERGED" if self._merged else "OPEN",
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "headRefOid": HEAD_OID,
            "commits": [{"committedDate": "2026-06-30T00:00:00Z"}],
            "latestReviews": [
                {
                    "state": "APPROVED",
                    "submittedAt": "2026-07-01T00:00:00Z",
                    "author": {
                        "login": "alice",
                        "is_bot": False,
                        "__typename": "User",
                    },
                }
            ],
            "author": {"login": "carol"},
        }
        stdout = json.dumps(payload)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def _pr_merge(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[3] == str(PR_NUMBER)
        assert "--squash" in argv
        assert "--match-head-commit" in argv
        assert HEAD_OID in argv
        self._merged = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class _RecordingPopen:
    """Fake for the stdlib ``subprocess.Popen`` — records argv, spawns nothing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> None:
        self.calls.append(list(argv))
        return None


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "run.db"), project_root=tmp_path)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset_tool_paths() -> Any:
    """Reset the allowlist to empty before the test, restore after.

    NOTE: this mutates ``subprocess_utils.TOOL_PATHS`` *in place* rather
    than rebinding the module attribute to a new dict (i.e. NOT
    ``monkeypatch.setattr(subprocess_utils, "TOOL_PATHS", {})``). Several
    modules (e.g. ``kew.graph.driver.dispatch``) did
    ``from kew.subprocess_utils import TOOL_PATHS`` at import time, binding
    their own module-level name to the *original* dict object. Rebinding
    the attribute on ``subprocess_utils`` would leave those already-bound
    references pointing at the old (stale) dict, silently defeating the
    reset for exactly the seam (``DetachedDispatchSink``) this test needs
    to prove runs preflight/registration correctly. Clearing in place keeps
    every alias looking at the same (now-empty) dict.
    """
    saved = dict(subprocess_utils.TOOL_PATHS)
    subprocess_utils.TOOL_PATHS.clear()
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def test_advance_tick_exercises_real_gh_allowlist_on_dispatch_and_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    # --- Assertion (1a): the allowlist really is empty at test start. ---
    assert subprocess_utils.TOOL_PATHS == {}

    fake_run = _ArgvScriptedSubprocess()
    fake_popen = _RecordingPopen()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    deps = GitHubConventionSource(REPO)
    facts = GitHubFactsReader(REPO)

    config = DispatchConfig(epic=EpicConfig(stall_days=3))
    from kew import credential_issuer

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        lambda run_id, limit, expiry, *, ledger_id, on_malformed: (
            credential_issuer.BoundedRunKey(
                "bounded-secret-1944", "a" * 64, limit, expiry.isoformat()
            )
        ),
    )
    dispatch_sink = DetachedDispatchSink(epic_ref=EPIC, config=config, store=store)
    lock_path = tmp_path / "epic.lock"

    result = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=dispatch_sink,
        max_parallel=5,
        now=datetime(2026, 7, 1, tzinfo=UTC),
        lock_path=lock_path,
        approvers=[],
        repo=REPO,
    )

    # --- The tick actually did the work we scripted it to do. ---
    assert result.dispatched == ("1",)
    assert result.merged == ("2",)
    assert result.signal in (TerminalSignal.COMPLETE, TerminalSignal.PROGRESSING)

    # --- Assertion (1b): preflight_tools() ran and populated the allowlist. ---
    assert "gh" in subprocess_utils.TOOL_PATHS
    assert "git" in subprocess_utils.TOOL_PATHS
    assert Path(subprocess_utils.TOOL_PATHS["gh"]).is_absolute()
    assert Path(subprocess_utils.TOOL_PATHS["git"]).is_absolute()

    # --- Assertion (2): a MERGE-path gh call carried a RESOLVED ABSOLUTE
    # path, not the bare string "gh". `gh pr merge` is the write itself;
    # requiring it specifically (not just any gh call) proves the merge
    # write path in particular went through the allowlist.
    merge_calls = [c for c in fake_run.calls if c[1:3] == ["pr", "merge"]]
    assert len(merge_calls) == 1, fake_run.calls
    merge_argv0 = merge_calls[0][0]
    assert merge_argv0 != "gh"
    assert Path(merge_argv0).is_absolute()
    assert merge_argv0.endswith("/gh")

    pr_list_calls = [c for c in fake_run.calls if c[1:3] == ["pr", "list"]]
    assert any("--state" in call and "all" in call for call in pr_list_calls)

    # --- Assertion (3a): a DISPATCH-path gh call (the `gh issue list` read
    # that build_graph performs, which precedes/enables this tick's
    # dispatch) also carried a resolved absolute path.
    issue_list_calls = [c for c in fake_run.calls if c[1:3] == ["issue", "list"]]
    assert len(issue_list_calls) == 1, fake_run.calls
    issue_list_argv0 = issue_list_calls[0][0]
    assert issue_list_argv0 != "gh"
    assert Path(issue_list_argv0).is_absolute()
    assert issue_list_argv0.endswith("/gh")

    # --- Assertion (3b): the DISPATCH spawn itself (`kew epic
    # _dispatch-node`) went through the allowlisted `popen`, with a
    # resolved absolute `kew` path, not the bare string "kew".
    assert len(fake_popen.calls) == 1, fake_popen.calls
    dispatch_argv = fake_popen.calls[0]
    kew_argv0 = dispatch_argv[0]
    assert kew_argv0 != "kew"
    assert Path(kew_argv0).is_absolute()
    assert kew_argv0.endswith("/kew")
    # The parent pins the child to the reservation it was admitted under
    # (--reservation-run-id / --reservation-generation, #1947 round 13), and
    # mints that child's bounded run key before the spawn.
    assert dispatch_argv[1:5] == ["epic", "_dispatch-node", EPIC, "1"]
    assert "--reservation-run-id" in dispatch_argv[5:]
    assert "--reservation-generation" in dispatch_argv[5:]
    assert "--run-id" in dispatch_argv[5:]
    spawned_run_id = dispatch_argv[dispatch_argv.index("--run-id") + 1]
    assert (
        store.list_audit_events(run_id=spawned_run_id)[0].payload["phase"] == "issuance"
    )
    assert "kew" in subprocess_utils.TOOL_PATHS
