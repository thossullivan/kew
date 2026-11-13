"""I10A domain: what a no-change judgment is made of, before anything stores it.

Every case here is a pure function over values. The store tests
(``test_run_store_no_change.py``) prove the same rules survive a crash.
"""

from __future__ import annotations

import pytest

from kew.no_change import (
    ALREADY_SATISFIED_CONTRACT_VERSION,
    ATTESTED,
    AUTHORIZED,
    CANDIDATE,
    CLOSE_IF_HOST_VERIFIED,
    COMPLETED,
    CONFIRM,
    DEFAULT_NO_CHANGE_POLICY,
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
    NoChangeEvidence,
    NoChangeIntent,
    NoChangeStateError,
    NoChangeValueError,
    decode_legacy_no_change,
    dependency_effect_id,
    outcome_sync_job_id,
    resolve_authorization,
    sync_idempotency_key,
    validate_dependency_effect_transition,
    validate_outcome_sync_transition,
    validate_transition,
)

BASE = "a" * 40
DIGEST = "sha256:" + "b" * 64
CHECK_DIGEST = "sha256:" + "c" * 64


def _checks() -> tuple[NamedCheck, ...]:
    return (NamedCheck(name="pytest", outcome="passed", digest=CHECK_DIGEST),)


def _evidence(**over: object) -> NoChangeEvidence:
    fields: dict[str, object] = {
        "trusted_base_sha": BASE,
        "observed_base_sha": BASE,
        "package_digest": DIGEST,
        "package_scope": "workspace",
        "package_entry_count": 0,
        "package_deletion_count": 0,
        "checks": _checks(),
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


class TestNoChangeEvidenceGaps:
    """Attestation is the absence of named gaps, never the absence of doubt."""

    def test_complete_host_evidence_attests(self):
        evidence = _evidence()
        assert evidence.gaps() == ()
        assert evidence.attested is True

    def test_missing_trusted_base_is_a_gap(self):
        gaps = _evidence(trusted_base_sha="").gaps()
        assert any("trusted base" in gap for gap in gaps)
        assert _evidence(trusted_base_sha="").attested is False

    def test_stale_base_is_a_gap(self):
        gaps = _evidence(observed_base_sha="d" * 40).gaps()
        assert any("moved" in gap for gap in gaps)

    def test_unobserved_base_is_a_gap(self):
        gaps = _evidence(observed_base_sha="").gaps()
        assert any("was not observed" in gap for gap in gaps)

    def test_missing_work_package_is_a_gap(self):
        gaps = _evidence(package_digest="").gaps()
        assert any("work package" in gap for gap in gaps)

    def test_work_package_that_does_not_cover_the_workspace_is_a_gap(self):
        gaps = _evidence(package_scope="commits").gaps()
        assert any("workspace" in gap for gap in gaps)

    def test_a_nonempty_work_package_is_a_gap(self):
        assert any(
            "not empty" in gap for gap in _evidence(package_entry_count=1).gaps()
        )
        assert any(
            "not empty" in gap for gap in _evidence(package_deletion_count=1).gaps()
        )

    def test_no_named_check_is_a_gap(self):
        gaps = _evidence(checks=()).gaps()
        assert any("named check" in gap for gap in gaps)

    def test_a_named_check_that_did_not_pass_is_a_gap(self):
        failing = (NamedCheck(name="pytest", outcome="failed", digest=CHECK_DIGEST),)
        gaps = _evidence(checks=failing).gaps()
        assert any("pytest" in gap and "failed" in gap for gap in gaps)

    def test_a_named_check_without_an_evidence_digest_is_a_gap(self):
        undigested = (NamedCheck(name="pytest", outcome="passed", digest="nope"),)
        gaps = _evidence(checks=undigested).gaps()
        assert any("digest" in gap for gap in gaps)

    @pytest.mark.parametrize(
        "disposition", ["filtered", "refused", "reverted", "dropped", "preserved"]
    )
    def test_suppressed_work_contradicts_a_no_change(self, disposition):
        attempted = (
            AttemptedChangeRecord(subject="src/kew/cli.py", disposition=disposition),
        )
        gaps = _evidence(attempted_changes=attempted).gaps()
        assert any(disposition in gap and "src/kew/cli.py" in gap for gap in gaps)

    def test_an_unavailable_attempted_change_outcome_is_a_gap(self):
        attempted = (
            AttemptedChangeRecord(subject="src/kew/cli.py", disposition="unavailable"),
        )
        assert _evidence(attempted_changes=attempted).attested is False

    def test_an_unknown_disposition_is_refused_rather_than_ignored(self):
        attempted = (AttemptedChangeRecord(subject="x", disposition="fine"),)
        with pytest.raises(NoChangeValueError):
            _evidence(attempted_changes=attempted).gaps()

    def test_evidence_digest_is_stable_and_round_trips_through_json(self):
        evidence = _evidence()
        assert evidence.digest == _evidence().digest
        assert NoChangeEvidence.from_json(evidence.to_json()) == evidence

    def test_evidence_digest_changes_with_the_evidence(self):
        assert _evidence().digest != _evidence(observed_base_sha="d" * 40).digest


class TestNoChangeIntent:
    """The keys a crash-replayed candidate has to converge on."""

    def test_judgment_id_is_derived_from_lineage_run_and_attempt(self):
        assert _intent().judgment_id == _intent().judgment_id
        assert _intent().judgment_id.startswith("nc-")
        assert _intent().judgment_id != _intent(attempt_number=2).judgment_id

    def test_lineage_key_is_the_item_not_the_run(self):
        assert _intent().lineage_key == "github:1531"
        assert _intent(run_id="run-2").lineage_key == _intent().lineage_key

    @pytest.mark.parametrize(
        "over",
        [
            {"source_kind": ""},
            {"source_id": ""},
            {"run_id": ""},
            {"attempt_number": 0},
            {"base_sha": "not-a-sha"},
        ],
    )
    def test_an_unbuildable_intent_is_refused(self, over):
        with pytest.raises(NoChangeValueError):
            _intent(**over).validate()


class TestLifecycle:
    """One monotonic projection; rejection is the only way out sideways."""

    def test_the_happy_path_advances_one_state_at_a_time(self):
        order = [
            CANDIDATE,
            ATTESTED,
            PENDING_CONFIRMATION,
            AUTHORIZED,
            SYNC_PENDING,
            EFFECT_PENDING,
            COMPLETED,
        ]
        for current, target in zip(order, order[1:], strict=False):
            validate_transition(current=current, target=target)

    def test_attested_may_skip_confirmation_when_policy_authorizes(self):
        validate_transition(current=ATTESTED, target=AUTHORIZED)

    def test_a_judgment_never_walks_backwards(self):
        with pytest.raises(NoChangeStateError):
            validate_transition(current=AUTHORIZED, target=ATTESTED)

    def test_rejection_is_reachable_from_any_live_state(self):
        for state in (CANDIDATE, ATTESTED, PENDING_CONFIRMATION, AUTHORIZED):
            validate_transition(current=state, target=REJECTED)

    @pytest.mark.parametrize("terminal", [COMPLETED, REJECTED])
    def test_a_settled_judgment_cannot_move(self, terminal):
        with pytest.raises(NoChangeStateError):
            validate_transition(current=terminal, target=AUTHORIZED)

    def test_an_unknown_state_is_refused(self):
        with pytest.raises(NoChangeStateError):
            validate_transition(current=CANDIDATE, target="closed")


class TestOutcomeSyncLifecycle:
    def test_intent_observation_ack_is_the_only_order(self):
        validate_outcome_sync_transition(current=SYNC_INTENT, target=SYNC_OBSERVED)
        validate_outcome_sync_transition(current=SYNC_OBSERVED, target=SYNC_ACKED)

    def test_a_sync_job_cannot_be_acked_without_an_observation(self):
        with pytest.raises(NoChangeStateError):
            validate_outcome_sync_transition(current=SYNC_INTENT, target=SYNC_ACKED)

    def test_an_acked_sync_job_is_settled(self):
        with pytest.raises(NoChangeStateError):
            validate_outcome_sync_transition(current=SYNC_ACKED, target=SYNC_OBSERVED)

    def test_sync_job_id_is_generation_bound(self):
        first = outcome_sync_job_id("nc-1", 1, "github", "owner/repo#1")
        assert first == outcome_sync_job_id("nc-1", 1, "github", "owner/repo#1")
        assert first != outcome_sync_job_id("nc-1", 2, "github", "owner/repo#1")

    def test_idempotency_key_is_stable_and_generation_bound(self):
        first = sync_idempotency_key("nc-1", 1, "github", "owner/repo#1")
        assert first.startswith("kew-no-change:")
        assert first == sync_idempotency_key("nc-1", 1, "github", "owner/repo#1")
        assert first != sync_idempotency_key("nc-1", 2, "github", "owner/repo#1")


class TestDependencyEffectLifecycle:
    def test_intent_applying_ack_is_the_only_order(self):
        validate_dependency_effect_transition(
            current=EFFECT_INTENT, target=EFFECT_APPLYING
        )
        validate_dependency_effect_transition(
            current=EFFECT_APPLYING, target=EFFECT_ACKED
        )

    def test_an_effect_is_never_acked_without_being_applied(self):
        with pytest.raises(NoChangeStateError):
            validate_dependency_effect_transition(
                current=EFFECT_INTENT, target=EFFECT_ACKED
            )

    def test_effect_id_is_per_dependent_and_generation_bound(self):
        first = dependency_effect_id("nc-1", 1, "gh:1532")
        assert first == dependency_effect_id("nc-1", 1, "gh:1532")
        assert first != dependency_effect_id("nc-1", 1, "gh:1533")
        assert first != dependency_effect_id("nc-1", 2, "gh:1532")


class TestAlreadySatisfiedContract:
    def test_a_trusted_versioned_host_contract_validates(self):
        _contract().validate(item_id="gh:1531")

    def test_a_contract_for_another_item_is_refused(self):
        with pytest.raises(NoChangeValueError):
            _contract().validate(item_id="gh:9999")

    @pytest.mark.parametrize(
        "over",
        [
            {"contract_version": ALREADY_SATISFIED_CONTRACT_VERSION + 1},
            {"contract_version": 0},
            {"satisfied": False},
            {"producer_kind": "runner"},
            {"verifier": ""},
            {"digest": "not-a-digest"},
        ],
    )
    def test_an_untrusted_contract_is_refused(self, over):
        with pytest.raises(NoChangeValueError):
            _contract(**over).validate(item_id="gh:1531")


class TestResolveAuthorization:
    def test_the_default_policy_is_confirm(self):
        assert DEFAULT_NO_CHANGE_POLICY == CONFIRM

    def test_confirm_never_authorizes_on_its_own(self):
        decision = resolve_authorization(
            policy=CONFIRM, evidence=_evidence(), item_id="gh:1531"
        )
        assert decision.state == PENDING_CONFIRMATION
        assert decision.human_confirmable is True

    def test_never_accept_is_never_satisfied_and_blocks_confirmation(self):
        decision = resolve_authorization(
            policy=NEVER_ACCEPT,
            evidence=_evidence(),
            item_id="gh:1531",
            contract=_contract(),
        )
        assert decision.state == PENDING_CONFIRMATION
        assert decision.human_confirmable is False
        assert "never_accept" in decision.reason

    def test_close_if_host_verified_authorizes_only_with_the_contract(self):
        decision = resolve_authorization(
            policy=CLOSE_IF_HOST_VERIFIED,
            evidence=_evidence(),
            item_id="gh:1531",
            contract=_contract(),
        )
        assert decision.state == AUTHORIZED

    def test_close_if_host_verified_without_a_contract_waits_for_a_person(self):
        decision = resolve_authorization(
            policy=CLOSE_IF_HOST_VERIFIED, evidence=_evidence(), item_id="gh:1531"
        )
        assert decision.state == PENDING_CONFIRMATION
        assert decision.human_confirmable is True

    def test_close_if_host_verified_refuses_an_untrusted_contract(self):
        decision = resolve_authorization(
            policy=CLOSE_IF_HOST_VERIFIED,
            evidence=_evidence(),
            item_id="gh:1531",
            contract=_contract(producer_kind="runner"),
        )
        assert decision.state == PENDING_CONFIRMATION
        assert "not trusted" in decision.reason

    def test_gapped_evidence_authorizes_nothing_and_confirms_nothing(self):
        decision = resolve_authorization(
            policy=CLOSE_IF_HOST_VERIFIED,
            evidence=_evidence(checks=()),
            item_id="gh:1531",
            contract=_contract(),
        )
        assert decision.state == PENDING_CONFIRMATION
        assert decision.human_confirmable is False
        assert "named check" in decision.reason

    def test_an_unknown_policy_is_refused(self):
        with pytest.raises(NoChangeValueError):
            resolve_authorization(
                policy="close_always", evidence=_evidence(), item_id="gh:1531"
            )


class TestLegacyRows:
    """A legacy row is unknown. Manufacturing a candidate would invent evidence."""

    def test_a_legacy_success_without_a_pr_reads_as_unknown(self):
        view = decode_legacy_no_change(
            status="success", run_id="old", attempt_number=1, pr_url=None
        )
        assert view is not None
        assert view.state == LEGACY_UNKNOWN
        assert view.persisted is False

    def test_a_legacy_success_with_a_pr_is_not_a_no_change_at_all(self):
        assert (
            decode_legacy_no_change(
                status="success",
                run_id="old",
                attempt_number=1,
                pr_url="https://example.invalid/pr/1",
            )
            is None
        )

    @pytest.mark.parametrize("status", ["failed", "running", "awaiting_publication"])
    def test_other_legacy_statuses_are_not_no_change_rows(self, status):
        assert (
            decode_legacy_no_change(
                status=status, run_id="old", attempt_number=1, pr_url=None
            )
            is None
        )


class TestEvidenceParity:
    """This module restates two things :mod:`kew.evidence` owns. Pin both.

    It restates them because the evidence writer reaches the audit ledger,
    which reaches the config, which reaches the runners — and this module is
    composed into the store, which sits below all three in the layered
    dispatch stack (``lint-imports``). Restating is the only way to keep one
    evidence vocabulary without inverting that stack, and these tests are what
    make "restated" different from "forked".
    """

    def test_disposition_vocabulary_matches_the_ledger(self):
        from kew.evidence import AttemptedChange
        from kew.no_change import ATTEMPTED_CHANGE_DISPOSITIONS

        assert set(ATTEMPTED_CHANGE_DISPOSITIONS) == {
            str(member) for member in AttemptedChange
        }

    def test_producer_version_matches_the_package(self):
        import kew
        from kew.no_change import producer_version

        assert producer_version() == kew.__version__

    def test_receipt_fact_payload_matches_kew_evidence(self):
        from kew.audit import Actor
        from kew.evidence import (
            EVIDENCE_SCHEMA_VERSION as LEDGER_SCHEMA,
        )
        from kew.evidence import (
            EvidenceKind,
            Producer,
            ProducerKind,
            record_evidence,
        )
        from kew.no_change import (
            EVIDENCE_ACTOR_ID,
            EVIDENCE_ACTOR_TYPE,
            EVIDENCE_SCHEMA_VERSION,
            JUDGMENT_STATE_FACT,
            RECEIPT_FACT_EVENT,
            build_receipt_fact_payload,
            producer_version,
        )

        assert EVIDENCE_SCHEMA_VERSION == LEDGER_SCHEMA
        assert EvidenceKind.RECEIPT_FACT.value == RECEIPT_FACT_EVENT

        captured: dict[str, object] = {}

        class _Recorder:
            def append_audit_event(self, **kwargs: object) -> int:
                captured.update(kwargs)
                return 1

        mine = build_receipt_fact_payload(
            fact=JUDGMENT_STATE_FACT,
            subject="nc-1",
            target="attested",
            detail="host evidence attests",
            attempt_number=1,
            producer_version=producer_version(),
        )
        record_evidence(
            _Recorder(),
            EvidenceKind.RECEIPT_FACT,
            actor=Actor(EVIDENCE_ACTOR_TYPE, EVIDENCE_ACTOR_ID),
            producer=Producer(
                kind=ProducerKind.HOST,
                name="kew.no_change",
                version=producer_version(),
            ),
            facts=mine["facts"],
            run_id="run-1",
        )
        assert captured["payload"] == mine
        assert captured["actor_type"] == EVIDENCE_ACTOR_TYPE
        assert captured["actor_id"] == EVIDENCE_ACTOR_ID
