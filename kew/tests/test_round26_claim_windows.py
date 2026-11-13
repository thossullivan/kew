"""Complete claim lifecycle regressions against 66017fd7; W1 and W2 are controls."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_linear_tracker import _config as tracker_config
from test_linear_tracker import _ok
from test_remediation_flow_gate import pinned_dispatch_scene as pinned_dispatch_scene
from test_round18_linear_compensation import linear_child as linear_child
from test_round21_admission_owner import EPIC, ITEM, _admit_host, _config, _kill
from test_round24_claim_recovery import AtomicReapObserver, _event
from test_round25_claim_states import (
    AppliedWorkflow,
    WorkflowWatcher,
    _confirmed_claim,
    _release_retry,
)

from kew import dispatch_core as core
from kew import dispatch_sandbox
from kew.claim_retention import LINEAR_CLAIM_RECOVERY, orphaned_claim
from kew.cli.epic import _LinearDispatchClaim
from kew.delegate.run import WatchOutcome
from kew.graph.driver import tick
from kew.linear.client import LinearApiError, LinearClient
from kew.linear.tracker import LinearTracker
from kew.run_store import SqliteRunStore

WINDOWS = ("W1", "W2", "W3", "W4", "W5")


def _context(store, directory, claim):
    ctx = core._prepare_dispatch_context(
        issue={"number": 0, "title": "Claim windows", "body": "", "labels": []},
        config=_config(directory),
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=EPIC,
        item_id=ITEM,
        provider="linear",
        on_refuse=claim.retain,
        on_terminal_failure=None,
        run_kind="delegation",
        worktree_dir=None,
        run_id=None,
        branch_override=None,
    )
    ctx.on_admitted = claim.admit
    ctx.admitted_run_id = "child"
    return ctx


class RetentionFault:
    def __init__(self, store, claim, failures=0, *, completion=False, mode="before"):
        self.append = store.append_audit_event
        self.append_once = store.append_audit_event_once
        self.claim = claim
        self.failures = failures
        self.completion = completion
        self.mode = mode
        self.guards = []

    def install(self, store):
        store.append_audit_event = self
        store.append_audit_event_once = self

    def __call__(self, **kwargs):
        if kwargs["event"] == "linear_claim_completed" and self.completion:
            raise OSError("completion unavailable")
        if kwargs["event"] == "linear_claim_retained":
            self.guards.append(self.claim.attempted)
            if self.failures:
                self.failures -= 1
                if self.mode == "after":
                    self._append(kwargs)
                if self.mode == "drop":
                    return 999999
                raise OSError("retention unavailable")
        return self._append(kwargs)

    def _append(self, kwargs):
        if "dedup_key" in kwargs:
            return self.append_once(**kwargs)
        return self.append(**kwargs)


class WindowTransport(AppliedWorkflow):
    def __call__(self, url, payload, headers):
        query = json.loads(payload)["query"]
        if "IssueClaimState" in query:
            return _ok({"issue": self.remote_issue()})
        return super().__call__(url, payload, headers)


class LostResponse(WindowTransport):
    def apply(self, fields):
        super().apply(fields)
        raise ConnectionResetError("response lost after commit")


def _pause_window(window):
    print(window, flush=True)
    signal.pause()
    raise AssertionError("The parent must kill the dispatcher")


def _child_main(db_path, remote_path, window):
    directory = Path(db_path).parent
    with SqliteRunStore(Path(db_path)) as store:
        _admit_host(store, _config(directory), "child")
        if window == "W1":
            _pause_window(window)
        transport_type = LostResponse if window == "W2" else WindowTransport
        transport = transport_type(remote_path, "Doing")
        client = LinearClient("round26-fixture", transport=transport, max_retries=1)
        tracker = LinearTracker(tracker_config(in_review_state="QA"), client)
        claim = _LinearDispatchClaim(tracker, store, ITEM, EPIC)
        ctx = _context(store, directory, claim)
        if window == "W3":
            RetentionFault(store, claim, 100, completion=True).install(store)
        try:
            core._claim_after_admission(ctx, "child")
        except (OSError, LinearApiError):
            if window == "W3":
                with suppress(OSError):
                    core._rollback_admitted_claim(ctx)
        if window == "W5":
            core._mark_claim_launch_started(ctx)
        _pause_window(window)


def _initial_remote(directory):
    path = directory / "remote.json"
    path.write_text(
        json.dumps(
            {
                "identifier": ITEM,
                "state": {"id": "st-todo", "name": "Todo", "type": "unstarted"},
                "assignee": None,
                "labels": {"nodes": [{"name": "kew:ready"}]},
                "updatedAt": "unclaimed",
            }
        )
    )
    return path


def _start_window(store, directory, window):
    remote = _initial_remote(directory)
    assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
    child = subprocess.Popen(
        [sys.executable, __file__, "--child", str(store._db_path), str(remote), window],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert select.select([child.stdout], [], [], 20)[0], "Dispatcher did not pause"
        assert child.stdout.readline() == f"{window}\n".encode(), child.communicate(
            timeout=5
        )
    except BaseException:
        _kill(child)
        raise
    return child


def _reconcile_process(store, directory, child):
    # Leave waitpid to production reconciliation. No poll/wait/communicate first.
    os.kill(child.pid, signal.SIGKILL)
    assert select.select([child.stdout], [], [], 20)[0]
    assert child.stdout.read() == b""
    tick.reconcile_epic_reservations(
        EPIC, store, _config(directory), now=datetime.now(UTC)
    )


def _assert_window_result(store, directory, window):
    assert store.get("child").status == "killed"
    assert not store.list_epic_reservations(EPIC)
    assert store.live_run_count() == 0
    markers = store.list_audit_events(event="linear_claim_orphaned", run_id="child")
    if window in ("W2", "W3", "W4"):
        assert len(markers) == 1, "Every unlaunched claim needs an orphan marker"
        assert markers[0].payload["stateId"] == "st-doing"
    else:
        assert not markers
    if window == "W4":
        assert markers[0].payload["snapshot"] == json.loads(
            (directory / "remote.json").read_text()
        )
        assert orphaned_claim(store, ITEM, EPIC) is not None, (
            "The original completion must not supersede its own orphan marker"
        )
    watcher = WorkflowWatcher(store, directory, "Doing")
    outcome = watcher.poll_once()
    retained = store.list_audit_events(event="linear_claim_retained", run_id="child")
    if window == "W5":
        assert outcome is WatchOutcome.RETAINED_CLAIM
        assert len(retained) == 1, "Launched orphan claims need recovery guidance"
        assert retained[0].payload["recovery"] == LINEAR_CLAIM_RECOVERY
        assert not watcher.dispatched
    else:
        assert outcome is WatchOutcome.DISPATCHED
        assert watcher.dispatched == watcher.claimed == [ITEM]
        _release_retry(store, directory)
        assert watcher.poll_once() is WatchOutcome.SKIPPED_NOT_RECOGNIZED
        assert watcher.dispatched == [ITEM], "Recovery must dispatch exactly once"
    assert watcher.dispatched == [ITEM] or retained, (
        "No item may remain assigned without dispatch or retained recovery guidance"
    )


def _exercise_window(directory, window):
    with SqliteRunStore(directory / "runs.db") as store:
        child = _start_window(store, directory, window)
        try:
            assert store.get("child").status == "pending", (
                "Retention failure must not finalize the admitted run"
            )
            assert store.get("child").owner_pid == child.pid
            completed = store.list_audit_events(event="linear_claim_completed")
            assert bool(completed) == (window in ("W4", "W5"))
            started = store.list_audit_events(event="linear_claim_started")
            assert bool(started) == (window != "W1")
            _reconcile_process(store, directory, child)
            _assert_window_result(store, directory, window)
        finally:
            _kill(child)


@pytest.mark.serial
@pytest.mark.parametrize("window", WINDOWS)
def test_round26_real_process_window(tmp_path, window):
    """W1/W2 controls; W3 pending, W4 marker, W5 recovery assertions fail on base."""
    _exercise_window(tmp_path, window)


@pytest.mark.serial
def test_round26_lifecycle_table_has_no_stranded_item(tmp_path):
    for window in WINDOWS:
        directory = tmp_path / window
        directory.mkdir()
        _exercise_window(directory, window)


class SemanticRetry(WindowTransport):
    def __init__(self, remote, response, ambiguous):
        super().__init__(remote, "Doing")
        self.response = response
        self.ambiguous = ambiguous
        self.mutations = 0
        self.verifications = 0

    def __call__(self, url, payload, headers):
        if "IssueClaimState" in json.loads(payload)["query"]:
            self.verifications += 1
        return super().__call__(url, payload, headers)

    def apply(self, fields):
        self.mutations += 1
        if self.ambiguous and self.mutations == 1:
            super().apply(fields)
            raise ConnectionResetError("response lost after commit")
        return _ok(self.response)


@pytest.mark.parametrize(
    "response",
    [{"issueUpdate": {"success": False}}, {}, {"issueUpdate": None}, None],
    ids=["false", "missing", "null-operation", "null-data"],
)
def test_round26_semantic_retry_preserves_ambiguity(
    admission_owner, tmp_path, response
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
        _admit_host(store, _config(tmp_path), "child")
        remote = _initial_remote(tmp_path)
        transport = SemanticRetry(remote, response, True)
        client = LinearClient("round26-fixture", transport=transport, sleep=Mock())
        tracker = LinearTracker(tracker_config(in_review_state="QA"), client)
        claim = _LinearDispatchClaim(tracker, store, ITEM, EPIC)
        with pytest.raises((LinearApiError, KeyError, TypeError)) as raised:
            claim.admit("child")
        assert tracker.mutation_outcome_ambiguous, (
            "Semantic rejection cannot clear response loss"
        )
        assert isinstance(raised.value, LinearApiError) and raised.value.ambiguous
        assert client.mutation_outcome_ambiguous
        assert tracker.mutation_outcome_ambiguous
        assert len(store.list_audit_events(event="linear_claim_outcome_unknown")) == 1
        claim.retain("claim failed")
        assert transport.mutations == 2
        assert transport.verifications == 1
        [retained] = store.list_audit_events(event="linear_claim_retained")
        assert retained.payload["snapshot"] == transport.remote_issue()
        [completed] = store.list_audit_events(event="linear_claim_completed")
        assert completed.payload["source"] == "verification"
        assert not claim.attempted


def test_round26_definite_semantic_rejection_control(tmp_path):
    """Control: a rejection without earlier ambiguity must remain definite."""
    transport = SemanticRetry(_initial_remote(tmp_path), {"issueUpdate": {}}, False)
    client = LinearClient("round26-fixture", transport=transport)
    tracker = LinearTracker(tracker_config(in_review_state="QA"), client)
    with pytest.raises(LinearApiError) as raised:
        tracker.claim(ITEM)
    assert not raised.value.ambiguous
    assert not tracker.mutation_outcome_ambiguous
    assert transport.verifications == 0


@pytest.mark.parametrize("mode", ["before", "after", "drop"])
def test_round26_retention_retries_until_durable(
    admission_owner, tmp_path, monkeypatch, mode
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, transport = _confirmed_claim(store, tmp_path)
        claim.admit("child")
        fault = RetentionFault(store, claim, 1, mode=mode)
        fault.install(store)
        monkeypatch.setattr(core.time, "sleep", Mock())
        ctx = _context(store, tmp_path, claim)
        core._rollback_admitted_claim(ctx)
        assert len(fault.guards) == 2, "Retry until retention is written and read back"
        assert all(fault.guards), "Keep the guard throughout persistence"
        assert not claim.attempted
        assert store.get("child").status == "failure"
        [retained] = store.list_audit_events(event="linear_claim_retained")
        assert retained.payload["snapshot"] == transport.remote_issue()
        core._rollback_admitted_claim(ctx)
        assert len(fault.guards) == 2, "Successful delivery must be idempotent"
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1


def test_round26_persistent_retention_keeps_pending_until_reconcile(
    admission_owner, tmp_path, monkeypatch
):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _ = _confirmed_claim(store, tmp_path)
        claim.admit("child")
        fault = RetentionFault(store, claim, 100)
        fault.install(store)
        monkeypatch.setattr(core.time, "sleep", Mock())
        ctx = _context(store, tmp_path, claim)
        with pytest.raises(OSError, match="retention unavailable"):
            core._rollback_admitted_claim(ctx)
        assert claim.attempted
        assert not ctx.refuse_rollback.handled
        assert len(fault.guards) == 6
        assert store.get("child").status == "pending"
        assert store.list_epic_reservations(EPIC)
        assert not store.list_audit_events(event="linear_claim_retained")
        tick._reconcile_reservations(
            EPIC,
            store,
            _config(tmp_path),
            now=datetime.now(UTC),
            liveness=Mock(return_value=False),
        )
        _assert_window_result(store, tmp_path, "W4")


def test_round26_failed_launch_evidence_preserves_retention_guard(
    admission_owner, tmp_path, monkeypatch
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _ = _confirmed_claim(store, tmp_path)
        claim.admit("child")
        ctx = _context(store, tmp_path, claim)
        monkeypatch.setattr(
            store,
            "append_audit_event_once",
            Mock(side_effect=OSError("audit unavailable")),
        )
        monkeypatch.setattr(core.time, "sleep", Mock())
        with pytest.raises(RuntimeError, match="linear_claim_launched"):
            core._mark_claim_launch_started(ctx)
        assert not ctx.claim_launch_started, "Launch ownership needs durable evidence"
        with pytest.raises(OSError, match="audit unavailable"):
            core._rollback_admitted_claim(ctx)
        assert claim.attempted
        assert not ctx.refuse_rollback.handled
        assert store.get("child").status == "pending"
        assert store.list_epic_reservations(EPIC)
        assert not store.list_audit_events(event="linear_claim_retained")


class EarlyFinalizer:
    def __init__(self, kind):
        self.kind = kind
        self.called = False

    def __call__(self, run, **_kwargs):
        self.called = True
        if self.kind == "host-identity":
            from kew.process_groups import HostIdentityUnavailable

            return core._host_identity_refusal(
                run, HostIdentityUnavailable("attempt-identity-unavailable"), 0
            )
        if self.kind == "host-error":
            return core._retry_exception_outcome(run, OSError("launch failed"), 0)
        if self.kind == "host-result":
            return core._finalize_result(run, core._RetryOutcome())
        dispatch_sandbox._finalize_sandbox_run(
            run.store, run.run_id, exit_code=1, error_category="sandbox_launch_error"
        )
        raise OSError("launch failed")


@pytest.mark.parametrize(
    "kind", ["host-error", "host-result", "host-identity", "sandbox"]
)
def test_round26_inner_finalization_preserves_ownership(
    admission_owner, linear_child, monkeypatch, kind
):
    child = linear_child
    fault = RetentionFault(child.store, Mock(attempted=True), 100)
    fault.install(child.store)
    finalizer = EarlyFinalizer(kind)
    monkeypatch.setattr(core, "_compute_breaker_baseline", Mock(return_value=(None, 0)))
    monkeypatch.setattr(core, "_run_retry_loop", finalizer)
    release = Mock()
    monkeypatch.setattr(core, "release_checkout_claims", release)
    monkeypatch.setattr(core.time, "sleep", Mock())
    result = child.invoke()
    assert result.exit_code == 1
    assert finalizer.called
    [row] = child.store.list_by_item(ITEM)
    assert row.status == "pending", "Inner finalizers must await durable claim recovery"
    assert child.store.list_epic_reservations("alpha")
    assert not child.store.list_audit_events(event="linear_claim_retained")
    assert not child.store.list_audit_events(event="dispatch_completed")
    release.assert_not_called()


@pytest.mark.parametrize("reject", [False, True])
def test_round26_completed_marker_and_release_are_atomic(
    admission_owner, tmp_path, reject
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _ = _confirmed_claim(store, tmp_path)
        claim.admit("child")
        [reservation] = store.list_epic_reservations(EPIC)
        observer = AtomicReapObserver(store, reject)
        if reject:
            with pytest.raises(RuntimeError, match="reject reap"):
                store.finalize_orphaned_pending(
                    "child",
                    error_category="orphaned_pending_reaped",
                    finished_at=datetime.now(UTC),
                    reservation=reservation,
                    expected_owner=store.get("child"),
                    precommit_check=observer,
                )
            assert store.get("child").status == "pending"
            assert store.list_epic_reservations(EPIC)
            assert not store.list_audit_events(event="linear_claim_orphaned")
        else:
            store.finalize_orphaned_pending(
                "child",
                error_category="orphaned_pending_reaped",
                finished_at=datetime.now(UTC),
                reservation=reservation,
                expected_owner=store.get("child"),
                precommit_check=observer,
            )
            assert orphaned_claim(store, ITEM, EPIC)
            _event(store, "linear_claim_completed", run_id="replacement")
            assert orphaned_claim(store, ITEM, EPIC) is None
        assert observer.calls == 1


@pytest.mark.parametrize("replacement", [False, True])
def test_round26_duplicate_completion_preserves_supersession(
    admission_owner, tmp_path, replacement
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _ = _confirmed_claim(store, tmp_path)
        claim.admit("child")
        if replacement:
            _event(store, "linear_claim_completed", run_id="replacement")
        # A completion commit can succeed before its acknowledgement is lost.
        claim.completion_recorded = False
        claim._record_completion()
        [reservation] = store.list_epic_reservations(EPIC)
        store.finalize_orphaned_pending(
            "child",
            error_category="orphaned_pending_reaped",
            finished_at=datetime.now(UTC),
            reservation=reservation,
        )
        assert store.list_audit_events(event="linear_claim_orphaned")
        assert (orphaned_claim(store, ITEM, EPIC) is None) == replacement


if __name__ == "__main__" and sys.argv[1] == "--child":
    _child_main(*sys.argv[2:])


@pytest.mark.parametrize("failure", ["registration", "permit", None])
def test_round26_spawn_transfers_claim_only_after_identity_permit(monkeypatch, failure):
    from types import SimpleNamespace

    launch = Mock()
    permit = Mock(side_effect=RuntimeError("refused") if failure == "permit" else None)
    register = Mock(
        return_value=permit,
        side_effect=RuntimeError("refused") if failure == "registration" else None,
    )
    monkeypatch.setattr(core, "_record_retry_pgid", register)
    run = SimpleNamespace(
        store=Mock(),
        run_id="waiting",
        on_launch=launch,
        contained_image="image",
        launch_state=None,
    )
    if failure == "registration":
        with pytest.raises(RuntimeError, match="refused"):
            core._record_retry_spawn(run, 123)
        permit.assert_not_called()
    else:
        check = core._record_retry_spawn(run, 123)
        launch.assert_not_called()
        if failure == "permit":
            with pytest.raises(RuntimeError, match="refused"):
                check()
        else:
            check()
        permit.assert_called_once_with()
    if failure is None:
        launch.assert_called_once_with()
    else:
        launch.assert_not_called()


def test_round26_owner_replacement_prevents_claim_reap(admission_owner, tmp_path):
    from dataclasses import replace

    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _ = _confirmed_claim(store, tmp_path)
        claim.admit("child")
        original = store.get("child")
        [reservation] = store.list_epic_reservations(EPIC)
        replacement = replace(original, owner_start_token="replacement-birth")
        store.save(replacement, update_ownership=True)
        assert not store.finalize_orphaned_pending(
            "child",
            error_category="orphaned_pending_reaped",
            finished_at=datetime.now(UTC),
            reservation=reservation,
            expected_owner=original,
        )
        assert store.get("child").status == "pending"
        assert store.list_epic_reservations(EPIC) == [reservation]
        assert not store.list_audit_events(event="linear_claim_orphaned")
