"""Regression tests for exclusive owners and surviving attempt descendants."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from support.checkout_generations import fixture_checkout_generation

from kew.config import DispatchConfig
from kew.fleet import _live_agent_pgid, stop_fleet
from kew.graph.driver.tick import _reap_orphaned_run, default_run_liveness
from kew.process_groups import group_alive
from kew.run_store import (
    ContinuationRecord,
    EpicReservation,
    RunRecord,
    SqliteRunStore,
)

BIRTH = "Tue Sep 15 10:00:00 2026"

_SPAWN_COMMAND = (
    'python3 -c "from multiprocessing.spawn import spawn_main; '
    'spawn_main(tracker_fd=8, pipe_handle=10)" --multiprocessing-fork'
)


def _record(pgid=None, attempt=None):
    record = RunRecord(
        run_id="regression",
        issue_num=42,
        branch="agent/issue-42",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        pgid=pgid,
        attempt_pgid=attempt,
    )
    # Setting the attribute also lets these tests execute against schema v50.
    record.owner_pid = pgid
    record.owner_start_time = BIRTH
    record.attempt_start_time = BIRTH
    from kew import process_groups

    snapshot = getattr(process_groups, "process_identity", lambda pid: None)
    for prefix, pid in (("owner", pgid), ("attempt", attempt)):
        if pid is not None:
            setattr(
                record, f"{prefix}_start_token", process_groups.process_start_token(pid)
            )
    for prefix, pid in (("owner", pgid), ("attempt", attempt)):
        if pid is not None and (identity := snapshot(pid)) is not None:
            setattr(record, f"{prefix}_start_time", identity.birth)
            if prefix == "owner":
                record.owner_ppid, record.owner_command = (
                    identity.parent,
                    identity.command,
                )
    return record


def _running(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.fixture
def leaderless_group():
    proc = subprocess.Popen(
        ["bash", "-c", "sleep 60 & echo $!; wait"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    child = int(proc.stdout.readline())
    proc.kill()
    proc.wait(timeout=5)
    try:
        assert _running(child)
        yield proc.pid, child
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.stdout.close()


_DISPATCH_SCRIPT = """
import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from kew.config import DispatchConfig
from kew.dispatch_core import run_tracked_agent
from kew.run_store import SqliteRunStore
from kew.tracking import DispatchResult
root = Path(sys.argv[1])
config = DispatchConfig()
config.logging.dir = str(root / "logs")
config.retry.max_attempts = 1
runner = MagicMock()
runner.name = "fake"
runner.supports_resume.return_value = False
before = os.getpgrp()
snapshot = {}
with SqliteRunStore(str(root / "dispatch.db")) as store:
    def spawn(**kwargs):
        row = store.list_pending()[0]
        snapshot.update(pid=os.getpid(), pgid=row.pgid,
                        owner_pid=getattr(row, "owner_pid", None), before=before)
        return DispatchResult(exit_code=0, issue_num=42, log_file=root / "x.log")
    with patch("kew.dispatch_core.run_agent", side_effect=spawn):
        if sys.argv[2] == "fail":
            os.setpgid = MagicMock(side_effect=PermissionError("isolation denied"))
        try:
            run_tracked_agent(prompt="p", worktree_dir=root, config=config,
                              issue_num=42, store=store, runner=runner,
                              branch="agent/issue-42", model="test")
        except RuntimeError as exc:
            snapshot["error"] = str(exc)
        snapshot["rows"] = len(store.list_all())
print(json.dumps(snapshot))
"""


@pytest.mark.parametrize("isolation_fails", [False, True])
def test_dispatch_isolates_before_admission(tmp_path, isolation_fails):
    script = tmp_path / "dispatch.py"
    script.write_text(_DISPATCH_SCRIPT)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set +m; "$@"; exit $?',
            "shell",
            sys.executable,
            str(script),
            str(tmp_path),
            "fail" if isolation_fails else "ok",
        ],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout.splitlines()[-1])
    if isolation_fails:
        assert row["rows"] == 0
        assert "exclusive process group" in row["error"]
    else:
        assert row["pgid"] == row["pid"]
        assert row["owner_pid"] == row["pid"]
        assert row["before"] != row["pgid"]


def test_shell_group_never_proves_owner_liveness():
    proc = subprocess.Popen(["bash", "-c", "sleep 60 & wait"], start_new_session=True)
    try:
        row = _record(proc.pid)
        row.owner_pid = proc.pid + 1
        assert default_run_liveness(DispatchConfig())(row) is not True
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_owner_fallback_refuses_unrelated_python():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "kew"],
        start_new_session=True,
    )
    try:
        target, note = _live_agent_pgid(_record(proc.pid))
        assert target is None
        assert "owner" in note
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_fleet_stops_leaderless_attempt(tmp_path, leaderless_group):
    pgid, child = leaderless_group
    row = _record(2**31 - 1, pgid)
    assert _live_agent_pgid(row)[0] is None
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(row)
        with patch("kew.fleet.reset_issue_to_ready"):
            report = stop_fleet(store, DispatchConfig(), grace_seconds=0.05)
        assert report.retained and _running(child)
        assert store.get(row.run_id).status == "pending"


@pytest.mark.parametrize("signal_works", [True, False])
def test_reap_waits_for_attempt_death(
    tmp_path, leaderless_group, monkeypatch, signal_works
):
    pgid, child = leaderless_group
    row = _record(2**31 - 1, pgid)
    monkeypatch.setattr("kew.fleet._GRACE_SECONDS", 0.05)
    if not signal_works:
        monkeypatch.setattr("kew.fleet._signal_group", lambda *args: None)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(row)
        finalize = store.finalize_orphaned_pending

        def checked_finalize(*args, **kwargs):
            assert not _running(child), "attempt must stop before finalization"
            return finalize(*args, **kwargs)

        monkeypatch.setattr(store, "finalize_orphaned_pending", checked_finalize)
        reaped = _reap_orphaned_run(
            store,
            DispatchConfig(),
            EpicReservation(42, "epic", row.run_id, row.started_at),
            row,
            now=datetime.now(UTC),
        )
        assert not reaped
        assert store.get(row.run_id).status == "pending"


@pytest.mark.parametrize("version", [49, 50])
def test_legacy_terminal_resume_requires_empty_group(
    tmp_path, leaderless_group, version
):
    from kew.cli.epic import _resume_owner_is_dead

    pgid, child = leaderless_group
    db = str(tmp_path / "legacy.db")
    continuation = SimpleNamespace(original_run_id="regression")
    config = DispatchConfig()
    with SqliteRunStore(db) as store:
        row = _record(pgid)
        row.status = "killed"
        row.owner_pid = None
        store.save(row)
        assert not _resume_owner_is_dead(store, continuation, config)
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        if "owner_pid" in columns:
            store._conn.execute("ALTER TABLE runs DROP COLUMN owner_pid")
        if version == 49:
            store._conn.execute("ALTER TABLE runs DROP COLUMN attempt_pgid")
        else:
            store._conn.execute("UPDATE runs SET attempt_pgid = pgid, pgid = NULL")
        store._conn.execute("UPDATE schema_version SET version = ?", (version,))
        store._conn.commit()
    with SqliteRunStore(db) as store:
        assert not _resume_owner_is_dead(store, continuation, config)
        os.killpg(pgid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while _running(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _resume_owner_is_dead(store, continuation, config)


def test_terminal_without_anchor_requires_operator_resolution(
    tmp_path, monkeypatch, capsys
):
    import typer

    from kew.cli.epic import _prepare_resume_record

    row = _record()
    row.status = "killed"
    continuation = SimpleNamespace(original_run_id=row.run_id)
    monkeypatch.setattr("kew.cli.epic._resume_record", lambda *a, **k: continuation)
    monkeypatch.setattr("kew.cli.epic._validate_resume_worktree", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(row)
        with pytest.raises(typer.Exit):
            _prepare_resume_record(
                store, epic_ref="epic", item_id="42", config=DispatchConfig()
            )
    assert "kew epic abandon" in capsys.readouterr().err


def _table_start_token(table, pid):
    if pid not in table.members:
        return None
    return table.tokens.get(pid, f"{table.births.get(pid, BIRTH)}+{pid}")


@pytest.fixture
def process_table(monkeypatch):
    """Supply ps and signal observations without host process permissions."""
    # Values are (group, state, command, arguments). Exercise production parsing.
    members = {}
    signals = []
    table = SimpleNamespace(
        members=members,
        signals=signals,
        signal_works=True,
        parents={},
        births={},
        tokens={},
    )

    def getpgid(pid):
        if pid not in members:
            raise ProcessLookupError
        return members[pid][0]

    def killpg(pgid, sig):
        pids = [pid for pid, entry in members.items() if entry[0] == pgid]
        if not pids:
            raise ProcessLookupError
        if sig:
            signals.append((pgid, sig))
            if sig == signal.SIGKILL and table.signal_works:
                for pid in pids:
                    del members[pid]

    def kill(pid, sig):
        if pid not in members:
            raise ProcessLookupError
        if sig:
            signals.append((pid, sig))
            if sig == signal.SIGKILL and table.signal_works:
                del members[pid]

    def read_ps(args, **kwargs):
        if "lstart=" in args:
            stdout = (
                table.births.get(int(args[-1]), BIRTH)
                if int(args[-1]) in members
                else ""
            )
        elif "pid=,pgid=,stat=,lstart=" in args:
            stdout = "\n".join(
                f"{pid} {group} {state} {table.births.get(pid, BIRTH)}"
                for pid, (group, state, _, _) in members.items()
            )
        elif "pid=,ppid=,pgid=,stat=,lstart=,comm=" in args:
            stdout = f"1 0 1 S {BIRTH} init\n" + "\n".join(
                f"{pid} {table.parents.get(pid, 1)} {group} {state} "
                f"{table.births.get(pid, BIRTH)} {command}"
                for pid, (group, state, command, _) in members.items()
            )
        elif "pid=,ppid=,pgid=,stat=,lstart=" in args:
            stdout = "\n".join(
                f"{pid} {table.parents.get(pid, 1)} {group} {state} "
                f"{table.births.get(pid, BIRTH)}"
                for pid, (group, state, _, _) in members.items()
            )
        elif "pid=,ppid=,pgid=,stat=,comm=" in args:
            stdout = "1 0 1 S init\n" + "\n".join(
                f"{pid} {table.parents.get(pid, 1)} {group} {state} {command}"
                for pid, (group, state, command, _) in members.items()
            )
        elif "pid=,ppid=,pgid=,stat=" in args:
            stdout = "\n".join(
                f"{pid} {table.parents.get(pid, 1)} {group} {state}"
                for pid, (group, state, _, _) in members.items()
            )
        elif "-p" not in args:
            stdout = "\n".join(
                f"{pid} {group} {state} {command}"
                for pid, (group, state, command, _) in members.items()
            )
        else:
            entry = members.get(int(args[-1]))
            if entry is None:
                return subprocess.CompletedProcess(args, 1, "", "")
            group, state, command, arguments = entry
            stdout = (
                f"{group} {command}"
                if "pgid=,comm=" in args
                else f"{group} {state} {arguments}"
            )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(os, "getpgid", getpgid)
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr("kew.fleet.run", read_ps)
    # The committed version reads ps in fleet; the new shared module also does.
    module = sys.modules.get("kew.process_groups")
    if module is not None:
        monkeypatch.setattr(module, "run", read_ps)
        monkeypatch.setattr(
            module,
            "process_start_token",
            partial(_table_start_token, table),
            raising=False,
        )
        monkeypatch.setattr(
            "kew.run_store.process_start_token",
            partial(_table_start_token, table),
            raising=False,
        )
    monkeypatch.setattr("kew.fleet._GRACE_SECONDS", 0)
    monkeypatch.setattr("kew.fleet._ESCALATION_PAUSE_SECONDS", 0, raising=False)
    return table


def test_owner_fallback_checks_executed_entrypoint(process_table):
    pgid = 100001
    row = _record(pgid)
    for arguments in (
        'python3 -c "import time; time.sleep(60)" kew',
        "python3 /tmp/unrelated.py kew dispatch",
        "bash -c 'kew dispatch'",
        "uv run python3 unrelated.py kew",
    ):
        command = arguments.split()[0]
        process_table.members[pgid] = (pgid, "S", command, arguments)
        assert _live_agent_pgid(row)[0] is None, arguments
    for arguments in (
        "/env/bin/kew dispatch 42",
        "python3.13 /env/bin/kew dispatch 42",
        "python3 -u -m kew dispatch 42",
    ):
        process_table.members[pgid] = (pgid, "S", arguments.split()[0], arguments)
        row.owner_start_token = _table_start_token(process_table, pgid)
        row.owner_ppid = 1
        row.owner_command = Path(arguments.split()[0]).name
        assert _live_agent_pgid(row)[0] == pgid, arguments
    row.owner_pid = pgid + 1
    assert _live_agent_pgid(row)[0] is None


def _refuse_group_probe(refused, original, pgid, sig):
    """Answer one group's signals EPERM, as macOS does for an all-zombie group.

    A zombie keeps no credentials for the kernel to check a signal against, so
    macOS refuses rather than reporting the ESRCH Linux reports (#2436).
    """
    if pgid == refused:
        raise PermissionError(1, "Operation not permitted")
    return original(pgid, sig)


@pytest.mark.parametrize("state", ["Z", "S"])
def test_a_refused_group_probe_is_resolved_by_its_members(
    monkeypatch, process_table, state
):
    """#2436: a refused probe is a missing answer, so read the group's members.

    macOS refuses a zero signal to a group whose members have all exited and
    not yet been reaped. Reading that refusal as "unknown" left a stop unable
    to prove the death of the attempt it had just signalled, so it never fell
    back to the owner: on the operator's Mac the owner survived its own stop.
    The member scan already excludes zombies, and it also keeps the other
    reason a probe is refused — a group another user now owns — reading alive
    off that user's surviving process.
    """
    owner, attempt = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.members[attempt + 1] = (attempt, state, "sleep", "sleep 60")
    monkeypatch.setattr(os, "killpg", partial(_refuse_group_probe, attempt, os.killpg))
    row = _record(owner, attempt)

    assert group_alive(attempt) is (state == "S"), (
        "only a surviving non-zombie member keeps a refused group alive"
    )
    assert _live_agent_pgid(row)[0] == (owner if state == "Z" else None)


def test_a_refused_group_probe_stays_unknown_without_a_member_scan(
    monkeypatch, process_table
):
    """An unavailable scan cannot turn a refusal into proof of death (#2436)."""
    attempt = 100002
    process_table.members[attempt + 1] = (attempt, "Z", "sleep", "sleep 60")
    monkeypatch.setattr(os, "killpg", partial(_refuse_group_probe, attempt, os.killpg))
    monkeypatch.setattr("kew.process_groups.group_members", Mock(return_value=None))

    assert group_alive(attempt) is None


@pytest.mark.parametrize("recognized", [True, False])
def test_attempt_members_control_owner_fallback(process_table, recognized):
    owner, attempt = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    command = "sleep" if recognized else "unrelated"
    process_table.members[attempt + 1] = (attempt, "S", command, command)
    row = _record(owner, attempt)
    assert _live_agent_pgid(row)[0] is None
    # Only positive emptiness allows the exclusive owner fallback.
    del process_table.members[attempt + 1]
    assert _live_agent_pgid(row)[0] == owner


@pytest.mark.parametrize("signal_works", [True, False])
def test_reap_confirms_scanned_descendants_before_finalizing(
    tmp_path, monkeypatch, process_table, signal_works
):
    pgid = 100002
    process_table.members[pgid + 1] = (pgid, "S", "sleep", "sleep 60")
    process_table.signal_works = signal_works
    row = _record(2**31 - 1, pgid)
    with SqliteRunStore(str(tmp_path / "reap.db")) as store:
        store.save(row)
        finalize = store.finalize_orphaned_pending

        def checked_finalize(*args, **kwargs):
            assert not process_table.members, "attempt must stop before finalization"
            return finalize(*args, **kwargs)

        monkeypatch.setattr(store, "finalize_orphaned_pending", checked_finalize)
        reaped = _reap_orphaned_run(
            store,
            DispatchConfig(),
            EpicReservation(42, "epic", row.run_id, row.started_at),
            row,
            now=datetime.now(UTC),
        )
        assert not reaped
        assert store.get(row.run_id).status == "pending"
        assert process_table.signals == []


@pytest.mark.parametrize("signal_works", [True, False])
def test_fleet_retains_ownership_until_scanned_group_is_empty(
    tmp_path, monkeypatch, process_table, signal_works
):
    pgid = 100002
    process_table.members[pgid] = (pgid, "S", "sleep", "sleep 60")
    process_table.signal_works = signal_works
    owner = 100001
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[pgid] = owner
    row = _record(owner, pgid)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    with SqliteRunStore(str(tmp_path / "stop.db")) as store:
        store.save(row)
        real_signal = os.killpg

        def checked_signal(group, sig):
            if sig:
                current = store.get(row.run_id)
                assert current.status == "pending", "ownership released before death"
                assert current.error_category == "killed"
            real_signal(group, sig)
            if sig == signal.SIGKILL and signal_works:
                process_table.members.pop(owner, None)

        real_kill = os.kill

        def checked_member(pid, sig):
            current = store.get(row.run_id)
            assert current.status == "pending", "ownership released before death"
            assert current.error_category == "killed"
            real_kill(pid, sig)
            if signal_works:
                process_table.members.pop(owner, None)

        monkeypatch.setattr(os, "kill", checked_member)
        monkeypatch.setattr(os, "killpg", checked_signal)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert store.get(row.run_id).status == ("killed" if signal_works else "pending")
        assert len(report.killed) == int(signal_works)
        assert len(report.retained) == int(not signal_works)


@pytest.mark.parametrize("version", [49, 50])
def test_migration_preserves_unknown_terminal_ownership(
    tmp_path, process_table, version
):
    from kew.cli.epic import _resume_owner_is_dead

    pgid = 100002
    process_table.members[pgid] = (pgid, "S", "sleep", "sleep 60")
    row = _record(pgid)
    row.owner_pid = None
    row.status = "killed"
    continuation = SimpleNamespace(original_run_id=row.run_id)
    config = DispatchConfig()
    db = str(tmp_path / "migration.db")
    with SqliteRunStore(db) as store:
        store.save(row)
        assert not _resume_owner_is_dead(store, continuation, config)
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        if "owner_pid" in columns:
            store._conn.execute("ALTER TABLE runs DROP COLUMN owner_pid")
        if version == 49:
            store._conn.execute("ALTER TABLE runs DROP COLUMN attempt_pgid")
        else:
            store._conn.execute("UPDATE runs SET attempt_pgid = pgid, pgid = NULL")
        store._conn.execute("UPDATE schema_version SET version = ?", (version,))
        store._conn.commit()
    for _ in range(2):  # Opening twice must preserve the unknown-owner marker.
        with SqliteRunStore(db) as store:
            assert not _resume_owner_is_dead(store, continuation, config)
            loaded = store.get(row.run_id)
            assert loaded.owner_pid is None
            assert loaded.attempt_pgid == pgid
    process_table.members.clear()
    with SqliteRunStore(db) as store:
        assert not _resume_owner_is_dead(store, continuation, config)


def test_owner_escalation_rechecks_identity(tmp_path, monkeypatch, process_table):
    pgid = 100001
    process_table.members[pgid] = (pgid, "S", "python3", "python3 -m kew dispatch")
    row = _record(pgid)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    real_signal = os.killpg

    def change_identity(group, sig):
        real_signal(group, sig)
        if sig == signal.SIGTERM:
            process_table.members[pgid] = (pgid, "S", "python3", "python3 other.py")

    monkeypatch.setattr(os, "killpg", change_identity)
    with SqliteRunStore(str(tmp_path / "escalate.db")) as store:
        store.save(row)
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        # Argument changes preserve birth, parent, and command identity.
        assert process_table.signals == [(pgid, signal.SIGTERM), (pgid, signal.SIGKILL)]
        assert store.get(row.run_id).status == "killed"


@pytest.mark.parametrize("output", [None, "", "unparseable process table"])
def test_unreadable_attempt_scan_blocks_owner_fallback(
    monkeypatch, process_table, output
):
    owner, attempt = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.members[attempt + 1] = (attempt, "S", "sleep", "sleep 60")
    original = sys.modules["kew.fleet"].run

    def unreadable(args, **kwargs):
        if "-axo" in args:
            return subprocess.CompletedProcess(
                args, int(output is None), output or "", ""
            )
        return original(args, **kwargs)

    monkeypatch.setattr("kew.fleet.run", unreadable)
    module = sys.modules.get("kew.process_groups")
    if module is not None:
        monkeypatch.setattr(module, "run", unreadable)
    assert _live_agent_pgid(_record(owner, attempt))[0] is None


@pytest.mark.parametrize("teardown_raises", [False, True])
def test_dispatch_stop_intent_prevents_retry_before_finalization(
    tmp_path, monkeypatch, teardown_raises
):
    from unittest.mock import MagicMock

    from kew.dispatch_core import run_tracked_agent
    from kew.tracking import DispatchResult

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.retry.max_attempts = 2
    runner = MagicMock()
    runner.name = "fake"
    runner.supports_resume.return_value = False
    with SqliteRunStore(str(tmp_path / "intent.db")) as store:

        def stop_then_return(**kwargs):
            row = store.list_pending()[0]
            row.error_category = "killed"
            store.save(row)
            if teardown_raises:
                raise RuntimeError("teardown after operator stop")
            return DispatchResult(
                exit_code=143,
                issue_num=42,
                log_file=tmp_path / "x.log",
                error_category="transient",
            )

        spawn = MagicMock(side_effect=stop_then_return)
        monkeypatch.setattr("kew.dispatch_core.run_agent", spawn)
        monkeypatch.setattr("kew.dispatch_core.check_pr_exists", lambda *args: None)
        monkeypatch.setattr("kew.dispatch_core.time.sleep", lambda *args: None)
        result = run_tracked_agent(
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            runner=runner,
            branch="agent/issue-42",
            model="test",
        )
        assert result.error_category == "killed"
        assert spawn.call_count == 1
        row = store.list_by_issue(42)[0]
        assert row.status == "pending", "fleet still owns termination confirmation"
        assert row.error_category == "killed"


@pytest.mark.parametrize("failure", ["database", "missing_write", "stop_race"])
def test_registration_failure_stops_real_spawn(tmp_path, monkeypatch, failure):
    import shutil
    import sqlite3

    from test_orchestrator import FakeRunner

    from kew.dispatch_core import _record_retry_pgid
    from kew.orchestrator import run_agent
    from kew.process_groups import group_alive
    from kew.subprocess_utils import TOOL_PATHS

    marker = tmp_path / "executed"
    runner = FakeRunner()
    monkeypatch.setitem(TOOL_PATHS, "bash", shutil.which("bash"))
    monkeypatch.setattr(
        runner,
        "build_command",
        lambda **kwargs: [
            "bash",
            "-c",
            'echo launched > "$1"; sleep 0.3',
            "agent",
            str(marker),
        ],
    )
    monkeypatch.setattr("kew.orchestrator.find_timeout_cmd", lambda: None)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log = tmp_path / "attempt.log"
    seen = []
    with SqliteRunStore(str(tmp_path / "spawn.db")) as store:
        write_pgid = store.set_attempt_pgid
        row = _record(os.getpgrp())
        row.owner_pid = None  # A stop cannot signal this legacy owner.
        row.log_file = log
        store.save(row)
        if failure == "database":

            def fail_write(*args):
                raise sqlite3.OperationalError("registration unavailable")

            monkeypatch.setattr(store, "set_attempt_pgid", fail_write)
        elif failure == "missing_write":
            monkeypatch.setattr(store, "set_attempt_pgid", lambda *args: None)

        def register(pid):
            seen.append(pid)
            if failure == "stop_race":
                stop_fleet(store, config, grace_seconds=0)
            _record_retry_pgid(store, row.run_id, pid)

        expected = (
            pytest.raises(RuntimeError, match="owner-identity-unavailable")
            if failure == "missing_write"
            else contextlib.nullcontext()
        )
        with expected:
            result = run_agent(
                runner=runner,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                log_file_path=log,
                emit_events=True,
                on_spawn=register,
            )
            assert result.exit_code != 0, (
                "registration failure must terminate the spawned group"
            )
        assert not marker.exists(), "agent must wait for verified registration"
        assert seen and group_alive(seen[0]) is False
        events = [
            json.loads(line)
            for line in log.with_suffix(".events.jsonl").read_text().splitlines()
        ]
        refusal = next(
            event for event in events if event["type"] == "spawn_registration_failed"
        )
        assert refusal["pid"] == seen[0]
        assert refusal["reason"]
        if failure == "database":
            monkeypatch.setattr(store, "set_attempt_pgid", write_pgid)
            from kew.process_groups import exclusive_owner_pid, identity_at_admission

            row = store.get(row.run_id)
            row.pgid = row.owner_pid = exclusive_owner_pid()
            for field, value in identity_at_admission(row.owner_pid).items():
                setattr(row, field, value)
            store.save(row, update_ownership=True)
            successful = run_agent(
                runner=runner,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                log_file_path=log,
                emit_events=True,
                on_spawn=register,
            )
            assert successful.exit_code == 0
            assert marker.exists(), "verified registration must release the agent"
            assert store.get(row.run_id).attempt_pgid == seen[-1]


@pytest.mark.parametrize("action", ["validate", "reap", "stop_target"])
def test_null_attempt_uses_spawn_evidence(tmp_path, monkeypatch, action):
    from kew.fleet import attempt_is_dead

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        row = _record(2**31 - 1)
        row.log_file = tmp_path / "attempt.log"
        row.log_file.with_suffix(".events.jsonl").write_text(
            json.dumps({"type": "pid", "pid": proc.pid}) + "\n"
        )
        if action == "validate":
            assert not attempt_is_dead(row), (
                "spawn evidence must prevent a dead verdict"
            )
        elif action == "stop_target":
            assert _live_agent_pgid(row)[0] is None, (
                "unrelated spawn claims must not authorize a signal"
            )
        else:
            monkeypatch.setattr("kew.fleet._signal_group", lambda *args: None)
            monkeypatch.setattr("kew.fleet._GRACE_SECONDS", 0)
            with SqliteRunStore(str(tmp_path / "events.db")) as store:
                store.save(row)
                assert not _reap_orphaned_run(
                    store,
                    DispatchConfig(),
                    EpicReservation(42, "epic", row.run_id, row.started_at),
                    row,
                    now=datetime.now(UTC),
                ), "live unrecorded attempt must retain the pending row"
                assert store.get(row.run_id).status == "pending"
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_unconfirmed_attempt_is_unknown():
    from kew.fleet import attempt_is_dead

    assert not attempt_is_dead(_record(2**31 - 1)), (
        "missing registration is unknown, never dead"
    )


def _install_dispatch_owner(monkeypatch, process_table):
    owner = os.getpid()
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    monkeypatch.setattr(
        "kew.process_groups.exclusive_owner_pid", Mock(return_value=owner)
    )


def _stop_in_backoff(store, spawn, stop_source, _delay):
    assert spawn.call_count == 1, "stop must occur after the first launch"
    [row] = store.list_by_issue(42)
    assert row.status == "pending"
    if stop_source in {"intent", "both"}:
        store.request_run_stop(row.run_id)
    if stop_source in {"latch", "both"}:
        store.set_fleet_stopped("backoff race")


@pytest.mark.parametrize("stop_source", ["latch", "intent", "both"])
def test_stop_during_backoff_prevents_second_launch(
    tmp_path, monkeypatch, process_table, stop_source
):
    from unittest.mock import MagicMock

    from kew.dispatch_core import run_tracked_agent
    from kew.tracking import DispatchResult

    _install_dispatch_owner(monkeypatch, process_table)
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.retry.max_attempts = 2
    runner = MagicMock()
    runner.name = "fake"
    runner.supports_resume.return_value = False
    with SqliteRunStore(str(tmp_path / "backoff.db")) as store:
        spawn = MagicMock(
            side_effect=lambda **kwargs: DispatchResult(
                exit_code=1,
                issue_num=42,
                log_file=tmp_path / "x.log",
                error_category="transient",
            )
        )

        stop_in_backoff = Mock(
            side_effect=partial(_stop_in_backoff, store, spawn, stop_source)
        )
        monkeypatch.setattr("kew.dispatch_core.run_agent", spawn)
        monkeypatch.setattr("kew.dispatch_core.check_pr_exists", lambda *args: None)
        # Replace only dispatch's clock; process probes and SQLite keep real sleeps.
        monkeypatch.setattr(
            "kew.dispatch_core.time",
            SimpleNamespace(sleep=stop_in_backoff, monotonic=time.monotonic),
        )
        result = run_tracked_agent(
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            runner=runner,
            branch="agent/issue-42",
            model="test",
        )
        stop_in_backoff.assert_called_once()
        assert spawn.call_count == 1, "stop during backoff must prevent another launch"
        assert result.error_category == "killed"
        assert store.list_by_issue(42)[0].error_category == "killed"


@pytest.mark.parametrize("resume_before_exit", [False, True])
def test_retained_standalone_stop_recovers_capacity(
    tmp_path, monkeypatch, resume_before_exit
):
    from kew.fleet import resume_fleet

    owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
    attempt = subprocess.Popen(["sleep", "60"], start_new_session=True)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    try:
        with SqliteRunStore(str(tmp_path / "capacity.db")) as store:
            row = _record(owner.pid, attempt.pid)
            store.save(row)
            attempt.terminate()
            attempt.wait(timeout=5)
            report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
            assert len(report.retained) == 1
            if resume_before_exit:
                assert resume_fleet(store, DispatchConfig())
                assert store.get(row.run_id).status == "pending"
            owner.terminate()
            owner.wait(timeout=5)
            if not resume_before_exit:
                assert resume_fleet(store, DispatchConfig())
                assert store.get(row.run_id).status == "killed", (
                    "resume must reconcile a dead standalone stop"
                )
            new = _record()
            new.run_id = "next"
            new.issue_num = 43
            assert store.try_admit_run(new, cap=1), (
                "admission must reclaim the dead stop's slot"
            )
            assert store.get(row.run_id).status == "killed"
            events = store.list_audit_events(event="host_stop_reconciled")
            assert len(events) == 1 and events[0].payload["reason"]
    finally:
        for proc in (owner, attempt):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize("scan_unknown", [False, True])
def test_retry_registration_retains_previous_group(
    tmp_path, monkeypatch, process_table, scan_unknown
):
    from kew.dispatch_core import _record_retry_pgid
    from kew.fleet import attempt_is_dead

    first, second = 100002, 100004
    process_table.members[first + 1] = (first, "S", "sleep", "sleep 60")
    row = _record(2**31 - 1, first)
    with SqliteRunStore(str(tmp_path / "retry.db")) as store:
        store.save(row)
        with monkeypatch.context() as probe:
            if scan_unknown:
                probe.setattr("kew.process_groups.ps_output", lambda *args: None)
            with pytest.raises(RuntimeError, match="not proven empty"):
                _record_retry_pgid(store, row.run_id, second)
        retained = store.get(row.run_id)
        assert retained.attempt_pgid == first
        assert retained.status == "pending"
        assert retained.error_category == "killed"
        assert not attempt_is_dead(retained)
        process_table.members.clear()
        # Explicit reset permits a clean retry after positive emptiness.
        retained.error_category = ""
        store.transition_run_status(
            row.run_id,
            expected_status="pending",
            new_status="pending",
            error_category="",
        )
        process_table.members[retained.owner_pid] = (
            retained.owner_pid,
            "S",
            "python3",
            "python3 -m kew dispatch",
        )
        process_table.members[second] = (second, "S", "sh", "sh")
        process_table.parents[second] = retained.owner_pid
        retained.owner_start_token = _table_start_token(
            process_table, retained.owner_pid
        )
        retained.owner_ppid, retained.owner_command = 1, "python3"
        store.save(retained, update_ownership=True)
        _record_retry_pgid(store, row.run_id, second)
        assert store.get(row.run_id).attempt_pgid == second


def test_retry_refusal_preserves_pending_completion(
    tmp_path, monkeypatch, process_table
):
    from unittest.mock import MagicMock

    from kew.dispatch_core import run_tracked_agent
    from kew.tracking import DispatchResult

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.retry.max_attempts = 2
    runner = MagicMock(name="runner")
    runner.name = "fake"
    runner.supports_resume.return_value = False
    first, second = 100002, 100004
    process_table.members[first + 1] = (first, "S", "sleep", "sleep 60")
    process_table.members[100001] = (100001, "S", "python3", "python3 -m kew dispatch")
    process_table.members[first] = (first, "S", "sh", "sh")
    process_table.parents[first] = 100001
    launches = []

    def spawn(**kwargs):
        pid = first if not launches else second
        launches.append(pid)
        kwargs["on_spawn"](pid)
        process_table.members.pop(first, None)
        return DispatchResult(
            exit_code=1,
            issue_num=42,
            log_file=tmp_path / "x.log",
            error_category="transient",
        )

    monkeypatch.setattr("kew.dispatch_core.run_agent", spawn)
    monkeypatch.setattr("kew.dispatch_core.check_pr_exists", lambda *args: None)
    monkeypatch.setattr("kew.dispatch_core.time.sleep", lambda *args: None)
    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: 100001)
    with SqliteRunStore(str(tmp_path / "completion.db")) as store:
        run_tracked_agent(
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            runner=runner,
            branch="agent/issue-42",
            model="test",
        )
        row = store.list_by_issue(42)[0]
        assert row.status == "pending", "retry refusal must retain checkout ownership"
        assert row.attempt_pgid == first
        assert row.error_category == "killed"


def test_real_retry_cannot_forget_surviving_child(tmp_path, monkeypatch):
    from kew.dispatch_core import _record_retry_pgid
    from kew.fleet import _signal_group

    first = subprocess.Popen(
        ["bash", "-c", "sleep 60 & echo $!; wait"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(["sleep", "60"], start_new_session=True)
    assert first.stdout is not None
    child = int(first.stdout.readline())
    first.kill()
    first.wait(timeout=5)
    try:
        os.kill(child, 0)
        with SqliteRunStore(str(tmp_path / "real-retry.db")) as store:
            row = _record(2**31 - 1, first.pid)
            store.save(row)
            with pytest.raises(RuntimeError, match="not proven empty"):
                _record_retry_pgid(store, row.run_id, second.pid)
            second.terminate()
            second.wait(timeout=5)
            row = store.get(row.run_id)
            with monkeypatch.context() as blocked:
                blocked.setattr("kew.fleet._signal_group", lambda *args: None)
                blocked.setattr("kew.fleet._GRACE_SECONDS", 0)
                assert not _reap_orphaned_run(
                    store,
                    DispatchConfig(),
                    EpicReservation(42, "epic", row.run_id, row.started_at),
                    row,
                    now=datetime.now(UTC),
                ), "a surviving child must prevent finalization"
            monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
            with patch(
                "kew.fleet._signal_group",
                wraps=_signal_group,
            ) as signals:
                stop_fleet(store, DispatchConfig(), grace_seconds=0.05)
                signals.assert_not_called()
            assert store.get(row.run_id).status == "pending"
            assert _running(child)
    finally:
        for proc in (first, second):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        first.stdout.close()


def test_all_event_groups_require_owner_ancestry(tmp_path, monkeypatch, process_table):
    from kew.fleet import _pgid_from_events, attempt_is_dead

    owner, first, second, unrelated = 100001, 100002, 100004, 100006
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    for group in (first, second, unrelated):
        process_table.members[group] = (group, "S", "bash", "bash")
    process_table.parents.update({first: owner, second: owner})
    row = _record(owner)
    row.log_file = tmp_path / "events.log"
    row.log_file.with_suffix(".events.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "type": "pid",
                    "pid": group,
                    "start_time": BIRTH,
                    "start_token": _table_start_token(process_table, group),
                    "owner_pid": owner,
                }
            )
            + "\n"
            for group in (first, second, unrelated, first)
        )
    )
    assert _pgid_from_events(row) == [first, second, unrelated]
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    process_table.signal_works = False
    with SqliteRunStore(str(tmp_path / "events.db")) as store:
        store.save(row)
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert {group for group, _ in process_table.signals} == {first, second}
        assert not attempt_is_dead(row), "unresolved claims must retain ownership"
        assert store.get(row.run_id).status == "pending"
        del process_table.members[second]
        del process_table.members[unrelated]
        assert not attempt_is_dead(row), "a later exit cannot erase the first group"
        assert not _reap_orphaned_run(
            store,
            DispatchConfig(),
            EpicReservation(42, "epic", row.run_id, row.started_at),
            row,
            now=datetime.now(UTC),
        ), "every recovered group must be empty before finalization"
        process_table.signal_works = True
        process_table.signals.clear()
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert {group for group, _ in process_table.signals} == {first}


def test_forged_event_never_overrides_registered_attempt(tmp_path, process_table):
    owner, registered, unrelated = 100001, 100002, 100004
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.members[registered] = (registered, "S", "sleep", "sleep 60")
    process_table.members[unrelated] = (unrelated, "S", "bash", "bash")
    process_table.parents[registered] = owner
    row = _record(owner, registered)
    row.log_file = tmp_path / "conflict.log"
    row.log_file.with_suffix(".events.jsonl").write_text(
        json.dumps({"type": "pid", "pid": unrelated}) + "\n"
    )
    assert _live_agent_pgid(row)[0] == registered
    row.attempt_pgid = None
    assert _live_agent_pgid(row)[0] is None


def test_real_unrelated_event_group_is_not_signalled(tmp_path, monkeypatch):
    proc = subprocess.Popen(["bash", "-c", "sleep 60 & wait"], start_new_session=True)
    try:
        row = _record(2**31 - 1)
        row.log_file = tmp_path / "forged.log"
        row.log_file.with_suffix(".events.jsonl").write_text(
            json.dumps({"type": "pid", "pid": proc.pid})
            + "\n"
            + json.dumps({"type": "pid", "pid": 2**100})
            + "\n"
        )
        assert _live_agent_pgid(row)[0] is None, "event pid is not launch authority"
        monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
        with SqliteRunStore(str(tmp_path / "forged.db")) as store:
            store.save(row)
            with patch("kew.fleet._signal_group") as signals:
                report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
            signals.assert_not_called()
            assert report.retained and proc.poll() is None
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


@pytest.mark.parametrize(
    "untrusted", ["missing_owner", "unrelated_parent", "reused_owner"]
)
def test_recovered_event_authenticates_owner_chain(
    tmp_path, monkeypatch, process_table, untrusted
):
    owner, attempt, intermediate = 100001, 100002, 100004
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.members[attempt] = (attempt, "S", "bash", "bash")
    process_table.members[intermediate] = (intermediate, "S", "sh", "sh")
    process_table.parents.update({attempt: intermediate, intermediate: owner})
    row = _record(owner)
    row.log_file = tmp_path / "ancestry.log"
    row.log_file.with_suffix(".events.jsonl").write_text(
        json.dumps(
            {
                "type": "pid",
                "pid": attempt,
                "start_time": BIRTH,
                "start_token": _table_start_token(process_table, attempt),
                "owner_pid": owner,
            }
        )
        + "\n"
    )
    if untrusted == "missing_owner":
        row.owner_pid = None
    elif untrusted == "unrelated_parent":
        process_table.parents[intermediate] = 1
    else:
        process_table.members[owner] = (owner, "S", "python3", "python3 -c 'pass' kew")
    assert _live_agent_pgid(row)[0] is None, (
        "event claims require trusted owner ancestry"
    )
    row.owner_pid = owner
    process_table.parents[intermediate] = owner
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    assert _live_agent_pgid(row)[0] == attempt

    real_signal = os.killpg

    def lose_ancestry(group, sig):
        real_signal(group, sig)
        if sig == signal.SIGTERM:
            process_table.parents[intermediate] = 1

    monkeypatch.setattr(os, "killpg", lose_ancestry)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    with SqliteRunStore(str(tmp_path / "ancestry.db")) as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert process_table.signals == [
            (attempt, signal.SIGTERM),
            (attempt, signal.SIGKILL),
        ]
        assert report.retained  # The owner remains alive.


@pytest.mark.parametrize("pid", [2**100, True, -2, "1234", None])
def test_invalid_event_pid_warns_without_aborting_stop(tmp_path, caplog, pid):
    row = _record()
    row.log_file = tmp_path / "invalid.log"
    row.log_file.with_suffix(".events.jsonl").write_text(
        json.dumps({"type": "pid", "pid": pid}) + "\n"
    )
    with SqliteRunStore(str(tmp_path / "invalid.db")) as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert report.retained
        assert "Skipping invalid spawn pid" in caplog.text


@pytest.mark.parametrize("registered", [False, True])
def test_malformed_events_do_not_abort_stop_finalization(
    tmp_path, monkeypatch, caplog, capsys, registered
):
    row = _record(2**31 - 1, 2**31 - 2 if registered else None)
    row.log_file = tmp_path / "malformed.log"
    row.log_file.with_suffix(".events.jsonl").write_text(
        '{"type":"pid","pid":' + "9" * 5000 + "}\n{broken\n[]\n"
    )
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    with SqliteRunStore(str(tmp_path / "malformed.db")) as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert len(report.killed) == int(registered)
        assert len(report.retained) == int(not registered)
        assert "Skipping malformed" in caplog.text + capsys.readouterr().err


def test_migrated_resume_requires_retained_owner_evidence(
    tmp_path, monkeypatch, capsys
):
    import typer

    from kew.cli.epic import _prepare_resume_record, _resume_owner_is_dead

    owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
    attempt = subprocess.Popen(["sleep", "0"], start_new_session=True)
    attempt.wait(timeout=5)
    db = str(tmp_path / "legacy-owner.db")
    now = datetime.now(UTC)
    continuation = ContinuationRecord(
        epic_ref="epic",
        item_id="42",
        original_run_id="regression",
        branch="agent/issue-42",
        worktree_identity=str(tmp_path / "issue-42"),
        state="awaiting_continuation",
        generation=0,
        continuation_run_id=None,
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr("kew.cli.epic._resume_record", lambda *a, **k: continuation)
    monkeypatch.setattr("kew.cli.epic._validate_resume_worktree", lambda *a: None)
    try:
        with SqliteRunStore(db) as store:
            row = _record(attempt.pid)
            row.status = "killed"
            store.save(row)
            store._conn.execute("ALTER TABLE runs DROP COLUMN owner_pid")
            store._conn.execute("ALTER TABLE runs DROP COLUMN attempt_pgid")
            store._conn.execute("UPDATE schema_version SET version = 49")
            store._conn.commit()
        with SqliteRunStore(db) as store:
            assert not _resume_owner_is_dead(store, continuation, DispatchConfig()), (
                "attempt death cannot prove the legacy owner's death"
            )
            with pytest.raises(typer.Exit):
                _prepare_resume_record(
                    store, epic_ref="epic", item_id="42", config=DispatchConfig()
                )
            error = capsys.readouterr().err
            assert "owner_pid" in error and "kew epic abandon" in error
            # A retained owner includes its matching birth, parent, and command.
            from kew.process_groups import identity_at_admission

            row = store.get("regression")
            row.pgid = row.owner_pid = owner.pid
            for field, value in identity_at_admission(owner.pid).items():
                setattr(row, field, value)
            store.save(row, update_ownership=True)
            assert not _resume_owner_is_dead(store, continuation, DispatchConfig())
            owner.terminate()
            owner.wait(timeout=5)
            assert _resume_owner_is_dead(store, continuation, DispatchConfig())
            assert (
                _prepare_resume_record(
                    store, epic_ref="epic", item_id="42", config=DispatchConfig()
                )
                == continuation
            )
            row.owner_pid = None
            store.save(row, update_ownership=True)
            assert not _resume_owner_is_dead(store, continuation, DispatchConfig())
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


def _round5_identity_row(process_table, tmp_path, source="database"):
    owner, attempt = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.members[attempt] = (attempt, "S", "sleep", "sleep 60")
    process_table.parents[attempt] = owner
    row = _record(owner, attempt if source == "database" else None)
    row.log_file = tmp_path / "round5.log"
    row.log_file.with_suffix(".events.jsonl").write_text(
        json.dumps(
            {
                "type": "pid",
                "pid": attempt,
                "start_time": BIRTH,
                "start_token": _table_start_token(process_table, attempt),
                "owner_pid": owner,
            }
        )
        + "\n"
    )
    return row, owner, attempt


@pytest.mark.parametrize("source", ["database", "event"])
def test_round5_attempt_birth_and_ancestry(
    tmp_path, monkeypatch, process_table, source
):
    row, owner, attempt = _round5_identity_row(process_table, tmp_path, source)
    process_table.births[attempt] = "Tue Sep 15 11:00:00 2026"
    process_table.signal_works = False
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "birth.db")) as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert not process_table.signals, "a reused sleep group must never be signalled"
        assert report.retained and store.get(row.run_id).status == "pending"
        process_table.births[attempt] = BIRTH
        process_table.parents[attempt] = 1
        assert _live_agent_pgid(store.get(row.run_id))[0] is None
        process_table.parents[attempt] = owner
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert (
            process_table.signals
            == [(attempt, signal.SIGTERM)] + [(attempt, signal.SIGKILL)] * 3
        ), "matching birth and ancestry must authorize bounded escalation"


@pytest.mark.parametrize("source", ["database", "event"])
def test_round5_escalation_rechecks_birth(tmp_path, monkeypatch, process_table, source):
    row, _, attempt = _round5_identity_row(process_table, tmp_path, source)
    real_signal = os.killpg

    def reuse_after_term(group, sig):
        real_signal(group, sig)
        if sig == signal.SIGTERM:
            process_table.births[group] = "Tue Sep 15 11:00:00 2026"

    monkeypatch.setattr(os, "killpg", reuse_after_term)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "escalation.db")) as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert process_table.signals == [(attempt, signal.SIGTERM)]
        assert report.retained and store.get(row.run_id).status == "pending"


def test_round5_owner_birth_reuse_is_not_signal_authority(process_table):
    owner = 100001
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    row = _record(owner)
    process_table.births[owner] = "Tue Sep 15 11:00:00 2026"
    assert _live_agent_pgid(row)[0] is None, "even a kew owner must match its birth"
    process_table.births[owner] = BIRTH
    assert _live_agent_pgid(row)[0] == owner
    row.owner_start_time = None
    row.owner_start_token = None
    assert _live_agent_pgid(row)[0] is None


def test_round5_registration_persists_birth(tmp_path, process_table):
    from kew.dispatch_core import _record_retry_pgid

    row, _, attempt = _round5_identity_row(process_table, tmp_path)
    row.attempt_pgid = None
    database = str(tmp_path / "registration.db")
    with SqliteRunStore(database) as store:
        store.save(row)
        permit_check = _record_retry_pgid(store, row.run_id, attempt)
        with SqliteRunStore(database) as observer:
            stored = observer.get(row.run_id)
            assert getattr(stored, "attempt_start_time", None) == BIRTH
            assert stored.owner_start_time == BIRTH
            observer.request_run_stop(row.run_id)
        with pytest.raises(RuntimeError, match="stop"):
            permit_check()


def test_round5_v52_preserves_unknown_births(tmp_path):
    from kew.run_store import CURRENT_SCHEMA_VERSION

    database = str(tmp_path / "migration.db")
    with SqliteRunStore(database) as store:
        row = _record(100001, 100002)
        store.save(row)
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        for column in ("attempt_start_time", "owner_start_time"):
            if column in columns:
                store._conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        store._conn.execute("UPDATE schema_version SET version = 51")
        store._conn.commit()
    for _ in range(2):
        with SqliteRunStore(database) as store:
            loaded = store.get(row.run_id)
            assert getattr(loaded, "attempt_start_time", "missing") is None
            assert loaded.owner_start_time is None
            assert (loaded.pgid, loaded.owner_pid, loaded.attempt_pgid) == (
                100001,
                100001,
                100002,
            )
            assert (
                store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
                == CURRENT_SCHEMA_VERSION
            )


def test_round5_contained_absence_waits_for_client(
    tmp_path, monkeypatch, process_table
):
    row, _, attempt = _round5_identity_row(process_table, tmp_path)
    row.container_id = "a" * 64
    row.container_name = "kew-test"
    process_table.signal_works = False
    calls = []

    def absent(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "No such container: kew-test")

    monkeypatch.setattr("kew.fleet.run", absent)
    with SqliteRunStore(str(tmp_path / "contained.db")) as store:
        store.save(row)
        with patch("kew.fleet.reset_issue_to_ready") as reset:
            report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
            assert report.retained, "absence cannot finalize an alive runtime client"
            assert store.get(row.run_id).status == "pending"
            assert (
                process_table.signals
                == [(attempt, signal.SIGTERM)] + [(attempt, signal.SIGKILL)] * 3
            )
            reset.assert_not_called()
            del process_table.members[attempt]
            report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
            assert report.killed and store.get(row.run_id).status == "killed"
            reset.assert_called_once()
            assert sum("inspect" in args for args in calls) == 3


@pytest.mark.parametrize("failure", ["unavailable", "refused"])
def test_round5_container_confirmation_failure_retains_row(
    tmp_path, monkeypatch, process_table, failure
):
    row = _record(100001, 100002)
    row.container_id = "a" * 64
    row.container_name = "kew-test"

    def unavailable(args, **kwargs):
        if failure == "unavailable":
            raise OSError("runtime unavailable")
        return subprocess.CompletedProcess(args, 1, "", "daemon connection refused")

    monkeypatch.setattr("kew.fleet.run", unavailable)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "unavailable.db")) as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert report.retained and store.get(row.run_id).status == "pending"


@pytest.mark.serial
def test_round5_stop_between_registration_and_permit(tmp_path, monkeypatch):
    import io
    import shutil

    from kew.dispatch_core import _record_retry_pgid
    from kew.orchestrator import _announce_spawn
    from kew.subprocess_utils import TOOL_PATHS, popen

    marker = tmp_path / "executed"
    monkeypatch.setitem(TOOL_PATHS, "sh", shutil.which("sh"))
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    monkeypatch.setattr(
        "kew.fleet.run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 1, "", "No such container: kew-test"
        ),
    )
    proc = popen(
        ["sh", "-c", 'echo executed > "$1"', "client", str(marker)],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        gate=True,
    )
    events = io.StringIO()
    try:
        with SqliteRunStore(str(tmp_path / "permit.db")) as store:
            from kew.process_groups import exclusive_owner_pid, process_start_time

            row = _record(exclusive_owner_pid())
            row.owner_start_time = process_start_time(row.owner_pid)
            row.container_id = "a" * 64
            row.container_name = "kew-test"
            store.save(row)

            reports = []

            def register(pid):
                check = _record_retry_pgid(store, row.run_id, pid)
                reports.append(stop_fleet(store, DispatchConfig(), grace_seconds=0))
                return check

            _announce_spawn(proc, events, register)
            proc.wait(timeout=5)
            assert not marker.exists(), "a stop after registration must deny execution"
            refusal = next(
                event
                for event in map(json.loads, events.getvalue().splitlines())
                if event["type"] == "spawn_registration_failed"
            )
            assert "stop" in refusal["reason"].lower()
            reports.append(stop_fleet(store, DispatchConfig(), grace_seconds=0))
            # Linux confirms the SIGTERMed client dead inside the first stop's
            # zero grace; macOS retains the row until the second. Exactly one
            # of the two stops kills it.
            killed = [entry.run_id for report in reports for entry in report.killed]
            assert killed == [row.run_id]
            assert store.get(row.run_id).status == "killed"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.serial
def test_round5_legacy_stop_resume_preserves_continuation(
    tmp_path, monkeypatch, capsys
):
    import typer

    from kew.cli.epic import _prepare_resume_record

    owner = subprocess.Popen(["sleep", "60"], start_new_session=True)
    attempt = subprocess.Popen(["sleep", "0"], start_new_session=True)
    attempt.wait(timeout=5)
    database = str(tmp_path / "legacy-stop.db")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    dirty = checkout / "unfinished"
    dirty.write_text("preserved work")
    monkeypatch.setattr("kew.cli.epic._validate_resume_worktree", lambda *a: None)
    try:
        with SqliteRunStore(database) as store:
            row = _record(attempt.pid)
            row.error_category = "killed"  # Existing stop intent must remain unchanged.
            store.save(row)
            continuation = store.record_continuation(
                "epic",
                "42",
                row.run_id,
                row.branch,
                checkout,
                checkout_generation=fixture_checkout_generation(checkout),
            )
            store._conn.execute("ALTER TABLE runs DROP COLUMN owner_pid")
            store._conn.execute("ALTER TABLE runs DROP COLUMN attempt_pgid")
            store._conn.execute("UPDATE schema_version SET version = 49")
            store._conn.commit()
        with SqliteRunStore(database) as store:
            original = store.get(row.run_id)
            with patch("kew.fleet.reset_issue_to_ready") as reset:
                report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
                assert report.retained, (
                    "attempt death cannot establish legacy owner death"
                )
                assert "owner identity unknown" in report.retained[0].note
                assert store.get(row.run_id) == original
                reset.assert_not_called()
            with pytest.raises(typer.Exit):
                _prepare_resume_record(
                    store, epic_ref="epic", item_id="42", config=DispatchConfig()
                )
            assert "kew epic abandon" in capsys.readouterr().err
            assert store.get(row.run_id) == original
            assert store.list_continuations("epic") == [continuation]
            assert dirty.read_text() == "preserved work"
            assert owner.poll() is None
    finally:
        owner.kill()
        owner.wait(timeout=5)


def test_round5_refusal_names_only_registered_commands(tmp_path, monkeypatch, capsys):
    import re

    import typer
    from typer.main import get_command

    from kew.cli._apps import epic_app
    from kew.cli.epic import _prepare_resume_record

    continuation = SimpleNamespace(original_run_id="regression")
    monkeypatch.setattr("kew.cli.epic._resume_record", lambda *a, **k: continuation)
    monkeypatch.setattr("kew.cli.epic._validate_resume_worktree", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "message.db")) as store:
        store.save(_record())
        for app in (epic_app, typer.Typer()):
            if app is not epic_app:
                app.command("abandon")(lambda: None)
                app.command("continue")(lambda: None)
            monkeypatch.setattr("kew.cli.epic.epic_app", app)
            with pytest.raises(typer.Exit):
                _prepare_resume_record(
                    store, epic_ref="epic", item_id="42", config=DispatchConfig()
                )
            reason = capsys.readouterr().err
            commands = set(get_command(app).commands)
            assert set(re.findall(r"kew epic (\w+)", reason)) <= commands
            if "abandon" in commands:
                assert "kew epic abandon" in reason
            else:
                assert "operator resolution" in reason and "PR #1952" in reason


def test_round5_permit_pipe_rechecks_durable_intent(tmp_path, process_table):
    import io
    from unittest.mock import Mock

    from kew.dispatch_core import _record_retry_pgid
    from kew.orchestrator import _announce_spawn
    from kew.subprocess_utils import _GatedProcess

    row, _, attempt = _round5_identity_row(process_table, tmp_path)
    read_fd, write_fd = os.pipe()
    proc = Mock(spec=_GatedProcess)
    proc.pid = attempt
    proc.returncode = 0
    proc._kew_spawn_gate_fd = write_fd
    events = io.StringIO()
    try:
        with SqliteRunStore(str(tmp_path / "permit-pipe.db")) as store:
            store.save(row)

            def register(pid):
                check = _record_retry_pgid(store, row.run_id, pid)
                store.request_run_stop(row.run_id)
                return check

            _announce_spawn(proc, events, register)
            assert os.read(read_fd, 10) == b"", "stop intent must close the permit pipe"
            refusal = next(
                event
                for event in map(json.loads, events.getvalue().splitlines())
                if event["type"] == "spawn_registration_failed"
            )
            assert "stop" in refusal["reason"].lower()
            assert store.get(row.run_id).error_category == "killed"
    finally:
        os.close(read_fd)
        if proc._kew_spawn_gate_fd is not None:
            os.close(proc._kew_spawn_gate_fd)


@pytest.mark.serial
@pytest.mark.parametrize("failed_probes", [1, 3])
def test_round6_owner_admission_gate(tmp_path, monkeypatch, failed_probes):
    """A real gate cannot execute without a persisted admission owner birth."""
    import io
    import shutil
    from functools import partial

    from test_dispatch_core import _profile_run

    from kew.dispatch_core import _record_retry_pgid, _save_pending_run
    from kew.orchestrator import _announce_spawn
    from kew.subprocess_utils import TOOL_PATHS, popen

    owner = 100001
    probes = []

    def birth(pid):
        if pid == owner:
            probes.append(pid)
            return None if len(probes) <= failed_probes else BIRTH
        return BIRTH

    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: owner)
    from kew.process_groups import ProcessIdentity

    def identity(pid):
        observed_birth = birth(pid)
        if observed_birth is None:
            return None
        return ProcessIdentity(
            1 if pid == owner else owner,
            pid,
            "S",
            observed_birth,
            "python3" if pid == owner else "sh",
            f"{observed_birth}+{pid}" if pid == owner else None,
        )

    from kew.process_groups import process_start_token as real_start_token

    def start_token(pid):
        # The fake owner carries a kernel token derived from its observed birth;
        # every real process keeps the host probe.
        if pid != owner:
            return real_start_token(pid)
        return None if len(probes) <= failed_probes else f"{BIRTH}+{pid}"

    def snapshot():
        # The permit check reads one host snapshot; the fake owner lives in it
        # exactly when the identity probe answers.
        entry = identity(owner)
        return {owner: entry} if entry is not None else {}

    monkeypatch.setattr("kew.process_groups.process_start_time", birth)
    monkeypatch.setattr("kew.process_groups.process_identity", identity)
    monkeypatch.setattr("kew.process_groups._identity_snapshot", snapshot)
    monkeypatch.setattr("kew.process_groups.process_start_token", start_token)
    monkeypatch.setattr("kew.run_store.process_start_time", birth)
    monkeypatch.setattr("kew.run_store.process_start_token", start_token)
    monkeypatch.setattr(
        "kew.run_store.attempt_descends_from", lambda *args: True, raising=False
    )
    monkeypatch.setitem(TOOL_PATHS, "sh", shutil.which("sh"))
    marker = tmp_path / "executed"
    events = io.StringIO()
    with SqliteRunStore(":memory:") as store:
        run = _profile_run(store, tmp_path, runner=SimpleNamespace(name="fake"))
        if failed_probes == 3:
            with pytest.raises(RuntimeError, match="owner-identity-unavailable"):
                _save_pending_run(
                    run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
                )
            assert len(probes) == 3
            assert store.list_pending() == []
            assert not marker.exists()
            return
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        admission_probes = len(probes)
        proc = popen(
            ["sh", "-c", 'echo executed > "$1"', "agent", str(marker)],
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            gate=True,
        )
        try:
            _announce_spawn(
                proc, events, partial(_record_retry_pgid, store, run.run_id)
            )
            proc.wait(timeout=5)
            row = store.get(run.run_id)
            assert row.owner_start_time == BIRTH, "retry must persist owner birth"
            assert marker.exists() and proc.returncode == 0
            assert row.attempt_owner_verified
            # Controlled probes establish that the admitted identity is signallable.
            monkeypatch.setattr("kew.fleet.group_alive", lambda pid: True)
            monkeypatch.setattr(
                "kew.fleet.recovered_attempt_signallable", lambda *a: True
            )
            monkeypatch.setattr("kew.fleet.start_token_matches", lambda *a: True)
            assert _live_agent_pgid(row)[0] == proc.pid
            assert admission_probes == min(failed_probes + 1, 3)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


@pytest.mark.parametrize("reused", [False, True])
def test_round6_retained_member_births(tmp_path, monkeypatch, process_table, reused):
    """A leader exit preserves authority only for each unchanged member birth."""
    row, owner, attempt = _round5_identity_row(process_table, tmp_path)
    child, survivor = attempt + 1, attempt + 2
    for pid in (child, survivor):
        process_table.members[pid] = (attempt, "S", "sleep", "sleep 60")
        process_table.parents[pid] = attempt
    real_signal = os.killpg

    def leader_exits(group, sig):
        real_signal(group, sig)
        if sig == signal.SIGTERM:
            del process_table.members[attempt]
            del process_table.members[owner]
            process_table.parents[child] = process_table.parents[survivor] = 1
            if reused:
                process_table.births[child] = "Tue Sep 15 11:00:00 2026"

    monkeypatch.setattr(os, "killpg", leader_exits)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert (survivor, signal.SIGKILL) in process_table.signals, (
            "retained survivor must be killed"
        )
        assert ((child, signal.SIGKILL) in process_table.signals) is not reused
        assert bool(report.retained) is reused


_PATIENT_SECONDS = 30.0


def _read_pid_file(path, deadline=_PATIENT_SECONDS):
    """Wait for a child's pid file; the file can exist before its content lands."""
    stop = time.monotonic() + deadline
    while time.monotonic() < stop:
        if path.exists() and (text := path.read_text().strip()):
            return int(text)
        time.sleep(0.01)
    return None


def _synchronized_stop(monkeypatch, spawned=None):
    """Replace scheduler races in real-process stop tests with explicit events.

    A loaded host can run the escalation scan before a late child exists, and
    can delay SIGKILL delivery past a short fixed grace. The SIGTERM grace
    first waits for *spawned* (the late child's pid file). Once escalation has
    begun, every wait polls for group death up to a generous deadline.
    """
    from kew import fleet

    real_wait, real_escalate = fleet._wait_for_groups, fleet._kill_retained_group
    escalated = []

    def escalate(*args, **kwargs):
        escalated.append(True)
        return real_escalate(*args, **kwargs)

    def wait(groups, grace):
        if escalated:
            return real_wait(groups, _PATIENT_SECONDS)
        if spawned is not None:
            assert _read_pid_file(spawned) is not None, "SIGTERM must spawn the child"
        return real_wait(groups, grace)

    monkeypatch.setattr(fleet, "_kill_retained_group", escalate)
    monkeypatch.setattr(fleet, "_wait_for_groups", wait)


@pytest.mark.serial
@pytest.mark.parametrize("owner_dies", [False, True])
def test_round6_real_leader_exit_escalation(tmp_path, monkeypatch, owner_dies):
    """Use real ps, ancestry, signals, and an uncooperative descendant."""
    from test_fleet import _cleanup_owned_attempt, _spawn_owned_attempt

    from kew.fleet import stop_orphaned_attempt
    from kew.process_groups import process_start_time

    child_file = tmp_path / "child"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.01)\n"
    )
    leader_script = tmp_path / "leader.py"
    leader_script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen(sys.argv[1:])\n"
        "while True: time.sleep(0.01)\n"
    )
    owner, attempt = _spawn_owned_attempt(
        tmp_path,
        [
            sys.executable,
            str(leader_script),
            sys.executable,
            str(child_script),
            str(child_file),
        ],
    )
    monkeypatch.setattr("kew.fleet._GRACE_SECONDS", 0.2)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    _synchronized_stop(monkeypatch)
    try:
        child = _read_pid_file(child_file)
        assert child is not None, "the leader must start its descendant"
        with SqliteRunStore(str(tmp_path / "real.db")) as store:
            row = _record(owner.pid)
            row.owner_start_time = process_start_time(owner.pid)
            store.save(row)
            store.set_attempt_pgid(row.run_id, attempt)
        with SqliteRunStore(str(tmp_path / "real.db")) as store:
            row = store.get(row.run_id)
            if owner_dies:
                owner.kill()
                owner.wait(timeout=_PATIENT_SECONDS)
                stopped = stop_orphaned_attempt(row, store)
            else:
                stop_fleet(store, DispatchConfig(), grace_seconds=0.2)
                stopped = not _running(child)
            assert stopped, (
                "authenticated descendant must die after leader or owner exit"
            )
            assert not _running(child)
    finally:
        _cleanup_owned_attempt(owner, attempt)


@pytest.mark.parametrize("owner_group_survives", [False, True])
def test_round6_registration_authority_survives_reopen(
    tmp_path, process_table, owner_group_survives
):
    from kew.fleet import stop_orphaned_attempt

    row, owner, attempt = _round5_identity_row(process_table, tmp_path)
    db = str(tmp_path / "authority.db")
    with SqliteRunStore(db) as store:
        store.save(row)
        store.set_attempt_pgid(row.run_id, attempt)
    del process_table.members[owner]
    if owner_group_survives:
        process_table.members[owner + 5] = (owner, "S", "sleep", "sleep 60")
    process_table.parents[attempt] = 1
    with SqliteRunStore(db) as store:
        assert stop_orphaned_attempt(store.get(row.run_id), store), (
            "registered ancestry must survive owner death"
        )
        assert (attempt, signal.SIGKILL) in process_table.signals


def test_round6_v53_does_not_invent_authority(tmp_path):
    from kew.run_store import CURRENT_SCHEMA_VERSION

    db = str(tmp_path / "v52.db")
    with SqliteRunStore(db) as store:
        store.save(_record(100001, 100002))
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        if "attempt_owner_verified" in columns:
            store._conn.execute("ALTER TABLE runs DROP COLUMN attempt_owner_verified")
        store._conn.execute("UPDATE schema_version SET version = 52")
        store._conn.commit()
    for _ in range(2):
        with SqliteRunStore(db) as store:
            assert (
                getattr(store.get("regression"), "attempt_owner_verified", None)
                is False
            )
            assert (
                store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
                == CURRENT_SCHEMA_VERSION
            )


def test_round6_owner_group_retains_descendants(tmp_path, monkeypatch, process_table):
    owner, child = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "python3 -m kew dispatch")
    process_table.members[child] = (owner, "S", "sleep", "sleep 60")
    process_table.parents[child] = owner
    row = _record(owner)
    real_signal = os.killpg

    def owner_exits(group, sig):
        real_signal(group, sig)
        if sig == signal.SIGTERM:
            del process_table.members[owner]
            process_table.parents[child] = 1

    monkeypatch.setattr(os, "killpg", owner_exits)
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert (child, signal.SIGKILL) in process_table.signals, (
            "owner descendants must remain stoppable"
        )
        assert child not in process_table.members


@pytest.mark.parametrize("owner_group", [False, True])
@pytest.mark.parametrize("foreign", ["none", "reused", "descendant"])
def test_round7_fresh_ancestry_escalation(
    tmp_path, monkeypatch, process_table, capsys, owner_group, foreign
):
    """Authenticate late descendants and reject reused members and their children."""
    row, owner, attempt = _round5_identity_row(process_table, tmp_path)
    group = owner if owner_group else attempt
    if owner_group:
        del process_table.members[attempt]
    anchor, late, reused, stranger = (group + n for n in (10, 11, 12, 13))
    for pid in (anchor, reused):
        process_table.members[pid] = (group, "S", "sleep", "sleep 60")
        process_table.parents[pid] = group
    signal_group = os.killpg

    def terminate(pgid, sig):
        signal_group(pgid, sig)
        if sig != signal.SIGTERM:
            return
        del process_table.members[group]
        process_table.parents[anchor] = 1
        process_table.members[late] = (group, "S", "sleep", "sleep 60")
        process_table.parents[late] = anchor
        if foreign != "none":
            process_table.births[reused] = "Tue Sep 15 11:00:00 2026"
        if foreign == "descendant":
            process_table.members[stranger] = (group, "S", "sleep", "sleep 60")
            process_table.parents[stranger] = reused

    monkeypatch.setattr(os, "killpg", terminate)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert (late, signal.SIGKILL) in process_table.signals, (
            "fresh ancestry must authorize the late descendant"
        )
        assert (anchor, signal.SIGKILL) in process_table.signals
        assert ((reused, signal.SIGKILL) in process_table.signals) == (
            foreign == "none"
        )
        assert (stranger, signal.SIGKILL) not in process_table.signals
        if foreign != "none":
            warning = capsys.readouterr().err
            assert "unauthenticated survivors" in warning and str(reused) in warning
            if foreign == "descendant":
                assert str(stranger) in warning


def test_round7_repeated_cleanup_after_reopen(tmp_path, monkeypatch, process_table):
    """Keep authority across failed delivery, leader exit, and store reopen."""
    from kew.fleet import stop_orphaned_attempt

    row, owner, attempt = _round5_identity_row(process_table, tmp_path)
    anchor, late = attempt + 10, attempt + 11
    process_table.members[anchor] = (attempt, "S", "sleep", "sleep 60")
    process_table.parents[anchor] = attempt
    process_table.signal_works = False
    monkeypatch.setattr("kew.fleet._wait_for_groups", lambda groups, grace: groups)
    db = str(tmp_path / "repeated.db")
    with SqliteRunStore(db) as store:
        store.save(row)
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        # A stale worker record must not overwrite newly retained identities.
        store.save(row)
        store.request_run_stop(row.run_id)
    del process_table.members[attempt]
    del process_table.members[owner]
    process_table.parents[anchor] = 1
    process_table.members[late] = (attempt, "S", "sleep", "sleep 60")
    process_table.parents[late] = anchor
    process_table.signal_works = True
    with SqliteRunStore(db) as store:
        assert stop_orphaned_attempt(store.get(row.run_id), store), (
            "repeated cleanup must retain authority after leader exit and store reopen"
        )
        assert (late, signal.SIGKILL) in process_table.signals
        assert store.get(row.run_id).error_category == "killed"


def _member_token(pid):
    from kew.process_groups import process_identity

    return process_identity(pid).token()


def test_round7_escalation_is_bounded(monkeypatch, process_table, capsys):
    from kew import process_groups
    from kew.fleet import _kill_survivors

    group = 100002
    process_table.members[group] = (group, "S", "sleep", "sleep 60")
    process_table.signal_works = False
    monkeypatch.setattr("kew.fleet._wait_for_groups", lambda groups, grace: groups)
    probes = []
    read_ps = process_groups.run

    def snapshot(args, **kwargs):
        if "pid=,ppid=,pgid=,stat=,lstart=,comm=" in args:
            probes.append(args)
        return read_ps(args, **kwargs)

    token = _member_token(group)
    monkeypatch.setattr(process_groups, "run", snapshot)
    _kill_survivors([group], 0, retained={group: {group: token}})
    assert len(probes) == 12, (
        "three rounds scan ancestry, capture tokens, verify ancestry, "
        "and recheck the target"
    )
    assert process_table.signals == [(group, signal.SIGKILL)] * 3
    assert "round limit reached" in capsys.readouterr().err


def test_round7_snapshot_failure_retains_ownership(monkeypatch, process_table, capsys):
    from kew import process_groups
    from kew.fleet import _kill_survivors

    group = 100002
    process_table.members[group] = (group, "S", "sleep", "sleep 60")
    read_ps = process_groups.run

    def snapshot(args, **kwargs):
        if "pid=,ppid=,pgid=,stat=,lstart=,comm=" in args:
            return subprocess.CompletedProcess(args, 0, "malformed", "")
        return read_ps(args, **kwargs)

    token = _member_token(group)
    monkeypatch.setattr(process_groups, "run", snapshot)
    _kill_survivors([group], 0, retained={group: {group: token}})
    assert not process_table.signals, "unavailable fresh ancestry must deny escalation"
    assert "process snapshot unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("parent", ["retained", "reused"])
def test_scheduler_state_flip_keeps_authority(monkeypatch, process_table, parent):
    """A run-state change between two ps reads must not strip retained authority."""
    from kew import process_groups
    from kew.fleet import _kill_survivors

    group, child = 100002, 100003
    for pid in (group, child):
        process_table.members[pid] = (group, "S", "sleep", "sleep 60")
    process_table.parents[child] = group
    token = _member_token(group)
    if parent == "reused":
        process_table.births[group] = "Tue Sep 15 11:00:00 2026"
    read_ps = process_groups.run
    flips = iter(("R", "S") * 50)

    def flipping(args, **kwargs):
        # Every snapshot sees each member in the other scheduler state, as
        # a loaded host does for a process that sleeps in short intervals.
        if "pid=,ppid=,pgid=,stat=,lstart=,comm=" in args:
            state = next(flips)
            for pid, entry in process_table.members.items():
                process_table.members[pid] = (entry[0], state, *entry[2:])
        return read_ps(args, **kwargs)

    monkeypatch.setattr(process_groups, "run", flipping)
    _kill_survivors([group], 0, retained={group: {group: token}})
    killed = {pid for pid, sig in process_table.signals if sig == signal.SIGKILL}
    if parent == "retained":
        assert killed == {group, child}, "state flips must not deny authority"
        assert not process_table.members
    else:
        assert not killed, "ignoring state must not admit a reused ancestor"


def test_round7_inherited_birth_survives_next_round(monkeypatch, process_table):
    from kew.fleet import _kill_survivors

    group, child, late = 100002, 100003, 100004
    for pid in (group, child):
        process_table.members[pid] = (group, "S", "sleep", "sleep 60")
    process_table.parents[child] = group
    kill = os.kill
    spawned = False

    def delayed_kill(pid, sig):
        nonlocal spawned
        if pid == child and not spawned:
            spawned = True
            process_table.members[late] = (group, "S", "sleep", "sleep 60")
            process_table.parents[late] = child
            return
        kill(pid, sig)

    monkeypatch.setattr(os, "kill", delayed_kill)
    _kill_survivors([group], 0, retained={group: {group: _member_token(group)}})
    assert (late, signal.SIGKILL) in process_table.signals, (
        "a previously authenticated child must authorize the next round's descendant"
    )
    assert not process_table.members


@pytest.mark.parametrize("stop", [False, True])
def test_round7_stale_unknown_owner_stays_pending(tmp_path, monkeypatch, stop):
    from datetime import timedelta

    from kew.scheduler import DispatchScheduler

    row = _record(None, 2**31 - 2)
    row.started_at -= timedelta(hours=25)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        if stop:
            report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
            assert report.retained and store.get_fleet_stopped() is not None
        scheduler = DispatchScheduler(config=DispatchConfig(), store=store)
        scheduler._cleanup_stale_pending()
        saved = store.get(row.run_id)
        assert saved.status == "pending", "age cannot prove an unknown owner dead"
        assert [r.run_id for r in store.list_pending()] == [row.run_id]
        assert saved.error_category == ("killed" if stop else "")
        assert saved.finished_at is None
        assert scheduler._pending_issue_numbers() == {42}


def test_round7_stale_dead_stop_keeps_intent():
    from datetime import timedelta

    row = _record(2**31 - 1, 2**31 - 2)
    row.started_at -= timedelta(hours=25)
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        store.request_run_stop(row.run_id)
        assert store.cleanup_stale_pending() == 1
        saved = store.get(row.run_id)
        assert saved.error_category == "killed", (
            "stale cleanup must preserve stop intent"
        )
        assert saved.status == "failure" and saved.finished_at is not None
        assert store.list_pending() == []
        assert store.cleanup_stale_pending() == 0


def test_round7_v54_has_no_legacy_authority(tmp_path):
    from kew.run_store import CURRENT_SCHEMA_VERSION

    db = str(tmp_path / "v53.db")
    with SqliteRunStore(db) as store:
        store.save(_record(100001, 100002))
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        if "termination_members" in columns:
            store._conn.execute("ALTER TABLE runs DROP COLUMN termination_members")
        store._conn.execute("UPDATE schema_version SET version = 53")
        store._conn.commit()
    for _ in range(2):
        with SqliteRunStore(db) as store:
            assert (
                getattr(store.get("regression"), "termination_members", None) == {}
            ), "migration must provide an empty termination snapshot"
            assert (
                store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
                == CURRENT_SCHEMA_VERSION
            )


@pytest.mark.serial
def test_round7_stale_live_attempt_keeps_intent():
    from datetime import timedelta

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        row = _record(2**31 - 1, proc.pid)
        row.started_at -= timedelta(hours=25)
        with SqliteRunStore(":memory:") as store:
            store.save(row)
            store.request_run_stop(row.run_id)
            assert store.cleanup_stale_pending() == 0, (
                "a live attempt must keep its slot"
            )
            assert store.get(row.run_id).error_category == "killed"
            assert store.get(row.run_id).status == "pending"
            assert len(store.list_pending()) == 1
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.serial
@pytest.mark.parametrize("repeated", [False, True])
def test_round7_real_late_descendant(tmp_path, monkeypatch, repeated):
    """A retained parent authenticates a child born after the group's SIGTERM."""
    from test_fleet import _cleanup_owned_attempt, _spawn_owned_attempt

    from kew.fleet import stop_orphaned_attempt
    from kew.process_groups import process_start_time

    ready, child_file = tmp_path / "ready", tmp_path / "child"
    child_script = tmp_path / "late.py"
    child_script.write_text(
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "p = Path(sys.argv[1]); p.with_suffix('.tmp').write_text(str(os.getpid()))\n"
        "p.with_suffix('.tmp').rename(p)\n"
        "while True: time.sleep(0.01)\n"
    )
    anchor_script = tmp_path / "anchor.py"
    anchor_script.write_text(
        "import signal, subprocess, sys, time\nfrom pathlib import Path\n"
        "def stop(*args):\n"
        "    subprocess.Popen(sys.argv[2:])\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[1]).touch()\n"
        "while True: time.sleep(0.01)\n"
    )
    leader_script = tmp_path / "leader.py"
    leader_script.write_text(
        "import subprocess, sys, time\nsubprocess.Popen(sys.argv[1:])\n"
        "while True: time.sleep(0.01)\n"
    )
    owner, attempt = _spawn_owned_attempt(
        tmp_path,
        [
            sys.executable,
            str(leader_script),
            sys.executable,
            str(anchor_script),
            str(ready),
            sys.executable,
            str(child_script),
            str(child_file),
        ],
    )
    monkeypatch.setattr("kew.fleet._GRACE_SECONDS", 0.2)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    _synchronized_stop(monkeypatch, spawned=child_file)
    try:
        deadline = time.monotonic() + _PATIENT_SECONDS
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        row = _record(owner.pid)
        row.owner_start_time = process_start_time(owner.pid)
        assert row.owner_start_time is not None, "real ps must expose the owner birth"
        db = str(tmp_path / "late.db")
        with SqliteRunStore(db) as store:
            store.save(row)
            store.set_attempt_pgid(row.run_id, attempt)
            with monkeypatch.context() as deferred:
                if repeated:
                    deferred.setattr("kew.fleet._kill_survivors", lambda *a: None)
                stop_fleet(store, DispatchConfig(), grace_seconds=0.3)
        owner.wait(timeout=_PATIENT_SECONDS)
        child = _read_pid_file(child_file)
        assert child is not None, "SIGTERM must spawn the late child"
        if repeated:
            with SqliteRunStore(db) as store:
                assert stop_orphaned_attempt(store.get(row.run_id), store), (
                    "repeated cleanup must authenticate the late child "
                    "through its retained parent"
                )
        assert not _running(child), (
            "initial stop must kill the late authenticated child"
        )
    finally:
        _cleanup_owned_attempt(owner, attempt)


@pytest.mark.serial
def test_round7_real_reparented_child_is_retained(tmp_path, capsys):
    """A direct child loses its ancestry when its sole authenticated parent exits."""
    from kew.fleet import _kill_survivors
    from kew.process_groups import group_member_births

    child_file = tmp_path / "orphan"
    child_script = tmp_path / "orphan.py"
    child_script.write_text(
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.01)\n"
    )
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
        "def stop(*args):\n"
        "    subprocess.Popen(sys.argv[1:])\n"
        "    while not Path(sys.argv[-1]).exists(): time.sleep(0.01)\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.01)\n"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(leader),
            sys.executable,
            str(child_script),
            str(child_file),
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        retained = group_member_births(proc.pid)
        assert retained, "real ps must expose the initial group"
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
        child = int(child_file.read_text().strip())
        _kill_survivors([proc.pid], 0, retained={proc.pid: retained})
        warning = capsys.readouterr().err
        assert "unauthenticated survivors" in warning and str(child) in warning, (
            "lost ancestry must identify the unauthenticated survivor"
        )
        assert _running(child), "unproven ancestry must never authorize a signal"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        proc.stdout.close()


def test_round6_missing_owner_reason_precedes_attempt_probe(tmp_path, process_table):
    from kew.dispatch_core import _record_retry_pgid

    row, _, attempt = _round5_identity_row(process_table, tmp_path)
    row.owner_start_time = None
    row.owner_start_token = None
    row.attempt_pgid = None
    process_table.births[attempt] = ""
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        with pytest.raises(RuntimeError, match="owner-identity-unavailable"):
            _record_retry_pgid(store, row.run_id, attempt)
        assert store.get(row.run_id).attempt_pgid == attempt


@pytest.mark.parametrize("route", ["attempt", "owner", "orphan"])
def test_round8_registered_spawn_owner(tmp_path, monkeypatch, process_table, route):
    """Registration authorizes spawn workers; legacy command claims do not."""
    from kew.fleet import stop_orphaned_attempt

    row, owner, attempt = _round5_identity_row(process_table, tmp_path)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    # Both a forged command argument and a real spawn command lack legacy authority.
    for command in ('python3 -c "import time; time.sleep(60)" kew', _SPAWN_COMMAND):
        process_table.members[owner] = (owner, "S", "python3", command)
        for legacy_attempt in (None, attempt):
            legacy = _record(owner, legacy_attempt)
            with SqliteRunStore(":memory:") as store:
                store.save(legacy)
                assert stop_fleet(store, DispatchConfig(), grace_seconds=0).retained
                assert not process_table.signals, (
                    "legacy python -c must not be signalled"
                )

    with SqliteRunStore(":memory:") as store:
        store.save(row)
        store.set_attempt_pgid(row.run_id, attempt)
        row = store.get(row.run_id)
        assert row.attempt_owner_verified
        # Registration cannot authorize either reused birth, even for spawn workers.
        for pid in (owner, attempt):
            process_table.births[pid] = "Tue Sep 15 11:00:00 2026"
            assert _live_agent_pgid(row)[0] is None
            process_table.births[pid] = BIRTH
        if route == "owner":
            del process_table.members[attempt]
        if route == "orphan":
            stop_orphaned_attempt(row, store)
        else:
            stop_fleet(store, DispatchConfig(), grace_seconds=0)
        target = owner if route == "owner" else attempt
        assert (target, signal.SIGTERM) in process_table.signals, (
            "registered spawn owner must authorize termination"
        )
        assert (target, signal.SIGKILL) in process_table.signals
        assert target not in process_table.members
        assert (
            json.loads(store.get(row.run_id).termination_members[target][target])[0]
            == BIRTH
        )


def test_round8_driver_cleanup_reopens_member_births(
    tmp_path, monkeypatch, process_table
):
    """A failed escalation keeps child authority across the next driver tick."""
    from kew import fleet

    read_members = fleet.authenticated_group_members
    signal_group = os.killpg

    def replay(use_driver):
        row, owner, attempt = _round5_identity_row(process_table, tmp_path)
        child = attempt + 10
        process_table.members[child] = (attempt, "S", "sleep", "sleep 60")
        process_table.parents[child] = attempt
        failed_probes = []

        def snapshot(pgid, retained):
            if not failed_probes:
                failed_probes.append(pgid)
                return None
            return read_members(pgid, retained)

        def terminate(pgid, sig):
            signal_group(pgid, sig)
            if sig == signal.SIGTERM:
                del process_table.members[attempt]
                process_table.parents[child] = 1

        monkeypatch.setattr(fleet, "authenticated_group_members", snapshot)
        monkeypatch.setattr(os, "killpg", terminate)
        db = str(tmp_path / f"cleanup-{use_driver}.db")

        def cleanup(store):
            fresh = store.get(row.run_id)
            if not use_driver:
                return fleet.stop_orphaned_attempt(fresh)
            return _reap_orphaned_run(
                store,
                DispatchConfig(),
                EpicReservation(42, "epic", row.run_id, row.started_at),
                fresh,
                now=datetime.now(UTC),
            )

        with SqliteRunStore(db) as store:
            store.save(row)
            store.set_attempt_pgid(row.run_id, attempt)
            assert store.get(row.run_id).attempt_owner_verified
            del process_table.members[owner]
            process_table.parents[attempt] = 1
            assert not cleanup(store)
            assert failed_probes == [attempt]
            assert (
                child in process_table.members and attempt not in process_table.members
            )
            assert store.get(row.run_id).status == "pending"
        with SqliteRunStore(db) as store:
            retained = store.get(row.run_id).termination_members
            stopped = cleanup(store)
            return stopped, retained, child, store.get(row.run_id).status

    stopped, retained, child, status = replay(False)
    assert not stopped and not retained and status == "pending"
    assert child in process_table.members, (
        "control must reproduce lost cleanup authority"
    )
    process_table.members.clear()
    process_table.signals.clear()
    stopped, retained, child, status = replay(True)
    assert stopped, "the next driver tick must kill the child after reopening the store"
    assert json.loads(retained[100002][child])[0] == BIRTH
    assert (child, signal.SIGKILL) in process_table.signals
    assert child not in process_table.members and status == "killed"


def _round8_spawn_worker(db, connection):
    """Register from multiprocessing's actual Python -c owner entrypoint."""
    from kew.process_groups import exclusive_owner_pid, process_start_time

    owner = exclusive_owner_pid()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        connection.send(child.pid)
        assert child.stdout.readline().strip() == "ready"
        with SqliteRunStore(db) as store:
            row = _record(owner)
            row.owner_start_time = process_start_time(owner)
            store.save(row)
            store.set_attempt_pgid(row.run_id, child.pid)
        connection.send("registered")
        connection.close()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.serial
def test_round8_real_spawn_worker_stop(tmp_path, monkeypatch):
    """Real spawn, registration, ps, ancestry, and signals run on the host."""
    import logging
    import multiprocessing

    from kew.process_groups import _kew_entrypoint, group_alive, ps_output

    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    db = str(tmp_path / "spawn.db")
    worker = context.Process(target=_round8_spawn_worker, args=(db, send))
    worker.start()
    send.close()
    attempt = None
    try:
        assert receive.poll(10), "spawn worker must register and report its attempt"
        attempt = receive.recv()
        assert receive.poll(10), "spawn worker must finish registration"
        assert receive.recv() == "registered"
        with SqliteRunStore(db) as store:
            row = store.get("regression")
            assert row.attempt_owner_verified, "real ps must authenticate registration"
            command = ps_output("-ww", "-o", "args=", "-p", str(worker.pid))
            assert command and "multiprocessing.spawn" in command
            assert not _kew_entrypoint(command)
            signals = []
            signal_group = os.killpg

            def observe(pgid, sig):
                if sig:
                    signals.append((pgid, sig))
                signal_group(pgid, sig)

            monkeypatch.setattr(os, "killpg", observe)
            monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
            stop_fleet(store, DispatchConfig(), grace_seconds=0.2)
            assert (attempt, signal.SIGTERM) in signals, (
                "stop_fleet must signal the spawn worker's registered attempt"
            )
            worker.join(timeout=5)
            assert worker.exitcode == 0, (
                "the worker must finish after attempt termination"
            )
            assert group_alive(attempt) is False
    finally:
        if attempt is not None:
            try:
                os.killpg(attempt, signal.SIGKILL)
            except ProcessLookupError as exc:
                logging.getLogger(__name__).debug("Attempt already exited: %s", exc)
        if worker.is_alive():
            worker.kill()
        worker.join(timeout=5)
        worker.close()
        receive.close()


def _round9_labels(monkeypatch, expected=None):
    """Observe provider effects through both stop and reopened-store paths."""
    resets = []
    failures = []

    def reset(issue, *args):
        if expected is not None and args:
            config = args[0]
            assert (config.project.repo, config.labels.ready) == expected[:2]
        resets.append(issue)
        if failures:
            raise RuntimeError(failures.pop())

    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", reset)
    recovery = sys.modules.get("kew.stop_recovery")
    if recovery is not None:

        def provider(args, **kwargs):
            if args[1:3] == ["issue", "view"]:
                labels = [
                    "in-progress",
                    "agent-working",
                    "custom-progress",
                    "custom-working",
                ]
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps({"labels": [{"name": label} for label in labels]}),
                    "",
                )
            assert args[1:3] == ["issue", "edit"]
            if expected is not None:
                assert args[args.index("--repo") + 1] == expected[0]
                assert args[args.index("--add-label") + 1] == expected[1]
                assert [
                    args[index + 1]
                    for index, arg in enumerate(args)
                    if arg == "--remove-label"
                ] == list(expected[2:])
            reset(int(args[3]))
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(recovery, "_tool", lambda name: f"/fake/{name}")
        monkeypatch.setattr(recovery.subprocess, "run", provider)
    return resets, failures


@pytest.mark.parametrize(
    "path", ["resume", "admission", "reconcile", "stale", "save", "orphan"]
)
def test_round9_deferred_labels_survive_reopen(
    tmp_path, monkeypatch, process_table, path
):
    from datetime import timedelta

    from kew.fleet import resume_fleet

    row = _record(100001, 100002)
    row.started_at -= timedelta(days=2)
    process_table.members[100001] = (100001, "S", "sleep", "sleep 60")
    config = DispatchConfig()
    config.project.repo = "original/project"
    config.labels.ready = "custom-ready"
    config.labels.in_progress = "custom-progress"
    config.labels.agent_working = "custom-working"
    resets, _ = _round9_labels(
        monkeypatch,
        ("original/project", "custom-ready", "custom-progress", "custom-working"),
    )
    database = str(tmp_path / "deferred.db")
    with SqliteRunStore(database) as store:
        store.save(row)
        report = stop_fleet(store, config, grace_seconds=0)
        assert report.retained and not resets
    del process_table.members[100001]
    with SqliteRunStore(database) as store:
        if path == "resume":
            resume_fleet(store, DispatchConfig())
        elif path == "admission":
            new = _record()
            new.run_id, new.issue_num = "next", 43
            assert store.try_admit_run(new, cap=1)
        elif path == "reconcile":
            store.reconcile_host_stops()
        elif path == "stale":
            store.cleanup_stale_pending()
        elif path == "save":
            saved = store.get(row.run_id)
            saved.status = "killed"
            store.save(saved)
        else:
            store.finalize_orphaned_pending(
                row.run_id, error_category="killed", finished_at=datetime.now(UTC)
            )
        assert store.get(row.run_id).status != "pending"
        assert resets == [42], "terminalization must restore ready after reopening"
        assert (
            len(
                store.list_audit_events(
                    event="stop_label_reset_completed", run_id=row.run_id
                )
            )
            == 1
        )
        store.reconcile_host_stops()
        assert resets == [42], "successful label tasks must not run twice"


@pytest.mark.parametrize("deferred", [False, True])
def test_round9_failed_label_reset_retries_terminal_row(
    tmp_path, monkeypatch, process_table, deferred
):
    resets, failures = _round9_labels(monkeypatch)
    failures.append("provider unavailable")
    database = str(tmp_path / "retry-label.db")
    if deferred:
        process_table.members[100001] = (100001, "S", "sleep", "sleep 60")
    with SqliteRunStore(database) as store:
        store.save(_record(100001, 100002))
        stop_fleet(store, DispatchConfig(), grace_seconds=0)
        if deferred:
            assert not resets
            process_table.members.clear()
            store.reconcile_host_stops()
        assert resets == [42], "finalization must attempt its pending label reset"
    with SqliteRunStore(database) as store:
        store.reconcile_host_stops()
        assert resets == [42, 42], (
            "a failed reset must survive and retry on terminal rows"
        )
        store.reconcile_host_stops()
        assert resets == [42, 42]


@pytest.mark.parametrize("container", ["absent", "running", "unknown", "stopped"])
def test_round9_legacy_contained_grace(tmp_path, monkeypatch, process_table, container):
    from datetime import timedelta

    row = _record()
    row.container_name = "kew-legacy"
    row.container_id = "a" * 64
    row.attempt_start_time = None
    row.attempt_start_token = None
    resets, _ = _round9_labels(monkeypatch)

    def runtime(args, **kwargs):
        if "kill" in args or container == "unknown":
            return subprocess.CompletedProcess(args, 1, "", "daemon unavailable")
        if container == "absent":
            return subprocess.CompletedProcess(
                args, 1, "", f"No such container: {row.container_id}"
            )
        return subprocess.CompletedProcess(
            args,
            0,
            f"{row.container_id} /kew-legacy "
            + ("true" if container == "running" else "false"),
            "",
        )

    monkeypatch.setattr("kew.fleet.run", runtime)
    with SqliteRunStore(str(tmp_path / "legacy-container.db")) as store:
        store.save(row)
        assert stop_fleet(store, DispatchConfig(), grace_seconds=0).retained
        assert not resets
        row = store.get(row.run_id)
        row.started_at -= timedelta(seconds=601)
        store.save(row)
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        if container != "absent":
            assert report.retained and not resets
            # All variants must exercise the missing positive recovery too.
            container = "absent"
            report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert report.killed, "legacy absence past startup grace must release capacity"
        assert "legacy" in report.killed[0].note.lower()
        assert "grace" in report.killed[0].note.lower()
        assert store.live_run_count() == 0 and resets == [42]


@pytest.mark.parametrize("recovery_path", ["stop", "resume", "admission"])
@pytest.mark.parametrize("cpu_retry", [False, True])
@pytest.mark.parametrize("origin", ["dispatch", "review"])
def test_round9_standard_sandbox_registers_client(
    tmp_path,
    monkeypatch,
    process_table,
    recovery_path,
    cpu_retry,
    origin,
):
    from unittest.mock import MagicMock

    from kew.dispatch_sandbox import _record_sandbox_start, run_container_and_stream
    from kew.tracking import DispatchResult

    monkeypatch.setattr(
        "kew.sandbox.create_registered_container", lambda argv, **kwargs: "a" * 64
    )
    owner, client = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", _SPAWN_COMMAND)
    process_table.members[client] = (client, "S", "sh", "kew-spawn-gate")
    process_table.parents[client] = owner
    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: owner)
    monkeypatch.setattr(os, "getpid", lambda: owner)
    log = tmp_path / "sandbox.log"
    config = DispatchConfig()
    config.project.repo = "example/project"
    proc = MagicMock(pid=client)
    retry_proc = MagicMock(pid=100003)
    launch_options = []
    resets, _ = _round9_labels(monkeypatch)

    def spawn(argv, **kwargs):
        launch_options.append(kwargs)
        return proc if len(launch_options) == 1 else retry_proc

    monkeypatch.setattr("kew.sandbox.subprocess_utils.popen", spawn)
    with SqliteRunStore(str(tmp_path / "client.db")) as store:
        if origin == "review":
            from kew.graph.driver.review_launcher import _record_pending

            run_id = "review-42"
            _record_pending(
                store,
                SimpleNamespace(run_id=run_id, item_id="42"),
                config,
                log,
                "test",
                "claude-code",
            )
        else:
            run_id = _record_sandbox_start(
                store,
                issue_num=42,
                item_id="42",
                branch="agent/42",
                title="test",
                model="test",
                runner_name="claude-code",
                log_file=log,
                repo=config.project.repo,
                worker_cap=1,
            )

        def stream(*args, **kwargs):
            row = store.get(run_id)
            assert row.attempt_pgid == client, (
                "standard launcher must register the runtime client"
            )
            assert row.owner_pid == owner and row.pgid == owner
            assert row.attempt_start_time == row.owner_start_time == BIRTH
            assert row.attempt_owner_verified
            events = list(
                map(
                    json.loads,
                    log.with_suffix(".events.jsonl").read_text().splitlines(),
                )
            )
            assert any(
                e["type"] == "pid"
                and e["pid"] == client
                and e["start_time"] == BIRTH
                and e["owner_pid"] == owner
                for e in events
            )
            assert len(launch_options) == 1
            options = launch_options[0]
            assert options["start_new_session"] is True and options["gate"] is True
            assert options["credential"] is None
            assert "OPENROUTER_API_KEY" not in options["env"]
            assert "OPENROUTER_PROVISIONING_KEY" not in options["env"]
            if cpu_retry:
                from decimal import Decimal

                del process_table.members[client]
                process_table.members[retry_proc.pid] = (
                    retry_proc.pid,
                    "S",
                    "sh",
                    "kew-spawn-gate",
                )
                process_table.parents[retry_proc.pid] = owner
                handle = args[0].handle
                handle._cpu_cap_rejection_maximum = Decimal(1)
                monkeypatch.setattr(
                    "kew.sandbox._cpu_cap_retry_argv", lambda argv, *a: argv
                )
                assert handle._retry_cpu_cap_rejection()
                retried = store.get(run_id)
                assert retried.attempt_pgid == retry_proc.pid
                assert (
                    retried.attempt_start_time == BIRTH
                    and retried.attempt_owner_verified
                )
                events = list(
                    map(
                        json.loads,
                        log.with_suffix(".events.jsonl").read_text().splitlines(),
                    )
                )
                assert [event["pid"] for event in events if event["type"] == "pid"] == [
                    client,
                    retry_proc.pid,
                ]
                assert len(launch_options) == 2
                assert launch_options[1] == launch_options[0]
            return 0

        monkeypatch.setattr("kew.dispatch_core._stream_sandbox_log", stream)
        monkeypatch.setattr(
            "kew.dispatch_sandbox.finalize_container_hard_stop", lambda *a, **kw: None
        )
        run_container_and_stream(
            config,
            store,
            image="test:local",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="42",
            run_id=run_id,
            model="test",
            log_file=log,
            run_kind="dispatch",
            result=DispatchResult(exit_code=-1, issue_num=42, log_file=log),
        )
        del process_table.members[owner]  # Dispatcher crashed without finalization.
        monkeypatch.setattr(
            "kew.fleet.run",
            lambda args, **kw: subprocess.CompletedProcess(
                args, 0, f"{'a' * 64} /kew-42 false", ""
            ),
        )
        if recovery_path != "stop":
            process_table.signal_works = False
        report = stop_fleet(store, config, grace_seconds=0)
        if recovery_path == "stop":
            assert report.killed and not report.retained
        else:
            from kew.fleet import resume_fleet

            assert report.retained and store.live_run_count() == 1
            monkeypatch.setattr(
                "kew.stop_recovery.container_state", lambda *a: "absent"
            )
            # An absent container cannot release an alive registered client.
            assert store.reconcile_host_stops() == 0
            assert store.live_run_count() == 1
            assert store.live_run_count(liveness=lambda row: False) == 1
            row = store.get(run_id)
            assert not _reap_orphaned_run(
                store,
                config,
                EpicReservation(42, "epic", run_id, row.started_at),
                row,
                now=datetime.now(UTC),
            ), "container absence cannot reap a live registered client"
            from kew.dispatch_sandbox import _finalize_sandbox_run

            _finalize_sandbox_run(store, run_id, exit_code=-9)
            assert store.get(run_id).status == "pending"
            process_table.members.clear()
            monkeypatch.setattr(
                "kew.stop_recovery.container_state", lambda *a: "unknown"
            )
            _finalize_sandbox_run(store, run_id, exit_code=-9)
            assert store.get(run_id).status == "pending", (
                "client exit cannot erase unresolved container ownership"
            )
            monkeypatch.setattr(
                "kew.stop_recovery.container_state", lambda *a: "absent"
            )
            if recovery_path == "resume":
                assert resume_fleet(store, config)
                assert store.get(run_id).status == "killed"
        new = _record()
        new.run_id, new.issue_num = "next", 43
        assert store.try_admit_run(new, cap=1), (
            "the killed client must release production admission capacity"
        )
        assert resets == [42]
