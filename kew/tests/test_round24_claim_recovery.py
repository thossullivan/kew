"""Transport phases and exact-run orphan recovery regressions against fee59936."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_round21_admission_owner import (
    EPIC,
    ITEM,
    RetryWatcher,
    _admit_host,
    _config,
    _kill,
)
from test_round22_dispatcher_liveness import _claim_scene
from test_round23_linear_ambiguity import REJECTION

from kew import dispatch_core as core
from kew.cli.epic import _LinearDispatchClaim
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import WatchOutcome, _recognize_candidate
from kew.delegate.watch import Candidate
from kew.graph.driver import tick
from kew.linear.client import (
    LinearApiError,
    LinearUnavailableError,
    _default_transport,
)
from kew.run_store import SqliteRunStore


class PhaseServer:
    """Record remote mutation independently of response delivery and audits."""

    def __init__(self, phase, error=None, status=200):
        self.phase = phase
        self.error = error or ConnectionResetError("lost round22-key")
        self.status = status
        self.issue = {"assignee": None, "state": {"id": "ready", "name": "Todo"}}
        self.connections = []
        self.mutations = 0
        self.verifications = 0

    def connection(self, host, timeout=30, **_kwargs):
        assert host == "api.linear.app"
        assert timeout == 30
        conn = PhaseConnection(self)
        self.connections.append(conn)
        return conn

    def legacy_open(self, req, timeout):
        # Exercise the installed urllib request wrapper on the regression base.
        req.timeout = timeout
        response = urllib.request.AbstractHTTPHandler().do_open(self.connection, req)
        if response.status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status, "Bad Request", {}, response
            )
        return response

    def install(self, monkeypatch):
        monkeypatch.setattr("http.client.HTTPSConnection", self.connection)
        monkeypatch.setattr("urllib.request.urlopen", self.legacy_open)


class PhaseConnection:
    sock = None

    def __init__(self, server):
        self.server = server
        self.connected = False
        self.closed = False
        self.explicit_connect = False
        self.query = ""
        self.body = b""

    def set_debuglevel(self, _level):
        pass

    def connect(self):
        self.explicit_connect = not self.query
        if self.server.phase == "connect":
            raise self.server.error
        self.connected = True

    def request(self, method, path, body, headers, **_kwargs):
        assert (method, path) == ("POST", "/graphql")
        self.query = json.loads(body).get("query", "")
        if not self.connected:
            self.connect()
        if "IssueClaimState" in self.query:
            self.server.verifications += 1
            self.body = json.dumps({"data": {"issue": self.server.issue}}).encode()
            return
        if self.server.status == 400:
            self.body = json.dumps(REJECTION).encode()
            return
        self.server.mutations += 1
        self.server.issue = {
            "identifier": ITEM,
            "assignee": {"id": "viewer"},
            "state": {"id": "started", "name": "In Progress"},
            "updatedAt": "committed",
        }
        self.body = json.dumps(
            {"data": {"issueUpdate": {"success": True, "issue": self.server.issue}}}
        ).encode()
        if self.server.phase == "request":
            raise self.server.error

    def getresponse(self):
        if self.server.phase == "getresponse" and "IssueClaimState" not in self.query:
            raise self.server.error
        return PhaseResponse(self)

    def close(self):
        self.closed = True


class PhaseResponse:
    reason = "test"

    def __init__(self, connection):
        self.connection = connection
        self.status = connection.server.status

    def read(self):
        conn = self.connection
        if conn.server.phase == "read" and "IssueClaimState" not in conn.query:
            raise conn.server.error
        return conn.body

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.mark.parametrize("phase", ["request", "getresponse", "read"])
def test_round24_post_connect_loss_verifies_and_retains(
    admission_owner, tmp_path, monkeypatch, phase
):
    server = PhaseServer(phase)
    server.install(monkeypatch)
    monkeypatch.setattr("kew.cli.epic._warn", Mock())
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, _default_transport)
        with pytest.raises(LinearUnavailableError) as raised:
            claim.admit("ambiguous")
        assert raised.value.ambiguous is True, "Sending can commit before a reset"
        assert "round22-key" not in str(raised.value)
        assert client.mutation_outcome_ambiguous is True
        assert server.mutations == 3
        assert len(store.list_audit_events(event="linear_claim_outcome_unknown")) == 1
        claim.retain("hook failed")
        assert server.verifications == 1
        [completed] = store.list_audit_events(event="linear_claim_completed")
        assert completed.payload["source"] == "verification"
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1
        assert all(c.explicit_connect and c.closed for c in server.connections)


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("connect reset"), socket.gaierror("DNS failed")],
    ids=["reset", "dns"],
)
def test_round24_connect_failure_is_definite(
    admission_owner, tmp_path, monkeypatch, error
):
    server = PhaseServer("connect", error)
    server.install(monkeypatch)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _client = _claim_scene(tmp_path, store, _default_transport)
        with pytest.raises(LinearApiError) as raised:
            claim.admit("ambiguous")
        assert raised.value.ambiguous is False
        # Both are socket-level outages: classified linear-unavailable (#1117).
        assert isinstance(raised.value, LinearUnavailableError)
        claim.retain("connect failed")
        assert server.mutations == server.verifications == 0
        assert not store.list_audit_events(event="linear_claim_outcome_unknown")
        assert not store.list_audit_events(event="linear_claim_retained")
        assert all(c.explicit_connect and c.closed for c in server.connections), (
            "Connect must precede request"
        )


def test_round24_complete_400_is_definite(admission_owner, tmp_path, monkeypatch):
    server = PhaseServer(None, status=400)
    server.install(monkeypatch)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, _client = _claim_scene(tmp_path, store, _default_transport)
        with pytest.raises(LinearApiError, match="Invalid claim state") as raised:
            claim.admit("ambiguous")
        assert raised.value.ambiguous is False
        claim.retain("rejected")
        assert not store.list_audit_events(event="linear_claim_outcome_unknown")
        assert not store.list_audit_events(event="linear_claim_retained")
        assert server.mutations == 0
        assert all(c.explicit_connect and c.closed for c in server.connections)


class AppliedBlockingClaim:
    def __init__(self, remote_path):
        self.remote_path = Path(remote_path)

    def claim_state(self):
        return {"id": "started", "name": "In Progress"}

    def claim_assignee(self):
        return "viewer"

    def claim(self, _item):
        self.remote_path.write_text(
            json.dumps(
                {
                    "assignee_id": "viewer",
                    "labels": [],
                    "state": "In Progress",
                    "state_id": "started",
                }
            )
        )
        print("applied", flush=True)
        signal.pause()
        raise AssertionError("The dispatcher must be killed before completion")


def _child_main(db_path, remote_path):
    with SqliteRunStore(Path(db_path)) as store:
        _admit_host(store, _config(Path(db_path).parent), "child")
        claim = _LinearDispatchClaim(
            AppliedBlockingClaim(remote_path), store, ITEM, EPIC
        )
        core._invoke_admitted(claim.admit, "child")


class AppliedRetryWatcher(RetryWatcher):
    def __init__(self, store, config, config_path, remote_path):
        super().__init__(store, config, config_path)
        self.remote_path = remote_path

    def fetch_live(self, _item):
        return CandidateIssue(**json.loads(self.remote_path.read_text()))


def _event(store, event, run_id="child", item_id=ITEM, **payload):
    return store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event=event,
        actor_type="policy",
        actor_id="test",
        run_id=run_id,
        item_id=item_id,
        payload={"epic_ref": EPIC, **payload},
    )


def _pending_claim(store, directory):
    assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
    _admit_host(store, _config(directory), "child")
    return _event(
        store,
        "linear_claim_started",
        updatedAt="snapshot",
        stateId="started",
        stateName="In Progress",
        assigneeId=None,
    )


def _retry_watcher(store, directory):
    remote_path = directory / "remote.json"
    remote_path.write_text(
        json.dumps(
            {
                "assignee_id": "viewer",
                "labels": [],
                "state": "In Progress",
                "state_id": "started",
            }
        )
    )
    config_path = directory / "config.toml"
    config_path.write_text("")
    return AppliedRetryWatcher(store, _config(directory), config_path, remote_path)


class AtomicReapObserver:
    def __init__(self, store, reject=False):
        self.store = store
        self.reject = reject
        self.calls = 0

    def __call__(self):
        if not self.store._conn.in_transaction:
            return
        self.calls += 1
        assert self.store.get("child").status == "killed"
        assert self.store.list_epic_reservations(EPIC) == []
        assert self.store.list_audit_events(event="linear_claim_orphaned")
        with SqliteRunStore(Path(self.store._db_path), read_only=True) as reader:
            assert reader.get("child").status == "pending"
            assert reader.list_epic_reservations(EPIC)
            assert not reader.list_audit_events(event="linear_claim_orphaned")
        if self.reject:
            raise RuntimeError("reject reap")


@pytest.mark.parametrize("reject", [False, True])
def test_round24_marker_and_release_are_atomic(
    monkeypatch, admission_owner, tmp_path, reject
):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        started_id = _pending_claim(store, tmp_path)
        [reservation] = store.list_epic_reservations(EPIC)
        observer = AtomicReapObserver(store, reject)
        kwargs = dict(now=datetime.now(UTC), precommit_check=observer)
        row = store.get("child")
        if reject:
            with pytest.raises(RuntimeError, match="reject reap"):
                tick._reap_orphaned_run(
                    store, _config(tmp_path), reservation, row, **kwargs
                )
            assert store.get("child").status == "pending"
            assert store.list_epic_reservations(EPIC)
            assert not store.list_audit_events(event="linear_claim_orphaned")
        else:
            assert tick._reap_orphaned_run(
                store, _config(tmp_path), reservation, row, **kwargs
            )
            [marker] = store.list_audit_events(event="linear_claim_orphaned")
            assert (marker.run_id, marker.item_id) == ("child", ITEM)
            assert marker.payload == dict(
                epic_ref=EPIC,
                run_id="child",
                item_id=ITEM,
                started_event_id=started_id,
                updatedAt="snapshot",
                stateId="started",
                stateName="In Progress",
                assigneeId=None,
            )
            assert not store.finalize_orphaned_pending(
                "child",
                error_category="orphaned_pending_reaped",
                finished_at=datetime.now(UTC),
            )
            assert len(store.list_audit_events(event="linear_claim_orphaned")) == 1
        assert observer.calls == 1


def test_round24_reaped_claim_redispatches_once(
    monkeypatch, admission_owner, tmp_path, caplog
):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        _pending_claim(store, tmp_path)
        tick._reconcile_reservations(
            EPIC,
            store,
            _config(tmp_path),
            now=datetime.now(UTC),
            liveness=Mock(return_value=False),
        )
        watcher = _retry_watcher(store, tmp_path)
        assert watcher.tick() is WatchOutcome.DISPATCHED, (
            "Orphaned In Progress must be actionable"
        )
        assert watcher.dispatched == watcher.claimed == [ITEM]
        assert store.live_run_count() == 1
        assert "recovering orphaned Linear claim" in caplog.text
        # Remove the cap as an explanation for the next poll's decision.
        row = store.get("retry")
        row.status = "success"
        store.save(row)
        tick.reconcile_epic_reservations(
            EPIC, store, _config(tmp_path), now=datetime.now(UTC)
        )
        assert store.live_run_count() == 0
        assert watcher.tick() is WatchOutcome.SKIPPED_NOT_RECOGNIZED
        assert watcher.dispatched == [ITEM]


@pytest.mark.serial
def test_round24_sigkill_after_remote_commit_redispatches_once(tmp_path):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        watcher = _retry_watcher(store, tmp_path)
        watcher.remote_path.write_text(
            json.dumps(
                {
                    "assignee_id": None,
                    "labels": ["kew:ready"],
                    "state": "Todo",
                    "state_id": "ready",
                }
            )
        )
        assert watcher.fetch_live(ITEM).state == "Todo"
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
        child = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--child",
                str(store._db_path),
                str(watcher.remote_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert select.select([child.stdout], [], [], 20)[0], (
                "Child never committed mutation"
            )
            assert child.stdout.readline() == b"applied\n"
            assert watcher.fetch_live(ITEM).state == "In Progress"
            assert watcher.fetch_live(ITEM).assignee_id == "viewer"
            assert store.list_audit_events(event="linear_claim_started", run_id="child")
            assert not store.list_audit_events(
                event="linear_claim_completed", run_id="child"
            )
            os.kill(child.pid, signal.SIGKILL)
            # Wait for pipe EOF, without reaping. Production waitpid proves death.
            assert select.select([child.stdout], [], [], 20)[0]
            assert child.stdout.read() == b""
            tick.reconcile_epic_reservations(
                EPIC, store, _config(tmp_path), now=datetime.now(UTC)
            )
            assert store.get("child").status == "killed"
            assert store.get("child").exit_code == 137
            assert store.list_epic_reservations(EPIC) == []
            assert store.live_run_count() == 0
            assert store.list_audit_events(
                event="linear_claim_orphaned", run_id="child"
            ), "Lost completed mutation must have recovery evidence"
            assert watcher.tick() is WatchOutcome.DISPATCHED
            assert store.live_run_count() == 1
            assert watcher.tick() is WatchOutcome.SKIPPED_NOT_RECOGNIZED
            assert watcher.dispatched == watcher.claimed == [ITEM]
        finally:
            _kill(child)


@pytest.mark.parametrize(
    "settled,before_reap",
    [
        ("linear_claim_completed", False),
        ("linear_claim_retained", False),
        ("linear_claim_completed", True),
    ],
)
def test_round24_resolution_supersedes_marker(
    monkeypatch, admission_owner, tmp_path, settled, before_reap
):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        _pending_claim(store, tmp_path)
        if before_reap:
            _event(store, settled, run_id="replacement")
        tick._reconcile_reservations(
            EPIC,
            store,
            _config(tmp_path),
            now=datetime.now(UTC),
            liveness=Mock(return_value=False),
        )
        assert store.list_audit_events(event="linear_claim_orphaned")
        if not before_reap:
            _event(store, settled, run_id="replacement")
        watcher = _retry_watcher(store, tmp_path)
        expected = (
            WatchOutcome.RETAINED_CLAIM
            if settled == "linear_claim_retained"
            else WatchOutcome.SKIPPED_NOT_RECOGNIZED
        )
        assert watcher.tick() is expected
        assert watcher.dispatched == []


@pytest.mark.parametrize("evidence", [None, "linear_claim_retained"])
def test_round24_in_progress_without_marker_stays_strict(tmp_path, evidence):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        if evidence:
            _event(store, evidence)
        watcher = _retry_watcher(store, tmp_path)
        expected = (
            WatchOutcome.RETAINED_CLAIM
            if evidence
            else WatchOutcome.SKIPPED_NOT_RECOGNIZED
        )
        assert watcher.tick() is expected
        assert watcher.dispatched == []


@pytest.mark.parametrize(
    "assignee,epic_admitted,epic_ref",
    [
        (None, False, EPIC),
        ("someone-else", False, EPIC),
        ("viewer", True, EPIC),
        ("viewer", False, "other-epic"),
    ],
)
def test_round24_marker_requires_current_ownership(
    monkeypatch, admission_owner, tmp_path, assignee, epic_admitted, epic_ref
):
    from kew.process_groups import OwnerSnapshot

    monkeypatch.setattr(
        "kew.process_groups.owner_snapshot", Mock(return_value=OwnerSnapshot({}))
    )
    with SqliteRunStore(tmp_path / "runs.db") as store:
        _pending_claim(store, tmp_path)
        tick._reconcile_reservations(
            EPIC,
            store,
            _config(tmp_path),
            now=datetime.now(UTC),
            liveness=Mock(return_value=False),
        )
        assert store.list_audit_events(event="linear_claim_orphaned")
        live = CandidateIssue(
            assignee, ("kew:ready",), "In Progress", "started", epic_admitted
        )
        candidate = Candidate(ITEM, "poll")
        assert (
            _recognize_candidate(
                store, candidate, live, "viewer", "kew:ready", epic_ref
            )
            is WatchOutcome.SKIPPED_NOT_RECOGNIZED
        )


if __name__ == "__main__" and sys.argv[1] == "--child":
    _child_main(*sys.argv[2:])
