"""WIP-1 — the pre-merge A→B two-node fixture (the in-release part of PCJ-2).

PCJ-2 asks whether the same customer contract still holds when one item's
outcome gates another's. The full journey needs a live forge, a live reviewer
and a supervised epic, so it belongs to the post-merge certification run. What
can be settled *before* merge is the part that has nothing to do with being
live: **does A's composed truth release B for exactly one disposition, and hold
B for every other?**

That is what this module fixes. Node A is composed by the real
``compose_work_view``; node B's dispatchability is decided by the real
``compute_frontier``; the only thing between them is
:func:`~tests.support.product_coherence.dependency_satisfied`, which states the
spec's release condition as a conjunction of named clauses over A's view. So a
red here is one of exactly two things — the composed contract stopped saying
what the spec requires, or the frontier stopped honouring it — and the failing
clause names which.

The table below is authored from the spec sentence, not from kew's output:
*B remains ineligible until A's exact artifact is host-verified, non-author
release-authorized, policy/CI eligible, observed merged, and source
synchronization acknowledged. Every other terminal disposition keeps B
ineligible.* Each row therefore also declares WHICH clause it fails, so a
disposition cannot drift into being held for an accidental reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest
from support import product_coherence as pc

from kew.service.work import NoChangeFact, WorkFacts


def _merged(**over: object) -> WorkFacts:
    """A delivered A: merged artifact, approved, checks green, item closed."""
    base = pc.facts(
        runs=(
            pc.run(
                "run-a",
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
    return replace(base, **over) if over else base


def _delivered() -> WorkFacts:
    return _merged()


def _sync_pending() -> WorkFacts:
    return _merged(tracker_item=pc.tracker_item("open"))


def _tracker_unreadable() -> WorkFacts:
    return _merged(
        tracker_item=pc.tracker_item(
            None,
            "unavailable",
            provenance="error",
            detail="the source tracker could not be read",
        )
    )


def _forge_unreadable() -> WorkFacts:
    return _merged(
        pull_request=pc.pull_request(
            "unavailable",
            provenance="error",
            detail="the delivery forge could not be read",
            url=pc.PR_URL,
        )
    )


def _checks_failing() -> WorkFacts:
    return _merged(
        pull_request=pc.pull_request(
            state="merged",
            url=pc.PR_URL,
            merged_at=pc.MERGED_AT,
            checks="failing",
            mergeability="not_applicable",
            review_decision="approved",
        )
    )


def _awaiting_decision() -> WorkFacts:
    return _merged(
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="passing",
            mergeability="clean",
            review_decision="review_required",
        ),
        tracker_item=pc.tracker_item("open"),
    )


def _changes_requested() -> WorkFacts:
    return _merged(
        pull_request=pc.pull_request(
            state="open",
            url=pc.PR_URL,
            checks="passing",
            mergeability="clean",
            review_decision="changes_requested",
        ),
        tracker_item=pc.tracker_item("open"),
    )


def _declined() -> WorkFacts:
    return _merged(
        pull_request=pc.pull_request(
            state="closed",
            url=pc.PR_URL,
            checks="passing",
            mergeability="not_applicable",
            review_decision="changes_requested",
        ),
        tracker_item=pc.tracker_item("closed"),
    )


def _awaiting_publication() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-a", pc.GH_ITEM, "awaiting_publication"),),
        readiness=pc.proven_boundary(),
    )


def _terminal_failure() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-a", pc.GH_ITEM, "failure", error_category="test_failure"),),
        readiness=pc.proven_boundary(),
    )


def _orphan_artifact() -> WorkFacts:
    return pc.facts(
        runs=(pc.run("run-a", pc.GH_ITEM, "failure"),),
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


def _no_change_owing() -> WorkFacts:
    """An authorized zero-diff outcome that still owes the source an update."""
    return pc.facts(
        item=pc.LINEAR_ITEM,
        tracker="linear",
        runs=(pc.run("run-a", pc.LINEAR_ITEM, "success"),),
        no_change=NoChangeFact(
            state="authorized",
            policy="confirm",
            reasons=("no files changed",),
            sync_outstanding=1,
            effects_outstanding=1,
            judgment_id="nc-a",
        ),
        readiness=pc.proven_boundary(),
    )


def _no_change_acknowledged() -> WorkFacts:
    """The same judgment, settled: the effect on this edge was applied."""
    return replace(
        _no_change_owing(),
        no_change=NoChangeFact(
            state="completed",
            policy="confirm",
            reasons=("no files changed",),
            judgment_id="nc-a",
        ),
    )


@dataclass(frozen=True)
class Disposition:
    """One terminal (or in-progress) shape of node A, and what it releases."""

    name: str
    build: Callable[[], WorkFacts]
    releases_b: bool
    #: The named release clauses this disposition must fail, exactly.
    unmet: tuple[str, ...]


DISPOSITIONS: tuple[Disposition, ...] = (
    Disposition("delivered_and_synchronized", _delivered, True, ()),
    Disposition(
        "merged_but_source_sync_pending",
        _sync_pending,
        False,
        ("source_synchronized", "no_exception"),
    ),
    Disposition(
        "merged_but_tracker_unreadable",
        _tracker_unreadable,
        False,
        ("source_synchronized", "no_exception", "source_read"),
    ),
    Disposition(
        "merged_but_forge_unreadable",
        _forge_unreadable,
        False,
        (
            "release_authorized",
            "checks_passing",
            "delivered",
            "source_synchronized",
            "no_exception",
            "forge_read",
        ),
    ),
    Disposition(
        "merged_with_a_red_check",
        _checks_failing,
        False,
        ("checks_passing", "no_exception"),
    ),
    Disposition(
        "published_awaiting_human_decision",
        _awaiting_decision,
        False,
        (
            "release_authorized",
            "delivered",
            "source_synchronized",
            "no_exception",
        ),
    ),
    Disposition(
        "changes_requested",
        _changes_requested,
        False,
        (
            "release_authorized",
            "delivered",
            "source_synchronized",
            "no_exception",
        ),
    ),
    Disposition(
        "declined_and_closed",
        _declined,
        False,
        (
            "release_authorized",
            "delivered",
            "source_synchronized",
            "no_exception",
        ),
    ),
    Disposition(
        "awaiting_publication",
        _awaiting_publication,
        False,
        (
            "artifact_published",
            "release_authorized",
            "checks_passing",
            "delivered",
            "source_synchronized",
            "no_exception",
            "forge_read",
        ),
    ),
    Disposition(
        "terminal_failure",
        _terminal_failure,
        False,
        (
            "artifact_published",
            "release_authorized",
            "checks_passing",
            "delivered",
            "source_synchronized",
            "no_exception",
            "forge_read",
        ),
    ),
    Disposition(
        "orphan_artifact",
        _orphan_artifact,
        False,
        (
            "artifact_not_orphan",
            "release_authorized",
            "delivered",
            "source_synchronized",
            "no_exception",
        ),
    ),
    Disposition(
        "zero_diff_still_owing_the_source",
        _no_change_owing,
        False,
        (
            "artifact_published",
            "release_authorized",
            "checks_passing",
            "delivered",
            "source_synchronized",
            "no_exception",
            "forge_read",
        ),
    ),
)

BY_NAME = {d.name: d for d in DISPOSITIONS}


@pytest.fixture(params=list(BY_NAME), ids=list(BY_NAME))
def disposition(request: pytest.FixtureRequest) -> Disposition:
    return BY_NAME[request.param]


class TestOneEdgeComposes:
    """B advances on exactly one disposition of A, and for named reasons."""

    def test_the_dependent_is_released_only_when_the_spec_says_so(
        self, disposition: Disposition
    ) -> None:
        frontier = pc.frontier_after(pc.view(disposition.build()))
        assert (pc.NODE_B in frontier) is disposition.releases_b

    def test_each_disposition_fails_exactly_the_clauses_it_declares(
        self, disposition: Disposition
    ) -> None:
        """Held for the RIGHT reason — a hold that drifted onto another clause
        is a contract change wearing the old answer's clothes."""
        unmet = pc.unmet_release_conditions(pc.view(disposition.build()))
        assert set(unmet) == set(disposition.unmet)

    def test_the_dependency_itself_never_re_enters_the_frontier(
        self, disposition: Disposition
    ) -> None:
        assert pc.NODE_A not in pc.frontier_after(pc.view(disposition.build()))

    def test_exactly_one_disposition_releases_the_dependent(self) -> None:
        released = [d.name for d in DISPOSITIONS if d.releases_b]
        assert released == ["delivered_and_synchronized"]


class TestTheEdgeIsLoadBearing:
    """Mutate one node's output and the composition must go red."""

    @pytest.mark.parametrize(
        ("mutation", "clause"),
        [
            ("tracker_item", "source_synchronized"),
            ("pull_request", "checks_passing"),
        ],
    )
    def test_mutating_the_dependency_output_withholds_the_dependent(
        self, mutation: str, clause: str
    ) -> None:
        mutated = {
            "tracker_item": lambda: _merged(tracker_item=pc.tracker_item("open")),
            "pull_request": _checks_failing,
        }[mutation]()
        view = pc.view(mutated)
        assert clause in pc.unmet_release_conditions(view)
        assert pc.frontier_after(view) == []

    def test_the_released_case_is_the_one_that_would_break(self) -> None:
        """The positive case is not passing by accident: it releases B, and the
        smallest possible change to A's observed truth takes that away."""
        delivered = pc.view(_delivered())
        assert pc.frontier_after(delivered) == [pc.NODE_B]
        reopened = pc.view(_merged(tracker_item=pc.tracker_item("open")))
        assert pc.frontier_after(reopened) == []
        assert delivered.view_revision != reopened.view_revision


class TestZeroDiffAdvancesOneEdgeOnly:
    """A dependency that produced no diff can never earn a provider DONE."""

    def test_an_owing_zero_diff_judgment_releases_nothing(self) -> None:
        view = pc.view(_no_change_owing())
        assert view.no_change.state == "synchronizing"
        assert pc.frontier_after(view) == []

    def test_an_acknowledged_zero_diff_judgment_releases_the_named_dependent(
        self,
    ) -> None:
        view = pc.view(_no_change_acknowledged())
        assert view.no_change.state == "completed"
        assert pc.frontier_after(view) == [pc.NODE_B]

    def test_it_never_earns_the_dependency_a_done(self) -> None:
        """The effect advances one edge; it does not rewrite A's own truth."""
        view = pc.view(_no_change_acknowledged())
        assert pc.dependency_satisfied(view) is False
        assert "artifact_published" in pc.unmet_release_conditions(view)


class TestTheFixtureIsDeterministic:
    def test_an_opaque_source_id_composes_without_a_numeric_shim(self) -> None:
        """At least one node uses a Linear-shaped id; nothing may coerce it."""
        view = pc.view(_no_change_owing())
        assert view.identity.item_id == pc.LINEAR_ITEM
        assert view.identity.item_ref == pc.LINEAR_ITEM

    def test_composing_twice_gives_the_same_answer(
        self, disposition: Disposition
    ) -> None:
        first = pc.view(disposition.build())
        second = pc.view(disposition.build())
        assert first.model_dump() == second.model_dump()
