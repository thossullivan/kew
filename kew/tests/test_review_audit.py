"""Task 13 (#981): the ``adversarial_review_verdict`` attested fact.

The per-round verdict is an additive hashed-payload fact (audit-chain forward
rule — no new entry_hash slots). These tests pin the payload contract, the
run_kind-filtered spend source, and chain-verify green across every verdict
shape, including the degenerate DEGRADED / MERGED_UNREVIEWED / PRE_POLICY.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kew.audit import Actor
from kew.audit_chain import verify_chain
from kew.config import ReviewConfig
from kew.review_audit import (
    REVIEW_VERDICT_EVENT,
    VERDICT_DEGRADED,
    VERDICT_MERGED_UNREVIEWED,
    VERDICT_PRE_POLICY,
    build_review_verdict_payload,
    emit_review_verdict,
    review_policy_block,
    review_spend_usd,
)
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)

ITEM = "42"
NOW = datetime(2026, 7, 23, tzinfo=UTC)

_REQUIRED_PAYLOAD_KEYS = {
    "pr_number",
    "item_id",
    "head_sha",
    "round",
    "verdict",
    "sidecar_digest",
    "reviewer_model",
    "prompt_artifact_sha",
    "acceptance_executed",
    "acceptance_reason",
    "review_mode",
    "policy",
    "spend_source",
    "spend_usd",
}


def _run(
    run_id: str, *, run_kind: str, cost: float, status: str = "success"
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=int(ITEM),
        item_id=ITEM,
        branch="epic/42",
        status=status,
        exit_code=0,
        started_at=NOW,
        finished_at=NOW,
        run_kind=run_kind,
        estimated_cost_usd=cost,
    )


def test_review_policy_block_binds_the_policy_in_force():
    cfg = ReviewConfig(
        adversarial=True,
        blocking=True,
        max_rounds=3,
        max_review_usd=7.5,
    )
    assert review_policy_block(cfg) == {
        "blocking": True,
        "max_rounds": 3,
        "max_review_usd": 7.5,
    }


def test_review_policy_block_no_longer_carries_effort():
    """A04: ``effort`` was attested but never reached the reviewer command, so
    it is gone from the policy block — and from every payload built from it.

    The guarantee it never actually provided (a reviewer tier bound into the
    verdict) is not relocated; the tier the reviewer really runs at is the
    runner's own default, and ``reviewer_model`` is what the payload attests.
    """
    payload = build_review_verdict_payload(
        pr_number=99,
        item_id=ITEM,
        head_sha="head1",
        review_round=1,
        verdict="READY",
        policy=review_policy_block(ReviewConfig(adversarial=True)),
        spend_usd=0.0,
    )
    assert "effort" not in payload["policy"]


def test_verdict_policy_block_carries_no_severity_floor():
    """#2017: ``min_severity_to_block`` was attested into every verdict but no
    code ever blocked on it, so the key is gone from the policy block and from
    every payload built from it. The enforced severity knob is the stop rule's
    ``stop_rule_max_severity``, which the verdict path reads directly."""
    payload = build_review_verdict_payload(
        pr_number=99,
        item_id=ITEM,
        head_sha="head1",
        review_round=1,
        verdict="NEEDS_CHANGES",
        policy=review_policy_block(ReviewConfig(adversarial=True)),
        spend_usd=0.0,
    )
    assert "min_severity_to_block" not in payload["policy"]
    assert set(payload["policy"]) == {"blocking", "max_rounds", "max_review_usd"}


def test_review_spend_sums_only_review_kind_runs(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.save(_run("impl-1", run_kind="dispatch", cost=4.00))
        store.save(_run("rev-1", run_kind=ADVERSARIAL_REVIEW_RUN_KIND, cost=1.25))
        store.save(_run("rem-1", run_kind=REVIEW_REMEDIATION_RUN_KIND, cost=0.75))
        # Implementation spend ($4.00) must NOT leak into the review pool figure.
        assert review_spend_usd(store, ITEM) == 2.00


def test_build_payload_is_complete_and_binds_run_records_spend():
    payload = build_review_verdict_payload(
        pr_number=99,
        item_id=ITEM,
        head_sha="head1",
        review_round=1,
        verdict="READY",
        policy=review_policy_block(ReviewConfig(adversarial=True)),
        sidecar_digest="sha256:abc",
        reviewer_model="claude-opus-4-8",
        prompt_artifact_sha="sha256:prompt",
        spend_usd=2.0,
        failure_layer="sandbox",
        error_category="sandbox_refused",
    )
    assert set(payload) >= _REQUIRED_PAYLOAD_KEYS
    assert payload["spend_source"] == "run_records"
    assert payload["spend_usd"] == 2.0
    assert payload["policy"]["blocking"] is True
    assert payload["failure_layer"] == "sandbox"
    assert payload["error_category"] == "sandbox_refused"
    # Acceptance status is always attested, even when the caller says nothing.
    assert payload["acceptance_executed"] is None
    assert payload["acceptance_reason"] is None
    # So is the round's scope — None on a verdict that reviewed nothing (#2111).
    assert payload["review_mode"] is None
    # Spend is keyed as run_records-sourced ``spend_usd``; no bare ``spend`` key
    # (the sidecar's self-reported cost is never accepted — F10).
    assert "spend" not in payload


def test_build_payload_attests_unexecuted_acceptance_reason():
    payload = build_review_verdict_payload(
        pr_number=99,
        item_id=ITEM,
        head_sha="head1",
        review_round=1,
        verdict="READY",
        policy=review_policy_block(ReviewConfig(adversarial=True)),
        spend_usd=2.0,
        acceptance_executed=False,
        acceptance_reason="gh and network-dependent checks are unavailable",
    )

    assert payload["acceptance_executed"] is False
    assert (
        payload["acceptance_reason"]
        == "gh and network-dependent checks are unavailable"
    )


def test_emit_appends_a_chained_verdict_event(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.save(_run("rev-1", run_kind=ADVERSARIAL_REVIEW_RUN_KIND, cost=1.5))
        emit_review_verdict(
            store,
            ReviewConfig(adversarial=True),
            pr_number=99,
            item_id=ITEM,
            head_sha="head1",
            review_round=1,
            verdict="NEEDS_CHANGES",
            sidecar_digest="sha256:abc",
            reviewer_model="claude-opus-4-8",
            prompt_artifact_sha="sha256:prompt",
            repo="o/r",
        )
        events = store.list_audit_events(item_id=ITEM)
        verdicts = [e for e in events if e.event == REVIEW_VERDICT_EVENT]
        assert len(verdicts) == 1
        p = verdicts[0].payload
        assert set(p) >= _REQUIRED_PAYLOAD_KEYS
        assert p["verdict"] == "NEEDS_CHANGES"
        # Spend drawn from RunRecords, not passed by the caller.
        assert p["spend_usd"] == 1.5
        assert verify_chain(store.read_chain_rows()).ok


@pytest.mark.parametrize(("mode", "verdict"), [("full", "READY"), ("delta", "READY")])
def test_emit_attests_the_round_scope(tmp_path, mode, verdict):
    """#2111: the chain has to tell a delta READY from a full one.

    The mode is persisted off-chain in ``review_rounds``; without it in the
    hashed payload the "a READY always has a full pass within the last two
    rounds" rule is unverifiable from the chain alone.
    """
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        emit_review_verdict(
            store,
            ReviewConfig(adversarial=True),
            pr_number=99,
            item_id=ITEM,
            head_sha="head1",
            review_round=2,
            verdict=verdict,
            sidecar_digest="sha256:abc",
            review_mode=mode,
            repo="o/r",
        )
        events = [
            e
            for e in store.list_audit_events(item_id=ITEM)
            if e.event == REVIEW_VERDICT_EVENT
        ]
        assert [e.payload["review_mode"] for e in events] == [mode]
        assert verify_chain(store.read_chain_rows()).ok


def test_all_verdict_shapes_chain_verify(tmp_path):
    """Chain stays green across READY, NEEDS_CHANGES, and the three degenerate
    verdicts — none breaks the hash chain (audit-chain forward rule)."""
    shapes = [
        "READY",
        "NEEDS_CHANGES",
        VERDICT_DEGRADED,
        VERDICT_MERGED_UNREVIEWED,
        VERDICT_PRE_POLICY,
    ]
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for i, verdict in enumerate(shapes, start=1):
            emit_review_verdict(
                store,
                ReviewConfig(adversarial=True),
                pr_number=99,
                item_id=ITEM,
                head_sha=f"head{i}",
                review_round=i,
                verdict=verdict,
                repo="o/r",
                actor=Actor("human", "out_of_band")
                if verdict == VERDICT_MERGED_UNREVIEWED
                else None,
            )
        events = sorted(
            (
                e
                for e in store.list_audit_events(item_id=ITEM)
                if e.event == REVIEW_VERDICT_EVENT
            ),
            key=lambda e: e.id,
        )
        assert [e.payload["verdict"] for e in events] == shapes
        result = verify_chain(store.read_chain_rows())
        assert result.ok
        assert result.count == len(shapes)
