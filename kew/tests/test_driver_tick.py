"""Scripted-timeline tests for the tick orchestrator `advance()` (Task 10).

Every seam is a fake except the run-store (a real `SqliteRunStore` on a tmp
db, per the task brief) and `remote_branch_exists`, which is monkeypatched
to avoid a real `git ls-remote` network call.

Shared fakes/fixtures live in `tests/support/driver_fakes.py` (extracted for
#454 Task A3 so `test_driver_tick_journal.py` can reuse them).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from support import driver_fakes as _fx
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.driver_fakes import (
    EPIC,
    NOW,
    REPO,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _config,
    _facts,
    _node,
)
from support.pr_batch import is_pr_batch, pr_batch_result

from kew.audit_chain import verify_chain
from kew.config import (
    DispatchConfig,
    EpicConfig,
    HumanReviewConfig,
    ParallelConfig,
)
from kew.graph.adapters.linear_facts import LinearFactsReader
from kew.graph.build import build_graph
from kew.graph.domain import Edge, NodeStatus
from kew.graph.driver.attention import AttentionKind, attention_events
from kew.graph.driver.facts import GitHubFactsReader, NodeFacts
from kew.graph.driver.lock import single_driver_lock
from kew.graph.driver.merge import (
    CI_PENDING_VERDICT_PREFIX,
    MERGEABILITY_UNKNOWN_REASON,
    MergeVerdict,
)
from kew.graph.driver.merge_queue import MERGE_VERIFY_NO_BRANCH
from kew.graph.driver.overlay import DriverStatus, overlay_status
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import (
    EmptyEpicGraphError,
    _awaiting_publication_reason,
    _classify_verdicts,
    _pending_implementation_owners,
    _PreexistingWorktreeRefusal,
    _publication_identity,
    _register_preexisting_dirty_worktree,
    _repair_closes_markers,
    _review_live,
    advance,
    summarize,
)
from kew.ids import branch_slug
from kew.no_change import (
    ALREADY_SATISFIED_CONTRACT_VERSION,
    CLOSE_IF_HOST_VERIFIED,
    COMPLETED,
    EFFECT_ACKED,
    EFFECT_INTENT,
    AlreadySatisfiedContract,
    NamedCheck,
    NoChangeEvidence,
    NoChangeIntent,
)
from kew.publication_job import (
    REVIEW_REMOTE_AVAILABLE,
    RemoteReviewObservation,
    ReviewObligation,
    unavailable_review_observation,
)
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    CI_FIX_RUN_KIND,
    EpicHold,
    RunRecord,
    SqliteRunStore,
)
from kew.service.verbs import epic_retry

# Re-exported by assignment (not `from x import y`) so ruff's F811 doesn't
# mistake the pytest-fixture-as-parameter pattern for a real redefinition.
store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths


class _BodyRepairFacts:
    def __init__(self, body: str | None) -> None:
        self.body = body
        self.calls: list[list[str]] = []

    def run(self, command: list[str], **_kwargs: Any) -> Any:
        self.calls.append(command)
        if command[3] == "GET":
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": json.dumps({"body": self.body})},
            )()
        self.body = command[-1].removeprefix("body=")
        return type("Result", (), {"returncode": 0, "stdout": ""})()


def test_an_unreadable_pr_body_says_why(caplog: pytest.LogCaptureFixture) -> None:
    # The None return is "not observable"; it must not be a silent default.
    from kew.graph.driver.tick import _read_pr_body

    def boom(cmd: list[str], **kwargs: object) -> None:
        raise OSError("gh vanished")

    with caplog.at_level("WARNING"):
        assert _read_pr_body("o/r", 7, boom) is None
        assert _read_pr_body("o/r", 8, lambda *a, **k: _NoBody()) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("PR #7 body unreadable: gh vanished" in m for m in messages)
    assert any("PR #8 body missing" in m for m in messages)


class _NoBody:
    returncode = 0
    stdout = "{}"


def test_tick_repairs_a_missing_marker_once(caplog: pytest.LogCaptureFixture) -> None:
    facts = _BodyRepairFacts("Details")
    node_facts = {
        "42": NodeFacts(
            issue_num=42,
            state="OPEN",
            state_reason=None,
            labels=frozenset(),
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            review_decision=None,
            merge_state_status=None,
            head_oid="head",
            pr_pushed_at=None,
            pr_state="OPEN",
            pr_link_source="ledger",
            pr_has_closing_ref=False,
        )
    }

    _repair_closes_markers(node_facts, facts=facts, repo="o/r")

    assert facts.body == "Details\n\nCloses #42"
    assert len(facts.calls) == 2
    assert "#42: PR #7 lacked a closing keyword; re-appended" in caplog.text


def test_tick_repairs_a_null_body() -> None:
    facts = _BodyRepairFacts(None)
    node_facts = {
        "42": NodeFacts(
            issue_num=42,
            state="OPEN",
            state_reason=None,
            labels=frozenset(),
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            review_decision=None,
            merge_state_status=None,
            head_oid="head",
            pr_pushed_at=None,
            pr_state="OPEN",
            pr_link_source="ledger",
            pr_has_closing_ref=False,
        )
    }

    _repair_closes_markers(node_facts, facts=facts, repo="o/r")

    assert facts.body == "\n\nCloses #42"


@pytest.mark.parametrize(
    ("source", "body"),
    [("closing-ref", "Details"), ("ledger", "Details\n\n<!-- kew:no-autoclose -->")],
)
def test_tick_does_not_repair_hand_made_or_opted_out_prs(
    source: str, body: str
) -> None:
    facts = _BodyRepairFacts(body)
    node_facts = {
        "42": NodeFacts(
            issue_num=42,
            state="OPEN",
            state_reason=None,
            labels=frozenset(),
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            review_decision=None,
            merge_state_status=None,
            head_oid="head",
            pr_pushed_at=None,
            pr_state="OPEN",
            pr_link_source=source,
            pr_has_closing_ref=False,
        )
    }

    _repair_closes_markers(node_facts, facts=facts, repo="o/r")

    expected_calls = 0 if source == "closing-ref" else 1
    assert len(facts.calls) == expected_calls
    assert facts.body == body


def test_tick_skips_body_read_when_closing_reference_is_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    facts = _BodyRepairFacts("Closes #42")
    node_facts = {
        "42": NodeFacts(
            issue_num=42,
            state="OPEN",
            state_reason=None,
            labels=frozenset(),
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            review_decision=None,
            merge_state_status=None,
            head_oid="head",
            pr_pushed_at=None,
            pr_state="OPEN",
            pr_link_source="ledger",
            pr_has_closing_ref=True,
        )
    }

    _repair_closes_markers(node_facts, facts=facts, repo="o/r")

    assert facts.calls == []
    assert "lacked a closing keyword" not in caplog.text


# ---------------------------------------------------------------------------
# 1. Full lifecycle across ticks
# ---------------------------------------------------------------------------


def test_full_lifecycle_root_dispatches_then_dependent_unblocks(
    tmp_path: Path, store: Any
) -> None:
    deps = FakeSource(
        nodes=[_node("1"), _node("2")],
        edges=[Edge("1", "2")],
    )
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN"),
            2: _facts(2, state="OPEN"),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    # Tick 1: root "1" is the only frontier node (its dep-free); "2" is
    # blocked on "1".
    result1 = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )
    assert result1.dispatched == ("1",)
    assert sink.calls == ["1"]
    assert result1.signal == TerminalSignal.PROGRESSING

    # `reserve_epic_node_item` stamps `reserved_at` with real wall-clock time,
    # but this scripted timeline pins every run row to the frozen `NOW`. Pin the
    # reservation into the same clock domain so the reconciler's Task-6 fallback
    # filter (`started_at >= reserved_at`) admits the terminal `run-1` row —
    # otherwise the reservation never releases and "1" never reads DONE (#972
    # review Finding 1; the new resilience tests pin reserved_at the same way).
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), "1"),
    )
    store._conn.commit()

    # Tick 2: the detached child has written its pending run row. Reservation
    # is still live (newest run is "pending") -> "1" stays in-flight, "2"
    # still blocked. No new dispatch.
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="pending",
            exit_code=0,
            started_at=NOW,
        )
    )
    result2 = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )
    assert result2.dispatched == ()
    assert result2.in_flight == ("1",)
    assert sink.calls == ["1"]  # not re-dispatched
    assert result2.signal == TerminalSignal.PROGRESSING

    # Tick 3: the run finished successfully and the issue is closed as
    # COMPLETED (merged, in this simplified lifecycle no PR gate is
    # exercised — see the CANCELLED/HELD tests for the PR-gated path).
    # The reservation reconciles away (newest run terminal) and "1" reads
    # DONE, unblocking "2" into the frontier.
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    facts.issue_facts_by_num[1] = _facts(1, state="CLOSED", state_reason="COMPLETED")
    result3 = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )
    assert result3.done == ("1",)
    assert result3.dispatched == ("2",)
    assert sink.calls == ["1", "2"]


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------


def test_second_tick_with_no_external_change_is_a_no_op(
    tmp_path: Path, store: Any
) -> None:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result1 = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )
    assert result1.dispatched == ("1",)
    assert sink.calls == ["1"]

    # No run row written, no facts change -- the atomic reservation alone
    # must block a second dispatch attempt.
    result2 = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )
    assert result2.dispatched == ()
    assert result2.merged == ()
    assert sink.calls == ["1"]  # sink was NOT called again


def test_observed_in_review_node_enters_adversarial_stage_without_round(
    tmp_path: Path, store: Any
) -> None:
    """A provider-observed review state enrolls work with no review bookkeeping.

    This is the path used by PRs created outside the epic driver's dispatch
    completion lifecycle: there is no review reservation or persisted round for
    the overlay to discover, but the provider still reports ``IN_REVIEW``.
    """
    config = _config()
    config.review.adversarial = True
    config.review.blocking = True
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: NodeFacts(
                issue_num=1,
                state="OPEN",
                state_reason=None,
                labels=frozenset(),
                pr_number=7,
                pr_url=None,
                review_decision="APPROVED",
                merge_state_status="CLEAN",
                head_oid="head1",
                pr_pushed_at=None,
            )
        }
    )
    result = _advance(
        deps=FakeSource(
            nodes=[_node("1", status=NodeStatus.IN_REVIEW)],
            edges=[],
        ),
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    assert result.waiting[0].reason == (
        "adversarial_review — fresh-context reviewer in flight"
    )


def test_unread_pr_preserves_ready_verdict_without_new_review_round(
    tmp_path: Path, store: Any
) -> None:
    config = _config()
    config.review.adversarial = True
    config.review.blocking = True
    store.record_review_round(
        EPIC,
        "1",
        head_sha="head1",
        verdict="READY",
        sidecar_digest="digest",
    )
    node_facts = NodeFacts(
        issue_num=1,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=77,
        pr_url="https://github.com/o/r/pull/77",
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
        pr_state="UNKNOWN",
        pr_link_source="ledger",
        pr_has_closing_ref=False,
    )
    facts = FakeFactsReader(issue_facts_by_num={1: node_facts})
    deps = FakeSource(nodes=[_node("1", status=NodeStatus.IN_REVIEW)], edges=[])
    graph = build_graph(deps, EPIC)

    summary = summarize(EPIC, graph, config=config, store=store, facts=facts, now=NOW)
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    assert summary.counts == {"in_review": 1}
    assert store.review_round_count("1") == 1
    latest = store.latest_review_round("1")
    assert latest is not None
    assert latest.verdict == "READY"
    assert latest.head_sha == "head1"
    assert [(stuck.node_id, stuck.reason) for stuck in result.stuck] == [
        ("1", "pr-state-unknown")
    ]


def test_external_pr_reconciles_publication_and_continuation_before_overlay(
    tmp_path: Path, store: Any
) -> None:
    now = NOW
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="awaiting_publication",
            exit_code=0,
            started_at=now,
            finished_at=now,
            error_category="publication_unavailable",
        )
    )
    store.record_continuation(
        EPIC,
        "1",
        "run-publication",
        "agent/issue-1",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )

    def run(command: list[str], **_kwargs: Any) -> Any:
        prs: list[Any] = [
            {
                "number": 77,
                "url": "https://github.com/o/r/pull/77",
                "state": "OPEN",
                "headRefName": "agent/issue-1",
                "reviewDecision": None,
                "mergeStateStatus": "BLOCKED",
                "headRefOid": "abc",
                "commits": [],
                "latestReviews": [],
                "author": {"login": "agent"},
            }
        ]
        if is_pr_batch(command):
            return pr_batch_result(command, prs)
        if command[:3] == ["gh", "pr", "list"]:
            payload: Any = prs
        elif command[:3] == ["gh", "issue", "view"]:
            payload = {
                "number": 1,
                "state": "OPEN",
                "stateReason": None,
                "labels": [],
                "closedByPullRequestsReferences": [],
            }
        elif command[:4] == ["gh", "api", "--method", "GET"]:
            payload = {"body": "Closes #1"}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(payload)})()

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=GitHubFactsReader("o/r", store=store, run=run),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert result.dispatched == ()
    assert result.waiting[0].reason == "in_review — awaiting merge (PR #77)"
    assert store.get("run-publication").status == "success"
    assert store.list_continuations(EPIC)[0].state == "terminal"


def test_keywordless_branch_link_writes_only_the_implementation_run(
    tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[list[str]] = []
    issue = {
        "number": 1,
        "state": "OPEN",
        "stateReason": None,
        "labels": [],
        "closedByPullRequestsReferences": [],
    }
    pr = {
        "number": 77,
        "url": "https://github.com/o/r/pull/77",
        "state": "OPEN",
        "headRefName": "agent/issue-1",
        "reviewDecision": None,
        "mergeStateStatus": "BLOCKED",
        "headRefOid": "head1",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"]:
            payload: Any = issue
        elif is_pr_batch(command):
            return pr_batch_result(command, [pr])
        elif command[:3] == ["gh", "pr", "list"]:
            payload = [pr]
        elif command[:3] == ["gh", "pr", "view"]:
            payload = pr
        elif command[:4] == ["gh", "api", "--method", "GET"]:
            payload = {"body": "Details"}
        elif command[:4] == ["gh", "api", "--method", "PATCH"]:
            payload = {}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    store.save(
        RunRecord(
            run_id="implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    store.save(
        RunRecord(
            run_id="reviewer",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW + timedelta(seconds=1),
            finished_at=NOW + timedelta(seconds=1),
            run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        )
    )
    store.save(
        RunRecord(
            run_id="ci-fix",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="failure",
            exit_code=1,
            started_at=NOW + timedelta(seconds=2),
            finished_at=NOW + timedelta(seconds=2),
            run_kind=CI_FIX_RUN_KIND,
        )
    )
    reader = GitHubFactsReader("o/r", store=store, host="github.com", run=run)
    reader.prepare(["1"])
    observed = reader.issue_facts(1)

    assert observed.pr_number == 77
    assert observed.pr_link_source == "branch"
    assert observed.pr_state == "OPEN"
    assert (
        overlay_status(observed, [], reserved=False, held_reason=None)
        is DriverStatus.IN_REVIEW
    )

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=reader,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert result.dispatched == ()
    assert store.get("implementation").pr_url == pr["url"]
    assert store.get("reviewer").pr_url is None
    assert store.get("ci-fix").pr_url is None
    api_calls = [
        command
        for command in calls
        if command[:2] == ["gh", "api"] and not is_pr_batch(command)
    ]
    assert api_calls == [
        ["gh", "api", "--method", "GET", "repos/o/r/pulls/77"],
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            "repos/o/r/pulls/77",
            "-f",
            "body=Details\n\nCloses #1",
        ],
    ]
    assert "#1: PR #77 lacked a closing keyword; re-appended" in caplog.text


def test_ci_fix_does_not_replace_awaiting_publication_reason(store: Any) -> None:
    store.save(
        RunRecord(
            run_id="implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="awaiting_publication",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
            error_category="implementation-publish-failed",
        )
    )
    store.save(
        RunRecord(
            run_id="ci-fix",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="failure",
            exit_code=1,
            started_at=NOW + timedelta(seconds=1),
            finished_at=NOW + timedelta(seconds=1),
            run_kind=CI_FIX_RUN_KIND,
        )
    )

    reason = _awaiting_publication_reason("1", store, None)

    assert "implementation-publish-failed" in reason
    assert "implementation" in reason


def test_pending_ci_fix_is_not_an_implementation_owner(store: Any) -> None:
    store.save(
        RunRecord(
            run_id="ci-fix",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            run_kind=CI_FIX_RUN_KIND,
        )
    )

    owners = _pending_implementation_owners(
        store,
        _config(),
        item_id="1",
        expected_branch="agent/issue-1",
        liveness=lambda _run: True,
    )

    assert owners == []


@pytest.mark.parametrize(
    ("hold_reason", "expected_hold", "expected_stuck"),
    [
        # The released node is not dispatched over its closed PR: it is stuck
        # closed-unmerged until an operator restarts it (#2689).
        ("ci_pending", None, {"1": "closed-unmerged: PR #77 closed without merging"}),
        # Still held: the merge pass's own verdict, not the restart prompt.
        ("review_pr_closed", "review_pr_closed", {"1": "closed-unmerged"}),
    ],
)
def test_closed_ledger_pr_reconciles_only_ci_pending(
    tmp_path: Path,
    store: Any,
    hold_reason: str,
    expected_hold: str | None,
    expected_stuck: dict[str, str],
) -> None:
    calls: list[list[str]] = []
    issue = {
        "number": 1,
        "state": "OPEN",
        "stateReason": None,
        "labels": [{"name": "pr-pending-review"}],
        "closedByPullRequestsReferences": [],
    }
    pr = {
        "number": 77,
        "url": "https://github.com/o/r/pull/77",
        "state": "CLOSED",
        "headRefName": "agent/issue-1",
        "reviewDecision": None,
        "mergeStateStatus": "CLEAN",
        "headRefOid": "head1",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"]:
            payload: Any = issue
        elif is_pr_batch(command):
            return pr_batch_result(command, [pr])
        elif command[:3] == ["gh", "pr", "list"]:
            payload = [pr]
        elif command[:3] == ["gh", "pr", "view"]:
            payload = pr
        elif command[:3] == ["gh", "issue", "edit"]:
            payload = {}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    store.save(
        RunRecord(
            run_id="implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            pr_url=pr["url"],
        )
    )
    store.hold_epic_node(EPIC, 1, hold_reason)
    reader = GitHubFactsReader("o/r", store=store, host="github.com", run=run)

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=reader,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    holds = {hold.item_id: hold.reason for hold in store.list_epic_holds(EPIC)}
    assert holds.get("1") == expected_hold
    assert result.dispatched == ()
    assert {s.node_id: s.reason for s in result.stuck} == expected_stuck
    assert any(command[:3] == ["gh", "issue", "edit"] for command in calls)
    if expected_hold is not None:
        tick = store.latest_epic_tick(EPIC)
        assert tick is not None
        statuses = {node["id"]: node["status"] for node in tick.snapshot["nodes"]}
        assert statuses["1"] == "held"


def test_merged_live_run_closes_issue_then_reads_done_next_tick(
    tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[list[str]] = []
    issue_state = "OPEN"
    current_pr = 70
    prs = {
        70: {
            "number": 70,
            "url": "https://github.com/o/r/pull/70",
            "state": "MERGED",
            "headRefName": "agent/issue-1",
            "reviewDecision": None,
            "mergeStateStatus": "CLEAN",
            "headRefOid": "merged-head",
            "commits": [],
            "latestReviews": [],
            "author": {"login": "agent"},
        },
        77: {
            "number": 77,
            "url": "https://github.com/o/r/pull/77",
            "state": "OPEN",
            "headRefName": "agent/issue-1",
            "reviewDecision": None,
            "mergeStateStatus": "BLOCKED",
            "headRefOid": "orphan-head",
            "commits": [],
            "latestReviews": [],
            "author": {"login": "agent"},
        },
    }

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal issue_state
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"]:
            payload: Any = {
                "number": 1,
                "state": issue_state,
                "stateReason": "COMPLETED" if issue_state == "CLOSED" else None,
                "labels": [],
                "closedByPullRequestsReferences": [],
            }
        elif is_pr_batch(command):
            return pr_batch_result(command, [prs[current_pr]])
        elif command[:3] == ["gh", "pr", "list"]:
            payload = [prs[current_pr]]
        elif command[:3] == ["gh", "pr", "view"]:
            payload = prs[int(command[3])]
        elif command[:3] == ["gh", "issue", "edit"]:
            payload = {}
        elif command[:3] == ["gh", "issue", "close"]:
            issue_state = "CLOSED"
            payload = {}
        elif command[:4] == ["gh", "api", "--method", "GET"]:
            payload = {"body": "Closes #1"}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    store.save(
        RunRecord(
            run_id="live-implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="pending",
            exit_code=0,
            started_at=NOW,
        )
    )
    reader = GitHubFactsReader("o/r", store=store, host="github.com", run=run)
    deps = FakeSource(nodes=[_node("1")], edges=[])

    first = _advance(
        deps=deps,
        facts=reader,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "first.lock",
    )

    assert issue_state == "CLOSED"
    assert first.closed == ("1",)
    assert first.done == ()
    assert first.waiting[0].reason == (
        "merged_unclosed — the merge pass closes the issue on this tick"
    )
    assert "unhold" not in first.waiting[0].reason
    assert any(command[:3] == ["gh", "issue", "close"] for command in calls)

    current_pr = 77
    second = _advance(
        deps=deps,
        facts=reader,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "second.lock",
    )

    assert second.done == ("1",)
    assert "orphaned-pr #77 is open after the issue closed" in caplog.text


def test_open_pr_and_success_override_stale_publication_continuation(
    tmp_path: Path, store: Any
) -> None:
    """The overlay remains live-safe if legacy writes left a split pair."""
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_unavailable",
        )
    )
    store.record_continuation(
        EPIC,
        "1",
        "run-publication",
        "agent/issue-1",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )
    assert store.transition_run_status(
        "run-publication",
        expected_status="awaiting_publication",
        new_status="success",
        error_category="",
        pr_url="https://github.com/o/r/pull/77",
    )

    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)
    summary = summarize(
        EPIC,
        graph,
        config=_config(),
        facts=FakeFactsReader(
            issue_facts_by_num={
                1: _facts(
                    1,
                    state="OPEN",
                    pr_number=77,
                    pr_url="https://github.com/o/r/pull/77",
                )
            }
        ),
        store=store,
        now=NOW,
    )

    assert summary.frontier == ()
    assert summary.counts == {"in_review": 1}
    assert summary.waiting[0].reason == "in_review — awaiting merge (PR #77)"
    assert store.list_continuations(EPIC)[0].state == "awaiting_continuation"


def test_awaiting_publication_frontier_hint_beats_continuation_marker(
    tmp_path: Path, store: Any
) -> None:
    """Publication state is authoritative over its preserved-work handle.

    Both records are expected after a host publication failure, but the next
    operator action is to publish/reconcile the completed branch, not resume
    the implementation agent.
    """
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=1089,
            item_id="1089",
            branch="agent/issue-1089",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_unavailable",
        )
    )
    store.record_continuation(
        EPIC,
        "1089",
        "run-publication",
        "agent/issue-1089",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )

    result = _advance(
        deps=FakeSource(nodes=[_node("1089")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1089: _facts(1089, state="OPEN")}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert result.dispatched == ()
    assert len(result.waiting) == 1
    reason = result.waiting[0].reason
    assert reason.startswith("awaiting_publication")
    assert "publication_unavailable" in reason
    assert "kew finalize" in reason
    assert "recovery of last resort" in reason
    assert "kew epic continue" in reason


def test_external_merged_pr_finalizes_run_and_closes_linear_item(
    tmp_path: Path, store: Any
) -> None:
    class Client:
        completed = False

        def execute(self, _query: str, variables: Any = None) -> dict[str, Any]:
            assert variables is not None
            state_type = "completed" if self.completed else "started"
            return {
                "issue": {
                    "identifier": "SFD-1",
                    "state": {"name": "Done", "type": state_type},
                    "labels": {"nodes": []},
                }
            }

    class Tracker:
        def __init__(self, client: Client) -> None:
            self.client = client
            self.closed: list[str] = []

        def close(self, item_id: str) -> None:
            self.closed.append(item_id)
            self.client.completed = True

    client = Client()
    tracker = Tracker(client)
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_unavailable",
        )
    )
    store.record_continuation(
        EPIC,
        "SFD-1",
        "run-publication",
        "agent/issue-sfd-1",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )

    pr = {
        "number": 77,
        "url": "https://github.com/o/r/pull/77",
        "state": "MERGED",
        "headRefName": "agent/issue-sfd-1",
        "reviewDecision": None,
        "mergeStateStatus": "CLEAN",
        "headRefOid": "abc",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }

    def run(command: list[str], **_kwargs: Any) -> Any:
        assert command[:3] == ["gh", "pr", "list"]
        return type("Result", (), {"returncode": 0, "stdout": json.dumps([pr])})()

    config = _config()
    config.tracker.type = "linear"
    result = _fx.advance(
        EPIC,
        config=config,
        store=store,
        deps=FakeSource(nodes=[_node("SFD-1")], edges=[]),
        facts=LinearFactsReader(client, store, "o/r", run=run),
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )

    assert tracker.closed == ["SFD-1"]
    assert result.closed == ("SFD-1",)
    assert result.done == ("SFD-1",)
    assert store.get("run-publication").status == "success"
    assert store.list_continuations(EPIC)[0].state == "terminal"


# ---------------------------------------------------------------------------
# 3. HELD-on-ci_pending excluded from frontier, signal PROGRESSING
# ---------------------------------------------------------------------------


def test_held_ci_pending_excluded_from_frontier_signal_progressing(
    tmp_path: Path, store: Any
) -> None:
    # "1" is approved but CI-red -> HELD; "2" depends on "1" so it can
    # never be satisfied while "1" sits HELD (not DONE).
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    store.hold_epic_node(EPIC, 1, "ci_pending")
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=42),
            2: _facts(2, state="OPEN"),
        },
        pr_facts_by_num={
            42: {
                "number": 42,
                "state": "OPEN",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "BLOCKED",
                "headRefOid": "deadbeef",
                "author": {"login": "carol"},
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
            }
        },
        permissions_by_login={"alice": "write"},
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert "1" not in result.dispatched
    assert "2" not in result.dispatched  # dependent stays blocked
    assert result.dispatched == ()
    assert "1" in result.held
    assert result.signal == TerminalSignal.PROGRESSING
    assert sink.calls == []


# ---------------------------------------------------------------------------
# 4. CANCELLED does not satisfy dependents
# ---------------------------------------------------------------------------


def test_cancelled_close_does_not_satisfy_dependent(tmp_path: Path, store: Any) -> None:
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="CLOSED", state_reason="NOT_PLANNED"),
            2: _facts(2, state="OPEN"),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.cancelled == ("1",)
    assert "2" not in result.dispatched
    assert result.dispatched == ()
    assert result.done == ()
    # Blast radius is surfaced via the stuck tuple (spec: FAILED/CANCELLED
    # blast radius is reported so an operator sees what a dead node holds).
    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert "1" in stuck_by_id
    assert stuck_by_id["1"].blast_radius == ("2",)
    assert result.signal == TerminalSignal.STALLED


# ---------------------------------------------------------------------------
# 5. FAILED terminal, not re-dispatched, blast radius reported
# ---------------------------------------------------------------------------


def test_failed_terminal_not_redispatched_blast_radius_reported(
    tmp_path: Path, store: Any
) -> None:
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN"),
            2: _facts(2, state="OPEN"),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.failed == ("1",)
    assert result.dispatched == ()
    assert sink.calls == []
    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert stuck_by_id["1"].blast_radius == ("2",)


def test_stale_pending_review_label_after_retry_reenters_frontier(
    tmp_path: Path, store: Any
) -> None:
    # #753: node "1" failed, was retried, and its PR was closed unmerged.
    # The closed PR dropped out of `closedByPullRequestsReferences`
    # (pr_number None) but left a stale `pr-pending-review` label behind.
    # The retry marker supersedes the failure, and the label alone must NOT
    # wedge the node as IN_REVIEW — it re-enters the dispatch frontier.
    deps = FakeSource(nodes=[_node("1")], edges=[])
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    store.mark_epic_retry(EPIC, 1)  # retried_at > NOW supersedes the failure
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(
                1,
                state="OPEN",
                pr_number=None,
                labels=frozenset({"pr-pending-review"}),
            ),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.dispatched == ("1",)
    assert sink.calls == ["1"]


def test_retry_then_advance_preserves_dirty_preexisting_worktree(
    tmp_path: Path, store: Any
) -> None:
    """A retry must not let the next tick destroy an existing dirty checkout."""
    config = _config()
    config.project.repo = "o/r"
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "test"],
        check=True,
        capture_output=True,
    )
    (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "tracked.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    (worktree / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (worktree / "untracked.txt").write_bytes(b"untracked bytes\x00\n")
    before = {
        "tracked": (worktree / "tracked.txt").read_bytes(),
        "untracked": (worktree / "untracked.txt").read_bytes(),
    }
    config.branches.worktree_base = str(tmp_path / "worktrees")
    store.save(
        RunRecord(
            run_id="failed-1010",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    _bind_checkout_base(store, config, worktree, "failed-1010", "1010")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "kew.service.verbs.mark_ready_from_failed",
            lambda *args, **kwargs: None,
        )
        epic_retry(store, epic_ref=EPIC, item_id="1010", config=config)

    class DestructiveChild(FakeSink):
        def dispatch(self, item_id: str) -> bool:
            super().dispatch(item_id)
            shutil.rmtree(worktree)
            return True

    sink = DestructiveChild()
    result = _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    assert result.dispatched == ()
    assert sink.calls == []
    rows = store.list_by_item("1010")
    assert [row.run_id for row in rows] == ["failed-1010"]
    assert (worktree / "tracked.txt").read_bytes() == before["tracked"]
    assert (worktree / "untracked.txt").read_bytes() == before["untracked"]
    continuation = store.list_continuations(EPIC)
    assert len(continuation) == 1
    assert continuation[0].state == "awaiting_continuation"
    evidence = store.list_audit_events(event="continuation_detected")
    assert len(evidence) == 1
    assert evidence[0].payload["worktree_identity"].startswith("sha256:")
    assert str(worktree) not in str(evidence[0].payload)


def test_retry_then_advance_refuses_mismatched_preexisting_worktree(
    tmp_path: Path, store: Any
) -> None:
    config = _config()
    config.project.repo = "o/r"
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1011")
    subprocess.run(
        ["git", "-C", str(worktree), "branch", "-m", "agent/issue-other"],
        check=True,
        capture_output=True,
    )
    marker = worktree / "untrusted.bin"
    marker.write_bytes(b"keep me\x00\n")
    config.branches.worktree_base = str(tmp_path / "worktrees")
    store.save(
        RunRecord(
            run_id="failed-1011",
            issue_num=1011,
            item_id="1011",
            branch="agent/issue-1011",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "kew.service.verbs.mark_ready_from_failed",
            lambda *args, **kwargs: None,
        )
        epic_retry(store, epic_ref=EPIC, item_id="1011", config=config)

    sink = FakeSink()
    result = _advance(
        deps=FakeSource(nodes=[_node("1011")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1011: _facts(1011, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    assert sink.calls == []
    assert any("untrusted identity" in stuck.reason for stuck in result.stuck)
    assert marker.read_bytes() == b"keep me\x00\n"
    assert store.list_epic_reservations(EPIC) == []


@pytest.mark.parametrize("owner_verdict", [True, None])
def test_dirty_continuation_rejects_live_or_unproven_pending_owner(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    owner_verdict: bool | None,
) -> None:
    """Dirty bytes cannot be re-registered while a branch owner is uncertain."""
    config = _config()
    config.project.repo = "o/r"
    _make_dirty_worktree(tmp_path / "worktrees", "1012")
    monkeypatch.setattr(
        "kew.graph.driver.tick._validate_preexisting_worktree",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "kew.graph.driver.tick._checkout_holds_work",
        lambda _config, _path, _branch, **_kwargs: True,
    )
    config.branches.worktree_base = str(tmp_path / "worktrees")
    store.save(
        RunRecord(
            run_id="failed-1012",
            issue_num=1012,
            item_id="1012",
            branch="agent/issue-1012",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    _bind_checkout_base(
        store,
        config,
        tmp_path / "worktrees" / "agent" / "issue-1012",
        "failed-1012",
        "1012",
    )
    store.save(
        RunRecord(
            run_id=f"pending-{owner_verdict}",
            issue_num=1012,
            item_id="1012",
            branch="agent/issue-1012",
            status="pending",
            exit_code=-1,
            started_at=NOW,
        )
    )
    assert store.reserve_epic_node_item(EPIC, "1012", "temporary") is True

    with pytest.raises(_PreexistingWorktreeRefusal, match="pending implementation"):
        _register_preexisting_dirty_worktree(
            store,
            config,
            epic_ref=EPIC,
            item_id="1012",
            provider="github",
            reservation_run_id="temporary",
            liveness=lambda _run: owner_verdict,
        )

    assert store.list_continuations(EPIC) == []
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == ["temporary"]


def test_marker_reload_reloads_retry_and_continuation_between_ticks(
    tmp_path: Path, store: Any
) -> None:
    """A poller tick sees operator markers written after the prior tick.

    Retry and continuation state are durable control-plane inputs, not process
    lifetime state.  The retry re-admits one failed node while the preserved
    worktree marker keeps its sibling out of the dispatch frontier.
    """
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN"),
            2: _facts(2, state="OPEN"),
        }
    )
    for item_id in (1, 2):
        store.save(
            RunRecord(
                run_id=f"run-{item_id}",
                issue_num=item_id,
                branch=f"agent/issue-{item_id}",
                status="failure",
                exit_code=1,
                started_at=NOW,
                finished_at=NOW,
            )
        )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    before = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )
    assert before.failed == ("1", "2")
    assert before.dispatched == ()

    store.mark_epic_retry_item(EPIC, "1")
    store.record_continuation(
        EPIC,
        "2",
        "run-2",
        "agent/issue-2",
        tmp_path / "preserved-2",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved-2"),
    )

    after = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert after.dispatched == ("1",)
    assert after.failed == ()
    assert sink.calls == ["1"]
    waiting = {node.node_id: node.reason for node in after.waiting}
    assert waiting["2"].startswith("awaiting_continuation")


# ---------------------------------------------------------------------------
# 6. STALLED
# ---------------------------------------------------------------------------


def test_stalled_when_frontier_empty_and_nothing_in_flight(
    tmp_path: Path, store: Any
) -> None:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", labels=frozenset({"blocked"}))}
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.dispatched == ()
    assert result.in_flight == ()
    assert result.held == ()
    assert result.remaining == 1
    assert result.signal == TerminalSignal.STALLED


# ---------------------------------------------------------------------------
# 6b. AWAITING_MERGES — PRs awaiting a human merge, distinct from STALLED (I7)
# ---------------------------------------------------------------------------


def test_awaiting_merges_not_stalled(tmp_path: Path, store: Any) -> None:
    """An epic whose only remaining work is a PR open in review awaiting a
    human's merge reports AWAITING_MERGES, not STALLED — the benign human-gate
    end state, not a broken pipeline needing an operator (Node I7)."""
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=100),
            2: _facts(2, state="OPEN"),
        },
        # PR #100 is open in review with no verified approval, so the merge
        # pass returns skip/awaiting-approval: node 1 is awaiting a human merge
        # and node 2 is blocked only on it.
        pr_facts_by_num={100: {"state": "OPEN"}},
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.dispatched == ()
    assert result.in_flight == ()
    assert result.merged == ()
    assert result.stuck == ()
    assert result.remaining == 2
    assert result.signal == TerminalSignal.AWAITING_MERGES


_APPROVED_CLEAN_PR = {
    "number": 100,
    "state": "OPEN",
    "reviewDecision": "APPROVED",
    "mergeStateStatus": "CLEAN",
    "headRefOid": "head100",
    "commits": [],
    "latestReviews": [
        {
            "state": "APPROVED",
            "submittedAt": "2026-07-01T00:00:00Z",
            "author": {"login": "alice", "is_bot": False, "__typename": "User"},
        }
    ],
    "author": {"login": "carol"},
}


@pytest.mark.parametrize(
    "checks",
    [
        (
            "awaiting_approval",
            "awaiting ci-approved: tests did not run (test skipped)",
        ),
        ("no_evidence", "no CI evidence on head: merge by hand"),
        ("no_evidence", "commit statuses green but no check runs: merge by hand"),
    ],
)
def test_a_human_ci_gate_is_awaiting_merges_not_stalled(
    tmp_path: Path, store: Any, checks: tuple[str, str]
) -> None:
    """#2033: an approved CLEAN PR whose tests await ci-approved, or whose head
    carries no CI evidence at all, waits for a human. The epic reports
    AWAITING_MERGES with the reason on its line, never STALLED, and nothing is
    held, so no `kew epic unhold` is needed."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", pr_number=100)},
        pr_facts_by_num={100: dict(_APPROVED_CLEAN_PR)},
        permissions_by_login={"alice": "write"},
        checks=checks,
    )

    for _tick in range(2):
        result = _advance(
            deps=deps,
            facts=facts,
            store=store,
            sink=FakeSink(),
            lock_path=tmp_path / "epic.lock",
        )
        assert result.merged == ()
        assert result.stuck == ()
        assert result.held == ()
        assert result.signal == TerminalSignal.AWAITING_MERGES
        [line] = [w for w in result.waiting if w.node_id == "1"]
        assert line.reason.startswith("in_review")
        assert checks[1] in line.reason
        assert "PR #100" in line.reason
        assert store.list_epic_holds(EPIC) == []


def test_the_tick_that_releases_a_ci_hold_names_the_gate(
    tmp_path: Path, store: Any
) -> None:
    """A node an earlier tick held on ci_pending, whose head now awaits
    ci-approved: this tick releases the hold AND reports the gate reason on
    the node's line, not a bare `held`."""
    gated = "awaiting ci-approved: tests did not run (test skipped)"
    store.hold_epic_node(EPIC, 1, "ci_pending")
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", pr_number=100)},
        pr_facts_by_num={100: dict(_APPROVED_CLEAN_PR)},
        permissions_by_login={"alice": "write"},
        checks=("awaiting_approval", gated),
    )

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert store.list_epic_holds(EPIC) == []
    assert result.held == ()
    assert result.signal == TerminalSignal.AWAITING_MERGES
    [line] = [w for w in result.waiting if w.node_id == "1"]
    assert line.reason == f"in_review — {gated} (PR #100)"


def test_epic_status_names_the_ci_approval_gate(store: Any) -> None:
    """`kew epic status` never merges, but it reads the head checks of an
    approved CLEAN PR and shows the same human gate the merge pass would."""
    import dataclasses

    gated = "awaiting ci-approved: tests did not run (test skipped)"
    node = dataclasses.replace(
        _facts(1, state="OPEN", pr_number=100),
        review_decision="APPROVED",
        merge_state_status="CLEAN",
        head_oid="head100",
    )
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)
    summary = summarize(
        EPIC,
        graph,
        config=_config(),
        facts=FakeFactsReader(
            issue_facts_by_num={1: node},
            pr_facts_by_num={100: dict(_APPROVED_CLEAN_PR)},
            checks=("awaiting_approval", gated),
        ),
        store=store,
        now=NOW,
    )
    [line] = [w for w in summary.waiting if w.node_id == "1"]
    assert line.reason == f"in_review — {gated} (PR #100)"


def test_true_stall_still_stalled(tmp_path: Path, store: Any) -> None:
    """A genuinely blocked frontier — no dispatch, nothing in-flight, and no PR
    awaiting merge (a blocked-label node that needs an operator) — still
    reports STALLED, never AWAITING_MERGES (Node I7)."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", labels=frozenset({"blocked"}))}
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.dispatched == ()
    assert result.in_flight == ()
    assert result.remaining == 1
    assert result.signal == TerminalSignal.STALLED


# ---------------------------------------------------------------------------
# 7. COMPLETE
# ---------------------------------------------------------------------------


def test_complete_when_every_node_done(tmp_path: Path, store: Any) -> None:
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="CLOSED", state_reason="COMPLETED"),
            2: _facts(2, state="CLOSED", state_reason="COMPLETED"),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert set(result.done) == {"1", "2"}
    assert result.remaining == 0
    assert result.signal == TerminalSignal.COMPLETE
    assert result.dispatched == ()


# ---------------------------------------------------------------------------
# 7b. EMPTY graph errors — never COMPLETE 0/0 (#819)
# ---------------------------------------------------------------------------


def test_empty_graph_raises_and_journals_no_complete(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracker query that resolves ZERO nodes must error, not report
    `COMPLETE done=0 remaining=0`: `advance` raises `EmptyEpicGraphError`
    BEFORE any tick is begun, so no COMPLETE journal row (nor heartbeat state)
    is recorded for work that never existed."""
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    deps = FakeSource(nodes=[], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    with pytest.raises(EmptyEpicGraphError) as exc:
        _advance(deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path)

    assert exc.value.name == EPIC
    assert sink.calls == []
    # No tick journaled and no heartbeat state mutated: the empty epic left no
    # trace that could later read as a completed drive.
    assert store.count_epic_ticks(EPIC) == 0
    assert store.latest_epic_tick(EPIC) is None
    assert store.get_epic_driver_state(EPIC) is None


# ---------------------------------------------------------------------------
# 8. non-int node id never crashes
# ---------------------------------------------------------------------------


def test_non_int_node_id_is_stuck_never_crashes(tmp_path: Path, store: Any) -> None:
    deps = FakeSource(nodes=[_node("ENG-7")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert sink.calls == []
    assert result.dispatched == ()
    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert stuck_by_id["ENG-7"].reason == "non-int-id"


# ---------------------------------------------------------------------------
# 9. locked_out
# ---------------------------------------------------------------------------


def test_locked_out_when_another_driver_holds_the_lock(
    tmp_path: Path, store: Any
) -> None:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    with single_driver_lock(lock_path) as got:
        assert got is True
        result = _advance(
            deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
        )

    assert result.locked_out is True
    assert result.dispatched == ()
    assert sink.calls == []
    assert result.signal == TerminalSignal.PROGRESSING


# ---------------------------------------------------------------------------
# 10. External (out-of-epic) boundary nodes are never dispatched
# ---------------------------------------------------------------------------


def test_external_dep_closed_not_planned_does_not_satisfy_dependent(
    tmp_path: Path, store: Any
) -> None:
    """Drives the same integration point the adapter fix protects: external
    boundary nodes skip the overlay (`_build_overlays` `continue`s on
    `node.external`) and keep whatever raw `NodeStatus` `build_graph`
    resolved for them via `deps.status_of`. Before the adapter fix,
    `GitHubConventionSource._status_from_issue` mapped a CLOSED+NOT_PLANNED
    issue to `NodeStatus.DONE`; this test simulates the now-corrected
    `NodeStatus.BLOCKED` result flowing through `status_of` and asserts the
    driver correctly refuses to let it satisfy dependent "2". (The adapter
    fix itself is regression-tested directly in
    `test_graph_adapter.py::test_status_of_closed_not_planned_is_blocked_not_done`,
    which fails on revert; this test guards the driver's consumption of
    that corrected value.)
    """
    deps = FakeSource(
        nodes=[_node("2")],
        edges=[Edge("1", "2")],
        statuses={"1": NodeStatus.BLOCKED},
    )
    facts = FakeFactsReader(issue_facts_by_num={2: _facts(2, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert "2" not in result.dispatched
    assert result.dispatched == ()
    assert sink.calls == []


def test_external_out_of_epic_node_is_never_dispatched(
    tmp_path: Path, store: Any
) -> None:
    """ "2" is the only epic member; it depends on "1", an out-of-epic
    issue. `build_graph` resolves "1" into an external boundary Node
    (status TODO, i.e. resolvable-but-not-done) so it isn't mistaken for a
    dangling reference. The driver must observe "1" for dependency-
    satisfaction only -- it must NEVER spawn a
    `kew epic _dispatch-node <this-epic> 1` for an issue that isn't part
    of this epic.
    """
    deps = FakeSource(
        nodes=[_node("2")],
        edges=[Edge("1", "2")],
        statuses={"1": NodeStatus.TODO},
    )
    facts = FakeFactsReader(issue_facts_by_num={2: _facts(2, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert "1" not in result.dispatched
    assert 1 not in sink.calls
    # "2" stays blocked: its out-of-epic dependency "1" is TODO, not DONE.
    assert "2" not in result.dispatched
    # The external boundary node "1" is not part of this epic -- it must
    # not inflate `remaining` (only member "2" counts).
    assert result.remaining == 1


# ---------------------------------------------------------------------------
# 11. HELD ci_pending past stall_days degrades to stuck (aged-hold clock)
# ---------------------------------------------------------------------------


def test_held_ci_pending_past_stall_days_degrades_to_stuck(
    tmp_path: Path, store: Any
) -> None:
    """A HELD ci_pending node whose hold has aged past
    `config.epic.stall_days` must degrade to a `StuckNode("...",
    "ci-stalled", ...)` rather than being reported as live/PROGRESSING
    forever. Regression for the held_at-resets-every-tick bug: the old
    `run_store.hold_epic_node` ON CONFLICT clobbered `held_at` on every
    re-hold (which `merge_pass` issues each tick a still-CI-red PR is
    reclassified), so the staleness clock could never advance.
    """
    stall_days = 3
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])

    # Seed the hold, then force its held_at to look like it started
    # stall_days + 7 days ago -- i.e. this tick is a later re-poll of an
    # already-stale hold, not the first hold.
    store.hold_epic_node(EPIC, 1, "ci_pending")
    old_held_at = (NOW - timedelta(days=stall_days + 7)).isoformat()
    store._conn.execute(
        "UPDATE epic_holds SET held_at = ? WHERE item_id = ?", (old_held_at, "1")
    )
    store._conn.commit()

    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=42),
            2: _facts(2, state="OPEN"),
        },
        pr_facts_by_num={
            42: {
                "number": 42,
                "state": "OPEN",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "BLOCKED",
                "headRefOid": "deadbeef",
                "author": {"login": "carol"},
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
            }
        },
        permissions_by_login={"alice": "write"},
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_config(stall_days=stall_days),
    )

    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert "1" in stuck_by_id
    assert stuck_by_id["1"].reason == "ci-stalled"
    assert "1" not in result.held
    assert sink.calls == []


# ---------------------------------------------------------------------------
# 12. Dead-spawn reservation reclaim (a dispatch child that dies before
#     writing its pending run row must not pin a node IN_PROGRESS forever)
# ---------------------------------------------------------------------------


def test_dead_spawn_reservation_with_no_run_rows_past_grace_is_reclaimed(
    tmp_path: Path, store: Any
) -> None:
    """A reservation for issue 42 with NO run rows at all, whose
    `reserved_at` is older than `_RESERVATION_STARTUP_GRACE_SECONDS`, must
    be reclaimed: released from the reservation table, excluded from
    `in_flight`/IN_PROGRESS this tick, and — instead of silently re-admitting
    the same broken spawn forever — failed with a terminal
    `dispatch_child_died` diagnostic keyed to the reservation's run_id (#808),
    so the node surfaces as FAILED and the terminal signal reaches STALLED
    instead of polling forever. Must FAIL (42 wrongly stuck in `in_flight`,
    signal stuck at PROGRESSING forever) if the grace-based reclaim in
    `_reconcile_reservations` is reverted.
    """
    deps = FakeSource(nodes=[_node("42")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    assert store.reserve_epic_node(EPIC, 42, "run-dead") is True
    old_reserved_at = (NOW - timedelta(minutes=20)).isoformat()
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (old_reserved_at, "42"),
    )
    store._conn.commit()

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert store.list_epic_reservations(EPIC) == []
    assert "42" not in result.in_flight
    assert "42" in result.failed
    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert "42" in stuck_by_id
    assert stuck_by_id["42"].reason.startswith("failed-terminal")
    assert "dispatch_child_died" in stuck_by_id["42"].reason
    assert result.signal == TerminalSignal.STALLED

    # The diagnostic is a terminal RunRecord keyed to the reservation's run_id,
    # naming the spawned executable so the operator can see WHICH kew died.
    rows = store.list_by_item("42")
    assert len(rows) == 1
    assert rows[0].run_id == "run-dead"
    assert rows[0].status == "failure"
    assert rows[0].error_category == "dispatch_child_died"
    assert "spawned" in rows[0].title


def test_budget_refused_child_records_refusal_row_not_dead_corpse(
    tmp_path: Path, store: Any
) -> None:
    """O2 (#891): a dispatch child that refuses on the budget preflight writes a
    terminal *refusal* run row (status/category distinct from `failure` AND from
    `dispatch_child_died`) against the driver's reservation before exiting.

    So even when the reservation is aged well past the startup grace — the exact
    window the dead-child reconciler fires in — the tick must (a) NOT synthesize
    a `dispatch_child_died` diagnostic, (b) leave the refusal row intact, and
    (c) classify the node as HELD from the budget hold + row (reservation
    released from the terminal row, never grace-timed out as a silent corpse).
    Must FAIL if the budget refusal leaves no run row (a spurious
    `dispatch_child_died` row appears and the refusal row is missing).
    """
    from kew.budget import BudgetExceeded, BudgetStatus
    from kew.config import BudgetConfig, DispatchConfig, EpicConfig
    from kew.dispatch_core import _refuse_budget

    deps = FakeSource(nodes=[_node("42")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    # A low weekly cap already breached by OTHER issues' spend — the exact
    # incident shape (#886): item 42 itself has spent nothing, but the shared
    # weekly cap refuses it. The over-cap spend is durable so the driver's
    # un-hold re-check keeps the hold across the tick.
    config = DispatchConfig(
        epic=EpicConfig(stall_days=3), budget=BudgetConfig(weekly_usd=15.0)
    )
    store.save(
        RunRecord(
            run_id="run-99",
            issue_num=99,
            item_id="99",
            branch="agent/issue-99",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            estimated_cost_usd=20.0,
        )
    )

    # The driver reserved the node, then spawned the child.
    assert store.reserve_epic_node(EPIC, 42, "run-refused") is True
    # Age the reservation well past the startup grace: with NO run row the
    # dead-child reconciler WOULD fire here — the refusal row is what stops it.
    old_reserved_at = (NOW - timedelta(minutes=20)).isoformat()
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (old_reserved_at, "42"),
    )
    store._conn.commit()

    # The child's execute_dispatch refuses on the weekly budget preflight: this
    # holds the node AND writes the terminal refusal row before the child exits.
    bstatus = BudgetStatus(
        issue_spent_usd=0,
        issue_limit_usd=15.0,
        daily_spent_usd=20.0,
        daily_limit_usd=50.0,
        weekly_spent_usd=20.0,
        weekly_limit_usd=15.0,
    )
    _refuse_budget(
        store,
        config,
        42,
        EPIC,
        "medium",
        BudgetExceeded("weekly", 15.0, 20.0, bstatus),
        0.0,
        item_id="42",
        expected_reservation_run_id="run-refused",
        expected_reservation_generation="run-refused",
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=config,
    )

    # (c) Classified as a budget hold — not re-dispatched, not FAILED, and never
    # surfaced as a dead corpse.
    assert result.dispatched == ()
    assert "42" not in result.failed
    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert "42" not in stuck_by_id
    assert any(
        w.node_id == "42" and w.reason.startswith("budget_preflight")
        for w in result.waiting
    )

    # The terminal refusal row reconciled the reservation (never grace-timeout).
    assert store.list_epic_reservations(EPIC) == []

    rows = store.list_by_item("42")
    # (a) NO dispatch_child_died diagnostic was synthesized by the tick.
    assert all(r.error_category != "dispatch_child_died" for r in rows)
    # (b) The refusal row exists — keyed to the reservation, distinct status.
    assert len(rows) == 1
    assert rows[0].run_id == "run-refused"
    assert rows[0].status == "refused"
    assert rows[0].status != "failure"
    assert rows[0].error_category == "budget_refused"


def test_containment_refused_child_frees_the_slot_and_holds(
    tmp_path: Path, store: Any
) -> None:
    """#1144 review: a containment refusal must free the reserved fleet slot.

    A dispatch whose resolved runner has no container image is refused AFTER
    the driver reserved a worker slot for it. Without a terminal refusal row
    the reservation dangles for the whole reconcile grace at $0 — and because
    the cause is the committed config, that repeats for every affected node on
    every tick. Same contract as the budget refusal (#891): hold the node,
    reconcile the reservation from the row, and stay out of the frontier (no
    re-dispatch, no FAILED latch, no `dispatch_child_died` corpse).
    """
    from kew.config import DispatchConfig, EpicConfig
    from kew.dispatch_core import _refuse_containment
    from kew.sandbox import SandboxImageUnmapped

    deps = FakeSource(nodes=[_node("42")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    config = DispatchConfig(epic=EpicConfig(stall_days=3))

    assert store.reserve_epic_node(EPIC, 42, "run-refused") is True
    old_reserved_at = (NOW - timedelta(minutes=20)).isoformat()
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (old_reserved_at, "42"),
    )
    store._conn.commit()

    _refuse_containment(
        store,
        config,
        42,
        EPIC,
        "simple",
        "opencode",
        SandboxImageUnmapped("Sandbox: runner 'opencode' has no image mapped"),
        0.0,
        item_id="42",
        expected_reservation_run_id="run-refused",
        expected_reservation_generation="run-refused",
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=config,
    )

    # The slot is free NOW — not at grace expiry.
    assert store.list_epic_reservations(EPIC) == []
    # HELD, out of the frontier: never re-dispatched into the same refusal.
    assert result.dispatched == ()
    assert "42" not in result.failed
    assert any(
        w.node_id == "42" and w.reason.startswith("sandbox_containment")
        for w in result.waiting
    )

    rows = store.list_by_item("42")
    assert all(r.error_category != "dispatch_child_died" for r in rows)
    assert len(rows) == 1
    assert rows[0].run_id == "run-refused"
    assert rows[0].status == "refused"
    # Its own category: the budget-keyed delegate paths must not be fooled.
    assert rows[0].error_category == "containment_refused"


def test_fresh_reservation_with_no_run_rows_is_not_reclaimed(
    tmp_path: Path, store: Any
) -> None:
    """A reservation that is fresh (`reserved_at == now`) with no run rows
    yet is a legitimately just-spawned child -- it must NOT be reclaimed
    (the whole point of the grace period)."""
    deps = FakeSource(nodes=[_node("42")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    assert store.reserve_epic_node(EPIC, 42, "run-fresh") is True
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), "42"),
    )
    store._conn.commit()

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert len(store.list_epic_reservations(EPIC)) == 1
    assert result.in_flight == ("42",)
    assert result.signal == TerminalSignal.PROGRESSING
    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert "42" not in stuck_by_id


# ---------------------------------------------------------------------------
# 12b. Orphaned pending-run liveness reaping (#1041): a dispatch that died WITH
#      the epic driver leaves its pending row pinned forever — no terminal row,
#      no reservation release, silently starving admissions.
# ---------------------------------------------------------------------------


_EXITED_OWNER_BIRTHS: dict[int, str] = {}


def _spawn_then_kill_pgid() -> int:
    """A fake dispatch child in its own session (pid == pgid), SIGKILL'd — the
    epic driver that owned it was killed and took the child with it. Returns the
    now-dead pgid the pending run row carries."""
    import os
    import signal
    import subprocess

    proc = subprocess.Popen(["sleep", "100"], start_new_session=True)  # noqa: S607
    from kew.process_groups import process_start_time

    dead_pgid = os.getpgid(proc.pid)
    try:
        birth = process_start_time(proc.pid)
        assert birth is not None, "real owner birth requires host ps access"
        _EXITED_OWNER_BIRTHS[dead_pgid] = birth
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    return dead_pgid


def test_retry_backoff_is_never_reaped(store):
    import os

    from kew.graph.driver.tick import (
        _pending_run_is_dead,
        _reap_orphaned_run,
        _reconcile_dead_pending,
        _reconcile_reservations,
        default_run_liveness,
    )

    _seed_pending_run(store, "1010", "retry", pgid=os.getpgrp())
    store.set_attempt_pgid("retry", 2**31 - 1)
    run = store.get("retry")
    reservation = store.list_epic_reservations(EPIC)[0]
    assert not _pending_run_is_dead(run, default_run_liveness(_config()), now=NOW)
    assert not _reap_orphaned_run(store, _config(), reservation, run, now=NOW)
    _reconcile_dead_pending(store, _config(), reservation, run, now=NOW)
    _reconcile_reservations(EPIC, store, _config(), now=NOW)
    assert store.get("retry").status == "pending"
    assert store.list_epic_reservations(EPIC) == [reservation]
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def test_orphan_reap_kills_surviving_attempt(store, monkeypatch):
    import signal

    monkeypatch.setattr(
        "kew.run_store.process_start_time", lambda pid: "registered birth"
    )
    from kew.graph.driver.tick import (
        _pending_run_is_dead,
        _reap_orphaned_run,
        default_run_liveness,
    )

    _seed_pending_run(store, "1010", "orphan", pgid=2**31 - 1)
    store.set_attempt_pgid("orphan", 12345)
    monkeypatch.setattr(
        "kew.graph.driver.tick._owner_alive", lambda row: row.pgid == 12345
    )

    alive = True
    monkeypatch.setattr("kew.fleet.start_token_matches", lambda *a: True)
    from kew.process_groups import ProcessIdentity

    monkeypatch.setattr("kew.fleet.identity_matches", lambda *a: True)
    monkeypatch.setattr("kew.fleet.retained_identity_matches", lambda *a: True)
    monkeypatch.setattr(
        "kew.fleet.process_identity",
        lambda pid: ProcessIdentity(1, pid, "S", "registered birth", "sh", "token"),
    )
    monkeypatch.setattr(
        "kew.fleet.group_member_identities",
        lambda pgid: {
            pgid: ProcessIdentity(
                1, pgid, "S", "registered birth", "sh", "token"
            ).token()
        },
    )
    monkeypatch.setattr("kew.fleet.recovered_attempt_signallable", lambda *a: True)
    monkeypatch.setattr("kew.fleet.group_alive", lambda pgid: alive)

    def terminate(*args):
        nonlocal alive
        alive = False

    signal_group = MagicMock(side_effect=terminate)
    monkeypatch.setattr("kew.fleet._signal_group", signal_group)
    assert _pending_run_is_dead(
        store.get("orphan"), default_run_liveness(_config()), now=NOW
    )
    assert _reap_orphaned_run(
        store,
        _config(),
        store.list_epic_reservations(EPIC)[0],
        store.get("orphan"),
        now=NOW,
    )
    signal_group.assert_called_once_with(12345, signal.SIGTERM)
    assert store.get("orphan").status == "killed"


def test_orphan_preservation_kills_surviving_attempt(tmp_path, store, monkeypatch):
    import signal

    monkeypatch.setattr(
        "kew.run_store.process_start_time", lambda pid: "registered birth"
    )
    from kew.graph.driver.tick import _reconcile_dead_pending

    config, _worktree, reservation, run = _orphan_recovery_case(store, tmp_path)
    run.pgid = run.owner_pid = 2**31 - 1
    store.save(run)
    store.set_attempt_pgid(run.run_id, 12345)
    monkeypatch.setattr(
        "kew.graph.driver.tick._owner_alive", lambda row: row.pgid == 12345
    )
    monkeypatch.setattr(
        "kew.graph.driver.tick._checkout_holds_work", lambda *a, **k: True
    )

    alive = True
    monkeypatch.setattr("kew.fleet.start_token_matches", lambda *a: True)
    from kew.process_groups import ProcessIdentity

    monkeypatch.setattr("kew.fleet.identity_matches", lambda *a: True)
    monkeypatch.setattr("kew.fleet.retained_identity_matches", lambda *a: True)
    monkeypatch.setattr(
        "kew.fleet.process_identity",
        lambda pid: ProcessIdentity(1, pid, "S", "registered birth", "sh", "token"),
    )
    monkeypatch.setattr(
        "kew.fleet.group_member_identities",
        lambda pgid: {
            pgid: ProcessIdentity(
                1, pgid, "S", "registered birth", "sh", "token"
            ).token()
        },
    )
    monkeypatch.setattr("kew.fleet.recovered_attempt_signallable", lambda *a: True)
    monkeypatch.setattr("kew.fleet.group_alive", lambda pgid: alive)

    def terminate(*args):
        nonlocal alive
        alive = False

    signal_group = MagicMock(side_effect=terminate)
    monkeypatch.setattr("kew.fleet._signal_group", signal_group)

    _reconcile_dead_pending(store, config, reservation, run, now=NOW)

    signal_group.assert_called_once_with(12345, signal.SIGTERM)
    assert store.get(run.run_id).error_category == "orphaned_pending_preserved"


@pytest.mark.parametrize("checkout", ["missing", "clean"])
def test_reap_recheck_keeps_pending_reservation(tmp_path, store, monkeypatch, checkout):
    from kew.graph.driver.tick import _reconcile_dead_pending

    if checkout == "clean":
        config, _worktree, reservation, run = _orphan_recovery_case(store, tmp_path)
        monkeypatch.setattr(
            "kew.graph.driver.tick._checkout_holds_work", lambda *a, **k: False
        )
    else:
        config = _config()
        _seed_pending_run(store, "1010", "orphan-run", pgid=111)
        reservation = store.list_epic_reservations(EPIC)[0]
        run = store.get("orphan-run")
    run.pgid = run.owner_pid = 111
    store.save(run, update_ownership=True)
    store.set_attempt_pgid(run.run_id, 222)
    monkeypatch.setattr(
        "kew.graph.driver.tick._owner_alive", MagicMock(side_effect=[False, True])
    )
    signal_group = MagicMock()
    monkeypatch.setattr("kew.fleet._signal_group", signal_group)

    _reconcile_dead_pending(store, config, reservation, run, now=NOW)

    assert store.get(run.run_id).status == "pending"
    assert store.list_epic_reservations(EPIC) == [reservation]
    signal_group.assert_not_called()
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def _seed_pending_run(
    store: Any,
    item_id: str,
    run_id: str,
    *,
    pgid: int,
    estimated_cost_usd: float = 0.0,
    record: RunRecord | None = None,
    epic_ref: str = EPIC,
) -> None:
    """Reserve the node and write its child's pending run row carrying *pgid*,
    pinned into the frozen `NOW` clock domain (see test_full_lifecycle).

    ``record`` replaces that row for a dispatch shaped differently — the
    container columns are written on INSERT and preserved by the upsert, so a
    contained run has to arrive as the row rather than be saved over one.
    """
    assert store.reserve_epic_node_item(epic_ref, item_id, run_id) is True
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), item_id),
    )
    store._conn.commit()
    if record is not None:
        store.save(record)
        return
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id),
            item_id=item_id,
            branch=f"agent/issue-{item_id}",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=pgid,
            owner_pid=pgid,
            owner_start_time=_EXITED_OWNER_BIRTHS.get(pgid),
            # The attempt exited before this fixture's owner liveness probe.
            attempt_pgid=2**31 - 1,
            estimated_cost_usd=estimated_cost_usd,
        )
    )


# Real child process; a pgid reused by a foreign process under churn reads
# as alive. Serial phase: tests/conftest.py.
@pytest.mark.serial
def test_orphaned_pending_run_reaped_release_and_starved_node_dispatches(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1041: a pending run whose owning process died with the epic driver is
    reaped (marked killed + attested), its reservation released, and a ready
    node the stale run's worker slot was starving dispatches within the SAME
    reconcile pass.

    The incident fixture: kill -9 a fake driver mid-dispatch, relaunch, assert
    reap + release + starved-node dispatch in one tick. Exercises the DEFAULT
    pgid liveness probe (no injection) so the fix is proven on the production
    path. Must FAIL if the pending-but-dead reap is removed (2020 stays starved
    at the cap and 1010 pins its reservation forever)."""
    # 1010: orphaned in-flight. 2020: ready, dep-free, starved by the fleet cap.
    deps = FakeSource(nodes=[_node("1010"), _node("2020")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1010: _facts(1010, state="OPEN"),
            2020: _facts(2020, state="OPEN"),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)

    _seed_pending_run(
        store,
        "1010",
        "run-1010",
        pgid=_spawn_then_kill_pgid(),
        estimated_cost_usd=2.50,
    )

    # Fleet cap = 1 (default): the lone stale pending run consumes the only slot.
    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    # 1010 reaped: its pending row is now terminal, so the node surfaces FAILED.
    rows = store.list_by_item("1010")
    assert len(rows) == 1
    assert rows[0].status == "killed"
    assert rows[0].error_category == "orphaned_pending_reaped"
    assert "1010" in result.failed
    # Reservation released (only 2020's fresh dispatch reservation remains).
    assert "1010" not in {r.item_id for r in store.list_epic_reservations(EPIC)}
    # The starved ready node dispatched in the same reconcile pass.
    assert "2020" in result.dispatched
    assert "2020" in sink.calls
    # Never silent: an attested audit event names the epic + reaped item.
    events = store.list_audit_events(event="orphaned_run_reaped")
    assert len(events) == 1
    assert events[0].item_id == "1010"
    assert events[0].payload["epic_ref"] == EPIC


def _make_dirty_worktree(base: Path, item_id: str) -> Path:
    """Create the configured worktree shape with an uncommitted WIP file."""
    from kew.worktree import write_checkout_generation

    worktree = base / "agent" / f"issue-{item_id}"
    worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch", f"agent/issue-{item_id}", str(worktree)],
        check=True,
        capture_output=True,
    )
    write_checkout_generation(worktree)
    (worktree / "wip.txt").write_text("preserve me\n", encoding="utf-8")
    return worktree


def _bind_checkout_base(
    store: Any,
    config: DispatchConfig,
    worktree: Path,
    run_id: str,
    item_id: str,
    *,
    base: str = "a" * 40,
) -> None:
    from kew.dispatch_core import _bind_publication_base

    slug = branch_slug(item_id)

    _bind_publication_base(
        store,
        publish=config.publication.publish,
        base=base,
        run_id=run_id,
        issue_num=int(item_id) if item_id.isdigit() else 0,
        item_id=item_id,
        branch=f"agent/issue-{slug}",
        checkout=worktree,
    )


def _save_terminal_run(
    store: Any,
    item_id: str,
    run_id: str,
    *,
    started_at=NOW,
    run_kind: str = "dispatch",
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id),
            item_id=item_id,
            branch=f"agent/issue-{item_id}",
            status="failure",
            exit_code=1,
            run_kind=run_kind,
            started_at=started_at,
            finished_at=started_at,
        )
    )


def _register_bound_candidate(
    store: Any,
    config: DispatchConfig,
    worktree: Path,
    *,
    item_id: str,
    reservation_run_id: str = "reservation",
) -> str:
    from kew.graph.driver.tick import _register_preexisting_dirty_worktree

    assert worktree.is_dir()
    assert store.reserve_epic_node_item(EPIC, item_id, reservation_run_id) is True
    return _register_preexisting_dirty_worktree(
        store,
        config,
        epic_ref=EPIC,
        item_id=item_id,
        provider="github",
        reservation_run_id=reservation_run_id,
        liveness=lambda _run: False,
    )


def _orphan_recovery_case(
    store: Any, tmp_path: Path, *, reopen: bool = False
) -> tuple[DispatchConfig, Path, Any, RunRecord]:
    """Create one canonical dead-run recovery case with real stored identity."""
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    run_id = "orphan-run"
    if reopen:
        worktree = _claim_continuation(store, tmp_path, run_id=run_id)
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=1010,
                item_id="1010",
                branch="agent/issue-1010",
                status="pending",
                exit_code=-1,
                started_at=NOW,
                pgid=0,
                attempt_pgid=2**31 - 1,
            )
        )
    else:
        worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
        _seed_pending_run(store, "1010", run_id, pgid=0)
        _bind_checkout_base(store, config, worktree, run_id, "1010")
    [reservation] = store.list_epic_reservations(EPIC)
    run = store.get(run_id)
    assert run is not None
    return config, worktree, reservation, run


@pytest.mark.parametrize("route", ["terminal", "orphan"])
@pytest.mark.parametrize("comparison_result", [False, True])
@pytest.mark.parametrize("replace_generation", [False, True])
def test_recovery_comparison_revalidates_generation_for_both_results(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    comparison_result: bool,
    replace_generation: bool,
) -> None:
    """A false byte comparison cannot bypass checkout replacement refusal."""
    from kew.graph.driver import tick
    from kew.worktree import read_checkout_generation, write_checkout_generation

    if route == "terminal":
        config = _config()
        config.branches.worktree_base = str(tmp_path / "worktrees")
        worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
        _save_terminal_run(store, "1010", "terminal-run")
        _bind_checkout_base(store, config, worktree, "terminal-run", "1010")

        def action() -> str | None:
            return _register_bound_candidate(store, config, worktree, item_id="1010")

    else:
        config, worktree, reservation, run = _orphan_recovery_case(store, tmp_path)

        def action() -> str | None:
            return tick._reconcile_dead_pending(
                store, config, reservation, run, now=NOW
            )

    generation = read_checkout_generation(worktree)

    def compare(*_args: Any, **_kwargs: Any) -> bool:
        if replace_generation:
            write_checkout_generation(worktree)
        return comparison_result

    comparison = MagicMock(side_effect=compare)
    monkeypatch.setattr(tick, "_checkout_holds_work", comparison)
    if replace_generation:
        with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
            action()
    else:
        outcome = action()
        if route == "terminal":
            assert outcome == ("registered" if comparison_result else "not_registered")
        else:
            recovered = store.get("orphan-run")
            assert recovered is not None
            assert recovered.error_category == (
                "orphaned_pending_preserved"
                if comparison_result
                else "orphaned_pending_reaped"
            )

    comparison.assert_called_once()
    assert (read_checkout_generation(worktree) != generation) == replace_generation
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    if replace_generation:
        assert store.list_continuations(EPIC) == []
        assert store.list_audit_events(event="orphaned_run_reaped") == []
        if route == "orphan":
            assert store.get("orphan-run").status == "pending"


@pytest.mark.parametrize("boundary", ["finalization", "record", "reopen"])
@pytest.mark.parametrize("replace_generation", [False, True])
def test_orphan_handoff_revalidates_generation_after_real_write(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    replace_generation: bool,
) -> None:
    """Each real orphan handoff write retains the checkout generation guard."""
    from kew.graph.driver import tick
    from kew.worktree import read_checkout_generation, write_checkout_generation

    config, worktree, reservation, run = _orphan_recovery_case(
        store, tmp_path, reopen=boundary == "reopen"
    )
    generation = read_checkout_generation(worktree)
    monkeypatch.setattr(tick, "_checkout_holds_work", MagicMock(return_value=True))
    method = {
        "finalization": "finalize_orphaned_pending",
        "record": "record_continuation",
        "reopen": "mark_continuation_awaiting",
    }[boundary]
    transition = getattr(store, method)

    def write_then_replace(*args: Any, **kwargs: Any) -> Any:
        result = transition(*args, **kwargs)
        assert result is not None and result is not False
        if replace_generation:
            write_checkout_generation(worktree)
        return result

    write = MagicMock(side_effect=write_then_replace)
    monkeypatch.setattr(store, method, write)
    if replace_generation:
        with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
            tick._preserve_dirty_orphan(store, config, reservation, run, now=NOW)
    else:
        assert tick._preserve_dirty_orphan(store, config, reservation, run, now=NOW)

    write.assert_called_once()
    assert (read_checkout_generation(worktree) != generation) == replace_generation
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    if replace_generation:
        assert store.list_audit_events(event="continuation_detected") == []
        reservation_ids = [row.run_id for row in store.list_epic_reservations(EPIC)]
        assert reservation_ids == ([] if boundary == "reopen" else [reservation.run_id])
        current = store.get(run.run_id)
        assert current is not None
        assert current.status == "killed"
        records = store.list_continuations(EPIC)
        if boundary == "finalization":
            assert records == []
        else:
            assert len(records) == 1
            assert records[0].state == "awaiting_continuation"
    else:
        [record] = store.list_continuations(EPIC)
        assert record.state == "awaiting_continuation"
        assert store.list_epic_reservations(EPIC) == []


@pytest.mark.parametrize("boundary", ["finalization", "reopen"])
def test_orphan_reopening_revalidates_lost_handoff_result(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """A lost orphan reopen CAS cannot hide checkout replacement."""
    from kew.graph.driver import tick
    from kew.worktree import write_checkout_generation

    config, worktree, reservation, run = _orphan_recovery_case(
        store, tmp_path, reopen=boundary == "reopen"
    )
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: True)

    def lose_after_replacement(*_args: Any, **_kwargs: Any) -> bool:
        write_checkout_generation(worktree)
        return False

    method = (
        "mark_continuation_awaiting"
        if boundary == "reopen"
        else "finalize_orphaned_pending"
    )
    monkeypatch.setattr(store, method, lose_after_replacement)
    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        tick._preserve_dirty_orphan(store, config, reservation, run, now=NOW)

    records = store.list_continuations(EPIC)
    if boundary == "reopen":
        assert records[0].state == "continuing"
    else:
        assert records == []
        assert store.get(run.run_id).status == "pending"
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
        reservation.run_id
    ]
    assert store.list_audit_events(event="continuation_detected") == []


@pytest.mark.parametrize("reopen", [False, True])
def test_dirty_orphan_rejects_multiple_recorded_producers(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    reopen: bool,
) -> None:
    """Fresh and reopened orphans reject every ambiguous bound producer set."""
    from kew.graph.driver import tick

    config, worktree, reservation, run = _orphan_recovery_case(
        store, tmp_path, reopen=reopen
    )
    _save_terminal_run(store, "1010", "second-producer")
    _bind_checkout_base(store, config, worktree, "second-producer", "1010")
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: True)

    with pytest.raises(_PreexistingWorktreeRefusal, match="multiple"):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    current = store.get(run.run_id)
    assert current is not None
    assert current.status == "pending"
    records = store.list_continuations(EPIC)
    assert [record.state for record in records] == (["continuing"] if reopen else [])
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
        reservation.run_id
    ]
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert store.list_audit_events(event="continuation_detected") == []


@pytest.mark.parametrize(
    "run_kind", [ADVERSARIAL_REVIEW_RUN_KIND, CI_FIX_RUN_KIND, "planning"]
)
def test_dirty_continuation_orphan_requires_terminal_implementation_producer(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    run_kind: str,
) -> None:
    """A review run cannot become a reopened continuation's producer."""
    from kew.graph.driver import tick

    config, worktree, reservation, run = _orphan_recovery_case(
        store, tmp_path, reopen=True
    )
    producer = store.get("original-1010")
    assert producer is not None
    producer.run_kind = run_kind
    store.save(producer)
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: True)

    with pytest.raises(_PreexistingWorktreeRefusal, match="producer"):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    [record] = store.list_continuations(EPIC)
    assert record.state == "continuing"
    assert store.get(run.run_id).status == "pending"
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
        reservation.run_id
    ]
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"


@pytest.mark.parametrize("corruption", ["head", "run_branch"])
def test_dirty_orphan_refuses_present_untrusted_candidate_before_reap(
    tmp_path: Path, store: Any, corruption: str
) -> None:
    """A present checkout with malformed identity is not an absent checkout."""
    from kew.graph.driver import tick

    config, worktree, reservation, run = _orphan_recovery_case(store, tmp_path)
    if corruption == "head":
        worktree.joinpath(".git", "HEAD").write_text(
            "ref: refs/heads/unrelated\n", encoding="utf-8"
        )
    else:
        run.branch = "agent/issue-unrelated"
        store.save(run)

    with pytest.raises(_PreexistingWorktreeRefusal, match="untrusted identity"):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    current = store.get(run.run_id)
    assert current is not None
    assert current.status == "pending"
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
        reservation.run_id
    ]
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def test_orphan_terminal_selection_rejects_duplicate_producers_control(
    tmp_path: Path, store: Any
) -> None:
    """The shared terminal selector retains its duplicate-producer refusal."""
    from kew.graph.driver import tick

    config, worktree, _reservation, run = _orphan_recovery_case(
        store, tmp_path, reopen=True
    )
    _save_terminal_run(store, "1010", "second-producer")
    _bind_checkout_base(store, config, worktree, "second-producer", "1010")

    with pytest.raises(_PreexistingWorktreeRefusal, match="multiple"):
        tick._terminal_run_for_preexisting_work(
            store, "1010", run.branch, worktree, config
        )


@pytest.mark.parametrize("replace_checkout", [False, True])
def test_clean_orphan_keeps_checkout_claim_through_reaping(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    replace_checkout: bool,
) -> None:
    """A cooperating replacement cannot enter after a clean comparison."""
    from kew import worktree as worktree_module
    from kew.graph.driver import tick

    config, checkout, reservation, run = _orphan_recovery_case(store, tmp_path)
    generation = worktree_module.read_checkout_generation(checkout)
    preserved = checkout.with_name(f"{checkout.name}-preserved")
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: False)
    acquired = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    observed_claims: list[bool] = []
    threads: list[threading.Thread] = []

    def contend() -> None:
        claimed = False
        try:
            worktree_module.claim_checkout(checkout)
            claimed = True
            observed_claims.append(True)
            if replace_checkout:
                checkout.rename(preserved)
                checkout.mkdir()
                checkout.joinpath(".git").mkdir()
                checkout.joinpath(".git", "HEAD").write_text(
                    "ref: refs/heads/agent/issue-1010\n", encoding="utf-8"
                )
                checkout.joinpath("candidate.txt").write_text(
                    "replacement bytes\n", encoding="utf-8"
                )
                worktree_module.write_checkout_generation(checkout)
            acquired.set()
            assert release.wait(5)
        except BaseException as exc:  # noqa: BLE001 - assertion captures refusal
            failures.append(exc)
            acquired.set()
        finally:
            if claimed:
                worktree_module.release_checkout_claim(checkout)

    reap = tick._reap_orphaned_run

    def reap_with_contender(*args: Any, **kwargs: Any) -> bool:
        thread = threading.Thread(target=contend)
        threads.append(thread)
        thread.start()
        assert acquired.wait(5)
        return reap(*args, **kwargs)

    monkeypatch.setattr(tick, "_reap_orphaned_run", reap_with_contender)
    refusal: BaseException | None = None
    try:
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)
    except (_PreexistingWorktreeRefusal, worktree_module.CheckoutBusy) as exc:
        refusal = exc
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        worktree_module.release_checkout_claim(checkout)

    blocked = len(failures) == 1 and isinstance(
        failures[0], worktree_module.CheckoutBusy
    )
    assert failures == [] or blocked
    assert observed_claims == ([] if blocked else [True])
    current = store.get(run.run_id)
    assert current is not None
    reservations = [row.run_id for row in store.list_epic_reservations(EPIC)]
    events = store.list_audit_events(event="orphaned_run_reaped")
    observed_generation = worktree_module.read_checkout_generation(checkout)
    if blocked:
        assert observed_generation == generation
        assert checkout.joinpath("wip.txt").read_text() == "preserve me\n"
        assert not preserved.exists()
        assert refusal is None
        assert current.error_category == "orphaned_pending_reaped"
        assert reservations == []
        assert len(events) == 1
    elif replace_checkout:
        assert observed_generation != generation
        assert preserved.joinpath("wip.txt").read_text() == "preserve me\n"
        assert checkout.joinpath("candidate.txt").read_text() == "replacement bytes\n"
        assert refusal is not None
        assert current.status == "pending"
        assert reservations == [reservation.run_id]
        assert events == []
    else:
        assert observed_generation == generation
        if refusal is None:
            assert current.error_category == "orphaned_pending_reaped"
            assert reservations == []
            assert len(events) == 1
        else:
            assert isinstance(refusal, worktree_module.CheckoutBusy)
            assert current.status == "pending"
            assert reservations == [reservation.run_id]
            assert events == []


def test_dirty_detection_selects_recorded_checkout_producer(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "recorded-producer")
    _bind_checkout_base(store, config, worktree, "recorded-producer", "1010")
    _save_terminal_run(
        store,
        "1010",
        "review-run",
        started_at=NOW + timedelta(seconds=1),
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
    )
    _bind_checkout_base(store, config, worktree, "review-run", "1010")
    monkeypatch.setattr(
        "kew.graph.driver.tick._checkout_holds_work",
        lambda _config, _path, _branch, **kwargs: kwargs["base_sha"] == "a" * 40,
    )

    outcome = _register_bound_candidate(store, config, worktree, item_id="1010")

    assert outcome == "registered"
    [record] = store.list_continuations(EPIC)
    assert record.original_run_id == "recorded-producer"


def test_dirty_detection_rejects_newer_same_branch_producer(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "bound-older", started_at=NOW - timedelta(days=1))
    _bind_checkout_base(store, config, worktree, "bound-older", "1010")
    _save_terminal_run(store, "1010", "unbound-newer", started_at=NOW)
    monkeypatch.setattr(
        "kew.graph.driver.tick._checkout_holds_work",
        lambda *_args, **_kwargs: True,
    )

    outcome = _register_bound_candidate(store, config, worktree, item_id="1010")

    assert outcome == "registered"
    [record] = store.list_continuations(EPIC)
    assert record.original_run_id == "bound-older"


def test_dirty_detection_never_selects_a_noncanonical_producer_by_recency(
    tmp_path: Path, store: Any
) -> None:
    from kew.graph.driver import tick

    config = _config()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "older", started_at=NOW - timedelta(days=1))
    _save_terminal_run(store, "1010", "newer", started_at=NOW)

    with pytest.raises(_PreexistingWorktreeRefusal, match="multiple terminal"):
        tick._terminal_run_for_preexisting_work(
            store, "1010", "agent/issue-1010", worktree, config
        )


def test_dirty_detection_rejects_missing_publication_base(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "unbound")
    comparison = MagicMock()
    monkeypatch.setattr("kew.graph.driver.tick._checkout_holds_work", comparison)

    with pytest.raises(_PreexistingWorktreeRefusal, match="no terminal.*producer"):
        _register_bound_candidate(store, config, worktree, item_id="1010")

    comparison.assert_not_called()
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    assert store.list_continuations(EPIC) == []


def test_dirty_detection_rejects_conflicting_checkout_generation(
    tmp_path: Path, store: Any
) -> None:
    from kew.worktree import write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "old-generation")
    _bind_checkout_base(store, config, worktree, "old-generation", "1010")
    write_checkout_generation(worktree)

    with pytest.raises(_PreexistingWorktreeRefusal, match="no terminal.*producer"):
        _register_bound_candidate(store, config, worktree, item_id="1010")

    assert worktree.joinpath("wip.txt").is_file()
    assert store.list_continuations(EPIC) == []


def test_dirty_detection_rejects_conflicting_recorded_bases(
    tmp_path: Path, store: Any
) -> None:
    from kew.publication import PublicationError

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "conflicted")
    _bind_checkout_base(store, config, worktree, "conflicted", "1010")
    with pytest.raises(PublicationError, match="conflicting publication bases"):
        _bind_checkout_base(
            store,
            config,
            worktree,
            "conflicted",
            "1010",
            base="b" * 40,
        )

    with pytest.raises(_PreexistingWorktreeRefusal, match="conflicting or unreadable"):
        _register_bound_candidate(store, config, worktree, item_id="1010")

    assert worktree.joinpath("wip.txt").is_file()
    assert store.list_continuations(EPIC) == []


def test_dirty_detection_accepts_identical_repeated_base_bindings(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "repeated")
    for _ in range(2):
        _bind_checkout_base(store, config, worktree, "repeated", "1010")
    monkeypatch.setattr(
        "kew.graph.driver.tick._checkout_holds_work",
        lambda *_args, **_kwargs: True,
    )

    assert (
        _register_bound_candidate(store, config, worktree, item_id="1010")
        == "registered"
    )


def test_dirty_detection_rejects_multiple_matching_producers(
    tmp_path: Path, store: Any
) -> None:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    for run_id in ("producer-a", "producer-b"):
        _save_terminal_run(store, "1010", run_id)
        _bind_checkout_base(store, config, worktree, run_id, "1010")

    with pytest.raises(
        _PreexistingWorktreeRefusal, match="multiple terminal.*producer"
    ):
        _register_bound_candidate(store, config, worktree, item_id="1010")

    assert worktree.joinpath("wip.txt").is_file()
    assert store.list_continuations(EPIC) == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_dirty_orphan_recovery_rejects_missing_or_conflicting_base(
    tmp_path: Path, store: Any, conflicting: bool
) -> None:
    from kew.graph.driver import tick
    from kew.publication import PublicationError

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _seed_pending_run(store, "1010", "orphan-run", pgid=0)
    if conflicting:
        _bind_checkout_base(store, config, worktree, "orphan-run", "1010")
        with pytest.raises(PublicationError, match="conflicting publication bases"):
            _bind_checkout_base(
                store,
                config,
                worktree,
                "orphan-run",
                "1010",
                base="b" * 40,
            )
    [reservation] = store.list_epic_reservations(EPIC)
    run = store.get("orphan-run")
    assert run is not None

    message = "conflicting or unreadable" if conflicting else "no immutable"
    with pytest.raises(_PreexistingWorktreeRefusal, match=message):
        tick._preserve_dirty_orphan(store, config, reservation, run, now=NOW)

    assert store.get("orphan-run").status == "pending"
    assert store.list_continuations(EPIC) == []
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"


@pytest.mark.parametrize("conflicting", [False, True])
def test_existing_continuation_rejects_missing_or_conflicting_base(
    tmp_path: Path, store: Any, conflicting: bool
) -> None:
    from kew.publication import PublicationError

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "original-run")
    if conflicting:
        _bind_checkout_base(store, config, worktree, "original-run", "1010")
        with pytest.raises(PublicationError, match="conflicting publication bases"):
            _bind_checkout_base(
                store,
                config,
                worktree,
                "original-run",
                "1010",
                base="b" * 40,
            )
    store.record_continuation(
        EPIC,
        "1010",
        "original-run",
        "agent/issue-1010",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert store.reserve_epic_node_item(EPIC, "1010", "reservation") is True

    message = "conflicting or unreadable" if conflicting else "no terminal"
    with pytest.raises(_PreexistingWorktreeRefusal, match=message):
        _register_preexisting_dirty_worktree(
            store,
            config,
            epic_ref=EPIC,
            item_id="1010",
            provider="github",
            reservation_run_id="reservation",
            liveness=lambda _run: False,
        )

    [record] = store.list_continuations(EPIC)
    assert record.original_run_id == "original-run"
    assert record.state == "awaiting_continuation"
    assert worktree.joinpath("wip.txt").is_file()


@pytest.mark.parametrize("boundary", ["matching", "comparison", "registration"])
def test_recovery_refuses_checkout_replacement_between_identity_boundaries(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from kew.graph.driver import tick
    from kew.worktree import write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "producer")
    _bind_checkout_base(store, config, worktree, "producer", "1010")

    comparison = MagicMock(return_value=True)
    monkeypatch.setattr(tick, "_checkout_holds_work", comparison)
    if boundary == "matching":
        read_base = tick._recorded_checkout_base

        def replace_during_matching(*args, **kwargs):
            base = read_base(*args, **kwargs)
            write_checkout_generation(worktree)
            return base

        monkeypatch.setattr(tick, "_recorded_checkout_base", replace_during_matching)
    elif boundary == "comparison":

        def replace_during_comparison(*_args, **_kwargs):
            write_checkout_generation(worktree)
            return True

        comparison.side_effect = replace_during_comparison
    else:
        register = store.register_continuation_and_release_reservation

        def replace_during_registration(*args, **kwargs):
            record = register(*args, **kwargs)
            write_checkout_generation(worktree)
            return record

        monkeypatch.setattr(
            store,
            "register_continuation_and_release_reservation",
            replace_during_registration,
        )

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        _register_bound_candidate(store, config, worktree, item_id="1010")

    continuations = store.list_continuations(EPIC)
    if boundary == "registration":
        assert len(continuations) == 1
        assert continuations[0].state == "awaiting_continuation"
        with pytest.raises(_PreexistingWorktreeRefusal, match="no terminal.*producer"):
            tick._terminal_run_for_preexisting_work(
                store,
                "1010",
                "agent/issue-1010",
                worktree,
                config,
            )
    else:
        assert continuations == []
    if boundary == "matching":
        comparison.assert_not_called()
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"


def test_refusal_preserves_every_candidate_checkout(tmp_path: Path, store: Any) -> None:
    from kew.publication import PublicationError

    config = _config()
    config.branches.worktree_base = str(tmp_path / "selected" / "worktrees")
    selected = _make_dirty_worktree(tmp_path / "selected" / "worktrees", "1010")
    other = _make_dirty_worktree(tmp_path / "other" / "worktrees", "1010")
    _save_terminal_run(store, "1010", "conflicted")
    _bind_checkout_base(store, config, selected, "conflicted", "1010")
    with pytest.raises(PublicationError, match="conflicting publication bases"):
        _bind_checkout_base(
            store,
            config,
            selected,
            "conflicted",
            "1010",
            base="b" * 40,
        )

    with pytest.raises(_PreexistingWorktreeRefusal, match="conflicting or unreadable"):
        _register_bound_candidate(store, config, selected, item_id="1010")

    assert selected.joinpath("wip.txt").read_text() == "preserve me\n"
    assert other.joinpath("wip.txt").read_text() == "preserve me\n"


def test_recovery_claim_blocks_concurrent_checkout_replacement(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import worktree as worktree_module

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "producer")
    _bind_checkout_base(store, config, worktree, "producer", "1010")
    failures: list[BaseException] = []

    def attempt_claim() -> None:
        try:
            worktree_module.claim_checkout(worktree)
        except BaseException as exc:  # noqa: BLE001 - assertion captures the refusal
            failures.append(exc)

    def compare_while_claimed(*_args, **_kwargs):
        contender = threading.Thread(target=attempt_claim)
        contender.start()
        contender.join(timeout=5)
        assert not contender.is_alive()
        return True

    monkeypatch.setattr(
        "kew.graph.driver.tick._checkout_holds_work", compare_while_claimed
    )

    outcome = _register_bound_candidate(store, config, worktree, item_id="1010")

    assert outcome == "registered"
    assert len(failures) == 1
    assert isinstance(failures[0], worktree_module.CheckoutBusy)
    assert worktree.joinpath("wip.txt").is_file()


def test_recovery_refuses_checkout_replacement_before_runner_launch(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import dispatch_core
    from kew import worktree as worktree_module
    from kew.publication import PublicationError
    from kew.worktree import write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "original-run")
    _bind_checkout_base(store, config, worktree, "original-run", "1010")
    record = store.record_continuation(
        EPIC,
        "1010",
        "original-run",
        "agent/issue-1010",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            "continuation-run",
            5,
            claim_evidence=fixture_claim_evidence(
                store,
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                "continuation-run",
            ),
        )
        == "claimed"
    )
    ctx = dispatch_core._DispatchContext(
        config=config,
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=EPIC,
        provider="github",
        issue_num=1010,
        issue_title="Resume",
        issue_body="",
        complexity="simple",
        item_id="1010",
        branch="agent/issue-1010",
        start_monotonic=0.0,
        refuse_rollback=lambda: None,
        terminal_failure=None,
        run_kind="dispatch",
        worktree_dir=worktree,
        run_id="continuation-run",
    )
    decision = MagicMock(posture="host", runtime=None, image=None)
    monkeypatch.setattr(
        dispatch_core, "_resolve_and_apply_sandbox", lambda *_a, **_k: decision
    )
    monkeypatch.setattr(dispatch_core, "_build_repo_map", lambda *_a, **_k: "")
    monkeypatch.setattr(
        dispatch_core, "build_prompt", lambda **_kwargs: "compiled prompt"
    )
    monkeypatch.setattr(dispatch_core, "_emit_sandbox_resolved", lambda *_a, **_k: None)
    monkeypatch.setattr(
        dispatch_core,
        "_require_final_runner_preflight",
        lambda *_a, **_k: write_checkout_generation(worktree),
    )
    paid_runner = MagicMock()
    monkeypatch.setattr(dispatch_core, "run_tracked_agent", paid_runner)
    runner = MagicMock(name="runner")
    runner.name = "claude-code"
    runner.publication_style = "host"
    resolved = MagicMock(runner=runner, model="claude-sonnet-4-6")
    ctx.model_override = "claude-sonnet-4-6"

    try:
        with pytest.raises(PublicationError, match="identity changed"):
            dispatch_core._launch_workspace(
                ctx,
                dispatch_core._LaunchSetup(),
                config=config,
                resolved=resolved,
                runner=runner,
                early=dispatch_core._EarlyRunInfo(
                    runner="claude-code", model="claude-sonnet-4-6"
                ),
                admit_budget_check=None,
            )
    finally:
        worktree_module.release_checkout_claim(worktree)

    paid_runner.assert_not_called()
    assert store.get("continuation-run") is None
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"


# Real child process; a pgid reused by a foreign process under churn reads
# as alive. Serial phase: tests/conftest.py.
@pytest.mark.serial
def test_dead_pending_dirty_worktree_becomes_awaiting_continuation(
    tmp_path: Path, store: Any
) -> None:
    """A dead owner must preserve dirty WIP and create one continuation record.

    The dead run is finalized as killed using its recorded partial telemetry;
    its old reservation is released for a later atomic continuation claim, and
    the node never re-enters the ordinary dispatch frontier.
    """
    config = _config()
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    config.branches.worktree_base = str(tmp_path / "worktrees")
    deps = FakeSource(nodes=[_node("1010")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")})
    sink = FakeSink()

    _seed_pending_run(
        store,
        "1010",
        "run-1010",
        pgid=_spawn_then_kill_pgid(),
        estimated_cost_usd=2.50,
    )
    _bind_checkout_base(store, config, worktree, "run-1010", "1010")
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    row = store.list_by_item("1010")[0]
    assert row.status == "killed"
    assert row.error_category == "orphaned_pending_preserved"
    from kew.budget import check_budget

    budget = check_budget(store, config.budget, 1010, item_id="1010", now=NOW)
    assert budget.issue_spent_usd == 2.50
    assert store.list_epic_reservations(EPIC) == []
    continuation = store.list_continuations(EPIC)
    assert len(continuation) == 1
    assert continuation[0].item_id == "1010"
    assert continuation[0].original_run_id == "run-1010"
    assert continuation[0].branch == "agent/issue-1010"
    assert continuation[0].worktree_identity == str(worktree.resolve())
    assert continuation[0].state == "awaiting_continuation"
    assert result.failed == ()
    assert result.in_flight == ()
    assert result.waiting[0].reason.startswith("awaiting_continuation")
    assert result.signal == TerminalSignal.STALLED
    assert sink.calls == []
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    evidence = store.list_audit_events(event="continuation_detected")
    assert len(evidence) == 1
    assert evidence[0].payload["original_run_id"] == "run-1010"
    assert evidence[0].payload["continuation_run_id"] is None
    assert evidence[0].payload["item_id"] == "1010"
    assert evidence[0].payload["branch"] == "agent/issue-1010"
    assert evidence[0].payload["worktree_identity"].startswith("sha256:")
    assert str(worktree) not in str(evidence[0].payload)
    assert verify_chain(store.read_chain_rows()).ok


def test_dirty_orphan_preservation_does_not_clobber_concurrent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host publisher can outlive the container the reconciler proved dead.

    Force that publisher to persist success after the dirty-work proof but
    before orphan preservation.  The stale reconciler must lose its pending
    ownership check and leave both the terminal row and reservation untouched.
    """
    from kew.graph.driver import tick

    db_path = str(tmp_path / "dispatch.db")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    dirty_proven = threading.Event()
    publication_finished = threading.Event()
    publisher_errors: list[Exception] = []

    with SqliteRunStore(db_path) as reconciler:
        assert reconciler.reserve_epic_node(EPIC, 1010, "run-1010")
        reconciler.save(
            RunRecord(
                run_id="run-1010",
                issue_num=1010,
                item_id="1010",
                branch="agent/issue-1010",
                status="pending",
                exit_code=-1,
                started_at=NOW,
                container_name="dead-container",
                container_id="a" * 64,
            )
        )
        _bind_checkout_base(reconciler, config, worktree, "run-1010", "1010")
        stale = reconciler.get("run-1010")
        assert stale is not None
        reservation = reconciler.list_epic_reservations(EPIC)[0]

        def publish_success() -> None:
            try:
                assert dirty_proven.wait(timeout=5)
                with SqliteRunStore(db_path) as publisher:
                    current = publisher.get("run-1010")
                    assert current is not None
                    current.status = "success"
                    current.exit_code = 0
                    current.error_category = ""
                    current.finished_at = NOW
                    publisher.save(current)
            except Exception as exc:  # pragma: no cover - diagnostic capture
                publisher_errors.append(exc)
            finally:
                publication_finished.set()

        def prove_dirty(_worktree: Path) -> bool:
            dirty_proven.set()
            assert publication_finished.wait(timeout=5)
            return True

        monkeypatch.setattr(
            tick,
            "_validated_recorded_worktree",
            lambda _reservation, _run, _config: worktree,
        )
        monkeypatch.setattr(
            tick,
            "_checkout_holds_work",
            lambda _c, path, _b, **_kwargs: prove_dirty(path),
        )
        publisher = threading.Thread(target=publish_success)
        publisher.start()
        monkeypatch.setattr(tick, "_kill_orphaned_attempt", lambda *a: True)
        preserved = tick._preserve_dirty_orphan(
            reconciler, config, reservation, stale, now=NOW
        )
        publisher.join(timeout=10)

        assert not publisher.is_alive()
        assert publisher_errors == []
        assert preserved is False
        current = reconciler.get("run-1010")
        assert current is not None
        assert current.status == "success"
        assert current.exit_code == 0
        assert reconciler.list_continuations(EPIC) == []
        assert [row.run_id for row in reconciler.list_epic_reservations(EPIC)] == [
            "run-1010"
        ]


def _claim_continuation(
    store: Any,
    tmp_path: Path,
    *,
    item_id: str = "1010",
    run_id: str = "continuation-1010",
    bind_base: bool = True,
) -> Path:
    slug = branch_slug(item_id)
    worktree = _make_dirty_worktree(tmp_path / "worktrees", slug)
    original_run_id = f"original-{item_id}"
    store.save(
        RunRecord(
            run_id=original_run_id,
            issue_num=int(item_id) if item_id.isdigit() else 0,
            item_id=item_id,
            branch=f"agent/issue-{slug}",
            status="failure",
            exit_code=137,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    if bind_base:
        _bind_checkout_base(store, DispatchConfig(), worktree, original_run_id, item_id)
    record = store.record_continuation(
        EPIC,
        item_id,
        original_run_id,
        f"agent/issue-{slug}",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            run_id,
            5,
            claim_evidence=fixture_claim_evidence(
                store,
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                run_id,
            ),
        )
        == "claimed"
    )
    return worktree


def _claim_owned_recovery(
    store: Any,
    *,
    epic_ref: str,
    item_id: str,
    original_run_id: str,
    branch: str,
    worktree: Path,
    child_run_id: str = "child",
) -> None:
    record = store.record_continuation(
        epic_ref,
        item_id,
        original_run_id,
        branch,
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            child_run_id,
            10,
            claim_evidence=fixture_claim_evidence(
                store,
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                child_run_id,
            ),
        )
        == "claimed"
    )


def _owned_orphan_identity_case(
    store: Any,
    tmp_path: Path,
    mismatch: str,
) -> tuple[DispatchConfig, Path, Path, Any, RunRecord]:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "producer-1010")
    _bind_checkout_base(store, config, worktree, "producer-1010", "1010")
    record_epic = "other-epic" if mismatch == "epic" else EPIC
    record_item = "2020" if mismatch == "item" else "1010"
    record_branch = (
        "agent/issue-unrelated" if mismatch == "branch" else "agent/issue-1010"
    )
    record_path = worktree
    if mismatch == "path":
        record_path = tmp_path / "other-preserved-checkout"
        record_path.mkdir()
        record_path.joinpath("candidate.txt").write_text(
            "other preserved bytes\n", encoding="utf-8"
        )
    elif mismatch == "alias":
        record_path = tmp_path / "checkout-alias"
        record_path.symlink_to(worktree, target_is_directory=True)
    producer = "wrong-producer" if mismatch == "producer" else "producer-1010"
    owner = "other-child" if mismatch == "child" else "child"
    _claim_owned_recovery(
        store,
        epic_ref=record_epic,
        item_id=record_item,
        original_run_id=producer,
        branch=record_branch,
        worktree=record_path,
        child_run_id=owner,
    )
    if mismatch in {"epic", "child"}:
        store.release_epic_node_item("1010", run_id=owner)
        assert store.reserve_epic_node_item(EPIC, "1010", "child")
    elif mismatch == "item":
        assert store.reserve_epic_node_item(EPIC, "1010", "child")
    run = RunRecord(
        run_id="child",
        issue_num=1010,
        item_id="2020" if mismatch == "child_item" else "1010",
        branch="agent/issue-1010",
        status="pending",
        exit_code=-1,
        started_at=NOW,
        pgid=0,
        attempt_pgid=2**31 - 1,  # This attempt already exited.
    )
    store.save(run)
    reservation = next(
        row for row in store.list_epic_reservations(EPIC) if row.item_id == "1010"
    )
    return config, worktree, record_path, reservation, run


@pytest.mark.parametrize("holds_work", [False, True], ids=["clean", "dirty"])
@pytest.mark.parametrize(
    "mismatch",
    [
        "none",
        "alias",
        "path",
        "branch",
        "item",
        "child_item",
        "epic",
        "child",
        "producer",
    ],
)
def test_orphan_reopening_requires_complete_owned_identity(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    holds_work: bool,
    mismatch: str,
) -> None:
    from kew.graph.driver import tick

    config, worktree, recorded_path, reservation, run = _owned_orphan_identity_case(
        store,
        tmp_path,
        mismatch,
    )
    comparison = MagicMock(return_value=holds_work)
    monkeypatch.setattr(tick, "_checkout_holds_work", comparison)
    continuations_before = store.list_continuations(EPIC)
    reservations_before = store.list_epic_reservations(EPIC)

    if mismatch in {"none", "alias"}:
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)
        assert store.list_continuations(EPIC)[0].state == "awaiting_continuation"
        assert store.list_epic_reservations(EPIC) == []
    else:
        with pytest.raises(_PreexistingWorktreeRefusal):
            tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)
        comparison.assert_not_called()
        current = store.get(run.run_id)
        assert current is not None
        assert current.status == "pending"
        assert store.list_continuations(EPIC) == continuations_before
        assert store.list_epic_reservations(EPIC) == reservations_before

    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    if mismatch == "path":
        assert (
            recorded_path.joinpath("candidate.txt").read_text(encoding="utf-8")
            == "other preserved bytes\n"
        )


@pytest.mark.parametrize("holds_work", [False, True], ids=["clean", "dirty"])
def test_multiple_managed_continuation_owners_refuse_before_reopening(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    holds_work: bool,
) -> None:
    from kew.graph.driver import tick

    config, _, _, reservation, run = _owned_orphan_identity_case(
        store,
        tmp_path,
        "none",
    )
    other = _make_dirty_worktree(tmp_path / "other-worktrees", "2020")
    _save_terminal_run(store, "2020", "producer-2020")
    _bind_checkout_base(store, config, other, "producer-2020", "2020")
    _claim_owned_recovery(
        store,
        epic_ref=EPIC,
        item_id="2020",
        original_run_id="producer-2020",
        branch="agent/issue-2020",
        worktree=other,
    )
    before = store.list_continuations(EPIC)
    reservations_before = store.list_epic_reservations(EPIC)
    assert len(before) == 2
    assert all(
        row.state == "continuing" and row.continuation_run_id == "child"
        for row in before
    )
    comparison = MagicMock(return_value=holds_work)
    monkeypatch.setattr(tick, "_checkout_holds_work", comparison)

    with pytest.raises(_PreexistingWorktreeRefusal, match="multiple managed owners"):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    comparison.assert_not_called()
    assert store.list_continuations(EPIC) == before
    assert store.list_epic_reservations(EPIC) == reservations_before
    current = store.get(run.run_id)
    assert current is not None
    assert current.status == "pending"


@pytest.mark.parametrize("route", ["terminal", "missing_run", "absent_checkout"])
@pytest.mark.parametrize("mismatch", [False, True], ids=["stable", "wrong_item"])
def test_alternate_reopening_requires_reserved_owned_item(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    mismatch: bool,
) -> None:
    from kew.graph.driver import tick

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    item_id = "2020" if mismatch else "1010"
    worktree = _claim_continuation(
        store,
        tmp_path,
        item_id=item_id,
        run_id="child",
    )
    if mismatch:
        store.release_epic_node_item("2020", run_id="child")
        assert store.reserve_epic_node_item(EPIC, "1010", "child")
    [reservation] = store.list_epic_reservations(EPIC)
    if route != "missing_run":
        store.save(
            RunRecord(
                run_id="child",
                issue_num=1010,
                item_id="1010",
                branch="agent/issue-1010",
                status="failure" if route == "terminal" else "pending",
                exit_code=1 if route == "terminal" else -1,
                started_at=NOW,
                finished_at=NOW if route == "terminal" else None,
                pgid=0,
                attempt_pgid=2**31 - 1,  # This attempt already exited.
            )
        )
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: False)
    before = store.list_continuations(EPIC)
    reservations_before = store.list_epic_reservations(EPIC)
    run_before = store.get("child")

    def reconcile() -> None:
        tick._reconcile_one_reservation(
            store,
            config,
            reservation,
            now=max(NOW, reservation.reserved_at) + timedelta(minutes=20),
            probe=lambda _run: False,
        )

    if mismatch:
        with pytest.raises(_PreexistingWorktreeRefusal):
            reconcile()
        assert store.list_continuations(EPIC) == before
        assert store.list_epic_reservations(EPIC) == reservations_before
        assert store.get("child") == run_before
    else:
        reconcile()
        assert store.list_continuations(EPIC)[0].state == "awaiting_continuation"
        assert store.list_epic_reservations(EPIC) == []

    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize(
    "arrival",
    [None, "pending", "success"],
    ids=["still_missing", "live_child", "successful_child"],
)
def test_missing_recovery_preserves_child_arriving_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrival: str | None,
) -> None:
    import os

    from kew import worktree as worktree_module
    from kew.graph.driver import tick

    database = tmp_path / "arrival.db"
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    with SqliteRunStore(str(database)) as store:
        checkout = _claim_continuation(store, tmp_path, run_id="child")
        [reservation] = store.list_epic_reservations(EPIC)
        continuation_before = store.list_continuations(EPIC)
        reservations_before = store.list_epic_reservations(EPIC)
        now = max(NOW, reservation.reserved_at) + timedelta(minutes=20)
        claim_and_validate = tick._claim_and_validate_continuation_checkout
        saved: list[RunRecord] = []
        failures: list[BaseException] = []

        def arrive() -> None:
            try:
                worktree_module.claim_checkout(checkout)
                try:
                    with SqliteRunStore(str(database)) as writer:
                        writer.save(
                            RunRecord(
                                run_id="child",
                                issue_num=1010,
                                item_id="1010",
                                branch="agent/issue-1010",
                                status=arrival,
                                exit_code=-1 if arrival == "pending" else 0,
                                started_at=now,
                                finished_at=now if arrival == "success" else None,
                                pgid=os.getpgrp(),
                                total_tokens=123,
                                estimated_cost_usd=0.25,
                            )
                        )
                        current = writer.get("child")
                        assert current is not None
                        saved.append(current)
                finally:
                    worktree_module.release_checkout_claim(checkout)
            except BaseException as exc:  # noqa: BLE001 - assertion captures actor
                failures.append(exc)

        def claim_after_arrival(*args: Any, **kwargs: Any) -> Any:
            if arrival is not None:
                actor = threading.Thread(target=arrive)
                actor.start()
                actor.join(timeout=5)
                assert not actor.is_alive()
                assert failures == []
                assert store.get("child") == saved[0]
            return claim_and_validate(*args, **kwargs)

        monkeypatch.setattr(
            tick,
            "_claim_and_validate_continuation_checkout",
            claim_after_arrival,
        )
        probe = MagicMock(return_value=True)
        try:
            tick._reconcile_one_reservation(
                store,
                config,
                reservation,
                now=now,
                probe=probe,
            )
        finally:
            worktree_module.release_checkout_claims()

        probe.assert_not_called()
        current = store.get("child")
        assert current is not None
        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )
        if arrival is None:
            assert current.error_category == "dispatch_child_died"
            assert store.list_continuations(EPIC)[0].state == "awaiting_continuation"
        else:
            assert current == saved[0]
            assert store.list_continuations(EPIC) == continuation_before
            assert store.list_epic_reservations(EPIC) == reservations_before


@pytest.mark.parametrize("concurrent_success", [False, True])
def test_terminal_reopening_respects_success_written_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_success: bool,
) -> None:
    from kew import worktree as worktree_module
    from kew.graph.driver import tick

    database = tmp_path / "terminal-race.db"
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    with SqliteRunStore(str(database)) as store:
        checkout = _claim_continuation(store, tmp_path, run_id="child")
        store.save(
            RunRecord(
                run_id="child",
                issue_num=1010,
                item_id="1010",
                branch="agent/issue-1010",
                status="awaiting_publication",
                exit_code=0,
                started_at=NOW,
                finished_at=NOW,
                total_tokens=123,
                estimated_cost_usd=0.25,
            )
        )
        [reservation] = store.list_epic_reservations(EPIC)
        records_before = store.list_continuations(EPIC)
        reservations_before = store.list_epic_reservations(EPIC)
        claim_and_validate = tick._claim_and_validate_continuation_checkout
        failures: list[BaseException] = []
        successful_rows: list[RunRecord] = []

        def finish() -> None:
            try:
                worktree_module.claim_checkout(checkout)
                try:
                    with SqliteRunStore(str(database)) as writer:
                        assert writer.transition_run_status(
                            "child",
                            expected_status="awaiting_publication",
                            new_status="success",
                            error_category="publisher-success",
                        )
                        current = writer.get("child")
                        assert current is not None
                        successful_rows.append(current)
                finally:
                    worktree_module.release_checkout_claim(checkout)
            except BaseException as exc:  # noqa: BLE001 - assertion captures actor
                failures.append(exc)

        def claim_after_success(*args: Any, **kwargs: Any) -> Any:
            if concurrent_success:
                actor = threading.Thread(target=finish)
                actor.start()
                actor.join(timeout=5)
                assert not actor.is_alive()
                assert failures == []
                assert store.get("child").status == "success"
            return claim_and_validate(*args, **kwargs)

        monkeypatch.setattr(
            tick,
            "_claim_and_validate_continuation_checkout",
            claim_after_success,
        )
        try:
            tick._reconcile_one_reservation(
                store,
                config,
                reservation,
                now=max(NOW, reservation.reserved_at) + timedelta(minutes=20),
                probe=lambda _run: True,
            )
        finally:
            worktree_module.release_checkout_claims()

        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )
        if concurrent_success:
            assert store.get("child") == successful_rows[0]
            assert store.list_continuations(EPIC) == records_before
            assert store.list_epic_reservations(EPIC) == reservations_before
        else:
            assert store.list_continuations(EPIC)[0].state == "awaiting_continuation"
            assert store.list_epic_reservations(EPIC) == []


def _replace_during_precommit(
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    worktree: Path,
) -> None:
    from kew.worktree import write_checkout_generation

    original = getattr(store, method_name)

    def replace_before_check(
        *args: Any,
        precommit_check: Any = None,
        **kwargs: Any,
    ) -> Any:
        assert precommit_check is not None

        def check_replacement() -> None:
            write_checkout_generation(worktree)
            precommit_check()

        return original(
            *args,
            precommit_check=check_replacement,
            **kwargs,
        )

    monkeypatch.setattr(store, method_name, replace_before_check)


@pytest.mark.parametrize(
    "route",
    ["terminal", "missing_run", "clean_orphan", "dirty_orphan"],
)
def test_reopening_precommit_refusal_rolls_back_only_current_handoff(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    from kew.graph.driver import tick

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    if route in {"clean_orphan", "dirty_orphan"}:
        config, worktree, reservation, run = _orphan_recovery_case(
            store,
            tmp_path,
            reopen=True,
        )
        monkeypatch.setattr(
            tick,
            "_checkout_holds_work",
            lambda *_a, **_k: route == "dirty_orphan",
        )

        def reconcile() -> None:
            tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    else:
        worktree = _claim_continuation(store, tmp_path, run_id="child")
        [reservation] = store.list_epic_reservations(EPIC)
        if route == "terminal":
            store.save(
                RunRecord(
                    run_id="child",
                    issue_num=1010,
                    item_id="1010",
                    branch="agent/issue-1010",
                    status="failure",
                    exit_code=1,
                    started_at=NOW,
                    finished_at=NOW,
                )
            )

            def reconcile() -> None:
                tick._reconcile_one_reservation(
                    store,
                    config,
                    reservation,
                    now=NOW,
                    probe=lambda _run: False,
                )

        else:
            store._conn.execute(
                "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
                ((NOW - timedelta(minutes=20)).isoformat(), "1010"),
            )
            store._conn.commit()
            [reservation] = store.list_epic_reservations(EPIC)

            def reconcile() -> None:
                tick._reconcile_missing_run(store, config, reservation, now=NOW)

    continuations_before = store.list_continuations(EPIC)
    reservations_before = store.list_epic_reservations(EPIC)
    detected_before = store.list_audit_events(event="continuation_detected")
    terminal_before = store.list_audit_events(event="continuation_terminal")
    _replace_during_precommit(
        store,
        monkeypatch,
        "mark_continuation_awaiting",
        worktree,
    )

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        reconcile()

    assert store.list_continuations(EPIC) == continuations_before
    assert store.list_epic_reservations(EPIC) == reservations_before
    assert store.list_audit_events(event="continuation_detected") == detected_before
    assert store.list_audit_events(event="continuation_terminal") == terminal_before
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    current = store.get(
        "child" if route in {"terminal", "missing_run"} else "orphan-run"
    )
    assert current is not None
    expected_status = "failure" if route in {"terminal", "missing_run"} else "killed"
    assert current.status == expected_status


@pytest.mark.parametrize("holds_work", [False, True], ids=["clean", "dirty"])
def test_orphan_finalization_precommit_refusal_preserves_pending_state(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    holds_work: bool,
) -> None:
    from kew.graph.driver import tick

    config, worktree, reservation, run = _orphan_recovery_case(store, tmp_path)
    monkeypatch.setattr(
        tick,
        "_checkout_holds_work",
        lambda *_a, **_k: holds_work,
    )
    continuations_before = store.list_continuations(EPIC)
    reservations_before = store.list_epic_reservations(EPIC)
    _replace_during_precommit(
        store,
        monkeypatch,
        "finalize_orphaned_pending",
        worktree,
    )

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    current = store.get(run.run_id)
    assert current is not None
    assert current.status == "pending"
    assert store.list_continuations(EPIC) == continuations_before
    assert store.list_epic_reservations(EPIC) == reservations_before
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_dirty_orphan_record_precommit_refusal_retains_committed_finalization(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.graph.driver import tick

    config, worktree, reservation, run = _orphan_recovery_case(store, tmp_path)
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: True)
    reservations_before = store.list_epic_reservations(EPIC)
    _replace_during_precommit(
        store,
        monkeypatch,
        "record_continuation",
        worktree,
    )

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    current = store.get(run.run_id)
    assert current is not None
    assert current.status == "killed"
    assert current.error_category == "orphaned_pending_preserved"
    assert store.list_continuations(EPIC) == []
    assert store.list_epic_reservations(EPIC) == reservations_before
    assert store.list_audit_events(event="continuation_detected") == []
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_dirty_registration_precommit_refusal_rolls_back_registration_and_release(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    from kew.graph.driver import tick

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "producer")
    _bind_checkout_base(store, config, worktree, "producer", "1010")
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: True)
    assert store.reserve_epic_node_item(EPIC, "1010", "reservation")
    if existing:
        store.record_continuation(
            EPIC,
            "1010",
            "producer",
            "agent/issue-1010",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )
    reservations_before = store.list_epic_reservations(EPIC)
    continuations_before = store.list_continuations(EPIC)
    _replace_during_precommit(
        store,
        monkeypatch,
        "register_continuation_and_release_reservation",
        worktree,
    )

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        tick._register_preexisting_dirty_worktree(
            store,
            config,
            epic_ref=EPIC,
            item_id="1010",
            provider="github",
            reservation_run_id="reservation",
            liveness=lambda _run: False,
        )

    assert store.list_continuations(EPIC) == continuations_before
    assert store.list_epic_reservations(EPIC) == reservations_before
    assert store.list_audit_events(event="continuation_detected") == []
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize("replaced", [False, True])
def test_terminal_settlement_validates_checkout_before_reopening(
    tmp_path: Path, store: Any, replaced: bool
) -> None:
    from kew.graph.driver import tick
    from kew.worktree import read_checkout_generation, write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _claim_continuation(store, tmp_path, run_id="child")
    [record] = store.list_continuations(EPIC)
    [reservation] = store.list_epic_reservations(EPIC)
    original_generation = read_checkout_generation(worktree)
    run = RunRecord(
        run_id="child",
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        status="failure",
        exit_code=1,
        started_at=NOW,
        finished_at=NOW,
    )
    store.save(run)
    if replaced:
        write_checkout_generation(worktree)

    if replaced:
        with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
            tick._settle_terminal_continuation(store, config, record, run)
    else:
        tick._settle_terminal_continuation(store, config, record, run)

    [after] = store.list_continuations(EPIC)
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    if replaced:
        assert read_checkout_generation(worktree) != original_generation
        assert after.state == "continuing"
        assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
            reservation.run_id
        ]
        assert store.list_audit_events(event="continuation_terminal") == []
    else:
        assert after.state == "awaiting_continuation"


@pytest.mark.parametrize("replaced", [False, True])
def test_missing_run_reconciliation_validates_checkout_before_reopening(
    tmp_path: Path, store: Any, replaced: bool
) -> None:
    from kew.graph.driver import tick
    from kew.worktree import read_checkout_generation, write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _claim_continuation(store, tmp_path, run_id="child")
    [record] = store.list_continuations(EPIC)
    original_generation = read_checkout_generation(worktree)
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        ((NOW - timedelta(minutes=20)).isoformat(), "1010"),
    )
    store._conn.commit()
    [reservation] = store.list_epic_reservations(EPIC)
    if replaced:
        write_checkout_generation(worktree)

    if replaced:
        with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
            tick._reconcile_missing_run(store, config, reservation, now=NOW)
    else:
        tick._reconcile_missing_run(store, config, reservation, now=NOW)

    [after] = store.list_continuations(EPIC)
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    if replaced:
        assert read_checkout_generation(worktree) != original_generation
        assert after.state == "continuing"
        assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
            reservation.run_id
        ]
        assert store.list_audit_events(event="continuation_terminal") == []
    else:
        assert after.state == "awaiting_continuation"
        assert store.list_epic_reservations(EPIC) == []


@pytest.mark.parametrize("route", ["settlement", "missing_run", "dead_pending"])
@pytest.mark.parametrize("evidence", ["missing", "conflicting"])
def test_alternate_reopening_rejects_missing_or_conflicting_base(
    tmp_path: Path,
    store: Any,
    route: str,
    evidence: str,
) -> None:
    """Alternate reopen paths require the original immutable producer binding."""
    from kew.graph.driver import tick
    from kew.publication import PublicationError

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _claim_continuation(
        store,
        tmp_path,
        run_id="child",
        bind_base=evidence != "missing",
    )
    if evidence == "conflicting":
        with pytest.raises(PublicationError, match="conflicting publication bases"):
            _bind_checkout_base(
                store,
                config,
                worktree,
                "original-1010",
                "1010",
                base="b" * 40,
            )
    [record] = store.list_continuations(EPIC)
    if route == "settlement":
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
        store.save(run)

        def action() -> None:
            tick._settle_terminal_continuation(store, config, record, run)

    elif route == "missing_run":
        store._conn.execute(
            "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
            ((NOW - timedelta(minutes=20)).isoformat(), "1010"),
        )
        store._conn.commit()
        [reservation] = store.list_epic_reservations(EPIC)

        def action() -> None:
            tick._reconcile_missing_run(store, config, reservation, now=NOW)

    else:
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=0,
        )
        store.save(run)
        [reservation] = store.list_epic_reservations(EPIC)

        def action() -> None:
            tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    continuations_before = store.list_continuations(EPIC)
    reservations_before = store.list_epic_reservations(EPIC)
    if evidence == "conflicting":
        message = "conflicting or unreadable"
    elif route == "dead_pending":
        message = "no immutable"
    else:
        message = "no terminal"
    with pytest.raises(_PreexistingWorktreeRefusal, match=message):
        action()

    assert store.list_continuations(EPIC) == continuations_before
    assert store.list_epic_reservations(EPIC) == reservations_before
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    assert store.list_audit_events(event="continuation_terminal") == []
    if route == "missing_run":
        assert store.get("child") is None


@pytest.mark.parametrize("route", ["settlement", "missing_run"])
def test_alternate_reopening_holds_checkout_claim_through_handoff(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """A real competing checkout claimant cannot enter either reopen handoff."""
    from kew import worktree as worktree_module
    from kew.graph.driver import tick

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _claim_continuation(store, tmp_path, run_id="child")
    [record] = store.list_continuations(EPIC)
    failures: list[BaseException] = []
    original_validate = tick._terminal_run_for_preexisting_work

    def attempt_claim() -> None:
        try:
            worktree_module.claim_checkout(worktree)
        except BaseException as exc:  # noqa: BLE001 - assertion captures refusal
            failures.append(exc)

    def validate_while_claimed(*args: Any, **kwargs: Any) -> Any:
        contender = threading.Thread(target=attempt_claim)
        contender.start()
        contender.join(timeout=5)
        assert not contender.is_alive()
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        tick, "_terminal_run_for_preexisting_work", validate_while_claimed
    )
    if route == "settlement":
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
        store.save(run)
        tick._settle_terminal_continuation(store, config, record, run)
    else:
        store._conn.execute(
            "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
            ((NOW - timedelta(minutes=20)).isoformat(), "1010"),
        )
        store._conn.commit()
        [reservation] = store.list_epic_reservations(EPIC)
        tick._reconcile_missing_run(store, config, reservation, now=NOW)

    assert len(failures) == 1
    assert all(isinstance(exc, worktree_module.CheckoutBusy) for exc in failures)
    [after] = store.list_continuations(EPIC)
    assert after.state == "awaiting_continuation"
    worktree_module.claim_checkout(worktree)
    worktree_module.release_checkout_claim(worktree)


@pytest.mark.parametrize("route", ["settlement", "missing_run", "dead_pending"])
def test_alternate_reopening_revalidates_lost_handoff_result(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """A lost continuation CAS cannot hide simultaneous checkout replacement."""
    from kew.graph.driver import tick
    from kew.worktree import write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _claim_continuation(store, tmp_path, run_id="child")
    [record] = store.list_continuations(EPIC)

    def lose_after_replacement(*_args: Any, **_kwargs: Any) -> bool:
        write_checkout_generation(worktree)
        return False

    monkeypatch.setattr(store, "mark_continuation_awaiting", lose_after_replacement)
    if route == "settlement":
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
        store.save(run)

        def action() -> None:
            tick._settle_terminal_continuation(store, config, record, run)

    elif route == "missing_run":
        store._conn.execute(
            "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
            ((NOW - timedelta(minutes=20)).isoformat(), "1010"),
        )
        store._conn.commit()
        [reservation] = store.list_epic_reservations(EPIC)

        def action() -> None:
            tick._reconcile_missing_run(store, config, reservation, now=NOW)

    else:
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=0,
            attempt_pgid=2**31 - 1,  # This attempt already exited.
        )
        store.save(run)
        [reservation] = store.list_epic_reservations(EPIC)
        monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: False)

        def action() -> None:
            tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        action()

    [after] = store.list_continuations(EPIC)
    assert after.state == "continuing"
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == ["child"]
    assert store.list_audit_events(event="continuation_terminal") == []


@pytest.mark.parametrize("route", ["settlement", "missing_run", "dead_pending"])
def test_alternate_reopening_revalidates_generation_after_real_write(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """A real reopen write cannot report success after checkout replacement."""
    from kew.graph.driver import tick
    from kew.worktree import write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _claim_continuation(store, tmp_path, run_id="child")
    [record] = store.list_continuations(EPIC)
    transition = store.mark_continuation_awaiting

    def write_then_replace(*args: Any, **kwargs: Any) -> bool:
        changed = transition(*args, **kwargs)
        assert changed
        write_checkout_generation(worktree)
        return changed

    monkeypatch.setattr(store, "mark_continuation_awaiting", write_then_replace)
    if route == "settlement":
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
        store.save(run)

        def action() -> None:
            tick._settle_terminal_continuation(store, config, record, run)

    elif route == "missing_run":
        store._conn.execute(
            "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
            ((NOW - timedelta(minutes=20)).isoformat(), "1010"),
        )
        store._conn.commit()
        [reservation] = store.list_epic_reservations(EPIC)

        def action() -> None:
            tick._reconcile_missing_run(store, config, reservation, now=NOW)

    else:
        run = RunRecord(
            run_id="child",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=0,
            attempt_pgid=2**31 - 1,  # This attempt already exited.
        )
        store.save(run)
        [reservation] = store.list_epic_reservations(EPIC)
        monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: False)

        def action() -> None:
            tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)

    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        action()

    [after] = store.list_continuations(EPIC)
    assert after.state == "awaiting_continuation"
    assert store.list_epic_reservations(EPIC) == []
    assert worktree.joinpath("wip.txt").read_text() == "preserve me\n"
    assert store.list_audit_events(event="continuation_terminal") == []
    current = store.get("child")
    assert current is not None
    if route == "missing_run":
        assert current.status == "failure"
        assert current.error_category == "dispatch_child_died"
    elif route == "dead_pending":
        assert current.status == "killed"
        assert current.error_category == "orphaned_pending_reaped"
    else:
        assert current.status == "failure"


def test_dirty_registration_revalidates_lost_handoff_result(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lost atomic registration still checks replacement before returning."""
    from kew.graph.driver import tick
    from kew.worktree import write_checkout_generation

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    _save_terminal_run(store, "1010", "producer")
    _bind_checkout_base(store, config, worktree, "producer", "1010")
    monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: True)

    def lose_after_replacement(*_args: Any, **_kwargs: Any) -> None:
        write_checkout_generation(worktree)
        return None

    monkeypatch.setattr(
        store,
        "register_continuation_and_release_reservation",
        lose_after_replacement,
    )
    with pytest.raises(_PreexistingWorktreeRefusal, match="identity changed"):
        _register_bound_candidate(store, config, worktree, item_id="1010")

    assert store.list_continuations(EPIC) == []
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == ["reservation"]


# Real child process; a pgid reused by a foreign process under churn reads
# as alive. Serial phase: tests/conftest.py.
@pytest.mark.serial
def test_continuation_crash_with_dirty_work_reopens_same_record(
    tmp_path: Path, store: Any
) -> None:
    config = _config()
    worktree = _claim_continuation(store, tmp_path)
    config.branches.worktree_base = str(tmp_path / "worktrees")
    dead_pgid = _spawn_then_kill_pgid()
    store.save(
        RunRecord(
            run_id="continuation-1010",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=dead_pgid,
            attempt_pgid=2**31 - 1,  # This attempt already exited.
            owner_pid=dead_pgid,
            owner_start_time=_EXITED_OWNER_BIRTHS[dead_pgid],
        )
    )
    result = _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    records = store.list_continuations(EPIC)
    assert len(records) == 1
    assert records[0].original_run_id == "original-1010"
    assert records[0].state == "awaiting_continuation"
    assert records[0].continuation_run_id is None
    assert any(row.status == "killed" for row in store.list_by_item("1010"))
    assert store.list_epic_reservations(EPIC) == []
    assert result.waiting[0].node_id == "1010"
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    evidence = store.list_audit_events(event="continuation_detected")
    assert len(evidence) == 2
    dirty_crash = next(
        event
        for event in evidence
        if event.payload["continuation_run_id"] == "continuation-1010"
    )
    assert dirty_crash.payload["original_run_id"] == "original-1010"
    assert dirty_crash.payload["outcome"] == "crash_with_dirty_work"


def test_continuation_crash_before_run_row_returns_to_awaiting(
    tmp_path: Path, store: Any
) -> None:
    config = _config()
    _claim_continuation(store, tmp_path, run_id="continuation-before-row")
    config.branches.worktree_base = str(tmp_path / "worktrees")
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? "
        "WHERE epic_ref = ? AND item_id = ?",
        ((NOW - timedelta(seconds=601)).isoformat(), EPIC, "1010"),
    )
    store._conn.commit()

    result = _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    record = store.list_continuations(EPIC)[0]
    assert record.state == "awaiting_continuation"
    assert record.continuation_run_id is None
    assert store.list_epic_reservations(EPIC) == []
    assert any(
        row.error_category == "dispatch_child_died"
        for row in store.list_by_item("1010")
    )
    assert result.waiting[0].node_id == "1010"
    evidence = store.list_audit_events(event="continuation_terminal")
    assert len(evidence) == 1
    assert evidence[0].payload["original_run_id"] == "original-1010"
    assert evidence[0].payload["continuation_run_id"] == "continuation-before-row"
    assert evidence[0].payload["outcome"] == "crash_before_run_row"


def test_successful_continuation_waits_for_resume_finalizer(
    tmp_path: Path, store: Any
) -> None:
    """A terminal agent row does not prove host publication is complete."""
    config = _config()
    _claim_continuation(store, tmp_path, run_id="continuation-success")
    config.branches.worktree_base = str(tmp_path / "worktrees")
    store.save(
        RunRecord(
            run_id="continuation-success",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )

    _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    current = store.list_continuations(EPIC)[0]
    assert current.state == "continuing"
    assert current.continuation_run_id == "continuation-success"
    assert [row.run_id for row in store.list_epic_reservations(EPIC)] == [
        "continuation-success"
    ]


def test_terminal_refresh_does_not_reopen_continuation_after_probe(
    tmp_path: Path, store: Any
) -> None:
    """A terminal row observed during the reap refresh is not a dead owner."""
    from kew.graph.driver.tick import _reconcile_dead_pending

    worktree = tmp_path / "missing-worktree"
    record = store.record_continuation(
        EPIC,
        "SFD-1010",
        "original-1010",
        "agent/issue-sfd-1010",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            "continuation-1010",
            5,
            claim_evidence=fixture_claim_evidence(
                store,
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                "continuation-1010",
            ),
        )
        == "claimed"
    )
    store.save(
        RunRecord(
            run_id="continuation-1010",
            issue_num=0,
            item_id="SFD-1010",
            branch="agent/issue-sfd-1010",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    reservation = store.list_epic_reservations(EPIC)[0]
    stale_pending = RunRecord(
        run_id="continuation-1010",
        issue_num=0,
        item_id="SFD-1010",
        branch="agent/issue-sfd-1010",
        status="pending",
        exit_code=-1,
        started_at=NOW,
    )

    _reconcile_dead_pending(
        store,
        _config(),
        reservation,
        stale_pending,
        now=NOW,
    )

    current = store.list_continuations(EPIC)[0]
    assert current.state == "continuing"
    assert current.continuation_run_id == "continuation-1010"
    assert store.list_epic_reservations(EPIC) == [reservation]
    assert store.list_audit_events(event="continuation_terminal") == []


@pytest.mark.parametrize(
    "success_during_probe",
    [False, True],
    ids=["stable_live", "publisher_wins"],
)
def test_pending_reconcile_preserves_successful_publication_reservation(
    tmp_path: Path,
    success_during_probe: bool,
) -> None:
    """A probe's stale pending view cannot release a successful child claim."""
    from kew import worktree as worktree_module
    from kew.graph.driver import tick

    database = tmp_path / "pending-success.db"
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    with SqliteRunStore(str(database)) as store:
        checkout = _claim_continuation(store, tmp_path, run_id="child")
        store.save(
            RunRecord(
                run_id="child",
                issue_num=1010,
                item_id="1010",
                branch="agent/issue-1010",
                status="pending",
                exit_code=-1,
                started_at=NOW,
                pgid=0,
                total_tokens=123,
                estimated_cost_usd=0.25,
            )
        )
        [reservation] = store.list_epic_reservations(EPIC)
        continuations_before = store.list_continuations(EPIC)
        reservations_before = store.list_epic_reservations(EPIC)
        successful_rows: list[RunRecord] = []
        failures: list[BaseException] = []

        def finish() -> None:
            try:
                worktree_module.claim_checkout(checkout)
                try:
                    with SqliteRunStore(str(database)) as writer:
                        assert writer.transition_run_status(
                            "child",
                            expected_status="pending",
                            new_status="success",
                            error_category="publisher-success",
                        )
                        current = writer.get("child")
                        assert current is not None
                        successful_rows.append(current)
                finally:
                    worktree_module.release_checkout_claim(checkout)
            except BaseException as exc:  # noqa: BLE001 - assertion captures actor
                failures.append(exc)

        def probe(_run: RunRecord) -> bool:
            if not success_during_probe:
                return True
            actor = threading.Thread(target=finish)
            actor.start()
            actor.join(timeout=5)
            assert not actor.is_alive()
            assert failures == []
            return False

        try:
            tick._reconcile_one_reservation(
                store,
                config,
                reservation,
                now=max(NOW, reservation.reserved_at) + timedelta(minutes=20),
                probe=probe,
            )
        finally:
            worktree_module.release_checkout_claims()

        if success_during_probe:
            assert store.get("child") == successful_rows[0]
        else:
            assert store.get("child").status == "pending"
        assert store.list_continuations(EPIC) == continuations_before
        assert store.list_epic_reservations(EPIC) == reservations_before
        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )


@pytest.mark.parametrize(
    "route",
    ["clean_refresh", "clean_finalize", "dirty_finalize", "owned_fallback"],
)
def test_orphan_cleanup_preserves_successful_continuation_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """Every stale orphan cleanup branch preserves successful ownership."""
    from kew import worktree as worktree_module
    from kew.graph.driver import tick

    database = tmp_path / "orphan-success.db"
    with SqliteRunStore(str(database)) as store:
        config, checkout, reservation, run = _orphan_recovery_case(
            store, tmp_path, reopen=True
        )
        continuations_before = store.list_continuations(EPIC)
        reservations_before = store.list_epic_reservations(EPIC)
        successful_rows: list[RunRecord] = []
        actor_failures: list[BaseException] = []

        def finish() -> None:
            with SqliteRunStore(str(database)) as writer:
                assert writer.transition_run_status(
                    run.run_id,
                    expected_status="pending",
                    new_status="success",
                    error_category="publisher-success",
                )
                current = writer.get(run.run_id)
                assert current is not None
                successful_rows.append(current)

        if route == "clean_refresh":
            claim_checkout = tick.claim_checkout
            arrived = False

            def finish_with_claim(candidate: Path) -> None:
                try:
                    worktree_module.claim_checkout(candidate)
                    try:
                        finish()
                    finally:
                        worktree_module.release_checkout_claim(candidate)
                except BaseException as exc:  # noqa: BLE001 - assertion captures actor
                    actor_failures.append(exc)

            def claim_after_success(candidate: Path) -> None:
                nonlocal arrived
                if not arrived:
                    actor = threading.Thread(
                        target=finish_with_claim,
                        args=(candidate,),
                    )
                    actor.start()
                    actor.join(timeout=5)
                    assert not actor.is_alive()
                    assert actor_failures == []
                    arrived = True
                claim_checkout(candidate)

            monkeypatch.setattr(tick, "claim_checkout", claim_after_success)
            monkeypatch.setattr(tick, "_checkout_holds_work", lambda *_a, **_k: False)
        elif route in {"clean_finalize", "dirty_finalize"}:
            finalize = store.finalize_orphaned_pending

            def finish_before_finalize(*args: Any, **kwargs: Any) -> bool:
                finish()
                return finalize(*args, **kwargs)

            monkeypatch.setattr(
                store, "finalize_orphaned_pending", finish_before_finalize
            )
            monkeypatch.setattr(
                tick,
                "_checkout_holds_work",
                lambda *_a, **_k: route == "dirty_finalize",
            )
        else:
            monkeypatch.setattr(tick, "_preserve_dirty_orphan", lambda *_a, **_k: False)

            def finish_before_reap(*_args: Any, **_kwargs: Any) -> bool:
                finish()
                return False

            monkeypatch.setattr(tick, "_reap_orphaned_run", finish_before_reap)

        try:
            tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)
        finally:
            worktree_module.release_checkout_claims()

        assert store.get(run.run_id) == successful_rows[0]
        assert store.list_continuations(EPIC) == continuations_before
        assert store.list_epic_reservations(EPIC) == reservations_before
        assert store.list_audit_events(event="orphaned_run_reaped") == []
        assert store.list_audit_events(event="continuation_detected") == []
        assert checkout.joinpath("wip.txt").read_text(encoding="utf-8") == (
            "preserve me\n"
        )


def test_linear_continuation_evidence_uses_zero_issue_sentinel(
    tmp_path: Path, store: Any
) -> None:
    from kew.graph.driver.tick import _settle_terminal_continuation

    config = _config()
    config.tracker.type = "linear"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    _claim_continuation(
        store,
        tmp_path,
        item_id="SFD-1010",
        run_id="continuation-failure",
    )
    [claimed] = store.list_continuations(EPIC)

    run = RunRecord(
        run_id="continuation-failure",
        issue_num=0,
        item_id="SFD-1010",
        branch=claimed.branch,
        status="failure",
        exit_code=1,
        started_at=NOW,
        finished_at=NOW,
    )
    store.save(run)

    _settle_terminal_continuation(
        store,
        config,
        claimed,
        run,
    )

    [event] = store.list_audit_events(event="continuation_terminal")
    assert event.issue_num == 0


def test_continuation_claim_and_start_are_attested_once_per_generation(
    tmp_path: Path, store: Any
) -> None:
    config = _config()
    worktree = _claim_continuation(store, tmp_path, run_id="continuation-live")
    config.branches.worktree_base = str(tmp_path / "worktrees")
    store.save(
        RunRecord(
            run_id="continuation-live",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=0,
        )
    )

    for _ in range(2):
        _advance(
            deps=FakeSource(nodes=[_node("1010")], edges=[]),
            facts=FakeFactsReader(
                issue_facts_by_num={1010: _facts(1010, state="OPEN")}
            ),
            store=store,
            sink=FakeSink(),
            lock_path=tmp_path / "epic.lock",
            config=config,
        )

    claimed = store.list_audit_events(event="continuation_claimed")
    started = store.list_audit_events(event="continuation_started")
    assert len(claimed) == 1
    assert len(started) == 1
    for event in (*claimed, *started):
        assert event.payload["original_run_id"] == "original-1010"
        assert event.payload["continuation_run_id"] == "continuation-live"
        assert event.payload["worktree_identity"].startswith("sha256:")
    assert worktree.is_dir()


def test_dirty_worktree_with_live_owner_is_not_classified_as_continuation(
    tmp_path: Path, store: Any
) -> None:
    """A dirty worktree alone is not evidence that a pending owner is dead."""
    config = _config()
    _make_dirty_worktree(tmp_path / "worktrees", "1010")
    config.branches.worktree_base = str(tmp_path / "worktrees")
    deps = FakeSource(nodes=[_node("1010")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")})
    sink = FakeSink()

    import os

    _seed_pending_run(store, "1010", "run-1010", pgid=os.getpgrp())
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    assert store.list_continuations(EPIC) == []
    assert store.list_epic_reservations(EPIC)[0].item_id == "1010"
    assert store.list_by_item("1010")[0].status == "pending"
    assert result.in_flight == ("1010",)
    assert result.signal == TerminalSignal.PROGRESSING


def test_live_pending_run_is_not_reaped(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending run whose process group is still alive survives the reconcile:
    the reap fires only on a PROVEN-dead probe, never on a live/mid-startup run
    (concurrent-state invariant — prove dead, never infer it from state-shape).
    Must FAIL if the reap keys on the pending status instead of the liveness
    verdict (a healthy in-flight run would be wrongly killed)."""
    import os

    deps = FakeSource(nodes=[_node("1010")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)

    # This test process's own (live) process group stands in for a healthy child.
    _seed_pending_run(store, "1010", "run-1010", pgid=os.getpgrp())

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    rows = store.list_by_item("1010")
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert "1010" in {r.item_id for r in store.list_epic_reservations(EPIC)}
    assert result.in_flight == ("1010",)
    assert result.signal == TerminalSignal.PROGRESSING
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def test_ready_node_held_by_full_cap_surfaced_in_waiting(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1041 SAY-WHY: when the worker cap is fully consumed by a live run, a
    ready dep-free node that can't be admitted this tick is reported as a
    'waiting: admission-held' line naming the cap — not silently skipped (#669).
    Must FAIL if the admission-held surfacing is removed (2020 vanishes from the
    tick's report entirely)."""
    import os

    deps = FakeSource(nodes=[_node("1010"), _node("2020")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1010: _facts(1010, state="OPEN"),
            2020: _facts(2020, state="OPEN"),
        }
    )
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)

    # 1010 is a LIVE in-flight run (its own pgid), consuming the single slot.
    _seed_pending_run(store, "1010", "run-1010", pgid=os.getpgrp())

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    # cap=1 fully consumed by the live 1010 run → 2020 could not be admitted.
    assert result.dispatched == ()
    assert "2020" not in sink.calls
    waiting = {w.node_id: w.reason for w in result.waiting}
    assert "2020" in waiting
    assert "admission-held" in waiting["2020"]
    assert "worker cap 1" in waiting["2020"]


def test_fleet_filled_between_budget_and_admission_reports_fleet_cap(
    store: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """#1363 review: capacity can fill between _compute_dispatch_budget and the
    atomic admission — the reservation outcome is the authority. A ready node
    refused fleet_full at admission must surface as admission-held with the
    fleet-cap reason, not vanish through the quiet-skip branch."""
    deps = FakeSource(nodes=[_node("2020")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={2020: _facts(2020, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)

    # No live runs at budget computation time (budget is positive); another
    # process takes the last slot before this tick's atomic reservation.
    monkeypatch.setattr(
        store,
        "reserve_epic_node_item_under_cap",
        lambda *args, **kwargs: "fleet_full",
    )

    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert result.dispatched == ()
    assert "2020" not in sink.calls
    waiting = {w.node_id: w.reason for w in result.waiting}
    assert "2020" in waiting
    assert "admission-held" in waiting["2020"]
    assert "worker cap" in waiting["2020"]


def test_admission_hold_reason_names_fair_share_limiter(store: Any) -> None:
    """#1057 review: a node held because THIS epic hit its fair share (while the
    fleet cap still has free slots) must report the fair-share limiter, not the
    worker cap — otherwise the say-why line misdirects the operator."""
    from kew.graph.driver.tick import _compute_dispatch_budget

    store.register_epic(EPIC)
    store.register_epic("beta")  # a second contender halves the cap (fair share 2)
    store.reserve_epic_node_item(EPIC, "1", "r1")
    store.reserve_epic_node_item(EPIC, "2", "r2")  # this epic now AT its fair share

    budget = _compute_dispatch_budget(
        store, epic_ref=EPIC, fleet_cap=4, per_epic_budget=5
    )

    assert budget.budget == 0  # fair share, not the fleet cap (2 free slots), binds
    reason = budget.admission_hold_reason()
    assert "admission-held" in reason
    assert "fair share 2 of 2 active epic(s)" in reason
    assert "worker cap" not in reason


def test_admission_hold_reason_names_per_epic_limiter(store: Any) -> None:
    """#1057 review: a node held because the per-epic in-flight budget is spent
    (``max_parallel - in_flight == 0``) while the fleet cap is wide open must
    name the per-epic budget, not the worker cap."""
    from kew.graph.driver.tick import _compute_dispatch_budget

    budget = _compute_dispatch_budget(
        store, epic_ref=EPIC, fleet_cap=10, per_epic_budget=0
    )

    assert budget.budget == 0
    reason = budget.admission_hold_reason()
    assert "per-epic worker budget" in reason
    assert "worker cap" not in reason


def test_dispatch_budget_frees_slot_of_provably_dead_orphan(store: Any) -> None:
    """#1059: the fleet cap check drops a ``pending`` row whose owner is provably
    dead so a reservation-less orphan (a run that crashed before its in-process
    ``finally``) cannot permanently shrink the fleet. Without the liveness probe
    the dead row counts and the whole cap is consumed. Must FAIL if the cap
    count ignores the tick liveness probe."""
    from kew.graph.driver.tick import _compute_dispatch_budget

    # A dead orphan pending row with NO reservation (the reconciler above only
    # sweeps reservation-backed rows) — the case only the count can reclaim.
    store.save(
        RunRecord(
            run_id="dead-orphan",
            issue_num=77,
            item_id="77",
            branch="agent/issue-77",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=999999,  # a dead process group
        )
    )
    dead_probe = lambda run: False  # noqa: E731

    # Without a probe the orphan fills the whole cap → no budget.
    blind = _compute_dispatch_budget(
        store, epic_ref=EPIC, fleet_cap=1, per_epic_budget=5
    )
    assert blind.budget == 0

    # With the probe the dead orphan is not counted → the slot is free.
    live = _compute_dispatch_budget(
        store, epic_ref=EPIC, fleet_cap=1, per_epic_budget=5, liveness=dead_probe
    )
    assert live.budget == 1


def test_reap_does_not_clobber_concurrent_terminal_row(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1057 review: ``run`` handed to the reaper is a snapshot taken before the
    liveness probe. If the dispatch child wrote its OWN terminal row in that
    window (normal finish → process group gone → probe reads dead), the reaper
    must leave that fresh outcome untouched — never overwrite a ``success`` row
    with a bogus ``killed``/137 that mis-reports the node as FAILED.

    The publication lands AFTER the re-read here, so the re-read returns a
    stale ``pending`` snapshot on purpose: the guard actually under test is the
    ``status = 'pending'`` CAS predicate inside ``finalize_orphaned_pending``,
    which is what holds when the row turns terminal in the window the re-read
    cannot cover. Weakening that predicate breaks this test; weakening the
    re-read alone would not."""
    from kew.graph.driver.tick import _reap_orphaned_run

    assert store.reserve_epic_node(EPIC, 1010, "run-1010") is True
    store.save(
        RunRecord(
            run_id="run-1010",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
        )
    )
    stale = RunRecord(
        run_id="run-1010",
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        status="pending",
        exit_code=-1,
        started_at=NOW,
    )
    reservation = store.list_epic_reservations(EPIC)[0]
    original_get = store.get

    def finish_after_refresh(run_id: str) -> RunRecord | None:
        fresh = original_get(run_id)
        current = original_get(run_id)
        assert fresh is not None
        assert current is not None
        current.status = "success"
        current.exit_code = 0
        current.finished_at = NOW
        store.save(current)
        return fresh

    monkeypatch.setattr(store, "get", finish_after_refresh)

    _reap_orphaned_run(store, _config(), reservation, stale, now=NOW)

    row = store.list_by_item("1010")[0]
    assert row.status == "success"  # untouched — the real outcome survives
    assert row.exit_code == 0
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def test_container_run_not_reaped_within_startup_grace_but_reaped_after(
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1041 startup guard: a sandboxed run's row carries a container_name from
    the instant it is written — BEFORE the container is created — so a
    liveness probe reading "no such container" during the launch window must NOT
    reap it. A container-only (no-pgid) run is reaped only once it has aged past
    the startup grace, unlike a pgid-carrying host run (unambiguously dead the
    moment its process group is gone). Must FAIL if the container path skips the
    grace guard (a just-launched container is wrongly reaped)."""
    from kew.graph.driver.tick import _reconcile_reservations

    # A probe that always reports the run positively dead (the "no such
    # container" verdict), so only the grace guard decides the outcome.
    dead_probe = lambda _run: False  # noqa: E731
    monkeypatch.setattr("kew.graph.driver.tick._container_alive", lambda *a: False)

    assert store.reserve_epic_node(EPIC, 1010, "run-1010") is True
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), "1010"),
    )
    store._conn.commit()
    # Sandboxed pending row: container_name set, NO pgid (the launch-window shape).
    store.save(
        RunRecord(
            run_id="run-1010",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            container_name="kew-1010",
            container_id="a" * 64,
            attempt_pgid=2**31 - 1,  # Registered client already exited.
        )
    )

    # Within the grace (now == started_at): the just-launched container survives.
    still = _reconcile_reservations(
        EPIC, store, _config(), now=NOW, liveness=dead_probe
    )
    assert "1010" in still
    assert store.list_by_item("1010")[0].status == "pending"

    # Past the grace: the same not-found verdict is now trusted → reaped.
    later = NOW + timedelta(minutes=20)
    still = _reconcile_reservations(
        EPIC, store, _config(), now=later, liveness=dead_probe
    )
    assert "1010" not in still
    rows = store.list_by_item("1010")
    assert rows[0].status == "killed"
    assert rows[0].error_category == "orphaned_pending_reaped"


def test_container_alive_maps_no_such_container_to_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``docker inspect`` exiting non-zero with a 'no such container' stderr is a
    PROVEN-dead verdict (the sandbox is gone) → False."""
    import subprocess
    from datetime import UTC, datetime

    from kew.graph.driver.tick import _container_alive

    monkeypatch.setattr(
        "kew.sandbox.detect_runtime_cached", lambda _cfg: "/usr/bin/docker"
    )
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="Error: No such object: kew-1010"
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: completed)

    assert (
        _container_alive(
            RunRecord(
                run_id="probe",
                issue_num=1010,
                branch="agent/1010",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                container_name="kew-1010",
                container_id="a" * 64,
            ),
            _config(),
        )
        is False
    )


def test_container_alive_maps_daemon_outage_to_undeterminable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient runtime/daemon outage makes ``docker inspect`` exit non-zero
    WITHOUT a 'no such container' verdict. That must stay ``None`` (undeterminable),
    not False — else a Docker/Orbstack daemon blip reaps every healthy sandboxed
    run past the startup grace, violating the three-state contract (prove dead,
    never infer it from a probe failure). Must FAIL if the branch maps any
    non-zero exit to False."""
    import subprocess
    from datetime import UTC, datetime

    from kew.graph.driver.tick import _container_alive

    monkeypatch.setattr(
        "kew.sandbox.detect_runtime_cached", lambda _cfg: "/usr/bin/docker"
    )
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: completed)

    assert (
        _container_alive(
            RunRecord(
                run_id="probe",
                issue_num=1010,
                branch="agent/1010",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                container_name="kew-1010",
                container_id="a" * 64,
            ),
            _config(),
        )
        is None
    )


def test_container_probe_requires_exact_missing_container_and_reaper_keeps_unknown(
    store, monkeypatch
):
    import subprocess

    from kew.graph.driver.tick import _container_alive, _reconcile_reservations

    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda _: "/fake/docker")
    record = RunRecord(
        run_id="probe",
        issue_num=1010,
        branch="agent/1010",
        status="pending",
        exit_code=-1,
        started_at=NOW,
        container_name="kew-1010",
        container_id="a" * 64,
    )
    socket_error = "dial unix /var/run/docker.sock: connect: no such file or directory"
    cases = [
        (1, "", socket_error, None),
        (1, "", "Error: No such container: kew-1010", False),
        (1, "", "Error: no such object: kew-1010", False),
        (1, "", "Error: No such container: kew-10101", None),
        (1, "", "Error: No such container: KEW-1010", None),
        (1, "", "Error: No such object: another-container", None),
        (1, "", "Cannot connect to the Docker daemon", None),
        (1, "", "connect: connection refused", None),
        (1, "", "context deadline exceeded", None),
        (1, "", "permission denied", None),
        (0, f"{record.container_id} /kew-1010 true", "", True),
        (0, f"{record.container_id} /kew-1010 false", "", False),
        (0, "unexpected", "", None),
    ]
    for code, stdout, stderr, expected in cases:
        monkeypatch.setattr(
            "kew.subprocess_utils.run",
            MagicMock(
                return_value=subprocess.CompletedProcess([], code, stdout, stderr)
            ),
        )
        assert _container_alive(record, _config()) is expected, stderr

    for error in (
        OSError("socket unavailable"),
        subprocess.TimeoutExpired("inspect", 10),
    ):
        monkeypatch.setattr("kew.subprocess_utils.run", MagicMock(side_effect=error))
        assert _container_alive(record, _config()) is None

    monkeypatch.setattr(
        "kew.subprocess_utils.run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", socket_error),
    )
    store.reserve_epic_node_item(EPIC, "1010", "run-1010")
    store.save(
        RunRecord(
            run_id="run-1010",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            container_name="kew-1010",
        )
    )
    still = _reconcile_reservations(
        EPIC, store, _config(), now=NOW + timedelta(minutes=20)
    )
    assert "1010" in still
    assert store.get("run-1010").status == "pending"


# ---------------------------------------------------------------------------
# Task 5 guard: driver tick sweeps delegation pseudo-epic reservations
# ---------------------------------------------------------------------------


def test_driver_tick_sweeps_pinned_delegation_reservation(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver tick releases delegation reservations whose run has terminated.

    Task 5: the epic driver's _run_tick now also calls _reconcile_reservations
    for the delegation pseudo-epic, so a watcher that crashes after dispatch
    leaves its reservation pinned until the next driver tick — not forever.
    Must FAIL if the _DELEGATION_EPIC_REF sweep is removed from _run_tick.
    """
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    from kew.delegate.claim import DELEGATION_EPIC_REF

    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    # Plant a pinned delegation reservation: watcher dispatched "SFD-42",
    # the run finished (terminal), but the watcher died before releasing it.
    assert (
        store.reserve_epic_node_item(DELEGATION_EPIC_REF, "SFD-42", "run-deleg") is True
    )
    store.save(
        RunRecord(
            run_id="run-deleg",
            issue_num=0,
            item_id="SFD-42",
            branch="SFD-42",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    assert len(store.list_epic_reservations(DELEGATION_EPIC_REF)) == 1

    # A driver tick for a DIFFERENT epic must also sweep the delegation pseudo-epic.
    _advance(deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path)

    # The pinned delegation reservation was released as a side-effect of the tick.
    assert store.list_epic_reservations(DELEGATION_EPIC_REF) == []


# ---------------------------------------------------------------------------
# Governed-review pseudo-epic reservations (N2/#977, Task 3)
# ---------------------------------------------------------------------------


def test_overlay_unaffected_by_live_review_reservation(store: Any) -> None:
    """A live reviewer's reservation on the node's item must NOT flip the node
    IN_PROGRESS — it parks under the review pseudo-epic, never the node's epic,
    so it stays out of this epic's reserved_item_ids (N2).

    Must FAIL if a reviewer reservation is parked on the node's own epic ref:
    overlay precedence 2 is kind-blind and would derive IN_PROGRESS from it.
    """
    from kew.graph.driver.tick import REVIEW_EPIC_REF

    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", pr_number=7)}
    )
    # A fresh-context reviewer holds a slot on the node's item under the
    # review pseudo-epic while the node sits at the approval surface.
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "1", "run-rev") is True

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )
    # The node reads IN_REVIEW (its real state), not IN_PROGRESS.
    assert summary.counts == {"in_review": 1}
    assert "in_progress" not in summary.counts


def test_review_reservation_mid_startup_survives_reconcile(store: Any) -> None:
    """Reconcile false-positive guard (mandatory, concurrent-state-change): a
    reviewer mid-startup — reservation written, run row not yet — must survive
    every reconcile pass until the startup grace elapses.

    Must FAIL if the review-pseudo-epic sweep infers 'dead' from the missing
    run row instead of proving it past the grace window.
    """
    from kew.config import DispatchConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, reconcile_epic_reservations

    config = DispatchConfig()
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "42", "run-rev") is True
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), "42"),
    )
    store._conn.commit()

    # 60s in: no run row yet, but well within the 600s startup grace — the
    # reviewer is starting, not stranded. It must NOT be reclaimed.
    still = reconcile_epic_reservations(
        REVIEW_EPIC_REF, store, config, now=NOW + timedelta(seconds=60)
    )
    assert "42" in still
    assert len(store.list_epic_reservations(REVIEW_EPIC_REF)) == 1


def test_review_reservation_released_on_terminal_run(store: Any) -> None:
    """Terminal-path release: once the reviewer run terminates, the sweep
    releases its reservation — matched by run_id (#972 semantics)."""
    from kew.config import DispatchConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, reconcile_epic_reservations

    config = DispatchConfig()
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "42", "run-rev") is True
    store.save(
        RunRecord(
            run_id="run-rev",
            issue_num=0,
            item_id="42",
            branch="agent/issue-42",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            run_kind="adversarial_review",
        )
    )
    still = reconcile_epic_reservations(REVIEW_EPIC_REF, store, config, now=NOW)
    assert "42" not in still
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_live_remediation_survives_reconcile_past_600s(store: Any) -> None:
    """F3 dispatch-vanished scenario (#978 Task 9): a review_remediation run that
    is still PENDING with the node's item_id must survive the 600s startup-grace
    reconcile pass — the reconciler finds the run via the item_id fallback and
    sees it is still live.

    The F3 failure mode: before Task 9, review.py passed item_id=None to
    run_tracked_agent, so list_by_item(reservation.item_id) returned an empty
    list, the reconciler concluded the child was dead at 600s, and released the
    reservation — killing a live remediation run.  With item_id set this fallback
    succeeds and the reservation is preserved.

    Must FAIL if run_tracked_agent is called without item_id in governed mode.
    """
    from kew.config import DispatchConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, reconcile_epic_reservations

    config = DispatchConfig()
    # Reservation run_id differs from the actual run_id — dispatch_review makes a
    # reservation before spawning run_tracked_agent, which generates its own run_id.
    reservation_run_id = "res-run-id"
    actual_run_id = "actual-run-id"

    assert (
        store.reserve_epic_node_item(REVIEW_EPIC_REF, "42", reservation_run_id) is True
    )
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), "42"),
    )
    store._conn.commit()

    # Pending review_remediation run with the node's item_id — simulates a live
    # remediation that has been running for several hundred seconds.
    store.save(
        RunRecord(
            run_id=actual_run_id,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="pending",
            exit_code=0,
            started_at=NOW,
            finished_at=None,
            run_kind="review_remediation",
        )
    )

    # Reconcile at 700s — past the 600s startup grace.  The reconciler must NOT
    # release the reservation because the item_id fallback finds the pending run.
    still = reconcile_epic_reservations(
        REVIEW_EPIC_REF, store, config, now=NOW + timedelta(seconds=700)
    )
    assert "42" in still, "live remediation run was wrongly killed by the reconciler"
    assert len(store.list_epic_reservations(REVIEW_EPIC_REF)) == 1


@pytest.mark.parametrize(
    ("run_kind", "status", "category"),
    [
        ("dispatch", "refused", "budget_refused"),
        ("review_remediation", "refused", "credential_publication_refused"),
        ("dispatch", "refused", "future_refusal_category"),
        ("resolve", "success", ""),
        ("resolve", "failure", "runner_failed"),
        ("review", "success", ""),
        ("review", "failure", "runner_failed"),
        ("delegation", "failure", "delegation_item_gone"),
    ],
)
def test_unowned_terminal_row_cannot_release_reservation(
    store: Any, run_kind: str, status: Any, category: str
) -> None:
    from datetime import UTC, datetime

    from kew.delegate.run import _mark_item_gone
    from kew.graph.driver.tick import (
        REVIEW_EPIC_REF,
        reconcile_epic_reservations,
        resolve_reservation_run,
    )

    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "42", "owner")
    live = RunRecord(
        run_id="live-child",
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        run_kind="review_remediation",
    )
    store.save(live)
    if category == "delegation_item_gone":
        _mark_item_gone(store, "42")
        unrelated = store.list_by_item("42")[-1]
    else:
        unrelated = RunRecord(
            run_id="unrelated",
            issue_num=42,
            item_id="42",
            branch=live.branch,
            status=status,
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            run_kind=run_kind,
            error_category=category,
        )
        store.save(unrelated)

    [reservation] = store.list_epic_reservations(REVIEW_EPIC_REF)
    assert resolve_reservation_run(store, reservation) == live
    assert reconcile_epic_reservations(
        REVIEW_EPIC_REF, store, DispatchConfig(), now=datetime.now(UTC)
    ) == {"42"}
    assert not store.reserve_epic_node_item(REVIEW_EPIC_REF, "42", "second")
    assert store.get(live.run_id).status == "pending"
    assert store.get(unrelated.run_id) == unrelated

    # Explicit ownership still releases terminal rows, independent of category.
    store.release_epic_node_item("42", run_id="owner")
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "42", unrelated.run_id)
    assert (
        reconcile_epic_reservations(
            REVIEW_EPIC_REF, store, DispatchConfig(), now=datetime.now(UTC)
        )
        == set()
    )


@pytest.mark.parametrize("foreign_field", ["reservation_run_id", "epic_ref", "item_id"])
def test_foreign_dispatch_binding_cannot_override_legacy_child(store, foreign_field):
    from dataclasses import replace
    from datetime import UTC, datetime

    from kew.graph.driver.tick import (
        reconcile_epic_reservations,
        resolve_reservation_run,
    )

    assert store.reserve_epic_node_item(EPIC, "42", "owner")
    [reservation] = store.list_epic_reservations(EPIC)
    legacy = RunRecord(
        run_id="legacy-child",
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        status="pending",
        exit_code=-1,
        started_at=reservation.reserved_at + timedelta(seconds=1),
    )
    store.save(legacy)
    foreign = replace(
        legacy,
        run_id="foreign-child",
        status="success",
        exit_code=0,
        started_at=reservation.reserved_at + timedelta(seconds=2),
        finished_at=reservation.reserved_at + timedelta(seconds=3),
    )
    store.save(foreign)
    payload = {"reservation_run_id": "owner", "epic_ref": EPIC, "item_id": "42"}
    payload[foreign_field] = "another-owner"
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="dispatch_reservation_bound",
        run_id=foreign.run_id,
        item_id="42",
        payload=payload,
    )
    assert resolve_reservation_run(store, reservation) == legacy
    assert reconcile_epic_reservations(
        EPIC, store, _config(), now=reservation.reserved_at + timedelta(seconds=4)
    ) == {"42"}
    # A legacy terminal may start before reservation time if its finish is current.
    legacy = replace(
        legacy,
        status="failure",
        exit_code=1,
        started_at=reservation.reserved_at - timedelta(seconds=1),
        finished_at=reservation.reserved_at + timedelta(seconds=1),
        error_category="runner_failed",
    )
    store.save(legacy)
    assert resolve_reservation_run(store, reservation) == legacy
    assert (
        reconcile_epic_reservations(
            EPIC, store, _config(), now=reservation.reserved_at + timedelta(seconds=5)
        )
        == set()
    )
    assert store.get(foreign.run_id) == foreign
    assert store.get(reservation.run_id) is None


@pytest.mark.parametrize("status", ["pending", "refused", "success"])
def test_owned_dispatch_binding_precedes_timestamp_fallback(store, status):
    from dataclasses import replace
    from datetime import UTC, datetime

    from kew.graph.driver.tick import (
        reconcile_epic_reservations,
        resolve_reservation_run,
    )

    assert store.reserve_epic_node_item(EPIC, "42", "owner")
    [reservation] = store.list_epic_reservations(EPIC)
    child = RunRecord(
        run_id="bound-child",
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        status=status,
        exit_code=1 if status == "refused" else 0,
        started_at=reservation.reserved_at - timedelta(seconds=2),
        finished_at=None
        if status == "pending"
        else reservation.reserved_at - timedelta(seconds=1),
    )
    store.save(child)
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="dispatch_reservation_bound",
        run_id=child.run_id,
        item_id="42",
        payload={"reservation_run_id": "owner", "epic_ref": EPIC, "item_id": "42"},
    )
    # Explicit ownership wins despite clock skew and newer unbound item rows.
    store.save(
        replace(
            child,
            run_id="newer-unbound",
            status="success",
            started_at=reservation.reserved_at + timedelta(seconds=1),
            finished_at=reservation.reserved_at + timedelta(seconds=2),
        )
    )
    assert resolve_reservation_run(store, reservation) == child
    assert reconcile_epic_reservations(
        EPIC, store, _config(), now=reservation.reserved_at + timedelta(seconds=3)
    ) == ({"42"} if status == "pending" else set())
    assert store.get(child.run_id) == child
    assert store.get(reservation.run_id) is None


def test_driver_tick_sweeps_pinned_review_reservation(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver tick for a DIFFERENT epic also sweeps a pinned review
    reservation whose run has terminated (N2 — the tick is the catch-all sweep
    site, mirroring the delegation pseudo-epic).

    Must FAIL if the REVIEW_EPIC_REF sweep is removed from _run_tick.
    """
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    from kew.graph.driver.tick import REVIEW_EPIC_REF

    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "99", "run-rev") is True
    store.save(
        RunRecord(
            run_id="run-rev",
            issue_num=0,
            item_id="99",
            branch="agent/issue-99",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            run_kind="adversarial_review",
        )
    )
    assert len(store.list_epic_reservations(REVIEW_EPIC_REF)) == 1

    _advance(deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path)

    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


# ---------------------------------------------------------------------------
# summarize() — read-only status snapshot for `kew epic status`
# ---------------------------------------------------------------------------


def test_summarize_reports_frontier_counts_and_signal_without_mutating_store(
    store: Any,
) -> None:
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN"),
            2: _facts(2, state="OPEN"),
        }
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.frontier == ("1",)
    assert summary.counts == {"todo": 2}
    assert summary.signal == TerminalSignal.PROGRESSING
    assert summary.done == ()
    assert summary.remaining == 2
    # Read-only: a status snapshot must never reserve/consume/mutate anything.
    assert store.list_epic_reservations(EPIC) == []


def test_summarize_reports_a_ready_node_awaiting_merge_as_awaiting_merges(
    store: Any,
) -> None:
    """The documented AWAITING_MERGES state was unreachable from a read: a lone
    READY node waiting on its merge reported `stalled`, which docs/cli/epic.md
    never says that state is, and which sent an operator looking for a break in
    the normal human gate (#2183)."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", pr_number=7)}
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.counts == {"in_review": 1}
    assert [w.reason for w in summary.waiting] == ["in_review — awaiting merge (PR #7)"]
    assert summary.stuck == ()
    assert summary.signal == TerminalSignal.AWAITING_MERGES


def test_summarize_keeps_a_stuck_node_stalled_even_beside_an_awaiting_merge(
    store: Any,
) -> None:
    """A stuck node still needs an operator, so it outranks the benign merge
    wait — the same reading a tick gives it (Node I7)."""
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=7),
            2: _facts(2, state="OPEN", labels=frozenset({"agent-failed"})),
        }
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert [s.node_id for s in summary.stuck] == ["2"]
    assert summary.signal == TerminalSignal.STALLED


def test_summarize_stays_stalled_when_a_node_also_needs_a_publish(
    store: Any,
) -> None:
    """AWAITING_MERGES means a human MERGE is the only thing left. A node stuck
    at `awaiting_publication` is not stuck in the `stuck` sense, so the first cut
    called an epic that needs `kew epic publish` a benign merge wait and told the
    operator nothing was owed (#2183)."""
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    graph = build_graph(deps, EPIC)
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=2,
            item_id="2",
            branch="agent/issue-2",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_unavailable",
        )
    )
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=7),
            2: _facts(2, state="OPEN"),
        }
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.stuck == ()
    reasons = sorted(w.reason.split(" ")[0] for w in summary.waiting)
    assert reasons == ["awaiting_publication", "in_review"]
    assert summary.signal == TerminalSignal.STALLED


def test_summarize_awaiting_merges_survives_a_node_blocked_behind_the_pr(
    store: Any,
) -> None:
    """The nodes waiting BEHIND the open PR are part of the merge wait, not a
    second thing an operator owes — merging clears them too (#2183)."""
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=7),
            2: _facts(2, state="OPEN"),
        }
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.frontier == ()
    assert [w.reason for w in summary.waiting] == [
        "in_review — awaiting merge (PR #7)",
        "blocked on #1",
    ]
    assert summary.signal == TerminalSignal.AWAITING_MERGES


def test_summarize_stays_stalled_when_a_node_waits_on_an_out_of_epic_dep(
    store: Any,
) -> None:
    """A TODO node blocked on a dependency OUTSIDE the epic is not behind the
    PR: merging it cannot unblock that node, so reading every TODO waiting row
    as part of the merge wait told the operator a merge would finish an epic
    that a stranger's issue actually gates (#2183)."""
    deps = FakeSource(
        nodes=[_node("1"), _node("2")],
        edges=[Edge("9", "2")],
        statuses={"9": NodeStatus.TODO},
    )
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=7),
            2: _facts(2, state="OPEN"),
        }
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert [w.reason for w in summary.waiting] == [
        "in_review — awaiting merge (PR #7)",
        "blocked on #9",
    ]
    assert summary.signal == TerminalSignal.STALLED


def test_advance_and_summarize_agree_an_epic_owing_a_publish_is_stalled(
    tmp_path: Path, store: Any
) -> None:
    """One epic state, one signal: `advance` read a PR awaiting approval as the
    whole story and reported AWAITING_MERGES while `status` reported stalled for
    the same nodes — and `kew epic run` maps AWAITING_MERGES to sleep-and-poll,
    so it polled forever an epic that owed `kew epic publish` (#2183)."""
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    facts_by_num = {
        1: _facts(1, state="OPEN", pr_number=100),
        2: _facts(2, state="OPEN"),
    }
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=2,
            item_id="2",
            branch="agent/issue-2",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            error_category="publication_unavailable",
        )
    )
    facts = FakeFactsReader(
        issue_facts_by_num=facts_by_num,
        # PR #100 is open with no verified approval: skip/awaiting-approval.
        pr_facts_by_num={100: {"state": "OPEN"}},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert sorted(w.reason.split(" ")[0] for w in result.waiting) == [
        "awaiting_publication",
        "in_review",
    ]
    assert result.signal == TerminalSignal.STALLED
    assert summary.signal == result.signal


_UNKNOWN_MERGEABILITY_PR = {
    "number": 200,
    "state": "OPEN",
    "reviewDecision": "APPROVED",
    # GitHub has not finished computing mergeability yet: the merge pass HOLDs
    # the node transiently — no hold row, no stall clock — and re-polls next tick.
    "mergeStateStatus": "UNKNOWN",
    "headRefOid": "head200",
    "commits": [],
    "latestReviews": [
        {
            "state": "APPROVED",
            "submittedAt": "2026-07-01T00:00:00Z",
            "author": {"login": "alice", "is_bot": False, "__typename": "User"},
        }
    ],
    "author": {"login": "carol"},
}


def test_a_transient_merge_hold_does_not_stall_an_awaiting_merges_epic(
    tmp_path: Path, store: Any
) -> None:
    """A transient `mergeability-unknown` HOLD is still a PR awaiting a merge.

    The merge pass rewrites the node IN_REVIEW -> HELD mid-tick, so reading the
    freed-by-merge closure off the patched statuses dropped it: an epic whose
    only other work was a PR awaiting a human merge flipped to STALLED and
    `kew epic run` exited with the repair code instead of polling (#2183).
    """
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=100),
            2: _facts(2, state="OPEN", pr_number=200),
        },
        # PR #100 is open with no verified approval: skip/awaiting-approval.
        pr_facts_by_num={100: {"state": "OPEN"}, 200: dict(_UNKNOWN_MERGEABILITY_PR)},
        permissions_by_login={"alice": "write"},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert result.held == ("2",)
    assert result.stuck == ()
    assert store.list_epic_holds(EPIC) == []
    assert result.signal == TerminalSignal.AWAITING_MERGES


def test_advance_and_summarize_agree_through_a_transient_merge_hold(
    tmp_path: Path, store: Any
) -> None:
    """One epic state, one signal (#2183): `summarize` never runs the merge
    pass, so it reads the transiently-held node as IN_REVIEW and answered
    AWAITING_MERGES while `advance`, reading its own mid-tick HELD rewrite,
    answered STALLED for the very same nodes."""
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=100),
            2: _facts(2, state="OPEN", pr_number=200),
        },
        pr_facts_by_num={100: {"state": "OPEN"}, 200: dict(_UNKNOWN_MERGEABILITY_PR)},
        permissions_by_login={"alice": "write"},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert summary.signal == result.signal == TerminalSignal.AWAITING_MERGES


def test_a_lone_transient_merge_hold_polls_rather_than_stalling(
    tmp_path: Path, store: Any
) -> None:
    """The self-healing hold is itself a PR awaiting a merge, so it satisfies
    the awaiting-merge gate on its own: an epic whose last node waits only for
    GitHub to finish computing mergeability polls, and reads the same signal
    from `kew epic status` and `kew epic advance` (#2183)."""
    deps = FakeSource(nodes=[_node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={2: _facts(2, state="OPEN", pr_number=200)},
        pr_facts_by_num={200: dict(_UNKNOWN_MERGEABILITY_PR)},
        permissions_by_login={"alice": "write"},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert store.list_epic_holds(EPIC) == []
    assert summary.signal == result.signal == TerminalSignal.AWAITING_MERGES


_CI_PENDING_PR = {
    "number": 300,
    "state": "OPEN",
    "reviewDecision": "APPROVED",
    # Mergeable and approved: the merge gate gets all the way to the head's own
    # check runs, which are still in progress.
    "mergeStateStatus": "CLEAN",
    "headRefOid": "head300",
    "commits": [],
    "latestReviews": [
        {
            "state": "APPROVED",
            "submittedAt": "2026-07-01T00:00:00Z",
            "author": {"login": "alice", "is_bot": False, "__typename": "User"},
        }
    ],
    "author": {"login": "carol"},
}


def test_pending_head_checks_do_not_stall_an_awaiting_merges_epic(
    tmp_path: Path, store: Any
) -> None:
    """Head checks still running is a transient hold, not a stall (#2183).

    `_head_checks_hold` writes `ci-pending: ...` with no hold row and no stall
    clock — the same self-healing shape as `mergeability-unknown`. Read as a
    permanent hold, it dropped an epic whose only other work was a PR awaiting
    a human merge from AWAITING_MERGES to STALLED, and `kew epic run` exited
    with the repair code instead of polling.
    """
    deps = FakeSource(nodes=[_node("1"), _node("3")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=100),
            3: _facts(3, state="OPEN", pr_number=300),
        },
        # PR #100 is open with no verified approval: skip/awaiting-approval.
        pr_facts_by_num={100: {"state": "OPEN"}, 300: dict(_CI_PENDING_PR)},
        permissions_by_login={"alice": "write"},
        checks=("pending", "2 checks running"),
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert result.held == ("3",)
    assert result.stuck == ()
    assert store.list_epic_holds(EPIC) == []  # no hold row, so no stall clock
    assert result.signal == TerminalSignal.AWAITING_MERGES


def test_advance_and_summarize_agree_through_pending_head_checks(
    tmp_path: Path, store: Any
) -> None:
    """One epic state, one signal (#2183). `summarize` runs no merge pass, so
    a node whose head checks are still running reads as plain IN_REVIEW and
    answered AWAITING_MERGES, while `advance` — reading its own mid-tick
    IN_REVIEW -> HELD rewrite — answered STALLED for the very same node."""
    deps = FakeSource(nodes=[_node("3")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={3: _facts(3, state="OPEN", pr_number=300)},
        pr_facts_by_num={300: dict(_CI_PENDING_PR)},
        permissions_by_login={"alice": "write"},
        checks=("pending", "2 checks running"),
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert store.list_epic_holds(EPIC) == []
    assert summary.signal == result.signal == TerminalSignal.AWAITING_MERGES


def _backdate_hold(store: Any, item_id: str, *, days: int) -> str:
    """Make *item_id*'s hold row look *days* old, as a later re-poll of an
    already-running stall clock would."""
    held_at = (NOW - timedelta(days=days)).isoformat()
    store._conn.execute(
        "UPDATE epic_holds SET held_at = ? WHERE item_id = ?", (held_at, item_id)
    )
    store._conn.commit()
    return held_at


def test_pending_head_checks_over_an_expired_red_ci_hold_still_stall(
    tmp_path: Path, store: Any
) -> None:
    """An unreadable `gh` read must not turn an expired red-CI stall into a
    merge wait (#2183).

    `head_checks` reports a failed `gh` as `pending`, so this is the same code
    path as "checks still running" — but the node carries the sticky
    `ci_pending` row an earlier red-CI pass wrote, and that row's clock is past
    `stall_days`. Read as self-healing, `advance` reported AWAITING_MERGES with
    no PR under it while `status`, reading the row, reported the node stalled,
    and `kew epic run` polled forever instead of exiting with the repair code.
    """
    stall_days = 3
    deps = FakeSource(nodes=[_node("3")], edges=[])
    store.hold_epic_node(EPIC, 3, "ci_pending")
    _backdate_hold(store, "3", days=stall_days + 7)
    facts = FakeFactsReader(
        issue_facts_by_num={3: _facts(3, state="OPEN", pr_number=300)},
        pr_facts_by_num={300: dict(_CI_PENDING_PR)},
        permissions_by_login={"alice": "write"},
        checks=("pending", "check runs unreadable: gh failed"),
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_config(stall_days=stall_days),
    )

    stuck_by_id = {s.node_id: s for s in result.stuck}
    assert stuck_by_id["3"].reason == "ci-stalled"
    assert result.held == ()
    assert result.signal == TerminalSignal.STALLED


def test_a_stale_review_under_a_transient_merge_hold_still_stalls(
    tmp_path: Path, store: Any
) -> None:
    """A self-healing hold must not restart a review stall clock it never owned.

    `_stale_review_stuck` reads IN_REVIEW rows, and the merge pass rewrites a
    transiently-held node IN_REVIEW -> HELD mid-tick — so a PR already past
    `stall_days` since its last push dropped out of the stale-review scan the
    moment GitHub happened not to have computed its mergeability. `advance`
    then reported AWAITING_MERGES for a node `status` reported `review-stalled`,
    and `kew epic run` polled forever instead of exiting with the repair code
    (#2183).
    """
    stall_days = 3
    deps = FakeSource(nodes=[_node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            2: _facts(
                2,
                state="OPEN",
                pr_number=200,
                pr_pushed_at=(NOW - timedelta(days=stall_days + 7)).isoformat(),
            )
        },
        pr_facts_by_num={200: dict(_UNKNOWN_MERGEABILITY_PR)},
        permissions_by_login={"alice": "write"},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_config(stall_days=stall_days),
    )

    assert [(s.node_id, s.reason) for s in result.stuck] == [("2", "review-stalled")]
    assert store.list_epic_holds(EPIC) == []  # the hold owns no clock of its own
    assert result.signal == TerminalSignal.STALLED


def test_advance_and_summarize_agree_on_a_stale_review_held_transiently(
    tmp_path: Path, store: Any
) -> None:
    """One epic state, one signal (#2183), for the head-check hold shape too.

    `summarize` runs no merge pass, so it reads the node as plain IN_REVIEW and
    runs the stall clock on its PR; `advance` read its own mid-tick HELD rewrite
    and skipped that clock entirely. Both must name the same stalled node.
    """
    stall_days = 3
    deps = FakeSource(nodes=[_node("3")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            3: _facts(
                3,
                state="OPEN",
                pr_number=300,
                pr_pushed_at=(NOW - timedelta(days=stall_days + 7)).isoformat(),
            )
        },
        pr_facts_by_num={300: dict(_CI_PENDING_PR)},
        permissions_by_login={"alice": "write"},
        checks=("pending", "2 checks running"),
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_config(stall_days=stall_days),
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(stall_days=stall_days),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert (
        [(s.node_id, s.reason) for s in summary.stuck]
        == [(s.node_id, s.reason) for s in result.stuck]
        == [("3", "review-stalled")]
    )
    assert summary.signal == result.signal == TerminalSignal.STALLED


def test_a_fresh_review_under_a_transient_merge_hold_still_awaits_a_merge(
    tmp_path: Path, store: Any
) -> None:
    """The guard is the stall clock, not the hold: a transiently-held PR pushed
    to inside `stall_days` is still a PR awaiting a human's merge, and both
    readings keep saying so (#2183)."""
    stall_days = 3
    deps = FakeSource(nodes=[_node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            2: _facts(
                2,
                state="OPEN",
                pr_number=200,
                pr_pushed_at=(NOW - timedelta(days=stall_days - 1)).isoformat(),
            )
        },
        pr_facts_by_num={200: dict(_UNKNOWN_MERGEABILITY_PR)},
        permissions_by_login={"alice": "write"},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_config(stall_days=stall_days),
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(stall_days=stall_days),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert result.held == ("2",)
    assert result.stuck == summary.stuck == ()
    assert summary.signal == result.signal == TerminalSignal.AWAITING_MERGES


def test_pending_head_checks_keep_a_live_red_ci_hold_and_its_clock(
    tmp_path: Path, store: Any
) -> None:
    """The same read inside the stall clock: the row (and the `held_at` the
    clock reads) survives untouched, so the clock keeps running toward the
    stall instead of restarting on every unreadable read — and the node reads
    HELD on CI, which is PROGRESSING, not a PR awaiting a human's merge."""
    stall_days = 3
    deps = FakeSource(nodes=[_node("3")], edges=[])
    store.hold_epic_node(EPIC, 3, "ci_pending")
    held_at = _backdate_hold(store, "3", days=stall_days - 1)
    facts = FakeFactsReader(
        issue_facts_by_num={3: _facts(3, state="OPEN", pr_number=300)},
        pr_facts_by_num={300: dict(_CI_PENDING_PR)},
        permissions_by_login={"alice": "write"},
        checks=("pending", "check runs unreadable: gh failed"),
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_config(stall_days=stall_days),
    )

    holds = store.list_epic_holds(EPIC)
    assert [(h.item_id, h.reason) for h in holds] == [("3", "ci_pending")]
    assert holds[0].held_at.isoformat() == held_at
    assert result.held == ("3",)
    assert result.stuck == ()
    assert result.signal == TerminalSignal.PROGRESSING


def test_only_the_self_healing_holds_count_as_awaiting_a_merge(store: Any) -> None:
    """Only the holds GitHub clears by itself — mergeability still being
    computed, and the head's own checks still running — join the merge wait.
    Every other non-`ci_pending` hold (an unverifiable head, a refused
    containment posture, a gate that could not run) is a broken pipeline an
    operator must fix, so it stays out and the epic stays STALLED (#2183, and
    `_is_awaiting_merge`'s rule for the queue's held outcomes)."""
    nodes = [_node("1"), _node("2"), _node("3")]
    graph = build_graph(FakeSource(nodes=nodes, edges=[]), EPIC)

    outcome = _classify_verdicts(
        [
            MergeVerdict("1", "held", MERGEABILITY_UNKNOWN_REASON),
            MergeVerdict("2", "held", MERGE_VERIFY_NO_BRANCH),
            MergeVerdict("3", "held", f"{CI_PENDING_VERDICT_PREFIX}2 checks running"),
        ],
        graph=graph,
        holds_after={},
        now=NOW,
        stall_days=3,
    )

    assert outcome.held == ["1", "2", "3"]
    assert outcome.held_live is False
    assert outcome.held_transient == ["1", "3"]


def test_a_red_ci_hold_is_not_self_healing(store: Any) -> None:
    """The sticky `ci_pending` hold a FAILED head writes shares the `ci-`
    verdict vocabulary with the transient pending one, but it carries a hold
    row and a stall clock: it is red CI an operator must fix, so it must never
    be read as a PR merely awaiting a merge (#2183)."""
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)

    outcome = _classify_verdicts(
        [MergeVerdict("1", "held", "ci-failure: tests failed", "ci_pending")],
        graph=graph,
        holds_after={},
        now=NOW,
        stall_days=3,
    )

    assert outcome.held == ["1"]
    assert outcome.held_transient == []


def test_an_honored_hold_row_is_never_a_self_healing_hold(store: Any) -> None:
    """The self-healing claim is about the NODE, not the verdict word (#2183).

    `is_self_healing_hold` answers "is this verdict one GitHub clears by
    itself"; what `held_transient` has to mean is "no hold row, no stall clock,
    and the PR under it is still open awaiting a merge". A node already held on
    any OTHER honored reason — a refused containment posture here — whose
    mergeability GitHub simply has not finished computing this tick satisfies
    the first and not the second: the row, and the clock reading it, are live.
    Classified off the verdict alone it joined the merge wait anyway — the
    `ci_pending` divergence generalized to every honored hold.
    """
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)
    hold = EpicHold(1, EPIC, "sandbox_containment: no image mapped", NOW, "1")

    outcome = _classify_verdicts(
        [MergeVerdict("1", "held", MERGEABILITY_UNKNOWN_REASON)],
        graph=graph,
        holds_after={"1": hold},
        now=NOW,
        stall_days=3,
    )

    assert outcome.held == ["1"]
    assert outcome.held_transient == []
    assert outcome.held_live is True


def test_an_expired_honored_hold_row_stalls_under_a_transient_verdict(
    store: Any,
) -> None:
    """And once that live clock runs out, the node is STALLED under its OWN
    reason — the same word `_held_signal` gives `kew epic status` for the same
    row (`_aged_hold_reason`, #2150), not `ci-stalled` and not a merge wait."""
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)
    reason = "sandbox_containment: no image mapped"
    hold = EpicHold(1, EPIC, reason, NOW - timedelta(days=10), "1")

    outcome = _classify_verdicts(
        [MergeVerdict("1", "held", MERGEABILITY_UNKNOWN_REASON)],
        graph=graph,
        holds_after={"1": hold},
        now=NOW,
        stall_days=3,
    )

    assert outcome.held == []
    assert outcome.held_transient == []
    assert outcome.held_live is False
    assert outcome.stuck_by_id["1"].reason == reason


def test_advance_and_summarize_agree_over_an_expired_containment_hold(
    tmp_path: Path, store: Any
) -> None:
    """The end-to-end shape of the same bug (#2183): one epic state, one signal.

    The node carries an expired `sandbox_containment` hold row and an open PR
    whose mergeability GitHub has not finished computing. `summarize` reads the
    expired row straight off the store and says STALLED; `advance`, folding the
    transient verdict into the merge wait, said AWAITING_MERGES — so
    `kew epic run` polled on an epic that owed an operator a fix.
    """
    stall_days = 3
    deps = FakeSource(nodes=[_node("2")], edges=[])
    store.hold_epic_node(EPIC, 2, "sandbox_containment: no image mapped")
    _backdate_hold(store, "2", days=stall_days + 7)
    facts = FakeFactsReader(
        issue_facts_by_num={2: _facts(2, state="OPEN", pr_number=200)},
        pr_facts_by_num={200: dict(_UNKNOWN_MERGEABILITY_PR)},
        permissions_by_login={"alice": "write"},
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_config(stall_days=stall_days),
    )
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(stall_days=stall_days),
        store=store,
        facts=facts,
        now=NOW,
    )

    assert {s.node_id: s.reason for s in result.stuck} == {
        s.node_id: s.reason for s in summary.stuck
    }
    assert summary.signal == result.signal == TerminalSignal.STALLED


def _summarize_review(store: Any, *, pr_head: str, reviewed: list[str]) -> Any:
    """One in-review node with an open PR on *pr_head* and a READY round per
    entry of *reviewed*, in order — the last is the newest round."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    for head in reviewed:
        store.record_review_round(EPIC, "1", head_sha=head, verdict="READY")
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", pr_number=7, head_oid=pr_head)}
    )
    return summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=facts,
        now=NOW,
    )


def _needs_you_rows(summary: Any) -> list[Any]:
    """The console's needs-you rows for this summary's journaled waits — the
    reason the tick writes into the snapshot's ``stuck`` field
    (``tick._write_journal``) read back through the service layer."""
    from kew.service.needs_you import _epic_wait_rows

    nodes = [
        {"id": w.node_id, "status": "in_review", "stuck": w.reason}
        for w in summary.waiting
    ]
    return _epic_wait_rows([(EPIC, {"nodes": nodes})], None)


def test_summarize_identifies_a_review_state_by_its_head_and_round(
    store: Any,
) -> None:
    """Two READY verdicts on different heads are two states: #2177 went READY,
    failed the merge verify, was fixed and went READY again, and a watcher with
    no head on the event dropped the second one (#2183)."""
    summary = _summarize_review(store, pr_head="7a40744b", reviewed=["7a40744b"])

    assert summary.identity == {"1": "head:7a40744b round:1"}
    events = attention_events(EPIC, summary)
    assert [(e.kind, e.identity) for e in events] == [
        (AttentionKind.MERGE_READY, "head:7a40744b round:1")
    ]
    assert [r.attention_kind for r in _needs_you_rows(summary)] == ["merge_ready"]


def test_a_round_on_another_head_than_the_pr_s_is_not_merge_ready(
    store: Any,
) -> None:
    """#2561: the PR head the facts read is A while the newest round (2) was
    READY on B. Nothing approved A, so no merge_ready event and no merge-ready
    needs-you row — and the identity names both heads, never pairing round 2
    with a commit it did not review."""
    summary = _summarize_review(
        store, pr_head="dd020669", reviewed=["c0404f4d", "a62a42fd"]
    )

    assert summary.identity == {"1": "head:dd020669 reviewed:a62a42fd round:2"}
    assert [w.reason for w in summary.waiting] == ["review pending on dd020669 (PR #7)"]
    events = attention_events(EPIC, summary)
    assert all(e.kind is not AttentionKind.MERGE_READY for e in events)
    assert _needs_you_rows(summary) == []


def test_publication_identity_names_the_run_its_reason_names(store: Any) -> None:
    """The identity of a publication hold is the run kew could not publish —
    the same authoritative run the operator-facing reason names (#2183)."""
    store.save(
        RunRecord(
            run_id="implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="awaiting_publication",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
            error_category="implementation-publish-failed",
        )
    )

    assert _publication_identity("1", store) == "run:implementation"
    assert "implementation" in _awaiting_publication_reason("1", store, None)


def test_summarize_all_done_is_complete_signal(store: Any) -> None:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="CLOSED", state_reason="COMPLETED")}
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.done == ("1",)
    assert summary.remaining == 0
    assert summary.signal == TerminalSignal.COMPLETE


def test_summarize_reports_unarmed_linear_work_without_calling_it_blocked(
    store: Any,
) -> None:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", admission_status=NodeStatus.QUEUED)
        }
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.frontier == ()
    assert summary.counts == {"queued": 1}
    assert [(item.node_id, item.reason) for item in summary.waiting] == [
        ("1", "waiting for human arm")
    ]
    assert summary.signal == TerminalSignal.STALLED


def test_summarize_remediating_and_blocked_is_progressing_not_stalled(
    store: Any,
) -> None:
    """A remediating node is live compute, so status must not read STALLED.

    #2107: `epic status` printed `Signal: stalled` for an epic whose only
    non-done nodes were remediating (or blocked behind one) while the driver's
    own tick was running those remediations. `advance()` already counts the
    governed-review lifecycle as progress; the read-only snapshot did not, so
    an operator — and `epic run`, which exits non-zero on STALLED — saw a stall
    that was not there.
    """
    config = _config()
    config.review.adversarial = True
    config.review.blocking = True
    store.record_review_round(
        EPIC,
        "1",
        head_sha="head1",
        verdict="NEEDS_CHANGES",
        sidecar_digest="digest",
    )
    node_facts = NodeFacts(
        issue_num=1,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=77,
        pr_url="https://github.com/o/r/pull/77",
        review_decision=None,
        merge_state_status=None,
        head_oid="head1",
        pr_pushed_at=None,
    )
    facts = FakeFactsReader(
        issue_facts_by_num={1: node_facts, 2: _facts(2, state="OPEN")}
    )
    deps = FakeSource(
        nodes=[_node("1", status=NodeStatus.IN_REVIEW), _node("2")],
        edges=[Edge("1", "2")],
    )
    graph = build_graph(deps, EPIC)

    summary = summarize(EPIC, graph, config=config, store=store, facts=facts, now=NOW)

    # Nothing dispatchable, nothing in flight, no hold — the pre-#2107 reading.
    assert summary.frontier == ()
    assert summary.in_flight == ()
    assert summary.held == ()
    assert summary.counts == {"remediating": 1, "todo": 1}
    assert summary.signal == TerminalSignal.PROGRESSING


def test_a_remediation_launched_this_tick_counts_as_live_review() -> None:
    """A tick that just launched a remediation is progressing (#2107).

    The launch does not move the node out of the review lifecycle, so the
    status scan normally catches it — but a tick whose action-patch rewrote
    that node's status would otherwise report a stall for work it started
    milliseconds earlier. Fail toward "still progressing", never toward a
    false STALLED.
    """
    assert _review_live({"1": DriverStatus.REMEDIATING}) is True
    assert _review_live({"1": DriverStatus.ADVERSARIAL_REVIEW}) is True
    assert _review_live({"1": DriverStatus.IN_REVIEW}) is False
    assert _review_live({"1": DriverStatus.IN_REVIEW}, ["1"]) is True


def test_summarize_invalid_graph_is_all_nodes_stuck(store: Any) -> None:
    # Node "1" depends on "999", which is not a known node (dangling).
    deps = FakeSource(nodes=[_node("1")], edges=[Edge("999", "1")])
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.frontier == ()
    assert summary.signal == TerminalSignal.STALLED
    assert {s.node_id for s in summary.stuck} == {"1"}


def test_summarize_keeps_an_aged_out_non_ci_hold_reason(store: Any) -> None:
    """An honored hold that ages past `stall_days` keeps its OWN reason.

    `_held_signal` is the read-only stand-in for `_classify_held_verdict`,
    which clocks the CI gate and nothing else. Relabelling every aged hold
    `ci-stalled` pointed the operator at a PR that was never the problem —
    and cost the attention stream (#2150) the hold's own kind and action, so a
    `sandbox_containment` hold older than `stall_days` stopped naming anybody.
    """
    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    reason = "sandbox_containment: runner 'claude' has no container image mapped"
    store.hold_epic_node(EPIC, 1, reason)
    store._conn.execute(
        "UPDATE epic_holds SET held_at = ? WHERE item_id = ?",
        ((NOW - timedelta(days=10)).isoformat(), "1"),
    )
    store._conn.commit()
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})

    summary = summarize(
        EPIC, graph, config=_config(stall_days=3), store=store, facts=facts, now=NOW
    )

    assert {s.node_id: s.reason for s in summary.stuck} == {"1": reason}
    # The aged hold is still no reason to read PROGRESSING.
    assert summary.signal is TerminalSignal.STALLED
    # ...and the one attention event it earns names the containment, not a PR.
    [event] = attention_events(EPIC, summary)
    assert event.kind is AttentionKind.SANDBOX_REFUSED
    assert event.item == "1"


def test_summarize_still_calls_an_aged_out_ci_gate_ci_stalled(store: Any) -> None:
    """The CI gate is the one hold that ages into a diagnosis of its own —
    the vocabulary `_classify_held_verdict` writes on the `advance` path."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    store.hold_epic_node(EPIC, 1, "ci_pending")
    store._conn.execute(
        "UPDATE epic_holds SET held_at = ? WHERE item_id = ?",
        ((NOW - timedelta(days=10)).isoformat(), "1"),
    )
    store._conn.commit()
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})

    summary = summarize(
        EPIC, graph, config=_config(stall_days=3), store=store, facts=facts, now=NOW
    )

    assert {s.node_id: s.reason for s in summary.stuck} == {"1": "ci-stalled"}
    assert summary.held == ()
    assert summary.signal is TerminalSignal.STALLED


def test_summarize_never_calls_merge_pass(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status snapshot must never (re-)evaluate the merge gate — that
    would mean a `gh` write from a read-only call."""
    from kew.graph.driver import tick as tick_module

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("summarize must never call merge_pass")

    monkeypatch.setattr(tick_module, "merge_pass", _boom)

    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    store.hold_epic_node(EPIC, 1, "ci_pending")
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN", pr_number=42)},
        pr_facts_by_num={42: {"mergeStateStatus": "BLOCKED"}},
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert "1" in summary.held
    assert summary.signal == TerminalSignal.PROGRESSING


def test_summarize_merged_unclosed_performs_no_github_write(
    store: Any,
) -> None:
    calls: list[list[str]] = []
    pr = {
        "number": 77,
        "url": "https://github.com/o/r/pull/77",
        "state": "CLOSED",
        "headRefName": "agent/issue-1",
        "reviewDecision": None,
        "mergeStateStatus": "CLEAN",
        "headRefOid": "head1",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if is_pr_batch(command):
            return pr_batch_result(command, [pr])
        elif command[:3] == ["gh", "pr", "list"]:
            payload: Any = [pr]
        elif command[:3] == ["gh", "issue", "view"]:
            payload = {
                "number": 1,
                "state": "OPEN",
                "stateReason": None,
                "labels": [{"name": "pr-pending-review"}],
                "closedByPullRequestsReferences": [],
            }
        else:
            raise AssertionError(f"status performed an unexpected call: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    store.save(
        RunRecord(
            run_id="implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    store.hold_epic_node(EPIC, 1, "ci_pending")
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)
    summary = summarize(
        EPIC,
        graph,
        config=_config(),
        store=store,
        facts=GitHubFactsReader("o/r", store=store, host="github.com", run=run),
        now=NOW,
    )

    assert summary.counts == {"held": 1}
    assert not any(command[:3] == ["gh", "issue", "edit"] for command in calls)
    holds = {hold.item_id: hold.reason for hold in store.list_epic_holds(EPIC)}
    assert holds == {"1": "ci_pending"}
    assert store.get("implementation").pr_url is None


def test_summarize_never_reads_or_repairs_a_missing_closing_marker(
    store: Any,
) -> None:
    pr = {
        "number": 77,
        "url": "https://github.com/o/r/pull/77",
        "state": "OPEN",
        "headRefName": "agent/issue-1",
        "reviewDecision": None,
        "mergeStateStatus": "BLOCKED",
        "headRefOid": "head1",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["gh", "api"] and not is_pr_batch(command):
            raise AssertionError(f"status performed an API call: {command}")
        if is_pr_batch(command):
            return pr_batch_result(command, [pr])
        elif command[:3] == ["gh", "pr", "list"]:
            payload: Any = [pr]
        elif command[:3] == ["gh", "pr", "view"]:
            payload = pr
        elif command[:3] == ["gh", "issue", "view"]:
            payload = {
                "number": 1,
                "state": "OPEN",
                "stateReason": None,
                "labels": [],
                "closedByPullRequestsReferences": [],
            }
        else:
            raise AssertionError(f"status performed an unexpected call: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    store.save(
        RunRecord(
            run_id="implementation",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            pr_url=pr["url"],
        )
    )
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)

    summary = summarize(
        EPIC,
        graph,
        config=_config(),
        store=store,
        facts=GitHubFactsReader("o/r", store=store, host="github.com", run=run),
        now=NOW,
    )

    assert summary.counts == {"in_review": 1}
    assert summary.pr_links[0].has_closing_ref is False


# ---------------------------------------------------------------------------
# #661 — per-node stall reasons + budget-preflight HOLD un-hold on cap raise
# ---------------------------------------------------------------------------


def _budget_config(*, per_issue_usd: float) -> Any:
    from kew.config import BudgetConfig, DispatchConfig, EpicConfig

    return DispatchConfig(
        epic=EpicConfig(stall_days=3),
        budget=BudgetConfig(per_issue_usd=per_issue_usd),
    )


def test_summarize_surfaces_per_node_stall_reasons(store: Any) -> None:
    """A STALLED snapshot names WHY each node is stuck/waiting: an in_review
    node awaiting merge, a failed-terminal node with its exit/category, and a
    dependency-blocked node naming its upstream (#661)."""
    deps = FakeSource(
        nodes=[_node("1"), _node("2"), _node("3")],
        edges=[Edge("1", "3"), Edge("2", "3")],
    )
    graph = build_graph(deps, EPIC)
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN", pr_number=662),
            2: _facts(2, state="OPEN", labels=frozenset({"agent-failed"})),
            3: _facts(3, state="OPEN"),
        }
    )
    store.save(
        RunRecord(
            run_id="r2",
            issue_num=2,
            branch="agent/issue-2",
            status="failure",
            exit_code=143,
            started_at=NOW,
            finished_at=NOW,
            error_category="circuit_breaker",
        )
    )

    summary = summarize(
        EPIC, graph, config=_config(), store=store, facts=facts, now=NOW
    )

    assert summary.signal == TerminalSignal.STALLED
    waiting = {w.node_id: w.reason for w in summary.waiting}
    stuck = {s.node_id: s.reason for s in summary.stuck}
    assert waiting["1"] == "in_review — awaiting merge (PR #662)"
    assert stuck["2"] == "failed-terminal (exit 143, circuit_breaker)"
    assert waiting["3"] == "blocked on #1, #2"
    # A node is surfaced once: the failed node is stuck, not also waiting.
    assert "2" not in waiting


def test_blocked_on_reason_bare_for_linear_hashed_for_github() -> None:
    """The blocked-on waiting line routes every upstream id through item_ref:
    a Linear id renders BARE (no GitHub-ism ``#``), while a GitHub-numeric id
    stays ``#N`` byte-identically (#SFD-140)."""
    from kew.graph.domain import Graph
    from kew.graph.driver.tick import _blocked_todo_reason

    linear = Graph(
        [_node("SFD-133"), _node("SFD-134")],
        [Edge("SFD-133", "SFD-134")],
    )
    assert (
        _blocked_todo_reason("SFD-134", linear, {"SFD-133": NodeStatus.TODO})
        == "blocked on SFD-133"
    )

    github = Graph([_node("1"), _node("2")], [Edge("1", "2")])
    assert _blocked_todo_reason("2", github, {"1": NodeStatus.TODO}) == "blocked on #1"


def test_advance_holds_budget_node_then_unholds_when_cap_raised(
    tmp_path: Path, store: Any
) -> None:
    """A budget_preflight HOLD keeps the node out of the frontier and surfaces
    its reason; raising the per-issue cap un-holds it on the very next tick,
    without ever relabelling the node (#661)."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    # A prior run cost $17.08 — over a $15 cap, under a $25 cap.
    store.save(
        RunRecord(
            run_id="r1",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            estimated_cost_usd=17.08,
        )
    )
    store.hold_epic_node(
        EPIC,
        1,
        "budget_preflight: spent $17.08 > per_issue cap $15.00 — "
        "raise budget.per_issue_usd in kew.toml",
    )

    res1 = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_budget_config(per_issue_usd=15.0),
    )
    assert res1.dispatched == ()
    assert sink.calls == []
    assert [h.issue_num for h in store.list_epic_holds(EPIC)] == [1]
    assert any(
        w.node_id == "1" and w.reason.startswith("budget_preflight")
        for w in res1.waiting
    )

    res2 = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_budget_config(per_issue_usd=25.0),
    )
    assert store.list_epic_holds(EPIC) == []  # un-held on the next tick
    assert res2.dispatched == ("1",)
    assert sink.calls == ["1"]


def _review_pool_config(*, max_remediation_usd: float) -> Any:
    from kew.config import DispatchConfig, EpicConfig, ReviewConfig

    return DispatchConfig(
        epic=EpicConfig(stall_days=3),
        review=ReviewConfig(adversarial=True, max_remediation_usd=max_remediation_usd),
    )


def test_advance_surfaces_a_review_budget_hold_then_unholds_on_a_raised_cap(
    tmp_path: Path, store: Any
) -> None:
    """#2153: an exhausted remediation pool is a needs-a-person state, not a
    silent one. The node stays out of the frontier, the tick NAMES the pool and
    the key to raise instead of reading `progressing`, and raising that key
    un-holds it on the very next tick — no `kew epic unhold` required."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    store.save(
        RunRecord(
            run_id="rem-1",
            issue_num=1,
            item_id="1",
            branch="agent/review-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            estimated_cost_usd=43.97,
            run_kind="review_remediation",
        )
    )
    store.hold_epic_node(
        EPIC,
        1,
        "review_budget: remediation pool $40.00 exhausted (spent $43.97) — "
        "raise [review].max_remediation_usd",
    )

    res1 = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_review_pool_config(max_remediation_usd=40.0),
    )
    assert res1.dispatched == ()
    assert sink.calls == []
    assert [h.issue_num for h in store.list_epic_holds(EPIC)] == [1]
    assert res1.signal is not TerminalSignal.PROGRESSING
    [waiting] = [w for w in res1.waiting if w.node_id == "1"]
    assert waiting.reason.startswith("review_budget: ")
    assert "spent $43.97" in waiting.reason
    assert "raise [review].max_remediation_usd" in waiting.reason

    res2 = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_review_pool_config(max_remediation_usd=60.0),
    )
    assert store.list_epic_holds(EPIC) == []  # un-held on the next tick
    assert res2.dispatched == ("1",)


# ---------------------------------------------------------------------------
# 12. Post-action summary: a merge/close done THIS tick shows in THIS tick's
#     output, not on the next one (SFD-139).
# ---------------------------------------------------------------------------


class _LinearReader:
    """Str-keyed facts reader whose item state flips CLOSED after a close."""

    def __init__(self) -> None:
        self.item_state = "OPEN"

    def issue_facts(self, item_id: str) -> NodeFacts:
        # "SFD-1" carries a merged PR; "SFD-2" is a bare dependent.
        if item_id == "SFD-1":
            return _facts(0, state=self.item_state, pr_number=42)
        return _facts(0, state="OPEN")

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        # The PR merged out-of-band (Linear PRs carry no `Closes #N`).
        return {"number": pr_number, "state": "MERGED", "author": {"login": "carol"}}

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return True

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return "success", "CI green"


class _CloseFlipTracker:
    """FakeTracker whose `close` flips its reader's item to CLOSED (#817)."""

    def __init__(self, reader: _LinearReader) -> None:
        self.reader = reader
        self.closed: list[str] = []

    def close(self, item_id: str) -> None:
        self.closed.append(item_id)
        self.reader.item_state = "CLOSED"


class _MergedLinearFacts(_LinearReader):
    """Linear facts seam with the persisted-PR REST reconciliation hook."""

    def __init__(
        self,
        *,
        reconciled: dict[str, dict[str, Any]] | None,
        item_state: str = "OPEN",
    ) -> None:
        super().__init__()
        self.item_state = item_state
        self.reconciled = reconciled
        self.rest_urls: list[str] = []

    def issue_facts(self, item_id: str) -> NodeFacts:
        if item_id == "SFD-1":
            return _facts(
                0,
                state=self.item_state,
                state_reason="COMPLETED" if self.item_state == "CLOSED" else None,
                pr_number=42,
            )
        return _facts(0, state="OPEN")

    def pr_facts_for_urls(self, urls: list[str]) -> dict[str, dict[str, Any]] | None:
        self.rest_urls.extend(urls)
        return self.reconciled


class _RecordingLinearTracker:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def close(self, item_id: str) -> None:
        self.closed.append(item_id)


def test_merged_linear_rest_batch_resolves_persisted_url_and_normalizes_state():
    responses = iter(
        [
            type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"number": 77, "state": "closed", '
                    '"merged_at": "2026-07-31T12:00:00Z", '
                    '"html_url": "https://github.com/o/r/pull/77"}',
                },
            )()
        ]
    )
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> Any:
        calls.append(command)
        return next(responses)

    reader = GitHubFactsReader("o/r", run=run)
    resolved = reader.pr_facts_for_urls(["https://github.com/o/r/pull/77"])

    assert resolved is not None
    assert resolved["https://github.com/o/r/pull/77"]["state"] == "MERGED"
    assert calls == [["gh", "api", "--method", "GET", "repos/o/r/pulls/77"]]


def test_merged_linear_item_is_reconciled_before_review_overlay(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged PR absent from the active PR listing still closes Linear.

    The review reservation must be killed before overlay construction, or the
    three-node incident's merged item remains an adversarial-review zombie.
    """
    from kew.config import DispatchConfig, EpicConfig, TrackerConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, advance

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    deps = FakeSource(
        nodes=[_node("SFD-1"), _node("SFD-2"), _node("SFD-3")],
        edges=[Edge("SFD-1", "SFD-2"), Edge("SFD-2", "SFD-3")],
    )
    url = "https://github.com/o/r/pull/42"
    facts = _MergedLinearFacts(reconciled={url: {"state": "MERGED"}})
    tracker = _RecordingLinearTracker()
    config = DispatchConfig(
        tracker=TrackerConfig(type="linear"), epic=EpicConfig(stall_days=3)
    )
    config.review.adversarial = True
    store.save(
        RunRecord(
            run_id="implementation-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            pr_url=url,
        )
    )
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "SFD-1", "review-1") is True
    store.save(
        RunRecord(
            run_id="review-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="pending",
            exit_code=0,
            started_at=NOW,
            run_kind="adversarial_review",
        )
    )

    result = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )

    assert facts.rest_urls == [url]
    assert tracker.closed == ["SFD-1"]
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
    assert store.get("review-1").status == "killed"
    assert result.closed == ("SFD-1",)
    assert result.done == ("SFD-1",)
    assert result.dispatched == ("SFD-2",)
    assert result.signal == TerminalSignal.PROGRESSING


@pytest.mark.parametrize("reconciled", [None, {}])
def test_merged_linear_reconciliation_failure_keeps_live_reviewer_and_fails_closed(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    reconciled: dict[str, dict[str, Any]] | None,
) -> None:
    from kew.config import DispatchConfig, EpicConfig, TrackerConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, advance

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    deps = FakeSource(nodes=[_node("SFD-1")], edges=[])
    url = "https://github.com/o/r/pull/42"
    facts = _MergedLinearFacts(reconciled=reconciled)
    tracker = _RecordingLinearTracker()
    config = DispatchConfig(
        tracker=TrackerConfig(type="linear"),
        epic=EpicConfig(stall_days=3),
    )
    config.review.adversarial = True
    store.save(
        RunRecord(
            run_id="implementation-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            pr_url=url,
        )
    )
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "SFD-1", "review-1") is True
    store.save(
        RunRecord(
            run_id="review-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="pending",
            exit_code=0,
            started_at=NOW,
            run_kind="adversarial_review",
        )
    )

    result = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )

    assert tracker.closed == []
    assert len(store.list_epic_reservations(REVIEW_EPIC_REF)) == 1
    assert store.get("review-1").status == "pending"
    assert result.done == ()
    assert result.signal == TerminalSignal.PROGRESSING


@pytest.mark.parametrize("run_kind", ["adversarial_review", "review_remediation"])
def test_provider_completed_linear_item_cleans_review_generation_before_overlay(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    run_kind: str,
) -> None:
    """A Completed Linear node must still reconcile its persisted merged PR.

    The active PR list no longer contains the merged PR, while a live review
    reservation and a stale hold still exist.  Reconciliation must clean that
    generation before the overlay is built, without reopening the terminal
    item or returning it to the frontier on a later tick.
    """
    from kew.config import DispatchConfig, EpicConfig, TrackerConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, advance

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    deps = FakeSource(
        nodes=[
            _node("SFD-1", status=NodeStatus.DONE),
            _node("SFD-2"),
            _node("SFD-3"),
        ],
        edges=[Edge("SFD-1", "SFD-2"), Edge("SFD-2", "SFD-3")],
    )
    url = "https://github.com/o/r/pull/42"
    facts = _MergedLinearFacts(
        reconciled={url: {"state": "MERGED"}}, item_state="CLOSED"
    )
    tracker = _RecordingLinearTracker()
    config = DispatchConfig(
        tracker=TrackerConfig(type="linear"), epic=EpicConfig(stall_days=3)
    )
    config.review.adversarial = True
    store.save(
        RunRecord(
            run_id="implementation-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            pr_url=url,
        )
    )
    store.hold_epic_node_item(EPIC, "SFD-1", "review_pending")
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "SFD-1", "review-1") is True
    store.save(
        RunRecord(
            run_id="review-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="pending",
            exit_code=0,
            started_at=NOW,
            run_kind=run_kind,
        )
    )

    result1 = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )

    assert tracker.closed == []  # Linear already reported Completed.
    assert store.list_epic_holds(EPIC) == []
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
    assert store.get("review-1").status == "killed"
    assert result1.done == ("SFD-1",)
    assert result1.dispatched == ("SFD-2",)
    assert "SFD-1" not in result1.dispatched

    # The same merged evidence is safe to observe again: the killed generation
    # is not resurrected and the terminal node is not redispatched.
    result2 = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic-2.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )
    assert tracker.closed == []
    assert result2.done == ("SFD-1",)
    assert result2.dispatched == ()


def test_merged_linear_partial_rest_batch_fails_closed(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial persisted-PR batch cannot settle even its merged subset."""
    from kew.config import DispatchConfig, EpicConfig, TrackerConfig
    from kew.graph.driver.tick import REVIEW_EPIC_REF, advance

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    deps = FakeSource(
        nodes=[_node("SFD-1"), _node("SFD-2")],
        edges=[Edge("SFD-1", "SFD-2")],
    )
    url1 = "https://github.com/o/r/pull/41"
    url2 = "https://github.com/o/r/pull/42"
    facts = _MergedLinearFacts(
        reconciled={url1: {"state": "MERGED"}},
    )
    tracker = _RecordingLinearTracker()
    config = DispatchConfig(
        tracker=TrackerConfig(type="linear"), epic=EpicConfig(stall_days=3)
    )
    config.review.adversarial = True
    for item_id, url in (("SFD-1", url1), ("SFD-2", url2)):
        store.save(
            RunRecord(
                run_id=f"implementation-{item_id}",
                issue_num=0,
                item_id=item_id,
                branch=f"agent/issue-{item_id.lower()}",
                status="success",
                exit_code=0,
                started_at=NOW,
                finished_at=NOW,
                pr_url=url,
            )
        )
    store.hold_epic_node_item(EPIC, "SFD-1", "review_pending")
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "SFD-1", "review-1") is True
    store.save(
        RunRecord(
            run_id="review-1",
            issue_num=0,
            item_id="SFD-1",
            branch="agent/issue-sfd-1",
            status="pending",
            exit_code=0,
            started_at=NOW,
            run_kind="adversarial_review",
        )
    )

    result = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )

    assert facts.rest_urls == [url1, url2]
    assert tracker.closed == []
    assert store.list_epic_holds(EPIC)[0].item_id == "SFD-1"
    assert len(store.list_epic_reservations(REVIEW_EPIC_REF)) == 1
    assert store.get("review-1").status == "pending"
    assert result.done == ()


def test_tick_summary_reflects_own_close_not_stale_in_review(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SFD-139: a tick whose merge pass closes SFD-1 (Linear went Done
    mid-tick) must report SFD-1 as done — not `in_review — awaiting merge` —
    and must NOT report STALLED when that close unblocks its dependent SFD-2.

    Reproduces the live confusion: SFD-1 reads IN_REVIEW in the tick-start
    overlay, the merge pass observes its PR merged out-of-band and closes the
    item, and the summary/journal must fold that action in rather than printing
    the pre-action snapshot.
    """
    from kew.config import DispatchConfig, EpicConfig, TrackerConfig
    from kew.graph.driver.tick import advance

    # No `gh` in this container; the Linear happy path shells out to nothing.
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)

    deps = FakeSource(
        nodes=[_node("SFD-1"), _node("SFD-2")],
        edges=[Edge("SFD-1", "SFD-2")],  # SFD-2 depends on SFD-1
    )
    reader = _LinearReader()
    tracker = _CloseFlipTracker(reader)
    config = DispatchConfig(
        tracker=TrackerConfig(type="linear"), epic=EpicConfig(stall_days=3)
    )

    result = advance(
        EPIC,
        config=config,
        store=store,
        deps=deps,
        facts=reader,
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=[],
        repo="o/r",
        tracker=tracker,
    )

    assert tracker.closed == ["SFD-1"]
    # SFD-1's own close shows this tick, not next.
    assert result.closed == ("SFD-1",)
    assert result.done == ("SFD-1",)
    assert result.remaining == 1
    # No stale "in_review — awaiting merge" line for the node we just closed.
    assert all(w.node_id != "SFD-1" for w in result.waiting)
    # The close unblocked SFD-2, so the tick is PROGRESSING, not STALLED.
    assert result.signal == TerminalSignal.PROGRESSING

    # The journal row agrees with stdout: SFD-1 done, SFD-2 on the frontier.
    tick = store.latest_epic_tick(EPIC)
    assert tick is not None
    by_id = {n["id"]: n for n in tick.snapshot["nodes"]}
    assert by_id["SFD-1"]["status"] == "done"
    assert tick.snapshot["frontier"] == ["SFD-2"]
    assert tick.snapshot["signal"] == "progressing"


# ---------------------------------------------------------------------------
# 14. Human-review backpressure (I15): saturation stops fresh independent
#     launches without evicting, stalling, or throttling anything else.
# ---------------------------------------------------------------------------


def _review_config(*, cap: int = 1, workers: int = 2) -> DispatchConfig:
    """A driver with room for two workers, so the review cap is the only brake."""
    return DispatchConfig(
        epic=EpicConfig(stall_days=3),
        parallel=ParallelConfig(max_workers=workers),
        human_review=HumanReviewConfig(max_pending_reviews=cap),
    )


def _observe_reviews(
    store: Any, *, refs: tuple[str, ...] = (), generation: int = 0
) -> None:
    """Persist what the delivery repository was last seen holding (I14)."""
    store.record_review_observation(
        RemoteReviewObservation(
            delivery_repo=REPO,
            availability=REVIEW_REMOTE_AVAILABLE,
            observed_at=NOW.isoformat(),
            generation=generation,
            obligations=tuple(ReviewObligation(ref=ref) for ref in refs),
        )
    )


def _two_ready_nodes() -> tuple[FakeSource, FakeFactsReader]:
    return (
        FakeSource(nodes=[_node("1"), _node("2")], edges=[]),
        FakeFactsReader(
            issue_facts_by_num={1: _facts(1, state="OPEN"), 2: _facts(2, state="OPEN")}
        ),
    )


def _waiting_reasons(result: Any) -> dict[str, str]:
    return {node.node_id: node.reason for node in result.waiting}


def test_saturated_review_capacity_withholds_fresh_frontier_work(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    sink = FakeSink()
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=1),
    )

    assert result.dispatched == ()
    assert sink.calls == []
    reasons = _waiting_reasons(result)
    assert set(reasons) == {"1", "2"}
    assert "review-backpressure" in reasons["1"]
    assert "holding 1 of 1" in reasons["1"]
    # Ready work waiting on a person is not a stalled epic.
    assert result.signal is TerminalSignal.PROGRESSING


def test_backpressure_reserves_nothing_and_evicts_nothing(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=1),
    )

    # No second reservation mechanism: withholding takes no epic slot, and it
    # gives back none of the review capacity already in front of a person.
    assert store.list_epic_reservations(EPIC) == []
    assert store.list_by_item("1") == []
    assert (
        store.review_capacity_occupancy(
            delivery_repo=REPO, observation=store.review_observation(REPO)
        )
        == 1
    )


def test_below_capacity_dispatches_the_whole_ready_frontier(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    sink = FakeSink()
    _observe_reviews(store)

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=1),
    )

    assert result.dispatched == ("1", "2")
    assert result.waiting == ()


def test_one_slot_goes_to_the_p1_node_not_the_lower_numbered_p2(
    tmp_path: Path, store: Any
) -> None:
    """#2575: a frontier of p2 #100 and p1 #200 dispatches #200 first."""
    deps = FakeSource(nodes=[_node("100"), _node("200")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            100: _facts(100, state="OPEN", labels=frozenset({"p2-medium"})),
            200: _facts(200, state="OPEN", labels=frozenset({"p1-high"})),
        }
    )
    sink = FakeSink()

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=1,
    )

    assert sink.calls == ["200"]
    assert result.dispatched == ("200",)
    [rank] = result.dispatch_ranks
    assert (rank.node_id, rank.priority, rank.blast) == ("200", 1, 0)


def test_falling_below_the_cap_restores_the_frontier_without_a_throttle(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    held = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_review_config(cap=1),
    )
    # The person merged it: the next reconciliation sees an empty queue.
    _observe_reviews(store, generation=1)
    freed = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_review_config(cap=1),
    )

    assert held.dispatched == ()
    # BOTH launch on the very next tick — no one-launch throttle, no residual
    # penalty for having been withheld.
    assert freed.dispatched == ("1", "2")
    assert sink.calls == ["1", "2"]


def test_saturation_never_withholds_work_this_host_already_started(
    tmp_path: Path, store: Any
) -> None:
    # Node "1" failed and was retried: dispatching it RESUMES work, it does
    # not start new work. Node "2" has never run. Only "2" is withheld.
    deps, facts = _two_ready_nodes()
    sink = FakeSink()
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="failure",
            exit_code=1,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    store.mark_epic_retry(EPIC, 1)
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=1),
    )

    assert result.dispatched == ("1",)
    assert sink.calls == ["1"]
    assert set(_waiting_reasons(result)) == {"2"}


def test_saturation_leaves_in_flight_completion_alone(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="pending",
            exit_code=-1,
            started_at=NOW,
        )
    )
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=1),
    )

    assert result.in_flight == ("1",)
    assert "1" not in _waiting_reasons(result)


def test_an_unreadable_delivery_repository_holds_fresh_work(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    sink = FakeSink()
    store.record_review_observation(
        unavailable_review_observation(
            delivery_repo=REPO, generation=0, observed_at=NOW.isoformat()
        )
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=5),
    )

    assert sink.calls == []
    assert "not an empty one" in _waiting_reasons(result)["1"]


def test_a_never_reconciled_repository_dispatches_as_before(
    tmp_path: Path, store: Any
) -> None:
    # Nothing has ever been observed: this host has no normalized obligation
    # to be honest about, and the driver invents no pressure from silence.
    deps, facts = _two_ready_nodes()
    sink = FakeSink()

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=0),
    )

    assert result.dispatched == ("1", "2")


def test_a_restarted_driver_reconstructs_the_same_backpressure(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _two_ready_nodes()
    lock_path = tmp_path / "epic.lock"
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    first = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=lock_path,
        config=_review_config(cap=1),
    )

    restarted = SqliteRunStore(str(tmp_path / "run.db"))
    try:
        second = _advance(
            deps=deps,
            facts=facts,
            store=restarted,
            sink=FakeSink(),
            lock_path=lock_path,
            config=_review_config(cap=1),
        )
    finally:
        restarted.close()

    assert _waiting_reasons(first) == _waiting_reasons(second)
    assert second.dispatched == ()


def test_a_second_driver_cannot_out_race_the_cap(tmp_path: Path, store: Any) -> None:
    # Two epics over the same delivery repository. Fairness decides who goes
    # first among admitted nodes; it never decides whether the cap applies.
    deps, facts = _two_ready_nodes()
    _observe_reviews(store, refs=("https://github.com/o/r/pull/9",))

    other = advance(
        "epic/dag-2",
        config=_review_config(cap=1),
        store=store,
        deps=FakeSource(nodes=[_node("3")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={3: _facts(3, state="OPEN")}),
        dispatch_sink=FakeSink(),
        max_parallel=5,
        now=NOW,
        lock_path=tmp_path / "epic-2.lock",
        approvers=[],
        repo=REPO,
    )
    mine = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=_review_config(cap=1),
    )

    assert other.dispatched == ()
    assert mine.dispatched == ()


# ---------------------------------------------------------------------------
# I12 (Tasks 49-51): an authorized no-change advances a dependency exactly
# once, only after an ACKNOWLEDGED source synchronization, and only through an
# explicitly authorized per-dependent effect.
#
# Every test here uses two dependents on purpose: the property under test is
# never "the dependent moved" — it is "the second one did not move with it,
# and a restart did not move the first one twice".
# ---------------------------------------------------------------------------

NC_BASE = "a" * 40
NC_DIGEST = "sha256:" + "b" * 64
NC_CHECK_DIGEST = "sha256:" + "c" * 64


def _nc_fan_out() -> tuple[FakeSource, FakeFactsReader]:
    """Node "1" is the no-change; "2" and "3" both depend on it."""
    deps = FakeSource(
        nodes=[_node("1"), _node("2"), _node("3")],
        edges=[Edge("1", "2"), Edge("1", "3")],
    )
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="CLOSED", state_reason="COMPLETED"),
            2: _facts(2, state="OPEN"),
            3: _facts(3, state="OPEN"),
        }
    )
    return deps, facts


def _nc_config() -> DispatchConfig:
    """Two dependents may both be in flight: the cap is not what is under test."""
    return DispatchConfig(
        epic=EpicConfig(stall_days=3), parallel=ParallelConfig(max_workers=5)
    )


def _nc_authorize(store: Any, *, item: str = "1", run_id: str = "run-nc") -> Any:
    """Carry one zero-diff exit for *item* all the way to ``authorized``."""
    intent = NoChangeIntent(
        source_kind="github",
        source_id=item,
        run_id=run_id,
        attempt_number=1,
        base_sha=NC_BASE,
        item_id=f"gh:{item}",
    )
    judgment = store.open_no_change_judgment(
        intent=intent, policy=CLOSE_IF_HOST_VERIFIED, now=NOW
    )
    store.attest_no_change(
        judgment_id=judgment.judgment_id,
        evidence=NoChangeEvidence(
            trusted_base_sha=NC_BASE,
            observed_base_sha=NC_BASE,
            package_digest=NC_DIGEST,
            package_scope="workspace",
            package_entry_count=0,
            package_deletion_count=0,
            checks=(
                NamedCheck(name="pytest", outcome="passed", digest=NC_CHECK_DIGEST),
            ),
            attempted_changes=(),
        ),
        now=NOW,
    )
    store.decide_no_change(
        judgment_id=judgment.judgment_id,
        contract=AlreadySatisfiedContract(
            contract_version=ALREADY_SATISFIED_CONTRACT_VERSION,
            item_id=f"gh:{item}",
            satisfied=True,
            producer_kind="host",
            verifier="kew.host_verifier",
            digest=NC_DIGEST,
        ),
        now=NOW,
    )
    return store.enqueue_outcome_sync(
        judgment_id=judgment.judgment_id, targets=(("github", item),), now=NOW
    )[0]


def _nc_ack_sync(store: Any, job: Any) -> None:
    """What one authority acknowledging the outcome looks like (I11)."""
    store.observe_outcome_sync(
        sync_job_id=job.sync_job_id,
        observation_kind="closed_by_kew",
        observation_ref=f"{REPO}#1",
        now=NOW,
    )
    store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=NOW)


def _nc_effects(store: Any, judgment_id: str) -> list[tuple[str, str]]:
    return [
        (e.dependent_id, e.state) for e in store.list_dependency_effects(judgment_id)
    ]


def test_provider_closed_no_change_alone_satisfies_no_dependent(
    tmp_path: Path, store: Any
) -> None:
    """The provider says CLOSED/COMPLETED and a person has authorized the
    no-change — and neither dependent moves, because no authority has
    acknowledged the outcome yet."""
    deps, facts = _nc_fan_out()
    job = _nc_authorize(store)
    sink = FakeSink()

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
    )

    assert result.dispatched == ()
    assert sink.calls == []
    assert result.done == ()
    # The intents ARE recorded — durably, before anything is advanced — so a
    # restart resumes the dependent list the decision was made against.
    assert _nc_effects(store, job.judgment_id) == [
        ("2", EFFECT_INTENT),
        ("3", EFFECT_INTENT),
    ]


def test_an_observed_warning_is_no_change_synchronization_a_dependent_may_use(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _nc_fan_out()
    job = _nc_authorize(store)
    store.observe_outcome_sync(
        sync_job_id=job.sync_job_id,
        observation_kind="conflicting_remote_state",
        observation_ref=f"{REPO}#1",
        now=NOW,
    )

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
    )

    assert result.dispatched == ()
    assert {state for _, state in _nc_effects(store, job.judgment_id)} == {
        EFFECT_INTENT
    }


def test_no_change_dependency_effects_advance_one_dependent_per_tick(
    tmp_path: Path, store: Any
) -> None:
    deps, facts = _nc_fan_out()
    job = _nc_authorize(store)
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    # Tick 1 records the intents; nothing is acknowledged, so nothing moves.
    assert (
        _advance(
            deps=deps,
            facts=facts,
            store=store,
            sink=sink,
            lock_path=lock_path,
            config=_nc_config(),
        ).dispatched
        == ()
    )

    _nc_ack_sync(store, job)

    # Tick 2 acknowledges exactly ONE effect — and "3" stays ineligible.
    second = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_nc_config(),
    )
    assert second.dispatched == ("2",)
    assert _nc_effects(store, job.judgment_id) == [
        ("2", EFFECT_ACKED),
        ("3", EFFECT_INTENT),
    ]
    # The effect advanced ONE edge. It is not a merge, not a close, and not a
    # terminal for the node that owns it.
    assert (second.merged, second.done) == ((), ())

    # Tick 3 acknowledges the second, and only then is the judgment complete.
    third = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=lock_path,
        config=_nc_config(),
    )
    assert third.dispatched == ("3",)
    assert _nc_effects(store, job.judgment_id) == [
        ("2", EFFECT_ACKED),
        ("3", EFFECT_ACKED),
    ]
    assert store.get_no_change_judgment(job.judgment_id).state == COMPLETED
    assert sink.calls == ["2", "3"]
    # Only now — every dependent acknowledged — is the no-change node itself
    # terminal, and it is terminal as a no-change, not as a merge.
    assert third.done == ("1",)
    assert third.merged == ()


def test_a_crash_after_the_first_dependency_effect_never_repeats_it(
    tmp_path: Path, store: Any
) -> None:
    """Restart between the two dependents: the first is not applied twice, and
    the second was never exposed early by the crash."""
    deps, facts = _nc_fan_out()
    job = _nc_authorize(store)
    lock_path = tmp_path / "epic.lock"

    _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=lock_path,
        config=_nc_config(),
    )
    _nc_ack_sync(store, job)
    first_tick = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=lock_path,
        config=_nc_config(),
    )
    assert first_tick.dispatched == ("2",)
    applied = store.list_dependency_effects(job.judgment_id)[0]

    # The host dies here. A new driver reopens the same database.
    with SqliteRunStore(str(tmp_path / "run.db")) as restarted:
        after = _advance(
            deps=deps,
            facts=facts,
            store=restarted,
            sink=FakeSink(),
            lock_path=lock_path,
            config=_nc_config(),
        )
        effects = restarted.list_dependency_effects(job.judgment_id)

    assert after.dispatched == ("3",)
    replayed = effects[0]
    assert replayed.state == EFFECT_ACKED
    assert (replayed.acked_at, replayed.applied_at, replayed.updated_at) == (
        applied.acked_at,
        applied.applied_at,
        applied.updated_at,
    )
    assert replayed.audit_event_id == applied.audit_event_id
    assert [e.dependent_id for e in effects] == ["2", "3"]


def test_a_rejected_no_change_advances_no_dependent(tmp_path: Path, store: Any) -> None:
    deps, facts = _nc_fan_out()
    job = _nc_authorize(store)
    lock_path = tmp_path / "epic.lock"
    _advance(deps=deps, facts=facts, store=store, sink=FakeSink(), lock_path=lock_path)
    _nc_ack_sync(store, job)
    _advance(deps=deps, facts=facts, store=store, sink=FakeSink(), lock_path=lock_path)
    store.reject_no_change(
        judgment_id=job.judgment_id,
        actor_id="reviewer",
        reason="the absence was not the answer",
        now=NOW,
    )

    sink = FakeSink()
    result = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    # "2" was reserved by the earlier effect; "3" was never exposed and a
    # rejection is not a route to exposing it.
    assert "3" not in result.dispatched
    assert "3" not in sink.calls
    assert _nc_effects(store, job.judgment_id)[1] == ("3", EFFECT_INTENT)


def test_a_dependency_effect_left_applying_is_acknowledged_not_reapplied(
    tmp_path: Path, store: Any
) -> None:
    """A host that died mid-apply left ``applying`` behind. The only safe next
    move is to acknowledge that dependent — never to claim it a second time,
    and never to skip past it to the one behind it."""
    deps, facts = _nc_fan_out()
    job = _nc_authorize(store)
    lock_path = tmp_path / "epic.lock"
    _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=lock_path,
        config=_nc_config(),
    )
    _nc_ack_sync(store, job)
    interrupted = store.list_dependency_effects(job.judgment_id)[0]
    store.begin_dependency_effect(effect_id=interrupted.effect_id, now=NOW)
    claimed = store.list_dependency_effects(job.judgment_id)[0]

    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=FakeSink(),
        lock_path=lock_path,
        config=_nc_config(),
    )

    assert result.dispatched == ("2",)
    resumed = store.list_dependency_effects(job.judgment_id)[0]
    assert resumed.state == EFFECT_ACKED
    # The claim was never re-taken: the applying stamp is the interrupted one.
    assert resumed.applied_at == claimed.applied_at
    assert _nc_effects(store, job.judgment_id)[1] == ("3", EFFECT_INTENT)


def test_repair_skips_merged_prs_and_unobservable_facts() -> None:
    """A merged PR and a PR whose closing-reference fact is not observable
    (None) produce zero gh calls; only an observed False on an open PR does."""
    from kew.graph.driver.tick import _needs_closing_marker

    base = dict(
        issue_num=1,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=77,
        pr_url="https://github.com/o/r/pull/77",
        review_decision=None,
        merge_state_status=None,
        head_oid="h",
        pr_pushed_at=None,
        pr_link_source="ledger",
    )
    assert not _needs_closing_marker(
        NodeFacts(**base, pr_state="MERGED", pr_has_closing_ref=False)
    )
    assert not _needs_closing_marker(
        NodeFacts(**base, pr_state="OPEN", pr_has_closing_ref=None)
    )
    assert not _needs_closing_marker(
        NodeFacts(**base, pr_state="OPEN", pr_has_closing_ref=True)
    )
    assert _needs_closing_marker(
        NodeFacts(**base, pr_state="OPEN", pr_has_closing_ref=False)
    )


def test_advance_classifies_a_retained_replacement_refusal_as_stuck(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained replacement whose run history retention removed refuses the
    fresh dispatch as a stuck node naming that run; the tick does not abort."""
    from kew.continuation_recovery import RetainedReplacementRefusal

    def refuse(store: Any, *, epic_ref: str, item_id: str) -> None:
        raise RetainedReplacementRefusal(
            f"{item_id} preserves checkout ck-b from replacement run 'replacement';"
            " its run history is unavailable after retention; fresh dispatch is refused"
        )

    monkeypatch.setattr(
        "kew.graph.driver.dispatch.require_retained_replacement", refuse
    )
    config = _config()
    config.project.repo = "o/r"
    sink = FakeSink()
    result = _advance(
        deps=FakeSource(nodes=[_node("1012")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1012: _facts(1012, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )

    assert sink.calls == []
    assert [stuck.node_id for stuck in result.stuck] == ["1012"]
    assert "replacement run 'replacement'" in result.stuck[0].reason
    assert store.list_epic_reservations(EPIC) == []


def _issued_driver_event(
    store, run_id, *, expiry="2099-01-01T00:00:00Z", key_hash="a" * 64
):
    store.append_audit_event(
        timestamp=NOW.isoformat(),
        event="sandbox_resolved",
        run_id=run_id,
        actor_type="policy",
        actor_id="credential-issuer",
        payload={
            "phase": "issuance",
            "issued_by": "driver",
            "credential_issuer": {
                "issuer": "openrouter",
                "key_hash": key_hash,
                "limit_usd": 7,
                "expires_at": expiry,
            },
        },
    )


@pytest.mark.parametrize(
    "status",
    [
        "success",
        "failure",
        "refused",
        "budget_exceeded",
        "budget_stopped",
        "budget_stop",
        "killed",
        "awaiting_publication",
    ],
)
def test_tick_revokes_terminal_driver_key_once(store, tmp_path, monkeypatch, status):
    from kew import credential_issuer

    revoke = MagicMock()
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    _issued_driver_event(store, "terminal-key")
    store.save(
        RunRecord(
            run_id="terminal-key",
            issue_num=1944,
            branch="agent/1944",
            status=status,
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    for _ in range(2):
        _advance(
            deps=FakeSource(nodes=[_node("1")], edges=[]),
            facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
            store=store,
            sink=FakeSink(),
            lock_path=tmp_path / "epic.lock",
        )
    revoke.assert_called_once_with("a" * 64, issued_key="")
    events = store.list_audit_events(event="sandbox_resolved", run_id="terminal-key")
    assert events[0].payload["phase"] == "revocation"
    assert events[0].payload["credential_issuer"]["revoked"] is True


def test_reconciler_revokes_expired_key_without_run(store, monkeypatch):
    from kew import credential_issuer
    from kew.graph.driver.credentials import reconcile_driver_keys

    revoke = MagicMock()
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    _issued_driver_event(store, "expired-key", expiry="2000-01-01T00:00:00Z")
    reconcile_driver_keys(store, now=NOW)
    revoke.assert_called_once_with("a" * 64, issued_key="")
    assert (
        store.list_audit_events(run_id="expired-key")[0].payload["credential_issuer"][
            "revoked"
        ]
        is True
    )


def test_reconciler_bounds_pages_and_retries_failure_once_per_tick(store, monkeypatch):
    from kew import credential_issuer
    from kew.graph.driver import credentials

    monkeypatch.setattr(credentials, "KEY_SCAN_LIMIT", 1)
    revoke = MagicMock(side_effect=RuntimeError("management-secret-1944"))
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    for run_id in ("first", "duplicate"):
        _issued_driver_event(store, run_id, expiry="2000-01-01T00:00:00Z")
    seen = set()
    for _ in range(4):
        credentials.reconcile_driver_keys(store, now=NOW, seen=seen)
    revoke.assert_called_once()
    event = store.list_audit_events(run_id="first")[0]
    assert event.payload["credential_issuer"]["revoked"] is False
    assert event.payload["credential_issuer"]["reason"] == "issuer unavailable"
    assert "management-secret" not in str(event)
    next_seen = set()
    for _ in range(5):
        credentials.reconcile_driver_keys(
            store, now=NOW + timedelta(seconds=30), seen=next_seen
        )
    assert revoke.call_count == 2


def test_driver_exit_reconciles_even_after_exception(store, monkeypatch):
    from kew import credential_issuer
    from kew.graph.driver.credentials import driver_key_cleanup

    revoke = MagicMock()
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    with (
        pytest.raises(RuntimeError, match="tick interrupted"),
        driver_key_cleanup(store),
    ):
        _issued_driver_event(store, "exit-key", expiry="2000-01-01T00:00:00Z")
        raise RuntimeError("tick interrupted")
    revoke.assert_called_once()


def test_credential_scan_seeks_the_issuance_index(store):
    queries = []
    store._conn.set_trace_callback(queries.append)
    store.list_driver_key_issuances(after_id=17, limit=100)
    store._conn.set_trace_callback(None)
    query = next(sql for sql in queries if sql.startswith("SELECT * FROM audit_events"))
    plan = [row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("idx_audit_driver_issuance (id>?)" in step for step in plan)
    assert all("TEMP B-TREE" not in step for step in plan)


@pytest.mark.parametrize("route", ["reap", "preserve", "claimed"])
def test_round8_cleanup_paths_pass_store(tmp_path, store, monkeypatch, route):
    """Every repeated cleanup route passes its durable store before returning."""
    from kew.graph.driver import tick

    _seed_pending_run(store, "1010", "orphan", pgid=0)
    run = store.get("orphan")
    reservation = store.list_epic_reservations(EPIC)[0]
    cleanup = MagicMock(return_value=False)
    monkeypatch.setattr("kew.fleet.stop_orphaned_attempt", cleanup)
    args = (store, _config(), reservation, run)
    if route == "reap":
        result = tick._reap_orphaned_run(*args, now=NOW)
    elif route == "preserve":
        result = tick._preserve_dirty_orphan(*args, now=NOW)
    else:
        result = tick._preserve_claimed_dirty_orphan(
            *args, tmp_path, "generation", None, now=NOW
        )
    assert not result
    cleanup.assert_called_once_with(run, store)
    assert store.get(run.run_id).status == "pending"
    assert store.list_epic_reservations(EPIC) == [reservation]


def test_container_probe_uses_the_runtime_the_run_launched_under(monkeypatch):
    """Re-audit F12: a docker-to-podman switch must not read a live run as gone."""
    from datetime import UTC, datetime

    from kew import run_liveness, stop_recovery
    from kew.graph.driver.tick import _container_alive

    identity = "a" * 64
    record = RunRecord(
        run_id="probe",
        issue_num=1978,
        branch="agent/1978",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        container_name="kew-1978",
        container_id=identity,
        container_runtime="docker",
    )
    config = DispatchConfig()
    config.sandbox.runtime = "podman"
    live = subprocess.CompletedProcess([], 0, f"{identity} /kew-1978 true", "")
    gone = subprocess.CompletedProcess([], 1, "", "Error: No such container: kew-1978")
    inspected: list[str] = []

    def detect(sandbox_config):
        return f"/usr/bin/{sandbox_config.runtime}"

    def run(argv, *, check, timeout):
        assert (check, timeout) == (False, 10)
        inspected.append(argv[0])
        return live if argv[0] == "docker" else gone

    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", detect)
    monkeypatch.setattr("kew.subprocess_utils.run", run)
    assert _container_alive(record, config) is True
    assert inspected == ["docker"]

    def container_state(runtime, identity_arg, name):
        assert (identity_arg, name) == (identity, "kew-1978")
        inspected.append(runtime)
        return "running" if runtime == "docker" else "absent"

    monkeypatch.setattr(stop_recovery, "container_state", container_state)
    assert run_liveness._recorded_owner_is_dead(record, config) is False
    assert inspected[-1] == "docker"
    # PR #1984 review round 3: the recorded runtime's "absent" is trusted, so a
    # run launched before the switch can still be proven dead.
    del inspected[:]

    def gone_run(argv, *, check, timeout):
        inspected.append(argv[0])
        return gone

    monkeypatch.setattr("kew.subprocess_utils.run", gone_run)
    assert _container_alive(record, config) is False
    assert inspected == ["docker"]

    def absent_state(runtime, identity_arg, name):
        inspected.append(runtime)
        return "absent"

    monkeypatch.setattr(stop_recovery, "container_state", absent_state)
    assert run_liveness._recorded_owner_is_dead(record, config) is True
    assert inspected[-1] == "docker"


def test_container_probe_without_the_recorded_runtime_binary_is_unknown(monkeypatch):
    """A docker-recorded run on a host without docker is unknown, never absent."""
    from datetime import UTC, datetime

    from kew import run_liveness, stop_recovery
    from kew.graph.driver.tick import _container_alive

    record = RunRecord(
        run_id="probe",
        issue_num=1978,
        branch="agent/1978",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        container_name="kew-1978",
        container_id="a" * 64,
        container_runtime="docker",
    )
    config = DispatchConfig()
    config.sandbox.runtime = "podman"

    def detect(sandbox_config):
        return None if sandbox_config.runtime == "docker" else "/usr/bin/podman"

    def which(name):
        return None if name == "docker" else f"/usr/bin/{name}"

    def run(argv, *, check, timeout):
        raise AssertionError(f"no probe may run without its binary: {argv[0]}")

    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", detect)
    monkeypatch.setattr("kew.subprocess_utils.run", run)
    monkeypatch.setattr(stop_recovery.shutil, "which", which)
    assert _container_alive(record, config) is None
    assert stop_recovery.record_container_state("docker", record) == "unknown"
    assert run_liveness._recorded_owner_is_dead(record, config) is False


# ── the OpenShell runtime: liveness and gateway-restart recovery (N8) ───────

_SANDBOX = "kew-issue-1010"
_SANDBOX_ID = "sbx_01HN4Q8ZJ2WME5T"


class _FakeGateway:
    """The gateway calls recovery makes, with the sandbox's own lifecycle.

    Installed over :class:`kew.openshell.OpenShellClient`'s methods rather than
    over the backend or the launch module, so the real client construction,
    the real backend mapping and the real driver branch all run.
    """

    def __init__(
        self, status: str = "running", download: Any = None, agent: str = "ended"
    ) -> None:
        from kew.openshell import Sandbox

        self.status = status
        #: What the in-sandbox exec probe reports about the agent: ``running``,
        #: ``ended``, ``absent``, or ``None`` for a probe the gateway cannot run.
        self.agent = agent
        self.probes: list[str] = []
        self.identity = _SANDBOX_ID
        self.deleted = False
        self.absent = status == "absent"
        self.downloads: list[tuple[str, str]] = []
        self._download = download
        self._sandbox = Sandbox

    def sandbox_get(self, name: str) -> Any:
        from kew.openshell import OpenShellNotFound

        if self.absent or self.deleted:
            raise OpenShellNotFound(
                ["openshell", "sandbox", "get", name], reason="no such sandbox"
            )
        return self._sandbox(
            name=name,
            status=self.status,
            identity=self.identity,
            image="kew-agent:openshell-test",
            labels={},
            raw={},
        )

    def sandbox_delete(self, name: str) -> None:
        self.deleted = True

    def sandbox_run(self, name: str, *, workdir: str, argv: Sequence[str]) -> str:
        from kew.openshell import OpenShellGatewayUnreachable

        self.probes.append(name)
        if self.agent is None:
            raise OpenShellGatewayUnreachable(
                ["openshell", "sandbox", "exec", name], reason="dial tcp"
            )
        return f"KEW_AGENT_EXEC {self.agent}\n"

    def sandbox_download(self, name: str, source: str, destination: Path) -> None:
        from kew.openshell import OpenShellGatewayUnreachable

        self.downloads.append((name, source))
        if self._download is None:
            raise OpenShellGatewayUnreachable(
                ["openshell", "sandbox", "download", name], reason="dial tcp"
            )
        self._download(Path(destination))


def _install_gateway(monkeypatch: pytest.MonkeyPatch, gateway: _FakeGateway) -> None:
    for call in ("sandbox_get", "sandbox_delete", "sandbox_download", "sandbox_run"):
        monkeypatch.setattr(
            f"kew.openshell.OpenShellClient.{call}",
            lambda _client, *args, _call=call, **kwargs: getattr(gateway, _call)(
                *args, **kwargs
            ),
        )


def _openshell_pending_run(
    *, exec_over: bool = True, started_at: Any = None
) -> RunRecord:
    """A pending row shaped the way ``openshell_launch`` records one.

    The sandbox NAME is the container identity, and the container-id column
    carries the GATEWAY id the create read back — the evidence recovery
    compares before it touches the sandbox (#2625).
    """
    return RunRecord(
        run_id="orphan-run",
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        status="pending",
        exit_code=-1,
        started_at=NOW if started_at is None else started_at,
        container_name=_SANDBOX,
        container_id=_SANDBOX_ID,
        container_runtime="openshell",
        attempt_pgid=2**31 - 1 if exec_over else 1,
    )


@pytest.mark.parametrize(
    ("status", "state", "alive"),
    [
        ("running", "running", False),
        ("stopped", "stopped", False),
        ("exited", "stopped", False),
        ("absent", "absent", False),
        ("creating", "unknown", None),
    ],
)
def test_each_openshell_sandbox_state_maps_through_both_recovery_probes(
    monkeypatch: pytest.MonkeyPatch, status: str, state: str, alive: bool | None
) -> None:
    """One mapping, asked by the driver reaper and by stop recovery alike.

    Both probes reach the sandbox by its recorded NAME: a gateway row carries
    no container id, so a probe that insisted on one would answer "unknown"
    about every OpenShell run forever and pin its reservation.

    The exec stream is over in every case here, which is why a ``running``
    sandbox still reads dead: a restart leaves the sandbox up with the agent
    gone, and presence alone is not liveness on this runtime.
    """
    from kew import run_liveness, stop_recovery
    from kew.graph.driver.tick import _container_alive

    _install_gateway(monkeypatch, _FakeGateway(status))
    record = _openshell_pending_run()
    config = _config()
    probe = run_liveness.container_state_probe("openshell", config)

    assert stop_recovery.record_container_state("openshell", record, probe=probe) == (
        state
    )
    assert _container_alive(record, config) is alive
    assert run_liveness._recorded_owner_is_dead(record, config) is (
        state in {"stopped", "absent"}
    )


@pytest.mark.parametrize(("agent", "alive"), [("running", True), (None, None)])
def test_a_running_sandbox_asks_its_agent_once_the_local_client_is_gone(
    monkeypatch: pytest.MonkeyPatch, agent: str | None, alive: bool | None
) -> None:
    """The local exec stream being over is not the remote agent being over.

    A dead client with a sandbox whose agent still runs is ALIVE, and one whose
    agent cannot be asked about is undeterminable — never dead (#2625).
    """
    from kew import run_liveness
    from kew.graph.driver.tick import _container_alive

    _install_gateway(monkeypatch, _FakeGateway("running", agent=agent))  # type: ignore[arg-type]
    record = _openshell_pending_run()

    assert _container_alive(record, _config()) is alive
    assert run_liveness._recorded_owner_is_dead(record, _config()) is False


def test_a_sweep_that_cannot_name_the_gateway_says_so_and_pins_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_store``'s own sweeps hold a run row and no ``[sandbox.openshell]``.

    They sit below the subprocess chokepoint the gateway backend reaches, so
    they cannot ask — and an unanswerable question must read unknown WITH a
    reason, which retains the run's capacity. The driver reconciler, which
    does hold the configuration, is what reaps it.
    """
    from kew import stop_recovery

    def refuse(_client, _name: str) -> None:
        raise AssertionError("no gateway call may be made without a backend")

    monkeypatch.setattr("kew.openshell.OpenShellClient.sandbox_get", refuse)
    record = _openshell_pending_run()

    with caplog.at_level("WARNING"):
        assert stop_recovery.record_container_state("openshell", record) == "unknown"
        assert stop_recovery.pending_owner_is_dead(record, NOW) is False

    assert "no gateway backend was supplied" in caplog.text


def test_a_running_sandbox_whose_exec_may_still_stream_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false-positive guard: a live run must survive the reaper.

    ``attempt_pgid`` resolves to a group this test keeps alive, so the exec
    client — the workload on this runtime — may still be running. Reading the
    present sandbox as dead here would reap a run mid-flight.
    """
    from kew import run_liveness, stop_recovery
    from kew.graph.driver.tick import _container_alive

    _install_gateway(monkeypatch, _FakeGateway("running"))
    monkeypatch.setattr("kew.stop_recovery.group_alive", lambda _pgid: True)
    record = _openshell_pending_run(exec_over=False)

    config = _config()
    probe = run_liveness.container_state_probe("openshell", config)

    assert _container_alive(record, config) is True
    assert (
        stop_recovery.record_container_state("openshell", record, probe=probe)
        == "running"
    )


def test_a_run_that_never_registered_its_exec_is_not_an_ended_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch window: created sandbox, exec not started, nothing to prove."""
    from kew.graph.driver.tick import _container_alive

    _install_gateway(monkeypatch, _FakeGateway("running"))
    record = _openshell_pending_run()
    record.attempt_pgid = None
    record.log_file = None

    assert _container_alive(record, _config(), now=NOW) is True


@pytest.mark.parametrize(
    ("past_grace_s", "alive"),
    [(-1, True), (1, False)],
)
def test_a_sandbox_that_never_got_an_exec_reads_alive_only_for_the_grace(
    monkeypatch: pytest.MonkeyPatch, past_grace_s: int, alive: bool
) -> None:
    """The leader died between the create and the exec.

    Inside the startup grace the exec may still come; past it the sandbox is
    an empty shell, and reading it alive pinned its reservation until the
    reaper deleted the sandbox.
    """
    from kew.graph.driver.tick import (
        _RESERVATION_STARTUP_GRACE_SECONDS,
        _container_alive,
    )

    _install_gateway(monkeypatch, _FakeGateway("running"))
    record = _openshell_pending_run(started_at=NOW)
    record.attempt_pgid = None
    record.log_file = None

    later = NOW + timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS + past_grace_s)
    assert _container_alive(record, _config(), now=later) is alive


def test_a_dead_leaders_never_execd_sandbox_frees_its_reservation(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    """Past the grace the reaper reads the shell dead and gives the item back.

    A review-pool reviewer, which records a sandbox and no clone: nothing is
    left to recover, so the reservation goes as soon as the run reads dead.
    """
    from datetime import UTC, datetime

    from kew.graph.driver.tick import REVIEW_EPIC_REF, _reconcile_reservations

    _install_gateway(monkeypatch, _FakeGateway("running", agent="absent"))
    record = _openshell_pending_run(started_at=NOW)
    record.attempt_pgid = None
    record.log_file = None
    record.run_kind = ADVERSARIAL_REVIEW_RUN_KIND
    # NOW is months behind the host clock the probe reads, far past the grace.
    _seed_pending_run(
        store, "1010", "orphan-run", pgid=0, record=record, epic_ref=REVIEW_EPIC_REF
    )

    _reconcile_reservations(REVIEW_EPIC_REF, store, _config(), now=datetime.now(UTC))

    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
    assert store.get("orphan-run").status != "pending"


def test_an_unreadable_gateway_pins_nothing_and_proves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway that cannot answer leaves the sandbox unknown, never absent."""
    from kew import run_liveness, stop_recovery
    from kew.graph.driver.tick import _container_alive
    from kew.openshell import OpenShellGatewayUnreachable

    def unreachable(_client, name: str) -> None:
        raise OpenShellGatewayUnreachable(["openshell"], reason="dial tcp")

    monkeypatch.setattr("kew.openshell.OpenShellClient.sandbox_get", unreachable)
    record = _openshell_pending_run()
    config = _config()
    probe = run_liveness.container_state_probe("openshell", config)

    assert _container_alive(record, config) is None
    assert (
        stop_recovery.record_container_state("openshell", record, probe=probe)
        == "unknown"
    )


def test_a_row_with_no_sandbox_name_is_unknown_without_a_gateway_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is asked about a run whose sandbox name never reached the ledger."""
    from kew import run_liveness, stop_recovery

    def refuse(_client, _name: str) -> None:
        raise AssertionError("no gateway call may be made without a sandbox name")

    monkeypatch.setattr("kew.openshell.OpenShellClient.sandbox_get", refuse)
    record = _openshell_pending_run()
    record.container_name = None
    probe = run_liveness.container_state_probe("openshell", _config())

    assert (
        stop_recovery.record_container_state("openshell", record, probe=probe)
        == "unknown"
    )


def _make_contained_checkout(base: Path, item_id: str) -> Path:
    """The checkout shape a CONTAINED dispatch records for itself.

    ``dispatch_core._materialize_checkout`` clones into a
    ``kew-sandbox-<slug>-<random>`` temp directory and that clone is what gets
    uploaded to the sandbox, so it — not ``<worktree_base>/agent/issue-<slug>``
    — is the directory a recovery has to bring the sandbox's bytes back into.
    """
    from kew.worktree import write_checkout_generation

    checkout = base / f"kew-sandbox-{item_id}-ab12cd"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch", f"agent/issue-{item_id}", str(checkout)],
        check=True,
        capture_output=True,
    )
    write_checkout_generation(checkout)
    return checkout


def _openshell_restart_case(
    store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway: _FakeGateway,
    *,
    bind: bool = True,
    publish: str = "checkout",
    nodes: Sequence[str] = ("1010",),
) -> tuple[DispatchConfig, Path, Any]:
    """One contained dispatch whose sandbox outlived its agent.

    The run LAUNCHED from a temp-directory clone, nowhere under
    ``branches.worktree_base``: that is the shape every OpenShell dispatch
    actually has, because the sandbox is created from the standalone clone
    ``_materialize_checkout`` makes. The clone starts EMPTY of work because on
    this runtime the agent writes to the uploaded copy inside the sandbox —
    which is the whole problem the gateway-restart path solves — and the HOST
    dispatch checkout does not exist at all, because no host dispatch of this
    item ever ran.

    ``bind`` writes the run's durable launch binding for that clone, which is
    what makes it a dispatch with an attested checkout; ``publish`` picks which
    of the two events carries it. The returned path is the clone, not the
    recovery destination: :func:`_host_checkout` is where the work comes back
    to.
    """
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    config.publication.publish = publish
    checkout = _make_contained_checkout(tmp_path / "containers", "1010")
    _seed_pending_run(
        store,
        "1010",
        "orphan-run",
        pgid=0,
        record=_openshell_pending_run(started_at=NOW - timedelta(hours=1)),
    )
    if bind:
        _bind_checkout_base(store, config, checkout, "orphan-run", "1010")
    _install_gateway(monkeypatch, gateway)
    # The recovery asks for room for a cap-sized checkout (4 GiB) above the
    # 1 GiB floor before it downloads (#2626); the test device is not obliged
    # to have it, so the probe is faked rather than made an input.
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=64 * 1024**3),
    )
    env = _fx.DriverEnv(
        store=store,
        lock_path=tmp_path / "epic.lock",
        config=config,
        deps=FakeSource(nodes=[_node(nid) for nid in nodes], edges=[]),
        facts=FakeFactsReader(
            issue_facts_by_num={
                int(nid): _facts(int(nid), state="OPEN") for nid in nodes
            }
        ),
        sink=FakeSink(),
    )
    return config, checkout, env


def _host_checkout(tmp_path: Path, item_id: str = "1010") -> Path:
    """Where a gateway-restart recovery brings the sandbox's work back to.

    The deterministic host dispatch path, which is the ONLY path a
    continuation can be registered against: every continuation boundary
    requires ``<worktree_base>/agent/issue-<slug>`` exactly, so work recovered
    anywhere else is reachable to nothing but a human holding the directory
    name.
    """
    return tmp_path / "worktrees" / "agent" / f"issue-{item_id}"


def _wrote_wip(destination: Path) -> None:
    """A download that hands back the sandbox's checkout, work and all.

    What ``openshell sandbox download /work`` returns is the directory that
    was uploaded: a standalone clone with its own ``.git``, on the agent
    branch, carrying whatever the agent wrote. The metadata is not incidental
    — the host path is only a checkout the recovery can bind and the orphan
    path can read because it comes back with it.
    """
    subprocess.run(
        ["git", "init", "--initial-branch", "agent/issue-1010", str(destination)],
        check=True,
        capture_output=True,
    )
    (destination / "wip.txt").write_text("agent work\n", encoding="utf-8")


def test_a_gateway_restart_offers_the_recovered_work_as_a_continuation(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole recovery in the shape that ships: download, bind, continue.

    A gateway restart kills every sandbox's processes and keeps its files, so
    the agent is gone and its work exists only inside the sandbox. It comes
    back to the HOST dispatch checkout and is bound to the dead run there,
    which is what lets the orphan pass that runs next read it as this run's
    dirty work: a continuation is registered against that path and attested,
    so `kew epic continue` can pick the work up.

    Recovering into the clone the run launched from instead — a random
    directory under the system temp directory — is what this pins against: no
    continuation boundary accepts a path outside ``branches.worktree_base``,
    so the node would surface FAILED with the only copy of the work in a
    directory nothing records a way back to, and the sandbox that held the
    other copy deleted.
    """
    gateway = _FakeGateway(download=_wrote_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )
    recovered = _host_checkout(tmp_path)

    env.advance()

    assert gateway.downloads == [(_SANDBOX, "/work")]
    assert (recovered / "wip.txt").read_text(encoding="utf-8") == "agent work\n"
    assert not (launched / "wip.txt").exists()
    # The work is offered, not just stored: one continuation, on that path.
    [continuation] = store.list_continuations(EPIC)
    assert (continuation.item_id, continuation.original_run_id) == (
        "1010",
        "orphan-run",
    )
    assert continuation.worktree_identity == str(recovered)
    assert continuation.state == "awaiting_continuation"
    [detected] = store.list_audit_events(event="continuation_detected")
    assert detected.payload["outcome"] == "crash_with_dirty_work"
    # No capacity pinned anywhere: not the epic reservation, not the sandbox.
    assert store.list_epic_reservations(EPIC) == []
    assert gateway.deleted is True
    assert env.sink.calls == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload == {
        "epic_ref": EPIC,
        "reason": "openshell_gateway_restart",
        "sandbox": _SANDBOX,
        "checkout": str(recovered),
        "checkout_recovered": True,
        "checkout_merge": "merged",
    }
    assert event.run_id == "orphan-run"
    assert verify_chain(store.read_chain_rows()).ok is True


def test_a_canonical_run_rebinds_its_recovered_checkout_at_its_recorded_base(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both publication modes bind a launch checkout; both have to be read.

    Canonical publication writes the binding as ``publication_base`` and
    checkout publication as ``dispatch_checkout_bound``. A reader that knew
    only one of them would recover nothing under the other mode — and under
    canonical the event carries more than a path: the base the work was cut
    from, which the recovered checkout has to be re-bound at, because the
    canonical comparison that decides whether there is work to preserve has
    nothing to compare against without it.
    """
    gateway = _FakeGateway(download=_wrote_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway, publish="canonical"
    )
    recovered = _host_checkout(tmp_path)

    env.advance()

    assert gateway.downloads == [(_SANDBOX, "/work")]
    assert (recovered / "wip.txt").read_text(encoding="utf-8") == "agent work\n"
    [continuation] = store.list_continuations(EPIC)
    assert continuation.worktree_identity == str(recovered)
    bases = {
        event.payload["base_sha"]
        for event in store.list_audit_events(event="publication_base")
        if event.payload["checkout"] == str(recovered)
    }
    assert bases == {"a" * 40}
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout"] == str(recovered)
    assert event.payload["checkout_recovered"] is True


def test_a_canonical_run_with_no_recorded_base_keeps_its_work_in_the_sandbox(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under canonical, bytes with no base are bytes nothing can continue.

    The recovered checkout is compared against the base its run recorded, and
    a canonical continuation is not minted without one. So a run carrying no
    base is refused BEFORE the gateway is asked for anything: the sandbox
    keeps the whole copy, where an operator can still fetch it, rather than
    the host gaining a tree no pass can read and the sandbox being deleted.

    A run reaches this by outliving a configuration change — it launched under
    checkout publication, which binds its checkout and no base at all, and the
    mode was canonical by the time its gateway restarted.
    """
    gateway = _FakeGateway(download=_wrote_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )
    config.publication.publish = "canonical"

    env.advance()

    assert gateway.downloads == []
    assert gateway.deleted is False
    assert not _host_checkout(tmp_path).exists()
    assert store.list_continuations(EPIC) == []
    assert store.get("orphan-run").status == "killed"
    assert store.list_epic_reservations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False


def test_a_leftover_host_checkout_is_neither_written_to_nor_wedges_the_tick(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree already at the host path stops the recovery, not the driver.

    An earlier HOST dispatch of the same item leaves a checkout at
    ``<worktree_base>/agent/issue-<slug>`` carrying the same branch and real
    Git metadata — everything a path-shaped check looks at. Merging a restarted
    sandbox into it would hand one run's work to another run's tree, attest it
    recovered and delete the sandbox holding the only copy, so the merge is
    refused and the sandbox is kept.

    The refusal must stop THERE. The leftover carries no binding naming the
    dead run, so the orphan pass's producer selection refused it — and that
    refusal came out of ``advance()`` every tick: the run stayed ``pending``,
    its reservation stayed held, nothing else in the epic was reconciled or
    dispatched, and the next tick did it again. A contained run has no work at
    the host path to preserve, so that pass now leaves the leftover alone: the
    run is finalized, the capacity released, and the second ready node in this
    epic dispatches in the same pass that hit the leftover.
    """
    gateway = _FakeGateway(download=_wrote_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway, nodes=("1010", "2020")
    )
    leftover = _make_dirty_worktree(tmp_path / "worktrees", "1010")

    env.advance()
    env.advance()  # and again: a wedge repeats, so one pass proves nothing

    assert gateway.downloads == []
    assert gateway.deleted is False
    assert (leftover / "wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    row = store.get("orphan-run")
    assert (row.status, row.error_category) == ("killed", "orphaned_pending_reaped")
    assert [r.item_id for r in store.list_epic_reservations(EPIC)] == ["2020"]
    assert "2020" in env.sink.calls
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout"] == str(_host_checkout(tmp_path))
    assert event.payload["checkout_recovered"] is False
    assert event.payload["checkout_merge"] == "unmerged"


def test_a_run_that_bound_no_checkout_recovers_nothing_and_asks_nothing(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no launch binding there is no attested dispatch to recover for.

    A row naming a sandbox and nothing else is not a dispatch kew can account
    for: nothing records what it was cut from or where it ran. The gateway is
    not asked, the host path is not created, the sandbox is kept, and the
    event says so with no checkout named — but the run is still finalized, so
    its capacity is not held hostage to missing evidence.
    """
    gateway = _FakeGateway(download=_wrote_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway, bind=False
    )

    env.advance()

    assert gateway.downloads == []
    assert gateway.deleted is False
    assert not _host_checkout(tmp_path).exists()
    assert store.get("orphan-run").status == "killed"
    assert store.list_epic_reservations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout"] == ""
    assert event.payload["checkout_recovered"] is False


def test_a_restart_whose_checkout_cannot_be_downloaded_still_frees_the_capacity(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work may be unreachable; the reservation must still be released.

    A download that fails leaves the sandbox on the gateway — an operator can
    still fetch it by hand, and a deleted sandbox could not be fetched at all
    — but the run is finalized either way, because a reservation held open
    waiting for a gateway starves every later admission.
    """
    gateway = _FakeGateway()
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert gateway.downloads == [(_SANDBOX, "/work")]
    assert gateway.deleted is False
    row = store.get("orphan-run")
    assert (row.status, row.error_category) == ("killed", "orphaned_pending_reaped")
    assert store.list_continuations(EPIC) == []
    assert store.list_epic_reservations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False


def test_a_download_that_wrote_nothing_leaves_no_half_made_host_checkout(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``sandbox download`` can exit 0 and leave the destination empty.

    That is the one failure the gateway does not announce, and the expensive
    one to believe: reporting it recovered deletes the sandbox holding the
    only copy of the agent's work and attests ``checkout_recovered: true``
    over a checkout that got nothing. It is treated as a failed download —
    sandbox kept, attestation false — and the run is still finalized.

    What must NOT survive it is the host dispatch path: an empty directory
    left standing there is read by the frontier as an existing checkout with
    an untrusted identity, which refuses this item's next dispatch over work
    that is not there. Only this recovery could have created it, because it
    proved the path free a moment earlier, so it takes it away again.
    """
    gateway = _FakeGateway(download=lambda destination: None)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert gateway.downloads == [(_SANDBOX, "/work")]
    assert gateway.deleted is False
    assert not _host_checkout(tmp_path).exists()
    row = store.get("orphan-run")
    assert (row.status, row.error_category) == ("killed", "orphaned_pending_reaped")
    assert store.list_continuations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False
    assert verify_chain(store.read_chain_rows()).ok is True


def test_a_download_whose_tree_is_only_empty_directories_recovers_nothing(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DEST/work/`` with nothing in it is the same silent loss as an empty DEST.

    The nested shape is one of the two the CLI may deliver, so an empty one
    reads as "the checkout is here" to a shape check alone. Only files make it
    a recovery.
    """
    gateway = _FakeGateway(download=lambda destination: (destination / "work").mkdir())
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert gateway.deleted is False
    assert not _host_checkout(tmp_path).exists()
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False


def test_a_part_way_merge_is_attested_as_the_mixed_tree_it_leaves(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge that failed half-way is attested as half-merged, not as nothing.

    The merge is not atomic: one entry the copy cannot carry leaves the rest
    of the sandbox's files at the host path. The event must not say
    ``checkout_recovered: false`` and nothing more — whoever comes for the
    work reads that as "the host checkout was never created" and hand-merges
    the sandbox into a path that is already half of it. ``checkout_merge``
    names the tree they are picking up, the sandbox is kept because it still
    holds the whole copy, and the half-tree is left in place because deleting
    the agent's files is not this path's call.
    """
    gateway = _FakeGateway(download=_lands_one_entry_of_two)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )
    recovered = _host_checkout(tmp_path)

    env.advance()

    assert (recovered / "landed.txt").read_text(encoding="utf-8") == "agent work\n"
    assert gateway.deleted is False
    row = store.get("orphan-run")
    assert (row.status, row.error_category) == ("killed", "orphaned_pending_reaped")
    assert store.list_continuations(EPIC) == []
    assert store.list_epic_reservations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False
    assert event.payload["checkout_merge"] == "part_merged"
    assert verify_chain(store.read_chain_rows()).ok is True


def _lands_one_entry_of_two(destination: Path) -> None:
    """A download whose merge gets one entry in and cannot carry the other.

    A named pipe is the entry no copy can carry whatever uid the tests run as
    — ``shutil.copy2`` refuses a special file outright — while the ordinary
    file beside it is copied first and stays.
    """
    (destination / "landed.txt").write_text("agent work\n", encoding="utf-8")
    os.mkfifo(destination / "pipe")


def _wrote_detached_wip(destination: Path) -> None:
    """A sandbox handed back on a DETACHED HEAD, which is a state agents reach.

    An interrupted rebase, a ``git bisect``, or a plain ``git checkout <sha>``
    all leave the checkout with no current branch. The tree is otherwise a
    real, complete checkout carrying the agent's work — which is the trap: it
    looks recoverable to everything except the identity gate that decides
    whether a continuation can be read from it.
    """
    _wrote_wip(destination)
    for argv in (
        ["add", "-A"],
        [
            "-c",
            "user.email=kew@example.invalid",
            "-c",
            "user.name=kew",
            "commit",
            "-m",
            "wip",
        ],
        ["checkout", "--detach"],
    ):
        subprocess.run(
            ["git", "-C", str(destination), *argv], check=True, capture_output=True
        )
    (destination / "wip.txt").write_text("agent work\n", encoding="utf-8")


def test_a_recovered_tree_on_a_detached_head_is_taken_back_off_the_host(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``merged`` verdict is only for a tree a continuation can be read from.

    A sandbox hands back whatever state its agent left it in, and a detached
    HEAD — mid-rebase, mid-bisect, or on a sha — is not a checkout of the
    item's branch. Claiming that tree recovered was the worst outcome the path
    had: the sandbox holding the other copy was deleted, the orphan pass that
    runs next raised on the present-but-untrusted host path instead of
    preserving anything, and the run kept its epic reservation on a ``pending``
    row no later tick could finalize — so the epic never advanced again.

    The readable-tree check is therefore asked BEFORE any of that is committed,
    the downloaded tree is taken back off the host (a tree left standing there
    refuses every later dispatch of the item over work that is not the item's),
    and the verdict is ``unmerged`` — the host exactly as it was, the sandbox
    kept with the work, and the run finalized so the epic moves on.
    """
    gateway = _FakeGateway(download=_wrote_detached_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway, nodes=("1010", "1011")
    )
    recovered = _host_checkout(tmp_path)

    env.advance()

    assert gateway.downloads == [(_SANDBOX, "/work")]
    assert not recovered.exists()
    assert gateway.deleted is False
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False
    assert event.payload["checkout_merge"] == "unmerged"
    # The reservation is released and the row is terminal, so the epic is free
    # to dispatch the node that was waiting behind it.
    assert store.get("orphan-run").status == "killed"
    assert [r.item_id for r in store.list_epic_reservations(EPIC)] == ["1011"]
    assert store.list_epic_holds(EPIC) == []
    assert store.list_continuations(EPIC) == []
    assert verify_chain(store.read_chain_rows()).ok is True


def test_a_recovered_tree_with_no_git_metadata_is_taken_back_off_the_host(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files without the metadata that identifies them are not a checkout.

    Every pass after this one matches a checkout by the generation its
    producing run bound to it, and binding needs the checkout's own ``.git``.
    A download that handed back the files alone fails the same readable-tree
    check a detached HEAD does, and gets the same answer: nothing is left at
    the host dispatch path to wedge the item's next dispatch, and the sandbox
    keeps the only copy nobody has lost.
    """
    gateway = _FakeGateway(
        download=lambda destination: (destination / "wip.txt").write_text(
            "agent work\n", encoding="utf-8"
        )
    )
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert not _host_checkout(tmp_path).exists()
    assert gateway.deleted is False
    assert store.list_continuations(EPIC) == []
    assert store.get("orphan-run").status == "killed"
    assert store.list_epic_reservations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_merge"] == "unmerged"


def test_a_readable_tree_whose_binding_fails_keeps_the_tree_and_the_sandbox(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree that stays is ``part_merged``, however little of it is readable.

    Once the tree passes the readable-tree check it is a checkout of the item's
    branch, so it is NOT taken back off the host: the orphan path and the
    operator both read what stands there. A binding that then fails leaves
    bytes on the host that nothing downstream can match to a producing run,
    which is exactly the mixed state ``part_merged`` exists to name — the
    sandbox is kept because it still holds the whole copy, and no continuation
    is minted over a tree nothing can verify.
    """
    from kew.publication import PublicationError

    gateway = _FakeGateway(download=_wrote_wip)
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )
    recovered = _host_checkout(tmp_path)

    def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PublicationError("bind_checkout", "refused")

    monkeypatch.setattr("kew.dispatch_core._bind_publication_base", _refuse)

    env.advance()

    assert (recovered / "wip.txt").read_text(encoding="utf-8") == "agent work\n"
    assert gateway.deleted is False
    assert store.list_continuations(EPIC) == []
    assert store.get("orphan-run").status == "killed"
    assert store.list_epic_reservations(EPIC) == []
    [event] = store.list_audit_events(event="openshell_gateway_restart")
    assert event.payload["checkout_recovered"] is False
    assert event.payload["checkout_merge"] == "part_merged"


def test_a_failed_download_leaves_no_pending_row_for_a_later_tick_to_retry(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery runs once: this tick finalizes the run whatever it returns.

    Worth pinning because the sandbox surviving a failed download invites the
    reading that a later tick comes back for it. It does not — no pending row
    is left to reconcile — so fetching the sandbox by hand is the only
    recovery left, and that is the whole reason it is not deleted.
    """
    gateway = _FakeGateway()
    config, launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert store.list_pending() == []
    assert store.get("orphan-run").status == "killed"
    assert len(gateway.downloads) == 1
    assert gateway.deleted is False


def test_a_dead_local_client_with_a_live_remote_agent_is_not_recovered(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kew's ``sandbox exec`` client dying is not the agent dying (#2625).

    The client's process group is gone and the sandbox is up — the shape the
    gateway-restart path keys on — but the sandbox says its agent is still
    running. Reading that as a restart downloaded a live checkout and
    finalized a running run; the run stays pending and nothing is touched.
    """
    gateway = _FakeGateway(agent="running")
    _config_, _launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert store.get("orphan-run").status == "pending"
    assert gateway.probes and set(gateway.probes) == {_SANDBOX}
    assert gateway.downloads == []
    assert gateway.deleted is False
    assert store.list_audit_events(event="openshell_gateway_restart") == []


def test_a_sandbox_that_cannot_say_whether_its_agent_runs_is_not_recovered(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswerable probe proves nothing: the run is held, not reaped."""
    gateway = _FakeGateway(agent=None)  # type: ignore[arg-type]
    _config_, _launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert store.get("orphan-run").status == "pending"
    assert gateway.downloads == []
    assert gateway.deleted is False


def test_another_runs_sandbox_under_the_recorded_name_is_never_touched(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway id the run recorded must be the one answering (#2625).

    A sandbox answering to the run's name with a different gateway id is some
    other run's — the collision two projects sharing a gateway produced. It
    is neither downloaded nor deleted, and its state proves nothing about this
    run, so the run is held rather than reaped.
    """
    gateway = _FakeGateway()
    gateway.identity = "sbx_ANOTHERPROJECTSRUN"
    _config_, _launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )

    env.advance()

    assert store.get("orphan-run").status == "pending"
    assert gateway.downloads == []
    assert gateway.deleted is False


def test_a_row_with_no_gateway_id_is_not_recovered_into_the_host(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the recorded id nothing proves the sandbox is this run's."""
    gateway = _FakeGateway()
    _config_, _launched, env = _openshell_restart_case(
        store, tmp_path, monkeypatch, gateway
    )
    store.set_container_identity("orphan-run", _SANDBOX, None, runtime="openshell")

    env.advance()

    assert gateway.downloads == []
    assert gateway.deleted is False
    assert store.list_audit_events(event="openshell_gateway_restart") == []


def test_a_reservation_whose_checkout_refuses_holds_its_node_not_the_tick(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unidentifiable checkout must not take the reconciler down with it.

    A HOST dead run whose recorded checkout is present but untrusted is the
    general shape of the wedge: the refusal is a statement about that one
    item's checkout, and it was propagating out of ``advance()``, so no other
    reservation in the epic was reconciled and no node was dispatched for as
    long as it stood. The refusal now holds the node it is about — under the
    frontier's own reason prefix, because a reserved node never reaches the
    frontier that would otherwise write it — and the pass carries on.
    """
    from kew.graph.driver.tick import _reconcile_reservations

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    (worktree / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    _seed_pending_run(store, "1010", "host-run", pgid=0)
    store.save(
        RunRecord(
            run_id="host-run",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW - timedelta(hours=1),
        )
    )

    assert _reconcile_reservations(
        EPIC, store, config, now=NOW, liveness=lambda _run: False
    ) == {"1010"}

    [hold] = store.list_epic_holds(EPIC)
    assert hold.item_id == "1010"
    assert hold.reason.startswith("preexisting_worktree_refused: ")
    assert (worktree / "wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_a_pseudo_epic_reservation_refusal_is_logged_and_never_held(
    tmp_path: Path,
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hold row is per item; a pseudo-epic must not take the item's only one.

    A hold is unique per item id, and both the dispatch that retires one and
    the operator's ``kew epic unhold`` are scoped by epic ref. A refusal
    reconciled under ``delegation`` or ``review`` that wrote a hold would park
    the item under a ref its real epic can never clear, so the reconciler logs
    it and leaves the row alone.
    """
    from kew.graph.driver.tick import _reconcile_reservations
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = _make_dirty_worktree(tmp_path / "worktrees", "1010")
    (worktree / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    # The run has to be provably dead for the reconciler to reach its checkout
    # at all: a row whose orphaned attempt cannot be killed is retained before
    # any identity is inspected, so the refusal this test is about is never
    # raised and the pseudo-epic branch is never taken.
    _seed_pending_run(store, "1010", "host-run", pgid=0, epic_ref=DELEGATION_EPIC_REF)
    store.save(
        RunRecord(
            run_id="host-run",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW - timedelta(hours=1),
        )
    )

    with caplog.at_level(logging.ERROR, logger="kew.graph.driver.tick"):
        assert _reconcile_reservations(
            DELEGATION_EPIC_REF, store, config, now=NOW, liveness=lambda _run: False
        ) == {"1010"}

    # The refusal was raised and reported — it just took no hold row with it.
    assert "could not be reconciled" in caplog.text
    assert store.list_all_epic_holds() == []
    assert (worktree / "wip.txt").read_text(encoding="utf-8") == "preserve me\n"


class _DriverCrash(BaseException):
    """The driver dying between two writes: nothing after it runs."""


@pytest.mark.parametrize("change", ["deleted", "replaced", "cleaned"])
def test_a_checkout_changed_between_finalize_and_recovery_keeps_the_reservation(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The driver dies after finalizing a dirty orphan and before recording its
    continuation (#3004 F1). If the checkout is then deleted, replaced or
    cleaned, recovery can no longer prove what the run left: the reservation
    is kept and the node is held, never released over unaccounted work."""
    from kew.graph.driver import tick
    from kew.worktree import write_checkout_generation

    config, worktree, reservation, run = _orphan_recovery_case(store, tmp_path)
    # The canonical walker fetches the remote base; the fake reads the bytes.
    monkeypatch.setattr(
        "kew.dispatch_core.publication_remote", lambda _repo: "/host/origin.git"
    )
    monkeypatch.setattr(
        "kew.dispatch_core.checkout_changed_paths",
        lambda checkout, **_k: ("wip.txt",) if (checkout / "wip.txt").exists() else (),
    )
    record = store.record_continuation

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise _DriverCrash

    monkeypatch.setattr(store, "record_continuation", crash)
    with pytest.raises(_DriverCrash):
        tick._reconcile_dead_pending(store, config, reservation, run, now=NOW)
    monkeypatch.setattr(store, "record_continuation", record)
    finalized = store.get(run.run_id)
    assert finalized is not None
    assert finalized.error_category == "orphaned_pending_preserved"
    assert store.list_continuations(EPIC) == []

    if change == "deleted":
        shutil.rmtree(worktree)
    elif change == "replaced":
        write_checkout_generation(worktree)
    else:
        worktree.joinpath("wip.txt").unlink()

    reserved = tick._reconcile_reservations(
        EPIC, store, config, now=NOW, liveness=lambda _run: False
    )

    assert reserved == {"1010"}
    assert store.list_epic_reservations(EPIC) == [reservation]
    assert store.list_continuations(EPIC) == []
    assert [h.item_id for h in store.list_epic_holds(EPIC)] == ["1010"]
