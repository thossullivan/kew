"""Round 15 transaction checks for incomplete terminal continuations."""

from dataclasses import replace
from datetime import UTC, datetime

from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

from kew.run_store import RunRecord, SqliteRunStore


def _terminal(store, tmp_path):
    args = ("epic:x", "42", "original", "agent/issue-42", tmp_path / "checkout")
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    assert (
        store.claim_continuation_under_cap(
            *args,
            "owner",
            0,
            claim_evidence=fixture_claim_evidence(store, *args, "owner"),
        )
        == "claimed"
    )
    store.save(
        RunRecord(
            run_id="owner",
            issue_num=42,
            item_id="42",
            branch=args[3],
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    assert store.finalize_continuation(*args, 1, "owner")
    return args


def test_round15_terminal_claim_rechecks_success_and_abandonment(tmp_path):
    with SqliteRunStore(":memory:") as store:
        args = _terminal(store, tmp_path)
        assert (
            store.claim_continuation_under_cap(
                *args,
                "retry",
                0,
                claim_evidence=fixture_claim_evidence(store, *args, "retry"),
            )
            == "claimed"
        )
        assert store.finalize_continuation(*args, 2, "retry")
        store.save(replace(store.get("owner"), run_id="retry", status="success"))
        assert (
            store.claim_continuation_under_cap(
                *args,
                "blocked",
                0,
                claim_evidence=fixture_claim_evidence(store, *args, "blocked"),
            ),
            store.finalize_continuation(
                *args, 2, "retry", abandon_reason="stop", actor_id="operator"
            ),
            store.list_epic_reservations("epic:x"),
            store.list_audit_events(event="continuation_abandoned"),
        ) == ("terminal", False, [], [])


def test_round15_terminal_abandon_rechecks_generation_and_existing_decision(tmp_path):
    with SqliteRunStore(":memory:") as store:
        args = _terminal(store, tmp_path)
        assert not store.finalize_continuation(
            *args, 0, "owner", abandon_reason="stale", actor_id="operator"
        )
        assert store.finalize_continuation(
            *args, 1, "owner", abandon_reason="stop", actor_id="operator"
        )
        assert (
            store.finalize_continuation(
                *args, 1, "owner", abandon_reason="again", actor_id="operator"
            ),
            store.claim_continuation_under_cap(
                *args,
                "blocked",
                0,
                claim_evidence=fixture_claim_evidence(store, *args, "blocked"),
            ),
            len(store.list_audit_events(event="continuation_abandoned")),
            store.list_epic_reservations("epic:x"),
        ) == (False, "terminal", 1, [])
