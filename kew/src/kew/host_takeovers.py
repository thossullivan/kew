"""Host lease takeovers on the audit trail, for every caller that leases a host.

A provider that can take a dead holder's lease over (:attr:`~kew.hosts.provider.
HostProvider.takes_over_holds`) refuses to unless a :class:`~kew.hosts.provider.
TakeoverLedger` is installed (#2874). This is the one that writes to the run
store: ``kew verify``, the mutation report, remote dispatch and ``kew hosts
provision`` all install it before they touch the host.

A takeover is *pending* before the host is asked for it and settled the moment
it answers — :data:`LEASE_LAPSED_EVENT` when the host took it,
:data:`LEASE_TAKEOVER_REFUSED_EVENT` when it refused, and
:data:`LEASE_TAKEOVER_UNCONFIRMED_EVENT` when the operator that asked died
first and the host no longer says. All of them carry the pending row's
``takeover_id``, and one takeover is settled once, whoever settles it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from kew.audit import emit, resolve_actor
from kew.hosts.errors import HostError
from kew.hosts.gcp import lease_token
from kew.hosts.provider import HostProvider, PendingTakeover

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore

#: A dead holder's host lease was taken over (#2811), as the host itself said.
#: The payload names the dead holder and the process that took over.
LEASE_LAPSED_EVENT = "host_lease_lapsed"

#: A takeover about to be asked of the host, written before it is (#2874).
LEASE_TAKEOVER_PENDING_EVENT = "host_lease_takeover_pending"

#: The host refused a pending takeover: the holder keeps its lease, or —
#: ``lease_changed`` in the payload — no longer held it when the host was
#: asked (it expired or was released since it was read), so nothing was taken.
LEASE_TAKEOVER_REFUSED_EVENT = "host_lease_takeover_refused"

#: A pending takeover whose operator died before it heard the answer, and
#: which the host no longer names (a stop clears it): it may or may not have
#: gone through, and is not reported as taken.
LEASE_TAKEOVER_UNCONFIRMED_EVENT = "host_lease_takeover_unconfirmed"

#: Every event that settles a pending takeover.
SETTLING_EVENTS = (
    LEASE_LAPSED_EVENT,
    LEASE_TAKEOVER_REFUSED_EVENT,
    LEASE_TAKEOVER_UNCONFIRMED_EVENT,
)


def record_lapsed_leases(
    store: RunStore | None,
    providers: Sequence[HostProvider],
    *,
    config: DispatchConfig,
) -> None:
    """Put every takeover of a dead holder's host lease on the audit trail.

    Each one is pending before the host is asked for it and settled the moment
    it answers, before anything ships (#2874): an event left for the job's end
    was never written when a reboot or ``SIGKILL`` ended it, and one written
    after the host answered was lost to a kill in between. A write that fails
    fails the step that took over, so no job runs under a takeover that is not
    on record. A worker thread writes through a handle of its own
    (:func:`_ledger_for_this_thread`).

    :class:`HostError` when a provider that can take over has no store to
    record it in, or one that will not take a write now
    (:func:`_prove_writable`): a takeover that cannot be recorded is not
    taken, and that is known before any host is touched.
    """
    owner = threading.current_thread()
    if any(provider.takes_over_holds for provider in providers):
        _prove_writable(store, providers[0])
    for provider in providers:
        provider.record_takeovers_in(
            AuditTakeoverLedger(store, provider, config=config, owner=owner)
        )


def _prove_writable(store: RunStore | None, provider: HostProvider) -> None:
    """Refuse a *store* that cannot write a takeover's audit row right now.

    A rolled-back audit insert, before any host is touched (#2874): a full or
    read-only ledger found only at the takeover would fail the step after
    the host had already been brought up and its lease changed.
    """
    reason = None
    if store is None:
        reason = "there is no run store to record it in"
    else:
        probe = getattr(store, "probe_audit_write", None)
        if probe is None:
            reason = "the run store keeps no audit trail"
        else:
            try:
                probe()
            except Exception as exc:
                reason = f"the run store will not take a write: {exc}"
    if reason is not None:
        raise HostError(
            f"kew will not lease the {provider.name} host: a takeover of its "
            f"lease could not be recorded, because {reason}"
        )


class AuditTakeoverLedger:
    """One host's lease takeovers, as strict audit events (#2874)."""

    def __init__(
        self,
        store: RunStore | None,
        provider: HostProvider,
        *,
        config: DispatchConfig,
        owner: threading.Thread,
    ) -> None:
        self._store = store
        self._provider = provider
        self._config = config
        self._owner = owner

    def pending(self, holder: str, takeover: str) -> None:
        self._emit(
            LEASE_TAKEOVER_PENDING_EVENT,
            holder=holder,
            takeover=takeover,
            taken_by=lease_token(),
        )

    def settled(
        self, holder: str, takeover: str, *, taken_by: str, confirmed: bool
    ) -> None:
        # Only the host's own word reads as "taken"; the confirmation is a
        # field of its own, never part of a text a console may cut short.
        event = LEASE_LAPSED_EVENT if confirmed else LEASE_TAKEOVER_UNCONFIRMED_EVENT
        self._emit(
            event,
            holder=holder,
            takeover=takeover,
            taken_by=taken_by,
            confirmed_on_host=confirmed,
        )

    def refused(
        self, holder: str, takeover: str, *, lease_changed: bool = False
    ) -> None:
        self._emit(
            LEASE_TAKEOVER_REFUSED_EVENT,
            holder=holder,
            takeover=takeover,
            taken_by=lease_token(),
            lease_changed=lease_changed,
        )

    def unsettled(self) -> list[PendingTakeover]:
        with _ledger_for_this_thread(self._store, self._owner) as ledger:
            if ledger is None or not hasattr(ledger, "list_audit_events"):
                # A store with no audit trail holds no pending row of ours:
                # writing one is strict, and fails there.
                return []
            pending = self._events(ledger, LEASE_TAKEOVER_PENDING_EVENT)
            settled = {
                payload.get("takeover_id")
                for event in SETTLING_EVENTS
                for payload in self._events(ledger, event)
            }
        return [
            PendingTakeover(
                str(payload["takeover_id"]),
                str(payload["holder"]),
                str(payload["taken_by"]),
            )
            for payload in pending
            if payload.get("takeover_id") not in settled
        ]

    def _events(self, ledger: RunStore, event: str) -> list[dict[str, object]]:
        """This host's *event* payloads, oldest first.

        Matched on the provider's whole resource key, never the instance name
        alone: a same-named instance in another project or zone is another
        host, and its takeovers are not this one's to read or settle.
        """
        rows = sorted(ledger.list_audit_events(None, event=event), key=lambda e: e.id)
        return [
            row.payload
            for row in rows
            if row.payload is not None
            and row.payload.get("resource") == self._provider.resource_key
            and row.payload.get("takeover_id")
        ]

    def _emit(self, event: str, *, holder: str, takeover: str, **extra: object) -> None:
        # One settling row per takeover, whoever writes it: a settle that
        # landed but read as failed, then settled again on the retry, is
        # still one answer on the trail.
        dedup = None
        if event in SETTLING_EVENTS:
            resource = self._provider.resource_key
            dedup = f"host_lease_takeover_settled:{resource}:{takeover}"
        with _ledger_for_this_thread(self._store, self._owner) as ledger:
            emit(
                ledger,
                event,
                actor=resolve_actor(),
                repo=self._config.project.repo or None,
                payload={
                    "host": self._provider.name,
                    "address": self._provider.address,
                    "resource": self._provider.resource_key,
                    "holder": holder,
                    "takeover_id": takeover,
                    **extra,
                },
                dedup_key=dedup,
                strict=True,
            )


@contextmanager
def _ledger_for_this_thread(
    store: RunStore | None, owner: threading.Thread
) -> Iterator[RunStore | None]:
    """*store*, or a sibling handle on its ledger when called off *owner*'s thread.

    A SQLite connection refuses every thread but the one that opened it, and
    a strict write it refuses would fail a shard whose takeover was fine.
    """
    from kew.run_store import SqliteRunStore

    if (
        threading.current_thread() is owner
        or not isinstance(store, SqliteRunStore)
        or not store.reopenable
    ):
        yield store
        return
    with store.open_sibling() as sibling:
        yield sibling
