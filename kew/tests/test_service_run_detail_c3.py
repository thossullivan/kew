"""RunDTO's C3 additions: the recorded route columns and the review verdict.

The run page (#2300) answers three questions the old projection could not: what
kew ROUTED to versus what the runner REPORTED, what the run was planned to cost
against what it is accounted at, and which governed-review verdict stands over
the work. The first two are columns already on the row and never projected; the
third is the attested ``adversarial_review_verdict`` fact, read on the detail
path only — this run's own verdict, then the work item's standing one as a
fallback, so two bounded reads at worst and never one per list row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew.audit import Actor, emit
from kew.config import ReviewConfig
from kew.review_audit import REVIEW_VERDICT_EVENT, emit_review_verdict
from kew.run_store import RunRecord, SqliteRunStore
from kew.service.models import VERDICT_SCOPE_ITEM, VERDICT_SCOPE_RUN
from kew.service.runs import get_run, list_runs

_START = datetime(2026, 4, 1, tzinfo=UTC)
ITEM = "4242"


def _record(run_id: str, **extra: Any) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=4242,
        item_id=ITEM,
        branch=f"agent/{run_id}",
        status="success",
        exit_code=0,
        started_at=_START,
        finished_at=_START + timedelta(minutes=3),
        **extra,
    )


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "detail.db")) as opened:
        yield opened


def test_the_recorded_route_and_accounting_columns_reach_the_dto(store: Any) -> None:
    store.save(
        _record(
            "routed",
            run_kind="adversarial_review",
            model="anthropic/claude-opus-4",
            reported_model="anthropic/claude-sonnet-4",
            provider="openrouter",
            transport_mode="gateway",
            planned_cost_usd=1.25,
            estimated_cost_usd=0.91,
            accounting_status="settled",
        )
    )

    dto = get_run(store, "routed")
    assert dto is not None
    assert dto.run_kind == "adversarial_review"
    assert dto.item_id == ITEM
    # A substitution is only visible as the difference between the two.
    assert (dto.model, dto.reported_model) == (
        "anthropic/claude-opus-4",
        "anthropic/claude-sonnet-4",
    )
    assert dto.provider == "openrouter"
    assert dto.transport_mode == "gateway"
    assert (dto.planned_cost_usd, dto.cost_usd) == (1.25, 0.91)
    assert dto.accounting_status == "settled"


def test_a_row_without_the_columns_answers_nothing_not_a_guess(store: Any) -> None:
    """A narrow read double has no route columns; the DTO says so."""

    class _NarrowRecord:
        run_id = "narrow"
        issue_num = 1
        status = "pending"

    from kew.service.runs import _record_to_run_dto

    dto = _record_to_run_dto(_NarrowRecord())
    assert dto.run_kind == ""
    assert dto.reported_model is None
    assert dto.provider is None
    assert dto.transport_mode == ""
    assert dto.planned_cost_usd is None
    assert dto.accounting_status is None
    assert dto.verdict is None


def _emit_verdict(
    store: Any,
    *,
    round_: int,
    verdict: str,
    sha: str,
    run_id: str | None = None,
) -> None:
    emit_review_verdict(
        store,
        ReviewConfig(),
        pr_number=99,
        item_id=ITEM,
        head_sha=sha,
        review_round=round_,
        verdict=verdict,
        reviewer_model="openai/gpt-5-codex",
        run_id=run_id,
        actor=Actor("policy", "driver"),
    )


def test_the_detail_carries_the_newest_attested_verdict(store: Any) -> None:
    store.save(_record("reviewed", run_kind="adversarial_review"))
    _emit_verdict(store, round_=1, verdict="NEEDS_CHANGES", sha="a" * 40)
    _emit_verdict(store, round_=2, verdict="READY", sha="b" * 40)

    dto = get_run(store, "reviewed")
    assert dto is not None
    assert dto.verdict is not None
    assert dto.verdict.round == 2
    assert dto.verdict.verdict == "READY"
    assert dto.verdict.head_sha == "b" * 40
    assert dto.verdict.reviewer_model == "openai/gpt-5-codex"
    assert dto.verdict.ts is not None


def test_a_run_with_no_review_has_no_verdict(store: Any) -> None:
    store.save(_record("unreviewed"))

    dto = get_run(store, "unreviewed")
    assert dto is not None
    assert dto.verdict is None


def test_a_malformed_verdict_row_is_tolerated_never_rewritten(store: Any) -> None:
    """The operator rule: a bad historical row is read around, not repaired.

    A payload whose ``round`` is text and whose ``head_sha`` is a number must
    not raise out of the detail read — the page shows the verdict it CAN read
    and says nothing it cannot.
    """
    store.save(_record("ragged"))
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=Actor("policy", "driver"),
        item_id=ITEM,
        issue_num=4242,
        payload={
            "verdict": "READY",
            "round": "one",
            "head_sha": 7,
            "reviewer_model": "",
        },
        strict=True,
    )

    dto = get_run(store, "ragged")
    assert dto is not None
    assert dto.verdict is not None
    assert dto.verdict.verdict == "READY"
    assert dto.verdict.round == 0
    assert dto.verdict.head_sha is None
    assert dto.verdict.reviewer_model is None

    # And the row itself is untouched.
    rows = store.list_audit_events(limit=1, event=REVIEW_VERDICT_EVENT)
    assert rows[0].payload["round"] == "one"


def test_a_verdict_event_with_no_verdict_word_is_no_verdict(store: Any) -> None:
    store.save(_record("wordless"))
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=Actor("policy", "driver"),
        item_id=ITEM,
        issue_num=4242,
        payload={"round": 1},
        strict=True,
    )

    dto = get_run(store, "wordless")
    assert dto is not None
    assert dto.verdict is None


def test_a_store_with_no_audit_surface_reads_no_verdict() -> None:
    """A narrow read double answers "no verdict", never a crash."""

    class _NarrowStore:
        def list_all(self) -> list[Any]:
            return []

        def get(self, run_id: str) -> Any:
            return None

    from kew.service.runs import _latest_verdict

    assert _latest_verdict(_NarrowStore(), _record("narrow")) is None


def test_a_store_that_refuses_the_filter_logs_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ "No verdict recorded" and "the verdict could not be read" differ."""

    class _RefusingStore:
        def list_audit_events(self, *args: Any, **kwargs: Any) -> list[Any]:
            raise TypeError("this double takes no filters")

    from kew.service.runs import _latest_verdict

    with caplog.at_level("WARNING"):
        assert _latest_verdict(_RefusingStore(), _record("refused")) is None
    assert "Could not read the review verdict" in caplog.text


def test_a_boolean_in_a_cost_column_is_not_a_dollar(store: Any) -> None:
    """``True`` passes ``isinstance(.., int)``; it is not a figure of $1.00."""

    class _BoolRecord:
        run_id = "bool"
        issue_num = 1
        status = "pending"
        planned_cost_usd = True

    from kew.service.runs import _record_to_run_dto

    assert _record_to_run_dto(_BoolRecord()).planned_cost_usd is None


def test_a_zero_planned_cost_is_a_figure_not_an_absence(store: Any) -> None:
    store.save(_record("free", planned_cost_usd=0.0))

    dto = get_run(store, "free")
    assert dto is not None
    assert dto.planned_cost_usd == 0.0


def test_the_list_page_never_spends_a_verdict_read_per_row(store: Any) -> None:
    """The verdict is a detail-only read: 200 rows may not cost 200 queries."""
    for index in range(5):
        store.save(_record(f"r{index}"))
    _emit_verdict(store, round_=1, verdict="READY", sha="c" * 40)

    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    try:
        page = list_runs(store, limit=5)
    finally:
        store._conn.set_trace_callback(None)

    assert len(page) == 5
    assert all(row.verdict is None for row in page)
    assert [sql for sql in statements if REVIEW_VERDICT_EVENT in sql] == []


def test_a_review_runs_page_shows_the_round_that_run_judged(store: Any) -> None:
    """A later attempt's verdict is not an earlier attempt's evidence (#2300).

    ``_latest_verdict`` used to read the newest verdict for the whole work
    item, so the page of the run that judged round 1 reported round 2's
    decision, round number and reviewed head as its own — the cross-attempt
    error this node fixes on the Gantt lane by naming the attempt. A governed
    review attests under its own run id, so that round is readable.
    """
    store.save(_record("round-one", run_kind="adversarial_review"))
    store.save(_record("round-two", run_kind="adversarial_review"))
    _emit_verdict(
        store, round_=1, verdict="NEEDS_CHANGES", sha="a" * 40, run_id="round-one"
    )
    _emit_verdict(store, round_=2, verdict="READY", sha="b" * 40, run_id="round-two")

    first = get_run(store, "round-one")
    assert first is not None and first.verdict is not None
    assert (first.verdict.round, first.verdict.verdict) == (1, "NEEDS_CHANGES")
    assert first.verdict.head_sha == "a" * 40
    assert first.verdict.scope == VERDICT_SCOPE_RUN

    second = get_run(store, "round-two")
    assert second is not None and second.verdict is not None
    assert (second.verdict.round, second.verdict.verdict) == (2, "READY")
    assert second.verdict.scope == VERDICT_SCOPE_RUN


def test_a_run_that_attested_nothing_says_the_verdict_is_the_items(
    store: Any,
) -> None:
    """An implementation run has no verdict of its own, and the DTO says so."""
    store.save(_record("implementation", run_kind="dispatch"))
    _emit_verdict(store, round_=1, verdict="READY", sha="c" * 40, run_id="reviewer")

    dto = get_run(store, "implementation")
    assert dto is not None and dto.verdict is not None
    assert dto.verdict.round == 1
    # The useful reading — which decision stands over the branch it pushed —
    # but labelled as the work item's, never as this run's own reviewed head.
    assert dto.verdict.scope == VERDICT_SCOPE_ITEM


def test_a_run_scoped_row_with_no_verdict_word_falls_back_to_the_item(
    store: Any,
) -> None:
    """A degenerate row under this run id is not a verdict; the item's is read."""
    store.save(_record("degenerate", run_kind="adversarial_review"))
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=Actor("policy", "driver"),
        run_id="degenerate",
        item_id=ITEM,
        issue_num=4242,
        payload={"round": 3},
        strict=True,
    )
    _emit_verdict(store, round_=2, verdict="READY", sha="d" * 40, run_id="elsewhere")

    dto = get_run(store, "degenerate")
    assert dto is not None and dto.verdict is not None
    assert (dto.verdict.round, dto.verdict.scope) == (2, VERDICT_SCOPE_ITEM)


def _verdict_narrowings(store: Any, run_id: str) -> list[dict[str, Any]]:
    """Every verdict read ``get_run`` spends, in order, with its narrowing."""
    reads: list[dict[str, Any]] = []
    underlying = store.list_audit_events

    def recording(**narrow: Any) -> Any:
        if narrow.get("event") == REVIEW_VERDICT_EVENT:
            reads.append(narrow)
        return underlying(**narrow)

    store.list_audit_events = recording
    try:
        get_run(store, run_id)
    finally:
        store.list_audit_events = underlying
    return reads


def test_a_runs_own_verdict_costs_one_read_not_the_fallback_too(
    store: Any,
) -> None:
    """The item arm is a fallback, so a run that attested spends ONE read."""
    store.save(_record("judged", run_kind="adversarial_review"))
    _emit_verdict(store, round_=1, verdict="READY", sha="e" * 40, run_id="judged")

    assert _verdict_narrowings(store, "judged") == [
        {"limit": 1, "event": REVIEW_VERDICT_EVENT, "run_id": "judged"}
    ]


def test_the_detail_path_spends_at_most_two_verdict_reads(store: Any) -> None:
    """The documented budget: two bounded reads at worst, run id first.

    Round 1 of this node asserted ONE non-posture read on the detail path and
    the run-scoped arm added a second, so the stale budget outlived the code it
    described. Pin the order and the ceiling here, where the arms live.
    """
    store.save(_record("implementation", run_kind="dispatch"))
    _emit_verdict(store, round_=1, verdict="READY", sha="f" * 40, run_id="reviewer")

    assert _verdict_narrowings(store, "implementation") == [
        {"limit": 1, "event": REVIEW_VERDICT_EVENT, "run_id": "implementation"},
        {"limit": 1, "event": REVIEW_VERDICT_EVENT, "item_or_issue": ITEM},
    ]
