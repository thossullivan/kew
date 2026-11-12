"""I09A/I09B vocabulary: the commit, push and pull request a job names.

Materialization has to survive a crash at every point, which means the commit
kew creates must be a pure function of rows that were written *before* git was
asked for anything. This module tests exactly that function.

Two properties carry the node:

* **Nothing here reads a clock, an environment or a repository.** The tree, the
  parent, the message, the identities and the timestamp all come off the
  persisted job, so a host that restarts mid-publication recomputes the same
  commit rather than a second one that happens to hold the same files.
* **An intent that cannot name one commit is refused.** A settled job, a
  malformed tree or parent, or a timestamp nothing can parse produce a refusal
  rather than a commit built from a plausible-looking default.

I09B holds the two remote effects to the same rule. A push and a pull request
are named by the persisted row plus *what the forge was just observed to show* —
never by what the host remembers doing. The classifiers below are the whole of
that judgment: each one names one effect, says the remote already holds it, or
raises a typed conflict. Nothing in between, and nothing that forces.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew.publication_job import (
    ABANDONED,
    AMBIGUOUS,
    COMMITTED,
    OPENED,
    PENDING,
    PR_CLOSED,
    PR_MERGED,
    PR_OPEN,
    PUSHED,
    RECONCILED,
    REMOTE_BRANCH,
    REMOTE_CONFLICT,
    REMOTE_PULL_REQUEST,
    REVIEW_REMOTE_AVAILABLE,
    REVIEW_REMOTE_UNAVAILABLE,
    SLOT_HELD,
    SLOT_RELEASED,
    STEP_COMMIT,
    SUPERSEDED,
    CommitIdentity,
    CommitIntent,
    ObservedBranch,
    ObservedPullRequest,
    PublicationCapacityUnknownError,
    PublicationIntent,
    PublicationJob,
    PublicationJobStateError,
    PublicationJobValueError,
    PublicationRemoteConflictError,
    RemoteObservation,
    RemoteReviewObservation,
    ReviewObligation,
    commit_intent_for,
    count_review_occupancy,
    host_commit_identity,
    publication_marker,
    pull_request_intent_for,
    push_intent_for,
    require_countable_observation,
    resolve_observation,
    review_slot_disposition,
    unavailable_review_observation,
)

_BASE = "b" * 40
_TREE = "c" * 40
_HEAD = "d" * 40


def _job(**overrides: object) -> PublicationJob:
    fields: dict[str, object] = {
        "job_id": "pj-0123456789abcdef",
        "lineage_key": "github:acme/widgets:agent/issue-7",
        "source_kind": "github",
        "source_id": "acme/widgets",
        "run_id": "run-1",
        "attempt_number": 1,
        "branch": "agent/issue-7",
        "base_sha": _BASE,
        "tree_sha": _TREE,
        "receipt_digest": "sha256:" + "e" * 64,
        "message": "feat(#7): the change the agent made",
        "state": PENDING,
        "resume_state": None,
        "head_sha": None,
        "remote_state": None,
        "remote_ref": None,
        "attempt_count": 0,
        "last_step": None,
        "last_attempt_at": None,
        "detail": None,
        "predecessor_job_id": None,
        "superseded_by": None,
        "created_at": "2026-08-18T12:00:00+00:00",
        "updated_at": "2026-08-18T12:00:00+00:00",
    }
    fields.update(overrides)
    return PublicationJob(**fields)  # type: ignore[arg-type]


class TestTheCommitIntentIsNamedByThePersistedRow:
    def test_the_tree_parent_and_message_come_off_the_job(self) -> None:
        intent = commit_intent_for(_job())

        assert intent.tree_sha == _TREE
        assert intent.parent_sha == _BASE
        assert intent.message == "feat(#7): the change the agent made"

    def test_the_commit_timestamp_is_the_job_creation_it_persisted(self) -> None:
        """Not the clock. A retry an hour later must name the same commit."""
        intent = commit_intent_for(_job())

        assert intent.when == "@1787054400 +0000"

    def test_a_naive_persisted_timestamp_is_read_as_utc(self) -> None:
        """SQLite rows carry whatever the caller stamped; UTC is the one
        reading that does not move a commit when a host's zone changes."""
        naive = commit_intent_for(_job(created_at="2026-08-18T12:00:00"))
        aware = commit_intent_for(_job(created_at="2026-08-18T12:00:00+00:00"))

        assert naive.when == aware.when

    def test_recomputing_from_the_same_row_names_the_same_intent(self) -> None:
        job = _job()

        assert commit_intent_for(job) == commit_intent_for(_job())

    def test_the_fields_a_retry_changes_do_not_change_the_intent(self) -> None:
        """attempt_count, last_step and updated_at move on every attempt. If
        any of them reached the commit, a retry would produce a second SHA."""
        first = commit_intent_for(_job())

        retried = commit_intent_for(
            _job(
                attempt_count=4,
                last_step=STEP_COMMIT,
                last_attempt_at="2026-08-19T09:00:00+00:00",
                updated_at="2026-08-19T09:00:00+00:00",
                detail="resumed after a crash",
            )
        )

        assert retried == first

    def test_the_identities_default_to_the_host_never_the_agent(self) -> None:
        intent = commit_intent_for(_job())

        assert intent.author == host_commit_identity()
        assert intent.committer == host_commit_identity()

    def test_an_explicit_identity_is_used_for_both_roles(self) -> None:
        identity = CommitIdentity(name="Release Bot", email="bot@example.invalid")

        intent = commit_intent_for(_job(), identity=identity)

        assert (intent.author, intent.committer) == (identity, identity)

    def test_a_job_with_no_message_still_names_one_deterministic_commit(self) -> None:
        intent = commit_intent_for(_job(message=""))

        assert intent.message == commit_intent_for(_job(message="")).message
        assert "run-1" in intent.message and "1" in intent.message


class TestTheSuccessorParent:
    def test_a_successor_commits_onto_the_head_its_predecessor_left(self) -> None:
        intent = commit_intent_for(
            _job(predecessor_job_id="pj-older"), predecessor_head=_HEAD
        )

        assert intent.parent_sha == _HEAD

    def test_a_predecessor_that_never_committed_leaves_the_base_as_parent(
        self,
    ) -> None:
        intent = commit_intent_for(
            _job(predecessor_job_id="pj-older"), predecessor_head=None
        )

        assert intent.parent_sha == _BASE

    def test_a_predecessor_head_offered_without_a_predecessor_is_refused(self) -> None:
        """A parent nothing in the row points at is a mismatch, not a hint."""
        with pytest.raises(PublicationJobValueError, match="no predecessor"):
            commit_intent_for(_job(), predecessor_head=_HEAD)

    def test_a_malformed_predecessor_head_is_refused(self) -> None:
        with pytest.raises(PublicationJobValueError, match="not a commit id"):
            commit_intent_for(
                _job(predecessor_job_id="pj-older"), predecessor_head="HEAD~1"
            )


class TestAnIntentThatCannotNameOneCommitIsRefused:
    @pytest.mark.parametrize("state", [RECONCILED, ABANDONED, SUPERSEDED])
    def test_a_settled_job_names_no_commit(self, state: str) -> None:
        with pytest.raises(PublicationJobStateError, match="settled"):
            commit_intent_for(_job(state=state))

    def test_a_committed_job_still_names_the_commit_it_already_made(self) -> None:
        """Recomputing is how a restarted host checks the SHA it recorded."""
        intent = commit_intent_for(_job(state=COMMITTED, head_sha=_HEAD))

        assert intent.tree_sha == _TREE

    @pytest.mark.parametrize("tree", ["", "not-a-tree", "c" * 39])
    def test_a_malformed_tree_names_no_commit(self, tree: str) -> None:
        with pytest.raises(PublicationJobValueError, match="not a tree id"):
            commit_intent_for(_job(tree_sha=tree))

    def test_a_malformed_base_names_no_commit(self) -> None:
        with pytest.raises(PublicationJobValueError, match="not a commit id"):
            commit_intent_for(_job(base_sha="main"))

    def test_a_timestamp_nothing_can_parse_names_no_commit(self) -> None:
        with pytest.raises(PublicationJobValueError, match="not a timestamp"):
            commit_intent_for(_job(created_at="last tuesday"))


class TestTheIntentIsInertData:
    def test_a_commit_intent_cannot_be_edited_after_it_is_derived(self) -> None:
        intent = commit_intent_for(_job())

        with pytest.raises(AttributeError):
            intent.tree_sha = "0" * 40  # type: ignore[misc]

    def test_a_replaced_intent_is_a_different_intent(self) -> None:
        intent = commit_intent_for(_job())

        assert replace(intent, message="other") != intent

    def test_the_intent_carries_no_environment(self) -> None:
        """Everything git is told comes from these fields and nowhere else."""
        assert set(CommitIntent.__dataclass_fields__) == {
            "tree_sha",
            "parent_sha",
            "message",
            "author",
            "committer",
            "when",
            "extra_parents",
        }


_BRANCH = "agent/issue-7"
_BASE_BRANCH = "main"
_FOREIGN = "f" * 40


def _pushable(**overrides: object) -> PublicationJob:
    """A job that already holds its commit, which is where I09B starts."""
    fields: dict[str, object] = {"state": COMMITTED, "head_sha": _HEAD}
    fields.update(overrides)
    return _job(**fields)


def _observed_pull_request(**overrides: object) -> ObservedPullRequest:
    fields: dict[str, object] = {
        "number": 12,
        "url": "https://forge.invalid/pull/12",
        "head_branch": _BRANCH,
        "base_branch": _BASE_BRANCH,
        "head_sha": _HEAD,
    }
    fields.update(overrides)
    return ObservedPullRequest(**fields)  # type: ignore[arg-type]


class TestThePushIsNamedByTheRowAndWhatTheRemoteShows:
    """Task 40: one push intent, carrying the lease the observation justifies."""

    def test_a_push_onto_a_branch_the_remote_lacks_expects_no_head(self) -> None:
        intent = push_intent_for(
            _pushable(), observed=ObservedBranch(name=_BRANCH, head_sha=None)
        )

        assert intent is not None
        assert (intent.branch, intent.head_sha) == (_BRANCH, _HEAD)
        assert intent.expected_head is None
        assert intent.lease == f"refs/heads/{_BRANCH}:"

    def test_a_push_onto_the_base_leases_exactly_the_head_it_observed(self) -> None:
        intent = push_intent_for(
            _pushable(), observed=ObservedBranch(name=_BRANCH, head_sha=_BASE)
        )

        assert intent is not None and intent.expected_head == _BASE
        assert intent.lease == f"refs/heads/{_BRANCH}:{_BASE}"

    def test_a_push_onto_the_predecessor_head_is_a_fast_forward(self) -> None:
        """A successor takes the branch on from where its predecessor left it."""
        predecessor = "a" * 40
        intent = push_intent_for(
            _pushable(predecessor_job_id="pj-older", head_sha=_HEAD),
            observed=ObservedBranch(name=_BRANCH, head_sha=predecessor),
            predecessor_head=predecessor,
        )

        assert intent is not None and intent.expected_head == predecessor

    def test_a_branch_already_at_our_head_names_no_push_at_all(self) -> None:
        assert (
            push_intent_for(
                _pushable(), observed=ObservedBranch(name=_BRANCH, head_sha=_HEAD)
            )
            is None
        )

    def test_a_push_onto_a_head_this_lineage_never_left_is_a_typed_conflict(
        self,
    ) -> None:
        with pytest.raises(PublicationRemoteConflictError) as err:
            push_intent_for(
                _pushable(), observed=ObservedBranch(name=_BRANCH, head_sha=_FOREIGN)
            )

        assert err.value.observation.kind == REMOTE_CONFLICT
        assert err.value.observation.ref == _BRANCH

    def test_a_push_from_a_job_that_has_no_commit_is_refused(self) -> None:
        with pytest.raises(PublicationJobStateError, match="no commit"):
            push_intent_for(
                _job(), observed=ObservedBranch(name=_BRANCH, head_sha=None)
            )

    def test_a_push_judged_against_another_branch_is_refused(self) -> None:
        """The observation has to be OF the branch the job names."""
        with pytest.raises(PublicationJobValueError, match="branch"):
            push_intent_for(
                _pushable(),
                observed=ObservedBranch(name="somebody/else", head_sha=None),
            )

    def test_a_push_onto_a_malformed_remote_head_is_refused(self) -> None:
        with pytest.raises(PublicationJobValueError, match="not a commit id"):
            push_intent_for(
                _pushable(), observed=ObservedBranch(name=_BRANCH, head_sha="HEAD")
            )


class TestThePullRequestIsNamedByTheRow:
    """Task 40: one pull request, or the one the forge already shows."""

    def test_the_pr_title_and_body_come_off_the_persisted_message(self) -> None:
        intent = pull_request_intent_for(
            _pushable(message="feat(#7): the change\n\nthe body the agent wrote"),
            base_branch=_BASE_BRANCH,
            observed=None,
        )

        assert intent is not None
        assert intent.title == "feat(#7): the change"
        assert "the body the agent wrote" in intent.body
        assert (intent.branch, intent.base_branch) == (_BRANCH, _BASE_BRANCH)
        assert intent.head_sha == _HEAD

    def test_the_pr_body_carries_the_marker_that_names_the_job(self) -> None:
        job = _pushable()
        intent = pull_request_intent_for(job, base_branch=_BASE_BRANCH, observed=None)

        assert intent is not None
        assert intent.marker == publication_marker(job)
        assert job.job_id in intent.marker
        assert intent.body.endswith(intent.marker)

    def test_the_pr_marker_is_the_same_string_every_time_it_is_derived(self) -> None:
        assert publication_marker(_pushable()) == publication_marker(_pushable())

    def test_a_job_with_no_message_still_names_one_pr_title(self) -> None:
        intent = pull_request_intent_for(
            _pushable(message=""), base_branch=_BASE_BRANCH, observed=None
        )

        assert intent is not None and intent.title

    def test_an_agent_opened_pr_from_the_same_branch_binds_rather_than_forking(
        self,
    ) -> None:
        """Compatible state binds: the agent's own PR is the lineage's PR."""
        assert (
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(body="opened by the agent"),
            )
            is None
        )

    def test_a_pr_the_forge_reports_without_a_head_still_binds(self) -> None:
        assert (
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(head_sha=None),
            )
            is None
        )

    def test_a_merged_pr_for_our_head_binds(self) -> None:
        assert (
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(state=PR_MERGED),
            )
            is None
        )

    def test_a_pr_onto_another_base_is_a_typed_conflict(self) -> None:
        with pytest.raises(PublicationRemoteConflictError) as err:
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(base_branch="release/1.x"),
            )

        assert err.value.observation.kind == REMOTE_CONFLICT

    def test_a_closed_pr_holds_rather_than_being_reopened(self) -> None:
        with pytest.raises(PublicationRemoteConflictError, match="closed"):
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(state=PR_CLOSED),
            )

    def test_a_pr_at_a_head_this_lineage_never_named_is_a_typed_conflict(self) -> None:
        with pytest.raises(PublicationRemoteConflictError):
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(head_sha=_FOREIGN),
            )

    def test_a_pr_from_another_branch_is_a_typed_conflict(self) -> None:
        with pytest.raises(PublicationRemoteConflictError):
            pull_request_intent_for(
                _pushable(),
                base_branch=_BASE_BRANCH,
                observed=_observed_pull_request(head_branch="somebody/else"),
            )

    def test_a_pr_for_a_job_with_no_commit_is_refused(self) -> None:
        with pytest.raises(PublicationJobStateError, match="no commit"):
            pull_request_intent_for(_job(), base_branch=_BASE_BRANCH, observed=None)

    def test_a_pr_onto_no_base_branch_is_refused(self) -> None:
        with pytest.raises(PublicationJobValueError, match="base branch"):
            pull_request_intent_for(_pushable(), base_branch="", observed=None)


class TestDoubtIsResolvedByWhatTheRemoteProves:
    """Task 40: the observations I09B has to be able to record."""

    def test_a_conflicting_remote_moves_the_push_nowhere(self) -> None:
        """Evidence worth recording, movement it does not justify."""
        assert (
            resolve_observation(
                state=COMMITTED,
                resume_state=None,
                observation=RemoteObservation(kind=REMOTE_CONFLICT, ref=_BRANCH),
            )
            == COMMITTED
        )

    def test_a_conflict_leaves_an_ambiguous_job_ambiguous(self) -> None:
        assert (
            resolve_observation(
                state=AMBIGUOUS,
                resume_state=PUSHED,
                observation=RemoteObservation(kind=REMOTE_CONFLICT),
            )
            == AMBIGUOUS
        )

    def test_reconciling_doubt_with_the_push_it_interrupted_resumes_the_job(
        self,
    ) -> None:
        """Seeing exactly what the job last knew ends the doubt. Anything less
        would strand a job that crashed between the push and its row."""
        assert (
            resolve_observation(
                state=AMBIGUOUS,
                resume_state=PUSHED,
                observation=RemoteObservation(kind=REMOTE_BRANCH, ref=_BRANCH),
            )
            == PUSHED
        )

    def test_reconciling_doubt_with_the_pr_it_interrupted_resumes_the_job(
        self,
    ) -> None:
        assert (
            resolve_observation(
                state=AMBIGUOUS,
                resume_state=OPENED,
                observation=RemoteObservation(kind=REMOTE_PULL_REQUEST, ref="pr/7"),
            )
            == OPENED
        )

    def test_a_push_observation_does_not_disprove_the_pr_a_job_knew_about(
        self,
    ) -> None:
        """Less than the job knew is not evidence against it — a host that did
        not look for the pull request has not shown that it is gone."""
        assert (
            resolve_observation(
                state=AMBIGUOUS,
                resume_state=OPENED,
                observation=RemoteObservation(kind=REMOTE_BRANCH, ref=_BRANCH),
            )
            == AMBIGUOUS
        )


# ---------------------------------------------------------------------------
# I14: the capacity a publication holds while a person looks at it.
# ---------------------------------------------------------------------------

_REPO = "acme/widgets"
_OTHER = "acme/gadgets"
_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _observation(**overrides: object) -> RemoteReviewObservation:
    fields: dict[str, object] = {
        "delivery_repo": _REPO,
        "availability": REVIEW_REMOTE_AVAILABLE,
        "observed_at": _NOW.isoformat(),
        "generation": 0,
        "obligations": (),
        "detail": "",
    }
    fields.update(overrides)
    return RemoteReviewObservation(**fields)  # type: ignore[arg-type]


class TestWhatTheForgeWasSeenHoldingIsATypedFact:
    """An observation carries availability and a time, or it carries nothing.

    The obligation list alone is not a fact anybody may act on: read without
    the availability beside it, a failed ``gh`` call becomes an empty review
    queue, and an empty review queue is what frees a slot.
    """

    def test_an_observation_round_trips_through_its_persisted_form(self) -> None:
        original = _observation(
            generation=7,
            obligations=(
                ReviewObligation(ref="https://x/pull/1", job_id="pj-1", detail="a"),
                ReviewObligation(ref="https://x/pull/2"),
            ),
            detail="2 open pull request(s)",
        )
        original.validate()

        assert RemoteReviewObservation.from_json(original.as_json()) == original

    def test_an_unavailable_observation_may_not_claim_it_saw_anything(self) -> None:
        blind = _observation(
            availability=REVIEW_REMOTE_UNAVAILABLE,
            obligations=(ReviewObligation(ref="https://x/pull/1"),),
        )

        with pytest.raises(PublicationJobValueError, match="may claim nothing"):
            blind.validate()

    def test_a_failed_remote_read_produces_unavailable_never_empty(self) -> None:
        blind = unavailable_review_observation(
            delivery_repo=_REPO, generation=3, observed_at=_NOW.isoformat()
        )
        blind.validate()

        assert not blind.available
        assert blind.obligations == ()
        assert blind.detail

    def test_an_observation_time_without_a_zone_is_refused(self) -> None:
        with pytest.raises(PublicationJobValueError, match="explicit UTC"):
            _observation(observed_at="2026-08-21T12:00:00").validate()


class TestStaleOrUnavailableRemoteTruthIsNeverZero:
    """Four ways to be uncountable, and not one of them frees a slot."""

    def _countable(self, observation: RemoteReviewObservation | None, **kw: object):
        return require_countable_observation(
            observation,
            delivery_repo=_REPO,
            expected_generation=kw.get("expected_generation", 0),  # type: ignore[arg-type]
            now=kw.get("now", _NOW),  # type: ignore[arg-type]
            max_age_seconds=kw.get("max_age_seconds", 300.0),  # type: ignore[arg-type]
        )

    def test_a_fresh_successful_observation_of_this_repo_is_countable(self) -> None:
        assert self._countable(_observation()).generation == 0

    def test_never_reconciled_refuses_rather_than_counting_zero(self) -> None:
        with pytest.raises(PublicationCapacityUnknownError, match="reconcile"):
            self._countable(None)

    def test_another_repositorys_observation_is_not_this_ones(self) -> None:
        with pytest.raises(PublicationCapacityUnknownError, match=_OTHER):
            self._countable(_observation(delivery_repo=_OTHER))

    def test_an_older_generation_than_the_caller_reconciled_is_refused(self) -> None:
        with pytest.raises(PublicationCapacityUnknownError, match="generation 0"):
            self._countable(_observation(generation=0), expected_generation=1)

    def test_an_unreadable_forge_is_not_an_empty_one(self) -> None:
        with pytest.raises(PublicationCapacityUnknownError, match="not an empty one"):
            self._countable(_observation(availability=REVIEW_REMOTE_UNAVAILABLE))

    def test_an_observation_older_than_the_window_is_refused(self) -> None:
        with pytest.raises(PublicationCapacityUnknownError, match="past the"):
            self._countable(
                _observation(), now=_NOW + timedelta(hours=2), max_age_seconds=300.0
            )


class TestWhatFreesAReviewSlot:
    """Only a terminal human decision, or a definite absence. Doubt holds."""

    def _pr(self, state: str = PR_OPEN) -> ObservedPullRequest:
        return ObservedPullRequest(
            number=4,
            url="https://x/pull/4",
            head_branch="agent/issue-7",
            base_branch="main",
            state=state,
        )

    @pytest.mark.parametrize("state", [PR_MERGED, PR_CLOSED])
    def test_a_terminal_human_decision_releases_the_slot(self, state: str) -> None:
        disposition = review_slot_disposition(
            availability=REVIEW_REMOTE_AVAILABLE, pull_request=self._pr(state)
        )

        assert disposition == SLOT_RELEASED

    def test_an_open_pull_request_holds_however_its_checks_look(self) -> None:
        # Approved, changes-requested, red CI and conflicting all report the
        # same forge state: still open, still in front of a person.
        disposition = review_slot_disposition(
            availability=REVIEW_REMOTE_AVAILABLE, pull_request=self._pr()
        )

        assert disposition == SLOT_HELD

    def test_a_definite_absence_before_any_pull_request_releases(self) -> None:
        disposition = review_slot_disposition(
            availability=REVIEW_REMOTE_AVAILABLE, pull_request=None
        )

        assert disposition == SLOT_RELEASED

    def test_an_outage_holds_provisionally_rather_than_releasing(self) -> None:
        assert (
            review_slot_disposition(
                availability=REVIEW_REMOTE_UNAVAILABLE, pull_request=None
            )
            == SLOT_HELD
        )
        assert (
            review_slot_disposition(
                availability=REVIEW_REMOTE_UNAVAILABLE, pull_request=self._pr(PR_MERGED)
            )
            == SLOT_HELD
        )


class TestOccupancyUnionsLocalHoldsWithWhatTheForgeShows:
    """One publication in front of a person is one obligation, counted once."""

    def test_a_local_hold_and_its_own_pull_request_are_one_obligation(self) -> None:
        occupancy = count_review_occupancy(
            held_lineages=["github:acme/widgets:agent/issue-7"],
            obligations=[ReviewObligation(ref="https://x/pull/4", job_id="pj-a")],
            lineage_by_job={"pj-a": "github:acme/widgets:agent/issue-7"},
        )

        assert occupancy == 1

    def test_a_successor_sha_collapses_onto_its_predecessors_lineage(self) -> None:
        lineage = "github:acme/widgets:agent/issue-7"
        occupancy = count_review_occupancy(
            held_lineages=[lineage],
            obligations=[ReviewObligation(ref="https://x/pull/4", job_id="pj-old")],
            lineage_by_job={"pj-old": lineage, "pj-new": lineage},
        )

        assert occupancy == 1

    def test_a_pull_request_this_host_did_not_open_still_occupies_a_reviewer(
        self,
    ) -> None:
        occupancy = count_review_occupancy(
            held_lineages=["github:acme/widgets:agent/issue-7"],
            obligations=[ReviewObligation(ref="https://x/pull/9")],
            lineage_by_job={},
        )

        assert occupancy == 2


class TestTheAtomicPublicationCapacityTransaction:
    """Two finishers cannot exceed the cap, and nobody counts from a guess.

    Everything below goes through the real store, because the property being
    tested is a property of one serialized ``BEGIN IMMEDIATE`` step: the read
    that decides whether a slot is free and the write that takes it are not
    two things another host can get between.
    """

    _PACKAGE = "sha256:" + "a" * 64

    def _store(self, tmp_path: Path, name: str = "capacity.db") -> Any:
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / name))

    def _job(
        self, store: Any, *, repo: str = _REPO, run_id: str = "run-1", tree: str = _TREE
    ) -> Any:
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage

        receipt = store.seal_artifact_receipt(
            lineage=ArtifactLineage(
                source_kind="github",
                source_id=repo,
                run_id=run_id,
                attempt_number=1,
                base_sha=_BASE,
                package_digest=self._PACKAGE,
                tree_sha=tree,
                verification=VERIFICATION_EXACT_TREE,
                locator="packages/" + "a" * 64,
            ),
            sealed_at=_NOW,
        )
        return store.open_publication_job(
            intent=PublicationIntent(
                source_kind="github",
                source_id=repo,
                run_id=run_id,
                attempt_number=1,
                branch=f"agent/{run_id}",
                base_sha=_BASE,
                tree_sha=tree,
                receipt_digest=receipt.receipt_digest,
                message="feat: the change",
            ),
            now=_NOW,
        )

    def _cleared(
        self,
        store: Any,
        *,
        repo: str = _REPO,
        run_id: str = "run-1",
        policy: Any = None,
        tree: str = _TREE,
    ) -> Any:
        """A job measured under *policy* and cleared to take a publication slot."""
        from kew.human_review import PathChange, ReviewPolicy, decide_publication
        from kew.human_review import measure_risk as _measure

        policy = policy or ReviewPolicy(delivery_repo=repo)
        job = self._job(store, repo=repo, run_id=run_id, tree=tree)
        risk = _measure(
            base_sha=_BASE, tree_sha=tree, changes=[PathChange("src/a.py", added=1)]
        )
        decision = decide_publication(policy=policy, risk=risk, pending_reviews=0)
        store.record_publication_review(
            job_id=job.job_id, policy=policy, risk=risk, decision=decision, now=_NOW
        )
        return job

    def _observe(
        self,
        store: Any,
        *,
        repo: str = _REPO,
        generation: int = 0,
        obligations: tuple[ReviewObligation, ...] = (),
        availability: str = REVIEW_REMOTE_AVAILABLE,
        observed_at: str | None = None,
    ) -> Any:
        return store.record_review_observation(
            RemoteReviewObservation(
                delivery_repo=repo,
                availability=availability,
                observed_at=observed_at or _NOW.isoformat(),
                generation=generation,
                obligations=obligations,
            )
        )

    def _reserve(
        self, store: Any, job: Any, *, policy: Any, generation: int, now: Any = None
    ) -> Any:
        return store.reserve_publication_capacity(
            job_id=job.job_id,
            system_policy=policy,
            expected_generation=generation,
            actor="ada",
            now=now or _NOW,
        )

    # -- exactly once ----------------------------------------------------

    def test_two_finishers_at_the_cap_produce_one_reservation(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewCapacityError, ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=1)
        with self._store(tmp_path) as store:
            first = self._cleared(store, run_id="run-a", policy=policy)
            second = self._cleared(store, run_id="run-b", policy=policy)
            self._observe(store)

            taken = self._reserve(store, first, policy=policy, generation=0)
            self._observe(store, generation=1)
            with pytest.raises(ReviewCapacityError, match="holding 1 of 1"):
                self._reserve(store, second, policy=policy, generation=1)

            assert taken.occupancy == 1
            assert store.pending_review_count(_REPO) == 1
            assert store.publication_review(second.job_id).reserved_at is None

    def test_the_reservation_transaction_makes_no_network_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew.human_review import ReviewPolicy

        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("the capacity transaction reached the network")

        policy = ReviewPolicy(delivery_repo=_REPO)
        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=policy)
            self._observe(store)
            monkeypatch.setattr("kew.subprocess_utils.run", _forbidden)

            assert self._reserve(store, job, policy=policy, generation=0).occupancy == 1

    def test_a_reservation_survives_a_crash_as_exactly_one_hold(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=2)
        db = tmp_path / "crash.db"
        with self._store(tmp_path, "crash.db") as store:
            job = self._cleared(store, policy=policy)
            self._observe(store)
            self._reserve(store, job, policy=policy, generation=0)

        from kew.run_store import SqliteRunStore

        # The remote ack never came back; the host died holding the slot.
        with SqliteRunStore(str(db)) as reopened:
            assert reopened.pending_review_count(_REPO) == 1
            assert reopened.publication_review(job.job_id).occupies_slot

    def test_reserving_a_job_that_already_holds_its_slot_is_refused(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=3)
        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=policy)
            self._observe(store)
            self._reserve(store, job, policy=policy, generation=0)
            self._observe(store, generation=1)

            with pytest.raises(PublicationJobStateError, match="not cleared"):
                self._reserve(store, job, policy=policy, generation=1)

            assert store.pending_review_count(_REPO) == 1

    # -- stale and unavailable truth -------------------------------------

    def test_an_unavailable_observation_refuses_rather_than_reserving(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO)
        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=policy)
            self._observe(store, availability=REVIEW_REMOTE_UNAVAILABLE)

            with pytest.raises(
                PublicationCapacityUnknownError, match="not an empty one"
            ):
                self._reserve(store, job, policy=policy, generation=0)

            assert store.pending_review_count(_REPO) == 0

    def test_a_stale_observation_refuses_rather_than_reserving(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO)
        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=policy)
            self._observe(store)

            with pytest.raises(PublicationCapacityUnknownError, match="past the"):
                self._reserve(
                    store,
                    job,
                    policy=policy,
                    generation=0,
                    now=_NOW + timedelta(hours=2),
                )

    def test_an_observation_generation_may_not_walk_backwards(
        self, tmp_path: Path
    ) -> None:
        with self._store(tmp_path) as store:
            self._observe(store, generation=4)

            with pytest.raises(PublicationJobValueError, match="does not supersede"):
                self._observe(store, generation=4)

    # -- occupancy belongs to one repository -----------------------------

    def test_repository_a_at_its_cap_neither_consumes_nor_frees_repository_b(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewCapacityError, ReviewPolicy

        one = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=1)
        other = ReviewPolicy(delivery_repo=_OTHER, max_pending_reviews=1)
        with self._store(tmp_path) as store:
            busy = self._cleared(store, run_id="run-a", policy=one)
            crowded = self._cleared(store, run_id="run-b", policy=one)
            quiet = self._cleared(store, repo=_OTHER, run_id="run-c", policy=other)
            self._observe(store)
            self._observe(store, repo=_OTHER)

            self._reserve(store, busy, policy=one, generation=0)
            self._observe(store, generation=1)
            with pytest.raises(ReviewCapacityError, match=_REPO):
                self._reserve(store, crowded, policy=one, generation=1)
            # B is at its own limit of one, and A's saturation is not B's.
            self._reserve(store, quiet, policy=other, generation=0)

            assert store.pending_review_count(_REPO) == 1
            assert store.pending_review_count(_OTHER) == 1

    def test_repository_bs_own_backlog_is_what_stops_repository_b(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewCapacityError, ReviewPolicy

        one = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=3)
        other = ReviewPolicy(delivery_repo=_OTHER, max_pending_reviews=1)
        with self._store(tmp_path) as store:
            free = self._cleared(store, run_id="run-a", policy=one)
            blocked = self._cleared(store, repo=_OTHER, run_id="run-c", policy=other)
            self._observe(store)
            # B is already over its own limit, from remote truth alone.
            self._observe(
                store,
                repo=_OTHER,
                obligations=(
                    ReviewObligation(ref="https://x/gadgets/pull/1"),
                    ReviewObligation(ref="https://x/gadgets/pull/2"),
                ),
            )

            with pytest.raises(ReviewCapacityError, match="holding 2 of 1"):
                self._reserve(store, blocked, policy=other, generation=0)
            self._reserve(store, free, policy=one, generation=0)

            assert store.pending_review_count(_REPO) == 1
            assert store.pending_review_count(_OTHER) == 0

    def test_a_system_policy_for_another_repository_may_not_reserve_this_one(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=ReviewPolicy(delivery_repo=_REPO))
            self._observe(store)

            with pytest.raises(PublicationJobValueError, match="different delivery"):
                self._reserve(
                    store, job, policy=ReviewPolicy(delivery_repo=_OTHER), generation=0
                )

    # -- reconciliation restores occupancy after a restart ---------------

    def test_imported_remote_obligations_restore_occupancy_after_a_restart(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewCapacityError, ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=2)
        with self._store(tmp_path) as store:
            # A fresh ledger holding nothing locally: everything this host
            # knows about the queue came back off the forge.
            job = self._cleared(store, policy=policy)
            self._observe(
                store,
                obligations=(
                    ReviewObligation(ref="https://x/pull/1", job_id="pj-gone"),
                    ReviewObligation(ref="https://x/pull/2"),
                ),
            )

            with pytest.raises(ReviewCapacityError, match="holding 2 of 2"):
                self._reserve(store, job, policy=policy, generation=0)

    def test_a_kew_created_obligation_does_not_double_count_its_own_hold(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=2)
        with self._store(tmp_path) as store:
            held = self._cleared(store, run_id="run-a", policy=policy)
            waiting = self._cleared(store, run_id="run-b", policy=policy)
            self._observe(store)
            self._reserve(store, held, policy=policy, generation=0)
            # Reconciliation now sees the pull request that hold produced.
            self._observe(
                store,
                generation=1,
                obligations=(
                    ReviewObligation(ref="https://x/pull/1", job_id=held.job_id),
                ),
            )

            reserved = self._reserve(store, waiting, policy=policy, generation=1)

            assert reserved.occupancy == 2

    # -- the slot's lifetime ---------------------------------------------

    def test_a_successful_publication_keeps_its_slot_to_the_end_of_review(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=1)
        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=policy)
            self._observe(store)
            self._reserve(store, job, policy=policy, generation=0)
            for state in (COMMITTED, PUSHED, OPENED, RECONCILED):
                job = store.mark_publication_state(
                    job_id=job.job_id,
                    state=state,
                    expected_state=job.state,
                    now=_NOW,
                )

            # The publication is done. The review is not, so the slot stands.
            assert store.pending_review_count(_REPO) == 1
            assert store.publication_review(job.job_id).occupies_slot

    def test_an_ambiguous_disposition_leaves_the_hold_exactly_where_it_was(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO)
        with self._store(tmp_path) as store:
            job = self._cleared(store, policy=policy)
            self._observe(store)
            taken = self._reserve(store, job, policy=policy, generation=0)

            after = store.release_publication_capacity(
                job_id=job.job_id,
                disposition=review_slot_disposition(
                    availability=REVIEW_REMOTE_UNAVAILABLE, pull_request=None
                ),
                now=_NOW + timedelta(hours=1),
            )

            assert after == taken.review
            assert store.pending_review_count(_REPO) == 1

    def test_a_terminal_human_outcome_frees_the_slot_for_the_next_publication(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=1)
        with self._store(tmp_path) as store:
            merged = self._cleared(store, run_id="run-a", policy=policy)
            waiting = self._cleared(store, run_id="run-b", policy=policy)
            self._observe(store)
            self._reserve(store, merged, policy=policy, generation=0)

            store.release_publication_capacity(
                job_id=merged.job_id,
                disposition=review_slot_disposition(
                    availability=REVIEW_REMOTE_AVAILABLE,
                    pull_request=ObservedPullRequest(
                        number=1,
                        url="https://x/pull/1",
                        head_branch="agent/run-a",
                        base_branch="main",
                        state=PR_MERGED,
                    ),
                ),
                now=_NOW + timedelta(hours=1),
            )
            self._observe(store, generation=1)

            assert store.pending_review_count(_REPO) == 0
            assert (
                self._reserve(store, waiting, policy=policy, generation=1).occupancy
                == 1
            )

    def test_a_successor_sha_inherits_the_queue_place_it_does_not_add_one(
        self, tmp_path: Path
    ) -> None:
        from kew.human_review import ReviewPolicy

        policy = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=1)
        with self._store(tmp_path) as store:
            first = self._cleared(store, policy=policy)
            self._observe(store)
            self._reserve(store, first, policy=policy, generation=0)

            # Remediation moves the tree; the predecessor's hold goes with it.
            successor = self._successor(store, first)
            assert store.pending_review_count(_REPO) == 0
            assert store.get_publication_job(first.job_id).state == SUPERSEDED
            assert successor.predecessor_job_id == first.job_id

    def _successor(self, store: Any, predecessor: Any) -> Any:
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage

        tree = "f" * 40
        receipt = store.seal_artifact_receipt(
            lineage=ArtifactLineage(
                source_kind="github",
                source_id=_REPO,
                run_id=predecessor.run_id,
                attempt_number=2,
                base_sha=_BASE,
                package_digest=self._PACKAGE,
                tree_sha=tree,
                verification=VERIFICATION_EXACT_TREE,
                locator="packages/" + "b" * 64,
            ),
            sealed_at=_NOW,
        )
        return store.open_successor_publication_job(
            intent=PublicationIntent(
                source_kind="github",
                source_id=_REPO,
                run_id=predecessor.run_id,
                attempt_number=2,
                branch=predecessor.branch,
                base_sha=_BASE,
                tree_sha=tree,
                receipt_digest=receipt.receipt_digest,
                message="feat: the remediated change",
            ),
            now=_NOW,
        )

    # -- policy expiry, resolved inside the same transaction --------------

    def test_an_expired_override_is_reverted_and_audited_by_the_reservation(
        self, tmp_path: Path
    ) -> None:
        """Expiry is resolved and attested inside the reservation itself."""
        from dataclasses import replace as _replace

        from kew.human_review import ReviewPolicy

        system = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=3)
        override = ReviewPolicy(
            delivery_repo=_REPO,
            max_pending_reviews=9,
            generation=1,
            expires_at=(_NOW - timedelta(hours=1)).isoformat(),
        )
        # Reverting is itself a policy change, so it lands on a NEW generation
        # and the job has to have been cleared under that one.
        reverted = _replace(system, generation=2, expires_at=None)
        with self._store(tmp_path) as store:
            store.set_human_review_policy(policy=override, actor="ada", now=_NOW)
            job = self._cleared(store, policy=reverted)
            self._observe(store)

            taken = self._reserve(store, job, policy=system, generation=0)

            assert taken.policy == reverted
            assert store.get_human_review_policy(_REPO) is None
            assert [
                event.event
                for event in store.list_audit_events()
                if event.event == "human_review_policy_reverted"
            ] == ["human_review_policy_reverted"]

    def test_a_job_cleared_under_the_expired_override_reserves_nothing_at_all(
        self, tmp_path: Path
    ) -> None:
        """One transaction: a refused reservation leaves the ledger untouched.

        The reversion the resolution would have written rolls back with it,
        which is the point of resolving expiry *inside* the reservation rather
        than beside it — there is no window in which the policy has moved but
        the decision made under it has not.
        """
        from kew.human_review import ReviewPolicy

        system = ReviewPolicy(delivery_repo=_REPO, max_pending_reviews=3)
        override = ReviewPolicy(
            delivery_repo=_REPO,
            max_pending_reviews=9,
            generation=1,
            expires_at=(_NOW - timedelta(hours=1)).isoformat(),
        )
        with self._store(tmp_path) as store:
            store.set_human_review_policy(policy=override, actor="ada", now=_NOW)
            job = self._cleared(store, policy=override)
            self._observe(store)

            with pytest.raises(PublicationJobValueError, match="policy generation"):
                self._reserve(store, job, policy=system, generation=0)

            assert store.get_human_review_policy(_REPO) == override
            assert store.pending_review_count(_REPO) == 0
