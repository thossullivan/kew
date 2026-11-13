"""Tests for the approval-gated, CI-green merge pass (Epic DAG driver, task 8).

NON-NEGOTIABLE #4: a merge is triggered ONLY by a verified human PR approval
(gh's own `reviewDecision`/`latestReviews`), never by a label. These tests
exist specifically to pin that property down — see in particular
`test_forged_label_never_triggers_merge` and
`test_author_self_approval_is_not_verified`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from kew.epic_holds import holds_node_out_of_frontier
from kew.graph.driver.facts import NodeFacts, RepositoryPermission
from kew.graph.driver.merge import (
    MergeVerdict,
    is_self_healing_hold,
    is_verified_approval,
    merge_pass,
)

REPO = "o/r"
BASE = "main"
EPIC_REF = "epic/1"


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _facts(issue_num: int, *, state: str, pr_number: int | None) -> NodeFacts:
    return NodeFacts(
        issue_num=issue_num,
        state=state,
        state_reason=None,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
    )


class _FakeReader:
    """Fake GitHubFactsReader: pre-seeded PR facts, mutable issue state."""

    def __init__(
        self, *, pr: dict[str, Any], issue_state: str = "OPEN", pr_number: int = 42
    ) -> None:
        self.pr = pr
        self.issue_state = issue_state
        self.pr_number = pr_number
        self.pr_facts_calls: list[int] = []
        self.permission = "write"
        self.permission_calls: list[str] = []
        # What the head's own check runs say (#2033); the real reader decides
        # this with kew.ci_checks, tested in test_driver_facts.py.
        self.checks: tuple[str, str] = ("success", "CI green (2 checks)")
        self.checked_heads: list[str] = []
        self.gated_seen: list[list[str]] = []

    def issue_facts(self, issue_num: int) -> NodeFacts:
        return _facts(issue_num, state=self.issue_state, pr_number=self.pr_number)

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        self.checked_heads.append(sha)
        self.gated_seen.append(list(gated_checks))
        return self.checks

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        self.pr_facts_calls.append(pr_number)
        return self.pr

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return pr.get("mergeStateStatus") in {"CLEAN", "HAS_HOOKS"}

    def repository_permission(self, login: str) -> RepositoryPermission | None:
        self.permission_calls.append(login)
        return RepositoryPermission(login, "User", self.permission)


@dataclass
class _FakeStore:
    holds: list[tuple[str, str, str]] = field(default_factory=list)
    clears: list[str] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)

    def hold_epic_node(self, epic_ref: str, item: str, reason: str) -> None:
        self.holds.append((epic_ref, item, reason))

    def list_epic_holds(self, epic_ref: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(item_id=item, reason=reason)
            for ref, item, reason in self.holds
            if ref == epic_ref and item not in self.clears
        ]

    def clear_epic_hold(self, item: str) -> None:
        self.clears.append(item)

    def latest_review_round(self, item_id: str) -> None:
        """No review round on record: the no-queue merge reads the latest READY
        round's ``not_checked`` (#3421), and these nodes were never reviewed."""
        return None

    def append_audit_event(self, **kwargs: Any) -> int:
        self.audit_events.append(kwargs)
        return len(self.audit_events)

    def list_audit_events(self, *, limit: int, **filters: Any) -> list[SimpleNamespace]:
        events = [
            SimpleNamespace(id=index, **event)
            for index, event in enumerate(self.audit_events, 1)
            if all(
                value is None or event.get(name) == value
                for name, value in filters.items()
            )
        ]
        return list(reversed(events))[:limit]


def _pr(
    *,
    number: int = 42,
    state: str = "OPEN",
    review_decision: str | None = "APPROVED",
    merge_state_status: str | None = "CLEAN",
    head_oid: str = "deadbeef",
    author_login: str = "carol",
    approver_login: str | None = "alice",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    latest_reviews = []
    if approver_login is not None:
        latest_reviews.append(
            {
                "state": "APPROVED",
                "submittedAt": "2026-07-01T00:00:00Z",
                "author": {
                    "login": approver_login,
                    "is_bot": False,
                    "__typename": "User",
                },
            }
        )
    pr: dict[str, Any] = {
        "number": number,
        "state": state,
        "reviewDecision": review_decision,
        "mergeStateStatus": merge_state_status,
        "headRefOid": head_oid,
        "commits": [],
        "latestReviews": latest_reviews,
        "author": {"login": author_login},
    }
    if labels is not None:
        pr["labels"] = [{"name": name} for name in labels]
    return pr


def _make_run(
    *,
    merge_rc: int = 0,
    reader: _FakeReader | None = None,
    close_flips_issue: bool = True,
):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _Result:
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _Result(returncode=merge_rc)
        if cmd[:3] == ["gh", "issue", "close"]:
            if reader is not None and close_flips_issue:
                reader.issue_state = "CLOSED"
            return _Result(returncode=0)
        return _Result(returncode=0)

    return calls, fake_run


def _run_one(node_id: str, reader: _FakeReader, store: _FakeStore, run: Any):
    verdicts = merge_pass(
        [node_id],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
    )
    assert len(verdicts) == 1
    return verdicts[0]


# ---------------------------------------------------------------------------
# is_verified_approval — unit tests
# ---------------------------------------------------------------------------


def _permission(login: str, permission: str = "write") -> RepositoryPermission | None:
    return RepositoryPermission(login, "User", permission)


def test_is_verified_approval_true_for_non_author_in_approvers() -> None:
    pr = _pr(author_login="carol", approver_login="alice")
    assert is_verified_approval(pr, ["alice"], _permission) is True


def test_is_verified_approval_empty_approvers_requires_write_authority() -> None:
    pr = _pr(author_login="carol", approver_login="bob")
    assert is_verified_approval(pr, [], _permission) is True


def test_is_verified_approval_false_when_approver_not_in_approvers_list() -> None:
    pr = _pr(author_login="carol", approver_login="mallory")
    assert is_verified_approval(pr, ["alice"], _permission) is False


def test_is_verified_approval_false_for_author_self_approval() -> None:
    pr = _pr(author_login="carol", approver_login="carol")
    assert is_verified_approval(pr, ["carol"], _permission) is False


def test_is_verified_approval_false_without_review_decision_approved() -> None:
    pr = _pr(review_decision="REVIEW_REQUIRED", approver_login="alice")
    assert is_verified_approval(pr, ["alice"], _permission) is False


def test_is_verified_approval_ignores_labels() -> None:
    pr = _pr(review_decision=None, approver_login=None, labels=["ready-to-merge"])
    assert is_verified_approval(pr, [], _permission) is False


def test_is_verified_approval_rejects_bot_and_external_without_authority() -> None:
    pr = _pr(approver_login="coderabbitai")
    pr["latestReviews"][0]["author"]["is_bot"] = True
    pr["latestReviews"][0]["author"]["__typename"] = "Bot"
    pr["latestReviews"][0]["authorAssociation"] = "NONE"
    assert is_verified_approval(pr, [], _permission) is False

    human = _pr(approver_login="external")
    human["latestReviews"][0]["authorAssociation"] = "NONE"
    assert (
        is_verified_approval(
            human, [], lambda login: RepositoryPermission(login, "User", "read")
        )
        is False
    )


def test_is_verified_approval_accepts_none_association_with_write_authority() -> None:
    pr = _pr(approver_login="alice")
    pr["latestReviews"][0]["authorAssociation"] = "NONE"
    assert is_verified_approval(pr, [], _permission) is True


def test_is_verified_approval_rejects_lookup_failure_and_read_or_triage() -> None:
    pr = _pr(approver_login="alice")
    assert is_verified_approval(pr, [], lambda login: None) is False
    for permission in ("read", "triage"):
        assert (
            is_verified_approval(
                pr,
                [],
                lambda login, p=permission: RepositoryPermission(login, "User", p),
            )
            is False
        )


def test_newest_approval_controls_without_authorized_fallback() -> None:
    pr = _pr(approver_login=None)
    pr["reviewDecision"] = "APPROVED"
    pr["latestReviews"] = [
        {
            "state": "APPROVED",
            "submittedAt": "2026-07-01T00:00:00Z",
            "author": {"login": "alice", "is_bot": False, "__typename": "User"},
        },
        {
            "state": "APPROVED",
            "submittedAt": "2026-07-02T00:00:00Z",
            "author": {"login": "mallory", "is_bot": False, "__typename": "User"},
        },
    ]
    calls = []

    def resolve(login: str) -> RepositoryPermission:
        calls.append(login)
        return RepositoryPermission(login, "User", "write")

    assert is_verified_approval(pr, ["alice"], resolve) is False
    assert calls == []


def test_permission_is_fresh_for_each_decision() -> None:
    pr = _pr(approver_login="alice")
    permissions = iter(("write", "read"))
    calls = []

    def resolve(login: str) -> RepositoryPermission:
        calls.append(login)
        return RepositoryPermission(login, "User", next(permissions))

    assert is_verified_approval(pr, [], resolve) is True
    assert is_verified_approval(pr, [], resolve) is False
    assert calls == ["alice", "alice"]


# ---------------------------------------------------------------------------
# 1. MERGE SAFETY
# ---------------------------------------------------------------------------


def test_merge_safety_argv_has_squash_and_match_head_commit_no_admin_no_force() -> None:
    reader = _FakeReader(
        pr=_pr(state="OPEN", merge_state_status="CLEAN", head_oid="cafef00d"),
        issue_state="CLOSED",  # auto-closed by GitHub on squash-merge
    )
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict == MergeVerdict("7", "merged", "approved+ci-green")
    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert len(merge_calls) == 1
    argv = merge_calls[0]
    assert "--squash" in argv
    assert "--match-head-commit" in argv
    assert argv[argv.index("--match-head-commit") + 1] == "cafef00d"
    assert "--admin" not in argv
    assert "--force" not in argv
    assert argv == [
        "gh",
        "pr",
        "merge",
        "42",
        "--repo",
        REPO,
        "--squash",
        "--match-head-commit",
        "cafef00d",
    ]


def _squash(title: object) -> list[str]:
    pr = _pr(state="OPEN", merge_state_status="CLEAN", head_oid="cafef00d")
    pr["title"] = title
    reader = _FakeReader(pr=pr, issue_state="CLOSED")
    calls, run = _make_run(reader=reader)

    verdict = _run_one("7", reader, _FakeStore(), run)

    assert verdict.action == "merged"
    (merge,) = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    return merge


def test_the_squash_subject_is_the_agent_prs_title() -> None:
    """The title says what the product now does; the squash lands it on main."""
    merge = _squash("An empty input now renders an  empty widget")

    assert merge[-2:] == [
        "--subject",
        "An empty input now renders an empty widget (#42)",
    ]


def test_a_closing_keyword_in_the_squash_subject_closes_nothing() -> None:
    subject = _squash("An empty input now renders, fixes #77")[-1]

    assert re.search(r"(?i)\bfixes\s*#77", subject) is None
    assert subject.replace("​", "") == "An empty input now renders, fixes #77 (#42)"


@pytest.mark.parametrize("title", [None, "", " \t ", 7])
def test_a_pr_with_no_title_in_its_facts_merges_under_githubs_default_subject(
    title: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="kew.graph.driver.merge"):
        merge = _squash(title)

    assert "--subject" not in merge
    assert "PR #42 has no title in its facts" in caplog.text


def test_has_hooks_is_clean_equivalent_and_merges() -> None:
    reader = _FakeReader(
        pr=_pr(state="OPEN", merge_state_status="HAS_HOOKS"),
        issue_state="CLOSED",
    )
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "merged"


# ---------------------------------------------------------------------------
# #2033: GitHub reports a skipped required check as passing, so CLEAN alone
# cannot merge an agent PR whose tests never ran.
# ---------------------------------------------------------------------------

AWAITING = "awaiting ci-approved: tests did not run (agent-image-scan, test skipped)"


def test_clean_pr_whose_tests_were_skipped_is_held_not_merged() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("awaiting_approval", AWAITING)
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    # A human gate, like a missing approval: AWAITING_MERGES, never a hold.
    assert verdict.action == "skip"
    assert verdict.reason == AWAITING
    assert "awaiting ci-approved: tests did not run" in verdict.reason
    assert verdict.held_reason is None
    assert store.holds == []
    assert reader.checked_heads == ["deadbeef"]
    assert reader.gated_seen == [["test", "agent-image-scan"]]
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_the_same_pr_merges_once_its_tests_really_passed() -> None:
    reader = _FakeReader(
        pr=_pr(state="OPEN", merge_state_status="CLEAN"), issue_state="CLOSED"
    )
    reader.checks = ("success", "CI green (3 checks)")
    calls, run = _make_run(reader=reader)

    verdict = _run_one("7", reader, _FakeStore(), run)

    assert verdict.action == "merged"
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_a_failed_check_on_a_clean_pr_is_still_a_failure() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("failure", "CI failed: lint")
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "held"
    assert verdict.held_reason == "ci_pending"
    assert verdict.reason == "ci-failure: CI failed: lint"
    assert store.holds == [(EPIC_REF, "7", "ci_pending")]
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_no_ci_evidence_on_a_clean_pr_waits_for_a_human_merge() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("no_evidence", "no CI evidence on head: merge by hand")
    calls, run = _make_run(reader=reader)
    store = _FakeStore()
    # A hold an earlier tick left behind is released: never `kew epic unhold`.
    store.hold_epic_node(EPIC_REF, "7", "ci_pending")

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "skip"
    assert verdict.reason == "no CI evidence on head: merge by hand"
    assert store.clears == ["7"]
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_statuses_only_success_waits_for_a_human_merge() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = (
        "no_evidence",
        "commit statuses green but no check runs: merge by hand",
    )
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "skip"
    assert verdict.reason == "commit statuses green but no check runs: merge by hand"
    assert store.holds == []
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_a_human_gate_leaves_other_holds_alone() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("awaiting_approval", AWAITING)
    calls, run = _make_run(reader=reader)
    store = _FakeStore()
    store.hold_epic_node(EPIC_REF, "7", "operator")

    _run_one("7", reader, store, run)

    assert store.clears == []


def test_no_successful_check_on_a_clean_pr_is_not_merged() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("no_checks", "no check run concluded success")
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "held"
    assert verdict.held_reason == "ci_pending"
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_checks_still_running_or_unreadable_wait_without_a_hold() -> None:
    for checks in (
        ("pending", "1/3 checks still running"),
        ("pending", "check runs unreadable: HTTP 502"),
    ):
        reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
        reader.checks = checks
        calls, run = _make_run(reader=reader)
        store = _FakeStore()

        verdict = _run_one("7", reader, store, run)

        assert verdict.action == "held"
        assert verdict.held_reason is None
        assert verdict.reason == f"ci-pending: {checks[1]}"
        assert store.holds == []
        assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_pending_checks_over_a_red_ci_hold_keep_the_row_and_its_clock() -> None:
    """A pending read does NOT release the sticky `ci_pending` row an earlier
    red-CI pass wrote, and must not call itself self-healing while it stands.

    `head_checks` reports an unreadable `gh` as `pending` too, so releasing
    here would reset a real red-CI stall clock on every failed read. Left as a
    self-healing verdict over a live row, `advance` read the node as awaiting a
    human merge while `status` — reading the row — read it held and then
    stalled, so `kew epic run` polled forever instead of exiting for the
    repair (#2183).
    """
    for checks in (
        ("pending", "1/3 checks still running"),
        ("pending", "check runs unreadable: gh failed"),
    ):
        reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
        reader.checks = checks
        calls, run = _make_run(reader=reader)
        # What an earlier red-CI pass left behind.
        store = _FakeStore(holds=[(EPIC_REF, "7", "ci_pending")])

        verdict = _run_one("7", reader, store, run)

        assert verdict.action == "held"
        assert verdict.held_reason == "ci_pending"
        assert not is_self_healing_hold(verdict.reason)
        assert verdict.reason == f"ci-pending-held: {checks[1]}"
        # The row (and the held_at the stall clock reads) is untouched: no
        # re-hold that would restart the clock, and no release.
        assert store.holds == [(EPIC_REF, "7", "ci_pending")]
        assert store.clears == []
        assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_a_human_ci_gate_still_releases_the_red_ci_hold() -> None:
    """Awaiting `ci-approved` IS a human gate, so the sticky row goes — that
    release is what spares the operator a `kew epic unhold`, and unlike a
    pending read it is a positive observation, not an unreadable one."""
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("awaiting_approval", "awaiting ci-approved")
    _, run = _make_run(reader=reader)
    store = _FakeStore(holds=[(EPIC_REF, "7", "ci_pending")])

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "skip"
    assert store.clears == ["7"]


# ---------------------------------------------------------------------------
# 2. UNKNOWN mergeability -> transient held, no sticky hold
# ---------------------------------------------------------------------------


def test_unknown_merge_state_is_transient_held_with_no_hold_call() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="UNKNOWN"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "held"
    assert verdict.held_reason is None
    assert verdict.reason == "mergeability-unknown"
    assert store.holds == []
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


# ---------------------------------------------------------------------------
# 3. BLOCKED -> sticky held via hold_epic_node
# ---------------------------------------------------------------------------


def test_blocked_merge_state_is_sticky_held() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="BLOCKED"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "held"
    # The merge state rides on the hold reason (#2303) so a surface can tell a
    # conflict a person must rebase from a base the factory catches up to.
    assert verdict.held_reason == "ci_pending:blocked"
    assert verdict.reason == "ci-blocked"
    assert store.holds == [(EPIC_REF, "7", "ci_pending:blocked")]
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


# ---------------------------------------------------------------------------
# 3b. ...but `ci_pending` is the only reason this pass may write (#2176 rd 2)
# ---------------------------------------------------------------------------


# One honored hold reason per other writer of `epic_holds`. Each says something
# `ci_pending` cannot and clears only on `kew epic unhold`, so the merge pass
# upserting its own reason over one both destroyed the detail an operator needs
# and made a stuck node look like it would clear itself.
_OTHER_WRITERS_HOLDS = [
    "base_integration_conflict: main no longer merges into this PR branch and "
    "the conflicts in src/pkg/__init__.py were not resolved. Resolve them on "
    "the branch, then unhold the node.",
    "budget_preflight: $41.20 observed against a $40.00 cap",
    "sandbox_containment: no image mapped for this runner",
    "review_rounds_exhausted",
]


@pytest.mark.parametrize("merge_state", ["BLOCKED", "DIRTY"])
@pytest.mark.parametrize("existing", _OTHER_WRITERS_HOLDS)
def test_a_non_green_merge_state_never_downgrades_another_writers_hold(
    existing: str, merge_state: str
) -> None:
    """The merge pass owns `ci_pending` and nothing else.

    `hold_epic_node` UPSERTS the reason, so the pass's sticky hold silently
    rewrote whatever reason the node already carried. For the merge-main step
    (#2149) that was fatal in both directions: the conflicting paths the hold
    named were lost, and an unhold-only hold became one the docs describe as
    self-clearing. The verdict is unchanged — the node is still reported held on
    `ci_pending`, so the tick's stall clock and CI recovery behave as before.
    """
    assert holds_node_out_of_frontier(existing)
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status=merge_state))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()
    store.hold_epic_node(EPIC_REF, "7", existing)

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "held"
    assert verdict.held_reason == f"ci_pending:{merge_state.lower()}"
    assert [h.reason for h in store.list_epic_holds(EPIC_REF)] == [existing]
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_a_non_green_merge_state_still_refreshes_its_own_pending_hold() -> None:
    """The guard is about OTHER writers: the pass re-upserts its own reason.

    Re-writing `ci_pending` over `ci_pending` is what keeps `held_at` the start
    of the hold (the run-store preserves it on upsert) while the reason stays
    re-derived from the live merge state every pass.
    """
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="DIRTY"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()
    store.hold_epic_node(EPIC_REF, "7", "ci_pending")

    verdict = _run_one("7", reader, store, run)

    assert verdict.held_reason == "ci_pending:dirty"
    assert store.holds == [
        (EPIC_REF, "7", "ci_pending"),
        (EPIC_REF, "7", "ci_pending:dirty"),
    ]


def test_a_non_green_merge_state_overwrites_a_hold_nothing_honors() -> None:
    """A reason the overlay does NOT route to HELD is not another writer's
    durable hold — it leaves the node dispatchable — so it is not protected."""
    unhonored = "credential_issuer_refused: no token for this runner"
    assert not holds_node_out_of_frontier(unhonored)
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="DIRTY"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()
    store.hold_epic_node(EPIC_REF, "7", unhonored)

    _run_one("7", reader, store, run)

    # `_FakeStore` appends rather than upserting, so the write log is what says
    # whether the pass wrote at all (the real upsert is pinned end-to-end in
    # test_driver_base_integration.py against a live run store).
    assert store.holds[-1] == (EPIC_REF, "7", "ci_pending:dirty")


@pytest.mark.parametrize("existing", _OTHER_WRITERS_HOLDS)
def test_a_red_head_check_never_downgrades_another_writers_hold(
    existing: str,
) -> None:
    """The head-check red path writes the same hold and needs the same guard."""
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    reader.checks = ("failure", "CI failed: lint")
    calls, run = _make_run(reader=reader)
    store = _FakeStore()
    store.hold_epic_node(EPIC_REF, "7", existing)

    verdict = _run_one("7", reader, store, run)

    assert verdict.held_reason == "ci_pending"
    assert [h.reason for h in store.list_epic_holds(EPIC_REF)] == [existing]


# ---------------------------------------------------------------------------
# 4. Unapproved OPEN -> skip, no merge argv
# ---------------------------------------------------------------------------


def test_unapproved_open_pr_is_skipped_no_merge_argv() -> None:
    reader = _FakeReader(
        pr=_pr(state="OPEN", review_decision="REVIEW_REQUIRED", approver_login=None)
    )
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "skip"
    assert verdict.reason == "awaiting-approval"
    assert calls == []
    assert store.holds == []
    assert store.clears == []


# ---------------------------------------------------------------------------
# 5. Forged label never triggers a merge
# ---------------------------------------------------------------------------


def test_forged_label_never_triggers_merge() -> None:
    pr = _pr(
        state="OPEN",
        review_decision=None,
        approver_login=None,
        labels=["ready-to-merge", "auto-mergeable"],
    )
    reader = _FakeReader(pr=pr)
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "skip"
    assert verdict.reason == "awaiting-approval"
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


# ---------------------------------------------------------------------------
# 6. Author self-approval is not verified
# ---------------------------------------------------------------------------


def test_author_self_approval_is_not_verified() -> None:
    pr = _pr(state="OPEN", author_login="carol", approver_login="carol")
    reader = _FakeReader(pr=pr)
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "skip"
    assert verdict.reason == "awaiting-approval"
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


# ---------------------------------------------------------------------------
# 7. merged + issue still open -> gh issue close emitted; still-open -> stuck
# ---------------------------------------------------------------------------


def test_merged_but_issue_open_triggers_issue_close_and_succeeds() -> None:
    reader = _FakeReader(
        pr=_pr(state="OPEN", merge_state_status="CLEAN"), issue_state="OPEN"
    )
    calls, run = _make_run(reader=reader, close_flips_issue=True)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    close_calls = [c for c in calls if c[:3] == ["gh", "issue", "close"]]
    assert close_calls == [
        ["gh", "issue", "close", "7", "--repo", REPO, "--reason", "completed"]
    ]
    assert verdict.action == "merged"


def test_merged_but_issue_stays_open_after_close_is_stuck() -> None:
    reader = _FakeReader(
        pr=_pr(state="OPEN", merge_state_status="CLEAN"), issue_state="OPEN"
    )
    calls, run = _make_run(reader=reader, close_flips_issue=False)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert any(c[:3] == ["gh", "issue", "close"] for c in calls)
    assert verdict.action == "stuck"
    assert verdict.reason == "merged-not-closed"


# ---------------------------------------------------------------------------
# 8. Attestation
# ---------------------------------------------------------------------------


def test_attestation_emitted_on_successful_merge() -> None:
    reader = _FakeReader(
        pr=_pr(
            state="OPEN",
            merge_state_status="CLEAN",
            head_oid="cafef00d",
            approver_login="alice",
        ),
        issue_state="CLOSED",
    )
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action == "merged"
    assert len(store.audit_events) == 1
    event = store.audit_events[0]
    assert event["event"] == "epic_merge"
    assert event["actor_type"] == "human"
    assert event["actor_id"] == "alice"
    assert event["issue_num"] == 7
    assert event["item_id"] == "7"  # addressable by durable item id (v24 column)
    assert event["repo"] == REPO
    assert event["payload"] == {
        "trigger": "approval",
        "actor": "alice",
        "actor_type": "User",
        "permission": "write",
        "sha": "cafef00d",
        "pr": 42,
    }


def test_no_attestation_when_not_merged() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="UNKNOWN"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    _run_one("7", reader, store, run)

    assert store.audit_events == []


# ---------------------------------------------------------------------------
# 9. MERGED / CLOSED-unmerged terminal PR states
# ---------------------------------------------------------------------------


_STRIP_LABEL_ARGV = [
    "gh",
    "issue",
    "edit",
    "7",
    "--repo",
    REPO,
    "--remove-label",
    "pr-pending-review",
]


def test_already_merged_pr_state_is_skip() -> None:
    reader = _FakeReader(pr=_pr(state="MERGED"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict == MergeVerdict("7", "skip", "already-merged-out-of-band")
    assert calls == [
        _STRIP_LABEL_ARGV,
        ["gh", "issue", "close", "7", "--repo", REPO, "--reason", "completed"],
    ]
    assert store.clears == ["7"]


def test_out_of_band_merge_clears_sticky_hold_and_reports_done() -> None:
    """#782: a node held at ci_pending whose PR is hand-merged out-of-band gets
    its hold cleared + label stripped, so the overlay reports DONE (not HELD)."""
    reader = _FakeReader(pr=_pr(state="MERGED"), issue_state="CLOSED")
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict == MergeVerdict("7", "skip", "already-merged-out-of-band")
    assert store.clears == ["7"]
    assert calls == [_STRIP_LABEL_ARGV]
    # With the hold gone, the closed issue overlays as DONE (see
    # test_overlay for the precedence guard); no attestation is emitted.
    assert store.audit_events == []


def test_closed_unmerged_pr_state_is_stuck() -> None:
    reader = _FakeReader(pr=_pr(state="CLOSED"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict == MergeVerdict("7", "stuck", "closed-unmerged")
    assert calls == []
    assert store.clears == []


def test_unread_pr_state_is_stuck_without_merging() -> None:
    reader = _FakeReader(pr={})
    calls, run = _make_run(reader=reader)

    verdict = _run_one("7", reader, _FakeStore(), run)

    assert verdict == MergeVerdict("7", "stuck", "pr-state-unknown")
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in calls)


def test_out_of_band_merge_that_stays_open_is_stuck() -> None:
    reader = _FakeReader(pr=_pr(state="MERGED"), issue_state="OPEN")
    calls, run = _make_run(reader=reader, close_flips_issue=False)

    verdict = _run_one("7", reader, _FakeStore(), run)

    assert verdict == MergeVerdict("7", "stuck", "merged-not-closed")
    assert any(call[:3] == ["gh", "issue", "close"] for call in calls)


# ---------------------------------------------------------------------------
# Guard rails: non-int id, no linked PR
# ---------------------------------------------------------------------------


def test_non_int_node_id_is_stuck() -> None:
    reader = _FakeReader(pr=_pr())
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("epic-root", reader, store, run)

    assert verdict == MergeVerdict("epic-root", "stuck", "non-int-id")
    assert calls == []
    assert reader.pr_facts_calls == []


def test_linear_node_closes_via_tracker_not_gh(monkeypatch: Any) -> None:
    """With a LinearTracker wired, a merged item is closed via ``tracker.close``
    (never ``gh issue close``) and the node id is validated as a Linear id."""

    class _LinearReader:
        """Str-keyed facts reader stand-in (LinearFactsReader shape)."""

        def __init__(self) -> None:
            self.issue_state = "OPEN"

        def issue_facts(self, item_id: str) -> NodeFacts:
            assert item_id == "SFD-7"  # keyed on the item id, not an int
            return _facts(0, state=self.issue_state, pr_number=42)

        def pr_facts(self, pr_number: int) -> dict[str, Any]:
            return _pr(state="OPEN", merge_state_status="CLEAN")

        def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
            return True

        def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
            return "success", "CI green"

        def repository_permission(self, login: str) -> RepositoryPermission:
            return RepositoryPermission(login, "User", "write")

    class _FakeTracker:
        def __init__(self, reader: _LinearReader) -> None:
            self.closed: list[str] = []
            self._reader = reader

        def close(self, item_id: str) -> None:
            self.closed.append(item_id)
            self._reader.issue_state = "CLOSED"

    class _LinearStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.clears_item: list[str] = []

        def clear_epic_hold_item(self, item_id: str) -> None:
            self.clears_item.append(item_id)

        def hold_epic_node_item(self, epic_ref: str, item_id: str, reason: str) -> None:
            self.holds.append((epic_ref, item_id, reason))

    reader = _LinearReader()
    tracker = _FakeTracker(reader)
    calls, run = _make_run(reader=None)
    store = _LinearStore()

    verdicts = merge_pass(
        ["SFD-7"],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        tracker=tracker,
    )

    assert verdicts == [MergeVerdict("SFD-7", "merged", "approved+ci-green")]
    assert tracker.closed == ["SFD-7"]
    assert not any(c[:3] == ["gh", "issue", "close"] for c in calls)
    # The attestation addresses the non-numeric item by its durable item_id
    # (the unchained column) while issue_num stays the chained 0 sentinel.
    event = store.audit_events[0]
    assert event["event"] == "epic_merge"
    assert event["issue_num"] == 0
    assert event["item_id"] == "SFD-7"


def test_no_linked_pr_is_skip() -> None:
    class _NoPrReader(_FakeReader):
        def issue_facts(self, issue_num: int) -> NodeFacts:
            return _facts(issue_num, state="OPEN", pr_number=None)

    reader = _NoPrReader(pr=_pr())
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict == MergeVerdict("7", "skip", "no-pr")
    assert reader.pr_facts_calls == []


def test_pr_facts_fetched_exactly_once_per_node() -> None:
    reader = _FakeReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    _run_one("7", reader, store, run)

    assert reader.pr_facts_calls == [42]


def test_merge_command_racing_external_merge_reconciles_without_attestation() -> None:
    class _RacingReader(_FakeReader):
        def pr_facts(self, pr_number: int) -> dict[str, Any]:
            self.pr_facts_calls.append(pr_number)
            if len(self.pr_facts_calls) == 1:
                return self.pr
            return _pr(state="MERGED")

    reader = _RacingReader(pr=_pr(state="OPEN", merge_state_status="CLEAN"))
    calls, run = _make_run(merge_rc=1, reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict == MergeVerdict("7", "skip", "already-merged-out-of-band")
    assert reader.pr_facts_calls == [42, 42]
    assert store.clears == ["7"]
    assert store.audit_events == []
    assert any(c[:3] == ["gh", "issue", "edit"] for c in calls)


# ---------------------------------------------------------------------------
# 10. Linear terminal-state semantics (#817)
# ---------------------------------------------------------------------------


class _LinearTermReader:
    """Str-keyed facts reader stand-in with a mutable item state + PR facts."""

    def __init__(self, *, pr_state: str, item_state: str = "OPEN") -> None:
        self.issue_state = item_state
        self._pr_state = pr_state
        self.pr_facts_calls: list[int] = []

    def issue_facts(self, item_id: str) -> NodeFacts:
        assert item_id == "SFD-7"  # keyed on the item id, not an int
        return _facts(0, state=self.issue_state, pr_number=42)

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        self.pr_facts_calls.append(pr_number)
        return _pr(state=self._pr_state)

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return True

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return "success", "CI green"


class _LinearTermTracker:
    def __init__(self, reader: _LinearTermReader, *, close_flips: bool = True) -> None:
        self.closed: list[str] = []
        self.reset: list[str] = []
        self._reader = reader
        self._close_flips = close_flips

    def close(self, item_id: str) -> None:
        self.closed.append(item_id)
        if self._close_flips:
            self._reader.issue_state = "CLOSED"

    def reset_to_ready(self, item_id: str) -> dict[str, Any]:
        self.reset.append(item_id)
        return {}


class _LinearTermStore(_FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.clears_item: list[str] = []

    def clear_epic_hold_item(self, item_id: str) -> None:
        self.clears_item.append(item_id)

    def hold_epic_node_item(self, epic_ref: str, item_id: str, reason: str) -> None:
        self.holds.append((epic_ref, item_id, reason))


def _run_one_linear(reader: _LinearTermReader, tracker: Any, store: _FakeStore):
    calls, run = _make_run(reader=None)
    verdicts = merge_pass(
        ["SFD-7"],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        tracker=tracker,
    )
    assert len(verdicts) == 1
    return verdicts[0], calls


def test_linear_out_of_band_merge_closes_item_via_tracker() -> None:
    """#817: a Linear node whose PR is merged out-of-band closes its item via
    ``tracker.close`` (no `Closes #N` auto-close on Linear PRs) — hold/label
    bookkeeping unchanged, no attestation, no ``gh issue close``."""
    reader = _LinearTermReader(pr_state="MERGED")
    tracker = _LinearTermTracker(reader)
    store = _LinearTermStore()

    verdict, calls = _run_one_linear(reader, tracker, store)

    assert verdict == MergeVerdict("SFD-7", "skip", "already-merged-out-of-band")
    assert tracker.closed == ["SFD-7"]  # called once
    assert store.clears_item == ["SFD-7"]  # sticky hold cleared
    assert store.audit_events == []  # no attestation (kew didn't merge)
    assert not any(c[:3] == ["gh", "issue", "close"] for c in calls)
    assert not any(c[:2] == ["gh", "issue"] for c in calls)  # no label edit either


def test_linear_out_of_band_merge_close_failure_is_stuck() -> None:
    """#817: if ``tracker.close`` doesn't take (item never reaches done), the
    verdict is loud (stuck), never a silent skip."""
    reader = _LinearTermReader(pr_state="MERGED")
    tracker = _LinearTermTracker(reader, close_flips=False)
    store = _LinearTermStore()

    verdict, _ = _run_one_linear(reader, tracker, store)

    assert verdict == MergeVerdict("SFD-7", "stuck", "merged-not-closed")
    assert tracker.closed == ["SFD-7"]
    assert store.audit_events == []


def test_round19_linear_closed_unmerged_retains_claim() -> None:
    """Closed PR recovery retains the claim and reports the stuck verdict."""
    reader = _LinearTermReader(pr_state="CLOSED")
    tracker = _LinearTermTracker(reader)
    store = _LinearTermStore()

    verdict, calls = _run_one_linear(reader, tracker, store)

    assert verdict == MergeVerdict("SFD-7", "stuck", "closed-unmerged")
    assert tracker.reset == []
    assert tracker.closed == []
    assert store.clears_item == ["SFD-7"]
    assert not any(c[:2] == ["gh", "issue"] for c in calls)


# ---------------------------------------------------------------------------
# I12 / Task 51: a no-change outcome is a distinct terminal, not a merge.
#
# An authorized no-change advances exactly one thing: the dependency effect a
# person authorized. It has no branch, no PR and no merge commit, so nothing in
# the merge pass may ever read it as delivered work.
# ---------------------------------------------------------------------------


def test_a_no_change_node_earns_no_merge_credit() -> None:
    """A zero-diff outcome has no PR, so the merge pass has nothing to credit."""

    class _NoChangeReader(_FakeReader):
        def issue_facts(self, issue_num: int) -> NodeFacts:
            # Closed by the acknowledged outcome synchronization (I11) — never
            # by a merge.
            return _facts(issue_num, state="CLOSED", pr_number=None)

    reader = _NoChangeReader(pr=_pr(state="MERGED", merge_state_status="CLEAN"))
    calls, run = _make_run(reader=reader)
    store = _FakeStore()

    verdict = _run_one("7", reader, store, run)

    assert verdict.action != "merged"
    assert verdict == MergeVerdict("7", "skip", "no-pr")
    # Nothing was merged, nothing was attested as a merge, and the PR facts of
    # some unrelated branch were never even consulted.
    assert reader.pr_facts_calls == []
    assert store.audit_events == []
    assert calls == []
