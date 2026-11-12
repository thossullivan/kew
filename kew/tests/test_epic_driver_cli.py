"""CLI wiring tests for Task 11: `kew epic advance|run|retry` + enriched
`epic status`.

`advance()`/`summarize()` themselves are exercised at the tick-orchestrator
level (see `test_driver_tick.py`); these tests only verify the CLI wires the
right config/store/deps/facts/sink into those seams and prints/exits
correctly. Every seam that would otherwise shell out (`advance`, `summarize`,
`preflight_tools`, `load_config`, the allowlisted `run`) is patched or given
a config that makes it a no-op.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support import source_ast
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from typer.testing import CliRunner

from kew.cli import epic_app
from kew.cli.epic import _epic_lock_path, watch_memory_path
from kew.config import DispatchConfig
from kew.drive import DriveLease, acquire_drive
from kew.drive_heartbeat import DRIVE_HEARTBEAT_KEY, DriveHeartbeat
from kew.graph.adapters.github_native import GitHubNativeSource
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.driver.attention_stream import MEMORY_VERSION
from kew.graph.driver.frontier import FrontierRank
from kew.graph.driver.result import (
    PrLink,
    StuckNode,
    TerminalSignal,
    TickResult,
    TickSummary,
)
from kew.graph.driver.tick import EmptyEpicGraphError
from kew.linear.client import LinearApiError, LinearUnavailableError
from kew.overlap import DispatchOutcome
from kew.run_store import RunRecord, SqliteRunStore

runner = CliRunner()
EPIC = "dag-1"
REPO = "o/r"


@pytest.fixture(autouse=True)
def _round14_fixture_ledger_discovery(tmp_path, monkeypatch):
    """Keep each CLI test's ledger discovery within its own fixture."""
    from kew import ledger_identity

    find_git_entry = ledger_identity._find_git_entry

    def fixture_git_entry(start):
        entry = find_git_entry(start)
        return entry if entry is not None and entry.is_relative_to(tmp_path) else None

    monkeypatch.setattr(ledger_identity, "_find_git_entry", fixture_git_entry)


@pytest.fixture(autouse=True)
def _isolated_ledger_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep CLI ledger registries inside each test's project."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)


class _StoreCtx:
    def __init__(self, store: SqliteRunStore) -> None:
        self.store = store

    def __enter__(self) -> SqliteRunStore:
        return self.store

    def __exit__(self, *exc: Any) -> bool:
        return False


def _config(tmp_path: Path) -> DispatchConfig:
    # Keep this test project's ledger out of any enclosing worktree metadata.
    (tmp_path / ".git").mkdir(exist_ok=True)
    cfg = DispatchConfig()
    cfg.project.repo = REPO
    cfg.logging.dir = str(tmp_path)
    return cfg


def _tick_result(**overrides: Any) -> TickResult:
    base: dict[str, Any] = dict(
        dispatched=(),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=0,
        signal=TerminalSignal.PROGRESSING,
        locked_out=False,
    )
    base.update(overrides)
    return TickResult(**base)


# ---------------------------------------------------------------------------
# `kew epic advance`
# ---------------------------------------------------------------------------


def test_advance_prints_summary_and_exits_zero(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = _tick_result(
        dispatched=("1",),
        in_flight=("2",),
        done=("3",),
        remaining=1,
        signal=TerminalSignal.PROGRESSING,
    )
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result) as advance_mock,
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "dispatched" in invoked.output.lower()
    advance_mock.assert_called_once()
    assert advance_mock.call_args.args[0] == EPIC


def test_advance_progressing_prints_the_limiter_that_withheld_ready_work(
    tmp_path: Path,
) -> None:
    """A PROGRESSING tick that withheld a READY node says which limiter did it
    (#2094). The waiting lines used to print only on STALLED, so an epic pinned
    to one dispatch per tick by its fair share trickled along in silence."""
    cfg = _config(tmp_path)
    waiting = (
        StuckNode(
            node_id="3",
            reason="admission-held: fair share 1 of 6 active epic(s) reached",
            blast_radius=("4",),
        ),
    )
    result = _tick_result(
        dispatched=("2",),
        waiting=waiting,
        remaining=3,
        signal=TerminalSignal.PROGRESSING,
    )
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result),
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "admission-held: fair share 1 of 6 active epic(s) reached" in invoked.output


def test_advance_progressing_does_not_print_ordinary_waiting_nodes(
    tmp_path: Path,
) -> None:
    """Only WITHHELD-ready reasons are promoted onto a progressing tick: a node
    merely awaiting its PR merge is not this tick's news (it prints in full on
    STALLED and in `kew epic status`)."""
    cfg = _config(tmp_path)
    waiting = (
        StuckNode(
            node_id="2",
            reason="in_review — awaiting merge (PR #662)",
            blast_radius=(),
        ),
    )
    result = _tick_result(
        waiting=waiting, remaining=1, signal=TerminalSignal.PROGRESSING
    )
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result),
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "waiting:" not in invoked.output


def test_run_progressing_tick_prints_the_withheld_limiter(tmp_path: Path) -> None:
    """The poll loop reports it too: `kew epic run` prints the withholding
    limiter on the progressing tick, not only when the epic finally stalls."""
    cfg = _config(tmp_path)
    progressing = _tick_result(
        dispatched=("2",),
        waiting=(
            StuckNode(
                node_id="3",
                reason=("review-backpressure: o/r is holding 3 of 3 human review(s)"),
                blast_radius=(),
            ),
        ),
        remaining=2,
        signal=TerminalSignal.PROGRESSING,
    )
    done = _tick_result(done=("2", "3"), signal=TerminalSignal.COMPLETE)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=[progressing, done]),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "review-backpressure: o/r is holding 3 of 3 human review(s)" in (
        invoked.output
    )


def test_advance_empty_graph_exits_2_with_diagnostic(tmp_path: Path) -> None:
    """`kew epic advance` on an epic whose graph resolves EMPTY must error
    (exit 2) with the tracker-specific diagnostic, never `COMPLETE 0/0` (#819)."""
    cfg = _config(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=EmptyEpicGraphError(EPIC)),
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 2, invoked.stdout
    assert "No issues found for label" in invoked.output
    assert f"epic:{EPIC}" in invoked.output
    assert "complete" not in invoked.output.lower()


def test_advance_linear_unavailable_exits_2_classified_not_traceback(
    tmp_path: Path,
) -> None:
    """A transient Linear outage that outlasts the client's retry budget must
    surface as a classified `linear-unavailable` diagnostic (exit 2), never a
    raw traceback."""
    cfg = _config(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.cli.advance",
            side_effect=LinearUnavailableError(
                "Linear API connection reset: [Errno 104]"
            ),
        ),
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 2, invoked.stdout
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit)
    assert "linear-unavailable" in invoked.output
    assert EPIC in invoked.output


def test_advance_locked_out_prints_message_and_exits_zero(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = _tick_result(locked_out=True)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result),
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "another driver" in invoked.output.lower()


def test_advance_takeover_flag_is_passed_to_advance(tmp_path: Path) -> None:
    """`--takeover` reaches `advance(takeover=True)` on this epic's OWN lock."""
    cfg = _config(tmp_path)
    result = _tick_result()
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result) as advance_mock,
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC, "--takeover"])

    assert invoked.exit_code == 0, invoked.stdout
    assert advance_mock.call_args.kwargs["takeover"] is True
    # Per-epic keyed lock path (#861/F2): the epic name is woven into the file.
    lock_path = advance_mock.call_args.kwargs["lock_path"]
    assert EPIC in lock_path.name


def test_epic_lock_path_is_distinct_per_epic(tmp_path: Path) -> None:
    """Two epics resolve to distinct lock files so they can tick concurrently."""
    from kew.cli import _epic_lock_path

    cfg = _config(tmp_path)
    a = _epic_lock_path(cfg, "alpha")
    b = _epic_lock_path(cfg, "beta")
    assert a != b
    assert "alpha" in a.name and "beta" in b.name


# ---------------------------------------------------------------------------
# `kew epic run` (poll wrapper)
# ---------------------------------------------------------------------------


def test_run_exits_zero_on_complete(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result) as advance_mock,
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    advance_mock.assert_called_once()
    sleep_mock.assert_not_called()


def test_run_exits_nonzero_on_stalled_and_never_sleeps_or_loops(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    stuck = (StuckNode(node_id="2", reason="failed-terminal", blast_radius=("3",)),)
    result = _tick_result(remaining=2, stuck=stuck, signal=TerminalSignal.STALLED)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result) as advance_mock,
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code != 0
    advance_mock.assert_called_once()
    sleep_mock.assert_not_called()


def test_run_stalled_prints_per_node_reasons_and_dominant_cause(
    tmp_path: Path,
) -> None:
    """On STALLED each stuck/waiting node prints a reason line (with a copyable
    retry command for failed-terminal nodes) and a dominant-cause summary
    replaces the bare 'needs an operator' line (#661)."""
    cfg = _config(tmp_path)
    stuck = (
        StuckNode(
            node_id="5",
            reason="failed-terminal (exit 143, circuit_breaker)",
            blast_radius=("7",),
        ),
    )
    waiting = (
        StuckNode(
            node_id="2",
            reason="in_review — awaiting merge (PR #662)",
            blast_radius=(),
        ),
        StuckNode(
            node_id="3",
            reason="in_review — awaiting merge (PR #663)",
            blast_radius=(),
        ),
    )
    result = _tick_result(
        remaining=4, stuck=stuck, waiting=waiting, signal=TerminalSignal.STALLED
    )
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code != 0
    out = invoked.output
    assert "failed-terminal (exit 143, circuit_breaker)" in out
    assert f"kew epic retry {EPIC} 5" in out
    assert "in_review — awaiting merge (PR #662)" in out
    # Dominant-cause summary line names the count + a resume hint.
    assert "STALLED — 2 in_review awaiting merge — merge to resume" in out


def _run_single(tmp_path: Path, result: TickResult) -> Any:
    """Invoke `kew epic run <EPIC>` with `advance` stubbed to return `result`."""
    cfg = _config(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result),
        patch("kew.cli._sleep"),
    ):
        return runner.invoke(epic_app, ["run", EPIC])


def test_run_stalled_all_in_review_exits_1(tmp_path: Path) -> None:
    """A STALLED tick whose only non-done nodes are IN_REVIEW awaiting merge
    ran and needs an operator: exit **1**. Code 2 means the driver could not
    run (#757, #1943)."""
    waiting = (
        StuckNode(
            node_id="2", reason="in_review — awaiting merge (PR #10)", blast_radius=()
        ),
        StuckNode(
            node_id="3", reason="in_review — awaiting merge (PR #11)", blast_radius=()
        ),
    )
    result = _tick_result(remaining=2, waiting=waiting, signal=TerminalSignal.STALLED)
    invoked = _run_single(tmp_path, result)
    assert invoked.exit_code == 1, invoked.output


def test_run_awaiting_merges_polls_until_external_merge_completes(
    tmp_path: Path,
) -> None:
    """The default driver remains alive while a human merges asynchronously."""
    cfg = _config(tmp_path)
    waiting = (
        StuckNode(
            node_id="1", reason="in_review — awaiting merge (PR #10)", blast_radius=()
        ),
        StuckNode(
            node_id="2", reason="in_review — awaiting merge (PR #11)", blast_radius=()
        ),
    )
    awaiting = _tick_result(
        remaining=2, waiting=waiting, signal=TerminalSignal.AWAITING_MERGES
    )
    complete = _tick_result(done=("1", "2"), signal=TerminalSignal.COMPLETE)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=[awaiting, complete]) as advance_mock,
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.output
    assert advance_mock.call_count == 2
    sleep_mock.assert_called_once()
    out = invoked.output
    assert "AWAITING_MERGES" in out
    assert "2 PRs awaiting a human merge" in out
    assert "PR #10" in out and "PR #11" in out
    assert "STALLED" not in out
    assert "needs an operator" not in out


def test_run_reloads_approvers_and_configured_parallel_each_tick(
    tmp_path: Path,
) -> None:
    initial = _config(tmp_path)
    initial.epic.approvers = ["first"]
    initial.epic.max_parallel = 1
    reloaded = initial.model_copy(deep=True)
    reloaded.epic.approvers = ["second"]
    reloaded.epic.max_parallel = 3
    awaiting = _tick_result(remaining=1, signal=TerminalSignal.AWAITING_MERGES)
    complete = _tick_result(done=("1",), signal=TerminalSignal.COMPLETE)

    with (
        patch("kew.cli.load_config", side_effect=[initial, initial, reloaded]),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=[awaiting, complete]) as advance_mock,
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.output
    first, second = advance_mock.call_args_list
    assert first.kwargs["approvers"] == ["first"]
    assert first.kwargs["max_parallel"] == 1
    assert second.kwargs["approvers"] == ["second"]
    assert second.kwargs["max_parallel"] == 3


def test_run_marker_reload_opens_fresh_store_for_the_next_tick(
    tmp_path: Path,
) -> None:
    """A long-running poller observes markers written between ticks."""
    cfg = _config(tmp_path)
    observed: list[tuple[int, int]] = []
    stores: list[Any] = []

    def _advance(_name: str, **kwargs: Any) -> TickResult:
        store = kwargs["store"]
        stores.append(store)
        observed.append(
            (
                len(store.list_epic_retries(EPIC)),
                len(store.list_continuations(EPIC)),
            )
        )
        if len(observed) == 1:
            store.mark_epic_retry_item(EPIC, "1")
            store.record_continuation(
                EPIC,
                "2",
                "original-run-2",
                "agent/issue-2",
                tmp_path / "preserved-2",
                checkout_generation=fixture_checkout_generation(
                    tmp_path / "preserved-2"
                ),
            )
            return _tick_result(remaining=1, signal=TerminalSignal.PROGRESSING)
        return _tick_result(done=("1", "2"), signal=TerminalSignal.COMPLETE)

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.output
    assert observed == [(0, 0), (1, 1)]
    assert stores[0] is not stores[1]


def test_run_reload_preserves_auto_detected_repo_identity(tmp_path: Path) -> None:
    initial = _config(tmp_path)
    initial.project.repo = ""
    reloaded = initial.model_copy(deep=True)
    complete = _tick_result(done=("1",), signal=TerminalSignal.COMPLETE)

    def _detect(config: DispatchConfig) -> None:
        config.project.repo = REPO

    with (
        patch("kew.cli.load_config", side_effect=[initial, reloaded]),
        patch("kew.cli._detect_repo", side_effect=_detect) as detect_mock,
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=complete),
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.output
    assert detect_mock.call_count == 2


def test_run_stalled_failed_terminal_exits_1(tmp_path: Path) -> None:
    """A STALLED tick with a failed-terminal node needs operator repair: exit
    **1**, the conservative "needs repair" code (#757)."""
    stuck = (StuckNode(node_id="2", reason="failed-terminal", blast_radius=("3",)),)
    result = _tick_result(remaining=2, stuck=stuck, signal=TerminalSignal.STALLED)
    invoked = _run_single(tmp_path, result)
    assert invoked.exit_code == 1, invoked.output


def test_run_stalled_in_review_plus_downstream_blocked_exits_1(tmp_path: Path) -> None:
    """IN_REVIEW awaiting merge with a downstream TODO blocked only on it is a
    STALLED tick that ran — exit **1** (#757, #1943)."""
    waiting = (
        StuckNode(
            node_id="2",
            reason="in_review — awaiting merge (PR #10)",
            blast_radius=("3",),
        ),
        StuckNode(node_id="3", reason="blocked on #2", blast_radius=()),
    )
    result = _tick_result(remaining=2, waiting=waiting, signal=TerminalSignal.STALLED)
    invoked = _run_single(tmp_path, result)
    assert invoked.exit_code == 1, invoked.output


def test_run_stalled_held_node_exits_1(tmp_path: Path) -> None:
    """A held (budget/review-stalled) node needs an operator, not a merge — a
    STALLED tick with one that has no IN_REVIEW node exits **1** (#757)."""
    waiting = (
        StuckNode(node_id="2", reason="budget_preflight (cap $5.00)", blast_radius=()),
    )
    result = _tick_result(remaining=1, waiting=waiting, signal=TerminalSignal.STALLED)
    invoked = _run_single(tmp_path, result)
    assert invoked.exit_code == 1, invoked.output


def test_run_stalled_awaiting_no_change_names_the_decision_and_exits_1(
    tmp_path: Path,
) -> None:
    """A recorded zero-change outcome needs a person, not a merge (I10B).

    Nothing was published, so there is no PR to merge and no node to retry —
    the epic resumes only when somebody says what the empty diff meant.
    """
    waiting = (
        StuckNode(
            node_id="2",
            reason=(
                "awaiting_no_change — the run changed nothing; confirm or"
                " reject the recorded no-change outcome"
            ),
            blast_radius=(),
        ),
    )
    result = _tick_result(remaining=1, waiting=waiting, signal=TerminalSignal.STALLED)
    invoked = _run_single(tmp_path, result)
    assert invoked.exit_code == 1, invoked.output
    assert "awaiting a no-change decision" in invoked.output


def _seed_resume_store(
    tmp_path: Path, *, state: str = "awaiting_continuation", in_memory: bool = False
) -> Any:
    store = SqliteRunStore(":memory:" if in_memory else str(tmp_path / "dispatch.db"))
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="original-run",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=now,
            finished_at=now,
        )
    )
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    store.record_continuation(
        "dag-1",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    if state == "continuing":
        assert (
            store.claim_continuation_under_cap(
                "dag-1",
                "42",
                "original-run",
                "agent/issue-42",
                worktree,
                "other-continuation",
                1,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "dag-1",
                    "42",
                    "original-run",
                    "agent/issue-42",
                    worktree,
                    "other-continuation",
                ),
            )
            == "claimed"
        )
    return store


def _make_safe_resume_checkout(worktree: Path) -> None:
    worktree.mkdir(parents=True)
    dot_git = worktree / ".git"
    dot_git.mkdir()
    (dot_git / "HEAD").write_text("ref: refs/heads/agent/issue-42\n")
    (dot_git / "objects").mkdir()

    from kew.worktree import write_checkout_generation

    write_checkout_generation(worktree)


def test_epic_resume_claims_once_and_dispatches_in_recorded_worktree(
    tmp_path: Path,
) -> None:
    from kew.config import RouteEntry, RoutingConfig, SandboxConfig
    from kew.dispatch_core import execute_dispatch as execute_dispatch_core
    from kew.tracking import DispatchResult

    cfg = _config(tmp_path)
    # The escape hatch: canonical resume refuses a checkout with no bound
    # base, and this mocked checkout has none (review round 16 of #1440).
    cfg.publication.publish = "checkout"
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    cfg.sandbox = SandboxConfig(enabled=False, dangerously_skip_containment=True)
    cfg.routing = RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
        medium=RouteEntry(runner="codex", model="gpt-5.6-sol"),
    )
    _make_safe_resume_checkout(tmp_path / "worktrees" / "agent" / "issue-42")
    store = _seed_resume_store(tmp_path)
    issue = {"number": 42, "title": "Continue", "body": "", "labels": []}
    preflighted: list[str | None] = []
    launch = MagicMock(
        return_value=DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=tmp_path / "run.log",
        )
    )

    def _preflight(_config: DispatchConfig, *, runner_name: str | None = None) -> None:
        preflighted.append(runner_name)

    try:
        with (
            patch("kew.cli.load_config", side_effect=[cfg, cfg]),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch("kew.cli.select_next_issue", return_value=issue),
            patch("kew.cli.runner_preflight_failure", side_effect=_preflight),
            patch(
                "kew.runner_preflight.runner_preflight_failure",
                side_effect=_preflight,
            ),
            patch("kew.dispatch_core._check_github_connectivity", return_value=True),
            patch("kew.dispatch_core.generate_repo_map", return_value=""),
            patch("kew.dispatch_core.build_prompt", return_value="prompt"),
            patch("kew.dispatch_core.run_tracked_agent", launch),
            patch(
                "kew.dispatch_core._collect_result",
                return_value=DispatchOutcome(exit_code=0),
            ),
            patch(
                "kew.cli.execute_dispatch",
                side_effect=execute_dispatch_core,
            ) as execute,
        ):
            invoked = runner.invoke(epic_app, ["continue", EPIC, "42"])

        assert invoked.exit_code == 0, invoked.output
        execute.assert_called_once()
        launch_kwargs = execute.call_args.kwargs
        assert launch_kwargs["issue"] is issue
        launched_runner = launch.call_args.kwargs["runner"]
        assert preflighted == [launched_runner.name, launched_runner.name]
        assert execute.call_args.kwargs["worktree_dir"] == (
            tmp_path / "worktrees" / "agent" / "issue-42"
        )
        assert execute.call_args.kwargs["branch"] == "agent/issue-42"
        continuation_run_id = execute.call_args.kwargs["run_id"]
        assert isinstance(continuation_run_id, str)
        record = store.get_continuation(
            EPIC,
            "42",
            "original-run",
            "agent/issue-42",
            tmp_path / "worktrees" / "agent" / "issue-42",
        )
        assert record is not None
        assert record.state == "terminal"
        assert record.continuation_run_id == continuation_run_id
    finally:
        store.close()


@pytest.mark.parametrize("in_memory", [False, True])
def test_epic_resume_lookup_failure_cancels_claim_before_retry_and_abandon(
    tmp_path, in_memory
):
    from dataclasses import replace

    from kew.audit_chain import verify_chain

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    store = _seed_resume_store(tmp_path, in_memory=in_memory)
    # The CLI checks ledger existence before opening the injected in-memory store.
    (tmp_path / "dispatch.db").touch(exist_ok=True)
    checkout = tmp_path / "worktrees" / "agent" / "issue-42"
    _make_safe_resume_checkout(checkout)
    (checkout / "edits.txt").write_text("preserved edits")

    def dispatch(**kwargs):
        store.save(replace(store.get("original-run"), run_id=kwargs["run_id"]))
        return DispatchOutcome(exit_code=1)

    try:
        with (
            patch("kew.cli.load_config", return_value=cfg),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch("kew.cli.epic._require_dispatch_child_runner"),
            patch(
                "kew.cli.select_next_issue",
                side_effect=[
                    RuntimeError("GitHub issue lookup failed"),
                    {"number": 42, "title": "Continue", "body": "", "labels": []},
                ],
            ) as lookup,
            patch("kew.cli.execute_dispatch", side_effect=dispatch) as execute,
        ):
            failed = runner.invoke(epic_app, ["continue", EPIC, "42"])
            # Main's two-axis exit scheme (#1943): a lookup failure before any
            # launch is could-not-run (2), not an executed failure (1).
            assert failed.exit_code == 2
            assert "GitHub issue lookup failed" in failed.output
            cancelled = store.list_audit_events(event="continuation_released")[0]
            assert cancelled.payload["outcome"] == "claim_undone"
            record = store.list_continuations(EPIC)[0]
            assert record.generation == cancelled.payload["restored_generation"] == 0
            assert store.get(cancelled.run_id) is None
            assert store.continuation_owner_run_ids(record) == ["original-run"]
            assert store.list_epic_reservations(EPIC) == []
            retried = runner.invoke(epic_app, ["continue", EPIC, "42"])
            assert retried.exit_code == 1
            assert lookup.call_count == 2
            execute.assert_called_once()
            current = store.list_continuations(EPIC)[0]
            assert current.generation == 1
            assert store.continuation_owner_run_ids(current) == [
                "original-run",
                execute.call_args.kwargs["run_id"],
            ]
            abandoned = runner.invoke(
                epic_app, ["abandon", EPIC, "42", "--reason", "replace stopped work"]
            )
            assert abandoned.exit_code == 0, abandoned.output
        assert store.list_continuations(EPIC)[0].state == "terminal"
        assert (checkout / "edits.txt").read_text() == "preserved edits"
        assert verify_chain(store.read_chain_rows()).ok
    finally:
        store.close()


def test_epic_resume_runner_preflight_refuses_before_launch(tmp_path: Path) -> None:
    from kew.config import RouteEntry, RoutingConfig

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    cfg.routing = RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
        complex=RouteEntry(runner="codex", model="gpt-5.6-sol"),
    )
    store = _seed_resume_store(tmp_path)
    refusal = "Runner 'codex' is not logged in. Authenticate with: codex login"
    preflighted: list[str | None] = []

    def _preflight(
        _config: DispatchConfig, *, runner_name: str | None = None
    ) -> str | None:
        preflighted.append(runner_name)
        return refusal if runner_name == "codex" else None

    try:
        with (
            patch("kew.cli.load_config", side_effect=[cfg, cfg]),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli._preflight_runner") as default_preflight,
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch(
                "kew.cli.select_next_issue",
                return_value={"number": 42, "labels": [{"name": "complex"}]},
            ),
            patch("kew.cli.runner_preflight_failure", side_effect=_preflight),
            patch(
                "kew.cli.execute_dispatch",
                return_value=DispatchOutcome(exit_code=0),
            ) as execute,
        ):
            invoked = runner.invoke(epic_app, ["continue", EPIC, "42"])

        assert invoked.exit_code == 2
        assert refusal in invoked.output
        assert preflighted == ["codex"]
        default_preflight.assert_not_called()
        execute.assert_not_called()
        record = store.get_continuation(
            EPIC,
            "42",
            "original-run",
            "agent/issue-42",
            tmp_path / "worktrees" / "agent" / "issue-42",
        )
        assert record is not None
        assert record.state == "awaiting_continuation"
    finally:
        store.close()


def test_epic_resume_rejects_concurrently_owned_continuation_without_dispatch(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    store = _seed_resume_store(tmp_path, state="continuing")

    try:
        with (
            patch("kew.cli.load_config", side_effect=[cfg, cfg]),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.execute_dispatch") as execute,
        ):
            invoked = runner.invoke(epic_app, ["continue", EPIC, "42"])

        assert invoked.exit_code == 2, invoked.output
        assert "another caller" in invoked.output
        execute.assert_not_called()
        record = store.get_continuation(
            EPIC,
            "42",
            "original-run",
            "agent/issue-42",
            tmp_path / "worktrees" / "agent" / "issue-42",
        )
        assert record is not None
        assert record.state == "continuing"
        assert record.continuation_run_id == "other-continuation"
    finally:
        store.close()


def test_epic_resume_rejects_escaped_worktree_before_claim(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="original-run",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=now,
            finished_at=now,
        )
    )
    store.record_continuation(
        EPIC,
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "outside",
        checkout_generation=fixture_checkout_generation(tmp_path / "outside"),
    )

    try:
        with (
            patch("kew.cli.load_config", side_effect=[cfg, cfg]),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.execute_dispatch") as execute,
        ):
            invoked = runner.invoke(epic_app, ["continue", EPIC, "42"])

        assert invoked.exit_code == 2, invoked.output
        assert "outside" in invoked.output
        execute.assert_not_called()
        pending = store.get_continuation(
            EPIC,
            "42",
            "original-run",
            "agent/issue-42",
            tmp_path / "outside",
        )
        assert pending is not None
        assert pending.state == "awaiting_continuation"
        assert store.list_epic_reservations(EPIC) == []
    finally:
        store.close()


def test_validate_resume_worktree_rejects_wrong_branch(tmp_path: Path) -> None:
    from kew.cli.epic import _validate_resume_worktree

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    # The escape hatch's git-based checks; canonical mode reads HEAD as data
    # and asks the host walker (tests/test_publication_round3.py).
    cfg.publication.publish = "checkout"
    cfg.sandbox.dangerously_skip_containment = True
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    _make_safe_resume_checkout(worktree)
    store = _seed_resume_store(tmp_path)
    record = store.get_continuation(
        EPIC,
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert record is not None

    try:
        with (
            patch(
                "kew.cli.epic.run",
                return_value=SimpleNamespace(returncode=0, stdout="agent/other\n"),
            ),
            pytest.raises(ValueError, match="not on recorded branch"),
        ):
            _validate_resume_worktree(record, cfg, store)
    finally:
        store.close()


def test_validate_resume_worktree_rejects_linked_checkout_without_deleting_it(
    tmp_path: Path,
) -> None:
    from kew.cli.epic import _validate_resume_worktree

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    store = _seed_resume_store(tmp_path)
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../../operator/.git/worktrees/issue-42")
    marker = worktree / "dirty.txt"
    marker.write_text("preserve me")
    record = store.get_continuation(
        EPIC,
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert record is not None

    try:
        with (
            patch("kew.cli.epic.run") as git_run,
            pytest.raises(ValueError) as raised,
        ):
            _validate_resume_worktree(record, cfg, store)

        message = str(raised.value)
        assert str(worktree) in message
        assert (
            "push the branch or copy the directory, then remove it and resume"
            in message
        )
        git_run.assert_not_called()
        assert worktree.is_dir()
        assert (worktree / ".git").is_file()
        assert marker.read_text() == "preserve me"
    finally:
        store.close()


def test_validate_resume_worktree_rejects_git_directory_symlink(
    tmp_path: Path,
) -> None:
    from kew.cli.epic import _validate_resume_worktree

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    store = _seed_resume_store(tmp_path)
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    worktree.mkdir(parents=True)
    operator_git = tmp_path / "operator.git"
    operator_git.mkdir()
    (operator_git / "HEAD").write_text("ref: refs/heads/main\n")
    (operator_git / "objects").mkdir()
    (worktree / ".git").symlink_to(operator_git, target_is_directory=True)
    marker = worktree / "dirty.txt"
    marker.write_text("preserve me")
    record = store.get_continuation(
        EPIC,
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert record is not None

    try:
        with (
            patch("kew.cli.epic.run") as git_run,
            pytest.raises(ValueError, match="standalone clone"),
        ):
            _validate_resume_worktree(record, cfg, store)

        git_run.assert_not_called()
        assert (worktree / ".git").is_symlink()
        assert marker.read_text() == "preserve me"
    finally:
        store.close()


def test_validate_resume_worktree_rejects_clean_checkout(tmp_path: Path) -> None:
    from kew.cli.epic import _validate_resume_worktree

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    # The escape hatch's git-based checks; canonical mode reads HEAD as data
    # and asks the host walker (tests/test_publication_round3.py).
    cfg.publication.publish = "checkout"
    cfg.sandbox.dangerously_skip_containment = True
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    _make_safe_resume_checkout(worktree)
    store = _seed_resume_store(tmp_path)
    record = store.get_continuation(
        EPIC,
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert record is not None

    try:
        with (
            patch(
                "kew.cli.epic.run",
                side_effect=[
                    SimpleNamespace(returncode=0, stdout="agent/issue-42\n"),
                    SimpleNamespace(returncode=0, stdout=""),
                ],
            ),
            pytest.raises(ValueError, match="is clean"),
        ):
            _validate_resume_worktree(record, cfg, store)
    finally:
        store.close()


def test_validate_resume_worktree_rejects_unreadable_checkout(tmp_path: Path) -> None:
    from kew.cli.epic import _validate_resume_worktree

    cfg = _config(tmp_path)
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    # The escape hatch's git-based checks; canonical mode reads HEAD as data
    # and asks the host walker (tests/test_publication_round3.py).
    cfg.publication.publish = "checkout"
    cfg.sandbox.dangerously_skip_containment = True
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    _make_safe_resume_checkout(worktree)
    store = _seed_resume_store(tmp_path)
    record = store.get_continuation(
        EPIC,
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert record is not None

    try:
        with (
            patch(
                "kew.cli.epic.run",
                side_effect=[
                    SimpleNamespace(returncode=0, stdout="agent/issue-42\n"),
                    SimpleNamespace(returncode=128, stdout=""),
                ],
            ),
            pytest.raises(ValueError, match="cannot inspect"),
        ):
            _validate_resume_worktree(record, cfg, store)
    finally:
        store.close()


def _run_all(tmp_path: Path, results: dict[str, TickResult]) -> Any:
    """Invoke `kew epic run --all` over `results` (epic name -> terminal tick)."""
    cfg = _config(tmp_path)

    def _advance(name: str, **_kw: Any) -> TickResult:
        return results[name]

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.run_store.SqliteRunStore.list_epic_refs",
            return_value=sorted(results),
        ),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        return runner.invoke(epic_app, ["run", "--all"])


def test_run_all_aggregate_is_worst_repair_beats_awaiting(tmp_path: Path) -> None:
    """`--all` aggregate exit code is the worst per-epic code: one repair epic
    plus one awaiting-merge epic exits **1** (#757)."""
    invoked = _run_all(
        tmp_path,
        {
            "a": _tick_result(
                remaining=1,
                stuck=(
                    StuckNode(node_id="1", reason="failed-terminal", blast_radius=()),
                ),
                signal=TerminalSignal.STALLED,
            ),
            "b": _tick_result(
                remaining=1,
                waiting=(
                    StuckNode(
                        node_id="2",
                        reason="in_review — awaiting merge",
                        blast_radius=(),
                    ),
                ),
                signal=TerminalSignal.STALLED,
            ),
        },
    )
    assert invoked.exit_code == 1, invoked.output


def test_run_all_stalled_awaiting_merge_exits_1(tmp_path: Path) -> None:
    """`--all` with a STALLED epic whose nodes await merge exits **1**: the
    driver ran and an operator must act (#757, #1943)."""
    invoked = _run_all(
        tmp_path,
        {
            "a": _tick_result(
                remaining=1,
                waiting=(
                    StuckNode(
                        node_id="1",
                        reason="in_review — awaiting merge",
                        blast_radius=(),
                    ),
                ),
                signal=TerminalSignal.STALLED,
            ),
            "b": _tick_result(done=("2",), remaining=0, signal=TerminalSignal.COMPLETE),
        },
    )
    assert invoked.exit_code == 1, invoked.output


def test_run_all_all_complete_exits_0(tmp_path: Path) -> None:
    """`--all` with every epic COMPLETE exits **0** (#757)."""
    invoked = _run_all(
        tmp_path,
        {
            "a": _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE),
            "b": _tick_result(done=("2",), remaining=0, signal=TerminalSignal.COMPLETE),
        },
    )
    assert invoked.exit_code == 0, invoked.output


def test_run_all_prints_combined_per_epic_classification_summary(
    tmp_path: Path,
) -> None:
    """`--all` ends with ONE combined fleet summary naming every driven epic
    under its exit classification (complete / locked-out / stalled), so a
    mixed-outcome fleet is legible even when the aggregate exit is a repair
    stall — the COMPLETE and awaiting epics are still named, not hidden behind
    the worst-wins exit line (F3 / #863)."""
    invoked = _run_all(
        tmp_path,
        {
            "alpha": _tick_result(
                done=("1",), remaining=0, signal=TerminalSignal.COMPLETE
            ),
            "beta": _tick_result(
                remaining=1,
                waiting=(
                    StuckNode(
                        node_id="2",
                        reason="in_review — awaiting merge",
                        blast_radius=(),
                    ),
                ),
                signal=TerminalSignal.STALLED,
            ),
            "gamma": _tick_result(
                remaining=1,
                stuck=(
                    StuckNode(node_id="3", reason="failed-terminal", blast_radius=()),
                ),
                signal=TerminalSignal.STALLED,
            ),
        },
    )
    out = invoked.output
    assert "fleet summary" in out
    assert "3 epic(s)" in out
    assert "complete: ['alpha']" in out
    assert "stalled: ['beta', 'gamma']" in out
    # Worst-wins aggregate is still the repair stall.
    assert invoked.exit_code == 1, out


def test_run_all_fleet_summary_labels_locked_out_distinctly(tmp_path: Path) -> None:
    """An epic held by another live driver exits 0 like a complete one, as a
    single locked-out run does, but is labelled `locked-out` in the fleet
    summary — the operator sees it is driven elsewhere (F3 / #863, #1943)."""
    invoked = _run_all(
        tmp_path,
        {
            "held": _tick_result(remaining=1, locked_out=True),
            "done": _tick_result(
                done=("1",), remaining=0, signal=TerminalSignal.COMPLETE
            ),
        },
    )
    out = invoked.output
    assert "locked-out: ['held']" in out
    assert "complete: ['done']" in out
    assert "1 epic(s) COMPLETE; ['held'] driven elsewhere." in out
    assert "all 2 epic(s) COMPLETE" not in out
    assert invoked.exit_code == 0, out


def test_run_all_logs_discovered_epics_and_source(tmp_path: Path) -> None:
    """`--all` logs the discovered roster + discovery source so a missing epic
    is diagnosable from output alone (#740)."""
    invoked = _run_all(
        tmp_path,
        {
            "a": _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE),
            "b": _tick_result(done=("2",), remaining=0, signal=TerminalSignal.COMPLETE),
        },
    )
    out = invoked.output
    assert "2 epic(s) discovered" in out
    assert "source: local" in out
    assert "a, b" in out


def test_run_all_empty_names_names_source(tmp_path: Path) -> None:
    """`--all` with no known epics names the local store as the source so an
    operator knows where a planned epic should have registered (#740)."""
    invoked = _run_all(tmp_path, {})
    assert "no known epics" in invoked.output
    assert "source: local" in invoked.output


def _run_all_scoped(
    tmp_path: Path,
    results: dict[str, TickResult],
    trackers: dict[str, str | None],
) -> Any:
    """Invoke `kew epic run --all` with an explicit per-epic tracker mapping so
    tracker-scoping (#882/B1) can be exercised without seeding item rows."""
    cfg = _config(tmp_path)

    def _advance(name: str, **_kw: Any) -> TickResult:
        return results[name]

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.run_store.SqliteRunStore.list_epic_refs",
            return_value=sorted(trackers),
        ),
        patch(
            "kew.run_store.SqliteRunStore.epic_trackers",
            return_value=trackers,
        ),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        return runner.invoke(epic_app, ["run", "--all"])


def test_run_all_skips_other_tracker_epics_with_note(tmp_path: Path) -> None:
    """`--all` on a GitHub project skips a Linear epic (different tracker) with
    a one-line note and never drives it — driving it would KeyError here since
    only the GitHub epic has a tick result (#882/B1)."""
    invoked = _run_all_scoped(
        tmp_path,
        {
            "gh-done": _tick_result(
                done=("1",), remaining=0, signal=TerminalSignal.COMPLETE
            )
        },
        {"gh-done": "github", "lin-epic": "linear"},
    )
    assert invoked.exit_code == 0, invoked.output
    assert "lin-epic" in invoked.output
    assert "(skipped: tracker=linear)" in invoked.output


def _archive(tmp_path: Path, *refs: str) -> None:
    """Record a real `epic_archived` event per ref in the ledger `--all` reads."""
    from kew.epic_archive import EPIC_ARCHIVED_EVENT
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        for ref in refs:
            store.append_audit_event(
                timestamp="2026-09-29T00:00:00+00:00",
                event=EPIC_ARCHIVED_EVENT,
                actor_type="human",
                actor_id="owner",
                payload={"epic_ref": ref, "reason": "retired"},
            )


def test_run_all_skips_archived_epics_with_a_note(tmp_path: Path) -> None:
    """#2206: `--all` never drives an epic an operator retired — driving it
    would KeyError here, since only the live epic has a tick result — and it
    says so, so a missing epic stays diagnosable from the output alone."""
    _archive(tmp_path, "retired")
    invoked = _run_all_scoped(
        tmp_path,
        {
            "live": _tick_result(
                done=("1",), remaining=0, signal=TerminalSignal.COMPLETE
            )
        },
        {"live": "github", "retired": "github"},
    )
    assert invoked.exit_code == 0, invoked.output
    assert "retired" in invoked.output
    assert "(skipped: archived)" in invoked.output


def test_run_all_with_every_epic_archived_drives_nothing(tmp_path: Path) -> None:
    """An all-archived roster is a deliberate empty, not a silent success with
    nothing said: name the count and the verb that reverses it."""
    _archive(tmp_path, "a", "b")
    invoked = _run_all_scoped(tmp_path, {}, {"a": "github", "b": "github"})
    assert invoked.exit_code == 0, invoked.output
    assert "kew epic unarchive" in invoked.output


def test_run_all_drives_an_unarchived_epic(tmp_path: Path) -> None:
    """Unarchive outranks the archive, so `--all` picks the epic back up."""
    from kew.epic_archive import EPIC_UNARCHIVED_EVENT
    from kew.run_store import SqliteRunStore

    _archive(tmp_path, "live")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.append_audit_event(
            timestamp="2026-09-30T00:00:00+00:00",
            event=EPIC_UNARCHIVED_EVENT,
            actor_type="human",
            actor_id="owner",
            payload={"epic_ref": "live"},
        )
    invoked = _run_all_scoped(
        tmp_path,
        {
            "live": _tick_result(
                done=("1",), remaining=0, signal=TerminalSignal.COMPLETE
            )
        },
        {"live": "github"},
    )
    assert invoked.exit_code == 0, invoked.output
    assert "(skipped: archived)" not in invoked.output


def test_run_all_honest_exit_excludes_skipped_epics(tmp_path: Path) -> None:
    """A complete GitHub epic exits **0** even though undriven Linear epics
    remain in the store — skipped epics contribute to neither STALLED
    classification nor the aggregate exit code (#882/B1)."""
    invoked = _run_all_scoped(
        tmp_path,
        {
            "gh-done": _tick_result(
                done=("1",), remaining=0, signal=TerminalSignal.COMPLETE
            )
        },
        {"gh-done": "github", "lin-1": "linear", "lin-2": "linear"},
    )
    assert invoked.exit_code == 0, invoked.output
    assert "all 1 epic(s) COMPLETE" in invoked.output
    # The fleet summary names only the driven epic, not the skipped Linear ones.
    assert "lin-1" not in invoked.output.split("fleet summary")[-1]


def _seed_pre_recording_linear_epic(
    store: SqliteRunStore, *, name: str, issue_num: int, key: str
) -> None:
    """Seed one Linear-era epic whose only work-item id lives in a pre-v25
    reservation row (item_id NULL) + the run it points at (runs.item_id). This
    is the shape the #887 heuristic missed: no item_id on any epic table, so it
    resolved None and drove the epic under the active GitHub tracker (#877/R2)."""
    store.register_epic(name)
    store.save(
        RunRecord(
            run_id=f"run-{key}",
            issue_num=0,
            branch=f"agent/{key}",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            item_id=key,
        )
    )
    store._conn.execute(
        "INSERT INTO epic_reservations (issue_num, epic_ref, run_id, reserved_at)"
        " VALUES (?, ?, ?, ?)",
        (issue_num, name, f"run-{key}", datetime.now(UTC).isoformat()),
    )
    store._conn.commit()


def test_run_all_skips_seven_pre_recording_linear_epics_and_exits_zero(
    tmp_path: Path,
) -> None:
    """Acceptance (#877/R2): a GitHub project whose store holds the seven
    Linear-era epics — four of them with item ids ONLY in the pre-recording
    source — lists all seven as `(skipped: tracker=linear)` and exits **0**,
    driving nothing. Uses the REAL `epic_trackers()` over seeded data (no mock),
    so it proves the backfill join resolves those tracker inferences."""
    cfg = _config(tmp_path)
    linear_epics = [
        ("driver-polish", 71, "SFD-71"),
        ("hold-controls", 72, "SFD-72"),
        ("ops-resilience", 73, "SFD-73"),
        ("pilot-rivets", 74, "SFD-74"),
    ]
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        # Four epics whose ids survive ONLY in the pre-recording source.
        for name, issue_num, key in linear_epics:
            _seed_pre_recording_linear_epic(
                store, name=name, issue_num=issue_num, key=key
            )
        # Three more that already carry a recorded Linear item id (#887 handled
        # these) — the full roster is seven Linear-era epics.
        for name, key in (("lin-a", "SFD-1"), ("lin-b", "SFD-2"), ("lin-c", "SFD-3")):
            store.register_epic(name)
            store.reserve_epic_node_item(name, key, f"res-{key}")

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=AssertionError("must not drive")),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert cfg.tracker.type == "github"
    assert invoked.exit_code == 0, invoked.output
    assert invoked.output.count("(skipped: tracker=linear)") == 7
    for name, *_ in linear_epics:
        assert name in invoked.output


def _seed_numeric_twin_linear_epic(
    store: SqliteRunStore, *, name: str, item_id: str
) -> None:
    """Seed a complete Linear epic whose only recorded work-item id is a NUMERIC
    GitHub-twin (a pre-SFD recording era, #927/N3). Shape inference reads a
    numeric id as a GitHub issue number, so WITHOUT the durable tracker this
    epic classifies ``github``, gets probed against GitHub labels, and
    false-STALLS. The driver recorded the active tracker (``linear``) at drive
    time — that is what must win."""
    store.register_epic(name)
    store.reserve_epic_node_item(name, item_id, f"run-{item_id}")
    store.record_epic_tracker(name, "linear")


def test_run_all_skips_linear_epic_with_numeric_twin_item_ids_and_exits_zero(
    tmp_path: Path,
) -> None:
    """Acceptance (#927/N3, third leak of #877): the REAL `epic run --all`
    discovery path over a GitHub project whose store holds a complete Linear
    epic carrying NUMERIC GitHub-twin item ids lists it `(skipped:
    tracker=linear)` and exits **0**, driving nothing. Uses the real
    `epic_trackers()` over seeded data (no mock) — the exact shape the two prior
    closes (#887 synthetic rows, #904 currency inference) never exercised, which
    is why the bug survived being closed twice."""
    cfg = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        for name, item_id in (
            ("driver-polish", "782"),
            ("hold-controls", "784"),
            ("ops-resilience", "819"),
            ("pilot-rivets", "820"),
        ):
            _seed_numeric_twin_linear_epic(store, name=name, item_id=item_id)

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=AssertionError("must not drive")),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert cfg.tracker.type == "github"
    assert invoked.exit_code == 0, invoked.output
    assert invoked.output.count("(skipped: tracker=linear)") == 4
    assert "STALLED" not in invoked.output


def test_run_all_all_epics_other_tracker_exits_clean(tmp_path: Path) -> None:
    """`--all` where every discovered epic belongs to another tracker drives
    nothing and exits **0** — an all-Linear roster is not a GitHub stall
    (#882/B1)."""
    invoked = _run_all_scoped(
        tmp_path,
        {},
        {"lin-1": "linear", "lin-2": "linear"},
    )
    assert invoked.exit_code == 0, invoked.output
    assert "(skipped: tracker=linear)" in invoked.output


def test_status_lists_waiting_holds_with_reasons(tmp_path: Path) -> None:
    """`kew epic status` lists held/waiting nodes with their reason strings so
    an operator can see why the epic isn't advancing (#661)."""
    cfg = _config(tmp_path)
    graph = Graph(
        [Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC)],
        [],
    )
    waiting = (
        StuckNode(
            node_id="1",
            reason="budget_preflight: spent $17.08 > per_issue cap $15.00 — "
            "raise budget.per_issue_usd in kew.toml",
            blast_radius=(),
        ),
    )
    summary = TickSummary(
        counts={"held": 1},
        frontier=(),
        in_flight=(),
        held=("1",),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.STALLED,
        waiting=waiting,
    )
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
        patch("kew.cli.advance") as advance_mock,
    ):
        invoked = runner.invoke(epic_app, ["status", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "budget_preflight" in invoked.output
    assert "budget.per_issue_usd" in invoked.output
    advance_mock.assert_not_called()


def test_run_loops_while_progressing_then_exits_zero_on_complete(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    progressing = _tick_result(
        dispatched=("1",), remaining=1, signal=TerminalSignal.PROGRESSING
    )
    complete = _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=[progressing, complete]) as advance_mock,
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC, "--interval", "1"])

    assert invoked.exit_code == 0, invoked.stdout
    assert advance_mock.call_count == 2
    sleep_mock.assert_called_once_with(1)


def test_run_locked_out_exits_zero_without_looping(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = _tick_result(locked_out=True)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result) as advance_mock,
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    advance_mock.assert_called_once()
    sleep_mock.assert_not_called()


def test_run_single_empty_graph_exits_2_without_looping(tmp_path: Path) -> None:
    """`kew epic run NAME` on an EMPTY epic errors (exit 2) and never enters the
    poll loop — an empty query is an operator problem, not `COMPLETE` (#819)."""
    cfg = _config(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=EmptyEpicGraphError(EPIC)),
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 2, invoked.stdout
    assert "No issues found for label" in invoked.output
    sleep_mock.assert_not_called()


def test_run_single_linear_unavailable_exits_2_classified_without_looping(
    tmp_path: Path,
) -> None:
    """`kew epic run NAME` on a Linear outage that outlasts the retry budget
    errors (exit 2) with the classified `linear-unavailable` diagnostic, never a
    traceback, and never enters the poll loop."""
    cfg = _config(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.cli.advance",
            side_effect=LinearUnavailableError(
                "Linear API connection reset: [Errno 104]"
            ),
        ),
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", EPIC])

    assert invoked.exit_code == 2, invoked.stdout
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit)
    assert "linear-unavailable" in invoked.output
    assert EPIC in invoked.output
    sleep_mock.assert_not_called()


def test_run_all_linear_unavailable_surfaced_and_continues(tmp_path: Path) -> None:
    """`--all` with one epic hitting a Linear outage surfaces its classified
    `linear-unavailable` diagnostic, marks it as needing an operator (worst exit
    1), and keeps driving the rest of the roster — never a traceback."""
    cfg = _config(tmp_path)
    good = _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE)

    def _advance(name: str, **_kw: Any) -> TickResult:
        if name == "down":
            raise LinearUnavailableError("Linear API connection reset: [Errno 104]")
        return good

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.run_store.SqliteRunStore.list_epic_refs",
            return_value=["down", "ok"],
        ),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert invoked.exit_code == 1, invoked.output
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit)
    assert "linear-unavailable" in invoked.output
    assert "down" in invoked.output
    # The healthy epic still ran to COMPLETE — the outage one did not abort it.
    assert "epic ok: COMPLETE" in invoked.output


def test_run_all_hard_linear_error_surfaced_and_continues(tmp_path: Path) -> None:
    """A non-transient Linear API error on one epic is a classified
    `linear-error` line (worst exit 1), never a traceback aborting the roster."""
    cfg = _config(tmp_path)
    good = _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE)

    def _advance(name: str, **_kw: Any) -> TickResult:
        if name == "down":
            raise LinearApiError("Linear API error: Cannot query field 'projectz'")
        return good

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.run_store.SqliteRunStore.list_epic_refs",
            return_value=["down", "ok"],
        ),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert invoked.exit_code == 1, invoked.output
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit)
    assert "epic down: linear-error" in invoked.output
    assert "epic ok: COMPLETE" in invoked.output


def test_run_all_empty_epic_surfaced_not_dropped(tmp_path: Path) -> None:
    """`--all` with one EMPTY epic must surface its diagnostic and mark it as
    needing an operator (worst exit), never silently drop it or hang the
    round-robin waiting for it to become terminal (#819)."""
    cfg = _config(tmp_path)
    good = _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE)

    def _advance(name: str, **_kw: Any) -> TickResult:
        if name == "empty":
            raise EmptyEpicGraphError("empty")
        return good

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.run_store.SqliteRunStore.list_epic_refs",
            return_value=["empty", "ok"],
        ),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert invoked.exit_code == 1, invoked.output
    assert "No issues found for label 'epic:empty'" in invoked.output
    # The healthy epic still ran to COMPLETE — the empty one did not abort it.
    assert "epic ok: COMPLETE" in invoked.output


def test_run_all_roster_never_grows_a_mistyped_epic_name(tmp_path: Path) -> None:
    """Reviewer finding (PR #2438). The roster is read from the real ledger
    here, not patched: a mistyped `kew epic advance typoo` leaves one
    `empty_graph` attempt row, and admitting that row as an epic made the typo
    a PERMANENT member of this roster — re-raising its empty graph as STALLED
    and a non-zero exit on every later `--all` run, with no way to remove it
    (nothing deletes an epic's attempt rows).
    """
    cfg = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.register_epic("real-epic")
        row = store.begin_epic_tick_log("typoo", "2026-07-02T00:00:00+00:00")
        store.finish_epic_tick_log(
            row,
            ts_finished="2026-07-02T00:00:01+00:00",
            outcome="empty_graph",
            error_class="EmptyEpicGraphError",
        )

    good = _tick_result(done=("1",), remaining=0, signal=TerminalSignal.COMPLETE)

    def _advance(name: str, **_kw: Any) -> TickResult:
        if name == "typoo":
            raise EmptyEpicGraphError("typoo")
        return good

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", side_effect=_advance),
        patch("kew.cli._sleep"),
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert invoked.exit_code == 0, invoked.output
    assert "1 epic(s) discovered" in invoked.output
    assert "typoo" not in invoked.output
    assert "epic real-epic: COMPLETE" in invoked.output


# ---------------------------------------------------------------------------
# `kew epic retry`
# ---------------------------------------------------------------------------


def test_retry_clears_label_marks_retry_and_preserves_failure_rows(
    tmp_path: Path,
) -> None:
    """Non-destructive retry (#427 Phase 2): the operator's retry marker
    supersedes the stale FAILED overlay without deleting the failed run's
    cost/token history — `kew report` must still be able to read it."""
    cfg = _config(tmp_path)
    db_path = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db_path))
    store.reserve_epic_node(EPIC, 42, "run-fail-1")
    store.save(
        RunRecord(
            run_id="run-fail-1",
            issue_num=42,
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    store.close()

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        # The label-clear effector now lives behind service.verbs (Group 3),
        # which calls labels.clear_failed_label -> kew.labels.run.
        patch("kew.labels.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        invoked = runner.invoke(epic_app, ["retry", EPIC, "42"])

    # The retry was applied; relaunching the driver is operator repair (#1943).
    assert invoked.exit_code == 1, invoked.stdout
    assert f"no live driver — relaunch required: kew epic run {EPIC}" in invoked.output
    run_mock.assert_called_once()
    call_args = run_mock.call_args[0][0]
    assert "--add-label" in call_args
    assert cfg.labels.ready in call_args
    assert "--remove-label" in call_args
    assert "agent-failed" in call_args
    assert "42" in call_args

    verify_store = SqliteRunStore(str(db_path))
    remaining = verify_store.list_by_issue(42)
    reservations = verify_store.list_epic_reservations(EPIC)
    retries = verify_store.list_epic_retries(EPIC)
    verify_store.close()
    # The stale failure run row is STILL PRESENT — non-destructive retry
    # preserves cost/token history for `kew report`.
    assert len(remaining) == 1
    assert remaining[0].run_id == "run-fail-1"
    assert reservations == []  # the stale reservation was released
    assert [r.issue_num for r in retries] == [42]  # retry marker recorded


def test_retry_active_driver_lock_avoids_dead_driver_diagnostic(
    tmp_path: Path,
) -> None:
    from kew.cli import _epic_lock_path
    from kew.graph.driver.lock import single_driver_lock

    cfg = _config(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.labels.run") as run_mock,
        single_driver_lock(_epic_lock_path(cfg, EPIC), holder=EPIC),
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        invoked = runner.invoke(epic_app, ["retry", EPIC, "42"])

    assert invoked.exit_code == 0, invoked.output
    assert "no live driver" not in invoked.output


def _seed_drive(db_path: Path, *, rotation: tuple[str, ...], age_s: float) -> None:
    """A `kew drive` holding the ledger's drive lock, sleeping between ticks
    with no per-epic driver lock, whose last heartbeat is ``age_s`` old."""
    store = SqliteRunStore(str(db_path))
    lease = acquire_drive(store)
    assert isinstance(lease, DriveLease)
    beat = DriveHeartbeat(
        token=lease.holder.token,
        pid=lease.holder.pid,
        host=lease.holder.host,
        at=datetime.now(UTC) - timedelta(seconds=age_s),
        every_s=10.0,
        stage="sleeping",
        rotation=rotation,
    )
    store.set_meta(DRIVE_HEARTBEAT_KEY, beat.encode())
    store.close()


def _retry_under_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rotation: tuple[str, ...],
    age_s: float,
) -> Any:
    """Run `kew epic retry` from a project whose kew.toml points the ledger at
    *tmp_path*. A stub `gh` on PATH satisfies the tool preflight; the label
    call it would make is faked."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{tmp_path}"\n'
    )
    monkeypatch.chdir(tmp_path)
    _seed_failed_reservation(tmp_path / "dispatch.db")
    _seed_drive(tmp_path / "dispatch.db", rotation=rotation, age_s=age_s)
    with (
        # Preflight registers the stub; put the real tool paths back after.
        patch.dict("kew.subprocess_utils.TOOL_PATHS"),
        patch("kew.labels.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        return runner.invoke(epic_app, ["retry", EPIC, "42"])


def test_retry_under_a_live_drive_that_rotates_the_epic_names_no_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sleeping `kew drive` holds no per-epic lock between ticks; the retry
    must say the drive picks the item up, never send the operator to start a
    second driver beside it (#2852)."""
    invoked = _retry_under_drive(
        tmp_path, monkeypatch, rotation=("other", EPIC), age_s=5
    )

    assert invoked.exit_code == 0, invoked.output
    assert "relaunch required" not in invoked.output
    assert "kew epic run" not in invoked.output
    assert "next tick" in invoked.output
    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    retries = store.list_epic_retries(EPIC)
    store.close()
    assert [r.issue_num for r in retries] == [42]


@pytest.mark.parametrize(
    ("rotation", "age_s"),
    [
        pytest.param(("other",), 5, id="epic-outside-the-rotation"),
        pytest.param((EPIC,), 3600, id="drive-heartbeat-stalled"),
    ],
)
def test_retry_under_a_drive_that_does_not_drive_the_epic_still_relaunches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rotation: tuple[str, ...],
    age_s: float,
) -> None:
    invoked = _retry_under_drive(tmp_path, monkeypatch, rotation=rotation, age_s=age_s)

    assert invoked.exit_code == 1, invoked.output
    assert f"no live driver — relaunch required: kew epic run {EPIC}" in invoked.output


def _seed_failed_reservation(db_path: Path) -> None:
    store = SqliteRunStore(str(db_path))
    store.reserve_epic_node(EPIC, 42, "run-fail-1")
    store.save(
        RunRecord(
            run_id="run-fail-1",
            issue_num=42,
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    store.close()


def test_retry_refuses_an_excluded_actor_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2552: the CLI binds the console's approver gate. An actor outside
    ``[approval].approvers`` gets the same refusal and no marker, event,
    reservation release or label edit."""
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    cfg = _config(tmp_path)
    cfg.approval.approvers = ["ada"]
    db_path = tmp_path / "dispatch.db"
    _seed_failed_reservation(db_path)

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.labels.run") as run_mock,
    ):
        invoked = runner.invoke(epic_app, ["retry", EPIC, "42"])

    assert invoked.exit_code == 2, invoked.output
    assert "'mallory' is not in [approval].approvers" in invoked.output
    run_mock.assert_not_called()
    verify_store = SqliteRunStore(str(db_path))
    try:
        assert verify_store.list_epic_retries(EPIC) == []
        assert [r.item_id for r in verify_store.list_epic_reservations(EPIC)] == ["42"]
        assert verify_store.list_audit_events(event="epic_node_retry") == []
    finally:
        verify_store.close()


def test_retry_by_an_approver_carries_the_bound_policy_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2552: the approver's retry event attests under the bound policy."""
    monkeypatch.setenv("KEW_ACTOR", "ada")
    cfg = _config(tmp_path)
    cfg.approval.approvers = ["ada"]
    db_path = tmp_path / "dispatch.db"
    _seed_failed_reservation(db_path)

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.labels.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        invoked = runner.invoke(epic_app, ["retry", EPIC, "42"])

    assert invoked.exit_code == 1, invoked.output  # applied; no live driver
    verify_store = SqliteRunStore(str(db_path))
    try:
        events = verify_store.list_audit_events(event="epic_node_retry")
        assert [r.item_id for r in verify_store.list_epic_retries(EPIC)] == ["42"]
    finally:
        verify_store.close()
    assert len(events) == 1
    assert events[0].actor_id == "ada"
    assert events[0].policy_hash


def test_retry_non_int_node_exits_2() -> None:
    invoked = runner.invoke(epic_app, ["retry", EPIC, "not-a-number"])
    assert invoked.exit_code == 2


def test_run_all_awaiting_merge_stays_on_roster_until_complete(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    outcomes = iter(
        [
            _tick_result(remaining=1, signal=TerminalSignal.AWAITING_MERGES),
            _tick_result(done=("1",), signal=TerminalSignal.COMPLETE),
        ]
    )
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.run_store.SqliteRunStore.list_epic_refs", return_value=[EPIC]),
        patch(
            "kew.cli.advance", side_effect=lambda *_a, **_kw: next(outcomes)
        ) as advance,
        patch("kew.cli._sleep") as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["run", "--all"])

    assert invoked.exit_code == 0, invoked.output
    assert advance.call_count == 2
    sleep_mock.assert_called_once()


# ---------------------------------------------------------------------------
# enriched `kew epic status` — read-only
# ---------------------------------------------------------------------------


def _linkage_summary() -> TickSummary:
    return TickSummary(
        counts={"todo": 1},
        frontier=("1",),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.PROGRESSING,
    )


def _invoke_status(tmp_path: Path, *, native_fields: bool) -> Any:
    cfg = _config(tmp_path)

    node = {
        "number": 1,
        "title": "t1",
        "url": "https://github.com/o/r/issues/1",
        "state": "OPEN",
        "stateReason": None,
        "body": "",
        "repository": {"nameWithOwner": REPO},
        "labels": {"nodes": []},
        "blockedBy": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": []},
        "parent": None,
        "subIssues": {"totalCount": 0},
    }

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if cmd[:3] == ["gh", "issue", "list"]:
            issue = dict(node)
            issue["labels"] = []
            return SimpleNamespace(returncode=0, stdout=json.dumps([issue]), stderr="")
        query = next((arg[6:] for arg in cmd if arg.startswith("query=")), "")
        if "blockedBy(first: 1)" in query:
            if native_fields:
                payload = {
                    "data": {
                        "repository": {
                            "issue": {
                                "blockedBy": {"totalCount": 0},
                                "subIssues": {"totalCount": 0},
                            }
                        }
                    }
                }
            else:
                payload = {
                    "errors": [
                        {"message": ("Field 'subIssues' doesn't exist on type 'Issue'")}
                    ]
                }
        elif "subIssues(first: 100" in query:
            payload = {
                "data": {
                    "repository": {
                        "issue": {
                            "subIssues": {
                                "nodes": [node],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        else:
            parent = dict(node)
            parent["number"] = 7
            parent["subIssues"] = {"totalCount": 1}
            payload = {
                "data": {
                    "repository": {
                        "issues": {
                            "nodes": [parent],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    source = GitHubNativeSource(repo=REPO, run=fake_run, probe_on_read=True)
    with (
        patch(
            "kew.cli._epic_driver_sources",
            return_value=(source, MagicMock(), None),
        ),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=_linkage_summary()),
    ):
        return runner.invoke(epic_app, ["status", EPIC])


def test_status_names_the_native_linkage_key_it_read(tmp_path: Path) -> None:
    """`epic status` says which key the epic was read from (W2.8a)."""
    invoked = _invoke_status(tmp_path, native_fields=True)
    assert invoked.exit_code == 0, invoked.output
    assert "[dispatch] Linkage: sub-issues of #7" in invoked.output


def test_status_says_why_it_fell_back_to_the_label_and_marker(
    tmp_path: Path,
) -> None:
    """A fallback that printed nothing would look like a repo with no epics."""
    invoked = _invoke_status(tmp_path, native_fields=False)
    assert invoked.exit_code == 0, invoked.output
    assert "[dispatch] Linkage: label epic:dag-1" in invoked.output
    assert "does not expose subIssues" in invoked.output


def test_status_shows_frontier_and_signal_without_dispatching(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    graph = Graph(
        [
            Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC),
            Node(id="2", title="t2", status=NodeStatus.TODO, epic_ref=EPIC),
        ],
        [Edge("1", "2")],
    )
    summary = TickSummary(
        counts={"todo": 2},
        frontier=("1",),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=2,
        signal=TerminalSignal.PROGRESSING,
    )
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary) as summarize_mock,
        patch("kew.cli.advance") as advance_mock,
    ):
        invoked = runner.invoke(epic_app, ["status", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "Ready to dispatch: ['1']" in invoked.stdout
    assert "progressing" in invoked.output.lower()
    summarize_mock.assert_called_once()
    advance_mock.assert_not_called()


def test_status_stuck_nodes_and_blast_radius_shown(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    graph = Graph(
        [Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC)],
        [],
    )
    stuck = (StuckNode(node_id="1", reason="failed-terminal", blast_radius=("2", "3")),)
    summary = TickSummary(
        counts={"failed": 1},
        frontier=(),
        in_flight=(),
        held=(),
        failed=("1",),
        cancelled=(),
        stuck=stuck,
        done=(),
        remaining=1,
        signal=TerminalSignal.STALLED,
    )
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
        patch("kew.cli.advance") as advance_mock,
    ):
        invoked = runner.invoke(epic_app, ["status", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "stalled" in invoked.output.lower()
    assert "2" in invoked.output and "3" in invoked.output
    advance_mock.assert_not_called()


def test_status_names_lock_holder_pid_epic_and_held_since(tmp_path: Path) -> None:
    """`kew epic status` names the driver lock's holder — pid, epic, held-since
    — when the epic's per-epic lock is held (the #861 transparency contract)."""
    import os

    from kew.cli import _epic_lock_path
    from kew.graph.driver.lock import single_driver_lock

    cfg = _config(tmp_path)
    graph = Graph(
        [Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC)],
        [],
    )
    summary = TickSummary(
        counts={"todo": 1},
        frontier=("1",),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.PROGRESSING,
    )
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
        single_driver_lock(_epic_lock_path(cfg, EPIC), holder=EPIC),
    ):
        invoked = runner.invoke(epic_app, ["status", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert f"pid={os.getpid()}" in invoked.output
    assert f"epic={EPIC}" in invoked.output
    assert "held-since=" in invoked.output


def test_blast_radius_str_renders_ids_via_item_ref() -> None:
    """Status-printer blast radii route ids through item_ref: GitHub numbers get
    ``#N``, Linear ids stay bare (#SFD-140)."""
    from kew.cli import _blast_radius_str

    assert _blast_radius_str(("1", "2")) == "[#1, #2]"
    assert _blast_radius_str(("SFD-133", "SFD-134")) == "[SFD-133, SFD-134]"
    assert _blast_radius_str(()) == "[]"


@pytest.fixture(autouse=True)
def _isolate_tool_paths(monkeypatch: pytest.MonkeyPatch) -> Any:
    from kew import subprocess_utils

    saved = subprocess_utils.TOOL_PATHS.copy()
    monkeypatch.setattr("kew.cli._preflight_runner", lambda _config: None)
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


# --------------------------------------------------------------------------- #
# #991: the CLI must construct a REAL reviewer launcher when the governed      #
# adversarial-review stage is on. This guards against reverting the epic.py    #
# wiring back to `reviewer_launcher=None` (the exact prior-review "built but    #
# unwired" gap): the production-wiring tests inject their OWN launcher into     #
# advance(), so ONLY this test fails if the CLI construction is removed.       #
# --------------------------------------------------------------------------- #
def _review_seams_passed_by_advance_epic(
    tmp_path: Path, *, adversarial: bool
) -> dict[str, Any]:
    from kew.cli import epic as _epic

    cfg = _config(tmp_path)
    cfg.review.adversarial = adversarial
    captured: dict[str, Any] = {}

    def _fake_advance(_name: str, **kwargs: Any) -> TickResult:
        captured["reviewer_launcher"] = kwargs.get("reviewer_launcher")
        captured["remediator"] = kwargs.get("remediator")
        return _tick_result()

    with (
        SqliteRunStore(str(tmp_path / "dispatch.db")) as store,
        patch.object(_epic._c, "advance", _fake_advance),
    ):
        _epic._advance_epic(
            "epic/x",
            config=cfg,
            store=store,
            deps=MagicMock(),
            facts=MagicMock(),
            repo=REPO,
            max_parallel=1,
            approvers=[],
            interval=0,
        )
    return captured


def _launcher_passed_by_advance_epic(tmp_path: Path, *, adversarial: bool) -> Any:
    return _review_seams_passed_by_advance_epic(tmp_path, adversarial=adversarial)[
        "reviewer_launcher"
    ]


def test_advance_epic_wires_a_real_launcher_when_adversarial_enabled(
    tmp_path: Path,
) -> None:
    launcher = _launcher_passed_by_advance_epic(tmp_path, adversarial=True)
    assert launcher is not None  # production path gets a live launcher, not None
    assert callable(launcher)


def test_advance_epic_passes_no_launcher_when_review_disabled(
    tmp_path: Path,
) -> None:
    assert _launcher_passed_by_advance_epic(tmp_path, adversarial=False) is None


def test_advance_epic_wires_a_real_remediator_when_adversarial_enabled(
    tmp_path: Path,
) -> None:
    """#1030: with the governed stage on, the CLI must pass a live remediator so a
    NEEDS_CHANGES node dispatches remediation instead of stalling in remediating
    forever. Guards against reverting the epic.py wiring back to ``None``."""
    remediator = _review_seams_passed_by_advance_epic(tmp_path, adversarial=True)[
        "remediator"
    ]
    assert remediator is not None
    assert callable(remediator)


def test_advance_epic_passes_no_remediator_when_review_disabled(
    tmp_path: Path,
) -> None:
    assert (
        _review_seams_passed_by_advance_epic(tmp_path, adversarial=False)["remediator"]
        is None
    )


# ---------------------------------------------------------------------------
# Regression: no shadow dispatch.db at CWD after an epic tick (#1037)
# ---------------------------------------------------------------------------


def test_no_shadow_dispatch_db_after_epic_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `kew epic advance`, the canonical dispatch.db is at the configured
    logging.dir subdirectory — no stray shadow file at the CWD.

    Guards against a default-path RunStore leaking a bare ./dispatch.db when
    the CWD differs from the configured logging.dir (#1037)."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    log_dir = tmp_path / "logs" / "dispatch"
    canonical_db = log_dir / "dispatch.db"
    shadow_db = tmp_path / "dispatch.db"

    cfg = DispatchConfig()
    cfg.project.repo = REPO
    cfg.logging.dir = str(log_dir)

    result = _tick_result(dispatched=("1",), signal=TerminalSignal.PROGRESSING)

    monkeypatch.chdir(tmp_path)

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=result),
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert canonical_db.exists(), (
        f"dispatch.db was not created at configured path {canonical_db}"
    )
    assert not shadow_db.exists(), (
        f"Stray shadow dispatch.db found at {shadow_db}; "
        f"canonical is {canonical_db} — a code path used a CWD-relative path"
    )
    all_dbs = list(tmp_path.rglob("dispatch.db"))
    assert all_dbs == [canonical_db], (
        f"Expected exactly one dispatch.db at {canonical_db}, found {all_dbs}"
    )


# --- Linear zero-change outcomes (I10B / Task 45) ---------------------------


@pytest.mark.policy
def test_the_linear_dispatch_child_wires_no_no_change_callback() -> None:
    """The legacy hook commented "no code changes required" on the item and
    closed it — a remote mutation decided by an empty diff, on a tracker where
    a close also arms the dependents. It is gone: the zero-change outcome is a
    durable local candidate, and nothing reaches Linear until a person (I11)
    authorizes it.

    Read off the syntax tree: the child must not name the hook in code, and the
    dispatch it calls must not accept one.
    """
    import inspect

    from kew.cli import epic as epic_cli
    from kew.dispatch_core import execute_dispatch

    assert not hasattr(epic_cli, "_close_linear_no_changes")
    child = source_ast.definition(epic_cli, "_dispatch_linear_node")
    assert "on_complete_no_changes" not in source_ast.identifiers(child)
    assert (
        "on_complete_no_changes" not in inspect.signature(execute_dispatch).parameters
    )


@pytest.mark.parametrize("has_closing_ref", [False, True, None])
def test_status_prints_the_pr_linkage_source_for_each_prd_node(
    tmp_path: Path,
    has_closing_ref: bool,
) -> None:
    """W2.6/#1741: an operator must be able to see when a node's PR is linked
    by the ledger or its branch — i.e. when the body keyword is gone."""
    cfg = _config(tmp_path)
    graph = Graph(
        [Node(id="7", title="t7", status=NodeStatus.IN_REVIEW, epic_ref=EPIC)],
        [],
    )
    summary = TickSummary(
        counts={"in_review": 1},
        frontier=(),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.AWAITING_MERGES,
        pr_links=(
            PrLink(
                node_id="7",
                pr_number=1723,
                source="ledger",
                has_closing_ref=has_closing_ref,
            ),
        ),
    )
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
    ):
        invoked = runner.invoke(epic_app, ["status", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert "PR linkage: #7->PR #1723 (ledger)" in invoked.output
    warning = "#7: PR #1723 lacks its GitHub closing keyword"
    # None = the provider cannot observe closing references (Linear): no warning.
    assert (warning in invoked.output) is (has_closing_ref is False)


def test_epic_abandon_command_routes_through_service(tmp_path):
    from kew.service import verbs

    store = _seed_resume_store(tmp_path)
    config = _config(tmp_path)
    verb = getattr(verbs, "epic_abandon", None)
    assert callable(verb), "CLI abandonment requires the service verb"
    try:
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.service_verbs.epic_abandon", wraps=verb) as abandon,
        ):
            result = runner.invoke(
                epic_app, ["abandon", EPIC, "42", "--reason", "cost breaker"]
            )
        assert result.exit_code == 0, result.output
        assert abandon.call_count == 1
        assert abandon.call_args.args == (store,)
        kwargs = abandon.call_args.kwargs
        assert {k: kwargs[k] for k in ("epic_ref", "item_id", "reason", "config")} == {
            "epic_ref": EPIC,
            "item_id": "42",
            "reason": "cost breaker",
            "config": config,
        }
        # The CLI injects the checkout claim guard (the service layer must not
        # import kew.worktree: import contract).
        assert callable(kwargs["checkout_guard"])
        assert "fresh dispatch on the next tick" in result.output
        assert store.list_continuations(EPIC)[0].state == "terminal"
    finally:
        store.close()


@pytest.mark.parametrize("verb", ["continue", "abandon"])
@pytest.mark.parametrize("cancelled_count", [1, 40])
def test_cli_recovers_after_dropped_claim_audit(
    tmp_path, monkeypatch, verb, cancelled_count
):
    from kew.service import verbs

    store = _seed_resume_store(tmp_path)
    config = _config(tmp_path)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                store, "_chained_insert_in_transaction", lambda *a, **kw: None
            )
            for _ in range(cancelled_count):
                with pytest.raises(RuntimeError, match="not durably recorded"):
                    verbs.epic_resume(store, epic_ref=EPIC, item_id="42", config=config)
        assert store.list_audit_events(event="continuation_released") == []
        assert store.list_audit_events(event="continuation_claimed") == []
        assert store.list_continuations(EPIC)[0].generation == 0
        before = store.read_chain_rows()
        argv = [verb, EPIC, "42"]
        if verb == "abandon":
            argv += ["--reason", "stop"]
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch("kew.cli.select_next_issue", return_value={"number": 42}),
            patch(
                "kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0)
            ) as launch,
        ):
            result = runner.invoke(epic_app, argv)
        assert result.exit_code == 0, result.output
        assert launch.call_count == (1 if verb == "continue" else 0)
        record = store.list_continuations(EPIC)[0]
        assert record.state == "terminal"
        assert record.generation == (1 if verb == "continue" else 0)
        store.require_continuation_owner_history(record)
        assert store.read_chain_rows()[: len(before)] == before
    finally:
        store.close()


@pytest.mark.parametrize(
    "case,code,message",
    [
        ("continuing", 1, "owned by another caller"),
        ("terminal", 1, "terminal state"),
        ("not-awaiting", 1, "no preserved continuation"),
        ("unknown-epic", 2, "unknown epic"),
        ("unknown-node", 2, "unknown node"),
        ("authority", 2, "authority"),
        ("no-ledger", 2, "no ledger"),
        ("blank-reason", 2, "non-empty"),
        ("missing-reason", 2, "Missing option"),
    ],
)
def test_epic_abandon_exit_codes(tmp_path, monkeypatch, case, code, message):
    from dataclasses import replace

    store = _seed_resume_store(
        tmp_path,
        state="continuing" if case == "continuing" else "awaiting_continuation",
    )
    config = _config(tmp_path)
    epic, node = EPIC, "42"
    if case == "terminal":
        args = (
            EPIC,
            "42",
            "original-run",
            "agent/issue-42",
            tmp_path / "worktrees" / "agent" / "issue-42",
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
        assert store.finalize_continuation(*args, 1, "owner")
        store.save(replace(store.get("original-run"), run_id="owner", status="success"))
    elif case == "not-awaiting":
        node = "43"
        store.append_epic_tick(
            epic_ref=EPIC,
            tick_ts=datetime.now(UTC).isoformat(),
            snapshot={"nodes": [{"id": node}]},
            actions={},
            snapshot_hash="known",
        )
    elif case == "unknown-epic":
        epic = "missing"
    elif case == "unknown-node":
        node = "99"
    elif case == "authority":
        config.approval.approvers = ["ada"]
        monkeypatch.setenv("KEW_ACTOR", "unlisted")
    elif case == "no-ledger":
        config.logging.dir = str(tmp_path / "missing")
    argv = ["abandon", epic, node]
    if case != "missing-reason":
        argv.extend(["--reason", " \t" if case == "blank-reason" else "fresh start"])
    before = (
        store.read_chain_rows(),
        store.list_continuations(EPIC),
        store.list_epic_reservations(EPIC),
    )
    try:
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
        ):
            result = runner.invoke(epic_app, argv)
        assert result.exit_code == code, result.output
        assert message in result.output
        assert before == (
            store.read_chain_rows(),
            store.list_continuations(EPIC),
            store.list_epic_reservations(EPIC),
        )
    finally:
        store.close()


@pytest.mark.parametrize("publication", ["canonical", "checkout"])
@pytest.mark.parametrize(
    "owner", ["pgid", "container", "unknown", "missing", "probe-error"]
)
def test_epic_abandon_refuses_live_or_unproven_owner(
    tmp_path, monkeypatch, publication, owner
):
    import subprocess
    import sys
    from dataclasses import replace

    from support.process_identity import fixture_child_identity

    from kew import process_groups

    config = _config(tmp_path)
    config.publication.publish = publication
    store = _seed_resume_store(tmp_path)
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    worktree.mkdir(parents=True)
    edits = worktree / "edits.txt"
    edits.write_text("preserved work")
    store.reserve_epic_node_item(EPIC, "42", "original-run")
    process = None
    try:
        original = store.get("original-run")
        if owner == "pgid":
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=True,
            )
            fixture_child_identity(monkeypatch, process)
            store.save(
                replace(
                    original,
                    pgid=process.pid,
                    owner_pid=process.pid,
                    attempt_pgid=process.pid,
                    **process_groups.identity_at_admission(process.pid),
                ),
                update_ownership=True,
            )
        elif owner in {"container", "probe-error"}:
            store.save(
                replace(
                    original, container_name="kew-live-owner", container_id="a" * 64
                ),
                update_ownership=True,
            )
            monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda _: "docker")
            probe = MagicMock(
                return_value=subprocess.CompletedProcess(
                    [],
                    0 if owner == "container" else 1,
                    stdout=("a" * 64 + " /kew-live-owner true")
                    if owner == "container"
                    else "",
                    stderr="" if owner == "container" else "daemon unavailable",
                )
            )
            monkeypatch.setattr("kew.stop_recovery._tool", lambda name: name)
            monkeypatch.setattr("kew.stop_recovery.subprocess.run", probe)
        elif owner == "missing":
            store._execute("DELETE FROM runs WHERE run_id = ?", ("original-run",))
            store._conn.commit()
        else:
            store.save(replace(original, status="pending", finished_at=None))
        before = (
            store.list_continuations(EPIC),
            store.list_epic_reservations(EPIC),
            store.list_epic_retries(EPIC),
            store.read_chain_rows(),
        )
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
        ):
            result = runner.invoke(
                epic_app, ["abandon", EPIC, "42", "--reason", "stop"]
            )
        assert result.exit_code == 1, result.output
        assert "live or unproven owner" in result.output
        assert before == (
            store.list_continuations(EPIC),
            store.list_epic_reservations(EPIC),
            store.list_epic_retries(EPIC),
            store.read_chain_rows(),
        )
        assert edits.read_text() == "preserved work"
        if owner in {"container", "probe-error"}:
            probe.assert_called_once()
            assert probe.call_args.args[0][-1] == "a" * 64
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
            # Positive process death permits the same operation with real probes.
            with (
                patch("kew.cli.load_config", return_value=config),
                patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            ):
                result = runner.invoke(
                    epic_app, ["abandon", EPIC, "42", "--reason", "stop"]
                )
            assert result.exit_code == 0, result.output
            assert store.list_continuations(EPIC)[0].state == "terminal"
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        store.close()


@pytest.mark.serial
def test_abandon_refuses_worker_spawned_before_pid_recording(tmp_path, monkeypatch):
    import subprocess
    import sys
    from dataclasses import replace

    from kew.audit import Actor, emit
    from kew.fleet import stop_fleet
    from kew.graph.driver.tick import reconcile_epic_reservations

    config = _config(tmp_path)
    store = _seed_resume_store(tmp_path)
    checkout = tmp_path / "worktrees" / "agent" / "issue-42"
    checkout.mkdir(parents=True)
    edits = checkout / "edits.txt"
    edits.write_text("orphan worker owns this checkout")
    original = store.get("original-run")
    store.save(replace(original, status="pending", exit_code=0, finished_at=None))
    store.reserve_epic_node_item(EPIC, "42", original.run_id)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        # The parent dies here, before either durable PID channel is written.
        report = stop_fleet(store, config, reason="recover crashed dispatch")
        assert report.killed == []
        assert report.retained
        assert store.get(original.run_id).status == "pending"
        # A legacy external terminalizer still cannot establish worker death.
        assert store.finalize_orphaned_pending(
            original.run_id,
            error_category="killed",
            finished_at=datetime.now(UTC),
            expected_owner=store.get(original.run_id),
        )
        assert store.get(original.run_id).status == "killed"
        assert (
            reconcile_epic_reservations(EPIC, store, config, now=datetime.now(UTC))
            == set()
        )
        assert process.poll() is None
        before = (store.list_continuations(EPIC), store.read_chain_rows())
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
        ):
            argv = ["abandon", EPIC, "42", "--reason", "recover stopped work"]
            refused = runner.invoke(epic_app, argv)
            assert refused.exit_code == 1, refused.output
            assert "original-run" in refused.output
            assert "no recorded worker identity" in refused.output
            assert "dispatch_completed" in refused.output
            assert before == (store.list_continuations(EPIC), store.read_chain_rows())
            assert edits.read_text() == "orphan worker owns this checkout"
            process.terminate()
            exit_code = process.wait(timeout=5)
            emit(
                store,
                "dispatch_completed",
                actor=Actor("policy", "dispatch"),
                run_id=original.run_id,
                payload={
                    "attempt_number": original.attempt_number,
                    "exit_code": exit_code,
                },
            )
            completed = runner.invoke(epic_app, argv)
            assert completed.exit_code == 0, completed.output
        assert store.list_continuations(EPIC)[0].state == "terminal"
        assert len(store.list_audit_events(event="continuation_abandoned")) == 1
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        store.close()


def test_missing_runtime_socket_refuses_resume_and_abandon(tmp_path, monkeypatch):
    import subprocess
    from dataclasses import replace

    import typer

    import kew.cli.epic as epic_cli

    config = _config(tmp_path)
    store = _seed_resume_store(tmp_path)
    try:
        store.save(
            replace(
                store.get("original-run"),
                container_name="kew-owner",
                container_id="a" * 64,
            ),
            update_ownership=True,
        )
        monkeypatch.setattr(
            "kew.sandbox.detect_runtime_cached", lambda _: "/fake/docker"
        )
        monkeypatch.setattr(
            "kew.subprocess_utils.run",
            lambda *a, **k: subprocess.CompletedProcess(
                [],
                1,
                "",
                "dial unix /var/run/docker.sock: connect: no such file or directory",
            ),
        )
        monkeypatch.setattr("kew.stop_recovery._tool", lambda name: name)
        monkeypatch.setattr(
            "kew.stop_recovery.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(
                [],
                1,
                "",
                "dial unix /var/run/docker.sock: connect: no such file or directory",
            ),
        )
        monkeypatch.setattr(epic_cli, "_validate_resume_worktree", lambda *a: None)
        before = (store.list_continuations(EPIC), store.read_chain_rows())
        with pytest.raises(typer.Exit) as refused:
            epic_cli._prepare_resume_record(
                store, epic_ref=EPIC, item_id="42", config=config
            )
        assert refused.value.exit_code == 2
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
        ):
            result = runner.invoke(
                epic_app, ["abandon", EPIC, "42", "--reason", "stop"]
            )
        assert result.exit_code == 1, result.output
        assert "live or unproven owner" in result.output
        assert before == (store.list_continuations(EPIC), store.read_chain_rows())
    finally:
        store.close()


@pytest.mark.parametrize("verb", ["abandon", "continue"])
def test_continuation_cli_reports_sqlite_failure(tmp_path, verb):
    import sqlite3

    config = _config(tmp_path)
    store = _seed_resume_store(tmp_path)
    before = (
        store.list_continuations(EPIC),
        store.list_epic_reservations(EPIC),
        store.list_epic_retries(EPIC),
        store.read_chain_rows(),
    )
    target = (
        "_chained_insert_in_transaction"
        if verb == "abandon"
        else "claim_continuation_under_cap"
    )
    argv = [verb, EPIC, "42"]
    if verb == "abandon":
        argv += ["--reason", "stop"]
    try:
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch.object(
                store, target, side_effect=sqlite3.OperationalError("disk I/O error")
            ) as append,
        ):
            result = runner.invoke(epic_app, argv)
        append.assert_called_once()
        assert result.exit_code == 2, result.output
        assert "disk I/O error" in result.output
        assert str(tmp_path / "dispatch.db") in result.output
        assert "Check disk space, permissions, and ledger integrity" in result.output
        assert before == (
            store.list_continuations(EPIC),
            store.list_epic_reservations(EPIC),
            store.list_epic_retries(EPIC),
            store.read_chain_rows(),
        )
    finally:
        store.close()


@pytest.mark.parametrize("raw,launched,expected", [(137, True, 1), (3, False, 2)])
def test_round_five_epic_resume_translates_raw_code(tmp_path, raw, launched, expected):
    config = _config(tmp_path)
    store = _seed_resume_store(tmp_path)
    outcome = DispatchOutcome(
        raw,
        launched=launched,
        category="github_auth" if not launched else "runner_failed",
    )
    try:
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch(
                "kew.cli.epic._run_resume_continuation", return_value=outcome
            ) as resume,
        ):
            result = runner.invoke(epic_app, ["continue", EPIC, "42"])
        assert result.exit_code == expected, result.output
        resume.assert_called_once()
        assert outcome.exit_code == raw
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["query", "refusal"])
def test_round_six_epic_resume_exception_translation(tmp_path, failure):
    from kew.selector import GhQueryError

    config = _config(tmp_path)
    store = _seed_resume_store(tmp_path)
    error = (
        GhQueryError("HTTP 503")
        if failure == "query"
        else RuntimeError("continuation refused")
    )
    try:
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value=REPO),
            patch("kew.cli.build_notifier", return_value=MagicMock()),
            patch("kew.cli._open_run_store", return_value=_StoreCtx(store)),
            patch("kew.cli.epic._validate_resume_worktree"),
            patch("kew.cli.epic._resume_owner_is_dead", return_value=True),
            patch("kew.cli.epic.resume_continuation", side_effect=error),
        ):
            result = runner.invoke(epic_app, ["continue", EPIC, "42"])
        assert result.exit_code == 2, result.output

        import typer

        from kew.cli.epic import _run_resume_continuation

        def fail_after_dispatch(claimed, *, store, dispatch):
            dispatch("42", tmp_path, "agent/issue-42", "continuation")
            raise RuntimeError("finalization failed")

        with (
            patch(
                "kew.cli.epic._resume_dispatch_callback",
                return_value=MagicMock(
                    return_value=DispatchOutcome(137, launched=True)
                ),
            ),
            patch("kew.cli.epic.resume_continuation", side_effect=fail_after_dispatch),
            pytest.raises(typer.Exit) as exit_info,
        ):
            _run_resume_continuation(
                MagicMock(),
                store=store,
                config=config,
                notifier=MagicMock(),
                epic_ref=EPIC,
                provider="github",
            )
        assert exit_info.value.exit_code == 1
    finally:
        store.close()


@pytest.mark.parametrize("failure_stage", ["bookkeeping", "release"])
@pytest.mark.parametrize("launched", [True, None])
def test_round_twelve_continuation_keeps_evidence_before_collection(
    tmp_path, capsys, caplog, failure_stage, launched
):
    from types import SimpleNamespace

    import typer

    from kew import dispatch_core
    from kew.cli.epic import _finish_resume_continuation, _run_resume_continuation
    from kew.tracking import DispatchResult, LaunchState

    config = _config(tmp_path)
    work = tmp_path / "unfinished.txt"
    work.write_text("attempt work")
    rollback = MagicMock()
    cleanup = MagicMock()

    # The confirmed pre-launch control and the executed case share the pipeline.
    for evidence, expected in ((False, 2), (launched, 1)):
        state = LaunchState()
        ctx = SimpleNamespace(
            config=config,
            store=None,
            run_id="continuation",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            epic_ref=EPIC,
            launch_state=state,
            refuse_rollback=rollback,
            expected_reservation_run_id=None,
            expected_reservation_generation=None,
            admitted_run_id=None,
            on_admitted=None,
            claim_launch_started=False,
            container_cleanup_pending=False,
            # The branch's checkout-binding fields, at their dataclass defaults.
            checkout_generation="",
            checkout_binding=None,
            checkout_abandonment=None,
        )
        result = DispatchResult(137, 42, tmp_path / "run.log", launched=evidence)
        launch = dispatch_core._LaunchResult(result=result, worktree_dir=tmp_path)
        error = OSError("terminal bookkeeping unavailable")

        def dispatch(item, worktree, branch, run_id):
            return dispatch_core.execute_dispatch(issue={}, config=config)

        def resume(claimed, *, store, dispatch):
            return dispatch("42", tmp_path, "agent/issue-42", "continuation")

        with (
            patch("kew.cli.epic._resume_dispatch_callback", return_value=dispatch),
            patch("kew.cli.epic.resume_continuation", side_effect=resume),
            patch.object(dispatch_core, "_prepare_dispatch_context", return_value=ctx),
            patch.object(
                dispatch_core, "_run_admission_gates", return_value=(None, None)
            ),
            patch.object(dispatch_core, "_run_approval_gate", return_value=None),
            patch.object(dispatch_core, "_check_connectivity_gate", return_value=None),
            patch.object(dispatch_core, "select_publication_mode"),
            patch.object(dispatch_core, "_launch_runner", return_value=launch),
            patch.object(
                dispatch_core, "_root_checkout_integrity_check", return_value=None
            ),
            patch.object(dispatch_core, "_try_rescue_incomplete", return_value=False),
            patch.object(
                dispatch_core,
                "_publish_terminal_failure",
                side_effect=error if failure_stage == "bookkeeping" else None,
            ) as record,
            patch.object(dispatch_core, "_print_failure_next_steps"),
            patch.object(dispatch_core, "_notify_dispatch_failure"),
            patch.object(
                dispatch_core,
                "release_checkout_claims",
                side_effect=error if failure_stage == "release" else None,
            ) as release,
            patch.object(dispatch_core, "cleanup_worktree", cleanup),
            pytest.raises(typer.Exit) as stopped,
        ):
            outcome = _run_resume_continuation(
                MagicMock(),
                store=MagicMock(),
                config=config,
                notifier=MagicMock(),
                epic_ref=EPIC,
                provider="github",
            )
            _finish_resume_continuation("42", outcome)
        assert stopped.value.exit_code == expected
        assert "terminal bookkeeping unavailable" in (
            capsys.readouterr().err + caplog.text
        )
        record.assert_called_once()
        release.assert_called_once()
        rollback.assert_not_called()
        cleanup.assert_not_called()
        assert work.read_text() == "attempt work"
        assert state.launched is evidence


@pytest.mark.parametrize("boundary", ["attached", "returned", "observed"])
def test_round_twelve_unknown_continuation_is_executed(tmp_path, boundary):
    import typer

    from kew.cli.epic import _finish_resume_continuation, _run_resume_continuation
    from kew.tracking import DispatchResult

    # False remains a genuine refusal; UNKNOWN must retain executed semantics.
    for evidence, expected in ((False, 2), (None, 1)):
        result = DispatchResult(137, 42, tmp_path / "run.log", launched=evidence)
        error = OSError("continuation finalization failed")
        error.dispatch_result = result

        def resume(claimed, *, store, dispatch, error=error):
            if boundary == "observed":
                dispatch("42", tmp_path, "agent/issue-42", "continuation")
                raise OSError("continuation finalization failed")
            raise error

        with (
            patch(
                "kew.cli.epic._resume_dispatch_callback",
                return_value=MagicMock(return_value=result),
            ),
            patch("kew.cli.epic.resume_continuation", side_effect=resume),
            pytest.raises(typer.Exit) as stopped,
        ):
            if boundary == "returned":
                _finish_resume_continuation("42", result)
            else:
                _run_resume_continuation(
                    MagicMock(),
                    store=MagicMock(),
                    config=_config(tmp_path),
                    notifier=MagicMock(),
                    epic_ref=EPIC,
                    provider="github",
                )
        assert stopped.value.exit_code == expected
        assert result.exit_code == 137


@pytest.mark.parametrize("legacy_recovery", [False, True])
def test_round20_status_reads_retained_claims_after_restart(tmp_path, legacy_recovery):
    from kew.claim_retention import LINEAR_CLAIM_RECOVERY, record_retained_claim

    cfg = _config(tmp_path)
    graph = Graph(
        [
            Node(id="SFD-42", title="retained", status=NodeStatus.TODO, epic_ref=EPIC),
            Node(id="SFD-43", title="other", status=NodeStatus.TODO, epic_ref=EPIC),
        ],
        [],
    )
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(db_path) as store:
        record_retained_claim(store, "SFD-42", reason="obsolete reason", epic_ref=EPIC)
        record_retained_claim(store, "SFD-43", reason="closed-unmerged")
        record_retained_claim(store, "SFD-99", reason="outside this epic")
        store.append_audit_event(
            timestamp=datetime.now(UTC).isoformat(),
            event="linear_claim_retained",
            item_id="SFD-42",
            payload={
                "reason": "post-admission refusal",
                "epic_ref": EPIC,
                "recovery": "run kew epic resume/abandon"
                if legacy_recovery
                else LINEAR_CLAIM_RECOVERY,
            },
        )
        before = store.list_audit_events(event="linear_claim_retained")
    # The command opens its own connection after the writer closes.
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._epic_driver_sources", return_value=(None, None, None)),
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.prime_issue_snapshots_from"),
        patch("kew.cli.summarize", return_value=_linkage_summary()),
        patch("kew.cli.advance") as advance,
    ):
        result = runner.invoke(epic_app, ["status", EPIC])
    assert result.exit_code == 0, result.output
    assert "retained claim: SFD-42: post-admission refusal" in result.output
    assert "retained claim: SFD-43: closed-unmerged" in result.output
    assert result.output.count(LINEAR_CLAIM_RECOVERY) == 2
    assert "SFD-99" not in result.output
    assert "obsolete reason" not in result.output
    assert "resume/abandon" not in result.output
    advance.assert_not_called()
    with SqliteRunStore(db_path, read_only=True) as store:
        assert store.list_audit_events(event="linear_claim_retained") == before


# ---------------------------------------------------------------------------
# `kew epic watch` — one operator-attention event stream (#2150)
# ---------------------------------------------------------------------------


def _watch_summary(**overrides: Any) -> TickSummary:
    base: dict[str, Any] = dict(
        counts={},
        frontier=(),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.STALLED,
        waiting=(),
    )
    base.update(overrides)
    return TickSummary(**base)


def _invoke_watch(
    tmp_path: Path,
    summaries: list[TickSummary],
    *args: str,
    sleeps: list[Any] | None = None,
) -> Any:
    """Drive `kew epic watch` over scripted summaries.

    `sleeps` scripts the patched inter-poll sleep, which is how a test ends a
    `--follow` watch that no longer exits on its first event: the last entry
    raises `KeyboardInterrupt`, standing in for the operator's Ctrl-C.
    """
    cfg = _config(tmp_path)
    graph = Graph([Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC)], [])
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", side_effect=summaries),
        patch("kew.cli.advance") as advance_mock,
        patch("kew.cli._sleep", side_effect=sleeps) as sleep_mock,
    ):
        invoked = runner.invoke(epic_app, ["watch", EPIC, *args])
    advance_mock.assert_not_called()
    invoked.sleep_mock = sleep_mock  # type: ignore[attr-defined]
    return invoked


def test_watch_json_emits_one_event_per_waiting_item(tmp_path: Path) -> None:
    """The five scattered stalls become one stream: one JSON object per line,
    each naming the item and the action (#2150)."""
    summary = _watch_summary(
        held=("2", "3"),
        waiting=(
            StuckNode("1", "in_review — awaiting merge (PR #7)", ()),
            StuckNode("2", "review_budget: pool spent", ()),
            StuckNode("3", "ci_pending", ()),
        ),
    )
    invoked = _invoke_watch(tmp_path, [summary], "--json")

    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["merge_ready", "budget_exhausted"]
    assert [e["item"] for e in events] == ["1", "2"]
    assert all(e["action"] and e["epic"] == EPIC for e in events)


def test_watch_exits_zero_and_says_so_when_nobody_is_waited_on(
    tmp_path: Path,
) -> None:
    """An empty stream must SAY it is empty, never print nothing (#2150)."""
    summary = _watch_summary(signal=TerminalSignal.PROGRESSING)
    invoked = _invoke_watch(tmp_path, [summary])

    assert invoked.exit_code == 0, invoked.output
    assert "nothing is waiting on a person" in invoked.output


def test_watch_reports_a_complete_epic_as_one_event(tmp_path: Path) -> None:
    summary = _watch_summary(signal=TerminalSignal.COMPLETE, remaining=0)
    invoked = _invoke_watch(tmp_path, [summary], "--json")

    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["epic_complete"]
    assert events[0]["item"] == EPIC


def test_watch_follow_keeps_running_after_the_event_it_emits(tmp_path: Path) -> None:
    """`--follow` exited on its first event, so it did not follow — an operator
    had to restart it in a loop (#2183). It now polls on past the event."""
    quiet = _watch_summary(signal=TerminalSignal.PROGRESSING)
    holding = _watch_summary(
        held=("1",),
        waiting=(StuckNode("1", "sandbox_containment: no image mapped", ()),),
    )
    invoked = _invoke_watch(
        tmp_path,
        [quiet, holding, quiet],
        "--json",
        "--follow",
        sleeps=[None, None, KeyboardInterrupt()],
    )

    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["sandbox_refused"]
    assert invoked.sleep_mock.call_count == 3
    assert invoked.sleep_mock.call_args_list[0].args == (30.0,)


def test_watch_follow_emits_each_state_once_and_a_new_head_again(
    tmp_path: Path,
) -> None:
    """Restarting a one-shot `--follow` re-emitted every still-open state, and
    de-duplicating on (item, kind, reason) in the wrapper then dropped a REAL
    second event: #2177 went READY at one head, failed the merge verify, was
    fixed and went READY again at another (#2183). One process now emits the
    unchanged state once, and the new head as its own event."""
    reason = "in_review — awaiting merge (PR #2177)"

    def ready_at(head: str) -> TickSummary:
        return _watch_summary(
            waiting=(StuckNode("77", reason, ()),),
            identity={"77": f"head:{head} round:1"},
        )

    invoked = _invoke_watch(
        tmp_path,
        [ready_at("c0404f4d"), ready_at("c0404f4d"), ready_at("7a40744b")],
        "--json",
        "--follow",
        sleeps=[None, None, KeyboardInterrupt()],
    )

    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["merge_ready", "merge_ready"]
    assert [e["identity"] for e in events] == [
        "head:c0404f4d round:1",
        "head:7a40744b round:1",
    ]


def test_watch_follow_re_emits_a_state_that_cleared_and_came_back(
    tmp_path: Path,
) -> None:
    """A recurrence is a second thing for a person. `sandbox_containment` carries
    no identity, so an accumulating memory of every state ever seen silenced the
    refusal that came back after the operator unheld the node — for the whole
    life of the watch (#2183)."""
    refused = _watch_summary(
        held=("1",),
        waiting=(StuckNode("1", "sandbox_containment: no image mapped", ()),),
    )
    quiet = _watch_summary(signal=TerminalSignal.PROGRESSING)

    invoked = _invoke_watch(
        tmp_path,
        [refused, quiet, refused],
        "--json",
        "--follow",
        sleeps=[None, None, KeyboardInterrupt()],
    )

    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["sandbox_refused", "sandbox_refused"]
    assert [e["identity"] for e in events] == ["", ""]


def test_watch_follow_says_nothing_is_waiting_once_not_every_poll(
    tmp_path: Path,
) -> None:
    """The empty-stream line is operator feedback that the watch is live, not a
    per-interval refrain that would bury the events it exists to show."""
    quiet = _watch_summary(signal=TerminalSignal.PROGRESSING)
    invoked = _invoke_watch(
        tmp_path, [quiet, quiet], "--follow", sleeps=[None, KeyboardInterrupt()]
    )

    assert invoked.exit_code == 0, invoked.output
    assert invoked.output.count("nothing is waiting on a person") == 1


def test_watch_follow_says_why_it_gave_up_at_the_timeout(tmp_path: Path) -> None:
    """A timed-out watch names the timeout rather than exiting clean-silent."""
    quiet = _watch_summary(signal=TerminalSignal.PROGRESSING)
    invoked = _invoke_watch(
        tmp_path, [quiet], "--follow", "--timeout", "0.0001", "--interval", "0"
    )

    assert invoked.exit_code == 0, invoked.output
    assert "no operator-attention event within" in invoked.output


def test_watch_reports_an_aged_out_stall_instead_of_exiting_clean(
    tmp_path: Path,
) -> None:
    """A stall clock that expires moves the row from `waiting` into `stuck`
    (`review-stalled`, `ci-stalled`, an aged hold). The epic is *more* stuck
    than before, so `watch` must not answer "nothing is waiting on a person"
    with exit 0 — the exact silence #2150 exists to end.
    """
    summary = _watch_summary(
        stuck=(
            StuckNode("1", "review-stalled", ()),
            StuckNode("2", "sandbox_containment: no image mapped", ()),
        ),
    )
    invoked = _invoke_watch(tmp_path, [summary], "--json")

    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["merge_ready", "sandbox_refused"]
    assert [e["item"] for e in events] == ["1", "2"]


def test_watch_never_dispatches_or_merges(tmp_path: Path) -> None:
    """`watch` is a read, exactly like `status` — it shares the same reader."""
    summary = _watch_summary(held=("1",), waiting=(StuckNode("1", "review_crash", ()),))
    invoked = _invoke_watch(tmp_path, [summary])

    assert invoked.exit_code == 1, invoked.output
    assert "held 1" in invoked.output
    assert "kew epic unhold" in invoked.output


# ---------------------------------------------------------------------------
# `kew epic watch --follow` — memory that survives a restart (#2202)
# ---------------------------------------------------------------------------

WATCH_MERGE_REASON = "in_review — awaiting merge (PR #2177)"


def _ready_at(identity: str) -> TickSummary:
    """A merge-ready node whose state identity is `identity`."""
    return _watch_summary(
        waiting=(StuckNode("77", WATCH_MERGE_REASON, ()),),
        identity={"77": identity},
    )


def _follow_once(tmp_path: Path, summary: TickSummary, *args: str) -> Any:
    """One `--follow` process that polls once and takes the operator's Ctrl-C."""
    return _invoke_watch(
        tmp_path, [summary], "--json", "--follow", *args, sleeps=[KeyboardInterrupt()]
    )


def _watch_events(invoked: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("changed", ["head:7a40744b round:1", "head:c0404f4d round:2"])
def test_watch_follow_remembers_emitted_identities_across_restarts(
    tmp_path: Path, changed: str
) -> None:
    """Every restart re-emitted every still-open state, so the operator wrapper
    kept its own state file (#2202). `--follow` now records the identity it
    emitted per item under the epic's state directory: the same state on the
    next process is silent, a new head or round is a new event."""
    first = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))
    again = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))
    moved = _follow_once(tmp_path, _ready_at(changed))

    assert [e["identity"] for e in _watch_events(first)] == ["head:c0404f4d round:1"]
    assert first.exit_code == 1, first.output
    assert _watch_events(again) == []
    assert again.exit_code == 0, again.output
    assert [e["identity"] for e in _watch_events(moved)] == [changed]
    assert moved.exit_code == 1, moved.output


def test_watch_follow_says_it_is_watching_states_it_already_reported(
    tmp_path: Path,
) -> None:
    """A restart into a fully-remembered epic prints no event — so it must say
    WHY it is quiet, or an operator reads a live watch as a broken one."""
    _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))
    again = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert "1 open state already reported" in again.output
    assert "nothing is waiting on a person" not in again.output


def test_watch_follow_forgets_a_cleared_state_so_its_return_is_a_new_event(
    tmp_path: Path,
) -> None:
    """The in-process rule holds across restarts: a state that CLEARS and later
    RECURS is a second thing for a person, and a `sandbox_containment` refusal
    carries no identity to tell the two apart (#2183)."""
    refused = _watch_summary(
        held=("1",),
        waiting=(StuckNode("1", "sandbox_containment: no image mapped", ()),),
    )
    quiet = _watch_summary(signal=TerminalSignal.PROGRESSING)

    _follow_once(tmp_path, refused)
    cleared = _follow_once(tmp_path, quiet)
    returned = _follow_once(tmp_path, refused)

    assert cleared.exit_code == 0, cleared.output
    assert [e["kind"] for e in _watch_events(returned)] == ["sandbox_refused"]
    assert returned.exit_code == 1, returned.output


def test_watch_follow_replay_emits_every_open_state_again(tmp_path: Path) -> None:
    """`--replay` is the escape hatch from the memory: every open state once,
    exactly as a watch with no memory behaved."""
    _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))
    replayed = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"), "--replay")
    after = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert [e["identity"] for e in _watch_events(replayed)] == ["head:c0404f4d round:1"]
    assert replayed.exit_code == 1, replayed.output
    # A replay still records what it said: the next plain watch is quiet again.
    assert _watch_events(after) == []
    assert after.exit_code == 0, after.output


def test_watch_snapshot_neither_reads_nor_writes_the_follow_memory(
    tmp_path: Path,
) -> None:
    """A snapshot `kew epic watch` is a probe an operator (or a cron) reads on
    every run — the memory is `--follow`'s, so a snapshot never goes silent and
    never silences the follow that comes after it."""
    snapshot = _invoke_watch(tmp_path, [_ready_at("head:c0404f4d round:1")], "--json")
    again = _invoke_watch(tmp_path, [_ready_at("head:c0404f4d round:1")], "--json")
    followed = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert len(_watch_events(snapshot)) == 1
    assert len(_watch_events(again)) == 1
    assert len(_watch_events(followed)) == 1


def test_watch_follow_replays_an_unreadable_memory_and_says_so(
    tmp_path: Path,
) -> None:
    """A corrupt state file must not crash the watch or silence an open state:
    it is read as NO memory, out loud."""
    _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))
    path = watch_memory_path(_config(tmp_path), EPIC)
    path.write_text("{not json", encoding="utf-8")

    invoked = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert invoked.exit_code == 1, invoked.output
    assert len(_watch_events(invoked)) == 1
    assert "unreadable watch memory" in invoked.output


def _write_memory(tmp_path: Path, document: Any) -> Path:
    """Put `document` where the next `--follow` watch will read its memory."""
    path = watch_memory_path(_config(tmp_path), EPIC)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("document", "said"),
    [
        (
            {"version": MEMORY_VERSION + 1, "epic": EPIC, "items": {}},
            "memory version",
        ),
        (
            {"version": MEMORY_VERSION, "epic": "another-epic", "items": {}},
            "belongs to epic",
        ),
        (
            {"version": MEMORY_VERSION, "epic": EPIC, "items": ["77"]},
            "records are missing or not a table",
        ),
        (["not", "a", "document"], "not a memory document"),
        # A file holding JSON `null` parsed to None, which read as "absent" —
        # so it replayed every open state with nothing on stderr to say why.
        (None, "not a memory document"),
    ],
)
def test_watch_follow_says_why_it_rejected_the_memory_it_replays(
    tmp_path: Path, document: Any, said: str
) -> None:
    """Only corrupt JSON used to say so: a memory rejected for a foreign epic, a
    different `MEMORY_VERSION` or a shape that is not a record table replayed in
    silence, which reads as a watch that lost its memory for no reason."""
    _write_memory(tmp_path, document)

    invoked = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert len(_watch_events(invoked)) == 1
    assert invoked.exit_code == 1, invoked.output
    assert "ignoring the watch memory" in invoked.output
    assert said in invoked.output


def test_watch_follow_says_which_memory_records_it_could_not_read(
    tmp_path: Path,
) -> None:
    """A record missing a field replays its own item — out loud, or that item's
    open state repeats on every restart with nothing to explain it."""
    _write_memory(
        tmp_path,
        {
            "version": MEMORY_VERSION,
            "epic": EPIC,
            "items": {"77": {"kind": "merge_ready"}},
        },
    )

    invoked = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert len(_watch_events(invoked)) == 1
    assert invoked.exit_code == 1, invoked.output
    assert "1 unreadable watch memory record" in invoked.output


def test_watch_follow_reads_a_whole_memory_without_a_word(tmp_path: Path) -> None:
    """The loud paths must stay rare: a memory this watch CAN read is silent
    about itself, or every quiet restart carries a warning an operator learns to
    ignore."""
    _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    again = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert _watch_events(again) == []
    assert "watch memory" not in again.output


@pytest.mark.parametrize("lock_path", [None, "epic-driver.lock", "epic-watch.json"])
def test_watch_memory_lives_beside_the_epic_lock_and_is_never_the_lock(
    tmp_path: Path, lock_path: str | None
) -> None:
    """One per-epic state directory: the memory sits next to the epic's driver
    lock and is keyed by the same per-epic filename, so two epics never share a
    memory — and it is never the lock FILE. A fixed `epic-watch-<slug>.json`
    made `[epic] lock_path = ".../epic-watch.json"` name the very same path, and
    every poll's `os.replace` then swapped the held lock's directory entry out
    from under a live driver."""
    config = _config(tmp_path)
    if lock_path is not None:
        config.epic.lock_path = str(tmp_path / lock_path)
    lock = _epic_lock_path(config, EPIC)
    path = watch_memory_path(config, EPIC)

    assert path.parent == lock.parent
    assert path != lock
    assert path.name.startswith(lock.name)
    assert watch_memory_path(config, "other") != path


def test_watch_follow_replays_an_event_whose_emit_never_made_it_out(
    tmp_path: Path,
) -> None:
    """The memory is committed AFTER the emit. A dead `--json` consumer makes
    the print raise, and a memory written first recorded that event as already
    said — the restarted watch was then silent, and exited 0, about an item
    still waiting on a person."""
    with patch(
        "kew.cli.epic._emit_attention", side_effect=BrokenPipeError("consumer gone")
    ):
        broken = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))
    assert broken.exit_code != 0
    assert not watch_memory_path(_config(tmp_path), EPIC).exists()

    restarted = _follow_once(tmp_path, _ready_at("head:c0404f4d round:1"))

    assert [e["identity"] for e in _watch_events(restarted)] == [
        "head:c0404f4d round:1"
    ]
    assert restarted.exit_code == 1, restarted.output


def test_advance_records_the_configured_expected_cadence(tmp_path: Path) -> None:
    """#2297: a one-shot advance recorded a NULL interval, so `driver_liveness`
    could never say "gone". It now records the configured EXPECTATION, weakly —
    `interval_s=None` keeps it from overwriting a poller's real cadence."""
    cfg = _config(tmp_path)
    cfg.epic.driver_expected_interval_seconds = 120
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.advance", return_value=_tick_result()) as advance_mock,
    ):
        invoked = runner.invoke(epic_app, ["advance", EPIC])

    assert invoked.exit_code == 0, invoked.stdout
    assert advance_mock.call_args.kwargs["expected_interval_s"] == 120.0
    assert advance_mock.call_args.kwargs["interval_s"] is None


def test_tick_log_names_priority_and_blast_for_each_dispatched_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2575: the dispatched line says why the frontier chose what it did."""
    from kew.cli import epic as epic_cmds

    lines: list[str] = []
    monkeypatch.setattr(epic_cmds, "_log", lines.append)
    result = TickResult(
        dispatched=("2540", "2570"),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=2,
        signal=TerminalSignal.PROGRESSING,
        dispatch_ranks=(
            FrontierRank(node_id="2570", priority=1, blast=0),
            FrontierRank(node_id="2540", priority=2, blast=2),
        ),
    )

    epic_cmds._print_tick_categories(result)

    assert lines == [
        "  dispatched: ['2540', '2570'] (#2570 p1, blast 0; #2540 p2, blast 2)"
    ]
