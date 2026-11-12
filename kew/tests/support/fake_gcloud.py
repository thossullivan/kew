"""A fake ``gcloud`` on PATH, shared by the host-provider and doctor tests.

No test touches a real cloud. The shim answers from a JSON state file the test
writes and appends every argv it is handed to the same file, so a test can
assert both the answers and the calls. ``job_stderr`` and ``job_sleep`` let a
test make the job write to stderr or outlive its ceiling, which is how the
marker-forgery and timeout rules are asserted. ``job_responses`` answers a
sharded verify's several jobs differently, matched on a substring of each
job's command, which is how one failing and one silent shard are asserted.
A ``job_responses`` entry may pair ``output`` with ``swallow`` to die mid-job
the way a pre-empted spot instance does: the lines it had written arrive, the
marker does not. An entry marked ``once`` answers a single job and is then
consumed, so a later identical command is answered by the entry behind it —
which is how a probe whose answer the job between them changed is written.
An entry's ``output_if`` is a list of ``[substring, output]`` pairs: the first
whose substring is also in the command answers with that output instead, so
one entry can answer what a ``--format`` asked for. The output may instead be a
dict of the entry's own fields (``output``, ``exit``, ``stderr``), for a format
the runtime fails on.
``orphan`` makes the shim fork a child that holds the
session's stdout and outlives it, which is what the real ``ssh`` under
``gcloud compute ssh`` does to a ceiling that ends only the wrapper's pid; the
child's pid is recorded as ``orphan_pid`` so a test can assert the whole process
group went and not only the pid kew held.
``scp_sleep`` holds every copy open that long, which is how a signal is landed
on the ship phase rather than on the job.
``job_started`` is ``{"marker": <path>, "pid": <pid>}``: a job opens the
marker for writing as it starts and, when ``pid`` is given, sends ``SIGTERM``
to it, a signal directed at the process the way a supervisor's is, landed on
the job without the test polling for it. Only the first job to start does. A
marker the test made a FIFO holds the job until the test reads it, so the
test learns the job started from that read returning. ``job_gate`` names a
file: every job takes an exclusive ``flock`` on it before it finishes, so a
test that holds that lock holds every job until it lets go, and the order of
the run's exit and the jobs' ends is the test's to decide, not a sleep's. A
job still at the gate after 60 seconds passes it and counts
``gate_hang_guard``: a hang guard for a run that never unwound, never an
answer. ``job_blocked_signals`` records, per job, the signals it was started
with blocked, which is the mask of the kew thread that started it. Every job
that runs to its end counts itself in ``jobs_finished``.
``takeover_marker`` is the instance's record of its last lease takeover
(``[id, holder, taken_by]``): a lease takeover writes it and every hold prints
it back, and ``first_copy_at`` is when the first copy landed, so a test can
put the audit row a takeover settles before anything shipped.
``takeover_exit`` ends a takeover that wrote the lease with that exit instead
of 0 — a backstop that would not arm, or an ssh that dropped — and
``takeover_unmarked`` has it fail before the marker was written;
``marker_unreadable`` fails a bare read of the marker the way ssh does, so
nothing it would have fenced is fenced. ``takeover_dropped`` answers the first
takeover 255 before the instance runs any of it, the way an ssh that timed out
on a session still to land does; replaying that call later is the late session.
``takeover_refused`` refuses even a lapsed holder's takeover, the way a job
that survives ``SIGKILL`` keeps its lease. ``lease_changed_at_takeover`` has
the lapsed holder's lease expire between the hold that read it and the
takeover: the takeover answers exit 6 and writes nothing, and the lease is
free for the next hold. ``lease_expired`` makes the first hold report an
expired foreign holder without changing it. The next hold takes it through
the same marked takeover path.
``fenced`` is the instance's :data:`~kew.hosts.gcp.TAKEOVER_FENCE_FILE`: a
lease command that appends ids to it adds them here, and a takeover whose id
is here answers exit 7 and writes nothing. ``takeover_delay`` names a
directory: the first takeover's session writes ``started`` there, then waits
outside the state lock until ``go`` appears, and writes ``done`` once it has
answered — a ``gcloud`` session that outlives the operator killed under it.
``nologin_until_start`` and ``cancel_failures`` make the disarming ssh fail the
two ways a real one does — pam's pre-shutdown refusal, which only a restart
lifts, and a cold start's connection refusal, which the next attempt does.

``execute_in`` goes one step further and *runs* the job command the provider
built, in a non-interactive, non-login ``/bin/sh`` whose environment is a HOME
and nothing else — no PATH — which is what ``gcloud compute ssh --command``
hands a real host. That is the only way to prove a job can find a tool it was
not given a PATH for (#2433); the marker on the output is then the job's own,
not the shim's.

The shim has to be launched, so it needs an executable bit that works. Some
sandboxes mount the pytest temporary filesystem ``noexec``; when that is the
case the shim goes under ``HOME`` instead and is removed on teardown, so the
test asserts the same thing either way rather than failing for a reason that
has nothing to do with kew.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from kew import subprocess_utils

#: What pam prints when ``shutdown`` has written ``/run/nologin``. Quoted
#: from the refusal that failed ``kew verify --host gcp`` on 2026-10-06, so
#: the provider is matched against the real wording and not a paraphrase.
NOLOGIN_REFUSAL = (
    "System is going down. Unprivileged users are not permitted to log in "
    "anymore. Please come back later.\n"
)

FAKE_GCLOUD = '''#!/usr/bin/env python3
"""A fake gcloud: answers from a JSON state file and records every call."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time

NOLOGIN_REFUSAL = @NOLOGIN_REFUSAL@
argv = sys.argv[1:]

# A delayed takeover waits OUTSIDE the state lock, so every other command the
# test runs meanwhile is answered; only the first takeover is delayed.
late = None
marking = "> /run/kew-host.lease.takeover"
if "--command" in argv and marking in argv[argv.index("--command") + 1]:
    with open(os.environ["KEW_FAKE_GCLOUD_STATE"], encoding="utf-8") as peek:
        delay = json.load(peek).get("takeover_delay")
    if delay:
        try:
            os.close(os.open(os.path.join(delay, "started"), os.O_CREAT | os.O_EXCL))
            late = delay
        except FileExistsError:
            pass
    if late:
        deadline = time.time() + 60
        go = os.path.join(late, "go")
        while not os.path.exists(go) and time.time() < deadline:
            time.sleep(0.05)

# One exclusive lock per invocation, held until this process exits. A sharded
# verify runs several jobs at once, so several shims read-modify-write this one
# state file concurrently; without the lock the last writer wins and the calls
# the others recorded are simply gone, which reads in a test as a job that
# never ran.
state_file = open(os.environ["KEW_FAKE_GCLOUD_STATE"], "r+", encoding="utf-8")
fcntl.flock(state_file, fcntl.LOCK_EX)
state = json.load(state_file)
state.setdefault("calls", []).append(argv)


def save():
    state_file.seek(0)
    json.dump(state, state_file)
    state_file.truncate()
    state_file.flush()


def finish(code, out="", err=""):
    save()
    if late:
        state["late_exit"] = code
        save()
        open(os.path.join(late, "done"), "w").close()
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


if argv[:2] == ["auth", "print-access-token"]:
    if state.get("auth_exit", 0) != 0:
        finish(state["auth_exit"], err=state.get("auth_stderr", ""))
    finish(0, out="ya29.fake-token\\n")

if argv[:3] == ["compute", "instances", "describe"]:
    statuses = state.get("statuses") or ["RUNNING"]
    status = statuses[0] if len(statuses) == 1 else statuses.pop(0)
    if state.get("describe_exit", 0) != 0:
        finish(state["describe_exit"], err=state.get("describe_stderr", ""))
    finish(0, out=status + "\\n")

if argv[:3] == ["compute", "instances", "start"]:
    state["statuses"] = ["RUNNING"]
    state["started"] = state.get("started", 0) + 1
    finish(0)

if argv[:3] == ["compute", "instances", "resume"]:
    state["statuses"] = ["RUNNING"]
    state["resumed"] = state.get("resumed", 0) + 1
    finish(0)

if argv[:2] == ["compute", "scp"]:
    remaining = state.get("scp_failures", 0)
    if remaining > 0:
        state["scp_failures"] = remaining - 1
        finish(1, err="connection reset\\n")
    if state.get("scp_sleep"):
        # A copy that is slow to finish, so a test can land a signal on the
        # ship phase rather than on the job (#2811).
        save()
        time.sleep(state["scp_sleep"])
    state["copies"] = state.get("copies", 0) + 1
    state.setdefault("first_copy_at", time.time())
    finish(0)

if argv[:2] == ["compute", "ssh"]:
    command = argv[argv.index("--command") + 1]
    if "kew-host.lease" in command and "KEW_HOST_EXIT" not in command:
        # The instance's lease (#2627); a job names its lock too (#2980).
        # `lease_held` is another kew process's live lease: taking it and
        # releasing over it are both refused with the lease's own exit 3.
        # `release_exit` fails the release itself. `lease_holder` names that
        # process; a hold command that names it as lapsed (#2811) stops its
        # job and takes the lease, which is recorded as `taken_over`. A hold
        # writes `$((now + N))`; every other lease command that removes the
        # lease is the one `release_exit` fails.
        state["leases"] = state.get("leases", 0) + 1
        # `hold_unreachable` drops every hold's login before the instance
        # runs a line of it, the way ssh says so: exit 255, nothing on stdout.
        if state.get("hold_unreachable") and "last takeover" in command:
            finish(255, err="ssh: connect to host port 22: refused\\n")
        # `takeover_marker` is the instance's record of its last takeover
        # (#2874): every hold reads it back first, and a takeover writes it.
        last = ""
        if state.get("takeover_marker") and "last takeover" in command:
            last = "last takeover %s of %s by %s\\n" % tuple(state["takeover_marker"])
        if "last takeover" in command and "$tok" not in command:
            # A bare read of the marker: no lease is taken or checked.
            if state.get("marker_unreadable"):
                finish(255, err="ssh: connect to host port 22: refused\\n")
        fence = re.search(
            r"printf \\S*%s\\\\n\\S* ([^>]*) >> /run/kew-host\\.lease\\.fenced", command
        )
        if fence:
            state.setdefault("fenced", []).extend(fence.group(1).split())
        if "last takeover" in command and "$tok" not in command:
            finish(0, out=last)
        if state.get("lease_held"):
            holder = state.get("lease_holder", "other-operator-1-abc")
            takeover = (
                '[ "$tok" != %s ]' % holder in command
                and "systemctl kill" in command
            )
            mark = re.search(
                r"([0-9a-f]{32}) (\\S+) (\\S+) > /run/kew-host\\.lease\\.takeover",
                command,
            )
            if takeover and state.pop("takeover_dropped", False):
                finish(255, err="ssh: connection timed out\\n")
            fenced = mark and mark.group(1) in state.get("fenced", [])
            if takeover and fenced and "/run/kew-host.lease.fenced" in command:
                finish(7, out=last + "kew: takeover was settled as not confirmed\\n")
            if takeover and state.pop("lease_changed_at_takeover", False):
                state["lease_held"] = False
                finish(6, out=last + "the lease changed since it was read\\n")
            if takeover and not state.get("takeover_refused"):
                state["lease_held"] = False
                state["taken_over"] = state.get("taken_over", 0) + 1
                if mark and not state.get("takeover_unmarked"):
                    state["takeover_marker"] = list(mark.groups())
                if state.get("takeover_exit"):
                    finish(state["takeover_exit"], err="systemd-run: failed\\n")
                out = "stopped the job of lapsed holder %s\\n" % holder
                finish(0, out=last + out)
            if state.get("lease_expired"):
                finish(3, out=last + "expired lease held by %s\\n" % holder)
            finish(3, out=last + "held by %s for 600s more\\n" % holder)
        drops = "rm -f" in command and "$((now + " not in command
        if drops and state.get("release_exit"):
            finish(state["release_exit"], err="systemd-run: bus unreachable\\n")
        finish(0, out=last)
    if "shutdown -c" in command:
        # The pre-shutdown nologin window (#2411): pam refuses the login
        # until the stop has fired and the instance has been started again,
        # so the refusal lifts on `started` rather than on a retry count.
        if state.get("nologin_until_start") and not state.get("started"):
            finish(255, err=NOLOGIN_REFUSAL)
        # A cold start: the instance reads RUNNING before sshd answers, so
        # the first `cancel_failures` contacts are refused by the connection.
        refusals = state.get("cancel_failures", 0)
        if refusals > 0:
            state["cancel_failures"] = refusals - 1
            finish(255, err="ssh: connect to host port 22: refused\\n")
        finish(state.get("cancel_exit", 0), err=state.get("cancel_stderr", ""))
    if "KEW_HOST_EXIT" in command and state.get("execute_in"):
        # The job really runs, the way a host runs it: a non-login `sh` with
        # a HOME and no PATH at all, in the login's own home directory. An
        # answer from the state file could never show whether the command the
        # provider built can reach a tool (#2433).
        save()
        ran = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=state["execute_in"],
            env={"HOME": state["execute_in"]},
            capture_output=True,
            text=True,
        )
        # ssh hands back the remote shell's own status, as here.
        finish(ran.returncode, out=ran.stdout, err=ran.stderr)

    if "KEW_HOST_EXIT" in command:
        # A sharded verify runs several jobs, on several instances, and they
        # must be able to answer differently: one shard fails, one goes
        # silent, one reports a count that does not add up. `job_responses` is
        # a list of {"match": <substring of the job command>, ...}; the first
        # whose substring appears in this job's command answers it, and
        # anything it leaves out falls back to the single-job state below.
        answer = {}
        for response in state.get("job_responses", []):
            if response.get("match", "") in command:
                answer = response
                break
        if answer.get("once"):
            # A `"once": true` answer is consumed, so the NEXT identical job
            # falls through to the entry behind it. That is how an instance
            # whose state the job before it changed answers: `image inspect`
            # fails, `kew sandbox build` runs, and the same inspect then
            # succeeds — one command, two answers, in that order.
            state["job_responses"].remove(answer)
        for needle, override in answer.get("output_if", []):
            if needle in command:
                # An override is the output, or a dict of the answer's own
                # fields (output, exit, stderr) for what that command gets.
                if not isinstance(override, dict):
                    override = {"output": override}
                answer = dict(answer, **override)
                break
        if answer.get("swallow", state.get("swallow_marker")):
            # A session that dies mid-job: whatever the job had already
            # written is on stdout, and the marker that would have carried
            # its outcome never arrives. That is a spot pre-emption, and it
            # is why `output` is honoured here as well as below — and why the
            # answer's own stderr goes out with ssh's, because a remote step
            # that wrote to stderr before the session died still wrote it, and
            # that tail is what the interruption explains itself with.
            finish(
                255,
                out=answer.get("output", state.get("job_output", "")),
                err=answer.get("stderr", "") + "ssh: connect failed\\n",
            )
        holder = answer.get("orphan")
        if holder:
            # `gcloud compute ssh` is a wrapper, and the `ssh` it spawns
            # inherits the session's stdout. Ending only the wrapper's pid
            # leaves that child holding the write end of the pipe, so the
            # relay's read never reaches EOF. Same process group, so a kill
            # aimed at the GROUP still gets it; the state file's lock is the
            # parent's alone, so a later job is not blocked behind this.
            child = os.fork()
            if child == 0:
                state_file.close()
                time.sleep(holder)
                os._exit(0)
            # Recorded so a test can assert the child is GONE: a ceiling that
            # ends only the wrapper's pid leaves it alive, still holding the
            # pipe and, on a real host, still running the job.
            state["orphan_pid"] = child
            save()
            time.sleep(holder)
            finish(0)
        # The signals this job was started with blocked: a child inherits the
        # mask of the thread that started it.
        state.setdefault("job_blocked_signals", []).append(
            sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, []))
        )
        started = state.pop("job_started", None)
        if started:
            # Once: the first job to start signals, and no later one does. A
            # FIFO marker holds this open until the test reads it.
            save()
            open(started["marker"], "a").close()
            if started.get("pid"):
                os.kill(started["pid"], signal.SIGTERM)
        delay = answer.get("sleep", state.get("job_sleep"))
        if delay:
            save()
            time.sleep(delay)
        if state.get("job_gate"):
            # Every job waits on the test's lock before it may finish.
            save()

            class HangGuard(Exception):
                pass

            def hang_guard(_signum, _frame):
                raise HangGuard

            signal.signal(signal.SIGALRM, hang_guard)
            signal.alarm(60)
            try:
                with open(state["job_gate"], "a") as gate:
                    fcntl.flock(gate, fcntl.LOCK_EX)
            except HangGuard:
                state["gate_hang_guard"] = state.get("gate_hang_guard", 0) + 1
            finally:
                signal.alarm(0)
        state["jobs_finished"] = state.get("jobs_finished", 0) + 1
        out = answer.get("output", state.get("job_output", ""))
        out += "\\nKEW_HOST_EXIT=%s\\n" % answer.get("exit", state.get("job_exit", 0))
        finish(0, out=out, err=answer.get("stderr", state.get("job_stderr", "")))
    finish(0)

finish(2, err="fake gcloud: unhandled %r\\n" % (argv,))
'''


def launchable(directory: Path) -> bool:
    """Whether a chmod +x file under *directory* can actually be launched.

    Public because the provisioning tests need the same answer: they put stub
    tools on PATH, and a sandbox that mounts the pytest temporary filesystem
    ``noexec`` would fail them for a reason that has nothing to do with kew.
    """
    probe = directory / ".exec-probe"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    probe.chmod(0o755)
    launchable = os.access(probe, os.X_OK)
    probe.unlink()
    return launchable


@contextmanager
def fake_gcloud_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Put the shim first on PATH and yield its JSON state file."""
    fallback: str | None = None
    if launchable(tmp_path):
        bin_dir = tmp_path / "fakebin"
    else:
        fallback = tempfile.mkdtemp(prefix="kew-fake-gcloud-", dir=Path.home())
        bin_dir = Path(fallback) / "fakebin"
    bin_dir.mkdir()
    shim = bin_dir / "gcloud"
    shim.write_text(
        FAKE_GCLOUD.replace("@NOLOGIN_REFUSAL@", repr(NOLOGIN_REFUSAL)),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    state = tmp_path / "gcloud-state.json"
    state.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("KEW_FAKE_GCLOUD_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delitem(subprocess_utils.TOOL_PATHS, "gcloud", raising=False)
    try:
        yield state
    finally:
        subprocess_utils.TOOL_PATHS.pop("gcloud", None)
        if fallback is not None:
            shutil.rmtree(fallback, ignore_errors=True)


def write_state(state: Path, **values: object) -> None:
    """Merge *values* into the shim's state file."""
    record = json.loads(state.read_text(encoding="utf-8"))
    record.update(values)
    state.write_text(json.dumps(record), encoding="utf-8")


def read_state(state: Path) -> dict[str, object]:
    """The shim's state file, including its recorded ``calls``."""
    record = json.loads(state.read_text(encoding="utf-8"))
    assert isinstance(record, dict)
    return record


def recorded_calls(state: Path) -> list[list[str]]:
    """Every argv the shim was handed, in order."""
    recorded = read_state(state).get("calls", [])
    assert isinstance(recorded, list)
    return [list(call) for call in recorded]
