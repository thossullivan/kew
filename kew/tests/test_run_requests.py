"""Run requests (#2537): the console queues a command, the drive runs it.

The acceptance: a Run on a fixture request is executed once by the drive,
idempotently, with an audit row naming the operator; a request the drive
refuses (budget, hold, stopped fleet) carries the refusal back to the block
that issued it. Every test runs on a real ledger; only the child the drive
starts is a stand-in (a real ``python -c`` process, not a kew).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import kew.cli.drive as drive_cli
from kew import run_requests as rr
from kew.config import DispatchConfig, ProjectConfig
from kew.drive import DriveLease, acquire_drive, current_holder, hold, request_stop
from kew.drive_heartbeat import DRIVE_HEARTBEAT_KEY, DriveHeartbeat
from kew.drive_requests import RequestRunner
from kew.run_store import RunRecord, SqliteRunStore
from kew.service import run_requests as svc

UNHOLD = "kew epic unhold epic:a 12"
_CFG = DispatchConfig(project=ProjectConfig(repo="o/r"))


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        yield store


@pytest.fixture
def config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(tmp_path)
    return cfg


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    return "tom"


class _Spawner:
    """Starts a real child per request, recording what it was asked to run."""

    def __init__(self, code: int = 0, output: str = "ran") -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.envs: list[dict[str, str]] = []
        self.children: list[subprocess.Popen[bytes]] = []
        self.code, self.output = code, output

    def __call__(
        self, argv: list[str], log: Path, actor: str, env: dict[str, str]
    ) -> Any:
        self.calls.append((argv, actor))
        self.envs.append(env)
        script = f"print({self.output!r}); raise SystemExit({self.code})"
        with log.open("ab") as out:
            child = subprocess.Popen(
                [sys.executable, "-c", script], stdout=out, stderr=subprocess.STDOUT
            )
        self.children.append(child)
        return child

    def settle(self) -> None:
        for child in self.children:
            child.wait(timeout=30)


def _runner(
    store: Any, config: DispatchConfig, spawn: Any, lease: DriveLease | None = None
) -> RequestRunner:
    if lease is None:
        got = acquire_drive(store)
        assert isinstance(got, DriveLease)
        lease = got
    return RequestRunner(
        store, lease, config, Path(config.logging.dir), spawn=spawn, say=lambda _m: None
    )


def _live_drive(store: Any, *, age_s: float = 0.0) -> DriveLease:
    lease = acquire_drive(store)
    assert isinstance(lease, DriveLease)
    beat = DriveHeartbeat(
        token=lease.holder.token,
        pid=lease.holder.pid,
        host=lease.holder.host,
        at=datetime.now(UTC) - timedelta(seconds=age_s),
        every_s=30.0,
        stage="sleeping",
    )
    store.set_meta(DRIVE_HEARTBEAT_KEY, beat.encode())
    return lease


def _events(store: Any, event: str) -> list[Any]:
    return store.list_audit_events(event=event)


# --------------------------------------------------------------------------- #
# What a request may carry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "kew dispatch 42",
        "kew epic continue epic:a 12",
        "kew epic retry go-live SFD-232",
        UNHOLD,
        "  kew   epic  unhold epic:a 12 ",
    ],
)
def test_the_bounded_console_commands_are_runnable(command: str) -> None:
    argv = rr.parse_command(command)
    assert rr.canonical(argv) == " ".join(command.split())


@pytest.mark.parametrize(
    "command",
    [
        "kew epic run epic:a",  # a long-lived loop that would fight the drive
        "kew dispatch 42 --force-budget",
        "kew dispatch --force-budget",
        "kew dispatch 1 2",
        "kew dispatch 1; rm -rf ~",
        "kew dispatch $(id)",
        "kew dispatch '1 && id'",
        "kew epic unhold epic:a",
        "rm -rf /",
        "kew",
        "",
        "kew dispatch 'unterminated",
        "/usr/bin/kew dispatch 1",
    ],
)
def test_anything_else_is_refused_before_a_row_is_written(
    command: str, ledger: Any
) -> None:
    _live_drive(ledger)
    with pytest.raises(rr.NotRunnable):
        svc.queue_request(
            ledger, command=command, item_id=None, source="web", config=_CFG
        )
    assert rr.list_requests(ledger) == []


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #


def test_a_run_with_no_drive_running_says_so_and_queues_nothing(ledger: Any) -> None:
    with pytest.raises(svc.NoDriveRunning, match="no drive running"):
        svc.queue_request(
            ledger, command=UNHOLD, item_id="12", source="web", config=_CFG
        )
    assert rr.list_requests(ledger) == []
    assert _events(ledger, "run_request_queued") == []


def test_a_stalled_drive_is_no_drive(ledger: Any) -> None:
    _live_drive(ledger, age_s=3600)
    with pytest.raises(svc.NoDriveRunning, match="heartbeat is"):
        svc.queue_request(
            ledger, command=UNHOLD, item_id="12", source="web", config=_CFG
        )


def test_queueing_writes_the_request_and_an_audit_row_naming_the_operator(
    ledger: Any,
) -> None:
    _live_drive(ledger)
    result = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG
    )

    request = result["request"]
    assert result["created"] is True
    assert result["drive"]["running"] is True
    assert (request["state"], request["operator"], request["item_id"]) == (
        rr.QUEUED,
        "tom",
        "12",
    )
    [event] = _events(ledger, "run_request_queued")
    assert event.actor_id == "tom"
    assert event.payload["command"] == UNHOLD
    assert event.payload["source"] == "web"


def test_queueing_is_gated_on_the_approvers_like_every_spend_verb(
    ledger: Any,
) -> None:
    from kew.service.verbs import VerbDenied

    _live_drive(ledger)
    gated = _CFG.model_copy(deep=True)
    gated.approval.approvers = ["ana"]
    with pytest.raises(VerbDenied):
        svc.queue_request(
            ledger, command=UNHOLD, item_id="12", source="web", config=gated
        )
    assert rr.list_requests(ledger) == []
    gated.approval.approvers = ["tom"]
    assert svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=gated
    )["created"]


def test_queueing_the_same_command_twice_returns_the_first_request(
    ledger: Any,
) -> None:
    _live_drive(ledger)
    first = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG
    )
    second = svc.queue_request(
        ledger, command=f" {UNHOLD} ", item_id="12", source="cli", config=_CFG
    )

    assert second["created"] is False
    assert second["request"]["id"] == first["request"]["id"]
    assert len(rr.list_requests(ledger)) == 1
    assert len(_events(ledger, "run_request_queued")) == 1


def test_a_finished_command_may_be_queued_again(ledger: Any, config: Any) -> None:
    lease = _live_drive(ledger)
    spawn = _Spawner()
    runner = _runner(ledger, config, spawn, lease)
    first, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")
    runner()
    spawn.settle()
    runner()

    again, created = rr.queue(ledger, UNHOLD, operator="tom", source="web")

    assert created is True
    assert again.id != first.id


# --------------------------------------------------------------------------- #
# The drive runs it — once
# --------------------------------------------------------------------------- #


def test_the_drive_runs_a_request_once_as_the_operator(
    ledger: Any, config: Any
) -> None:
    request, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web", item_id="12")
    spawn = _Spawner(output="unheld #12")
    runner = _runner(ledger, config, spawn)

    runner()
    runner()  # a second pass while the child runs starts nothing
    spawn.settle()
    runner()  # reaps it
    runner()  # and never runs it again

    assert spawn.calls == [(["epic", "unhold", "epic:a", "12"], "tom")]
    done = rr.get_request(ledger, request.id)
    assert done is not None
    assert (done.state, done.exit_code) == (rr.DONE, 0)
    assert done.detail == "unheld #12"
    for event in ("run_request_started", "run_request_finished"):
        [row] = _events(ledger, event)
        assert row.actor_id == "tom"
        assert row.item_id == "12"
        assert row.payload["request_id"] == request.id


def test_two_drives_racing_for_one_request_run_it_once(
    ledger: Any, config: Any, tmp_path: Path
) -> None:
    """Two executors on two connections: the claim is a compare-and-set, so
    only one of them starts a child."""
    rr.queue(ledger, UNHOLD, operator="tom", source="web")
    spawn = _Spawner()
    first = _runner(ledger, config, spawn)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as other:
        stale = rr.list_requests(other)[0]
        first()
        # The second drive acts on the row as it read it before the claim.
        assert rr.claim(other, stale, drive="other", host="h", log="x") is None
        second = _runner(other, config, spawn, first.lease)
        second()
    spawn.settle()
    assert len(spawn.calls) == 1


def test_concurrent_same_pair_queues_with_different_keys_create_and_execute_one_request(
    tmp_path: Path, config: Any
) -> None:
    """Two submissions on two connections, each with its own key: the first
    stops right after its active pointer is published, before it answers, and
    the second queues meanwhile. The pointer, the row, the key and the fact are
    one write, so the second sees the pair taken and is refused, then and once
    the first is acknowledged: it never writes its own request."""
    path = str(tmp_path / "dispatch.db")
    published, resumed = threading.Barrier(2), threading.Barrier(2)
    first: dict[str, Any] = {}

    def pause_after_pointer(store: Any, name: str) -> None:
        write = getattr(store, name)

        def paused(*args: Any, **kwargs: Any) -> bool:
            done = write(*args, **kwargs)
            keys = [args[0]] if name == "compare_set_meta" else [w[0] for w in args[0]]
            if (
                done
                and not first.get("paused")
                and any(k.startswith(rr.ACTIVE_PREFIX) for k in keys)
            ):
                first["paused"] = True
                published.wait(timeout=30)
                resumed.wait(timeout=30)
            return done

        setattr(store, name, paused)

    def submit_first() -> None:
        with SqliteRunStore(path) as store:
            pause_after_pointer(store, "compare_set_meta")
            pause_after_pointer(store, "append_audit_event_with_metas")
            first["result"] = svc.queue_request(
                store, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
            )

    with SqliteRunStore(path) as ledger:
        lease = _live_drive(ledger)
        worker = threading.Thread(target=submit_first)
        worker.start()
        second_key = "f" * 32
        try:
            published.wait(timeout=30)
            with pytest.raises(svc.PairBusy):
                svc.queue_request(
                    ledger,
                    command=UNHOLD,
                    item_id="12",
                    source="web",
                    config=_CFG,
                    key=second_key,
                )
            resumed.wait(timeout=30)
        except BaseException:
            # A failure above must not leave the first submission parked.
            resumed.abort()
            raise
        finally:
            worker.join(timeout=30)
        with pytest.raises(svc.PairBusy):
            svc.queue_request(
                ledger,
                command=UNHOLD,
                item_id="12",
                source="web",
                config=_CFG,
                key=second_key,
            )

        assert first["result"]["created"] is True
        assert len(rr.list_requests(ledger)) == 1
        spawn = _Spawner()
        runner = _runner(ledger, config, spawn, lease)
        runner()
        spawn.settle()
        runner()
        assert len(spawn.calls) == 1


def test_a_failing_command_is_failed_with_its_output(ledger: Any, config: Any) -> None:
    request, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")
    spawn = _Spawner(code=2, output="kew epic unhold: actor 'tom' is not an approver")
    runner = _runner(ledger, config, spawn)
    runner()
    spawn.settle()
    runner()

    failed = rr.get_request(ledger, request.id)
    assert failed is not None
    assert (failed.state, failed.exit_code) == (rr.FAILED, 2)
    assert "not an approver" in (failed.detail or "")


def test_requests_run_one_at_a_time_in_queue_order(ledger: Any, config: Any) -> None:
    rr.queue(ledger, UNHOLD, operator="tom", source="web")
    rr.queue(ledger, "kew dispatch 7", operator="ana", source="cli")
    spawn = _Spawner()
    runner = _runner(ledger, config, spawn)

    runner()
    assert [argv for argv, _ in spawn.calls] == [["epic", "unhold", "epic:a", "12"]]
    spawn.settle()
    runner()  # reaps the first, starts the second
    assert spawn.calls[1] == (["dispatch", "7"], "ana")


def test_a_child_that_cannot_start_fails_the_request(ledger: Any, config: Any) -> None:
    request, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")

    def spawn(_argv: list[str], _log: Path, _actor: str, _env: dict[str, str]) -> Any:
        raise ValueError("Configured executable digest mismatch")

    _runner(ledger, config, spawn)()

    failed = rr.get_request(ledger, request.id)
    assert failed is not None
    assert failed.state == rr.FAILED
    assert "digest mismatch" in (failed.detail or "")


def test_a_row_rewritten_to_an_unrunnable_command_is_refused_by_the_drive(
    ledger: Any, config: Any
) -> None:
    """The drive re-parses the stored text: a row written around the queue
    function runs nothing."""
    request, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")
    forged = rr.RunRequest(**{**request.to_json(), "command": "kew epic run epic:a"})
    assert ledger.compare_set_meta(request.key, request.encode(), forged.encode())
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    refused = rr.get_request(ledger, request.id)
    assert refused is not None and refused.state == rr.REFUSED


# --------------------------------------------------------------------------- #
# Refusals reach the block
# --------------------------------------------------------------------------- #


def test_a_stopped_fleet_refuses_the_request(ledger: Any, config: Any) -> None:
    request, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")
    ledger.set_fleet_stopped(reason="budget breach")
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    refused = rr.get_request(ledger, request.id)
    assert refused is not None
    assert refused.state == rr.REFUSED
    assert "the fleet is stopped (budget breach)" in (refused.detail or "")
    [row] = _events(ledger, "run_request_refused")
    assert row.actor_id == "tom"
    assert "fleet is stopped" in row.payload["reason"]


def test_a_held_drive_refuses_the_request(ledger: Any, config: Any) -> None:
    request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    hold(ledger)

    _runner(ledger, config, _Spawner())()

    refused = rr.get_request(ledger, request.id)
    assert refused is not None
    assert refused.state == rr.REFUSED
    assert "the drive is held" in (refused.detail or "")


def test_a_spent_daily_budget_refuses_the_request(
    ledger: Any, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kew.service.palette_ask as palette_ask

    request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    monkeypatch.setattr(
        palette_ask, "daily_budget_reached", lambda *_a, **_k: (25.0, 20.0)
    )

    _runner(ledger, config, _Spawner())()

    refused = rr.get_request(ledger, request.id)
    assert refused is not None
    assert refused.state == rr.REFUSED
    assert "daily budget of $20.00" in (refused.detail or "")


# --------------------------------------------------------------------------- #
# A drive that exited mid-request
# --------------------------------------------------------------------------- #


def _orphan(
    store: Any, pid: int | None, claimed_at: datetime | None = None
) -> rr.RunRequest:
    import socket

    request, _ = rr.queue(store, UNHOLD, operator="tom", source="web")
    claimed = rr.claim(
        store,
        request,
        drive="gone",
        host=socket.gethostname(),
        log="",
        now=claimed_at,
    )
    assert claimed is not None
    return claimed if pid is None else rr.started(store, claimed, pid)


def test_a_request_left_running_by_a_dead_drive_is_unknown_never_rerun(
    ledger: Any, config: Any
) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    orphan = _orphan(ledger, dead.pid)
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    settled = rr.get_request(ledger, orphan.id)
    assert settled is not None
    assert settled.state == rr.UNKNOWN
    assert "outcome is unknown" in (settled.detail or "")


def test_a_live_orphan_child_holds_the_queue(ledger: Any, config: Any) -> None:
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        orphan = _orphan(ledger, live.pid)
        rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
        spawn = _Spawner()

        _runner(ledger, config, spawn)()

        assert spawn.calls == []
        still = rr.get_request(ledger, orphan.id)
        assert still is not None and still.state == rr.RUNNING
    finally:
        live.kill()
        live.wait()


def test_an_orphan_with_no_recorded_pid_holds_the_queue_for_the_grace(
    ledger: Any, config: Any
) -> None:
    # Its drive died between the claim and the pid write: the child may still
    # run, so a second request must not start alongside it.
    orphan = _orphan(ledger, None)
    rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    still = rr.get_request(ledger, orphan.id)
    assert still is not None and still.state == rr.RUNNING


def test_an_orphan_with_no_recorded_pid_is_unknown_after_the_grace(
    ledger: Any, config: Any
) -> None:
    from kew.drive_requests import UNSEEN_CHILD_GRACE

    long_ago = datetime.now(UTC) - UNSEEN_CHILD_GRACE - timedelta(minutes=1)
    orphan = _orphan(ledger, None, claimed_at=long_ago)
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    settled = rr.get_request(ledger, orphan.id)
    assert settled is not None and settled.state == rr.UNKNOWN


# --------------------------------------------------------------------------- #
# A request that waited too long
# --------------------------------------------------------------------------- #


def test_a_stale_queued_request_holds_its_pair_until_the_drive_refuses_it(
    ledger: Any, config: Any
) -> None:
    """#2975: a pair has one nonterminal request whatever its age. A click on
    a pair whose request sat queued past QUEUE_TTL gets that request back, a
    keyed one is refused; once the drive refuses the stale request, a new
    click queues anew and runs."""
    old = datetime.now(UTC) - rr.QUEUE_TTL - timedelta(days=30)
    stale, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web", now=old)

    joined, created = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    assert created is False and joined.id == stale.id
    with pytest.raises(rr.PairBusy, match=stale.id):
        rr.queue(ledger, "kew dispatch 7", operator="tom", source="web", key="k" * 32)
    assert [r.id for r in rr.list_requests(ledger)] == [stale.id]

    spawn = _Spawner()
    runner = _runner(ledger, config, spawn)
    runner()
    assert spawn.calls == []
    refused = rr.get_request(ledger, stale.id)
    assert refused is not None and refused.state == rr.REFUSED
    assert "queue it again" in (refused.detail or "")

    fresh, created = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    assert created is True and fresh.id != stale.id
    runner()
    spawn.settle()
    assert spawn.calls == [(["dispatch", "7"], "tom")]
    ran = rr.get_request(ledger, fresh.id)
    assert ran is not None and ran.state == rr.RUNNING


def test_a_request_row_written_before_superseded_by_existed_still_runs(
    ledger: Any, config: Any
) -> None:
    """#2975: a queued row stored by the kew before the drive fence has no
    ``superseded_by`` key. The claim's compare-and-set must still match it, or
    the row stays queued forever, holds its pair and stops the queue behind
    it."""
    request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    stored = request.to_json()
    del stored["superseded_by"]
    legacy = json.dumps(stored, sort_keys=True)
    assert ledger.compare_set_meta(request.key, ledger.get_meta(request.key), legacy)
    behind, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")

    spawn = _Spawner()
    runner = _runner(ledger, config, spawn)
    for _ in range(3):
        runner()
        spawn.settle()

    assert [argv for argv, _ in spawn.calls] == [
        ["dispatch", "7"],
        ["epic", "unhold", "epic:a", "12"],
    ]
    ran = rr.get_request(ledger, request.id)
    assert ran is not None and ran.state != rr.QUEUED
    after = rr.get_request(ledger, behind.id)
    assert after is not None and after.state != rr.QUEUED


def test_an_operator_dropped_from_the_approvers_is_refused_at_the_drive(
    ledger: Any, config: Any
) -> None:
    request, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")
    config.approval.approvers = ["ana"]
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    refused = rr.get_request(ledger, request.id)
    assert refused is not None and refused.state == rr.REFUSED
    assert "approvers" in (refused.detail or "")


# --------------------------------------------------------------------------- #
# The drive fence: a copied command already ran it (#2975)
# --------------------------------------------------------------------------- #


def _run(
    store: Any, item: str, *, status: Any, started: datetime, kind: str = "dispatch"
) -> str:
    run_id = f"run-{item}-{status}-{kind}"
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item) if item.isdigit() else 0,
            item_id=item,
            branch=f"agent/issue-{item}",
            status=status,
            exit_code=0,
            started_at=started,
            run_kind=kind,
        )
    )
    return run_id


@pytest.mark.parametrize(
    ("status", "kind", "after"),
    [
        ("pending", "dispatch", False),  # in flight, whenever it started
        ("pending", "review", False),  # any run in flight on the item
        ("success", "dispatch", True),  # a pasted dispatch that already finished
    ],
)
def test_a_dispatch_whose_item_ran_since_it_was_queued_is_superseded_and_never_spawned(
    ledger: Any, config: Any, status: str, kind: str, after: bool
) -> None:
    """The drive fence: just before the drive starts a queued dispatch, the
    claim reads the item's runs in the same transaction. A run in flight, or a
    dispatch run that started after the request was queued, settles the
    request ``superseded`` naming that run; nothing is spawned, the pair is
    free and the step is audited."""
    queued_at = datetime.now(UTC) - timedelta(minutes=5)
    request, _ = rr.queue(
        ledger, "kew dispatch 2060", operator="tom", source="web", now=queued_at
    )
    started = queued_at + (timedelta(minutes=1) if after else -timedelta(hours=2))
    run_id = _run(ledger, "2060", status=status, started=started, kind=kind)
    spawn = _Spawner()

    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    settled = rr.get_request(ledger, request.id)
    assert settled is not None and settled.state == rr.SUPERSEDED
    assert settled.terminal and settled.superseded_by == run_id
    assert run_id in (settled.detail or "")
    [row] = _events(ledger, "run_request_superseded")
    assert row.payload["superseded_by"] == run_id
    assert rr.active_request(ledger, "kew dispatch 2060") is None


def test_a_dispatch_run_older_than_the_request_does_not_fence_it(
    ledger: Any, config: Any
) -> None:
    """A finished dispatch from before the press is history, not a copy of it:
    the request runs. So does a request for another item."""
    queued_at = datetime.now(UTC) - timedelta(minutes=5)
    _run(ledger, "2060", status="success", started=queued_at - timedelta(hours=1))
    _run(ledger, "2061", status="pending", started=queued_at)
    request, _ = rr.queue(
        ledger, "kew dispatch 2060", operator="tom", source="web", now=queued_at
    )
    spawn = _Spawner()

    _runner(ledger, config, spawn)()
    spawn.settle()

    assert spawn.calls == [(["dispatch", "2060"], "tom")]
    ran = rr.get_request(ledger, request.id)
    assert ran is not None and ran.state == rr.RUNNING


def test_a_run_recorded_after_the_drive_read_the_queue_still_fences_the_claim(
    ledger: Any, tmp_path: Path
) -> None:
    """The fence is in the claim's own transaction, not the drive's earlier
    read: a copied dispatch that records its run on another connection after
    the drive read the request queued still supersedes it."""
    request, _ = rr.queue(ledger, "kew dispatch SFD-9", operator="tom", source="web")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as other:
        run_id = _run(other, "SFD-9", status="pending", started=datetime.now(UTC))

    claimed = rr.claim(ledger, request, drive="d", host="h", log="x")

    assert claimed is not None and claimed.state == rr.SUPERSEDED
    assert claimed.superseded_by == run_id
    stored = rr.get_request(ledger, request.id)
    assert stored == claimed
    # A row the claim already settled is not claimed again.
    assert rr.claim(ledger, request, drive="d", host="h", log="x") is None


def test_the_fence_holds_only_dispatch_requests(ledger: Any, config: Any) -> None:
    """Only a dispatch is fenced on its item's runs: an epic verb's request
    on an item with a run in flight is the drive's to run as before."""
    _run(ledger, "12", status="pending", started=datetime.now(UTC))
    rr.queue(ledger, UNHOLD, operator="tom", source="web", item_id="12")
    spawn = _Spawner()

    _runner(ledger, config, spawn)()
    spawn.settle()

    assert spawn.calls == [(["epic", "unhold", "epic:a", "12"], "tom")]


# --------------------------------------------------------------------------- #
# The drive loop calls the executor
# --------------------------------------------------------------------------- #


def test_the_drive_runs_queued_requests_each_round(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    passes: list[str] = []

    with SqliteRunStore(str(log_dir / "dispatch.db")) as shell:
        shell.register_epic("epic:a")

        def tick(_name: str, _cap: int) -> int:
            holder = current_holder(shell)
            assert holder is not None
            request_stop(shell, holder, None)
            return 10

        def factory(_store: Any, lease: DriveLease) -> Any:
            passes.append(lease.holder.token)
            return lambda: passes.append("pass")

        drive_cli.run_drive(cfg, tick, interval=1, requests=factory)

    # Built once with the drive's own lease, run before the round and after
    # the tick.
    assert passes[1:] == ["pass", "pass"]


def test_the_console_offers_run_for_exactly_the_runnable_set() -> None:
    """The console's ``RUNNABLE`` mirrors this module's: a Run the drive would
    refuse is a button that can only fail, and a runnable command without one
    is a paste the operator need not make."""
    import re

    source = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "lib"
        / "run-request.ts"
    ).read_text()
    block = source[source.index("export const RUNNABLE") :]
    block = block[: block.index("];")]
    entries = re.findall(r"\[\[([^\]]*)\],\s*(\d+)\]", block)
    console = {
        tuple(re.findall(r"'([^']+)'", verb)): int(arity) for verb, arity in entries
    }
    assert console == rr.RUNNABLE


# --------------------------------------------------------------------------- #
# kew run-request
# --------------------------------------------------------------------------- #


def _cli(config: DispatchConfig, monkeypatch: pytest.MonkeyPatch, *args: str) -> Any:
    from typer.testing import CliRunner

    import kew.cli as cli
    from kew.cli import app

    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)
    return CliRunner().invoke(app, ["run-request", *args])


def test_the_cli_queues_what_the_console_queues(
    ledger: Any, config: DispatchConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _live_drive(ledger)
    result = _cli(config, monkeypatch, "queue", "--item", "12", *UNHOLD.split())

    assert result.exit_code == 0, result.output
    [request] = rr.list_requests(ledger)
    assert (request.command, request.source, request.operator) == (UNHOLD, "cli", "tom")
    [event] = _events(ledger, "run_request_queued")
    assert event.actor_id == "tom"

    listed = _cli(config, monkeypatch, "list")
    assert UNHOLD in listed.output
    shown = _cli(config, monkeypatch, "show", request.id, "--json")
    assert '"state": "queued"' in shown.output


def test_the_cli_refuses_with_no_drive_and_for_an_unrunnable_command(
    ledger: Any, config: DispatchConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _cli(config, monkeypatch, "queue", *UNHOLD.split()).exit_code == 1
    _live_drive(ledger)
    assert _cli(config, monkeypatch, "queue", "kew", "epic", "run", "e").exit_code == 2
    assert rr.list_requests(ledger) == []
    assert _cli(config, monkeypatch, "show", "nope").exit_code == 1


def test_a_new_request_is_found_behind_any_number_of_settled_ones(
    ledger: Any, config: Any
) -> None:
    """Rows are never deleted: the executor must find a request queued after
    hundreds of settled ones, on its first (full) read and on later
    (windowed) ones alike."""
    for _ in range(520):  # past the 500-row cap the first version read
        old, _ = rr.queue(ledger, UNHOLD, operator="tom", source="web")
        assert rr.refuse(ledger, old, "old", drive="d") is not None
    spawn = _Spawner()
    runner = _runner(ledger, config, spawn)
    rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")

    runner()  # the first, full read
    spawn.settle()
    runner()  # reaps
    rr.queue(ledger, "kew dispatch 8", operator="tom", source="web")
    runner()  # a windowed read

    assert [argv for argv, _ in spawn.calls] == [["dispatch", "7"], ["dispatch", "8"]]
    assert runner.unsettled is not None and len(runner.unsettled) == 1


# --------------------------------------------------------------------------- #
# A launchpad model (#2741)
# --------------------------------------------------------------------------- #

DISPATCH = "kew dispatch 42"


def _routed(runner: str, model: str) -> DispatchConfig:
    from kew.config import RouteEntry, RoutingConfig

    return DispatchConfig(
        routing=RoutingConfig(default=RouteEntry(runner=runner, model=model))
    )


def test_the_drive_runs_a_model_as_its_flag_and_attests_the_source(
    ledger: Any, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kew.drive_requests as dr

    monkeypatch.setattr(
        dr, "_effective_config", lambda _s: _routed("claude-code", "claude-opus-5")
    )
    request, _ = rr.queue(
        ledger, DISPATCH, operator="tom", source="web", model="claude-sonnet-5-5"
    )
    spawn = _Spawner()
    runner = _runner(ledger, config, spawn)
    runner()
    spawn.settle()

    assert spawn.calls == [(["dispatch", "42", "--model", "claude-sonnet-5-5"], "tom")]
    assert spawn.envs == [{rr.MODEL_SOURCE_ENV: rr.LAUNCHPAD_MODEL_SOURCE}]


def test_a_plain_request_gains_no_model_and_no_source(ledger: Any, config: Any) -> None:
    rr.queue(ledger, DISPATCH, operator="tom", source="web")
    spawn = _Spawner()
    _runner(ledger, config, spawn)()
    spawn.settle()
    assert spawn.calls == [(["dispatch", "42"], "tom")]
    assert spawn.envs == [{}]


def test_the_drive_refuses_a_model_the_route_no_longer_fits(
    ledger: Any, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked when queued AND when taken: the route may move while it waits."""
    import kew.drive_requests as dr

    monkeypatch.setattr(
        dr, "_effective_config", lambda _s: _routed("codex", "gpt-5.6-sol")
    )
    request, _ = rr.queue(
        ledger, DISPATCH, operator="tom", source="web", model="claude-sonnet-5-5"
    )
    spawn = _Spawner()
    _runner(ledger, config, spawn)()

    assert spawn.calls == []
    refused = rr.get_request(ledger, request.id)
    assert refused is not None and refused.state == rr.REFUSED
    assert "runs on claude-code, but this dispatch runs on codex" in (
        refused.detail or ""
    )


def test_a_model_must_fit_every_tier_the_dispatch_could_take(ledger: Any) -> None:
    """The tier is read off the item's labels by the dispatch, so a model on
    one tier's runner is refused when another tier routes elsewhere."""
    from kew.config import RouteEntry, RoutingConfig

    split = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-5"),
            simple=RouteEntry(runner="codex", model="gpt-5.6-sol"),
        )
    )
    with pytest.raises(rr.ModelRefused, match="claude-code or codex"):
        rr.check_model(split, ["dispatch", "42"], "claude-sonnet-5-5")
    rr.check_model(
        _routed("claude-code", "claude-opus-5"), ["dispatch", "42"], "claude-sonnet-5-5"
    )


def test_a_row_written_around_queue_cannot_smuggle_an_option(ledger: Any) -> None:
    bad = rr.RunRequest(
        id="x",
        command=DISPATCH,
        operator="tom",
        requested_at=datetime.now(UTC).isoformat(),
        source="web",
        model="--dangerously-skip",
    )
    with pytest.raises(rr.NotRunnable):
        rr.request_argv(bad)
    with pytest.raises(rr.NotRunnable):
        rr.request_argv(
            rr.RunRequest(
                **{**bad.to_json(), "command": UNHOLD, "model": "claude-sonnet-5-5"}
            )
        )


# --------------------------------------------------------------------------- #
# A submission key: a press asked again is the same press (#2975)
# --------------------------------------------------------------------------- #

KEY = "ab" * 16  # a fake submission key, low entropy so the leak scan stays quiet


def test_a_press_asked_again_gets_its_request_even_once_it_settled(
    ledger: Any, config: Any
) -> None:
    lease = _live_drive(ledger)
    first = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    spawn = _Spawner(code=1, output="boom")
    runner = _runner(ledger, config, spawn, lease)
    runner()
    spawn.settle()
    runner()

    again = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )

    assert first["created"] is True and again["created"] is False
    assert again["request"]["id"] == first["request"]["id"]
    assert again["request"]["state"] == rr.FAILED
    assert len(rr.list_requests(ledger)) == 1
    assert len(_events(ledger, "run_request_queued")) == 1


def test_a_keyed_press_on_a_pair_already_live_is_refused_and_keeps_no_key(
    ledger: Any, config: Any
) -> None:
    lease = _live_drive(ledger)
    live = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="cli", config=_CFG
    )

    with pytest.raises(svc.PairBusy, match=live["request"]["id"]):
        svc.queue_request(
            ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
        )
    assert len(rr.list_requests(ledger)) == 1

    # The refused press queued nothing, so its key names nothing: asked again
    # once the pair is free, it queues the one request it stands for.
    spawn = _Spawner()
    runner = _runner(ledger, config, spawn, lease)
    runner()
    spawn.settle()
    runner()
    fresh = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    again = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    assert fresh["created"] is True and fresh["request"]["id"] != live["request"]["id"]
    assert again["request"]["id"] == fresh["request"]["id"]
    assert len(rr.list_requests(ledger)) == 2


def test_a_refused_press_asked_again_decides_afresh_and_queues_once(
    ledger: Any,
) -> None:
    with pytest.raises(svc.NoDriveRunning, match="no drive running"):
        svc.queue_request(
            ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
        )
    assert rr.list_requests(ledger) == []
    _live_drive(ledger)

    first = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    again = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )

    assert first["created"] is True and again["created"] is False
    assert again["request"]["id"] == first["request"]["id"]
    assert len(rr.list_requests(ledger)) == 1


def test_a_press_whose_answer_was_lost_after_it_ran_is_never_run_again(
    ledger: Any, config: Any
) -> None:
    """The service answered, the answer never reached the console, and the
    drive finished the request before the console asked again: the retry gets
    the finished request back, and the drive runs nothing more."""
    lease = _live_drive(ledger)
    svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    spawn = _Spawner()
    runner = _runner(ledger, config, spawn, lease)
    runner()
    spawn.settle()
    runner()

    again = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    runner()
    spawn.settle()
    runner()

    assert (again["created"], again["request"]["state"]) == (False, rr.DONE)
    assert len(spawn.calls) == 1
    assert len(rr.list_requests(ledger)) == 1


def test_a_key_sent_again_with_another_command_or_model_is_refused(
    ledger: Any,
) -> None:
    _live_drive(ledger)
    svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )

    with pytest.raises(svc.KeyReused):
        svc.queue_request(
            ledger,
            command="kew epic unhold epic:a 13",
            item_id="13",
            source="web",
            config=_CFG,
            key=KEY,
        )
    assert len(rr.list_requests(ledger)) == 1


# --------------------------------------------------------------------------- #
# A request and its queued fact are one write (#2975)
# --------------------------------------------------------------------------- #


# The child a kill test queues in: it presses Run under *key* and dies by
# ``os._exit`` — no unwinding, no rollback, no commit — at *when*: ``before``
# once the queued fact is chained but not committed, ``after`` once the one
# transaction has committed and before any answer.
_QUEUE_THEN_DIE = """
import os, sys
from kew.config import DispatchConfig, ProjectConfig
from kew.run_store import SqliteRunStore
from kew.service import run_requests as svc

path, when, command, key = sys.argv[1:]
point = (
    "_chained_insert_in_transaction"
    if when == "before"
    else "append_audit_event_with_metas"
)
real = getattr(SqliteRunStore, point)

def die(self, *args, **kwargs):
    done = real(self, *args, **kwargs)
    if when == "after" or args[0][1] == "run_request_queued":
        os._exit(9)
    return done

setattr(SqliteRunStore, point, die)
store = SqliteRunStore(path)
svc.queue_request(
    store,
    command=command,
    item_id="12",
    source="web",
    config=DispatchConfig(project=ProjectConfig(repo="o/r")),
    key=key,
)
os._exit(0)
"""


def _queue_then_die(path: Path, when: str) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _QUEUE_THEN_DIE, str(path), when, UNHOLD, KEY],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == 9, child.stderr


def _drive_twice(store: Any, config: DispatchConfig, lease: DriveLease) -> _Spawner:
    spawn = _Spawner()
    runner = _runner(store, config, spawn, lease)
    runner()
    spawn.settle()
    runner()
    return spawn


def test_a_kill_after_the_atomic_commit_replays_and_runs_the_original_once(
    tmp_path: Path, ledger: Any, config: Any
) -> None:
    lease = _live_drive(ledger)
    _queue_then_die(tmp_path / "dispatch.db", "after")

    [left] = rr.list_requests(ledger)
    [fact] = _events(ledger, "run_request_queued")
    assert fact.payload["request_id"] == left.id
    again = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    assert (again["created"], again["request"]["id"]) == (False, left.id)

    spawn = _drive_twice(ledger, config, lease)
    last = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )

    assert len(spawn.calls) == 1
    assert (last["request"]["id"], last["request"]["state"]) == (left.id, rr.DONE)
    assert len(rr.list_requests(ledger)) == 1
    assert len(_events(ledger, "run_request_queued")) == 1


def test_a_kill_before_the_atomic_commit_leaves_nothing_and_the_retry_queues_once(
    tmp_path: Path, ledger: Any, config: Any
) -> None:
    lease = _live_drive(ledger)
    _queue_then_die(tmp_path / "dispatch.db", "before")

    assert rr.list_requests(ledger) == []
    assert _events(ledger, "run_request_queued") == []
    first = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    spawn = _drive_twice(ledger, config, lease)
    again = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )

    assert first["created"] is True
    assert (again["created"], again["request"]["id"]) == (False, first["request"]["id"])
    assert len(spawn.calls) == 1
    [fact] = _events(ledger, "run_request_queued")
    assert fact.payload["request_id"] == first["request"]["id"]


def _fail_queued_facts(monkeypatch: pytest.MonkeyPatch, store: Any) -> list[str]:
    """Make the chain refuse every ``run_request_queued`` row, as a full disk
    or a corrupt audit table would; returns the events refused."""
    refused: list[str] = []
    insert = store._chained_insert_in_transaction

    def failing(values: tuple[Any, ...], **kwargs: Any) -> Any:
        if values[1] == "run_request_queued":
            refused.append(values[1])
            raise OSError("the audit table is locked")
        return insert(values, **kwargs)

    monkeypatch.setattr(store, "_chained_insert_in_transaction", failing)
    return refused


def test_a_queued_fact_that_fails_writes_nothing_and_the_retry_queues_once(
    ledger: Any, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _live_drive(ledger)
    refused = _fail_queued_facts(monkeypatch, ledger)
    for _ in range(3):
        with pytest.raises(rr.QueuedFactFailed, match="nothing was queued"):
            svc.queue_request(
                ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
            )
    assert refused == ["run_request_queued"] * 3
    assert rr.list_requests(ledger) == []
    assert _events(ledger, "run_request_queued") == []

    # Nothing was written under the key, the row or the pointer: once the
    # audit takes the fact, the same press queues its one request.
    monkeypatch.undo()
    retry = svc.queue_request(
        ledger, command=UNHOLD, item_id="12", source="web", config=_CFG, key=KEY
    )
    assert retry["created"] is True
    [fact] = _events(ledger, "run_request_queued")
    assert fact.payload["request_id"] == retry["request"]["id"]
    spawn = _drive_twice(ledger, config, lease)
    assert len(spawn.calls) == 1
    assert len(rr.list_requests(ledger)) == 1
