"""Unit tests for the delegation claim + dispatch seam (D4, #923).

The money-path reuse core: claim atomically via the reservation primitive,
re-verify live assignee, hand off to the shared detached dispatch sink with
``run_kind="delegation"``, and on a budget/preflight refusal write the terminal
refusal row (#898) plus a Linear refusal comment. Reuses budget/breaker/
containment — no reimplementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from support import source_ast

from kew.delegate.claim import ClaimOutcome, claim_and_dispatch
from kew.delegate.recognize import CandidateIssue
from kew.graph.driver.dispatch import DetachedDispatchSink
from kew.run_store import RunRecord, SqliteRunStore

MEMBER_ID = "11111111-2222-3333-4444-555555555555"
ITEM_ID = "SFD-42"
EPIC_REF = "delegation"


def _generous_toml(tmp_path):
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 2000.0\n"
    )
    return toml


class _FakeTracker:
    """Records the live-state re-verify, transition, and comment I/O."""

    def __init__(
        self, *, assignee_id=MEMBER_ID, state="Todo", labels=(), epic_admitted=False
    ):
        self._issue = CandidateIssue(
            assignee_id=assignee_id,
            labels=tuple(labels),
            state=state,
            state_id=state,
            epic_admitted=epic_admitted,
        )
        self.fetched: list[str] = []
        self.transitioned: list[str] = []
        self.reset: list[str] = []
        self.comments: list[tuple[str, str]] = []

    def fetch_live(self, item_id: str) -> CandidateIssue:
        self.fetched.append(item_id)
        return self._issue

    def transition_in_progress(self, item_id: str) -> None:
        self.transitioned.append(item_id)

    def reset_to_ready(self, item_id: str) -> None:
        self.reset.append(item_id)

    def comment(self, item_id: str, body: str) -> None:
        self.comments.append((item_id, body))


class _FakeSink:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(self, item_id: str) -> bool:
        self.dispatched.append(item_id)
        return True


def _spend(store: SqliteRunStore, usd: float, *, run_id: str, when: datetime) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=when,
            estimated_cost_usd=usd,
        )
    )


def test_dispatch_goes_through_sink_on_happy_path(tmp_path):
    """A recognized, still-assigned issue dispatches via the shared sink."""
    toml = _generous_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
    assert outcome is ClaimOutcome.DISPATCHED
    assert sink.dispatched == [ITEM_ID]
    # Live re-verify happened; the child owns the post-admission Linear claim.
    assert tracker.fetched == [ITEM_ID]
    assert tracker.transitioned == []
    assert tracker.comments == []


def test_single_claim_wins_under_a_double_surface(tmp_path):
    """Two claims for one item: the reservation is the sole double-dispatch guard."""
    toml = _generous_toml(tmp_path)
    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=_FakeTracker(),
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
            new_run_id=lambda: "run-A",
        )
        # A second surface (poll + fast read seeing the same event) races in.
        second_tracker, second_sink = _FakeTracker(), _FakeSink()
        second = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=second_tracker,
            sink=second_sink,
            config_path=toml,
            epic_ref=EPIC_REF,
            new_run_id=lambda: "run-B",
        )
    assert first is ClaimOutcome.DISPATCHED
    assert second is ClaimOutcome.SKIPPED_LOST_CLAIM
    # The loser is a clean no-op: it never re-verifies, transitions, or dispatches.
    assert sink.dispatched == [ITEM_ID]
    assert second_sink.dispatched == []
    assert second_tracker.fetched == []
    assert second_tracker.transitioned == []


def test_replayed_claim_after_process_restart_hits_persisted_reservation(tmp_path):
    """A crash/replay cannot dispatch an item whose reservation survived."""
    db_path = tmp_path / "replay.db"
    first_sink = _FakeSink()
    with SqliteRunStore(str(db_path)) as store:
        first = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=_FakeTracker(),
            sink=first_sink,
            config_path=_generous_toml(tmp_path),
            epic_ref=EPIC_REF,
            new_run_id=lambda: "run-before-crash",
        )

    second_sink = _FakeSink()
    second_tracker = _FakeTracker()
    with SqliteRunStore(str(db_path)) as store:
        second = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=second_tracker,
            sink=second_sink,
            config_path=_generous_toml(tmp_path),
            epic_ref=EPIC_REF,
            new_run_id=lambda: "run-after-restart",
        )

    assert first is ClaimOutcome.DISPATCHED
    assert second is ClaimOutcome.SKIPPED_LOST_CLAIM
    assert second_tracker.fetched == []
    assert second_sink.dispatched == []


def test_live_reassign_rejects_and_releases(tmp_path):
    """A snapshot may be ~20s stale: live re-verify rejects a reassigned issue."""
    toml = _generous_toml(tmp_path)
    # The candidate snapshot said kew, but live state shows a different assignee.
    tracker = _FakeTracker(assignee_id="99999999-aaaa-bbbb-cccc-dddddddddddd")
    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert outcome is ClaimOutcome.SKIPPED_REASSIGNED
        # Never transitioned, never commented, never dispatched.
        assert tracker.transitioned == []
        assert sink.dispatched == []
        # The reservation was released so a later legitimate claim can re-take it.
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-after") is True


def test_claim_time_epic_admission_wins_and_releases(tmp_path):
    """An item admitted after the watcher's first read cannot enter delegation."""
    toml = _generous_toml(tmp_path)
    tracker = _FakeTracker(epic_admitted=True)
    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert store.list_epic_reservations(EPIC_REF) == []
    assert outcome is ClaimOutcome.SKIPPED_EPIC_ADMITTED
    assert tracker.transitioned == []
    assert sink.dispatched == []


def test_backlog_is_claimed_but_not_transitioned(tmp_path):
    """Transition fires only if the live state is still Todo; Backlog dispatches."""
    toml = _generous_toml(tmp_path)
    tracker = _FakeTracker(state="Backlog")
    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
    assert outcome is ClaimOutcome.DISPATCHED
    assert sink.dispatched == [ITEM_ID]
    # "Todo -> In Progress only if still Todo": a Backlog issue is not transitioned.
    assert tracker.transitioned == []


def test_budget_refusal_writes_row_and_comments(tmp_path):
    """A budget refusal writes the terminal refusal row (#898) AND comments back."""
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    now = datetime.now(UTC)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 50.0, run_id="prior", when=now)  # blow past the $10 weekly cap
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert outcome is ClaimOutcome.REFUSED
        # Never dispatched.
        assert sink.dispatched == []
        # kew posted a refusal comment on the Linear issue.
        assert len(tracker.comments) == 1
        item, body = tracker.comments[0]
        assert item == ITEM_ID
        assert "budget" in body.lower()
        # A terminal refusal RunRecord was written for the item (#898 reuse).
        rows = store.list_by_item(ITEM_ID)
        assert [r.status for r in rows] == ["refused"]
        assert rows[0].error_category == "budget_refused"
        # The refusal happens before any Linear claim.
        assert tracker.transitioned == []
        assert tracker.reset == []
        # The reservation was released so the item is re-delegatable (N1).
        assert store.list_epic_reservations(EPIC_REF) == []


def test_budget_refusal_emits_audit_event(tmp_path):
    """A budget refusal emits a ``dispatch_completed`` audit event (N6).

    The epic path records declined work on the tamper-evident ledger; the
    delegation path must reach the same governance bar so a refused delegated
    run is not a governance blind spot.
    """
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    now = datetime.now(UTC)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 50.0, run_id="prior", when=now)
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert outcome is ClaimOutcome.REFUSED
        events = store.list_audit_events(event="dispatch_completed", item_id=ITEM_ID)
        assert len(events) == 1
        assert events[0].payload["error_category"] == "budget_exceeded"
        assert events[0].item_id == ITEM_ID


def test_budget_refusal_survives_a_rate_limited_comment(tmp_path):
    """A comment failure preserves the retained claim and reservation release."""
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    now = datetime.now(UTC)

    class _RateLimitedComment(_FakeTracker):
        def comment(self, item_id: str, body: str) -> None:
            raise RuntimeError("Linear 429 rate limited")

    tracker, sink = _RateLimitedComment(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 50.0, run_id="prior", when=now)
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert outcome is ClaimOutcome.REFUSED
        # The reservation was released even though the comment failed.
        assert tracker.reset == []
        assert store.list_epic_reservations(EPIC_REF) == []
        # Re-delegatable, never a stranded lost-claim.
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-after") is True
        # The terminal refusal row still landed (governance is unaffected).
        assert [r.status for r in store.list_by_item(ITEM_ID)] == ["refused"]


def test_comment_once_per_refusal_episode(tmp_path):
    """Three consecutive cooldown-expiry refusals post exactly ONE comment (Task 1).

    #966's cooldown damps the 20s re-claim churn, but each cooldown expiry still
    re-claims -> refuses -> comments — ~288 Linear comments/day per over-budget
    item. We comment once per refusal episode: the first refusal comments, every
    subsequent refusal in the same episode updates state silently.
    """
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    now = datetime.now(UTC)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 50.0, run_id="prior", when=now)  # blow past the $10 weekly cap
        for _ in range(3):
            outcome = claim_and_dispatch(
                ITEM_ID,
                member_id=MEMBER_ID,
                store=store,
                tracker=tracker,
                sink=sink,
                config_path=toml,
                epic_ref=EPIC_REF,
            )
            assert outcome is ClaimOutcome.REFUSED
        # Every refusal still writes its terminal row (governance is unaffected)...
        assert [r.status for r in store.list_by_item(ITEM_ID)] == [
            "refused",
            "refused",
            "refused",
        ]
        # ...but exactly one comment landed across the whole episode.
        assert len(tracker.comments) == 1


def test_new_refusal_episode_comments_again(tmp_path):
    """A refusal after a non-refusal newest row opens a new episode -> comments."""
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    now = datetime.now(UTC)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 50.0, run_id="prior", when=now)
        claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        # A later success closes the episode; the newest row is no longer a
        # refusal (later started_at than the refusal row the claim just wrote).
        later = now + timedelta(seconds=60)
        store.save(
            RunRecord(
                run_id="dispatched-run",
                issue_num=0,
                item_id=ITEM_ID,
                branch=ITEM_ID,
                status="success",
                exit_code=0,
                started_at=later,
                finished_at=later,
            )
        )
        claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        # First refusal + the post-success refusal each comment: two episodes.
        assert len(tracker.comments) == 2


def test_round19_delegation_refusal_never_schedules_reset(tmp_path):
    """A budget refusal leaves ownership unchanged without scheduling a reset."""
    from kew.delegate.claim import load_stranded_rollbacks

    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    now = datetime.now(UTC)

    class _RollbackFails(_FakeTracker):
        def reset_to_ready(self, item_id: str) -> None:
            raise RuntimeError("Linear 429 rate limited")

    tracker, sink = _RollbackFails(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 50.0, run_id="prior", when=now)
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert outcome is ClaimOutcome.REFUSED
        assert load_stranded_rollbacks(store) == []
        # The reservation was still released so a live slot is never pinned.
        assert store.list_epic_reservations(EPIC_REF) == []
        events = store.list_audit_events(event="linear_claim_retained")
        assert events == []


def test_unexpected_tracker_error_releases_reservation(tmp_path):
    """A failure after reserving must release the reservation, never dangle it."""
    toml = _generous_toml(tmp_path)

    class _Boom(_FakeTracker):
        def fetch_live(self, item_id: str) -> CandidateIssue:
            raise RuntimeError("Linear API down")

    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with pytest.raises(RuntimeError, match="Linear API down"):
            claim_and_dispatch(
                ITEM_ID,
                member_id=MEMBER_ID,
                store=store,
                tracker=_Boom(),
                sink=sink,
                config_path=toml,
                epic_ref=EPIC_REF,
            )
        # Reservation was rolled back so the item is re-claimable.
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-after") is True


@pytest.fixture
def parent_credentials(tmp_path, monkeypatch):
    from kew import credential_issuer
    from kew.config import DispatchConfig

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
    with SqliteRunStore(str(tmp_path / "sink.db"), project_root=tmp_path) as store:
        yield {"config": DispatchConfig(), "store": store}


def test_detached_sink_threads_run_kind_to_child_command(parent_credentials):
    """The shared sink passes ``--run-kind`` to the detached _dispatch-node child."""
    captured: dict[str, list[str]] = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

        class _P:
            pass

        return _P()

    sink = DetachedDispatchSink(
        epic_ref=EPIC_REF,
        run_kind="delegation",
        popen=_fake_popen,
        **parent_credentials,
    )
    assert sink.dispatch(ITEM_ID) is True
    cmd = captured["cmd"]
    assert cmd[:3] == ["kew", "epic", "_dispatch-node"]
    assert "--run-kind" in cmd
    assert cmd[cmd.index("--run-kind") + 1] == "delegation"


def test_detached_sink_default_run_kind_omits_flag(parent_credentials):
    """The default (epic) run_kind keeps the child command flag-free."""
    captured: dict[str, list[str]] = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return object()

    sink = DetachedDispatchSink(
        epic_ref=EPIC_REF, popen=_fake_popen, **parent_credentials
    )
    sink.dispatch("7")
    assert "--run-kind" not in captured["cmd"]


def _round15_expire_and_replace(store):
    from kew.config import DispatchConfig
    from kew.graph.driver.tick import reconcile_epic_reservations

    old = datetime.now(UTC) - timedelta(hours=1)
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE run_id = ?",
        (old.isoformat(), "run-A"),
    )
    store._conn.commit()
    assert (
        reconcile_epic_reservations(
            EPIC_REF,
            store,
            DispatchConfig(),
            now=datetime.now(UTC),
        )
        == set()
    )
    assert (
        store.reserve_epic_node_item_under_cap(EPIC_REF, ITEM_ID, "run-B", 1)
        == "reserved"
    )


@pytest.mark.parametrize("path", ["epic_admitted", "reassigned", "exception", "budget"])
def test_round15_delegation_late_cleanup_preserves_replacement(
    tmp_path, monkeypatch, path
):
    from kew.budget import BudgetExceeded
    from kew.config import DispatchConfig
    from kew.graph.driver.tick import reconcile_epic_reservations

    tracker = _FakeTracker(epic_admitted=path == "epic_admitted")
    if path == "reassigned":
        tracker = _FakeTracker(assignee_id="another-member")
    sink = _FakeSink()
    with SqliteRunStore(tmp_path / "d.db") as store:
        fetch_live = tracker.fetch_live

        def delayed_fetch(item_id):
            if path != "budget":
                _round15_expire_and_replace(store)
            if path == "exception":
                raise RuntimeError("delayed tracker failure")
            return fetch_live(item_id)

        def delayed_budget(*args, **kwargs):
            _round15_expire_and_replace(store)
            raise BudgetExceeded("daily", 1.0, 10.0, status=None)

        monkeypatch.setattr(tracker, "fetch_live", delayed_fetch)
        if path == "budget":
            monkeypatch.setattr(
                "kew.delegate.claim.delegation_budget_preflight", delayed_budget
            )
        kwargs = dict(
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=_generous_toml(tmp_path),
            epic_ref=EPIC_REF,
            fleet_cap=1,
            new_run_id=lambda: "run-A",
        )
        if path == "exception":
            with pytest.raises(RuntimeError, match="delayed tracker failure"):
                claim_and_dispatch(ITEM_ID, **kwargs)
        else:
            result = claim_and_dispatch(ITEM_ID, **kwargs)
            assert (
                result
                is {
                    "epic_admitted": ClaimOutcome.SKIPPED_EPIC_ADMITTED,
                    "reassigned": ClaimOutcome.SKIPPED_REASSIGNED,
                    "budget": ClaimOutcome.REFUSED,
                }[path]
            )
        assert [r.run_id for r in store.list_epic_reservations(EPIC_REF)] == ["run-B"]
        assert store.get("run-B") is None
        assert reconcile_epic_reservations(
            EPIC_REF,
            store,
            DispatchConfig(),
            now=datetime.now(UTC),
        ) == {ITEM_ID}
        assert (
            store.reserve_epic_node_item_under_cap(EPIC_REF, "SFD-99", "run-C", 1)
            == "fleet_full"
        )
        assert store.live_run_count() == 1
        assert sink.dispatched == []
        if path == "budget":
            [refusal] = [r for r in store.list_by_item(ITEM_ID) if r.run_id != "run-A"]
            assert refusal.error_category == "reservation_replaced"
            assert refusal.run_id not in {"run-A", "run-B"}
            assert store.list_audit_events(event="dispatch_reservation_bound") == []
            assert tracker.reset == []
            assert tracker.comments == []


@pytest.mark.policy
def test_delegation_releases_only_the_reservation_it_owns():
    """Every ``release_epic_node_item`` in the claim module names its own run.

    Read off the module's syntax tree, so a reformat of the call cannot turn
    this red and an unowned release cannot keep it green.
    """
    import ast

    import kew.delegate.claim as claim

    calls = [
        node
        for node in source_ast.calls(source_ast.module_tree(claim))
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "release_epic_node_item"
    ]
    assert calls
    for call in calls:
        assert any(
            arg.arg == "run_id"
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "run_id"
            for arg in call.keywords
        ), f"unowned release at line {call.lineno}"


class _Round20OrderingTracker(_FakeTracker):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def transition_in_progress(self, item_id):
        self.order.append("tracker_mutation")
        super().transition_in_progress(item_id)


class _Round20RefusingSink:
    def __init__(self, order):
        self.order = order

    def dispatch(self, item_id):
        self.order.append("last_admission_check")
        raise RuntimeError("runner unavailable before admission")


def test_round20_delegate_run_and_merge_do_not_mutate_before_admission(tmp_path):
    from kew.delegate.run import process_candidate, reconcile_stranded_rollbacks
    from kew.delegate.watch import Candidate
    from kew.graph.driver.merge import _handle_closed_unmerged

    order = []
    tracker = _Round20OrderingTracker(order)
    with SqliteRunStore(tmp_path / "order.db") as store:
        # Both recovery paths precede the next watcher dispatch.
        store.set_meta("delegation_stranded_rollbacks", '["SFD-42"]')
        reconcile_stranded_rollbacks(store, tracker)
        verdict = _handle_closed_unmerged(
            ITEM_ID,
            item_id=ITEM_ID,
            provider="linear",
            tracker=tracker,
            repo="o/r",
            run=None,
            store=store,
            pending_review_label="review",
        )
        assert verdict.reason == "closed-unmerged"
        with pytest.raises(RuntimeError, match="runner unavailable"):
            process_candidate(
                Candidate(ITEM_ID, "poll"),
                member_id=MEMBER_ID,
                label="kew:ready",
                store=store,
                tracker=tracker,
                sink=_Round20RefusingSink(order),
                fleet_cap=2,
                dry_run=False,
                config_path=_generous_toml(tmp_path),
            )
        assert order == ["last_admission_check"]
        assert tracker.reset == []
        assert store.list_epic_reservations("delegation") == []
