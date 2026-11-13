"""Host-provider failures. Every one of them is loud and names its subject."""

from __future__ import annotations


class HostError(RuntimeError):
    """A host could not do what was asked of it."""


class HostBusy(HostError):
    """Another job already holds this host's lock.

    One job at a time per host is kew's own invariant, not the cloud's: two
    jobs sharing a checkout on one instance would read each other's tree.
    """


class HostNotProvisioned(HostError):
    """``run()`` was called before ``ship()`` put a checkout on the host."""


class CredentialVariableRefused(HostError):
    """A requested job variable is credential-shaped, so it is not passed.

    Refused by NAME, never by value: a host receives no GitHub, Linear, Google
    or model credential even when a caller asks for one. The message names the
    variable so the caller can see which one was dropped.
    """

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(
            f"refusing to pass {name} to a host: {reason}. A host receives code "
            "as a git bundle and returns results as data; it never holds a "
            "GitHub, Linear, Google or model credential."
        )
        self.name = name
        self.reason = reason


class HostTimeout(HostError):
    """A host command did not finish inside its ceiling, so it has no exit code.

    Distinct from :class:`HostError` because the caller knows which ceiling it
    asked for: a job that outran its own ``timeout_seconds`` is reported
    against that number, not as "the host could not be reached".
    """


class VariableNameRefused(HostError):
    """A requested job variable is not named like a variable, so it is refused.

    A remote job's declared variables are interpolated into an ``env
    NAME=value`` prefix on the host's shell command line. A *value* can be
    quoted; a *name* cannot — ``env 'A;B'=1`` is not an assignment — so a name
    outside the portable character set is refused rather than escaped.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"refusing to pass {name!r} to a host: a job variable name must be "
            "a letter or underscore followed by letters, digits or "
            "underscores. A name is interpolated into the host's command "
            "line, where it cannot be quoted."
        )
        self.name = name


class HostInterrupted(HostError):
    """A job reached no verdict: the host, or the session carrying it, went away.

    Its own family on purpose, and never a test failure. A job that ran and
    exited non-zero is a verdict; a job whose session died is the *absence* of
    one, and reporting the second as the first attests a red gate nothing
    produced. Retryable for the same reason: nothing about the code under test
    is known yet, so the work to do is the job again and not a bisect.
    """

    #: Recorded against the run, so an interruption is distinguishable from a
    #: host that answered. Narrowed by each subclass.
    error_category = "host_interrupted"


class HostConnectionLost(HostInterrupted):
    """The session ended with no exit marker, so the connection was lost.

    What a half-open TCP connection looks like from this end (#2465): the local
    ``ssh`` reports an end — or none at all — and the remote marker that would
    have carried the job's own exit code never arrives. ``gcloud``'s status
    cannot tell a failed job from a failed connection, so the missing marker is
    the only honest reading and it reads as an interruption.
    """

    error_category = "host_connection_lost"


class HostJobDeadlineExceeded(HostInterrupted):
    """The job outlived the host's overall deadline and its session was killed.

    Distinct from :class:`HostTimeout`, which is a job outrunning the ceiling
    its *caller* asked for and is reported against that number. This is the
    host's own backstop (``[hosts.gcp] job_timeout_minutes``), which bounds a
    job that named **no** ceiling of its own and is set well above a full
    verification: reaching it means the session stopped making progress rather
    than that the job was slow, so the outcome is an interruption and the next
    thing to do is run it again. A ceiling the caller did name is never cut down
    to it, because cutting it would report a merely slow job as this.
    """

    error_category = "host_job_deadline_exceeded"
