"""Tracker-error classification the delegation watch loop handles (N3).

The watch loop must never crash-loop on a hard tracker error: a deleted issue or
an exhausted rate-limit in the per-candidate process leg would otherwise kill the
foreground loop, and — because the cursor never advanced past the offending
event — a restart would die on the very same event forever.

These two delegate-level exceptions let the loop decide *per candidate* without
importing the concrete Linear error taxonomy: the live adapter
(``cli/delegate.py``) maps the Linear client's errors onto them, and tests raise
them directly from a fake.
"""

from __future__ import annotations


class DelegationTrackerError(Exception):
    """Base for a tracker failure the watch loop handles per candidate."""


class ItemGone(DelegationTrackerError):
    """The tracked item is permanently gone (deleted / entity-not-found).

    Terminal for this candidate: the loop records a terminal decision so the
    cursor advances past it and never re-fetches the ghost.
    """


class TransientTrackerError(DelegationTrackerError):
    """A transient tracker failure (rate-limit / gateway blip / unavailable).

    Not terminal: the loop backs off and leaves the candidate undecided so a
    later tick retries it once the tracker recovers.
    """


class FatalTrackerError(DelegationTrackerError):
    """A fatal, non-retryable tracker failure — an expired/revoked API key.

    Distinct from :class:`TransientTrackerError`: retrying an auth failure only
    spins forever doing nothing, so the watch loop must terminate loudly with a
    non-zero exit rather than back off (Task 3, #971). The live adapter maps a
    ``LinearAuthError`` onto this so neither the poll leg (swallowed as a
    transient blip) nor the per-candidate fetch leg (deferred) can hide it.
    """
