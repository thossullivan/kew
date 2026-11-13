"""I26 — one golden WorkView matrix, projected through every surface at once.

The product-coherence charter's last promise is the one nobody can prove from
inside a single surface: that the CLI, the TUI and the web console tell the
customer the SAME thing about the same work at the same instant. Each surface
has its own adapter, and an adapter is exactly where a second, quietly
disagreeing resolver appears — a badge derived from ``RunStatus`` here, a
"retry" button invented there, a certificate read as if it were a live probe
somewhere else.

So this module fixes one matrix of golden views and runs all four projections
over it:

* ``kew.cli._status_run_payload`` — the ``status --run --json`` contract
* ``kew.tui.state.work_display`` — the terminal row's view model
* ``WorkViewDTO`` JSON — the exact document ``/api/verbs/work/{item_id}``
  returns, which ``frontend/src/lib/surface-convergence.test.ts`` reads back
  out of the SAME fixture file
* ``kew.service.work.offered_actions`` / ``kew.service.verbs._offer_or_raise``
  — the safe-action projection and the one gate every mutation passes through

What is compared is semantics, never pixels: canonical identity, the ten
orthogonal dimensions, per-dimension availability, review pressure, the one
safest action, the offered set, and ``view_revision``. Wording, column widths,
badge glyphs and the steps a surface deliberately does not perform (resolving a
conflict, deciding on a pull request) are outside the comparison by design.

Two properties are asserted about the claims beside them, because they are the
pair most often collapsed into one word:

* **Execution validity and journey certification are orthogonal.** A passing
  live probe never implies a certificate, and a standing certificate never
  implies current reachability. Since P1.1 the certificate is a real dimension
  of the composed document (``certificate_status``) backed by
  :mod:`kew.certificate_ref`, and each case declares ONE certificate reading
  that feeds both the view and the web claim resolver — so the two surfaces
  cannot drift apart while still being unable to imply one another.
* **Absent evidence never renders ready.** A certificate whose evidence could
  not be read is ``unknown`` — not certified, and not uncertified either.

The fixture is generated, never hand-digested: ``view_revision`` is a hash of
the composed facts, so re-writing it by hand would make the comparison
meaningless. Regenerate with::

    KEW_WRITE_WORK_VIEW_GOLDEN=1 uv run pytest tests/test_surface_convergence.py

and read the diff — a changed revision means a surface's shared contract moved.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kew.human_review import REVIEW_PENDING
from kew.run_store import AdmissionIdentity, OrphanPRRecord, RunRecord
from kew.service.models import WorkViewDTO
from kew.service.work import (
    CertificateFact,
    NoChangeFact,
    PublicationFact,
    PullRequestFact,
    ReadinessFact,
    RemoteObservation,
    ReviewPressureFact,
    TrackerItemFact,
    WorkFacts,
    _unobserved_certificate,
    compose_work_view,
)

FIXTURE = Path(__file__).parent / "fixtures" / "product_coherence" / "work_views.json"

COMPOSED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 8, 18, 11, 59, tzinfo=UTC)
STARTED_AT = datetime(2026, 8, 18, 11, 30, tzinfo=UTC)
FINISHED_AT = datetime(2026, 8, 18, 11, 48, tzinfo=UTC)

DELIVERY_REPO = "acme/widgets"
EPIC_REF = "epic-product-coherence"
LANE_MODEL = "claude-opus-5"
LANE_RUNNER = "claude-code"

#: Every remote dimension a composed view carries an observation for, in the
#: order ``compose_work_view`` sorts them.
OBSERVED_DIMENSIONS = (
    "pull_request",
    "review_capacity",
    "route_boundary",
    "tracker_item",
)

#: The claim shapes E14a requires the matrix to exercise at named revisions.
REQUIRED_CLAIM_SHAPES = frozenset(
    {
        "configured_live_unverified",
        "live_verified",
        "prior_certificate_during_outage",
        "recertification_due",
        "certificate_evidence_unavailable",
        "anchored_certified",
    }
)

#: The customer states E14a requires the matrix to exercise.
REQUIRED_EXCEPTIONS = frozenset(
    {
        "in_flight",
        "awaiting_publication",
        "publication_risk_hold",
        "review_capacity_saturated",
        "awaiting_human_decision",
        "changes_requested",
        "no_change_pending",
        "no_change_authorized",
        "no_change_sync_failed",
        "source_desynchronized",
        "orphan_artifact",
        "remote_unavailable",
        "terminal_failure",
    }
)


# --------------------------------------------------------------------------- #
# Fact builders. Every fact is passed explicitly — no store, no clock, no
# network — so the matrix is the same on any machine and the revisions in the
# committed fixture are reproducible.
# --------------------------------------------------------------------------- #


def _observation(
    dimension: str,
    availability: str = "available",
    *,
    provenance: str = "observed",
    detail: str | None = None,
) -> RemoteObservation:
    return RemoteObservation(
        dimension=dimension,
        availability=availability,
        provenance=provenance,
        observed_at=None if availability == "unavailable" else OBSERVED_AT,
        detail=detail,
    )


def _run(run_id: str, item: str, status: str, **over: Any) -> RunRecord:
    """One recorded attempt. ``issue_num`` is 0 for a non-numeric tracker id."""
    fields: dict[str, Any] = {
        "run_id": run_id,
        "issue_num": int(item) if item.isdigit() else 0,
        "branch": f"kew/{run_id}",
        "status": status,
        "exit_code": 0
        if status in ("pending", "success", "awaiting_publication")
        else 1,
        "started_at": STARTED_AT,
        "finished_at": None if status == "pending" else FINISHED_AT,
        "duration_seconds": 1080.0,
        "estimated_cost_usd": 1.75,
        "input_tokens": 90_000,
        "output_tokens": 12_000,
        "total_tokens": 102_000,
        "attempt_number": 1,
        "runner_name": LANE_RUNNER,
        "model": LANE_MODEL,
        "item_id": item,
        "repo": DELIVERY_REPO,
    }
    fields.update(over)
    return RunRecord(**fields)


def _identity(run_id: str, item: str, tracker: str) -> AdmissionIdentity:
    return AdmissionIdentity(
        run_id=run_id,
        attempt_number=1,
        source_tracker=tracker,
        item_id=item,
        delivery_repo=DELIVERY_REPO,
        epic_ref=EPIC_REF,
        node_id="I26",
    )


def _pr(
    availability: str = "available",
    *,
    provenance: str = "observed",
    state: str | None = None,
    url: str | None = None,
    checks: str | None = None,
    mergeability: str | None = None,
    review_decision: str | None = None,
    merged_at: datetime | None = None,
    detail: str | None = None,
) -> PullRequestFact:
    return PullRequestFact(
        observation=_observation(
            "pull_request", availability, provenance=provenance, detail=detail
        ),
        url=url,
        state=state,
        merged_at=merged_at,
        checks=checks,
        mergeability=mergeability,
        review_decision=review_decision,
    )


def _item(
    state: str | None = "open", availability: str = "available"
) -> TrackerItemFact:
    return TrackerItemFact(
        observation=_observation("tracker_item", availability), state=state
    )


def _readiness(
    state: str = "unproven",
    availability: str = "unavailable",
    *,
    detail: str = "",
    fingerprint: str | None = None,
    evidence_ref: str | None = None,
) -> ReadinessFact:
    return ReadinessFact(
        observation=_observation("route_boundary", availability, detail=detail or None),
        state=state,
        fingerprint=fingerprint,
        evidence_ref=evidence_ref,
        detail=detail,
    )


def _proven_boundary() -> ReadinessFact:
    return _readiness(
        "proven",
        "available",
        detail="a settled probe of this exact boundary passed",
        fingerprint="rb1:" + "9" * 16,
        evidence_ref="probe-4242",
    )


def _pressure(
    *,
    used: int = 1,
    total: int = 4,
    engaged: bool = True,
    countable: bool = True,
    availability: str = "available",
    detail: str = "",
) -> ReviewPressureFact:
    return ReviewPressureFact(
        observation=_observation(
            "review_capacity", availability, detail=detail or None
        ),
        delivery_repo=DELIVERY_REPO,
        engaged=engaged,
        countable=countable,
        used=used,
        total=total,
        held=used,
        oldest_hold_age_seconds=7200.0 if used else None,
        diff_burden_lines=1240,
        policy_generation=3,
        detail=detail,
        enforcement_active=False,
    )


def _facts(
    item: str,
    tracker: str,
    runs: tuple[RunRecord, ...],
    **over: Any,
) -> WorkFacts:
    base: dict[str, Any] = {
        "item_id": item,
        "identity": _identity(runs[0].run_id, item, tracker) if runs else None,
        "runs": runs,
        "orphan_prs": (),
        "pull_request": _pr(),
        "tracker_item": _item(),
        "readiness": _readiness(),
        "pressure": _pressure(),
        # The snapshot's own moment. Pinned, because a rule with an AGE in it
        # (a pending row is a run in flight only while it is young —
        # ``PENDING_RUN_MAX_AGE_S`` in kew/service/work.py) would otherwise be
        # measured against the wall clock, and this module's whole claim is
        # that one fact set composes one view forever.
        "as_of": COMPOSED_AT,
    }
    base.update(over)
    return WorkFacts(**base)


def _certificate(
    state: str,
    *,
    availability: str = "available",
    certified_at: str | None = None,
    evidence_ref: str | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """One journey-certificate reading, exactly as the resolver produced it.

    It is BOTH the web claim resolver's side input and the view's own
    ``certificate_status`` dimension (P1.1), so the two cannot drift: every
    case declares one certificate truth and both surfaces answer from it.

    The state written here is the resolver's verdict, never a wish — an
    unreadable carrier is ``unknown``, because
    :mod:`kew.certificate_ref` will not preserve an unverified ``certified``.
    """
    return {
        "state": state,
        "certified_at": certified_at,
        "evidence_ref": evidence_ref,
        "detail": detail,
        "observation": {
            "availability": availability,
            "observed_at": None
            if availability == "unavailable"
            else (OBSERVED_AT.isoformat()),
            "detail": detail or None,
        },
    }


def _certificate_fact(reading: dict[str, Any] | None) -> CertificateFact:
    """The declared reading as the view's own fact — a straight adaptation.

    Nothing is re-derived here. The resolver already failed closed when it
    produced the reading; restating its rules in a fixture would let the two
    drift, which is the exact defect this matrix exists to catch.
    """
    if reading is None:
        return _unobserved_certificate()
    observation = reading["observation"]
    available = observation["availability"] != "unavailable"
    return CertificateFact(
        observation=RemoteObservation(
            dimension="certificate_evidence",
            availability=observation["availability"],
            provenance="observed" if available else "error",
            observed_at=OBSERVED_AT if observation["observed_at"] else None,
            detail=observation["detail"],
        ),
        state=reading["state"],
        certified_at=reading["certified_at"],
        evidence_ref=reading["evidence_ref"],
        detail=reading["detail"],
    )


@dataclass(frozen=True)
class Case:
    """One golden fixture: the facts, the certificate beside them, the truth."""

    name: str
    summary: str
    tracker: str
    claim_shape: str
    facts: WorkFacts
    certification: dict[str, Any] | None
    expected: dict[str, Any]


def _expect(
    *,
    item_ref: str,
    execution: str,
    artifact: str,
    publication: str,
    judgment: str,
    outcome: str,
    synchronization: str,
    no_change: str,
    readiness: str,
    route_claim: str,
    exception: str,
    blocking: bool,
    safest_action: str,
    blocked_by: str | None,
    actions: list[dict[str, Any]],
    availability: dict[str, str],
    saturated: bool,
    countable: bool,
    claims: dict[str, str],
    attempts: int = 1,
) -> dict[str, Any]:
    """The cross-surface truth for one case, declared before it is composed."""
    worst = (
        "unavailable"
        if "unavailable" in availability.values()
        else ("stale" if "stale" in availability.values() else "available")
    )
    return {
        "item_ref": item_ref,
        "execution_state": execution,
        "attempts": attempts,
        "artifact_state": artifact,
        "artifact_publication": publication,
        "judgment_state": judgment,
        "outcome_state": outcome,
        "source_synchronization": synchronization,
        "no_change_state": no_change,
        "readiness_state": readiness,
        "route_claim": route_claim,
        "exception": exception,
        "exception_blocking": blocking,
        "safest_action": safest_action,
        "blocked_by": blocked_by,
        "actions": actions,
        "availability": availability,
        "worst_availability": worst,
        "pressure_saturated": saturated,
        "pressure_countable": countable,
        "claims": claims,
    }


def _offer(
    action: str,
    target_kind: str,
    target_ref: str,
    *,
    safest: bool = True,
    requires_reason: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "target_kind": target_kind,
        "target_ref": target_ref,
        "safest": safest,
        "requires_reason": requires_reason,
    }


#: The certificate dimension is deliberately absent: its availability is part
#: of the reading each case DECLARES, so it is folded in at document time
#: rather than restated (and possibly contradicted) case by case.
_ALL_READ = {d: "available" for d in OBSERVED_DIMENSIONS if d != "certificate_evidence"}


def _avail(**over: str) -> dict[str, str]:
    return {**_ALL_READ, **over}


def _claims(execution: str, certification: str) -> dict[str, str]:
    return {
        "capability": "configured",
        "execution": execution,
        "certification": certification,
    }


# --------------------------------------------------------------------------- #
# The matrix. GitHub numeric ids and an opaque Linear key, each at a named
# revision, covering every customer state the E14 acceptance names.
# --------------------------------------------------------------------------- #

GH = "4242"
LINEAR = "SFD-12"


def _case_in_flight() -> Case:
    facts = _facts(
        GH,
        "github",
        (_run("run-inflight", GH, "pending"),),
        pull_request=_pr("unavailable", provenance="unobserved", detail="no artifact"),
        readiness=_readiness(detail="nothing has probed this boundary"),
    )
    return Case(
        name="github_in_flight",
        summary="an attempt is running; a second dispatch would duplicate it",
        tracker="github",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="in_flight",
            artifact="absent",
            publication="not_required",
            judgment="not_applicable",
            outcome="in_progress",
            synchronization="unknown",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="in_flight",
            blocking=False,
            safest_action="wait",
            blocked_by=None,
            actions=[],
            availability=_avail(
                pull_request="unavailable", route_boundary="unavailable"
            ),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _case_awaiting_publication() -> Case:
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-preserved",
                GH,
                "awaiting_publication",
                reported_model=LANE_MODEL,
            ),
        ),
        pull_request=_pr(
            "unavailable", provenance="error", detail="the forge could not be read"
        ),
        readiness=_readiness(
            "unknown", detail="the route boundary could not be read from here"
        ),
    )
    return Case(
        name="github_awaiting_publication_forge_down",
        summary=(
            "the implementation is preserved on its branch; finalize that same "
            "artifact even with the forge unreadable"
        ),
        tracker="github",
        claim_shape="prior_certificate_during_outage",
        facts=facts,
        certification=_certificate(
            "certified",
            certified_at="2026-07-30T09:00:00+00:00",
            detail="",
        ),
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="preserved",
            publication="pending",
            judgment="not_applicable",
            outcome="in_progress",
            synchronization="unknown",
            no_change="not_applicable",
            readiness="unknown",
            route_claim="honored",
            exception="awaiting_publication",
            blocking=False,
            safest_action="finalize_publication",
            blocked_by=None,
            actions=[_offer("finalize_publication", "run", "run-preserved")],
            availability=_avail(
                pull_request="unavailable", route_boundary="unavailable"
            ),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "certified"),
        ),
    )


def _case_publication_risk_hold() -> Case:
    facts = _facts(
        GH,
        "github",
        (_run("run-held", GH, "awaiting_publication", reported_model=LANE_MODEL),),
        pull_request=_pr(),
        publication=PublicationFact(
            state="reserved",
            review_state=REVIEW_PENDING,
            risk_reasons=("1240 diff lines", "touches the serve auth boundary"),
            reserved_at="2026-08-18T11:50:00+00:00",
            job_id="pj-7",
        ),
        readiness=_proven_boundary(),
        pressure=_pressure(used=3, total=4),
    )
    return Case(
        name="github_publication_risk_hold",
        summary="publication is reserved against human-review capacity on purpose",
        tracker="github",
        claim_shape="certificate_evidence_unavailable",
        facts=facts,
        certification=_certificate(
            "unknown",
            availability="unavailable",
            detail="the certificate evidence could not be read",
        ),
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="preserved",
            publication="pending",
            judgment="not_applicable",
            outcome="in_progress",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="proven",
            route_claim="honored",
            exception="publication_risk_hold",
            blocking=True,
            safest_action="confirm_publication_risk",
            blocked_by=None,
            actions=[_offer("confirm_publication_risk", "publication_job", "pj-7")],
            availability=_avail(),
            saturated=False,
            countable=True,
            claims=_claims("verified", "unknown"),
        ),
    )


def _case_capacity_hold() -> Case:
    facts = _facts(
        GH,
        "github",
        (_run("run-capacity", GH, "failure", reported_model=LANE_MODEL),),
        readiness=_proven_boundary(),
        pressure=_pressure(used=4, total=4),
    )
    return Case(
        name="github_review_capacity_saturated",
        summary="a retryable failure a person has no room to review",
        tracker="github",
        claim_shape="anchored_certified",
        facts=facts,
        certification=_certificate(
            "certified",
            certified_at="2026-08-12T16:20:00+00:00",
            evidence_ref="anchor:sha256:" + "b" * 16,
        ),
        expected=_expect(
            item_ref="#4242",
            execution="terminal",
            artifact="absent",
            publication="not_required",
            judgment="not_applicable",
            outcome="failed",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="proven",
            route_claim="honored",
            exception="review_capacity_saturated",
            blocking=True,
            safest_action="wait",
            blocked_by="review_capacity",
            actions=[],
            availability=_avail(),
            saturated=True,
            countable=True,
            claims=_claims("verified", "certified"),
        ),
    )


def _case_capacity_unreadable() -> Case:
    """An unreadable review queue is the one most likely to be full."""
    facts = _facts(
        GH,
        "github",
        (_run("run-blind-capacity", GH, "failure"),),
        pressure=_pressure(
            used=0,
            total=0,
            countable=False,
            availability="unavailable",
            detail="the delivery repository could not be read",
        ),
    )
    return Case(
        name="github_review_capacity_unreadable",
        summary="review capacity could not be read, so it counts as full",
        tracker="github",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="terminal",
            artifact="absent",
            publication="not_required",
            judgment="not_applicable",
            outcome="failed",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="review_capacity_saturated",
            blocking=True,
            safest_action="wait",
            blocked_by="review_capacity",
            actions=[],
            availability=_avail(
                review_capacity="unavailable", route_boundary="unavailable"
            ),
            saturated=True,
            countable=False,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _case_awaiting_judgment() -> Case:
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-published",
                GH,
                "success",
                pr_url="https://example.invalid/pr/7",
                reported_model=LANE_MODEL,
            ),
        ),
        pull_request=_pr(
            state="open",
            url="https://example.invalid/pr/7",
            checks="passing",
            mergeability="clean",
            review_decision="review_required",
        ),
        readiness=_proven_boundary(),
    )
    return Case(
        name="github_published_awaiting_judgment",
        summary="the artifact is published and a person owns the next decision",
        tracker="github",
        claim_shape="live_verified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            outcome="in_progress",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="proven",
            route_claim="honored",
            exception="awaiting_human_decision",
            blocking=False,
            safest_action="await_human_decision",
            blocked_by=None,
            actions=[],
            availability=_avail(),
            saturated=False,
            countable=True,
            claims=_claims("verified", "uncertified"),
        ),
    )


def _case_successor_head() -> Case:
    """A second attempt pushed a successor head onto the same artifact."""
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-successor",
                GH,
                "success",
                attempt_number=2,
                branch="kew/run-published",
                pr_url="https://example.invalid/pr/7",
            ),
            _run("run-published", GH, "success", pr_url="https://example.invalid/pr/7"),
        ),
        pull_request=_pr(
            state="open",
            url="https://example.invalid/pr/7",
            checks="pending",
            mergeability="clean",
            review_decision="review_required",
        ),
    )
    return Case(
        name="github_successor_head_awaiting_rereview",
        summary=(
            "attempt 2 pushed a successor head to the same pull request; the "
            "review thread is intact and re-review is pending"
        ),
        tracker="github",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            attempts=2,
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            outcome="in_progress",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="awaiting_human_decision",
            blocking=False,
            safest_action="await_human_decision",
            blocked_by=None,
            actions=[],
            availability=_avail(route_boundary="unavailable"),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _case_changes_requested() -> Case:
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-reviewed",
                GH,
                "success",
                pr_url="https://example.invalid/pr/7",
                reported_model=LANE_MODEL,
            ),
        ),
        pull_request=_pr(
            state="open",
            url="https://example.invalid/pr/7",
            checks="passing",
            mergeability="clean",
            review_decision="changes_requested",
        ),
        readiness=_proven_boundary(),
    )
    return Case(
        name="github_changes_requested",
        summary="a reviewer asked for changes; the ball is back in our court",
        tracker="github",
        claim_shape="recertification_due",
        facts=facts,
        certification=_certificate("recertification_due"),
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="published",
            publication="published",
            judgment="changes_requested",
            outcome="in_progress",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="proven",
            route_claim="honored",
            exception="changes_requested",
            blocking=True,
            safest_action="address_review_feedback",
            blocked_by=None,
            actions=[],
            availability=_avail(),
            saturated=False,
            countable=True,
            claims=_claims("verified", "recertification_due"),
        ),
    )


def _case_tracker_sync_pending() -> Case:
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-merged",
                GH,
                "success",
                pr_url="https://example.invalid/pr/7",
                reported_model=LANE_MODEL,
            ),
        ),
        pull_request=_pr(
            state="merged",
            url="https://example.invalid/pr/7",
            merged_at=datetime(2026, 8, 18, 11, 55, tzinfo=UTC),
            checks="passing",
            mergeability="not_applicable",
            review_decision="approved",
        ),
        tracker_item=_item("open"),
        readiness=_proven_boundary(),
    )
    return Case(
        name="github_tracker_sync_pending",
        summary="the artifact merged but the source item is still open",
        tracker="github",
        claim_shape="live_verified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="published",
            publication="published",
            judgment="accepted",
            outcome="delivered",
            synchronization="pending",
            no_change="not_applicable",
            readiness="proven",
            route_claim="honored",
            exception="source_desynchronized",
            blocking=False,
            safest_action="synchronize_tracker",
            blocked_by=None,
            actions=[],
            availability=_avail(),
            saturated=False,
            countable=True,
            claims=_claims("verified", "uncertified"),
        ),
    )


def _case_terminal_failure() -> Case:
    facts = _facts(
        GH,
        "github",
        (_run("run-failed", GH, "failure", error_category="test_failure"),),
    )
    return Case(
        name="github_terminal_failure_redispatchable",
        summary="the attempt left no artifact to preserve, so a dispatch is safe",
        tracker="github",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="terminal",
            artifact="absent",
            publication="not_required",
            judgment="not_applicable",
            outcome="failed",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="terminal_failure",
            blocking=True,
            safest_action="dispatch",
            blocked_by=None,
            actions=[_offer("dispatch", "item", GH)],
            availability=_avail(route_boundary="unavailable"),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _case_stale_reading() -> Case:
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-cached",
                GH,
                "success",
                pr_url="https://example.invalid/pr/7",
                pr_state="open",
                reported_model=LANE_MODEL,
            ),
        ),
        pull_request=_pr(
            "stale",
            provenance="cached",
            state="open",
            url="https://example.invalid/pr/7",
            detail="read from the run's own record, not the forge",
        ),
        readiness=_readiness(
            "proven",
            "stale",
            detail="a past probe of this boundary is not current reachability",
            fingerprint="rb1:" + "9" * 16,
            evidence_ref="probe-4242",
        ),
    )
    return Case(
        name="github_stale_cached_judgment",
        summary="an old-but-real reading is usable, and says that it is old",
        tracker="github",
        claim_shape="expired_live_with_prior_certificate",
        facts=facts,
        certification=_certificate(
            "certified", certified_at="2026-06-01T00:00:00+00:00"
        ),
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            outcome="in_progress",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="proven",
            route_claim="honored",
            exception="awaiting_human_decision",
            blocking=False,
            safest_action="await_human_decision",
            blocked_by=None,
            actions=[],
            availability=_avail(pull_request="stale", route_boundary="stale"),
            saturated=False,
            countable=True,
            claims=_claims("expired", "certified"),
        ),
    )


def _case_unavailable_forge() -> Case:
    facts = _facts(
        GH,
        "github",
        (
            _run(
                "run-unreadable",
                GH,
                "success",
                pr_url="https://example.invalid/pr/7",
            ),
        ),
        pull_request=_pr(
            "unavailable",
            provenance="error",
            detail="the forge returned 503",
        ),
    )
    return Case(
        name="github_published_artifact_unreadable",
        summary="a published artifact nobody could read is not a decided one",
        tracker="github",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="completed",
            artifact="published",
            publication="published",
            judgment="unknown",
            outcome="unknown",
            synchronization="unknown",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="remote_unavailable",
            blocking=False,
            safest_action="observe_remote",
            blocked_by="pull_request",
            actions=[],
            availability=_avail(
                pull_request="unavailable", route_boundary="unavailable"
            ),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _case_orphan_artifact() -> Case:
    facts = _facts(
        GH,
        "github",
        (_run("run-orphaned", GH, "failure"),),
        orphan_prs=(
            OrphanPRRecord(
                pr_number=9001,
                url="https://example.invalid/pr/9001",
                branch="kew/run-orphaned",
                reason="failed-run",
                detected_at=OBSERVED_AT,
                acked_at=None,
            ),
        ),
        pull_request=_pr(
            state="open",
            url="https://example.invalid/pr/9001",
            checks="passing",
            mergeability="clean",
            review_decision="review_required",
        ),
    )
    return Case(
        name="github_orphan_artifact",
        summary="an artifact exists with no successful run behind it",
        tracker="github",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="#4242",
            execution="terminal",
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            outcome="in_progress",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="orphan_artifact",
            blocking=True,
            safest_action="inspect",
            blocked_by=None,
            actions=[_offer("acknowledge_orphan", "orphan_pr", "9001")],
            availability=_avail(route_boundary="unavailable"),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _no_change_case(
    name: str,
    summary: str,
    *,
    run_id: str,
    fact: NoChangeFact,
    state: str,
    exception: str,
    blocking: bool,
    safest_action: str,
    actions: list[dict[str, Any]],
) -> Case:
    facts = _facts(
        LINEAR,
        "linear",
        (_run(run_id, LINEAR, "success"),),
        no_change=fact,
    )
    return Case(
        name=name,
        summary=summary,
        tracker="linear",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref=LINEAR,
            execution="completed",
            artifact="absent",
            publication="not_required",
            judgment="not_applicable",
            outcome="unknown",
            synchronization="not_applicable",
            no_change=state,
            readiness="unproven",
            route_claim="unverified",
            exception=exception,
            blocking=blocking,
            safest_action=safest_action,
            blocked_by=None,
            actions=actions,
            availability=_avail(route_boundary="unavailable"),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


def _case_no_change_pending() -> Case:
    return _no_change_case(
        "linear_no_change_candidate",
        "a zero-diff exit awaiting a person's decision",
        run_id="run-nochange-1",
        fact=NoChangeFact(
            state="candidate",
            policy="confirm",
            reasons=("no files changed",),
            judgment_id="nc-1",
        ),
        state="pending",
        exception="no_change_pending",
        blocking=True,
        safest_action="confirm_no_change",
        actions=[
            _offer("confirm_no_change", "no_change_judgment", "nc-1"),
            _offer(
                "reject_no_change",
                "no_change_judgment",
                "nc-1",
                safest=False,
                requires_reason=True,
            ),
        ],
    )


def _case_no_change_authorized() -> Case:
    return _no_change_case(
        "linear_no_change_authorized",
        "the zero-diff outcome is authorized and still owes the source an update",
        run_id="run-nochange-2",
        fact=NoChangeFact(
            state="authorized",
            policy="confirm",
            reasons=("no files changed",),
            sync_outstanding=1,
            effects_outstanding=1,
            judgment_id="nc-2",
        ),
        state="synchronizing",
        exception="no_change_authorized",
        blocking=False,
        safest_action="synchronize_source",
        actions=[_offer("retry_no_change_sync", "no_change_judgment", "nc-2")],
    )


def _case_no_change_sync_failed() -> Case:
    return _no_change_case(
        "linear_no_change_sync_failed",
        "an authorized source write was abandoned; the item is open against it",
        run_id="run-nochange-3",
        fact=NoChangeFact(
            state="sync_pending",
            policy="confirm",
            reasons=("no files changed",),
            sync_failed=1,
            judgment_id="nc-3",
        ),
        state="sync_failed",
        exception="no_change_sync_failed",
        blocking=True,
        safest_action="synchronize_source",
        actions=[
            _offer("retry_no_change_sync", "no_change_judgment", "nc-3"),
            _offer(
                "reject_no_change",
                "no_change_judgment",
                "nc-3",
                safest=False,
                requires_reason=True,
            ),
        ],
    )


def _case_linear_unqueueable() -> Case:
    """A retryable failure on a tracker kew cannot queue against.

    Dispatch is label-based on GitHub, so an opaque Linear key has nothing to
    queue. The verdict still says ``dispatch`` — that IS the safest thing for a
    person to arrange — and the OFFER is withheld, because degrading the opaque
    id into a numeric shim to make one fit is the exact lie this projection ends.
    """
    facts = _facts(
        "SFD-77",
        "linear",
        (_run("run-linear-failed", "SFD-77", "killed"),),
    )
    return Case(
        name="linear_terminal_failure_unqueueable",
        summary="a retryable failure whose opaque tracker id kew cannot queue",
        tracker="linear",
        claim_shape="configured_live_unverified",
        facts=facts,
        certification=None,
        expected=_expect(
            item_ref="SFD-77",
            execution="terminal",
            artifact="absent",
            publication="not_required",
            judgment="not_applicable",
            outcome="failed",
            synchronization="not_applicable",
            no_change="not_applicable",
            readiness="unproven",
            route_claim="unverified",
            exception="terminal_failure",
            blocking=True,
            safest_action="dispatch",
            blocked_by=None,
            actions=[],
            availability=_avail(route_boundary="unavailable"),
            saturated=False,
            countable=True,
            claims=_claims("unverified", "uncertified"),
        ),
    )


CASES: tuple[Case, ...] = (
    _case_in_flight(),
    _case_awaiting_publication(),
    _case_publication_risk_hold(),
    _case_capacity_hold(),
    _case_capacity_unreadable(),
    _case_awaiting_judgment(),
    _case_successor_head(),
    _case_changes_requested(),
    _case_tracker_sync_pending(),
    _case_terminal_failure(),
    _case_stale_reading(),
    _case_unavailable_forge(),
    _case_orphan_artifact(),
    _case_no_change_pending(),
    _case_no_change_authorized(),
    _case_no_change_sync_failed(),
    _case_linear_unqueueable(),
)

BY_NAME = {case.name: case for case in CASES}


# --------------------------------------------------------------------------- #
# The golden document. `view_revision` is a digest of the composed facts, so it
# is generated and committed — never hand-written.
# --------------------------------------------------------------------------- #

_FIXTURE_COMMENT = (
    "Golden WorkView matrix (I26). Generated by "
    "kew/tests/test_surface_convergence.py and read by it and by "
    "frontend/src/lib/surface-convergence.test.ts, so both surfaces answer "
    "for the SAME facts at the SAME view_revision. `certification` is the "
    "reading the web claim resolver takes beside the view; the SAME reading "
    "composes the view's own certificate_status dimension. Regenerate with "
    "KEW_WRITE_WORK_VIEW_GOLDEN=1 uv run pytest tests/test_surface_convergence.py"
)


def _case_facts(case: Case) -> WorkFacts:
    """The case's facts INCLUDING its declared certificate reading."""
    return replace(case.facts, certificate=_certificate_fact(case.certification))


def _compose(case: Case) -> WorkViewDTO:
    return compose_work_view(_case_facts(case), composed_at=COMPOSED_AT)


def _case_document(case: Case) -> dict[str, Any]:
    view = _compose(case)
    expected = {**case.expected, "view_revision": view.view_revision}
    return {
        "name": case.name,
        "summary": case.summary,
        "tracker": case.tracker,
        "claim_shape": case.claim_shape,
        "certification": case.certification,
        "expected": expected,
        "view": view.model_dump(),
    }


def _live_document() -> dict[str, Any]:
    return {
        "$comment": _FIXTURE_COMMENT,
        "composed_at": COMPOSED_AT.isoformat(),
        "cases": [_case_document(case) for case in CASES],
    }


def _golden() -> dict[str, Any]:
    if not FIXTURE.exists():  # pragma: no cover - only before the first write
        pytest.fail(f"missing golden matrix: {FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _golden_case(name: str) -> dict[str, Any]:
    return next(c for c in _golden()["cases"] if c["name"] == name)


NAMES = [case.name for case in CASES]


@pytest.fixture(params=NAMES, ids=NAMES)
def golden(request: Any) -> dict[str, Any]:
    """One committed fixture, with its declared truth and its composed view."""
    return _golden_case(request.param)


def _view_of(golden: dict[str, Any]) -> WorkViewDTO:
    """The committed document, revalidated exactly as the web layer receives it."""
    return WorkViewDTO.model_validate(golden["view"])


def _offer_shape(action: Any) -> dict[str, Any]:
    return {
        "action": action.action,
        "target_kind": action.target_kind,
        "target_ref": action.target_ref,
        "safest": action.safest,
        "requires_reason": action.requires_reason,
    }


def test_write_golden_matrix() -> None:
    """Rewrite the committed fixture. Opt-in: a generator is not a test."""
    if not os.environ.get("KEW_WRITE_WORK_VIEW_GOLDEN"):
        pytest.skip("set KEW_WRITE_WORK_VIEW_GOLDEN=1 to regenerate the matrix")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(_live_document(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


class TestMatrixCoverage:
    """Task 94 — the matrix itself is the contract; it must cover the release."""

    def test_every_named_customer_state_is_present(self) -> None:
        covered = {c["expected"]["exception"] for c in _golden()["cases"]}
        assert covered >= REQUIRED_EXCEPTIONS

    def test_every_named_claim_shape_is_present(self) -> None:
        covered = {c["claim_shape"] for c in _golden()["cases"]}
        assert covered >= REQUIRED_CLAIM_SHAPES

    def test_both_trackers_are_represented_with_their_own_id_form(self) -> None:
        """A GitHub id renders as ``#N``; any other tracker's rides through bare."""
        refs = [(c["tracker"], c["expected"]["item_ref"]) for c in _golden()["cases"]]
        assert {tracker for tracker, _ in refs} == {"github", "linear"}
        for tracker, ref in refs:
            assert ref.startswith("#") is (tracker == "github")

    def test_every_availability_word_is_exercised(self) -> None:
        worst = {c["expected"]["worst_availability"] for c in _golden()["cases"]}
        assert worst == {"available", "stale", "unavailable"}

    def test_committed_matrix_is_the_one_this_module_builds(self) -> None:
        """No fixture drift: the file is exactly what the case table composes."""
        assert _golden() == _live_document()


class TestSameFactsSameView:
    """Task 95 — the committed view is what the pure composer actually makes."""

    def test_composer_reproduces_the_committed_view(self, golden: Any) -> None:
        composed = _compose(BY_NAME[golden["name"]])
        assert composed.model_dump() == golden["view"]

    def test_declared_semantics_are_what_the_composer_produced(
        self, golden: Any
    ) -> None:
        view, want = _view_of(golden), golden["expected"]
        assert view.identity.item_ref == want["item_ref"]
        assert view.execution.state == want["execution_state"]
        assert view.execution.attempts == want["attempts"]
        assert view.artifact.state == want["artifact_state"]
        assert view.artifact.publication == want["artifact_publication"]
        assert view.judgment.state == want["judgment_state"]
        assert view.outcome.state == want["outcome_state"]
        assert view.source.synchronization == want["source_synchronization"]
        assert view.no_change.state == want["no_change_state"]
        assert view.readiness.state == want["readiness_state"]
        assert view.route.claim == want["route_claim"]
        assert view.exception.state == want["exception"]
        assert view.exception.blocking is want["exception_blocking"]
        assert view.safest_action.action == want["safest_action"]
        assert view.safest_action.blocked_by == want["blocked_by"]
        assert [_offer_shape(a) for a in view.actions] == want["actions"]
        assert view.view_revision == want["view_revision"]

    def test_every_remote_dimension_reports_its_own_availability(
        self, golden: Any
    ) -> None:
        view, want = _view_of(golden), golden["expected"]
        seen = {o.dimension: o.availability for o in view.observations}
        assert seen == want["availability"]
        assert tuple(sorted(seen)) == OBSERVED_DIMENSIONS

    def test_review_pressure_reads_the_same_on_every_surface(self, golden: Any) -> None:
        pressure = _view_of(golden).review_pressure
        assert pressure.saturated is golden["expected"]["pressure_saturated"]
        assert pressure.countable is golden["expected"]["pressure_countable"]
        assert pressure.delivery_repo == DELIVERY_REPO


class TestCliProjection:
    """``kew status --run --json`` — the machine contract."""

    def _payload(self, golden: Any) -> dict[str, Any]:
        from kew.cli import _status_run_payload

        record = BY_NAME[golden["name"]].facts.latest
        assert record is not None
        return _status_run_payload(record, _view_of(golden))

    def test_cli_carries_the_whole_projection_unaltered(self, golden: Any) -> None:
        assert self._payload(golden)["work"] == golden["view"]

    def test_cli_hoists_the_same_identity_revision_and_action(
        self, golden: Any
    ) -> None:
        payload, want = self._payload(golden), golden["expected"]
        assert payload["item_ref"] == want["item_ref"]
        assert payload["view_revision"] == want["view_revision"]
        assert payload["route"]["claim"] == want["route_claim"]
        assert payload["safest_action"]["action"] == want["safest_action"]
        assert payload["safest_action"]["blocked_by"] == want["blocked_by"]
        assert [a["action"] for a in payload["actions"]] == [
            a["action"] for a in want["actions"]
        ]

    def test_cli_evidence_names_every_reading_with_its_availability(
        self, golden: Any
    ) -> None:
        rows = self._payload(golden)["evidence"]
        seen = {row["dimension"]: row["availability"] for row in rows}
        assert seen == golden["expected"]["availability"]

    def test_cli_healthy_is_on_normal_and_currently_readable(self, golden: Any) -> None:
        want = golden["expected"]
        expected = want["exception"] == "none" and want["worst_availability"] == (
            "available"
        )
        assert self._payload(golden)["healthy"] is expected


class TestTuiProjection:
    """The terminal row's view model — a rendering, never a second resolver."""

    def _display(self, golden: Any) -> Any:
        from kew.tui.state import work_display

        return work_display(_view_of(golden))

    def test_tui_row_says_the_same_thing_as_the_projection(self, golden: Any) -> None:
        row, want = self._display(golden), golden["expected"]
        assert row.read is True
        assert row.ref == want["item_ref"]
        assert row.exception == want["exception"]
        assert row.action == want["safest_action"]
        assert row.revision == want["view_revision"]

    def test_tui_evidence_word_matches_the_worst_reading(self, golden: Any) -> None:
        worst = golden["expected"]["worst_availability"]
        expected = "current" if worst == "available" else worst
        assert self._display(golden).evidence == expected


class TestWebProjection:
    """The DTO the console receives — the same document, field for field."""

    def test_web_dto_serializes_the_committed_document(self, golden: Any) -> None:
        served = json.loads(_view_of(golden).model_dump_json())
        assert served == golden["view"]

    def test_web_read_route_returns_the_canonical_view_model(self) -> None:
        from typing import get_type_hints

        from kew.serve import api_verbs

        assert get_type_hints(api_verbs.work_view)["return"] is WorkViewDTO


class TestOneRevision:
    """The whole point: three surfaces, one revision, one instant."""

    def test_every_surface_reports_the_same_revision(self, golden: Any) -> None:
        from kew.cli import _status_run_payload
        from kew.tui.state import work_display

        view = _view_of(golden)
        record = BY_NAME[golden["name"]].facts.latest
        assert record is not None
        cli = _status_run_payload(record, view)["view_revision"]
        tui = work_display(view).revision
        web = json.loads(view.model_dump_json())["view_revision"]
        assert cli == tui == web == golden["expected"]["view_revision"]

    def test_the_revision_is_a_digest_of_facts_not_of_the_clock(
        self, golden: Any
    ) -> None:
        case = BY_NAME[golden["name"]]
        later = compose_work_view(
            _case_facts(case), composed_at=datetime(2027, 1, 1, tzinfo=UTC)
        )
        assert later.view_revision == golden["expected"]["view_revision"]
        assert later.composed_at != golden["view"]["composed_at"]


class _FakeApp:
    """The minimum a TUI action handler reads: the projection and a toast."""

    def __init__(self, view: WorkViewDTO) -> None:
        self._view = view
        self.state = SimpleNamespace(work_view=lambda item_id: view)
        self.notices: list[str] = []

    def notify(self, message: str, **_: Any) -> None:
        self.notices.append(message)


class TestActionParity:
    """Task 96 — one verb set, one gate, and no way around either."""

    def test_every_offered_action_is_an_i20_verb(self, golden: Any) -> None:
        from kew.service.verbs import _WORK_EFFECTS

        for offer in golden["expected"]["actions"]:
            assert offer["action"] in _WORK_EFFECTS

    def test_only_the_first_offer_is_the_safest_one(self, golden: Any) -> None:
        """Safest-first, and exactly one of them.

        The safest action NAMES the step (``synchronize_source``); the first
        offer is the verb that PERFORMS it (``retry_no_change_sync``). They
        coincide wherever the step is itself an executable verb, and anything
        after index 0 is the authorized counterpart decision.
        """
        from kew.service.verbs import _WORK_EFFECTS

        want = golden["expected"]
        offers = want["actions"]
        assert [o["safest"] for o in offers] == [i == 0 for i in range(len(offers))]
        if offers and want["safest_action"] in _WORK_EFFECTS:
            assert offers[0]["action"] == want["safest_action"]

    def test_every_offer_names_the_thing_it_acts_on(self, golden: Any) -> None:
        for offer in golden["expected"]["actions"]:
            assert offer["target_ref"]
            assert offer["target_kind"] in {
                "run",
                "publication_job",
                "no_change_judgment",
                "orphan_pr",
                "item",
                "fleet",
            }

    def test_an_unoffered_verb_is_refused_by_the_single_gate(self, golden: Any) -> None:
        from kew.service.verbs import (
            _WORK_EFFECTS,
            WorkActionUnavailable,
            _offer_or_raise,
        )

        view = _view_of(golden)
        offered = {a.action for a in view.actions}
        for verb in sorted(set(_WORK_EFFECTS) - offered):
            with pytest.raises(WorkActionUnavailable):
                _offer_or_raise(view, verb)

    def test_an_offered_verb_passes_the_same_gate(self, golden: Any) -> None:
        from kew.service.verbs import _offer_or_raise

        view = _view_of(golden)
        for offer in view.actions:
            assert _offer_or_raise(view, offer.action).target_ref == offer.target_ref

    def test_the_tui_cannot_press_a_button_the_projection_withholds(
        self, golden: Any
    ) -> None:
        from kew.service.verbs import _WORK_EFFECTS
        from kew.tui.actions import refuses_work_action

        view = _view_of(golden)
        item = view.identity.item_id or ""
        offered = {a.action for a in view.actions}
        for verb in sorted(_WORK_EFFECTS):
            app = _FakeApp(view)
            assert refuses_work_action(app, item, verb) is (verb not in offered)

    def test_no_surface_may_dispatch_over_a_preserved_artifact(
        self, golden: Any
    ) -> None:
        """The redispatch that destroys completed work is offered nowhere."""
        if golden["expected"]["artifact_state"] != "preserved":
            pytest.skip("this fixture has no preserved artifact")
        assert "dispatch" not in {a["action"] for a in golden["expected"]["actions"]}

    def test_an_action_kew_does_not_perform_is_a_link_not_a_button(
        self, golden: Any
    ) -> None:
        """A safest action with no verb still points at a canonical surface.

        ``await_human_decision``, ``resolve_conflict``, ``inspect`` and
        ``observe_remote`` are steps kew does not take. The projection offers
        nothing for them, and every surface deep-links to the run instead —
        which is what stops "there is no button" from becoming "invent one".
        """
        from kew.service.verbs import _WORK_EFFECTS

        want = golden["expected"]
        if want["actions"]:
            assert want["safest_action"] in _WORK_EFFECTS or all(
                offer["action"] in _WORK_EFFECTS for offer in want["actions"]
            )
            pytest.skip("this fixture offers an executable verb")
        assert _view_of(golden).identity.run_id

    def test_the_web_action_request_names_no_target_of_its_own(self) -> None:
        """A client sends a verb and a revision — never which run or job."""
        from kew.serve.api_verbs import WorkActionBody

        assert set(WorkActionBody.model_fields) == {
            "action",
            "expected_revision",
            "reason",
        }


class TestClaimOrthogonality:
    """Execution validity and journey certification are different questions."""

    def test_the_view_and_the_claim_resolver_read_one_certificate(
        self, golden: Any
    ) -> None:
        """P1.1 — the view carries the certificate, and it is the SAME reading
        the web claim resolver takes beside it. Before P1.1 the web rendered
        four certificate states with nothing behind them; a divergence here is
        that honesty gap reopening."""
        view = _view_of(golden)
        reading = golden["certification"]
        if reading is None:
            assert view.certificate_status.state == "unknown"
            assert view.certificate_status.observation.availability == "unavailable"
            return
        assert view.certificate_status.state == reading["state"]
        assert view.certificate_status.certified_at == reading["certified_at"]
        assert (
            view.certificate_status.observation.availability
            == (reading["observation"]["availability"])
        )

    def test_unreadable_certificate_evidence_is_never_a_certified_view(
        self, golden: Any
    ) -> None:
        """Fail-closed, in the document itself: a non-available observation
        can never sit beside a ``certified`` state, so no surface can preserve
        an unverified claim by rendering the state and ignoring the read."""
        certificate = _view_of(golden).certificate_status
        if certificate.observation.availability != "available":
            assert certificate.state == "unknown"
            assert certificate.certified_at is None

    def test_the_certificate_moves_the_revision(self, golden: Any) -> None:
        """It IS in the digest now — two views that differ only in certificate
        evidence must not compare equal, or a surface could show a stale
        certification claim at a revision that says it is current."""
        case = BY_NAME[golden["name"]]
        assert _compose(case).view_revision == golden["expected"]["view_revision"]
        stripped = compose_work_view(
            replace(case.facts, certificate=_unobserved_certificate()),
            composed_at=COMPOSED_AT,
        )
        if golden["certification"] is not None:
            assert stripped.view_revision != golden["expected"]["view_revision"]

    def test_a_live_probe_never_implies_a_certificate(self) -> None:
        verified = [
            c
            for c in _golden()["cases"]
            if c["expected"]["claims"]["execution"] == "verified"
        ]
        assert verified
        assert any(
            c["expected"]["claims"]["certification"] == "uncertified" for c in verified
        )

    def test_a_certificate_never_implies_current_reachability(self) -> None:
        certified = [
            c
            for c in _golden()["cases"]
            if c["expected"]["claims"]["certification"] == "certified"
        ]
        assert certified
        assert any(
            c["expected"]["claims"]["execution"] != "verified" for c in certified
        )

    def test_unreadable_certificate_evidence_is_unknown_not_certified(self) -> None:
        for case in _golden()["cases"]:
            certification = case["certification"]
            if certification is None:
                continue
            if certification["observation"]["availability"] != "unavailable":
                continue
            assert case["expected"]["claims"]["certification"] == "unknown"

    def test_an_absent_certificate_reads_uncertified_not_unknown(self) -> None:
        for case in _golden()["cases"]:
            if case["certification"] is None:
                assert case["expected"]["claims"]["certification"] == "uncertified"

    def test_readiness_and_the_execution_claim_agree(self, golden: Any) -> None:
        """The web claim is a rendering of the readiness dimension, not a guess."""
        view, claim = _view_of(golden), golden["expected"]["claims"]["execution"]
        readiness_available = view.readiness.observation.availability
        if claim == "verified":
            assert view.readiness.state == "proven"
            assert readiness_available == "available"
        if claim == "expired":
            assert readiness_available == "stale" or view.readiness.state == "expired"
        if claim == "unverified":
            assert view.readiness.state != "proven" or readiness_available != (
                "available"
            )
