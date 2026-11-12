"""A container left ``created`` must not block its item's later launches (#2973).

A sandboxed launch that created ``kew-<item>`` and then failed to start it
left a never-started container holding the name, so every later create for
the item was refused in seconds and read as a 0-token exception.
"""

from __future__ import annotations

import sqlite3
import subprocess
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from support.fake_daemon import FakeDaemon

from kew import sandbox
from kew.config import DispatchConfig, RetryConfig
from kew.dispatch_core import _apply_completion_status, run_tracked_agent
from kew.dispatch_sandbox import (
    _cleanup_container_identity,
    _create_registered_sidecar,
    _CreatedContainerHandle,
    _finalize_launch_marker_failure,
    _own_container_launch,
    _owned_sidecar_context,
    _prepare_owned_container,
    _record_sandbox_start,
    container_launch_failure,
    launch_error_category,
    prepare_registered_container,
)
from kew.graph.driver.review_launcher import sandbox_reviewer_launcher
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore
from kew.sandbox import (
    ContainerHandle,
    ContainerStartFailed,
    capture_container_launch,
    launch_container,
)
from kew.switchyard_supervisor import SwitchyardSupervisorError
from kew.tracking import DispatchResult, startup_refused

STALE = "5" * 64


@pytest.fixture(
    params=["No such container", "no such object"], ids=["docker", "podman"]
)
def daemon(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> FakeDaemon:
    fake = FakeDaemon(request.param)
    monkeypatch.setattr("kew.sandbox.subprocess_utils.run", fake.run)
    monkeypatch.setattr("kew.dispatch_sandbox._run", fake.run)
    # Stop recovery reads a recorded container's state straight off the runtime.
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


def _store(root: Path, cls: type[SqliteRunStore] = SqliteRunStore) -> SqliteRunStore:
    """A ledger with its own identity, as every project's store has."""
    root.mkdir(parents=True, exist_ok=True)
    return cls(str(root / "runs.db"), project_root=root)


def _argv(tmp_path: Path, attempt: str, name: str = "kew-42") -> list[str]:
    return [
        "docker",
        "run",
        "--name",
        name,
        "--cidfile",
        str(tmp_path / f"{attempt}.cid"),
        "image",
    ]


def _admit(store: SqliteRunStore, tmp_path: Path) -> str:
    return _record_sandbox_start(
        store,
        issue_num=42,
        item_id="42",
        branch="agent/42",
        title="test",
        model="test",
        runner_name="claude-code",
        log_file=tmp_path / "run.log",
        repo="o/r",
        worker_cap=4,
    )


def _finish(store: SqliteRunStore, run_id: str) -> None:
    record = store.get(run_id)
    assert record is not None
    record.status = "failed"
    store.save(record)


def _created(store: SqliteRunStore, tmp_path: Path, attempt: str) -> tuple[str, str]:
    """One run of *store* creates ``kew-42``; returns (run id, container id)."""
    run_id = _admit(store, tmp_path)
    execution = prepare_registered_container(_argv(tmp_path, attempt), store, run_id)
    assert execution.container_id is not None
    return run_id, execution.container_id


def _left_behind(store: SqliteRunStore, tmp_path: Path) -> str:
    """A run of *store* created ``kew-42`` and ended without starting it."""
    run_id, container_id = _created(store, tmp_path, "left")
    _finish(store, run_id)
    return container_id


def test_a_failed_start_leaves_no_container_and_the_next_launch_succeeds(
    tmp_path: Path, daemon: FakeDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_start(*_: object, **__: object) -> None:
        raise OSError("start refused")

    monkeypatch.setattr("kew.sandbox.subprocess_utils.popen", refuse_start)
    with _store(tmp_path / "project") as store:
        first = _admit(store, tmp_path)
        argv = _argv(tmp_path, "first")
        handle = _CreatedContainerHandle(
            "docker", "kew-42", cidfile=Path(argv[5]), owned=False
        )
        prepare = partial(_prepare_owned_container, handle, store=store, run_id=first)
        with (
            pytest.raises(OSError, match="start refused") as raised,
            _own_container_launch(handle),
        ):
            launch_container(argv, prepare=prepare)
        assert daemon.containers == {}
        _finalize_launch_marker_failure(raised.value, store, first, "test", None)
        record = store.get(first)
        assert record is not None
        assert record.status != "pending"
        assert record.error_category == "container_start_failed"

        second = _admit(store, tmp_path)
        execution = prepare_registered_container(
            _argv(tmp_path, "second"), store, second
        )
        assert list(daemon.containers) == [execution.container_id]
        assert store.list_audit_events(event="stale_container_removed") == []


def test_a_start_that_exits_1_fails_as_a_container_start_and_leaves_nothing(
    tmp_path: Path, daemon: FakeDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``docker start`` refusing with 1 leaves its container ``created``."""

    def start_exits_1(argv: list[str], **_: Any) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["/bin/sh", "-c", "exit 1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    monkeypatch.setattr("kew.sandbox.subprocess_utils.popen", start_exits_1)
    with _store(tmp_path / "project") as store:
        run_id, container_id = _created(store, tmp_path, "first")
        start = ["docker", "start", "--attach", "--interactive", container_id]
        handle = launch_container(
            start, container_name="kew-42", container_id=container_id
        )
        result = DispatchResult(-1, 42, tmp_path / "run.log")
        assert list(handle) == []
        result.exit_code = handle.wait(cleanup_identity=False)
        capture_container_launch(handle, result)
        assert result.error_category == "sandbox_startup_refused"
        _cleanup_container_identity(handle, True, result)
        assert daemon.containers == {}
        assert result.launched is False
        assert result.error_category == "container_start_failed"
        assert "'kew-42'" in result.stop_reason
        assert startup_refused(result)
        record = store.get(run_id)
        assert record is not None
        _apply_completion_status(record, result, 1)
        assert record.status == "failure"


def _exited_handle(verb: str) -> ContainerHandle:
    """The handle of a ``docker <verb>`` client that has already exited."""
    handle = ContainerHandle(
        subprocess.Popen(["true"]),
        "kew-42",
        "docker",
        container_id=STALE,
        launch_argv=["docker", verb, STALE],
    )
    handle._proc.wait()
    return handle


def test_a_start_the_client_could_not_make_removes_its_own_container(
    daemon: FakeDaemon,
) -> None:
    daemon.add(STALE, "kew-42", "created")
    assert _exited_handle("start").remove_if_never_started()
    assert daemon.containers == {}


def test_a_running_container_is_not_removed_as_never_started(
    daemon: FakeDaemon,
) -> None:
    daemon.add(STALE, "kew-42", "running")
    assert not _exited_handle("start").remove_if_never_started()
    assert list(daemon.containers) == [STALE]


def test_an_exec_into_a_worker_never_removes_that_worker(
    daemon: FakeDaemon,
) -> None:
    """A sidecar worker belongs to its supervisor, even when it never started."""
    daemon.add(STALE, "kew-42", "created")
    assert not _exited_handle("exec").remove_if_never_started()
    assert list(daemon.containers) == [STALE]


def test_a_stale_container_of_a_dead_run_here_is_removed_and_audited(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    with _store(tmp_path / "project") as store:
        stale = _left_behind(store, tmp_path)
        run_id = _admit(store, tmp_path)
        execution = prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        assert stale not in daemon.containers
        assert list(daemon.containers) == [execution.container_id]
        payload = {
            "container_name": "kew-42",
            "container_id": stale,
            "state": "created",
            "runtime": "docker",
            "daemon": "docker:" + " ".join(["docker-engine-1"] * 3),
        }
        for event in ("stale_container_removal_intended", "stale_container_removed"):
            events = store.list_audit_events(event=event)
            assert [(e.run_id, e.payload) for e in events] == [(run_id, payload)]


@pytest.mark.parametrize("finished", [False, True])
def test_another_projects_container_is_never_removed(
    tmp_path: Path, daemon: FakeDaemon, finished: bool
) -> None:
    """``kew-<item>`` is host-wide; only the ledger that made it may reclaim it."""
    with (
        _store(tmp_path / "a") as project_a,
        _store(tmp_path / "b") as project_b,
    ):
        a_run, a_container = _created(project_a, tmp_path, "a")
        if finished:
            _finish(project_a, a_run)
        b_run = _admit(project_b, tmp_path)
        with pytest.raises(ContainerStartFailed, match="'kew-42'"):
            prepare_registered_container(_argv(tmp_path, "b"), project_b, b_run)
        assert list(daemon.containers) == [a_container]
        assert project_b.list_audit_events(event="stale_container_removed") == []


@pytest.mark.parametrize("labels", ["none", "other-ledger"])
def test_a_container_this_ledger_did_not_label_is_never_removed(
    tmp_path: Path, daemon: FakeDaemon, labels: str
) -> None:
    """A dead run's id is not enough: the ledger label must be this one's."""
    with _store(tmp_path / "project") as store:
        dead = _admit(store, tmp_path)
        _finish(store, dead)
        daemon.add(
            STALE,
            "kew-42",
            "created",
            None
            if labels == "none"
            else {"io.kew.ledger": "another-ledger", "io.kew.run": dead},
        )
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerStartFailed, match="'kew-42'"):
            prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        assert list(daemon.containers) == [STALE]


@pytest.mark.parametrize("status", ["running", "owned"])
def test_a_held_name_is_never_freed_and_fails_as_a_container_start(
    tmp_path: Path, daemon: FakeDaemon, status: str
) -> None:
    with _store(tmp_path / "project") as store:
        if status == "owned":
            _, held = _created(store, tmp_path, "owner")
        else:
            held = STALE
            daemon.add(STALE, "kew-42", "running")
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerStartFailed, match="'kew-42'") as raised:
            prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        assert list(daemon.containers) == [held]
        assert store.list_audit_events(event="stale_container_removed") == []
        _finalize_launch_marker_failure(raised.value, store, run_id, "test", None)
        record = store.get(run_id)
        assert record is not None
        assert record.status != "pending"
        assert record.error_category == "container_start_failed"


def test_a_create_whose_client_failed_is_not_certified_clean_and_stays_held(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    """The daemon may have made the container, so cleanup is left unconfirmed.

    A container under the name that does not carry this run's labels is not
    provably this create's, so it is read and never removed (#3007).
    """
    daemon.create_response_lost = True
    daemon.unlabelled = True
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerStartFailed, match="'kew-42'") as raised:
            prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        evidence = container_launch_failure(raised.value)
        assert evidence is not None
        assert evidence.cleanup_confirmed is False
        # Nothing is stopped or removed by a name that may not be ours.
        assert [
            call[1] for call in daemon.calls if call[1] not in {"info", "inspect"}
        ] == ["create"]
        assert [c["name"] for c in daemon.containers.values()] == ["kew-42"]
        _finalize_launch_marker_failure(raised.value, store, run_id, "test", None)
        record = store.get(run_id)
        assert record is not None
        assert record.status == "pending"
        assert record.container_name == "kew-42"


def test_a_create_committed_under_this_runs_labels_is_removed_and_relaunches(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    """A create the daemon committed while its client exited 1 (#3007).

    The container under the name carries this ledger's and this run's labels,
    so it is this create's own: its id is persisted, it is removed by that id,
    and the create runs once more (#3246). Here the daemon loses that answer
    too, so the second container goes the same way, the run fails as a
    container start, and the item's next launch succeeds.
    """
    daemon.create_response_lost = True
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerStartFailed, match="'kew-42'") as raised:
            prepare_registered_container(_argv(tmp_path, "lost"), store, run_id)
        first, second_create = f"{1:064x}", f"{2:064x}"
        record = store.get(run_id)
        assert record is not None
        assert record.container_id == second_create
        assert daemon.containers == {}
        removals = [call[1:] for call in daemon.calls if call[1] == "rm"]
        assert removals == [["rm", first], ["rm", second_create]]
        _finalize_launch_marker_failure(raised.value, store, run_id, "test", None)
        record = store.get(run_id)
        assert record is not None
        assert record.status != "pending"
        assert record.error_category == "container_start_failed"

        daemon.create_response_lost = False
        second = _admit(store, tmp_path)
        execution = prepare_registered_container(
            _argv(tmp_path, "second"), store, second
        )
        assert list(daemon.containers) == [execution.container_id]


def test_a_stale_container_another_remover_took_first_still_frees_the_name(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    with _store(tmp_path / "project") as store:
        stale = _left_behind(store, tmp_path)
        daemon.rm_raced = True
        run_id = _admit(store, tmp_path)
        execution = prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        assert list(daemon.containers) == [execution.container_id]
        removed = store.list_audit_events(event="stale_container_removed")
        assert [e.payload["container_id"] for e in removed] == [stale]


class _AuditFailsOnce(SqliteRunStore):
    """A ledger whose next write of each named event is lost once."""

    failing: set[str] = set()

    def append_audit_event_once(self, **kwargs: Any) -> int | None:
        if kwargs["event"] in self.failing:
            self.failing.discard(kwargs["event"])
            raise sqlite3.OperationalError("disk I/O error")
        return super().append_audit_event_once(**kwargs)


def test_a_removal_whose_audit_was_lost_is_recorded_once_by_a_later_launch(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    with _store(tmp_path / "project", _AuditFailsOnce) as store:
        assert isinstance(store, _AuditFailsOnce)
        stale = _left_behind(store, tmp_path)
        store.failing = {"stale_container_removed"}
        run_id = _admit(store, tmp_path)
        prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        assert stale not in daemon.containers
        assert store.list_audit_events(event="stale_container_removed") == []

        for attempt in ("later", "later-again"):
            later = _admit(store, tmp_path)
            prepare_registered_container(
                _argv(tmp_path, attempt, f"kew-{attempt}"), store, later
            )
        removed = store.list_audit_events(event="stale_container_removed")
        assert [(e.run_id, e.payload["container_id"]) for e in removed] == [
            (run_id, stale)
        ]


def test_a_removal_whose_intent_was_not_recorded_does_not_happen(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    with _store(tmp_path / "project", _AuditFailsOnce) as store:
        assert isinstance(store, _AuditFailsOnce)
        stale = _left_behind(store, tmp_path)
        store.failing = {"stale_container_removal_intended"}
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerStartFailed, match="'kew-42'"):
            prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
        assert list(daemon.containers) == [stale]
        assert store.list_audit_events(event="stale_container_removed") == []


def _removal_left_unconfirmed(
    store: SqliteRunStore, tmp_path: Path, daemon: FakeDaemon
) -> str:
    """A reclaim of ``kew-42`` recorded its intent, then the ``rm`` failed."""
    stale = _left_behind(store, tmp_path)
    daemon.rm_refused = True
    run_id = _admit(store, tmp_path)
    with pytest.raises(ContainerStartFailed, match="'kew-42'"):
        prepare_registered_container(_argv(tmp_path, "next"), store, run_id)
    daemon.rm_refused = False
    assert len(store.list_audit_events(event="stale_container_removal_intended")) == 1
    return stale


def _launch_elsewhere(
    store: SqliteRunStore, tmp_path: Path, attempt: str, runtime: str = "docker"
) -> None:
    argv = _argv(tmp_path, attempt, f"kew-{attempt}")
    argv[0] = runtime
    prepare_registered_container(argv, store, _admit(store, tmp_path))


@pytest.mark.parametrize("switch", ["runtime", "daemon"])
def test_an_intent_is_only_completed_by_the_daemon_that_held_the_container(
    tmp_path: Path, daemon: FakeDaemon, switch: str
) -> None:
    """Another daemon never held the id, so its "no such container" proves nothing."""
    with _store(tmp_path / "project") as store:
        stale = _removal_left_unconfirmed(store, tmp_path, daemon)
        if switch == "runtime":
            _launch_elsewhere(store, tmp_path, "podman-run", "podman")
        else:
            daemon.engine = "engine-2"
            _launch_elsewhere(store, tmp_path, "other-daemon")
        assert stale in daemon.containers
        assert store.list_audit_events(event="stale_container_removed") == []

        daemon.engine = "engine-1"
        _launch_elsewhere(store, tmp_path, "still-there")
        assert store.list_audit_events(event="stale_container_removed") == []
        del daemon.containers[stale]
        _launch_elsewhere(store, tmp_path, "gone-now")
        removed = store.list_audit_events(event="stale_container_removed")
        assert [e.payload["container_id"] for e in removed] == [stale]


class _SidecarWhoseWorkerCannotStart:
    """A sidecar supervisor that creates its worker, then fails to start it.

    Its own teardown (proxy, network, volume) runs only when the launch's
    cleanup confirms the worker is gone, as the real supervisor's does.
    """

    worker_name = "kew-42"

    def __init__(self, daemon: FakeDaemon, cidfile: Path) -> None:
        self.daemon = daemon
        self.cidfile = cidfile
        self.torn_down = False

    def __enter__(self) -> _SidecarWhoseWorkerCannotStart:
        return self

    def __exit__(self, *_: object) -> None:
        self.torn_down = True

    def create_worker(self, argv: list[str], *, on_identity: Any) -> str:
        created = self.daemon.run(
            [*argv, "--name", self.worker_name, "--cidfile", str(self.cidfile), "img"]
        )
        on_identity(created.stdout)
        raise SwitchyardSupervisorError("sidecar worker bootstrap startup failed")


def test_a_sidecar_worker_that_never_started_is_removed_with_its_topology(
    tmp_path: Path, daemon: FakeDaemon
) -> None:
    """The worker's id reaches its cleanup before ``start``, so nothing stays."""
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        context = _SidecarWhoseWorkerCannotStart(daemon, tmp_path / "worker.cid")
        with (
            pytest.raises(SwitchyardSupervisorError) as raised,
            _owned_sidecar_context(context) as supervisor,
        ):
            _create_registered_sidecar(store, run_id, supervisor, ["docker", "create"])
        assert daemon.containers == {}
        assert context.torn_down
        assert launch_error_category(raised.value) == "container_start_failed"
        _finalize_launch_marker_failure(raised.value, store, run_id, "test", None)
        record = store.get(run_id)
        assert record is not None
        assert record.error_category == "container_start_failed"


def _start_refused(failure: str, argv: list[str], **_: Any) -> subprocess.Popen[str]:
    """A ``docker start`` client that exits 1, or that cannot be spawned."""
    if failure == "spawn":
        raise OSError("start refused")
    return subprocess.Popen(
        ["/bin/sh", "-c", "exit 1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.parametrize("failure", ["exit-1", "spawn"])
def test_a_tracked_review_whose_start_fails_leaves_nothing_and_can_relaunch(
    tmp_path: Path,
    daemon: FakeDaemon,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Reviews and remediations launch through ``run_tracked_agent``, not the
    direct sandbox path; a refused start there must clean up the same way."""
    monkeypatch.setattr("kew.orchestrator.popen", partial(_start_refused, failure))
    # The fake daemon is the runtime this host found.
    monkeypatch.setitem(sandbox._RUNTIME_CACHE, "docker", "docker")
    runner = MagicMock()
    runner.name = "claude-code"
    runner.container_args.return_value = []
    config = DispatchConfig(retry=RetryConfig(max_attempts=2, backoff_base_seconds=0))
    with _store(tmp_path / "project") as store:
        for launch in (1, 2):
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                runner=runner,
                model="m",
                contained_image="image",
                store=store,
                run_kind="adversarial_review",
            )
            assert daemon.containers == {}
            assert result.error_category == "container_start_failed"
            assert "'kew-42'" in result.stop_reason
            rows = store.list_by_issue(42)
            assert len(rows) == launch
            assert {(row.status, row.error_category) for row in rows} == {
                ("failure", "container_start_failed")
            }
        # Every attempt created its own container: none was refused the name.
        attempts = 1 if failure == "spawn" else config.retry.max_attempts
        assert daemon.created == 2 * attempts


def _reviewer_ctx(tmp_path: Path) -> ReviewerLaunchContext:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    return ReviewerLaunchContext(
        item_id="42",
        run_id="review-run",
        prompt="review the work",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path / "review",
    )


@pytest.mark.parametrize("failure", ["held-name", "exit-1", "spawn"])
def test_a_reviewer_whose_container_never_started_keeps_that_diagnosis(
    tmp_path: Path,
    daemon: FakeDaemon,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The reviewer's own failure fallback must not relabel a container start
    failure as the generic ``launch_failed``: the category names the container
    an operator has to look at."""
    monkeypatch.setattr(
        "kew.sandbox.subprocess_utils.popen", partial(_start_refused, failure)
    )
    # The fake daemon is the runtime this host found.
    monkeypatch.setitem(sandbox._RUNTIME_CACHE, "docker", "docker")
    if failure == "held-name":
        daemon.add(STALE, "kew-42", "running")
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with _store(tmp_path / "project") as store:
        collected = sandbox_reviewer_launcher(config, store)(_reviewer_ctx(tmp_path))
        assert collected.error_category == "container_start_failed"
        assert collected.failure_layer == "container"
        row = store.get("review-run")
        assert row is not None
        assert (row.status, row.error_category, row.container_name) == (
            "failure",
            "container_start_failed",
            "kew-42",
        )
        held = [STALE] if failure == "held-name" else []
        assert list(daemon.containers) == held
