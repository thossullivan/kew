"""The canonical customer work projection: one `WorkView`, composed not stored.

Every surface (CLI, TUI, web) used to answer "what is happening with this work,
and what should I do about it?" by reading `RunStatus` and guessing the rest.
That produced three recurring lies:

* **A torn view.** Two independent reads straddling a concurrent reconciliation
  show half of one world and half of the next. Fixed by composing inside ONE
  held read snapshot (:meth:`SqliteRunStore.read_snapshot`).
* **A fabricated empty.** An unreadable remote rendered as "nothing there" —
  the project's most common bug archetype. Fixed by making every remote-derived
  dimension carry its own :class:`RemoteObservation`: ``unavailable`` keeps the
  dimension ``unknown``, and an empty/absent answer is reported ONLY for a read
  that actually succeeded.
* **An invented action.** Each surface deriving its own next step, most
  dangerously "redispatch" over work that is already preserved. Fixed by
  deriving exactly one :class:`SafestActionDTO` here and nowhere else.

Two rules keep this module honest as later slices land:

1. **Nothing here is persisted.** There is no monolithic `WorkView` row and no
   second status machine. `RunStatus` remains execution state; publication,
   judgment, source synchronization, and remote availability are separate
   orthogonal dimensions of the composed view.
2. **The composer is pure.** :func:`compose_work_view` takes facts and returns a
   view — no clock, no store, no network. Remote facts are the caller's
   observation to make and are passed in; an unpassed remote is honestly
   ``unavailable``, never an implied success.

I19 adds four more orthogonal dimensions — integration health, boundary
readiness, the zero-diff judgment, and the human-review pressure on the
delivery repository — and ONE derived name for what is off-normal
(:class:`WorkExceptionDTO`). Exception and safest action come out of the SAME
ordered rule table (:data:`_RULES`), so a surface can never read a name that
disagrees with the action beside it.

``awaiting_publication`` is read here as the compatibility encoding it is:
completed execution plus a preserved artifact whose publication is outstanding.
Its safest action is always to finalize *that same artifact*, never to
redispatch — a retry would destroy the preserved implementation. I14 owns the
writer cutover to the normalized publication job; this module only stops
misreading the legacy rows in the meantime.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from kew.certificate_ref import CertificateReading
from kew.fleet_placement import run_site
from kew.human_review import REVIEW_PENDING, ReviewPolicy, resolve_policy
from kew.ids import GITHUB_ID_RE, item_ref
from kew.publication_job import (
    publication_jobs_active,
    require_countable_observation,
)
from kew.run_store import REVIEW_RUN_KINDS
from kew.run_store_publication_jobs import DEFAULT_OBSERVATION_MAX_AGE_SECONDS
from kew.service.models import (
    RemoteObservationDTO,
    ReviewPressureDTO,
    SafestActionDTO,
    WorkActionDTO,
    WorkArtifactDTO,
    WorkCertificateDTO,
    WorkExceptionDTO,
    WorkExecutionDTO,
    WorkIdentityDTO,
    WorkIntegrationDTO,
    WorkJudgmentDTO,
    WorkNoChangeDTO,
    WorkOutcomeDTO,
    WorkProjectionDTO,
    WorkReadinessDTO,
    WorkRouteDTO,
    WorkSourceDTO,
    WorkViewDTO,
)

if TYPE_CHECKING:
    from kew.run_store import AdmissionIdentity, OrphanPRRecord, RunRecord

_logger = logging.getLogger(__name__)

Availability = Literal["available", "stale", "unavailable"]

REVISION_PREFIX = "wv1"
"""Version tag on ``view_revision``. Bump it if the composed shape changes, so
a surface holding an old revision can never silently compare against a new one.
"""

# RunStatus -> execution state. Statuses absent from this map are terminal;
# listing the non-terminal ones explicitly means a NEW RunStatus value defaults
# to "terminal" (loud and wrong) rather than to "completed" (quiet and wrong).
_EXECUTION_STATE: dict[str, str] = {
    "pending": "in_flight",
    # The compatibility encoding: execution DID complete. Only publication is
    # outstanding, and that is the artifact dimension's business, not this one.
    "awaiting_publication": "completed",
    "success": "completed",
}

# Terminal statuses a plain retry is a sane response to. Everything else
# terminal (a refusal, any budget stop) is a decision a human already made or
# must make — dispatching again would just spend against the same wall.
_RETRYABLE_TERMINAL = frozenset({"failure", "killed"})

_JUDGMENT_BY_PR_STATE: dict[str, str] = {
    "open": "awaiting_human_decision",
    "merged": "accepted",
    "closed": "declined",
}

_OUTCOME_BY_JUDGMENT: dict[str, tuple[str, str]] = {
    "awaiting_human_decision": ("in_progress", "artifact published, decision pending"),
    # kew's own review cycle is reading the artifact; nothing is pending on a
    # person until the verdict lands (#2279/D8).
    "under_review": ("in_progress", "artifact published, governed review running"),
    # The same cycle, between two of its rounds (#2328): the verdict that
    # landed left the next move with kew, so the artifact is no nearer a
    # person than it was mid-round.
    "between_review_rounds": (
        "in_progress",
        "artifact published, kew's review cycle owes the next round",
    ),
    "changes_requested": ("in_progress", "artifact published, changes requested"),
    "accepted": ("delivered", "artifact accepted"),
    "declined": ("abandoned", "artifact declined without merge"),
}

PROJECTION_PREFIX = "wp1"
"""Version tag on ``projection_revision`` — the aggregate of ``wv1`` views."""

# The zero-diff lifecycle (kew.no_change) collapsed to what a surface must act
# on. Absent states (``completed``, ``rejected``) are settled and owe nothing.
_NO_CHANGE_STATE: dict[str, str] = {
    "candidate": "pending",
    "attested": "pending",
    "pending_confirmation": "pending",
    "authorized": "synchronizing",
    "sync_pending": "synchronizing",
    "effect_pending": "applying_effects",
    "completed": "completed",
    "rejected": "rejected",
}

# Terminal run statuses that are somebody's decision rather than a fault.
_REFUSAL_TERMINAL = frozenset(
    {"refused", "budget_exceeded", "budget_stopped", "budget_stop"}
)

_ENFORCEMENT_INACTIVE = (
    "the human-review policy is configured and reported, but not active: "
    "nothing is prevented until the publication-capacity broker is promoted"
)

_ENFORCEMENT_ACTIVE = "the publication-capacity broker enforces this policy"

#: How many recent publication jobs the local hold detail is read from. The
#: detail explains the authoritative number; it never replaces it, so a bound
#: here cannot make the projection wrong — only less specific.
_REVIEW_DETAIL_SCAN_LIMIT = 200


class _Store(Protocol):
    """The read surface a work view composes over (structural, not imported)."""

    def read_snapshot(self) -> AbstractContextManager[None]: ...
    def list_by_item(self, item_id: str) -> list[Any]: ...
    def get_admission_identity(self, run_id: str) -> Any | None: ...
    def list_orphan_prs(self, include_acked: bool = False) -> list[Any]: ...


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class RemoteObservation:
    """When and how one remote dimension was last read.

    The default is the honest one: ``unavailable`` / ``unobserved``. A caller
    that made no remote read gets a view that says so, rather than one that
    quietly reads as "checked, and there was nothing".
    """

    dimension: str
    availability: Availability = "unavailable"
    provenance: str = "unobserved"
    observed_at: datetime | None = None
    detail: str | None = None

    @classmethod
    def unavailable(
        cls, dimension: str, *, detail: str | None = None
    ) -> RemoteObservation:
        """An attempted-but-failed (or never attempted) read of *dimension*."""
        return cls(dimension=dimension, detail=detail)

    @property
    def has_reading(self) -> bool:
        """True when a real value was read. ``stale`` counts — it is an old
        reading, not an absent one; the view says which so a surface can age it.
        """
        return self.availability in ("available", "stale")

    def to_dto(self) -> RemoteObservationDTO:
        return RemoteObservationDTO(
            dimension=self.dimension,
            availability=self.availability,
            provenance=self.provenance,
            observed_at=_iso(self.observed_at),
            detail=self.detail,
        )


@dataclass(frozen=True)
class PullRequestFact:
    """One observation of the delivery artifact on the forge.

    ``checks``/``mergeability``/``review_decision`` are the landability facts
    I19 needs. They are deliberately separate from ``state``: an approved pull
    request with a red build or a conflicted tree is open, decided, and still
    not landable, and collapsing those into one word is how work quietly stops
    moving.
    """

    observation: RemoteObservation
    url: str | None = None
    state: str | None = None  # "open" | "merged" | "closed"
    merged_at: datetime | None = None
    checks: str | None = None  # "passing" | "failing" | "pending" | "none"
    mergeability: str | None = None  # "clean" | "conflicted" | "not_applicable"
    review_decision: str | None = None  # "approved" | "changes_requested" | ...
    #: The artifact's CURRENT head commit, when a reading carried one. ``None``
    #: is "not known", never "unchanged": the judgment only reads a recorded
    #: round as stale against a head it actually has (#2576).
    head_sha: str | None = None


@dataclass(frozen=True)
class TrackerItemFact:
    """One observation of the source item on its tracker."""

    observation: RemoteObservation
    state: str | None = None  # "open" | "closed"


@dataclass(frozen=True)
class PublicationFact:
    """The normalized publication job standing behind a preserved artifact.

    ``review_state`` is what makes a *hold* distinguishable from a *stall*: a
    publication reserved against human-review capacity is waiting for a person
    on purpose, and reporting it as stuck would invite exactly the redispatch
    the preservation exists to prevent.
    """

    state: str | None = None  # kew.publication_job lifecycle state
    review_state: str | None = None  # kew.human_review REVIEW_* value
    risk_reasons: tuple[str, ...] = ()
    reserved_at: str | None = None
    #: The job an I20 risk confirmation is addressed to. Read back from the
    #: same row the hold was read from, never supplied by a caller.
    job_id: str | None = None


@dataclass(frozen=True)
class NoChangeFact:
    """The zero-diff judgment for this item, and what it still owes.

    ``state`` is ``None`` when no judgment exists — which is not the same as a
    judgment that was rejected, and very much not the same as one that closed
    the item. Only a live judgment is read: a settled one owes nothing, so the
    execution and artifact dimensions answer for the item again.
    """

    state: str | None = None
    policy: str = ""
    reasons: tuple[str, ...] = ()
    sync_outstanding: int = 0
    sync_failed: int = 0
    effects_outstanding: int = 0
    #: The judgment an I20 confirm/reject/resync is addressed to.
    judgment_id: str | None = None


@dataclass(frozen=True)
class ReadinessFact:
    """What is proven about the EXACT route boundary this work would run on.

    ``unproven`` (nobody probed this fingerprint) and ``refuted`` (a probe
    proved it broken) are different answers, and neither is ``proven``. The
    default is ``unknown`` with an unavailable observation: no realized route
    profile was recorded, so there is nothing to have an opinion about.
    """

    observation: RemoteObservation
    state: str = "unknown"  # "proven" | "refuted" | "expired" | "unproven"
    fingerprint: str | None = None
    evidence_ref: str | None = None
    detail: str = ""


_CERTIFICATE_DIMENSION = "certificate_evidence"


def _certificate_provenance(
    reading: CertificateReading, observed_at: datetime | None
) -> str:
    """A read that never happened is ``unobserved``; one that failed is an
    ``error``. Collapsing the two would make "nobody looked" indistinguishable
    from "we looked and could not tell", which is the whole point of the
    field."""
    if reading.availability != "unavailable":
        return "observed"
    return "unobserved" if observed_at is None else "error"


@dataclass(frozen=True)
class CertificateFact:
    """What the certificate evidence carrier says about this work's build.

    The THIRD orthogonal truth (P1.1). It is read from
    :mod:`kew.certificate_ref`, never inferred: a passing execution probe
    cannot produce it and a standing certificate cannot repair a route that is
    currently unreachable. The default is ``unknown`` with an unavailable
    observation — nobody fetched the evidence ref, which is emphatically not
    the same as having fetched it and found nothing (``uncertified``).
    """

    observation: RemoteObservation
    state: str = "unknown"
    certified_at: str | None = None
    lane: str | None = None
    evidence_ref: str | None = None
    detail: str = ""

    @classmethod
    def from_reading(
        cls, reading: CertificateReading, *, observed_at: datetime | None = None
    ) -> CertificateFact:
        """Adapt one resolver reading, carrying its availability verbatim.

        The resolver has already failed closed; this must not soften that, so
        ``state`` and ``availability`` travel across unchanged rather than
        being re-derived here — one resolver, one verdict.
        """
        return cls(
            observation=RemoteObservation(
                dimension=_CERTIFICATE_DIMENSION,
                availability=cast(Availability, reading.availability),
                provenance=_certificate_provenance(reading, observed_at),
                observed_at=observed_at,
                detail=reading.detail or None,
            ),
            state=reading.state,
            certified_at=reading.certified_at,
            lane=reading.lane,
            evidence_ref=reading.evidence_ref,
            detail=reading.detail,
        )


@dataclass(frozen=True)
class ReviewPressureFact:
    """What one delivery repository is holding in front of a person.

    Mirrors the driver's ``ReviewPressure`` deliberately rather than importing
    it: ``kew.service`` may not import ``kew.graph`` (import-linter contract
    "epic snapshots cross to serve/service as JSON, not imports"). The *rules*
    are still shared — the countability rule is
    :func:`require_countable_observation` and the policy is resolved by
    :func:`resolve_policy` — so only the plumbing is restated here.
    """

    observation: RemoteObservation
    delivery_repo: str = ""
    engaged: bool = False
    countable: bool = False
    used: int = 0
    total: int = 0
    held: int = 0
    oldest_hold_age_seconds: float | None = None
    diff_burden_lines: int = 0
    policy_generation: int = 0
    detail: str = ""
    enforcement_active: bool = False

    @property
    def saturated(self) -> bool:
        """Whether fresh work must wait for a person.

        An uncountable reading is saturated, never zero: a review queue nobody
        could read is the one most likely to be full.
        """
        if not self.engaged:
            return False
        return not self.countable or self.used >= self.total

    def reasons(self) -> list[str]:
        """The operator-facing lines, or [] when nothing is being held back."""
        if not self.saturated:
            return []
        if not self.countable:
            return [
                f"{self.delivery_repo} review capacity is unknown"
                f" ({self.detail or 'the delivery repository could not be read'});"
                " an unreadable delivery repository is not an empty one"
            ]
        return [
            f"{self.delivery_repo} is holding {self.used} of {self.total} human"
            f" review(s) ({self._burden()})"
        ]

    def _burden(self) -> str:
        parts = [f"{self.held} local hold(s)"]
        if self.oldest_hold_age_seconds is not None:
            parts.append(f"oldest {self.oldest_hold_age_seconds / 3600:.1f}h")
        parts.append(f"{self.diff_burden_lines} diff line(s) waiting")
        return ", ".join(parts)

    def to_dto(self) -> ReviewPressureDTO:
        active = self.enforcement_active
        return ReviewPressureDTO(
            delivery_repo=self.delivery_repo,
            engaged=self.engaged,
            countable=self.countable,
            used=self.used,
            total=self.total,
            held=self.held,
            oldest_hold_age_seconds=self.oldest_hold_age_seconds,
            diff_burden_lines=self.diff_burden_lines,
            saturated=self.saturated,
            reasons=self.reasons(),
            enforcement="active" if active else "inactive",
            enforcement_detail=_ENFORCEMENT_ACTIVE if active else _ENFORCEMENT_INACTIVE,
            policy_generation=self.policy_generation,
            observation=self.observation.to_dto(),
        )


_NO_READ = "no remote read was attempted"
_NO_CERTIFICATE_READ = "the certificate evidence ref was not read"


def _unobserved_pr() -> PullRequestFact:
    return PullRequestFact(
        observation=RemoteObservation.unavailable("pull_request", detail=_NO_READ)
    )


def _unobserved_item() -> TrackerItemFact:
    return TrackerItemFact(
        observation=RemoteObservation.unavailable("tracker_item", detail=_NO_READ)
    )


def _unobserved_readiness(detail: str = _NO_READ) -> ReadinessFact:
    return ReadinessFact(
        observation=RemoteObservation.unavailable("route_boundary", detail=detail),
        detail=detail,
    )


def _unobserved_certificate(detail: str = _NO_CERTIFICATE_READ) -> CertificateFact:
    return CertificateFact(
        observation=RemoteObservation.unavailable(
            _CERTIFICATE_DIMENSION, detail=detail
        ),
        detail=detail,
    )


def _unobserved_pressure(
    delivery_repo: str = "", *, detail: str = _NO_READ
) -> ReviewPressureFact:
    """Not engaged. No observation exists, so no pressure is invented from it."""
    return ReviewPressureFact(
        observation=RemoteObservation.unavailable("review_capacity", detail=detail),
        delivery_repo=delivery_repo,
        detail=detail,
        enforcement_active=publication_jobs_active(),
    )


@dataclass(frozen=True)
class WorkFacts:
    """Everything one view is composed from — read inside one snapshot."""

    item_id: str
    identity: AdmissionIdentity | None
    runs: tuple[RunRecord, ...]  # newest attempt first
    orphan_prs: tuple[OrphanPRRecord, ...]
    pull_request: PullRequestFact
    tracker_item: TrackerItemFact
    publication: PublicationFact = PublicationFact()
    no_change: NoChangeFact = NoChangeFact()
    readiness: ReadinessFact = field(default_factory=_unobserved_readiness)
    certificate: CertificateFact = field(default_factory=_unobserved_certificate)
    pressure: ReviewPressureFact = field(default_factory=_unobserved_pressure)
    #: The fleet-stop latch, read as a local fact. A stopped fleet dispatches
    #: nothing, so recommending a dispatch under one is an action that cannot
    #: happen — the safest move is to resume first, deliberately.
    fleet_stopped: bool = False
    fleet_stop_reason: str = ""
    #: The moment this snapshot was read. Carried because one rule has an AGE
    #: in it (:func:`review_in_flight`), and a rule that reached for the clock
    #: itself would make :func:`compose_work_view` impure — same facts in,
    #: a different view out depending on when it was called. ``None`` only for
    #: a fact set assembled by hand; those read the wall clock, as before.
    as_of: datetime | None = None
    #: Every run recorded on the item, when :attr:`runs` is a NARROWED view of
    #: it. :mod:`kew.service.pipeline` narrows ``runs`` to the implementation
    #: attempts so its swimlane reads the artifact off the run that produced
    #: it — and a review run is precisely what that narrowing drops, so a
    #: judgment composed from the narrowed list answers "no round in flight"
    #: for an item kew is reviewing and hands the merge decision back to a
    #: person (#2279/D8). ``None`` means :attr:`runs` IS every run, which it is
    #: everywhere else.
    all_runs: tuple[RunRecord, ...] | None = None
    #: The in-flight bound this snapshot measures a pending row against
    #: (:func:`pending_run_max_age_s`). Carried on the facts for the same
    #: reason :attr:`as_of` is: the rule has an operator-set VALUE in it, and a
    #: rule that reached for the config itself would make
    #: :func:`compose_work_view` impure — same facts in, a different view out
    #: depending on whose config happened to be loaded. ``None`` means the
    #: floor :data:`PENDING_RUN_MAX_AGE_S`, which is what a fact set assembled
    #: by hand gets.
    pending_max_age_s: float | None = None
    #: The disposition of the item's latest governed review round, as the
    #: ledger recorded it (:func:`_read_review_verdict`) — ``""`` when no kew
    #: review applies to the item at all. Carried as a fact for the same
    #: reason :attr:`as_of` is: the judgment turns on it
    #: (:func:`_judgment_state`), and a rule that read the store itself would
    #: make :func:`compose_work_view` impure.
    review_verdict: str = ""
    #: The head commit that latest round reviewed — ``""`` with no round. A
    #: verdict is a verdict on ONE head (#2576): READY on a head the pull
    #: request has since moved past decides nothing about the current one.
    review_head: str = ""
    #: Whether kew's governed review owes this item a round before a person
    #: decides (#2576): the caller's config runs the adversarial stage and the
    #: item is an epic node, so "no round recorded yet" is kew's move, not a
    #: person's. ``False`` — the default — keeps "no round" a person's move,
    #: which is what an item no kew review applies to really is.
    review_governed: bool = False

    @property
    def latest(self) -> RunRecord | None:
        return self.runs[0] if self.runs else None

    @property
    def pending_bound(self) -> float:
        """The bound a pending row is read against — this snapshot's own, or
        the floor when the caller bound none."""
        if self.pending_max_age_s is None:
            return float(PENDING_RUN_MAX_AGE_S)
        return float(self.pending_max_age_s)

    @property
    def runs_on_item(self) -> tuple[RunRecord, ...]:
        """Every run on the item — :attr:`runs` unless a caller narrowed it."""
        return self.runs if self.all_runs is None else self.all_runs


def _attempt_order(record: Any) -> tuple[str, int]:
    return (
        str(_iso(getattr(record, "started_at", None)) or ""),
        int(getattr(record, "attempt_number", 1) or 1),
    )


def _reader(store: Any, name: str) -> Any:
    """A store method if this store has one, else None.

    Every I19 dimension rests on a later slice's table. A store that predates
    one of them (or a duck-typed test double) must degrade to "nothing
    recorded" — never to a fabricated judgment or an invented pressure.
    """
    reader = getattr(store, name, None)
    return reader if callable(reader) else None


def _tracker_of(identity: Any, default: str = "github") -> str:
    """The provider half of a lineage key.

    Lineage keys are written as ``<provider>:<item id>`` by the dispatch
    writers, so the provider must come from the run's recorded admission
    identity. A pre-identity row falls back to the historical default rather
    than to a key that matches nothing — a miss reads as "no judgment
    recorded", which is a silence, never a fabricated one.
    """
    return str(getattr(identity, "source_tracker", None) or default)


def _read_no_change(store: Any, identity: Any, item_id: str) -> NoChangeFact:
    """The LIVE zero-diff judgment for this item, with what it still owes.

    Only the live judgment is read. A settled one (completed, rejected) owes
    nothing and hands the item back to the execution and artifact dimensions,
    which is why there is no scan of history here.
    """
    live = _reader(store, "live_no_change_judgment")
    if live is None:
        return NoChangeFact()
    judgment = live(f"{_tracker_of(identity)}:{item_id}")
    if judgment is None:
        return NoChangeFact()
    return NoChangeFact(
        state=str(judgment.state),
        policy=str(judgment.policy or ""),
        reasons=tuple(r for r in (judgment.decision_reason,) if r),
        judgment_id=str(judgment.judgment_id),
        **_no_change_debts(store, judgment.judgment_id),
    )


def _no_change_debts(store: Any, judgment_id: str) -> dict[str, int]:
    """How many source writes and dependent effects are still outstanding."""
    syncs = _rows(store, "list_outcome_sync_jobs", judgment_id)
    effects = _rows(store, "list_dependency_effects", judgment_id)
    return {
        "sync_outstanding": sum(1 for j in syncs if not j.settled),
        "sync_failed": sum(1 for j in syncs if j.state == "abandoned"),
        "effects_outstanding": sum(1 for e in effects if not e.settled),
    }


def _rows(store: Any, name: str, *args: Any) -> tuple[Any, ...]:
    lister = _reader(store, name)
    return tuple(lister(*args)) if lister is not None else ()


def _read_publication(
    store: Any, identity: Any, item_id: str, latest: Any
) -> PublicationFact:
    """The live publication job for this item's branch, plus its review row.

    One source, one branch — the same lineage key the publication writers
    derive, so a job opened by a writer and the hold shown to a person are the
    same row rather than two guesses that happen to agree.
    """
    active = _reader(store, "active_publication_job")
    branch = getattr(latest, "branch", None)
    if active is None or latest is None or not branch:
        return PublicationFact()
    job = active(f"{_tracker_of(identity)}:{item_id}:{branch}")
    if job is None:
        return PublicationFact()
    return _publication_with_review(store, job)


def _publication_with_review(store: Any, job: Any) -> PublicationFact:
    reader = _reader(store, "publication_review")
    review = reader(job.job_id) if reader is not None else None
    if review is None:
        return PublicationFact(state=str(job.state), job_id=str(job.job_id))
    return PublicationFact(
        state=str(job.state),
        review_state=str(review.review_state or ""),
        risk_reasons=tuple(review.risk_reasons or ()),
        reserved_at=review.reserved_at,
        job_id=str(job.job_id),
    )


# ProbeVerdict/ProbeState values, read as the strings the store persists so
# this module needs no import of the readiness store's enums.
_READINESS_BY_PROBE: dict[tuple[str, str], str] = {
    ("hit", "succeeded"): "proven",
    ("hit", "failed"): "refuted",
    ("expired", "succeeded"): "expired",
    ("expired", "failed"): "expired",
}


def _read_readiness(store: Any, latest: Any, *, now: datetime) -> ReadinessFact:
    """What a settled probe proved about this attempt's exact boundary."""
    get_profile = _reader(store, "get_route_profile")
    lookup = _reader(store, "lookup_readiness_probe")
    if get_profile is None or lookup is None or latest is None:
        return _unobserved_readiness("no realized route profile was recorded")
    profile = get_profile(latest.run_id, getattr(latest, "attempt_number", 1) or 1)
    if profile is None:
        return _unobserved_readiness("no realized route profile was recorded")
    return _probe_readiness(profile.fingerprint, lookup(profile.fingerprint, now=now))


def _probe_readiness(fingerprint: str, result: Any) -> ReadinessFact:
    verdict = str(getattr(result.verdict, "value", result.verdict))
    probe = result.probe
    if probe is None:
        return replace(
            _unobserved_readiness("this exact boundary has never been probed"),
            state="unproven",
            fingerprint=fingerprint,
        )
    state = str(getattr(probe.state, "value", probe.state))
    availability: Availability = "available" if verdict == "hit" else "stale"
    return ReadinessFact(
        observation=RemoteObservation(
            dimension="route_boundary",
            availability=availability,
            provenance="observed" if verdict == "hit" else "cached",
            observed_at=probe.settled_at,
            detail=probe.detail or None,
        ),
        state=_READINESS_BY_PROBE.get((verdict, state), "unproven"),
        fingerprint=fingerprint,
        evidence_ref=probe.run_id,
        detail=probe.detail or "",
    )


def _hold_age_seconds(review: Any, now: datetime) -> float | None:
    if not review.reserved_at:
        return None
    try:
        reserved = datetime.fromisoformat(review.reserved_at)
    except ValueError:
        return None
    if reserved.tzinfo is None:
        return None
    return max(0.0, (now - reserved).total_seconds())


def _live_holds(store: Any, delivery_repo: str) -> list[Any]:
    """This repository's live holds, from a bounded read of recent jobs."""
    reader = _reader(store, "publication_review")
    if reader is None:
        return []
    recent = list(_rows(store, "list_publication_jobs"))[-_REVIEW_DETAIL_SCAN_LIMIT:]
    reviews = (reader(job.job_id) for job in recent)
    return [
        r
        for r in reviews
        if r is not None and r.delivery_repo == delivery_repo and r.occupies_slot
    ]


def _hold_detail(store: Any, delivery_repo: str, *, now: datetime) -> dict[str, Any]:
    """The bounded detail behind the count: how many, how old, how much diff."""
    held = _live_holds(store, delivery_repo)
    ages = [a for a in (_hold_age_seconds(r, now) for r in held) if a is not None]
    return {
        "held": len(held),
        "oldest_hold_age_seconds": max(ages) if ages else None,
        "diff_burden_lines": sum(
            (r.risk_added_lines or 0) + (r.risk_deleted_lines or 0) for r in held
        ),
    }


def _effective_policy(store: Any, delivery_repo: str, *, now: datetime) -> ReviewPolicy:
    """The policy in force, with an expired override resolved away — READ ONLY.

    Deliberately not ``store.effective_human_review_policy``: that resolver
    writes (it attests and clears an expired override) inside its own
    transaction, and this runs inside a held read snapshot on the same
    connection, where a second transaction would deadlock. The rule is still
    the shared one — :func:`resolve_policy` — applied to a plain read.
    """
    system = ReviewPolicy(delivery_repo=delivery_repo)
    getter = _reader(store, "get_human_review_policy")
    override = getter(delivery_repo) if getter is not None else None
    if override is None:
        return system
    return resolve_policy(system=system, override=override, now=now).policy


def _read_pressure(
    store: Any, delivery_repo: str, *, now: datetime
) -> ReviewPressureFact:
    """The human-review pressure on *delivery_repo*, or honest silence.

    No observation at all means this host has never reconciled the repository:
    there are no obligations to be honest about, so nothing is invented from
    the silence. An observation that exists but may not be counted from is the
    opposite — engaged and blind, which brakes.
    """
    reader = _reader(store, "review_observation")
    if not delivery_repo or reader is None:
        return _unobserved_pressure(delivery_repo)
    try:
        observation = reader(delivery_repo)
    except Exception as exc:  # noqa: BLE001 - engaged and blind, never dropped
        return _blind_pressure(delivery_repo, f"the observation is unreadable: {exc}")
    if observation is None:
        return _unobserved_pressure(delivery_repo)
    try:
        return _engaged_pressure(store, delivery_repo, observation, now=now)
    except Exception as exc:  # noqa: BLE001 - a downstream read blew up
        return _blind_pressure(
            delivery_repo, f"the review pressure is unreadable: {exc}"
        )


def _blind_pressure(delivery_repo: str, detail: str) -> ReviewPressureFact:
    """Engaged, and unable to count. Saturated — never a healthy zero."""
    return ReviewPressureFact(
        observation=RemoteObservation.unavailable("review_capacity", detail=detail),
        delivery_repo=delivery_repo,
        engaged=True,
        detail=detail,
        enforcement_active=publication_jobs_active(),
    )


def _engaged_pressure(
    store: Any, delivery_repo: str, observation: Any, *, now: datetime
) -> ReviewPressureFact:
    policy = _effective_policy(store, delivery_repo, now=now)
    pressure = ReviewPressureFact(
        observation=_capacity_observation(observation, "available", "observed"),
        delivery_repo=delivery_repo,
        engaged=True,
        total=policy.max_pending_reviews,
        policy_generation=policy.generation,
        enforcement_active=publication_jobs_active(),
        **_hold_detail(store, delivery_repo, now=now),
    )
    uncountable = _uncountable_reason(observation, delivery_repo, now=now)
    if uncountable:
        return replace(
            pressure,
            observation=_capacity_observation(observation, "stale", "cached"),
            detail=uncountable,
        )
    return replace(
        pressure,
        countable=True,
        used=store.review_capacity_occupancy(
            delivery_repo=delivery_repo, observation=observation
        ),
    )


def _capacity_observation(
    observation: Any, availability: Availability, provenance: str
) -> RemoteObservation:
    try:
        observed_at = datetime.fromisoformat(observation.observed_at)
    except (TypeError, ValueError):
        observed_at = None
    return RemoteObservation(
        dimension="review_capacity",
        availability=availability,
        provenance=provenance,
        observed_at=observed_at,
        detail=observation.detail or None,
    )


def _uncountable_reason(observation: Any, delivery_repo: str, *, now: datetime) -> str:
    """Why this observation may not be counted from, or "" when it may.

    Delegates to the capacity transaction's own rule rather than restating it.
    A reader reconciles nothing, so it demands only the generation it read.
    """
    try:
        require_countable_observation(
            observation,
            delivery_repo=delivery_repo,
            expected_generation=observation.generation,
            now=now,
            max_age_seconds=DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - any refusal is uncountable
        return str(exc)
    return ""


#: A stored ``pr_state`` is a *cached* observation of the forge — an old
#: answer, not an absent one. Mapping it to the composer's vocabulary is what
#: lets a surface with no network still read a truthful judgment dimension.
_CACHED_PR_STATE: dict[str, str] = {
    "OPEN": "open",
    "MERGED": "merged",
    "CLOSED": "closed",
}


#: The driver's journaled ``mergeStateStatus`` -> this composer's
#: ``mergeability`` vocabulary (#2303). Only the two words the composer acts on
#: are mapped: ``conflicted`` is what ``_integration_verdict`` raises
#: ``merge_conflict`` for, and ``clean`` is the state that says the tree really
#: was checked. Every other merge state — a branch rule, a draft, a base that is
#: merely behind, an UNKNOWN GitHub has not computed — maps to NOTHING, so the
#: dimension stays ``unknown`` rather than claiming a tree nobody proved clean.
_CACHED_MERGEABILITY: dict[str, str] = {
    "CLEAN": "clean",
    "HAS_HOOKS": "clean",
    "UNSTABLE": "clean",
    "BEHIND": "clean",
    "DIRTY": "conflicted",
}

#: How old the driver's journaled merge state may be and still be read as a
#: fact about the tree (#2303). ``kew epic run`` ticks every ``--interval``
#: seconds (60 by default) and re-reads ``mergeStateStatus`` every tick, so an
#: hour is roughly sixty missed reads: far past any transient, and still short
#: of letting a journal nobody has written to since — a driver that died, an
#: epic nobody drives — raise a BLOCKING ``merge_conflict`` over a conflict that
#: may have been rebased away hours ago. Past the bound the dimension reads
#: ``unknown``, which is what serve answered before the fact existed at all.
JOURNALED_MERGE_STATE_MAX_AGE_S = 60 * 60

_CACHED_PR_DETAIL = "the last persisted reconciliation of this pull request"
_CACHED_PR_WITH_MERGE_DETAIL = (
    "the last persisted reconciliation of this pull request; its mergeability "
    "is the driver's journaled merge state, read at "
)
#: What the detail says when the mergeability dimension is unknown because the
#: journaled merge state could not be READ (the reader is absent, the query
#: refused, the page ran out) rather than because no tick ever recorded one. The
#: two are different worlds and only one of them is "nothing conflicts" (#2303).
_CACHED_PR_UNREAD_MERGE_DETAIL = (
    "the last persisted reconciliation of this pull request; its mergeability "
    "is unknown because the journaled merge state could not be read: "
)


def _journaled_mergeability(
    merge_state: str | None, read_at: datetime | None, now: datetime
) -> str | None:
    """The ``mergeability`` one journaled merge state proves, or None.

    None — the dimension stays ``unknown`` — for a state nobody read, a state
    with no reading instant to age it by, and a reading older than
    :data:`JOURNALED_MERGE_STATE_MAX_AGE_S`. A fact whose age cannot be
    established is not a fact a blocking verdict may be raised from.
    """
    word = _CACHED_MERGEABILITY.get(str(merge_state or "").strip().upper())
    if word is None or read_at is None:
        return None
    age = (now - read_at).total_seconds()
    return None if age > JOURNALED_MERGE_STATE_MAX_AGE_S else word


def _instant(value: Any) -> datetime | None:
    """An aware datetime, or None — a naive or unparseable stamp cannot be aged.

    None is the REPORTED reading here, not a swallowed failure: the caller turns
    it into an ``unknown`` dimension rather than a fact, so an age nobody can
    establish cannot raise a verdict. A stamp that was there and would not parse
    is logged, because that is a malformed journal row rather than an absent
    one, and nothing else would ever say so.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            if value not in (None, ""):
                _logger.warning("unparseable observation instant: %r", value)
            return None
    return None if moment.tzinfo is None else moment.astimezone(UTC)


def _cached_pr_detail(
    mergeability: str | None, read_at: datetime | None, unread: str | None
) -> str:
    """The sentence under the cached artifact reading.

    Three worlds, and a reader has to be able to tell them apart: the merge
    state was used (and the detail names the instant it was read at), nothing
    was ever journaled about the tree (the shipped sentence, which claims
    nothing about mergeability), or the journaled state could not be READ — in
    which case the reason is named rather than left to look like the second one.
    """
    if mergeability is not None:
        return f"{_CACHED_PR_WITH_MERGE_DETAIL}{_iso(read_at)}"
    if unread:
        return f"{_CACHED_PR_UNREAD_MERGE_DETAIL}{unread}"
    return _CACHED_PR_DETAIL


def cached_pull_request(
    record: Any,
    *,
    merge_state: str | None = None,
    merge_state_at: Any = None,
    merge_state_unread: str | None = None,
    head_sha: str | None = None,
    now: datetime | None = None,
) -> PullRequestFact:
    """The forge observation a run row already carries, marked ``stale``.

    A surface that cannot reach the network still holds one real reading: the
    ``pr_url``/``pr_state`` the last reconciliation persisted. Reporting it as
    ``stale``/``cached`` keeps two truths at once — the answer is real, and it
    is old — where treating it as fresh would hide a decision made since, and
    treating it as absent would fabricate an empty.

    ``merge_state`` is the driver's journaled ``mergeStateStatus`` for this
    item's artifact, when a tick recorded one. Without it the ``mergeability``
    dimension was ``unknown`` on every serve-composed view, which meant
    :func:`_integration_verdict`'s ``merge_conflict`` — a blocking verdict with
    a conformance gate behind it (PCJ-3) — could never fire outside a test that
    built the facts by hand (#2303). Passing none keeps the old reading, which
    is ``unknown``: a caller with no snapshot has not proven a clean tree.

    ``merge_state_at`` is when that state was last READ (the snapshot's own
    ``merge_state_at``, not the tick that reported it — the driver carries a
    state forward over a tick GitHub answered ``UNKNOWN`` on). It is REQUIRED
    for the state to be used at all: an age nobody can establish is not
    something a blocking verdict may be raised from, and past
    :data:`JOURNALED_MERGE_STATE_MAX_AGE_S` the dimension goes back to
    ``unknown``. When the state IS used, the observation is stamped with the
    OLDER of the two readings and says which — a composite fact may not read as
    fresher than its stalest part.

    ``merge_state_unread`` is why this caller could not read a journaled merge
    state for the item (no journal reader, a refused query, a page that ran
    out). It changes no verdict — the dimension is ``unknown`` either way — but
    it keeps "no tick ever recorded a tree state" and "we could not look"
    distinguishable in the detail, which is the only place a reader of this
    fact could ever tell them apart.

    ``head_sha`` is the artifact's head as the same journal last read it, so a
    review round can be told apart from one on code the pull request has since
    moved past (#2576). ``None`` claims nothing.
    """
    url = getattr(record, "pr_url", None)
    state = _CACHED_PR_STATE.get(str(getattr(record, "pr_state", "") or "").upper())
    if not url or state is None:
        return _unobserved_pr()
    read_at = _instant(merge_state_at)
    mergeability = _journaled_mergeability(
        merge_state, read_at, now or datetime.now(UTC)
    )
    stamp = _cached_pr_stamp(record, read_at if mergeability is not None else None)
    return PullRequestFact(
        observation=RemoteObservation(
            dimension="pull_request",
            availability="stale",
            provenance="cached",
            observed_at=stamp,
            detail=_cached_pr_detail(mergeability, read_at, merge_state_unread),
        ),
        url=url,
        state=state,
        mergeability=mergeability,
        head_sha=head_sha or None,
    )


def _cached_pr_stamp(record: Any, read_at: datetime | None) -> datetime | None:
    """When the composite reading was taken: its OLDEST part.

    The run row's ``finished_at`` alone while the journaled merge state is
    unused — the reading this function has always reported. With the merge state
    in hand the stamp is the earlier of the two, because a surface ageing the
    fact must not be told the conflict was read when the reconciliation was.
    """
    reconciled = getattr(record, "finished_at", None)
    if read_at is None:
        return reconciled
    aged = _instant(reconciled)
    return read_at if aged is None else min(aged, read_at)


def _joined_orphans(store: _Store, runs: tuple[Any, ...]) -> tuple[Any, ...]:
    """Orphan PRs matched to this item by the branch of a recorded run.

    An orphan on a branch this item never ran (the ``no-record`` reason) is
    deliberately NOT joined: there is no durable fact tying it to this item,
    and guessing one from the branch name is the heuristic this projection
    exists to end.
    """
    branches = {r.branch for r in runs if r.branch}
    return tuple(o for o in store.list_orphan_prs() if o.branch in branches)


def _fleet_latch(store: _Store) -> dict[str, Any]:
    """Whether the fleet is stopped, and why — absent on a store without one."""
    getter = _reader(store, "get_fleet_stopped")
    latch = getter() if getter is not None else None
    return {
        "fleet_stopped": latch is not None,
        "fleet_stop_reason": str((latch or {}).get("reason", "") or ""),
    }


def _read_review_verdict(store: Any, item_id: str) -> tuple[str, str]:
    """The disposition of this item's latest governed review round and the head
    it reviewed, or ``("", "")``.

    ``""`` means "no kew review applies to this item": the ledger holds no
    round for it (a pull request outside any epic, or an epic whose review is
    off), or this store predates the table. It is deliberately not a verdict —
    the rule that reads it (:func:`_judgment_state`) only quiets an item whose
    NEXT move kew itself owes, and nothing recorded can prove that (#2328).

    ``effective_verdict`` and not the raw ``verdict``, for the reason
    :mod:`kew.service.epics` already reads it that way: the stop rule's
    acceptance of a low/medium-only NEEDS_CHANGES round is recorded beside the
    reviewer's own word (``READY_WITH_RESIDUALS``, #2151), and a reader of
    ``verdict`` alone takes an accepted round for one still owing a revision.
    """
    reader = _reader(store, "latest_review_round")
    row = reader(item_id) if reader is not None else None
    if row is None:
        return "", ""
    verdict = getattr(row, "effective_verdict", None) or getattr(row, "verdict", "")
    return str(verdict or ""), str(getattr(row, "head_sha", "") or "")


def _current_attempt_first(store: _Store, item_id: str) -> tuple[Any, ...]:
    """The item's runs, newest attempt first, by attempt rather than by clock.

    The runs a host-restart retry marker is bound to — the reaped run and
    every attempt an earlier retry had cleared — are superseded whatever the
    clocks say (#2969), so they sort behind every other run: a superseded run
    whose clock reads ahead of the replacement must not stand for the item's
    current attempt and offer a dispatch while the replacement is in flight.
    They stay in the list, so the attempt history still counts them.
    """
    reader = _reader(store, "superseded_runs_for_item")
    superseded = reader(item_id) if reader is not None else frozenset()
    return tuple(
        sorted(
            store.list_by_item(item_id),
            key=lambda run: (
                getattr(run, "run_id", None) not in superseded,
                _attempt_order(run),
            ),
            reverse=True,
        )
    )


def _later_slice_facts(
    store: _Store,
    item_id: str,
    identity: Any,
    latest: Any,
    *,
    delivery_repo: str | None,
    now: datetime,
) -> dict[str, Any]:
    """The dimensions later slices made durable, read where they live."""
    repo = delivery_repo or getattr(identity, "delivery_repo", None) or ""
    verdict, reviewed_head = _read_review_verdict(store, item_id)
    return {
        "publication": _read_publication(store, identity, item_id, latest),
        "no_change": _read_no_change(store, identity, item_id),
        "readiness": _read_readiness(store, latest, now=now),
        "pressure": _read_pressure(store, repo, now=now),
        "review_verdict": verdict,
        "review_head": reviewed_head,
    }


def _snapshot_facts(
    store: _Store, item_id: str, *, delivery_repo: str | None, now: datetime
) -> dict[str, Any]:
    """Every local fact for *item_id*, read inside ONE held snapshot.

    Every read below is read-only. Nothing in this block may open a write
    transaction: the snapshot reads through its own read-only connection, so
    a write inside it would fail rather than tear.
    """
    with store.read_snapshot():
        runs = _current_attempt_first(store, item_id)
        latest = runs[0] if runs else None
        identity = store.get_admission_identity(latest.run_id) if latest else None
        return {
            "as_of": now,
            "identity": identity,
            "runs": runs,
            "orphan_prs": _joined_orphans(store, runs),
            **_fleet_latch(store),
            **_later_slice_facts(
                store,
                item_id,
                identity,
                latest,
                delivery_repo=delivery_repo,
                now=now,
            ),
        }


def collect_work_facts(
    store: _Store,
    item_id: str,
    *,
    pull_request: PullRequestFact | None = None,
    tracker_item: TrackerItemFact | None = None,
    certificate: CertificateFact | None = None,
    delivery_repo: str | None = None,
    now: datetime | None = None,
) -> WorkFacts:
    """Read every local fact for *item_id* inside ONE consistent snapshot.

    Nesting is intended: a caller already holding a snapshot (composing several
    items for one revision) joins it rather than opening a second one, so the
    whole batch is bound to a single image of the database.
    """
    local = _snapshot_facts(
        store, item_id, delivery_repo=delivery_repo, now=now or datetime.now(UTC)
    )
    return WorkFacts(
        item_id=item_id,
        pull_request=pull_request or _unobserved_pr(),
        tracker_item=tracker_item or _unobserved_item(),
        certificate=certificate or _unobserved_certificate(),
        **local,
    )


def _identity_dto(facts: WorkFacts) -> WorkIdentityDTO:
    identity = facts.identity
    latest = facts.latest
    resolved = (identity.item_id if identity else None) or facts.item_id
    return WorkIdentityDTO(
        source_tracker=identity.source_tracker if identity else None,
        item_id=resolved,
        item_ref=item_ref(resolved),
        delivery_repo=identity.delivery_repo if identity else None,
        epic_ref=identity.epic_ref if identity else None,
        node_id=identity.node_id if identity else None,
        run_id=latest.run_id if latest else None,
        attempt_number=latest.attempt_number if latest else 0,
    )


def _synchronization(facts: WorkFacts) -> str:
    """Is the source item's state consistent with the delivered artifact?

    Both halves must have been read: with either missing, "synchronized" and
    "pending" are each a guess, so the answer is ``unknown``.
    """
    if not (
        facts.pull_request.observation.has_reading
        and facts.tracker_item.observation.has_reading
    ):
        return "unknown"
    if facts.pull_request.state != "merged":
        return "not_applicable"
    return "synchronized" if facts.tracker_item.state == "closed" else "pending"


def _source_dto(facts: WorkFacts) -> WorkSourceDTO:
    observation = facts.tracker_item.observation
    state = facts.tracker_item.state if observation.has_reading else None
    return WorkSourceDTO(
        tracker=facts.identity.source_tracker if facts.identity else None,
        item_state=state or "unknown",
        synchronization=_synchronization(facts),
        observation=observation.to_dto(),
    )


def _route_claim(declared: str, reported: str | None) -> str:
    """Whether the lane that was asked for is the lane that actually served.

    Nothing came back to check against is ``unverified`` — deliberately not
    ``honored``. Silence from a runner is not a promise kept.
    """
    if not reported or not declared:
        return "unverified"
    return "honored" if reported == declared else "substituted"


def _route_dto(latest: RunRecord | None) -> WorkRouteDTO:
    if latest is None:
        return WorkRouteDTO(containment="unknown")
    site = run_site(latest)
    return WorkRouteDTO(
        runner=latest.runner_name or "",
        model=latest.model or "",
        reported_model=latest.reported_model,
        containment="contained" if latest.container_name else "host",
        host=site.host,
        instance=site.instance,
        claim=_route_claim(latest.model or "", latest.reported_model),
    )


def _execution_dto(facts: WorkFacts) -> WorkExecutionDTO:
    latest = facts.latest
    if latest is None:
        return WorkExecutionDTO(state="not_started")
    return WorkExecutionDTO(
        state=_EXECUTION_STATE.get(latest.status, "terminal"),
        run_status=latest.status,
        attempts=len(facts.runs),
        started_at=_iso(latest.started_at),
        finished_at=_iso(latest.finished_at),
        error_category=latest.error_category or "",
    )


def _published_url(facts: WorkFacts, latest: RunRecord) -> str | None:
    """The artifact's URL from local fact first, then an orphan row.

    A remotely observed URL is deliberately NOT enough on its own: the local
    record and the orphan table are what tie a PR to *this* work item. The
    orphan list is ``pr_number``-ordered by the store, so the choice among
    several is stable — and so, therefore, is ``view_revision``.
    """
    if latest.pr_url:
        return latest.pr_url
    return facts.orphan_prs[0].url if facts.orphan_prs else None


def _artifact_dto(facts: WorkFacts) -> WorkArtifactDTO:
    latest = facts.latest
    if latest is None:
        return WorkArtifactDTO(state="absent", publication="not_required")
    if latest.status == "awaiting_publication":
        # Legacy encoding decoded honestly: the implementation is preserved on
        # its branch and publication is what remains outstanding.
        return WorkArtifactDTO(
            state="preserved", publication="pending", branch=latest.branch or None
        )
    url = _published_url(facts, latest)
    return WorkArtifactDTO(
        state="published" if url else "absent",
        publication="published" if url else "not_required",
        branch=latest.branch or None,
        pr_url=url,
        orphan=bool(facts.orphan_prs),
    )


#: How long a pending run row may still be read as a run IN FLIGHT.
#:
#: The pending row is the only fact that says "this run is executing right
#: now", and nothing reliably finalizes one whose run died: the store's
#: ``cleanup_stale_pending`` backstop waits 24h AND only flips a row whose host
#: it can prove dead (``_stale_host_is_dead`` in kew/src/kew/run_store.py), so
#: a row with an unknown owner stays pending for good. Unbounded, that one row
#: reads as the quiet ``in_flight``
#: (:data:`kew.service.attention.QUIET_EXCEPTIONS`) and takes the item off
#: every needs-you surface — the header's count, the epic legend, Work's band
#: — for a run nobody is running (#2279, rounds 2 and 3). A review row did it
#: twice over, holding the item in ``under_review`` as well.
#:
#: Past the bound the row is read two ways, because two dimensions were hiding
#: behind it: a REVIEW row stops holding the judgment in ``under_review``
#: (:func:`_judgment_state`), and ANY pending row stops reading as the quiet
#: ``in_flight`` (:func:`_rule_in_flight`) — ``review_abandoned`` for the round
#: that stopped reporting, ``run_abandoned`` for the attempt whose host died.
#: The second is what actually puts the item back on the needs-you surfaces,
#: since the judgment door is only reachable through a published artifact and
#: the dead row is the newest one on the item.
#:
#: This is the FLOOR, not the whole bound. Four hours is twice the DEFAULT
#: agent budget (``agent.timeout_minutes``, 120), so no run under an ordinary
#: budget is cut short by it; a host that raises that budget raises the bound
#: with it (:func:`pending_run_max_age_s`, which is what every production
#: reader passes).
PENDING_RUN_MAX_AGE_S = 4 * 60 * 60

#: How many agent budgets a pending row gets before it is read as dead. Two,
#: for the same reason the floor is twice the default: one budget is when the
#: runner itself gives up, so a row still pending at two has outlived the only
#: thing that was ever going to finalize it.
_PENDING_RUN_BUDGETS = 2


def pending_run_max_age_s(config: Any | None) -> float:
    """The in-flight bound for THIS host's agent budget.

    :data:`PENDING_RUN_MAX_AGE_S` is justified as twice ``agent.timeout_minutes``
    at its default of 120 — and read as a bare constant that justification
    expires the moment an operator raises the budget: a six-hour agent would
    have its LIVE review round called abandoned at four hours, which is #2279/D8
    pointing the other way, a merge decision handed to a person while kew is
    still reading the artifact. So the bound is derived from the operator's own
    value instead of asserting something about it.

    Floored at the constant, never shortened below it: a host that lowers
    ``timeout_minutes`` has not asked for a twitchier definition of "dead row",
    and the floor is what keeps a ten-minute budget from reading every ordinary
    pending row as abandoned.

    ``None`` config — a caller composing facts by hand, a duck-typed store —
    gets the floor, which is what this module did before the budget was
    consulted at all.

    KNOWN GAP (#2337): this is the bound for the host's CONFIGURED budget, and
    one run kind carries its own. A ``loop`` run's wall clock is the loop
    record's ``wall_clock_secs`` (``kew/src/kew/loops/delegate.py``), which
    starts at ``agent.timeout_minutes * 60`` — the same value — but a resume
    bump (``_apply_bumps``) can raise it above this bound. A live loop run past
    max(4h, 2 x budget) is then read as ``run_abandoned`` and put back on the
    needs-you surfaces while it is still executing. Closing it needs a fact
    this module does not have: ``RunRecord`` carries ``run_kind`` but no
    ``loop_id`` and no per-run deadline, so the bound has to become per-record
    with the loop's own wall clock joined onto it at every caller. Tracked
    separately rather than widened here.
    """
    minutes = getattr(getattr(config, "agent", None), "timeout_minutes", 0) or 0
    budgeted = float(minutes) * 60.0 * _PENDING_RUN_BUDGETS
    return max(float(PENDING_RUN_MAX_AGE_S), budgeted)


def _pending_run(rec: Any, now: datetime, max_age_s: float) -> bool:
    """One record read as a run still in flight at *now*.

    A row with no readable start is left in flight: an age that could not be
    taken is not evidence of a stale one, and the whole point of the bound is
    that a guess must not silence a decision in either direction.
    """
    if getattr(rec, "status", None) != "pending":
        return False
    started = getattr(rec, "started_at", None)
    if not isinstance(started, datetime):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds() <= max_age_s


def run_executing(rec: Any, *, now: datetime, max_age_s: float) -> bool:
    """Is this record a run executing at *now*? The projection's own reading.

    The needs-you set asks it (#2784): a running item is "working", never
    needs-you, and the bound is the one that keeps a dead row from hiding a
    real wait — past it the projection reads ``run_abandoned``, a needs-you
    exception, and so must the set.
    """
    return _pending_run(rec, now, max_age_s)


def _is_review_run(rec: Any) -> bool:
    """Whether this row is one of kew's OWN governed review runs."""
    return str(getattr(rec, "run_kind", "") or "") in REVIEW_RUN_KINDS


def _pending_review_run(rec: Any, now: datetime, max_age_s: float) -> bool:
    """One record read as a review round still in flight at *now*."""
    return _is_review_run(rec) and _pending_run(rec, now, max_age_s)


def review_in_flight(
    runs: Iterable[Any], *, now: datetime | None = None, max_age_s: float | None = None
) -> bool:
    """Is one of kew's own governed review runs in flight for this item?

    A recorded ``review_rounds`` row is a FINISHED round, so a pending run of a
    review kind is the only fact that tells "round N is being read right now"
    from "round N-1 returned its verdict and nothing has started since" — until
    it is *max_age_s* old, after which it tells neither and is read as the dead
    row it is.

    ``now`` is the snapshot's own moment (``WorkFacts.as_of``), so a view stays
    a function of its facts; the wall clock is the fallback for a caller that
    has no snapshot. ``max_age_s`` is the bound that snapshot was composed
    under (``WorkFacts.pending_bound``, from
    :func:`pending_run_max_age_s`); ``None`` is the floor
    :data:`PENDING_RUN_MAX_AGE_S`, for the same kind of caller.

    Shared with :mod:`kew.service.pipeline`, which draws the same fact as the
    review stage's ``working`` cell: the swimlane saying "round 1 · reviewing"
    beside a merge cell saying "needs you" was ONE reading of this predicate
    and one guess made without it (#2279/D8). The bound travels with it for the
    same reason — two readings of one predicate under two different ages is the
    same disagreement with an extra step.
    """
    moment = now or datetime.now(UTC)
    bound = PENDING_RUN_MAX_AGE_S if max_age_s is None else max_age_s
    return any(_pending_review_run(rec, moment, bound) for rec in runs)


def _abandoned_pending_run(rec: Any, now: datetime, max_age_s: float) -> bool:
    """A pending row *max_age_s* has given up on — review round or not.

    The state the bound leaves to answer for. Taking a review row out of
    ``under_review`` is only half the move: the judgment door is reachable only
    through a PUBLISHED artifact, and the dead row is the newest one on the
    item — a row the run executed under, which never carried a pull request —
    so the artifact reads ``absent`` and the judgment is never consulted. The
    execution dimension is therefore the one that has to answer, and answering
    it with the plain ``in_flight`` left the item quiet
    (:data:`kew.service.attention.QUIET_EXCEPTIONS`), which is the very silence
    the bound was added to end (#2279, round 2).

    Not narrowed to review kinds, because the silence was never review's alone
    (#2279, round 3): an implementation run whose host died leaves exactly the
    same pending row, and ``in_flight`` dropped THAT item from every needs-you
    surface for good too.
    """
    return getattr(rec, "status", None) == "pending" and not _pending_run(
        rec, now, max_age_s
    )


#: The recorded verdicts that leave the NEXT move with kew (#2328). The
#: vocabulary is :mod:`kew.review_delta`'s; the two words are spelled here
#: rather than imported for the reason :mod:`kew.service.pipeline` spells its
#: own ``_REVIEW_VERDICT`` table — a service module reads the ledger's words,
#: it does not pull the review machinery in to do it.
#:
#: Only these two. Every other recorded disposition either ends the cycle
#: (``READY``, ``READY_WITH_RESIDUALS`` — a merge decision, which IS a
#: person's) or is a round nothing will act on by itself (``UNPARSEABLE``, a
#: refusal placeholder), where a person going to look is the honest next move.
_VERDICT_NEEDS_CHANGES = "NEEDS_CHANGES"
_VERDICT_NEEDS_FULL = "NEEDS_FULL"
_KEW_OWES_THE_ROUND = frozenset({_VERDICT_NEEDS_CHANGES, _VERDICT_NEEDS_FULL})


def _judgment_state(facts: WorkFacts) -> str:
    """The human decision on the artifact, ``changes_requested`` included.

    An open pull request whose reviewer asked for changes is a decision that
    was already made — reading it as "awaiting a decision" hides that the ball
    is back in our court.

    An open pull request kew's OWN review cycle is still reading is not
    awaiting anybody (#2279/D8): #2055 read magenta "Merge decision" with a
    merge button beside a swimlane saying review round 1 was still running, and
    every surface that raises that band reads this field — the Work screen's
    needs-you band keys on ``awaiting_human_decision`` whatever the exception
    says, and so does the pipeline's merge cell. The verdict is what makes the
    decision a person's, so the state says so only once the verdict has landed.

    The check comes FIRST, ahead of ``changes_requested``, because the
    remediation leg is a review run too: while kew is fixing what the reviewer
    flagged, the next move is kew's, not the reviewer's.

    It reads ``runs_on_item`` and not ``runs``: the judgment is about the
    ARTIFACT, so a caller that narrowed ``runs`` to the implementation attempts
    (the pipeline, for its implement and publish cells) must not thereby have
    narrowed away the review run this rule exists to see.

    ``between_review_rounds`` is the same reading one step further out
    (#2328). A round being READ is not the only phase kew owns: #2278 sat
    BETWEEN rounds — round 4 returned NEEDS_FULL, round 5 had not started, no
    review run was pending — and read as a merge decision beside #2279, which
    was READY and really awaiting one. Between rounds the next move is kew's
    own remediation or its next full round, so nobody has been asked, and only
    the verdict that ENDS the cycle hands the artifact to a person.

    It comes after ``changes_requested`` deliberately: a human reviewer's own
    request for changes is that person's input on this artifact, and
    :data:`_KEW_OWES_THE_ROUND` speaks only for kew's own cycle.
    """
    state = _JUDGMENT_BY_PR_STATE.get(facts.pull_request.state or "", "unknown")
    if state != "awaiting_human_decision":
        return state
    if review_in_flight(
        facts.runs_on_item, now=facts.as_of, max_age_s=facts.pending_bound
    ):
        return "under_review"
    if facts.pull_request.review_decision == "changes_requested":
        return "changes_requested"
    if facts.review_verdict in _KEW_OWES_THE_ROUND or _round_owed(facts):
        return "between_review_rounds"
    return state


def _round_owed(facts: WorkFacts) -> bool:
    """Whether kew owes a round on the artifact's CURRENT head (#2576).

    A merge decision is a person's only once a verdict that ends the cycle has
    landed on the head the pull request carries now. Two readings stop short
    of that and leave the next move with kew: a governed item with no round
    recorded yet (the review stage runs one on every head it has not read),
    and a recorded round on a head the pull request has since moved past —
    whatever that round said, it was said about other code.

    Each half needs a fact in hand to fire. An item no kew review governs, and
    a pull request whose current head nobody read, keep the old reading: an
    unknown is not proof that kew is working, and quieting on one is how an
    item drops off every needs-you surface for good.
    """
    if not facts.review_verdict:
        return facts.review_governed
    head = facts.pull_request.head_sha
    return bool(head and facts.review_head and head != facts.review_head)


def _judgment_dto(facts: WorkFacts, artifact: WorkArtifactDTO) -> WorkJudgmentDTO:
    observation = facts.pull_request.observation
    if artifact.state != "published":
        return WorkJudgmentDTO(state="not_applicable", observation=observation.to_dto())
    if not observation.has_reading:
        return WorkJudgmentDTO(state="unknown", observation=observation.to_dto())
    return WorkJudgmentDTO(
        state=_judgment_state(facts),
        observation=observation.to_dto(),
        decided_at=_iso(facts.pull_request.merged_at),
    )


def _integration_dto(facts: WorkFacts) -> WorkIntegrationDTO:
    """Dimension 7 — landability, and ``unknown`` whenever nobody could look.

    Every field falls back to ``unknown`` rather than to a green default: an
    unreadable forge is not a passing build and not a clean tree.
    """
    observation = facts.pull_request.observation
    pr = facts.pull_request if observation.has_reading else PullRequestFact(observation)
    return WorkIntegrationDTO(
        checks=pr.checks or "unknown",
        mergeability=pr.mergeability or "unknown",
        review_decision=pr.review_decision or "unknown",
        observation=observation.to_dto(),
    )


def _readiness_dto(facts: WorkFacts) -> WorkReadinessDTO:
    """Dimension 8 — what a settled probe proved about the exact boundary."""
    readiness = facts.readiness
    return WorkReadinessDTO(
        state=readiness.state,
        fingerprint=readiness.fingerprint,
        evidence_ref=readiness.evidence_ref,
        detail=readiness.detail,
        observation=readiness.observation.to_dto(),
    )


def _certificate_dto(facts: WorkFacts) -> WorkCertificateDTO:
    """Dimension 12 — journey certification, and nothing else.

    Nothing in this function looks at the run, the probe, or the artifact.
    That is the orthogonality guarantee in code: the certificate dimension is
    composed from the certificate fact alone, so no execution outcome can ever
    leak into a certification claim or the reverse.
    """
    certificate = facts.certificate
    return WorkCertificateDTO(
        state=certificate.state,
        certified_at=certificate.certified_at,
        lane=certificate.lane,
        evidence_ref=certificate.evidence_ref,
        detail=certificate.detail,
        observation=certificate.observation.to_dto(),
    )


def _no_change_dto(facts: WorkFacts) -> WorkNoChangeDTO:
    """Dimension 9 — the zero-diff judgment and the debts it still carries.

    A failed source write outranks the lifecycle state: a judgment sitting in
    ``sync_pending`` with an abandoned job authorized a close that never
    happened, and reporting it as "synchronizing" would hide a silent loss.
    """
    fact = facts.no_change
    if fact.state is None:
        return WorkNoChangeDTO()
    state = _NO_CHANGE_STATE.get(fact.state, "pending")
    if fact.sync_failed:
        state = "sync_failed"
    return WorkNoChangeDTO(
        state=state,
        judgment_state=fact.state,
        policy=fact.policy,
        reasons=list(fact.reasons),
        sync_outstanding=fact.sync_outstanding,
        sync_failed=fact.sync_failed,
        effects_outstanding=fact.effects_outstanding,
    )


def _outcome_dto(
    execution: WorkExecutionDTO, artifact: WorkArtifactDTO, judgment: WorkJudgmentDTO
) -> WorkOutcomeDTO:
    if execution.state in ("not_started", "in_flight"):
        return WorkOutcomeDTO(state="in_progress", detail="execution not finished")
    if artifact.publication == "pending":
        return WorkOutcomeDTO(
            state="in_progress", detail="implementation preserved, publication pending"
        )
    if artifact.state == "published":
        state, detail = _OUTCOME_BY_JUDGMENT.get(
            judgment.state, ("unknown", "artifact published, decision unreadable")
        )
        return WorkOutcomeDTO(state=state, detail=detail)
    if execution.state == "terminal":
        return WorkOutcomeDTO(state="failed", detail=execution.run_status or "")
    return WorkOutcomeDTO(state="unknown", detail="completed with no recorded artifact")


# --------------------------------------------------------------------------- #
# One ordered rule table, two outputs.
#
# Each rule answers BOTH "what is off-normal here?" and "what is the safest
# thing to do about it?". They come out of the same function on purpose: two
# tables would drift, and a surface would eventually show an exception name
# that disagrees with the action printed beside it.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Derivation:
    """The already-composed dimensions one rule may consult."""

    facts: WorkFacts
    execution: WorkExecutionDTO
    artifact: WorkArtifactDTO
    judgment: WorkJudgmentDTO
    source: WorkSourceDTO
    integration: WorkIntegrationDTO
    no_change: WorkNoChangeDTO


@dataclass(frozen=True)
class _Verdict:
    """One exception name and the one action that answers it."""

    exception: WorkExceptionDTO
    action: SafestActionDTO


def _verdict(
    state: str,
    detail: str,
    *,
    dimension: str,
    action: str,
    reason: str,
    blocking: bool = False,
    blocked_by: str | None = None,
) -> _Verdict:
    return _Verdict(
        exception=WorkExceptionDTO(
            state=state, detail=detail, blocking=blocking, dimension=dimension
        ),
        action=SafestActionDTO(action=action, reason=reason, blocked_by=blocked_by),
    )


def _observe(dimension: str, why: str) -> _Verdict:
    return _verdict(
        "remote_unavailable",
        why,
        dimension=dimension,
        action="observe_remote",
        reason=f"{why}; observe it before acting on a guess",
        blocked_by=dimension,
    )


def _dispatch_blocker(ctx: _Derivation) -> _Verdict | None:
    """Everything that must be true before starting fresh work is safe.

    Order is deliberate: the fleet must not be stopped, the source must
    affirmatively be open, the boundary must not be known-broken, and a person
    must have room to review what comes back. Any one of them unmet makes a
    dispatch the wrong move.
    """
    return (
        _fleet_blocker(ctx)
        or _source_blocker(ctx)
        or _readiness_blocker(ctx)
        or _capacity_blocker(ctx.facts.pressure)
    )


def _fleet_blocker(ctx: _Derivation) -> _Verdict | None:
    """A stopped fleet dispatches nothing — so resuming is the honest action.

    Recommending a dispatch under the stop latch would name a step that
    silently does not happen. Resuming is a separate, deliberate decision, and
    saying so is what keeps the latch meaningful.
    """
    if not ctx.facts.fleet_stopped:
        return None
    why = ctx.facts.fleet_stop_reason or "no reason recorded"
    return _verdict(
        "fleet_stopped",
        f"the fleet is stopped ({why})",
        dimension="execution",
        action="resume_fleet",
        reason=f"the fleet is stopped ({why}); nothing dispatches until a "
        "person resumes it, so starting work here would be a no-op",
        blocking=True,
    )


def _source_blocker(ctx: _Derivation) -> _Verdict | None:
    if ctx.source.observation.availability != "available":
        return _observe(
            "tracker_item", "the source item's current state could not be read"
        )
    if ctx.source.item_state == "closed":
        return _verdict(
            "none",
            "the source item is closed",
            dimension="source",
            action="none",
            reason="the source item is closed; no dispatch is needed",
        )
    if ctx.source.item_state != "open":
        return _verdict(
            "source_state_unknown",
            f"the tracker reported {ctx.source.item_state!r}",
            dimension="source",
            action="inspect",
            reason="the source item's state is not understood",
        )
    return None


def _readiness_blocker(ctx: _Derivation) -> _Verdict | None:
    if ctx.facts.readiness.state != "refuted":
        return None
    return _readiness_refuted(ctx.facts.readiness)


def _readiness_refuted(readiness: ReadinessFact) -> _Verdict:
    detail = readiness.detail or "a probe of this exact boundary failed"
    return _verdict(
        "readiness_refuted",
        detail,
        dimension="readiness",
        action="inspect",
        reason=f"the route boundary is proven broken ({detail}); a dispatch"
        " would spend against a wall that is already known",
        blocking=True,
    )


def _capacity_blocker(pressure: ReviewPressureFact) -> _Verdict | None:
    if not pressure.saturated:
        return None
    reason = "; ".join(pressure.reasons())
    return _verdict(
        "review_capacity_saturated",
        reason,
        dimension="review_pressure",
        action="wait",
        reason=f"{reason}; starting more work adds to a queue a person cannot clear",
        blocked_by="review_capacity",
        blocking=True,
    )


#: The ONE wording for "kew's own review cycle holds this artifact" (#2279/D8).
#: Two doors reach it — the in-flight rule and the judgment table — and a
#: second sentence in either would be a second reading of one state, which is
#: the whole of what this node is about.
_REVIEWING = "a governed review round is reading the artifact"


def _reviewing_verdict(dimension: str) -> _Verdict:
    """``review_in_flight``, attributed to whichever dimension saw it."""
    return _verdict(
        "review_in_flight",
        _REVIEWING,
        dimension=dimension,
        action="wait",
        reason=f"{_REVIEWING}; its verdict decides whose move is next",
    )


#: The ONE wording for a review round that stopped reporting (#2279, round 2).
_ABANDONED = "a governed review round stopped reporting"

#: The same, for the dispatched attempt whose host died (#2279, round 3). A
#: separate sentence because they are separate facts to go and look at: one
#: leaves a published artifact nobody will return a verdict on, the other
#: leaves a reservation held by a run that is not running.
_ABANDONED_RUN = "a dispatched run stopped reporting"


def _review_abandoned_verdict() -> _Verdict:
    """The pending review row kew is no longer running anything behind.

    NOT quiet, and deliberately not ``in_flight``: the row says a round is
    being read, :data:`PENDING_RUN_MAX_AGE_S` says nothing is, and nothing in
    the system will finalize it. Whether the artifact gets another round or the
    decision it was always waiting on is a person's call, so the item is on the
    needs-you surfaces until somebody makes it.
    """
    return _verdict(
        "review_abandoned",
        _ABANDONED,
        dimension="execution",
        action="inspect",
        reason=f"{_ABANDONED}; its row will never be finalized, so the artifact"
        " waits for a person to review it again or decide on it as it stands",
        blocking=True,
    )


def _run_abandoned_verdict() -> _Verdict:
    """The pending row of any OTHER kind the bound has given up on.

    Same silence, one dimension over (#2279, round 3). ``in_flight`` is quiet
    because a run kew is executing is not a thing a person was asked for — and
    a pending row left behind by a dead host is not a run kew is executing. The
    store's own backstop cannot be relied on to say so: it waits 24h and only
    flips a row whose host it can PROVE dead, so a row with an unknown owner
    held the item off every needs-you surface permanently.

    ``inspect`` rather than ``dispatch``: the row still holds the item's
    reservation, so the honest next move is a person looking at what happened
    to the host, not a second attempt the reservation would refuse anyway.
    """
    return _verdict(
        "run_abandoned",
        _ABANDONED_RUN,
        dimension="execution",
        action="inspect",
        reason=f"{_ABANDONED_RUN}; its row will never be finalized and still"
        " holds the item, so a person decides whether the work is retried",
        blocking=True,
    )


def _abandoned_verdict(ctx: _Derivation, max_age_s: float) -> _Verdict | None:
    """The verdict for a pending row past the bound, or ``None`` for a live one.

    The newest row is the one that made the execution read ``in_flight``, so it
    is the one the bound is about. Which of the two words it gets turns on the
    run kind, because they read as different things to go and look at.
    """
    latest = ctx.facts.latest
    moment = ctx.facts.as_of or datetime.now(UTC)
    if latest is None or not _abandoned_pending_run(latest, moment, max_age_s):
        return None
    if _is_review_run(latest):
        return _review_abandoned_verdict()
    return _run_abandoned_verdict()


def _rule_in_flight(ctx: _Derivation) -> _Verdict | None:
    """A run kew is executing — and, when it is a review run, which one.

    ``review_in_flight`` is its own exception state rather than a detail on
    ``in_flight`` because the two read as different words on every surface: an
    item kew is implementing is *working*, an item kew is reviewing is *in
    review*. Before #2279 both read "in flight: run pending", which is how
    #2272 came to count in the header's "2 needs you" for a run kew itself was
    running.

    A pending row is not the same as a run, which is the third case: once the
    newest row is past :data:`PENDING_RUN_MAX_AGE_S`, "a run kew is executing"
    is no longer true of it and the quiet word would hide the item for good.
    That holds whatever the row's kind — a review round that stopped reporting
    and an implementation attempt whose host died leave the identical pending
    row — so the bound is read off the row, not off the kind, and only the
    WORDING turns on which one it was.
    """
    if ctx.execution.state != "in_flight":
        return None
    bound = ctx.facts.pending_bound
    if review_in_flight(ctx.facts.runs, now=ctx.facts.as_of, max_age_s=bound):
        return _reviewing_verdict("execution")
    abandoned = _abandoned_verdict(ctx, bound)
    if abandoned is not None:
        return abandoned
    status = ctx.execution.run_status
    return _verdict(
        "in_flight",
        f"run {status}",
        dimension="execution",
        action="wait",
        reason=f"run {status} — a second dispatch would duplicate it",
    )


def _rule_publication_hold(ctx: _Derivation) -> _Verdict | None:
    """Held for a person on purpose — not stalled, and never redispatched."""
    publication = ctx.facts.publication
    if publication.review_state != REVIEW_PENDING:
        return None
    reasons = ", ".join(publication.risk_reasons) or "policy risk bounds"
    return _verdict(
        "publication_risk_hold",
        f"held for human review: {reasons}",
        dimension="artifact",
        action="confirm_publication_risk",
        reason=f"publication is reserved against human-review capacity"
        f" ({reasons}); a person confirms the risk before it publishes",
        blocking=True,
    )


def _rule_publication_pending(ctx: _Derivation) -> _Verdict | None:
    """Preserved work outranks everything else, including an unreadable remote.

    The artifact is local fact, so finalizing it needs no network to be the
    safest move — and a redispatch here would destroy a completed
    implementation.
    """
    if ctx.artifact.publication != "pending":
        return None
    where = ctx.artifact.branch or "the recorded branch"
    return _verdict(
        "awaiting_publication",
        f"implementation preserved on {where}",
        dimension="artifact",
        action="finalize_publication",
        reason=f"implementation is preserved on {where}; publish that same "
        "artifact rather than running the work again",
    )


def _rule_no_change(ctx: _Derivation) -> _Verdict | None:
    """A judged zero-diff exit is a decision, never a reason to run again."""
    handler = _NO_CHANGE_VERDICT.get(ctx.no_change.state)
    return None if handler is None else handler(ctx.no_change)


def _no_change_sync_failed(dto: WorkNoChangeDTO) -> _Verdict:
    return _verdict(
        "no_change_sync_failed",
        f"{dto.sync_failed} authorized source write(s) were abandoned",
        dimension="no_change",
        action="synchronize_source",
        reason="the zero-diff outcome was authorized but the source item was"
        " never updated; the item is open against a decision already made",
        blocking=True,
    )


def _no_change_authorized(dto: WorkNoChangeDTO) -> _Verdict:
    owed = dto.sync_outstanding + dto.effects_outstanding
    return _verdict(
        "no_change_authorized",
        f"{owed} authorized effect(s) outstanding",
        dimension="no_change",
        action="synchronize_source",
        reason="the zero-diff outcome is authorized and still owes the source"
        " item and its dependents an update",
    )


def _no_change_pending(dto: WorkNoChangeDTO) -> _Verdict:
    reasons = "; ".join(dto.reasons) or f"policy {dto.policy or 'confirm'}"
    return _verdict(
        "no_change_pending",
        f"zero-diff exit awaiting a decision ({reasons})",
        dimension="no_change",
        action="confirm_no_change",
        reason="the run changed nothing and a person decides what that means;"
        " running it again can only produce the same empty diff",
        blocking=True,
    )


def _no_change_done(_dto: WorkNoChangeDTO) -> _Verdict:
    return _verdict(
        "none",
        "the zero-diff outcome was authorized and applied",
        dimension="no_change",
        action="none",
        reason="the run changed nothing, that was authorized, and every"
        " resulting effect has been acknowledged",
    )


_NO_CHANGE_VERDICT = {
    "sync_failed": _no_change_sync_failed,
    "synchronizing": _no_change_authorized,
    "applying_effects": _no_change_authorized,
    "pending": _no_change_pending,
    "completed": _no_change_done,
}


def _rule_not_started(ctx: _Derivation) -> _Verdict | None:
    if ctx.execution.state != "not_started":
        return None
    return _dispatch_blocker(ctx) or _verdict(
        "not_started",
        "no attempt has been recorded",
        dimension="execution",
        action="dispatch",
        reason="no attempt has been recorded for this item",
    )


def _rule_unreadable_remote(ctx: _Derivation) -> _Verdict | None:
    if ctx.artifact.state != "published":
        return None
    if not ctx.facts.pull_request.observation.has_reading:
        return _observe("pull_request", "the artifact's state could not be read")
    merged = ctx.facts.pull_request.state == "merged"
    if merged and not ctx.facts.tracker_item.observation.has_reading:
        return _observe("tracker_item", "the source item's state could not be read")
    return None


def _rule_published_artifact(ctx: _Derivation) -> _Verdict | None:
    """A real artifact exists. Landability first, then the human decision."""
    if ctx.artifact.state != "published":
        return None
    return (
        _orphan_verdict(ctx)
        or _integration_verdict(ctx)
        or _ACTION_BY_JUDGMENT.get(ctx.judgment.state, _unknown_judgment)(ctx)
    )


def _orphan_verdict(ctx: _Derivation) -> _Verdict | None:
    if not ctx.artifact.orphan:
        return None
    return _verdict(
        "orphan_artifact",
        "an artifact exists with no successful run behind it",
        dimension="artifact",
        action="inspect",
        reason="an artifact exists with no successful run behind it; "
        "dispatching over it would duplicate or clobber work",
        blocking=True,
    )


def _integration_verdict(ctx: _Derivation) -> _Verdict | None:
    """A conflict outranks a build result, which outranks a clean approval."""
    if ctx.integration.mergeability == "conflicted":
        return _verdict(
            "merge_conflict",
            "the artifact conflicts with its base",
            dimension="integration",
            action="resolve_conflict",
            reason="the artifact cannot merge until the conflict is resolved;"
            " no review outcome changes that",
            blocking=True,
        )
    if ctx.integration.checks == "failing":
        return _verdict(
            "checks_failing",
            "required checks are failing on the artifact",
            dimension="integration",
            action="fix_checks",
            reason="the artifact is published but red; an approval does not"
            " make it landable",
            blocking=True,
        )
    return None


def _await_decision(_ctx: _Derivation) -> _Verdict:
    return _verdict(
        "awaiting_human_decision",
        "the artifact is published for review",
        dimension="judgment",
        action="await_human_decision",
        reason="the artifact is published for review",
    )


def _under_review(_ctx: _Derivation) -> _Verdict:
    """kew's review cycle holds the artifact.

    The judgment door onto the same verdict ``_rule_in_flight`` serves. It is
    the one that answers when the review run's row is not the NEWEST on the
    item — an artifact whose pull request is still the latest recorded fact
    while a round reads it — which is the state the Work screen's needs-you
    band and the pipeline's merge cell were both reading as a person's move.
    """
    return _reviewing_verdict("judgment")


#: The two sentences a between-rounds item reads with (#2328). Short on
#: purpose: what an operator needs from a row about work kew is doing is that
#: it is not theirs.
_REVISING = "kew is revising"
_RE_REVIEWING = "kew re-reviews next"


def _revision_due(_ctx: _Derivation) -> _Verdict:
    """The round returned findings and kew's remediation leg is next.

    Quiet (:data:`kew.service.attention.QUIET_EXCEPTIONS`), like every other
    phase of a cycle kew owns: the remediation dispatch is kew's, and a person
    has nothing to decide until a verdict ends the cycle. A node AT the
    remediation cap is HELD instead, and a hold is a needs-you with its own
    command — the epic-hold source serves that row, not this one.
    """
    return _verdict(
        "review_remediation_due",
        _REVISING,
        dimension="judgment",
        action="wait",
        reason=f"{_REVISING} after its last review round's findings; the next"
        " move is kew's remediation, not a person's",
    )


def _full_round_due(ctx: _Derivation) -> _Verdict:
    """A delta READY that still owes a full round (``NEEDS_FULL``, #2111) — or
    a governed artifact whose current head no round has read yet (#2576).

    The artifact is no nearer a merge decision than it was mid-round: the
    round that decides is the one kew runs next.
    """
    owed = (
        "the last round owed a full one"
        if ctx.facts.review_verdict == _VERDICT_NEEDS_FULL
        else "no round has read the current head yet"
    )
    return _verdict(
        "review_round_due",
        _RE_REVIEWING,
        dimension="judgment",
        action="wait",
        reason=f"{_RE_REVIEWING}: {owed}, and its verdict decides whose move is next",
    )


def _between_rounds(ctx: _Derivation) -> _Verdict:
    """Which half of :data:`_KEW_OWES_THE_ROUND` the recorded verdict was.

    Two exception states rather than one with a detail, because the Work
    screen's state word is keyed on the state: an item kew is revising and one
    kew re-reviews next are both kew's move, and both read cyan, but they are
    different things to be told.
    """
    if ctx.facts.review_verdict == _VERDICT_NEEDS_CHANGES and not _round_owed(
        ctx.facts
    ):
        return _revision_due(ctx)
    return _full_round_due(ctx)


def _changes_requested(_ctx: _Derivation) -> _Verdict:
    return _verdict(
        "changes_requested",
        "a reviewer asked for changes",
        dimension="judgment",
        action="address_review_feedback",
        reason="a reviewer asked for changes; the next move is ours, and a"
        " fresh dispatch would abandon the review thread",
        blocking=True,
    )


def _after_acceptance(ctx: _Derivation) -> _Verdict:
    if ctx.source.synchronization == "pending":
        return _verdict(
            "source_desynchronized",
            "the artifact merged but the source item is still open",
            dimension="source",
            action="synchronize_tracker",
            reason="the artifact merged but the source item is still open",
        )
    return _verdict(
        "none",
        "the work is delivered and in sync",
        dimension="outcome",
        action="none",
        reason="the work is delivered and in sync",
    )


def _after_decline(_ctx: _Derivation) -> _Verdict:
    return _verdict(
        "declined",
        "the artifact was closed without merging",
        dimension="judgment",
        action="inspect",
        reason="the artifact was closed without merging; a redispatch would "
        "re-do work a human already declined",
        blocking=True,
    )


def _unknown_judgment(_ctx: _Derivation) -> _Verdict:
    return _verdict(
        "judgment_unknown",
        "the artifact's decision state is not known",
        dimension="judgment",
        action="inspect",
        reason="the artifact's decision state is not known",
    )


_ACTION_BY_JUDGMENT = {
    "awaiting_human_decision": _await_decision,
    "under_review": _under_review,
    "between_review_rounds": _between_rounds,
    "changes_requested": _changes_requested,
    "accepted": _after_acceptance,
    "declined": _after_decline,
}


def _rule_readiness(ctx: _Derivation) -> _Verdict | None:
    """A boundary proven broken is worth naming even with nothing in flight."""
    if ctx.facts.readiness.state != "refuted":
        return None
    return _readiness_refuted(ctx.facts.readiness)


def _rule_route_claim(ctx: _Derivation) -> _Verdict | None:
    """The lane that served was not the lane that was asked for."""
    latest = ctx.facts.latest
    if latest is None or _route_claim(latest.model or "", latest.reported_model) != (
        "substituted"
    ):
        return None
    detail = f"{latest.model} was requested; {latest.reported_model} served"
    return _verdict(
        "route_substituted",
        detail,
        dimension="route",
        action="inspect",
        reason=f"the route claim was not honored ({detail}); the result was"
        " not produced by the model this work was priced and routed for",
        blocking=True,
    )


def _rule_terminal(ctx: _Derivation) -> _Verdict | None:
    if ctx.execution.state != "terminal":
        return None
    status = ctx.execution.run_status or ""
    if status in _RETRYABLE_TERMINAL:
        return _dispatch_blocker(ctx) or _verdict(
            "terminal_failure",
            f"the attempt ended {status}",
            dimension="execution",
            action="dispatch",
            reason=f"the attempt ended {status} leaving no artifact to preserve",
            blocking=True,
        )
    return _verdict(
        "terminal_refused" if status in _REFUSAL_TERMINAL else "terminal_failure",
        f"the attempt ended {status}",
        dimension="execution",
        action="inspect",
        reason=f"the attempt ended {status}; another dispatch would meet the same wall",
        blocking=True,
    )


#: Precedence, highest first. Locally provable exceptions come before ones
#: that need a network, so an unreadable forge can never turn preserved work
#: into a retry.
_RULES = (
    _rule_in_flight,
    _rule_publication_hold,
    _rule_publication_pending,
    _rule_no_change,
    _rule_not_started,
    _rule_unreadable_remote,
    _rule_published_artifact,
    _rule_readiness,
    _rule_route_claim,
    _rule_terminal,
)

_FALLBACK = _verdict(
    "no_artifact",
    "execution completed without a recorded artifact or judgment",
    dimension="outcome",
    action="inspect",
    reason="execution completed without a recorded artifact or judgment",
    blocking=True,
)


def derive_verdict(ctx: _Derivation) -> _Verdict:
    """The ONE exception and the ONE action. Every surface reads this."""
    for rule in _RULES:
        verdict = rule(ctx)
        if verdict is not None:
            return verdict
    return _FALLBACK


# --------------------------------------------------------------------------- #
# I20 — the ONE place an executable action is offered.
#
# There is no second resolver. The offered set is keyed on the (exception,
# safest action) pair :func:`derive_verdict` already produced, so an action can
# only exist for a situation the projection itself named. Three consequences
# are deliberate:
#
# * **A verdict with no entry offers nothing.** Resolving a conflict, fixing a
#   build, addressing review feedback and waiting on a person are real next
#   steps that kew does not perform. Offering a "dispatch" in their place is
#   precisely the invented action this module exists to end.
# * **Blocked facts offer nothing.** ``blocked_by`` means the decision rests on
#   something nobody could read; a stale or unavailable reading of the very
#   dimension the verdict came from means the same. Both fail closed.
# * **Preserved work offers only finalization.** ``awaiting_publication`` maps
#   to finalizing THAT artifact and to nothing else, so no caller — CLI, web,
#   or a future one — can ask for a blind redispatch over completed work.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Offer:
    """One offerable action and the kind of thing it is addressed to."""

    action: str
    intent: str
    target_kind: str
    requires_reason: bool = False


#: Keyed by ``(exception state, safest action)`` — the exact verdict, never a
#: word on its own. ``terminal_failure`` reaches this table only through the
#: retryable branch that already derived ``dispatch``; the refusal branch
#: derives ``inspect`` and therefore offers nothing.
_OFFERS: dict[tuple[str, str], tuple[_Offer, ...]] = {
    ("awaiting_publication", "finalize_publication"): (
        _Offer(
            "finalize_publication",
            "publish the preserved artifact this run already produced",
            "run",
        ),
    ),
    ("publication_risk_hold", "confirm_publication_risk"): (
        _Offer(
            "confirm_publication_risk",
            "confirm the reviewed risk for the exact tree this job publishes",
            "publication_job",
        ),
    ),
    ("no_change_pending", "confirm_no_change"): (
        _Offer(
            "confirm_no_change",
            "authorize the zero-diff outcome and synchronize the source item",
            "no_change_judgment",
        ),
        _Offer(
            "reject_no_change",
            "refuse the zero-diff outcome, leaving the item open",
            "no_change_judgment",
            requires_reason=True,
        ),
    ),
    ("no_change_authorized", "synchronize_source"): (
        _Offer(
            "retry_no_change_sync",
            "re-run the authorized source write this outcome still owes",
            "no_change_judgment",
        ),
    ),
    ("no_change_sync_failed", "synchronize_source"): (
        _Offer(
            "retry_no_change_sync",
            "re-run the abandoned source write for a decision already made",
            "no_change_judgment",
        ),
        _Offer(
            "reject_no_change",
            "refuse the zero-diff outcome instead, leaving the item open",
            "no_change_judgment",
            requires_reason=True,
        ),
    ),
    ("orphan_artifact", "inspect"): (
        _Offer(
            "acknowledge_orphan",
            "record that a person has seen this unbacked artifact",
            "orphan_pr",
        ),
    ),
    ("not_started", "dispatch"): (
        _Offer("dispatch", "queue this item for its first attempt", "item"),
    ),
    ("terminal_failure", "dispatch"): (
        _Offer(
            "dispatch",
            "queue this item again — the attempt left no artifact to preserve",
            "item",
        ),
    ),
    ("fleet_stopped", "resume_fleet"): (
        _Offer("resume_fleet", "clear the fleet-stop latch", "fleet"),
    ),
}

#: The observation each derived dimension rests on. A verdict from a dimension
#: whose reading is stale or unavailable offers nothing: acting on it would be
#: acting on a guess. Dimensions absent here are local fact (the artifact on
#: disk, the run row, the judgment) and need no network to be current — which
#: is why preserved work still finalizes with the forge down.
_DIMENSION_OBSERVATION: dict[str, Any] = {
    "source": lambda f: f.tracker_item.observation,
    "judgment": lambda f: f.pull_request.observation,
    "integration": lambda f: f.pull_request.observation,
    "readiness": lambda f: f.readiness.observation,
    "review_pressure": lambda f: f.pressure.observation,
}

#: How each target kind is read back out of the facts. ``None`` means the
#: canonical id is not recorded, and an action with no target is not offered.
_TARGET_REF: dict[str, Any] = {
    "run": lambda f: getattr(f.latest, "run_id", None),
    "no_change_judgment": lambda f: f.no_change.judgment_id,
    "publication_job": lambda f: f.publication.job_id,
    "orphan_pr": lambda f: str(f.orphan_prs[0].pr_number) if f.orphan_prs else None,
    # Queueing is GitHub-label-based, so a non-numeric tracker id has nothing
    # to queue. Refusing the offer is honest; degrading the opaque id to a
    # numeric shim to make one fit is the bug this projection exists to end.
    "item": lambda f: f.item_id if GITHUB_ID_RE.fullmatch(f.item_id or "") else None,
    "fleet": lambda f: "fleet",
}


def _current_enough(facts: WorkFacts, exception: WorkExceptionDTO) -> bool:
    """Whether the dimension this verdict came from was actually read."""
    reader = _DIMENSION_OBSERVATION.get(exception.dimension)
    return reader is None or reader(facts).availability == "available"


def _action_dto(offer: _Offer, facts: WorkFacts, *, safest: bool) -> Any:
    target = _TARGET_REF[offer.target_kind](facts)
    if not target:
        return None
    return WorkActionDTO(
        action=offer.action,
        intent=offer.intent,
        target_kind=offer.target_kind,
        target_ref=str(target),
        safest=safest,
        requires_reason=offer.requires_reason,
    )


def offered_actions(
    facts: WorkFacts, exception: WorkExceptionDTO, safest: SafestActionDTO
) -> list[WorkActionDTO]:
    """Every action a caller may request for this work — derived, never stored.

    Offers are ordered safest-first: index 0 is the derived safest action, and
    anything after it is an authorized counterpart decision (refusing a
    zero-diff outcome rather than confirming it) that the same person is
    entitled to make with the same facts in front of them.
    """
    if safest.blocked_by is not None or not _current_enough(facts, exception):
        return []
    offers = _OFFERS.get((exception.state, safest.action), ())
    dtos = (
        _action_dto(offer, facts, safest=index == 0)
        for index, offer in enumerate(offers)
    )
    return [dto for dto in dtos if dto is not None]


def _view_revision(parts: dict[str, Any]) -> str:
    """Digest the fact material — never ``composed_at``.

    Two surfaces holding the same revision are therefore showing the same
    facts, which is what makes comparing them meaningful instead of comparing
    two undefined instants.
    """
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{REVISION_PREFIX}:{digest}"


def _dimensions(facts: WorkFacts) -> dict[str, Any]:
    """Every orthogonal dimension of one item, composed from *facts* alone."""
    execution = _execution_dto(facts)
    artifact = _artifact_dto(facts)
    judgment = _judgment_dto(facts, artifact)
    source = _source_dto(facts)
    integration = _integration_dto(facts)
    no_change = _no_change_dto(facts)
    dimensions = {
        "identity": _identity_dto(facts),
        "source": source,
        "route": _route_dto(facts.latest),
        "execution": execution,
        "artifact": artifact,
        "judgment": judgment,
        "outcome": _outcome_dto(execution, artifact, judgment),
        "integration": integration,
        "readiness": _readiness_dto(facts),
        "no_change": no_change,
        "certificate_status": _certificate_dto(facts),
        "review_pressure": facts.pressure.to_dto(),
    }
    verdict = derive_verdict(
        _Derivation(
            facts, execution, artifact, judgment, source, integration, no_change
        )
    )
    dimensions["exception"] = verdict.exception
    dimensions["safest_action"] = verdict.action
    return dimensions


def compose_work_view(
    facts: WorkFacts, *, composed_at: datetime | None = None
) -> WorkViewDTO:
    """Project *facts* into one work view. Pure: same facts in, same view out."""
    dimensions = _dimensions(facts)
    actions = offered_actions(
        facts, dimensions["exception"], dimensions["safest_action"]
    )
    revision_parts: dict[str, Any] = {
        key: value.model_dump() for key, value in dimensions.items()
    }
    # The offered set is digested too: a caller's ``expected_revision`` then
    # pins the exact actions it was looking at, not just the facts behind them.
    revision_parts["actions"] = [action.model_dump() for action in actions]
    # The CERTIFICATE observation is deliberately absent from this aggregate.
    # It rides on ``certificate_status.observation``, where the surface that
    # renders the certification claim reads it. This list is what the work's
    # CURRENT truth rests on — it gates the health verdict, the TUI evidence
    # word, and the offered set — and journey certification gates none of
    # those. Folding it in would make an unfetched evidence ref render every
    # item unhealthy and every row "unavailable", which is the third truth
    # leaking into the first two: exactly the orthogonality this dimension
    # exists to keep.
    observations = sorted(
        (
            facts.pull_request.observation,
            facts.tracker_item.observation,
            facts.readiness.observation,
            facts.pressure.observation,
        ),
        key=lambda o: o.dimension,
    )
    return WorkViewDTO(
        **dimensions,
        actions=actions,
        view_revision=_view_revision(revision_parts),
        composed_at=_iso(composed_at) or "",
        observations=[o.to_dto() for o in observations],
    )


def get_work_view(
    store: _Store,
    item_id: str,
    *,
    pull_request: PullRequestFact | None = None,
    tracker_item: TrackerItemFact | None = None,
    certificate: CertificateFact | None = None,
    delivery_repo: str | None = None,
    composed_at: datetime | None = None,
    pending_max_age_s: float | None = None,
) -> WorkViewDTO:
    """Collect (snapshot-bound) then compose (pure) — the one-call form.

    ``pending_max_age_s`` is the in-flight bound (:func:`pending_run_max_age_s`),
    which a caller holding a config passes: ``view_revision`` is a digest of the
    composed view, so a one-call view composed under a different bound from the
    projection the client read would differ from it — and every work action on
    that item would be refused as stale.
    """
    moment = composed_at or datetime.now(UTC)
    facts = collect_work_facts(
        store,
        item_id,
        pull_request=pull_request,
        tracker_item=tracker_item,
        certificate=certificate,
        delivery_repo=delivery_repo,
        now=moment,
    )
    return compose_work_view(
        replace(facts, pending_max_age_s=pending_max_age_s), composed_at=moment
    )


def _projection_revision(views: list[WorkViewDTO], pressure: ReviewPressureDTO) -> str:
    """Digest every member view plus the shared pressure — never the clock."""
    payload = json.dumps(
        {
            "views": [v.view_revision for v in views],
            "pressure": pressure.model_dump(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{PROJECTION_PREFIX}:{digest}"


def compose_work_projection(
    store: _Store,
    item_ids: Iterable[str],
    *,
    pull_requests: Mapping[str, PullRequestFact] | None = None,
    tracker_items: Mapping[str, TrackerItemFact] | None = None,
    certificates: Mapping[str, CertificateFact] | None = None,
    delivery_repo: str | None = None,
    composed_at: datetime | None = None,
    pending_max_age_s: float | None = None,
    review_governed: bool = False,
) -> WorkProjectionDTO:
    """Compose MANY work views inside ONE held read snapshot.

    The aggregate form of the no-torn-view guarantee: every item in the set is
    read from a single image of the database, and the human-review pressure is
    read once and shared, so no two rows of a dashboard can disagree about how
    full the reviewer queue is.

    An item that cannot be read is NAMED in ``unreadable`` rather than dropped:
    a projection that silently omitted it would render as a shorter, healthier
    world than the real one.

    ``pending_max_age_s`` is the in-flight bound every view in the set is
    composed under (:func:`pending_run_max_age_s` of the caller's config), bound
    once here for the same reason the pressure reading is: two views of one
    projection reading a pending row against two different ages is a
    disagreement about who is waiting. ``None`` is the floor
    :data:`PENDING_RUN_MAX_AGE_S`.

    ``review_governed`` is whether the caller's config runs kew's governed
    review stage; an epic node under it with no round yet is kew's move, not a
    merge decision (:func:`_round_owed`, #2576).
    """
    moment = composed_at or datetime.now(UTC)
    views: list[WorkViewDTO] = []
    unreadable: list[str] = []
    with store.read_snapshot():
        pressure = _read_pressure(store, delivery_repo or "", now=moment)
        for item_id in item_ids:
            view = _projected_view(
                store,
                item_id,
                pressure=pressure,
                pull_request=(pull_requests or {}).get(item_id),
                tracker_item=(tracker_items or {}).get(item_id),
                certificate=(certificates or {}).get(item_id),
                delivery_repo=delivery_repo,
                moment=moment,
                pending_max_age_s=pending_max_age_s,
                review_governed=review_governed,
            )
            (views.append(view) if view is not None else unreadable.append(item_id))
    dto = pressure.to_dto()
    return WorkProjectionDTO(
        views=views,
        pressure=dto,
        projection_revision=_projection_revision(views, dto),
        composed_at=_iso(moment) or "",
        unreadable=unreadable,
    )


def _projected_view(
    store: _Store,
    item_id: str,
    *,
    pressure: ReviewPressureFact,
    pull_request: PullRequestFact | None,
    tracker_item: TrackerItemFact | None,
    certificate: CertificateFact | None,
    delivery_repo: str | None,
    moment: datetime,
    pending_max_age_s: float | None = None,
    review_governed: bool = False,
) -> WorkViewDTO | None:
    """One member of a projection, or None when that ONE item is unreadable."""
    try:
        facts = collect_work_facts(
            store,
            item_id,
            pull_request=pull_request,
            tracker_item=tracker_item,
            certificate=certificate,
            delivery_repo=delivery_repo,
            now=moment,
        )
    except Exception:  # noqa: BLE001 - one item never sinks the whole projection
        return None
    governed = review_governed and bool(getattr(facts.identity, "epic_ref", None))
    return compose_work_view(
        replace(
            facts,
            pressure=pressure,
            pending_max_age_s=pending_max_age_s,
            review_governed=governed,
        ),
        composed_at=moment,
    )
