"""Round-14 kernel identity, detached owners, and safe orphan termination."""

import contextlib
import csv
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial, partialmethod
from unittest.mock import Mock

import pytest
from test_liveness_owner import BIRTH
from test_liveness_owner import process_table as process_table

from kew import fleet, process_groups
from kew.config import DispatchConfig
from kew.graph.driver import tick
from kew.run_store import RunRecord, SqliteRunStore, export_runs_csv, export_runs_json

OWNER, ATTEMPT = 100001, 100002
_REAL_START_TOKEN = getattr(process_groups, "process_start_token", None)


def _row():
    row = RunRecord("round14", 42, "agent/42", "pending", -1, datetime.now(UTC))
    row.pgid = row.owner_pid = OWNER
    row.owner_start_time = row.attempt_start_time = BIRTH
    row.owner_ppid, row.owner_command = 44, "python3"
    row.owner_start_token = "100.000001"
    row.attempt_pgid = ATTEMPT
    row.attempt_start_token = "100.000002"
    row.attempt_owner_verified = True
    return row


def _kernel_token(tokens, pid):
    return tokens.get(pid)


def _tokens(monkeypatch, table):
    tokens = {OWNER: "100.000001", ATTEMPT: "100.000002"}
    reader = partial(_kernel_token, tokens)
    monkeypatch.setattr(process_groups, "process_start_token", reader, raising=False)
    monkeypatch.setattr("kew.run_store.process_start_token", reader, raising=False)
    table.members[ATTEMPT] = (ATTEMPT, "S", "sleep", "sleep 60")
    table.parents[ATTEMPT] = 1
    return tokens


@pytest.mark.parametrize("retained", [False, True])
def test_round14_absent_owner_reuse_never_signals(
    monkeypatch, process_table, caplog, capsys, retained
):
    tokens = _tokens(monkeypatch, process_table)
    row = _row()
    if retained:
        snapshot = getattr(
            process_groups,
            "group_member_identities",
            process_groups.group_member_births,
        )
        row.termination_members[ATTEMPT] = snapshot(ATTEMPT)
    tokens[ATTEMPT] = "100.000003"  # Same lstart, different kernel birth.
    fleet.stop_orphaned_attempt(row)
    assert not process_table.signals, "same-second reuse must receive zero signals"
    diagnostic = caplog.text + capsys.readouterr().err
    assert "attempt_identity_unverifiable" in diagnostic
    tokens[ATTEMPT] = row.attempt_start_token
    fleet.stop_orphaned_attempt(row)
    assert process_table.signals, "matching kernel identity must authorize termination"


def _unavailable(pid):
    return None


def test_round14_unavailable_token_allows_liveness_never_signals(
    monkeypatch, process_table, caplog
):
    _tokens(monkeypatch, process_table)
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[OWNER] = 1
    row = _row()
    monkeypatch.setattr(
        process_groups, "_darwin_start_token", _unavailable, raising=False
    )
    monkeypatch.setattr(
        process_groups, "_linux_start_token", _unavailable, raising=False
    )
    if _REAL_START_TOKEN is not None:
        monkeypatch.setattr(process_groups, "process_start_token", _REAL_START_TOKEN)
    assert tick._owner_alive(row) is None, "unreadable recorded tokens remain unknown"
    process_table.members.pop(OWNER)
    fleet.stop_orphaned_attempt(row)
    assert not process_table.signals, "lstart alone must never authorize a signal"
    assert "attempt_identity_unverifiable" in caplog.text


@pytest.mark.parametrize("parent", [1, OWNER])
def test_round14_reparenting_changes_authority_not_liveness(
    monkeypatch, process_table, parent
):
    _tokens(monkeypatch, process_table)
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[OWNER] = parent
    row = _row()
    assert tick._owner_alive(row) is True, "PPID drift must never prove owner death"
    assert fleet._validated_owner(row)[0] is None


def test_round14_v54_token_columns_replay_export_readback(tmp_path):
    path = str(tmp_path / "tokens.db")
    fields = {"owner_start_token", "attempt_start_token"}
    with SqliteRunStore(path) as store:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        for field in fields & columns:
            store._conn.execute(f"ALTER TABLE runs DROP COLUMN {field}")
        store._conn.execute("UPDATE schema_version SET version = 53")
        store._conn.commit()
    with SqliteRunStore(path) as store:
        columns = [r[1] for r in store._conn.execute("PRAGMA table_info(runs)")]
        assert fields <= set(columns), "v54 must add both kernel token columns"
        row = _row()
        row.termination_members = {
            ATTEMPT: {ATTEMPT: '["birth",1,"sleep",100002,"100.000002"]'}
        }
        store.save(row)
        for _ in range(2):
            store._apply_v54(store._conn.cursor())
        store._conn.commit()
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            # 73: v58 review_mode (#2111), v59 accepted_verdict (#2151),
            # v60 item_plans + the round's plan binding (#2290),
            # v61 triage_decisions (#2293), v62 the driver's expected cadence,
            # v63 its lock snapshot (#2297), v64 the epic_tick_log
            # tick-attempt table (#2304), v65 the prompt's self-check section
            # (#2454), v66 a run's fleet placement (#2398), v67 the
            # acceptance_contracts table (#2726), v68 its plan binding
            # (#2774), v69 the boot a run's owner ran under (#2812), v70 the
            # epic_merge_state_reads table (#2879), and v71 the merge_queue
            # table (#2814), and v72 the run a host-restart retry marker
            # supersedes (#2969), and v73 the queue stop's kind and failure
            # (#2883). v74 adds prompt profiles (#3020). v75 marks the tick
            # attempt that journaled each change (#2949). v76 adds the merge
            # claims an operator verify holds (#3198).
            == 77
        )
    with SqliteRunStore(path) as store:
        saved = store.get(row.run_id)
        assert saved.owner_start_token == row.owner_start_token
        assert saved.attempt_start_token == row.attempt_start_token
        assert saved.termination_members == row.termination_members
        for exported in (
            json.loads(export_runs_json([saved]))[0],
            next(csv.DictReader(io.StringIO(export_runs_csv([saved])))),
        ):
            assert exported["owner_start_token"] == row.owner_start_token
            assert exported["attempt_start_token"] == row.attempt_start_token
        stale = _row()
        stale.owner_start_token = stale.attempt_start_token = "stale"
        store.save(stale)
        assert store.get(row.run_id).owner_start_token == row.owner_start_token
        assert store.get(row.run_id).attempt_start_token == row.attempt_start_token


def test_round14_registration_persists_tokens(monkeypatch, process_table):
    _tokens(monkeypatch, process_table)
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[OWNER] = 1  # Detached admission PPID was 44.
    process_table.parents[ATTEMPT] = OWNER
    row = _row()
    row.attempt_pgid = row.attempt_start_token = None
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        store.set_attempt_pgid(row.run_id, ATTEMPT)
        saved = store.get(row.run_id)
        assert getattr(saved, "attempt_start_token", None) == "100.000002"
        assert saved.attempt_owner_verified, "reparented owners may register retries"


_DETACH = """
import json, os, subprocess, sys
from kew.process_groups import (
    identity_at_admission, process_start_token, process_start_time,
)
child = subprocess.Popen(['sleep', '60'], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
identity = identity_at_admission(child.pid)
print(json.dumps({'pid': child.pid, 'identity': identity,
                  'previous_token': process_start_token(os.getpid()),
                  'previous_birth': process_start_time(os.getpid())}), flush=True)
"""


class _Clock(datetime):
    instant = datetime(2026, 9, 20, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.instant


def _runtime(state, argv, **kwargs):
    state["calls"].append(argv[1])
    if argv[1] == "kill":
        state["running"] = False
    live = str(state["running"]).lower()
    return subprocess.CompletedProcess(argv, 0, f"{'a' * 64} /kew-42 {live}", "")


def _reconcile(store):
    tick._reconcile_reservations(
        "epic", store, DispatchConfig(), now=tick.datetime.now(UTC)
    )


@pytest.mark.serial
@pytest.mark.parametrize("contained", [True, False])
def test_round14_real_detached_parent_exit(monkeypatch, contained):
    with subprocess.Popen(
        [sys.executable, "-c", _DETACH],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as wrapper:
        payload = json.loads(wrapper.stdout.readline())
        wrapper.wait(timeout=10)
    child = payload["pid"]
    try:
        row = _row()
        row.pgid = row.owner_pid = child
        # The exited wrapper group supplies a completed attempt anchor.
        row.attempt_pgid = wrapper.pid
        row.attempt_start_token = payload["previous_token"]
        row.attempt_start_time = payload["previous_birth"]
        assert payload["identity"], "real admission requires host ps access"
        for field, value in payload["identity"].items():
            setattr(row, field, value)
        assert row.owner_ppid == wrapper.pid, "admission must precede parent exit"
        assert tick._owner_alive(row) is True, "detached owner must survive reparenting"
        monkeypatch.setattr(tick, "datetime", _Clock)
        row.started_at = _Clock.instant - timedelta(seconds=601)
        state = {"running": True, "calls": []}
        monkeypatch.setattr(
            "kew.sandbox.detect_runtime_cached", Mock(return_value="/fake/docker")
        )
        monkeypatch.setattr(tick.subprocess_utils, "run", partial(_runtime, state))
        if contained:
            row.container_name, row.container_id = "kew-42", "a" * 64
            row.container_runtime = "docker"
        with SqliteRunStore(":memory:") as store:
            store.save(row)
            store.reserve_epic_node("epic", 42, row.run_id)
            _reconcile(store)
            assert store.get(row.run_id).status == "pending"
            assert "kill" not in state["calls"], (
                "live contained work must survive cleanup"
            )
            os.kill(child, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while (
                not process_groups.process_exited(child) and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            _reconcile(store)
            assert store.get(row.run_id).status == "killed", "owner death must finalize"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


_SAME_SECOND_ATTEMPTS = 5
# One kernel tick at the coarsest CONFIG_HZ Linux ships (100Hz), plus slack.
_TICK = 0.02


def _pause(seconds):
    """Wait out ``seconds`` of wall clock without touching ``time.sleep``.

    ``conftest``'s autouse ``_no_selector_requery_sleep`` sets
    ``kew.selector.time.sleep`` -- and ``kew.selector.time`` *is* the stdlib
    ``time`` module, so that no-op lands on ``time.sleep`` process-wide. Under
    it the tick separation below evaporates, both children are born on one
    kernel tick, and their start tokens compare equal: that, not a second
    boundary, is what made this test red (#2221). ``Event.wait`` blocks in the
    C lock, so it cannot be hollowed out by a patch of ``time.sleep``.
    """
    threading.Event().wait(seconds)


@contextlib.contextmanager
def _same_second_births(attempts=_SAME_SECOND_ATTEMPTS, clock=time.time, pause=_pause):
    """Yield two processes born one kernel tick apart inside one wall-clock second.

    Linux has tick resolution, so the pair must share a second for the token
    claim to mean anything. A loaded host can still deschedule us across the
    boundary between the two births; that is a setup miss, not a verdict, so
    retry the births a bounded number of times instead of failing on them.
    """
    for _ in range(attempts):
        # Start on a fresh second so the pair has a whole one to be born in.
        pause(1.02 - clock() % 1)
        started = int(clock())
        with subprocess.Popen(["sleep", "60"]) as first:
            try:
                pause(_TICK)
                with subprocess.Popen(["sleep", "60"]) as second:
                    try:
                        if int(clock()) == started:
                            yield first, second
                            return
                    finally:
                        second.kill()
            finally:
                first.kill()
    pytest.fail(f"no birth pair in {attempts} attempts shared one wall-clock second")


@pytest.mark.serial
def test_round14_real_same_second_tokens_differ():
    with _same_second_births() as (first, second):
        reader = getattr(
            process_groups,
            "process_start_token",
            process_groups.process_start_time,
        )
        tokens = reader(first.pid), reader(second.pid)
        assert all(tokens), "kernel readers must provide both tokens"
        assert tokens[0] != tokens[1], "same-second births need different kernel tokens"
        assert process_groups.process_start_time(
            first.pid
        ) == process_groups.process_start_time(second.pid)


def test_round14_birth_pause_survives_the_suite_wide_sleep_noop(monkeypatch):
    # The real cause of #2221: conftest no-ops time.sleep for the whole process,
    # so a pause spent through it separates the births by nothing at all.
    monkeypatch.setattr(time, "sleep", lambda _s: pytest.fail("pause used time.sleep"))
    started = time.monotonic()
    _pause(_TICK)
    assert time.monotonic() - started >= _TICK, "the pause must spend real time"


def test_round14_same_second_births_retry_a_rollover():
    # Attempt one crosses the boundary (10.6 -> 11.1); the helper must retry it.
    instants = iter([10.5, 10.6, 11.1, 20.5, 20.6, 20.9])
    naps = []
    with _same_second_births(clock=lambda: next(instants), pause=naps.append) as pair:
        assert all(born.poll() is None for born in pair), "retry must yield live births"
    assert naps == [0.52, _TICK, 0.52, _TICK], "a rolled-over attempt must retry"


def test_round14_same_second_births_fail_only_after_every_attempt():
    # Every attempt crosses the boundary, so the bounded retry must give up loudly.
    # The instants are finite on purpose: an unbounded retry runs them out and
    # dies, where an endless cycle would hang this test instead of reddening it.
    rollovers = iter([10.5, 10.6, 11.1] * 2)
    naps = []
    with (
        pytest.raises(
            pytest.fail.Exception, match="shared one wall-clock second"
        ) as failure,
        _same_second_births(
            attempts=2, clock=lambda: next(rollovers), pause=naps.append
        ),
    ):
        pytest.fail("a rolled-over attempt must never be yielded")
    assert "2 attempts" in str(failure.value), "the bound must name its attempts"
    assert len(naps) == 4, "the bound must stop after exactly its attempts"


def _read_proc(path, files, *args, **kwargs):
    value = files[str(path)]
    if isinstance(value, Exception):
        raise value
    return value


def test_round14_linux_kernel_reader(monkeypatch):
    from pathlib import Path

    reader = getattr(process_groups, "_linux_start_token", None)
    assert reader is not None, "Linux must read kernel ticks instead of lstart"
    files = {
        "/proc/123/stat": "123 (a tricky ) comm) S " + "0 " * 18 + "987 0",
        "/proc/stat": "cpu 0 0\nbtime 123456\nprocesses 42\n",
    }
    monkeypatch.setattr(Path, "read_text", partialmethod(_read_proc, files))
    assert reader(123) == "123456+987", "field 22 must survive spaces and ')' in comm"
    monkeypatch.setattr(process_groups.sys, "platform", "linux")
    files["/proc/123/stat"] = PermissionError("denied")
    assert process_groups.process_start_token(123) is None
    files["/proc/123/stat"] = "123 (truncated) S"
    assert process_groups.process_start_token(123) is None


def _bsd_probe(info, result, pid, flavor, argument, output, size):
    import ctypes

    assert (pid, flavor, argument, size) == (123, 3, 0, 136)
    ctypes.memmove(output, ctypes.byref(info), size)
    return result


@pytest.mark.parametrize("damage", [None, "pid", "short", "usec"])
def test_round14_darwin_kernel_reader(monkeypatch, damage):
    reader = getattr(process_groups, "_darwin_start_token", None)
    assert reader is not None, "Darwin must read libproc microsecond births"
    info = process_groups._ProcBsdInfo()
    info.pbi_pid = 456 if damage == "pid" else 123
    info.pbi_start_tvsec = 100
    info.pbi_start_tvusec = 1_000_000 if damage == "usec" else 42
    probe = Mock(side_effect=partial(_bsd_probe, info, 0 if damage == "short" else 136))
    library = Mock(proc_pidinfo=probe)
    loader = Mock(return_value=library)
    monkeypatch.setattr(process_groups.ctypes, "CDLL", loader)
    assert reader(123) == (None if damage else "100.000042")
    loader.assert_called_once_with("/usr/lib/libproc.dylib", use_errno=True)


@pytest.mark.parametrize("contained", [True, False])
def test_round14_reparented_owner_cleanup(monkeypatch, process_table, contained):
    _tokens(monkeypatch, process_table)
    process_table.members.pop(ATTEMPT)
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[OWNER] = 1
    row = _row()
    # The registered attempt is already absent during owner retry backoff.
    row.started_at = _Clock.instant - timedelta(seconds=601)
    monkeypatch.setattr(tick, "datetime", _Clock)
    state = {"running": True, "calls": []}
    monkeypatch.setattr(
        "kew.sandbox.detect_runtime_cached", Mock(return_value="/fake/docker")
    )
    monkeypatch.setattr(tick.subprocess_utils, "run", partial(_runtime, state))
    if contained:
        row.container_name, row.container_id = "kew-42", "a" * 64
        row.container_runtime = "docker"
    with SqliteRunStore(":memory:") as store:
        store.save(row)
        store.reserve_epic_node("epic", 42, row.run_id)
        _reconcile(store)
        assert store.get(row.run_id).status == "pending", (
            "reparenting must retain the row"
        )
        assert "kill" not in state["calls"], "a live owner's container must survive"
        assert not process_table.signals
        process_table.members.pop(OWNER)
        _reconcile(store)
        assert store.get(row.run_id).status == "killed", (
            "owner death must release the row"
        )
