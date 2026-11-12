"""Unit tests for the auto-ci-fix / infra-park recovery pass (F5).

No real ``gh``: the classifier and dispatcher seams are injected. Uses a real
``SqliteRunStore`` (via the shared ``store`` fixture) for the budget check.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC, NOW, FakeSource, _facts, _node

from kew.config import BudgetConfig, DispatchConfig, EpicConfig
from kew.graph.build import build_graph
from kew.graph.domain import Edge
from kew.graph.driver.ci_recovery import (
    INFRA_DEAD_REASON,
    ci_recovery_pass,
    decide_ci_recovery,
    default_dispatch_fix,
)
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL
from kew.prompt_refusals import PromptRefusal
from kew.run_store import RunRecord

store = _fx.store


# ---------------------------------------------------------------------------
# decide_ci_recovery — the pure policy
# ---------------------------------------------------------------------------


def test_decide_unknown_holds() -> None:
    assert decide_ci_recovery(None, budget_ok=True) == "hold"


def test_decide_infra_dead_parks_even_when_budget_ok() -> None:
    assert decide_ci_recovery("infra_dead", budget_ok=True) == "park"


def test_decide_code_failure_within_budget_dispatches() -> None:
    assert decide_ci_recovery("code_failure", budget_ok=True) == "dispatch"


def test_decide_code_failure_over_budget_holds() -> None:
    assert decide_ci_recovery("code_failure", budget_ok=False) == "hold"


# ---------------------------------------------------------------------------
# ci_recovery_pass — routing with injected classify/dispatch
# ---------------------------------------------------------------------------


def _config(*, per_issue_usd: float = 1000.0) -> DispatchConfig:
    return DispatchConfig(
        epic=EpicConfig(auto_ci_fix=True),
        budget=BudgetConfig(per_issue_usd=per_issue_usd),
    )


def _graph_with(*ids: str, edges: list[Edge] | None = None) -> Any:
    return build_graph(
        FakeSource(nodes=[_node(i) for i in ids], edges=edges or []), EPIC
    )


def test_infra_dead_node_is_parked_and_no_fix_dispatched(store: Any) -> None:
    graph = _graph_with("1", "2", edges=[Edge("1", "2")])
    node_facts = {"1": _facts(1, pr_number=101)}
    dispatched_calls: list[int] = []

    outcome = ci_recovery_pass(
        ["1"],
        graph=graph,
        node_facts=node_facts,
        config=_config(),
        store=store,
        now=NOW,
        classify=lambda f: "infra_dead",
        dispatch_fix=lambda f: dispatched_calls.append(f.issue_num) or True,
    )

    assert [s.node_id for s in outcome.parked] == ["1"]
    assert outcome.parked[0].reason == INFRA_DEAD_REASON
    # blast radius names the dependent it holds up.
    assert outcome.parked[0].blast_radius == ("2",)
    assert outcome.dispatched == []
    assert dispatched_calls == []  # NO agent run burned on infra-dead CI


def test_code_failure_within_budget_dispatches_fix(store: Any) -> None:
    graph = _graph_with("5")
    node_facts = {"5": _facts(5, pr_number=505)}
    seen: list[int] = []

    outcome = ci_recovery_pass(
        ["5"],
        graph=graph,
        node_facts=node_facts,
        config=_config(),
        store=store,
        now=NOW,
        classify=lambda f: "code_failure",
        dispatch_fix=lambda f: seen.append(f.issue_num) or True,
    )

    assert outcome.dispatched == ["5"]
    assert outcome.parked == []
    assert seen == [5]


def _save_run(
    store: Any, run_id: str, item: int, *, status: str, cost: float = 0.0
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=item,
            item_id=str(item),
            branch=f"agent/issue-{item}",
            status=status,
            exit_code=0 if status == "success" else -1,
            started_at=NOW,
            finished_at=NOW if status == "success" else None,
            estimated_cost_usd=cost,
        )
    )


_LOGGER = "kew.graph.driver.ci_recovery"


def test_code_failure_over_daily_cap_is_not_dispatched_and_stays_ci_pending(
    store: Any, caplog: Any
) -> None:
    # Committed spend today is past the daily cap: the ci-fix gate refuses.
    _save_run(store, "r-over", 7, status="success", cost=100.0)
    store.hold_epic_node(EPIC, "7", "ci_pending")
    graph = _graph_with("7")
    node_facts = {"7": _facts(7, pr_number=707)}
    calls: list[int] = []

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        outcome = ci_recovery_pass(
            ["7"],
            graph=graph,
            node_facts=node_facts,
            config=_config(),
            store=store,
            now=NOW,
            classify=lambda f: "code_failure",
            dispatch_fix=lambda f: calls.append(f.issue_num) or True,
        )

    assert outcome.dispatched == []
    assert outcome.parked == []
    assert calls == []  # budget-capped: no fix dispatched, no checkout
    assert [(h.item_id, h.reason) for h in store.list_epic_holds(EPIC)] == [
        ("7", "ci_pending")
    ]
    [record] = [r for r in caplog.records if r.name == _LOGGER]
    assert record.levelno == logging.WARNING
    assert "7 not dispatched" in record.getMessage()
    assert "Budget exceeded: daily limit $50.00" in record.getMessage()


def test_budget_refused_ci_fix_never_checks_out_and_warns_each_tick(
    store: Any, caplog: Any
) -> None:
    """Round 3 of PR 2034: under the shipped caps ($50 daily, $25 kill cap),
    two in-flight runs elsewhere plus the fix's own reservation are $75. The
    admit gate refuses that fix after its worktree checkout, so a driver that
    asked only about committed spend re-dispatched (and re-checked out) every
    tick. The pass now asks the admit gate's own question before any checkout;
    the node keeps its ci_pending hold and each tick logs the refusal."""
    _save_run(store, "in-flight-1", 1, status="pending")
    _save_run(store, "in-flight-2", 2, status="pending")
    store.hold_epic_node(EPIC, "7", "ci_pending")
    graph = _graph_with("7")
    node_facts = {"7": _facts(7, pr_number=707)}
    checkouts: list[int] = []
    config = DispatchConfig(epic=EpicConfig(auto_ci_fix=True))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _tick in range(2):
            outcome = ci_recovery_pass(
                ["7"],
                graph=graph,
                node_facts=node_facts,
                config=config,
                store=store,
                now=NOW,
                classify=lambda f: "code_failure",
                dispatch_fix=lambda f: checkouts.append(f.issue_num) or False,
            )
            assert outcome.dispatched == []

    assert checkouts == []
    assert [(h.item_id, h.reason) for h in store.list_epic_holds(EPIC)] == [
        ("7", "ci_pending")
    ]
    messages = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    assert (
        messages
        == [
            "ci recovery: 7 not dispatched, ci-fix budget refused: "
            "Budget exceeded: daily limit $50.00, spent $75.00"
        ]
        * 2
    )


def test_issue_implementation_spend_never_blocks_its_ci_fix(
    store: Any, caplog: Any
) -> None:
    """#1932: the per-issue implementation cap does not bind a ci-fix. An
    item past its per-issue cap, with another implementation run in flight,
    still gets its PR's CI fixed ($70 worst case is within a $100 daily cap;
    the per-issue cap stays at the shipped $15)."""
    _save_run(store, "impl-spent", 7, status="success", cost=20.0)
    _save_run(store, "impl-in-flight", 7, status="pending")
    store.hold_epic_node(EPIC, "7", "ci_pending")
    graph = _graph_with("7")
    node_facts = {"7": _facts(7, pr_number=707)}
    seen: list[int] = []

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        outcome = ci_recovery_pass(
            ["7"],
            graph=graph,
            node_facts=node_facts,
            config=DispatchConfig(
                epic=EpicConfig(auto_ci_fix=True),
                budget=BudgetConfig(daily_usd=100.0),
            ),
            store=store,
            now=NOW,
            classify=lambda f: "code_failure",
            dispatch_fix=lambda f: seen.append(f.issue_num) or True,
        )

    assert outcome.dispatched == ["7"]
    assert seen == [7]
    assert not [r for r in caplog.records if r.name == _LOGGER]


def test_unclassifiable_node_asks_no_budget_question(store: Any, caplog: Any) -> None:
    """Only a fixable failure is refused on budget; a still-pending CI keeps
    its ci_pending hold silently even when the daily cap is spent."""
    _save_run(store, "r-over", 7, status="success", cost=100.0)
    store.hold_epic_node(EPIC, "7", "ci_pending")
    graph = _graph_with("7")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        ci_recovery_pass(
            ["7"],
            graph=graph,
            node_facts={"7": _facts(7, pr_number=707)},
            config=_config(),
            store=store,
            now=NOW,
            classify=lambda f: None,
            dispatch_fix=lambda f: True,
        )

    assert [h.reason for h in store.list_epic_holds(EPIC)] == ["ci_pending"]
    assert not [r for r in caplog.records if r.name == _LOGGER]


def test_budget_refused_node_is_cleared_when_its_pr_closes(store: Any) -> None:
    """Round 4 of PR 2034: a refusal must not rewrite the ci_pending hold, or
    the tick's closed-PR cleanup (which matches ci_pending) strands the node
    held after its PR closes."""
    from kew.graph.driver.tick import _clear_closed_pr_artifacts

    _save_run(store, "r-over", 7, status="success", cost=100.0)
    store.hold_epic_node(EPIC, "7", "ci_pending")
    facts = _facts(7, pr_number=707)

    outcome = ci_recovery_pass(
        ["7"],
        graph=_graph_with("7"),
        node_facts={"7": facts},
        config=_config(),
        store=store,
        now=NOW,
        classify=lambda f: "code_failure",
        dispatch_fix=lambda f: True,
    )
    assert outcome.dispatched == []

    _clear_closed_pr_artifacts(
        EPIC,
        "7",
        replace(facts, pr_state="CLOSED"),
        facts=_fx.FakeFactsReader(),
        store=store,
        pending_review_label="pr-pending-review",
    )

    assert store.list_epic_holds(EPIC) == []


def test_unclassifiable_node_is_left_held(store: Any) -> None:
    graph = _graph_with("3")
    node_facts = {"3": _facts(3, pr_number=303)}

    outcome = ci_recovery_pass(
        ["3"],
        graph=graph,
        node_facts=node_facts,
        config=_config(),
        store=store,
        now=NOW,
        classify=lambda f: None,  # still pending / gh unreachable
        dispatch_fix=lambda f: True,
    )

    assert outcome.parked == []
    assert outcome.dispatched == []


def test_node_without_linked_pr_is_skipped(store: Any) -> None:
    graph = _graph_with("9")
    node_facts = {"9": _facts(9, pr_number=None)}
    calls: list[int] = []

    outcome = ci_recovery_pass(
        ["9"],
        graph=graph,
        node_facts=node_facts,
        config=_config(),
        store=store,
        now=NOW,
        classify=lambda f: calls.append(f.issue_num) or "code_failure",
        dispatch_fix=lambda f: True,
    )

    assert outcome.parked == []
    assert outcome.dispatched == []
    assert calls == []  # never even classified — no PR to fix


def test_at_most_one_dispatch_attempt_per_node_per_tick(store: Any) -> None:
    graph = _graph_with("1", "2")
    node_facts = {"1": _facts(1, pr_number=11), "2": _facts(2, pr_number=22)}
    classify_calls: list[int] = []

    outcome = ci_recovery_pass(
        ["1", "2"],
        graph=graph,
        node_facts=node_facts,
        config=_config(),
        store=store,
        now=NOW,
        classify=lambda f: classify_calls.append(f.issue_num) or "code_failure",
        dispatch_fix=lambda f: True,
    )

    # Each held node is visited exactly once this tick.
    assert sorted(classify_calls) == [1, 2]
    assert sorted(outcome.dispatched) == ["1", "2"]


def test_default_dispatch_fix_runner_preflight_refusal_keeps_node_held(
    store: Any, monkeypatch: Any, caplog: Any
) -> None:
    from kew.config import RouteEntry, RoutingConfig
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    config = _config()
    config.project.repo = "owner/repo"
    config.routing = RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6")
    )
    facts = replace(_facts(5, pr_number=505), head_oid="a" * 40)
    store.hold_epic_node(EPIC, "5", "ci_pending")
    refusal = "Runner 'codex' is not logged in"
    launched: list[str] = []
    preflighted: list[str | None] = []

    monkeypatch.setattr(
        "kew.graph.driver.ci_recovery.resolve_runner",
        lambda _config: ResolvedRoute(CodexRunner(), "gpt-5.6-sol"),
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: preflighted.append(runner_name) or refusal,
    )
    monkeypatch.setattr(
        "kew.graph.driver.ci_recovery._resolve_failed_run_id",
        lambda _repo, _sha: "123",
    )
    monkeypatch.setattr(
        "kew.ci_fix.dispatch_ci_fix",
        lambda **_kwargs: launched.append("ci-fix") or 0,
    )

    with caplog.at_level(logging.ERROR, logger="kew.graph.driver.ci_recovery"):
        dispatched = default_dispatch_fix(config, store)(facts)

    assert dispatched is False
    assert preflighted == ["codex"]
    assert launched == []
    assert [hold.reason for hold in store.list_epic_holds(EPIC)] == ["ci_pending"]
    assert refusal in caplog.text


_REFUSAL = PromptRefusal("active ci_fix prompt profile v1 is refused")


@pytest.mark.parametrize(
    ("exit_code", "dispatched"),
    [(0, True), (1, True), (2, False), (3, False), (4, False), (_REFUSAL, _REFUSAL)],
)
def test_default_dispatch_fix_reports_only_executed_attempts(
    store: Any, monkeypatch: Any, exit_code: int, dispatched: bool
) -> None:
    from unittest.mock import MagicMock

    facts = replace(_facts(5, pr_number=505), head_oid="a" * 40)
    fix = MagicMock(return_value=exit_code)
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        "kew.graph.driver.ci_recovery._resolve_failed_run_id",
        MagicMock(return_value="123"),
    )
    monkeypatch.setattr("kew.ci_fix.attempt_ci_fix", fix)

    assert default_dispatch_fix(_config(), store)(facts) is dispatched
    fix.assert_called_once()


def test_default_dispatch_fix_launches_the_preflighted_runner(
    store: Any, monkeypatch: Any
) -> None:
    from pathlib import Path
    from unittest.mock import MagicMock

    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute
    from kew.tracking import DispatchResult

    config = _config()
    config.project.repo = "owner/repo"
    facts = replace(_facts(5, pr_number=505), head_oid="a" * 40)
    resolved = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    preflighted: list[str | None] = []

    launch = MagicMock(
        return_value=DispatchResult(
            exit_code=1,
            issue_num=5,
            log_file=Path("/tmp/worktree/ci-fix.log"),
            launched=True,
        )
    )
    pinned_head = MagicMock()

    monkeypatch.setattr(
        "kew.graph.driver.ci_recovery.resolve_runner", lambda _config: resolved
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: preflighted.append(runner_name) or None,
    )
    monkeypatch.setattr(
        "kew.graph.driver.ci_recovery._resolve_failed_run_id",
        lambda _repo, _sha: "123",
    )
    monkeypatch.setattr(
        "kew.ci_fix.get_pr_for_run",
        lambda **_kwargs: {
            "number": 505,
            "headRefName": "agent/issue-5",
            "headRefOid": "c" * 40,
            "baseRefName": "main",
            "body": "Closes #5",
            "title": "Fix widget",
            "labels": [{"name": "agent-pr"}],
        },
    )
    monkeypatch.setattr("kew.ci_fix.extract_ci_log", lambda **_kwargs: "FAILED test")
    monkeypatch.setattr(
        "kew.ci_fix.checkout_existing_worktree",
        lambda **_kwargs: (Path("/tmp/worktree"), None),
    )
    monkeypatch.setattr("kew.ci_fix.resolve_runner", lambda _config: resolved)
    monkeypatch.setattr("kew.ci_fix.update_pr_labels", lambda *a, **k: None)
    # The checkout is fake; explicitly satisfy the pinned-head launch gate.
    monkeypatch.setattr("kew.ci_fix.require_pinned_pr_head", pinned_head)
    monkeypatch.setattr("kew.ci_fix.run_tracked_agent", launch)
    monkeypatch.setattr("kew.ci_fix.cleanup_worktree", lambda *a, **k: None)

    dispatched = default_dispatch_fix(config, store)(facts)

    assert dispatched is True
    pinned_head.assert_called_once_with(
        Path("/tmp/worktree"), "agent/issue-5", "c" * 40
    )
    launched_runner = launch.call_args.kwargs["runner"]
    assert preflighted == [launched_runner.name, launched_runner.name]
    assert launched_runner is resolved.runner


# ---------------------------------------------------------------------------
# _run_ci_recovery — tick-level gating + held-set adjustment
# ---------------------------------------------------------------------------


def _outcome_and_overlays(node_facts: dict[str, Any], held: list[str]):
    from kew.graph.driver.tick import _MergeOutcome, _Overlays

    overlays = _Overlays(statuses={}, node_facts=node_facts, stuck=[])
    merge_outcome = _MergeOutcome(
        merged=[], held=held, stuck_by_id={}, held_live=True, closed=[]
    )
    return merge_outcome, overlays


def _ci_held_verdict(node_id: str):
    from kew.graph.driver.merge import MergeVerdict

    return MergeVerdict(node_id, "held", "ci-blocked", "ci_pending")


def test_run_ci_recovery_is_noop_when_flag_off(store: Any) -> None:
    from kew.graph.driver.tick import _run_ci_recovery

    graph = _graph_with("1")
    merge_outcome, overlays = _outcome_and_overlays(
        {"1": _facts(1, pr_number=101)}, held=["1"]
    )
    classified: list[int] = []

    outcome = _run_ci_recovery(
        [_ci_held_verdict("1")],
        merge_outcome,
        overlays,
        graph,
        config=DispatchConfig(epic=EpicConfig(auto_ci_fix=False)),
        store=store,
        now=NOW,
        repo="owner/repo",
        classify=lambda f: classified.append(f.issue_num) or "infra_dead",
        dispatch_fix=lambda f: True,
    )

    assert outcome.parked == [] and outcome.dispatched == []
    assert classified == []  # gated off: classifier never called
    assert merge_outcome.held == ["1"]  # held set untouched


def test_run_ci_recovery_parks_infra_and_drops_from_held(store: Any) -> None:
    from kew.graph.driver.tick import _run_ci_recovery

    graph = _graph_with("1", "2")
    merge_outcome, overlays = _outcome_and_overlays(
        {"1": _facts(1, pr_number=101), "2": _facts(2, pr_number=202)},
        held=["1", "2"],
    )

    outcome = _run_ci_recovery(
        [_ci_held_verdict("1"), _ci_held_verdict("2")],
        merge_outcome,
        overlays,
        graph,
        config=_config(),
        store=store,
        now=NOW,
        repo="owner/repo",
        classify=lambda f: "infra_dead" if f.issue_num == 1 else "code_failure",
        dispatch_fix=lambda f: True,
    )

    assert [s.node_id for s in outcome.parked] == ["1"]
    assert outcome.dispatched == ["2"]
    # Parked node leaves held; dispatched (still fixing) node stays held.
    assert merge_outcome.held == ["2"]


def test_run_ci_recovery_ignores_ci_node_not_in_held(store: Any) -> None:
    # A ci_pending verdict whose node was already dropped from held (e.g. gone
    # stale -> ci-stalled) must not be picked up by the recovery pass.
    from kew.graph.driver.tick import _run_ci_recovery

    graph = _graph_with("1")
    merge_outcome, overlays = _outcome_and_overlays(
        {"1": _facts(1, pr_number=101)}, held=[]
    )
    classified: list[int] = []

    outcome = _run_ci_recovery(
        [_ci_held_verdict("1")],
        merge_outcome,
        overlays,
        graph,
        config=_config(),
        store=store,
        now=NOW,
        repo="owner/repo",
        classify=lambda f: classified.append(f.issue_num) or "infra_dead",
        dispatch_fix=lambda f: True,
    )

    assert outcome.parked == [] and outcome.dispatched == []
    assert classified == []


def test_a_ci_fix_waits_for_a_running_local_verify_then_dispatches(
    store: Any,
) -> None:
    """#3190: a ci-fix is a dispatch launch; while a local merge verify holds
    its lease the node stays held unchanged, with one deferral recorded."""
    from support.verify_lease_holder import lease_held

    from kew.graph.driver.tick import _run_ci_recovery
    from kew.verify_lease import LAUNCH_DEFERRED_EVENT

    graph = _graph_with("1")
    dispatched: list[int] = []

    def _tick() -> Any:
        merge_outcome, overlays = _outcome_and_overlays(
            {"1": _facts(1, pr_number=101)}, held=["1"]
        )
        outcome = _run_ci_recovery(
            [_ci_held_verdict("1")],
            merge_outcome,
            overlays,
            graph,
            config=_config(),
            store=store,
            now=NOW,
            repo="owner/repo",
            classify=lambda f: "code_failure",
            dispatch_fix=lambda f: dispatched.append(f.issue_num) or True,
        )
        return outcome, merge_outcome

    with lease_held() as holder:
        for _ in range(2):
            outcome, merge_outcome = _tick()
            assert dispatched == [] and outcome.dispatched == []
            assert merge_outcome.held == ["1"]
        events = store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
        assert [(e.item_id, e.payload["launch"]) for e in events] == [("1", "ci-fix")]
        holder.release()

    outcome, _ = _tick()
    assert dispatched == [1] and outcome.dispatched == ["1"]


def test_a_deferred_ci_fix_is_named_by_its_node_id_not_its_issue_number(
    store: Any,
) -> None:
    """A Linear node carries no issue number (``0``): keyed on it, every
    deferred ci-fix of one verify collapsed into a single event for item 0."""
    from support.verify_lease_holder import lease_held

    from kew.graph.driver.tick import _run_ci_recovery
    from kew.verify_lease import LAUNCH_DEFERRED_EVENT

    nodes = ("SFD-1", "SFD-2")
    merge_outcome, overlays = _outcome_and_overlays(
        {nid: _facts(0, pr_number=101 + i) for i, nid in enumerate(nodes)},
        held=list(nodes),
    )
    with lease_held():
        _run_ci_recovery(
            [_ci_held_verdict(nid) for nid in nodes],
            merge_outcome,
            overlays,
            _graph_with(*nodes),
            config=_config(),
            store=store,
            now=NOW,
            repo="owner/repo",
            classify=lambda f: "code_failure",
            dispatch_fix=lambda f: True,
        )
        events = store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)

    assert sorted(e.item_id for e in events) == list(nodes)


def test_epic_status_names_a_deferred_ci_fix_on_the_node_the_event_names(
    store: Any,
) -> None:
    """The deferral event and ``kew epic status`` key the node the same way,
    and status keeps the node's ``ci_pending`` hold line, naming the
    deferred fix after it."""
    from support.driver_fakes import FakeFactsReader
    from support.verify_lease_holder import lease_held

    from kew.epic_holds import CI_PENDING_HOLD_REASON
    from kew.graph.driver.tick import _run_ci_recovery, summarize
    from kew.verify_lease import LAUNCH_DEFERRED_EVENT

    store.hold_epic_node(EPIC, "1", CI_PENDING_HOLD_REASON)
    merge_outcome, overlays = _outcome_and_overlays(
        {"1": _facts(1, pr_number=101)}, held=["1"]
    )
    reader = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, pr_number=101, pr_state="OPEN")}
    )
    with lease_held() as holder:
        _run_ci_recovery(
            [_ci_held_verdict("1")],
            merge_outcome,
            overlays,
            _graph_with("1"),
            config=_config(),
            store=store,
            now=NOW,
            repo="owner/repo",
            classify=lambda f: "code_failure",
            dispatch_fix=lambda f: True,
        )
        [event] = store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
        status = summarize(
            EPIC, _graph_with("1"), config=_config(), store=store, facts=reader
        )
        waiting = {w.node_id: w.reason for w in status.waiting}
        holder.release()

    assert event.item_id == "1"
    assert waiting["1"].startswith(CI_PENDING_HOLD_REASON), waiting
    assert "; ci-fix deferred: verify running (" in waiting["1"]
    after = summarize(
        EPIC, _graph_with("1"), config=_config(), store=store, facts=reader
    )
    assert "deferred: verify running" not in {
        w.node_id: w.reason for w in after.waiting
    }.get("1", "")


def test_a_refused_ci_fix_profile_holds_the_node_and_is_not_retried(
    store: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """#3023: ``auto_ci_fix``, a red ``ci_pending`` node, and an active
    ``ci_fix`` profile whose digest no longer matches.

    The production dispatcher runs the real CI fix, which refuses before any
    checkout. The pass replaces ``ci_pending`` with an operator hold naming the
    refusal and its remedy, and the next tick, which still sees the merge
    pass's ``ci_pending`` verdict, attempts nothing: one refused run, one read
    of the CI logs.
    """
    import json
    import sys

    from support.prompt_profiles import activate_unchecked

    from kew import ci_fix
    from kew.config import ProjectConfig
    from kew.graph.driver.tick import _run_ci_recovery
    from kew.subprocess_utils import TOOL_PATHS

    # The `gh` process boundary: an executable that logs its argv and answers
    # the reads one failed CI run on an agent PR takes.
    calls = tmp_path / "gh-calls.jsonl"
    pr = {
        "number": 505,
        "headRefName": "agent/issue-5",
        "headRefOid": "c" * 40,
        "baseRefName": "main",
        "body": "Closes #5",
        "title": "Fix widget",
        "labels": [{"name": "agent-pr"}],
    }
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        f"with open({str(calls)!r}, 'a') as log:\n"
        "    log.write(json.dumps(argv) + '\\n')\n"
        "if argv[:1] == ['api'] and 'actions/runs' in argv[1]:\n"
        "    print('123')\n"
        "elif argv[:2] == ['run', 'view'] and 'headBranch' in argv:\n"
        "    print(json.dumps({'headBranch': 'agent/issue-5'}))\n"
        "elif argv[:2] == ['pr', 'view']:\n"
        f"    print(json.dumps({pr!r}))\n"
        "elif '--log-failed' in argv:\n"
        "    print('FAILED tests/test_a.py::test_a')\n"
    )
    gh.chmod(0o755)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))

    checkouts: list[Any] = []
    monkeypatch.setattr(ci_fix, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        ci_fix,
        "checkout_existing_worktree",
        lambda **kw: checkouts.append(kw) or (tmp_path / "worktree", None),
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure", lambda *_a, **_k: None
    )
    config = _config()
    config.project = ProjectConfig(repo="o/r")
    config.logging.dir = str(tmp_path / "logs")
    config.triggers.ci_failure_max_retries = 2
    activate_unchecked(store, "Keep it tidy.", "ci_fix", body_sha256="0" * 64)
    store.hold_epic_node(EPIC, "5", "ci_pending")
    graph = _graph_with("5")
    facts = {"5": replace(_facts(5, pr_number=505), head_oid="a" * 40)}

    outcomes = []
    for _tick in range(2):
        merge_outcome, overlays = _outcome_and_overlays(facts, held=["5"])
        outcomes.append(
            _run_ci_recovery(
                [_ci_held_verdict("5")],
                merge_outcome,
                overlays,
                graph,
                config=config,
                store=store,
                now=NOW,
                repo="o/r",
                classify=lambda f: "code_failure",
                dispatch_fix=None,
                epic_ref=EPIC,
            )
        )
    first, second = outcomes

    [hold] = store.list_epic_holds(EPIC)
    assert hold.reason.startswith(
        f"admission_refused: {PROMPT_CONFIGURATION_REFUSAL}: active ci_fix prompt"
    )
    assert "does not match the approved digest" in hold.reason
    assert "kew epic unhold" in hold.reason
    assert first.profile_held == {"5": hold.reason}
    assert first.dispatched == [] and second.dispatched == []
    assert second.profile_held == {}
    [row] = store.list_runs_by_branch("agent/issue-5")
    assert row.status == "refused"
    assert row.error_category == PROMPT_CONFIGURATION_REFUSAL
    assert checkouts == []
    gh_calls = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("--log-failed" in argv for argv in gh_calls) == 1
