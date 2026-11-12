"""I13: one human-review policy owns capacity, risk, and terminal judgement.

The numbers in this file are the product's promises, so they are asserted at
their exact boundaries rather than "somewhere over the line": 500 changed lines
and 20 paths pass unattended, 501 and 21 stop for a person. Anything looser
would let the threshold drift a line at a time.

Four properties are asserted here rather than assumed.

* **A deletion is not the harmless direction.** The line count is additions
  plus deletions, so removing 900 lines is not a small diff.
* **Work nobody can measure stops.** A binary or unreadable path contributes
  no lines — it cannot — so it is carried as a named path and holds the
  publication instead of scoring zero and sailing through.
* **A refusal is not a heavy flag.** The publication filter's rejections
  produce an outcome no approver, allowlist entry or threshold releases.
* **Capacity belongs to a repository.** Occupancy is a fact about one
  canonical GitHub delivery repository. There is no policy in this module that
  can be built, and no decision that can be taken, without naming one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kew.config import DispatchConfig
from kew.human_review import (
    ALLOWED,
    BLOCKED,
    CONFIRMATION_REQUIRED,
    REASON_BINARY,
    REASON_CAPACITY,
    REASON_DIFF_LINES,
    REASON_PATHS,
    REASON_REVIEW_FLAG,
    REASON_UNMEASURABLE,
    REFUSED,
    REVERSION_EXPIRED,
    SYSTEM_MAX_DIFF_LINES,
    SYSTEM_MAX_PATHS,
    SYSTEM_MAX_PENDING_REVIEWS,
    PathChange,
    RefusalNotOverridableError,
    ReviewAuthorityError,
    ReviewCapacityError,
    ReviewPolicy,
    ReviewPolicyError,
    authorize_approver,
    confirm_risk,
    confirmation_covers,
    decide_publication,
    measure_risk,
    policy_from_config,
    resolve_policy,
)
from kew.publication_filter import ManifestEntry, filter_manifest

_REPO = "acme/widgets"
_BASE = "b" * 40
_TREE = "c" * 40
_SUCCESSOR_TREE = "e" * 40
_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _policy(**overrides: object) -> ReviewPolicy:
    fields: dict[str, object] = {"delivery_repo": _REPO}
    fields.update(overrides)
    return ReviewPolicy(**fields)  # type: ignore[arg-type]


def _risk(
    *,
    added: int = 0,
    deleted: int = 0,
    paths: int = 1,
    tree_sha: str = _TREE,
    binary: bool = False,
    measurable: bool = True,
    review_flags: tuple[str, ...] = (),
    refusals: tuple[str, ...] = (),
):
    """A measurement with *paths* files, the line budget spent on the first."""
    changes = [
        PathChange(
            path=f"src/file{index}.py",
            added=added if index == 0 else 0,
            deleted=deleted if index == 0 else 0,
            binary=binary and index == 0,
            measurable=measurable or index != 0,
        )
        for index in range(paths)
    ]
    return measure_risk(
        base_sha=_BASE,
        tree_sha=tree_sha,
        changes=changes,
        review_flags=review_flags,
        refusals=refusals,
    )


def _decide(policy: ReviewPolicy, risk, pending: int = 0):
    return decide_publication(policy=policy, risk=risk, pending_reviews=pending)


class TestTheDefaultsTheProductPromises:
    def test_the_shipped_policy_is_three_reviews_five_hundred_lines_twenty_paths(
        self,
    ) -> None:
        policy = _policy()

        assert policy.max_pending_reviews == SYSTEM_MAX_PENDING_REVIEWS == 3
        assert policy.max_diff_lines == SYSTEM_MAX_DIFF_LINES == 500
        assert policy.max_paths == SYSTEM_MAX_PATHS == 20

    def test_the_config_section_is_the_same_three_numbers(self) -> None:
        config = DispatchConfig(project={"repo": _REPO})

        policy = policy_from_config(config)

        assert policy.delivery_repo == _REPO
        assert (policy.max_pending_reviews, policy.max_diff_lines) == (3, 500)
        assert policy.max_paths == 20

    def test_approvers_fall_back_to_the_spend_allowlist(self) -> None:
        """Naming approvers once is enough; naming them twice invites drift."""
        config = DispatchConfig(
            project={"repo": _REPO}, approval={"approvers": ["ada"]}
        )

        assert policy_from_config(config).approvers == ("ada",)

    def test_a_human_review_allowlist_wins_over_the_spend_allowlist(self) -> None:
        config = DispatchConfig(
            project={"repo": _REPO},
            approval={"approvers": ["ada"]},
            human_review={"approvers": ["grace"]},
        )

        assert policy_from_config(config).approvers == ("grace",)


class TestCapacityBelongsToOneDeliveryRepository:
    def test_a_policy_that_names_no_delivery_repository_is_refused(self) -> None:
        """There is no such thing as a global count of pending reviews."""
        with pytest.raises(ReviewPolicyError, match="canonical GitHub delivery"):
            _policy(delivery_repo="").validate()

    def test_a_bare_project_name_is_not_a_delivery_repository(self) -> None:
        with pytest.raises(ReviewPolicyError, match="owner/name"):
            _policy(delivery_repo="widgets").validate()

    def test_occupancy_below_the_limit_still_admits_a_review(self) -> None:
        decision = _decide(_policy(), _risk(added=900), pending=2)

        assert decision.outcome == CONFIRMATION_REQUIRED

    def test_occupancy_at_the_limit_blocks_rather_than_refusing(self) -> None:
        decision = _decide(_policy(), _risk(added=900), pending=3)

        assert decision.outcome == BLOCKED
        assert REASON_CAPACITY in decision.reasons
        assert decision.retryable and not decision.overridable

    def test_a_full_queue_does_not_hold_up_work_that_needs_no_review(self) -> None:
        """A slot is only spent by work that actually wants a person."""
        decision = _decide(_policy(), _risk(added=10), pending=99)

        assert decision.outcome == ALLOWED
        assert decision.publishable

    def test_a_cross_repository_count_cannot_be_smuggled_in(self) -> None:
        with pytest.raises(ReviewPolicyError, match="non-negative count"):
            _decide(_policy(), _risk(added=900), pending=-1)


class TestTheDiffBoundaryIsExact:
    def test_exactly_max_diff_lines_publishes_unattended(self) -> None:
        assert _decide(_policy(), _risk(added=500)).outcome == ALLOWED

    def test_one_line_over_max_diff_lines_stops_for_a_person(self) -> None:
        decision = _decide(_policy(), _risk(added=501))

        assert decision.outcome == CONFIRMATION_REQUIRED
        assert decision.reasons == (REASON_DIFF_LINES,)

    def test_the_count_is_additions_plus_deletions(self) -> None:
        """250 added and 250 removed is a 500-line change, not a 250-line one."""
        assert _decide(_policy(), _risk(added=250, deleted=250)).outcome == ALLOWED
        assert (
            _decide(_policy(), _risk(added=250, deleted=251)).outcome
            == CONFIRMATION_REQUIRED
        )

    def test_a_large_deletion_is_not_the_harmless_direction(self) -> None:
        decision = _decide(_policy(), _risk(deleted=900))

        assert decision.reasons == (REASON_DIFF_LINES,)

    def test_a_zero_max_diff_lines_holds_everything(self) -> None:
        """Zero is the loud end of the range, never a disabled check."""
        decision = _decide(_policy(max_diff_lines=0), _risk(added=1))

        assert decision.outcome == CONFIRMATION_REQUIRED


class TestThePathBoundaryIsExact:
    def test_exactly_max_paths_publishes_unattended(self) -> None:
        assert _decide(_policy(), _risk(paths=20)).outcome == ALLOWED

    def test_one_path_over_max_paths_stops_for_a_person(self) -> None:
        decision = _decide(_policy(), _risk(paths=21))

        assert decision.outcome == CONFIRMATION_REQUIRED
        assert decision.reasons == (REASON_PATHS,)

    def test_the_same_path_twice_is_one_path(self) -> None:
        risk = measure_risk(
            base_sha=_BASE,
            tree_sha=_TREE,
            changes=[PathChange("a.py", added=1), PathChange("a.py", deleted=1)],
        )

        assert risk.path_count == 1
        assert risk.changed_lines == 2


class TestWorkNobodyCanMeasureStops:
    def test_a_binary_path_holds_the_publication(self) -> None:
        decision = _decide(_policy(), _risk(binary=True))

        assert decision.outcome == CONFIRMATION_REQUIRED
        assert decision.reasons == (REASON_BINARY,)

    def test_an_unmeasurable_path_holds_the_publication(self) -> None:
        decision = _decide(_policy(), _risk(measurable=False))

        assert decision.reasons == (REASON_UNMEASURABLE,)

    def test_unmeasurable_work_never_scores_a_reassuring_zero(self) -> None:
        """A blob's bytes are not lines; counting them either way is a lie."""
        risk = _risk(added=9_000, binary=True)

        assert risk.changed_lines == 0
        assert not risk.measurable
        assert risk.binary_paths == ("src/file0.py",)


class TestTheFilterFeedsThePolicy:
    def test_a_flagged_manifest_asks_for_a_person(self) -> None:
        verdict = filter_manifest(
            [ManifestEntry(path="Makefile", mode="100644", sha256="a" * 64)],
            deletions=(),
            existing_modes={},
            destination_paths=(),
        )

        decision = _decide(
            _policy(), _risk(added=1, review_flags=verdict.review_reasons())
        )

        assert verdict.review_required
        assert decision.reasons == (REASON_REVIEW_FLAG,)

    def test_a_refused_manifest_is_refused_not_reviewed(self) -> None:
        verdict = filter_manifest(
            [ManifestEntry(path=".git/config", mode="100644", sha256="a" * 64)],
            deletions=(),
            existing_modes={},
            destination_paths=(),
        )

        decision = _decide(
            _policy(), _risk(added=1, refusals=verdict.refusal_reasons())
        )

        assert not verdict.review_required
        assert decision.outcome == REFUSED

    def test_a_refusal_outranks_every_other_reason(self) -> None:
        decision = _decide(
            _policy(),
            _risk(added=9_000, paths=30, binary=True, refusals=("x: no",)),
            pending=99,
        )

        assert decision.outcome == REFUSED


class TestARefusalIsNotOverridable:
    def test_no_approver_can_confirm_a_refused_artifact(self) -> None:
        policy = _policy(approvers=("ada",))
        decision = _decide(policy, _risk(added=1, refusals=("x: no",)))

        with pytest.raises(RefusalNotOverridableError):
            confirm_risk(
                job_id="pj-1", decision=decision, policy=policy, actor="ada", now=_NOW
            )

    def test_a_refused_decision_is_neither_overridable_nor_retryable(self) -> None:
        decision = _decide(_policy(), _risk(added=1, refusals=("x: no",)))

        assert not decision.overridable
        assert not decision.retryable
        assert not decision.publishable

    def test_a_blocked_decision_has_no_hold_to_confirm(self) -> None:
        policy = _policy()
        decision = _decide(policy, _risk(added=900), pending=3)

        with pytest.raises(ReviewCapacityError):
            confirm_risk(
                job_id="pj-1", decision=decision, policy=policy, actor="ada", now=_NOW
            )

    def test_confirming_an_unheld_publication_is_refused(self) -> None:
        policy = _policy()
        decision = _decide(policy, _risk(added=1))

        with pytest.raises(ReviewPolicyError, match="nothing was held"):
            confirm_risk(
                job_id="pj-1", decision=decision, policy=policy, actor="ada", now=_NOW
            )


class TestTerminalJudgementIsAuthenticated:
    def test_an_unnamed_actor_cannot_release_a_hold(self) -> None:
        with pytest.raises(ReviewAuthorityError, match="unauthenticated"):
            authorize_approver(_policy(), "   ")

    def test_an_actor_outside_the_allowlist_cannot_release_a_hold(self) -> None:
        with pytest.raises(ReviewAuthorityError, match="not a human_review"):
            authorize_approver(_policy(approvers=("ada",)), "mallory")

    def test_an_empty_allowlist_admits_any_named_actor(self) -> None:
        """The same stance the spend gate takes — never 'anyone', but not nobody."""
        authorize_approver(_policy(), "ada")

    def test_a_confirmation_from_a_stranger_is_refused(self) -> None:
        policy = _policy(approvers=("ada",))
        decision = _decide(policy, _risk(added=900))

        with pytest.raises(ReviewAuthorityError):
            confirm_risk(
                job_id="pj-1",
                decision=decision,
                policy=policy,
                actor="mallory",
                now=_NOW,
            )

    def test_a_confirmation_under_a_different_policy_is_refused(self) -> None:
        decided_under = _policy(max_diff_lines=100)
        decision = _decide(decided_under, _risk(added=900))

        with pytest.raises(ReviewPolicyError, match="different policy"):
            confirm_risk(
                job_id="pj-1",
                decision=decision,
                policy=_policy(),
                actor="ada",
                now=_NOW,
            )


class TestAConfirmationIsBoundToTheExactSha:
    def test_a_confirmation_covers_the_tree_it_was_given_on(self) -> None:
        policy = _policy()
        decision = _decide(policy, _risk(added=900))

        confirmation = confirm_risk(
            job_id="pj-1", decision=decision, policy=policy, actor="ada", now=_NOW
        )

        assert confirmation_covers(confirmation, decision=decision)
        assert confirmation.tree_sha == _TREE

    def test_remediation_that_changes_the_sha_invalidates_it(self) -> None:
        policy = _policy()
        decision = _decide(policy, _risk(added=900))
        confirmation = confirm_risk(
            job_id="pj-1", decision=decision, policy=policy, actor="ada", now=_NOW
        )

        successor = _decide(policy, _risk(added=900, tree_sha=_SUCCESSOR_TREE))

        assert not confirmation_covers(confirmation, decision=successor)

    def test_a_changed_measurement_on_the_same_tree_invalidates_it(self) -> None:
        """Same SHA, different diff, is a contradiction — never a match."""
        policy = _policy()
        confirmation = confirm_risk(
            job_id="pj-1",
            decision=_decide(policy, _risk(added=900)),
            policy=policy,
            actor="ada",
            now=_NOW,
        )

        assert not confirmation_covers(
            confirmation, decision=_decide(policy, _risk(added=901))
        )

    def test_a_policy_edit_invalidates_it(self) -> None:
        policy = _policy()
        confirmation = confirm_risk(
            job_id="pj-1",
            decision=_decide(policy, _risk(added=900)),
            policy=policy,
            actor="ada",
            now=_NOW,
        )

        edited = _policy(max_diff_lines=100, generation=1)

        assert not confirmation_covers(
            confirmation, decision=_decide(edited, _risk(added=900))
        )


class TestThePolicyHashAndGenerationAnswerDifferentQuestions:
    def test_the_hash_covers_the_values(self) -> None:
        assert _policy().policy_hash == _policy().policy_hash
        assert _policy().policy_hash != _policy(max_paths=21).policy_hash

    def test_the_hash_ignores_which_edit_stated_the_values(self) -> None:
        assert _policy(generation=0).policy_hash == _policy(generation=9).policy_hash

    def test_approver_order_is_not_a_policy_change(self) -> None:
        left = _policy(approvers=("ada", "grace"))
        right = _policy(approvers=("grace", "ada"))

        assert left.policy_hash == right.policy_hash

    def test_the_delivery_repository_is_part_of_the_hash(self) -> None:
        assert _policy().policy_hash != _policy(delivery_repo="acme/other").policy_hash


class TestDurableExpiryRevertsToTheSystemPolicy:
    def test_an_unexpired_override_is_the_policy_in_force(self) -> None:
        override = _policy(
            max_diff_lines=50, generation=1, expires_at="2026-09-01T00:00:00+00:00"
        )

        resolution = resolve_policy(system=_policy(), override=override, now=_NOW)

        assert resolution.policy is override
        assert not resolution.reverted

    def test_an_expired_override_reverts_to_the_system_values(self) -> None:
        override = _policy(
            max_diff_lines=50, generation=1, expires_at="2026-08-01T00:00:00+00:00"
        )

        resolution = resolve_policy(system=_policy(), override=override, now=_NOW)

        assert resolution.reverted
        assert resolution.reason == REVERSION_EXPIRED
        assert resolution.policy.max_diff_lines == 500
        assert resolution.policy.expires_at is None

    def test_a_reversion_advances_the_generation(self) -> None:
        """Reverting is itself a policy change, so holds granted before it lapse."""
        override = _policy(
            max_diff_lines=50, generation=4, expires_at="2026-08-01T00:00:00+00:00"
        )

        resolution = resolve_policy(system=_policy(), override=override, now=_NOW)

        assert resolution.policy.generation == 5

    def test_an_expiry_exactly_now_has_expired(self) -> None:
        override = _policy(generation=1, expires_at=_NOW.isoformat())

        assert resolve_policy(system=_policy(), override=override, now=_NOW).reverted

    def test_a_naive_expiry_is_refused_rather_than_guessed(self) -> None:
        override = _policy(generation=1, expires_at="2026-08-01T00:00:00")

        with pytest.raises(ReviewPolicyError, match="explicit UTC"):
            resolve_policy(system=_policy(), override=override, now=_NOW)

    def test_an_override_may_not_move_the_delivery_repository(self) -> None:
        override = ReviewPolicy(delivery_repo="acme/other", generation=1)

        with pytest.raises(ReviewPolicyError, match="may not move"):
            resolve_policy(system=_policy(), override=override, now=_NOW)

    def test_an_override_must_carry_a_newer_generation(self) -> None:
        with pytest.raises(ReviewPolicyError, match="does not supersede"):
            resolve_policy(
                system=_policy(generation=3), override=_policy(generation=3), now=_NOW
            )

    def test_no_override_leaves_the_system_policy_alone(self) -> None:
        resolution = resolve_policy(system=_policy(), override=None, now=_NOW)

        assert resolution.policy == _policy()
        assert not resolution.reverted


class TestAMeasurementNamesTheTreeItWasTakenOn:
    def test_a_measurement_needs_a_real_base_and_tree(self) -> None:
        with pytest.raises(ReviewPolicyError, match="not a commit id"):
            measure_risk(base_sha="nope", tree_sha=_TREE, changes=())

    def test_the_digest_changes_with_the_tree(self) -> None:
        assert (
            _risk(added=1).risk_digest
            != _risk(added=1, tree_sha=_SUCCESSOR_TREE).risk_digest
        )

    def test_the_decision_carries_the_tree_and_the_policy_it_used(self) -> None:
        policy = _policy(generation=7)

        decision = _decide(policy, _risk(added=900))

        assert decision.tree_sha == _TREE
        assert decision.base_sha == _BASE
        assert decision.delivery_repo == _REPO
        assert decision.policy_generation == 7
        assert decision.policy_hash == policy.policy_hash
