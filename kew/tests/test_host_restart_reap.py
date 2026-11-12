"""A run the host died under is re-admitted once, not failed (#2812).

When the operator machine rebooted mid-drive, one orphaned run was reaped to
``failed-terminal`` and stalled its node until ``kew epic retry``, and another
held a cap slot for many minutes. A run's owner records the boot it ran under,
so the drive's first tick after start can prove the owner died with an earlier
boot, reap the run, and re-admit its node once.

The fixture owner is this test process's own LIVE process group: after a
reboot, a recorded pid may well name a stranger, and the boot is what tells
the two apart. Its identity comes from ``identity_at_admission``, exactly what
a dispatch records.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.checkout_generations import fixture_claim_evidence
from support.driver_fakes import (
    EPIC,
    NOW,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _config,
    _facts,
    _node,
)
from support.verify_lease_holder import lease_held

from kew import process_groups, subprocess_utils
from kew.graph.build import build_graph
from kew.graph.driver.attention import AttentionKind, attention_events
from kew.graph.driver.tick import _pending_run_is_dead, summarize
from kew.process_groups import from_earlier_boot, identity_at_admission, machine_boot
from kew.run_store import NO_EARLIER_RETRY, RunRecord
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.verify_lease import LAUNCH_DEFERRED_EVENT
from kew.waits import (
    HOST_RESTART_REASON,
    HOST_RESTART_REPEATED_CONTINUATION_REASON,
    bucket_for,
    fill_command,
)

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

#: Where the faked docker CLI answers from; nothing is installed there.
_DOCKER = "/usr/bin/docker"

#: The operator guidance an unprovable-clone hold's reason ends with.
_CLONE_RECOVERY_GUIDANCE = "recover its work by hand; kew epic unhold re-dispatches it"

_FAILURE_STATUSES = {"failure", "killed", "budget_exceeded", "budget_stopped"}


@pytest.fixture(autouse=True)
def _this_machine_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the machine id: the host running the tests may have none (#2812)."""
    machine_id = tmp_path / "pinned-machine-id"
    machine_id.write_text("c" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (machine_id,))
    process_groups._this_machine.cache_clear()
    yield
    process_groups._this_machine.cache_clear()


@pytest.fixture(autouse=True)
def _no_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess_utils._register_optional_tool("git")
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)


def _current_boot() -> str:
    boot = machine_boot()
    assert boot is not None, "Linux and macOS both say their boot id"
    return boot


def _earlier_boot() -> str:
    machine, _, boot = _current_boot().rpartition("/")
    return f"{machine}/{'0' * 32 if boot != '0' * 32 else '1' * 32}"


def _pending_run(run_id: str, *, boot: str, started_at: datetime = NOW) -> RunRecord:
    """A pending dispatch row whose owner is this process's live group."""
    owner = os.getpgrp()
    identity = identity_at_admission(owner, require_token=True)
    return RunRecord(
        run_id=run_id,
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        status="pending",
        exit_code=-1,
        started_at=started_at,
        pgid=owner,
        owner_pid=owner,
        **{**identity, "owner_boot_id": boot},
    )


def _reserve(store: Any, run_id: str) -> Any:
    assert store.reserve_epic_node_item(EPIC, "1010", run_id) is True
    [reservation] = store.list_epic_reservations(EPIC)
    return reservation


def _admit_bound_pending(store: Any, reservation: Any, pending: RunRecord) -> None:
    """Admit a dispatch child pinned to its distinct parent reservation."""
    identity = store.build_admission_identity(
        pending.run_id,
        tracker="github",
        item_id="1010",
        repo=None,
    )
    assert store.admit_run_with_identity(
        pending,
        identity,
        reservation=reservation,
        reservation_pinned=True,
    )


def _tick(
    store: Any,
    tmp_path: Path,
    *,
    now: datetime = NOW,
    config: Any | None = None,
    max_parallel: int = 5,
    sink: Any | None = None,
    deps: FakeSource | None = None,
) -> tuple[Any, Any]:
    sink = FakeSink() if sink is None else sink
    deps = FakeSource(nodes=[_node("1010")], edges=[]) if deps is None else deps
    result = _advance(
        deps=deps,
        facts=FakeFactsReader(
            issue_facts_by_num={
                int(n.id): _facts(int(n.id), state="OPEN")
                for n in deps.list_epic_nodes(EPIC)
            }
        ),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        now=now,
        config=config,
        max_parallel=max_parallel,
    )
    return result, sink


def _pending_run_with_checkout(
    store: Any, tmp_path: Path, run_id: str, *, dirty: bool, checkout: bool = False
) -> tuple[Any, Path]:
    """Bind one earlier-boot child to its production-shaped checkout.

    ``checkout`` uses the checkout publication mode for dirty work too, where
    ``git status`` (not a fetch of the bound base) says what the bytes hold.
    """
    from kew.dispatch_core import _bind_publication_base
    from kew.worktree import write_checkout_generation

    config = _config()
    if not dirty or checkout:
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = tmp_path / "worktrees" / "agent" / "issue-1010"
    worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch", "agent/issue-1010", str(worktree)],
        check=True,
        capture_output=True,
    )
    write_checkout_generation(worktree)
    if dirty:
        worktree.joinpath("wip.txt").write_text("preserve me\n", encoding="utf-8")
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(store, reservation, _pending_run(run_id, boot=_earlier_boot()))
    _bind_publication_base(
        store,
        publish=config.publication.publish,
        base=_bound_base(config),
        run_id=run_id,
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        checkout=worktree,
    )
    return config, worktree


def _bound_base(config: Any) -> str:
    """The base a dispatch binds: canonical publication records one."""
    return "a" * 40 if config.publication.publish == "canonical" else ""


def _counted_failures(store: Any) -> int:
    """The node's failure runs no retry marker supersedes."""
    retries = {r.item_id: r for r in store.list_epic_retries(EPIC)}
    retry = retries.get("1010")
    return sum(
        1
        for r in store.list_by_item("1010")
        if r.status in _FAILURE_STATUSES
        and (
            retry is None
            or (
                r.run_id not in retry.superseded_run_ids
                and r.started_at >= retry.supersedes_before
            )
        )
    )


def test_an_earlier_boots_pending_run_is_reaped_on_the_first_tick(
    store: Any, tmp_path: Path
) -> None:
    """AC1: one tick, no liveness window — even though the recorded pid is
    alive (it names someone else now)."""
    config, _ = _pending_run_with_checkout(store, tmp_path, "dispatch-1", dirty=False)

    _tick(store, tmp_path, config=config)

    run = store.get("dispatch-1")
    assert run.status == "killed"
    assert run.error_category == "host_restart_reaped"
    [reap] = store.list_audit_events(event="orphaned_run_reaped")
    assert reap.payload["reason"] == "host_restart"


def test_the_node_is_re_admitted_once_without_counting_a_failure(
    store: Any, tmp_path: Path
) -> None:
    """AC2: a retry marker with reason ``host_restart``; the node dispatches
    again and its failure count is unchanged."""
    before = _counted_failures(store)
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store, reservation, _pending_run("dispatch-1", boot=_earlier_boot())
    )

    result, sink = _tick(store, tmp_path)

    assert [r.item_id for r in store.list_epic_retries(EPIC)] == ["1010"]
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == "host_restart"
    assert retry.payload["node"] == "1010"
    assert _counted_failures(store) == before
    assert "1010" not in result.failed
    assert sink.calls == ["1010"]


def test_a_remote_earlier_boot_run_stays_counted_during_admission(
    store: Any,
) -> None:
    """A run placed on another host can outlive this machine's reboot, so its
    slot stays taken: the earlier local boot proves nothing about it."""
    reservation = _reserve(store, "reservation-1")
    remote = _pending_run("dispatch-1", boot=_earlier_boot())
    remote.run_host = "gcp"
    remote.run_host_instance = "kew-verify"
    _admit_bound_pending(store, reservation, remote)
    other = RunRecord(
        run_id="other-run",
        issue_num=2020,
        item_id="2020",
        branch="agent/issue-2020",
        status="pending",
        exit_code=-1,
        started_at=NOW,
    )

    assert not store.try_admit_run(other, cap=1)
    assert store.get("dispatch-1").status == "pending"


def test_admission_before_the_first_tick_defers_the_host_restart_reap(
    store: Any, tmp_path: Path
) -> None:
    """Admission ignores the dead slot but leaves its epic outcome to the tick."""
    before = _counted_failures(store)
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store, reservation, _pending_run("dispatch-1", boot=_earlier_boot())
    )
    other = RunRecord(
        run_id="other-run",
        issue_num=2020,
        item_id="2020",
        branch="agent/issue-2020",
        status="pending",
        exit_code=-1,
        started_at=NOW,
    )

    assert store.try_admit_run(other, cap=1)
    assert store.get("dispatch-1").status == "pending"
    # The admitted run finishes, so its slot no longer holds back the re-admit.
    other.status = "success"
    other.exit_code = 0
    other.finished_at = NOW
    store.save(other)

    result, sink = _tick(store, tmp_path)

    run = store.get("dispatch-1")
    assert run.status == "killed"
    assert run.error_category == "host_restart_reaped"
    retries = store.list_audit_events(event="epic_node_retry")
    assert len(retries) == 1
    assert retries[0].payload["reason"] == HOST_RESTART_REASON
    assert sink.calls == ["1010"]
    assert "1010" not in result.failed
    assert _counted_failures(store) == before


def _containers_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """No container survived the reboot: the docker CLI says so of each one."""
    real_run, real_lookup = subprocess.run, shutil.which

    def lookup(name: str, *args: Any, **kwargs: Any) -> str | None:
        return _DOCKER if name == "docker" else real_lookup(name, *args, **kwargs)

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if list(argv[:2]) == [_DOCKER, "inspect"]:
            return subprocess.CompletedProcess(
                argv, 1, "", f"Error: No such object: {argv[-1]}\n"
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", lookup)
    monkeypatch.setattr(subprocess, "run", run)
    # conftest stubs runtime detection out for the whole suite; a test that
    # reads a container says where its runtime is.
    monkeypatch.setattr(
        "kew.sandbox.detect_runtime_cached",
        lambda config: _DOCKER if config.runtime == "docker" else None,
    )


def _dead_owner_run(run_id: str, *, started_at: datetime = NOW) -> RunRecord:
    """A pending row whose owner and attempt group really have exited.

    A reboot ends every process of the boot before it; a child of this test,
    reaped before the row is written, is that evidence without a reboot.
    """
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        identity = identity_at_admission(child.pid, require_token=True)
    finally:
        child.kill()
        child.wait()
    return RunRecord(
        run_id=run_id,
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        status="pending",
        exit_code=-1,
        started_at=started_at,
        pgid=child.pid,
        owner_pid=child.pid,
        attempt_pgid=child.pid,
        **{**identity, "owner_boot_id": _earlier_boot()},
    )


def _contained(run: RunRecord) -> RunRecord:
    """Give *run* the docker sandbox identity a contained dispatch records."""
    run.container_name = "kew-1010"
    run.container_runtime = "docker"
    run.container_id = "c" * 64
    return run


def test_admission_before_the_first_tick_leaves_a_contained_run_to_the_tick(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued admission that proves a contained earlier-boot run and its
    container gone frees the slot but does not reap it: the tick re-admits the
    node once instead of the run failing as an ordinary orphan (#2971)."""
    _containers_gone(monkeypatch)
    before = _counted_failures(store)
    config = _clone_config(tmp_path)
    config.publication.publish = "checkout"
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(store, reservation, _contained(_dead_owner_run("dispatch-1")))
    _bind_clone(store, config, _clone(tmp_path, dirty=False))
    other = RunRecord(
        run_id="other-run",
        issue_num=2020,
        item_id="2020",
        branch="agent/issue-2020",
        status="pending",
        exit_code=-1,
        started_at=NOW,
    )

    admitted = store.try_admit_run(other, cap=1)

    assert store.get("dispatch-1").status == "pending"
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert admitted
    other.status = "success"
    other.exit_code = 0
    other.finished_at = NOW
    store.save(other)

    result, sink = _tick(store, tmp_path, config=config)

    run = store.get("dispatch-1")
    assert run.status == "killed"
    assert run.error_category == "host_restart_reaped"
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == HOST_RESTART_REASON
    assert sink.calls == ["1010"]
    assert "1010" not in result.failed
    assert _counted_failures(store) == before


def test_a_contained_run_restarted_under_moments_after_it_started_frees_its_slot(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup grace is no reason to keep a contained run from an earlier
    boot counted: the reboot already ended its client, so a container the
    runtime says is gone frees the slot at once, not ten minutes later
    (#2971)."""
    _containers_gone(monkeypatch)
    clock = datetime.now(UTC)
    config = _clone_config(tmp_path)
    config.publication.publish = "checkout"
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _contained(
            _dead_owner_run("dispatch-1", started_at=clock - timedelta(seconds=30))
        ),
    )
    _bind_clone(store, config, _clone(tmp_path, dirty=False))
    other = RunRecord(
        run_id="other-run",
        issue_num=2020,
        item_id="2020",
        branch="agent/issue-2020",
        status="pending",
        exit_code=-1,
        started_at=clock,
    )

    assert store.try_admit_run(other, cap=1)
    assert store.get("dispatch-1").status == "pending"
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    other.status = "success"
    other.exit_code = 0
    other.finished_at = clock
    store.save(other)

    result, sink = _tick(store, tmp_path, now=datetime.now(UTC), config=config)

    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    assert sink.calls == ["1010"]
    assert "1010" not in result.failed


def _containers_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each container survived the reboot stopped: it exists, not running."""
    real_run, real_lookup = subprocess.run, shutil.which

    def lookup(name: str, *args: Any, **kwargs: Any) -> str | None:
        return _DOCKER if name == "docker" else real_lookup(name, *args, **kwargs)

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if list(argv[:2]) == [_DOCKER, "inspect"]:
            return subprocess.CompletedProcess(
                argv, 0, f"{argv[-1]} /kew-1010 false\n", ""
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", lookup)
    monkeypatch.setattr(subprocess, "run", run)


def test_a_contained_run_restarted_under_whose_container_is_stopped_keeps_its_slot(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped container still exists, holding its name, so it frees no
    slot on its own (#3009 F2): admission leaves the run, and its slot, to
    the tick that stops and reaps it."""
    _containers_stopped(monkeypatch)
    clock = datetime.now(UTC)
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _contained(
            _dead_owner_run("dispatch-1", started_at=clock - timedelta(seconds=30))
        ),
    )
    other = RunRecord(
        run_id="other-run",
        issue_num=2020,
        item_id="2020",
        branch="agent/issue-2020",
        status="pending",
        exit_code=-1,
        started_at=clock,
    )

    assert not store.try_admit_run(other, cap=1)
    assert store.get("dispatch-1").status == "pending"
    assert store.get("other-run") is None


def test_a_driver_dying_right_after_the_reap_keeps_one_retry_reason(
    store: Any, tmp_path: Path
) -> None:
    """The reap and the re-admission commit together: a driver killed the
    moment the reaped row is durable leaves the node re-admitted, so the next
    tick dispatches it instead of holding it failed-terminal."""
    reservation = _reserve(store, "reservation-1")
    pending = _pending_run("dispatch-1", boot=_earlier_boot())
    _admit_bound_pending(store, reservation, pending)

    # This is the last store call the driver completed before it died.
    assert store.finalize_orphaned_pending(
        pending.run_id,
        error_category="host_restart_reaped",
        finished_at=NOW,
        reservation=reservation,
        expected_owner=pending,
        retry_reason=HOST_RESTART_REASON,
        retry_repo="o/r",
    )

    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    assert [r.item_id for r in store.list_epic_retries(EPIC)] == ["1010"]
    result, sink = _tick(store, tmp_path)
    assert "1010" not in result.failed
    assert sink.calls == ["1010"]
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == HOST_RESTART_REASON
    assert _counted_failures(store) == 0


def test_a_reap_clock_behind_the_run_still_re_admits_it_once(
    store: Any, tmp_path: Path
) -> None:
    """A reboot clock behind the run's start does not matter: the marker names
    the reaped run, so that run is superseded and the node dispatches (#2969)."""
    started_at = NOW + timedelta(minutes=1)
    reservation = _reserve(store, "reservation-1")
    pending = _pending_run("dispatch-1", boot=_earlier_boot(), started_at=started_at)
    _admit_bound_pending(store, reservation, pending)

    assert store.finalize_orphaned_pending(
        pending.run_id,
        error_category="host_restart_reaped",
        finished_at=NOW,
        reservation=reservation,
        expected_owner=pending,
        retry_reason=HOST_RESTART_REASON,
        retry_repo="o/r",
    )

    [retry] = store.list_epic_retries(EPIC)
    assert retry.superseded_run_ids == {"dispatch-1"}
    result, sink = _tick(store, tmp_path)
    assert "1010" not in result.failed
    assert sink.calls == ["1010"]
    assert _counted_failures(store) == 0


def test_a_second_host_death_fails_the_node_and_asks_a_person_once(
    store: Any, tmp_path: Path
) -> None:
    """AC3: the second host-death reap of the node fails it, writes no
    second retry marker, and the attention stream carries one event for it."""
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store, reservation, _pending_run("dispatch-1", boot=_earlier_boot())
    )
    _tick(store, tmp_path)
    [first_retry] = store.list_epic_retries(EPIC)
    # The re-admitted dispatch's own run, killed by a second host restart.
    [reservation] = store.list_epic_reservations(EPIC)
    later = datetime.now(UTC) + timedelta(seconds=1)
    second = _pending_run("dispatch-2", boot=_earlier_boot(), started_at=later)
    _admit_bound_pending(store, reservation, second)

    result, sink = _tick(store, tmp_path, now=later + timedelta(seconds=1))

    run = store.get("dispatch-2")
    assert run.status == "killed"
    assert run.error_category == "host_restart_repeated"
    assert "1010" in result.failed
    assert sink.calls == []
    assert store.list_epic_retries(EPIC) == [first_retry]
    assert len(store.list_audit_events(event="epic_node_retry")) == 1
    deps = FakeSource(nodes=[_node("1010")], edges=[])
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=later + timedelta(seconds=2),
    )
    events = [e for e in attention_events(EPIC, summary) if e.item == "1010"]
    assert [(e.kind, e.reason) for e in events] == [
        (
            AttentionKind.HOST_RESTART_REPEATED,
            "failed-terminal (exit 137, host_restart_repeated)",
        )
    ]


@pytest.mark.parametrize(
    ("first_start", "second_start", "second_reap"),
    [
        pytest.param(
            NOW + timedelta(hours=1),
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=10),
            id="every-reap-clock-behind-the-first-run",
        ),
        pytest.param(
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            NOW - timedelta(minutes=30),
            id="clock-set-back-after-the-first-reap",
        ),
    ],
)
def test_a_second_host_death_fails_the_node_whatever_the_clocks_say(
    store: Any,
    tmp_path: Path,
    first_start: datetime,
    second_start: datetime,
    second_reap: datetime,
) -> None:
    """#2969: the marker supersedes the first reaped run only. The
    replacement's own host death fails the node with one attention event,
    even when its clock reads earlier than the first run or the first reap."""
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run("dispatch-1", boot=_earlier_boot(), started_at=first_start),
    )
    _, sink = _tick(store, tmp_path)
    assert sink.calls == ["1010"]
    [first_retry] = store.list_epic_retries(EPIC)
    assert first_retry.superseded_run_ids == {"dispatch-1"}
    [reservation] = store.list_epic_reservations(EPIC)
    second = _pending_run("dispatch-2", boot=_earlier_boot(), started_at=second_start)
    _admit_bound_pending(store, reservation, second)

    result, sink = _tick(store, tmp_path, now=second_reap)

    run = store.get("dispatch-2")
    assert run.status == "killed"
    assert run.error_category == "host_restart_repeated"
    assert "1010" in result.failed
    assert sink.calls == []
    assert store.list_epic_retries(EPIC) == [first_retry]
    assert len(store.list_audit_events(event="epic_node_retry")) == 1
    summary = summarize(
        EPIC,
        build_graph(FakeSource(nodes=[_node("1010")], edges=[]), EPIC),
        config=_config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=second_reap + timedelta(seconds=1),
    )
    events = [e for e in attention_events(EPIC, summary) if e.item == "1010"]
    # A repeated host death raises its own attention kind on main (#2971).
    assert [(e.kind, e.reason) for e in events] == [
        (
            AttentionKind.HOST_RESTART_REPEATED,
            "failed-terminal (exit 137, host_restart_repeated)",
        )
    ]


def test_an_operator_retry_still_supersedes_after_a_host_restart(
    store: Any, tmp_path: Path
) -> None:
    """The failure an operator retried stays superseded when the retried run
    then dies with its host: the bound marker names it alongside the reaped
    run, keeps no cutoff of its own, and the operator retry's own time stays
    the review cycle's start."""
    store.save(
        RunRecord(
            run_id="dispatch-0",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=1),
        )
    )
    store.mark_epic_retry_item(EPIC, "1010")
    [operator_retry] = store.list_epic_retries(EPIC)
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store, reservation, _pending_run("dispatch-1", boot=_earlier_boot())
    )

    result, sink = _tick(store, tmp_path)

    assert "1010" not in result.failed
    assert sink.calls == ["1010"]
    [retry] = store.list_epic_retries(EPIC)
    assert retry.retried_at == operator_retry.retried_at
    assert retry.supersedes_before == NO_EARLIER_RETRY
    assert retry.superseded_run_ids == {"dispatch-0", "dispatch-1"}


def test_an_operator_retry_does_not_supersede_the_replacements_host_death(
    store: Any, tmp_path: Path
) -> None:
    """#2969: after an operator retry, the retried run's host death binds the
    marker; the replacement's own host death fails the node even when the
    clock was set back to before the operator retry."""
    operator_now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="dispatch-0",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=operator_now - timedelta(hours=2),
            finished_at=operator_now - timedelta(hours=1),
        )
    )
    store.mark_epic_retry_item(EPIC, "1010")
    [operator_retry] = store.list_epic_retries(EPIC)
    retried_at = operator_retry.retried_at
    reservation = _reserve(store, "reservation-1")
    first = _pending_run(
        "dispatch-1",
        boot=_earlier_boot(),
        started_at=retried_at + timedelta(minutes=1),
    )
    _admit_bound_pending(store, reservation, first)
    _, sink = _tick(store, tmp_path, now=retried_at + timedelta(minutes=2))
    assert sink.calls == ["1010"]
    [reservation] = store.list_epic_reservations(EPIC)
    second = _pending_run(
        "dispatch-2",
        boot=_earlier_boot(),
        started_at=retried_at - timedelta(minutes=55),
    )
    _admit_bound_pending(store, reservation, second)

    result, sink = _tick(store, tmp_path, now=retried_at - timedelta(minutes=50))

    assert store.get("dispatch-2").error_category == "host_restart_repeated"
    assert "1010" in result.failed
    assert sink.calls == []


def test_an_operator_retry_keeps_the_reaped_run_superseded(
    store: Any, tmp_path: Path
) -> None:
    """#2969: a later time-based retry keeps the marker's run binding, so a
    reaped run whose clock reads ahead of the retry never fails the node again
    once its replacement has succeeded."""
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-1", boot=_earlier_boot(), started_at=NOW + timedelta(hours=1)
        ),
    )
    _, sink = _tick(store, tmp_path)
    assert sink.calls == ["1010"]
    store.save(
        RunRecord(
            run_id="dispatch-2",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="success",
            exit_code=0,
            started_at=NOW + timedelta(minutes=5),
            finished_at=NOW + timedelta(minutes=10),
        )
    )

    store.mark_epic_retry_item(EPIC, "1010")

    [retry] = store.list_epic_retries(EPIC)
    assert retry.superseded_run_ids == {"dispatch-1"}
    assert retry.retried_at > NOW
    result, _ = _tick(store, tmp_path, now=NOW + timedelta(minutes=15))
    assert "1010" not in result.failed


def test_a_future_dated_reaped_run_does_not_hide_its_replacements_publication(
    store: Any, tmp_path: Path
) -> None:
    """#2969: the bound run is dropped before every precedence check. A reaped
    run whose clock reads ahead of its replacement must not stand for the
    node's newest run, or the replacement's preserved work reads as TODO and
    the node is dispatched a third time."""
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-1", boot=_earlier_boot(), started_at=NOW + timedelta(hours=1)
        ),
    )
    _, sink = _tick(store, tmp_path)
    assert sink.calls == ["1010"]
    store.save(
        RunRecord(
            run_id="dispatch-2",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="awaiting_publication",
            exit_code=0,
            started_at=NOW + timedelta(minutes=5),
            finished_at=NOW + timedelta(minutes=8),
        )
    )
    store.release_epic_node_item("1010")

    result, sink = _tick(store, tmp_path, now=NOW + timedelta(minutes=10))

    assert sink.calls == []
    assert "1010" not in result.failed
    summary = summarize(
        EPIC,
        build_graph(FakeSource(nodes=[_node("1010")], edges=[]), EPIC),
        config=_config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=NOW + timedelta(minutes=11),
    )
    assert summary.counts.get("awaiting_publication") == 1


def test_a_review_run_is_not_carried_into_the_bound_marker(
    store: Any, tmp_path: Path
) -> None:
    """#2969: a bound marker carries only the implementation runs an operator
    retry cleared, by name. A review run between the cleared failure and the
    retry is not named, and the marker keeps no cutoff, so a replacement whose
    clock was set back past that review still fails the node when it dies with
    its host. The operator retry's own time stays the review cycle's start."""
    operator_now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="dispatch-0",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=operator_now - timedelta(hours=3),
            finished_at=operator_now - timedelta(hours=3) + timedelta(minutes=5),
        )
    )
    store.save(
        RunRecord(
            run_id="review-0",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="success",
            exit_code=0,
            started_at=operator_now - timedelta(hours=1),
            finished_at=operator_now - timedelta(minutes=50),
            run_kind="adversarial_review",
        )
    )
    store.mark_epic_retry_item(EPIC, "1010")
    [operator_retry] = store.list_epic_retries(EPIC)
    retried_at = operator_retry.retried_at
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-1",
            boot=_earlier_boot(),
            started_at=retried_at + timedelta(minutes=1),
        ),
    )
    _, sink = _tick(store, tmp_path, now=retried_at + timedelta(minutes=2))
    assert sink.calls == ["1010"]
    [bound] = store.list_epic_retries(EPIC)
    assert bound.retried_at == retried_at
    cycle_start = store.review_cycle_start("1010")
    assert cycle_start is not None
    assert datetime.fromisoformat(cycle_start) == retried_at
    assert bound.superseded_run_ids == {"dispatch-0", "dispatch-1"}
    assert bound.supersedes_before == NO_EARLIER_RETRY
    [reservation] = store.list_epic_reservations(EPIC)
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-2",
            boot=_earlier_boot(),
            started_at=retried_at - timedelta(hours=2),
        ),
    )

    result, sink = _tick(store, tmp_path, now=retried_at - timedelta(minutes=110))

    assert store.get("dispatch-2").error_category == "host_restart_repeated"
    assert "1010" in result.failed
    assert sink.calls == []


@pytest.mark.parametrize("death", ["host-restart", "failure"])
def test_a_replacement_older_than_a_cleared_failure_still_counts(
    store: Any, tmp_path: Path, death: str
) -> None:
    """#2969: an operator-cleared failure stays cleared by name, not by a
    cutoff. A replacement whose clock was set back to before that failure
    must not sort behind it and vanish: its death fails the node, and no
    third attempt is dispatched."""
    operator_now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="dispatch-0",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="failure",
            exit_code=1,
            started_at=operator_now - timedelta(hours=1),
            finished_at=operator_now - timedelta(minutes=50),
        )
    )
    store.mark_epic_retry_item(EPIC, "1010")
    [operator_retry] = store.list_epic_retries(EPIC)
    retried_at = operator_retry.retried_at
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-1",
            boot=_earlier_boot(),
            started_at=retried_at + timedelta(minutes=1),
        ),
    )
    _, sink = _tick(store, tmp_path, now=retried_at + timedelta(minutes=2))
    assert sink.calls == ["1010"]
    [reservation] = store.list_epic_reservations(EPIC)
    replacement_start = retried_at - timedelta(hours=1, minutes=55)
    if death == "host-restart":
        _admit_bound_pending(
            store,
            reservation,
            _pending_run(
                "dispatch-2", boot=_earlier_boot(), started_at=replacement_start
            ),
        )
    else:
        store.release_epic_node_item("1010")
        store.save(
            RunRecord(
                run_id="dispatch-2",
                issue_num=1010,
                item_id="1010",
                branch="agent/issue-1010",
                status="failure",
                exit_code=1,
                started_at=replacement_start,
                finished_at=replacement_start + timedelta(minutes=5),
            )
        )

    result, sink = _tick(store, tmp_path, now=replacement_start + timedelta(minutes=10))

    assert "1010" in result.failed
    assert sink.calls == []
    assert len(store.list_audit_events(event="epic_node_retry")) == 1


def test_a_closed_replacement_pr_stays_stuck_behind_a_future_dated_reap(
    store: Any, tmp_path: Path
) -> None:
    """#2969: the host-restart marker is not the operator's restart. A reaped
    run whose clock reads ahead must not stand in for the replacement whose
    PR then closed unmerged: the node stays closed-unmerged until a later
    `kew epic retry`."""
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-1", boot=_earlier_boot(), started_at=NOW + timedelta(hours=1)
        ),
    )
    _, sink = _tick(store, tmp_path)
    assert sink.calls == ["1010"]
    store.save(
        RunRecord(
            run_id="dispatch-2",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="success",
            exit_code=0,
            started_at=NOW + timedelta(minutes=5),
            finished_at=NOW + timedelta(minutes=8),
            pr_url="https://github.com/o/r/pull/77",
        )
    )
    store.release_epic_node_item("1010")
    closed = FakeFactsReader(
        issue_facts_by_num={
            1010: _facts(1010, state="OPEN", pr_number=77, pr_state="CLOSED")
        }
    )
    graph = build_graph(FakeSource(nodes=[_node("1010")], edges=[]), EPIC)
    reason = "closed-unmerged: PR #77 closed without merging"

    def stuck_reasons(now: datetime) -> list[str]:
        summary = summarize(
            EPIC, graph, config=_config(), store=store, facts=closed, now=now
        )
        return [s.reason for s in summary.stuck if s.node_id == "1010"]

    sink = FakeSink()
    _advance(
        deps=FakeSource(nodes=[_node("1010")], edges=[]),
        facts=closed,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        now=NOW + timedelta(minutes=10),
    )
    assert sink.calls == []
    assert stuck_reasons(NOW + timedelta(minutes=11)) == [reason]

    store.mark_epic_retry_item(EPIC, "1010")

    assert stuck_reasons(NOW + timedelta(minutes=12)) == []


def test_a_live_run_of_the_current_boot_is_not_reaped(
    store: Any, tmp_path: Path
) -> None:
    """AC4: same live owner, recorded under this boot — it keeps its row,
    its reservation and its slot."""
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store, reservation, _pending_run("dispatch-1", boot=_current_boot())
    )

    result, sink = _tick(store, tmp_path)

    assert store.get("dispatch-1").status == "pending"
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == ["reservation-1"]
    assert result.in_flight == ("1010",)
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert store.list_epic_retries(EPIC) == []
    assert sink.calls == []


def test_a_dirty_earlier_boot_run_awaits_explicit_continuation(
    store: Any, tmp_path: Path
) -> None:
    """Admission is honored (#2964): with ``max_parallel = 0`` the automatic
    continuation claims nothing, and the preserved work stays waiting."""
    config, worktree = _pending_run_with_checkout(
        store, tmp_path, "dispatch-1", dirty=True
    )

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)
    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    run = store.get("dispatch-1")
    assert run.status == "killed"
    assert run.error_category == "orphaned_pending_preserved"
    [continuation] = store.list_continuations(EPIC)
    assert continuation.state == "awaiting_continuation"
    assert continuation.continuation_run_id is None
    assert store.list_epic_reservations(EPIC) == []
    assert result.in_flight == ()
    assert result.waiting[0].reason.startswith("awaiting_continuation")
    assert sink.calls == []
    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="epic_node_retry") == []
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == ("preserve me\n")
    assert sink.continued == []
    assert store.list_audit_events(event="continuation_claimed") == []


class _DriverKilled(BaseException):
    """The driver dying between two writes: nothing after it runs."""


def test_a_driver_killed_before_the_continuation_is_recorded_offers_it_next_tick(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is terminalized before its continuation is recorded. A kill
    between the two writes leaves the reservation held, and the next tick
    records the missing continuation before it releases that reservation, so
    the node awaits its continuation instead of failing with its work on
    disk (#2971). ``max_parallel = 0`` keeps the automatic continuation
    (#2964) from claiming it, so the recorded state is what this pins."""
    config, worktree = _pending_run_with_checkout(
        store, tmp_path, "dispatch-1", dirty=True
    )
    record_continuation = store.record_continuation

    def killed(*args: Any, **kwargs: Any) -> Any:
        raise _DriverKilled

    monkeypatch.setattr(store, "record_continuation", killed)
    with pytest.raises(_DriverKilled):
        _tick(store, tmp_path, config=config)
    monkeypatch.setattr(store, "record_continuation", record_continuation)
    assert store.get("dispatch-1").error_category == "orphaned_pending_preserved"
    assert store.list_continuations(EPIC) == []
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == ["reservation-1"]

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    [continuation] = store.list_continuations(EPIC)
    assert continuation.state == "awaiting_continuation"
    assert continuation.original_run_id == "dispatch-1"
    assert Path(continuation.worktree_identity) == worktree.resolve()
    [detected] = store.list_audit_events(event="continuation_detected")
    assert detected.payload["outcome"] == "crash_with_dirty_work"
    assert store.list_epic_reservations(EPIC) == []
    assert result.waiting[0].reason.startswith("awaiting_continuation")
    assert "1010" not in result.failed
    assert sink.calls == []
    assert store.list_epic_retries(EPIC) == []
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def _clone(tmp_path: Path, *, dirty: bool) -> Path:
    """A contained dispatch's standalone clone under the system temp directory."""
    from kew.worktree import write_checkout_generation

    clone = tmp_path / "system-tmp" / "kew-clone-1010"
    clone.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch", "agent/issue-1010", str(clone)],
        check=True,
        capture_output=True,
    )
    write_checkout_generation(clone)
    if dirty:
        clone.joinpath("wip.txt").write_text("preserve me\n", encoding="utf-8")
    return clone


def _bind_clone(store: Any, config: Any, clone: Path) -> None:
    """Record *clone* as ``dispatch-1``'s launch checkout, as its launch does."""
    from kew.dispatch_core import _bind_publication_base

    _bind_publication_base(
        store,
        publish=config.publication.publish,
        base="a" * 40,
        run_id="dispatch-1",
        issue_num=1010,
        item_id="1010",
        branch="agent/issue-1010",
        checkout=clone,
    )


def _clone_config(tmp_path: Path) -> Any:
    config = _config()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    return config


def _contained_run_with_clone(
    store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dirty: bool,
    epic_ref: str = EPIC,
) -> tuple[Any, Path, Path]:
    """Bind one earlier-boot contained child to its standalone clone.

    A contained dispatch launches from a clone under the system temp
    directory, not from the host dispatch path, and its container is gone
    after the reboot.
    """
    _containers_gone(monkeypatch)
    config = _clone_config(tmp_path)
    clone = _clone(tmp_path, dirty=dirty)
    assert store.reserve_epic_node_item(epic_ref, "1010", "reservation-1")
    [reservation] = store.list_epic_reservations(epic_ref)
    _admit_bound_pending(
        store,
        reservation,
        _contained(_pending_run("dispatch-1", boot=_earlier_boot())),
    )
    _bind_clone(store, config, clone)
    return config, clone, tmp_path / "worktrees" / "agent" / "issue-1010"


def _held_attention(store: Any, config: Any) -> tuple[str, list[Any]]:
    """The node's summarized status and the attention a person is shown."""
    deps = FakeSource(nodes=[_node("1010")], edges=[])
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=config,
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=NOW,
    )
    events = [e for e in attention_events(EPIC, summary) if e.item == "1010"]
    return summary.statuses["1010"], events


def test_a_dirty_contained_clone_awaits_explicit_continuation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work in a contained run's own clone is brought to the host
    dispatch path and preserved for ``kew epic continue``, not discarded by
    a fresh dispatch (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    run = store.get("dispatch-1")
    assert run.status == "killed"
    assert run.error_category == "orphaned_pending_preserved"
    [continuation] = store.list_continuations(EPIC)
    assert continuation.state == "awaiting_continuation"
    assert Path(continuation.worktree_identity) == host.resolve()
    assert host.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert store.list_epic_reservations(EPIC) == []
    assert result.waiting[0].reason.startswith("awaiting_continuation")
    assert sink.calls == []
    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def test_a_dirty_contained_clone_brought_home_is_continued_once_automatically(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy at the host dispatch path is ordinary preserved work, so the
    host death's one automatic continuation (#2964) resumes it there, while
    the clone is left as it was (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )

    continued = []
    for _ in range(2):
        _, sink = _tick(store, tmp_path, config=config)
        continued += sink.continued

    record = _continuation(store)
    assert record.state == "continuing"
    assert Path(record.worktree_identity) == host.resolve()
    assert continued == [("1010", record.continuation_run_id)]
    [death] = store.list_audit_events(event="epic_node_host_restart")
    assert death.payload["outcome"] == "continue"
    assert len(_auto_claims(store)) == 1
    assert host.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_a_clean_contained_clone_is_re_admitted_once(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=False
    )
    config.publication.publish = "checkout"

    result, sink = _tick(store, tmp_path, config=config)

    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == HOST_RESTART_REASON
    assert store.list_continuations(EPIC) == []
    assert not host.exists()
    assert sink.calls == ["1010"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        + ["-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _operator_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: Path
) -> None:
    """Publish one base commit to a local origin, and run the drive from an
    operator repository cloned from it, as ``kew epic drive`` runs from the
    operator's own checkout. The contained clone was cut from that base."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch", "main", str(seed)],
        check=True,
        capture_output=True,
    )
    _git(seed, "commit", "--allow-empty", "-m", "base")
    _git(seed, "push", str(origin), "main")
    operator = tmp_path / "operator"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(operator)],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(operator)
    _git(clone, "fetch", "--quiet", str(origin), "main:refs/remotes/origin/main")
    _git(clone, "reset", "--quiet", "refs/remotes/origin/main")


def _commit_past_fetched_base(clone: Path) -> None:
    """Commit ``wip.txt`` on top of the clone's base, leaving its tree clean,
    as an agent that committed and never pushed."""
    clone.joinpath("wip.txt").write_text("preserve me\n", encoding="utf-8")
    _git(clone, "add", "wip.txt")
    _git(clone, "commit", "-m", "wip")


def test_a_committed_but_unpublished_clone_awaits_explicit_continuation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under checkout publication a committed change leaves the clone's tree
    clean, yet no remote holds it: it is preserved for ``kew epic continue``,
    not discarded by a fresh dispatch (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=False
    )
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    _operator_repository(tmp_path, monkeypatch, clone)
    _commit_past_fetched_base(clone)

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    _assert_committed_clone_preserved(store, host, result, sink)


def test_a_committed_clone_with_a_rewritten_base_ref_awaits_continuation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent moves its own clone's ``origin/main`` onto its unpublished
    commit, so nothing is ahead of THAT ref; no remote holds the commit, so
    it is preserved all the same (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=False
    )
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    _operator_repository(tmp_path, monkeypatch, clone)
    _commit_past_fetched_base(clone)
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    _assert_committed_clone_preserved(store, host, result, sink)


def test_a_clone_left_on_its_published_base_is_re_admitted_once(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone whose HEAD is still the base it was cut from holds no commit
    a remote lacks: nothing is lost, and the node is re-admitted (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=False
    )
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    _operator_repository(tmp_path, monkeypatch, clone)

    _, sink = _tick(store, tmp_path, config=config)

    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    assert store.list_continuations(EPIC) == []
    assert not host.exists()
    assert sink.calls == ["1010"]


def _publish_item_branch(tmp_path: Path, clone: Path) -> None:
    """Push one commit ahead of main to origin's ``agent/issue-1010`` and
    start the clone clean at it, as a retry of a published node starts."""
    origin = tmp_path / "origin.git"
    clone.joinpath("published.txt").write_text("on origin\n", encoding="utf-8")
    _git(clone, "add", "published.txt")
    _git(clone, "commit", "-m", "published")
    _git(clone, "push", "--quiet", str(origin), "HEAD:refs/heads/agent/issue-1010")


def test_a_clean_clone_at_its_published_item_branch_is_re_admitted_once(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone's HEAD is ahead of main but is exactly what origin's item
    branch holds: nothing is lost, and the node is re-admitted once rather
    than parked for a continuation (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=False
    )
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    _operator_repository(tmp_path, monkeypatch, clone)
    _publish_item_branch(tmp_path, clone)

    _, sink = _tick(store, tmp_path, config=config)

    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == HOST_RESTART_REASON
    assert store.list_continuations(EPIC) == []
    assert not host.exists()
    assert sink.calls == ["1010"]


def test_a_commit_past_the_published_item_branch_awaits_continuation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit on top of the published item branch is on no remote: it is
    preserved for ``kew epic continue`` (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=False
    )
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    _operator_repository(tmp_path, monkeypatch, clone)
    _publish_item_branch(tmp_path, clone)
    _commit_past_fetched_base(clone)

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    _assert_committed_clone_preserved(store, host, result, sink)


def _assert_committed_clone_preserved(
    store: Any, host: Path, result: Any, sink: FakeSink
) -> None:
    """The committed clone is preserved, awaiting its continuation.

    ``max_parallel = 0`` keeps the automatic continuation (#2964) from
    claiming the preserved work, so the preserved state is what this pins."""
    assert store.get("dispatch-1").error_category == "orphaned_pending_preserved"
    [continuation] = store.list_continuations(EPIC)
    assert continuation.state == "awaiting_continuation"
    assert Path(continuation.worktree_identity) == host.resolve()
    assert _git(host, "show", "HEAD:wip.txt") == "preserve me"
    assert store.list_epic_reservations(EPIC) == []
    assert result.waiting[0].reason.startswith("awaiting_continuation")
    assert sink.calls == []
    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="epic_node_retry") == []


def test_dirty_contained_work_after_a_host_restart_retry_awaits_continuation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first host restart reaps ``dispatch-0`` and re-admits the node; the
    retry, ``dispatch-1``, dies under a second restart with uncommitted work
    in its clone. The first attempt's terminal row on the same branch does
    not stand in the way: the work is preserved for ``kew epic continue``
    under checkout publication (#2971). It is the node's second host death,
    so nothing continues it automatically and a person is asked (#2964)."""
    _containers_gone(monkeypatch)
    config = _clone_config(tmp_path)
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    reservation = _reserve(store, "reservation-0")
    _admit_bound_pending(
        store, reservation, _pending_run("dispatch-0", boot=_earlier_boot())
    )
    _tick(store, tmp_path, config=config)
    assert store.get("dispatch-0").error_category == "host_restart_reaped"
    [reservation] = store.list_epic_reservations(EPIC)
    later = datetime.now(UTC) + timedelta(seconds=1)
    _admit_bound_pending(
        store,
        reservation,
        _contained(_pending_run("dispatch-1", boot=_earlier_boot(), started_at=later)),
    )
    clone = _clone(tmp_path, dirty=True)
    _bind_clone(store, config, clone)
    host = tmp_path / "worktrees" / "agent" / "issue-1010"

    result, sink = _tick(
        store, tmp_path, now=later + timedelta(seconds=1), config=config
    )

    assert store.list_epic_holds(EPIC) == []
    assert store.get("dispatch-1").error_category == "orphaned_pending_preserved"
    [continuation] = store.list_continuations(EPIC)
    assert continuation.original_run_id == "dispatch-1"
    assert continuation.state == "awaiting_continuation"
    assert Path(continuation.worktree_identity) == host.resolve()
    assert host.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert store.list_epic_reservations(EPIC) == []
    [stuck] = [s for s in result.stuck if s.node_id == "1010"]
    assert stuck.reason.startswith(HOST_RESTART_REPEATED_CONTINUATION_REASON)
    assert sink.calls == sink.continued == []


def test_a_contained_clone_whose_state_cannot_be_proved_holds_the_node(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree already standing at the host path leaves nowhere to preserve
    the clone's work: the node is held, neither retried nor failed."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )
    host.mkdir(parents=True)
    host.joinpath("someone-elses.txt").write_text("keep\n", encoding="utf-8")

    result, sink = _tick(store, tmp_path, config=config)

    [hold] = store.list_epic_holds(EPIC)
    assert hold.item_id == "1010"
    assert hold.reason.startswith("preexisting_worktree_refused: ")
    assert f"a tree already stands at host checkout {host}" in hold.reason
    status, events = _held_attention(store, config)
    assert status == "held"
    assert [e.kind for e in events] == [AttentionKind.HELD]
    assert store.get("dispatch-1").status == "pending"
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == ["reservation-1"]
    assert store.list_epic_retries(EPIC) == []
    assert store.list_continuations(EPIC) == []
    assert "1010" not in result.failed
    assert sink.calls == []
    assert sorted(p.name for p in host.iterdir()) == ["someone-elses.txt"]
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_a_dispatch_started_after_a_stale_refusal_hold_reads_in_progress(
    store: Any, tmp_path: Path
) -> None:
    """A driver killed after a dispatch returned but before it cleared the
    refusal that dispatch disproved leaves the stale hold beside the live
    child. The child started after the hold, so the node reads in progress
    and nothing asks a person (#2971)."""
    store.hold_epic_node_item(EPIC, "1010", "preexisting_worktree_refused: stale")
    [hold] = store.list_epic_holds(EPIC)
    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        _pending_run(
            "dispatch-1",
            boot=_current_boot(),
            started_at=hold.held_at + timedelta(seconds=1),
        ),
    )

    status, events = _held_attention(store, _config())

    assert status == "in_progress"
    assert AttentionKind.HELD not in [e.kind for e in events]
    summary = summarize(
        EPIC,
        build_graph(FakeSource(nodes=[_node("1010")], edges=[]), EPIC),
        config=_config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=NOW,
    )
    assert summary.in_flight == ("1010",)
    assert summary.held == ()


def test_a_refusal_hold_clears_once_a_later_pass_preserves_the_clone(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the operator clears what refused the clone's preservation, the
    next pass preserves it and settles the reservation; the refusal hold that
    pass disproved goes with it, so the node awaits continuation instead of
    reading HELD over live work (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )
    host.mkdir(parents=True)
    host.joinpath("someone-elses.txt").write_text("keep\n", encoding="utf-8")
    _tick(store, tmp_path, config=config)
    [hold] = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("preexisting_worktree_refused: ")
    shutil.rmtree(host)

    result, sink = _tick(store, tmp_path, config=config, max_parallel=0)

    assert store.list_epic_holds(EPIC) == []
    [continuation] = store.list_continuations(EPIC)
    assert continuation.state == "awaiting_continuation"
    assert store.list_epic_reservations(EPIC) == []
    assert result.waiting[0].reason.startswith("awaiting_continuation")
    assert sink.calls == []
    assert host.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    claimed = store.claim_continuation_under_cap(
        continuation.epic_ref,
        continuation.item_id,
        continuation.original_run_id,
        continuation.branch,
        continuation.worktree_identity,
        "continuation-1",
        5,
        claim_evidence=fixture_claim_evidence(
            store,
            continuation.epic_ref,
            continuation.item_id,
            continuation.original_run_id,
            continuation.branch,
            continuation.worktree_identity,
            "continuation-1",
        ),
    )
    assert claimed == "claimed"
    status, events = _held_attention(store, config)
    assert status == "in_progress"
    assert AttentionKind.HELD not in [e.kind for e in events]


def test_a_refusal_hold_stands_while_its_reservation_is_unsettled(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that cannot tell whether the run is dead leaves the reservation
    in place and proved nothing about the refused checkout, so it keeps the
    hold (#2971)."""
    config, _, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )
    host.mkdir(parents=True)
    host.joinpath("someone-elses.txt").write_text("keep\n", encoding="utf-8")
    _tick(store, tmp_path, config=config)
    shutil.rmtree(host)
    patched_run = subprocess.run

    def daemon_down(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if list(argv[:2]) == [_DOCKER, "inspect"]:
            return subprocess.CompletedProcess(
                argv, 1, "", "Cannot connect to the Docker daemon\n"
            )
        return patched_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", daemon_down)

    _tick(store, tmp_path, config=config)

    [hold] = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("preexisting_worktree_refused: ")
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == ["reservation-1"]


def test_a_recorded_contained_clone_gone_after_the_reboot_holds_the_node(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone binding proves a checkout existed; once its path is gone,
    nothing says whether it held work. The node is held with the operator's
    guidance, neither retried nor dispatched fresh (#2971)."""
    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )
    shutil.rmtree(clone)

    result, sink = _tick(store, tmp_path, config=config)

    [hold] = store.list_epic_holds(EPIC)
    assert hold.item_id == "1010"
    assert hold.reason.startswith("preexisting_worktree_refused: ")
    assert f"recorded clone {clone} is gone" in hold.reason
    assert hold.reason.endswith(_CLONE_RECOVERY_GUIDANCE)
    status, events = _held_attention(store, config)
    assert status == "held"
    [event] = events
    assert event.kind is AttentionKind.HELD
    assert event.reason.endswith(_CLONE_RECOVERY_GUIDANCE)
    assert store.get("dispatch-1").status == "pending"
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == ["reservation-1"]
    assert store.list_epic_retries(EPIC) == []
    assert store.list_audit_events(event="epic_node_retry") == []
    assert store.list_continuations(EPIC) == []
    assert "1010" not in result.failed
    assert sink.calls == []
    assert not host.exists()


def test_unholding_a_gone_clone_hold_re_admits_the_node_once(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gone clone never comes back, so ``kew epic unhold`` is the
    person's word that its work is accounted for: the next tick re-admits the
    node instead of writing the same hold again and leaving it wedged with its
    run pending (#2971)."""
    from kew.cli.epic import _record_unhold

    config, clone, _ = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )
    shutil.rmtree(clone)
    _tick(store, tmp_path, config=config)
    hold = store.remove_epic_hold(EPIC, "1010")
    assert hold is not None
    _record_unhold(store, config, epic_ref=EPIC, item_id="1010", hold=hold)

    result, sink = _tick(store, tmp_path, config=config)

    assert store.list_epic_holds(EPIC) == []
    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == HOST_RESTART_REASON
    assert sink.calls == ["1010"]
    assert "1010" not in result.failed


def test_an_unrelated_unhold_does_not_release_a_gone_clone(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the release of the gone-clone hold itself is that word: an
    earlier unhold of another refusal on the node re-holds it."""
    from kew.cli.epic import _record_unhold

    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True
    )
    host.mkdir(parents=True)
    _tick(store, tmp_path, config=config)
    hold = store.remove_epic_hold(EPIC, "1010")
    assert hold is not None
    _record_unhold(store, config, epic_ref=EPIC, item_id="1010", hold=hold)
    host.rmdir()
    shutil.rmtree(clone)

    result, sink = _tick(store, tmp_path, config=config)

    [held] = store.list_epic_holds(EPIC)
    assert held.reason.endswith(_CLONE_RECOVERY_GUIDANCE)
    assert store.get("dispatch-1").status == "pending"
    assert sink.calls == []


def test_a_delegated_contained_run_whose_clone_is_gone_is_reaped(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pseudo-epic gets no hold, so a gone-clone refusal there would keep
    the delegated run pending and its reservation held on every pass. The
    delegation reconciler reaps it and releases the reservation instead."""
    from kew.graph.driver.tick import reconcile_epic_reservations
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    config, clone, _ = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True, epic_ref=DELEGATION_EPIC_REF
    )
    shutil.rmtree(clone)

    for _ in range(3):
        reserved = reconcile_epic_reservations(
            DELEGATION_EPIC_REF, store, config, now=NOW
        )

    assert reserved == set()
    assert store.list_epic_reservations(DELEGATION_EPIC_REF) == []
    assert store.get("dispatch-1").status == "killed"
    assert store.list_epic_holds(DELEGATION_EPIC_REF) == []


@pytest.mark.parametrize("run_kind", ["adversarial_review", "review_stage"])
def test_a_reviewer_contained_run_without_a_clone_binding_frees_its_slot(
    store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_kind: str,
) -> None:
    """A reviewer records its container but never owns a clone. A no-clone
    refusal there would keep its run pending and its fleet slot held forever
    (#1059): the reconciler reaps it and releases the reservation instead."""
    from kew.graph.driver.tick import reconcile_epic_reservations

    epic_ref = "review"
    _containers_gone(monkeypatch)
    config = _clone_config(tmp_path)
    assert store.reserve_epic_node_item(epic_ref, "1010", "reservation-1")
    [reservation] = store.list_epic_reservations(epic_ref)
    pending = _contained(_pending_run("dispatch-1", boot=_earlier_boot()))
    pending.run_kind = run_kind
    _admit_bound_pending(store, reservation, pending)

    for _ in range(3):
        reserved = reconcile_epic_reservations(epic_ref, store, config, now=NOW)

    assert reserved == set()
    assert store.list_epic_reservations(epic_ref) == []
    assert store.get("dispatch-1").status == "killed"
    assert store.list_epic_holds(epic_ref) == []


def _unbound_delegated_run(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Path]:
    """A legacy delegated dispatch: contained, pending from an earlier boot,
    with a dirty clone under the system temp directory and no binding."""
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    _containers_gone(monkeypatch)
    config = _clone_config(tmp_path)
    clone = _clone(tmp_path, dirty=True)
    assert store.reserve_epic_node_item(DELEGATION_EPIC_REF, "1010", "reservation-1")
    [reservation] = store.list_epic_reservations(DELEGATION_EPIC_REF)
    _admit_bound_pending(
        store,
        reservation,
        _contained(_pending_run("dispatch-1", boot=_earlier_boot())),
    )
    return config, clone


def test_an_unbound_delegated_dispatch_keeps_its_reservation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthetic epic proves nothing about a delegated dispatch's clone:
    one launched before clone binding existed may hold work no row names. Its
    run stays pending and its reservation held, never reaped or released for
    a fresh dispatch to duplicate (#2971)."""
    from kew.graph.driver.tick import reconcile_epic_reservations
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    config, clone = _unbound_delegated_run(store, tmp_path, monkeypatch)

    for _ in range(3):
        reserved = reconcile_epic_reservations(
            DELEGATION_EPIC_REF, store, config, now=NOW
        )

    assert reserved == {"1010"}
    assert store.get("dispatch-1").status == "pending"
    assert [r.run_id for r in store.list_epic_reservations(DELEGATION_EPIC_REF)] == [
        "reservation-1"
    ]
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert store.list_continuations(DELEGATION_EPIC_REF) == []
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_unholding_an_unbound_delegated_dispatch_releases_its_reservation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew epic unhold delegation <item>`` is the durable way out: the
    refusal it names is released on the chain, and the next reconcile reaps
    the run and frees the reservation."""
    from kew.cli.epic import _record_unhold
    from kew.graph.driver.tick import (
        pseudo_epic_clone_refusal,
        reconcile_epic_reservations,
    )
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    config, _ = _unbound_delegated_run(store, tmp_path, monkeypatch)
    reconcile_epic_reservations(DELEGATION_EPIC_REF, store, config, now=NOW)
    refusal = pseudo_epic_clone_refusal(store, DELEGATION_EPIC_REF, "1010")
    assert refusal is not None
    assert refusal.reason.endswith(_CLONE_RECOVERY_GUIDANCE)
    _record_unhold(
        store, config, epic_ref=DELEGATION_EPIC_REF, item_id="1010", hold=refusal
    )

    reserved = reconcile_epic_reservations(DELEGATION_EPIC_REF, store, config, now=NOW)

    assert reserved == set()
    assert store.list_epic_reservations(DELEGATION_EPIC_REF) == []
    assert store.get("dispatch-1").status == "killed"
    assert pseudo_epic_clone_refusal(store, DELEGATION_EPIC_REF, "1010") is None


def test_a_current_boot_delegated_run_names_no_refusal(store: Any) -> None:
    """Only a run this machine rebooted under can stand on the refusal, so an
    unhold can never pre-release a live delegated run."""
    from kew.graph.driver.tick import pseudo_epic_clone_refusal
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    assert store.reserve_epic_node_item(DELEGATION_EPIC_REF, "1010", "reservation-1")
    [reservation] = store.list_epic_reservations(DELEGATION_EPIC_REF)
    _admit_bound_pending(
        store,
        reservation,
        _contained(_pending_run("dispatch-1", boot=_current_boot())),
    )

    assert pseudo_epic_clone_refusal(store, DELEGATION_EPIC_REF, "1010") is None


def test_a_legacy_contained_run_without_a_checkout_binding_is_held(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contained launch records its clone before its pending row exists, so
    a contained row with no clone recorded predates that binding: its clone
    sits somewhere under the system temp directory, unfound and unproved
    clean. The node is held with the operator's guidance, neither reaped nor
    dispatched fresh over that clone (#2971)."""
    _containers_gone(monkeypatch)
    config = _clone_config(tmp_path)
    clone = _clone(tmp_path, dirty=True)
    _admit_bound_pending(
        store,
        _reserve(store, "reservation-1"),
        _contained(_pending_run("dispatch-1", boot=_earlier_boot())),
    )

    result, sink = _tick(store, tmp_path, config=config)

    [hold] = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("preexisting_worktree_refused: ")
    assert "run dispatch-1 recorded no clone" in hold.reason
    assert hold.reason.endswith(_CLONE_RECOVERY_GUIDANCE)
    status, events = _held_attention(store, config)
    assert status == "held"
    [event] = events
    assert event.kind is AttentionKind.HELD
    assert store.get("dispatch-1").status == "pending"
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == ["reservation-1"]
    assert store.list_audit_events(event="orphaned_run_reaped") == []
    assert store.list_audit_events(event="epic_node_retry") == []
    assert store.list_continuations(EPIC) == []
    assert "1010" not in result.failed
    assert sink.calls == []
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_unholding_a_legacy_contained_run_re_admits_the_node_once(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew epic unhold`` is the person's word that the unrecorded clone's
    work is accounted for: the next tick re-admits the node once."""
    from kew.cli.epic import _record_unhold

    _containers_gone(monkeypatch)
    config = _clone_config(tmp_path)
    _admit_bound_pending(
        store,
        _reserve(store, "reservation-1"),
        _contained(_pending_run("dispatch-1", boot=_earlier_boot())),
    )
    _tick(store, tmp_path, config=config)
    hold = store.remove_epic_hold(EPIC, "1010")
    assert hold is not None
    _record_unhold(store, config, epic_ref=EPIC, item_id="1010", hold=hold)

    result, sink = _tick(store, tmp_path, config=config)

    assert store.list_epic_holds(EPIC) == []
    assert store.get("dispatch-1").error_category == "host_restart_reaped"
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert retry.payload["reason"] == HOST_RESTART_REASON
    assert sink.calls == ["1010"]
    assert "1010" not in result.failed


def test_a_dirty_delegated_contained_clone_remains_recoverable_before_release(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegated run's dirty clone is brought home and preserved under the
    delegation pseudo-epic before its reservation is released, as a delegated
    run's dirty host checkout is — never reaped with the work left unnamed in
    the system temp directory (#2971)."""
    from kew.graph.driver.tick import reconcile_epic_reservations
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True, epic_ref=DELEGATION_EPIC_REF
    )

    reserved = reconcile_epic_reservations(DELEGATION_EPIC_REF, store, config, now=NOW)

    assert reserved == set()
    run = store.get("dispatch-1")
    assert run.status == "killed"
    assert run.error_category == "orphaned_pending_preserved"
    [continuation] = store.list_continuations(DELEGATION_EPIC_REF)
    assert continuation.state == "awaiting_continuation"
    assert continuation.original_run_id == "dispatch-1"
    assert Path(continuation.worktree_identity) == host.resolve()
    assert host.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert store.list_epic_reservations(DELEGATION_EPIC_REF) == []
    assert store.list_audit_events(event="orphaned_run_reaped") == []


def test_a_dirty_delegated_clone_that_cannot_be_preserved_keeps_its_reservation(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nowhere to preserve the work, the delegated run stays pending and
    its reservation stays held: nothing is released over the dirty clone."""
    from kew.graph.driver.tick import reconcile_epic_reservations
    from kew.pseudo_epics import DELEGATION_EPIC_REF

    config, clone, host = _contained_run_with_clone(
        store, tmp_path, monkeypatch, dirty=True, epic_ref=DELEGATION_EPIC_REF
    )
    host.mkdir(parents=True)
    host.joinpath("someone-elses.txt").write_text("keep\n", encoding="utf-8")

    for _ in range(2):
        reserved = reconcile_epic_reservations(
            DELEGATION_EPIC_REF, store, config, now=NOW
        )

    assert reserved == {"1010"}
    assert store.get("dispatch-1").status == "pending"
    assert [r.run_id for r in store.list_epic_reservations(DELEGATION_EPIC_REF)] == [
        "reservation-1"
    ]
    assert store.list_continuations(DELEGATION_EPIC_REF) == []
    assert sorted(p.name for p in host.iterdir()) == ["someone-elses.txt"]
    assert clone.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_another_machines_boot_proves_nothing(store: Any, tmp_path: Path) -> None:
    """A boot id from a second machine writing the same store is not an
    earlier boot of this one: its live run is left alone."""
    machine, _, _ = _current_boot().rpartition("/")
    reservation = _reserve(store, "reservation-1")
    other = _earlier_boot().replace(f"{machine}/", f"not-{machine}/", 1)
    _admit_bound_pending(store, reservation, _pending_run("dispatch-1", boot=other))

    _tick(store, tmp_path)

    assert store.get("dispatch-1").status == "pending"


def test_same_hostname_different_machine_ids_do_not_prove_an_earlier_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_id = tmp_path / "first-machine-id"
    second_id = tmp_path / "second-machine-id"
    first_id.write_text("1" * 32, encoding="ascii")
    second_id.write_text("2" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    monkeypatch.setattr(socket, "gethostname", lambda: "operator")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (first_id,))
    process_groups._this_machine.cache_clear()
    first = machine_boot()
    assert first is not None
    first_machine, _, first_boot = first.rpartition("/")
    earlier = "0" * 32 if first_boot != "0" * 32 else "f" * 32
    recorded = f"{first_machine}/{earlier}"

    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (second_id,))
    process_groups._this_machine.cache_clear()
    try:
        assert not from_earlier_boot(recorded)
    finally:
        process_groups._this_machine.cache_clear()


def test_hostname_change_still_recognizes_this_machines_earlier_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("3" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (machine_id,))
    monkeypatch.setattr(socket, "gethostname", lambda: "old-name")
    process_groups._this_machine.cache_clear()
    recorded = _earlier_boot()

    monkeypatch.setattr(socket, "gethostname", lambda: "new-name")
    try:
        assert from_earlier_boot(recorded)
    finally:
        process_groups._this_machine.cache_clear()


def test_the_dbus_machine_id_stands_in_when_etc_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dbus_id = tmp_path / "dbus-machine-id"
    dbus_id.write_text("4" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    monkeypatch.setattr(
        process_groups, "LINUX_MACHINE_IDS", (tmp_path / "absent", dbus_id)
    )
    process_groups._this_machine.cache_clear()

    assert (machine_boot() or "").startswith(f"machine-id:{'4' * 32}/")


def test_an_invalid_primary_machine_id_uses_the_valid_dbus_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary_id = tmp_path / "machine-id"
    dbus_id = tmp_path / "dbus-machine-id"
    primary_id.write_text("not-a-machine-id\n", encoding="ascii")
    dbus_id.write_text("5" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (primary_id, dbus_id))
    process_groups._this_machine.cache_clear()

    assert (machine_boot() or "").startswith(f"machine-id:{'5' * 32}/")


class _DarwinLibc:
    """libc as macOS exposes it: ``gethostuuid`` refuses a NULL wait (EFAULT)."""

    def __init__(self) -> None:
        self.gethostuuid = self._gethostuuid

    @staticmethod
    def _gethostuuid(buffer: Any, wait: Any) -> int:
        if wait is None:
            return -1
        ctypes.memmove(buffer, bytes(range(16)), 16)
        return 0


def test_macos_machine_identity_passes_a_real_wait_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_k: _DarwinLibc())
    process_groups._this_machine.cache_clear()

    assert process_groups.read_raw_machine_id() == bytes(range(16)).hex()


def test_an_all_zero_machine_id_is_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unset images both carry 32 zeros; the sentinel must never pair
    one host's boots with another's."""
    zeros = tmp_path / "machine-id"
    zeros.write_text("0" * 32 + "\n", encoding="ascii")
    dbus_id = tmp_path / "dbus-machine-id"
    dbus_id.write_text("7" * 32, encoding="ascii")
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (zeros, dbus_id))
    process_groups._this_machine.cache_clear()
    assert (machine_boot() or "").startswith(f"machine-id:{'7' * 32}/")

    monkeypatch.setattr(process_groups, "LINUX_MACHINE_IDS", (zeros,))
    process_groups._this_machine.cache_clear()
    assert machine_boot() is None
    assert not from_earlier_boot(f"machine-id:{'0' * 32}/{'1' * 32}")


def test_a_host_without_a_valid_machine_id_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kew mints no stand-in id: two hosts could share it and reap each
    other's live runs, so a host with no durable id of its own proves nothing."""
    recorded = _earlier_boot()
    invalid = tmp_path / "machine-id"
    invalid.write_text("\n", encoding="ascii")
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    monkeypatch.setattr(
        process_groups, "LINUX_MACHINE_IDS", (invalid, tmp_path / "absent-dbus-id")
    )
    process_groups._this_machine.cache_clear()

    assert machine_boot() is None
    assert not from_earlier_boot(recorded)


def test_a_legacy_hostname_boot_proves_nothing() -> None:
    _, _, boot = _earlier_boot().rpartition("/")

    assert not from_earlier_boot(f"operator/{boot}")
    assert not from_earlier_boot(f"{'a' * 32}/{boot}")


def test_an_earlier_boot_openshell_run_waits_for_its_own_probe() -> None:
    """An OpenShell agent lives in the gateway and outlives a local reboot, so
    the earlier boot proves nothing; an unanswered probe keeps it pending."""
    openshell = _pending_run("run-1", boot=_earlier_boot())
    openshell.container_name = "kew-1010"
    openshell.container_runtime = OPENSHELL_RUNTIME
    host_run = _pending_run("run-2", boot=_earlier_boot())

    def unanswered(_run: RunRecord) -> bool | None:
        return None

    assert not _pending_run_is_dead(openshell, unanswered, now=NOW)
    assert _pending_run_is_dead(host_run, unanswered, now=NOW)


def test_contained_identity_failure_keeps_boot_evidence_for_reaping(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = os.getpgrp()

    def failed_ps(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 1, "", "ps failed")

    with monkeypatch.context() as identity_failure:
        identity_failure.setattr(subprocess, "run", failed_ps)
        identity = identity_at_admission(owner)
    assert identity["owner_boot_id"] == _current_boot()
    assert "owner_start_time" not in identity

    reservation = _reserve(store, "reservation-1")
    _admit_bound_pending(
        store,
        reservation,
        RunRecord(
            run_id="dispatch-1",
            issue_num=1010,
            item_id="1010",
            branch="agent/issue-1010",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            pgid=owner,
            owner_pid=owner,
            **{**identity, "owner_boot_id": _earlier_boot()},
        ),
    )

    _tick(store, tmp_path)

    assert store.get("dispatch-1").error_category == "host_restart_reaped"


def test_admission_records_the_owners_boot() -> None:
    """The identity every dispatch records names the boot it ran under."""
    identity = identity_at_admission(os.getpgrp(), require_token=True)
    assert identity["owner_boot_id"] == _current_boot()


def test_the_boot_survives_the_store(store: Any) -> None:
    store.save(_pending_run("run-1", boot=_earlier_boot()))
    assert store.get("run-1").owner_boot_id == _earlier_boot()


def test_v69_adds_the_owner_boot_and_keeps_existing_runs(tmp_path: Path) -> None:
    """A pre-v69 ledger keeps its rows; they name no boot, so prove nothing."""
    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    path = str(tmp_path / "run.db")
    with SqliteRunStore(path) as old:
        old.save(_pending_run("run-1", boot=_current_boot()))
        old._conn.execute("ALTER TABLE runs DROP COLUMN owner_boot_id")
        old._conn.execute("UPDATE schema_version SET version = 68")
        old._conn.commit()

    with SqliteRunStore(path) as migrated:
        version = migrated._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION
        run = migrated.get("run-1")
        assert run is not None and run.status == "pending"
        assert run.owner_boot_id is None


def test_v72_binds_retry_markers_and_keeps_existing_ones_time_based(
    tmp_path: Path,
) -> None:
    """A pre-v72 marker names no run, so it keeps its time-based meaning."""
    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    path = str(tmp_path / "run.db")
    with SqliteRunStore(path) as old:
        old.mark_epic_retry_item(EPIC, "1010")
        [before] = old.list_epic_retries(EPIC)
        old._conn.execute("ALTER TABLE epic_retries DROP COLUMN superseded_run_ids")
        old._conn.execute("ALTER TABLE epic_retries DROP COLUMN superseded_before")
        old._conn.execute("ALTER TABLE merge_queue_suspects DROP COLUMN kind")
        old._conn.execute("ALTER TABLE merge_queue_suspects DROP COLUMN failure")
        old._conn.execute("DROP TABLE prompt_profile_versions")
        old._conn.execute("ALTER TABLE runs DROP COLUMN prompt_profile_version_id")
        old._conn.execute(
            "ALTER TABLE review_rounds DROP COLUMN prompt_profile_version_id"
        )
        old._conn.execute("UPDATE schema_version SET version = 71")
        old._conn.commit()

    with SqliteRunStore(path) as migrated:
        version = migrated._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION
        [after] = migrated.list_epic_retries(EPIC)
        assert after.superseded_run_ids == frozenset()
        assert after.superseded_before is None
        assert after.retried_at == before.retried_at
        assert after.supersedes_before == before.retried_at


# --- #2964: dirty host-death work is continued automatically, once ---------


def _dirty_first_death(
    store: Any, tmp_path: Path, *, checkout: bool = False
) -> tuple[Any, Path]:
    """A dirty earlier-boot run the next tick preserves and arms."""
    return _pending_run_with_checkout(
        store, tmp_path, "dispatch-1", dirty=True, checkout=checkout
    )


def _continuation(store: Any) -> Any:
    [record] = store.list_continuations(EPIC)
    return record


def _auto_claims(store: Any) -> list[Any]:
    return [
        e
        for e in store.list_audit_events(event="continuation_claimed")
        if e.actor_id == "driver_auto_continue"
    ]


def _continuation_dies_with_the_host(
    store: Any, config: Any, run_id: str, *, started_at: datetime
) -> None:
    """The auto-continued child's own run row, from a boot that has since ended."""
    run = _pending_run(run_id, boot=_earlier_boot(), started_at=started_at)
    record = _continuation(store)
    run.branch = record.branch
    store.save(run)
    from kew.dispatch_core import _bind_publication_base

    _bind_publication_base(
        store,
        publish=config.publication.publish,
        base=_bound_base(config),
        run_id=run_id,
        issue_num=1010,
        item_id="1010",
        branch=record.branch,
        checkout=Path(record.worktree_identity),
    )


def test_a_dirty_first_host_death_is_continued_once_automatically(
    store: Any, tmp_path: Path
) -> None:
    config, worktree = _dirty_first_death(store, tmp_path)

    _, sink = _tick(store, tmp_path, config=config)
    _, again = _tick(store, tmp_path, config=config)

    record = _continuation(store)
    assert record.state == "continuing"
    assert sink.continued + again.continued == [("1010", record.continuation_run_id)]
    assert sink.calls == again.calls == []
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == [
        record.continuation_run_id
    ]
    [death] = store.list_audit_events(event="epic_node_host_restart")
    assert death.payload["outcome"] == "continue"
    assert len(_auto_claims(store)) == 1
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_capacity_blocked_auto_continuation_remains_progressing_and_launches_after_capacity_frees(  # noqa: E501
    store: Any, tmp_path: Path
) -> None:
    """Another epic holds the only fleet slot: the armed continuation waits
    for admission like fresh work, and the tick says it will move, so
    ``kew epic run`` keeps driving until the slot frees and it is claimed."""
    from kew.graph.driver.result import TerminalSignal

    config, worktree = _dirty_first_death(store, tmp_path)
    assert config.fleet_concurrency() == 1
    store.register_epic("epic/other")
    assert store.reserve_epic_node_item("epic/other", "2020", "other-run") is True

    for _ in range(2):
        result, sink = _tick(store, tmp_path, config=config)
        assert result.signal is TerminalSignal.PROGRESSING
        assert sink.continued == []
        assert _continuation(store).state == "awaiting_continuation"
        assert "worker cap" in _frontier_hold_reason_seen(result)
    assert _auto_claims(store) == []
    summary = summarize(
        EPIC,
        build_graph(FakeSource(nodes=[_node("1010")], edges=[]), EPIC),
        config=config,
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=NOW,
    )
    assert summary.signal is TerminalSignal.PROGRESSING

    store.release_epic_node_item("2020", run_id="other-run")
    result, sink = _tick(store, tmp_path, config=config)

    record = _continuation(store)
    assert record.state == "continuing"
    assert sink.continued == [("1010", record.continuation_run_id)]
    assert result.signal is TerminalSignal.PROGRESSING
    assert len(_auto_claims(store)) == 1
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_an_auto_continuation_waits_for_a_running_local_verify_then_launches(
    store: Any, tmp_path: Path
) -> None:
    """A continuation is a dispatch launch: while a local merge verify holds
    the lease it is deferred like a fresh node, and launches once it ends."""
    from kew.graph.driver.result import TerminalSignal

    config, worktree = _dirty_first_death(store, tmp_path)

    with lease_held() as holder:
        result, sink = _tick(store, tmp_path, config=config)
        assert sink.continued == []
        assert _continuation(store).state == "awaiting_continuation"
        assert result.signal is TerminalSignal.PROGRESSING
        assert "deferred: verify running" in _frontier_hold_reason_seen(result)
        assert [
            e.item_id for e in store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
        ] == ["1010"]
        assert _auto_claims(store) == []
        holder.release()

    _, sink = _tick(store, tmp_path, config=config)

    record = _continuation(store)
    assert record.state == "continuing"
    assert sink.continued == [("1010", record.continuation_run_id)]
    assert len(_auto_claims(store)) == 1
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_a_fair_share_blocked_auto_continuation_launches_once_its_share_frees(
    store: Any, tmp_path: Path
) -> None:
    """At its fair share the epic's armed continuation waits for its turn,
    then is claimed on the tick its own in-flight work releases the share."""
    config, _ = _dirty_first_death(store, tmp_path)
    config.parallel.max_workers = 2
    store.register_epic(EPIC)
    store.register_epic("epic/other")
    assert store.reserve_epic_node_item("epic/other", "3030", "other-run") is True
    deps = FakeSource(nodes=[_node("1010"), _node("2020")], edges=[])
    store.reserve_epic_node_item(EPIC, "2020", "busy-run")

    for _ in range(2):
        _, sink = _tick(store, tmp_path, config=config, deps=deps)
        assert sink.continued == []
        assert _continuation(store).state == "awaiting_continuation"

    store.release_epic_node_item("2020", run_id="busy-run")
    store.save(_finished_run("busy-run", "2020"))
    _, sink = _tick(store, tmp_path, config=config, deps=deps)

    record = _continuation(store)
    assert sink.continued == [("1010", record.continuation_run_id)]
    assert len(_auto_claims(store)) == 1


def _finished_run(run_id: str, item_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=int(item_id),
        item_id=item_id,
        branch=f"agent/issue-{item_id}",
        status="success",
        exit_code=0,
        started_at=NOW,
        finished_at=NOW,
    )


def _frontier_hold_reason_seen(result: Any) -> str:
    """Every admission reason this tick reported, joined."""
    return " | ".join(w.reason for w in result.waiting)


class _Store:
    """The CLI's store context, yielding the test's own open store."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def __enter__(self) -> Any:
        return self.store

    def __exit__(self, *exc: object) -> None:
        return None


def _attention(store: Any, *, now: datetime) -> list[tuple[Any, str, str]]:
    """The node's attention entries: kind, reason, and the command offered."""
    deps = FakeSource(nodes=[_node("1010")], edges=[])
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={1010: _facts(1010, state="OPEN")}),
        now=now,
    )
    rows = []
    for event in attention_events(EPIC, summary):
        if event.item != "1010":
            continue
        bucket = bucket_for(event.reason)
        command = fill_command(bucket.command, epic=EPIC, item="1010") if bucket else ""
        rows.append((event.kind, event.reason, command))
    return rows


def _second_death(
    store: Any, tmp_path: Path, config: Any, *, dirty: bool
) -> tuple[str, datetime]:
    """Continue the first death automatically, then lose the host again."""
    _tick(store, tmp_path, config=config)
    record = _continuation(store)
    assert record.state == "continuing"
    run_id = record.continuation_run_id
    later = datetime.now(UTC) + timedelta(seconds=1)
    _continuation_dies_with_the_host(store, config, run_id, started_at=later)
    if not dirty:
        Path(record.worktree_identity).joinpath("wip.txt").unlink()
    _, sink = _tick(store, tmp_path, config=config, now=later + timedelta(seconds=1))
    assert sink.continued == []
    return run_id, later + timedelta(seconds=2)


def test_a_dirty_second_death_offers_continue_and_the_command_resumes_the_bytes(
    store: Any, tmp_path: Path
) -> None:
    config, _ = _dirty_first_death(store, tmp_path)

    run_id, now = _second_death(store, tmp_path, config, dirty=True)

    assert store.get(run_id).status == "killed"
    assert _continuation(store).state == "awaiting_continuation"
    assert len(_auto_claims(store)) == 1
    outcomes = [
        e.payload["outcome"]
        for e in store.list_audit_events(event="epic_node_host_restart")
    ]
    assert sorted(outcomes) == ["continue", "host_restart_repeated"]
    [(kind, reason, command)] = _attention(store, now=now)
    assert kind is AttentionKind.HELD
    assert reason.startswith(HOST_RESTART_REPEATED_CONTINUATION_REASON)
    assert command == f"kew epic continue {EPIC} 1010"

    seen = _run_offered_command(store, tmp_path, config, command)

    assert seen == ["preserve me\n"]
    assert _continuation(store).state == "terminal"


def test_dirty_second_death_reports_failed_node_with_one_continue_attention_row(
    store: Any, tmp_path: Path
) -> None:
    """A dirty second death fails the node, as its clean twin does, while the
    checkout keeps the work and the one attention row offers to continue it."""
    from kew.graph.domain import Edge

    config, worktree = _dirty_first_death(store, tmp_path)
    _, now = _second_death(store, tmp_path, config, dirty=True)
    deps = FakeSource(
        nodes=[_node("1010"), _node("1011")], edges=[Edge("1010", "1011")]
    )

    result, sink = _tick(store, tmp_path, config=config, now=now, deps=deps)

    assert result.failed == ("1010",)
    assert "1010" not in {w.node_id for w in result.waiting}
    [stuck] = [s for s in result.stuck if s.node_id == "1010"]
    assert stuck.blast_radius == ("1011",)
    assert stuck.reason.startswith(HOST_RESTART_REPEATED_CONTINUATION_REASON)
    assert sink.calls == sink.continued == []
    assert _continuation(store).state == "awaiting_continuation"
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"
    summary = summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=FakeFactsReader(
            issue_facts_by_num={n: _facts(n, state="OPEN") for n in (1010, 1011)}
        ),
        now=now,
    )
    assert summary.failed == ("1010",)
    [(kind, reason, command)] = _attention(store, now=now)
    assert kind is AttentionKind.HELD
    assert reason.startswith(HOST_RESTART_REPEATED_CONTINUATION_REASON)
    assert command == f"kew epic continue {EPIC} 1010"


def _run_offered_command(
    store: Any, tmp_path: Path, config: Any, command: str
) -> list[str]:
    """Run *command* as printed; return what the agent found in its checkout."""
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from kew.cli.epic import epic_app
    from kew.dispatch_core import DispatchOutcome

    seen: list[str] = []

    def _agent(**kwargs: Any) -> DispatchOutcome:
        checkout = Path(kwargs["worktree_dir"])
        seen.append(checkout.joinpath("wip.txt").read_text(encoding="utf-8"))
        return DispatchOutcome(exit_code=0)

    config.logging.dir = str(tmp_path / "logs")
    issue = {"number": 1010, "title": "Continue", "body": "", "labels": []}
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._epic_repo", return_value="o/r"),
        patch("kew.cli.build_notifier", return_value=MagicMock()),
        patch("kew.cli._open_run_store", return_value=_Store(store)),
        patch("kew.cli.select_next_issue", return_value=issue),
        patch("kew.cli.runner_preflight_failure", return_value=None),
        patch("kew.cli.execute_dispatch", side_effect=_agent),
    ):
        invoked = CliRunner().invoke(epic_app, command.split()[2:])
    assert invoked.exit_code == 0, invoked.output
    return seen


def test_a_clean_second_death_makes_the_continuation_terminal_and_fails_the_node(
    store: Any, tmp_path: Path
) -> None:
    """A stale record cannot reopen a terminal continuation: the continued
    run left a clean checkout, so its host death fails the node."""
    config, _ = _dirty_first_death(store, tmp_path, checkout=True)

    run_id, now = _second_death(store, tmp_path, config, dirty=False)

    assert store.get(run_id).error_category == "host_restart_repeated"
    assert _continuation(store).state == "terminal"
    assert store.list_epic_reservations(EPIC) == []
    assert store.list_epic_retries(EPIC) == []
    assert [(k, c) for k, _, c in _attention(store, now=now)] == [
        (AttentionKind.HOST_RESTART_REPEATED, f"kew epic retry {EPIC} 1010")
    ]
    _, sink = _tick(store, tmp_path, config=config, now=now)
    assert _continuation(store).state == "terminal"
    assert sink.calls == sink.continued == []


def test_the_retry_a_clean_second_death_offers_is_not_refused_as_preserved_work(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed continuation holds no bytes, so retry's preserved-work
    refusal (#2960) does not refuse the command the attention row offers."""
    from unittest.mock import MagicMock, patch

    from kew.service import verbs

    monkeypatch.setenv("KEW_ACTOR", "operator")
    config, _ = _dirty_first_death(store, tmp_path, checkout=True)
    _second_death(store, tmp_path, config, dirty=False)
    assert _continuation(store).state == "terminal"

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref=EPIC, item_id="1010", config=config)

    assert [r.item_id for r in store.list_epic_retries(EPIC)] == ["1010"]
    assert len(store.list_audit_events(event="epic_node_retry")) == 1


def test_a_driver_dying_after_a_clean_second_death_still_fails_the_node(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reap wrote the second host death, then the driver died before it
    closed the continuation. The next tick's reservation settlement must
    finish that close, not reopen the clean checkout as preserved work."""
    config, _ = _dirty_first_death(store, tmp_path, checkout=True)

    real = store.finalize_continuation

    def _dies(*_: Any, **__: Any) -> Any:
        raise _DriverDied

    monkeypatch.setattr(store, "finalize_continuation", _dies)
    with pytest.raises(_DriverDied):
        _second_death(store, tmp_path, config, dirty=False)
    monkeypatch.setattr(store, "finalize_continuation", real)
    record = _continuation(store)
    run_id = record.continuation_run_id
    assert store.get(run_id).error_category == "host_restart_repeated"
    assert record.state == "continuing"
    now = datetime.now(UTC) + timedelta(seconds=3)

    _, sink = _tick(store, tmp_path, config=config, now=now)

    assert _continuation(store).state == "terminal"
    assert store.list_epic_reservations(EPIC) == []
    assert sink.calls == sink.continued == []
    assert [(k, c) for k, _, c in _attention(store, now=now)] == [
        (AttentionKind.HOST_RESTART_REPEATED, f"kew epic retry {EPIC} 1010")
    ]
    _, sink = _tick(store, tmp_path, config=config, now=now)
    assert _continuation(store).state == "terminal"
    assert sink.calls == sink.continued == []


class _SpawnFails(FakeSink):
    """A sink whose continuation child never starts."""

    def continue_claimed(self, item_id: str, continuation_run_id: str) -> bool:
        super().continue_claimed(item_id, continuation_run_id)
        raise OSError("fork failed")


def test_a_transient_spawn_failure_retries_once_then_leaves_the_wait(
    store: Any, tmp_path: Path
) -> None:
    config, worktree = _dirty_first_death(store, tmp_path)

    attempts = []
    for _ in range(4):
        _, sink = _tick(store, tmp_path, config=config, sink=_SpawnFails())
        attempts += sink.continued

    assert len(attempts) == 2
    assert len(_auto_claims(store)) == 2
    undone = [
        e.payload["outcome"]
        for e in store.list_audit_events(event="continuation_released")
    ]
    assert undone == ["claim_undone", "claim_undone"]
    assert _continuation(store).state == "awaiting_continuation"
    assert store.list_epic_reservations(EPIC) == []
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_a_spawn_failure_with_its_retry_left_keeps_the_epic_running(
    store: Any, tmp_path: Path
) -> None:
    """``kew epic run`` keeps ticking on PROGRESSING and exits on anything
    else, so the tick whose spawn failed must say the retry is still coming;
    the tick that spends the second and last attempt must not."""
    from kew.graph.driver.result import TerminalSignal

    config, _ = _dirty_first_death(store, tmp_path)

    first, sink = _tick(store, tmp_path, config=config, sink=_SpawnFails())
    assert len(sink.continued) == 1
    assert first.signal is TerminalSignal.PROGRESSING
    assert "1010" in {w.node_id for w in first.waiting}

    last, sink = _tick(store, tmp_path, config=config, sink=_SpawnFails())
    assert len(sink.continued) == 1
    assert last.signal is not TerminalSignal.PROGRESSING
    assert len(_auto_claims(store)) == 2


class _SpawnedThenBookkeepingFails(FakeSink):
    """A sink whose child launched, then whose key handover write failed."""

    def continue_claimed(self, item_id: str, continuation_run_id: str) -> bool:
        from kew.graph.driver.dispatch import ChildSpawnedError

        super().continue_claimed(item_id, continuation_run_id)
        try:
            raise OSError("database is locked")
        except OSError as exc:
            raise ChildSpawnedError(continuation_run_id) from exc


def test_a_launched_child_keeps_its_claim_when_bookkeeping_fails_after_spawn(
    store: Any, tmp_path: Path
) -> None:
    """The child exists once popen returned: rolling its claim back would let
    a second claim start another agent beside it in the same checkout."""
    from unittest.mock import MagicMock, patch

    from kew.dispatch_core import DispatchOutcome

    config, _ = _dirty_first_death(store, tmp_path)

    _, sink = _tick(store, tmp_path, config=config, sink=_SpawnedThenBookkeepingFails())

    record = _continuation(store)
    assert sink.continued == [("1010", record.continuation_run_id)]
    assert record.state == "continuing"
    assert [r.run_id for r in store.list_epic_reservations(EPIC)] == [
        record.continuation_run_id
    ]
    assert store.list_audit_events(event="continuation_released") == []
    _, again = _tick(store, tmp_path, config=config)
    assert again.continued == []
    assert len(_auto_claims(store)) == 1

    seen: list[str] = []

    def _agent(**kwargs: Any) -> DispatchOutcome:
        seen.append(kwargs["run_id"])
        return DispatchOutcome(exit_code=0)

    issue = {"number": 1010, "title": "Continue", "body": "", "labels": []}
    with (
        patch("kew.cli.build_notifier", return_value=MagicMock()),
        patch("kew.cli.select_next_issue", return_value=issue),
        patch("kew.cli.runner_preflight_failure", return_value=None),
        patch("kew.cli.execute_dispatch", side_effect=_agent),
    ):
        invoked = _run_child(store, tmp_path, config, record)

    assert invoked.exit_code == 0, invoked.output
    assert seen == [record.continuation_run_id]
    assert _continuation(store).state == "terminal"


def _run_child(
    store: Any,
    tmp_path: Path,
    config: Any,
    record: Any,
    *,
    key_hash: str | None = None,
    run_id: str | None = None,
    load_config: Any = None,
    preflight: Any = None,
) -> Any:
    """Invoke the detached ``_continue-node`` child the driver spawns.

    With *key_hash* the child inherits a driver-issued bounded key for the
    claim's run id, exactly as ``DetachedDispatchSink._spawn`` hands it over.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from kew.cli.epic import epic_app
    from kew.run_credentials import ISSUED_KEY_HASH_ENV

    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.cli.load_config", side_effect=load_config or (lambda **_: config)),
        patch("kew.cli.preflight_tools", side_effect=preflight),
        patch("kew.cli._open_run_store", return_value=_Store(store)),
    ):
        return CliRunner().invoke(
            epic_app,
            [
                "_continue-node",
                EPIC,
                "1010",
                "--continuation-run-id",
                record.continuation_run_id,
                *([] if key_hash is None else ["--run-id", run_id or ""]),
            ],
            env={} if key_hash is None else {ISSUED_KEY_HASH_ENV: key_hash},
        )


def test_the_driver_spawned_child_runs_the_agent_in_the_preserved_checkout(
    store: Any, tmp_path: Path
) -> None:
    from unittest.mock import MagicMock, patch

    from kew.dispatch_core import DispatchOutcome

    config, _ = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config)
    record = _continuation(store)
    seen: list[tuple[str, str]] = []

    def _agent(**kwargs: Any) -> DispatchOutcome:
        checkout = Path(kwargs["worktree_dir"])
        seen.append(
            (kwargs["run_id"], checkout.joinpath("wip.txt").read_text(encoding="utf-8"))
        )
        return DispatchOutcome(exit_code=0)

    issue = {"number": 1010, "title": "Continue", "body": "", "labels": []}
    with (
        patch("kew.cli.build_notifier", return_value=MagicMock()),
        patch("kew.cli.select_next_issue", return_value=issue),
        patch("kew.cli.runner_preflight_failure", return_value=None),
        patch("kew.cli.execute_dispatch", side_effect=_agent),
    ):
        invoked = _run_child(store, tmp_path, config, record)

    assert invoked.exit_code == 0, invoked.output
    assert seen == [(record.continuation_run_id, "preserve me\n")]
    assert _continuation(store).state == "terminal"


def test_a_deleted_checkout_stops_after_the_second_cancellation(
    store: Any, tmp_path: Path
) -> None:
    """The real child refuses the missing checkout before any agent launches."""
    config, worktree = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config)
    for path in sorted(worktree.rglob("*"), reverse=True):
        path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
    worktree.rmdir()

    for tick in range(3):
        record = _continuation(store)
        if record.state == "continuing":
            invoked = _run_child(store, tmp_path, config, record)
            assert invoked.exit_code == 2, invoked.output
            assert "missing" in invoked.output
        _, sink = _tick(store, tmp_path, config=config)
        assert len(sink.continued) == (1 if tick == 0 else 0)

    assert len(_auto_claims(store)) == 2
    assert _continuation(store).state == "awaiting_continuation"
    assert store.list_epic_reservations(EPIC) == []


def _fails_on_reload(config: Any) -> Any:
    """``load_config`` that answers the child's first read, then fails."""
    calls: list[object] = []

    def _load(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) > 1:
            raise ValueError("config became unreadable")
        return config

    return _load


def _fail_child(store: Any, tmp_path: Path, config: Any, record: Any, step: str) -> Any:
    """Run the driver's child so it fails at *step*, before any agent launches."""
    from unittest.mock import patch

    if step == "preflight":
        return _run_child(
            store, tmp_path, config, record, preflight=OSError("git is missing")
        )
    if step == "config_reload":
        return _run_child(
            store, tmp_path, config, record, load_config=_fails_on_reload(config)
        )
    with patch("kew.cli.build_notifier", side_effect=RuntimeError("no notifier")):
        return _run_child(store, tmp_path, config, record)


@pytest.mark.parametrize("step", ["preflight", "config_reload", "notifier"])
def test_prelaunch_child_failure_is_cancelled_and_retried_once(
    store: Any, tmp_path: Path, step: str
) -> None:
    """A child that fails before its run exists hands the claim back unlaunched,
    so the driver's one retry is still there; a second failure ends it."""
    config, worktree = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config)

    for attempt in range(2):
        record = _continuation(store)
        assert record.state == "continuing"
        invoked = _fail_child(store, tmp_path, config, record, step)
        assert invoked.exit_code != 0
        assert store.get(record.continuation_run_id) is None
        assert _continuation(store).state == "awaiting_continuation"
        assert store.list_epic_reservations(EPIC) == []
        _, sink = _tick(store, tmp_path, config=config)
        assert len(sink.continued) == (1 if attempt == 0 else 0)

    undone = [
        e.payload["outcome"]
        for e in store.list_audit_events(event="continuation_released")
    ]
    assert undone == ["claim_undone", "claim_undone"]
    assert len(_auto_claims(store)) == 2
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_a_child_dying_before_it_opens_the_store_is_retried_once(
    store: Any, tmp_path: Path
) -> None:
    """A child that dies before it can cancel anything leaves the claim to
    missing-run reconciliation, whose verdict spends the attempt the same way
    a cancellation does: one retry, then the work waits for a person."""
    from kew.graph.driver.tick import _RESERVATION_STARTUP_GRACE_SECONDS

    config, _ = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config)
    now = datetime.now(UTC)

    def _unreadable(**_: Any) -> Any:
        raise ValueError("config is unreadable")

    claims = []
    for _ in range(3):
        record = _continuation(store)
        if record.state == "continuing":
            claims.append(record.continuation_run_id)
            invoked = _run_child(
                store, tmp_path, config, record, load_config=_unreadable
            )
            assert invoked.exit_code != 0
            assert _continuation(store).state == "continuing"
        now += timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS + 1)
        _tick(store, tmp_path, config=config, now=now)

    assert len(claims) == 2
    assert len(_auto_claims(store)) == 2
    assert _continuation(store).state == "awaiting_continuation"
    assert store.list_epic_reservations(EPIC) == []
    reopened = [
        e.payload["outcome"]
        for e in store.list_audit_events(event="continuation_terminal")
    ]
    assert reopened == ["crash_before_run_row", "crash_before_run_row"]


def _delete_checkout(worktree: Path) -> None:
    for path in sorted(worktree.rglob("*"), reverse=True):
        path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
    worktree.rmdir()


@pytest.mark.parametrize("refusal", ["checkout_missing", "run_id_mismatch"])
def test_a_child_refused_before_launch_tombstones_its_bounded_key(
    store: Any, tmp_path: Path, refusal: str
) -> None:
    """A pre-launch refusal leaves the driver's key terminal for reconciliation.

    The driver marked the key child-owned when it spawned the child, so without
    a tombstone a cancelled claim — which writes no run row — kept the key
    live until it expired.
    """
    from kew.credential_prelaunch import absent_run_is_terminal

    config, worktree = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config)
    record = _continuation(store)
    key_hash = "k" * 64
    run_id = record.continuation_run_id
    if refusal == "checkout_missing":
        _delete_checkout(worktree)
    else:
        run_id = "not-the-claim"

    invoked = _run_child(
        store, tmp_path, config, record, key_hash=key_hash, run_id=run_id
    )

    assert invoked.exit_code == 2, invoked.output
    assert store.get(run_id) is None
    assert store.get_meta(f"run_key_prelaunch_tombstone:{run_id}:{key_hash}")
    issuance = SimpleNamespace(run_id=run_id)
    far_future = {"key_hash": key_hash, "expires_at": "2999-01-01T00:00:00+00:00"}
    assert absent_run_is_terminal(store, issuance, far_future, NOW)
    if refusal == "checkout_missing":
        assert _continuation(store).state == "awaiting_continuation"
        [undone] = store.list_audit_events(event="continuation_released")
        assert undone.payload["outcome"] == "claim_undone"


def test_a_child_that_launches_leaves_its_key_to_the_run(
    store: Any, tmp_path: Path
) -> None:
    """Once the run row exists the key is the run's; no tombstone is written."""
    from unittest.mock import MagicMock, patch

    from kew.dispatch_core import DispatchOutcome

    config, _ = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config)
    record = _continuation(store)
    key_hash = "k" * 64

    def _agent_admitted_then_failed(**kwargs: Any) -> DispatchOutcome:
        store.save(_pending_run(kwargs["run_id"], boot=_current_boot()))
        return DispatchOutcome(exit_code=1)

    issue = {"number": 1010, "title": "Continue", "body": "", "labels": []}
    with (
        patch("kew.cli.build_notifier", return_value=MagicMock()),
        patch("kew.cli.select_next_issue", return_value=issue),
        patch("kew.cli.runner_preflight_failure", return_value=None),
        patch("kew.cli.execute_dispatch", side_effect=_agent_admitted_then_failed),
    ):
        invoked = _run_child(
            store,
            tmp_path,
            config,
            record,
            key_hash=key_hash,
            run_id=record.continuation_run_id,
        )

    assert invoked.exit_code != 0, invoked.output
    tombstone = f"run_key_prelaunch_tombstone:{record.continuation_run_id}:{key_hash}"
    assert store.get(record.continuation_run_id) is not None
    assert store.get_meta(tombstone) is None


class _DriverDied(BaseException):
    """The driver process ending between two of the preservation's writes."""


@pytest.mark.parametrize(
    ("write", "after"),
    [
        ("finalize_orphaned_pending", False),
        ("finalize_orphaned_pending", True),
        ("record_continuation", True),
        ("release_epic_node_item", False),
        ("release_epic_node_item", True),
    ],
    ids=[
        "before-finalize",
        "after-finalize",
        "after-continuation",
        "before-release",
        "after-release",
    ],
)
def test_a_driver_dying_mid_preservation_counts_one_host_death_and_continues_once(
    store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
    after: bool,
) -> None:
    """Every boundary of the dirty preservation keeps the death's budget.

    The marker was written after the continuation, so a driver that died in
    between left a continuation no death had been charged for: it was never
    continued automatically, and the node's next host death counted as its
    first. The next tick here starts from whatever the dead driver committed.
    """
    config, worktree = _dirty_first_death(store, tmp_path)
    real = getattr(store, write)

    def _dies(*args: Any, **kwargs: Any) -> Any:
        if after:
            real(*args, **kwargs)
        raise _DriverDied

    monkeypatch.setattr(store, write, _dies)
    with pytest.raises(_DriverDied):
        _tick(store, tmp_path, config=config)
    monkeypatch.setattr(store, write, real)

    continued = []
    for _ in range(3):
        _, sink = _tick(store, tmp_path, config=config)
        continued += sink.continued

    [death] = store.list_audit_events(event="epic_node_host_restart")
    assert death.payload["outcome"] == "continue"
    record = _continuation(store)
    assert record.state == "continuing"
    assert continued == [("1010", record.continuation_run_id)]
    assert len(_auto_claims(store)) == 1
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def _second_identity_for_the_item(store: Any, tmp_path: Path, run: RunRecord) -> None:
    """Record another continuation for node 1010 in its own dirty checkout."""
    from kew.worktree import write_checkout_generation

    store.save(run)
    other = tmp_path / "other-checkout"
    subprocess.run(
        ["git", "init", "--initial-branch", run.branch, str(other)],
        check=True,
        capture_output=True,
    )
    generation = write_checkout_generation(other)
    other.joinpath("wip.txt").write_text("older work\n", encoding="utf-8")
    store.record_continuation(
        EPIC, "1010", run.run_id, run.branch, other, checkout_generation=generation
    )


def _older_run(status: str) -> RunRecord:
    older = _pending_run("dispatch-0", boot=_earlier_boot())
    older.status = status
    older.exit_code = 0 if status == "success" else 137
    older.finished_at = NOW
    return older


def test_automatic_continuation_refuses_multiple_incomplete_identities_for_one_item(
    store: Any, tmp_path: Path
) -> None:
    """``kew epic continue`` refuses an item with two incomplete continuation
    identities as ambiguous; the driver must not settle that choice by record
    order and resume a stale checkout."""
    config, worktree = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config, max_parallel=0)
    _second_identity_for_the_item(store, tmp_path, _older_run("killed"))

    continued = []
    for _ in range(2):
        _, sink = _tick(store, tmp_path, config=config)
        continued += sink.continued

    assert continued == []
    assert _auto_claims(store) == []
    assert [r.state for r in store.list_continuations(EPIC)] == [
        "awaiting_continuation",
        "awaiting_continuation",
    ]
    assert worktree.joinpath("wip.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_automatic_continuation_beside_a_terminal_identity_resumes_the_sole_incomplete_one(  # noqa: E501
    store: Any, tmp_path: Path
) -> None:
    """A finished identity beside the armed one is no ambiguity for the
    operator's selector, so the driver continues the one incomplete record."""
    config, _ = _dirty_first_death(store, tmp_path)
    _tick(store, tmp_path, config=config, max_parallel=0)
    _second_identity_for_the_item(store, tmp_path, _older_run("success"))

    continued = []
    for _ in range(2):
        _, sink = _tick(store, tmp_path, config=config)
        continued += sink.continued

    [armed] = [
        r for r in store.list_continuations(EPIC) if r.original_run_id == "dispatch-1"
    ]
    [finished] = [
        r for r in store.list_continuations(EPIC) if r.original_run_id == "dispatch-0"
    ]
    assert finished.state == "terminal"
    assert armed.state == "continuing"
    assert continued == [("1010", armed.continuation_run_id)]
    assert len(_auto_claims(store)) == 1


def _committed_repeated_restart_rows() -> dict[str, Any]:
    """The rows ``ui-check`` serves the browser for a second host death."""
    import json

    repo = Path(__file__).resolve().parents[2]
    scenario = repo / "frontend/fixtures/scenarios/dashboard-host-restart-repeated.json"
    rows: dict[str, Any] = json.loads(scenario.read_text(encoding="utf-8"))
    return rows


@pytest.mark.parametrize("case", ["clean", "dirty"])
def test_the_browser_check_serves_the_rows_the_dashboard_serves_for_a_second_death(
    store: Any, tmp_path: Path, inbox_client: Any, case: str
) -> None:
    """The console's repeated-restart rows come from the ledger, not a hand.

    The browser check once injected a row it composed itself, so a backend that
    offered the wrong command would still have passed it. The driver writes
    each second death here, the real ``GET /api/dashboard`` serves its row,
    and the committed scenario the browser draws must be that row, field for
    field — all but ``age``, which is the tick's own clock.
    """
    from kew.config import DispatchConfig, ProjectConfig
    from kew.run_store import SqliteRunStore
    from kew.serve.app import create_app

    dirty = case == "dirty"
    config, _ = _dirty_first_death(store, tmp_path, checkout=not dirty)
    _second_death(store, tmp_path, config, dirty=dirty)
    app = create_app(
        lambda: SqliteRunStore(str(tmp_path / "run.db")),
        repo_dir=tmp_path,
        expose=False,
        config=DispatchConfig(project=ProjectConfig(repo="")),
    )

    body = inbox_client(app, loopback_peer=True).get("/api/dashboard").json()

    [served] = [row for row in body["rows"] if row["item_id"] == "1010"]
    committed = _committed_repeated_restart_rows()[case]
    verb = "continue" if dirty else "retry"
    assert served["command"] == f"kew epic {verb} {EPIC} 1010"
    assert {k: v for k, v in served.items() if k != "age"} == {
        k: v for k, v in committed.items() if k != "age"
    }


def _awaiting_continuation(store: Any, tmp_path: Path) -> Any:
    """A dirty earlier-boot run, preserved and offered as a continuation."""
    config, _worktree = _pending_run_with_checkout(
        store, tmp_path, "dispatch-1", dirty=True
    )
    _tick(store, tmp_path, config=config, max_parallel=0)
    assert _continuation(store).state == "awaiting_continuation"
    return config


def test_max_parallel_zero_holds_an_awaiting_continuation_across_a_reload(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--max-parallel 0`` is an operator stop (#3192): it reaches the tick
    as 0, not as "unset", both at start and after a per-tick config reload, so
    the awaiting continuation is never claimed and nothing is spawned."""
    from kew.cli.epic import (
        _driver_identity,
        _DriverPin,
        _epic_driver_config,
        _reload_driver_controls,
    )

    config = _awaiting_continuation(store, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{tmp_path}"\n\n'
        "[epic]\nmax_parallel = 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(project))

    loaded, _, started, _ = _epic_driver_config(0)
    pin = _DriverPin(_driver_identity(loaded), loaded)
    _, reloaded, _ = _reload_driver_controls(store, pin=pin, max_parallel_override=0)

    assert (started, reloaded) == (0, 0)
    sinks = [
        _tick(store, tmp_path, config=config, max_parallel=cap)[1]
        for cap in (started, reloaded)
    ]
    assert [sink.continued for sink in sinks] == [[], []]
    assert [sink.calls for sink in sinks] == [[], []]
    assert _auto_claims(store) == []
    assert _continuation(store).continuation_run_id is None
    _, _, unset, _ = _epic_driver_config(None)
    assert unset == 4


def test_a_lost_continuation_claim_reports_the_node_in_flight(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another caller (``kew epic continue``) wins the claim CAS between the
    tick's read and its claim (#3192). The tick re-reads the winner and
    reports the node in flight under it — not refused, not STALLED — and
    spawns nothing of its own."""
    from kew.graph.driver.result import TerminalSignal

    config = _awaiting_continuation(store, tmp_path)
    claim = store.claim_continuation_under_cap

    def concurrent_winner(*args: Any, **kwargs: Any) -> Any:
        winner = dict(kwargs, actor_id="epic_resume")
        winner["claim_evidence"] = dict(
            kwargs["claim_evidence"], continuation_run_id="winner-run"
        )
        won = (*args[:5], "winner-run", *args[6:])
        assert claim(*won, **winner) == "claimed"
        return claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim_continuation_under_cap", concurrent_winner)

    result, sink = _tick(store, tmp_path, config=config)

    assert result.in_flight == ("1010",)
    assert result.signal is not TerminalSignal.STALLED
    assert sink.continued == []
    record = _continuation(store)
    assert (record.state, record.continuation_run_id) == ("continuing", "winner-run")
