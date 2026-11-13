"""A dead run whose container never started gives its slot back (#3124).

The owner of a contained dispatch died before its container ever started: no
attempt group was registered, and the container sat ``created`` with pid 0,
or never came to exist. The row stayed pending, held an epic slot for 42 h,
and read as progressing. One reconcile now removes that container by id,
finalizes the row as ``container_start_failed``, frees the slot and writes one
audit event, while an owner whose liveness is unknown keeps everything as it
was. ``docker`` is faked at the process boundary; the owner is a real process
that has exited; the startup grace is crossed by the row's recorded start, or
by the tick's pinned clock, never by a sleep.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.driver_fakes import (
    EPIC,
    NOW,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _facts,
    _node,
)
from support.fake_daemon import FakeDaemon
from typer.testing import CliRunner

from kew import process_groups
from kew.cli import app
from kew.dispatch_core import WorkerCapReached
from kew.dispatch_sandbox import (
    _finalize_launch_marker_failure,
    _record_sandbox_start,
    prepare_registered_container,
)
from kew.ledger_identity import open_managed_store
from kew.process_groups import process_start_time
from kew.run_requests import QUEUED, SUPERSEDED, RunRequest, claim
from kew.run_store import RunRecord, SqliteRunStore
from kew.sandbox import ContainerStartFailed
from kew.stop_recovery import (
    container_daemon_key,
    current_daemon_identity,
    never_started_verdict,
)

CREATED = "c" * 64
REAPED = "never_started_run_reaped"
INTENDED = "dead_run_container_removal_intended"
REMOVED = "dead_run_container_removed"
#: What the reap event says on every path: admission, the tick, kew settle.
REAP_FIELDS = {
    "container_id",
    "container_name",
    "runtime",
    "state",
    "rule",
    "reason",
    "error_category",
    "epic_ref",
}


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> FakeDaemon:
    fake = FakeDaemon()
    monkeypatch.setattr("kew.sandbox.subprocess_utils.run", fake.run)
    monkeypatch.setattr("kew.dispatch_sandbox._run", fake.run)
    monkeypatch.setattr(
        "kew.stop_recovery.subprocess",
        SimpleNamespace(
            run=fake.run,
            CompletedProcess=subprocess.CompletedProcess,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    monkeypatch.setattr(
        "kew.stop_recovery.shutil", SimpleNamespace(which=lambda tool: tool)
    )
    return fake


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    """A ledger with its own identity, as every project's store has."""
    with SqliteRunStore(str(tmp_path / "dispatch.db"), project_root=tmp_path) as s:
        yield s


@pytest.fixture
def managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SqliteRunStore]:
    """The registered project ledger the CLI opens from this project's kew.toml."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (tmp_path / "kew.toml").write_text('[logging]\ndir = "logs"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with open_managed_store(
        project_root=tmp_path,
        db_path=log_dir / "dispatch.db",
        anchors_path=tmp_path / "audit-anchors.jsonl",
    ) as s:
        yield s


def _exited_owner() -> tuple[int, str]:
    """A real owner process, now gone: its pid and recorded birth."""
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)  # noqa: S607
    try:
        birth = process_start_time(proc.pid)
        assert birth is not None, "owner birth requires host ps access"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    return proc.pid, birth


def _dead_row(
    run_id: str = "dead-run",
    *,
    item_id: str = "42",
    container_id: str | None = CREATED,
    started_at: datetime | None = None,
    owner_known: bool = True,
    attempt_pgid: int | None = None,
    owner_boot_id: str | None = None,
    container_cidfile: str | None = None,
) -> RunRecord:
    """A pending contained run whose owner died before any attempt started."""
    pid, birth = _exited_owner()
    return RunRecord(
        run_id=run_id,
        issue_num=int(item_id),
        item_id=item_id,
        branch=f"agent/issue-{item_id}",
        status="pending",
        exit_code=-1,
        started_at=started_at or datetime.now(UTC) - timedelta(hours=1),
        pgid=pid,
        owner_pid=pid if owner_known else None,
        owner_start_time=birth,
        owner_boot_id=owner_boot_id,
        attempt_pgid=attempt_pgid,
        container_name=f"kew-{item_id}",
        container_id=container_id,
        container_cidfile=container_cidfile,
        container_runtime="docker",
    )


def _admit(store: SqliteRunStore, tmp_path: Path, *, cap: int) -> str:
    return _record_sandbox_start(
        store,
        issue_num=7,
        item_id="7",
        branch="agent/7",
        title="next",
        model="test",
        runner_name="claude-code",
        log_file=tmp_path / "next.log",
        repo="o/r",
        worker_cap=cap,
    )


def _ours(store: SqliteRunStore, run_id: str) -> dict[str, str]:
    ledger = store.read_ledger_id()
    assert ledger is not None
    return {"io.kew.ledger": ledger, "io.kew.run": run_id}


def test_a_dead_runs_never_started_container_is_removed_and_its_slot_freed(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    store.save(_dead_row())
    daemon.add(
        CREATED, "kew-42", "created", _ours(store, "dead-run"), "docker-engine-1"
    )

    assert store.sweep_dead_pending() == 1

    row = store.get("dead-run")
    assert row is not None
    assert row.status != "pending"
    assert row.error_category == "container_start_failed"
    assert daemon.containers == {}
    assert ["docker", "rm", CREATED] in daemon.calls
    [event] = store.list_audit_events(event=REAPED, run_id="dead-run")
    assert event.payload["container_id"] == CREATED
    assert set(event.payload) == REAP_FIELDS
    # The slot is free: a fleet capped at one admits the next run.
    assert _admit(store, tmp_path, cap=1)
    # A second reconcile finds nothing more to do and audits nothing more.
    assert store.sweep_dead_pending() == 0
    assert len(store.list_audit_events(event=REAPED)) == 1


def test_admission_itself_reaps_the_never_started_run_holding_the_last_slot(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    store.save(_dead_row(container_id=None))
    daemon.add("d" * 64, "kew-42", "created", _ours(store, "dead-run"))

    assert _admit(store, tmp_path, cap=1)

    row = store.get("dead-run")
    assert row is not None
    assert row.error_category == "container_start_failed"
    assert daemon.containers == {}


def test_a_dead_run_whose_container_never_came_to_exist_is_reaped(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    store.save(_dead_row(container_id=None))

    assert store.sweep_dead_pending() == 1

    row = store.get("dead-run")
    assert row is not None
    assert row.error_category == "container_start_failed"
    [event] = store.list_audit_events(event=REAPED, run_id="dead-run")
    assert event.payload["container_id"] is None


@pytest.mark.parametrize(
    "labels",
    [None, {"io.kew.ledger": "another-ledger", "io.kew.run": "dead-run"}],
    ids=["unlabelled", "another-ledger"],
)
def test_a_container_found_by_name_without_this_runs_labels_is_kept(
    tmp_path: Path,
    daemon: FakeDaemon,
    store: SqliteRunStore,
    labels: dict[str, str] | None,
) -> None:
    store.save(_dead_row(container_id=None))
    daemon.add("e" * 64, "kew-42", "created", labels)

    assert store.sweep_dead_pending() == 0

    row = store.get("dead-run")
    assert row is not None
    assert row.status == "pending"
    assert list(daemon.containers) == ["e" * 64]
    with pytest.raises(WorkerCapReached):
        _admit(store, tmp_path, cap=1)


def test_a_container_whose_labels_cannot_be_read_is_kept_with_that_reason(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    store.save(_dead_row(container_id=None))
    daemon.add("e" * 64, "kew-42", "created", _ours(store, "dead-run"))
    daemon.labels_unreadable = True
    record = store.get("dead-run")
    assert record is not None

    verdict = never_started_verdict(
        record, datetime.now(UTC), ledger_id=store.read_ledger_id()
    )

    assert verdict == "container labels unreadable"
    assert store.sweep_dead_pending() == 0
    assert list(daemon.containers) == ["e" * 64]
    assert store.list_audit_events(event=REAPED) == []


#: The instant the liveness reconciler's clock is held at, where a case needs
#: a start that is a fixed distance from "now" however long the run takes.
_PINNED = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class _PinnedClock(datetime):
    """The reconciler's clock (its module's ``datetime``), held at _PINNED."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        return _PINNED if tz is None else _PINNED.astimezone(tz)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"started_at": _PINNED - timedelta(seconds=30)}, id="in-grace"),
        pytest.param(
            {"started_at": _PINNED - timedelta(hours=1), "attempt_pgid": os.getpgrp()},
            id="attempt-live",
        ),
    ],
)
def test_a_row_outside_the_never_started_rule_is_retained(
    tmp_path: Path,
    daemon: FakeDaemon,
    store: SqliteRunStore,
    row: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kew.run_store_liveness.datetime", _PinnedClock)
    store.save(_dead_row(**row))
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))

    store.sweep_dead_pending()

    record = store.get("dead-run")
    assert record is not None
    assert record.status == "pending"
    assert list(daemon.containers) == [CREATED]
    assert store.list_audit_events(event=REAPED) == []


def test_an_owner_of_unknown_liveness_is_retained_and_settle_refuses_it(
    daemon: FakeDaemon, managed: SqliteRunStore
) -> None:
    managed.save(_dead_row(owner_known=False))
    daemon.add(CREATED, "kew-42", "created", _ours(managed, "dead-run"))

    assert managed.sweep_dead_pending() == 0
    result = CliRunner().invoke(app, ["settle", "dead-run"])

    assert result.exit_code != 0
    assert "owner liveness is unknown" in result.output
    record = managed.get("dead-run")
    assert record is not None
    assert record.status == "pending"
    assert list(daemon.containers) == [CREATED]
    assert managed.list_audit_events(event=REAPED) == []


def test_settle_reaps_a_proven_dead_never_started_run_and_audits_the_operator(
    daemon: FakeDaemon, managed: SqliteRunStore
) -> None:
    managed.save(_dead_row())
    daemon.add(CREATED, "kew-42", "created", _ours(managed, "dead-run"))

    result = CliRunner().invoke(app, ["settle", "dead-run"])

    assert result.exit_code == 0, result.output
    assert "container_start_failed" in result.output
    record = managed.get("dead-run")
    assert record is not None
    assert record.error_category == "container_start_failed"
    assert daemon.containers == {}
    [event] = managed.list_audit_events(event=REAPED)
    assert (event.actor_type, event.actor_id) == ("human", "operator")
    assert set(event.payload) == REAP_FIELDS


def test_a_stopped_container_frees_its_slot_only_once_kew_has_removed_it(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """A stopped container still exists, so it frees nothing by itself (#3009).

    Its owner and its registered client group are proven dead, so kew removes
    it by id, without force; the slot is freed by that removal. A daemon that
    refuses the removal keeps the run, and its slot, as they were.
    """
    store.save(_dead_row(attempt_pgid=_exited_owner()[0]))
    daemon.add(CREATED, "kew-42", "exited", _ours(store, "dead-run"))
    daemon.rm_refused = True

    with pytest.raises(WorkerCapReached):
        _admit(store, tmp_path, cap=1)
    record = store.get("dead-run")
    assert record is not None
    assert record.status == "pending"
    assert list(daemon.containers) == [CREATED]

    daemon.rm_refused = False
    assert _admit(store, tmp_path, cap=1)
    assert daemon.containers == {}
    record = store.get("dead-run")
    assert record is not None
    assert record.status != "pending"


def test_a_dead_never_started_run_does_not_supersede_a_queued_run_request(
    daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """The Run-request fence reconciles liveness first (#3010 F2)."""
    store.save(_dead_row())
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))
    queued_at = datetime.now(UTC)
    request = RunRequest(
        id="req-1",
        command="kew dispatch 42",
        operator="tom",
        requested_at=queued_at.isoformat(),
        source="console",
        state=QUEUED,
    )
    store.set_meta(request.key, request.encode())

    claimed = claim(store, request, drive="drive-1", host="host", log="drive.log")

    assert claimed is not None
    assert claimed.state != SUPERSEDED
    record = store.get("dead-run")
    assert record is not None
    assert record.error_category == "container_start_failed"


def test_a_failed_creates_stderr_reaches_the_run_and_its_hold_redacted(
    tmp_path: Path,
    daemon: FakeDaemon,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime's own words travel with the failure, bounded (#2913).

    The daemon refuses the create and makes nothing, so the run is finalized
    as a container start failure whose recorded reason, and the failed node's
    stuck reason, carry docker's stderr with credentials redacted.
    """
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: False
    )
    secret = "sk-ant-api03-" + "a" * 40
    daemon.create_refusal = (
        "Error response from daemon: failed to create shim task: OCI runtime "
        f"create failed: key {secret}\nAuthorization: Bearer hunter2-token\n"
        + "x"
        * 1000
    )
    run_id = _record_sandbox_start(
        store,
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        title="t",
        model="test",
        runner_name="claude-code",
        log_file=tmp_path / "run.log",
        repo="o/r",
    )
    argv = ["docker", "run", "--name", "kew-1010", "--cidfile"]
    argv += [str(tmp_path / "run.cid"), "image"]

    with pytest.raises(ContainerStartFailed, match="'kew-1010'") as raised:
        prepare_registered_container(argv, store, run_id)
    _finalize_launch_marker_failure(raised.value, store, run_id, "test", None)

    row = store.get(run_id)
    assert row is not None
    assert row.error_category == "container_start_failed"
    [failure] = store.list_audit_events(event="sandbox_execution_failed")
    reason = failure.payload["reason"]
    assert "OCI runtime create failed" in reason
    assert secret not in reason
    assert "hunter2-token" not in reason
    assert len(reason) < 500
    result = _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010)}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        now=datetime.now(UTC),
    )
    [stuck] = [s for s in result.stuck if s.node_id == "1010"]
    assert "OCI runtime create failed" in stuck.reason
    assert secret not in stuck.reason


def test_one_tick_reaps_a_never_started_node_so_it_no_longer_reads_progressing(
    tmp_path: Path,
    daemon: FakeDaemon,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver's reconcile frees the slot the #3124 epic held for 42 h."""
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: False
    )
    assert store.reserve_epic_node_item(EPIC, "1010", "run-1010") is True
    store.save(_dead_row("run-1010", item_id="1010", started_at=NOW))
    daemon.add(CREATED, "kew-1010", "created", _ours(store, "run-1010"))

    result = _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010)}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        now=NOW + timedelta(hours=1),
    )

    row = store.get("run-1010")
    assert row is not None
    assert row.error_category == "container_start_failed"
    assert daemon.containers == {}
    assert store.list_epic_reservations(EPIC) == []
    assert "1010" in result.failed
    assert "1010" not in result.in_flight
    assert result.signal.value != "progressing"
    # The tick writes the same reap event, with the same fields, as admission
    # and kew settle do, after the intent naming the container.
    [event] = store.list_audit_events(event=REAPED, run_id="run-1010")
    assert set(event.payload) == REAP_FIELDS
    assert event.payload["container_id"] == CREATED
    assert event.payload["epic_ref"] == EPIC
    assert (event.actor_type, event.actor_id) == ("policy", "driver_reconcile")
    [intent] = store.list_audit_events(event=INTENDED, run_id="run-1010")
    assert intent.id < event.id
    assert store.list_audit_events(event="orphaned_run_reaped", run_id="run-1010") == []


def test_a_remotely_placed_run_is_retained_though_this_host_sees_no_container(
    daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """The local runtime cannot see a remote host's container, so its absence
    here proves nothing: the row, its slot and its reservation stay."""
    store.save(_dead_row(container_id=None))
    assert store.set_run_site("dead-run", "aws", "i-123")
    record = store.get("dead-run")
    assert record is not None

    verdict = never_started_verdict(
        record, datetime.now(UTC), ledger_id=store.read_ledger_id()
    )

    assert verdict == "the run is placed on another host"
    assert store.sweep_dead_pending() == 0
    kept = store.get("dead-run")
    assert kept is not None
    assert kept.status == "pending"
    assert store.list_audit_events(event=REAPED) == []


def _kept(store: SqliteRunStore, daemon: FakeDaemon, cid: str) -> None:
    """The run still holds its row and its slot, and its container is untouched."""
    record = store.get("dead-run")
    assert record is not None
    assert record.status == "pending"
    assert cid in daemon.containers
    assert not any(call[1] == "rm" for call in daemon.calls)
    assert store.list_audit_events(event=REAPED) == []
    assert store.list_audit_events(event=INTENDED) == []


@pytest.mark.parametrize("found_by", ["name", "cidfile"])
def test_a_container_labelled_as_another_run_of_this_ledger_is_never_reaped(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore, found_by: str
) -> None:
    """A newer run of the same item owns the ``kew-<item>`` name now.

    Its container is this ledger's, but its run label names that newer run,
    so the dead run may not remove it: not when found by name, and not when
    the dead run's own cidfile names that id (a reused cidfile path).
    """
    cidfile = tmp_path / "dead.cid"
    cidfile.write_text("e" * 64, encoding="utf-8")
    store.save(
        _dead_row(
            container_id=None,
            container_cidfile=str(cidfile) if found_by == "cidfile" else None,
        )
    )
    daemon.add("e" * 64, "kew-42", "created", _ours(store, "newer-run"))

    assert store.sweep_dead_pending() == 0

    _kept(store, daemon, "e" * 64)


def test_an_exited_container_whose_run_never_registered_an_attempt_is_kept(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """Pid 0 is not enough: only ``created`` means the container never started.

    An ``exited`` one ran something, so with no attempt on record nothing
    proves what it ran or that it is safe to remove.
    """
    store.save(_dead_row())
    daemon.add(CREATED, "kew-42", "exited", _ours(store, "dead-run"))

    assert store.sweep_dead_pending() == 0

    _kept(store, daemon, CREATED)


@pytest.mark.parametrize("container_id", [CREATED, None], ids=["by-id", "by-name"])
def test_a_daemon_that_cannot_answer_keeps_the_run(
    tmp_path: Path,
    daemon: FakeDaemon,
    store: SqliteRunStore,
    container_id: str | None,
) -> None:
    """Only the runtime saying "No such container" proves a container absent."""
    store.save(_dead_row(container_id=container_id))
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))
    daemon.inspect_refusal = (
        "Cannot connect to the Docker daemon. Is the docker daemon running?"
    )

    assert store.sweep_dead_pending() == 0

    _kept(store, daemon, CREATED)


def test_the_removal_is_recorded_before_the_container_is_removed(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """Write-ahead, as the stale-name removal does (#2973): the intent naming
    the container, its runtime and its run is on the chain when ``rm`` runs."""
    store.save(_dead_row())
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))
    at_removal: list[Any] = []
    daemon.on_rm = lambda cid: at_removal.extend(
        store.list_audit_events(event=INTENDED, run_id="dead-run")
    )

    assert store.sweep_dead_pending() == 1

    [intent] = at_removal
    assert intent.payload == {
        "container_id": CREATED,
        "container_name": "kew-42",
        "runtime": "docker",
        "state": "created",
        "rule": "never_started",
    }
    [reaped] = store.list_audit_events(event=REAPED, run_id="dead-run")
    assert intent.id < reaped.id


def test_a_removal_whose_run_finished_meanwhile_is_recorded_and_settle_says_so(
    daemon: FakeDaemon, managed: SqliteRunStore
) -> None:
    """The container is gone but the row moved under kew: no reap, and no
    "nothing changed". The intent and the removal stay on the chain."""
    record = _dead_row()
    managed.save(record)
    daemon.add(CREATED, "kew-42", "created", _ours(managed, "dead-run"))

    def finish_elsewhere(cid: str) -> None:
        record.status = "failure"
        managed.save(record)

    daemon.on_rm = finish_elsewhere

    result = CliRunner().invoke(app, ["settle", "dead-run"])

    assert result.exit_code != 0
    assert f"container {CREATED} was removed" in result.output
    assert "nothing changed" not in result.output
    assert daemon.containers == {}
    row = managed.get("dead-run")
    assert row is not None
    assert row.status == "failure"
    assert managed.list_audit_events(event=REAPED) == []
    [intent] = managed.list_audit_events(event=INTENDED, run_id="dead-run")
    [removed] = managed.list_audit_events(event=REMOVED, run_id="dead-run")
    assert intent.payload["container_id"] == removed.payload["container_id"]
    assert removed.payload["run_finalized"] is False


def test_a_stopped_containers_removal_is_recorded_before_it_is_made(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """A dead run's exited container, removed on the admission path, is
    named on the chain before ``rm``, with the state it was in, and after."""
    store.save(_dead_row(attempt_pgid=_exited_owner()[0]))
    daemon.add(CREATED, "kew-42", "exited", _ours(store, "dead-run"))
    at_removal: list[Any] = []
    daemon.on_rm = lambda cid: at_removal.extend(
        store.list_audit_events(event=INTENDED, run_id="dead-run")
    )

    assert _admit(store, tmp_path, cap=1)

    assert daemon.containers == {}
    [intent] = at_removal
    assert intent.payload == {
        "container_id": CREATED,
        "container_name": "kew-42",
        "runtime": "docker",
        "state": "exited",
        "rule": "stopped_after_attempt",
    }
    [removed] = store.list_audit_events(event=REMOVED, run_id="dead-run")
    assert removed.payload == intent.payload
    assert intent.id < removed.id


@pytest.fixture
def this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the machine id: the host running the tests may have none (#2812)."""
    machine_id = tmp_path / "pinned-machine-id"
    machine_id.write_text("c" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (machine_id,))
    process_groups._this_machine.cache_clear()
    yield
    process_groups._this_machine.cache_clear()


@pytest.mark.usefixtures("this_machine")
def test_an_owner_recorded_on_another_machine_is_unknown_and_kept(
    daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """This machine's process table says nothing about another machine's pids.

    The owner's pid is gone from it, but the row says the owner ran
    elsewhere, so its liveness is unknown and nothing is removed.
    """
    own = process_groups.machine_boot()
    assert own is not None, "Linux and macOS both say their machine and boot"
    other = "7" * 32 if not own.startswith(f"machine-id:{'7' * 32}") else "6" * 32
    store.save(_dead_row(owner_boot_id=f"machine-id:{other}/{'8' * 32}"))
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))
    record = store.get("dead-run")
    assert record is not None

    verdict = never_started_verdict(
        record, datetime.now(UTC), ledger_id=store.read_ledger_id()
    )

    assert verdict == "owner liveness is unknown"
    assert store.sweep_dead_pending() == 0
    _kept(store, daemon, CREATED)


def test_a_stopped_run_whose_container_never_started_stays_killed(
    daemon: FakeDaemon, managed: SqliteRunStore
) -> None:
    """``kew kill`` recorded its stop before the owner died: the reap keeps
    ``killed`` and says the run was stopped."""
    managed.save(_dead_row())
    managed.request_run_stop(
        "dead-run",
        label_reset={
            "repo": "o/r",
            "tracker": "local",
            "ready": "ready",
            "in_progress": "in-progress",
            "agent_working": "agent-working",
            "runtime": "docker",
        },
    )
    daemon.add(CREATED, "kew-42", "created", _ours(managed, "dead-run"))

    result = CliRunner().invoke(app, ["settle", "dead-run"])

    assert result.exit_code == 0, result.output
    assert "it had been stopped" in result.output
    assert "finalized as killed" in result.output
    row = managed.get("dead-run")
    assert row is not None
    assert row.error_category == "killed"
    [event] = managed.list_audit_events(event=REAPED, run_id="dead-run")
    assert event.payload["error_category"] == "killed"
    assert event.payload["reason"].startswith("Stopped")


def _launched_on(store: SqliteRunStore, run_id: str, daemon: str) -> None:
    """What a run's launch records: the daemon its container is created on."""
    store.set_meta(container_daemon_key(run_id), daemon)


@pytest.mark.parametrize(
    "container_id", [CREATED, None], ids=["recorded-id", "found-by-name"]
)
def test_absence_from_another_daemon_than_the_runs_keeps_the_run(
    tmp_path: Path,
    daemon: FakeDaemon,
    store: SqliteRunStore,
    container_id: str | None,
) -> None:
    """The run's container lives on the daemon its launch recorded. After a
    context switch the runtime answers from another daemon, which says "No
    such container" about any id: that proves nothing, so nothing is reaped."""
    store.save(_dead_row(container_id=container_id))
    daemon.add(
        CREATED, "kew-42", "created", _ours(store, "dead-run"), "docker-engine-1"
    )
    first = current_daemon_identity("docker")
    assert first is not None
    _launched_on(store, "dead-run", first)
    daemon.engine = "engine-2"
    record = store.get("dead-run")
    assert record is not None

    verdict = never_started_verdict(
        record, datetime.now(UTC), ledger_id=store.read_ledger_id(), daemon=first
    )

    assert verdict == (
        "the runtime answers from another daemon than the one the run used"
    )
    assert store.sweep_dead_pending() == 0
    _kept(store, daemon, CREATED)


def test_absence_from_a_daemon_that_cannot_be_named_keeps_the_run(
    daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    store.save(_dead_row(container_id=None))
    first = current_daemon_identity("docker")
    assert first is not None
    _launched_on(store, "dead-run", first)
    daemon.info_refused = True

    assert store.sweep_dead_pending() == 0

    record = store.get("dead-run")
    assert record is not None
    assert record.status == "pending"
    assert store.list_audit_events(event=REAPED) == []


def test_absence_from_the_runs_own_daemon_still_reaps(
    daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """The same answer from the recorded daemon is proof, as it always was."""
    store.save(_dead_row(container_id=None))
    first = current_daemon_identity("docker")
    assert first is not None
    _launched_on(store, "dead-run", first)

    assert store.sweep_dead_pending() == 1

    [event] = store.list_audit_events(event=REAPED, run_id="dead-run")
    assert event.payload["state"] == "absent"


def test_a_stopped_runs_container_gone_from_another_daemon_keeps_the_run(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """The stopped-container rule trusts absence only from the run's daemon."""
    store.save(_dead_row(attempt_pgid=_exited_owner()[0]))
    daemon.add(CREATED, "kew-42", "exited", _ours(store, "dead-run"))
    first = current_daemon_identity("docker")
    assert first is not None
    _launched_on(store, "dead-run", first)
    daemon.engine = "engine-2"

    with pytest.raises(WorkerCapReached):
        _admit(store, tmp_path, cap=1)

    record = store.get("dead-run")
    assert record is not None
    assert record.status == "pending"
    assert list(daemon.containers) == [CREATED]


def test_a_launch_records_the_daemon_it_creates_its_container_on(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    run_id = _admit(store, tmp_path, cap=2)
    argv = ["docker", "run", "--name", "kew-7", "--cidfile"]
    argv += [str(tmp_path / "run.cid"), "image"]

    prepare_registered_container(argv, store, run_id)

    assert store.get_meta(container_daemon_key(run_id)) == (
        current_daemon_identity("docker")
    )


def _owning_continuation(store: SqliteRunStore, tmp_path: Path) -> None:
    """``dead-run`` is the claimed continuation run of preserved work."""
    worktree = tmp_path / "preserved"
    args = ("epic:x", "42", "original-run", "agent/issue-42", worktree)
    store.record_continuation(
        *args, checkout_generation=fixture_checkout_generation(worktree)
    )
    claimed = store.claim_continuation_under_cap(
        *args,
        "dead-run",
        5,
        claim_evidence=fixture_claim_evidence(store, *args, "dead-run"),
    )
    assert claimed == "claimed"


def test_admission_never_reaps_a_run_that_owns_a_continuation(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """The epic tick owns a continuation's recovery; admission leaves it."""
    _owning_continuation(store, tmp_path)
    store.save(_dead_row())
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))

    assert store.sweep_dead_pending() == 0

    _kept(store, daemon, CREATED)


def test_settle_refuses_a_run_that_owns_a_continuation_and_names_the_way_out(
    tmp_path: Path, daemon: FakeDaemon, managed: SqliteRunStore
) -> None:
    _owning_continuation(managed, tmp_path)
    managed.save(_dead_row())
    daemon.add(CREATED, "kew-42", "created", _ours(managed, "dead-run"))

    result = CliRunner().invoke(app, ["settle", "dead-run"])

    assert result.exit_code == 2, result.output
    output = " ".join(result.output.split())
    assert "owns continuation epic:x/42 (continuing)" in output
    assert "kew epic continue epic:x 42" in output
    assert "kew epic abandon epic:x 42" in output
    _kept(managed, daemon, CREATED)


class _LockWatchingConnection:
    """The store's connection, noting whether the store's lock is held for
    each statement. A store shared across threads (``check_same_thread=False``)
    is only safe when no statement runs on it outside that lock."""

    def __init__(self, store: SqliteRunStore) -> None:
        self.conn = store._conn
        self._locks = (store._txn_lock, store._write_lock)
        self.unlocked: list[str] = []

    def execute(self, sql: str, *args: Any) -> Any:
        if not any(_held(lock) for lock in self._locks):
            self.unlocked.append(" ".join(sql.split()))
        return self.conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


def _held(lock: Any) -> bool:
    """Whether this thread holds *lock* (an RLock says so; a Lock says only
    that someone does, which on one thread is this one)."""
    owned = getattr(lock, "_is_owned", None)
    return owned() if owned is not None else lock.locked()


def test_admission_reads_nothing_outside_the_stores_lock(
    tmp_path: Path, daemon: FakeDaemon, store: SqliteRunStore
) -> None:
    """The reaper's reads (ledger, recorded daemon, stop intent, continuation)
    run before admission's transaction, on a connection a TUI or a server
    shares across threads. Unlocked, one interleaves with another thread's
    statement and sqlite3 raises "bad parameter or other API misuse"."""
    for i in range(2):
        store.save(
            RunRecord(
                run_id=f"live-{i}",
                issue_num=100 + i,
                branch=f"agent/issue-{100 + i}",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )
    store.save(_dead_row())
    daemon.add(CREATED, "kew-42", "created", _ours(store, "dead-run"))
    first = current_daemon_identity("docker")
    assert first is not None
    _launched_on(store, "dead-run", first)
    store.save(
        _dead_row(
            "stopped-run",
            item_id="43",
            container_id="d" * 64,
            attempt_pgid=_exited_owner()[0],
        )
    )
    daemon.add("d" * 64, "kew-43", "exited", _ours(store, "stopped-run"))
    watching = _LockWatchingConnection(store)
    store._conn = watching  # type: ignore[assignment]
    try:
        reaped = store.sweep_dead_pending()
    finally:
        store._conn = watching.conn

    assert reaped == 2
    assert watching.unlocked == []
