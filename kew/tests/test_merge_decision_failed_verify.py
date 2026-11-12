"""A merge decision over a failed merge verify leads with the failure (#3460).

The needs-you set used to read "PR #2059 is open — the merge decision is
yours" directly above a red MERGE VERIFY section: it asked a person to merge a
PR kew had just seen fail. The decision stays theirs, so the row stays a
needs-you, but its sentence now leads with the failure and its count, and its
first move is the item's MERGE VERIFY section, the PR link second.

A verify that passed, none recorded, or one that failed at a head the PR has
since moved past keeps today's sentence: that is not this head's failure.

The epic, its holds and its runs come from the dashboard suite's fakes; the
verify, the review rounds and the head events are read off a REAL
``SqliteRunStore``, so the join by head is the store's own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from support.dashboard_fakes import FakeDriverState, FakeEpicTick, FakeStore

from kew.audit import Actor, emit
from kew.config import DispatchConfig, ProjectConfig
from kew.merge_verify import VERIFY_EVENT
from kew.run_store import SqliteRunStore
from kew.service import needs_you
from kew.service.attention import compose_node_attention
from kew.service.merge_verify_reading import HEAD_EVENTS

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
EPIC = "launch-fixes"
ITEM = "2055"
PR_URL = "https://github.com/o/r/pull/2059"
HEAD = "3670e43f9a1b2c3d4e5f60718293a4b5c6d7e8f9"
OLDER_HEAD = "9c8b7a6f5e4d3c2b1a0987654321fedcba098765"
BASE = "b71c04e2aaaabbbbccccddddeeeeffff00001111"
MERGE_SENTENCE = "PR #2059 is open — the merge decision is yours"
FAILURE = "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR"


@pytest.fixture
def ledger() -> Iterator[SqliteRunStore]:
    store = SqliteRunStore(":memory:")
    try:
        yield store
    finally:
        store.close()


class _VerifyLedger(FakeStore):
    """The suite's fake for the epic, with the verify's reads off a real ledger:
    the ``verify_completed`` rows, the head events and the review rounds."""

    _LEDGER_EVENTS = frozenset({VERIFY_EVENT, *HEAD_EVENTS})

    def __init__(self, ledger: SqliteRunStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ledger = ledger

    def list_audit_events(self, **kwargs: Any) -> Any:
        if kwargs.get("event") in self._LEDGER_EVENTS:
            return self._ledger.list_audit_events(**kwargs)
        return super().list_audit_events(**kwargs)

    def review_rounds_in_cycle(self, item_id: str) -> Any:
        return self._ledger.review_rounds_in_cycle(item_id)


def _config() -> DispatchConfig:
    """No repo: the gh-backed ready queue has nothing to read, so no shell-out."""
    return DispatchConfig(project=ProjectConfig(repo=""))


def _node() -> dict[str, Any]:
    return {
        "id": ITEM,
        "title": "review gate lows",
        "status": "in_review",
        "status_since": "2026-10-01T09:00:00+00:00",
        "url": None,
        "pr_url": PR_URL,
        "external": False,
        "stuck": None,
    }


def _store(ledger: SqliteRunStore) -> _VerifyLedger:
    snapshot = {
        "schema": 1,
        "epic_ref": EPIC,
        "signal": "awaiting_merges",
        "counts": {},
        "frontier": [],
        "nodes": [_node()],
        "edges": [],
    }
    return _VerifyLedger(
        ledger,
        epic_refs=[EPIC],
        driver_states={
            EPIC: FakeDriverState(epic_ref=EPIC, last_tick_started_at=NOW.isoformat())
        },
        epic_ticks={EPIC: FakeEpicTick(snapshot=snapshot)},
    )


def _review_rounds(ledger: SqliteRunStore, *heads: str) -> None:
    """The heads the PR has had this cycle, oldest first: the last is current."""
    for head in heads:
        ledger.record_review_round(f"epic:{EPIC}", ITEM, head_sha=head, verdict="READY")


def _verify(
    ledger: SqliteRunStore,
    outcome: Any,
    *,
    head: str = HEAD,
    recorded_at: str = "2026-10-02T11:00:00+00:00",
) -> None:
    """One drive merge verify of main merged into *head*, as the host writes it.

    A verify that did not finish ran no held-out tests, so it records none.
    """
    failed = [
        "tests/test_queue.py::test_lease_expires_after_ttl",
        "tests/test_queue.py::test_claim_is_exclusive",
    ]
    held_out: dict[str, Any] | None = (
        {"outcome": "failed", "failed": failed, "declared": [], "undeclared": failed}
        if outcome == "failed"
        else {"outcome": "passed", "failed": [], "declared": [], "undeclared": []}
    )
    if outcome == "incomplete":
        held_out = None
    emit(
        ledger,
        VERIFY_EVENT,
        actor=Actor("policy", "merge-verify"),
        payload={
            "outcome": outcome,
            "ref_sha": "aaaa0000bbbb1111cccc2222dddd3333eeee4444",
            "ref_parents": [head, BASE],
            "base_sha": BASE,
            "held_out": held_out,
            "recorded_at": recorded_at,
        },
    )


def _row(ledger: SqliteRunStore) -> Any:
    entries = needs_you.needs_you_set(_store(ledger), _config(), now=NOW).entries
    [row] = [entry for entry in entries if entry.item_id == ITEM]
    return row


def _attention(ledger: SqliteRunStore) -> Any:
    return compose_node_attention(
        _store(ledger),
        _config(),
        [ITEM],
        nodes=[_node()],
        epic_ref=EPIC,
        signal="awaiting_merges",
        now=NOW,
    ).by_node[ITEM]


def test_a_merge_over_a_verify_failed_at_its_head_leads_with_the_failure(
    ledger: SqliteRunStore,
) -> None:
    """Work's list, Monitor's hold, the item's block and the epic rail all
    print the set row's or the node join's sentence and links."""
    _review_rounds(ledger, HEAD)
    _verify(ledger, "failed")

    row, attention = _row(ledger), _attention(ledger)

    sentence = f"{FAILURE} — the merge decision is yours"
    assert row.sentence == attention.sentence == sentence
    # The first move is the failure: the item's MERGE VERIFY section.
    for dto in (row, attention):
        assert dto.verify_link is not None
        assert dto.verify_link.label == "See the failed merge verify"
        assert dto.verify_link.href == f"/work?item={ITEM}#merge-verify"
        # The PR is still the second, and still the merge's link.
        assert dto.link is not None
        assert (dto.link.label, dto.link.href) == ("Open PR #2059 to merge", PR_URL)


def test_a_re_verify_that_did_not_finish_keeps_the_failure_lead(
    ledger: SqliteRunStore,
) -> None:
    """A re-verify of the same head that ended with no verdict knows nothing
    new about it: the failure still leads, with its link."""
    _review_rounds(ledger, HEAD)
    _verify(ledger, "failed", recorded_at="2026-10-02T11:00:00+00:00")
    _verify(ledger, "incomplete", recorded_at="2026-10-02T11:30:00+00:00")

    row, attention = _row(ledger), _attention(ledger)

    sentence = f"{FAILURE} — the merge decision is yours"
    assert row.sentence == attention.sentence == sentence
    for dto in (row, attention):
        assert dto.verify_link is not None
        assert dto.verify_link.label == "See the failed merge verify"
        assert dto.verify_link.href == f"/work?item={ITEM}#merge-verify"


def test_a_later_row_whose_outcome_is_not_a_string_keeps_the_failure_lead(
    ledger: SqliteRunStore,
) -> None:
    """A malformed outcome is no verdict: the failure still leads, and the
    merge verify source is not named unavailable."""
    _review_rounds(ledger, HEAD)
    _verify(ledger, "failed", recorded_at="2026-10-02T11:00:00+00:00")
    _verify(ledger, ["failed"], recorded_at="2026-10-02T11:30:00+00:00")

    result = needs_you.needs_you_set(_store(ledger), _config(), now=NOW)
    [row] = [entry for entry in result.entries if entry.item_id == ITEM]

    assert result.unavailable == []
    assert row.sentence == f"{FAILURE} — the merge decision is yours"
    assert row.verify_link is not None
    assert row.verify_link.label == "See the failed merge verify"


def test_a_pass_after_the_failure_at_the_same_head_clears_the_lead(
    ledger: SqliteRunStore,
) -> None:
    _review_rounds(ledger, HEAD)
    _verify(ledger, "failed", recorded_at="2026-10-02T11:00:00+00:00")
    _verify(ledger, "passed", recorded_at="2026-10-02T11:30:00+00:00")

    row, attention = _row(ledger), _attention(ledger)

    assert row.sentence == attention.sentence == MERGE_SENTENCE
    assert row.verify_link is None and attention.verify_link is None


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(lambda ledger: _verify(ledger, "passed"), id="passed"),
        pytest.param(lambda ledger: None, id="not-recorded"),
        pytest.param(
            lambda ledger: _verify(ledger, "failed", head=OLDER_HEAD),
            id="failed-at-an-older-head",
        ),
    ],
)
def test_a_merge_with_no_failure_at_its_head_keeps_today_s_sentence(
    ledger: SqliteRunStore, seed: Any
) -> None:
    _review_rounds(ledger, OLDER_HEAD, HEAD)
    seed(ledger)

    row, attention = _row(ledger), _attention(ledger)

    assert row.sentence == attention.sentence == MERGE_SENTENCE
    assert row.verify_link is None and attention.verify_link is None
    assert row.link is not None and row.link.href == PR_URL


def test_an_unreadable_verify_keeps_today_s_sentence_and_says_why(
    ledger: SqliteRunStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A verify the store cannot answer is not a failure to lead with — and the
    fallback is not silent: a warning names why the merge kept its sentence."""
    _review_rounds(ledger, HEAD)
    _verify(ledger, "failed")
    ledger.close()

    with caplog.at_level(logging.WARNING, logger="kew.service.attention"):
        row = _row(ledger)
        _row(ledger)

    assert row.sentence == MERGE_SENTENCE
    assert row.verify_link is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # Once, not once per poll: the set is composed on every one.
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith(
        "merge verify unreadable for the needs-you set, "
        "so its merge keeps today's sentence: "
    )
