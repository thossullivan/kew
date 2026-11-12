"""The one rule for reading an epic driver's RECORDED cadence (#2297).

``epic_driver_state.interval_s`` is a measured cadence: the interval of the
``kew epic run --interval N`` poller that last ticked. Two sides consume it and
they must not disagree:

* the reader — :func:`kew.service.epics.driver_liveness` — thresholds a
  driver's freshest heartbeat against it to decide ``live`` vs ``gone``;
* the writer — :meth:`kew.run_store.SqliteRunStore.finish_epic_tick` — retires
  it when a drive with no cadence of its own (a one-shot ``kew epic advance``)
  observes that nothing kept up with it.

Both ask the same question, so they ask it here: a cadence is **lapsed** once
more silence than it promised has gone by. Letting the two sides pick their own
threshold is how a cadence gets retired while the reader still believes it, or
kept while the reader already calls it dead.
"""

from __future__ import annotations

#: A cadence is lapsed once this many of its own intervals pass in silence —
#: 2x absorbs one fully-missed tick (jitter, a slow poll) before anything is
#: concluded, so a live-but-lagging driver is neither flagged as gone nor
#: stripped of its recorded interval.
DRIVER_GONE_INTERVALS = 2.0

#: A driver lock whose heartbeat is older than this is stale: the lock layer
#: (``kew.graph.driver.lock``) reclaims it even from a pid that still looks
#: alive. Defined here so the liveness reader can bound its ``busy`` reading by
#: the same line — a holder the lock layer would steal from is not working
#: (#2534 review), and the service layer cannot import the lock module.
LOCK_HEARTBEAT_STALE_S: int = 300


def cadence_lapsed(silence_s: float | None, interval_s: float | None) -> bool:
    """True when ``silence_s`` of quiet PROVES nothing is driving at ``interval_s``.

    Unthresholdable inputs prove nothing and so are never a lapse: a missing
    silence (an unreadable or absent stamp) or a non-positive cadence — ``age >
    2 * 0`` would otherwise condemn every live driver. "We cannot tell" is the
    honest answer, and both callers already have a path for it.
    """
    if silence_s is None or interval_s is None or interval_s <= 0:
        return False
    return silence_s > DRIVER_GONE_INTERVALS * interval_s
