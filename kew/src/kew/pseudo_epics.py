"""Registry of pseudo-epic refs that dispatch reservations park under.

A *pseudo-epic* is a synthetic epic ref (never a real GitHub/Linear epic) that
holds reservations for work which must NOT derive its node's status from an item
reservation — delegated runs and governed-review (reviewer + remediation) runs.
Parking such work under a distinct ref keeps it out of the node's own epic
``reserved_item_ids`` set, so overlay precedence (reservation → IN_PROGRESS,
which is kind-blind) never corrupts the node's status while the work holds a slot.

Centralised here (a leaf module with no kew imports, so no layer can create a
cycle by importing it) so every reconcile/doctor sweep can iterate EVERY
pseudo-epic instead of hard-coding one — closing the #979-F1 string-duplication
and #979-F2 doctor-coverage gaps. When a new pseudo-epic is added, append it to
``PSEUDO_EPIC_REFS`` and both the driver reconcile and ``kew doctor`` cover it.
"""

from __future__ import annotations

DELEGATION_EPIC_REF = "delegation"
"""Delegated (`kew delegate`) reservations park here (D5)."""

REVIEW_EPIC_REF = "review"
"""Governed adversarial-review (reviewer + remediation) reservations park here
(N2/#977)."""

PSEUDO_EPIC_REFS: tuple[str, ...] = (DELEGATION_EPIC_REF, REVIEW_EPIC_REF)
"""Every pseudo-epic ref — iterate this in reconcile/doctor sweeps so no
pseudo-epic's stranded reservations become invisible."""
