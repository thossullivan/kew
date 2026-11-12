"""Holding a merge claim for the length of an operator's merge verify (#3198).

:class:`ClaimHeartbeat` renews a claim every
:data:`~kew.run_store_merge_claims.MERGE_CLAIM_RENEW_EVERY` on a thread of its
own, so a verify of any length keeps it and a killed one loses it within one
:data:`~kew.run_store_merge_claims.MERGE_CLAIM_TTL`. :func:`held_merge_claim`
takes the claim, runs the heartbeat, and releases the claim however the block
ends: success, failure, a timeout, or a ``SIGTERM`` the caller turned into an
unwinding exit.

The heartbeat renews through a store it opens itself on each tick: a
``SqliteRunStore`` connection belongs to the thread that opened it.

:func:`wait_out_merge_claim` is the publishing side: a remediation, merge-main
step or ci-fix already running when a verify took its claim calls it just
before it pushes, and waits for the claim to end instead of pushing under it.
Its finished work stays where it is and is published once the head is free.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kew.epic_holds import MERGE_CLAIMED_WAIT_PREFIX
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_store import CI_FIX_RUN_KIND, MAX_NODE_PR_FACTS_LIMIT, RunStore
from kew.run_store_merge_claims import (
    MERGE_CLAIM_RENEW_EVERY,
    MERGE_CLAIM_TTL,
    MergeClaim,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: Opens a store for one renewal, on the heartbeat's own thread.
StoreOpener = Callable[[], AbstractContextManager[RunStore]]

#: How often a publish waiting out a merge claim looks at it again.
MERGE_CLAIM_POLL = timedelta(seconds=30)


def merge_claim_wait_reason(claim: MergeClaim, *, action: str = "remediation") -> str:
    """The wait a node reports while a merge claim holds *action* back."""
    return (
        f"{MERGE_CLAIMED_WAIT_PREFIX} — {claim.owner} is verifying PR "
        f"#{claim.pr} at {claim.head[:12]}; the {action} runs once its claim "
        f"ends (by {claim.expires_at.isoformat(timespec='seconds')} unless renewed)"
    )


def utc_now() -> datetime:
    """The wall clock every claim time is read from, unless a test pins one."""
    return datetime.now(UTC)


def claim_owner(flow: str = "kew verify") -> str:
    """A claim owner naming this process, so ``kew merge claim list`` says who."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"{flow} {user}:{os.getpid()}"


class ClaimHeartbeat:
    """Renews one owner's claim on ``(pr, heads)`` until stopped or lost.

    :meth:`tick` is one renewal at the clock's *now*; the thread calls it every
    *every*. A renewal that fails means the claim lapsed or someone else holds
    it: the heartbeat says so once and stops, because reviving a claim the drive
    may already have acted past would promise a protection that did not hold.
    """

    def __init__(
        self,
        opener: StoreOpener,
        pr: int,
        heads: Sequence[str],
        *,
        owner: str,
        clock: Clock = utc_now,
        every: timedelta = MERGE_CLAIM_RENEW_EVERY,
    ) -> None:
        self._opener = opener
        self._pr = pr
        self._heads = tuple(heads)
        self._owner = owner
        self._clock = clock
        self._every = every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def tick(self) -> bool:
        """Renew once; False (and :attr:`lost`) when the claim did not renew."""
        try:
            with self._opener() as store:
                renewed = store.renew_merge_claim(
                    self._pr, self._heads, owner=self._owner, now=self._clock()
                )
        except Exception as exc:  # noqa: BLE001 — the next tick may well succeed
            logger.warning(
                "merge claim on PR #%s could not be renewed this time (%s)",
                self._pr,
                exc,
            )
            return True
        if not renewed:
            self.lost = True
            logger.warning(
                "the merge claim on PR #%s lapsed or was taken by someone else: "
                "the drive may remediate the head this verify is testing",
                self._pr,
            )
        return renewed

    def _run(self) -> None:
        while not self._stop.wait(self._every.total_seconds()):
            if not self.tick():
                return

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"merge-claim-{self._pr}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


class HeldClaim:
    """The claim a verify holds, and whether it held for the whole verify.

    :meth:`lapsed` turns True once the heartbeat failed to renew, or once the
    claim is no longer this owner's live claim on every head at the wall
    clock's *now*. The second reading is what catches a Mac asleep past one
    TTL: the claim expired by the wall clock while the heartbeat's monotonic
    wait never woke to notice, and the drive may have pushed onto the head the
    verify was testing in the gap.
    """

    def __init__(
        self,
        store: RunStore,
        heartbeat: ClaimHeartbeat,
        pr: int,
        heads: Sequence[str],
        *,
        owner: str,
        clock: Clock,
    ) -> None:
        self.pr = pr
        self.heads = tuple(heads)
        self.owner = owner
        self._store = store
        self._heartbeat = heartbeat
        self._clock = clock
        self._lapsed = False
        self._settled = False

    def lapsed(self) -> bool:
        """Whether the claim stopped holding at any point so far.

        Once :meth:`settle` ran (at the release) the answer is frozen: the
        released claim is no longer live, and that is not a lapse.
        """
        if not self._lapsed and not self._settled:
            self._lapsed = self._heartbeat.lost or not self._still_held()
        return self._lapsed

    def settle(self) -> bool:
        """Read :meth:`lapsed` one last time and freeze it; the release follows.

        A reading that fails counts as a lapse: whether the claim held is then
        unknown, and the operator is told to check the head.
        """
        try:
            return self.lapsed()
        except Exception:
            self._lapsed = True
            raise
        finally:
            self._settled = True

    def _still_held(self) -> bool:
        now = self._clock()
        return all(
            (claim := self._store.live_merge_claim(self.pr, head, now=now)) is not None
            and claim.owner == self.owner
            for head in self.heads
        )


@contextmanager
def held_merge_claim(
    store: RunStore,
    opener: StoreOpener,
    pr: int,
    heads: Sequence[str],
    *,
    owner: str,
    clock: Clock = utc_now,
) -> Iterator[HeldClaim]:
    """Hold a claim on *heads* of *pr* for the block, renewed by a heartbeat.

    The take raises :class:`~kew.run_store_merge_claims.MergeClaimRefused`
    when another live claim holds one of the heads; nothing is taken then.
    The release runs on every way out of the block, after the yielded
    :class:`HeldClaim` has read whether the claim lapsed. A release that fails
    is logged and never raised: it must not hide the block's own result, and a
    claim left behind lapses on its own within one TTL.
    """
    store.take_merge_claim(pr, heads, owner=owner, now=clock())
    heartbeat = ClaimHeartbeat(opener, pr, heads, owner=owner, clock=clock)
    held = HeldClaim(store, heartbeat, pr, heads, owner=owner, clock=clock)
    heartbeat.start()
    try:
        yield held
    finally:
        heartbeat.stop()
        _release(held, store)


def _release(held: HeldClaim, store: RunStore) -> None:
    """Read the claim's last state, then give it back; never raise."""
    try:
        held.settle()
    except Exception:  # noqa: BLE001 — settle() already reads it as a lapse
        logger.exception(
            "could not read whether the merge claim on PR #%s held", held.pr
        )
    try:
        store.release_merge_claim(held.pr, held.heads, owner=held.owner)
    except Exception:  # noqa: BLE001 — see held_merge_claim
        logger.exception(
            "the merge claim on PR #%s could not be released; it lapses on its "
            "own within %s",
            held.pr,
            MERGE_CLAIM_TTL,
        )


def wait_out_merge_claim(
    store: RunStore | None,
    pr: int | None,
    head: str | None,
    *,
    flow: str,
    clock: Clock = utc_now,
    sleep: Callable[[float], None] | None = None,
    poll: timedelta = MERGE_CLAIM_POLL,
) -> MergeClaim | None:
    """Block while a live merge claim holds *pr* at *head*; the claim waited on.

    A publisher calls this just before it pushes onto *head* (#3198): a merge
    verify that took its claim after this run launched would otherwise see the
    head move under it. The run's finished work is kept where it is and
    published once the claim is released or lapses — no hold for a person,
    nothing redone. None, and no wait, when nothing claims the head or there
    is no ledger, PR or head to ask about. A store that keeps no claims (a
    test's stand-in) is read as one with none. *sleep* defaults to
    ``time.sleep``, read when called.
    """
    lookup = getattr(store, "live_merge_claim", None)
    if pr is None or not head or not callable(lookup):
        return None
    sleep = sleep or time.sleep
    first: MergeClaim | None = None
    while isinstance(claim := lookup(pr, head, now=clock()), MergeClaim):
        if first is None:
            first = claim
            logger.warning(
                "%s for PR #%s is finished but waits to publish: %s claims %s "
                "for a merge verify until %s unless renewed",
                flow,
                pr,
                claim.owner,
                head[:12],
                claim.expires_at.isoformat(timespec="seconds"),
            )
        remaining = (claim.expires_at - clock()).total_seconds()
        sleep(max(1.0, min(poll.total_seconds(), remaining)))
    if first is not None:
        logger.warning("%s for PR #%s: the merge claim ended; publishing", flow, pr)
    return first


#: The PR number at the end of a journaled pull-request URL.
_PR_URL_NUMBER = re.compile(r"/pull/(\d+)/?$")


@dataclass(frozen=True)
class PrLinks:
    """What the drive's newest snapshots say about one pull request."""

    #: The items whose node links to it.
    items: frozenset[str]
    #: The heads those snapshots last read for it.
    heads: frozenset[str]


def _linked_rows(store: RunStore) -> Iterator[tuple[int, str, str | None]]:
    """``(pr, item_id, head_oid)`` for each node the newest snapshots link."""
    after: tuple[str, str] | None = None
    while True:
        page = store.latest_node_pr_facts(limit=MAX_NODE_PR_FACTS_LIMIT, after=after)
        for row in page:
            match = _PR_URL_NUMBER.search(row.pr_url or "")
            if match is not None:
                yield int(match.group(1)), row.item_id, row.head_oid
        if len(page) < MAX_NODE_PR_FACTS_LIMIT:
            return
        after = (page[-1].epic_ref, page[-1].item_id)


def item_pr(store: RunStore, item_id: str) -> int | None:
    """The open PR the drive's newest snapshot links to *item_id*, if one.

    None when no epic journals the item with a PR, or when two epics journal it
    with different ones: a claim on a guessed PR would protect the wrong head.
    """
    found = {pr for pr, item, _head in _linked_rows(store) if item == item_id}
    return found.pop() if len(found) == 1 else None


def pr_links(store: RunStore, pr: int) -> PrLinks:
    """The items and heads the drive's newest snapshots link to *pr*."""
    rows = [(item, head) for number, item, head in _linked_rows(store) if number == pr]
    return PrLinks(
        items=frozenset(item for item, _head in rows),
        heads=frozenset(head for _item, head in rows if head),
    )


class ClaimBusy(RuntimeError):
    """A verify would claim a head that a running review action may push onto."""


@dataclass(frozen=True)
class BusyRun:
    """A run of the PR's node that may push onto the head when it finishes."""

    kind: str
    item_id: str
    run_id: str


def busy_claim_runs(store: RunStore, items: frozenset[str]) -> list[BusyRun]:
    """The runs of *items* a new merge claim would race.

    A node's review reservation, which its reviewer, remediation or merge-main
    step holds for as long as it runs (:mod:`kew.graph.driver.review_detach`),
    and any ci-fix run still in flight: all but the reviewer push onto the head
    when they finish.
    """
    busy = [
        BusyRun("review action", row.item_id, row.run_id)
        for row in store.list_epic_reservations(REVIEW_EPIC_REF)
        if row.item_id in items
    ]
    for item in sorted(items):
        busy.extend(
            BusyRun("ci-fix", item, run.run_id)
            for run in store.list_by_item(item)
            if run.run_kind == CI_FIX_RUN_KIND and run.status == "pending"
        )
    return busy


class PublishDeferred(RuntimeError):
    """A finished run found its PR head claimed and published nothing."""

    def __init__(self, claim: MergeClaim, flow: str) -> None:
        super().__init__(
            f"{flow} not published: {claim.owner} claims PR #{claim.pr} at "
            f"{claim.head[:12]} for a merge verify until "
            f"{claim.expires_at.isoformat(timespec='seconds')}"
        )
        self.claim = claim


def defer_if_claimed(
    store: RunStore | None,
    pr: int | None,
    head: str | None,
    *,
    flow: str,
    clock: Clock = utc_now,
) -> None:
    """Raise :class:`PublishDeferred` when a live claim holds *pr* at *head*.

    For a publisher that must not block where it runs — ci-fix runs inside the
    drive's tick, so waiting there would stop every merge and status update for
    the length of the verify. It publishes nothing, keeps its work where it is,
    and the next tick tries again once the claim ends.
    """
    lookup = getattr(store, "live_merge_claim", None)
    if pr is None or not head or not callable(lookup):
        return
    claim = lookup(pr, head, now=clock())
    if isinstance(claim, MergeClaim):
        raise PublishDeferred(claim, flow)
