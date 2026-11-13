"""Claim-state identity and confirmed-retention regressions against 148883eb."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_linear_tracker import FakeLinear, _ok
from test_linear_tracker import _config as tracker_config
from test_round21_admission_owner import (
    EPIC,
    ITEM,
    RetryWatcher,
    _admit_host,
    _config,
    _kill,
)

from kew.cli.delegate import _LinearDelegationTracker
from kew.cli.epic import _LinearDispatchClaim
from kew.delegate.run import WatchOutcome
from kew.graph.driver import tick
from kew.linear.client import LinearClient
from kew.linear.tracker import LinearTracker
from kew.run_store import SqliteRunStore


class AppliedWorkflow(FakeLinear):
    """Use the existing configurable workflow fixture with durable fake mutations."""

    def __init__(self, remote_path, state_name, interruption=None):
        state_id = "st-doing" if state_name == "Doing" else "st-progress"
        super().__init__(
            states=[
                {"id": "st-todo", "name": "Todo", "type": "unstarted"},
                {"id": "st-qa", "name": "QA", "type": "started"},
                {"id": state_id, "name": state_name, "type": "started"},
                {"id": "st-done", "name": "Done", "type": "completed"},
            ]
        )
        self.remote_path = Path(remote_path)
        self.interruption = interruption
        self.live_reads = 0
        self.change_on_read = None

    def __call__(self, url, payload, headers):
        request = json.loads(payload)
        query = request["query"]
        if "DelegationLive" in query:
            self.live_reads += 1
            if self.live_reads == self.change_on_read:
                self.move_state("human-state", "In Progress")
            return _ok({"issue": self.remote_issue()})
        if "issueUpdate" in query:
            return self.apply(request["variables"]["input"])
        return super().__call__(url, payload, headers)

    def remote_issue(self):
        return json.loads(self.remote_path.read_text())

    def move_state(self, state_id, name):
        issue = self.remote_issue()
        issue["state"] = {"id": state_id, "name": name, "type": "started"}
        self.remote_path.write_text(json.dumps(issue))

    def apply(self, fields):
        issue = {
            "identifier": ITEM,
            "state": next(s for s in self.states if s["id"] == fields["stateId"]),
            "assignee": {"id": fields["assigneeId"]},
            "labels": {"nodes": []},
            "updatedAt": "confirmed-version",
        }
        self.remote_path.write_text(json.dumps(issue))
        if self.interruption == "kill":
            print("applied", flush=True)
            signal.pause()
            raise AssertionError("The dispatcher must die before completion")
        if self.interruption == "exit":
            raise SystemExit("dispatcher died after mutation")
        return _ok({"issueUpdate": {"success": True, "issue": issue}})


def _workflow_tracker(remote_path, state_name, interruption=None):
    transport = AppliedWorkflow(remote_path, state_name, interruption)
    client = LinearClient("round25-test-key", transport=transport)
    tracker = LinearTracker(tracker_config(in_review_state="QA"), client)
    return tracker, client, transport


def _child_main(db_path, remote_path, state_name):
    with SqliteRunStore(Path(db_path)) as store:
        _admit_host(store, _config(Path(db_path).parent), "child")
        tracker, _, _ = _workflow_tracker(remote_path, state_name, "kill")
        _LinearDispatchClaim(tracker, store, ITEM, EPIC).admit("child")


class WorkflowWatcher(RetryWatcher):
    def __init__(self, store, directory, state_name):
        config_path = directory / "config.toml"
        config_path.write_text("")
        super().__init__(store, _config(directory), config_path)
        self.remote_path = directory / "remote.json"
        self.tracker, client, self.transport = _workflow_tracker(
            self.remote_path, state_name
        )
        self.adapter = _LinearDelegationTracker(client, self.tracker)
        self.watcher._assignee_id = "viewer-uuid"

    def fetch_live(self, item):
        return self.adapter.fetch_live(item)

    def dispatch(self, item):
        self.dispatched.append(item)
        _admit_host(self.store, self.config, "retry", item)
        claim = _LinearDispatchClaim(self.tracker, self.store, item, EPIC)
        claim.admit("retry")
        self.claimed.append(item)
        return True

    def poll_once(self):
        from kew.delegate.run import process_candidate

        [candidate] = self.watcher.tick()
        return process_candidate(
            candidate,
            member_id="viewer-uuid",
            label="kew:ready",
            store=self.store,
            tracker=self,
            sink=self,
            fleet_cap=1,
            dry_run=False,
            config_path=self.config_path,
            epic_ref=EPIC,
        )


def _reconcile_dead(store, directory):
    tick._reconcile_reservations(
        EPIC,
        store,
        _config(directory),
        now=datetime.now(UTC),
        liveness=Mock(return_value=False),
    )


def _orphan_claim(store, directory, state_name):
    assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
    _admit_host(store, _config(directory), "child")
    tracker, _, _ = _workflow_tracker(directory / "remote.json", state_name, "exit")
    with pytest.raises(SystemExit, match="dispatcher died"):
        _LinearDispatchClaim(tracker, store, ITEM, EPIC).admit("child")
    _reconcile_dead(store, directory)


def _release_retry(store, directory):
    row = store.get("retry")
    row.status = "success"
    store.save(row)
    tick.reconcile_epic_reservations(
        EPIC, store, _config(directory), now=datetime.now(UTC)
    )
    assert store.live_run_count() == 0


@pytest.mark.serial
@pytest.mark.parametrize(
    "state_name,state_id", [("Doing", "st-doing"), ("In Progress", "st-progress")]
)
def test_round25_dead_dispatcher_recovers_intended_state(
    tmp_path, state_name, state_id
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        watcher = WorkflowWatcher(store, tmp_path, state_name)
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
        child = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--child",
                str(store._db_path),
                str(watcher.remote_path),
                state_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 20)[0], (
                "Child never applied claim"
            )
            assert child.stdout.readline() == b"applied\n", child.communicate(timeout=5)
            [started] = store.list_audit_events(
                event="linear_claim_started", run_id="child"
            )
            assert not store.list_audit_events(
                event="linear_claim_completed", run_id="child"
            )
            assert watcher.transport.remote_issue()["state"]["id"] == state_id
            os.kill(child.pid, signal.SIGKILL)
            assert select.select([child.stdout], [], [], 20)[0]
            assert child.stdout.read() == b""
            tick.reconcile_epic_reservations(
                EPIC, store, _config(tmp_path), now=datetime.now(UTC)
            )
            assert store.get("child").status == "killed"
            assert store.list_epic_reservations(EPIC) == []
            [marker] = store.list_audit_events(
                event="linear_claim_orphaned", run_id="child"
            )
            assert (
                started.payload.get("stateId")
                == marker.payload.get("stateId")
                == state_id
            )
            assert (
                started.payload["stateName"]
                == marker.payload["stateName"]
                == state_name
            )
            assert watcher.poll_once() is WatchOutcome.DISPATCHED
            assert store.live_run_count() == 1
            _release_retry(store, tmp_path)
            assert watcher.poll_once() is WatchOutcome.SKIPPED_NOT_RECOGNIZED
            assert watcher.dispatched == watcher.claimed == [ITEM]
        finally:
            _kill(child)


@pytest.mark.parametrize("change_on_read", [1, 2], ids=["watcher", "claim_time"])
def test_round25_human_state_change_is_surfaced(
    monkeypatch, admission_owner, tmp_path, caplog, change_on_read
):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        _orphan_claim(store, tmp_path, "In Progress")
        watcher = WorkflowWatcher(store, tmp_path, "In Progress")
        watcher.transport.change_on_read = change_on_read
        watcher.poll_once()
        assert watcher.dispatched == [], "A different state ID must not recover by name"
        assert not store.list_epic_reservations(EPIC)
        assert ITEM in caplog.text and "needs manual recovery" in caplog.text
        assert "human-state" in caplog.text and "st-progress" in caplog.text


def test_round25_state_rename_recovers_by_id(monkeypatch, admission_owner, tmp_path):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        _orphan_claim(store, tmp_path, "Doing")
        watcher = WorkflowWatcher(store, tmp_path, "Doing")
        watcher.transport.move_state("st-doing", "Active")
        assert watcher.poll_once() is WatchOutcome.DISPATCHED, (
            "State ID survives a rename"
        )
        assert watcher.dispatched == [ITEM]


class AuditFault:
    def __init__(self, store, claim, completion_failures, retention_fails=False):
        self.append = store.append_audit_event
        self.append_once = store.append_audit_event_once
        self.claim = claim
        self.completion_failures = completion_failures
        self.retention_fails = retention_fails
        self.attempts = []
        self.guards = []

    def __call__(self, **kwargs):
        event = kwargs["event"]
        self.attempts.append(event)
        if event == "linear_claim_completed" and self.completion_failures:
            self.completion_failures -= 1
            raise OSError("completion audit unavailable")
        if event == "linear_claim_retained":
            self.guards.append(self.claim.attempted)
            if self.retention_fails:
                raise OSError("retention audit unavailable")
        if "dedup_key" in kwargs:
            return self.append_once(**kwargs)
        return self.append(**kwargs)


def _confirmed_claim(store, directory):
    assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
    _admit_host(store, _config(directory), "child")
    tracker, _, transport = _workflow_tracker(directory / "remote.json", "Doing")
    return _LinearDispatchClaim(tracker, store, ITEM, EPIC), transport


@pytest.mark.parametrize(
    "completion_failures", [1, 2], ids=["retry_succeeds", "retry_fails"]
)
def test_round25_completion_audit_failure_retains_confirmation(
    admission_owner, tmp_path, monkeypatch, completion_failures
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, transport = _confirmed_claim(store, tmp_path)
        fault = AuditFault(store, claim, completion_failures)
        monkeypatch.setattr(store, "append_audit_event", fault)
        monkeypatch.setattr(store, "append_audit_event_once", fault)
        with pytest.raises(OSError, match="completion audit"):
            claim.admit("child")
        confirmed = deepcopy(transport.remote_issue())
        assert claim.snapshot == confirmed
        claim.retain("admission hook failed")
        retained = store.list_audit_events(
            event="linear_claim_retained", run_id="child"
        )
        assert len(retained) == 1, (
            "Confirmed claims need retention despite audit failure"
        )
        assert retained[0].payload["snapshot"] == confirmed
        assert retained[0].payload["stateId"] == "st-doing"
        assert retained[0].payload["updatedAt"] == "confirmed-version"
        assert fault.guards == [True], (
            "Keep the delivery guard until retention persists"
        )
        assert not claim.attempted
        assert fault.attempts[-2:] == [
            "linear_claim_completed",
            "linear_claim_retained",
        ]
        completed = store.list_audit_events(
            event="linear_claim_completed", run_id="child"
        )
        assert len(completed) == (1 if completion_failures == 1 else 0)
        if completed:
            assert completed[0].id < retained[0].id
        claim.retain("duplicate delivery")
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1
        assert transport.remote_issue() == confirmed


def test_round25_both_audit_writes_fail_preserves_guard(
    admission_owner, tmp_path, monkeypatch, caplog
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, transport = _confirmed_claim(store, tmp_path)
        fault = AuditFault(store, claim, 2, retention_fails=True)
        monkeypatch.setattr(store, "append_audit_event", fault)
        monkeypatch.setattr(store, "append_audit_event_once", fault)
        with pytest.raises(OSError, match="completion audit"):
            claim.admit("child")
        with pytest.raises(OSError, match="retention audit"):
            claim.retain("admission hook failed")
        assert claim.attempted, "Failed retention must leave its delivery guard set"
        assert claim.snapshot == transport.remote_issue()
        assert (
            ITEM in caplog.text
            and "Cannot persist Linear claim retention" in caplog.text
        )
        assert not store.list_audit_events(event="linear_claim_completed")
        assert not store.list_audit_events(event="linear_claim_retained")
        fault.retention_fails = False
        claim.retain("retry delivery")
        assert not claim.attempted
        assert len(store.list_audit_events(event="linear_claim_completed")) == 1
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1


if __name__ == "__main__" and sys.argv[1] == "--child":
    _child_main(*sys.argv[2:])
