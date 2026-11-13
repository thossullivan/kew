"""Tests for the fleet kill switch (kew stop / kew resume)."""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig
from kew.run_store import RunRecord, SqliteRunStore


def _spawn_group(seconds: str = "30") -> subprocess.Popen[bytes]:
    """Launch a real sleep process as its own process-group leader."""
    sleep_path = shutil.which("sleep")
    assert sleep_path is not None
    return subprocess.Popen([sleep_path, seconds], start_new_session=True)


def _spawn_owned_attempt(tmp_path, command):
    """Start a real kew entry point that owns and reaps an exclusive attempt."""
    script = tmp_path / "kew"
    script.write_text(
        "import subprocess, sys\n"
        "proc = subprocess.Popen(sys.argv[1:], start_new_session=True)\n"
        "print(proc.pid, flush=True)\n"
        "proc.wait()\n"
    )
    owner = subprocess.Popen(
        [sys.executable, str(script), *command],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert owner.stdout is not None
    return owner, int(owner.stdout.readline())


def _cleanup_owned_attempt(owner, attempt):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(attempt, signal.SIGKILL)
    if owner.poll() is None:
        owner.kill()
    owner.wait(timeout=5)
    owner.stdout.close()


def _group_dead(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        # The pgid now belongs to another user's process: ours is gone and
        # the id was reused. Seen under xdist plus CI churn on one host.
        return True


def _reap_async(proc: subprocess.Popen[bytes]) -> threading.Thread:
    """Reap the child in the background, like a kew worker's proc.wait().

    Without this the test process (the child's parent) leaves a zombie,
    which keeps the process group 'alive' for os.killpg existence checks.
    """
    t = threading.Thread(target=proc.wait, daemon=True)
    t.start()
    return t


@pytest.fixture
def ps_registered(monkeypatch):
    """Register ps in TOOL_PATHS like preflight_tools does."""
    from kew import subprocess_utils

    ps_path = shutil.which("ps")
    assert ps_path is not None
    try:
        probe = subprocess.run(
            [ps_path, "-p", str(os.getpid()), "-o", "pid="],
            capture_output=True,
            text=True,
            check=False,
        )
    except PermissionError:
        pytest.skip("Sandbox forbids live process inspection through ps")
    if probe.returncode and "Operation not permitted" in probe.stderr:
        pytest.skip("Sandbox forbids live process inspection through ps")
    assert probe.returncode == 0, probe.stderr
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "ps", ps_path)


class TestPgidCommand:
    def test_returns_command_name_for_live_group(self, ps_registered):
        from kew.fleet import _pgid_command

        proc = _spawn_group()
        try:
            comm = _pgid_command(proc.pid)
            assert comm == "sleep"
        finally:
            proc.kill()
            proc.wait()

    def test_returns_none_for_dead_group(self, ps_registered):
        from kew.fleet import _pgid_command

        proc = _spawn_group("0.05")
        proc.wait()
        # Reap completed; the pid no longer exists as a live process
        assert _pgid_command(proc.pid) is None


class TestSignalAndGrace:
    def test_terminate_groups_term_honored(self, ps_registered):
        from kew.fleet import _group_alive, _signal_group

        proc = _spawn_group()
        assert _group_alive(proc.pid)
        _signal_group(proc.pid, signal.SIGTERM)
        proc.wait()
        assert not _group_alive(proc.pid)

    def test_signal_dead_group_does_not_raise(self):
        from kew.fleet import _signal_group

        proc = _spawn_group("0.05")
        proc.wait()
        _signal_group(proc.pid, signal.SIGTERM)  # must not raise


class TestPgidFromEvents:
    def test_reads_pid_event_from_events_file(self, tmp_path):
        from kew.fleet import _pgid_from_events

        log_file = tmp_path / "dispatch-42-x.log"
        events = tmp_path / "dispatch-42-x.events.jsonl"
        events.write_text(
            json.dumps({"ts": 1.0, "type": "started"})
            + "\n"
            + json.dumps({"ts": 2.0, "type": "pid", "pid": 777})
            + "\n"
        )
        record = RunRecord(
            run_id="r",
            issue_num=42,
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            log_file=log_file,
        )
        assert _pgid_from_events(record) == [777]

    def test_returns_none_without_log_file(self):
        from kew.fleet import _pgid_from_events

        record = RunRecord(
            run_id="r",
            issue_num=42,
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
        )
        assert _pgid_from_events(record) == []

    def test_returns_none_when_events_file_missing(self, tmp_path):
        from kew.fleet import _pgid_from_events

        record = RunRecord(
            run_id="r",
            issue_num=42,
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            log_file=tmp_path / "nope.log",
        )
        assert _pgid_from_events(record) == []


class TestCostFromEvents:
    def test_latest_tokens_event_wins(self, tmp_path):
        from kew.fleet import _cost_from_events

        log_file = tmp_path / "dispatch-42-x.log"
        events = tmp_path / "dispatch-42-x.events.jsonl"
        events.write_text(
            json.dumps({"ts": 1.0, "type": "tokens", "cost_usd": 0.10})
            + "\n"
            + json.dumps({"ts": 2.0, "type": "tokens", "cost_usd": 0.42})
            + "\n"
        )
        record = RunRecord(
            run_id="r",
            issue_num=42,
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            log_file=log_file,
        )
        assert _cost_from_events(record) == pytest.approx(0.42)

    def test_falls_back_to_record_cost(self, tmp_path):
        from kew.fleet import _cost_from_events

        record = RunRecord(
            run_id="r",
            issue_num=42,
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            estimated_cost_usd=1.23,
            log_file=tmp_path / "missing.log",
        )
        assert _cost_from_events(record) == pytest.approx(1.23)


def _make_config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "owner/repo"
    config.logging.dir = str(tmp_path / "logs")
    return config


def _pending_record(
    issue_num: int,
    pgid: int | None,
    log_file: Path | None = None,
    *,
    owner_pid: int = 2**31 - 1,
) -> RunRecord:
    from kew.process_groups import (
        identity_at_admission,
        process_start_time,
        process_start_token,
    )

    identity = identity_at_admission(owner_pid)
    if owner_pid == 2**31 - 1:
        identity = {"owner_start_time": "Tue Sep 15 10:00:00 2026"}
    return RunRecord(
        run_id=f"run-{issue_num}",
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        attempt_pgid=pgid,
        pgid=owner_pid,
        owner_pid=owner_pid,
        attempt_start_time=process_start_time(pgid) if pgid else None,
        attempt_start_token=process_start_token(pgid) if pgid else None,
        **identity,
        log_file=log_file,
    )


def _fleet_audit_events(store) -> list:
    """All fleet_* audit events, oldest first."""
    events = [
        e
        for e in store.list_audit_events(limit=None)
        if e.event in ("fleet_stop", "fleet_resume")
    ]
    return list(reversed(events))


class TestStopFleet:
    @pytest.mark.parametrize("attempt", [12345, None, 2**31 - 1])
    def test_kill_prefers_live_attempt_then_owner(self, tmp_path, monkeypatch, attempt):
        from kew.fleet import stop_fleet

        record = _pending_record(42, attempt)
        record.pgid = record.owner_pid = 54321
        record.attempt_start_time = record.owner_start_time = "birth"
        from kew.process_groups import ProcessIdentity

        monkeypatch.setattr("kew.fleet.identity_matches", lambda *a: True)
        monkeypatch.setattr("kew.fleet.retained_identity_matches", lambda *a: True)
        monkeypatch.setattr(
            "kew.fleet.process_identity",
            lambda pid: ProcessIdentity(1, pid, "S", "birth", "python3", "token"),
        )
        monkeypatch.setattr("kew.fleet.start_token_matches", lambda *a: True)
        monkeypatch.setattr(
            "kew.fleet.group_member_identities",
            lambda pgid: {
                pgid: ProcessIdentity(1, pgid, "S", "birth", "python3", "token").token()
            },
        )
        monkeypatch.setattr("kew.fleet.recovered_attempt_signallable", lambda *a: True)
        monkeypatch.setattr("kew.fleet.group_alive", lambda pgid: pgid != 2**31 - 1)
        monkeypatch.setattr(
            "kew.fleet.owner_signallable",
            lambda pgid, owner, *a: pgid == owner == 54321,
        )
        signal_group = MagicMock()
        monkeypatch.setattr("kew.fleet._signal_group", signal_group)
        monkeypatch.setattr("kew.fleet._kill_survivors", lambda *args: None)
        monkeypatch.setattr(
            "kew.fleet.reset_issue_to_ready", lambda *args, **kwargs: None
        )
        with SqliteRunStore(str(tmp_path / "kill.db")) as store:
            store.save(record)
            stop_fleet(store, _make_config(tmp_path), reason="operator")
            signal_group.assert_called_once_with(
                12345 if attempt == 12345 else 54321, signal.SIGTERM
            )

    # Real child process; a reused pgid under process churn reads as alive
    # (kew.fleet._group_alive). Serial phase: tests/conftest.py.
    @pytest.mark.serial
    @patch("kew.fleet.reset_issue_to_ready")
    def test_kills_live_run_and_latches(self, mock_reset, tmp_path, ps_registered):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        proc, attempt = _spawn_owned_attempt(tmp_path, ["sleep", "30"])
        reaper = _reap_async(proc)
        try:
            with SqliteRunStore(str(tmp_path / "t.db")) as store:
                store.save(_pending_record(42, attempt, owner_pid=proc.pid))

                report = stop_fleet(store, config, reason="runaway", grace_seconds=5.0)
                assert report.already_stopped is False
                if report.retained:
                    reaper.join(timeout=5)
                    report = stop_fleet(store, config, grace_seconds=0)

                # Latch engaged
                latch = store.get_fleet_stopped()
                assert latch is not None
                assert latch["reason"] == "runaway"
                # Run marked killed
                record = store.get("run-42")
                assert record is not None
                assert record.status == "killed"
                assert record.error_category == "killed"
                assert record.finished_at is not None
                # Process group is gone (TERM honored within grace)
                reaper.join(timeout=10)
                assert _group_dead(attempt)
                # Labels reset
                mock_reset.assert_called_once_with(42, config)
                # Report
                assert [k.issue_num for k in report.killed] == [42]
                # Audit trail event
                events = _fleet_audit_events(store)
                assert events[-1].event == "fleet_stop"
                assert events[-1].payload["reason"] == "runaway"
                assert events[-1].payload["runs_killed"][0]["issue"] == 42
        finally:
            _cleanup_owned_attempt(proc, attempt)

    # Real child process; a reused pgid under process churn reads as alive
    # (kew.fleet._group_alive). Serial phase: tests/conftest.py.
    @pytest.mark.serial
    @patch("kew.fleet.reset_issue_to_ready")
    def test_sigkill_after_grace_for_term_ignoring_process(
        self, mock_reset, tmp_path, ps_registered
    ):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        # bash trap '' TERM — ignores SIGTERM, must be SIGKILLed
        bash_path = shutil.which("bash")
        assert bash_path is not None
        proc, attempt = _spawn_owned_attempt(
            tmp_path, [bash_path, "-c", "trap '' TERM; sleep 30"]
        )
        reaper = _reap_async(proc)
        time.sleep(0.2)  # let the trap install
        try:
            with SqliteRunStore(str(tmp_path / "t.db")) as store:
                store.save(_pending_record(7, attempt, owner_pid=proc.pid))
                stop_fleet(store, config, reason="x", grace_seconds=1.0)
            reaper.join(timeout=10)
            assert _group_dead(attempt)
        finally:
            _cleanup_owned_attempt(proc, attempt)

    @patch("kew.fleet._signal_group")
    @patch("kew.fleet.reset_issue_to_ready")
    # Real child process; a reused pgid under process churn reads as alive
    # (kew.fleet._group_alive). Serial phase: tests/conftest.py.
    @pytest.mark.serial
    def test_dead_pgid_marked_killed_without_signal(
        self, mock_reset, mock_signal, tmp_path, ps_registered
    ):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        dead = _spawn_group("0.05")
        dead.wait()
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.save(_pending_record(9, dead.pid))
            report = stop_fleet(store, config, reason="", grace_seconds=0.1)
            record = store.get("run-9")
            assert record is not None
            assert record.status == "killed"
            assert "stale" in report.killed[0].note
        mock_signal.assert_not_called()

    @patch("kew.fleet._signal_group")
    @patch("kew.fleet.reset_issue_to_ready")
    def test_command_mismatch_no_signal_sent(
        self, mock_reset, mock_signal, tmp_path, ps_registered
    ):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)

        with (
            patch("kew.fleet.start_token_matches", return_value=False),
            patch("kew.fleet.group_alive", return_value=True),
            patch("kew.process_groups.group_alive", return_value=True),
            SqliteRunStore(str(tmp_path / "t.db")) as store,
        ):
            store.save(_pending_record(3, 12345))
            report = stop_fleet(store, config, reason="", grace_seconds=0.1)
            record = store.get("run-3")
            assert record is not None
            assert record.status == "pending"
            assert "birth" in report.retained[0].note
        mock_signal.assert_not_called()

    def test_codex_process_group_is_recognized(self):
        from kew.fleet import _live_agent_pgid

        record = _pending_record(3, 12345)
        with (
            patch("kew.fleet.start_token_matches", return_value=False),
            patch("kew.fleet.group_alive", return_value=True),
            patch("kew.process_groups.group_alive", return_value=True),
        ):
            pgid, note = _live_agent_pgid(record)
        assert pgid is None
        assert "birth" in note

    @patch("kew.fleet.reset_issue_to_ready")
    # Real child process; a reused pgid under process churn reads as alive
    # (kew.fleet._group_alive). Serial phase: tests/conftest.py.
    @pytest.mark.serial
    def test_missing_pgid_refuses_unowned_events_file(
        self, mock_reset, tmp_path, ps_registered
    ):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        proc = _spawn_group()
        reaper = _reap_async(proc)
        log_file = tmp_path / "dispatch-11-x.log"
        events = tmp_path / "dispatch-11-x.events.jsonl"
        events.write_text(
            json.dumps({"ts": 1.0, "type": "pid", "pid": proc.pid}) + "\n"
        )
        try:
            with SqliteRunStore(str(tmp_path / "t.db")) as store:
                store.save(_pending_record(11, None, log_file=log_file))
                report = stop_fleet(store, config, reason="", grace_seconds=0)
            assert proc.poll() is None
            assert len(report.retained) == 1
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reaper.join(timeout=5)

    @patch("kew.fleet.reset_issue_to_ready")
    def test_no_pgid_no_events_marked_killed_stale(
        self, mock_reset, tmp_path, ps_registered
    ):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            row = _pending_record(13, None)
            # This case has no owner evidence, unlike the helper's dead owner.
            row.pgid = row.owner_pid = row.owner_start_time = None
            store.save(row)
            report = stop_fleet(store, config, reason="", grace_seconds=0.1)
            record = store.get("run-13")
            assert record is not None
            assert record.status == "pending"
            assert "owner identity unknown" in report.retained[0].note
            mock_reset.assert_not_called()

    @patch("kew.fleet.reset_issue_to_ready")
    def test_nothing_in_flight_still_latches(self, mock_reset, tmp_path):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            report = stop_fleet(store, config, reason="be safe")
            assert report.killed == []
            assert store.get_fleet_stopped() is not None
        mock_reset.assert_not_called()

    @patch("kew.fleet.reset_issue_to_ready")
    def test_double_stop_is_idempotent(self, mock_reset, tmp_path):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            first = stop_fleet(store, config, reason="first")
            second = stop_fleet(store, config, reason="second")
            assert first.already_stopped is False
            assert second.already_stopped is True
            # Original latch (timestamp + reason) preserved
            latch = store.get_fleet_stopped()
            assert latch is not None
            assert latch["reason"] == "first"
            assert second.killed == []

    @patch("kew.fleet.reset_issue_to_ready", side_effect=RuntimeError("gh down"))
    def test_label_reset_failure_does_not_abort(self, mock_reset, tmp_path):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.save(_pending_record(21, 2**31 - 1))
            report = stop_fleet(store, config, reason="", grace_seconds=0.1)
            assert [k.issue_num for k in report.killed] == [21]
            assert store.get_fleet_stopped() is not None


class TestResumeFleet:
    def test_resume_clears_latch_and_logs(self, tmp_path):
        from kew.fleet import resume_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped("x")
            assert resume_fleet(store, config) is True
            assert store.get_fleet_stopped() is None
            events = _fleet_audit_events(store)
            assert events[-1].event == "fleet_resume"

    def test_resume_when_not_stopped_returns_false(self, tmp_path):
        from kew.fleet import resume_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            assert resume_fleet(store, config) is False


class TestFleetStopMessage:
    def test_includes_reason_and_relative_timestamp(self):
        from kew.fleet import fleet_stop_message

        # A timestamp safely in the past renders as relative time.
        msg = fleet_stop_message(
            {"stopped_at": "2026-06-09T10:00:00+00:00", "reason": "runaway"}
        )
        assert "FLEET STOPPED" in msg
        assert "runaway" in msg
        assert "ago" in msg
        assert "kew resume" in msg

    def test_no_reason_placeholder(self):
        from kew.fleet import fleet_stop_message

        msg = fleet_stop_message({"stopped_at": "t", "reason": ""})
        assert "no reason given" in msg


class TestFormatRelativeTime:
    def test_empty_is_unknown(self):
        from kew.fleet import format_relative_time

        assert format_relative_time("") == "unknown time"

    def test_unparseable_falls_back_to_raw(self):
        from kew.fleet import format_relative_time

        assert format_relative_time("not-a-timestamp") == "not-a-timestamp"

    def test_minutes_ago(self):
        from datetime import timedelta

        from kew.fleet import format_relative_time

        ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        assert format_relative_time(ts) == "5m ago"

    def test_hours_ago(self):
        from datetime import timedelta

        from kew.fleet import format_relative_time

        ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        assert format_relative_time(ts) == "3h ago"

    def test_recent_is_just_now(self):
        from datetime import timedelta

        from kew.fleet import format_relative_time

        ts = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        assert format_relative_time(ts) == "just now"

    def test_naive_timestamp_assumed_utc(self):
        from datetime import timedelta

        from kew.fleet import format_relative_time

        # No tz suffix — treated as UTC rather than raising.
        ts = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
        assert format_relative_time(ts) == "2h ago"


class TestEndToEndStopResume:
    """dispatch -> stop -> killed/ready/worktree/latch -> resume -> dispatchable."""

    @pytest.mark.serial
    @patch("kew.labels.run")
    def test_full_cycle(self, mock_gh_run, tmp_path, ps_registered):
        from kew.dispatch_core import execute_dispatch
        from kew.fleet import resume_fleet, stop_fleet

        mock_gh_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "in-progress"}]}),
            stderr="",
        )
        config = _make_config(tmp_path)
        issue = {
            "number": 42,
            "title": "Test",
            "body": "",
            "labels": [{"name": "ready"}],
        }

        # 1. "Dispatch": a pending run backed by a real agent process group,
        #    with a worktree on disk.
        proc, attempt = _spawn_owned_attempt(tmp_path, ["sleep", "30"])
        reaper = _reap_async(proc)
        worktree = tmp_path / ".worktrees" / "issue-42"
        worktree.mkdir(parents=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        try:
            with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
                store.save(_pending_record(42, attempt, owner_pid=proc.pid))

                # 2. Stop the fleet.
                report = stop_fleet(store, config, reason="drill", grace_seconds=5.0)
                if report.retained:
                    reaper.join(timeout=5)
                    report = stop_fleet(store, config, grace_seconds=0)

                # Run killed, not failed
                record = store.get("run-42")
                assert record is not None
                assert record.status == "killed"
                # Agent process group is dead
                reaper.join(timeout=10)
                assert _group_dead(attempt)
                # Label reset attempted via gh (idempotent edit call)
                edit_calls = [
                    c for c in mock_gh_run.call_args_list if "edit" in c.args[0]
                ]
                assert edit_calls, "expected a gh issue edit label-reset call"
                # Worktree preserved
                assert worktree.exists()
                # Latch engaged; audit trail has fleet_stop
                assert store.get_fleet_stopped() is not None
                events = _fleet_audit_events(store)
                assert any(e.event == "fleet_stop" for e in events)
                assert [k.issue_num for k in report.killed] == [42]

                # 3. While latched, execute_dispatch refuses up front.
                with patch("kew.dispatch_core._check_github_connectivity") as mock_conn:
                    outcome = execute_dispatch(issue=issue, config=config, store=store)
                assert outcome.exit_code == 1
                mock_conn.assert_not_called()

                # 4. Resume re-enables dispatch.
                assert resume_fleet(store, config) is True
                assert store.get_fleet_stopped() is None
                events = _fleet_audit_events(store)
                assert any(e.event == "fleet_resume" for e in events)

                # Dispatch now proceeds past the latch (stub the rest).
                with patch(
                    "kew.dispatch_core._check_github_connectivity",
                    return_value=False,
                ) as mock_conn2:
                    outcome = execute_dispatch(issue=issue, config=config, store=store)
                # Reached the connectivity check (exit 3 = unreachable),
                # i.e. the latch no longer blocks dispatch.
                mock_conn2.assert_called_once()
                assert outcome.exit_code == 3
        finally:
            _cleanup_owned_attempt(proc, attempt)


class TestFleetAuditEnvelope:
    """fleet_stop/fleet_resume carry the actor of whoever ran the command."""

    @patch("kew.fleet.reset_issue_to_ready")
    def test_stop_records_kew_actor(self, mock_reset, tmp_path, monkeypatch):
        from kew.fleet import stop_fleet

        monkeypatch.setenv("KEW_ACTOR", "alice")
        config = _make_config(tmp_path)
        config.project.repo = "octo/widgets"
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            stop_fleet(store, config, reason="runaway", grace_seconds=0.1)
            events = _fleet_audit_events(store)
            assert len(events) == 1
            ev = events[0]
            assert ev.event == "fleet_stop"
            assert ev.actor_type == "human"
            assert ev.actor_id == "alice"
            assert ev.repo == "octo/widgets"
            assert ev.payload["reason"] == "runaway"
            assert ev.payload["runs_killed"] == []

    @patch("kew.fleet.reset_issue_to_ready")
    def test_stop_unset_actor_records_os_user(self, mock_reset, tmp_path, monkeypatch):
        from kew.fleet import stop_fleet

        monkeypatch.delenv("KEW_ACTOR", raising=False)
        monkeypatch.setattr("kew.audit.getpass.getuser", lambda: "osuser")
        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            stop_fleet(store, config, reason="", grace_seconds=0.1)
            assert _fleet_audit_events(store)[0].actor_id == "osuser"

    def test_resume_emits_fleet_resume(self, tmp_path, monkeypatch):
        from kew.fleet import resume_fleet

        monkeypatch.setenv("KEW_ACTOR", "bob")
        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped("test")
            assert resume_fleet(store, config) is True
            ev = _fleet_audit_events(store)[0]
            assert ev.event == "fleet_resume"
            assert ev.actor_id == "bob"

    @patch("kew.fleet.reset_issue_to_ready")
    def test_killed_runs_in_payload(self, mock_reset, tmp_path, ps_registered):
        """Parity: runs_killed keeps run_id/issue/cost_usd/note shape."""
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.save(_pending_record(42, 2**31 - 1))  # positively dead attempt
            stop_fleet(store, config, reason="x", grace_seconds=0.1)
            killed = _fleet_audit_events(store)[0].payload["runs_killed"]
            assert len(killed) == 1
            assert killed[0]["run_id"] == "run-42"
            assert killed[0]["issue"] == 42
            assert "cost_usd" in killed[0]
            assert "note" in killed[0]

    @patch("kew.fleet.reset_issue_to_ready")
    def test_no_jsonl_file_written(self, mock_reset, tmp_path):
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            stop_fleet(store, config, reason="x", grace_seconds=0.1)
        assert not (tmp_path / "logs" / "dispatch-log.jsonl").exists()

    @patch("kew.fleet.reset_issue_to_ready")
    def test_fleet_kill_switch_stops_containers(self, mock_reset, tmp_path):
        """Container termination cannot prove an unknown runtime client dead."""
        from kew.fleet import stop_fleet

        config = _make_config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            record = RunRecord(
                run_id="run-42",
                issue_num=42,
                branch="agent/issue-42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                container_name="kew-42",
                container_id="a" * 64,
            )
            store.save(record)

            with (
                patch("kew.fleet.run") as mock_run,
                patch("kew.fleet._signal_group") as mock_signal,
            ):
                mock_run.side_effect = [
                    subprocess.CompletedProcess([], 0, f"{'a' * 64} /kew-42 true", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, f"{'a' * 64} /kew-42 false", ""),
                ]
                report = stop_fleet(store, config, reason="test-stop")

        mock_run.assert_any_call(["docker", "kill", "a" * 64], check=False, timeout=10)
        mock_signal.assert_not_called()
        assert len(report.retained) == 1
        assert report.retained[0].issue_num == 42


@patch("kew.fleet.reset_issue_to_ready")
@pytest.mark.parametrize("binary", ["present", "missing"])
def test_fleet_stop_probes_the_runtime_the_run_launched_under(
    mock_reset, tmp_path, monkeypatch, binary
):
    """PR #1984 review round 4 (F12): after a docker-to-podman switch, fleet
    stop inspects and kills with the run's recorded runtime; a recorded runtime
    whose binary is missing is unknown, never absent.
    """
    from kew import fleet
    from kew.fleet import stop_fleet

    config = _make_config(tmp_path)
    config.sandbox.runtime = "podman"
    identity = "a" * 64
    monkeypatch.delitem(fleet.TOOL_PATHS, "docker", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: None if binary == "missing" else f"/usr/bin/{name}",
    )
    calls: list[list[str]] = []
    states = iter(["true", "false"])

    def run(argv, *, check, timeout):
        assert (check, timeout) == (False, 10)
        calls.append(list(argv))
        if argv[0] != "docker":
            return subprocess.CompletedProcess(
                argv, 1, "", "Error: No such container: kew-42"
            )
        if argv[1] == "kill":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv, 0, f"{identity} /kew-42 {next(states)}", ""
        )

    with SqliteRunStore(str(tmp_path / "t.db")) as store:
        store.save(
            RunRecord(
                run_id="run-42",
                issue_num=42,
                branch="agent/issue-42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                container_name="kew-42",
                container_id=identity,
                container_runtime="docker",
            )
        )
        with patch("kew.fleet.run", side_effect=run), patch("kew.fleet._signal_group"):
            report = stop_fleet(store, config, reason="runtime-switch")
        task = store.list_audit_events(
            event="stop_label_reset_requested", run_id="run-42"
        )[0]
        assert store.get("run-42").status == "pending"
    assert task.payload["runtime"] == "docker"
    assert [entry.run_id for entry in report.retained] == ["run-42"]
    assert all(argv[0] == "docker" for argv in calls), calls
    if binary == "present":
        assert ["docker", "kill", identity] in calls
    else:
        assert calls == []
