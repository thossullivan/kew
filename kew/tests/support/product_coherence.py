"""Deterministic fixture material for the PCJ / C-gate conformance pack (P1.2).

The pack in ``tests/conformance/`` has to establish, offline and on any
machine, that kew's shared customer contract holds at every gate the
production-certification spec names. That means the facts it composes must be
**declared**, never collected: no store, no clock, no network, no environment.
Everything a case needs is built here from literals, so a red case is always a
statement about the contract and never about the machine it ran on.

This module deliberately does NOT reuse ``tests/test_surface_convergence.py``'s
builders. That module *generates* the committed golden matrix; if the
conformance pack read its output, the pack's "expectation" would be kew's own
dump of itself and a hollow pass would look identical to a real one (the #1542
parity-proof lesson). The two modules build their facts independently and the
pack states its expectations by hand.

It also carries the **WIP-1 A→B two-node fixture** — the in-release portion of
PCJ-2. A live supervised epic needs a real forge and a real reviewer; what can
be proven pre-merge is that the SAME composed contract, under one dependency
edge, releases the dependent for exactly one disposition of the dependency and
holds it for every other. :func:`dependency_satisfied` states that condition as
a conjunction of composed-view clauses, and :func:`frontier_after` runs kew's
own :func:`~kew.graph.driver.frontier.compute_frontier` over it, so the fixture
is a composition of two real subsystems rather than a restatement of either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.driver.frontier import compute_frontier
from kew.run_store import AdmissionIdentity, OrphanPRRecord, RunRecord
from kew.service.models import WorkViewDTO
from kew.service.work import (
    CertificateFact,
    PullRequestFact,
    ReadinessFact,
    RemoteObservation,
    ReviewPressureFact,
    TrackerItemFact,
    WorkFacts,
    compose_work_view,
)

# --------------------------------------------------------------------------- #
# The fixed world. Every value is a literal so the pack is reproducible.
# --------------------------------------------------------------------------- #

COMPOSED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 8, 24, 11, 59, tzinfo=UTC)
STARTED_AT = datetime(2026, 8, 24, 11, 30, tzinfo=UTC)
FINISHED_AT = datetime(2026, 8, 24, 11, 48, tzinfo=UTC)
MERGED_AT = datetime(2026, 8, 24, 11, 55, tzinfo=UTC)

DELIVERY_REPO = "acme/widgets"
EPIC_REF = "epic-product-coherence"
LANE_RUNNER = "claude-code"
LANE_MODEL = "claude-opus-5"
LANE = f"{LANE_RUNNER}/github"

#: A GitHub item and an opaque Linear key. The Linear key is load-bearing: the
#: A→B fixture must compose over a source id kew cannot turn into an integer.
GH_ITEM = "4242"
LINEAR_ITEM = "SFD-12"

PR_URL = "https://example.invalid/pr/7"

NODE_A = "WIP-1-A"
NODE_B = "WIP-1-B"

#: The remote dimensions a composed view always carries an observation for.
OBSERVED_DIMENSIONS = (
    "pull_request",
    "review_capacity",
    "route_boundary",
    "tracker_item",
)


# --------------------------------------------------------------------------- #
# Fact builders
# --------------------------------------------------------------------------- #


def observation(
    dimension: str,
    availability: str = "available",
    *,
    provenance: str = "observed",
    detail: str | None = None,
) -> RemoteObservation:
    """One remote reading. An unavailable reading never carries a timestamp."""
    return RemoteObservation(
        dimension=dimension,
        availability=availability,
        provenance=provenance,
        observed_at=None if availability == "unavailable" else OBSERVED_AT,
        detail=detail,
    )


def run(run_id: str, item: str, status: str, **over: Any) -> RunRecord:
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


def identity(
    run_id: str, item: str, tracker: str = "github", node_id: str = NODE_A
) -> AdmissionIdentity:
    return AdmissionIdentity(
        run_id=run_id,
        attempt_number=1,
        source_tracker=tracker,
        item_id=item,
        delivery_repo=DELIVERY_REPO,
        epic_ref=EPIC_REF,
        node_id=node_id,
    )


def pull_request(
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
        observation=observation(
            "pull_request", availability, provenance=provenance, detail=detail
        ),
        url=url,
        state=state,
        merged_at=merged_at,
        checks=checks,
        mergeability=mergeability,
        review_decision=review_decision,
    )


def tracker_item(
    state: str | None = "open",
    availability: str = "available",
    *,
    provenance: str = "observed",
    detail: str | None = None,
) -> TrackerItemFact:
    return TrackerItemFact(
        observation=observation(
            "tracker_item", availability, provenance=provenance, detail=detail
        ),
        state=state,
    )


def readiness(
    state: str = "unproven",
    availability: str = "unavailable",
    *,
    detail: str = "",
    fingerprint: str | None = None,
    evidence_ref: str | None = None,
) -> ReadinessFact:
    return ReadinessFact(
        observation=observation("route_boundary", availability, detail=detail or None),
        state=state,
        fingerprint=fingerprint,
        evidence_ref=evidence_ref,
        detail=detail,
    )


def proven_boundary() -> ReadinessFact:
    """A settled probe of this exact boundary — the only thing that is
    allowed to turn ``configured`` into ``live verified`` (C0)."""
    return readiness(
        "proven",
        "available",
        detail="a settled probe of this exact boundary passed",
        fingerprint="rb1:" + "7" * 16,
        evidence_ref="probe-1616",
    )


def refuted_boundary() -> ReadinessFact:
    return readiness(
        "refuted",
        "available",
        detail="the probe of this exact boundary failed",
        fingerprint="rb1:" + "8" * 16,
        evidence_ref="probe-1616-red",
    )


def pressure(
    *,
    used: int = 1,
    total: int = 4,
    engaged: bool = True,
    countable: bool = True,
    availability: str = "available",
    detail: str = "",
) -> ReviewPressureFact:
    return ReviewPressureFact(
        observation=observation("review_capacity", availability, detail=detail or None),
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


def certificate(
    state: str = "uncertified",
    *,
    availability: str = "available",
    certified_at: str | None = None,
    lane: str | None = None,
    evidence_ref: str | None = None,
    detail: str = "",
) -> CertificateFact:
    """One certificate reading, exactly as ``kew.certificate_ref`` produced it.

    The pack never manufactures a ``certified`` reading out of an execution
    outcome — that is the collapse the third orthogonal truth exists to
    prevent — so cases that carry one say so explicitly.
    """
    return CertificateFact(
        observation=observation(
            "certificate_evidence",
            availability,
            provenance="observed" if availability != "unavailable" else "unobserved",
            detail=detail or None,
        ),
        state=state,
        certified_at=certified_at,
        lane=lane,
        evidence_ref=evidence_ref,
        detail=detail,
    )


def facts(
    item: str = GH_ITEM,
    tracker: str = "github",
    runs: tuple[RunRecord, ...] = (),
    **over: Any,
) -> WorkFacts:
    """One work item's whole fact surface, defaulting to a readable world."""
    base: dict[str, Any] = {
        "item_id": item,
        "identity": identity(runs[0].run_id, item, tracker) if runs else None,
        "runs": runs,
        "orphan_prs": (),
        "pull_request": pull_request(
            "unavailable", provenance="unobserved", detail="no artifact was published"
        ),
        "tracker_item": tracker_item("open"),
        "readiness": readiness(),
        "pressure": pressure(),
        "certificate": certificate(),
        # The snapshot's own moment, pinned alongside the rest. A rule with an
        # AGE in it (``PENDING_RUN_MAX_AGE_S`` in kew/service/work.py: a pending
        # row is a run in flight only while it is young) would otherwise be
        # measured against the wall clock, and a declared truth that changes
        # meaning as the calendar moves is not a declared truth.
        "as_of": COMPOSED_AT,
    }
    base.update(over)
    return WorkFacts(**base)


def view(work_facts: WorkFacts) -> WorkViewDTO:
    """Compose at the fixed instant — ``composed_at`` is never digested."""
    return compose_work_view(work_facts, composed_at=COMPOSED_AT)


def orphan(pr_number: int = 9001) -> OrphanPRRecord:
    return OrphanPRRecord(
        pr_number=pr_number,
        url=f"https://example.invalid/pr/{pr_number}",
        branch="kew/run-orphaned",
        reason="failed-run",
        detected_at=OBSERVED_AT,
        acked_at=None,
    )


# --------------------------------------------------------------------------- #
# WIP-1 — the pre-merge A→B two-node fixture (the in-release part of PCJ-2)
# --------------------------------------------------------------------------- #

#: Every condition the spec requires of node A before node B may be released,
#: as ``(name, predicate)`` over A's COMPOSED view. Stated as a conjunction of
#: independently falsifiable clauses on purpose: each disposition in the
#: pack's A→B table must fail at least one NAMED clause, so "B is ineligible"
#: is always attributable rather than merely true.
_RELEASE_CONDITIONS: tuple[tuple[str, Any], ...] = (
    # "A's exact artifact is host-verified" — a real published artifact with a
    # successful run behind it, never an orphan the host never claimed.
    ("artifact_published", lambda v: v.artifact.state == "published"),
    ("artifact_not_orphan", lambda v: v.artifact.orphan is False),
    # "non-author release-authorized" — a human decision that ACCEPTED it.
    ("release_authorized", lambda v: v.judgment.state == "accepted"),
    # "policy/CI eligible" — the mechanical gate, separate from the person.
    ("checks_passing", lambda v: v.integration.checks == "passing"),
    # "observed merged" — the delivery outcome, read rather than assumed.
    ("delivered", lambda v: v.outcome.state == "delivered"),
    # "source synchronization acknowledged" — the tracker agrees it is done.
    ("source_synchronized", lambda v: v.source.synchronization == "synchronized"),
    # Nothing about A may still be off-normal, and every reading the clauses
    # above rest on must have actually been READ. An unavailable forge or
    # tracker is not a quiet "yes".
    ("no_exception", lambda v: v.exception.state == "none"),
    (
        "forge_read",
        lambda v: v.integration.observation.availability == "available",
    ),
    ("source_read", lambda v: v.source.observation.availability == "available"),
)


def unmet_release_conditions(dependency: WorkViewDTO) -> tuple[str, ...]:
    """The named clauses *dependency* fails, in declaration order."""
    return tuple(name for name, holds in _RELEASE_CONDITIONS if not holds(dependency))


def dependency_satisfied(dependency: WorkViewDTO) -> bool:
    """Whether A's composed truth releases its dependent. Fails closed."""
    return not unmet_release_conditions(dependency)


def two_node_graph(
    *, a_status: NodeStatus, b_status: NodeStatus = NodeStatus.TODO
) -> Graph:
    """The WIP-1 epic: one edge, A → B, and nothing else."""
    nodes = [
        Node(
            id=NODE_A, title="A: land the contract", status=a_status, epic_ref=EPIC_REF
        ),
        Node(id=NODE_B, title="B: build on it", status=b_status, epic_ref=EPIC_REF),
    ]
    return Graph(nodes, [Edge(src=NODE_A, dst=NODE_B)])


def frontier_after(dependency: WorkViewDTO) -> list[str]:
    """Which WIP-1 nodes kew's own frontier predicate would dispatch.

    A's provider status is projected from its composed view and NOTHING else,
    then handed to ``compute_frontier`` unchanged. The composition under test
    is exactly that hand-off: change what A's view says and this answer must
    change with it.

    The second route is the acknowledged zero-diff dependency effect (I12): a
    dependency that produced no diff can never earn a provider DONE, so a
    *completed* zero-diff judgment advances the one edge it was authorized to
    advance — and only that one. A zero-diff judgment still owing a source
    write or an effect is not acknowledged and releases nothing.
    """
    a_status = NodeStatus.DONE if dependency_satisfied(dependency) else NodeStatus.TODO
    effects = {NODE_A: (NODE_B,)} if dependency.no_change.state == "completed" else {}
    graph = two_node_graph(a_status=a_status)
    status = {NODE_A: a_status, NODE_B: NodeStatus.TODO}
    return compute_frontier(
        graph, status=status, excluded={NODE_A}, dependency_effects=effects
    )


__all__ = [
    "COMPOSED_AT",
    "DELIVERY_REPO",
    "EPIC_REF",
    "FINISHED_AT",
    "GH_ITEM",
    "LANE",
    "LANE_MODEL",
    "LANE_RUNNER",
    "LINEAR_ITEM",
    "MERGED_AT",
    "NODE_A",
    "NODE_B",
    "OBSERVED_AT",
    "OBSERVED_DIMENSIONS",
    "PR_URL",
    "STARTED_AT",
    "certificate",
    "dependency_satisfied",
    "facts",
    "frontier_after",
    "identity",
    "observation",
    "orphan",
    "pressure",
    "proven_boundary",
    "pull_request",
    "readiness",
    "refuted_boundary",
    "run",
    "tracker_item",
    "two_node_graph",
    "unmet_release_conditions",
    "view",
]
