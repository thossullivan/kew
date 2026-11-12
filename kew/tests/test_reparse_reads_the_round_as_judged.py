"""Every re-read of a stored verdict sees the findings the round was judged on
(#3516).

kew takes the tests a PR body declares off a round's ``kind: "declaration"``
findings, unless the finding failed an acceptance criterion. A later tick
re-parses the stored sidecar — the next delta review, the stop rule, the
remediation's findings load and the declaration-only check — and each must
pass both the declared set recorded beside the sidecar and the round's
recorded criterion ids. Without the record a re-read finds tests the round
excused; without the ids it strips each finding's criterion, excuses it, and
the delta reviewer is never asked whether that defect was fixed.

Each round here is built so that either omission changes what the site reads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from support.driver_fakes import EPIC
from support.review_stream import capture_of, reviewer_stream

from kew import test_integrity as integrity
from kew.behavior_declarations import load_declaration_request, record_flagged_tests
from kew.graph.driver.review_launcher import _load_sidecar_findings
from kew.graph.driver.review_stage import _standing_verdict
from kew.review_contract import CollectedSidecar, collect_stream_verdict
from kew.review_delta import delta_section, plan_review_mode
from kew.run_store import ReviewRound

ITEM = "3516"
HEAD = "a" * 40
NEXT_HEAD = "b" * 40
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
WIDGET = "tests/test_widget.py::test_widget"
GADGET = "tests/test_widget.py::test_gadget"
SPROCKET = "tests/test_widget.py::test_sprocket"
CRITERIA_JSON = json.dumps([{"id": "AC1", "text": "the widget counts every slot"}])

_EVIDENCE = {"command": "uv run pytest tests/", "output_digest": "1 failed"}
_ACCEPTANCE = {
    "acceptance_executed": True,
    "acceptance_evidence": [
        {"command": "uv run pytest tests/", "output_digest": "2 passed"}
    ],
}


def _declaration(*node_ids: str, criterion: str | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "severity": "medium",
        "location": "tests/test_widget.py:1",
        "summary": f"no Behavior changes line names {', '.join(node_ids)}",
        "evidence": _EVIDENCE,
        "kind": "declaration",
        "node_ids": list(node_ids),
    }
    if criterion is not None:
        finding["criterion"] = criterion
    return finding


def _collect(
    tmp_path: Path,
    findings: list[dict[str, Any]],
    *,
    declared: list[str],
    criterion_ids: tuple[str, ...] = (),
    **extra: Any,
) -> CollectedSidecar:
    sidecar = {"verdict": "NEEDS_CHANGES", "findings": findings, **_ACCEPTANCE}
    return collect_stream_verdict(
        capture_of(reviewer_stream({**sidecar, **extra}, HEAD)),
        head_sha=HEAD,
        exit_code=0,
        dest_dir=tmp_path,
        run_id="rev-1",
        plan_criterion_ids=criterion_ids,
        declared_tests=declared,
    )


def _judged(tmp_path: Path) -> CollectedSidecar:
    """A round whose WIDGET finding failed AC1, whose GADGET finding the body
    excused, and whose SPROCKET finding stands: ignoring the record brings
    GADGET back, ignoring the criterion ids excuses WIDGET too."""
    collected = _collect(
        tmp_path,
        [
            _declaration(WIDGET, criterion="AC1"),
            _declaration(GADGET),
            _declaration(SPROCKET),
        ],
        declared=[WIDGET, GADGET],
        criterion_ids=("AC1",),
        criteria_results=[{"id": "AC1", "result": "fail", "evidence": _EVIDENCE}],
    )
    assert _read(collected.parsed.findings) == [(WIDGET, "AC1"), (SPROCKET, None)]
    return collected


def _round(collected: CollectedSidecar) -> ReviewRound:
    return ReviewRound(
        epic_ref=EPIC,
        item_id=ITEM,
        round=1,
        head_sha=HEAD,
        verdict="NEEDS_CHANGES",
        sidecar_digest=collected.digest,
        recorded_at=NOW,
        sidecar_path=str(collected.stored_path),
        criteria_json=CRITERIA_JSON,
    )


def _read(findings: Any) -> list[tuple[str, str | None]]:
    return [(i, f.criterion) for f in findings for i in f.node_ids]


def test_the_next_delta_review_carries_the_findings_the_round_was_judged_on(
    tmp_path: Path,
) -> None:
    plan = plan_review_mode(_round(_judged(tmp_path)), current_head=NEXT_HEAD)

    assert plan.candidate is not None
    assert _read(plan.candidate.findings) == [(WIDGET, "AC1"), (SPROCKET, None)]


def test_a_delta_after_a_round_whose_criterion_finding_stood_asks_about_it(
    tmp_path: Path,
) -> None:
    """The operator's round: WIDGET failed AC1 and GADGET was excused. The
    delta reviewer must be asked to confirm WIDGET was fixed."""
    collected = _collect(
        tmp_path,
        [_declaration(WIDGET, criterion="AC1"), _declaration(GADGET)],
        declared=[WIDGET, GADGET],
        criterion_ids=("AC1",),
        criteria_results=[{"id": "AC1", "result": "fail", "evidence": _EVIDENCE}],
    )
    assert collected.parsed.verdict == "NEEDS_CHANGES"
    assert _read(collected.parsed.findings) == [(WIDGET, "AC1")]

    plan = plan_review_mode(_round(collected), current_head=NEXT_HEAD)

    assert plan.candidate is not None
    assert _read(plan.candidate.findings) == [(WIDGET, "AC1")]
    section = delta_section(plan.candidate, base_ref="main", head_ref=NEXT_HEAD)
    assert "recorded no findings" not in section
    assert WIDGET in section


def test_the_stop_rule_reads_the_findings_the_round_was_judged_on(
    tmp_path: Path,
) -> None:
    parsed = _standing_verdict(_round(_judged(tmp_path)))

    assert parsed is not None
    assert _read(parsed.findings) == [(WIDGET, "AC1"), (SPROCKET, None)]


def test_the_remediation_loads_the_findings_the_round_was_judged_on(
    tmp_path: Path,
) -> None:
    collected = _judged(tmp_path)

    findings = _load_sidecar_findings(str(collected.stored_path), ("AC1",))

    assert _read(findings) == [(WIDGET, "AC1"), (SPROCKET, None)]


def _declaration_round(
    tmp_path: Path, findings: list[dict[str, Any]], declared: list[str]
) -> str:
    """A stored round rejecting only undeclared test edits, with the host's
    record of the tests it flagged. AC1 is judged ``unverifiable``, which
    fails no criterion, so only a finding's own criterion rules it out."""
    collected = _collect(
        tmp_path,
        findings,
        declared=declared,
        criterion_ids=("AC1",),
        criteria_results=[
            {"id": "AC1", "result": "unverifiable", "evidence": _EVIDENCE}
        ],
        test_changes=[
            {
                "flag": integrity.FLAG_UNDECLARED_TEST_EDITS,
                "answer": "REJECTED",
                "reason": "no Behavior changes line names them",
            }
        ],
    )
    edits = [
        integrity.Evidence("tests/test_widget.py", n, f"{node} is edited")
        for n, node in enumerate((WIDGET, GADGET, SPROCKET), start=1)
    ]
    report = integrity.with_undeclared_test_edits(
        integrity.TestIntegrityReport(), edits
    )
    assert record_flagged_tests(collected.stored_path, report) is None
    return str(collected.stored_path)


def test_a_declaration_request_names_only_what_the_round_left_standing(
    tmp_path: Path,
) -> None:
    sidecar = _declaration_round(
        tmp_path, [_declaration(GADGET), _declaration(SPROCKET)], [GADGET]
    )

    request = load_declaration_request(sidecar, ("AC1",))

    assert request is not None
    assert request.node_ids == (SPROCKET,)


def test_a_declaration_finding_that_failed_a_criterion_is_no_declaration_request(
    tmp_path: Path,
) -> None:
    sidecar = _declaration_round(
        tmp_path,
        [_declaration(WIDGET, criterion="AC1"), _declaration(SPROCKET)],
        [WIDGET],
    )

    assert load_declaration_request(sidecar, ("AC1",)) is None
