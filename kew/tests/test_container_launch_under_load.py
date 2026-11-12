"""Container launches on a loaded host (#1963, #3246).

The runtime is a real executable on ``TOOL_PATHS`` (:class:`LaunchDocker`), so
every ``create``, ``inspect``, ``rm`` and ``kill`` kew issues is read back from
what that ``docker`` was asked, and every container from what it still holds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from support.fake_docker import LaunchDocker

from kew import sandbox, subprocess_utils
from kew.dispatch_sandbox import _record_sandbox_start, prepare_registered_container
from kew.orchestrator import _sweep_stale_container
from kew.run_store import SqliteRunStore
from kew.sandbox import (
    KEW_LEDGER_LABEL,
    KEW_RUN_LABEL,
    ContainedExecution,
    ContainerHandle,
    ContainerStartFailed,
    KillStatus,
    build_run_argv,
)

NAME = "kew-42"


@pytest.fixture
def docker_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    # A short limit stands in for a loaded host's slow daemon; no backoff wait.
    monkeypatch.setattr(sandbox, "RUNTIME_CONTROL_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr("kew.dispatch_sandbox.CREATE_TIMEOUT_BACKOFF_SECONDS", 0.0)

    def make(**kwargs: object) -> LaunchDocker:
        fake = LaunchDocker(tmp_path / "runtime", **kwargs)  # type: ignore[arg-type]
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(fake.path))
        return fake

    return make


def _store(root: Path) -> SqliteRunStore:
    root.mkdir(parents=True, exist_ok=True)
    return SqliteRunStore(str(root / "runs.db"), project_root=root)


def _admit(store: SqliteRunStore, tmp_path: Path, issue: int = 42) -> str:
    return _record_sandbox_start(
        store,
        issue_num=issue,
        item_id=str(issue),
        branch=f"agent/{issue}",
        title="test",
        model="test",
        runner_name="claude-code",
        log_file=tmp_path / f"run-{issue}.log",
        repo="o/r",
        worker_cap=4,
    )


def _create_argv(tmp_path: Path) -> list[str]:
    cidfile = tmp_path / "run.cid"
    return ["docker", "run", "--name", NAME, "--cidfile", str(cidfile), "image"]


@pytest.mark.parametrize(
    "failed_create",
    [
        # The client lost the daemon's answer: the container is made, exit 1.
        "commit-then-fail",
        # The client timed out and the daemon committed the container only
        # after the name was checked free, so the retry meets it by name.
        "hang-commit-late",
    ],
)
def test_a_create_that_failed_after_committing_this_runs_container_still_launches(
    tmp_path: Path, docker_factory: object, failed_create: str
) -> None:
    # A loaded host's daemon also fails ``info``: the dead-run reclaim, which
    # needs the daemon named, cannot be what frees the name.
    fake = docker_factory(plan=(failed_create,), info_fails=True)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)

        execution = prepare_registered_container(_create_argv(tmp_path), store, run_id)

        left = fake.containers()
        assert list(left) == [execution.container_id]
        assert left[execution.container_id]["labels"][KEW_RUN_LABEL] == run_id
        # The committed one went by its id; nothing was removed by name.
        assert all(call[-1] != NAME for call in fake.calls("rm"))
        assert execution.argv[1:3] == ("start", "--attach")
        assert execution.argv[-1] == execution.container_id
        record = store.get(run_id)
        assert record is not None
        assert record.container_id == execution.container_id
        assert record.error_category in (None, "")


@pytest.mark.parametrize("owner", ["live-run-of-this-ledger", "another-ledger"])
def test_a_container_of_another_run_under_the_name_is_still_a_conflict(
    tmp_path: Path, docker_factory: object, owner: str
) -> None:
    fake = docker_factory()  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        other_run = _admit(store, tmp_path, issue=43)
        ledger = store.read_ledger_id()
        assert ledger is not None
        labels = (
            {KEW_LEDGER_LABEL: ledger, KEW_RUN_LABEL: other_run}
            if owner == "live-run-of-this-ledger"
            else {KEW_LEDGER_LABEL: "another-ledger", KEW_RUN_LABEL: run_id}
        )
        theirs = fake.add(NAME, labels)

        with pytest.raises(ContainerStartFailed) as raised:
            prepare_registered_container(_create_argv(tmp_path), store, run_id)

        assert raised.value.name_conflict
        assert list(fake.containers()) == [theirs]
        assert fake.calls("rm") == []
        record = store.get(run_id)
        assert record is not None
        assert record.container_id != theirs


def _handle(tmp_path: Path, cidfile: Path) -> ContainerHandle:
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = 0
    return ContainerHandle(proc, NAME, "docker", cidfile=cidfile)


def test_removal_targets_the_recorded_id_and_removes_nothing_without_one(
    tmp_path: Path, docker_factory: object
) -> None:
    """The container under the name now is another launch's; this launch's own
    is whatever its cidfile recorded, or nothing."""
    fake = docker_factory()  # type: ignore[operator]
    theirs = fake.add(NAME, {}, status="running")
    recorded = format(7, "064x")
    cidfile = tmp_path / "recorded.cid"
    cidfile.write_text(recorded)

    with_id = _handle(tmp_path, cidfile)
    with_id.kill()
    with_id.remove()

    assert fake.calls("kill") == [["kill", recorded]]
    assert fake.calls("rm") == [["rm", "-f", recorded]]

    without_id = _handle(tmp_path, tmp_path / "never-written.cid")
    assert without_id.kill() is KillStatus.UNKNOWN
    assert without_id.remove() is False

    # Nothing more was sent, and the other launch's container is untouched.
    assert len(fake.calls("kill")) == 1
    assert len(fake.calls("rm")) == 1
    assert fake.containers()[theirs]["status"] == "running"


def test_a_removal_the_runtime_never_answers_reports_unremoved(
    tmp_path: Path, docker_factory: object
) -> None:
    fake = docker_factory(rm_hangs=True)  # type: ignore[operator]
    recorded = fake.add(NAME, {})
    cidfile = tmp_path / "recorded.cid"
    cidfile.write_text(recorded)

    assert _handle(tmp_path, cidfile).remove() is False
    assert fake.calls("rm") == [["rm", "-f", recorded]]
    assert list(fake.containers()) == [recorded]


def _contained(tmp_path: Path) -> ContainedExecution:
    return ContainedExecution.from_argv(
        build_run_argv(
            runtime="docker",
            image="ghcr.io/example/agent:latest",
            checkout_dir=tmp_path,
            slug="42",
            network="open",
            runner="claude-code",
        )
    )


def test_the_pre_launch_sweep_never_removes_by_name(
    tmp_path: Path, docker_factory: object
) -> None:
    fake = docker_factory()  # type: ignore[operator]
    contained = _contained(tmp_path)
    assert contained.name == NAME
    theirs = fake.add(NAME, {}, status="running")

    _sweep_stale_container(contained)

    assert fake.calls("rm") == []
    assert list(fake.containers()) == [theirs]

    # An id this launch's own cidfile records is the one thing it may free.
    recorded = fake.add("kew-42-earlier", {})
    cidfile = Path(contained.argv[list(contained.argv).index("--cidfile") + 1])
    cidfile.parent.mkdir(parents=True, exist_ok=True)
    cidfile.write_text(recorded)

    _sweep_stale_container(contained)

    assert fake.calls("rm") == [["rm", "-f", recorded]]
    assert list(fake.containers()) == [theirs]


def test_an_interrupted_create_never_kills_the_name(
    tmp_path: Path, docker_factory: object
) -> None:
    """``kew-<item>`` may be another run's running container: an unwind with no
    recorded id stops nothing."""
    fake = docker_factory(plan=("interrupt",))  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        other = _admit(store, tmp_path)
        ledger = store.read_ledger_id()
        assert ledger is not None
        theirs = fake.add(
            NAME, {KEW_LEDGER_LABEL: ledger, KEW_RUN_LABEL: other}, status="running"
        )
        run_id = _admit(store, tmp_path, issue=43)

        with pytest.raises(KeyboardInterrupt):
            prepare_registered_container(_create_argv(tmp_path), store, run_id)

        assert fake.calls("kill") == []
        assert fake.calls("wait") == []
        assert fake.calls("rm") == []
        assert fake.containers()[theirs]["status"] == "running"


def test_an_interrupted_create_stops_the_container_its_cidfile_records(
    tmp_path: Path, docker_factory: object
) -> None:
    fake = docker_factory(plan=("commit-then-interrupt",))  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)

        with pytest.raises(KeyboardInterrupt):
            prepare_registered_container(_create_argv(tmp_path), store, run_id)

        [created] = fake.containers()
        assert fake.calls("kill") == [["kill", created]]
        assert all(call[-1] != NAME for call in fake.calls("rm"))


def _removal_audit(store: SqliteRunStore) -> dict[str, list[dict[str, object]]]:
    return {
        event: [
            entry.payload
            for entry in store.list_audit_events(None, event=event)
            if isinstance(entry.payload, dict)
        ]
        for event in ("stale_container_removal_intended", "stale_container_removed")
    }


@pytest.mark.parametrize("info_fails", [False, True])
def test_a_finished_runs_never_started_container_is_removed_by_id_and_audited(
    tmp_path: Path, docker_factory: object, info_fails: bool
) -> None:
    """A run whose create timed out and was finalized can have its container
    committed late; the next launch of the item frees the name even when the
    loaded daemon cannot be named."""
    fake = docker_factory(info_fails=info_fails)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        prior = _admit(store, tmp_path)
        record = store.get(prior)
        assert record is not None
        record.status = "failure"
        record.error_category = "runtime_timeout"
        store.save(record)
        ledger = store.read_ledger_id()
        assert ledger is not None
        leftover = fake.add(NAME, {KEW_LEDGER_LABEL: ledger, KEW_RUN_LABEL: prior})
        run_id = _admit(store, tmp_path, issue=43)

        execution = prepare_registered_container(_create_argv(tmp_path), store, run_id)

        assert list(fake.containers()) == [execution.container_id]
        assert fake.calls("rm") == [["rm", leftover]]
        audit = _removal_audit(store)
        for payloads in audit.values():
            assert [p["container_id"] for p in payloads] == [leftover]
            assert (payloads[0]["daemon"] is None) is info_fails


def test_a_never_started_container_of_a_pending_run_is_kept_when_the_daemon_is_unnamed(
    tmp_path: Path, docker_factory: object
) -> None:
    fake = docker_factory(info_fails=True)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        live = _admit(store, tmp_path)
        ledger = store.read_ledger_id()
        assert ledger is not None
        theirs = fake.add(NAME, {KEW_LEDGER_LABEL: ledger, KEW_RUN_LABEL: live})
        run_id = _admit(store, tmp_path, issue=43)

        with pytest.raises(ContainerStartFailed):
            prepare_registered_container(_create_argv(tmp_path), store, run_id)

        assert list(fake.containers()) == [theirs]
        assert fake.calls("rm") == []


def test_removing_this_runs_own_committed_create_is_audited(
    tmp_path: Path, docker_factory: object
) -> None:
    fake = docker_factory(plan=("commit-then-fail",), info_fails=True)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)

        execution = prepare_registered_container(_create_argv(tmp_path), store, run_id)

        [removed] = [call[-1] for call in fake.calls("rm")]
        assert removed != execution.container_id
        audit = _removal_audit(store)
        for payloads in audit.values():
            assert [p["container_id"] for p in payloads] == [removed]


@pytest.mark.parametrize(
    "failed_create", ["commit-then-fail", "hang-commit-late", "commit-then-hang"]
)
def test_recovery_never_targets_the_removed_create_while_the_next_one_runs(
    tmp_path: Path, docker_factory: object, failed_create: str
) -> None:
    """Between removing the container a failed create left and the next create
    returning, a crash leaves recovery the cidfile, never the removed id."""
    from kew.stop_recovery import container_target

    root = tmp_path / "project"
    fake = docker_factory(  # type: ignore[operator]
        plan=(failed_create,), info_fails=True, snapshot_db=root / "runs.db"
    )
    with _store(root) as store:
        run_id = _admit(store, tmp_path)

        execution = prepare_registered_container(_create_argv(tmp_path), store, run_id)

    calls = fake.read()["calls"]
    [removal] = [i for i, call in enumerate(calls) if call[0] == "rm"]
    removed = calls[removal][-1]
    assert removed != execution.container_id
    # The create that followed the removal, as the ledger stood when it began.
    after = next(
        i for i, call in enumerate(calls) if i > removal and call[0] == "create"
    )
    with SqliteRunStore(str(fake.snapshot(after)), project_root=root) as crashed:
        record = crashed.get(run_id)
    assert record is not None
    assert record.container_id is None
    # So recovery reads the cidfile, which holds the new container once that
    # create has written it, and nothing at all before.
    cidfile = Path(record.container_cidfile or "")
    assert cidfile == tmp_path / "run.cid"
    assert container_target(record) == execution.container_id


def test_a_handle_with_no_identity_source_kills_nothing(
    tmp_path: Path, docker_factory: object
) -> None:
    fake = docker_factory()  # type: ignore[operator]
    theirs = fake.add(NAME, {}, status="running")
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = 0

    assert ContainerHandle(proc, NAME, "docker").kill() is KillStatus.UNKNOWN

    assert fake.calls("kill") == []
    assert fake.containers()[theirs]["status"] == "running"
