"""PCJ-0/1/3/4 and the C0-C8 certification gates, as deterministic acceptance.

This is the instrument that RUNS certification pre-merge. The live reference
suite (PCJ-2, the paid canary, the real forge) cannot run in CI, so what is
provable here is the part that does not need them: that the shared customer
contract — one work item, ten orthogonal dimensions, one safest action —
answers correctly at every gate the production-certification spec names, with
no network, no clock and no store.

Three rules keep this pack from becoming a hollow pass:

* **Expectations are authored, never dumped.** Each case declares the customer
  truth it expects BEFORE the composer runs, in :class:`Expect`. Nothing here
  reads ``tests/fixtures/product_coherence/work_views.json`` — that file is
  kew's own output, and comparing kew to its own output proves nothing (the
  #1542 parity-proof lesson).
* **Every gate has a distinct assertion.** :data:`CASES` is checked for
  duplicate expectations, and every gate id must be claimed by a case whose
  expectation differs from its siblings'. A gate proven only by a case that
  another gate already proves is not proven.
* **Forbidden outcomes are asserted negatively.** The PCJ-3 recovery rail
  names, per seam, the false outcome that must NOT appear. Each case carries
  those and the pack asserts their absence — a contract can be wrong by
  offering something as easily as by withholding it.

What this pack deliberately does NOT claim: it is a *contract* proof, not a
live one. No case here observes a real provider, a real forge, or real money,
and nothing in it may be read as a journey certificate. That boundary is
enforced in ``tests/test_certification_conformance.py``, which proves the
candidate this pack feeds can never resolve as ``certified``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest
from support import product_coherence as pc

from kew.service.models import WorkViewDTO
from kew.service.work import NoChangeFact, PublicationFact, WorkFacts

# --------------------------------------------------------------------------- #
# The gate catalogue. One sentence per gate, taken from the spec, so a reader
# can check what is claimed without opening the spec beside it.
# --------------------------------------------------------------------------- #

GATES: dict[str, str] = {
    "PCJ-0": "before entrusting work or money, the visible contract is honest",
    "PCJ-1": "one admitted item becomes one decision-ready change and syncs",
    "PCJ-3": "recovery exposes what survived and the safest continuation",
    "PCJ-4": "the outcome is comprehensible, with honest telemetry and identity",
    "C0": "preflight: configured is not live-verified",
    "C1": "admission: a bounded item is admitted once, never twice",
    "C2": "execute and preserve: every recoverable change survives a stop",
    "C3": "verify and comprehend: the host's evidence, not self-attestation",
    "C4": "publish: exactly one artifact, and capacity is reserved atomically",
    "C5": "judge: authorization is bound to the exact SHA, never carried",
    "C6": "reconcile and attest: outcome, source and record agree, or say so",
    "C7": "cross-surface truth: one revision, one safest action, no invention",
    "C8": "recovery: the minimum recovery set is proven, seam by seam",
}


@dataclass(frozen=True)
class Expect:
    """The customer truth for one case, declared before it is composed."""

    execution: str
    artifact: str
    publication: str
    judgment: str
    outcome: str
    synchronization: str
    no_change: str
    readiness: str
    route_claim: str
    exception: str
    blocking: bool
    safest_action: str
    blocked_by: str | None
    actions: tuple[str, ...]
    certification: str


@dataclass(frozen=True)
class Case:
    """One deterministic conformance case."""

    name: str
    gates: tuple[str, ...]
    #: What this case exists to prove, in the customer's terms.
    claim: str
    build: Callable[[], WorkFacts]
    expect: Expect
    #: Actions the spec forbids this state from ever offering or recommending.
    forbidden_actions: tuple[str, ...] = ()
    #: Dimension words this state may never report (the "forbidden false
    #: outcome" column of the PCJ-3 recovery rail).
    forbidden_states: tuple[tuple[str, str], ...] = ()


def _expect(**over: object) -> Expect:
    """A readable, fully-observed baseline; each case overrides what it means.

    The baseline assumes a case whose delivery forge WAS read. Where a case
    leaves the forge unobserved, source synchronization is ``unknown`` rather
    than ``not_applicable`` — "there is nothing to synchronize" is a claim, and
    it cannot be made from a reading nobody took. Those cases say so.
    """
    base: dict[str, object] = {
        "execution": "completed",
        "artifact": "absent",
        "publication": "not_required",
        "judgment": "not_applicable",
        "outcome": "in_progress",
        "synchronization": "not_applicable",
        "no_change": "not_applicable",
        "readiness": "unproven",
        "route_claim": "unverified",
        "exception": "none",
        "blocking": False,
        "safest_action": "none",
        "blocked_by": None,
        "actions": (),
        "certification": "uncertified",
    }
    base.update(over)
    return Expect(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PCJ-0 — prove this route is ready (C0)
# --------------------------------------------------------------------------- #


def _contract_only() -> WorkFacts:
    """Nothing has probed the boundary: configured, live unverified."""
    return pc.facts()


def _boundary_refuted() -> WorkFacts:
    return pc.facts(readiness=pc.refuted_boundary())


def _fleet_stopped() -> WorkFacts:
    return pc.facts(
        readiness=pc.proven_boundary(),
        fleet_stopped=True,
        fleet_stop_reason="a person stopped the fleet",
    )


# --------------------------------------------------------------------------- #
# PCJ-1 — one bounded item becomes one decision-ready change (C1-C6)
# --------------------------------------------------------------------------- #


def _in_flight() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-inflight", pc.GH_ITEM, "pending"),),
        readiness=pc.proven_boundary(),
    )


def _preserved_awaiting_publication() -> WorkFacts:
    return pc.facts(
        runs=(
            pc.run(
                "run-preserved",
                pc.GH_ITEM,
                "awaiting_publication",
                reported_model=pc.LANE_MODEL,
            ),
        ),
        readiness=pc.proven_boundary(),
    )


def _published_awaiting_decision() -> WorkFacts:
    return pc.facts(
        runs=(
            pc.run(
                "run-published",
                pc.GH_ITEM,
                "success",
                pr_url=pc.PR_URL,
                reported_model=pc.LANE_MODEL,
            ),
        ),
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="passing",
            mergeability="clean",
            review_decision="review_required",
        ),
        readiness=pc.proven_boundary(),
    )


def _merged_and_synchronized() -> WorkFacts:
    return pc.facts(
        runs=(
            pc.run(
                "run-merged",
                pc.GH_ITEM,
                "success",
                pr_url=pc.PR_URL,
                reported_model=pc.LANE_MODEL,
            ),
        ),
        pull_request=pc.pull_request(
            state="merged",
            url=pc.PR_URL,
            merged_at=pc.MERGED_AT,
            checks="passing",
            mergeability="not_applicable",
            review_decision="approved",
        ),
        tracker_item=pc.tracker_item("closed"),
        readiness=pc.proven_boundary(),
    )


def _no_change_pending() -> WorkFacts:
    return pc.facts(
        item=pc.LINEAR_ITEM,
        tracker="linear",
        runs=(pc.run("run-nochange", pc.LINEAR_ITEM, "success"),),
        no_change=NoChangeFact(
            state="candidate",
            policy="confirm",
            reasons=("no files changed",),
            judgment_id="nc-1",
        ),
        readiness=pc.proven_boundary(),
    )


def _no_change_authorized() -> WorkFacts:
    return pc.facts(
        item=pc.LINEAR_ITEM,
        tracker="linear",
        runs=(pc.run("run-nochange-ok", pc.LINEAR_ITEM, "success"),),
        no_change=NoChangeFact(
            state="authorized",
            policy="confirm",
            reasons=("no files changed",),
            sync_outstanding=1,
            effects_outstanding=1,
            judgment_id="nc-2",
        ),
        readiness=pc.proven_boundary(),
    )


# --------------------------------------------------------------------------- #
# PCJ-3 — the recovery rail, one case per seam of the spec's table (C8)
# --------------------------------------------------------------------------- #


def _publication_risk_hold() -> WorkFacts:
    return pc.facts(
        runs=(
            pc.run(
                "run-held",
                pc.GH_ITEM,
                "awaiting_publication",
                reported_model=pc.LANE_MODEL,
            ),
        ),
        pull_request=pc.pull_request(),
        publication=PublicationFact(
            state="reserved",
            review_state="pending",
            risk_reasons=("1240 diff lines",),
            reserved_at="2026-08-24T11:50:00+00:00",
            job_id="pj-7",
        ),
        readiness=pc.proven_boundary(),
        pressure=pc.pressure(used=3, total=4),
    )


def _capacity_saturated() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-capacity", pc.GH_ITEM, "failure"),),
        readiness=pc.proven_boundary(),
        pressure=pc.pressure(used=4, total=4),
    )


def _terminal_without_artifact() -> WorkFacts:
    return pc.facts(
        runs=(
            pc.run(
                "run-failed",
                pc.GH_ITEM,
                "failure",
                error_category="test_failure",
            ),
        ),
        readiness=pc.proven_boundary(),
    )


def _ci_pending_same_sha() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-ci", pc.GH_ITEM, "success", pr_url=pc.PR_URL),),
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="pending",
            mergeability="clean",
            review_decision="review_required",
        ),
        readiness=pc.proven_boundary(),
    )


def _changes_requested() -> WorkFacts:
    return pc.facts(
        runs=(
            pc.run(
                "run-reviewed",
                pc.GH_ITEM,
                "success",
                pr_url=pc.PR_URL,
                reported_model=pc.LANE_MODEL,
            ),
        ),
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="passing",
            mergeability="clean",
            review_decision="changes_requested",
        ),
        readiness=pc.proven_boundary(),
    )


def _merge_conflict() -> WorkFacts:
    return replace(
        _changes_requested(),
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="failing",
            mergeability="conflicted",
            review_decision="review_required",
        ),
    )


def _tracker_sync_pending() -> WorkFacts:
    return replace(_merged_and_synchronized(), tracker_item=pc.tracker_item("open"))


def _tracker_unreadable() -> WorkFacts:
    return replace(
        _merged_and_synchronized(),
        tracker_item=pc.tracker_item(
            None,
            "unavailable",
            provenance="error",
            detail="the source tracker could not be read",
        ),
    )


def _forge_unreadable() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-blind", pc.GH_ITEM, "success", pr_url=pc.PR_URL),),
        pull_request=pc.pull_request(
            "unavailable",
            provenance="error",
            detail="the delivery forge could not be read",
            url=pc.PR_URL,
        ),
        readiness=pc.proven_boundary(),
    )


def _orphan_artifact() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-orphaned", pc.GH_ITEM, "failure"),),
        orphan_prs=(pc.orphan(),),
        pull_request=pc.pull_request(
            state="open",
            url="https://example.invalid/pr/9001",
            checks="passing",
            mergeability="clean",
            review_decision="review_required",
        ),
        readiness=pc.proven_boundary(),
    )


# --------------------------------------------------------------------------- #
# PCJ-4 — understand the outcome (C3, C7)
# --------------------------------------------------------------------------- #


def _route_substituted() -> WorkFacts:
    """The runner served a different model than the lane declared."""
    return pc.facts(
        runs=(
            pc.run(
                "run-substituted",
                pc.GH_ITEM,
                "success",
                pr_url=pc.PR_URL,
                reported_model="some-other-model",
            ),
        ),
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="passing",
            mergeability="clean",
            review_decision="review_required",
        ),
        readiness=pc.proven_boundary(),
        certificate=pc.certificate(
            "certified",
            certified_at="2026-08-12T16:20:00+00:00",
            lane=pc.LANE,
            evidence_ref="anchor:sha256:" + "b" * 16,
        ),
    )


def _certificate_evidence_unavailable() -> WorkFacts:
    return replace(
        _published_awaiting_decision(),
        certificate=pc.certificate(
            "unknown",
            availability="unavailable",
            detail="the certificate evidence ref was never fetched",
        ),
    )


# --------------------------------------------------------------------------- #
# The pack. Ordered by journey, then by gate.
# --------------------------------------------------------------------------- #

CASES: tuple[Case, ...] = (
    Case(
        name="pcj0_configured_live_unverified",
        gates=("PCJ-0", "C0"),
        claim="an unprobed boundary is 'configured', never 'live verified'",
        build=_contract_only,
        expect=_expect(
            execution="not_started",
            exception="not_started",
            safest_action="dispatch",
            actions=("dispatch",),
            synchronization="unknown",
        ),
        forbidden_actions=("wait",),
        forbidden_states=(("readiness", "proven"), ("route_claim", "honored")),
    ),
    Case(
        name="pcj0_boundary_refuted",
        gates=("PCJ-0", "C0"),
        claim="a boundary proven broken blocks the spend it would waste",
        build=_boundary_refuted,
        expect=_expect(
            execution="not_started",
            readiness="refuted",
            exception="readiness_refuted",
            blocking=True,
            safest_action="inspect",
            synchronization="unknown",
        ),
        forbidden_actions=("dispatch",),
    ),
    Case(
        name="pcj0_fleet_stopped_launches_nothing",
        gates=("PCJ-0", "C1", "C8"),
        claim="a pre-run hold launches nothing and spends nothing",
        build=_fleet_stopped,
        expect=_expect(
            execution="not_started",
            readiness="proven",
            exception="fleet_stopped",
            blocking=True,
            safest_action="resume_fleet",
            actions=("resume_fleet",),
            synchronization="unknown",
        ),
        forbidden_actions=("dispatch",),
        forbidden_states=(("execution", "in_flight"),),
    ),
    Case(
        name="pcj1_admitted_once_in_flight",
        gates=("PCJ-1", "C1"),
        claim="an admitted item is admitted once; a second dispatch duplicates it",
        build=_in_flight,
        expect=_expect(
            execution="in_flight",
            readiness="proven",
            exception="in_flight",
            safest_action="wait",
            synchronization="unknown",
        ),
        forbidden_actions=("dispatch",),
    ),
    Case(
        name="pcj1_preserved_awaiting_publication",
        gates=("PCJ-1", "C2", "C8"),
        claim="a preserved artifact is finalized, never re-earned by a retry",
        build=_preserved_awaiting_publication,
        expect=_expect(
            artifact="preserved",
            publication="pending",
            readiness="proven",
            route_claim="honored",
            exception="awaiting_publication",
            safest_action="finalize_publication",
            actions=("finalize_publication",),
            synchronization="unknown",
        ),
        forbidden_actions=("dispatch",),
        forbidden_states=(("execution", "terminal"), ("outcome", "failed")),
    ),
    Case(
        name="pcj1_published_awaiting_human_decision",
        gates=("PCJ-1", "C4", "C5"),
        claim="a published artifact hands the decision to a person, not a retry",
        build=_published_awaiting_decision,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            readiness="proven",
            route_claim="honored",
            exception="awaiting_human_decision",
            safest_action="await_human_decision",
        ),
        forbidden_actions=("dispatch", "merge"),
    ),
    Case(
        name="pcj1_delivered_and_synchronized",
        gates=("PCJ-1", "C6"),
        claim="merged plus a closed source item is the ONE delivered terminal",
        build=_merged_and_synchronized,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="accepted",
            outcome="delivered",
            synchronization="synchronized",
            readiness="proven",
            route_claim="honored",
            safest_action="none",
        ),
        forbidden_actions=("dispatch", "synchronize_tracker"),
    ),
    Case(
        name="pcj1_no_change_awaiting_confirmation",
        gates=("PCJ-1", "C3"),
        claim="a zero-diff exit is a decision to make, not a silent success",
        build=_no_change_pending,
        expect=_expect(
            no_change="pending",
            readiness="proven",
            outcome="unknown",
            synchronization="unknown",
            exception="no_change_pending",
            blocking=True,
            safest_action="confirm_no_change",
            actions=("confirm_no_change", "reject_no_change"),
        ),
        forbidden_actions=("dispatch",),
        forbidden_states=(("outcome", "delivered"), ("artifact", "published")),
    ),
    Case(
        name="pcj1_no_change_authorized_synchronizing",
        gates=("PCJ-1", "C6"),
        claim="an authorized zero-diff outcome still owes the source an update",
        build=_no_change_authorized,
        expect=_expect(
            no_change="synchronizing",
            readiness="proven",
            outcome="unknown",
            synchronization="unknown",
            exception="no_change_authorized",
            safest_action="synchronize_source",
            actions=("retry_no_change_sync",),
        ),
        forbidden_actions=("dispatch",),
    ),
    Case(
        name="pcj3_publication_held_for_a_person",
        gates=("PCJ-3", "C4"),
        claim="a risk hold is waiting for a person on purpose, not stalled",
        build=_publication_risk_hold,
        expect=_expect(
            artifact="preserved",
            publication="pending",
            readiness="proven",
            route_claim="honored",
            exception="publication_risk_hold",
            blocking=True,
            safest_action="confirm_publication_risk",
            actions=("confirm_publication_risk",),
        ),
        forbidden_actions=("dispatch", "finalize_publication"),
    ),
    Case(
        name="pcj3_review_capacity_saturated",
        gates=("PCJ-3", "C4"),
        claim="a full review queue withholds new work instead of growing it",
        build=_capacity_saturated,
        expect=_expect(
            execution="terminal",
            outcome="failed",
            readiness="proven",
            synchronization="unknown",
            exception="review_capacity_saturated",
            blocking=True,
            safest_action="wait",
            blocked_by="review_capacity",
        ),
        forbidden_actions=("dispatch",),
    ),
    Case(
        name="pcj3_terminal_without_artifact",
        gates=("PCJ-3", "C2", "C8"),
        claim="no recoverable artifact is an evidenced fact beside the failure",
        build=_terminal_without_artifact,
        expect=_expect(
            execution="terminal",
            outcome="failed",
            readiness="proven",
            synchronization="unknown",
            exception="terminal_failure",
            blocking=True,
            safest_action="dispatch",
            actions=("dispatch",),
        ),
        forbidden_states=(("artifact", "preserved"), ("artifact", "published")),
    ),
    Case(
        name="pcj3_ci_pending_retains_the_same_sha",
        gates=("PCJ-3", "C5", "C8"),
        claim="a pending check waits on the same artifact; it never re-runs the agent",
        build=_ci_pending_same_sha,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            readiness="proven",
            exception="awaiting_human_decision",
            safest_action="await_human_decision",
        ),
        forbidden_actions=("dispatch", "merge"),
        forbidden_states=(("judgment", "accepted"),),
    ),
    Case(
        name="pcj3_changes_requested_needs_a_successor",
        gates=("PCJ-3", "C5", "C8"),
        claim="review feedback needs a successor SHA; approval is never carried",
        build=_changes_requested,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="changes_requested",
            readiness="proven",
            route_claim="honored",
            exception="changes_requested",
            blocking=True,
            safest_action="address_review_feedback",
        ),
        forbidden_actions=("dispatch", "merge"),
        forbidden_states=(("judgment", "accepted"), ("outcome", "delivered")),
    ),
    Case(
        name="pcj3_merge_conflict_needs_a_successor",
        gates=("PCJ-3", "C5", "C8"),
        claim="a conflicted tree is remediated on a successor SHA, not re-run",
        build=_merge_conflict,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            readiness="proven",
            route_claim="honored",
            exception="merge_conflict",
            blocking=True,
            safest_action="resolve_conflict",
        ),
        forbidden_actions=("dispatch", "merge"),
        forbidden_states=(("judgment", "accepted"), ("outcome", "delivered")),
    ),
    Case(
        name="pcj3_tracker_write_back_pending",
        gates=("PCJ-3", "C6", "C8"),
        claim="a merged artifact with an open item is sync-pending, not finished",
        build=_tracker_sync_pending,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="accepted",
            outcome="delivered",
            synchronization="pending",
            readiness="proven",
            route_claim="honored",
            exception="source_desynchronized",
            safest_action="synchronize_tracker",
        ),
        forbidden_states=(("synchronization", "synchronized"),),
    ),
    Case(
        name="pcj3_tracker_outage_blocks_the_sync_claim",
        gates=("PCJ-3", "C6", "C7"),
        claim="an unreadable tracker is unavailable, never a healthy empty",
        build=_tracker_unreadable,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="accepted",
            outcome="delivered",
            synchronization="unknown",
            readiness="proven",
            route_claim="honored",
            exception="remote_unavailable",
            safest_action="observe_remote",
            blocked_by="tracker_item",
        ),
        forbidden_actions=("dispatch", "synchronize_tracker"),
        forbidden_states=(("synchronization", "synchronized"),),
    ),
    Case(
        name="pcj3_forge_outage_is_not_an_absent_artifact",
        gates=("PCJ-3", "C7", "C8"),
        claim="an unreadable forge blocks the claim rather than inventing one",
        build=_forge_unreadable,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="unknown",
            outcome="unknown",
            synchronization="unknown",
            readiness="proven",
            exception="remote_unavailable",
            safest_action="observe_remote",
            blocked_by="pull_request",
        ),
        forbidden_actions=("dispatch",),
        forbidden_states=(("outcome", "delivered"), ("judgment", "accepted")),
    ),
    Case(
        name="c4_orphan_artifact_blocks_a_second_one",
        gates=("C4", "C1"),
        claim="an artifact with no run behind it still stops a second dispatch",
        build=_orphan_artifact,
        expect=_expect(
            execution="terminal",
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            readiness="proven",
            exception="orphan_artifact",
            blocking=True,
            safest_action="inspect",
            actions=("acknowledge_orphan",),
        ),
        forbidden_actions=("dispatch",),
    ),
    Case(
        name="pcj4_route_substitution_is_visible",
        gates=("PCJ-4", "C3"),
        claim="a served model that differs from the declared one is not 'honored'",
        build=_route_substituted,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            readiness="proven",
            route_claim="substituted",
            exception="awaiting_human_decision",
            safest_action="await_human_decision",
            certification="certified",
        ),
        forbidden_states=(("route_claim", "honored"),),
    ),
    Case(
        name="pcj4_certificate_evidence_unavailable",
        gates=("PCJ-4", "C0", "C7"),
        claim="unread certificate evidence is unknown — not uncertified",
        build=_certificate_evidence_unavailable,
        expect=_expect(
            artifact="published",
            publication="published",
            judgment="awaiting_human_decision",
            readiness="proven",
            route_claim="honored",
            exception="awaiting_human_decision",
            safest_action="await_human_decision",
            certification="unknown",
        ),
        forbidden_states=(("certification", "uncertified"),),
    ),
)

BY_NAME = {case.name: case for case in CASES}
NAMES = [case.name for case in CASES]


@pytest.fixture(params=NAMES, ids=NAMES)
def case(request: pytest.FixtureRequest) -> Case:
    return BY_NAME[request.param]


def _observed(view: WorkViewDTO) -> dict[str, object]:
    """The composed truth, in exactly the vocabulary :class:`Expect` uses."""
    return {
        "execution": view.execution.state,
        "artifact": view.artifact.state,
        "publication": view.artifact.publication,
        "judgment": view.judgment.state,
        "outcome": view.outcome.state,
        "synchronization": view.source.synchronization,
        "no_change": view.no_change.state,
        "readiness": view.readiness.state,
        "route_claim": view.route.claim,
        "exception": view.exception.state,
        "blocking": view.exception.blocking,
        "safest_action": view.safest_action.action,
        "blocked_by": view.safest_action.blocked_by,
        "actions": tuple(action.action for action in view.actions),
        "certification": view.certificate_status.state,
    }


def _declared(expect: Expect) -> dict[str, object]:
    return {
        "execution": expect.execution,
        "artifact": expect.artifact,
        "publication": expect.publication,
        "judgment": expect.judgment,
        "outcome": expect.outcome,
        "synchronization": expect.synchronization,
        "no_change": expect.no_change,
        "readiness": expect.readiness,
        "route_claim": expect.route_claim,
        "exception": expect.exception,
        "blocking": expect.blocking,
        "safest_action": expect.safest_action,
        "blocked_by": expect.blocked_by,
        "actions": expect.actions,
        "certification": expect.certification,
    }


class TestPackIsNotHollow:
    """The pack's own integrity — checked before anything it asserts is trusted."""

    def test_every_gate_is_claimed_by_at_least_one_case(self) -> None:
        claimed = {gate for c in CASES for gate in c.gates}
        assert claimed == set(GATES), (
            f"unclaimed or unknown gates: {claimed ^ set(GATES)}"
        )

    def test_no_two_cases_declare_the_same_expectation(self) -> None:
        """Two identical expectations mean one of them proves nothing new."""
        shapes = Counter(tuple(sorted(_declared(c.expect).items())) for c in CASES)
        duplicates = [count for count in shapes.values() if count > 1]
        assert not duplicates, "duplicate expectations make a gate vacuous"

    def test_case_names_are_unique(self) -> None:
        assert len(BY_NAME) == len(CASES)

    def test_the_pack_needs_no_network_clock_or_store(self) -> None:
        """Composing twice, in either order, gives byte-identical documents."""
        first = [pc.view(c.build()).model_dump() for c in CASES]
        second = [pc.view(c.build()).model_dump() for c in reversed(CASES)]
        assert first == list(reversed(second))


class TestDeclaredTruth:
    """Each case's authored expectation is what the composer actually says."""

    def test_composed_view_matches_the_declared_expectation(self, case: Case) -> None:
        assert _observed(pc.view(case.build())) == _declared(case.expect), case.claim

    def test_the_forbidden_action_is_never_offered_or_recommended(
        self, case: Case
    ) -> None:
        view = pc.view(case.build())
        offered = {action.action for action in view.actions} | {
            view.safest_action.action
        }
        assert offered.isdisjoint(case.forbidden_actions), case.claim

    def test_the_forbidden_false_outcome_is_never_reported(self, case: Case) -> None:
        observed = _observed(pc.view(case.build()))
        for dimension, forbidden in case.forbidden_states:
            assert observed[dimension] != forbidden, f"{case.name}: {case.claim}"

    def test_exactly_one_safest_action_and_it_leads_the_offers(
        self, case: Case
    ) -> None:
        """C7 — no surface may invent a second 'primary' next step."""
        view = pc.view(case.build())
        safest = [action for action in view.actions if action.safest]
        assert len(safest) <= 1
        if safest:
            # The offer may NAME the step concretely where the verdict names it
            # abstractly ("inspect" -> "acknowledge_orphan"); what may never
            # happen is a second offer competing for primacy, or a primary one
            # buried below the alternatives a person is also entitled to take.
            assert view.actions[0].safest is True
            assert all(not a.safest for a in view.actions[1:])

    def test_a_blocked_verdict_offers_nothing(self, case: Case) -> None:
        """C7 — 'find out' is never dressed up as an executable button."""
        view = pc.view(case.build())
        if view.safest_action.blocked_by is not None:
            assert view.actions == []


class TestC7CrossSurfaceTruth:
    """One revision per fact set, and it moves only when the facts do."""

    def test_the_same_facts_compose_to_the_same_revision(self, case: Case) -> None:
        assert pc.view(case.build()).view_revision == (
            pc.view(case.build()).view_revision
        )

    def test_every_case_has_its_own_revision(self) -> None:
        revisions = [pc.view(c.build()).view_revision for c in CASES]
        assert len(set(revisions)) == len(revisions)

    def test_every_named_customer_state_is_covered(self) -> None:
        """The six states C7 requires the surfaces to agree at."""
        required = {
            "in_flight",
            "awaiting_publication",
            "awaiting_human_decision",
            "source_desynchronized",
            "terminal_failure",
            "remote_unavailable",
        }
        assert {c.expect.exception for c in CASES} >= required

    def test_every_remote_dimension_reports_its_own_availability(
        self, case: Case
    ) -> None:
        view = pc.view(case.build())
        assert {o.dimension for o in view.observations} == set(pc.OBSERVED_DIMENSIONS)
        for observed in view.observations:
            assert observed.provenance != ""
            if observed.availability == "unavailable":
                assert observed.observed_at is None


class TestOrthogonality:
    """The three truths may never be derived from one another."""

    def test_a_live_verified_route_never_produces_a_certificate(self) -> None:
        """C0 — a passing probe is reachability, not journey certification."""
        view = pc.view(_published_awaiting_decision())
        assert view.readiness.state == "proven"
        assert view.route.claim == "honored"
        assert view.certificate_status.state != "certified"

    def test_a_standing_certificate_never_proves_current_reachability(self) -> None:
        facts = pc.facts(
            runs=(pc.run("run-certified", pc.GH_ITEM, "success"),),
            readiness=pc.readiness(detail="nothing has probed this boundary"),
            certificate=pc.certificate(
                "certified",
                certified_at="2026-08-12T16:20:00+00:00",
                lane=pc.LANE,
                evidence_ref="anchor:sha256:" + "c" * 16,
            ),
        )
        view = pc.view(facts)
        assert view.certificate_status.state == "certified"
        assert view.readiness.state == "unproven"
        assert view.route.claim == "unverified"

    def test_an_unread_certificate_ref_does_not_make_the_work_unhealthy(self) -> None:
        """Journey certification gates no current action, so it stays off the
        aggregate observation list the health verdict reads."""
        view = pc.view(_certificate_evidence_unavailable())
        assert view.certificate_status.observation.availability == "unavailable"
        assert "certificate_evidence" not in {o.dimension for o in view.observations}
        assert view.safest_action.blocked_by is None
