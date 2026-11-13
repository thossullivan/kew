"""An approval hold is said as the action a person takes (design review c9, D12).

The console printed the hold's reason raw: "awaiting approval — no history —
assuming per-issue cap of $25.00", which names no action. The sentence every
surface prints is now the action and why its amount is what it is; the stored
basis, the audit payload and the row's reason are unchanged.

The amount is an estimate, never a ceiling: the per-issue cap is checked at
admission only, and ``agent.max_budget_usd`` bounds a running agent. So no
sentence says "up to".
"""

from __future__ import annotations

from dataclasses import dataclass

from kew.approval import estimate_dispatch_cost
from kew.config import BudgetConfig
from kew.report import ApprovalHold
from kew.service.attention import approval_reason, say_wait, wait_sentence
from kew.service.models import DashboardRowDTO

NO_HISTORY = (
    "approve a run estimated at $15.00, the per-issue cap; "
    "no cost history to estimate from"
)


@dataclass
class _Run:
    status: str
    model: str
    complexity: str
    estimated_cost_usd: float


class _History:
    """A run store at its one read here: the runs, oldest first."""

    def __init__(self, costs: list[float]) -> None:
        self._runs = [_Run("success", "m", "", cost) for cost in costs]

    def list_all(self) -> list[_Run]:
        return self._runs


def _sentence(basis: str, estimate: float | None = None) -> str:
    hold = ApprovalHold(issue_num=114, estimate_usd=estimate, basis=basis)
    return wait_sentence("approval", approval_reason(hold), estimate_usd=estimate)


def _basis(costs: list[float] | None) -> str:
    """The basis the writer records, under the default per-issue cap."""
    store = None if costs is None else _History(costs)
    budget = BudgetConfig()
    return estimate_dispatch_cost(store, complexity="", model="m", budget=budget).basis  # type: ignore[arg-type]


def test_a_hold_with_no_history_reads_as_the_cap_estimate() -> None:
    basis = _basis(None)
    # The stored basis is the one the audit payload has always carried.
    assert basis == "no history — assuming per-issue cap of $15.00"
    assert _sentence(basis, 15.0) == NO_HISTORY


def test_a_hold_with_too_few_runs_counts_them() -> None:
    assert _sentence(_basis([1.0])) == (
        "approve a run estimated at $15.00, the per-issue cap; "
        "only 1 similar run to estimate from"
    )
    assert _sentence(_basis([1.0, 2.0])) == (
        "approve a run estimated at $15.00, the per-issue cap; "
        "only 2 similar runs to estimate from"
    )


def test_a_hold_with_history_names_the_median() -> None:
    assert _sentence(_basis([30.0, 40.0, 50.0])) == (
        "approve a run estimated at $40.00, the median of 3 similar runs"
    )


def test_no_approval_sentence_names_a_ceiling() -> None:
    """The per-issue cap is not what bounds a run, so nothing says "up to"."""
    bases = [
        _basis(None),
        _basis([1.0]),
        _basis([1.0, 2.0]),
        _basis([30.0, 40.0, 50.0]),
        "over the per-issue cap",
        "",
    ]
    for basis in bases:
        assert "up to" not in _sentence(basis, 12.5), basis
        assert "up to" not in _sentence(basis), basis


def test_a_basis_kew_did_not_write_is_kept_after_the_action() -> None:
    assert _sentence("over the per-issue cap", 12.5) == (
        "approve a run estimated at $12.50; over the per-issue cap"
    )
    assert _sentence("") == "approve a run"


def test_only_the_exact_lead_comes_off_an_unknown_reason() -> None:
    """A reason that merely starts like the lead is kept as written."""
    said = wait_sentence("approval", "awaiting approvalx", estimate_usd=None)
    assert said == "approve a run; awaiting approvalx"
    # The basis keeps its own edges: only the lead and its separator come off.
    said = wait_sentence(
        "approval", "awaiting approval — — odd basis —", estimate_usd=None
    )
    assert said == "approve a run; — odd basis —"
    assert wait_sentence("approval", "awaiting approval") == "approve a run"


def test_the_row_keeps_its_reason_and_carries_the_sentence() -> None:
    hold = ApprovalHold(
        issue_num=114,
        estimate_usd=15.0,
        basis="no history — assuming per-issue cap of $15.00",
    )
    row = DashboardRowDTO(
        type="approval_hold",
        reason=approval_reason(hold),
        issue_num=114,
        item_id="114",
        estimate_usd=15.0,
        attention_kind="approval",
    )
    said = say_wait(row, "approval", item_id="114")
    assert said.reason == (
        "awaiting approval — no history — assuming per-issue cap of $15.00"
    )
    assert said.sentence == NO_HISTORY
    assert said.link is None and said.verify_link is None
