"""Hermetic end-to-end delegation seam: assign → admit → claim → close (D6, C3).

One offline pass over the WHOLE delegation money path, wired through the real
primitives rather than a parallel stack:

  assign (a Linear webhook row lands in ``tracker_events``)
    → :class:`DelegationWatcher` unions the fast local read with the throttled
      poll into a candidate
    → :func:`process_candidate` recognizes it and hands off to the D4 claim seam
    → the reservation lands in a REAL :class:`SqliteRunStore` and the dispatch
      goes to a **stub** sink (no agent, no container, no spend)
    → the delegated child run terminates and the reconcile pass returns the
      fleet slot to baseline
    → :func:`merge_pass` closes the item through ``tracker.close`` — the Linear
      terminal step from ``graph/driver/merge.py`` (never ``gh issue close``)

Everything crossing a process/network boundary is faked at the SAME seams the
existing suites use: the gh subprocess seam (``tests/support/gh_fakes.FakeGh``,
#435/T3) for the merge shell-out, and the Linear tracker/facts-reader seams from
``tests/test_driver_merge.py`` / ``tests/delegate/test_claim.py``. No socket is
ever opened; the only real I/O is the tmp-path SQLite store and kew.toml.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.gh_fakes import FakeGh

from kew.cli.epic import _LinearDispatchClaim
from kew.config import DispatchConfig
from kew.delegate.claim import DELEGATION_EPIC_REF
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import (
    WatchOutcome,
    make_is_terminally_decided,
    process_candidate,
    reconcile_delegation_reservations,
)
from kew.delegate.watch import Candidate, DelegationWatcher, MetaCursorStore
from kew.dispatch_core import _invoke_admitted
from kew.graph.driver.facts import NodeFacts, RepositoryPermission
from kew.graph.driver.merge import MergeVerdict, merge_pass
from kew.run_store import RunRecord, SqliteRunStore, TrackerEvent

MEMBER_ID = "11111111-2222-3333-4444-555555555555"
ITEM_ID = "SFD-42"
LABEL = "kew:ready"
REPO = "o/r"
BASE = "main"
PR_NUMBER = 42
APPROVER = "alice"
T0 = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Seam fakes — every one stands in for a network boundary.
# --------------------------------------------------------------------------


class FakeLinear:
    """The Linear seam: poll source, delegation tracker, and facts reader.

    One object because in production they are one API key against one issue —
    keeping the issue's assignee/state/label in a single place is what lets the
    test assert that the *live* re-verify and the post-merge close see the same
    issue the watcher saw.
    """

    def __init__(self, store: SqliteRunStore) -> None:
        self.store = store
        self.assignee_id: str | None = MEMBER_ID
        self.state = "Todo"
        self.labels: tuple[str, ...] = (LABEL,)
        self.issue_state = "OPEN"  # GitHub-side issue/item state for merge
        self.transitioned: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.polls = 0

    # -- poll source (kew.delegate.watch._DelegatedPoll) --------------------

    def list_delegated(self, assignee_id: str, label: str) -> list[str]:
        self.polls += 1
        if assignee_id == self.assignee_id or label in self.labels:
            return [ITEM_ID]
        return []

    # -- delegation tracker (kew.delegate.claim.DelegationTracker) ----------

    def fetch_live(self, item_id: str) -> CandidateIssue:
        return CandidateIssue(
            assignee_id=self.assignee_id,
            labels=self.labels,
            state=self.state,
            state_id=self.state,
        )

    def transition_in_progress(self, item_id: str) -> None:
        self.transitioned.append(item_id)
        self.state = "In Progress"

    def claim_assignee(self) -> str:
        return MEMBER_ID

    def claim_state(self) -> dict[str, str]:
        return {"id": "in-progress", "name": "In Progress"}

    def claim(self, item_id: str) -> dict[str, Any]:
        # C3: the remote mutation must observe committed child admission.
        [reservation] = self.store.list_epic_reservations(DELEGATION_EPIC_REF)
        with SqliteRunStore(self.store._db_path, read_only=True) as reader:
            assert reader.get(reservation.run_id).status == "pending"
            assert reader.get_admission_identity(reservation.run_id) is not None
        self.transition_in_progress(item_id)
        return {
            "issue": {
                "identifier": item_id,
                "updatedAt": T0.isoformat(),
                "state": {"id": "in-progress"},
                "assignee": {"id": self.assignee_id},
            }
        }

    def reset_to_ready(self, item_id: str) -> None:
        self.state = "Todo"

    def comment(self, item_id: str, body: str) -> None:
        self.comments.append((item_id, body))

    # -- LinearTracker close seam (kew.graph.driver.merge) ------------------

    def close(self, item_id: str) -> None:
        self.closed.append(item_id)
        self.state = "Done"
        self.issue_state = "CLOSED"

    # -- LinearFactsReader (str-keyed, mirrors test_driver_merge) -----------

    def issue_facts(self, item_id: str) -> NodeFacts:
        return NodeFacts(
            issue_num=0,
            state=self.issue_state,
            state_reason=None,
            labels=frozenset(),
            pr_number=PR_NUMBER,
            pr_url=None,
            review_decision=None,
            merge_state_status=None,
            head_oid=None,
            pr_pushed_at=None,
        )

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        return {
            "number": pr_number,
            "state": "OPEN",
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "headRefOid": "deadbeef",
            "commits": [],
            "latestReviews": [
                {
                    "state": "APPROVED",
                    "submittedAt": "2026-07-01T00:00:00Z",
                    "author": {
                        "login": APPROVER,
                        "is_bot": False,
                        "__typename": "User",
                    },
                }
            ],
            "author": {"login": "carol"},
        }

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return pr.get("mergeStateStatus") in {"CLEAN", "HAS_HOOKS"}

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return "success", "CI green"

    def repository_permission(self, login: str) -> RepositoryPermission:
        return RepositoryPermission(login, "User", "write")


class StubSink:
    """Admit the offline child and run its production claim hook (C3)."""

    def __init__(self, store: SqliteRunStore, linear: FakeLinear) -> None:
        self.store = store
        self.linear = linear
        self.dispatched: list[str] = []

    def dispatch(self, item_id: str) -> bool:
        self.dispatched.append(item_id)
        [reservation] = self.store.list_epic_reservations(DELEGATION_EPIC_REF)
        record = RunRecord(
            run_id=reservation.run_id,
            issue_num=0,
            item_id=item_id,
            branch="agent/sfd-42",
            status="pending",
            exit_code=0,
            started_at=T0,
            run_kind="delegation",
        )
        identity = self.store.build_admission_identity(
            record.run_id, tracker="linear", item_id=item_id, repo=REPO
        )
        assert self.store.admit_run_with_identity(
            record, identity, reservation=reservation, reservation_pinned=True
        )
        claim = _LinearDispatchClaim(
            self.linear, self.store, item_id, DELEGATION_EPIC_REF
        )
        _invoke_admitted(claim.admit, record.run_id)
        return True


def _assign_event(item_id: str, *, at: datetime) -> TrackerEvent:
    """The webhook a human assignment produces (Issue update, assigneeId)."""
    payload = {
        "action": "update",
        "type": "Issue",
        "data": {
            "id": "linear-internal-uuid",
            "identifier": item_id,
            "assigneeId": MEMBER_ID,
        },
        "updatedFrom": {"assigneeId": None},
    }
    return TrackerEvent(
        delivery_id=f"d-{item_id}",
        source="linear",
        event_type="Issue",
        action="update",
        entity_id=item_id,
        webhook_timestamp=None,
        payload=json.dumps(payload),
        received_at=at.isoformat(),
    )


def _generous_toml(tmp_path: Path) -> Path:
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 2000.0\n"
    )
    return toml


@pytest.fixture
def linear(store) -> FakeLinear:
    return FakeLinear(store)


@pytest.fixture
def store(tmp_path: Path):
    with SqliteRunStore(str(tmp_path / "delegation.db")) as s:
        yield s


def _watcher(store: Any, linear: FakeLinear) -> DelegationWatcher:
    return DelegationWatcher(
        events=store,
        poll=linear,
        cursor_store=MetaCursorStore(store),
        assignee_id=MEMBER_ID,
        label=LABEL,
        is_terminally_decided=make_is_terminally_decided(store),
        poll_interval_seconds=20.0,
        clock=lambda: 0.0,
    )


def _claim(store: Any, linear: FakeLinear, candidate: Candidate, toml: Path):
    sink = StubSink(store, linear)
    outcome = process_candidate(
        candidate,
        member_id=MEMBER_ID,
        label=LABEL,
        store=store,
        tracker=linear,
        sink=sink,
        fleet_cap=0,
        dry_run=False,
        config_path=toml,
    )
    return outcome, sink


# --------------------------------------------------------------------------
# The end-to-end pass
# --------------------------------------------------------------------------


def test_assign_to_close_runs_end_to_end_offline(tmp_path, store, linear) -> None:
    """assign → claim → dispatch stub → tracker.close, entirely offline."""
    toml = _generous_toml(tmp_path)
    baseline = store.live_run_count()

    # 1. ASSIGN — a human assigns the issue to the kew seat; the webhook row
    #    lands in tracker_events exactly as the ingest receiver writes it.
    assert store.record_tracker_event(_assign_event(ITEM_ID, at=T0)) is True

    # 2. RECOGNIZE — one watcher tick unions the fast local read with the
    #    throttled poll and emits the candidate once (deduped across surfaces).
    candidates = _watcher(store, linear).tick()
    assert candidates == [Candidate(item_id=ITEM_ID, source="local")]
    assert linear.polls == 1

    # 3. CLAIM + DISPATCH (stub) — the reservation lands in the real store and
    #    the admitted child shows the issue In Progress. No agent runs.
    outcome, sink = _claim(store, linear, candidates[0], toml)
    assert outcome is WatchOutcome.DISPATCHED
    assert sink.dispatched == [ITEM_ID]
    assert linear.transitioned == [ITEM_ID]
    assert linear.comments == []
    reserved = store.list_epic_reservations(DELEGATION_EPIC_REF)
    assert [r.item_id for r in reserved] == [ITEM_ID]
    assert store.live_run_count() == baseline + 1

    # 4. THE DELEGATED RUN FINISHES — its child run row reaches a terminal
    #    status, so the watch loop's reconcile pass returns the fleet slot.
    now = T0 + timedelta(minutes=5)
    store.save(
        RunRecord(
            run_id=reserved[0].run_id,
            issue_num=0,
            item_id=ITEM_ID,
            branch="agent/sfd-42",
            status="success",
            exit_code=0,
            started_at=T0,
            finished_at=now,
        )
    )
    assert reconcile_delegation_reservations(store, DispatchConfig(), now=now) == set()
    assert store.live_run_count() == baseline

    # 5. MERGE + CLOSE — the approved, CI-green PR merges through the gh seam
    #    and the Linear item is closed via `tracker.close` (a Linear PR carries
    #    no `Closes #N` auto-close, so the tracker call IS the close).
    gh = FakeGh()
    verdicts = merge_pass(
        [ITEM_ID],
        repo=REPO,
        base=BASE,
        epic_ref=DELEGATION_EPIC_REF,
        reader=linear,
        approvers=[APPROVER],
        store=store,
        gated_checks=["test", "agent-image-scan"],
        run=gh,
        tracker=linear,
    )

    assert verdicts == [MergeVerdict(ITEM_ID, "merged", "approved+ci-green")]
    assert linear.closed == [ITEM_ID]
    # The Linear path never shells out to close — `gh` is only ever the merge.
    assert not any(c[:3] == ["gh", "issue", "close"] for c in gh.calls)
    assert [c[:3] for c in gh.calls] == [["gh", "pr", "merge"]]


def test_watcher_does_not_re_emit_an_already_claimed_item(
    tmp_path, store, linear
) -> None:
    """A second tick after the claim is a clean no-op — no double dispatch.

    The poll floor has no memory, so a still-assigned+labelled issue re-surfaces
    on every tick until it leaves the delegated set. Nothing downstream may act
    on it twice: the claim transitioned it to In Progress, so recognition (which
    reads LIVE fields) drops it before the reservation is even consulted. This
    is the property that makes the foreground loop safe to leave running.
    """
    toml = _generous_toml(tmp_path)
    store.record_tracker_event(_assign_event(ITEM_ID, at=T0))

    first = _watcher(store, linear).tick()
    outcome, sink = _claim(store, linear, first[0], toml)
    assert outcome is WatchOutcome.DISPATCHED
    assert sink.dispatched == [ITEM_ID]

    # Second tick: both surfaces re-surface it (the cursor bound is inclusive
    # and the poll floor has no memory), deduped back to one candidate.
    second = _watcher(store, linear).tick()
    assert second == [Candidate(item_id=ITEM_ID, source="local")]

    again, again_sink = _claim(store, linear, second[0], toml)
    assert again is WatchOutcome.SKIPPED_NOT_RECOGNIZED
    assert again_sink.dispatched == []
    # Still exactly one reservation and one In Progress transition.
    assert len(store.list_epic_reservations(DELEGATION_EPIC_REF)) == 1
    assert linear.transitioned == [ITEM_ID]


def test_reassigned_away_after_the_snapshot_never_dispatches(
    tmp_path, store, linear
) -> None:
    """The stale-snapshot guard, end to end: reassign between tick and claim.

    The watcher's candidate can be ~one poll interval stale; recognition reads
    LIVE fields, so an issue handed back to a human after the event landed is
    dropped before a cent is spent and no reservation survives.
    """
    toml = _generous_toml(tmp_path)
    store.record_tracker_event(_assign_event(ITEM_ID, at=T0))
    candidates = _watcher(store, linear).tick()
    assert candidates

    # The human takes it back before the claim runs.
    linear.assignee_id = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    linear.labels = ()

    outcome, sink = _claim(store, linear, candidates[0], toml)

    assert outcome is WatchOutcome.SKIPPED_NOT_RECOGNIZED
    assert sink.dispatched == []
    assert linear.transitioned == []
    assert store.list_epic_reservations(DELEGATION_EPIC_REF) == []
