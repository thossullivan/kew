"""A ``kind = test`` criterion is judged by its acceptance-contract test (#2732).

The node's acceptance contract holds one test per ``kind = test`` criterion,
``test_ac<k>``. The reviewer's ``criteria_results`` entry for such a criterion
must cite that test's executed outcome: a ``pass`` that cites no passing run is
recorded ``unverifiable``, and a cited failing run is a ``fail`` — which keeps
the round out of READY and out of residual acceptance like any other fail.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kew.review_contract import ParsedVerdict, parse_verdict_sidecar
from kew.review_criteria import contract_test_ids, review_plan_section
from kew.review_residuals import stop_rule_decision
from kew.triage.plan import canonical_plan_json, plan_sha
from kew.triage.schema import AcceptanceCriterion, ItemPlan

PLAN_IDS = ("AC1", "AC2")
CONTRACT_IDS = ("AC1",)


def _evidence() -> dict[str, str]:
    return {"command": "uv run pytest tests/acceptance -q", "output_digest": "ab12"}


def _entry(cid: str, result: str, contract: Any = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": cid, "result": result, "evidence": _evidence()}
    if contract is not None:
        entry["contract_test"] = contract
    return entry


def _ready(*entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "READY",
        "acceptance_executed": True,
        "acceptance_evidence": [_evidence()],
        "criteria_results": list(entries),
    }


def _needs_changes(*entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "NEEDS_CHANGES",
        "findings": [
            {
                "severity": "low",
                "location": "code.py:1",
                "summary": "the contract test fails",
                "evidence": _evidence(),
                "criterion": "AC1",
            }
        ],
        "criteria_results": list(entries),
    }


def _parse(sidecar: dict[str, Any]) -> ParsedVerdict:
    return parse_verdict_sidecar(
        json.dumps(sidecar),
        plan_criterion_ids=PLAN_IDS,
        contract_test_ids=CONTRACT_IDS,
    )


def _results(parsed: ParsedVerdict) -> list[tuple[str, str]]:
    return [(r.id, r.result) for r in parsed.criteria_results]


@pytest.mark.parametrize(
    "contract",
    [
        None,
        {"name": "test_ac1"},
        {"name": "test_ac1", "outcome": "skipped"},
        {"name": "test_ac1", "outcome": "error"},
        {"name": "test_ac2", "outcome": "passed"},
        {"outcome": "passed"},
        "test_ac1 passed",
    ],
)
def test_a_pass_without_the_contract_test_outcome_is_unverifiable(
    contract: Any,
) -> None:
    """AC1: an evidenced ``pass`` that cites no passing run of THE contract test
    for the criterion is recorded ``unverifiable`` — a missing citation, one with
    no outcome, a non-run outcome, another criterion's test, and a bare string
    alike. AC2 has no contract test, so its evidenced pass stands."""
    parsed = _parse(_ready(_entry("AC1", "pass", contract), _entry("AC2", "pass")))

    assert parsed.verdict == "READY"
    assert _results(parsed) == [("AC1", "unverifiable"), ("AC2", "pass")]
    assert parsed.criteria_results[0].contract_test_outcome == ""


def test_a_pass_citing_the_contract_test_passing_stands() -> None:
    parsed = _parse(
        _ready(
            _entry("AC1", "pass", {"name": "test_ac1", "outcome": "passed"}),
            _entry("AC2", "pass"),
        )
    )

    assert parsed.verdict == "READY"
    assert _results(parsed) == [("AC1", "pass"), ("AC2", "pass")]
    assert parsed.criteria_results[0].contract_test_outcome == "passed"


def test_a_pass_citing_a_passing_test_still_needs_evidence() -> None:
    entry = _entry("AC1", "pass", {"name": "test_ac1", "outcome": "passed"})
    del entry["evidence"]

    parsed = _parse(_ready(entry, _entry("AC2", "pass")))

    assert _results(parsed) == [("AC1", "unverifiable"), ("AC2", "pass")]


@pytest.mark.parametrize("claimed", ["pass", "unverifiable", "fail"])
def test_a_cited_failing_contract_test_makes_the_criterion_fail(claimed: str) -> None:
    """AC2: whatever the reviewer called it, a cited failing run is a ``fail``,
    which a NEEDS_CHANGES carries as a failing criterion."""
    parsed = _parse(
        _needs_changes(
            _entry("AC1", claimed, {"name": "test_ac1", "outcome": "failed"}),
            _entry("AC2", "pass"),
        )
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert _results(parsed) == [("AC1", "fail"), ("AC2", "pass")]
    assert parsed.failing_criteria == ["AC1"]
    assert parsed.criteria_results[0].contract_test_outcome == "failed"


def test_a_cited_failing_contract_test_blocks_residual_acceptance() -> None:
    """AC2: the stop rule would accept this all-low round on severity alone;
    the failed contract test keeps it in remediation."""
    parsed = _parse(
        _needs_changes(
            _entry("AC1", "pass", {"name": "test_ac1", "outcome": "failed"}),
            _entry("AC2", "pass"),
        )
    )
    decide = {"after_rounds": 6, "max_severity": "low"}

    assert (
        stop_rule_decision(7, "NEEDS_CHANGES", parsed.findings, **decide)
        == "READY_WITH_RESIDUALS"
    )
    assert (
        stop_rule_decision(
            7,
            "NEEDS_CHANGES",
            parsed.findings,
            failing_criteria=parsed.failing_criteria,
            **decide,
        )
        == "NEEDS_CHANGES"
    )


def test_a_ready_citing_a_failing_contract_test_is_unparseable() -> None:
    """The forged READY: a pass its own cited contract test contradicts."""
    parsed = _parse(
        _ready(
            _entry("AC1", "pass", {"name": "test_ac1", "outcome": "failed"}),
            _entry("AC2", "pass"),
        )
    )

    assert parsed.unparseable
    assert "AC1" in (parsed.unparseable_reason or "")


def test_a_cited_failing_contract_test_needs_a_finding_naming_it() -> None:
    sidecar = _needs_changes(
        _entry("AC1", "pass", {"name": "test_ac1", "outcome": "failed"}),
        _entry("AC2", "pass"),
    )
    del sidecar["findings"][0]["criterion"]

    parsed = _parse(sidecar)

    assert parsed.unparseable
    assert "AC1" in (parsed.unparseable_reason or "")


def test_criteria_without_a_contract_test_ignore_the_citation() -> None:
    """A round handed no contract-test ids parses exactly as before."""
    parsed = parse_verdict_sidecar(
        json.dumps(
            _ready(
                _entry("AC1", "pass"),
                _entry("AC2", "pass", {"name": "test_ac2", "outcome": "failed"}),
            )
        ),
        plan_criterion_ids=PLAN_IDS,
    )

    assert parsed.verdict == "READY"
    assert _results(parsed) == [("AC1", "pass"), ("AC2", "pass")]


class _Row:
    def __init__(self, plan: ItemPlan) -> None:
        self.plan_json = canonical_plan_json(plan)
        self.plan_sha = plan_sha(plan)
        self.id = 1
        self.item_id = "42"


def _mixed_plan() -> ItemPlan:
    return ItemPlan(
        acceptance_criteria=[
            AcceptanceCriterion(id="AC1", statement="pinned by a test", kind="test"),
            AcceptanceCriterion(id="AC2", statement="observed behaviour"),
        ]
    )


def test_the_section_names_each_contract_test_and_the_citation_rule() -> None:
    plan = review_plan_section(_Row(_mixed_plan()))  # type: ignore[arg-type]

    assert plan is not None
    assert plan.contract_test_ids == ("AC1",)
    ac1, ac2 = (
        line for line in plan.section.splitlines() if line.startswith(("AC1:", "AC2:"))
    )
    assert "contract test: test_ac1" in ac1
    assert "contract test" not in ac2
    assert '"contract_test"' in plan.section


def test_a_plan_with_no_test_criteria_states_no_citation_rule() -> None:
    plan = review_plan_section(
        _Row(  # type: ignore[arg-type]
            ItemPlan(acceptance_criteria=[AcceptanceCriterion(id="AC1", statement="x")])
        )
    )

    assert plan is not None
    assert plan.contract_test_ids == ()
    assert "contract_test" not in plan.section


def test_contract_test_ids_round_trip_out_of_the_round_row() -> None:
    plan = review_plan_section(_Row(_mixed_plan()))  # type: ignore[arg-type]

    assert plan is not None
    assert contract_test_ids(plan.criteria_json) == ("AC1",)


@pytest.mark.parametrize("stored", [None, "", "not json", '{"id": "AC1"}'])
def test_an_unreadable_criteria_json_yields_no_contract_test_ids(
    stored: str | None,
) -> None:
    assert contract_test_ids(stored) == ()
