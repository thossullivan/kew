"""Round-18 operator stops authenticate durable identities on both surfaces."""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_dispatch_core import _profile_run
from test_liveness_owner import BIRTH
from typer.testing import CliRunner

from kew import cli, dispatch_core, process_groups
from kew.config import DispatchConfig
from kew.run_store import RunRecord, SqliteRunStore
from kew.service import fleet as service
from kew.tracking import DispatchResult
from kew.tui import actions

_KILL = os.kill
_KILLPG = os.killpg

# The owner only has to release its spawn gate and then reap the attempt it
# owns. It writes nothing about the attempt's death: a stop proves that from
# the kernel at the reap, so any marker the owner writes afterwards can arrive
# after the stop has already signalled the owner (#2402).
_OWNER = """
import shutil, subprocess, sys, time
from pathlib import Path
from kew.subprocess_utils import TOOL_PATHS, popen, release_spawn
TOOL_PATHS['sleep'] = shutil.which('sleep')
child = popen(['sleep', '60'], stdin=subprocess.DEVNULL,
              start_new_session=True, gate=True)
print(child.pid, flush=True)
sys.stdin.readline()
release_spawn(child, registered=True)
Path(sys.argv[1]).write_text('released')
child.wait()
time.sleep(60)
"""

_MARKER_TIMEOUT = 30.0


def _group_is_dead(pgid):
    """Witness a process group's death where a stop witnesses it: the kernel.

    This is the first proof ``process_groups.group_alive`` demands, so no
    test witness built on it can lag the stop it observes (#2402).

    ``PermissionError`` is the same answer as ``ProcessLookupError`` here. Every
    group this module probes is one it or its own owner child spawned under this
    uid, so a refused zero signal cannot mean another user's process: it means
    the group's members are all gone and not yet reaped, which macOS answers
    EPERM and Linux answers ESRCH. Reaped or not, nothing in that group can run
    again — the only fact a stop reads off this probe — so the witness must not
    depend on the platform's errno for a zombie (#2436). ``group_alive`` reaches
    the same answer, resolving the refusal through its zombie-excluding member
    scan, so this witness neither leads nor lags the stop it observes.
    """
    try:
        _KILLPG(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _wait_for(proven, failure, timeout=_MARKER_TIMEOUT, abandoned=None):
    """Poll a fact against a deadline instead of betting on a fixed sleep.

    The processes under test reach each fact on the kernel's schedule, so a
    loaded or freshly booted host can take seconds to get there (#2402).
    ``abandoned`` reports that nothing can produce the fact any more, which
    ends the wait at once rather than sitting out the deadline.
    """
    deadline = time.monotonic() + timeout
    while not proven() and time.monotonic() < deadline:
        if abandoned is not None and abandoned():
            break
        time.sleep(0.01)
    assert proven(), failure()


def _wait_for_stop_proof(pgid, timeout=_MARKER_TIMEOUT):
    """Hold until the stop's own proof of a group's death would succeed.

    A stop reconsiders the owner only once every attempt group reads
    positively dead (``_stop_owner_after_attempts``), and it gives that proof
    a bounded wait of ``fleet._GRACE_SECONDS``. The proof arrives when the
    owner reaps the attempt it owns, which is work on another process's
    schedule: on a loaded host the reap can land after the bound, the stop
    then never reaches the owner, and a test asserting owner signals fails on
    the host's load rather than on the stop (#2436).

    So the test waits for that same proof under its own, generous deadline
    before letting the stop probe for it. This asserts nothing the stop does
    not already demand — ``process_groups.group_alive`` is the stop's own
    witness, called here exactly as the stop calls it — it only stops the
    production bound from deciding the test's outcome.
    """
    _wait_for(
        lambda: process_groups.group_alive(pgid) is False,
        lambda: (
            f"group {pgid} was still not provably dead after {timeout}s, so no "
            "stop could have proven the attempt dead and gone on to the owner"
        ),
        timeout,
    )


def _wait_for_marker(marker, owner, timeout=_MARKER_TIMEOUT):
    """Wait for the owner to write one of its own markers (#2402)."""
    _wait_for(
        marker.exists,
        lambda: (
            f"the attempt owner wrote no {marker.name} marker within {timeout}s "
            f"(owner exit status {owner.poll()})"
        ),
        timeout,
        lambda: owner.poll() is not None,
    )


class _ProcessFamily:
    """Real processes and kernel tokens; controlled snapshots replace sandbox ps."""

    def __init__(self, root):
        self.released = root / "gate-released"
        self.owner = subprocess.Popen(
            [sys.executable, "-c", _OWNER, str(self.released)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.attempt = int(self.owner.stdout.readline())
        self._attempt_dead = False

    def attempt_is_dead(self):
        """Witness the attempt's death from the kernel, exactly as a stop does.

        A marker written by the owner cannot serve as this witness: the owner
        writes one only after reaping, while the stop proves death from the
        kernel at the reap itself, so a descheduled owner leaves the stop
        looking unproven (#2402). The answer latches: a reaped group never
        comes back, and a recycled PID must not make it look alive again.
        """
        self._attempt_dead = self._attempt_dead or _group_is_dead(self.attempt)
        return self._attempt_dead

    def snapshot(self):
        entries = {}
        if self.owner.poll() is None:
            entries[self.owner.pid] = process_groups.ProcessIdentity(
                os.getpid(), self.owner.pid, "S", BIRTH, "python3"
            )
        if not self.attempt_is_dead():
            entries[self.attempt] = process_groups.ProcessIdentity(
                self.owner.pid, self.attempt, "S", BIRTH, "sleep"
            )
        return entries

    def members(self, pgid):
        return [
            process_groups.GroupMember(pid, entry.command)
            for pid, entry in self.snapshot().items()
            if entry.group == pgid
        ]

    def parents(self):
        return {
            pid: (entry.parent, entry.group, entry.state)
            for pid, entry in self.snapshot().items()
        }

    def install(self, monkeypatch):
        monkeypatch.setattr(process_groups, "_identity_snapshot", self.snapshot)
        monkeypatch.setattr(process_groups, "group_members", self.members)
        monkeypatch.setattr(process_groups, "_parent_table", self.parents)
        monkeypatch.setattr(
            process_groups, "process_start_time", Mock(return_value=BIRTH)
        )
        monkeypatch.setattr(
            "kew.run_store.process_start_time", Mock(return_value=BIRTH)
        )

    def record(self, store):
        row = RunRecord(
            "round18",
            42,
            "agent/42",
            "pending",
            -1,
            datetime.now(UTC),
            pgid=self.owner.pid,
            owner_pid=self.owner.pid,
            owner_ppid=os.getpid(),
            owner_command="python3",
            owner_start_time=BIRTH,
            owner_start_token=process_groups.process_start_token(self.owner.pid),
        )
        assert row.owner_start_token, "the owner needs a real kernel token"
        store.save(row)
        store.set_attempt_pgid(row.run_id, self.attempt)
        row = store.get(row.run_id)
        assert row.attempt_owner_verified
        assert row.attempt_start_token == process_groups.process_start_token(
            self.attempt
        )
        self.owner.stdin.write("go\n")
        self.owner.stdin.flush()
        # Signalling the attempt before its gate is released breaks the owner's
        # permit pipe, and a dead owner never reaps anything (#2402).
        _wait_for_marker(self.released, self.owner)
        return row

    def wait_attempt_dead(self, timeout=_MARKER_TIMEOUT):
        _wait_for(
            self.attempt_is_dead,
            lambda: f"the real attempt outlived its stop by {timeout}s",
            timeout,
        )

    def close(self):
        for pid in (self.attempt, self.owner.pid):
            # A group that is already gone refuses the signal, and which errno
            # it refuses with is the platform's business (#2436) — teardown has
            # nothing left to do either way.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                _KILLPG(pid, signal.SIGKILL)
        self.owner.wait(timeout=5)
        self.owner.stdin.close()
        self.owner.stdout.close()


@pytest.fixture
def family(tmp_path, monkeypatch):
    children = _ProcessFamily(tmp_path)
    try:
        children.install(monkeypatch)
        yield children
    finally:
        children.close()


def _app(row, store, snapshot_pid):
    return SimpleNamespace(
        screen=Mock(),
        state=SimpleNamespace(
            pending_runs=[row], worker_snapshots={row.run_id: {"pid": snapshot_pid}}
        ),
        _store=store,
        config=DispatchConfig(),
        notify=Mock(),
        push_screen=Mock(),
        run_worker=Mock(side_effect=asyncio.run),
    )


def _confirm(app, monkeypatch):
    monkeypatch.setattr(actions, "get_selected_run", Mock(return_value="round18"))
    actions.handle_kill(app)
    app.push_screen.call_args.kwargs["callback"](True)


def _observe_signal(events, store, family, original, pid, sig):
    if sig:
        events.append(
            (
                pid,
                sig,
                store.get("round18").error_category,
                family.attempt_is_dead(),
                original is _KILLPG,
            )
        )
    try:
        return original(pid, sig)
    finally:
        if sig and pid == family.attempt:
            # The stop that just signalled the attempt is about to probe it for
            # proven death, under a bound a loaded host can outlast. Hold the
            # signal call open until that proof is in (#2436); a zero probe must
            # not wait, since the probe itself arrives through here. The wait
            # runs even when the signal is refused — a group that has already
            # exited refuses it, which is exactly when the proof matters.
            _wait_for_stop_proof(family.attempt)


class _LateOwner:
    """A live owner that only gets around to its marker after some polls."""

    def __init__(self, marker, polls):
        self.marker = marker
        self.polls = polls
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.calls >= self.polls:
            self.marker.write_text("released")
        return None


def test_round18_marker_wait_outlasts_a_late_owner(tmp_path):
    marker = tmp_path / "gate-released"
    owner = _LateOwner(marker, polls=5)

    _wait_for_marker(marker, owner, timeout=5)

    assert owner.calls >= 5, "the wait must poll, not sleep once and give up"


def test_round18_marker_wait_fails_when_a_live_owner_writes_nothing(tmp_path):
    owner = Mock(poll=Mock(return_value=None))

    with pytest.raises(AssertionError, match="no gate-released marker"):
        _wait_for_marker(tmp_path / "gate-released", owner, timeout=0.2)


def test_round18_marker_wait_abandons_a_dead_owner_at_once(tmp_path):
    """A dead owner can write nothing, so the wait must not sit out 30s.

    Only the elapsed time distinguishes the early break from the live-owner
    case: both raise the same assertion, so a dropped break would stay green
    while costing every caller the whole deadline.
    """
    owner = Mock(poll=Mock(return_value=1))
    start = time.monotonic()

    with pytest.raises(AssertionError, match="owner exit status 1"):
        _wait_for_marker(tmp_path / "gate-released", owner, timeout=_MARKER_TIMEOUT)

    assert time.monotonic() - start < 1, (
        f"a dead owner must end the wait at once, not after {_MARKER_TIMEOUT}s"
    )


def test_round18_stop_proof_wait_outlasts_a_late_reap(monkeypatch):
    """#2436: the reap that proves attempt death can land after the stop's bound.

    ``group_alive`` answers ``None`` — unknown — for a group whose members are
    all gone but not yet reaped, which is what macOS reports until the owner
    gets the CPU. Unknown is not the proof a stop needs before it signals the
    owner, so the wait must poll through it rather than accept it.
    """
    answers = [None, None, None, False]
    probe = Mock(side_effect=lambda pgid: answers.pop(0) if answers else False)
    monkeypatch.setattr(process_groups, "group_alive", probe)

    _wait_for_stop_proof(424242, timeout=5)

    assert probe.call_count > 3, "an unknown answer is not proof of death"


def test_round18_stop_proof_wait_reports_a_group_that_never_proves_dead():
    """A never-proven group must name the missing proof, not an empty list.

    The macOS failure this replaces surfaced as ``assert ([])`` on the owner
    signals, which says nothing about why the stop stopped short (#2436).
    """
    with pytest.raises(AssertionError, match="not provably dead"):
        _wait_for_stop_proof(os.getpgid(0), timeout=0.2)


@pytest.mark.serial
def test_round18_attempt_death_witness_needs_no_owner_bookkeeping(monkeypatch, family):
    """The attempt is dead the moment the kernel says so, not a poll later.

    A stop proves attempt death from the kernel at the reap and then signals
    the owner, so a witness waiting on anything the owner writes afterwards
    reports an already-proven stop as unproven whenever the owner loses the
    CPU in between (#2402). A group this test reaped itself pins that: no
    owner ever knew about it, and the kernel still calls it dead.
    """
    reaped = subprocess.Popen(["sleep", "60"], start_new_session=True)
    _KILLPG(reaped.pid, signal.SIGKILL)
    reaped.wait(timeout=5)

    assert _group_is_dead(reaped.pid), "a reaped group is dead with no owner help"
    assert not _group_is_dead(family.attempt), "the live attempt is not dead yet"
    assert process_groups.group_alive(reaped.pid) is False, (
        "the stop under test reads that same kernel proof"
    )
    monkeypatch.setattr(family, "attempt_is_dead", lambda: True)
    assert family.attempt not in family.snapshot(), (
        "the snapshot must follow the kernel witness, not owner bookkeeping"
    )


def test_round18_a_zombie_group_is_dead_whatever_errno_the_kernel_picks(
    monkeypatch,
):
    """#2436: the witness must not read a zombie group as still alive.

    A group whose members are all gone but not yet reaped is refused a zero
    signal with EPERM on macOS and ESRCH on Linux. Catching only the Linux
    errno, this witness raised ``PermissionError`` out of the stop test's wait
    on the operator's Mac while passing on the verify VM — a platform's errno
    for a zombie is not a fact about the stop under test.
    """
    refused = Mock(side_effect=PermissionError(1, "Operation not permitted"))
    monkeypatch.setattr(sys.modules[__name__], "_KILLPG", refused)

    assert _group_is_dead(424242) is True
    refused.assert_called_once_with(424242, 0)


@pytest.mark.serial
def test_round18_stale_snapshot_pid_never_signalled(tmp_path, monkeypatch, family):
    sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        with SqliteRunStore(
            str(tmp_path / "stale.db"), check_same_thread=False
        ) as store:
            row = family.record(store)
            _KILLPG(family.attempt, signal.SIGTERM)
            family.wait_attempt_dead()
            # The durable attempt PID has been reused, but its token has not.
            row.attempt_pgid = sleeper.pid
            store.save(row, update_ownership=True)
            app = _app(row, store, sleeper.pid)
            events = []
            monkeypatch.setattr(
                os, "kill", partial(_observe_signal, events, store, family, _KILL)
            )
            monkeypatch.setattr(
                os, "killpg", partial(_observe_signal, events, store, family, _KILLPG)
            )
            # Preserve the live unrelated group's uncertainty despite unavailable ps.
            monkeypatch.setattr(
                process_groups, "group_members", Mock(return_value=None)
            )

            _confirm(app, monkeypatch)

            assert store.get(row.run_id).error_category == "killed", (
                f"TUI must persist stop intent: {app.notify.call_args}"
            )
            assert events == [], "a reused snapshot PID must receive no signal"
            assert sleeper.poll() is None, "the unrelated sleeper must survive"
            assert "identity unverifiable" in app.notify.call_args.args[0].lower()
    finally:
        with contextlib.suppress(ProcessLookupError):
            _KILLPG(sleeper.pid, signal.SIGKILL)
        sleeper.wait(timeout=5)


def _stop_during_backoff(app, monkeypatch, wait):
    assert wait > 0, "the operator stop must occur during retry backoff"
    _confirm(app, monkeypatch)


@pytest.mark.serial
def test_round18_authenticated_group_stop_prevents_retry(tmp_path, monkeypatch, family):
    with SqliteRunStore(str(tmp_path / "live.db"), check_same_thread=False) as store:
        row = family.record(store)
        app = _app(row, store, family.attempt)
        events = []
        monkeypatch.setattr(
            os, "kill", partial(_observe_signal, events, store, family, _KILL)
        )
        monkeypatch.setattr(
            os, "killpg", partial(_observe_signal, events, store, family, _KILLPG)
        )
        run = _profile_run(store, tmp_path)
        run.run_id = row.run_id
        run.config.retry.max_attempts = 3
        launched = Mock(
            return_value=(
                DispatchResult(
                    exit_code=1,
                    issue_num=42,
                    log_file=tmp_path / "run.log",
                    error_category="transient",
                ),
                False,
            )
        )
        monkeypatch.setattr(dispatch_core, "_run_retry_attempt", launched)
        monkeypatch.setattr(
            dispatch_core, "_check_existing_pr", Mock(return_value=(False, None))
        )
        monkeypatch.setattr(
            dispatch_core,
            "time",
            SimpleNamespace(
                sleep=partial(_stop_during_backoff, app, monkeypatch),
                monotonic=time.monotonic,
            ),
        )

        outcome = dispatch_core._run_retry_loop(
            run,
            prompt="probe",
            worktree_dir=tmp_path,
            budget_override_usd=None,
            timeout_override_minutes=None,
            should_pause=None,
            budget_check=None,
            anomaly_threshold_usd=None,
            anomaly_compaction_allowance_usd=0,
        )

        assert events and all(event[2] == "killed" for event in events), (
            "stop intent must precede every signal"
        )
        assert events[0][:2] == (family.attempt, signal.SIGTERM)
        assert events[0][4], "the attempt must receive a group signal"
        family.wait_attempt_dead()
        owner_signals = [event for event in events if event[0] == family.owner.pid]
        # Report the stop's own account of itself, not just the empty list: the
        # notification carries the note naming the branch the stop took, which
        # is the one thing a bare `assert ([])` never said (#2436).
        assert owner_signals and all(event[3] for event in owner_signals), (
            f"owner signals require proven attempt death: observed {events}, "
            f"stop reported {app.notify.call_args}"
        )
        family.owner.wait(timeout=5)
        assert launched.call_count == 1, (
            "stop intent must refuse the next retry after backoff"
        )
        assert outcome.early_return.error_category == "killed"
        assert "authenticated and signalled" in app.notify.call_args.args[0].lower()


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_round18_callers_share_stop_service(tmp_path, monkeypatch, surface):
    stop = Mock(
        return_value=SimpleNamespace(
            outcome="unverifiable", message="Identity unverifiable; nothing signalled"
        )
    )
    monkeypatch.setattr(service, "stop_run", stop, raising=False)
    with SqliteRunStore(str(tmp_path / "seam.db")) as store:
        row = RunRecord("round18", 42, "agent/42", "pending", -1, datetime.now(UTC))
        store.save(row)
        config = DispatchConfig()
        if surface == "tui":
            app = _app(row, store, 424242)
            monkeypatch.setattr(os, "kill", Mock())
            _confirm(app, monkeypatch)
        else:
            monkeypatch.setattr(cli, "load_config", Mock(return_value=config))
            monkeypatch.setattr(
                cli, "_open_run_store", Mock(return_value=contextlib.nullcontext(store))
            )
            result = CliRunner().invoke(cli.app, ["kill", row.run_id])
            assert "Identity unverifiable" in result.output, result.output
            assert result.exit_code == 2
        assert stop.call_count == 1, (
            "both operator surfaces must use the same service operation"
        )
        assert stop.call_args.args[:2] == (store, row.run_id)
