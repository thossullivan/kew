"""Round-22 owner and lost-response regressions against 2889ec8b."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_round21_admission_owner import EPIC, ITEM, RetryWatcher, _admit_host, _config

from kew import process_groups, subprocess_utils
from kew.claim_retention import retained_claim_lines
from kew.cli import delegate as delegate_cli
from kew.cli.epic import _LinearDispatchClaim
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import WatchOutcome, process_candidate, run_watch_loop
from kew.delegate.watch import Candidate
from kew.graph.driver import dispatch, tick
from kew.linear.client import LinearApiError, LinearClient
from kew.linear.tracker import LinearTracker
from kew.run_store import RunRecord, SqliteRunStore


class CapturedChildren:
    def __init__(self):
        self.children = []
        self.store = None

    def __call__(self, *args, **kwargs):
        child = subprocess_utils.popen(*args, **kwargs)
        self.children.append(child)
        return child

    def close(self):
        for child in self.children:
            child.wait(timeout=10)
        if self.store is not None:
            self.store.close()
            self.store = None


def _exit_script(tmp_path, monkeypatch, *, db_path=None, store=None):
    script = tmp_path / "kew-exit"
    body = ""
    if db_path is not None:
        body = (
            "from test_round21_admission_owner import _admit_host, _config\n"
            "from kew.run_store import SqliteRunStore\n"
            "from pathlib import Path\n"
            f"with SqliteRunStore(Path({str(db_path)!r})) as store:\n"
            f"    _admit_host(store, _config(Path({str(tmp_path)!r})), 'child')\n"
        )
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o700)
    monkeypatch.setattr(dispatch, "resolve_kew_console_script", partial(str, script))
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "ps", "/bin/ps")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [
                str(Path(__file__).parent),
                str(Path(subprocess_utils.__file__).parents[1]),
            ]
        ),
    )
    captured = CapturedChildren()
    if store is None:
        # The sink mints this child's bounded run key before Popen, so it needs
        # a ledger of its own when the caller does not supply one.
        store = SqliteRunStore(tmp_path / "sink.db")
        captured.store = store
    sink = dispatch.DetachedDispatchSink(
        epic_ref=EPIC,
        config=_config(tmp_path),
        store=store,
        popen=captured,
        publication_token=partial(type(None)),
    )
    return sink, captured


def _ps(*args):
    try:
        return subprocess_utils.run(["ps", *args], check=False, timeout=5)
    except (OSError, ValueError) as exc:
        pytest.fail(f"Real ps probe blocked: {exc}")


def _wait_zombie(pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = _ps("-o", "stat=", "-p", str(pid))
        assert result.returncode == 0, f"Real ps probe failed: {result.stderr}"
        if result.stdout.strip().startswith("Z"):
            return
        time.sleep(0.01)
    pytest.fail("The unreaped dispatch child never became a zombie")


@pytest.mark.serial
def test_round22_zombie_reconciles_and_one_worker_watcher_redispatches(
    tmp_path, monkeypatch
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        sink, captured = _exit_script(
            tmp_path, monkeypatch, db_path=store._db_path, store=store
        )
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation")
        sink.dispatch(
            ITEM, reservation_run_id="reservation", reservation_generation="reservation"
        )
        try:
            # No wait(), poll(), or communicate() before production reconciliation.
            _wait_zombie(captured.children[0].pid)
            assert store.get("child").status == "pending"
            tick.reconcile_epic_reservations(
                EPIC, store, _config(tmp_path), now=datetime.now(UTC)
            )
            assert store.get("child").status == "killed", (
                "A zombie owner must release the pending row"
            )
            assert store.list_epic_reservations(EPIC) == []
            assert store.live_run_count() == 0
            config_path = tmp_path / "config.toml"
            config_path.write_text("")
            watcher = RetryWatcher(store, _config(tmp_path), config_path)
            assert watcher.tick() is WatchOutcome.DISPATCHED
            assert watcher.dispatched == [ITEM]
            assert store.live_run_count() == 1
        finally:
            captured.close()


def _stop_loop(_interval):
    raise KeyboardInterrupt


@pytest.mark.serial
@pytest.mark.parametrize("surface", ["driver", "watcher"])
def test_round22_tick_reaps_all_detached_handles(tmp_path, monkeypatch, surface):
    sink, captured = _exit_script(tmp_path, monkeypatch)
    for index in range(4):
        sink.dispatch(str(index))
    try:
        for child in captured.children:
            _wait_zombie(child.pid)
        _one_tick(surface, sink, tmp_path, monkeypatch)
        assert all(child.returncode == 0 for child in captured.children), (
            "The tick must poll every retained child"
        )
        assert sink._children == []
        result = _ps("-axo", "ppid=,stat=")
        assert result.returncode == 0
        assert not any(
            fields[0] == str(os.getpid()) and fields[1].startswith("Z")
            for line in result.stdout.splitlines()
            if len(fields := line.split()) == 2
        ), "No zombie child may remain after the tick"
    finally:
        captured.close()


def _one_tick(surface, sink, tmp_path, monkeypatch):
    if surface == "watcher":
        with SqliteRunStore(tmp_path / "watch.db") as store:
            context = SimpleNamespace(sink=sink, tracker=Mock())
            with pytest.raises(KeyboardInterrupt):
                extra = {}
                if hasattr(dispatch, "reap_dispatch_children"):
                    extra["reap"] = partial(dispatch.reap_dispatch_children, sink)
                run_watch_loop(
                    watcher=SimpleNamespace(tick=partial(list)),
                    process_one=Mock(),
                    sleep=_stop_loop,
                    fast_interval_seconds=0,
                    reconcile=partial(
                        delegate_cli._reconcile_watch,
                        store,
                        _config(tmp_path),
                        context,
                        # The watcher owns the driver-key sweep's seen-set.
                        set(),
                    ),
                    **extra,
                )
        return
    monkeypatch.setattr(tick, "preflight_tools", partial(type(None)))
    monkeypatch.setattr(
        tick, "build_graph", Mock(return_value=SimpleNamespace(nodes={}))
    )
    with pytest.raises(tick.EmptyEpicGraphError):
        tick.advance(
            EPIC,
            config=_config(tmp_path),
            store=Mock(),
            deps=Mock(),
            facts=Mock(),
            dispatch_sink=sink,
            max_parallel=1,
            now=datetime.now(UTC),
            lock_path=tmp_path / "lock",
            approvers=[],
            repo="test/repo",
        )


def _row():
    return RunRecord(
        run_id="owner",
        issue_num=0,
        branch="round22",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        pgid=os.getpid(),
        owner_pid=os.getpid(),
        owner_start_time="Sun Sep 20 01:00:00 2026",
        owner_start_token="recorded-birth",
    )


def test_round22_reused_pid_is_dead(monkeypatch):
    row = _row()
    monkeypatch.setattr(
        process_groups, "process_start_token", Mock(return_value="new-birth")
    )
    entry = process_groups.ProcessIdentity(
        1, row.pgid, "S", row.owner_start_time, "python3"
    )
    monkeypatch.setattr(
        process_groups,
        "owner_snapshot",
        Mock(return_value=process_groups.OwnerSnapshot({row.pgid: entry})),
    )
    assert tick.default_run_liveness(_config(Path.cwd()))(row) is False, (
        "A reused live PID must not retain its predecessor's run"
    )


@pytest.mark.parametrize(
    "state,code,stderr", [("Z", 0, ""), ("", 1, ""), ("", 1, "ps failed")]
)
def test_round22_nonchild_zombie_gone_and_unknown(monkeypatch, state, code, stderr):
    pid = os.getpid()
    # A successful complete snapshot can prove absence; an unavailable scan cannot.
    output = (
        f"{pid} 1 {pid} {state} Sun Sep 20 01:00:00 2026 python3\n"
        if state
        else "2 1 2 S Sun Sep 20 01:00:00 2026 other\n"
    )
    monkeypatch.setattr(
        process_groups,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 1 if stderr else 0, output, stderr
            )
        ),
    )
    expected = None if stderr else False
    assert tick.default_run_liveness(_config(Path.cwd()))(_row()) is expected, (
        "kill(pid, 0) cannot establish owner identity"
    )


def test_round22_owner_start_time_is_chained_and_projected(tmp_path, monkeypatch):
    stamp = "Sun Sep 20 02:00:00 2026"
    monkeypatch.setattr(process_groups, "exclusive_owner_pid", os.getpid)
    entry = process_groups.ProcessIdentity(
        os.getppid(), os.getpid(), "S", stamp, "python3", "admitted-birth"
    )
    monkeypatch.setattr(process_groups, "process_identity", Mock(return_value=entry))
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation")
        _admit_host(store, _config(tmp_path), "owner")
        [event] = store.list_audit_events(event="dispatch_owner_recorded")
        assert event.payload.get("owner_start_time") == stamp, (
            "Admission must record ps lstart in its chained event"
        )
        owner = store.get("owner")
        assert owner.owner_start_time == stamp
        for field in ("owner_start_token", "owner_ppid", "owner_command"):
            assert event.payload[field] == getattr(owner, field)
        [row], _ = store.list_runs_by_branch_after("round21")
        assert row.owner_start_time == stamp


class LostResponseTransport:
    def __init__(self, verification):
        self.verification = verification
        self.mutations = 0
        self.issue = {"assignee": None, "state": {"id": "ready", "name": "Todo"}}

    def __call__(self, _url, payload, _headers):
        request = json.loads(payload)
        query = request["query"]
        if "IssueUpdate" in query:
            self.mutations += 1
            fields = request["variables"]["input"]
            self.issue = {
                "identifier": ITEM,
                "assignee": {"id": fields["assigneeId"]},
                "state": {"id": fields["stateId"], "name": "In Progress"},
                "updatedAt": f"version-{self.mutations}",
            }
            raise ConnectionResetError("response lost after remote commit")
        if self.verification == "unavailable":
            raise ConnectionResetError("verification unavailable")
        issue = deepcopy(self.issue)
        if self.verification == "not_applied":
            issue = {"assignee": None, "state": {"id": "ready"}}
        return 200, json.dumps({"data": {"issue": issue}}).encode()

    def fetch_live(self, _item):
        return CandidateIssue(
            assignee_id="viewer",
            labels=("kew:ready",),
            state="In Progress",
            state_id="In Progress",
        )


def _claim_scene(tmp_path, store, transport):
    client = LinearClient(
        "round22-key", transport=transport, max_retries=3, sleep=Mock()
    )
    tracker = LinearTracker(_config(tmp_path), client)
    tracker._viewer_id = "viewer"
    tracker._states = [{"id": "started", "name": "In Progress", "type": "started"}]
    assert store.reserve_epic_node_item(EPIC, ITEM, "reservation")
    _admit_host(store, _config(tmp_path), "ambiguous")
    return _LinearDispatchClaim(tracker, store, ITEM, EPIC), client


@pytest.mark.parametrize("verification", ["applied", "unavailable", "not_applied"])
def test_round22_lost_mutation_responses_are_retained(
    admission_owner, tmp_path, monkeypatch, caplog, verification
):
    warnings = []
    monkeypatch.setattr("kew.cli.epic._warn", warnings.append)
    transport = LostResponseTransport(verification)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, transport)
        with pytest.raises((LinearApiError, ConnectionResetError)):
            claim.admit("ambiguous")
        events = store.list_audit_events(event="linear_claim_outcome_unknown")
        assert len(events) == 1, (
            "A sent mutation without a response must record an unknown outcome"
        )
        assert "response lost" in events[0].payload["error"]
        assert claim.outcome_unknown and client.mutation_outcome_ambiguous
        assert transport.mutations == 3
        claim.retain("hook failed")
        completed = store.list_audit_events(event="linear_claim_completed")
        if verification == "applied":
            assert completed[0].payload["source"] == "verification"
            assert completed[0].payload["assigneeId"] == "viewer"
            assert completed[0].payload["stateId"] == "started"
            assert not claim.outcome_unknown
        else:
            assert completed == []
        [retained] = store.list_audit_events(event="linear_claim_retained")
        reason = "hook failed" if verification == "applied" else "claim outcome unknown"
        assert retained.payload["reason"] == reason
        assert retained.payload["reservation_run_id"] == "reservation"
        output = "\n".join(warnings)
        assert ITEM in output and reason in output
        assert "manual recovery" in output
        assert reason in retained_claim_lines(store, [ITEM])[0]
        store.release_epic_node_item(ITEM, run_id="reservation")
        outcome = process_candidate(
            Candidate(ITEM, "poll"),
            member_id="viewer",
            label="kew:ready",
            store=store,
            tracker=transport,
            sink=Mock(),
            fleet_cap=1,
            dry_run=False,
        )
        assert outcome.value == "retained_claim", (
            "Recognition must surface retention instead of rejecting the started item"
        )
        assert "manual recovery" in caplog.text
        claim.retain("again")
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1


def test_round22_retention_recognition_surfaces_existing_evidence(tmp_path, caplog):
    from kew.claim_retention import record_retained_claim

    with SqliteRunStore(tmp_path / "runs.db") as store:
        record_retained_claim(store, ITEM, reason="claim outcome unknown")
        outcome = process_candidate(
            Candidate(ITEM, "poll"),
            member_id="viewer",
            label="kew:ready",
            store=store,
            tracker=LostResponseTransport("applied"),
            sink=Mock(),
            fleet_cap=1,
            dry_run=False,
        )
        assert outcome.value == "retained_claim", (
            "Retained started items need a distinct recognition result"
        )
        assert "manual recovery" in caplog.text


@pytest.mark.serial
def test_round22_real_live_pid_start_mismatch_is_dead(tmp_path, monkeypatch):
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "ps", "/bin/ps")
    result = _ps("-o", "lstart=", "-p", str(os.getpid()))
    assert result.returncode == 0 and result.stdout.strip()
    row = _row()
    row.owner_start_time = result.stdout.strip()
    current = process_groups.process_start_token(row.owner_pid)
    assert current is not None
    row.owner_start_token = current + "-predecessor"
    assert tick.default_run_liveness(_config(tmp_path))(row) is False, (
        "A live process with another kernel birth cannot own the recorded run"
    )


@pytest.mark.parametrize("surface", ["driver", "watcher"])
def test_round22_tick_polls_registered_children(tmp_path, monkeypatch, surface):
    monkeypatch.setattr(
        dispatch, "resolve_kew_console_script", partial(str, "/unused/kew")
    )
    child = Mock(pid=123, poll=Mock(return_value=0))
    with SqliteRunStore(tmp_path / "sink.db") as store:
        sink = dispatch.DetachedDispatchSink(
            epic_ref=EPIC,
            config=_config(tmp_path),
            store=store,
            popen=Mock(return_value=child),
            publication_token=partial(type(None)),
        )
        sink.dispatch(ITEM)
    _one_tick(surface, sink, tmp_path, monkeypatch)
    assert child.poll.call_count == 1, (
        "Every production tick must reap the sink's finished children"
    )
    assert sink._children == []


def test_round22_waitpid_collects_owned_zombie(monkeypatch, tmp_path):
    sink, captured = _exit_script(tmp_path, monkeypatch)
    ps = Mock(side_effect=AssertionError("Child reaping must use waitpid"))
    monkeypatch.setattr(process_groups, "run", ps)
    sink.dispatch(ITEM)
    child = captured.children[0]
    try:
        deadline = time.monotonic() + 10
        while sink._children and time.monotonic() < deadline:
            dispatch.reap_dispatch_children(sink)
            time.sleep(0.01)
        assert child.returncode == 0
        assert sink._children == []
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        ps.assert_not_called()
        row = _row()
        row.pgid = row.owner_pid = child.pid
        monkeypatch.setattr(
            process_groups,
            "owner_snapshot",
            Mock(return_value=process_groups.OwnerSnapshot({})),
        )
        assert tick.default_run_liveness(_config(tmp_path))(row) is False
    finally:
        captured.close()
