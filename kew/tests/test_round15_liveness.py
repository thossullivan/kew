"""Round-15 host starts require the identities used by fleet termination."""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from functools import partial
from unittest.mock import Mock

import pytest
from test_dispatch_core import _profile_run
from test_liveness_owner import BIRTH
from test_liveness_owner import process_table as process_table
from test_round14_liveness import ATTEMPT, OWNER, _row, _tokens

from kew import dispatch_core, fleet, process_groups
from kew.graph.driver import tick
from kew.orchestrator import _announce_spawn
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS, popen

_REAL_TOKEN = process_groups.process_start_token
_OWNER_BIRTH = "owner-token"


class _Probes:
    """Control unavailable probes without replacing child creation or signals."""

    def __init__(self, owner_token=_OWNER_BIRTH, attempt_available=True):
        self.owner_token = owner_token
        self.attempt_available = attempt_available
        self.proc = None
        self.snapshots = 0

    def token(self, pid):
        if pid == OWNER:
            return self.owner_token
        return _REAL_TOKEN(pid) if self.attempt_available else None

    def birth(self, pid):
        return BIRTH

    def snapshot(self):
        self.snapshots += 1
        entries = {
            OWNER: process_groups.ProcessIdentity(44, OWNER, "S", BIRTH, "python3")
        }
        if self.proc is not None and self.proc.poll() is None:
            entries[self.proc.pid] = process_groups.ProcessIdentity(
                OWNER, self.proc.pid, "S", BIRTH, "sh"
            )
        return entries

    def group_alive(self, pid):
        return pid == OWNER or self.proc is not None and self.proc.poll() is None

    def install(self, monkeypatch):
        monkeypatch.setattr(
            process_groups, "exclusive_owner_pid", Mock(return_value=OWNER)
        )
        monkeypatch.setattr(process_groups, "_identity_snapshot", self.snapshot)
        for module in (process_groups, sys.modules["kew.run_store"]):
            monkeypatch.setattr(module, "process_start_token", self.token)
            monkeypatch.setattr(module, "process_start_time", self.birth)
        monkeypatch.setattr(
            "kew.run_store.attempt_descends_from", Mock(return_value=True)
        )
        monkeypatch.setattr(fleet, "group_alive", self.group_alive)
        monkeypatch.setitem(TOOL_PATHS, "sh", shutil.which("sh"))


def _start_child(probes, marker):
    probes.proc = popen(
        ["sh", "-c", 'echo executed > "$1"; exec sleep 60', "agent", str(marker)],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        gate=True,
    )
    return probes.proc


def _cleanup_child(proc):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _admit(store, tmp_path):
    run = _profile_run(store, tmp_path)
    dispatch_core._save_pending_run(
        run, worker_cap=1, cap_exclude_issue=None, admit_budget_check=None
    )
    return run


def _launch_refused_attempt(proc, events, run, *, on_spawn, **kwargs):
    _announce_spawn(proc, events, on_spawn)
    raise AssertionError("unavailable identity opened the gate")


def _run_refused_attempt(run, proc, events, tmp_path, monkeypatch):
    monkeypatch.setattr(
        dispatch_core,
        "_run_retry_attempt",
        partial(_launch_refused_attempt, proc, events),
    )
    return dispatch_core._run_retry_loop(
        run,
        prompt="probe",
        worktree_dir=tmp_path,
        budget_override_usd=None,
        timeout_override_minutes=None,
        should_pause=None,
        budget_check=None,
        anomaly_threshold_usd=None,
        anomaly_compaction_allowance_usd=0,
    )


def _refusal_event(events):
    return next(
        event
        for event in map(json.loads, events.getvalue().splitlines())
        if event["type"] == "spawn_registration_failed"
    )


@pytest.mark.serial
@pytest.mark.parametrize("missing_owner", [False, True])
def test_round15_missing_attempt_never_executes(
    tmp_path, monkeypatch, caplog, missing_owner
):
    probes = _Probes(attempt_available=False)
    probes.install(monkeypatch)
    marker = tmp_path / "executed"
    events = io.StringIO()
    with SqliteRunStore(":memory:") as store:
        run = _admit(store, tmp_path)
        if missing_owner:
            # A legacy row admitted before tokens became mandatory.
            probes.owner_token = None
            row = store.get(run.run_id)
            row.owner_start_token = None
            store.save(row, update_ownership=True)
        assert store.reserve_epic_node_item("epic", run.item_id, run.run_id)
        reason = (
            "owner-identity-unavailable"
            if missing_owner
            else "attempt-identity-unavailable"
        )
        proc = _start_child(probes, marker)
        try:
            store.set_attempt_pgid(run.run_id, proc.pid)
            row = store.get(run.run_id)
            assert not row.attempt_owner_verified, (
                "missing tokens cannot verify a host attempt"
            )
            assert row.attempt_start_token is None
            assert reason in caplog.text
            with pytest.raises(RuntimeError, match=reason):
                dispatch_core._check_spawn_permit(store, run.run_id)
            outcome = _run_refused_attempt(run, proc, events, tmp_path, monkeypatch)
            proc.wait(timeout=5)
            assert not marker.exists(), "the real child must never execute its marker"
            assert reason in _refusal_event(events)["reason"]
            assert outcome.early_return.exit_code == 2
            assert outcome.early_return.launched is False
            row = store.get(run.run_id)
            assert (row.status, row.exit_code, row.accounting_status) == (
                "refused",
                2,
                "not-run",
            )
            assert row.error_category == reason
            assert store.list_epic_reservations("epic") == []
            assert store.live_run_count() == 0
        finally:
            _cleanup_child(proc)


def test_round15_owner_token_required_at_admission(tmp_path, monkeypatch):
    probes = _Probes(owner_token=None)
    probes.install(monkeypatch)
    with SqliteRunStore(":memory:") as store:
        with pytest.raises(RuntimeError, match="owner-identity-unavailable") as refused:
            _admit(store, tmp_path)
        assert probes.snapshots == 3, "retain all three admission probes"
        assert sys.platform in str(refused.value)
        assert "kernel" in str(refused.value)
        assert store.list_pending() == []
        assert store.live_run_count() == 0

        run = _profile_run(store, tmp_path)
        run.config.logging.dir = str(tmp_path / "logs")
        assert store.reserve_epic_node_item("epic", run.item_id, run.run_id)
        result = dispatch_core.run_tracked_agent(
            prompt="probe",
            worktree_dir=tmp_path,
            config=run.config,
            issue_num=42,
            item_id=run.item_id,
            run_id=run.run_id,
            store=store,
            runner=run.runner,
            model=run.effective_model,
            branch=run.branch,
            validate_continuation=False,
        )
        assert result.exit_code == 2
        assert result.error_category == "owner-identity-unavailable"
        assert result.accounting_status == "not-run"
        assert store.get(run.run_id) is None
        assert store.list_epic_reservations("epic") == []


@pytest.mark.serial
def test_round15_tokens_execute_and_stop_then_refuse_loss(tmp_path, monkeypatch):
    probes = _Probes()
    probes.install(monkeypatch)
    marker = tmp_path / "executed"
    events = io.StringIO()
    with SqliteRunStore(":memory:") as store:
        run = _admit(store, tmp_path)
        proc = _start_child(probes, marker)
        try:
            _announce_spawn(
                proc,
                events,
                partial(dispatch_core._record_retry_pgid, store, run.run_id),
            )
            # The child must run: a late marker passes, a missing one fails.
            deadline = time.monotonic() + 60
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), "matching tokens must permit real execution"
            row = store.get(run.run_id)
            assert row.attempt_owner_verified
            assert process_groups.start_token_matches(proc.pid, row.attempt_start_token)
            assert fleet._live_agent_pgid(row) == (proc.pid, ""), (
                "fleet must find the signallable attempt"
            )
            # Recheck the durable flag at the final permit boundary.
            probes.attempt_available = False
            with pytest.raises(RuntimeError, match="attempt-identity-unavailable"):
                dispatch_core._check_spawn_permit(store, run.run_id)
            assert fleet._live_agent_pgid(row)[0] is None
            probes.attempt_available = True
            terminate = Mock(return_value=True)
            monkeypatch.setattr(fleet, "_terminate_group", terminate)
            monkeypatch.setattr(fleet, "_kill_survivors", Mock())
            fleet.stop_fleet(store, run.config, grace_seconds=0)
            assert terminate.call_args.args[0] == proc.pid
        finally:
            _cleanup_child(proc)


def test_round15_legacy_lstart_reaps_but_never_authorizes(monkeypatch, process_table):
    tokens = _tokens(monkeypatch, process_table)
    tokens[OWNER] = tokens[ATTEMPT] = None
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[OWNER] = 44
    process_table.parents[ATTEMPT] = OWNER
    row = _row()
    row.owner_start_token = row.attempt_start_token = None
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        assert tick._owner_alive(row) is True, "legacy lstart must retain live owners"
        assert store.reconcile_dead_pending() == 0
        assert fleet._live_agent_pgid(row)[0] is None
        with pytest.raises(RuntimeError, match="owner-identity-unavailable"):
            dispatch_core._check_spawn_permit(store, row.run_id)
        store.set_attempt_pgid(row.run_id, ATTEMPT)
        assert not store.get(row.run_id).attempt_owner_verified
        process_table.members.clear()
        assert tick._owner_alive(row) is False
        assert store.reconcile_dead_pending() == 1
        assert store.get(row.run_id).status != "pending"
        assert not process_table.signals


def test_round15_container_keeps_durable_identity(monkeypatch, process_table):
    from kew.stop_recovery import container_target

    tokens = _tokens(monkeypatch, process_table)
    tokens[OWNER] = tokens[ATTEMPT] = None
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[ATTEMPT] = OWNER
    row = _row()
    row.owner_start_token = row.attempt_start_token = None
    row.container_name = "kew-42"
    row.container_id = "a" * 64
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        store.set_attempt_pgid(row.run_id, ATTEMPT)
        saved = store.get(row.run_id)
        assert not saved.attempt_owner_verified, (
            "container IDs do not prove host identity"
        )
        dispatch_core._check_spawn_permit(store, row.run_id)
        assert container_target(saved) == row.container_id
        saved.container_id = None
        store.save(saved, update_ownership=True)
        with pytest.raises(RuntimeError, match="container identity unavailable"):
            dispatch_core._check_spawn_permit(store, row.run_id)
