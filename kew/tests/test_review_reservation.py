"""A hand-run ``kew review`` and the drive never work on one item at once.

The two used to meet only on a container name: a standalone review launches
``kew-<pr>``, the drive's reviewer ``kew-<item>``, so when the names differed
nothing stopped both from remediating the same branch. A hand-run review now
takes the item's review reservation, the one the drive takes, and each side
refuses the other on it. Every test here uses PR 3040 for item 3024, so the two
container names differ throughout.

``gh``, ``git`` and the container runtime are faked at the process boundary. A
live hand-run holder is a real helper process taking the reservation through
the same call the review makes.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kew import process_groups, subprocess_utils
from kew.cli import app
from kew.config import DispatchConfig, ReviewConfig
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.reservation_sweep import slot_holders, sweep_reservations
from kew.graph.driver.review_detach import DetachedReview
from kew.graph.driver.review_stage import (
    REVIEW_DEFERRED_TO_HAND_RUN,
    ReviewStageOutcome,
    drive_review_stage,
)
from kew.graph.driver.tick import (
    _RESERVATION_STARTUP_GRACE_SECONDS,
    _kill_reviewer_reservations,
    _reconcile_reservations,
)
from kew.process_groups import identity_at_admission, machine_boot
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review import dispatch_review
from kew.review_checkout import review_checkout_path
from kew.review_dispatch import ReviewerLaunchContext
from kew.review_occupancy import (
    STANDALONE_REVIEW_RESERVED,
    reserve_standalone_review,
    standalone_reservation,
)
from kew.run_store import (
    REVIEW_STAGE_RUN_KIND,
    EpicReservation,
    RunRecord,
    SqliteRunStore,
)

PR = 3040
ITEM = "3024"
REPO = "o/r"
EPIC = "epic:reliability"
_RUNNER_STAND_IN = "kew-test-runner"
#: The git commands a review checkout starts with; anything else only reads.
_GIT_LAUNCHES = frozenset({"worktree", "clone", "fetch", "checkout"})

_HOLDER = """
import sys
from kew.review_occupancy import reserve_standalone_review
from kew.run_store import SqliteRunStore

with SqliteRunStore(sys.argv[1]) as store:
    refusal = reserve_standalone_review(
        store, sys.argv[2], sys.argv[3], pr_number=int(sys.argv[4]), repo="o/r"
    )
print("held" if refusal is None else refusal, flush=True)
sys.stdin.read()
"""


class _Boundary:
    """``subprocess`` as ``kew.subprocess_utils`` sees it.

    ``gh`` answers the PR and one review comment; ``docker inspect`` reports
    every name absent, or raises when the daemon is *unreachable*; a ``git``
    checkout command is recorded and refused, which ends a review at its
    checkout, and other ``git`` reads find nothing. Any other tool (the CLI's
    runner probes) answers success. :meth:`launched` keeps every checkout
    command and every ``docker`` command but an inspect.
    """

    def __init__(self, *, unreachable: bool = False) -> None:
        self.unreachable = unreachable
        self.calls: list[list[str]] = []
        self.on_git: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [Path(str(cmd[0])).name, *(str(part) for part in cmd[1:])]
        if argv[0] == _RUNNER_STAND_IN and argv[1:2] in (["pr"], ["api"]):
            argv[0] = "gh"  # the suite resolves a missing ``gh`` to its stand-in
        self.calls.append(argv)
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, self._gh(argv), "")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, f"{os.getcwd()}\n", "")
        if argv[0] == "git" and argv[1] not in _GIT_LAUNCHES:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not faked\n")
        if argv[:2] == ["docker", "inspect"]:
            if self.unreachable:
                raise OSError("Cannot connect to the Docker daemon")
            return subprocess.CompletedProcess(
                argv, 1, "", f"Error: No such object: {argv[-1]}\n"
            )
        if argv[0] in {"git", "docker"}:
            for hook in self.on_git:
                hook()
            raise AssertionError(f"no launch may reach the boundary: {argv}")
        return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true}\n', "")

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

    def launched(self) -> list[list[str]]:
        return [
            argv
            for argv in self.calls
            if (argv[0] == "git" and argv[1] in _GIT_LAUNCHES)
            or (argv[0] == "docker" and argv[1] != "inspect")
        ]


def _install(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Boundary:
    boundary = _Boundary(**kwargs)
    for tool in ("gh", "docker", "git"):
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, tool, tool)
    monkeypatch.setattr(subprocess_utils, "subprocess", boundary)
    return boundary


@pytest.fixture
def config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.project.repo = REPO
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    cfg.sandbox.runtime = "docker"
    cfg.review = ReviewConfig(
        adversarial=True,
        blocking=True,
        max_rounds=3,
        max_review_usd=10.0,
        publish_status=False,
        publish_comment=False,
    )
    return cfg


@pytest.fixture
def db(config: DispatchConfig) -> Path:
    log_dir = Path(config.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "dispatch.db"


@pytest.fixture
def store(db: Path) -> Iterator[SqliteRunStore]:
    with SqliteRunStore(str(db)) as opened:
        yield opened


@pytest.fixture(params=[True, False], ids=["own-group", "in-callers-group"])
def own_session(request: pytest.FixtureRequest) -> bool:
    """Whether the hand-run holder leads its own process group.

    A shell job does; ``uv run kew review``, a script or a pipeline stage does
    not, and its reservation must hold just the same.
    """
    return bool(request.param)


@contextmanager
def _hand_run_holder(
    db: Path, run_id: str, *, own_session: bool, path: str | None = None
) -> Iterator[subprocess.Popen[str]]:
    """A live process holding *ITEM* the way a hand-run review takes it.

    In its own session, as a shell job is, or in the caller's process group,
    as a process run by ``uv run`` or a script is. It holds until its stdin
    closes; the ``held`` line is the ready signal. *path* replaces its
    ``PATH``, so it can reserve under a ``ps`` that fails.
    """
    env = dict(os.environ) if path is None else {**os.environ, "PATH": path}
    proc = subprocess.Popen(  # noqa: S603 — this interpreter, fixed script
        [sys.executable, "-c", _HOLDER, str(db), ITEM, run_id, str(PR)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=own_session,
        env=env,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def _facts() -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=f"https://example.invalid/pr/{PR}",
        review_decision=None,
        merge_state_status=None,
        head_oid="a" * 40,
        pr_pushed_at=None,
    )


class _Runner:
    """The reviewer and remediator a tick would launch; records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def reviewer(self, ctx: ReviewerLaunchContext) -> Any:
        self.calls.append(f"review {ctx.item_id}")
        raise AssertionError("no reviewer may launch")

    def remediator(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("remediate")
        raise AssertionError("no remediation may launch")


def _tick(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    runner: _Runner,
    *,
    detacher: Any = None,
) -> ReviewStageOutcome:
    return drive_review_stage(
        {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
        {ITEM: _facts()},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo=REPO,
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=runner.reviewer,
        remediator=runner.remediator,
        worker_cap=4,
        max_concurrent=2,
        detacher=detacher,
    )


def _review_reservations(store: SqliteRunStore) -> list[EpicReservation]:
    return store.list_epic_reservations(REVIEW_EPIC_REF)


@pytest.fixture(params=["tick", "sweep"])
def settle(
    request: pytest.FixtureRequest, store: SqliteRunStore, config: DispatchConfig
) -> Callable[[datetime], object]:
    """One pass of a path that releases reservations: the review pseudo-epic's
    tick reconciler, or the sweep ``kew drive`` runs every round."""

    def run(now: datetime) -> object:
        if request.param == "sweep":
            return sweep_reservations(store, config, now=now)
        return _reconcile_reservations(REVIEW_EPIC_REF, store, config, now=now)

    return run


def test_a_hand_run_review_holds_the_item_until_it_ends(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held from before the checkout, recorded to this process, then released."""
    boundary = _install(monkeypatch)
    seen: list[list[EpicReservation]] = []
    boundary.on_git.append(lambda: seen.append(_review_reservations(store)))

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"  # the boundary refused the checkout's git
    [[held]] = seen
    assert held.item_id == ITEM
    [event] = store.list_audit_events(
        event=STANDALONE_REVIEW_RESERVED, run_id=held.run_id
    )
    assert event.payload is not None
    assert event.payload["owner_pid"] == os.getpid()
    # Its own birth, whether or not it leads its process group.
    assert event.payload.get("owner_start_token") or event.payload.get(
        "owner_start_time"
    )
    assert event.payload["pr_number"] == PR
    assert _review_reservations(store) == []


@pytest.mark.parametrize("detached", [True, False])
def test_a_tick_launches_nothing_for_an_item_a_hand_run_review_holds(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    tmp_path: Path,
    detached: bool,
    own_session: bool,
) -> None:
    runner = _Runner()
    spawned: list[DetachedReview] = []
    with _hand_run_holder(db, "hand-run-1", own_session=own_session):
        first = _tick(
            store,
            config,
            tmp_path,
            runner,
            detacher=spawned.append if detached else None,
        )
        _tick(
            store,
            config,
            tmp_path,
            runner,
            detacher=spawned.append if detached else None,
        )

    assert runner.calls == []
    assert spawned == []
    [event] = store.list_audit_events(event=REVIEW_DEFERRED_TO_HAND_RUN)
    assert event.run_id == "hand-run-1"
    assert event.item_id == ITEM
    assert "hand-run-1" in first.detached[ITEM]
    assert "hand-run `kew review`" in first.detached[ITEM]
    assert [r.run_id for r in _review_reservations(store)] == ["hand-run-1"]


def test_a_hand_run_review_killed_with_9_gives_the_item_back_to_the_next_tick(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    tmp_path: Path,
    own_session: bool,
) -> None:
    """Its run row never written, its process provably gone: no grace to wait."""
    runner = _Runner()
    with _hand_run_holder(db, "hand-run-killed", own_session=own_session) as holder:
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait()

    _reconcile_reservations(REVIEW_EPIC_REF, store, config, now=datetime.now(UTC))

    assert _review_reservations(store) == []
    spawned: list[DetachedReview] = []
    _tick(store, config, tmp_path, runner, detacher=spawned.append)
    assert [review.item_id for review in spawned] == [ITEM]
    assert runner.calls == []  # the detached child, not the tick, would run it


def test_a_live_hand_run_review_keeps_the_item_past_the_startup_grace(
    store: SqliteRunStore,
    db: Path,
    own_session: bool,
    settle: Callable[[datetime], object],
) -> None:
    """A slow checkout or image build is not a dead holder."""
    later = datetime.now(UTC) + timedelta(
        seconds=2 * _RESERVATION_STARTUP_GRACE_SECONDS
    )
    with _hand_run_holder(db, "hand-run-slow", own_session=own_session):
        settle(later)

        assert [r.run_id for r in _review_reservations(store)] == ["hand-run-slow"]


def test_a_hand_run_review_keeps_the_item_while_it_publishes_a_finished_run(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    tmp_path: Path,
    own_session: bool,
    settle: Callable[[datetime], object],
) -> None:
    """Its agent row finishes before it publishes; the process still holds.

    Neither the epic tick's reconciler nor the sweep ``kew drive`` runs every
    round reads the finished row as the end of the review.
    """
    runner = _Runner()
    spawned: list[DetachedReview] = []
    now = datetime.now(UTC)
    with _hand_run_holder(db, "hand-run-publishing", own_session=own_session) as holder:
        store.save(
            RunRecord(
                run_id="hand-run-publishing",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch=f"agent/issue-{ITEM}",
                status="success",
                exit_code=0,
                started_at=now - timedelta(minutes=5),
                finished_at=now,
            )
        )
        settle(now)
        _tick(store, config, tmp_path, runner, detacher=spawned.append)

        assert spawned == []
        assert runner.calls == []
        assert [r.run_id for r in _review_reservations(store)] == [
            "hand-run-publishing"
        ]
        [holding] = slot_holders(store, now=now)
        assert "hand-run `kew review`" in holding.verdict
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait()

    settle(now)
    assert _review_reservations(store) == []


def test_a_merged_pr_leaves_a_live_hand_run_review_to_finish(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    own_session: bool,
) -> None:
    """The drive kills its own reviewer on a merged or closed PR, not this one.

    The live hand-run review gives its reservation back itself when it ends;
    once it is provably dead the kill settles it as it would any reviewer.
    """
    now = datetime.now(UTC)
    with _hand_run_holder(db, "hand-run-merged", own_session=own_session) as holder:
        _kill_reviewer_reservations(store, ITEM, config, now=now)

        assert [r.run_id for r in _review_reservations(store)] == ["hand-run-merged"]
        assert store.get("hand-run-merged") is None
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait()

    _kill_reviewer_reservations(store, ITEM, config, now=now)
    assert _review_reservations(store) == []
    killed = store.get("hand-run-merged")
    assert killed is not None and killed.status == "killed"


def test_no_reader_sees_a_hand_run_reservation_without_its_holder(
    store: SqliteRunStore,
    db: Path,
) -> None:
    """The reservation and the record naming its holder commit together.

    A reader in between would take the reservation for a drive reviewer's,
    one a merged PR lets it kill. A second connection reads the ledger at
    every statement the reserving connection runs.
    """
    seen: list[bool] = []
    with SqliteRunStore(str(db)) as reader:

        def observe(_statement: str) -> None:
            seen.extend(
                standalone_reservation(reader, res.run_id) is not None
                for res in reader.list_epic_reservations(REVIEW_EPIC_REF)
            )

        store._conn.set_trace_callback(observe)
        try:
            refusal = reserve_standalone_review(
                store, ITEM, "hand-run-atomic", pr_number=PR, repo=REPO
            )
        finally:
            store._conn.set_trace_callback(None)
        observe("after")

    assert refusal is None
    assert seen and all(seen)


class _CommitFails:
    """The store's sqlite connection, whose next ``commit`` fails as a full
    disk does; every other call reaches the real connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._real = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def commit(self) -> None:
        raise sqlite3.OperationalError("database or disk is full")


def test_a_failed_commit_leaves_no_reservation_and_no_open_transaction(
    store: SqliteRunStore,
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = store._conn
    monkeypatch.setattr(store, "_conn", _CommitFails(real))

    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        reserve_standalone_review(
            store, ITEM, "hand-run-full-disk", pr_number=PR, repo=REPO
        )

    assert not real.in_transaction
    monkeypatch.setattr(store, "_conn", real)
    with SqliteRunStore(str(db)) as reader:
        assert reader.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert standalone_reservation(reader, "hand-run-full-disk") is None
    assert (
        reserve_standalone_review(store, ITEM, "hand-run-next", pr_number=PR, repo=REPO)
        is None
    )


@pytest.fixture
def failing_ps(tmp_path: Path) -> str:
    """A ``PATH`` whose first ``ps`` fails, as one under memory pressure or a
    sandbox that denies it does; the rest of the caller's ``PATH`` follows."""
    bin_dir = tmp_path / "failing-ps"
    bin_dir.mkdir()
    exe = bin_dir / "ps"
    exe.write_text("#!/bin/sh\necho 'ps: cannot read the process table' >&2\nexit 1\n")
    exe.chmod(0o755)
    return os.pathsep.join((str(bin_dir), os.environ.get("PATH", "")))


def _finished_agent_row(run_id: str, now: datetime) -> RunRecord:
    """The run row a hand-run review's agent leaves before it publishes."""
    return RunRecord(
        run_id=run_id,
        issue_num=int(ITEM),
        item_id=ITEM,
        branch=f"agent/issue-{ITEM}",
        status="success",
        exit_code=0,
        started_at=now - timedelta(minutes=5),
        finished_at=now,
    )


def _release_holder(holder: subprocess.Popen[str]) -> None:
    """Let the helper finish on its own, as a review that has published does."""
    assert holder.stdin is not None
    holder.stdin.close()
    assert holder.wait(timeout=30) == 0


@pytest.mark.parametrize("ps_fails_at", ["reserve", "probe"])
def test_a_hand_run_review_keeps_the_item_when_ps_fails(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_ps: str,
    settle: Callable[[datetime], object],
    ps_fails_at: str,
) -> None:
    """The kernel's birth token, not a ``ps`` snapshot, identifies the review.

    A ``ps`` that fails when the review reserves, or when the drive probes it,
    neither releases a review that is still publishing nor lets the drive
    start a second one; once the review exits, the next pass releases it.
    """
    runner = _Runner()
    spawned: list[DetachedReview] = []
    now = datetime.now(UTC)
    at_reserve = failing_ps if ps_fails_at == "reserve" else None
    with _hand_run_holder(
        db, "hand-run-no-ps", own_session=False, path=at_reserve
    ) as holder:
        store.save(_finished_agent_row("hand-run-no-ps", now))
        if ps_fails_at == "probe":
            monkeypatch.setenv("PATH", failing_ps)
        later = now + timedelta(seconds=2 * _RESERVATION_STARTUP_GRACE_SECONDS)
        settle(now)
        settle(later)
        _tick(store, config, tmp_path, runner, detacher=spawned.append)

        assert spawned == []
        assert runner.calls == []
        assert [r.run_id for r in _review_reservations(store)] == ["hand-run-no-ps"]
        _release_holder(holder)

    settle(later)
    assert _review_reservations(store) == []


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
def test_a_hand_run_review_whose_identity_went_unrecorded_holds_while_its_pid_lives(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    tmp_path: Path,
    settle: Callable[[datetime], object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recorded on this boot with no readable birth: kept while the pid exists.

    Past the startup grace, with no run row and with a finished one, the
    reservation stands and one warning names it; the pid gone, it is released.
    """
    holder = subprocess.Popen(  # noqa: S603 — this interpreter, fixed script
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        text=True,
    )
    # Its own id: the warning is rate-limited per reservation per process.
    run_id = f"hand-run-unprobed-{uuid.uuid4().hex[:8]}"
    try:
        assert store.append_audit_event_reserving_item(
            REVIEW_EPIC_REF,
            ITEM,
            run_id,
            event=STANDALONE_REVIEW_RESERVED,
            actor_type="human",
            actor_id="kew review",
            repo=REPO,
            payload={
                "pr_number": PR,
                "item_id": ITEM,
                "owner_pid": holder.pid,
                "owner_boot_id": machine_boot(),
            },
        )
        later = datetime.now(UTC) + timedelta(
            seconds=2 * _RESERVATION_STARTUP_GRACE_SECONDS
        )
        with caplog.at_level(logging.WARNING, logger="kew.review_occupancy"):
            settle(later)
            store.save(_finished_agent_row(run_id, later))
            settle(later)
        spawned: list[DetachedReview] = []
        _tick(store, config, tmp_path, _Runner(), detacher=spawned.append)

        assert spawned == []
        assert [r.run_id for r in _review_reservations(store)] == [run_id]
        kept = [r for r in caplog.records if run_id in r.getMessage()]
        assert len(kept) == 1
        assert f"pid {holder.pid}" in kept[0].getMessage()
        _release_holder(holder)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait()

    settle(later)
    assert _review_reservations(store) == []


def _fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh``, ``git`` and ``docker`` on PATH for the CLI's tool preflight."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("gh", "git", "docker"):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\nexit 97\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), "/usr/bin", "/bin")))


def test_kew_review_refuses_an_item_the_drive_holds_and_names_its_run(
    config: DispatchConfig,
    db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tools(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n'
        f'[logging]\ndir = "{config.logging.dir}"\n'
        f'[branches]\nworktree_base = "{config.branches.worktree_base}"\n'
    )
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.chdir(project)
    spawned: list[DetachedReview] = []
    with SqliteRunStore(str(db)) as drive_store:
        _tick(drive_store, config, tmp_path, _Runner(), detacher=spawned.append)
    [drive] = spawned
    boundary = _install(monkeypatch)

    result = CliRunner().invoke(app, ["review", str(PR)])

    assert result.exit_code != 0, result.output
    assert f"Review of PR #{PR} refused" in result.stderr
    assert drive.run_id[:8] in result.stderr
    assert boundary.launched() == []
    assert not review_checkout_path(config, PR).exists()
    with SqliteRunStore(str(db)) as ledger:
        assert [r.run_id for r in _review_reservations(ledger)] == [drive.run_id]


def _dead_owner() -> tuple[int, dict[str, Any]]:
    """A process's pid and identity, recorded while it lived; it is gone now."""
    proc = subprocess.Popen(  # noqa: S603 — this interpreter, fixed script
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    identity = dict(identity_at_admission(proc.pid))
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait()
    return proc.pid, identity


def test_a_held_reservation_refuses_when_docker_cannot_answer(
    store: SqliteRunStore,
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drive's child died and no tick has reaped it yet: still the drive's.

    The occupancy check reads the dead child as no holder and cannot ask the
    daemon about either container name; the reservation it left is what
    refuses, naming the run, its pid and its start.
    """
    boundary = _install(monkeypatch, unreachable=True)
    spawned: list[DetachedReview] = []
    _tick(store, config, Path(config.logging.dir), _Runner(), detacher=spawned.append)
    [drive] = spawned
    stage = store.get(drive.run_id)
    assert stage is not None and stage.run_kind == REVIEW_STAGE_RUN_KIND
    pid, identity = _dead_owner()
    assert identity.get("owner_start_time") or identity.get("owner_start_token")
    store.save(
        replace(stage, pgid=pid, owner_pid=pid, **identity),
        update_ownership=True,
    )

    rc = dispatch_review(pr_number=PR, config=config, store=store)

    assert rc.kind == "not_run"
    assert rc.exit_code != 0
    assert drive.run_id in rc.reason
    assert f"pid {pid}" in rc.reason
    assert "started " in rc.reason
    assert any(argv[:2] == ["docker", "inspect"] for argv in boundary.calls)
    assert boundary.launched() == []
    assert [r.run_id for r in _review_reservations(store)] == [drive.run_id]


def test_a_hand_run_review_refuses_another_and_names_it(
    store: SqliteRunStore,
    config: DispatchConfig,
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
    own_session: bool,
) -> None:
    boundary = _install(monkeypatch, unreachable=True)
    with _hand_run_holder(db, "hand-run-first", own_session=own_session) as holder:
        rc = dispatch_review(pr_number=PR, config=config, store=store)

        assert rc.kind == "not_run"
        assert "hand-run `kew review`" in rc.reason
        assert "run hand-run-first" in rc.reason
        assert f"pid {holder.pid}" in rc.reason
        assert [r.run_id for r in _review_reservations(store)] == ["hand-run-first"]
    assert boundary.launched() == []
