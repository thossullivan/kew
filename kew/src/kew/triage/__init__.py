"""Triage station: host-side reading of an item's spec into a recorded plan.

The factory pattern kew is adopting (#2290 and its epic) separates *reading an
item* from *working on it*. This package owns the reading half, and only the
part of it that needs no model: :mod:`kew.triage.plan` extracts the acceptance
criteria an issue body already states, and :mod:`kew.triage.schema` is the shape
a plan takes once extracted.

Nothing here calls a model, a tracker, or the network. The host records what it
extracted into the ledger; a plan is never read back from a file an agent can
write.
"""

from __future__ import annotations
