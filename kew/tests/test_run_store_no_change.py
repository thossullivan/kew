"""I10A durability: the no-change judgment, sync jobs and effects survive a crash.

Every test here reopens the store or re-reads the row, because the property
under test is never "the method returned the right thing" — it is "a host that
died halfway through cannot repeat an effect or expose the next one early".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kew.no_change import (
    ALREADY_SATISFIED_CONTRACT_VERSION,
    ATTESTED,
    AUTHORIZED,
    CANDIDATE,
    CLOSE_IF_HOST_VERIFIED,
    COMPLETED,
    CONFIRM,
    EFFECT_ACKED,
    EFFECT_APPLYING,
    EFFECT_INTENT,
    EFFECT_PENDING,
    LEGACY_UNKNOWN,
    NEVER_ACCEPT,
    PENDING_CONFIRMATION,
    REJECTED,
    SYNC_ACKED,
    SYNC_INTENT,
    SYNC_OBSERVED,
    SYNC_PENDING,
    AlreadySatisfiedContract,
    AttemptedChangeRecord,
    NamedCheck,
    NoChangeConflictError,
    NoChangeEvidence,
    NoChangeIntent,
    NoChangeStateError,
    NoChangeValueError,
)
from kew.run_store import RunRecord, SqliteRunStore

BASE = "a" * 40
DIGEST = "sha256:" + "b" * 64
CHECK_DIGEST = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 22, 13, 0, tzinfo=UTC)

GITHUB = ("github", "owner/repo#1531")
LINEAR = ("linear", "KEW-1531")


def _evidence(**over: object) -> NoChangeEvidence:
    fields: dict[str, object] = {
        "trusted_base_sha": BASE,
        "observed_base_sha": BASE,
        "package_digest": DIGEST,
        "package_scope": "workspace",
        "package_entry_count": 0,
        "package_deletion_count": 0,
        "checks": (NamedCheck(name="pytest", outcome="passed", digest=CHECK_DIGEST),),
        "attempted_changes": (),
    }
    fields.update(over)
    return NoChangeEvidence(**fields)  # type: ignore[arg-type]


def _intent(**over: object) -> NoChangeIntent:
    fields: dict[str, object] = {
        "source_kind": "github",
        "source_id": "1531",
        "run_id": "run-1",
        "attempt_number": 1,
        "base_sha": BASE,
        "item_id": "gh:1531",
    }
    fields.update(over)
    return NoChangeIntent(**fields)  # type: ignore[arg-type]


def _contract(**over: object) -> AlreadySatisfiedContract:
    fields: dict[str, object] = {
        "contract_version": ALREADY_SATISFIED_CONTRACT_VERSION,
        "item_id": "gh:1531",
        "satisfied": True,
        "producer_kind": "host",
        "verifier": "kew.host_verifier",
        "digest": DIGEST,
    }
    fields.update(over)
    return AlreadySatisfiedContract(**fields)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _authorized(store, *, policy=CLOSE_IF_HOST_VERIFIED, intent=None):
    """A judgment carried all the way to ``authorized`` by host verification."""
    judgment = store.open_no_change_judgment(
        intent=intent or _intent(), policy=policy, now=NOW
    )
    store.attest_no_change(
        judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
    )
    return store.decide_no_change(
        judgment_id=judgment.judgment_id, contract=_contract(), now=NOW
    )


class TestOpenJudgment:
    def test_a_zero_diff_exit_is_persisted_as_a_candidate(self, store, tmp_path):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        assert judgment.state == CANDIDATE
        assert judgment.generation == 1
        assert judgment.policy == CONFIRM
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as reopened:
            survivor = reopened.get_no_change_judgment(judgment.judgment_id)
        assert survivor is not None
        assert survivor.state == CANDIDATE
        assert survivor.item_id == "gh:1531"

    def test_a_retried_creation_converges_on_one_row(self, store):
        first = store.open_no_change_judgment(intent=_intent(), now=NOW)
        again = store.open_no_change_judgment(intent=_intent(), now=LATER)
        assert again.judgment_id == first.judgment_id
        assert again.created_at == first.created_at
        assert len(store.list_no_change_judgments()) == 1

    def test_a_second_live_judgment_for_one_item_is_refused(self, store):
        store.open_no_change_judgment(intent=_intent(), now=NOW)
        with pytest.raises(NoChangeConflictError):
            store.open_no_change_judgment(
                intent=_intent(run_id="run-2", attempt_number=2), now=LATER
            )

    def test_a_retry_that_names_a_different_base_is_a_conflict(self, store):
        store.open_no_change_judgment(intent=_intent(), now=NOW)
        with pytest.raises(NoChangeConflictError):
            store.open_no_change_judgment(intent=_intent(base_sha="d" * 40), now=LATER)

    def test_an_unbuildable_intent_writes_nothing(self, store):
        with pytest.raises(NoChangeValueError):
            store.open_no_change_judgment(intent=_intent(base_sha="nope"), now=NOW)
        assert store.list_no_change_judgments() == ()

    def test_an_unknown_policy_writes_nothing(self, store):
        with pytest.raises(NoChangeValueError):
            store.open_no_change_judgment(
                intent=_intent(), policy="close_always", now=NOW
            )
        assert store.list_no_change_judgments() == ()

    def test_the_generation_advances_after_the_previous_one_settles(self, store):
        first = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.reject_no_change(
            judgment_id=first.judgment_id,
            actor_id="maintainer",
            reason="there was work to do",
            now=NOW,
        )
        second = store.open_no_change_judgment(
            intent=_intent(run_id="run-2", attempt_number=2), now=LATER
        )
        assert second.generation == 2
        assert second.lineage_key == first.lineage_key

    def test_a_judgment_is_findable_by_run_and_attempt(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        found = store.get_no_change_judgment_for_run(run_id="run-1", attempt_number=1)
        assert found is not None and found.judgment_id == judgment.judgment_id
        assert (
            store.get_no_change_judgment_for_run(run_id="run-1", attempt_number=2)
            is None
        )


class TestAttestation:
    def test_complete_host_evidence_attests_and_is_round_tripped(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        attested = store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        assert attested.state == ATTESTED
        assert attested.evidence == _evidence()
        assert attested.evidence_digest == _evidence().digest

    def test_every_transition_records_strict_host_evidence(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        attested = store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        assert attested.audit_event_id is not None
        events = store.list_audit_events(event="evidence.receipt_fact")
        assert any(
            e.payload is not None
            and e.payload["facts"]["value"] == ATTESTED
            and e.payload["facts"]["source"] == judgment.judgment_id
            and e.payload["attested_by_host"] is True
            for e in events
        )

    @pytest.mark.parametrize(
        "gapped",
        [
            {"observed_base_sha": "d" * 40},
            {"package_entry_count": 1},
            {"package_scope": "commits"},
            {"checks": ()},
            {
                "attempted_changes": (
                    AttemptedChangeRecord(subject="a.py", disposition="filtered"),
                )
            },
        ],
    )
    def test_gapped_evidence_cannot_be_attested(self, store, gapped):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        with pytest.raises(NoChangeValueError):
            store.attest_no_change(
                judgment_id=judgment.judgment_id, evidence=_evidence(**gapped), now=NOW
            )
        still = store.get_no_change_judgment(judgment.judgment_id)
        assert still is not None and still.state == CANDIDATE
        assert still.evidence_json is None

    def test_attesting_twice_is_refused_rather_than_rewritten(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        with pytest.raises(NoChangeStateError):
            store.attest_no_change(
                judgment_id=judgment.judgment_id, evidence=_evidence(), now=LATER
            )

    def test_an_unknown_judgment_is_refused(self, store):
        with pytest.raises(NoChangeValueError):
            store.attest_no_change(judgment_id="nc-nope", evidence=_evidence(), now=NOW)


class TestDecisionAndPolicy:
    def test_the_default_policy_holds_the_outcome_for_a_person(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        decided = store.decide_no_change(judgment_id=judgment.judgment_id, now=NOW)
        assert decided.state == PENDING_CONFIRMATION
        assert "confirm" in decided.decision_reason

    def test_close_if_host_verified_authorizes_against_the_contract(self, store):
        decided = _authorized(store)
        assert decided.state == AUTHORIZED
        assert decided.contract_digest == DIGEST

    def test_close_if_host_verified_without_a_contract_waits(self, store):
        judgment = store.open_no_change_judgment(
            intent=_intent(), policy=CLOSE_IF_HOST_VERIFIED, now=NOW
        )
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        decided = store.decide_no_change(judgment_id=judgment.judgment_id, now=NOW)
        assert decided.state == PENDING_CONFIRMATION
        assert decided.contract_digest is None

    def test_close_if_host_verified_refuses_another_items_contract(self, store):
        judgment = store.open_no_change_judgment(
            intent=_intent(), policy=CLOSE_IF_HOST_VERIFIED, now=NOW
        )
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        decided = store.decide_no_change(
            judgment_id=judgment.judgment_id,
            contract=_contract(item_id="gh:9999"),
            now=NOW,
        )
        assert decided.state == PENDING_CONFIRMATION
        assert "not trusted" in decided.decision_reason

    def test_a_person_confirms_a_pending_judgment(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        store.decide_no_change(judgment_id=judgment.judgment_id, now=NOW)
        confirmed = store.confirm_no_change(
            judgment_id=judgment.judgment_id, actor_id="maintainer", now=LATER
        )
        assert confirmed.state == AUTHORIZED
        assert confirmed.decided_by == "maintainer"
        assert confirmed.decided_at == LATER.isoformat()

    def test_never_accept_is_unsatisfied_by_evidence_and_by_a_person(self, store):
        judgment = store.open_no_change_judgment(
            intent=_intent(), policy=NEVER_ACCEPT, now=NOW
        )
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        decided = store.decide_no_change(
            judgment_id=judgment.judgment_id, contract=_contract(), now=NOW
        )
        assert decided.state == PENDING_CONFIRMATION
        with pytest.raises(NoChangeStateError):
            store.confirm_no_change(
                judgment_id=judgment.judgment_id, actor_id="maintainer", now=LATER
            )
        still = store.get_no_change_judgment(judgment.judgment_id)
        assert still is not None and still.state == PENDING_CONFIRMATION

    def test_a_candidate_cannot_be_decided_before_it_is_attested(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        with pytest.raises(NoChangeStateError):
            store.decide_no_change(judgment_id=judgment.judgment_id, now=NOW)

    def test_rejection_settles_the_judgment_with_its_reason(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        rejected = store.reject_no_change(
            judgment_id=judgment.judgment_id,
            actor_id="maintainer",
            reason="the linter was never run",
            now=LATER,
        )
        assert rejected.state == REJECTED
        assert "linter" in rejected.decision_reason
        with pytest.raises(NoChangeStateError):
            store.reject_no_change(
                judgment_id=judgment.judgment_id,
                actor_id="maintainer",
                reason="again",
                now=LATER,
            )


class TestOutcomeSyncJobs:
    def test_authorization_enqueues_one_generation_bound_job_per_target(self, store):
        judgment = _authorized(store)
        jobs = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB, LINEAR], now=NOW
        )
        assert [j.target_kind for j in jobs] == ["github", "linear"]
        assert {j.generation for j in jobs} == {judgment.generation}
        assert all(j.state == SYNC_INTENT for j in jobs)
        assert len({j.idempotency_key for j in jobs}) == 2
        moved = store.get_no_change_judgment(judgment.judgment_id)
        assert moved is not None and moved.state == SYNC_PENDING

    def test_enqueue_is_idempotent_for_the_same_targets(self, store):
        judgment = _authorized(store)
        first = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        again = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=LATER
        )
        assert [j.sync_job_id for j in again] == [j.sync_job_id for j in first]
        assert len(store.list_outcome_sync_jobs(judgment.judgment_id)) == 1

    def test_a_different_target_set_is_a_conflict_not_a_second_intent(self, store):
        judgment = _authorized(store)
        store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        with pytest.raises(NoChangeConflictError):
            store.enqueue_outcome_sync(
                judgment_id=judgment.judgment_id, targets=[LINEAR], now=LATER
            )

    def test_an_unauthorized_judgment_enqueues_nothing(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        with pytest.raises(NoChangeStateError):
            store.enqueue_outcome_sync(
                judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
            )
        assert store.list_outcome_sync_jobs(judgment.judgment_id) == ()

    def test_a_job_is_acked_only_after_the_remote_was_observed(self, store):
        judgment = _authorized(store)
        (job,) = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        with pytest.raises(NoChangeStateError):
            store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=NOW)
        observed = store.observe_outcome_sync(
            sync_job_id=job.sync_job_id,
            observation_kind="closed",
            observation_ref="https://example.invalid/issues/1531",
            now=NOW,
        )
        assert observed.state == SYNC_OBSERVED
        acked = store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=LATER)
        assert acked.state == SYNC_ACKED
        assert acked.acked_at == LATER.isoformat()

    def test_the_last_ack_completes_a_judgment_with_no_dependents(self, store):
        judgment = _authorized(store)
        jobs = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB, LINEAR], now=NOW
        )
        for job in jobs:
            store.observe_outcome_sync(
                sync_job_id=job.sync_job_id, observation_kind="closed", now=NOW
            )
        store.ack_outcome_sync(sync_job_id=jobs[0].sync_job_id, now=NOW)
        halfway = store.get_no_change_judgment(judgment.judgment_id)
        assert halfway is not None and halfway.state == SYNC_PENDING
        store.ack_outcome_sync(sync_job_id=jobs[1].sync_job_id, now=LATER)
        done = store.get_no_change_judgment(judgment.judgment_id)
        assert done is not None and done.state == COMPLETED

    def test_acking_twice_is_refused_so_a_replay_writes_once(self, store):
        judgment = _authorized(store)
        (job,) = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        store.observe_outcome_sync(
            sync_job_id=job.sync_job_id, observation_kind="closed", now=NOW
        )
        store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=NOW)
        with pytest.raises(NoChangeStateError):
            store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=LATER)

    def test_a_crash_between_the_ack_and_the_advance_is_resumable(
        self, store, monkeypatch
    ):
        # The ack settles the sync job in one transaction and advances the
        # judgment in a second. A host that dies between them leaves the job
        # `acked` and the judgment stranded in `sync_pending`. Re-driving the
        # same ack must resume the advance, not raise on an acked->acked move.
        judgment = _authorized(store)
        (job,) = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        store.observe_outcome_sync(
            sync_job_id=job.sync_job_id, observation_kind="closed", now=NOW
        )
        monkeypatch.setattr(store, "_advance_after_sync", lambda *a, **k: None)
        store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=NOW)
        stranded = store.get_no_change_judgment(judgment.judgment_id)
        assert stranded is not None and stranded.state == SYNC_PENDING
        monkeypatch.undo()

        resumed = store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=LATER)
        assert resumed.state == SYNC_ACKED
        assert resumed.acked_at == NOW.isoformat()  # the settle is not rewritten
        done = store.get_no_change_judgment(judgment.judgment_id)
        assert done is not None and done.state == COMPLETED

    def test_a_superseded_generations_job_cannot_be_advanced(self, store):
        judgment = _authorized(store)
        (job,) = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        store.reject_no_change(
            judgment_id=judgment.judgment_id,
            actor_id="maintainer",
            reason="a person disagreed",
            now=NOW,
        )
        store.open_no_change_judgment(
            intent=_intent(run_id="run-2", attempt_number=2), now=LATER
        )
        with pytest.raises(NoChangeStateError):
            store.observe_outcome_sync(
                sync_job_id=job.sync_job_id, observation_kind="closed", now=LATER
            )


class TestDependencyEffects:
    def _with_two_dependents(self, store):
        judgment = _authorized(store)
        store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id,
            dependents=["gh:1532", "gh:1533"],
            now=NOW,
        )
        (job,) = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=NOW
        )
        store.observe_outcome_sync(
            sync_job_id=job.sync_job_id, observation_kind="closed", now=NOW
        )
        store.ack_outcome_sync(sync_job_id=job.sync_job_id, now=NOW)
        return judgment

    def test_effects_are_persisted_in_a_stable_order_before_any_effect(self, store):
        judgment = _authorized(store)
        effects = store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id,
            dependents=["gh:1532", "gh:1533"],
            now=NOW,
        )
        assert [e.dependent_id for e in effects] == ["gh:1532", "gh:1533"]
        assert [e.ordinal for e in effects] == [0, 1]
        assert all(e.state == EFFECT_INTENT for e in effects)
        assert all(e.generation == judgment.generation for e in effects)

    def test_enqueue_is_idempotent_and_a_different_set_conflicts(self, store):
        judgment = _authorized(store)
        first = store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id, dependents=["gh:1532"], now=NOW
        )
        again = store.enqueue_dependency_effects(
            judgment_id=judgment.judgment_id, dependents=["gh:1532"], now=LATER
        )
        assert [e.effect_id for e in again] == [e.effect_id for e in first]
        with pytest.raises(NoChangeConflictError):
            store.enqueue_dependency_effects(
                judgment_id=judgment.judgment_id,
                dependents=["gh:1532", "gh:1533"],
                now=LATER,
            )

    def test_the_second_dependent_is_not_exposed_until_the_first_is_acked(self, store):
        judgment = self._with_two_dependents(store)
        first = store.next_dependency_effect(judgment.judgment_id)
        assert first is not None and first.dependent_id == "gh:1532"
        store.begin_dependency_effect(effect_id=first.effect_id, now=NOW)
        assert (
            store.next_dependency_effect(judgment.judgment_id).dependent_id == "gh:1532"
        )
        store.ack_dependency_effect(effect_id=first.effect_id, now=NOW)
        second = store.next_dependency_effect(judgment.judgment_id)
        assert second is not None and second.dependent_id == "gh:1533"

    def test_a_crash_after_the_first_dependent_cannot_repeat_it(self, store, tmp_path):
        judgment = self._with_two_dependents(store)
        first = store.next_dependency_effect(judgment.judgment_id)
        store.begin_dependency_effect(effect_id=first.effect_id, now=NOW)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as restarted:
            resumed = restarted.next_dependency_effect(judgment.judgment_id)
            assert resumed.effect_id == first.effect_id
            assert resumed.state == EFFECT_APPLYING
            with pytest.raises(NoChangeStateError):
                restarted.begin_dependency_effect(effect_id=first.effect_id, now=LATER)

    def test_the_last_ack_completes_the_judgment(self, store):
        judgment = self._with_two_dependents(store)
        assert (
            store.get_no_change_judgment(judgment.judgment_id).state == EFFECT_PENDING
        )
        for _ in range(2):
            effect = store.next_dependency_effect(judgment.judgment_id)
            store.begin_dependency_effect(effect_id=effect.effect_id, now=NOW)
            store.ack_dependency_effect(effect_id=effect.effect_id, now=LATER)
        assert store.next_dependency_effect(judgment.judgment_id) is None
        done = store.get_no_change_judgment(judgment.judgment_id)
        assert done is not None and done.state == COMPLETED
        assert [
            e.state for e in store.list_dependency_effects(judgment.judgment_id)
        ] == [
            EFFECT_ACKED,
            EFFECT_ACKED,
        ]

    def test_a_crash_between_the_effect_ack_and_completion_is_resumable(
        self, store, monkeypatch
    ):
        # Same asymmetry as the sync ack: the effect ack and the judgment
        # completion are separate transactions. A crash between them strands
        # the judgment in `effect_pending` with the effect already `acked`.
        # Re-driving the ack must resume the completion, not raise.
        judgment = self._with_two_dependents(store)
        first = store.next_dependency_effect(judgment.judgment_id)
        store.begin_dependency_effect(effect_id=first.effect_id, now=NOW)
        store.ack_dependency_effect(effect_id=first.effect_id, now=NOW)
        second = store.next_dependency_effect(judgment.judgment_id)
        store.begin_dependency_effect(effect_id=second.effect_id, now=NOW)
        monkeypatch.setattr(store, "_complete_after_effects", lambda *a, **k: None)
        store.ack_dependency_effect(effect_id=second.effect_id, now=NOW)
        stranded = store.get_no_change_judgment(judgment.judgment_id)
        assert stranded is not None and stranded.state == EFFECT_PENDING
        monkeypatch.undo()

        resumed = store.ack_dependency_effect(effect_id=second.effect_id, now=LATER)
        assert resumed.state == EFFECT_ACKED
        assert resumed.acked_at == NOW.isoformat()  # the ack is not rewritten
        done = store.get_no_change_judgment(judgment.judgment_id)
        assert done is not None and done.state == COMPLETED

    def test_an_effect_is_never_acked_without_being_applied(self, store):
        judgment = self._with_two_dependents(store)
        first = store.next_dependency_effect(judgment.judgment_id)
        with pytest.raises(NoChangeStateError):
            store.ack_dependency_effect(effect_id=first.effect_id, now=NOW)

    def test_an_unauthorized_judgment_records_no_effect_intent(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        with pytest.raises(NoChangeStateError):
            store.enqueue_dependency_effects(
                judgment_id=judgment.judgment_id, dependents=["gh:1532"], now=NOW
            )
        assert store.list_dependency_effects(judgment.judgment_id) == ()


class TestLegacyRows:
    def test_a_legacy_success_without_a_pr_stays_unknown_and_unwritten(self, store):
        store.save(
            RunRecord(
                run_id="old-run",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=NOW,
            )
        )
        views = store.legacy_no_change_views()
        assert [v.state for v in views] == [LEGACY_UNKNOWN]
        assert views[0].persisted is False
        assert store.list_no_change_judgments() == ()

    def test_a_legacy_success_with_a_pr_is_not_a_no_change_row(self, store):
        store.save(
            RunRecord(
                run_id="published",
                issue_num=8,
                branch="agent/issue-8",
                status="success",
                exit_code=0,
                started_at=NOW,
                pr_url="https://example.invalid/pr/8",
            )
        )
        assert store.legacy_no_change_views() == ()


class TestRefusals:
    """The edges a caller reaches by being wrong, stale, or racing."""

    def test_the_live_judgment_for_an_item_is_readable_and_clears_on_settle(
        self, store
    ):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        live = store.live_no_change_judgment("github:1531")
        assert live is not None and live.judgment_id == judgment.judgment_id
        store.reject_no_change(
            judgment_id=judgment.judgment_id,
            actor_id="maintainer",
            reason="no",
            now=NOW,
        )
        assert store.live_no_change_judgment("github:1531") is None

    def test_an_outcome_sync_with_no_target_is_refused(self, store):
        judgment = _authorized(store)
        with pytest.raises(NoChangeValueError):
            store.enqueue_outcome_sync(
                judgment_id=judgment.judgment_id, targets=[], now=NOW
            )

    def test_unknown_ids_are_refused_rather_than_silently_ignored(self, store):
        with pytest.raises(NoChangeValueError):
            store.observe_outcome_sync(
                sync_job_id="ncs-nope", observation_kind="closed", now=NOW
            )
        with pytest.raises(NoChangeValueError):
            store.begin_dependency_effect(effect_id="nce-nope", now=NOW)
        with pytest.raises(NoChangeValueError):
            store.confirm_no_change(
                judgment_id="nc-nope", actor_id="maintainer", now=NOW
            )

    def test_a_stale_belief_about_the_state_loses_the_race(self, store):
        """Two hosts, one judgment: the second move is refused, not applied."""
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        # `judgment` is the caller's stale CANDIDATE view; the row has moved on.
        with pytest.raises(NoChangeStateError):
            store._move_judgment(
                judgment, target=REJECTED, reason="racing writer", now=LATER
            )
        current = store.get_no_change_judgment(judgment.judgment_id)
        assert current is not None and current.state == ATTESTED


class TestCrashRecovery:
    """What a host that died between two writes comes back to."""

    def test_who_confirmed_survives_the_transitions_that_follow(self, store):
        judgment = store.open_no_change_judgment(intent=_intent(), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id, evidence=_evidence(), now=NOW
        )
        store.decide_no_change(judgment_id=judgment.judgment_id, now=NOW)
        store.confirm_no_change(
            judgment_id=judgment.judgment_id, actor_id="maintainer", now=LATER
        )
        store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=LATER
        )
        moved = store.get_no_change_judgment(judgment.judgment_id)
        assert moved is not None and moved.state == SYNC_PENDING
        assert moved.decided_by == "maintainer"
        assert moved.decided_at == LATER.isoformat()

    def test_a_crash_between_the_intents_and_the_state_is_resumable(self, store):
        """Intents written, judgment not yet moved: the retry finishes the move."""
        judgment = _authorized(store)
        store._insert_sync_jobs(judgment, [GITHUB], NOW.isoformat())
        assert store.get_no_change_judgment(judgment.judgment_id).state == AUTHORIZED
        jobs = store.enqueue_outcome_sync(
            judgment_id=judgment.judgment_id, targets=[GITHUB], now=LATER
        )
        assert len(jobs) == 1
        resumed = store.get_no_change_judgment(judgment.judgment_id)
        assert resumed is not None and resumed.state == SYNC_PENDING
