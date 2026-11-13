"""Round-16 owner death frees capacity only after authenticated attempt cleanup."""

import contextlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from unittest.mock import Mock

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from test_liveness_owner import _record
from test_liveness_owner import process_table as process_table
from test_round14_liveness import ATTEMPT, OWNER, _row, _tokens

from kew import process_groups, stop_recovery
from kew.config import DispatchConfig
from kew.fleet import resume_fleet, stop_fleet
from kew.graph.driver import tick
from kew.run_store import RunRecord, SqliteRunStore

SURFACES = ("plain", "identity", "reservation", "continuation")


def _new_row():
    return RunRecord("new", 43, "agent/43", "pending", -1, datetime.now(UTC))


def _prepare_surface(store, surface, tmp_path):
    if surface == "continuation":
        store.record_continuation(
            "epic",
            "43",
            "previous",
            "agent/43",
            tmp_path,
            checkout_generation=fixture_checkout_generation(tmp_path),
        )


def _admit(store, surface, tmp_path):
    if surface == "plain":
        return store.try_admit_run(_new_row(), 1)
    if surface == "identity":
        identity = store.build_admission_identity(
            "new", tracker="github", item_id="43", repo="example/repo"
        )
        return store.admit_run_with_identity(_new_row(), identity, cap=1)
    if surface == "reservation":
        return (
            store.reserve_epic_node_item_under_cap("epic", "43", "new", 1) == "reserved"
        )
    return (
        store.claim_continuation_under_cap(
            "epic",
            "43",
            "previous",
            "agent/43",
            tmp_path,
            "new",
            1,
            claim_evidence=fixture_claim_evidence(
                store, "epic", "43", "previous", "agent/43", tmp_path, "new"
            ),
        )
        == "claimed"
    )


def _assert_reaped(store, row):
    saved = store.get(row.run_id)
    assert (saved.status, saved.exit_code, saved.error_category) == (
        "killed",
        137,
        "orphaned_pending_reaped",
    )
    assert all(res.run_id != row.run_id for res in store.list_epic_reservations("epic"))
    events = [event.event for event in store.list_audit_events(run_id=row.run_id)]
    assert "orphaned_attempt_terminated" in events
    assert "orphaned_run_reaped" in events
    assert events.index("orphaned_run_reaped") < events.index(
        "orphaned_attempt_terminated"
    )


def _fast_termination(monkeypatch):
    module = sys.modules.get("kew.process_termination")
    if module is not None:
        monkeypatch.setattr(module, "_GRACE_SECONDS", 0)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("resume_before_exit", [False, True])
def test_round16_admission_preserves_standalone_stop_audit(
    process_table, monkeypatch, tmp_path, surface, resume_before_exit
):
    # An unrelated owner command cannot authorize fleet to signal this owner.
    process_table.members[OWNER] = (OWNER, "S", "sleep", "sleep 60")
    process_table.members[ATTEMPT] = (ATTEMPT, "S", "sleep", "sleep 60")
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", Mock())
    with SqliteRunStore(":memory:") as store:
        row = _record(OWNER, ATTEMPT)
        store.save(row)
        _prepare_surface(store, surface, tmp_path)
        del process_table.members[ATTEMPT]
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert len(report.retained) == 1
        assert not process_table.signals
        if resume_before_exit:
            assert resume_fleet(store, DispatchConfig())
            assert store.get(row.run_id).status == "pending"
            assert not _admit(store, surface, tmp_path), (
                "live owner must retain capacity"
            )
        del process_table.members[OWNER]
        if not resume_before_exit:
            assert resume_fleet(store, DispatchConfig())
            assert store.get(row.run_id).status == "killed"
        assert _admit(store, surface, tmp_path), "dead stop must release capacity"
        assert store.get(row.run_id).status == "killed"
        events = store.list_audit_events(
            event="host_stop_reconciled", run_id=row.run_id
        )
        assert len(events) == 1 and events[0].payload["reason"]
        assert store.reconcile_host_stops() == 0
        assert (
            len(
                store.list_audit_events(event="host_stop_reconciled", run_id=row.run_id)
            )
            == 1
        )


def test_round16_absent_owner_does_not_inherit_group(process_table):
    row = _row()
    process_table.members[ATTEMPT] = (OWNER, "S", "sleep", "sleep 60")
    assert tick._owner_alive(row) is False, "complete owner absence must prove death"
    assert [member.pid for member in process_groups.group_members(OWNER)] == [ATTEMPT]
    row.attempt_pgid = None
    assert stop_recovery.pending_owner_is_dead(row, datetime.now(UTC))


@pytest.mark.parametrize("surface", SURFACES)
def test_round16_dead_owner_terminates_then_admits(
    process_table, monkeypatch, tmp_path, surface
):
    _tokens(monkeypatch, process_table)
    _fast_termination(monkeypatch)
    row = _row()
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        assert store.reserve_epic_node_item("epic", "42", row.run_id)
        _prepare_surface(store, surface, tmp_path)
        assert _admit(store, surface, tmp_path), "authenticated orphan must free cap 1"
        assert (ATTEMPT, signal.SIGTERM) in process_table.signals
        assert (ATTEMPT, signal.SIGKILL) in process_table.signals
        _assert_reaped(store, row)


@pytest.mark.parametrize("surface", SURFACES)
def test_round16_mismatched_attempt_stays_counted(
    process_table, monkeypatch, tmp_path, caplog, surface
):
    tokens = _tokens(monkeypatch, process_table)
    tokens[ATTEMPT] = "reused-kernel-token"
    with SqliteRunStore(":memory:") as store:
        row = _row()
        store.save(row)
        assert store.reserve_epic_node_item("epic", "42", row.run_id)
        _prepare_surface(store, surface, tmp_path)
        assert not _admit(store, surface, tmp_path)
        assert not process_table.signals
        assert store.get(row.run_id).status == "pending"
        assert store.live_run_count() == 1
        assert "attempt_identity_unverifiable" in caplog.text, (
            "log unauthenticated survivors"
        )
        assert store.list_audit_events(event="orphaned_attempt_retained")


class _SnapshotCounter:
    def __init__(self, store, result):
        self.store = store
        self.result = result
        self.calls = 0

    def __call__(self):
        assert not self.store._conn.in_transaction, "owner probes must precede BEGIN"
        self.calls += 1
        return self.result


@pytest.mark.parametrize("surface", SURFACES)
def test_round16_unknown_snapshot_counts_once(monkeypatch, tmp_path, surface):
    with SqliteRunStore(":memory:") as store:
        store.save(_row())
        store.save(replace(_row(), run_id="second", issue_num=44))
        _prepare_surface(store, surface, tmp_path)
        probe = _SnapshotCounter(store, None)
        monkeypatch.setattr(process_groups, "_identity_snapshot", probe)
        assert not _admit(store, surface, tmp_path)
        assert len(store.list_pending()) == 2
        assert probe.calls == 1, (
            "one failed owner snapshot must cover every pending row"
        )


@pytest.mark.parametrize("surface", SURFACES)
def test_round16_dead_set_survives_deferred_finalization(
    process_table, monkeypatch, tmp_path, surface
):
    row = _row()
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        assert store.reserve_epic_node_item("epic", "42", row.run_id)
        _prepare_surface(store, surface, tmp_path)
        finalize = Mock(return_value=False)
        monkeypatch.setattr(store, "finalize_orphaned_pending", finalize)
        probe = _SnapshotCounter(store, {})
        monkeypatch.setattr(process_groups, "_identity_snapshot", probe)
        assert _admit(store, surface, tmp_path), (
            "SQL must exclude only the carried dead generation"
        )
        assert finalize.call_count == 1
        assert probe.calls == 1
        assert store.get(row.run_id).status == "pending"
        assert store.list_audit_events(event="orphaned_run_reaped") == []


class _FinalizationRace:
    def __init__(self, other, replacement):
        self.other = other
        self.replacement = replacement
        self.calls = 0

    def __call__(self, run_id, **kwargs):
        self.calls += 1
        row = self.other.get(run_id)
        if self.replacement == "owner":
            row.owner_start_token = "new-generation"
            self.other.save(row, update_ownership=True)
        else:
            self.other.release_epic_node_item("42", run_id=run_id)
            self.other.reserve_epic_node_item("epic", "42", "new-reservation")
        return False


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("replacement", ["owner", "reservation"])
def test_round16_dead_set_preserves_concurrent_generation(
    process_table, monkeypatch, tmp_path, surface, replacement
):
    _tokens(monkeypatch, process_table)
    _fast_termination(monkeypatch)
    path = str(tmp_path / "race.db")
    with SqliteRunStore(path) as store, SqliteRunStore(path) as other:
        row = _row()
        store.save(row)
        assert store.reserve_epic_node_item("epic", "42", row.run_id)
        _prepare_surface(store, surface, tmp_path)
        race = _FinalizationRace(other, replacement)
        monkeypatch.setattr(store, "finalize_orphaned_pending", race)
        assert not _admit(store, surface, tmp_path)
        assert race.calls == 1, (
            "every admission must carry a generation-bound death proof"
        )
        assert store.get(row.run_id).status == "pending"
        assert store.list_epic_reservations("epic")


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("holder", ["live", "unknown", "reservation"])
@pytest.mark.parametrize("same_item", [False, True])
def test_round16_other_capacity_survives_reap(
    process_table, monkeypatch, tmp_path, surface, holder, same_item
):
    _tokens(monkeypatch, process_table)
    _fast_termination(monkeypatch)
    row = _row()
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        assert store.reserve_epic_node_item("epic", "42", row.run_id)
        if holder == "reservation":
            if same_item:
                store.release_epic_node_item("42", run_id=row.run_id)
            store.reserve_epic_node_item(
                "epic", "42" if same_item else "44", "unstarted"
            )
        else:
            live = replace(
                row,
                run_id="other",
                issue_num=42 if same_item else 44,
                pgid=OWNER + 10,
                owner_pid=OWNER + 10,
            )
            if holder == "live":
                process_table.members[live.pgid] = (
                    live.pgid,
                    "S",
                    "python3",
                    "python3 -m kew",
                )
                process_table.parents[live.pgid] = 44
                process_table.tokens[live.pgid] = live.owner_start_token
                # _tokens uses its own mapping; retain liveness with the row's token.
                monkeypatch.setattr(
                    process_groups, "process_start_token", partial(_mixed_token, live)
                )
            else:
                live.owner_start_time = live.owner_start_token = None
            store.save(live)
        _prepare_surface(store, surface, tmp_path)
        assert not _admit(store, surface, tmp_path)
        assert store.get(row.run_id).status == "killed", (
            "reap dead rows without dropping other claims"
        )
        assert store.live_run_count() == 1


def _mixed_token(row, pid):
    if pid == row.pgid:
        return row.owner_start_token
    return {OWNER: "100.000001", ATTEMPT: "100.000002"}.get(pid)


_EXITING_OWNER = """
import json, os, subprocess
from kew.process_groups import identity_at_admission
child = subprocess.Popen(['sleep', '60'], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
payload = {'child': child.pid, 'identity': identity_at_admission(os.getpid())}
print(json.dumps(payload), flush=True)
"""


@pytest.mark.serial
def test_round16_real_exited_owner_with_same_group_child():
    with subprocess.Popen(
        [sys.executable, "-c", _EXITING_OWNER],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    ) as owner:
        payload = json.loads(owner.stdout.readline())
        owner.wait(timeout=10)
    try:
        identity = payload["identity"]
        assert identity, "real identity requires host ps access"
        assert process_groups.owner_alive(owner.pid, owner.pid, **identity) is False, (
            "an exited owner must stay dead while its same-group child sleeps"
        )
        assert payload["child"] in [
            m.pid for m in process_groups.group_members(owner.pid)
        ]
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(owner.pid, signal.SIGKILL)


_GATED_OWNER = """
import json, os, subprocess, sys, time
from kew.process_groups import identity_at_admission
fd = int(sys.argv[1])
child = subprocess.Popen(
    [sys.executable, '-c',
     'import os,sys,time; os.read(int(sys.argv[1]),1); time.sleep(60)', str(fd)],
    pass_fds=(fd,), start_new_session=True,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
payload = {'child': child.pid, 'identity': identity_at_admission(os.getpid())}
print(json.dumps(payload), flush=True)
time.sleep(60)
"""


def _stop_real(owner, child):
    for pid in (owner.pid, child):
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
    owner.wait(timeout=5)
    owner.stdout.close()


@pytest.mark.serial
@pytest.mark.parametrize("surface", SURFACES)
def test_round16_real_killed_owner_releases_capacity(tmp_path, surface):
    read_fd, write_fd = os.pipe()
    owner = subprocess.Popen(
        [sys.executable, "-c", _GATED_OWNER, str(read_fd)],
        pass_fds=(read_fd,),
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    os.close(read_fd)
    child = None
    try:
        payload = json.loads(owner.stdout.readline())
        child = payload["child"]
        assert payload["identity"], "real admission requires host ps access"
        row = RunRecord(
            "orphan",
            42,
            "agent/42",
            "pending",
            -1,
            datetime.now(UTC),
            pgid=owner.pid,
            owner_pid=owner.pid,
            **payload["identity"],
        )
        with SqliteRunStore(":memory:") as store:
            assert store.try_admit_run(row, 1)
            store.set_attempt_pgid(row.run_id, child)
            row = store.get(row.run_id)
            assert row.attempt_owner_verified
            assert store.reserve_epic_node_item("epic", "42", row.run_id)
            _prepare_surface(store, surface, tmp_path)
            owner.kill()
            owner.wait(timeout=5)
            assert process_groups.group_alive(child) is True
            assert _admit(store, surface, tmp_path), (
                "dead owner must free cap 1 after authenticated termination"
            )
            assert process_groups.group_alive(child) is False
            _assert_reaped(store, row)
    finally:
        os.close(write_fd)
        _stop_real(owner, child)
