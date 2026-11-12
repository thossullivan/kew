"""The drive's attention inbox: each new attention event notifies once (#2620).

`kew drive` already ticks every active epic, and every tick journals what it
saw. This module reads that journal back — the same composition ``kew epic
watch`` serves (:mod:`kew.graph.driver.attention_journal`), with no forge call
and no driver lock — and hands each event that is NEW to a notifier, naming
who owns it:

* ``maintainer`` — a Tom-class decision: a PR awaiting its merge decision.
  Merges are gated on a human review, so that person is the only one who can
  clear it.
* ``operator`` — every other kind of attention: a hold, an exhausted budget, a
  held publication, a sandbox refusal — and any kind this build does not know
  yet (the vocabulary is additive, and an unknown kind is someone's job).
* nobody — a routine event (an epic that finished, which the drive archives on
  its own). It goes to the drive log and never to the notifier, like the
  merges and retries the drive already logs per tick.

The console's Needs-you needs no push for the states it holds: it composes its
set off the same ledger (:mod:`kew.service.needs_you`) — holds, budgets, waits
— so it shows those for as long as the state stands, and the desktop
notification is the one-shot ping on top. Not every kind has a Needs-you row:
an agent token's usage-limit warning (#3329) shows only in ``kew epic status``,
``kew epic watch`` and this notification.

*Once* is kept by a cursor in the ledger, the ``drive:attention`` meta row:
per epic, a digest of every event state already delivered. An event notifies
when its state (``AttentionEvent.state_key`` — item, kind, reason and the
identity that separates a second READY at a new head) is not in the cursor; a
state that persists across ticks — a verify host still down — therefore
notifies once. A state that clears leaves the cursor, so if it comes back it
is news again: "notifies once until it changes".

The cursor is CLAIMED (compare-and-set) before the notifier runs, never after.
A drive that crashes between the two loses that one desktop ping, and the
Needs-you surface still shows the event; the opposite order would ping again
on every restart that landed in the window, and a second drive racing the
claim (one whose lock was just reclaimed) would ping twice. A snapshot the
journal refuses to read leaves that epic's cursor as it was: "could not tell"
must not clear a state and re-notify it next round.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kew.graph.driver.attention import AttentionEvent
from kew.graph.driver.attention_journal import (
    journaled_attention_events,
    read_journaled_observations,
)
from kew.waits import AttentionKind

_logger = logging.getLogger(__name__)

DRIVE_ATTENTION_KEY = "drive:attention"

# The same bound as ``kew.drive``'s control rows: a claim that keeps losing
# races is reported, never retried forever.
_CAS_ATTEMPTS = 8

# osascript is quick; a notification that hangs must not hold up a round.
_NOTIFY_TIMEOUT_S = 10


class Owner(StrEnum):
    """Who an attention event waits on."""

    OPERATOR = "operator"
    MAINTAINER = "maintainer"


#: Every kind this build knows, and its owner; ``None`` is routine.
OWNER_BY_KIND: dict[AttentionKind, Owner | None] = {
    AttentionKind.MERGE_READY: Owner.MAINTAINER,
    AttentionKind.HELD: Owner.OPERATOR,
    AttentionKind.BUDGET_EXHAUSTED: Owner.OPERATOR,
    AttentionKind.PUBLICATION_HELD: Owner.OPERATOR,
    AttentionKind.SANDBOX_REFUSED: Owner.OPERATOR,
    AttentionKind.HOST_RESTART_REPEATED: Owner.OPERATOR,
    AttentionKind.EPIC_COMPLETE: None,
}


def owner_for(event: AttentionEvent) -> Owner | None:
    """The event's owner, or None for a routine event."""
    return OWNER_BY_KIND.get(event.kind, Owner.OPERATOR)


@dataclass(frozen=True)
class Notice:
    """One attention event delivered to its owner."""

    owner: Owner
    event: AttentionEvent

    @property
    def title(self) -> str:
        return f"kew: {self.event.epic} needs the {self.owner.value}"

    @property
    def body(self) -> str:
        return self.event.as_line()


Notifier = Callable[[Notice], None]


def desktop_notifier() -> Notifier | None:
    """A macOS desktop notifier, or None where this host has none."""
    if sys.platform != "darwin":
        return None
    from kew.subprocess_utils import ensure_tool

    if ensure_tool("osascript") is None:
        return None
    return _osascript_notify


def _osascript_notify(notice: Notice) -> None:
    from kew.subprocess_utils import run

    # The text travels as argv, never spliced into the script: a reason is
    # journaled data and must not be able to end a string literal.
    run(
        [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e",
            "end run",
            notice.body,
            notice.title,
        ],
        timeout=_NOTIFY_TIMEOUT_S,
    )


def _digest(event: AttentionEvent) -> str:
    raw = json.dumps([event.epic, *event.state_key])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


#: An epic's attention the journal cannot carry, read live by the caller: a
#: driver lock held by a stalled process (#3331) — the tick it locks out holds
#: no lock, so it journals nothing.
LiveAttention = Callable[[str], AttentionEvent | None]


def _journaled(store: Any, epic: str) -> list[AttentionEvent] | None:
    """The epic's events off its last journaled tick; None when there is no
    journal to read or it cannot be read."""
    try:
        observations = read_journaled_observations(
            store.latest_epic_tick(epic),
            driver_state=store.get_epic_driver_state(epic),
        )
        if observations is None:
            return None
        return list(journaled_attention_events(epic, observations, store=store))
    except (sqlite3.Error, ValueError) as exc:
        _logger.warning("epic %s: attention journal unreadable: %s", epic, exc)
        return None


def _live_event(live: LiveAttention, epic: str) -> AttentionEvent | None:
    """``live``'s event for ``epic``; a read that fails counts as none, saying
    so — a damaged lock file must never end the drive's round (#3331)."""
    try:
        return live(epic)
    except Exception as exc:  # noqa: BLE001 — any failed read is the same
        _logger.warning("epic %s: live attention unreadable: %s", epic, exc)
        return None


def _current_events(
    store: Any, epics: Iterable[str], live: LiveAttention | None = None
) -> dict[str, list[AttentionEvent]]:
    """Each readable epic's attention events off its last journaled tick, plus
    the one ``live`` reads for it.

    An epic whose journal cannot be read is absent, not empty: absent keeps its
    cursor, where empty would clear it. A live event still reaches it — a
    stalled driver lock is exactly the epic whose journal stopped moving — at
    the cost of re-notifying its journaled events once the journal reads again.
    """
    out: dict[str, list[AttentionEvent]] = {}
    for epic in epics:
        events = _journaled(store, epic)
        extra = None if live is None else _live_event(live, epic)
        if extra is not None:
            events = [*(events or []), extra]
        if events is not None:
            out[epic] = events
    return out


def _decode(raw: str | None) -> dict[str, list[str]]:
    """The cursor row as ``{epic: [digest, ...]}``.

    An unreadable row is started over, saying so: that re-notifies what is
    standing once, which is the safe direction — reading it as "all delivered"
    would silence every future event.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        seen = value["seen"]
        return {str(k): [str(d) for d in v] for k, v in seen.items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        _logger.warning(
            "%s is unreadable (%s); starting it over", DRIVE_ATTENTION_KEY, exc
        )
        return {}


def _encode(seen: dict[str, list[str]]) -> str:
    return json.dumps({"v": 1, "seen": seen}, sort_keys=True)


def _advance(
    before: dict[str, list[str]],
    epics: tuple[str, ...],
    current: dict[str, list[AttentionEvent]],
) -> tuple[dict[str, list[str]], list[AttentionEvent]]:
    """The cursor after ``current``, and the events it had not seen."""
    # Epics that left the rotation drop out; unreadable ones keep theirs.
    after = {e: before[e] for e in epics if e in before}
    fresh: list[AttentionEvent] = []
    for epic, events in current.items():
        known = set(before.get(epic, ()))
        fresh += [ev for ev in events if _digest(ev) not in known]
        after[epic] = sorted({_digest(ev) for ev in events})
    return after, fresh


def _claim(
    store: Any, epics: tuple[str, ...], current: dict[str, list[AttentionEvent]]
) -> list[AttentionEvent] | None:
    """Advance the cursor to ``current`` and return the events new to it.

    None when the row kept changing under the claim: nothing is delivered and
    the next round tries again.
    """
    for _ in range(_CAS_ATTEMPTS):
        raw = store.get_meta(DRIVE_ATTENTION_KEY)
        after, fresh = _advance(_decode(raw), epics, current)
        encoded = _encode(after)
        if encoded == (raw or _encode({})) or store.compare_set_meta(
            DRIVE_ATTENTION_KEY, raw, encoded
        ):
            return fresh
    return None


def deliver_attention(
    store: Any,
    epics: Iterable[str],
    *,
    notify: Notifier | None,
    log: Callable[[str], None],
    warn: Callable[[str], None],
    live: LiveAttention | None = None,
) -> list[Notice]:
    """Deliver every attention event new since the cursor, once.

    ``log`` takes routine lines, ``warn`` takes the ones a person owns; both
    go to the drive log, and only the owned ones reach ``notify``. ``live``
    adds the per-epic event the journal cannot carry (a stalled driver lock);
    it is de-duplicated by the same cursor.
    """
    roster = tuple(epics)
    fresh = _claim(store, roster, _current_events(store, roster, live))
    if fresh is None:
        warn(f"kew drive: {DRIVE_ATTENTION_KEY} kept changing; notifying next round.")
        return []
    delivered: list[Notice] = []
    for event in fresh:
        owner = owner_for(event)
        if owner is None:
            log(f"epic {event.epic}: {event.as_line()}")
            continue
        notice = Notice(owner, event)
        warn(f"epic {event.epic}: needs the {owner.value}: {event.as_line()}")
        delivered.append(notice)
        if notify is not None:
            _send(notify, notice, warn)
    return delivered


def _send(notify: Notifier, notice: Notice, warn: Callable[[str], None]) -> None:
    # A notifier that fails costs that one ping, never the drive.
    try:
        notify(notice)
    except Exception as exc:  # noqa: BLE001 — any notifier failure is the same
        warn(f"kew drive: could not notify ({type(exc).__name__}: {exc}).")


def raise_attention(
    event: AttentionEvent,
    *,
    notify: Notifier | None,
    warn: Callable[[str], None],
) -> Notice:
    """Deliver one event the drive raises itself, not one off a tick's journal
    — the host-load hold (#2813). The caller owns "once": it raises the event
    only for the write that began the state."""
    notice = Notice(owner_for(event) or Owner.OPERATOR, event)
    warn(f"kew drive: needs the {notice.owner.value}: {event.as_line()}")
    if notify is not None:
        _send(notify, notice, warn)
    return notice
