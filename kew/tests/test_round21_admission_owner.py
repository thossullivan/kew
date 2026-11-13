"""Durable dispatch ownership across the post-admission claim crash window."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_remediation_flow_gate import pinned_dispatch_scene as pinned_dispatch_scene
from test_round18_linear_compensation import linear_child as linear_child

from kew import dispatch_core as core
from kew import dispatch_sandbox, process_groups
from kew.audit import Actor
from kew.cli.epic import _LinearDispatchClaim
from kew.config import DispatchConfig
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import WatchOutcome, make_is_terminally_decided, process_candidate
from kew.delegate.watch import DelegationWatcher, MetaCursorStore
from kew.graph.driver import review_launcher, tick
from kew.inference_transport import (
    AdmittedInferenceTransport,
    ResolvedInferenceTransport,
)
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore

ITEM = "SFD-42"
EPIC = "round21"


def _config(directory):
    config = DispatchConfig()
    config.tracker.type = "linear"
    config.tracker.linear.team = "SFD"
    config.parallel.max_workers = 1
    config.logging.dir = str(directory / "logs")
    config.branches.worktree_base = str(directory / "worktrees")
    return config


def _admit_host(store, config, run_id, item_id=ITEM):
    reservation = next(
        r for r in store.list_epic_reservations(EPIC) if r.item_id == item_id
    )
    tracked = core._TrackedRun(
        config=config,
        store=store,
        run_id=run_id,
        issue_num=0,
        item_id=item_id,
        branch="round21",
        title="Crash before claim completion",
        complexity="",
        run_kind="delegation",
        loop_id=None,
        prefix_key=None,
        provider=None,
        runner=SimpleNamespace(name="round21-fake"),
        effective_model="test",
        actor=Actor("policy", "round21"),
        endpoint=None,
        repo=None,
        log_file=Path(config.logging.dir) / f"{run_id}.log",
        dispatch_start=0.0,
        transport=AdmittedInferenceTransport(
            ResolvedInferenceTransport("direct", "test", "test", ""), None, "admitted"
        ),
        source_tracker="linear",
        reservation=reservation,
        reservation_pinned=True,
    )
    core._save_pending_run(
        tracked, worker_cap=1, cap_exclude_issue=None, admit_budget_check=None
    )


class BlockingClaim:
    def claim_assignee(self):
        return "viewer"

    def claim_state(self):
        return {"id": "started", "name": "In Progress"}

    def claim(self, _item_id):
        print("mid-hook", flush=True)
        signal.pause()
        raise AssertionError("The test must kill the hook owner")


def _child_main(db_path, run_id, item_id):
    with SqliteRunStore(Path(db_path)) as store:
        _admit_host(store, _config(Path(db_path).parent), run_id, item_id)
        claim = _LinearDispatchClaim(BlockingClaim(), store, item_id, EPIC)
        core._invoke_admitted(claim.admit, run_id)


def _start_hook(store, directory, run_id="child", item_id=ITEM):
    assert store.reserve_epic_node_item(EPIC, item_id, f"reservation-{run_id}")
    child = subprocess.Popen(
        [sys.executable, __file__, "--child", str(store._db_path), run_id, item_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert select.select([child.stdout], [], [], 20)[0], "Child never entered hook"
        line = child.stdout.readline()
        assert line == b"mid-hook\n", child.communicate(timeout=5)
    except BaseException:
        _kill(child)
        raise
    return child


def _kill(child):
    if child.poll() is None:
        child.send_signal(signal.SIGKILL)
    child.communicate(timeout=10)


class RetryWatcher:
    """Recording Linear boundary; watcher, claim seam, and admission stay real."""

    def __init__(self, store, config, config_path):
        self.store = store
        self.config = config
        self.config_path = config_path
        self.dispatched = []
        self.claimed = []
        self.watcher = DelegationWatcher(
            events=store,
            poll=self,
            cursor_store=MetaCursorStore(store),
            assignee_id="viewer",
            label="kew:ready",
            is_terminally_decided=make_is_terminally_decided(store, EPIC),
            poll_interval_seconds=0,
            clock=partial(float, 1),
        )

    def list_delegated(self, *_args):
        return [ITEM]

    def fetch_live(self, _item):
        return CandidateIssue(
            assignee_id=None, labels=("kew:ready",), state="Todo", state_id="Todo"
        )

    def claim_assignee(self):
        return "viewer"

    def claim_state(self):
        return {"id": "started", "name": "In Progress"}

    def claim(self, item):
        self.claimed.append(item)
        return {
            "issue": {
                "identifier": item,
                "updatedAt": "claimed",
                "state": self.claim_state(),
                "assignee": {"id": "viewer"},
            }
        }

    def comment(self, *_args):
        raise AssertionError("Retry must not be refused")

    def dispatch(self, item):
        self.dispatched.append(item)
        _admit_host(self.store, self.config, "retry", item)
        claim = _LinearDispatchClaim(self, self.store, item, EPIC)
        core._invoke_admitted(claim.admit, "retry")
        return True

    def tick(self):
        [candidate] = self.watcher.tick()
        return process_candidate(
            candidate,
            member_id="viewer",
            label="kew:ready",
            store=self.store,
            tracker=self,
            sink=self,
            fleet_cap=1,
            dry_run=False,
            config_path=self.config_path,
            epic_ref=EPIC,
        )


@pytest.mark.serial
def test_round21_sigkill_mid_hook_reaps_and_watcher_redispatches(tmp_path):
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    with SqliteRunStore(tmp_path / "runs.db") as store:
        child = _start_hook(store, tmp_path)
        _kill(child)
        assert child.returncode == -signal.SIGKILL
        assert store.list_audit_events(event="linear_claim_started", run_id="child")
        assert not store.list_audit_events(
            event="linear_claim_completed", run_id="child"
        )
        tick.reconcile_epic_reservations(EPIC, store, config, now=datetime.now(UTC))
        row = store.get("child")
        assert row.status == "killed", (
            "Dead admitted owner must release its worker slot"
        )
        assert row.error_category == "orphaned_pending_reaped"
        assert row.exit_code == 137
        assert store.list_epic_reservations(EPIC) == []
        assert store.live_run_count() == 0
        assert store.list_audit_events(event="linear_claim_retained") == []
        watcher = RetryWatcher(store, config, config_path)
        assert watcher.fetch_live(ITEM).state == "Todo"
        assert watcher.tick() is WatchOutcome.DISPATCHED
        assert watcher.dispatched == watcher.claimed == [ITEM]
        assert store.get("retry").status == "pending"
        assert store.live_run_count() == 1


class ObserveClaim:
    def __init__(self, original, store):
        self.original = original
        self.store = store
        self.order = []

    def __call__(self, run_id):
        with SqliteRunStore(self.store._db_path, read_only=True) as reader:
            row = reader.get(run_id)
            assert row.pgid == os.getpid(), "Hook must observe committed dispatch PGID"
            assert row.owner_pid == os.getpid()
            assert reader.get_admission_identity(run_id) is not None
            assert reader.list_audit_events(
                event="dispatch_reservation_bound", run_id=run_id
            )
        self.order.append("owner_committed")
        self.original(run_id)
        self.order.append("hook_returned")


class ClaimObserverFactory:
    def __init__(self, original, store):
        self.original = original
        self.store = store
        self.observer = None

    def invoke(self, callback, run_id):
        self.observer = ObserveClaim(callback, self.store)
        self.original(self.observer, run_id)


def test_round21_hook_observes_committed_owner(
    admission_owner, linear_child, monkeypatch
):
    # Model the detached process's established exclusive group without changing
    # the test runner's real process group.
    monkeypatch.setattr("kew.subprocess_utils.os.getpgrp", os.getpid)
    factory = ClaimObserverFactory(core._invoke_admitted, linear_child.store)
    monkeypatch.setattr(core, "_invoke_admitted", factory.invoke)
    result = linear_child.invoke()
    assert factory.observer.order == ["owner_committed", "hook_returned"], result.output
    assert len(linear_child.client.claims) == 1
    assert linear_child.store.list_audit_events(event="linear_claim_retained")


@pytest.mark.serial
def test_round21_two_day_dead_owner_reaped_live_hook_retained(tmp_path):
    config = _config(tmp_path)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        dead = _start_hook(store, tmp_path)
        _kill(dead)
        row = store.get("child")
        row.started_at = datetime.now(UTC) - timedelta(days=2)
        store.save(row)
        tick.reconcile_epic_reservations(EPIC, store, config, now=datetime.now(UTC))
        assert store.get("child").status == "killed", "Age must not hide a dead owner"
        assert store.live_run_count() == 0
        live = _start_hook(store, tmp_path, "live")
        try:
            row = store.get("live")
            row.started_at = datetime.now(UTC) - timedelta(days=2)
            store.save(row)
            assert tick.default_run_liveness(config)(row) is True
            assert tick.reconcile_epic_reservations(
                EPIC, store, config, now=datetime.now(UTC)
            ) == {ITEM}
            assert store.get("live").status == "pending"
            assert store.live_run_count() == 1
        finally:
            _kill(live)


class AdmissionObserver:
    def __init__(self, store, reject):
        self.store = store
        self.original = store.admit_run_with_identity
        self.reject = reject
        self.observed = []
        self.enricher = None

    def admit(self, record, identity, **kwargs):
        self.enricher = kwargs.pop("enrich", None)
        return self.original(record, identity, **kwargs, enrich=self.inspect)

    def inspect(self, cursor):
        row = cursor.execute("SELECT * FROM runs").fetchone()
        assert row["pgid"] == os.getpid(), "Owner PGID must exist inside admission"
        owner = self.store.get(row["run_id"])
        assert owner.owner_pid == os.getpid()
        with SqliteRunStore(self.store._db_path, read_only=True) as reader:
            assert reader.get(row["run_id"]) is None, (
                "Admission must still be uncommitted"
            )
        self.observed.append("identity_in_transaction")
        if self.enricher is not None:
            self.enricher(cursor)
        if self.reject:
            raise RuntimeError("reject admission")


def _other_admission(kind, store, config, directory):
    if kind == "host":
        _admit_host(store, config, "other")
        return "other"
    if kind == "sandbox":
        [reservation] = store.list_epic_reservations(EPIC)
        return dispatch_sandbox._record_sandbox_start(
            store,
            run_id="other",
            issue_num=0,
            item_id=ITEM,
            branch="round21",
            title="t",
            model="test",
            runner_name="round21-fake",
            log_file=directory / "other.log",
            tracker="linear",
            worker_cap=1,
            reservation=reservation,
            reservation_pinned=True,
        )
    context = ReviewerLaunchContext(
        worktree=directory,
        prompt="prompt",
        prompt_artifact_sha="hash",
        run_id="other",
        item_id=ITEM,
        head_sha="head",
        log_dir=directory,
    )
    review_launcher._record_pending(
        store, context, config, directory / "other.log", "test", "round21-fake"
    )
    return "other"


@pytest.mark.parametrize("kind", ["host", "sandbox", "review"])
@pytest.mark.parametrize("reject", [False, True])
def test_round21_admission_owner_is_atomic(
    admission_owner, tmp_path, monkeypatch, kind, reject
):
    monkeypatch.setattr("kew.subprocess_utils.os.getpgrp", os.getpid)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation")
        observer = AdmissionObserver(store, reject)
        monkeypatch.setattr(store, "admit_run_with_identity", observer.admit)
        if reject:
            with pytest.raises(RuntimeError, match="reject admission"):
                _other_admission(kind, store, _config(tmp_path), tmp_path)
            assert store.get("other") is None
            assert not store.list_audit_events(event="dispatch_owner_recorded")
            assert not store.list_audit_events(event="dispatch_reservation_bound")
        else:
            _other_admission(kind, store, _config(tmp_path), tmp_path)
            assert store.get("other").owner_pid == os.getpid()
        assert observer.observed == ["identity_in_transaction"]


class FailedClaim:
    def claim_assignee(self):
        return "viewer"

    def claim_state(self):
        return {"id": "started", "name": "In Progress"}

    def claim(self, _item):
        raise RuntimeError("claim did not return")


def test_round21_missing_claim_completion_is_not_retained(admission_owner, tmp_path):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation")
        _admit_host(store, _config(tmp_path), "failed-claim")
        claim = _LinearDispatchClaim(FailedClaim(), store, ITEM, EPIC)
        with pytest.raises(RuntimeError, match="claim did not return"):
            claim.admit("failed-claim")
        claim.retain("hook failed")
        assert store.list_audit_events(event="linear_claim_completed") == []
        assert store.list_audit_events(event="linear_claim_retained") == [], (
            "An incomplete claim must not emit retention evidence"
        )


class ReplaceDuringProbe:
    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.probe = tick.default_run_liveness(config)

    def __call__(self, row):
        verdict = self.probe(row)
        if verdict is False:
            self.store.release_epic_node_item(ITEM, run_id="reservation-child")
            assert self.store.reserve_epic_node_item(EPIC, ITEM, "replacement")
            _admit_host(self.store, self.config, "replacement-child")
        return verdict


@pytest.mark.serial
def test_round21_reap_preserves_replacement_generation(tmp_path):
    config = _config(tmp_path)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        child = _start_hook(store, tmp_path)
        _kill(child)
        tick._reconcile_reservations(
            EPIC,
            store,
            config,
            now=datetime.now(UTC),
            liveness=ReplaceDuringProbe(store, config),
        )
        assert store.get("child").status == "killed", "Reap only the dead generation"
        [reservation] = store.list_epic_reservations(EPIC)
        assert reservation.run_id == "replacement"
        assert store.get("replacement-child").status == "pending"
        assert store.live_run_count() == 1


def test_round21_shared_group_establishes_exclusive_owner(tmp_path, monkeypatch):
    pid = os.getpid()
    groups = iter((pid + 1, pid))
    monkeypatch.setattr(process_groups.os, "getpgrp", lambda: next(groups))
    moved = []
    monkeypatch.setattr(process_groups.os, "setpgid", lambda *args: moved.append(args))
    snapshot = f"{pid} {os.getppid()} {pid} S python3\n"
    monkeypatch.setattr(
        process_groups,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, snapshot, ""),
    )
    entry = process_groups.ProcessIdentity(
        os.getppid(), pid, "S", "Sun Sep 20 01:00:00 2026", "python3", "admitted-birth"
    )
    monkeypatch.setattr(process_groups, "process_identity", lambda _: entry)
    monkeypatch.setattr(
        process_groups, "process_start_token", lambda _: entry.start_token
    )
    config = _config(tmp_path)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation")
        _admit_host(store, config, "shared")
        with store.read_committed() as reader:
            row = reader.get("shared")
            assert row.owner_pid == row.pgid == pid
        assert moved == [(0, 0)]
        monkeypatch.setattr(
            process_groups,
            "owner_snapshot",
            lambda: process_groups.OwnerSnapshot({pid: entry}),
        )
        assert tick.default_run_liveness(config)(row) is True


def test_round21_owner_without_group_requires_startup_grace(tmp_path, monkeypatch):
    from kew.run_store import RunRecord

    now = datetime.now(UTC)
    row = RunRecord("legacy", 0, "round21", "pending", -1, now, owner_pid=os.getpid())
    probe = tick.default_run_liveness(_config(tmp_path))
    assert probe(row) is None
    assert not tick._pending_run_is_dead(row, lambda _: False, now=now)
    assert tick._pending_run_is_dead(row, lambda _: False, now=now + timedelta(hours=1))


if __name__ == "__main__" and sys.argv[1] == "--child":
    _child_main(*sys.argv[2:])
