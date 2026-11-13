"""``gcp`` — the drive's remote-verify scripts, as a host provider.

What the operator scripts did (start the VM, ship a git bundle, run one job over
SSH, collect the log, schedule a stop) now lives here, driven through the
``gcloud`` CLI. ``gcloud`` is an optional external tool like ``docker`` and
``gh``: probed at use, reported by ``kew doctor``, never a Python dependency.

Three behaviours are carried over from the scripts because each one is a scar:

* A **stopped** instance is started, and ``ensure_up()`` does not return until
  the instance reads ``RUNNING``.
* An instance **mid-stop** is waited out before being started again: a stop
  fired mid-copy on 2026-10-04, and starting a ``STOPPING`` instance is
  refused by the API rather than queued. A **suspended** instance is *resumed*:
  ``instances start`` is refused for one, so the verb follows the status.
* A **pending stop** is cancelled. ``release()`` arms the host's own idle
  stop, so an instance picked up for a second job still has the previous
  job's stop armed. The idle stop rides a ``systemd`` timer rather than
  ``shutdown -h +N`` because ``shutdown`` writes ``/run/nologin`` five minutes
  ahead of the stop and pam then refuses the very login that would disarm it
  (#2411). A ``shutdown`` armed by an older kew, or by hand, is still
  cancelled, and if its nologin window has already closed logins the stop is
  waited out and the instance started again — the only way through a window
  no login can reach.
* **sshd is waited for.** An instance reads ``RUNNING`` before it accepts
  logins, so the first ssh after a cold start is refused by the connection
  rather than by the host, and a bounded retry tells the two apart.

The job's exit code comes back through an explicit marker rather than
``gcloud``'s own status, which cannot distinguish "the job failed" from "the
SSH connection failed". A missing marker is an error, never a clean zero, and
it is read off the host's **stdout** alone: the wrapper prints it there, so a
job writing ``KEW_HOST_EXIT=0`` to stderr cannot forge a passing result.

Every remote command, a job's included, is given a known PATH first
(:func:`kew.hosts.environment.remote_path_prefix`). ``--command`` is a
non-login shell: no profile runs, and kew forwards no ambient PATH to a host,
so a job calling ``uv`` found nothing and exited 127 in 1.6 seconds (#2433).

This provider can also *build* its instance: :meth:`GcpProvider.run_recipe`
copies the checked-in provisioning recipe up and runs it as root, which is how
``kew hosts provision`` makes a second host the same as the first
(:mod:`kew.hosts.provisioning`). It is not a fifth job verb — a recipe is not a
shipped bundle — but it reads its outcome off the same explicit marker, and it
hands the recipe the job's own PATH so the self-check cannot pass a host every
job would fail.

Timeouts are the job's, not this module's. The ``gcloud`` plumbing — describe,
start, scp, clone — is bounded by :data:`_GCLOUD_TIMEOUT`, but the job's own
ssh call is bounded by :attr:`~kew.hosts.provider.Job.timeout_seconds` as it is
on the local host, so a 20-minute verification is not cut off at five.

Three things bound a session the network has already killed, because a ceiling
alone did not (#2465). Every session carries ssh **keepalives**
(:data:`SSH_KEEPALIVE_FLAGS`), so a dead peer is noticed in about a minute
instead of never: a verify session once stayed open on the operator's Mac for 66
minutes with nothing left of it on the instance. Every ceiling ends the
session's whole process GROUP — ``gcloud compute ssh`` is a wrapper, and ending
only the pid kew holds leaves its ``ssh`` child connected with the job still
running on the instance, which kew is about to release and arm the idle stop on.
And a job that named *no* ceiling runs under this host's own **deadline**
(``[hosts.gcp] job_timeout_minutes``), because an unbounded wait on a session
that will never end holds the host's lock for ever. The deadline never cuts a
ceiling the caller did name: that would end a merely slow job and report it as a
lost connection.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from math import ceil
from pathlib import Path
from typing import NamedTuple, NoReturn

from kew import subprocess_utils
from kew.config import GcpHostConfig
from kew.credential_surface import is_opaque_credential_key
from kew.hosts.environment import (
    credential_shape,
    refuse_credentials,
    remote_path_prefix,
)
from kew.hosts.errors import (
    HostBusy,
    HostConnectionLost,
    HostError,
    HostInterrupted,
    HostJobDeadlineExceeded,
    HostTimeout,
)
from kew.hosts.provider import HostProvider, Job, PendingTakeover, TakeoverLedger
from kew.hosts.provisioning import PIN_FILE, pin_path
from kew.process_groups import LINUX_BOOT_ID, normalized_boot_id, read_raw_boot_id

logger = logging.getLogger(__name__)

#: Where the bundle and its checkout live, relative to the SSH login's home.
#: The checkout path is public because remote dispatch runs its container over
#: the very checkout ``ship()`` wrote (:mod:`kew.hosts.remote_dispatch`), and a
#: second copy of this string is a second place for the two to disagree.
REMOTE_WORKSPACE = "kew-host"
REMOTE_CHECKOUT = f"{REMOTE_WORKSPACE}/checkout"

#: Where the shipped bundle waits for its clone: a directory of this login's
#: own on the instance's tmpfs, never its disk (#2569). The clone's ``trap``
#: removes the bundle on every exit a shell sees, but a ``SIGKILL`` or a power
#: loss is not one of them; on ``/dev/shm`` a power loss takes the bundle with
#: it, and every ship removes whatever an earlier killed one left there before
#: it copies anything (:meth:`GcpProvider._ship`).
_REMOTE_BUNDLE_DIR = "/dev/shm/kew-host"  # nosec B108 - instance tmpfs, by design
_REMOTE_BUNDLE = f"{_REMOTE_BUNDLE_DIR}/repo.bundle"

#: Where a kew older than #2569 left the bundle: on the instance's disk. Still
#: removed at the start of every ship, so an upgrade cannot leave one behind.
_LEGACY_REMOTE_BUNDLE = f"{REMOTE_WORKSPACE}/repo.bundle"

#: The ship's first step: every stale bundle gone, the checkout gone, and the
#: tmpfs directory recreated for this login alone (mode 0700). ``rm -rf`` of a
#: directory another login owns in the sticky ``/dev/shm`` fails, and so does
#: the step, rather than copying into a directory someone else controls.
_PREPARE_SHIP = (
    f"rm -rf {REMOTE_CHECKOUT} {_LEGACY_REMOTE_BUNDLE} {_REMOTE_BUNDLE_DIR} && "
    f"mkdir -p {REMOTE_WORKSPACE} && mkdir -m 700 {_REMOTE_BUNDLE_DIR}"
)

#: Bundle refs under this prefix name a shallow boundary, not a branch: a
#: commit whose parents were deliberately left out of the bundle (#2550). The
#: clone writes them to ``.git/shallow`` so git on the instance stops there
#: instead of failing to traverse parents it was never sent.
SHALLOW_REF_PREFIX = "refs/kew/shallow/"


#: ``git`` as the instance runs it on its host: an emptied environment, no
#: system or global config, the LFS driver emptied and no hooks (#2569 review).
#: The checkout the clone writes is the branch under review, and its
#: ``.gitattributes`` choose the filters a checkout runs — on the instance's
#: host, as the ssh login, beside the daemon. ``env -i`` rather than a list of
#: names to unset: ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_<n>`` and
#: ``GIT_CONFIG_PARAMETERS`` inherited from the VM's login would define a
#: filter at command scope, which no config FILE setting turns off, and a list
#: would need extending every time git adds another such variable. Only PATH
#: (to find git) and HOME (git refuses to start without one) cross over.
_INERT_GIT = (
    'env -i PATH="$PATH" HOME="$HOME" '
    "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_LFS_SKIP_SMUDGE=1 "
    "git -c filter.lfs.process= -c filter.lfs.smudge= -c filter.lfs.required=false "
    "-c core.hooksPath=/dev/null"
)


def clone_command(bundle: str = _REMOTE_BUNDLE) -> str:
    """The remote step that clones the shipped bundle, then deletes it.

    The bundle is removed whatever the clone did (a ``trap``), so no copy of
    the shipped history stays on the instance beside the checkout (#2550). The
    clone runs no filter, LFS fetch or hook, and neither git call inherits a
    ``GIT_*`` variable from the login (:data:`_INERT_GIT`). A shallow
    bundle's boundaries are read off its own refs; a full one has none and
    writes no ``.git/shallow``. *bundle* is the instance's path; a test names
    another one rather than writing into this machine's ``/dev/shm``.
    """
    checkout = REMOTE_CHECKOUT
    prefix = SHALLOW_REF_PREFIX.replace("/", "\\/")
    return (
        f"trap 'rm -f {bundle}' EXIT; "
        f"{_INERT_GIT} clone --quiet {bundle} {checkout} && "
        f"boundaries=$({_INERT_GIT} bundle list-heads {bundle} | "
        f"sed -n 's/ {prefix}.*$//p') && "
        f'{{ [ -z "$boundaries" ] || '
        f"printf '%s\\n' $boundaries > {checkout}/.git/shallow; }}"
    )


#: Where a provisioning recipe is copied before it is run (N4). Beside the
#: bundle, in the same workspace: a host holds code and recipes, never a
#: credential.
_REMOTE_RECIPE = f"{REMOTE_WORKSPACE}/provision.sh"

#: Where the recipe's tool pins are copied: beside the recipe, under the name
#: the recipe looks for next to itself (:func:`kew.hosts.provisioning.pin_path`).
#: A recipe that cannot read its pins refuses to install an unpinned scanner,
#: so this travels with it or provisioning fails loudly (#2462).
_REMOTE_PIN = f"{REMOTE_WORKSPACE}/{PIN_FILE}"

#: Statuses an instance passes through on its way down. Starting one is
#: refused by the API, so ``ensure_up()`` waits them out.
STOPPING_STATUSES = frozenset({"STOPPING", "SUSPENDING"})

#: Statuses ``instances start`` is the right verb from.
STOPPED_STATUSES = frozenset({"TERMINATED", "STOPPED"})

#: Statuses ``instances resume`` is the right verb from. GCE refuses
#: ``instances start`` on a suspended instance, and ``SUSPENDING`` is waited
#: out into this, so the verb is chosen from the status rather than fixed.
SUSPENDED_STATUSES = frozenset({"SUSPENDED"})

#: Every status the instance can be brought up from, by one verb or the other.
STARTABLE_STATUSES = STOPPED_STATUSES | SUSPENDED_STATUSES

_RUNNING = "RUNNING"
EXIT_MARKER = "KEW_HOST_EXIT"
_EXIT_PATTERN = re.compile(rf"^{EXIT_MARKER}=(-?\d+)$", re.MULTILINE)

#: ``systemd`` records a scheduled shutdown here; its absence is the proof
#: ``shutdown -c`` actually cancelled one.
_SHUTDOWN_SENTINEL = "/run/systemd/shutdown/scheduled"

#: The transient ``systemd`` timer the host's idle stop rides on. One named
#: unit so the next job has one thing to cancel rather than a deadline to
#: guess at, and — the point of it — a timer closes no logins while it waits.
IDLE_STOP_UNIT = "kew-idle-stop"

#: The instance's own record of which kew process holds it, and until when
#: (#2627). The operator-side ``flock`` is released the moment an operator
#: process dies, while the job it shipped runs on to its deadline; this file
#: is what the NEXT process — on this machine or another — reads before it
#: ships over that job's checkout. On ``/run`` so a reboot, which ends every
#: job, also clears it.
LEASE_FILE = "/run/kew-host.lease"

#: Serialises the lease's read-then-write on the instance.
_LEASE_LOCK = "/run/kew-host.lease.lock"

#: The last takeover of the instance's lease: its id, the dead holder and the
#: process that took over, written with the lease it gave (#2874). Every hold
#: reads it back first, so an operator killed between the takeover and its
#: audit row leaves the next one proof of which pending takeover went through.
TAKEOVER_FILE = "/run/kew-host.lease.takeover"

#: Takeover ids a later hold settled as not confirmed, one per line (#2978).
#: A hold appends them under the lease lock in the same breath as it reads
#: :data:`TAKEOVER_FILE`, and a takeover whose id is here refuses before it
#: stops or writes anything. So a detached ``gcloud`` session that outlived
#: its killed operator cannot take the lease after its takeover was put on
#: record as never confirmed. On ``/run``, like the marker it fences.
TAKEOVER_FENCE_FILE = "/run/kew-host.lease.fenced"

#: The lease commands' one answer for "another kew process holds this
#: instance". An expired foreign lease reports through this answer before an
#: audited takeover. Not 1 (a command that failed) and not 255 (ssh itself).
LEASE_HELD_EXIT = 3

#: A job's answer for "this job's session could not be recorded, so it never
#: started" (#2811): a job no record names is one a later takeover could not
#: stop, so it is refused before it runs rather than left unstoppable. So is
#: a job whose lease a stop removed before the job took the lease lock (#2980).
JOB_UNRECORDED_EXIT = 4

#: A release's answer for "the job this process ran is not proved stopped"
#: (#2811): the lease and its backstop are kept, so the next ship cannot
#: delete the checkout under a job that outlived its ssh session.
JOB_SURVIVES_EXIT = 5

#: A takeover's answer for "the lease no longer names the holder this process
#: saw lapse" (#2874): it expired, was released or was taken between the hold
#: that read it and the takeover. Nothing is stopped, written or marked; the
#: lease is read again rather than taken on a stale observation.
LEASE_CHANGED_EXIT = 6

#: A takeover's answer for "a later hold already settled this takeover as not
#: confirmed" (#2978): its id is in :data:`TAKEOVER_FENCE_FILE`, so nothing is
#: stopped, written or marked.
TAKEOVER_FENCED_EXIT = 7

#: How many times a hold reads the lease again after a takeover found it
#: changed (:data:`LEASE_CHANGED_EXIT`) before it gives up.
_LEASE_CHANGED_RETRIES = 3

#: A takeover's answers that take nothing and send the hold to read the lease
#: again: it changed, or another hold fenced this takeover off (#2978).
_READ_AGAIN_EXITS = (LEASE_CHANGED_EXIT, TAKEOVER_FENCED_EXIT)

#: The instance's own backstop for a held lease: a power-off armed on the
#: instance when a job takes the host, for the lease's deadline plus the idle
#: grace (#2627). The idle stop is only ever armed by ``release()``, which runs
#: in the operator process; an operator killed mid-job never reaches it, and
#: the job's own ``ensure_up`` had cancelled the previous one, so without this
#: the VM stayed up and billing. A unit of its own, apart from
#: :data:`IDLE_STOP_UNIT`: ``ensure_up`` proves the idle stop gone, and this
#: one is meant to stay armed for as long as the job runs.
DEADLINE_STOP_UNIT = "kew-host-deadline"

#: Slack past a job's own ceiling before its lease lapses, so the operator's
#: kill — which keeps the verdict — always gets there first.
_LEASE_GRACE_SECONDS = 300.0

#: What ``systemctl is-active`` *says* about a timer that will not fire. The
#: proof reads the word, not the negated exit status: ``! systemctl is-active
#: --quiet`` cannot tell "the timer is not armed" from "systemctl could not
#: answer", so a missing binary or an unreachable bus — both of which exit
#: non-zero and print nothing — came back as positive proof that no stop was
#: pending. An answer this list does not contain, the empty answer included,
#: is read as a stop still armed.
_DISARMED_STATES = ("inactive", "failed", "dead", "unknown")

#: Disarm both stops kew can find and prove neither is left: the timer this
#: host's own ``release()`` arms, and a ``shutdown -h`` armed by a kew older
#: than #2411 or by an operator's hand. Every branch ends in an explicit
#: ``exit 0`` or ``exit 1`` and nothing else, so a 255 is still ssh's own
#: failure.
_CANCEL_SHUTDOWN = (
    f"sudo systemctl stop {IDLE_STOP_UNIT}.timer >/dev/null 2>&1 || true; "
    "sudo shutdown -c >/dev/null 2>&1 || true; "
    f"test -e {_SHUTDOWN_SENTINEL} && exit 1; "
    f"state=$(systemctl is-active {IDLE_STOP_UNIT}.timer 2>/dev/null); "
    f'case "$state" in {"|".join(_DISARMED_STATES)}) exit 0 ;; *) exit 1 ;; esac'
)

#: pam refuses every unprivileged login once ``shutdown`` has written
#: ``/run/nologin``, which it does five minutes before the stop fires. The
#: refusal arrives as ssh's own 255, so it is told apart by the message: it
#: means "this host is going down", not "this host is unreachable", and no
#: number of retries gets in. Matched loosely because the wording is pam's
#: and the file's, and both vary by distribution.
_NOLOGIN_PATTERN = re.compile(
    r"system is going down|not permitted to log ?in|/run/nologin",
    re.IGNORECASE,
)

#: ``ssh``'s own failure status, as ``gcloud compute ssh`` passes it back. The
#: cancel command exits 0 or 1 and nothing else, so a 255 is the connection
#: failing and not a sentinel still present.
#: Worth telling apart: the two failures ask the operator for different
#: things, and reporting an unreachable instance as one with a shutdown
#: pending sends them looking for a stop that was never armed.
_SSH_FAILURE = 255

_POLL_SECONDS = 5.0
_POLL_ATTEMPTS = 60
_GCLOUD_TIMEOUT = 300.0

#: How often the local ssh probes a silent peer, and how many probes go
#: unanswered before it gives up: a dead connection is detected in about a
#: minute. Without them a half-open TCP connection is invisible from this end —
#: the session stays open on a peer that will never speak again, and every
#: ceiling kew owns is downstream of a read that never returns (#2465).
_KEEPALIVE_INTERVAL = 15
_KEEPALIVE_COUNT = 4

#: The keepalive options as ``gcloud compute ssh`` takes them: ``--ssh-flag``
#: is forwarded verbatim to ``ssh``. One tuple so no session can be opened with
#: half of them.
SSH_KEEPALIVE_FLAGS = (
    f"--ssh-flag=-oServerAliveInterval={_KEEPALIVE_INTERVAL}",
    f"--ssh-flag=-oServerAliveCountMax={_KEEPALIVE_COUNT}",
)

#: The same options for ``gcloud compute scp``, which spells its passthrough
#: ``--scp-flag`` and REFUSES ``--ssh-flag`` outright. The options themselves
#: are ssh's either way (``scp`` reads ``-o``), so a copy to a peer that has
#: gone away ends in about a minute like every other session; spelling it
#: ``--ssh-flag`` here would fail every bundle ship instead.
SCP_KEEPALIVE_FLAGS = tuple(
    flag.replace("--ssh-flag=", "--scp-flag=", 1) for flag in SSH_KEEPALIVE_FLAGS
)

#: What no session to an instance may carry back with it (#2550). An operator's
#: own ``~/.ssh/config`` can say ``ForwardAgent yes``, ``SendEnv GH_*`` or
#: ``SetEnv``, and a VM holding a live agent socket can use every key in it.
#: A command-line ``-o`` does not settle that: ``SendEnv`` and ``SetEnv`` are
#: lists a later config line still appends to, and ``-oSendEnv=-*`` clears only
#: what came before it (#2569). So no config file is read at all — ``-F``
#: names an empty one, which also skips ``/etc/ssh/ssh_config`` and its stock
#: ``SendEnv LANG LC_*`` — and every forwarding is still refused explicitly.
#: The session environment is scrubbed too (:func:`remote_session_environment`)
#: so that nothing credential-shaped is there to send even if one did.
SSH_CONFIG_FILE = os.devnull

_SSH_BOUNDARY_OPTIONS = (
    f"-F{SSH_CONFIG_FILE}",
    "-oForwardAgent=no",
    "-oForwardX11=no",
    "-oClearAllForwardings=yes",
    "-oSendEnv=-*",
)

#: The boundary options as ``gcloud compute ssh`` passes them through.
SSH_BOUNDARY_FLAGS = tuple(f"--ssh-flag={option}" for option in _SSH_BOUNDARY_OPTIONS)

#: The same options in the spelling ``gcloud compute scp`` takes.
SCP_BOUNDARY_FLAGS = tuple(f"--scp-flag={option}" for option in _SSH_BOUNDARY_OPTIONS)

#: The agent's own variables: a socket the session could forward and the pid
#: that owns it.
_SSH_AGENT_ENVS = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID"})


def remote_session_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every ``gcloud`` call to an instance runs in.

    Without the ssh agent and without any credential-shaped name: ``GH_TOKEN``,
    ``GITHUB_TOKEN``, ``LINEAR_API_KEY`` and every model key stay on this
    machine, so no ``SendEnv`` in an operator's ssh config has anything to send
    (#2550). Two classifiers, and a name either one claims is dropped
    (#2569): the host namespace rule
    (:func:`~kew.hosts.environment.credential_shape`) and kew's own credential
    word classifier (:func:`~kew.credential_surface.is_opaque_credential_key`),
    which is what catches a name like ``LC_AUTH`` — the ``LC_*`` namespace a
    stock ``SendEnv`` forwards — that no prefix rule names.

    ``CLOUDSDK_*`` is gcloud's own configuration, read here and never
    forwarded, and is exempt from both classifiers. That includes its auth:
    ``CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE``, ``CLOUDSDK_AUTH_ACCESS_TOKEN``
    and ``CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`` are how gcloud knows who
    it is, and dropping them makes every API call fail or quietly run as
    whatever principal the config directory holds instead. Keeping them is
    safe because no session can send them: ``-F`` names an empty config, so no
    ``SendEnv`` is read, ``-oSendEnv=-*`` clears the rest, and the stock
    ``SendEnv LANG LC_*`` never named the namespace anyway.
    """
    source = os.environ if env is None else env
    return {
        name: value
        for name, value in source.items()
        if name not in _SSH_AGENT_ENVS and not _session_credential(name)
    }


def _session_credential(name: str) -> bool:
    """Whether *name* reads as a credential to either of kew's classifiers."""
    if name.upper().startswith("CLOUDSDK_"):
        return False
    return is_opaque_credential_key(name) or credential_shape(name) is not None


#: How long a killed session is waited for before the verdict is raised without
#: it. The group has already taken a ``SIGKILL``, so this is only the kernel
#: reaping what it held.
_KILL_DRAIN_SECONDS = 10.0

#: Seconds in the minute ``[hosts.gcp] job_timeout_minutes`` counts in.
_MINUTE = 60.0

#: How wide the pre-shutdown nologin window is: ``shutdown`` writes
#: ``/run/nologin`` five minutes before the stop fires. A wait that has to
#: outlast the window has to outlast *this*, and then the stop itself.
_NOLOGIN_WINDOW_SECONDS = 300.0

#: The budget for waiting out a stop no login of kew's can cancel: the whole
#: nologin window, and then the ordinary poll budget for the ``STOPPING`` ->
#: ``TERMINATED`` transition that follows it. Exactly the window's width is
#: not enough — it expires with the instance still ``RUNNING``, one tick
#: before the stop it was waiting for even begins (#2411).
_STOP_BUDGET_SECONDS = _NOLOGIN_WINDOW_SECONDS + _POLL_SECONDS * _POLL_ATTEMPTS

#: How many times a bare ssh refusal is retried before it is reported as an
#: unreachable host. A just-started instance reads ``RUNNING`` before sshd
#: answers, so the first contact after a cold start is expected to fail.
_SSH_READY_ATTEMPTS = 12


_LEASE_TOKENS: dict[int, str] = {}

#: Serialises :func:`lease_token`'s get-then-create. Shards lease from several
#: threads at once, and the boot-id read makes the create slow enough for two
#: of them to mint different tokens for one process — the second of which then
#: refuses the first's renewal and release as a stranger's.
_LEASE_TOKENS_LOCK = threading.Lock()

#: The takeovers this process has asked an instance for and not yet settled,
#: by takeover id (#2874). A pending row named here is being settled by the
#: thread that asked; any other pending row of this process's — one whose
#: settle failed — is reconciled like a dead operator's.
_IN_FLIGHT_TAKEOVERS: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()


@contextlib.contextmanager
def _in_flight(takeover: str) -> Iterator[None]:
    """Mark *takeover* as being asked for and settled, for as long as this is held."""
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT_TAKEOVERS.add(takeover)
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT_TAKEOVERS.discard(takeover)


def _is_in_flight(takeover: str) -> bool:
    with _IN_FLIGHT_LOCK:
        return takeover in _IN_FLIGHT_TAKEOVERS


#: How many times the takeover checks a lapsed holder's scope for a process
#: left alive after ``SIGKILL``, a fifth of a second apart, before it refuses.
_SCOPE_KILL_CHECKS = 15

#: How many times a job waits, a fifth of a second apart, for the scope it
#: asked systemd for to read active with the job's shell inside it.
_SCOPE_START_CHECKS = 25

#: How long a stop waits between the two empty container listings it needs
#: before it calls the job's containers gone: a container the daemon accepted
#: from a ``docker run`` still in flight is listed only a moment later (#2980).
_CONTAINER_SETTLE = "0.5"

#: Where the instance records which root-owned scope carries each job, one
#: ``<token> <unit> <start>`` line per job under the token of the lease it ran
#: under, and a ``<token> - <start>`` line per lease newly taken (#2876). On
#: ``/run`` and written as root. It is not out of the job's reach — the job
#: runs as the ssh login, which has passwordless ``sudo`` — so a stop reads it
#: only for the holder's earliest start, to stop the containers started since,
#: and kills every job scope on the instance (:func:`_stop_and_drop`). Appended to
#: and never rewritten, so no two writers race; a reboot, which ends every
#: scope, clears it.
JOB_SESSION_FILE = "/run/kew-job.scopes"

#: The name every job scope starts with; the rest is a fresh nonce.
_SCOPE_PREFIX = "kew-job-"

#: Linux's boot id: a fresh uuid every boot, readable by any user.
_LINUX_BOOT_ID = LINUX_BOOT_ID

#: The boot ids this operator machine has leased an instance under, one per
#: line, beside its host locks. A token's machine is only its hostname, which
#: a second operator machine may share, so a boot id kew never recorded here
#: is someone else's boot, not an earlier one of this machine (#2811).
BOOTS_FILE = "boots"

#: ``<machine>-<pid>-<boot>-<nonce>``. The machine part may itself hold ``-``,
#: so the token is read from the right.
_TOKEN_PATTERN = re.compile(
    r"^(?P<machine>.+)-(?P<pid>[0-9]+)-(?P<boot>[0-9a-f]{32})-[0-9a-f]{12}$"
)

#: ``<machine>-<pid>-<nonce>``: a token from a kew older than #2811, or from a
#: machine whose boot id could not be read. It names no boot, so it can never
#: be proved lapsed, but its pid can still be proved live.
_BOOTLESS_TOKEN_PATTERN = re.compile(r"^(?P<machine>.+)-(?P<pid>[0-9]+)-[0-9a-f]{12}$")

#: What :func:`_lease_guard` prints for a live lease: the holder comes first.
_HELD_BY_PATTERN = re.compile(r"held by (\S+) for ")

#: What :func:`_lease_guard` prints for an expired foreign lease.
_EXPIRED_HOLDER_PATTERN = re.compile(r"expired lease held by (\S+)")

#: What :func:`hold_command` prints of :data:`TAKEOVER_FILE` before anything.
_LAST_TAKEOVER_PATTERN = re.compile(r"^last takeover (\S+) ", re.MULTILINE)


class LeaseHolder(NamedTuple):
    """Who a lease token names: the operator machine, its pid, its boot."""

    machine: str
    pid: int
    boot: str


def _machine() -> str:
    """This operator machine's name as a lease token carries it."""
    machine = re.sub(r"[^A-Za-z0-9_.-]+", "-", socket.gethostname()).strip("-")
    return machine or "operator"


def _read_boot_id() -> str:
    """This machine's raw boot id; :class:`OSError` when it will not say."""
    return read_raw_boot_id(_LINUX_BOOT_ID)


def boot_id() -> str | None:
    """This operator machine's boot id, as 32 hex digits, or ``None``.

    Linux's ``/proc/sys/kernel/random/boot_id``, macOS's
    ``kern.bootsessionuuid``. A pid only names a process within one boot, so
    a lease that names a pid has to name the boot too before anyone can say
    its holder is gone (#2811).
    """
    try:
        raw = _read_boot_id()
    except OSError as exc:
        logger.warning(
            "cannot read this machine's boot id (%s): no lease it holds can "
            "be proved lapsed before its deadline",
            exc,
        )
        return None
    return normalized_boot_id(raw)


def lease_token() -> str:
    """This process's name on an instance's lease, fresh in every process.

    The machine, the pid, the machine's boot id and a random part, so neither
    a second operator machine nor a recycled pid can pass for the holder, and
    a holder from an earlier boot can be told apart from a live one (#2811).
    Keyed by pid so a forked child does not inherit its parent's claim. A
    machine whose boot id cannot be read names no boot, and its leases are
    then never judged lapsed before their deadline.
    """
    pid = os.getpid()
    with _LEASE_TOKENS_LOCK:
        token = _LEASE_TOKENS.get(pid)
        if token is None:
            boot = boot_id()
            booted = "" if boot is None else f"-{boot}"
            token = f"{_machine()}-{pid}{booted}-{uuid.uuid4().hex[:12]}"
            _LEASE_TOKENS[pid] = token
    return token


def parse_lease_token(token: str) -> LeaseHolder | None:
    """The machine, pid and boot *token* names, or ``None`` for any other shape.

    A token from a kew older than #2811 names no boot and reads as ``None``:
    nothing about it can prove its holder gone.
    """
    match = _TOKEN_PATTERN.match(token)
    if match is None:
        return None
    return LeaseHolder(match["machine"], int(match["pid"]), match["boot"])


def _recorded_boots(lock_dir: Path) -> set[str]:
    """Every boot id :func:`remember_boot` recorded under *lock_dir*.

    An unreadable record is said and read as no boots: no lease is then judged
    an earlier boot's, and each holds to its deadline.
    """
    boots = lock_dir / BOOTS_FILE
    text = ""
    if boots.exists():
        try:
            text = boots.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("cannot read this machine's boot record: %s", exc)
    return {line.strip() for line in text.splitlines() if line.strip()}


def remember_boot(lock_dir: Path) -> None:
    """Record this boot as one of this machine's, before any lease names it.

    Never fatal: a boot that could not be recorded only means a lease taken
    in it can never be judged an earlier boot's, and holds to its deadline.
    """
    boot = boot_id()
    if boot is None or boot in _recorded_boots(lock_dir):
        return
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / BOOTS_FILE).open("a", encoding="ascii") as boots:
            boots.write(boot + "\n")
    except OSError as exc:
        logger.warning("cannot record this machine's boot id: %s", exc)


def _pid_running(pid: int) -> bool:
    """Whether *pid* names a process on this machine right now."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Someone else's process, or one this machine will not say about.
        return True
    return True


def lease_lapsed(token: str, *, lock_dir: Path) -> bool:
    """Whether *token* names a holder on THIS machine that is provably gone.

    Gone means it was taken in an earlier boot of this machine — one
    :func:`remember_boot` recorded under *lock_dir* — or in this boot by a pid
    that is not running. A holder on another machine, a token with no boot in
    it, or a machine whose own boot id is unreadable proves nothing, and its
    lease holds until its deadline. Nor does a boot id never recorded here: a
    second machine sharing this one's hostname names its own boots, and its
    live job is not this machine's to stop. A live pid is taken at its word
    even though it may have been recycled: refusing a dead holder costs a
    wait, taking over a live one costs its job.
    """
    holder = parse_lease_token(token)
    if holder is None or holder.machine != _machine():
        return False
    own = boot_id()
    if own is None:
        return False
    if holder.boot != own:
        return holder.boot in _recorded_boots(lock_dir)
    return not _pid_running(holder.pid)


def _of_this_boot(token: str) -> bool:
    """Whether *token* names a process of this machine's current boot."""
    holder = parse_lease_token(token)
    if holder is None or holder.machine != _machine():
        return False
    own = boot_id()
    return own is not None and holder.boot == own


def live_holder_pid(token: str) -> int | None:
    """The pid of a process on THIS machine *token* may name, or ``None``.

    A token with no boot in it — a kew older than #2811 — still names a pid,
    and a live one is taken at its word: such a job has no scope record, so
    dropping its lease would stop nothing. So is a live pid this machine cannot
    tell from its own boot, because its boot id is unreadable.
    """
    holder = parse_lease_token(token)
    if holder is None:
        bootless = _BOOTLESS_TOKEN_PATTERN.match(token)
        if bootless is None:
            return None
        holder = LeaseHolder(bootless["machine"], int(bootless["pid"]), "")
    if holder.machine != _machine() or not _pid_running(holder.pid):
        return None
    own = boot_id()
    return holder.pid if not holder.boot or own is None or holder.boot == own else None


def _lease_guard(
    token: str, *, lapsed: str | None = None, report_expired: bool = False
) -> str:
    """Exit :data:`LEASE_HELD_EXIT`, naming the holder, while another live lease
    holds the instance; fall through when the lease is this process's,
    *lapsed*, unreadable or absent. Runs as root, under :data:`_LEASE_LOCK`.

    *lapsed* names the holder an audited takeover will stop. The operator
    proved it gone (:func:`lease_lapsed`) or first observed its lease expired.
    Its lease falls through too, and nobody else's does.

    *report_expired* also exits on an expired foreign lease, naming it,
    without changing the host, so :func:`hold_command` takes it over only
    through the audited takeover (#2874). Without it an expired lease falls
    through to the caller's own refusal (:func:`_refuse_another_holder`).
    """
    others = f'[ "$tok" != {shlex.quote(token)} ] '
    if lapsed is not None:
        others += f'&& [ "$tok" != {shlex.quote(lapsed)} ] '
    held = 'echo "held by $tok for $((exp - now))s more"; '
    if report_expired:
        check = (
            f'if [ -n "$tok" ] && {others}; then '
            f'if [ "$exp" -gt "$now" ]; then {held}'
            'else echo "expired lease held by $tok"; fi; '
            f"exit {LEASE_HELD_EXIT}; fi; "
        )
    else:
        check = (
            f'if [ -n "$tok" ] && {others}&& [ "$exp" -gt "$now" ]; then '
            f"{held}exit {LEASE_HELD_EXIT}; fi; "
        )
    return (
        "now=$(date +%s) || exit 1; tok=; exp=0; "
        f"if [ -r {LEASE_FILE} ]; then read -r tok exp < {LEASE_FILE}; fi; "
        'case "$exp" in ""|*[!0-9]*) exp=0 ;; esac; ' + check
    )


def _still_held_by(holder: str, *, expired: bool = False) -> str:
    """Exit :data:`LEASE_CHANGED_EXIT` unless the observed lease is unchanged.

    Runs after :func:`_lease_guard`, under :data:`_LEASE_LOCK`, so the lease a
    takeover stops a job for and replaces is the very one the operator saw
    lapse or expire. A lapsed lease must still be live. An expired lease must
    still be expired. A lease that changed hands, disappeared or crossed its
    deadline is not taken, and no takeover is marked for it (#2874).
    """
    deadline_changed = '[ "$exp" -gt "$now" ]' if expired else '[ "$exp" -le "$now" ]'
    return (
        f'if [ "$tok" != {shlex.quote(holder)} ] || {deadline_changed}; then '
        'echo "the lease changed since it was read: it names ${tok:-nobody}"; '
        f"exit {LEASE_CHANGED_EXIT}; fi; "
    )


def _stop_and_drop(
    holder: str,
    *,
    require_record: bool = True,
    refuse_exit: int = LEASE_HELD_EXIT,
    hint: str = "",
) -> str:
    """Stop *holder*'s work, then remove its lease: the one way a lease goes.

    Runs after :func:`_lease_guard`, under the same hold of the lease lock,
    and does nothing unless the lease names *holder* exactly — live or past
    its deadline alike, since a job can outrun its lease (#2876). Every
    command that removes or overwrites a lease goes through here: a takeover,
    a release, a drop and a hand drop. Only a forced hand drop skips it
    (:func:`drop_holder_command`). *holder* is a shell word.

    Gone means proved gone: the command exits *refuse_exit*, the lease left in
    place, while any job scope reads anything but ended after ``systemctl
    kill``, or ``systemctl`` is missing, or a container started since the
    holder's recorded start is still running or created (:func:`_kill_scopes`).
    A scope is the job's own cgroup, so a child the job detached with
    ``setsid`` before it exited 0 is still in it, and still stopped.

    The record only says when the holder took the lease or started a job: the
    scopes killed are every ``kew-job-*`` scope loaded on the instance. The
    lease admits one holder at a time, so any other is the holder's own or
    one an earlier holder left when its lease was forced off, and a record
    rewritten to name some other scope cannot steer the kill away.

    A holder no record names with a start time — a kew older than #2876 — gives
    no start to stop containers from, so its lease is kept unless
    *require_record* is off (a process dropping its own lease that ran no
    job). *hint* ends that refusal and the others, naming the way out.
    """
    keep = f'its lease is kept until it is stopped{hint}"; exit {refuse_exit}; '
    unrecorded = (
        'if [ -z "$since" ]; then echo "no scope record with a start time '
        f"names holder $tok, so its job cannot be stopped: {keep}fi; "
        if require_record
        else ""
    )
    return (
        f'if [ "$tok" = {holder} ]; then '
        + _read_scope_record(holder)
        + unrecorded
        + _kill_scopes(keep)
        + f"rm -f {LEASE_FILE} || exit 1; fi; "
    )


def _refuse_another_holder(holder: str) -> str:
    """Exit :data:`LEASE_HELD_EXIT` while the lease names anyone but *holder*.

    For :func:`_lease_guard`'s fall-through on an expired lease: only a
    takeover (:func:`hold_command`) stops another holder's job and replaces
    its lease, so a drop or a release leaves it, and its job, alone.
    """
    return (
        f'if [ -n "$tok" ] && [ "$tok" != {shlex.quote(holder)} ]; then '
        'echo "the lease names $tok, whose deadline has passed: it is not '
        'the holder this command drops, so it and its job are left alone"; '
        f"exit {LEASE_HELD_EXIT}; fi; "
    )


def _read_scope_record(holder: str) -> str:
    """Set ``$units`` and ``$since`` from the record lines naming *holder*.

    *holder* is a shell word. ``$units`` lists the scopes recorded under it,
    ``$since`` the earliest start time recorded with them, in epoch seconds.
    ``$ran`` is set when any line names a job of it — a unit field other than
    a lease's ``-``, well formed or not — and empty for a holder that only
    took the lease.
    """
    return (
        f"units=; since=; ran=; if [ -r {JOB_SESSION_FILE} ]; then "
        "while read -r jtok unit start; do "
        f'if [ "$jtok" = {holder} ]; then case "$unit" in -) ;; *) ran=1 ;; esac; '
        'case "$unit" in '
        f"{_SCOPE_PREFIX}*[!0-9a-f]*) ;; "
        f'{_SCOPE_PREFIX}?*) units="$units $unit" ;; esac; '
        'case "$start" in ""|*[!0-9]*) ;; *) '
        '{ [ -n "$since" ] && [ "$since" -le "$start" ]; } || since=$start ;; '
        f"esac; fi; done < {JOB_SESSION_FILE}; fi; "
    )


#: Sets ``$gone`` when ``$unit``'s scope is proved ended: it reads inactive
#: or failed, or systemd no longer has it loaded — a finished transient scope
#: is collected (``CollectMode=inactive-or-failed``), and a collected unit is
#: either shown as ``not-found`` or refused as not loaded, by systemd version.
#: Any other answer, or a failure to answer, leaves ``$gone`` empty.
_SCOPE_GONE = (
    "gone=; st=$(systemctl show --property=LoadState --property=ActiveState "
    '"$unit.scope" 2>&1) && case "$st" in *LoadState=not-found*|'
    "*ActiveState=inactive*|*ActiveState=failed*) gone=1 ;; esac || "
    'case "$st" in *" could not be found."*|*" not loaded."*) gone=1 ;; esac; '
)


def _kill_scopes(keep: str) -> str:
    """Kill every job scope on the instance, then the job's containers.

    *keep* ends a refusal: the rest of the ``echo`` and the ``exit``.
    """
    return (
        "if ! command -v systemctl >/dev/null; then "
        f'echo "systemctl is missing, so the job of holder $tok cannot be '
        f"stopped: {keep}fi; "
        "loaded=$(systemctl list-units --all --plain --no-legend --type=scope "
        f"'{_SCOPE_PREFIX}*' 2>/dev/null) || {{ "
        f'echo "the scopes on this instance cannot be listed, so the job of '
        f"holder $tok cannot be stopped: {keep}}}; "
        'for unit in $loaded; do case "$unit" in '
        f"{_SCOPE_PREFIX}*[!0-9a-f]*.scope) ;; "
        f'{_SCOPE_PREFIX}?*.scope) units="$units ${{unit%.scope}}" ;; esac; done; '
        "for unit in $units; do n=0; while " + _SCOPE_GONE + '[ -z "$gone" ]; do '
        f'if [ "$n" -ge {_SCOPE_KILL_CHECKS} ]; then '
        'echo "the job of holder $tok (scope $unit) is still running '
        f"after SIGKILL: {keep}fi; "
        'systemctl kill --signal=KILL "$unit.scope" >/dev/null 2>&1; '
        "n=$((n + 1)); sleep 0.2; done; done; "
        + _kill_containers(keep)
        + 'echo "no process is left of holder $tok (scopes:$units)"; '
    )


def _kill_containers(keep: str) -> str:
    """Remove every live container created or started at or after ``$since``.

    A job's ``docker run -d`` leaves a container the daemon parents, outside
    the job's scope, still using the checkout (#2876). Every container is
    listed (``docker ps -a``), not only the running ones, so one still in
    ``created`` — its ``docker run`` not yet through — is caught before it
    starts. Created since the start counts, and so does started since it, for
    a container the job restarted. Each is force-removed (``docker rm -f``
    kills a running one and works on a created one too), and the stop is
    proved by two listings in a row, :data:`_CONTAINER_SETTLE` apart, in which
    none of them is left running or created: a container the daemon accepted
    from a ``docker run`` in flight is listed only a moment later (#2980). An
    exited or dead one runs nothing and is left. A daemon that cannot list
    its containers proves nothing and refuses, and so does a listed
    container it cannot inspect, unless a second listing shows it gone: its
    start is unknown, so it may be the job's. A holder with no recorded start
    ran no job. A holder the record names a job of (``$ran``), on an instance
    with no ``docker`` client, refuses: nothing can list the containers a
    daemon may be running for it, and no socket path or ``PATH`` lookup
    proves there is no daemon — a rootless or custom one listens elsewhere
    (#2980). One that only took the lease started nothing, so an instance
    with no ``docker`` yet — a first provisioning that failed before it was
    installed — still releases it.
    """
    inspect = (
        "docker inspect --format "
        "'{{.State.Status}} {{.Created}} {{.State.StartedAt}}' \"$id\" 2>/dev/null"
    )
    return (
        'if [ -n "$ran" ] && ! command -v docker >/dev/null; then '
        'echo "the docker client is missing on this instance, so whether the '
        f"job of holder $tok left a container running is unknown: {keep}"
        'elif [ -n "$since" ] && command -v docker >/dev/null; then n=0; quiet=0; '
        'while [ "$quiet" -lt 2 ]; do '
        "ids=$(docker ps -aq --no-trunc) || { "
        'echo "the containers on this instance cannot be listed, so the job '
        f"of holder $tok cannot be stopped: {keep}}}; late=; for id in $ids; do "
        f"info=$({inspect}) || {{ listed=$(docker ps -aq --no-trunc 2>/dev/null) "
        '&& ! printf \'%s\\n\' "$listed" | grep -qx -- "$id" || { '
        'echo "container $id cannot be inspected, so whether the job of holder '
        f"$tok started it is unknown: {keep}}}; continue; }}; "
        'set -- $info; case "$1" in exited|dead) continue ;; esac; '
        'made=$(date -d "$2" +%s 2>/dev/null) || made=; '
        'began=$(date -d "$3" +%s 2>/dev/null) || began=; '
        'if [ -z "$made" ] || [ "$made" -ge "$since" ] || '
        '{ [ -n "$began" ] && [ "$began" -ge "$since" ]; }; then '
        'late="$late $id"; fi; done; '
        'if [ -z "$late" ]; then quiet=$((quiet + 1)); '
        f'if [ "$quiet" -lt 2 ]; then sleep {_CONTAINER_SETTLE}; fi; continue; fi; '
        f'quiet=0; if [ "$n" -ge {_SCOPE_KILL_CHECKS} ]; then '
        'echo "a container the job of holder $tok started is still there '
        f"after docker rm -f:$late: {keep}fi; "
        "docker rm -f $late >/dev/null 2>&1; n=$((n + 1)); sleep 0.2; done; fi; "
    )


def job_session_record(token: str) -> str:
    """Move this job's shell into a root-owned scope recorded under *token*.

    The scope (``kew-job-<nonce>.scope``) is a cgroup every process the job
    starts inherits — one detached with ``setsid`` included — so each is
    still in it when a release or a takeover stops it (#2876). Only ``sudo``
    can move a process out, and the ssh login has it: a job that turns it on
    its own scope is root on the instance, and nothing here contains that.
    The record goes on ``/run`` through ``sudo`` before the scope is asked
    for, so no scope exists unrecorded, and the job runs only once the scope
    reads active with its shell inside. Both happen under the lease lock
    (:data:`_LEASE_LOCK`), so a stop never reads a scope recorded but not yet
    started, which it would count as collected and gone (#2980): it waits for
    the scope to start, and then kills it. A stop that took the lock first
    may already have removed the lease, so under the lock the job records
    nothing and starts nothing unless :data:`LEASE_FILE` still names *token*.

    Fatal: a job whose lease is gone, or whose scope cannot be recorded and
    started, exits
    :data:`JOB_UNRECORDED_EXIT` without starting, because a takeover of its
    lease would find nothing to stop and refuse (:func:`_stop_and_drop`).
    """
    unit = f"{_SCOPE_PREFIX}{uuid.uuid4().hex[:12]}"
    quoted = shlex.quote(token)
    contain = (
        f"tok=; if [ -r {LEASE_FILE} ]; then read -r tok exp < {LEASE_FILE}; fi; "
        f'if [ "$tok" != {quoted} ]; then '
        'echo "kew: the lease on this instance names ${tok:-nobody}, not this '
        "job's holder: it was released or taken\" >&2; exit 1; fi; "
        f"umask 022; printf '%s %s %s\\n' {quoted} {unit} "
        f'"$(date +%s)" >> {JOB_SESSION_FILE} || exit 1; '
        "busctl call --quiet org.freedesktop.systemd1 /org/freedesktop/systemd1 "
        "org.freedesktop.systemd1.Manager StartTransientUnit 'ssa(sv)a(sa(sv))' "
        f'{unit}.scope fail 2 PIDs au 1 "$1" CollectMode s inactive-or-failed 0 '
        "|| exit 1; n=0; "
        f"until systemctl is-active --quiet {unit}.scope; do "
        f'[ "$n" -ge {_SCOPE_START_CHECKS} ] && exit 1; '
        "n=$((n + 1)); sleep 0.2; done"
    )
    return (
        f"sudo -n flock {_LEASE_LOCK} sh -c {shlex.quote(contain)} kew-job $$ || "
        "{ echo 'kew: cannot contain this job in a scope recorded in "
        f"{JOB_SESSION_FILE}, so it was not started' >&2; "
        f"exit {JOB_UNRECORDED_EXIT}; }}; "
    )


def _last_takeover_id(result: subprocess.CompletedProcess[str]) -> str | None:
    """The takeover id a hold read back from :data:`TAKEOVER_FILE`, or ``None``."""
    match = _LAST_TAKEOVER_PATTERN.search(result.stdout or "")
    return None if match is None else match[1]


def _held_by(result: subprocess.CompletedProcess[str]) -> str | None:
    """The holder a refused lease command named, or ``None``."""
    match = _HELD_BY_PATTERN.search(result.stdout or "")
    return None if match is None else match[1]


def _expired_holder(result: subprocess.CompletedProcess[str]) -> str | None:
    """The expired foreign holder a lease command named, or ``None``."""
    match = _EXPIRED_HOLDER_PATTERN.search(result.stdout or "")
    return None if match is None else match[1]


def _arm_unit(unit: str, delay: str) -> str:
    """Re-arm *unit* as a power-off after *delay* and prove it is waiting."""
    return (
        f"systemctl stop {unit}.timer >/dev/null 2>&1 || true; "
        f"systemd-run --quiet --on-active={delay} --unit={unit} "
        f"systemctl poweroff && systemctl is-active --quiet {unit}.timer"
    )


def _under_lease_lock(script: str) -> str:
    """Run *script* as root with the instance's lease lock held."""
    return f"sudo flock {_LEASE_LOCK} sh -c {shlex.quote(script)}"


def _last_takeover() -> str:
    """Print the takeover :data:`TAKEOVER_FILE` records, if there is one."""
    return (
        f"if [ -r {TAKEOVER_FILE} ]; then "
        f"read -r ktk kgone kby < {TAKEOVER_FILE}; "
        'echo "last takeover $ktk of $kgone by $kby"; fi; '
    )


def _fence_takeovers(takeovers: Sequence[str]) -> str:
    """Append *takeovers* to :data:`TAKEOVER_FENCE_FILE`, so none can run later."""
    if not takeovers:
        return ""
    ids = " ".join(shlex.quote(takeover) for takeover in takeovers)
    return f"printf '%s\\n' {ids} >> {TAKEOVER_FENCE_FILE} || exit 1; "


def _refuse_fenced(takeover: str) -> str:
    """Exit :data:`TAKEOVER_FENCED_EXIT` when *takeover* was fenced off (#2978)."""
    quoted = shlex.quote(takeover)
    return (
        f"if grep -qxF -e {quoted} {TAKEOVER_FENCE_FILE} 2>/dev/null; then "
        f"echo kew: takeover {quoted} was settled as not confirmed by a later "
        f"hold, so it was not taken; exit {TAKEOVER_FENCED_EXIT}; fi; "
    )


def _mark_takeover(token: str, *, lapsed: str, takeover: str | None) -> str:
    """Write :data:`TAKEOVER_FILE` for the lease just taken from *lapsed*."""
    if takeover is None:
        return ""
    return (
        f"printf '%s %s %s\\n' {shlex.quote(takeover)} {shlex.quote(lapsed)} "
        f"{shlex.quote(token)} > {TAKEOVER_FILE} || exit 1; "
    )


#: Ends a refusal to stop another holder's job: the operator's way out.
_FORCE_HINT = "; kew hosts release NAME --holder $tok --force drops it"


def hold_command(
    token: str,
    *,
    seconds: int,
    idle_minutes: int,
    lapsed: str | None = None,
    takeover: str | None = None,
    expired: bool = False,
    fence: Sequence[str] = (),
) -> str:
    """Take or extend the instance's lease for *seconds*, and arm its backstop.

    Refused with :data:`LEASE_HELD_EXIT` while another process's lease is
    live. Otherwise the lease names *token* until ``now + seconds`` and the
    backstop power-off is armed for that plus the idle grace, so a job whose
    operator dies still ends with the instance stopped.

    A foreign holder always returns before the host changes. The operator can
    then record a pending takeover and ask again with that holder as *lapsed*.
    That one takeover path overwrites the lease only once the holder's whole
    job is proved stopped (:func:`_stop_and_drop`). A lease newly taken records
    its start with the job scopes, so its containers can be found and stopped.

    *lapsed* is taken over only while the lease still names it and its observed
    deadline state is unchanged, else :data:`LEASE_CHANGED_EXIT`. *expired*
    states that the first command observed its deadline passed. *takeover*
    names that takeover in
    :data:`TAKEOVER_FILE`, written once the job is proved stopped and the
    lease given (#2874); the last takeover recorded there is printed first,
    whatever the hold answers.

    *fence* names pending takeovers the operator is about to settle by that
    printed record: they are fenced under the same lock hold, so a command of
    theirs still in flight refuses with :data:`TAKEOVER_FENCED_EXIT` rather
    than taking the lease after the record was read (#2978).
    """
    backstop = seconds + idle_minutes * 60
    quoted = shlex.quote(token)
    stop, mark, refuse = "", "", ""
    if lapsed is not None:
        stop = _still_held_by(lapsed, expired=expired) + _stop_and_drop(
            shlex.quote(lapsed), hint=_FORCE_HINT
        )
        mark = _mark_takeover(token, lapsed=lapsed, takeover=takeover)
    if takeover is not None:
        refuse = _refuse_fenced(takeover)
    return _under_lease_lock(
        _last_takeover()
        + _fence_takeovers(fence)
        + refuse
        + _lease_guard(token, lapsed=lapsed, report_expired=True)
        + stop
        + f'if [ "$tok" != {quoted} ]; then '
        f"(umask 022; printf '%s - %s\\n' {quoted} \"$now\" >> {JOB_SESSION_FILE}) "
        "|| exit 1; fi; " + f"printf '%s %s\\n' {quoted} $((now + {seconds})) "
        f"> {LEASE_FILE} || exit 1; "
        + mark
        + _arm_unit(DEADLINE_STOP_UNIT, f"{backstop}s")
    )


def drop_command(token: str) -> str:
    """Drop this process's lease once its work is stopped, arming nothing.

    Another's lease is left alone, live or not. The work is stopped first
    (:func:`_stop_and_drop`): a placed run that lost its session may leave a
    container running in the checkout the next ship deletes, so one that
    cannot be proved stopped keeps the lease, exiting
    :data:`JOB_SURVIVES_EXIT`. The backstop stays armed: it is this job's,
    and the next job's lease replaces it with its own.
    """
    return _under_lease_lock(
        _lease_guard(token)
        + _refuse_another_holder(token)
        + _stop_and_drop(
            shlex.quote(token), require_record=False, refuse_exit=JOB_SURVIVES_EXIT
        )
    )


def drop_holder_command(holder: str, *, force: bool = False) -> str:
    """Drop *holder*'s lease by hand, stopping the job it left running first.

    The job and the lease go together, under one hold of the lease lock, and
    whether or not the lease is past its deadline: a lease dropped with its
    job still running would let the next job ship over that job's checkout,
    since the next hold finds no holder to stop (#2811). A lease no scope
    record names — a holder from a kew older than #2876 — is refused with
    :data:`JOB_SURVIVES_EXIT` like one whose job survives the kill, since
    nothing proves its job gone. Another holder's lease is left alone, and so
    is its job.

    *force* drops the lease and stops nothing: the operator's explicit call,
    for a job they know is gone or accept may run on.
    """
    quoted = shlex.quote(holder)
    drop = (
        f'if [ "$tok" = {quoted} ]; then rm -f {LEASE_FILE} || exit 1; fi; '
        if force
        else _stop_and_drop(quoted, refuse_exit=JOB_SURVIVES_EXIT)
    )
    return _under_lease_lock(
        _lease_guard(holder) + _refuse_another_holder(holder) + drop
    )


def release_command(token: str, *, idle_minutes: int, ran_job: bool = False) -> str:
    """Drop this process's lease and arm the idle stop in place of the backstop.

    Refused with :data:`LEASE_HELD_EXIT` while another process's lease is
    there, live or past its deadline: a live one disarmed the idle stop for
    a job of its own and owns its shutdown, and arming one here would stop
    that job mid-run; an expired one is a takeover's to stop, and its
    backstop is still armed.

    The job this process ran is stopped first, under the same lock (#2811):
    an interrupted operator ends only its own side of the ssh session, and a
    job child that ignores the hangup would otherwise run on in a checkout the
    next ship deletes. A job not proved stopped — or, when *ran_job*, a job no
    record names — exits :data:`JOB_SURVIVES_EXIT` with the lease and its
    backstop left in place.

    Deliberately not ``shutdown -h +{minutes}``: that is the same delay with
    a five-minute nologin window on the end of it (#2411).
    """
    return _under_lease_lock(
        _lease_guard(token)
        + _refuse_another_holder(token)
        + _stop_and_drop(
            shlex.quote(token),
            require_record=ran_job,
            refuse_exit=JOB_SURVIVES_EXIT,
        )
        + f"systemctl stop {DEADLINE_STOP_UNIT}.timer >/dev/null 2>&1 || true; "
        + _arm_unit(IDLE_STOP_UNIT, f"{idle_minutes}m")
    )


def _refused_by_nologin(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether *result* is pam refusing a login because a stop is pending."""
    if result.returncode == 0:
        return False
    return bool(_NOLOGIN_PATTERN.search((result.stderr or "") + (result.stdout or "")))


def marker_command(inner: str) -> str:
    """Wrap *inner* so the remote shell reports its own exit status."""
    return f"{inner}; printf '\\n{EXIT_MARKER}=%s\\n' \"$?\""


def _environment_prefix(environment: Mapping[str, str]) -> str:
    """``env NAME=value ...`` for the job's declared, credential-checked vars.

    ``refuse_credentials`` is also what makes the names safe to interpolate: a
    value is quoted here, but a name cannot be — ``env 'A;B'=1`` is not an
    assignment — so a name outside the portable character set is refused there
    rather than escaped here.
    """
    declared = refuse_credentials(environment)
    if not declared:
        return ""
    pairs = " ".join(
        f"{name}={shlex.quote(declared[name])}" for name in sorted(declared)
    )
    return f"env {pairs} "


#: The line between a job's stdout and its stderr in the log this host
#: returns. The two come back as separate streams, so all of stderr follows
#: all of stdout; this says where one ends, so a reader of the log
#: (:func:`kew.main_health.failing_gate`) can tell which stream a line is from.
STDERR_FOLLOWS = "KEW_HOST_STDERR follows (all of the job's stderr, after its stdout)"


def with_stderr(stdout: str, stderr: str) -> str:
    """The job's log: *stdout*, then — marked — *stderr*, when there is any."""
    if not stderr:
        return stdout
    joint = "" if not stdout or stdout.endswith("\n") else "\n"
    return f"{stdout}{joint}{STDERR_FOLLOWS}\n{stderr}"


def parse_exit_marker(output: str) -> tuple[int, str]:
    """Split the remote exit marker off *output*, the host's **stdout**.

    Raises :class:`~kew.hosts.errors.HostConnectionLost` when no marker is
    present: ``gcloud``'s own exit status conflates a failed job with a failed
    connection, so a result without the marker is an unknown outcome rather
    than a passing one — and, since the marker is printed by the remote shell
    after the job whatever the job did, the connection going away is the
    reading that fits. An interruption, retryable, and never a test failure
    (#2465).

    Pass stdout alone. The wrapper's ``printf`` runs after the job and writes
    there, so the *last* marker on stdout is always the real one; feeding
    stderr in as well would let a job print ``KEW_HOST_EXIT=0`` on stderr and
    have it read as the outcome.
    """
    matches = list(_EXIT_PATTERN.finditer(output))
    if not matches:
        raise HostConnectionLost(
            "the connection to the gcp host was lost: the session ended with "
            "no exit marker, so the job's outcome is unknown (this is NOT a "
            f"success and NOT a test failure). Output: {output[-2000:]!r}"
        )
    last = matches[-1]
    log = output[: last.start()] + output[last.end() :]
    return int(last.group(1)), log


def require_gcloud() -> None:
    """Raise unless ``gcloud`` is on PATH, naming how to install it.

    Public because remote dispatch streams its own ssh session rather than
    going through :meth:`GcpProvider._gcloud` (it needs the child's stdout as
    it arrives), and it owes the operator the same sentence about an absent
    CLI that every other gcloud call makes.
    """
    if subprocess_utils.ensure_tool("gcloud") is None:
        raise HostError(
            "gcloud is not on PATH, so the gcp host cannot be reached. "
            "Install it: https://cloud.google.com/sdk/docs/install"
        )


def _kill_session_group(session: subprocess.Popen[str]) -> None:
    """Kill the session's whole process GROUP, not just the pid kew holds.

    ``gcloud compute ssh`` is a wrapper, and the ``ssh`` it spawns is what
    carries the job: killing the wrapper's pid alone leaves that child
    connected to the instance with the job — a verification, or a recipe
    running as root — still running on it, while kew releases this host's lock
    and arms its idle stop (#2465). It also keeps the write end of the
    session's pipes open, so every read left on them waits on a pipe no live
    process will close. The same reasoning, and the same remedy, as
    :func:`kew.hosts.remote_dispatch._end_session_group`.

    The group is the session's own, because it was started in a new one, so
    nothing of kew's is in it. Best effort: a group that has already gone is the
    outcome asked for, and one this process may not signal is not a reason to
    lose the verdict the caller is on its way to raising.
    """
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(session.pid), signal.SIGKILL)
    with contextlib.suppress(OSError, ValueError):
        session.kill()


def _end_session(session: subprocess.Popen[str]) -> None:
    """End a session still running, then close its pipes.

    However the read above leaves, the session does not outlive it: the ceiling
    is only one of the ways out. A ``KeyboardInterrupt`` at the terminal, a
    ``SIGTERM`` the driver turned into an exception, an error raised further up
    this stack — each unwinds through here, and the session is in a process
    group of its own, so the terminal's own signal no longer reaches it.
    Without this the escape would leave a detached ``gcloud``/``ssh`` pair, and
    the job it carries still running on the instance, while kew released the
    host's lock and armed its idle stop (#2465).

    The wait after the kill is bounded for the same reason the read is: the
    group has taken a ``SIGKILL``, so all that is left is the kernel reaping
    it, and an unbounded wait here would be the very hang being escaped.
    """
    if session.poll() is None:
        _kill_session_group(session)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            session.wait(timeout=_KILL_DRAIN_SECONDS)
    for stream in (session.stdout, session.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _collect_session(
    session: subprocess.Popen[str], *, timeout: float | None
) -> subprocess.CompletedProcess[str]:
    """Read *session* to its end under *timeout*, leaving nothing of it alive.

    The ceiling is reported as a :class:`~kew.hosts.errors.HostTimeout` and the
    session is ended on the way out — on that path and on every other one
    (:func:`_end_session`), because a session kew has stopped reading is a
    session nothing is left to end.
    """
    try:
        stdout, stderr = session.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HostTimeout(
            f"the ssh session was killed after {timeout}s, so it has no exit code"
        ) from exc
    finally:
        _end_session(session)
    return subprocess.CompletedProcess(session.args, session.returncode, stdout, stderr)


class GcpProvider(HostProvider):
    """A Compute Engine instance, driven through the ``gcloud`` CLI."""

    name = "gcp"
    takes_over_holds = True
    unreachable_fix = (
        "for gcloud: run `gcloud auth login`, and check that the [hosts.gcp] "
        "instance exists"
    )

    def __init__(
        self,
        settings: GcpHostConfig,
        *,
        lock_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = _POLL_SECONDS,
        job_deadline_seconds: float | None = None,
    ) -> None:
        super().__init__(lock_dir=lock_dir)
        if not settings.configured():
            raise HostError(
                "the gcp host needs [hosts.gcp] project, zone and instance: "
                "without all three there is no instance to address"
            )
        self._settings = settings
        self._deadline_seconds = (
            settings.job_timeout_minutes * _MINUTE
            if job_deadline_seconds is None
            else job_deadline_seconds
        )
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._lapsed_holders: list[str] = []
        self._ledger: TakeoverLedger | None = None
        # Whether a job of this process's was sent to the instance since its
        # last release, so the release knows a scope record must name it.
        self._ran_job = False
        self._interrupted = threading.Event()
        self._sessions: set[subprocess.Popen[str]] = set()
        self._sessions_lock = threading.Lock()

    @property
    def address(self) -> str:
        """The instance this provider addresses, which a pool makes plural."""
        return self._settings.instance

    @property
    def resource_key(self) -> str:
        """The instance by project, zone and name: one name, in two, is two."""
        settings = self._settings
        return f"gcp/{settings.project}/{settings.zone}/{settings.instance}"

    @property
    def lock_key(self) -> str:
        """One instance is one host, whichever kew process addresses it."""
        settings = self._settings
        return f"gcp-{settings.project}-{settings.zone}-{settings.instance}"

    # --- gcloud plumbing -------------------------------------------------

    def _gcloud(
        self,
        args: list[str],
        *,
        timeout: float | None = _GCLOUD_TIMEOUT,
        interruptible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one ``gcloud`` command, or say plainly that gcloud is absent.

        Every call is a session :meth:`interrupt` can end — bringing the host
        up, the lease, the copy, the clone — not only the job's: a ``SIGTERM``
        that waited out a 300s ``scp`` could be followed by a ``SIGKILL`` before
        the release ran (#2811). The release path itself passes
        *interruptible* off, since it is what the interruption unwinds to.
        """
        require_gcloud()
        try:
            return self._session(
                ["gcloud", *args], timeout=timeout, interruptible=interruptible
            )
        except HostTimeout as exc:
            raise HostTimeout(
                f"gcloud {' '.join(args)} did not finish within {timeout}s, so "
                "it has no exit code"
            ) from exc

    def _instance_args(self) -> list[str]:
        settings = self._settings
        return [
            f"--project={settings.project}",
            f"--zone={settings.zone}",
            "--quiet",
        ]

    def _compute(
        self,
        verb: list[str],
        *,
        timeout: float | None = _GCLOUD_TIMEOUT,
        interruptible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._gcloud(
            ["compute", *verb, *self._instance_args()],
            timeout=timeout,
            interruptible=interruptible,
        )

    def ssh_argv(self, command: str) -> list[str]:
        """The argv for one ssh session to this instance running *command*.

        Public so a caller that needs the session's stdout *as it arrives* —
        remote dispatch relaying an agent's JSON event stream
        (:mod:`kew.hosts.remote_dispatch`) — opens the same session this
        provider's own calls do, instead of assembling a second gcloud argv
        that could drift from it. ``gcloud`` stays the BARE name for the
        subprocess allowlist, which matches names and not resolved paths.

        The command carries the same PATH prefix :meth:`_ssh` gives every other
        remote command (#2433): this session is a non-login shell too, and a
        streamed dispatch that could not find ``docker`` or ``git`` would fail
        127 exactly as the verify job did.

        It carries the same keepalives too (:data:`SSH_KEEPALIVE_FLAGS`). A
        streamed session reads the child's stdout, so a peer that dies silently
        leaves that read waiting on a connection no packet will ever end —
        which is the one failure no ceiling downstream of the read can see.
        """
        require_gcloud()
        return [
            "gcloud",
            "compute",
            "ssh",
            self._settings.instance,
            "--command",
            remote_path_prefix() + command,
            *SSH_KEEPALIVE_FLAGS,
            *SSH_BOUNDARY_FLAGS,
            *self._instance_args(),
        ]

    def _ssh(
        self,
        command: str,
        *,
        timeout: float | None = _GCLOUD_TIMEOUT,
        interruptible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run *command* on the instance, under a known PATH.

        Every remote command goes through here, which is why the PATH prefix
        does too: ``--command`` is a non-interactive, non-login shell, so no
        profile has run and kew forwards no ambient PATH to a host
        (:mod:`kew.hosts.environment`). A job calling ``uv`` found nothing and
        exited 127 (#2433), and ``git clone`` would have been next on a host
        whose git is not where this shell's own PATH happens to point.

        Every session also carries ssh keepalives
        (:data:`SSH_KEEPALIVE_FLAGS`), so a connection whose peer has gone
        silently ends in about a minute rather than outliving the job it was
        carrying (#2465).
        """
        return self._compute(
            [
                "ssh",
                self._settings.instance,
                "--command",
                remote_path_prefix() + command,
                *SSH_KEEPALIVE_FLAGS,
                *SSH_BOUNDARY_FLAGS,
            ],
            timeout=timeout,
            interruptible=interruptible,
        )

    def status(self) -> str:
        """The instance's current status, as ``gcloud`` reports it."""
        result = self._compute(
            [
                "instances",
                "describe",
                self._settings.instance,
                "--format=value(status)",
            ],
            timeout=60.0,
        )
        if result.returncode != 0:
            raise HostError(
                f"cannot read the status of instance {self._settings.instance}: "
                f"{(result.stderr or '').strip()}"
            )
        return (result.stdout or "").strip().upper()

    def probe(self) -> None:
        """Read the instance's status: an expired login or a missing instance
        fails here, a stopped one does not (``ensure_up`` starts it)."""
        self.status()

    # --- the contract ----------------------------------------------------

    def _await_status(
        self,
        *,
        leaving: frozenset[str] | None,
        until: frozenset[str] | None,
        attempts: int = _POLL_ATTEMPTS,
        because: str = "",
    ) -> str:
        """Poll until the status leaves *leaving* or reaches one of *until*.

        *because* is appended to the error a spent budget raises. A status
        the operator has to act on is never just a status: it is a status
        reached on the way to something, and the wait that gave up knows
        what that something was.
        """
        status = self.status()
        for _ in range(attempts):
            if leaving is not None and status not in leaving:
                return status
            if until is not None and status in until:
                return status
            self._sleep(self._poll_seconds)
            status = self.status()
        wanted = " or ".join(sorted(until)) if until else "startable"
        raise HostError(
            f"instance {self._settings.instance} is still {status} after "
            f"{attempts} polls; it never became {wanted}{because}"
        )

    def _start(self, status: str) -> None:
        """Bring the instance up with the verb its *status* allows."""
        verb = "resume" if status in SUSPENDED_STATUSES else "start"
        result = self._compute(
            ["instances", verb, self._settings.instance], timeout=_GCLOUD_TIMEOUT
        )
        if result.returncode != 0:
            raise HostError(
                f"cannot {verb} instance {self._settings.instance} from "
                f"{status}: {(result.stderr or '').strip()}"
            )

    def _ssh_when_ready(self, command: str) -> subprocess.CompletedProcess[str]:
        """Run *command* over ssh, waiting out an instance whose sshd is not up.

        An instance reads ``RUNNING`` before it accepts logins, so the first
        ssh after a cold start is refused by the *connection*, and reporting
        that as an unreachable host refuses one that is merely still booting.
        Bounded, so a host that really is unreachable is still named.

        A refusal carrying the pre-shutdown nologin message is handed straight
        back: that window is pam's, not sshd's, and it ends when the stop
        fires rather than when the retries run out.
        """
        result = self._ssh(command)
        for _ in range(_SSH_READY_ATTEMPTS):
            if result.returncode != _SSH_FAILURE or _refused_by_nologin(result):
                return result
            self._sleep(self._poll_seconds)
            result = self._ssh(command)
        return result

    def cancel_pending_shutdown(self) -> None:
        """Cancel the idle stop a previous ``release()`` armed, and prove it.

        Either failure refuses the job, and each is named for what it is: an
        instance kew cannot reach at all, or one whose stop is still armed.
        Both leave a job shipped now liable to be stopped mid-run.

        One refusal is neither: a pending ``shutdown -h`` closes unprivileged
        logins five minutes before it fires, so the login that would cancel it
        is itself refused. There is nothing to retry — the window ends when
        the stop does — so the stop is waited out and the instance started
        again, which is what the operator's own scripts did by hand (#2411).
        """
        result = self._ssh_when_ready(_CANCEL_SHUTDOWN)
        if result.returncode == 0:
            return
        if _refused_by_nologin(result):
            self._restart_through_the_stop()
            result = self._ssh_when_ready(_CANCEL_SHUTDOWN)
            if result.returncode == 0:
                return
        self._refuse_to_ship(result)

    def _refuse_to_ship(self, result: subprocess.CompletedProcess[str]) -> NoReturn:
        """Name why this instance will not be shipped a job, by what refused.

        Three refusals, three different things for the operator to do, so the
        nologin one is not folded into "unreachable": an instance still going
        down after kew waited it out and started it again is a host someone
        else is stopping, not one with a broken route to it.
        """
        instance = self._settings.instance
        detail = (result.stderr or "").strip()
        if _refused_by_nologin(result):
            raise HostError(
                f"instance {instance} is still inside a pre-shutdown nologin "
                "window after kew waited its stop out and started it again, "
                "so kew cannot disarm the stop and will not ship a job it may "
                f"lose mid-run: {detail}"
            )
        if result.returncode == _SSH_FAILURE:
            raise HostError(
                f"instance {instance} cannot be reached over ssh, so kew "
                f"could not disarm its idle stop and will not ship a job it "
                f"may lose mid-run: {detail}"
            )
        raise HostError(
            f"instance {instance} still has a stop pending "
            f"({_SHUTDOWN_SENTINEL} or the {IDLE_STOP_UNIT} timer), so a job "
            f"shipped now could be stopped mid-run: {detail}"
        )

    def _stop_wait_attempts(self) -> int:
        """Polls enough to outlast the nologin window *and* the stop after it.

        Derived from this provider's own poll interval rather than fixed at
        :data:`_POLL_ATTEMPTS`, because the budget that matters is measured
        in seconds — the window is five minutes wide however often kew asks.
        """
        if self._poll_seconds <= 0.0:
            return _POLL_ATTEMPTS
        wanted = ceil(_STOP_BUDGET_SECONDS / self._poll_seconds)
        return max(_POLL_ATTEMPTS, wanted)

    def _restart_through_the_stop(self) -> None:
        """Let a stop kew cannot log in to cancel finish, then start again.

        The wait outlasts the nologin window and the stop behind it. A budget
        only as wide as the window expires while the instance is still
        ``RUNNING`` — the stop has not even begun — and the bare status error
        it raised named neither the pending stop nor the window (#2411).
        """
        self._await_status(
            leaving=None,
            until=STOPPED_STATUSES,
            attempts=self._stop_wait_attempts(),
            because=(
                ", so kew cannot start it again: it is inside a pre-shutdown "
                "nologin window no login of kew's can cancel, and the stop "
                "behind that window has not finished"
            ),
        )
        self._bring_up()

    def _bring_up(self) -> None:
        """Wait out a stop and start the instance, without touching its stops."""
        status = self.status()
        if status in STOPPING_STATUSES:
            status = self._await_status(leaving=STOPPING_STATUSES, until=None)
        if status in STARTABLE_STATUSES:
            self._start(status)
        if status != _RUNNING:
            self._await_status(leaving=None, until=frozenset({_RUNNING}))

    def _ensure_up(self) -> None:
        """Wait out a stop, start a stopped instance, disarm its idle stop, and
        take its lease.

        The lease comes after the disarm because a login inside a pending
        ``shutdown``'s nologin window is refused, and only the disarm knows how
        to wait that window out. A live job of another process's never has an
        idle stop armed — it disarmed it itself — so nothing it relies on is
        undone before the lease refuses this one.
        """
        self._bring_up()
        self.cancel_pending_shutdown()
        self._hold(self._deadline_seconds)

    def _hold(self, seconds: float) -> None:
        """Take or extend this instance's lease for *seconds* (#2627).

        :class:`HostBusy` while another kew process's lease is live: its
        operator may be gone, but the job it shipped is still running in the
        checkout a ship here would delete. A holder this machine can prove
        gone — an earlier boot of it, or a pid no longer running — is one
        exception (#2811). An expired foreign holder is the other (#2876).
        The first command reports either holder without changing the host.
        Its job is then stopped through one audited takeover path before any
        work ships.
        """
        lease_seconds = max(1, ceil(seconds))
        remember_boot(self._lock_dir)
        for _ in range(_LEASE_CHANGED_RETRIES):
            result = self._try_hold(lease_seconds)
            if result.returncode not in _READ_AGAIN_EXITS:
                break
        if result.returncode == 0:
            return
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        instance = self._settings.instance
        if result.returncode == LEASE_HELD_EXIT:
            raise HostBusy(
                f"instance {instance} is still leased to another kew job "
                f"({detail}): its operator may have exited, but the job it "
                "shipped runs until its deadline, so kew will not ship over it"
            )
        if result.returncode in _READ_AGAIN_EXITS:
            raise HostBusy(
                f"the lease of instance {instance} changed under every takeover "
                f"kew tried ({detail}), so it was not taken"
            )
        raise HostError(
            f"cannot take the lease and arm the {DEADLINE_STOP_UNIT} backstop "
            f"on instance {instance}, so a job shipped now would leave it "
            f"running if this process died: {detail}"
        )

    def _try_hold(self, seconds: int) -> subprocess.CompletedProcess[str]:
        """Take the lease once, taking a reported holder's over if permitted.

        A reported live holder must be provably lapsed. An expired holder can
        be foreign. :data:`LEASE_CHANGED_EXIT` means its lease changed before
        the takeover reached it, so the caller reads the lease again (#2874).
        """
        fenced = self._abandoned_takeovers()
        result = self._ssh(self._hold_command(seconds, fence=fenced))
        if result.returncode in (0, LEASE_HELD_EXIT):
            # Only a hold that ran has read the instance's takeover record,
            # and fenced the takeovers it settles by it.
            self._settle_dead_takeovers(_last_takeover_id(result), fenced)
        if result.returncode == LEASE_HELD_EXIT:
            expired = _expired_holder(result)
            if expired is not None:
                result = self._take_over(expired, seconds, expired=True)
            else:
                holder = _held_by(result)
                if holder is not None and lease_lapsed(holder, lock_dir=self._lock_dir):
                    result = self._take_over(holder, seconds)
        return result

    def _hold_command(
        self,
        seconds: int,
        *,
        lapsed: str | None = None,
        takeover: str | None = None,
        expired: bool = False,
        fence: Sequence[str] = (),
    ) -> str:
        return hold_command(
            lease_token(),
            seconds=seconds,
            idle_minutes=self._settings.idle_stop_minutes,
            lapsed=lapsed,
            takeover=takeover,
            expired=expired,
            fence=fence,
        )

    def _take_over(
        self, holder: str, seconds: int, *, expired: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Stop *holder*'s whole job and take its lease, and say so.

        The takeover is pending on the ledger before the instance is asked
        for it and settled as soon as it answers (#2874), so no kill in
        between loses it: the next hold settles it (:meth:`_settle_dead_takeovers`).
        With no ledger installed it is refused before the instance is asked:
        a takeover nobody can read about is not taken.
        """
        instance = self._settings.instance
        if self._ledger is None:
            raise HostError(
                f"instance {instance} is leased to {holder}, whose lease can "
                "be taken over, but nothing is installed to record "
                "a takeover of its lease, so it was not taken"
            )
        takeover = uuid.uuid4().hex
        with _in_flight(takeover):
            self._write_ledger(
                lambda ledger: ledger.pending(holder, takeover),
                f"cannot record the takeover of the lease of {holder} on instance "
                f"{instance}, so it was not taken",
            )
            try:
                result = self._ssh(
                    self._hold_command(
                        seconds,
                        lapsed=holder,
                        takeover=takeover,
                        expired=expired,
                    )
                )
            except HostError:
                # No answer at all: the instance's marker is the only word.
                self._settle_from_marker(holder, takeover)
                raise
            self._settle_own_takeover(holder, takeover, result)
        return result

    def _settle_own_takeover(
        self, holder: str, takeover: str, result: subprocess.CompletedProcess[str]
    ) -> None:
        """Write down what the instance answered *takeover* (#2874)."""
        instance = self._settings.instance
        if result.returncode == 0:
            self._lapsed_holders.append(holder)
            logger.warning(
                "instance %s was leased to %s; its whole job was stopped and "
                "the lease taken over: %s",
                instance,
                holder,
                (result.stdout or "").strip(),
            )
            self._write_ledger(
                lambda ledger: ledger.settled(
                    holder, takeover, taken_by=lease_token(), confirmed=True
                ),
                f"took over the lease of {holder} on instance {instance} but "
                "could not record it",
            )
        elif result.returncode in (
            LEASE_HELD_EXIT,
            LEASE_CHANGED_EXIT,
            TAKEOVER_FENCED_EXIT,
        ):
            # Refused before the lease was written: nothing was taken. A lease
            # that changed since it was read is no takeover either; it is read
            # again (:meth:`_hold`). A fenced one was already settled by the
            # hold that fenced it, so this answer is the same one (#2978).
            changed = result.returncode == LEASE_CHANGED_EXIT
            self._write_ledger(
                lambda ledger: ledger.refused(holder, takeover, lease_changed=changed),
                f"could not record that instance {instance} refused the "
                f"takeover of the lease of {holder}",
            )
        else:
            # Failed somewhere — before the lease, or after it and the marker
            # (a ``systemd-run`` that could not arm the backstop), or in ssh
            # itself — so the hold's own exit cannot say which.
            self._settle_from_marker(holder, takeover)

    def _settle_from_marker(self, holder: str, takeover: str) -> None:
        """Settle *takeover* by what the instance's :data:`TAKEOVER_FILE` says.

        Taken when the marker names it; otherwise not confirmed (#2874). The
        read fences *takeover* under the same lock hold, so a session of it
        still running cannot take the lease after this said it did not
        (#2978). A read that failed fenced nothing, so the takeover is left
        pending: a session still running may yet take the lease, and the
        next hold fences it before settling it (:meth:`_settle_dead_takeovers`).
        """
        fenced, marker = self._read_takeover_marker(takeover)
        if not fenced:
            return
        confirmed = marker == takeover
        if confirmed:
            self._lapsed_holders.append(holder)
        self._write_ledger(
            lambda ledger: ledger.settled(
                holder, takeover, taken_by=lease_token(), confirmed=confirmed
            ),
            f"could not record how the takeover of the lease of {holder} on "
            f"instance {self._settings.instance} ended",
        )

    def _read_takeover_marker(self, fence: str) -> tuple[bool, str | None]:
        """Whether *fence* was fenced off, and the id :data:`TAKEOVER_FILE` names.

        Both come from one lock hold (:func:`_fence_takeovers`); only a
        command that succeeded proves the fence was written (#2978).
        """
        try:
            result = self._ssh(
                _under_lease_lock(_last_takeover() + _fence_takeovers([fence]))
            )
        except HostError as exc:
            self._warn_unfenced(fence, str(exc))
            return False, None
        if result.returncode != 0:
            self._warn_unfenced(
                fence, ((result.stdout or "") + (result.stderr or "")).strip()
            )
            return False, None
        return True, _last_takeover_id(result)

    def _warn_unfenced(self, fence: str, detail: str) -> None:
        logger.warning(
            "could not fence takeover %s on instance %s, so it stays pending "
            "until a later hold fences it: %s",
            fence,
            self._settings.instance,
            detail,
        )

    def _abandoned_takeovers(self) -> list[str]:
        """The pending takeovers nobody is settling now, for a hold to fence."""
        abandoned: list[str] = []
        if self._ledger is None:
            return abandoned
        self._write_ledger(
            lambda ledger: abandoned.extend(
                row.takeover for row in ledger.unsettled() if self._abandoned(row)
            ),
            f"cannot read the lease takeovers pending on instance "
            f"{self._settings.instance}",
        )
        return abandoned

    def _settle_dead_takeovers(self, seen: str | None, fenced: Sequence[str]) -> None:
        """Settle every pending takeover the hold that read *seen* fenced.

        Its operator died, or its settle failed, between asking the instance
        and writing the answer down, so the instance's own
        :data:`TAKEOVER_FILE` — *seen*, the last takeover id there — says
        whether it went through. A takeover the file no longer names (a stop
        clears ``/run``) is settled unconfirmed rather than dropped. Only the
        *fenced* ones are: the hold put them in :data:`TAKEOVER_FENCE_FILE` in
        the same lock hold as it read *seen*, so a detached session of one
        that runs later is refused rather than proving the record wrong
        (#2978). Which rows those are is :meth:`_abandoned`'s call.
        """
        ledger = self._ledger
        if ledger is None or not fenced:
            return

        def settle(ledger: TakeoverLedger) -> None:
            for row in ledger.unsettled():
                if row.takeover not in fenced:
                    continue
                ledger.settled(
                    row.holder,
                    row.takeover,
                    taken_by=row.taken_by,
                    confirmed=row.takeover == seen,
                )

        self._write_ledger(
            settle,
            f"cannot settle the lease takeovers pending on instance "
            f"{self._settings.instance}",
        )

    def _abandoned(self, row: PendingTakeover) -> bool:
        """Whether nobody alive is settling the pending takeover *row*.

        With this host's exclusive claim held, every takeover of this
        machine's boot is: one is only asked for inside that claim, so a
        process that took one over and failed to write its answer — alive or
        not — is no longer settling it (#2978). Another machine's is settled
        only once its holder is proved gone.
        """
        if _is_in_flight(row.takeover):
            return False
        if row.taken_by == lease_token():
            return True
        if self._held and _of_this_boot(row.taken_by):
            return True
        return lease_lapsed(row.taken_by, lock_dir=self._lock_dir)

    def _write_ledger(
        self, write: Callable[[TakeoverLedger], None], failure: str
    ) -> None:
        """Run *write* on the ledger; a failure fails the step (#2874).

        A takeover nobody can read about is refused: the step fails with this
        provider holding whatever lease it has, which its release then drops.
        """
        if self._ledger is None:
            return
        try:
            write(self._ledger)
        except Exception as exc:
            raise HostError(f"{failure}: {exc}") from exc

    def lapsed_holders(self) -> tuple[str, ...]:
        """Every dead holder whose lease this provider took over (#2811)."""
        return tuple(self._lapsed_holders)

    def record_takeovers_in(self, ledger: TakeoverLedger) -> None:
        """Put each takeover on *ledger*, pending then settled (#2874)."""
        self._ledger = ledger

    def release_holder(self, holder: str, *, force: bool = False) -> None:
        """Drop *holder*'s lease by hand, refusing a live holder on this machine.

        A holder on another machine cannot be checked from here, so dropping
        it is the operator's call; one of this machine whose pid is running is
        refused, because its job may be live and the lease is what keeps the
        next ship off its checkout — unless it names an earlier boot, which
        only a readable boot id of this one can show. A token with no boot in
        it, from a kew older than #2811, is judged by its pid alone. The job the dropped
        holder left running on the instance is stopped with it, and a job
        that survives the kill, or that no scope record names, keeps its
        lease unless *force* drops it without stopping anything
        (:func:`drop_holder_command`). Only a stopped instance has no lease
        left to drop; one suspended, or in any other state but running, is
        refused, because a suspend keeps the lease (#2874).
        """
        pid = live_holder_pid(holder)
        if pid is not None:
            raise HostBusy(
                f"{holder} may be a live kew process on this machine (pid {pid}); "
                f"its lease on instance {self._settings.instance} stays"
            )
        with self.claim():
            status = self.status()
            if status in STOPPED_STATUSES:
                # The lease is on the instance's /run, which a stop clears.
                return
            if status != _RUNNING:
                # A suspend keeps memory, /run and its lease with it (#2874).
                raise HostError(
                    f"instance {self._settings.instance} is {status}, not "
                    f"stopped, so the lease of {holder} is still on it and "
                    "cannot be dropped over ssh; resume it, or stop it, and "
                    "run this again"
                )
            result = self._ssh(drop_holder_command(holder, force=force))
        if result.returncode != 0:
            detail = ((result.stdout or "") + (result.stderr or "")).strip()
            hint = (
                "; --force drops it anyway and stops nothing"
                if result.returncode == JOB_SURVIVES_EXIT
                else ""
            )
            raise HostError(
                f"cannot drop the lease of {holder} on instance "
                f"{self._settings.instance}: {detail}{hint}"
            )

    def _copy(self, source: Path, destination: str) -> None:
        """Copy one file up, retrying once — a scp that fails once usually works.

        Keepalived like every other session (:data:`SCP_KEEPALIVE_FLAGS`): a
        bundle copy to a peer that has gone away is the same half-open
        connection as a job's, and a retry only happens once the first attempt
        has actually ended.
        """
        attempts: list[str] = []
        for _ in range(2):
            result = self._compute(
                [
                    "scp",
                    str(source),
                    f"{self._settings.instance}:{destination}",
                    *SCP_KEEPALIVE_FLAGS,
                    *SCP_BOUNDARY_FLAGS,
                ]
            )
            if result.returncode == 0:
                return
            attempts.append((result.stderr or "").strip())
        raise HostError(
            f"cannot copy {source} to instance {self._settings.instance} after "
            f"2 attempts: {' | '.join(attempts)}"
        )

    def _ship(self, bundle_path: Path) -> None:
        """Copy the git bundle to the instance and clone it there."""
        bundle = Path(bundle_path)
        if not bundle.is_file():
            raise HostError(f"git bundle not found: {bundle}")
        # Before anything is deleted: the checkout removed here may be the one
        # a job of a dead operator's is still running in (#2627).
        self._hold(self._deadline_seconds)
        self._remote_step(_PREPARE_SHIP, "prepare the remote workspace on")
        self._copy(bundle, _REMOTE_BUNDLE)
        self._remote_step(clone_command(), "clone the shipped bundle on")

    def run_recipe(
        self, recipe: Path, *, timeout_seconds: float | None
    ) -> tuple[int, str]:
        """Copy a provisioning recipe to the instance and run it as root.

        Not one of the four job verbs: a recipe is not a shipped bundle and it
        runs *above* the checkout, as root, to install what a verification job
        needs (:mod:`kew.hosts.provisioning`). The outcome is read the same way
        a job's is — off the explicit marker on **stdout**, so a recipe that
        prints to stderr cannot forge its own result, and a missing marker is
        an unknown outcome rather than a clean zero.

        ``sudo -n`` on purpose: a recipe that stops at a password prompt would
        hold this host's lock for its whole ceiling and then time out with
        nothing installed. Refusing immediately names the problem instead.

        The recipe is handed ``KEW_JOB_PATH``: the PATH this provider gives a
        *job*, already expanded by the login's own shell, so the recipe's
        self-check looks for the tools where a job will look for them rather
        than wherever ``sudo`` happens to point root. Without it a host could
        pass provisioning — root finding every tool on ``secure_path`` — and
        still fail every job with 127 (#2433).

        The ceiling runs the session a job's does (:meth:`_ssh_bounded`), under
        the same deadline when the caller named none. A recipe runs as **root**,
        so a ceiling that ended only the ``gcloud`` wrapper would leave an apt
        transaction going on an instance kew had just released and armed the
        idle stop on — a half-provisioned host, and no record of why (review of
        #2475).
        """
        script = Path(recipe)
        if not script.is_file():
            raise HostError(f"provisioning recipe not found: {script}")
        ceiling = self._job_ceiling(timeout_seconds)
        with self.claim():
            self._hold(ceiling + _LEASE_GRACE_SECONDS)
            self._remote_step(
                f"mkdir -p {REMOTE_WORKSPACE}", "prepare the remote workspace on"
            )
            self._copy(script, _REMOTE_RECIPE)
            # The pins travel with the recipe, in the same directory it lands
            # in: the recipe reads the scanner version out of this file rather
            # than carrying a literal of its own, so a host provisioned here
            # installs exactly the trivy `scripts/scan-agent-image.sh` demands
            # (#2462). Without it the recipe refuses rather than installing an
            # unpinned scanner.
            self._copy(pin_path(), _REMOTE_PIN)
            self._ran_job = True
            try:
                result = self._ssh_bounded(
                    marker_command(
                        job_session_record(lease_token())
                        + f'sudo -n env KEW_JOB_PATH="$PATH" bash {_REMOTE_RECIPE}'
                    ),
                    timeout=ceiling,
                )
            except HostTimeout as exc:
                raise HostTimeout(
                    f"the recipe {script.name} exceeded its {ceiling}s "
                    f"ceiling on instance {self._settings.instance}, so the "
                    "host is half-provisioned and reports no exit code"
                ) from exc
        self._refuse_unrecorded(result)
        exit_code, log = parse_exit_marker(result.stdout or "")
        return exit_code, log + (result.stderr or "")

    def _refuse_unrecorded(self, result: subprocess.CompletedProcess[str]) -> None:
        """Raise when the job never started because its scope went unrecorded."""
        if result.returncode != JOB_UNRECORDED_EXIT or EXIT_MARKER in (
            result.stdout or ""
        ):
            return
        # Nothing ran, so the release has no scope of this job to find.
        self._ran_job = False
        raise HostError(
            f"the job was not started on instance {self._settings.instance}: "
            f"{(result.stderr or '').strip()}"
        )

    def _remote_step(self, command: str, description: str) -> None:
        result = self._ssh(command)
        if result.returncode != 0:
            raise HostError(
                f"cannot {description} instance {self._settings.instance}: "
                f"{(result.stderr or '').strip()}"
            )

    def _job_ceiling(self, requested: float | None) -> float:
        """The ceiling a job really runs under: the caller's, or this host's.

        A caller that named a ceiling gets exactly it, however long. The
        deadline does **not** cut it down: cutting it would end a job that was
        merely slow and attest the result as a lost connection, so a host whose
        deadline sat below ``[verify] timeout_seconds`` would silently shorten
        every verification run on it (review of #2475). What the ceiling needs
        from this host is not a smaller number but a kill that really ends the
        session (:meth:`_ssh_bounded`).

        What is left to bound is a caller with no ceiling at all — ``None`` is
        unbounded on the local host — and an unbounded wait on a session that
        will never end on its own holds this host's lock for ever (#2465). That
        is the one the deadline answers.
        """
        return self._deadline_seconds if requested is None else requested

    def _outran(self, job: Job, ceiling: float) -> HostError:
        """Which ceiling *job* outran, and therefore what its outcome is.

        The caller's own ceiling is a verdict about the job: it asked for N
        seconds and the job took longer, reported against that number. This
        host's deadline is a verdict about the *session*: it bounds only a job
        that named no ceiling, and it sits well above a full verification, so
        reaching it means the session stopped making progress — an interruption,
        retryable, and never a test failure.
        """
        if job.timeout_seconds is not None:
            return HostTimeout(
                f"{job.name!r} exceeded its {job.timeout_seconds}s ceiling on "
                f"instance {self._settings.instance}, so it has no exit code"
            )
        return HostJobDeadlineExceeded(
            f"{job.name!r} was still running after the {ceiling}s deadline of "
            f"instance {self._settings.instance} "
            "([hosts.gcp] job_timeout_minutes), so its session was killed and "
            "the host released. The connection is presumed lost and the job "
            "reached no verdict: this is NOT a test failure"
        )

    def _ssh_bounded(
        self, command: str, *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run *command* over ssh under a ceiling that ends the whole session.

        Its own method because the ceiling is the job's, not the default one
        :meth:`_ssh` gives a step. ``subprocess.run(timeout=...)`` kills the
        **pid it started** and nothing else. That call does return — on POSIX it
        waits for that one pid, which it has just killed — but the wrapper's
        ``ssh`` child survives it, still connected to the instance with the job
        still running on it, while kew releases this host's lock and arms its
        idle stop; the surviving child also holds the session's pipes open, so
        any read still on them waits on a pipe nothing will close (#2465). The
        session here is its own process group and the ceiling kills the group
        (:func:`_kill_session_group`), so the ceiling ends the job and not just
        kew's view of it — and since #2811 every other gcloud call is run the
        same way (:meth:`_session`).

        The argv is :meth:`ssh_argv`, which is the argv :meth:`_ssh` builds, so
        a job's session and every other session carry the same PATH prefix and
        the same keepalives rather than drifting apart.
        """
        return self._session(self.ssh_argv(command), timeout=timeout)

    def _session(
        self, argv: list[str], *, timeout: float | None, interruptible: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* in a process group of its own, read to its end.

        An *interruptible* session is tracked while it runs, so
        :meth:`interrupt` can kill it from another thread, and none is opened
        once an interruption has begun.
        """
        if interruptible:
            self._refuse_if_interrupted()
        try:
            session = subprocess_utils.popen(
                argv,
                env=remote_session_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostError(
                f"cannot open a gcloud session to instance "
                f"{self._settings.instance}: {exc}"
            ) from exc
        if not interruptible:
            return _collect_session(session, timeout=timeout)
        with self._sessions_lock:
            self._sessions.add(session)
        try:
            if self._interrupted.is_set():
                _kill_session_group(session)
            result = _collect_session(session, timeout=timeout)
        finally:
            with self._sessions_lock:
                self._sessions.discard(session)
        self._refuse_if_interrupted()
        return result

    def _refuse_if_interrupted(self) -> None:
        if self._interrupted.is_set():
            raise HostInterrupted(
                f"the job on instance {self._settings.instance} was interrupted: "
                "kew is exiting, so its session was ended and the host released"
            )

    def interrupt(self) -> None:
        """Kill every gcloud session in flight here, and refuse any after it.

        Each session is in a process group of its own, so the signal that
        unwinds kew's main thread never reaches it, and the thread waiting on
        it would hold that unwinding off until the job finished (#2811).
        """
        self._interrupted.set()
        with self._sessions_lock:
            live = list(self._sessions)
        for session in live:
            _kill_session_group(session)

    def _execute(self, job: Job) -> tuple[int, str]:
        """Run the job over SSH and read its exit code off the remote marker.

        The marker is parsed from stdout and stderr is appended to the log
        afterwards, so nothing the job writes to stderr can be read as its
        outcome. The job's own ceiling bounds the call, and this host's deadline
        bounds that (:meth:`_job_ceiling`): a job with no ceiling of its own is
        still not allowed to outlive a session that has stopped answering.
        """
        inner = (
            f"{job_session_record(lease_token())}"
            f"cd {REMOTE_CHECKOUT} && "
            f"{_environment_prefix(job.environment)}"
            f"sh -c {shlex.quote(job.command)}"
        )
        ceiling = self._job_ceiling(job.timeout_seconds)
        self._hold(ceiling + _LEASE_GRACE_SECONDS)
        self._ran_job = True
        try:
            result = self._ssh_bounded(marker_command(inner), timeout=ceiling)
        except HostTimeout as exc:
            raise self._outran(job, ceiling) from exc
        self._refuse_unrecorded(result)
        exit_code, log = parse_exit_marker(result.stdout or "")
        return exit_code, with_stderr(log, result.stderr or "")

    def _drop_hold(self) -> None:
        """Drop this process's lease on the instance, arming nothing.

        :data:`LEASE_HELD_EXIT` means the lease is already another process's,
        which is nothing to drop. Any other failure — a job or container of
        this process's not proved stopped among them (:func:`drop_command`) —
        leaves the lease to lapse at its deadline, so it is said rather than
        raised.
        """
        result = self._ssh(drop_command(lease_token()), interruptible=False)
        if result.returncode not in (0, LEASE_HELD_EXIT):
            logger.warning(
                "could not drop the lease on instance %s; it lapses at its "
                "deadline: %s",
                self._settings.instance,
                ((result.stdout or "") + (result.stderr or "")).strip(),
            )

    def _release(self) -> None:
        """Arm the host's own idle stop, so no job can leave it billing.

        A ``systemd`` timer rather than ``shutdown -h +N``: ``shutdown`` is
        the same delay with a nologin window on the end of it, and the next
        job's ``ensure_up`` arrives inside that window to find it cannot log
        in to disarm the stop this one armed (#2411). The timer holds the
        delay and closes no logins until it fires.

        The lease is dropped in the same command, and both are left alone while
        another process's lease is live (#2627): that process owns the
        instance's shutdown now, so this release is a no-op, as one that cannot
        take the operator-side lock already is. The job this process ran there
        is stopped first, and one that cannot be proved stopped keeps the lease
        and its backstop (#2811): a release after ``SIGTERM`` has only ended
        the operator's side of the job's session.
        """
        minutes = self._settings.idle_stop_minutes
        result = self._ssh(
            release_command(lease_token(), idle_minutes=minutes, ran_job=self._ran_job),
            interruptible=False,
        )
        if result.returncode == JOB_SURVIVES_EXIT:
            # Raised, not logged: a release that leaves the job running is a
            # host left leased, and a verify that passed must say so beside
            # its verdict rather than read as a clean run.
            raise HostError(
                f"not releasing instance {self._settings.instance}: the job "
                "this process ran there is not proved stopped "
                f"({(result.stdout or '').strip()}), so its lease holds until "
                f"its deadline and the {DEADLINE_STOP_UNIT} backstop stays armed"
            )
        if result.returncode == LEASE_HELD_EXIT:
            logger.warning(
                "not releasing instance %s: another kew job holds its lease "
                "and owns its shutdown (%s)",
                self._settings.instance,
                (result.stdout or "").strip(),
            )
            return
        if result.returncode != 0:
            raise HostError(
                f"cannot schedule the idle stop on instance "
                f"{self._settings.instance}; it may keep running and billing: "
                f"{(result.stderr or '').strip()}"
            )
        self._ran_job = False
