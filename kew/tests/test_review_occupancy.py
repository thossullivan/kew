"""``kew review <PR>`` refuses a PR the drive already holds (#3047).

The drive's ``kew epic _review-node`` child and a hand-started review used to
race to the same ``kew-<pr>`` container name: the second launch checked out a
review worktree, failed on ``docker create`` and reported a container failure.
The review now refuses before checkout and names the holder. ``gh`` and the
container runtime are faked at the process boundary; any other command (``git``
above all — a worktree is a ``git worktree add``) is recorded and refused.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kew import subprocess_utils
from kew.cli import app
from kew.config import DispatchConfig
from kew.process_groups import identity_at_admission
from kew.review import dispatch_review
from kew.review_checkout import review_checkout_path
from kew.review_occupancy import review_occupant
from kew.review_outcome import list_review_outcomes
from kew.run_store import (
    REVIEW_REMEDIATION_RUN_KIND,
    REVIEW_STAGE_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)

PR = 3040
ITEM = "3024"
REPO = "o/r"


class _Boundary:
    """``subprocess`` as ``kew.subprocess_utils`` sees it: ``gh`` and ``docker``.

    *running* and *stopped* are the container names ``docker inspect`` reports
    running or stopped; any other name is absent. Every command reaching the
    boundary is kept in :attr:`calls`, its executable by bare name (the CLI's
    preflight resolves tools to absolute paths).
    """

    def __init__(
        self, running: set[str], stopped: frozenset[str] = frozenset()
    ) -> None:
        self.running = running
        self.stopped = stopped
        self.calls: list[list[str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [Path(str(cmd[0])).name, *(str(part) for part in cmd[1:])]
        self.calls.append(argv)
        if argv[0] == "gh":
            out = self._gh(argv)
        elif argv[0] == "docker" and argv[1] == "inspect":
            return self._inspect(argv)
        elif argv[0] == "openshell" and argv[-5:-3] == ["sandbox", "get"]:
            return self._sandbox_get(argv)
        else:
            raise AssertionError(f"unexpected command at the boundary: {argv}")
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _gh(self, argv: list[str]) -> str:
        if argv[1:3] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": PR,
                    "headRefName": f"agent/issue-{ITEM}",
                    "headRefOid": "a" * 40,
                    "baseRefName": "main",
                    "body": "",
                    "title": "Prompt profiles",
                }
            )
        if argv[2] == "graphql":
            return json.dumps({"data": {}})
        if argv[2].endswith("/reviews"):
            review = {"id": 1, "body": "fix it", "user": {"login": "bob"}}
            return json.dumps([[review]])
        return json.dumps([[]])

    def _inspect(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        name = argv[-1]
        if name in self.running:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        if name in self.stopped:
            return subprocess.CompletedProcess(argv, 0, "false\n", "")
        return subprocess.CompletedProcess(
            argv, 1, "", f"Error: No such object: {name}\n"
        )

    def _sandbox_get(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """The gateway's ``sandbox get <name> -o json``: *running* names run."""
        name = argv[-3]
        if name in self.running:
            sandbox = {
                "name": name,
                "status": "running",
                "id": "sbx_fixture",
                "image": "kew-agent:openshell-test",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(sandbox), "")
        return subprocess.CompletedProcess(
            argv, 1, "", f"Error: sandbox {name} not found\n"
        )

    def launched(self) -> list[list[str]]:
        """Every command other than ``gh`` and a read-only ``docker inspect``."""
        return [
            argv
            for argv in self.calls
            if argv[0] != "gh" and argv[:2] != ["docker", "inspect"]
        ]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.project.repo = REPO
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    return cfg


def _install(
    monkeypatch: pytest.MonkeyPatch,
    running: set[str],
    stopped: frozenset[str] = frozenset(),
) -> _Boundary:
    boundary = _Boundary(running, stopped)
    for tool in ("gh", "docker", "git", "openshell"):
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, tool, tool)
    monkeypatch.setattr(subprocess_utils, "subprocess", boundary)
    return boundary


def _hold_with_review_stage_child(
    store: SqliteRunStore,
    run_id: str,
    *,
    item: str = ITEM,
    started_at: datetime | None = None,
) -> None:
    """What the drive's tick leaves behind it as it spawns ``_review-node``."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item),
            item_id=item,
            branch="",
            status="pending",
            exit_code=0,
            started_at=started_at or datetime.now(UTC),
            run_kind=REVIEW_STAGE_RUN_KIND,
            repo=REPO,
        )
    )
    assert store.reserve_epic_node_item("review", item, run_id)


def _claim_stage_row(store: SqliteRunStore, run_id: str, pid: int) -> None:
    """The stage row as a real child leaves it once it has claimed ownership."""
    row = store.get(run_id)
    assert row is not None
    store.save(
        replace(row, pgid=pid, owner_pid=pid, **identity_at_admission(pid)),
        update_ownership=True,
    )


def _fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh``, ``git`` and ``docker`` on PATH for the CLI's tool preflight.

    They are never executed: the faked ``subprocess`` answers every call.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("gh", "git", "docker"):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\nexit 97\n")
        exe.chmod(0o755)
    # The system directories stay on PATH for the harness's own ``ps``.
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), "/usr/bin", "/bin")))


def test_a_running_review_container_refuses_before_any_worktree(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boundary = _install(monkeypatch, running={f"kew-{PR}"})

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert rc.exit_code != 0
    assert f"'kew-{PR}'" in rc.reason
    assert f"'kew-{PR}'" in capsys.readouterr().err
    # No git (so no worktree) and no container start reached the boundary.
    assert boundary.launched() == []
    assert not review_checkout_path(config, PR).exists()
    [outcome] = list_review_outcomes(store, pr_number=PR)
    assert outcome.classification == "review_in_flight"
    assert f"kew-{PR}" in outcome.detail


def test_a_live_review_node_child_refuses_and_names_it(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _install(monkeypatch, running=set())
    _hold_with_review_stage_child(store, "stage-run-1234")

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert "_review-node" in rc.reason
    assert "stage-ru" in rc.reason
    assert f"item {ITEM}" in rc.reason
    assert boundary.launched() == []


def test_a_finished_review_node_child_does_not_hold_the_pr(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, running=set())
    _hold_with_review_stage_child(store, "stage-run-done")
    row = store.get("stage-run-done")
    assert row is not None
    store.save(replace(row, status="success"))

    assert review_occupant(PR, items=(ITEM,), config=config, store=store) is None


@pytest.mark.parametrize("runtime", ["docker", "openshell"])
def test_nothing_holds_a_pr_with_no_child_and_no_running_container(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    # A container of that name on openshell is never asked after: it is a
    # gateway sandbox, not a ``docker inspect`` target.
    _install(monkeypatch, running={f"kew-{PR}"} if runtime == "openshell" else set())
    config.sandbox.runtime = runtime

    assert review_occupant(PR, items=(ITEM,), config=config, store=store) is None


def test_a_runtime_that_cannot_answer_reads_as_free_and_says_why(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An inspect that cannot run is not a clean "nothing is running".

    The review still goes on (the launch's own name check stands behind it),
    but the warning names the runtime, the container and the failure.
    """
    boundary = _install(monkeypatch, running=set())

    def _no_daemon(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise OSError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(boundary, "_inspect", _no_daemon)
    config.sandbox.runtime = "docker"

    with caplog.at_level("WARNING", logger="kew.review_occupancy"):
        occupant = review_occupant(PR, items=(ITEM,), config=config, store=store)

    assert occupant is None
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert f"kew-{PR}" in warning
    assert "docker" in warning
    assert "Cannot connect to the Docker daemon" in warning


def test_a_governed_launch_is_never_refused_by_its_own_hold(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``_review-node`` child's own remediation passes ``item_id``.

    It goes on past the hold to its checkout: here the boundary refuses the
    checkout's ``git``, so the review stops there, never at the hold.
    """
    boundary = _install(monkeypatch, running={f"kew-{PR}"})
    _hold_with_review_stage_child(store, "stage-run-gov")

    dispatch_review(
        pr_number=PR,
        config=config,
        store=store,
        item_id=ITEM,
        reservation_held=True,
        force_budget=True,
    )

    assert [o.classification for o in list_review_outcomes(store, pr_number=PR)] != []
    assert all(
        o.classification != "review_in_flight"
        for o in list_review_outcomes(store, pr_number=PR)
    )
    assert any(argv[0] == "git" for argv in boundary.launched())


def _no_launch(boundary: _Boundary) -> None:
    """No checkout (``git worktree``/``clone``/``fetch``) and no container start."""
    for argv in boundary.launched():
        assert argv[0] != "docker", argv
        assert not {"worktree", "clone", "fetch"} & set(argv), argv


def test_kew_review_exits_2_with_the_refusal_naming_the_holder(
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tools(tmp_path, monkeypatch)
    boundary = _install(monkeypatch, running=set())
    # A real project the CLI loads its configuration from, with the same
    # repo, ledger and worktree base as the ``config`` fixture.
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n'
        f'[logging]\ndir = "{config.logging.dir}"\n'
        f'[branches]\nworktree_base = "{config.branches.worktree_base}"\n'
    )
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.chdir(project)
    log_dir = Path(config.logging.dir)
    log_dir.mkdir(parents=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as seeded:
        _hold_with_review_stage_child(seeded, "stage-run-cli1")

    result = CliRunner().invoke(app, ["review", str(PR)])

    assert result.exit_code == 2, result.output
    assert f"Review of PR #{PR} refused" in result.stderr
    assert "_review-node" in result.stderr
    assert "stage-ru" in result.stderr
    assert f"item {ITEM}" in result.stderr
    _no_launch(boundary)
    assert not review_checkout_path(config, PR).exists()
    with SqliteRunStore(str(log_dir / "dispatch.db")) as ledger:
        [outcome] = list_review_outcomes(ledger, pr_number=PR)
    assert outcome.classification == "review_in_flight"


def test_the_ledger_owner_of_the_branch_is_the_item_checked(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch says ``issue-3024``; the ledger says the branch is item 3050.

    The drive holds 3050 (its own name for the work), under a container named
    for that item, so both have to be asked about the owner the ledger names.
    """
    owner = "3050"
    boundary = _install(monkeypatch, running={f"kew-{owner}"})
    store.save(
        RunRecord(
            run_id="impl-run",
            issue_num=int(ITEM),
            item_id=owner,
            branch=f"agent/issue-{ITEM}",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=datetime.now(UTC) - timedelta(minutes=30),
            repo=REPO,
        )
    )
    _hold_with_review_stage_child(store, "stage-run-own", item=owner)

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert f"item {owner}" in rc.reason
    _no_launch(boundary)

    store.release_epic_node_item(owner, run_id="stage-run-own")
    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert f"'kew-{owner}'" in rc.reason


def test_a_pending_row_whose_owner_died_does_not_block(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGKILLed ``_review-node`` child leaves its row pending until a tick.

    While the recorded owner lives it holds the PR; once that owner is provably
    gone, the row is the dead row the reaper will reap, and blocks nothing.
    """
    _install(monkeypatch, running=set())
    child = subprocess.Popen(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    try:
        _hold_with_review_stage_child(store, "stage-run-dead")
        _claim_stage_row(store, "stage-run-dead", child.pid)

        live = review_occupant(PR, items=(ITEM,), config=config, store=store)
        assert live is not None and "stage-ru" in live

        os.killpg(child.pid, signal.SIGKILL)
        child.wait(20)

        assert review_occupant(PR, items=(ITEM,), config=config, store=store) is None
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(20)


@contextmanager
def _owner_process() -> Iterator[subprocess.Popen[bytes]]:
    """A real process in its own session, to stand as a recorded owner."""
    child = subprocess.Popen(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(20)


def _kill(child: subprocess.Popen[bytes]) -> None:
    os.killpg(child.pid, signal.SIGKILL)
    child.wait(20)


def test_a_live_owner_holds_past_the_in_flight_bound(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The age bound is the work view's reading, not proof of death (#2337).

    A remediation with several attempts can run past four hours; while its
    owner answers alive, the PR stays held however old the row is.
    """
    _install(monkeypatch, running=set())
    with _owner_process() as child:
        _hold_with_review_stage_child(store, "stage-run-long")
        _claim_stage_row(store, "stage-run-long", child.pid)
        later = datetime.now(UTC) + timedelta(hours=5)

        held = review_occupant(PR, items=(ITEM,), config=config, store=store, now=later)

        assert held is not None
        assert "stage-ru" in held


def test_a_live_run_under_a_dead_stage_owner_still_holds(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGKILLed ``_review-node`` leader leaves its remediation running.

    On OpenShell no ``docker inspect`` stands behind the check, so the run the
    child started is what has to hold the PR, for as long as the gateway says
    its sandbox runs — past the reaper's startup grace and past the in-flight
    bound alike (the reaper may use the grace only because it then kills the
    workload; this check never kills). Once the sandbox is gone, nothing holds.
    """
    config.sandbox.runtime = "openshell"
    sandbox = f"kew-{ITEM}"
    boundary = _install(monkeypatch, running={sandbox})
    with _owner_process() as child:
        _hold_with_review_stage_child(store, "stage-run-lead")
        _claim_stage_row(store, "stage-run-lead", child.pid)
        stage = store.get("stage-run-lead")
        assert stage is not None
        store.save(
            replace(
                stage,
                run_id="remediation-run",
                run_kind=REVIEW_REMEDIATION_RUN_KIND,
                container_name=sandbox,
                container_runtime="openshell",
            ),
            update_ownership=True,
        )
        _kill(child)

    # Asked of the check directly: the full review on this fake boundary stops
    # earlier, at the OpenShell route's image preflight.
    now = datetime.now(UTC)
    for moment in (now, now + timedelta(minutes=11), now + timedelta(hours=5)):
        held = review_occupant(
            PR, items=(ITEM,), config=config, store=store, now=moment
        )
        assert held is not None, moment
        assert "run remediat" in held  # the live child run is the holder named
        assert "stage-ru" in held
    # The answer came from the gateway, read-only: only ``sandbox get`` calls.
    assert boundary.calls
    assert all(argv[-5:-3] == ["sandbox", "get"] for argv in boundary.calls)

    boundary.running.discard(sandbox)
    # Gone within the startup grace still holds: the sandbox name is recorded
    # before the create, so a create the dead leader started can still commit.
    assert (
        review_occupant(PR, items=(ITEM,), config=config, store=store, now=now)
        is not None
    )
    later = now + timedelta(minutes=11)
    assert (
        review_occupant(PR, items=(ITEM,), config=config, store=store, now=later)
        is None
    )


def test_the_occupancy_refusal_comes_after_admission_and_before_checkout(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is the last admission step, as late as it goes without a tree.

    By the time it refuses, the route has minted the run's id; nothing was
    checked out.
    """
    boundary = _install(monkeypatch, running={f"kew-{PR}"})

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    [outcome] = list_review_outcomes(store, pr_number=PR)
    assert outcome.classification == "review_in_flight"
    assert rc.run_id is not None  # the route ran
    assert not any(argv[0] == "git" for argv in boundary.calls)
    assert not review_checkout_path(config, PR).exists()


def test_a_pending_row_past_the_in_flight_bound_does_not_block(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owner nobody can probe is presumed live — until the row ages out."""
    _install(monkeypatch, running=set())
    _hold_with_review_stage_child(store, "stage-run-old")
    row = store.get("stage-run-old")
    assert row is not None
    # An owner whose identity was never captured: unprobeable, so not dead.
    store.save(replace(row, pgid=4242, owner_pid=4242), update_ownership=True)
    now = datetime.now(UTC)

    assert review_occupant(PR, items=(ITEM,), config=config, store=store, now=now)
    later = now + timedelta(hours=5)
    assert (
        review_occupant(PR, items=(ITEM,), config=config, store=store, now=later)
        is None
    )


def test_an_unclaimed_stage_row_past_its_grace_names_no_child(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage row its child never claimed started nothing on the item.

    Past the startup grace it is dead, and an ownerless pending row on the
    same item is not its child (the reaper returns early for such a stage
    too), so nothing is named as a run the dead child started.
    """
    _install(monkeypatch, running=set())
    then = datetime.now(UTC) - timedelta(minutes=20)
    _hold_with_review_stage_child(store, "stage-run-unclaimed", started_at=then)
    store.save(
        RunRecord(
            run_id="remediation-ownerless",
            issue_num=int(ITEM),
            item_id=ITEM,
            branch="",
            status="pending",
            exit_code=0,
            started_at=then,
            run_kind="review_remediation",
            repo=REPO,
        )
    )

    held = review_occupant(
        PR, items=(ITEM,), config=config, store=store, now=datetime.now(UTC)
    )

    assert held is None or "the dead `kew epic _review-node` child" not in held


def test_a_reservation_without_its_row_blocks_only_while_fresh(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drive reserves first and writes the stage row second.

    Between the two the reservation is a holder starting up. One whose row was
    never written within the startup grace died before writing it, and the
    tick reclaims it, so it must not block the review for good either.
    """
    _install(monkeypatch, running=set())
    assert store.reserve_epic_node_item("review", ITEM, "stage-run-norow")
    now = datetime.now(UTC)

    fresh = review_occupant(PR, items=(ITEM,), config=config, store=store, now=now)
    assert fresh is not None
    assert "stage-ru" in fresh
    assert f"item {ITEM}" in fresh

    stale = now + timedelta(minutes=11)
    assert (
        review_occupant(PR, items=(ITEM,), config=config, store=store, now=stale)
        is None
    )


def test_a_governed_container_named_for_the_item_blocks(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drive runs PR #3040 of issue #3024 as ``kew-3024``, not ``kew-3040``."""
    boundary = _install(monkeypatch, running={f"kew-{ITEM}"})

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert f"'kew-{ITEM}' is already running" in rc.reason
    _no_launch(boundary)
    [outcome] = list_review_outcomes(store, pr_number=PR)
    assert outcome.classification == "review_in_flight"


def test_a_stopped_container_of_the_name_is_reported_with_its_removal(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exited ``kew-<pr>`` still owns the name ``docker create`` needs."""
    boundary = _install(monkeypatch, running=set(), stopped=frozenset({f"kew-{PR}"}))

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert f"stopped container 'kew-{PR}'" in rc.reason
    assert f"`docker rm kew-{PR}`" in rc.reason
    _no_launch(boundary)
    inspects = [argv for argv in boundary.calls if argv[:2] == ["docker", "inspect"]]
    assert inspects
    assert all(argv[2:4] == ["--type", "container"] for argv in inspects)
