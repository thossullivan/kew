"""Task 11 (#980) — THE governed-review merge-pass invariant (N1).

For every policy-covered candidate of a review-enabled BLOCKING epic the merge
pass merges only if the latest verdict is READY, bound to the current head SHA,
with an approval that postdates the verdict. Every other shape is skipped with a
loud, attested reason and NEVER merges. PRE_POLICY nodes (no verdict) are
grandfathered and merge under the pre-existing approval+CI rules.

These are the load-bearing tests: without them, blocking mode is a lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from kew.graph.driver.facts import NodeFacts, RepositoryPermission
from kew.graph.driver.merge import (
    REVIEW_SKIP_HEAD_MOVED,
    REVIEW_SKIP_NOT_READY,
    REVIEW_SKIP_STALE_APPROVAL,
    merge_pass,
)
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, ReviewRound

REPO = "o/r"
BASE = "main"
EPIC_REF = "epic/1"
ITEM = "42"

_VERDICT_AT = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


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


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _FakeReader:
    def __init__(self, *, pr: dict[str, Any]) -> None:
        self.pr = pr
        self.issue_state = "OPEN"

    def issue_facts(self, issue_num: int) -> NodeFacts:
        return _facts(issue_num, state=self.issue_state, pr_number=42)

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        return self.pr

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return pr.get("mergeStateStatus") in {"CLEAN", "HAS_HOOKS"}

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return "success", "CI green"

    def repository_permission(self, login: str) -> RepositoryPermission:
        return RepositoryPermission(login, "User", "write")


@dataclass
class _ReviewStore:
    rounds: list[ReviewRound] = field(default_factory=list)
    holds: list[tuple[str, str, str]] = field(default_factory=list)
    runs: list[Any] = field(default_factory=list)
    clears: list[str] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)

    def hold_epic_node(self, epic_ref: str, item: str, reason: str) -> None:
        self.holds.append((epic_ref, item, reason))

    def clear_epic_hold(self, item: str) -> None:
        self.clears.append(item)

    def append_audit_event(self, **kwargs: Any) -> None:
        self.audit_events.append(kwargs)

    def review_round_count(self, item_id: str) -> int:
        return len([r for r in self.rounds if r.item_id == item_id])

    def latest_review_round(self, item_id: str) -> ReviewRound | None:
        matching = [r for r in self.rounds if r.item_id == item_id]
        return matching[-1] if matching else None

    def list_epic_holds(self, epic_ref: str) -> list[Any]:
        return [
            SimpleNamespace(item_id=item, reason=reason)
            for (e, item, reason) in self.holds
            if e == epic_ref
        ]

    def list_by_item(self, item_id: str) -> list[Any]:
        return [r for r in self.runs if r.item_id == item_id]


def _round(verdict: str, head_sha: str) -> ReviewRound:
    return ReviewRound(
        epic_ref=EPIC_REF,
        item_id=ITEM,
        round=1,
        head_sha=head_sha,
        verdict=verdict,
        sidecar_digest=None,
        recorded_at=_VERDICT_AT,
    )


def _review_run(*, run_kind: str = ADVERSARIAL_REVIEW_RUN_KIND) -> Any:
    """A terminal run row for *ITEM* — the evidence a review was attempted."""
    return SimpleNamespace(item_id=ITEM, run_kind=run_kind)


def _pr(
    *,
    head_oid: str = "head1",
    approver_login: str | None = "alice",
    approved_at: str = "2026-07-23T13:00:00Z",
    merge_state_status: str = "CLEAN",
) -> dict[str, Any]:
    latest_reviews = []
    if approver_login is not None:
        latest_reviews.append(
            {
                "state": "APPROVED",
                "author": {
                    "login": approver_login,
                    "is_bot": False,
                    "__typename": "User",
                },
                "submittedAt": approved_at,
            }
        )
    return {
        "number": 42,
        "state": "OPEN",
        "reviewDecision": "APPROVED" if approver_login else None,
        "mergeStateStatus": merge_state_status,
        "headRefOid": head_oid,
        "commits": [],
        "latestReviews": latest_reviews,
        "author": {"login": "carol"},
    }


def _make_run(reader: _FakeReader | None = None):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _Result:
        calls.append(cmd)
        # A squash merge auto-closes the linked issue via `Closes #N`; mirror
        # that so `_verify_closed` sees CLOSED and the verdict reads "merged".
        if cmd[:3] == ["gh", "pr", "merge"] and reader is not None:
            reader.issue_state = "CLOSED"
        return _Result(returncode=0)

    return calls, fake_run


def _run_one(reader: _FakeReader, store: _ReviewStore, run: Any):
    verdicts = merge_pass(
        [ITEM],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        blocking_review=True,
    )
    assert len(verdicts) == 1
    return verdicts[0]


def _merge_attempted(calls: list[list[str]]) -> bool:
    return any(c[:3] == ["gh", "pr", "merge"] for c in calls)


# --- the four N1 scenarios ------------------------------------------------


def test_held_with_approval_and_green_ci_does_not_merge() -> None:
    # NEEDS_CHANGES verdict (the HELD-rounds-exhausted case): even with a
    # verified approval and green CI, the invariant blocks the merge.
    store = _ReviewStore(rounds=[_round("NEEDS_CHANGES", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "skip"
    assert verdict.reason == REVIEW_SKIP_NOT_READY
    assert not _merge_attempted(calls)


def test_post_verdict_force_push_does_not_merge() -> None:
    # READY verdict bound to head1, but the PR head is now head2 (force-push):
    # verdict void → skip, no merge.
    store = _ReviewStore(rounds=[_round("READY", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head2"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "skip"
    assert verdict.reason == REVIEW_SKIP_HEAD_MOVED
    assert not _merge_attempted(calls)


def test_stale_approval_does_not_merge() -> None:
    # READY verdict, current head, but the approval predates the verdict.
    store = _ReviewStore(rounds=[_round("READY", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1", approved_at="2026-07-23T11:00:00Z"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "skip"
    assert verdict.reason == REVIEW_SKIP_STALE_APPROVAL
    assert not _merge_attempted(calls)


def test_happy_path_ready_current_head_fresh_approval_merges() -> None:
    store = _ReviewStore(rounds=[_round("READY", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1", approved_at="2026-07-23T13:00:00Z"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "merged"
    assert _merge_attempted(calls)


def test_pre_policy_no_rounds_merges_under_old_rules() -> None:
    # No verdict recorded → PRE_POLICY grandfather → merges on approval+CI green.
    store = _ReviewStore(rounds=[])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "merged"
    assert _merge_attempted(calls)


def test_zero_rounds_with_review_hold_is_not_grandfathered() -> None:
    # Finding 2 (defense-in-depth): a node the review subsystem has already HELD
    # (a `review_*` reason) with ZERO recorded rounds is NOT pre-policy — e.g. a
    # review-pool budget refusal that HELDs before writing round #1. It must be
    # blocked, not grandfathered into an unreviewed merge.
    store = _ReviewStore(rounds=[], holds=[(EPIC_REF, ITEM, "review_pool_exhausted")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "skip"
    assert verdict.reason == REVIEW_SKIP_NOT_READY
    assert not _merge_attempted(calls)


def test_zero_rounds_with_a_recorded_review_run_is_not_grandfathered() -> None:
    # #2147 regression: a review ATTEMPT that ended without a round (a sandbox
    # refusal the review stage defers for retry, a launch failure, a reviewer
    # still in flight) leaves a terminal review run row and no `review_*` hold.
    # The review subsystem has demonstrably engaged this node, so it is NOT
    # pre-policy: merging it on approval+CI green would close a blocking-review
    # node with ZERO review verdicts.
    store = _ReviewStore(rounds=[], runs=[_review_run()])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "skip"
    assert verdict.reason == REVIEW_SKIP_NOT_READY
    assert not _merge_attempted(calls)


def test_zero_rounds_with_only_implementation_runs_still_grandfathers() -> None:
    # The disqualifier is a REVIEW run, not any run: every node has dispatch
    # runs, so keying on those would retire the PRE_POLICY grandfather entirely.
    store = _ReviewStore(rounds=[], runs=[_review_run(run_kind="dispatch")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "merged"
    assert _merge_attempted(calls)


def test_zero_rounds_with_non_review_hold_still_grandfathers() -> None:
    # A non-review hold (e.g. a sticky ci_pending) does NOT block the PRE_POLICY
    # grandfather — only `review_*` holds signal review-subsystem involvement.
    store = _ReviewStore(rounds=[], holds=[(EPIC_REF, ITEM, "ci_pending")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "merged"
    assert _merge_attempted(calls)


# --- attestation ----------------------------------------------------------


def test_blocked_merge_is_attested() -> None:
    store = _ReviewStore(rounds=[_round("NEEDS_CHANGES", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    _, run = _make_run(reader)
    _run_one(reader, store, run)
    events = [e for e in store.audit_events if e.get("event") == "review_merge_blocked"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == REVIEW_SKIP_NOT_READY


# --- non-blocking / advisory: invariant NOT enforced ----------------------


def test_advisory_mode_does_not_enforce_invariant() -> None:
    # blocking_review=False (advisory or non-review epic): a NEEDS_CHANGES round
    # does NOT block an approved+green merge. An attested NEEDS_CHANGES verdict
    # on the head does (the merge verdict rule, #3196); this store holds none.
    store = _ReviewStore(rounds=[_round("NEEDS_CHANGES", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdicts = merge_pass(
        [ITEM],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        blocking_review=False,
    )
    assert verdicts[0].action == "merged"
    assert _merge_attempted(calls)


def test_advisory_degraded_with_green_checks_and_approval_merges() -> None:
    store = _ReviewStore(rounds=[_round("DEGRADED", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdicts = merge_pass(
        [ITEM],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        blocking_review=False,
    )
    assert verdicts[0].action == "merged"
    assert _merge_attempted(calls)


def test_advisory_degraded_with_unstable_merge_state_never_merges() -> None:
    store = _ReviewStore(rounds=[_round("DEGRADED", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1", merge_state_status="UNSTABLE"))
    calls, run = _make_run(reader)
    verdicts = merge_pass(
        [ITEM],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        blocking_review=False,
    )
    assert verdicts[0].action == "held"
    assert not _merge_attempted(calls)


def test_advisory_degraded_without_approval_never_merges() -> None:
    store = _ReviewStore(rounds=[_round("DEGRADED", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1", approver_login=None))
    calls, run = _make_run(reader)
    verdicts = merge_pass(
        [ITEM],
        repo=REPO,
        base=BASE,
        epic_ref=EPIC_REF,
        reader=reader,
        approvers=["alice"],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=run,
        blocking_review=False,
    )
    assert verdicts[0].action == "skip"
    assert verdicts[0].reason == "awaiting-approval"
    assert not _merge_attempted(calls)


def test_blocking_degraded_never_merges() -> None:
    store = _ReviewStore(rounds=[_round("DEGRADED", "head1")])
    reader = _FakeReader(pr=_pr(head_oid="head1"))
    calls, run = _make_run(reader)
    verdict = _run_one(reader, store, run)
    assert verdict.action == "skip"
    assert verdict.reason == REVIEW_SKIP_NOT_READY
    assert not _merge_attempted(calls)


# --- signal classification: invariant skips read AWAITING_MERGES ----------


def test_review_block_reasons_classify_as_awaiting_merge() -> None:
    # Every invariant skip is a human-gate wait (re-review / re-approval), NOT a
    # stall — so it must fold into the AWAITING_MERGES signal, not STALLED.
    from kew.graph.driver.merge import MergeVerdict
    from kew.graph.driver.tick import _is_awaiting_merge

    for reason in (
        REVIEW_SKIP_NOT_READY,
        REVIEW_SKIP_HEAD_MOVED,
        REVIEW_SKIP_STALE_APPROVAL,
        "awaiting-approval",
    ):
        assert _is_awaiting_merge(MergeVerdict(ITEM, "skip", reason)) is True


def test_non_review_skips_are_not_awaiting_merge() -> None:
    from kew.graph.driver.merge import MergeVerdict
    from kew.graph.driver.tick import _is_awaiting_merge

    assert _is_awaiting_merge(MergeVerdict(ITEM, "skip", "no-pr")) is False
    assert _is_awaiting_merge(MergeVerdict(ITEM, "stuck", "merge-failed")) is False
    assert _is_awaiting_merge(MergeVerdict(ITEM, "held", "ci-blocked")) is False


def test_a_standing_verdict_skip_is_awaiting_merge_and_a_broken_ledger_is_not() -> None:
    """The merge verdict rule's NEEDS_CHANGES / NEEDS_FULL skips wait on a
    re-review (#3196); an unknown verdict or an unreadable ledger is a broken
    pipeline an operator must fix, which STALLED says."""
    from kew.graph.driver.merge import MergeVerdict
    from kew.graph.driver.tick import _is_awaiting_merge

    for reason in ("merge-verdict-needs-changes", "merge-verdict-needs-full"):
        assert _is_awaiting_merge(MergeVerdict(ITEM, "skip", reason)) is True
    for reason in (
        "merge-verdict-unknown-verdict",
        "merge-verdict-unreadable-ledger",
        "merge-verdict-corrupt-verdict",
    ):
        assert _is_awaiting_merge(MergeVerdict(ITEM, "skip", reason)) is False
