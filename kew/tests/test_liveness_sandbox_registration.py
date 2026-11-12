"""Round-11 durable container launch and stop regressions. No daemon is used."""

import json
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_liveness_owner import _SPAWN_COMMAND, BIRTH
from test_liveness_owner import process_table as process_table

from kew.config import DispatchConfig
from kew.dispatch_admission import PendingAdmission
from kew.dispatch_sandbox import _launch_streaming_container, _record_sandbox_start
from kew.fleet import stop_fleet
from kew.graph.driver.tick import default_run_liveness
from kew.run_store import SqliteRunStore
from kew.sandbox import ContainedExecution

CID = "a" * 64
NAME = "kew-switchyard-0123456789-worker"


def _admit(store, config, log, origin):
    if origin == "review":
        from kew.graph.driver.review_launcher import _record_pending

        _record_pending(
            store,
            SimpleNamespace(run_id="review-42", item_id="42"),
            config,
            log,
            "test",
            "claude-code",
        )
        return "review-42"
    return _record_sandbox_start(
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


def _processes(monkeypatch, table):
    owner, client = 100001, 100002
    table.members[owner] = (owner, "S", "python3", _SPAWN_COMMAND)
    table.members[client] = (client, "S", "sh", "kew-spawn-gate")
    table.parents[client] = owner
    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: owner)
    monkeypatch.setattr("os.getpid", lambda: owner)
    proc = MagicMock(pid=client, returncode=None)
    proc.wait.return_value = -9
    launches, releases = [], []

    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return proc

    def release(proc, *, registered, before_release=None):
        if registered and before_release is not None:
            before_release()
        releases.append(registered)

    monkeypatch.setattr("kew.sandbox.subprocess_utils.popen", spawn)
    monkeypatch.setattr("kew.orchestrator.release_spawn", release)
    return client, launches, releases


def _assert_sidecar_registration(row, client, launches, log):
    assert row.container_name == NAME, "persist the actual sidecar worker name"
    assert row.container_id == CID
    assert row.attempt_pgid == client and row.attempt_start_time == BIRTH
    options = launches[0][1]
    assert options["start_new_session"] is True and options["gate"] is True
    assert options["credential"] is None
    assert "OPENROUTER_API_KEY" not in options["env"]
    assert "OPENROUTER_PROVISIONING_KEY" not in options["env"]
    events = map(json.loads, log.with_suffix(".events.jsonl").read_text().splitlines())
    assert any(e["type"] == "pid" for e in events)


def _assert_sidecar_outcome(stage, store, run_id, config, calls, releases):
    if stage == "before-exec":
        assert True not in releases
        return
    assert releases == [True]
    if stage == "reap":
        _assert_running_sidecar_not_reaped(store, run_id, config)
    else:
        report = stop_fleet(store, config, grace_seconds=0)
        assert report.killed
        assert not report.retained
        assert ["docker", "kill", CID] in calls
        assert store.get(run_id).status == "killed"
    assert all(argv[-1] == CID for argv in calls)


def _assert_running_sidecar_not_reaped(store, run_id, config):
    from kew.graph.driver.tick import _pending_run_is_dead

    row = store.get(run_id)
    row.started_at = datetime.now(UTC) - timedelta(seconds=601)
    store.save(row)
    probe = default_run_liveness(config)
    assert probe(store.get(run_id)) is True
    assert not _pending_run_is_dead(row, probe, now=datetime.now(UTC))
    assert store.live_run_count(liveness=probe) == 1
    assert store.get(run_id).status == "pending"


def _sidecar_runtime(calls):
    running = True

    def runtime(argv, **kwargs):
        nonlocal running
        calls.append(argv)
        if argv[-1] != CID:
            return subprocess.CompletedProcess(
                argv, 1, "", f"No such container: {argv[-1]}"
            )
        if argv[1] == "kill":
            running = False
        return subprocess.CompletedProcess(
            argv, 0, f"{CID} /{NAME} {str(running).lower()}", ""
        )

    return runtime


@pytest.mark.parametrize("origin", ["dispatch", "review"])
@pytest.mark.parametrize("stage", ["before-create", "before-exec", "running", "reap"])
def test_round11_sidecar_durable_launch(
    tmp_path, monkeypatch, process_table, origin, stage
):
    client, launches, releases = _processes(monkeypatch, process_table)
    config, log = DispatchConfig(), tmp_path / "worker.log"
    config.project.repo = "example/project"
    calls, created = [], []
    runtime = _sidecar_runtime(calls)

    monkeypatch.setattr("kew.fleet.run", runtime)
    monkeypatch.setattr("kew.graph.driver.tick.subprocess_utils.run", runtime)
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda c: "/fake/docker")
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "sidecar.db")) as store:
        run_id = _admit(store, config, log, origin)

        def prepare(*args, **kwargs):
            created.append(True)
            return ContainedExecution(
                ("docker", "exec", CID, "agent"), NAME, "docker", tmp_path, CID, True
            )

        monkeypatch.setattr("kew.dispatch_sandbox.prepare_sidecar_worker", prepare)
        if stage == "before-create":
            store.request_run_stop(run_id)
        ready = (
            (lambda *a: store.request_run_stop(run_id))
            if stage == "before-exec"
            else None
        )
        kwargs = dict(
            admission=PendingAdmission(store, run_id),
            run_id=run_id,
            log_file=log,
            image="sha256:" + "b" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="42",
            model="test",
            transport=SimpleNamespace(),
            sidecar_supervisor=object(),
            sidecar_ready=ready,
            run_kind="dispatch",
        )
        if stage == "before-create":
            with pytest.raises(RuntimeError, match="stop"):
                _launch_streaming_container(config, store, **kwargs)
            assert not created and not launches
            return
        _launch_streaming_container(config, store, **kwargs)
        _assert_sidecar_registration(store.get(run_id), client, launches, log)
        _assert_sidecar_outcome(stage, store, run_id, config, calls, releases)


@pytest.mark.parametrize("origin", ["dispatch", "review"])
def test_round11_standard_id_precedes_gate(
    tmp_path, monkeypatch, process_table, origin
):
    from kew.dispatch_sandbox import (
        _register_sandbox_client,
        prepare_registered_container,
    )
    from kew.sandbox import launch_container

    client, launches, releases = _processes(monkeypatch, process_table)
    config, log = DispatchConfig(), tmp_path / "standard.log"
    cidfile = tmp_path / "worker.cid"
    argv = [
        "docker",
        "run",
        "--rm",
        "--cidfile",
        str(cidfile),
        "--name",
        "kew-42",
        "image",
    ]
    with SqliteRunStore(str(tmp_path / "standard.db")) as store:
        run_id = _admit(store, config, log, origin)

        def create(command, **kwargs):
            assert command[1] == "create" and not releases
            assert store.get(run_id).container_cidfile == str(cidfile)
            cidfile.write_text(CID)
            return subprocess.CompletedProcess(command, 0, CID, "")

        monkeypatch.setattr("kew.sandbox.subprocess_utils.run", create)
        execution = prepare_registered_container(argv, store, run_id)
        assert store.get(run_id).container_id == CID
        launch_container(
            list(execution.argv),
            container_name=execution.name,
            container_id=execution.container_id,
            checkout_dir=tmp_path,
            on_spawn=lambda p: _register_sandbox_client(store, run_id, log, p),
        )
        assert store.get(run_id).attempt_pgid == client
        assert launches[0][0][-1] == CID and releases == [True]
        identities = [
            json.loads(line)
            for line in log.with_suffix(".events.jsonl").read_text().splitlines()
        ]
        assert any(
            e.get("container_id") == CID and e["pid"] == client for e in identities
        )


@pytest.mark.parametrize("reopen", [False, True])
def test_round11_rename_replacement_retains_original(
    tmp_path, monkeypatch, process_table, reopen, caplog
):
    from test_liveness_owner import _record

    db, calls = str(tmp_path / "rename.db"), []
    row = _record(100001, 100002)
    row.container_name = "kew-42"
    row.container_id = CID
    row.started_at -= timedelta(seconds=601)

    def runtime(argv, **kwargs):
        calls.append(argv)
        # A name lookup sees the replacement. An id lookup sees the renamed original.
        target = argv[-1]
        name = "renamed-original" if target == CID else "kew-42"
        identity = CID if target == CID else "b" * 64
        return subprocess.CompletedProcess(argv, 0, f"{identity} /{name} true", "")

    monkeypatch.setattr("kew.fleet.run", runtime)
    monkeypatch.setattr("kew.stop_recovery._tool", lambda n: n)
    monkeypatch.setattr("kew.stop_recovery.subprocess.run", runtime)
    with SqliteRunStore(db) as store:
        store.save(row)
        if not reopen:
            assert stop_fleet(store, DispatchConfig(), grace_seconds=0).retained
        else:
            store.request_run_stop(row.run_id, label_reset={"runtime": "docker"})
    if reopen:
        with SqliteRunStore(db) as store:
            assert store.reconcile_host_stops() == 0
            assert store.get(row.run_id).status == "pending"
    assert calls and all(argv[-1] == CID for argv in calls)
    assert not any(argv[1] == "kill" for argv in calls)
    assert "container_identity_mismatch" in caplog.text


@pytest.mark.parametrize("origin", ["dispatch", "review"])
def test_round11_sidecar_identity_survives_stop_during_create(
    tmp_path, monkeypatch, process_table, origin
):
    from kew.dispatch_sandbox import _create_registered_sidecar

    _processes(monkeypatch, process_table)
    config, log = DispatchConfig(), tmp_path / "creation.log"
    db = str(tmp_path / "creation.db")
    with SqliteRunStore(db) as store:
        run_id = _admit(store, config, log, origin)
        stale = store.get(run_id)

        def create(argv, *, on_identity):
            assert store.get(run_id).container_name == NAME
            store.request_run_stop(run_id)
            on_identity(CID)
            return CID

        supervisor = SimpleNamespace(worker_name=NAME, create_worker=create)
        with pytest.raises(RuntimeError, match="stop"):
            _create_registered_sidecar(store, run_id, supervisor, ["docker", "create"])
        # A stale metrics save must preserve the real stop target.
        store.save(stale)
    with SqliteRunStore(db) as store:
        assert store.get(run_id).container_id == CID
        assert store.get(run_id).container_name == NAME


def _round12_absent_runtime(monkeypatch):
    calls = []

    def inspect(argv, **kwargs):
        assert argv[1] == "inspect" and argv[-1] == CID
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 1, "", f"No such container: {argv[-1]}"
        )

    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda config: "docker")
    monkeypatch.setattr("kew.graph.driver.tick.subprocess_utils.run", inspect)
    return calls


def _round12_contained_row(owner, client):
    from test_liveness_owner import _record

    row = _record(owner, client)
    row.container_name, row.container_id = NAME, CID
    row.attempt_owner_verified = True
    return row


def _round12_check_backoff(store, row, now):
    from kew.graph.driver.tick import _pending_run_is_dead, _reap_orphaned_run
    from kew.run_store import EpicReservation

    config = DispatchConfig()
    probe = default_run_liveness(config)
    assert probe(row) is True, "the living owner must survive container loss"
    assert store.live_run_count(liveness=probe) == 1
    assert not _pending_run_is_dead(row, probe, now=now)
    reservation = EpicReservation(42, "epic", row.run_id, row.started_at)
    assert not _reap_orphaned_run(store, config, reservation, row, now=now)
    assert store.get(row.run_id).status == "pending"
    return reservation


def test_round12_contained_backoff_keeps_real_owner(tmp_path, monkeypatch):
    from kew.graph.driver.tick import _reap_orphaned_run
    from kew.process_groups import process_start_time

    _round12_absent_runtime(monkeypatch)
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'pass'], "
        "start_new_session=True); child.wait(); "
        "print(child.pid, flush=True); time.sleep(60)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        row = _round12_contained_row(owner.pid, int(owner.stdout.readline()))
        row.owner_start_time = process_start_time(owner.pid)
        assert row.owner_start_time is not None, "real birth probe requires ps access"
        now = row.started_at + timedelta(seconds=601)
        with SqliteRunStore(str(tmp_path / "real-backoff.db")) as store:
            store.save(row)
            reservation = _round12_check_backoff(store, row, now)
            owner.terminate()
            owner.wait(timeout=5)
            assert _reap_orphaned_run(
                store, DispatchConfig(), reservation, row, now=now
            )
            assert store.get(row.run_id).status == "killed"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if owner.stdout is not None:
            owner.stdout.close()


def test_round12_contained_backoff_keeps_owner(tmp_path, monkeypatch, process_table):
    from kew.graph.driver.tick import _reap_orphaned_run

    _round12_absent_runtime(monkeypatch)
    owner, client = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", _SPAWN_COMMAND)
    row = _round12_contained_row(owner, client)
    now = row.started_at + timedelta(seconds=601)
    with SqliteRunStore(str(tmp_path / "backoff.db")) as store:
        store.save(row)
        reservation = _round12_check_backoff(store, row, now)
        del process_table.members[owner]
        assert _reap_orphaned_run(store, DispatchConfig(), reservation, row, now=now)
        assert (
            store.live_run_count(liveness=default_run_liveness(DispatchConfig())) == 0
        )


@pytest.mark.parametrize("birth", [None, "Tue Sep 15 11:00:00 2026"])
def test_round12_contained_owner_requires_birth(monkeypatch, process_table, birth):
    _round12_absent_runtime(monkeypatch)
    owner = 100001
    process_table.members[owner] = (owner, "S", "python3", _SPAWN_COMMAND)
    row = _round12_contained_row(owner, 100002)
    row.owner_start_time = birth
    row.owner_start_token = f"{birth}+{owner}" if birth else None
    assert default_run_liveness(DispatchConfig())(row) is (
        None if birth is None else False
    )


@pytest.mark.parametrize("survives", [False, True])
def test_round12_contained_orphan_signals_client(
    tmp_path, monkeypatch, process_table, survives, capsys
):
    from kew.graph.driver.tick import _reap_orphaned_run
    from kew.run_store import EpicReservation

    calls = _round12_absent_runtime(monkeypatch)
    owner, client = 100001, 100002
    process_table.members[client] = (client, "S", "docker", "docker exec")
    process_table.signal_works = not survives
    row = _round12_contained_row(owner, client)
    row.owner_start_token = f"{BIRTH}+{owner}"
    row.started_at -= timedelta(seconds=601)
    with SqliteRunStore(str(tmp_path / "orphan.db")) as store:
        store.save(row)
        reaped = _reap_orphaned_run(
            store,
            DispatchConfig(),
            EpicReservation(42, "epic", row.run_id, row.started_at),
            row,
            now=datetime.now(UTC),
        )
        assert (client, signal.SIGTERM) in process_table.signals
        assert reaped is not survives
        assert store.get(row.run_id).status == ("pending" if survives else "killed")
        if survives:
            assert "contained_client_termination_unproven" in capsys.readouterr().err
            assert (
                store.live_run_count(liveness=default_run_liveness(DispatchConfig()))
                == 1
            )
        else:
            assert client not in process_table.members and calls


def test_round12_orphan_rechecks_container_after_client(
    tmp_path, monkeypatch, process_table
):
    from kew.graph.driver.tick import _reap_orphaned_run
    from kew.run_store import EpicReservation

    owner, client = 100001, 100002
    process_table.members[client] = (client, "S", "docker", "docker exec")
    row = _round12_contained_row(owner, client)
    monkeypatch.setattr("kew.graph.driver.tick._container_alive", lambda *a: True)
    with SqliteRunStore(str(tmp_path / "late-container.db")) as store:
        store.save(row)
        assert not _reap_orphaned_run(
            store,
            DispatchConfig(),
            EpicReservation(42, "epic", row.run_id, row.started_at),
            row,
            now=row.started_at + timedelta(seconds=601),
        )
        assert not process_table.signals and client in process_table.members
        assert store.get(row.run_id).status == "pending"
        assert store.get(row.run_id).cleanup_runtime == "docker"


_ROUND12_CRASH_SIDECAR = """
import os, subprocess, sys
from pathlib import Path
from kew.config import DispatchConfig
from kew.dispatch_sandbox import _create_registered_sidecar
from kew.run_store import SqliteRunStore
from kew.switchyard_supervisor import SidecarAttemptSupervisor
store = SqliteRunStore(sys.argv[1])
supervisor = SidecarAttemptSupervisor(
    DispatchConfig().inference_transport.switchyard, runtime='docker',
    worker_image_id='sha256:' + 'b' * 64, owner_factory=lambda: '0' * 32)
supervisor.endpoint = 'http://proxy:4000'
def command(argv, operation):
    if argv[1] == 'create':
        return subprocess.CompletedProcess(argv, 0, 'a' * 64, '')
    # Hard process death after id capture. No finally block or normal return runs.
    store.request_run_stop('regression')
    os._exit(73)
supervisor._command = command
_create_registered_sidecar(store, 'regression', supervisor, ['docker', 'create'])
"""


def test_round12_sidecar_identity_survives_process_death(tmp_path):
    from test_liveness_owner import _record

    db = str(tmp_path / "creation-crash.db")
    log = tmp_path / "creation.log"
    with SqliteRunStore(db) as store:
        row = _record()
        row.log_file = log
        store.save(row)
    result = subprocess.run(
        [sys.executable, "-c", _ROUND12_CRASH_SIDECAR, db], check=False
    )
    assert result.returncode == 73
    with SqliteRunStore(db) as store:
        row = store.get("regression")
        assert row.container_id == CID, "creation must commit before worker startup"
        assert row.error_category == "killed"
    events = [
        json.loads(line)
        for line in log.with_suffix(".events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["container_id"] == CID
    assert events[-1]["attempt_number"] == row.attempt_number


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("initial_owner", [None, 88881])
@pytest.mark.parametrize("saved_status", ["pending", "success"])
def test_round12_stale_save_preserves_stop_and_identity(
    tmp_path, terminal, initial_owner, saved_status
):
    from test_liveness_owner import _record

    db = str(tmp_path / "stale.db")
    with SqliteRunStore(db) as telemetry, SqliteRunStore(db) as owner:
        telemetry.save(_record(initial_owner, 88882))
        stale = telemetry.get("regression")
        current = _round12_contained_row(99991, 99992)
        owner._conn.execute(
            "UPDATE runs SET pgid = ?, owner_pid = ?, owner_start_time = ?,"
            " attempt_pgid = ?, attempt_start_time = ?, attempt_owner_verified = 1,"
            " container_name = ?, container_id = ?, attempt_number = 2,"
            " container_cidfile = 'current.cid' WHERE run_id = ?",
            (99991, 99991, BIRTH, 99992, BIRTH, NAME, CID, current.run_id),
        )
        owner._conn.commit()
        owner.request_run_stop(current.run_id)
        if terminal:
            owner.finalize_orphaned_pending(
                current.run_id, error_category="killed", finished_at=datetime.now(UTC)
            )
        stale.total_tokens = 123
        stale.status, stale.exit_code = saved_status, 0
        telemetry.save(stale)
        saved = owner.get(current.run_id)
        assert saved.error_category == "killed", "telemetry must preserve stop intent"
        assert saved.status == ("killed" if terminal else "pending")
        assert saved.exit_code == (137 if terminal else -1)
        assert saved.total_tokens == 123
        assert saved.attempt_number == 2 and saved.container_cidfile == "current.cid"
        assert (saved.pgid, saved.owner_pid, saved.owner_start_time) == (
            99991,
            99991,
            BIRTH,
        )
        assert (
            saved.attempt_pgid,
            saved.attempt_start_time,
            saved.attempt_owner_verified,
        ) == (99992, BIRTH, True)
        assert (saved.container_name, saved.container_id) == (NAME, CID)


def test_round12_explicit_ownership_save(tmp_path):
    from test_liveness_owner import _record

    with SqliteRunStore(str(tmp_path / "explicit.db")) as store:
        store.save(_record(88881, 88882))
        current = _round12_contained_row(99991, 99992)
        store.save(current, update_ownership=True)
        saved = store.get(current.run_id)
        assert (saved.pgid, saved.owner_pid, saved.owner_start_time) == (
            99991,
            99991,
            BIRTH,
        )
        assert (saved.attempt_pgid, saved.attempt_start_time) == (99992, BIRTH)
        assert saved.attempt_owner_verified and saved.container_id == CID


def test_round11_cidfile_recovers_interrupted_registration(
    tmp_path, monkeypatch, process_table
):
    from test_liveness_owner import _record

    from kew.stop_recovery import record_container_state

    row = _record()
    row.container_name = "kew-42"
    cidfile = tmp_path / "interrupted.cid"
    cidfile.write_text(CID)
    row.container_cidfile = str(cidfile)
    db = str(tmp_path / "interrupted.db")
    with SqliteRunStore(db) as store:
        store.save(row)
    calls = []

    def inspect(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{CID} /kew-42 true", "")

    monkeypatch.setattr("kew.stop_recovery._tool", lambda name: name)
    monkeypatch.setattr("kew.stop_recovery.subprocess.run", inspect)
    with SqliteRunStore(db) as store:
        assert record_container_state("docker", store.get(row.run_id)) == "running"
    assert calls[0][-1] == CID


def test_round11_missing_id_never_targets_a_mutable_name(
    tmp_path, monkeypatch, process_table
):
    from test_liveness_owner import _record

    row = _record(100001, 100002)
    row.container_name = "kew-42"
    row.started_at -= timedelta(seconds=601)
    calls = []

    def runtime(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{CID} /kew-42 false", "")

    monkeypatch.setattr("kew.fleet.run", runtime)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *a: None)
    with SqliteRunStore(str(tmp_path / "unknown.db")) as store:
        store.save(row)
        assert stop_fleet(store, DispatchConfig(), grace_seconds=0).retained
        assert store.get(row.run_id).status == "pending"
    assert not calls, "missing immutable evidence cannot authorize a name-based kill"


def test_round11_retry_preserves_unresolved_container_identity(
    tmp_path, monkeypatch, process_table
):
    from test_liveness_owner import _record

    from kew.dispatch_sandbox import prepare_registered_container

    row = _record()
    row.container_id, row.container_name = CID, "kew-42"
    monkeypatch.setattr("kew.stop_recovery.container_state", lambda *a: "mismatch")
    with SqliteRunStore(str(tmp_path / "retry.db")) as store:
        store.save(row)
        argv = [
            "docker",
            "run",
            "--name",
            "kew-42",
            "--cidfile",
            str(tmp_path / "next.cid"),
            "image",
        ]
        with pytest.raises(RuntimeError, match="retaining identity"):
            prepare_registered_container(argv, store, row.run_id)
        assert store.get(row.run_id).container_id == CID
        assert store.get(row.run_id).error_category == "killed"


@pytest.mark.parametrize("refusal_code", [1, 125])
def test_round11_standard_creation_retries_cpu_refusal(
    tmp_path, monkeypatch, refusal_code
):
    from kew.sandbox import create_registered_container

    cidfile, commands = tmp_path / "retry.cid", []
    argv = [
        "docker",
        "run",
        "--cpus",
        "4",
        "--name",
        "kew-42",
        "--cidfile",
        str(cidfile),
        "image",
    ]

    def runtime(command, **kwargs):
        commands.append(command)
        if command[1] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[command.index("--cpus") + 1] == "4":
            return subprocess.CompletedProcess(
                command,
                refusal_code,
                "",
                "docker: Error response from daemon: "
                "range of CPUs is from 0.01 to 2.00, "
                "as there are only 2 CPUs available",
            )
        cidfile.write_text(CID)
        return subprocess.CompletedProcess(command, 0, CID, "")

    monkeypatch.setattr("kew.sandbox.subprocess_utils.run", runtime)
    monkeypatch.setattr("kew.sandbox._daemon_cpu_count", lambda r: 2)
    assert create_registered_container(argv) == CID
    # main (#1956) confirms a failed creation through the recorded id, not a
    # name-based `ps` probe, so the CPU refusal retries the creation directly.
    assert [command[1] for command in commands] == ["create", "create"]
    assert commands[-1][commands[-1].index("--cpus") + 1] == "1"


@pytest.mark.parametrize("path", ["streaming", "tracked"])
def test_registered_start_retains_nonce_and_settles_receipt(
    tmp_path, monkeypatch, process_table, path
):
    from kew.dispatch_sandbox import (
        _register_sandbox_client,
        prepare_registered_container,
    )
    from kew.sandbox import STARTUP_ACK_PREFIX, STARTUP_NONCE_ENV, launch_container

    _, launches, releases = _processes(monkeypatch, process_table)
    config, log = DispatchConfig(), tmp_path / "receipt.log"
    config.project.repo = "o/r"
    argv = [
        "docker",
        "run",
        "--name",
        "kew-42",
        "--cidfile",
        str(tmp_path / "run.cid"),
        "-e",
        f"{STARTUP_NONCE_ENV}=receipt-nonce",
        "image",
    ]
    monkeypatch.setattr(
        "kew.sandbox.create_registered_container", MagicMock(return_value=CID)
    )
    with SqliteRunStore(":memory:") as store:
        run_id = _admit(store, config, log, "dispatch")
        record = store.get(run_id)
        record.repo = config.project.repo
        store.save(record)
        store.register_ci_fix_attempt(run_id, 87)
        admission = PendingAdmission(store, run_id)
        prepare = partial(prepare_registered_container, store=store, run_id=run_id)
        if path == "streaming":
            handle = launch_container(
                argv,
                admission=admission,
                prepare=prepare,
                on_spawn=partial(_register_sandbox_client, store, run_id, log),
            )
            assert releases == [True]
            assert launches[0][0] == [
                "docker",
                "start",
                "--attach",
                "--interactive",
                CID,
            ]
        else:
            execution = prepare(argv)
            handle = execution.handle_for(MagicMock())
            handle.accept_admission(admission)
        assert store.get(run_id).container_id == CID
        handle.observe_workload_output(f"{STARTUP_ACK_PREFIX}receipt-nonce\n")
        assert handle.observe_workload_start()
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0
        handle.observe_workload_output('{"type":"system"}\n')
        assert handle.observe_workload_start()
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1
