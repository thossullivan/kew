"""Round-13 admission, orphan cleanup, and process identity regressions."""

import inspect
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from test_liveness_owner import BIRTH
from test_liveness_owner import process_table as process_table

from kew import process_groups
from kew.config import DispatchConfig
from kew.fleet import _live_agent_pgid, stop_orphaned_attempt
from kew.graph.driver import tick
from kew.run_store import RunRecord, SqliteRunStore

CID = "a" * 64
OWNER, CLIENT = 100001, 100002


def _round13b_dispatch(monkeypatch, table):
    monkeypatch.setattr(os, "getpid", lambda: OWNER)
    monkeypatch.setattr(os, "getpgrp", lambda: OWNER)
    table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    table.parents[OWNER] = 44


def test_round13b_descendants_admit(monkeypatch, process_table):
    _round13b_dispatch(monkeypatch, process_table)
    # Follow the complete snapshot even when an intermediate parent changed groups.
    for pid, parent, group in ((CLIENT, OWNER, CLIENT), (CLIENT + 1, CLIENT, OWNER)):
        process_table.members[pid] = (group, "S", "git", "git status")
        process_table.parents[pid] = parent
    process_table.members[CLIENT + 2] = (OWNER, "Z", "tee", "tee log")
    assert process_groups.exclusive_owner_pid() == OWNER, "descendants may coexist"


def test_round13b_peer_refusal_names_peer(monkeypatch, process_table):
    _round13b_dispatch(monkeypatch, process_table)
    process_table.members[CLIENT] = (OWNER, "S", "tee", "tee log")
    process_table.parents[CLIENT] = 44
    with pytest.raises(RuntimeError, match=f"peer {CLIENT} tee"):
        process_groups.exclusive_owner_pid()


@pytest.mark.parametrize("recovers", [True, False])
@pytest.mark.parametrize("unavailable", ["missing", "timeout", "malformed"])
def test_round13b_scan_retries(monkeypatch, process_table, recovers, unavailable):
    _round13b_dispatch(monkeypatch, process_table)
    original = process_groups.run
    scans = []
    pause = Mock()
    monkeypatch.setattr("time.sleep", pause)

    def scan(argv, **kwargs):
        scans.append(argv)
        if recovers and len(scans) == 3:
            return original(argv, **kwargs)
        if unavailable == "missing":
            raise FileNotFoundError("ps")
        if unavailable == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "unparsable", "")

    monkeypatch.setattr(process_groups, "run", scan)
    if recovers:
        assert process_groups.exclusive_owner_pid() == OWNER, "third scan may admit"
    else:
        with pytest.raises(RuntimeError, match="scan unavailable"):
            process_groups.exclusive_owner_pid()
    assert len(scans) == 3, "retry unavailable scans exactly three times"
    assert pause.call_count == 2
    assert 1.8 <= sum(call.args[0] for call in pause.call_args_list) <= 2.0


def test_round13b_stale_missing_birth_absent_or_present(process_table):
    with SqliteRunStore(":memory:") as store:
        for name, pgid in (("absent", OWNER), ("present", CLIENT)):
            row = _row(name)
            row.started_at -= timedelta(hours=25)
            row.pgid = pgid
            row.owner_pid = row.owner_start_time = row.attempt_pgid = None
            row.owner_start_token = None
            store.save(row)
        process_table.members[CLIENT] = (CLIENT, "S", "python3", "python3")
        assert process_groups.owner_alive(OWNER, None) is None
        assert store.cleanup_stale_pending() == 1, "absent legacy group is dead"
        assert store.get("absent").status == "failure"
        assert store.get("absent").error_category == "stale_pending"
        assert store.get("present").status == "pending", (
            "present legacy group is unknown"
        )


_ROUND13B_CHILD_CODE = """
import os, subprocess, sys
from kew.process_groups import exclusive_owner_pid
with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                      stdin=subprocess.PIPE) as child:
    try:
        assert os.getpgid(child.pid) == os.getpid()
        assert exclusive_owner_pid() == os.getpid()
        assert child.poll() is None
        print('admitted with live child')
    finally:
        child.communicate(timeout=5)
"""


@pytest.mark.serial
def test_round13b_real_live_descendant_admits():
    result = subprocess.run(
        [sys.executable, "-c", _ROUND13B_CHILD_CODE],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "admitted with live child"


def _row(run_id="old", issue=42):
    row = RunRecord(run_id, issue, f"agent/{issue}", "pending", -1, datetime.now(UTC))
    row.pgid = row.owner_pid = OWNER
    row.owner_start_time = BIRTH
    row.owner_ppid, row.owner_command = 1, "python3"
    row.attempt_pgid, row.attempt_start_time = CLIENT, BIRTH
    row.owner_start_token = f"{BIRTH}+{OWNER}"
    row.attempt_start_token = f"{BIRTH}+{CLIENT}"
    row.attempt_owner_verified = True
    return row


def _admit(store, row, surface):
    if surface == "plain":
        return store.try_admit_run(row, 1)
    identity = store.build_admission_identity(
        row.run_id, tracker="github", item_id=str(row.issue_num), repo="example/repo"
    )
    return store.admit_run_with_identity(row, identity, cap=1)


def _check_admission(store, row, surface):
    store.save(row)
    probe = tick.default_run_liveness(DispatchConfig())
    assert store.live_run_count(liveness=probe) == 0, "driver preflight admits"
    assert _admit(store, _row("new", 43), surface), "atomic admission must also admit"
    assert store.get(row.run_id).status == "killed"
    assert store.list_audit_events(event="orphaned_run_reaped", run_id=row.run_id)


@pytest.mark.parametrize("surface", ["plain", "identity"])
def test_round13_cap_one_crash_admits(process_table, surface):
    with SqliteRunStore(":memory:") as store:
        _check_admission(store, _row(), surface)


@pytest.mark.parametrize("surface", ["plain", "identity"])
def test_round13_cap_one_real_exited_child(surface):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        birth = process_groups.process_start_time(proc.pid)
        assert birth is not None, "real child birth requires host ps access"
        row = _row()
        row.pgid = row.owner_pid = proc.pid
        row.owner_start_time = birth
        row.attempt_pgid = None
        proc.communicate(timeout=5)
        with SqliteRunStore(":memory:") as store:
            _check_admission(store, row, surface)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


@pytest.mark.parametrize("surface", ["plain", "identity"])
def test_round13_reconciles_dead_but_unknown_still_blocks(process_table, surface):
    with SqliteRunStore(":memory:") as store:
        store.save(_row())
        unknown = _row("unknown", 44)
        unknown.pgid = unknown.owner_pid = None
        unknown.owner_start_time = None
        store.save(unknown)
        assert not _admit(store, _row("new", 43), surface)
        assert store.get("old").status == "killed", "only the proven death reconciles"
        assert store.get("unknown").status == "pending"


def _contained(store):
    row = _row()
    row.container_name, row.container_id = "kew-42", CID
    row.container_runtime = "docker"
    row.started_at -= timedelta(seconds=601)
    store.save(row)
    store.reserve_epic_node("epic", 42, row.run_id)
    return row


def _runtime(monkeypatch, table, store, *, unproven=False):
    state = {"inspects": 0, "running": True, "unproven": unproven, "store": store}
    calls = []
    table.members[CLIENT] = (CLIENT, "S", "sh", "sh")
    table.parents[CLIENT] = OWNER
    real_kill = os.kill

    def kill(pid, sig):
        calls.append(("client", pid))
        assert not state["running"], "container must stop before client termination"
        assert getattr(state["store"].get("old"), "cleanup_runtime", None) == "docker"
        real_kill(pid, sig)

    def runtime(argv, **kwargs):
        assert argv[-1] == CID, "every runtime operation must use the durable id"
        calls.append((argv[1], argv[-1]))
        if argv[1] == "kill" and not state["unproven"]:
            state["running"] = False
        if argv[1] == "inspect":
            state["inspects"] += 1
            if state["inspects"] == 1:
                return subprocess.CompletedProcess(
                    argv, 1, "", f"No such container: {argv[-1]}"
                )
        return subprocess.CompletedProcess(
            argv, 0, f"{CID} /kew-42 {str(state['running']).lower()}", ""
        )

    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda c: "/fake/docker")
    monkeypatch.setattr(tick.subprocess_utils, "run", runtime)
    monkeypatch.setattr(os, "kill", kill)
    # TERM is observed too. The fixture keeps clients alive until KILL.
    killpg = os.killpg

    def signal_group(pgid, sig):
        if sig:
            calls.append(("client", pgid))
            assert not state["running"]
            assert (
                getattr(state["store"].get("old"), "cleanup_runtime", None) == "docker"
            )
        killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", signal_group)
    return state, calls


def _tick(store):
    tick._reconcile_reservations("epic", store, DispatchConfig(), now=datetime.now(UTC))


@pytest.mark.parametrize("unproven", [False, True])
def test_round13_late_container_order_and_retry(
    tmp_path, monkeypatch, process_table, unproven
):
    path = str(tmp_path / "retry.db")
    with SqliteRunStore(path) as store:
        row = _contained(store)
        state, calls = _runtime(monkeypatch, process_table, store, unproven=unproven)
        assert tick.default_run_liveness(DispatchConfig())(row) is False
        _tick(store)
        if unproven:
            assert getattr(store.get("old"), "cleanup_runtime", None) == "docker", (
                "persist retry intent"
            )
            assert store.get("old").status == "pending"
            assert CLIENT in process_table.members
            state["unproven"] = False
        else:
            assert store.get("old").status == "killed"
    if unproven:
        with SqliteRunStore(path) as store:
            state["store"] = store
            _tick(store)
            assert store.get("old").status == "killed", "next tick completes cleanup"
    _assert_cleanup_calls(calls, process_table)


def _assert_cleanup_calls(calls, process_table):
    assert calls.index(("kill", CID)) < next(
        i for i, c in enumerate(calls) if c[0] == "client"
    )
    assert CLIENT not in process_table.members
    assert calls[-1] == ("inspect", CID), (
        "container death must be verified after client death"
    )


def test_round13_dead_client_running_container(monkeypatch, process_table):
    with SqliteRunStore(":memory:") as store:
        _contained(store)
        state, calls = _runtime(monkeypatch, process_table, store)
        state["inspects"] = 1
        process_table.members.clear()
        _tick(store)
        assert ("kill", CID) in calls, "a live container cannot hide its dead owner"
        assert store.get("old").status == "killed"


@pytest.mark.parametrize("missing", ["runtime", "inspect"])
def test_round13_missing_proof_retains_intent(monkeypatch, process_table, missing):
    with SqliteRunStore(":memory:") as store:
        _contained(store)
        _, calls = _runtime(monkeypatch, process_table, store)
        if missing == "runtime":
            monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda c: None)
        else:
            monkeypatch.setattr(
                tick.subprocess_utils,
                "run",
                lambda *a, **k: (_ for _ in ()).throw(OSError("inspect")),
            )
        _tick(store)
        assert getattr(store.get("old"), "cleanup_runtime", None) == "docker"
        assert store.get("old").status == "pending"
        assert CLIENT in process_table.members
        assert not any(call[0] == "client" for call in calls)


@pytest.mark.parametrize(
    "recorded,expected", [("Mon Sep 14 10:00:00 2026", False), (None, None)]
)
def test_round13_owner_birth_verdict(process_table, recorded, expected):
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    row = _row()
    row.owner_start_time = recorded
    row.owner_start_token = f"{recorded}+{OWNER}" if recorded else None
    evidence = dict(
        owner_start_time=recorded,
        owner_ppid=1,
        owner_command="python3",
        owner_start_token=row.owner_start_token,
    )
    parameters = inspect.signature(process_groups.owner_alive).parameters
    verdict = process_groups.owner_alive(
        OWNER,
        OWNER,
        **{key: value for key, value in evidence.items() if key in parameters},
    )
    assert verdict is expected, (
        "owner_alive must distinguish mismatch from missing birth"
    )
    assert tick._owner_alive(row) is expected
    assert tick._owner_may_be_alive(row) is (expected is not False)


@pytest.mark.parametrize("target", ["owner", "attempt", "retained"])
@pytest.mark.parametrize("changed", ["parent", "command"])
def test_round13_same_second_reuse_never_signals(process_table, target, changed):
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.members[CLIENT] = (CLIENT, "S", "sh", "sh")
    process_table.parents[CLIENT] = OWNER
    row = _row()
    pid = OWNER
    if target == "owner":
        process_table.members.pop(CLIENT)
    if target == "retained":
        # Use the public snapshot representation of each implementation.
        snapshot = getattr(
            process_groups,
            "group_member_identities",
            process_groups.group_member_births,
        )
        row.termination_members[CLIENT] = snapshot(CLIENT)
    if changed == "parent":
        process_table.parents[pid] = 999
    else:
        group, state, _, args = process_table.members[pid]
        process_table.members[pid] = (group, state, "replacement", args)
    if target == "retained":
        stop_orphaned_attempt(row)
        assert not process_table.signals, (
            "same-second retained PID reuse must refuse signals"
        )
    else:
        assert _live_agent_pgid(row)[0] is None, (
            "same-second reuse must refuse signal authority"
        )


def test_round13_absent_container_dead_reused_owner(monkeypatch, process_table):
    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.births[OWNER] = "Wed Sep 16 10:00:00 2026"
    with SqliteRunStore(":memory:") as store:
        row = _contained(store)
        monkeypatch.setattr(tick, "_container_alive", lambda *a: False)
        assert tick.default_run_liveness(DispatchConfig())(row) is False


def test_round13_exclusive_scan_refuses_unknown_or_peer(monkeypatch, process_table):
    _round13b_dispatch(monkeypatch, process_table)
    monkeypatch.setattr("time.sleep", lambda _: None)
    for output in (None, f"{OWNER} 44 {OWNER} S kew\n{CLIENT} 44 {OWNER} S tee"):
        monkeypatch.setattr(
            process_groups, "ps_output", lambda *a, output=output, **k: output
        )
        with pytest.raises(
            RuntimeError, match="shared foreground group.*without a pipeline.*setsid"
        ):
            process_groups.exclusive_owner_pid()


_PIPELINE_CODE = """
import json, sys, time
from datetime import UTC, datetime
from kew.process_groups import exclusive_owner_pid
from kew.run_store import SqliteRunStore, RunRecord
time.sleep(0.1)
with SqliteRunStore(sys.argv[1]) as store:
    error = ''
    try:
        exclusive_owner_pid()
        store.save(RunRecord('pipeline', 42, 'b', 'pending', -1, datetime.now(UTC)))
    except RuntimeError as exc:
        error = str(exc)
    print(json.dumps({'rows': len(store.list_all()), 'error': error}))
"""


@pytest.mark.parametrize("pipeline", [True, False])
@pytest.mark.serial
def test_round13_real_foreground_pipeline(tmp_path, pipeline):
    command = 'set -m; "$@" | cat' if pipeline else 'set +m; "$@"'
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "shell",
            sys.executable,
            "-c",
            _PIPELINE_CODE,
            str(tmp_path / "pipeline.db"),
        ],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["rows"] == (0 if pipeline else 1), outcome
    if pipeline:
        assert "shared foreground group" in outcome["error"]
        assert "without a pipeline, or under setsid" in outcome["error"]
        assert "peer " in outcome["error"] and " cat" in outcome["error"]


@pytest.mark.parametrize("version", [53, 54])
def test_round13_v53_upgrade_adds_identity(tmp_path, version):
    fields = {
        "owner_ppid",
        "owner_command",
        "container_runtime",
        "cleanup_runtime",
    }
    path = str(tmp_path / "v53.db")
    with SqliteRunStore(path) as store:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        removed = fields | {"container_id", "container_cidfile"}
        if version == 53:
            removed.add("termination_members")
        for field in removed & columns:
            store._conn.execute(f"ALTER TABLE runs DROP COLUMN {field}")
        store._conn.execute("DROP TABLE stop_label_outbox")
        store._conn.execute("UPDATE schema_version SET version = ?", (version,))
        store._conn.commit()
    with SqliteRunStore(path) as store:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(runs)")}
        assert fields <= columns, "v54 must install the complete signal identity"
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


def test_round13_admission_rechecks_owner_identity(
    tmp_path, monkeypatch, process_table
):
    from kew import stop_recovery

    path = str(tmp_path / "guard.db")
    checked = []
    with SqliteRunStore(path) as store, SqliteRunStore(path) as other:
        row = _row()
        store.save(row)

        def replace_owner(observed, now, **kwargs):
            checked.append(observed.run_id)
            fresh = other.get(observed.run_id)
            fresh.owner_start_time = "Wed Sep 16 10:00:00 2026"
            other.save(fresh, update_ownership=True)
            return True

        monkeypatch.setattr(
            stop_recovery, "pending_owner_is_dead", replace_owner, raising=False
        )
        assert not store.try_admit_run(_row("new", 43), 1)
        assert checked == ["old"], "admission must reconcile before counting"
        assert store.get("old").status == "pending", (
            "changed ownership must defeat finalization"
        )
        assert not store.list_audit_events(event="orphaned_run_reaped")


def test_round13_contained_admission_reconciles(monkeypatch, process_table):
    from kew import stop_recovery

    with SqliteRunStore(":memory:") as store:
        row = _row()
        row.container_name, row.container_id = "kew-42", CID
        row.container_runtime = "docker"
        row.started_at -= timedelta(seconds=601)
        inspected = []

        def probe(runtime, record):
            inspected.append((runtime, record.container_id))
            return "absent"

        monkeypatch.setattr(stop_recovery, "record_container_state", probe)
        store.save(row)
        assert store.try_admit_run(_row("new", 43), 1)
        assert inspected == [("docker", CID)]
        assert store.get("old").status == "killed"


def test_round13_client_survival_retries(monkeypatch, process_table):
    with SqliteRunStore(":memory:") as store:
        _contained(store)
        state, calls = _runtime(monkeypatch, process_table, store)
        state["inspects"] = 1
        process_table.signal_works = False
        _tick(store)
        assert getattr(store.get("old"), "cleanup_runtime", None) == "docker"
        assert store.get("old").status == "pending"
        assert not state["running"] and CLIENT in process_table.members
        process_table.signal_works = True
        _tick(store)
        assert store.get("old").status == "killed"
        _assert_cleanup_calls(calls, process_table)


@pytest.mark.parametrize("origin", ["host", "dispatch", "review"])
def test_round13_admission_persists_owner_snapshot(
    tmp_path, monkeypatch, process_table, origin
):
    from types import SimpleNamespace

    from test_dispatch_core import _profile_run
    from test_liveness_sandbox_registration import _admit as sandbox_admit

    from kew.dispatch_core import _save_pending_run

    process_table.members[OWNER] = (OWNER, "S", "python3", "python3 -m kew dispatch")
    process_table.parents[OWNER] = 44
    monkeypatch.setattr(process_groups, "exclusive_owner_pid", lambda: OWNER)
    with SqliteRunStore(":memory:") as store:
        if origin == "host":
            run = _profile_run(store, tmp_path, runner=SimpleNamespace(name="fake"))
            _save_pending_run(
                run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
            )
            run_id = run.run_id
        else:
            run_id = sandbox_admit(
                store, DispatchConfig(), tmp_path / "owner.log", origin
            )
        row = store.get(run_id)
        assert (
            row.owner_start_time,
            getattr(row, "owner_ppid", None),
            getattr(row, "owner_command", None),
        ) == (BIRTH, 44, "python3")
        row.owner_ppid, row.owner_command = 999, "stale"
        store.save(row)
        saved = store.get(run_id)
        assert (saved.owner_ppid, saved.owner_command) == (44, "python3")
