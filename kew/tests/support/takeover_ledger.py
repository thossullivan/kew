"""An in-memory :class:`~kew.hosts.provider.TakeoverLedger`, for provider tests.

A gcp provider refuses to take a dead holder's lease over with no ledger
installed (#2874). A test about the takeover itself, not its audit trail,
installs this one and can read back what went on record, in order; a
takeover it holds pending is one this process asked for, which a later hold
settles.
:func:`dead_holder` is a lease token such a takeover goes ahead for.
"""

from __future__ import annotations

import subprocess

from kew.hosts.gcp import lease_token, parse_lease_token
from kew.hosts.provider import PendingTakeover


def dead_holder() -> str:
    """A lease token of this machine's whose pid has been reaped: a dead holder."""
    child = subprocess.Popen(["true"])
    child.wait()
    me = parse_lease_token(lease_token())
    assert me is not None, "this machine's boot id is unreadable"
    return f"{me.machine}-{child.pid}-{me.boot}-{'0' * 12}"


class MemoryTakeoverLedger:
    """Every takeover write, as ``(kind, holder, takeover_id)``, in order."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    def pending(self, holder: str, takeover: str) -> None:
        self.writes.append(("pending", holder, takeover))

    def settled(
        self, holder: str, takeover: str, *, taken_by: str, confirmed: bool
    ) -> None:
        kind = "settled" if confirmed else "unconfirmed"
        self.writes.append((kind, holder, takeover))

    def refused(
        self, holder: str, takeover: str, *, lease_changed: bool = False
    ) -> None:
        kind = "lease_changed" if lease_changed else "refused"
        self.writes.append((kind, holder, takeover))

    def unsettled(self) -> list[PendingTakeover]:
        """Every pending takeover no later write settled, as this process's."""
        settled = {takeover for kind, _, takeover in self.writes if kind != "pending"}
        return [
            PendingTakeover(takeover, holder, lease_token())
            for kind, holder, takeover in self.writes
            if kind == "pending" and takeover not in settled
        ]
