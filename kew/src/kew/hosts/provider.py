"""The host contract: four verbs, one job at a time, results as data.

A *host* is a machine kew ships a git bundle to and runs one job on. The
contract is deliberately narrow, because the trust boundary is:

* ``ensure_up()`` — the host is running and will stay running.
* ``ship(bundle_path)`` — a git bundle becomes a checkout on the host.
* ``run(job)`` — exactly one job runs, and returns a :class:`JobResult`.
* ``release()`` — the host stops, or schedules its own idle stop.

Code goes in as a bundle; results come back as **data** — an exit code, a
duration, and log text. Nothing a host returns is an instruction, and nothing
kew sends it is a credential (:mod:`kew.hosts.environment`).

Every one of the four verbs is wrapped by this base class so a provider
inherits the per-host lock — a subclass implements ``_ensure_up``, ``_ship``,
``_execute`` and ``_release`` and cannot forget it. The lock has to cover
``ensure_up()`` and ``release()`` too, not just the job: ``release()`` arms the
host's idle shutdown, and a process arming it on an instance another holder
has just claimed and disarmed would stop that holder's job mid-run.

The claim is held per *thread*, not per provider object. Two threads sharing
one provider are two jobs on one host, and contend for the lock exactly as two
processes do — only the thread already inside a claim re-enters it.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol

from kew.hosts.errors import HostBusy, HostError
from kew.hosts.lock import host_lock, host_lock_held

logger = logging.getLogger(__name__)


class PendingTakeover(NamedTuple):
    """A takeover put on record before it was attempted, and not yet settled."""

    takeover: str
    holder: str
    taken_by: str


class TakeoverLedger(Protocol):
    """Where one host's lease takeovers go on record, each step before the next.

    A takeover is *pending* before the host is asked for it, and *settled*
    once the host answers (#2874), so an operator killed between the two
    leaves a row the next one settles. Every method raises when its write or
    read fails, which fails the step that called it.
    """

    def pending(self, holder: str, takeover: str) -> None:
        """Record that *takeover* of *holder*'s hold is about to be asked for."""

    def settled(
        self, holder: str, takeover: str, *, taken_by: str, confirmed: bool
    ) -> None:
        """Record that *takeover* took *holder*'s hold for *taken_by*.

        *confirmed* is whether the host itself said so; a takeover settled
        for an operator that died before it heard the answer may not be.
        """

    def refused(
        self, holder: str, takeover: str, *, lease_changed: bool = False
    ) -> None:
        """Record that the host refused *takeover*: nothing was taken.

        *holder* keeps its hold, or — *lease_changed* — no longer held it by
        the time the host was asked, so the lease is read again.
        """

    def unsettled(self) -> list[PendingTakeover]:
        """Every takeover of this host still pending, oldest first."""


@dataclass(frozen=True)
class Job:
    """One shell command to run inside the shipped checkout."""

    #: A short label for logs and lock diagnostics.
    name: str

    #: The command, run by ``sh -c`` with the checkout as the working directory.
    command: str

    #: Variables the job declares. Credential-shaped names are refused
    #: (:func:`kew.hosts.environment.job_environment`).
    environment: Mapping[str, str] = field(default_factory=dict)

    #: Wall-clock ceiling. ``None`` leaves the job unbounded.
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class JobResult:
    """What a host returns: data, never an instruction.

    Both providers return this exact shape, so a caller cannot tell a local
    run from a remote one by its result type.
    """

    #: The :attr:`Job.name` that produced it.
    job: str

    #: The provider that ran it (``"local"``, ``"gcp"``).
    host: str

    #: The job's exit status. A provider that could not determine one raises
    #: instead of reporting a clean zero.
    exit_code: int

    #: Wall-clock seconds the job took, measured by kew around the call.
    seconds: float

    #: Combined stdout and stderr, as text.
    log: str

    @property
    def ok(self) -> bool:
        """True iff the job exited zero."""
        return self.exit_code == 0


class HostProvider(ABC):
    """A machine kew can ship a bundle to and run one job on."""

    #: Provider name, as it appears in a :class:`JobResult` and in config.
    name: str = "host"

    #: Whether this host can build kew's images from private inputs. Only the
    #: operator machine holds the pinned Switchyard source archive, and it
    #: never leaves it, so a remote host's job is told to skip that layer
    #: rather than being handed the input.
    builds_private_images: bool = False

    #: What an operator runs when :meth:`probe` cannot reach this host, for a
    #: notice that names the fix rather than only the failure.
    unreachable_fix: str = ""

    def __init__(self, *, lock_dir: Path) -> None:
        self._lock_dir = Path(lock_dir)
        self._claim_state = threading.local()

    @property
    def _held(self) -> bool:
        """Whether *this thread* is already inside this provider's claim.

        Thread-local on purpose: a plain flag on the object would let a second
        thread sharing the provider skip the lock on the strength of the first
        thread's claim and run a job alongside it.
        """
        return bool(getattr(self._claim_state, "held", False))

    @property
    def address(self) -> str:
        """The machine this provider addresses, for a report a person reads.

        The provider's own name for a host there is only one of; a provider
        that can address more than one machine (the gcp pool a sharded verify
        spreads across) names the one it holds, so a per-shard report says
        which instance answered.
        """
        return self.name

    @property
    def resource_key(self) -> str:
        """This exact host, unambiguously, for a record that outlives the process.

        :attr:`address` is a short name for a person, which two hosts of one
        provider can share (an instance of one name in two projects); this
        names the provider and everything that tells them apart (#2874).
        """
        return f"{self.name}/{self.address}"

    @property
    @abstractmethod
    def lock_key(self) -> str:
        """The identity two kew processes contend on for this exact host."""

    @abstractmethod
    def _ensure_up(self) -> None:
        """Make the host ready to receive a bundle, waiting out a stop."""

    @abstractmethod
    def _ship(self, bundle_path: Path) -> None:
        """Put the git bundle at *bundle_path* on the host as a checkout."""

    @abstractmethod
    def _release(self) -> None:
        """Stop the host, or schedule the idle stop that will."""

    @abstractmethod
    def _execute(self, job: Job) -> tuple[int, str]:
        """Run *job* on the host and return its exit code and log text."""

    def _hold(self, seconds: float) -> None:
        """Keep the host held for *seconds* even if this process dies.

        The operator-side lock dies with its process; a remote host's job does
        not. A provider whose jobs outlive kew records the hold on the host
        itself (#2627). The operator machine's jobs are its own children, so
        the default holds nothing beyond the lock.
        """
        return None

    def _drop_hold(self) -> None:
        """Give up this process's hold on the host, arming nothing."""
        return None

    def probe(self) -> None:
        """Raise :class:`HostError` unless the host can be reached right now.

        Cheap and side-effect free: it starts nothing, claims nothing and ships
        nothing. The operator machine is always reachable, so the default
        answers at once.
        """
        return None

    def interrupt(self) -> None:
        """End this provider's jobs in flight, and refuse any not yet started.

        Called from another thread — the one a ``SIGTERM`` unwinds — so the
        threads running jobs here reach their release promptly rather than
        once every job has finished. The operator machine's jobs are its own
        children, which the signal's unwinding already ends.
        """
        return None

    def lapsed_holders(self) -> tuple[str, ...]:
        """Every dead holder whose hold on the host this provider took over.

        Only a provider that records its hold on the host itself can find one
        (#2811); the operator machine's jobs die with their operator.
        """
        return ()

    #: Whether this provider can take over a dead holder's hold on the host,
    #: and so needs a :class:`TakeoverLedger` before it is handed any work.
    takes_over_holds: bool = False

    def record_takeovers_in(self, ledger: TakeoverLedger) -> None:
        """Put every takeover of a dead holder's hold on *ledger* as it happens.

        Before anything ships, so a takeover followed by a reboot or a
        ``SIGKILL`` is already on record (#2874); a *ledger* that raises fails
        the step that took over. The operator machine takes over nothing, so
        it never writes to *ledger*.
        """
        return None

    def release_holder(self, holder: str, *, force: bool = False) -> None:
        """Drop the hold *holder* has on the host, by hand.

        The operator machine records no hold beyond its lock, which dies with
        its process, so there is nothing here to drop.
        """
        raise HostError(f"the {self.name} host records no hold to release")

    def held_by_a_job(self) -> bool:
        """Whether a job — in any kew process — holds this host right now.

        Asked by a caller choosing a host rather than using one, so it never
        takes the lock (:func:`~kew.hosts.lock.host_lock_held`). The answer is
        a snapshot; :meth:`claim` stays what actually keeps two jobs apart.
        """
        return host_lock_held(self._lock_dir, self.lock_key)

    @contextmanager
    def claim(self) -> Iterator[None]:
        """Hold this host for a whole ``ensure_up``/``ship``/``run`` session.

        ``run()`` claims the host on its own, which is enough for one job. A
        caller that ships first wants the claim to span the copy too: two
        processes shipping to one instance would clone over each other's
        checkout before either job started. Re-entrant for the holding
        *thread*, so ``run()`` inside a claim does not contend with its own
        holder; another thread on the same provider object does contend, being
        a second job on one host.
        """
        if self._held:
            yield
            return
        with host_lock(self._lock_dir, self.lock_key):
            self._claim_state.held = True
            try:
                yield
            finally:
                self._claim_state.held = False

    def ensure_up(self) -> None:
        """Make the host ready, under this host's exclusive lock.

        Claimed like the job itself: starting an instance, waiting out its
        stop and cancelling its pending shutdown are all writes to a host
        another process may be mid-job on. :class:`HostBusy` says so rather
        than reaching in.
        """
        with self.claim():
            self._ensure_up()

    def release(self) -> None:
        """Release the host under its lock, or leave it to the holder that has it.

        A ``release()`` that cannot claim the host is a **no-op**, not an
        error: another process holds it now, which means that process disarmed
        the idle shutdown for a job of its own and owns arming it again.
        Scheduling a stop here is exactly how a departing process stops
        someone else's job mid-run. A provider's own cleanup is skipped with
        it, which can cost a temporary directory; arming the stop would cost a
        running job.
        """
        with ExitStack() as holding:
            try:
                holding.enter_context(self.claim())
            except HostBusy as exc:
                logger.warning(
                    "not releasing host %s: it is held by another job now, "
                    "which owns its shutdown (%s)",
                    self.lock_key,
                    exc,
                )
                return
            self._release()

    def hold(self, seconds: float) -> None:
        """Hold the host, under its lock, for *seconds* past this moment.

        For a caller that runs work on the host outside :meth:`run` — a placed
        agent's container — and knows its deadline: the host's own record of
        the hold is what stops the next process shipping over that work if
        this one is killed mid-run. :class:`HostBusy` if another job's hold is
        still live.
        """
        with self.claim():
            self._hold(seconds)

    def drop_hold(self) -> None:
        """Give up this job's hold, under the lock, WITHOUT arming a stop.

        For a release that is skipped because another run is attached to the
        host: the stop is that run's to arm, but a hold left in place would
        refuse it the host until this job's deadline had passed.
        """
        with self.claim():
            self._drop_hold()

    def ship(self, bundle_path: Path) -> None:
        """Ship the bundle while holding the host, then leave it shipped.

        Claimed for the same reason ``run()`` is: two processes copying into
        one instance would clone over each other's checkout before either job
        started, and neither would know.
        """
        with self.claim():
            self._ship(Path(bundle_path))

    def run(self, job: Job) -> JobResult:
        """Run *job* under this host's exclusive lock and time it."""
        with self.claim():
            started = time.monotonic()
            exit_code, log = self._execute(job)
            elapsed = time.monotonic() - started
        return JobResult(
            job=job.name,
            host=self.name,
            exit_code=exit_code,
            seconds=elapsed,
            log=log,
        )
